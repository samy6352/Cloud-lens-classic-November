"""Keep a dated copy of every dataset the warehouse loads.

`exports.py` reads Cost Management's own exports and states in its opening paragraph that
nothing in it writes to storage. That guarantee is worth keeping, so the write lives here --
the same reason `budgets.py` is separate from `cost.py`.

What this is for: the warehouse is a working set, not a record. A refresh *replaces* the
periods it loads, so yesterday's numbers are gone the moment today's arrive -- and Azure's own
cost data is restated for several days after the fact. Without a copy taken at the time, the
question "what did we think this cost when we reported it?" has no answer.

Three decisions shape the naming:

  * **One blob per dataset per day.** The name is derived from the dataset and today's date
    and nothing else, so a second refresh on the same day overwrites the first rather than
    accumulating near-identical copies. Someone pressing Refresh four times in an afternoon is
    correcting something, not asking for four archives.

  * **The date is the day the archive was taken**, not the last day of data in it. Cost data is
    restated, so two archives taken a week apart can cover the same period and differ -- which
    is exactly the fact this archive exists to preserve. Naming by data coverage would make the
    second one overwrite the first and destroy it.

  * **Parquet, not CSV.** It is the format FOCUS exports already use, `exports.py` can read it
    back, and it is roughly a tenth the size -- which matters for a file written every day and
    kept indefinitely.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import threading
import time
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4
from xml.etree import ElementTree

import httpx

log = logging.getLogger("cloudlens.archive")

BLOB_VERSION = "2021-12-02"
BLOB_RESOURCE = "https://storage.azure.com"

# Where archives go, and where scheduled exports this deployment creates are told to write.
# Both name the same account; the resource id is what Cost Management needs and the bare name
# is what the blob endpoint needs, so the deployment supplies whichever it has and the other
# is derived. There is deliberately no built-in default account name: one used to be hardcoded
# here, which meant a fresh deployment silently pointed at a storage account in somebody else's
# resource group -- reachable from nowhere, and impossible to spot from the app settings.
ACCOUNT_ID = os.getenv("ARCHIVE_ACCOUNT_ID", "").strip()
ACCOUNT = (os.getenv("ARCHIVE_ACCOUNT", "").strip()
           or (ACCOUNT_ID.rsplit("/", 1)[-1] if ACCOUNT_ID else ""))
CONTAINER = os.getenv("ARCHIVE_CONTAINER", "cloudlens").strip()

# Region stamped on exports created by this deployment. An export that carries an identity must
# name a real region, and it should be the one the stack was deployed into.
LOCATION = os.getenv("ARCHIVE_LOCATION", "centralindia").strip() or "centralindia"

# A refresh that loaded nothing must not overwrite a good archive with an empty one.
MIN_ROWS = 1

# Rows held in memory at once when streaming a result set out to a file. The archive can be
# millions of rows and the app runs on a small instance, so it is never materialised whole.
_ROW_BATCH = 50000
_UPLOAD_CHUNK = 4 * 1024 * 1024
_LIST_MAX_PAGES = 100

# The datasets a refresh can produce. The key is what the UI asks for; the value is the folder.
DATASETS = {
    "FocusCost": "focus",
    "AmortizedCost": "amortized",
    "ActualCost": "actual",
}

_SAFE = re.compile(r"[^a-z0-9._-]+")


class ArchiveError(RuntimeError):
    """Something went wrong writing the archive, phrased for whoever pressed Refresh."""


async def _join_cancelled_worker(task: asyncio.Task) -> None:
    """Repeated cancellation must not release files still owned by a worker thread."""
    joined = asyncio.gather(task, return_exceptions=True)
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            continue
    joined.result()


def enabled() -> bool:
    """Off when no account is configured, so a deployment without storage still refreshes."""
    return bool(ACCOUNT and CONTAINER)


def dataset_folder(metric: str) -> str:
    """Folder for a metric. Unknown metrics get a sanitised folder rather than an error --
    failing an archive because a new metric appeared would block the refresh that produced it."""
    if metric in DATASETS:
        return DATASETS[metric]
    return _SAFE.sub("-", (metric or "dataset").lower()).strip("-") or "dataset"


def blob_name(metric: str, when: date | None = None) -> str:
    """`focus/2026/08/focus-2026-08-27.parquet`.

    Foldered by year and month because a container with a thousand flat files is unusable in
    the portal, and because lifecycle rules to tier or expire old data are written against
    prefixes.
    """
    day = when or datetime.now(UTC).date()
    folder = dataset_folder(metric)
    return f"{folder}/{day.year:04d}/{day.month:02d}/{folder}-{day.isoformat()}.parquet"


class BlobWriter:
    """The smallest client that can put a blob with a managed identity.

    Deliberately not the azure-storage-blob SDK: `exports.py` already talks to the blob REST
    API directly with httpx, and adding a second, heavier way to reach the same service would
    mean two connection pools, two auth paths and two sets of failure modes to reason about.
    """

    def __init__(self, account: str = ACCOUNT, container: str = CONTAINER) -> None:
        self.account = account
        self.container = container
        self._token: str | None = None
        self._credential: Any = None

    async def _headers(self) -> dict[str, str]:
        if self._token is None:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            got = await self._credential.get_token(f"{BLOB_RESOURCE}/.default")
            self._token = got.token
        return {"x-ms-version": BLOB_VERSION, "Authorization": "Bearer " + str(self._token)}

    def _url(self, path: str = "", query: str = "") -> str:
        base = f"https://{self.account}.blob.core.windows.net/{self.container}"
        if path:
            base += "/" + path.lstrip("/")
        return base + (f"?{query}" if query else "")

    async def close(self) -> None:
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def ensure_container(self, client: httpx.AsyncClient) -> None:
        """Create the container if missing. 409 means it already exists, which is success."""
        r = await client.put(self._url(query="restype=container"), headers=await self._headers())
        if r.status_code in (201, 409):
            return
        raise ArchiveError(_explain(r.status_code, r.text, self.account))

    async def put(self, name: str, data: bytes, content_type: str) -> dict[str, Any]:
        """Write one blob, replacing any blob of the same name.

        A plain PUT is an overwrite, which is the whole mechanism behind "max one a day": the
        name carries the date, so the second run of the day lands on the first.
        """
        headers = {
            **(await self._headers()),
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
        }
        async with httpx.AsyncClient(timeout=300) as client:
            await self.ensure_container(client)
            r = await client.put(self._url(name), headers=headers, content=data)
            if r.status_code not in (201, 202):
                raise ArchiveError(_explain(r.status_code, r.text, self.account))
            return {
                "name": name,
                "bytes": len(data),
                "url": self._url(name),
                "etag": r.headers.get("ETag"),
                "written": datetime.now(UTC).isoformat(timespec="seconds"),
            }

    async def put_file(self, name: str, path: Path, content_type: str) -> dict[str, Any]:
        """Stage bounded blocks; publish only after the entire file was uploaded."""
        size = path.stat().st_size
        if size <= 0 or (size + _UPLOAD_CHUNK - 1) // _UPLOAD_CHUNK > 50000:
            raise ArchiveError("Archive file is empty or exceeds the 50,000-block upload limit.")
        prefix = uuid4().hex
        blocks = ElementTree.Element("BlockList")
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=300) as client:
            await self.ensure_container(client)
            with path.open("rb") as source:
                uploaded = 0
                index = 0
                while True:
                    read = asyncio.create_task(asyncio.to_thread(source.read, _UPLOAD_CHUNK))
                    try:
                        chunk = await asyncio.shield(read)
                    except asyncio.CancelledError:
                        await _join_cancelled_worker(read)
                        raise
                    if not chunk:
                        break
                    block = base64.b64encode(f"{prefix}-{index:06d}".encode("ascii")).decode("ascii")
                    r = await client.put(self._url(name, urlencode({"comp": "block", "blockid": block})),
                                         headers=headers, content=chunk)
                    if r.status_code != 201:
                        raise ArchiveError(_explain(r.status_code, r.text, self.account))
                    ElementTree.SubElement(blocks, "Latest").text = block
                    uploaded += len(chunk)
                    index += 1
                    _job_progress(phase="uploading", uploaded_bytes=uploaded, total_bytes=size)
            if uploaded != size:
                raise ArchiveError("Archive file changed during upload; the previous blob was not replaced.")
            from . import jobs
            if jobs.current_job.get():
                _job_progress(phase="publishing")
                def fence():
                    with jobs.transaction() as con:
                        jobs.assert_lease(con)
                        record = jobs._read(con, "job:" + jobs.current_job.get())
                        if record.get("cancel_requested"):
                            raise ArchiveError("Archive cancelled before publication.")
                        record["progress"] = {**record.get("progress", {}), "phase": "publishing"}
                        jobs._put(con, "job:" + record["job_id"], record)
                        lease = jobs._read(con, "jobs:lease")
                        clock = float(con.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()[0])
                        lease["expires"] = clock + jobs.LEASE_SECONDS
                        jobs._put(con, "jobs:lease", lease)
                await asyncio.to_thread(fence)
            async def publish():
                # Shorter than the refreshed writer lease; shutdown must observe the outcome.
                async with asyncio.timeout(60):
                    return await client.put(self._url(name, "comp=blocklist"),
                                            headers={**headers, "Content-Type": "application/xml",
                                                     "x-ms-blob-content-type": content_type},
                                            content=ElementTree.tostring(blocks, encoding="utf-8"))
            publication = asyncio.create_task(publish())
            r = await jobs._settle(publication)
            if r.status_code != 201:
                raise ArchiveError(_explain(r.status_code, r.text, self.account))
            return {"name": name, "bytes": uploaded, "url": self._url(name),
                    "etag": r.headers.get("ETag"),
                    "written": datetime.now(UTC).isoformat(timespec="seconds")}

    async def get(self, name: str) -> bytes:
        """One blob's bytes. Used to read an archived snapshot back for comparison."""
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(self._url(name), headers=await self._headers())
            if r.status_code == 404:
                raise ArchiveError(f"No archive at {name}.")
            if r.status_code != 200:
                raise ArchiveError(_explain(r.status_code, r.text, self.account))
            return r.content

    async def list(self, prefix: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Blobs under a prefix, newest first. Used to show what the archive already holds."""
        limit = max(1, min(limit, 1000))
        out: list[dict[str, Any]] = []
        marker = ""
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=90) as client:
            for _ in range(_LIST_MAX_PAGES):
                q = urlencode({"restype": "container", "comp": "list", "maxresults": 1000,
                               "prefix": prefix, "marker": marker})
                r = await client.get(self._url(query=q), headers=await self._headers())
                if r.status_code != 200:
                    raise ArchiveError(_explain(r.status_code, r.text, self.account))
                try:
                    page = ElementTree.fromstring(r.text)
                    if page.tag != "EnumerationResults" or page.find("Blobs") is None:
                        raise ArchiveError("Storage returned an invalid archive listing.")
                    blobs = page.findall("./Blobs/Blob")
                    if len(blobs) > 1000:
                        raise ArchiveError("Archive listing exceeded the page size limit.")
                    for blob in blobs:
                        out.append({
                            "name": blob.findtext("Name"),
                            "size": int(blob.findtext("Properties/Content-Length") or 0),
                            "modified": blob.findtext("Properties/Last-Modified"),
                        })
                except (ElementTree.ParseError, ValueError) as exc:
                    raise ArchiveError("Storage returned an invalid archive listing.") from exc
                out.sort(key=_newest_blob, reverse=True)
                del out[limit:]
                marker = page.findtext("NextMarker") or ""
                if not marker:
                    return out
                if marker in seen:
                    raise ArchiveError("Storage repeated an archive continuation marker.")
                seen.add(marker)
        raise ArchiveError("Archive listing exceeded its scan limit; narrow the prefix to find latest files.")


def _newest_blob(blob: dict[str, Any]) -> tuple[str, str]:
    name = blob["name"] or ""
    day = re.search(r"-(\d{4}-\d{2}-\d{2})(?:[.-]|$)", name)
    if day:
        return day.group(1), name
    modified = blob.get("modified")
    if modified:
        try:
            return parsedate_to_datetime(modified).astimezone(UTC).isoformat(), name
        except (TypeError, ValueError) as exc:
            raise ArchiveError("Storage returned an invalid archive modification date.") from exc
    return "", name


def _tag(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1) if m else None


def _explain(status: int, body: str, account: str) -> str:
    """Turn the failures that actually happen into something someone can act on.

    The 403 here is nearly always the network, not the role: in a tenant that forces
    `publicNetworkAccess=Disabled` the blob endpoint refuses before RBAC is even consulted, and
    telling someone to check their permissions sends them to look at the one thing that is
    already correct.
    """
    if status == 403:
        return (
            f"Storage account '{account}' refused the write. If this app has Storage Blob Data "
            "Contributor, the cause is the network rather than the role: the account has public "
            "access disabled and needs a private endpoint reachable from the app's VNet."
        )
    if status == 404:
        return f"Container not found on '{account}', and it could not be created."
    if status == 401:
        return f"Not authorized to write to '{account}'. It needs Storage Blob Data Contributor."
    return f"Storage returned {status}: {body[:200]}"


def _where(scope: list[str] | None, since: str | None, until: str | None) -> str:
    """The row filter shared by every export path.

    Subscription ids and dates are the only things interpolated, and both are validated by the
    caller before they get here — ids come from the scope the person is already permitted to
    see, dates are checked against ISO format. Neither reaches SQL unexamined.
    """
    clauses: list[str] = []
    if scope:
        ids = ", ".join(f"'{s}'" for s in scope)
        clauses.append(f'("SubAccountId" IN ({ids}) OR "SubAccountName" IN ({ids}))')
    if since:
        clauses.append(f"\"ChargePeriodStart\" >= DATE '{since}'")
    if until:
        clauses.append(f"\"ChargePeriodStart\" <= DATE '{until}'")
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def _check_day(value: str | None, label: str) -> str | None:
    """A date, or a refusal. Interpolated into SQL, so it is never taken on trust."""
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ArchiveError(f"{label} must be a date like 2026-08-01.") from None
    return text


def _write_rows(destination: Path, fmt: str, *, source_sql: str,
                scope: list[str] | None, since: str | None, until: str | None,
                con: Any) -> tuple[int, int]:
    """Copy the selected rows out of the warehouse, in the requested format.

    Written by streaming the result set through Python rather than with the engine's own
    `COPY ... TO '<file>'`. PostgreSQL reserves writing
    to a server-side file to superusers -- which nobody is on Azure Flexible Server -- and
    has no Parquet format at all, so archiving silently could not work on the deployed
    backend. Fetching in batches keeps the memory profile of a native COPY: only
    one batch is resident at a time, never the whole table.
    """
    where = _where(scope, since, until)
    rows = con.execute(f"SELECT count(*) FROM {source_sql}{where}").fetchone()[0]
    if rows < MIN_ROWS:
        return 0, 0

    cur = con.execute(
        f'SELECT * FROM {source_sql}{where} ORDER BY "ChargePeriodStart"'
    )
    columns = [d[0] for d in cur.description]
    _write_batches(destination, fmt, columns, _cursor_batches(cur))
    return rows, destination.stat().st_size


def _archive_limits() -> tuple[int, int]:
    values = []
    for name, default in (("ARCHIVE_QUERY_TIMEOUT_SECONDS", 300),
                          ("ARCHIVE_EXPORT_TIMEOUT_SECONDS", 3600)):
        raw = os.getenv(name, str(default)).strip()
        if not raw.isdecimal() or not 1 <= int(raw) <= 86400:
            raise ArchiveError(f"{name} must be an integer between 1 and 86400 seconds.")
        values.append(int(raw))
    return values[0], values[1]


def _job_progress(**values: Any) -> None:
    from . import jobs
    progress = jobs.current_progress.get()
    if progress is not None:
        progress.update(values)


def _write_archive(destination: Path, fmt: str, con: Any,
                   cancel: threading.Event | None = None) -> tuple[int, int]:
    """One cursor snapshot, counted while writing, without a whole-estate sort."""
    import pyarrow as pa

    from .warehouse import NUMERIC

    query_timeout, export_timeout = _archive_limits()
    deadline = time.monotonic() + export_timeout
    rows = 0
    with con.raw.transaction(), con.raw.cursor(name=f"archive_{uuid4().hex}") as cur:
        con.execute("SELECT set_config('statement_timeout', ?, true)", [f"{query_timeout}s"])
        cur.execute("SELECT * FROM costs")
        columns = [d.name for d in cur.description]
        schema = pa.schema([(name, pa.float64() if name in NUMERIC else
                             pa.date32() if name == "ChargePeriodStart" else pa.string())
                            for name in columns])

        def batches():
            nonlocal rows
            while True:
                if cancel is not None and cancel.is_set():
                    raise ArchiveError("Archive extraction was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ArchiveError("Archive extraction exceeded its total time limit.")
                con.execute("SELECT set_config('statement_timeout', ?, true)",
                            [str(max(1, int(min(query_timeout, remaining) * 1000)))])
                batch = cur.fetchmany(_ROW_BATCH)
                if not batch:
                    break
                rows += len(batch)
                yield batch
                _job_progress(phase="extracting", rows=rows)
            if time.monotonic() > deadline:
                raise ArchiveError("Archive extraction exceeded its total time limit.")

        _write_batches(destination, fmt, columns, batches(), schema=schema)
    return (rows, destination.stat().st_size) if rows else (0, 0)


def _write_snapshot(destination: Path, fmt: str, raw: bytes, *,
                    scope: list[str] | None, since: str | None,
                    until: str | None) -> tuple[int, int]:
    """The same, from an archived Parquet snapshot instead of the warehouse.

    The filters are applied in Arrow rather than SQL. That keeps the selection honest -- a
    snapshot export and a live one narrow by the same fields -- without needing a query
    engine present just to read a file back.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(raw))

    if scope:
        wanted = set(scope)
        keep = None
        for column in ("SubAccountId", "SubAccountName"):
            if column not in table.column_names:
                continue
            match = pc.is_in(table[column], value_set=pa_array(wanted))
            keep = match if keep is None else pc.or_(keep, match)
        if keep is not None:
            table = table.filter(pc.fill_null(keep, False))

    start = "ChargePeriodStart"
    if since and start in table.column_names:
        table = table.filter(pc.fill_null(
            pc.greater_equal(table[start], _as_day(table[start], since)), False))
    if until and start in table.column_names:
        table = table.filter(pc.fill_null(
            pc.less_equal(table[start], _as_day(table[start], until)), False))

    if table.num_rows < MIN_ROWS:
        return 0, 0

    if start in table.column_names:
        table = table.sort_by([(start, "ascending")])

    _write_batches(destination, fmt, list(table.column_names),
                   _table_batches(table))
    return table.num_rows, destination.stat().st_size


def pa_array(values: set[str]) -> Any:
    """The scope as an Arrow array, for `is_in`."""
    import pyarrow as pa

    return pa.array(sorted(values), type=pa.string())


def _as_day(column: Any, day: str) -> Any:
    """`day` as something comparable with `column`.

    An archived ChargePeriodStart may be a date, a timestamp or -- in older archives -- a
    string. Comparing a date scalar against a string column raises rather than returning
    nothing, so the bound is built to match whatever the file actually holds.
    """
    import pyarrow as pa

    if pa.types.is_string(column.type):
        return pa.scalar(day)
    return pa.scalar(date.fromisoformat(day)).cast(column.type)


def _cursor_batches(cur: Any) -> Any:
    """Row batches from a database cursor."""
    while True:
        batch = cur.fetchmany(_ROW_BATCH)
        if not batch:
            break
        yield batch


def _table_batches(table: Any) -> Any:
    """Row batches from an Arrow table, in the same shape a cursor yields."""
    for batch in table.to_batches(max_chunksize=_ROW_BATCH):
        columns = [col.to_pylist() for col in batch.columns]
        yield list(zip(*columns, strict=True))


def _write_batches(destination: Path, fmt: str, columns: list[str], batches: Any,
                   *, schema: Any = None) -> None:
    if fmt == "parquet":
        _stream_parquet(destination, columns, batches, schema=schema)
    else:
        _stream_csv(destination, columns, batches)


def _stream_parquet(destination: Path, columns: list[str], batches: Any,
                    *, schema: Any = None) -> None:
    """Write row batches to Parquet, one batch at a time.

    Unless the warehouse's stable schema is supplied, it is taken from the first batch, so a column
    whose Python type depends on how the driver decoded it -- `Decimal` for a numeric,
    `float` for a double -- is described by what actually arrived instead of by an
    assumption. ZSTD matches what earlier archives used, so they stay the same size.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer: Any = None
    try:
        for batch in batches:
            table = pa.Table.from_pydict(
                {name: [row[i] for row in batch] for i, name in enumerate(columns)},
                schema=schema,
            )
            if writer is None:
                writer = pq.ParquetWriter(destination, table.schema, compression="zstd")
            else:
                # A later batch that is entirely NULL where the first had values arrives
                # as a null-typed column; casting keeps the file's schema single.
                table = table.cast(writer.schema)
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()


def _stream_csv(destination: Path, columns: list[str], batches: Any) -> None:
    """Spreadsheet-safe CSV; Parquet retains exact source strings."""
    from .spreadsheet import SafeCsvWriter

    with destination.open("w", newline="", encoding="utf-8") as fh:
        out = SafeCsvWriter(fh)
        out.writerow(columns)
        for batch in batches:
            out.writerows(
                ["" if v is None else v for v in row] for row in batch
            )


def _export_parquet(destination: Path, scope: list[str] | None = None,
                    cancel: threading.Event | None = None) -> tuple[int, int]:
    """Write the warehouse's cost rows to a Parquet file. Returns (rows, bytes).

    Streamed in batches through pyarrow rather than materialised in Python, which matters
    because the warehouse can hold millions of rows and the app runs on a small instance.
    PostgreSQL has no Parquet format of its own, so the encoding happens here.
    """
    from .warehouse import warehouse

    con = warehouse.connect(read_only=True, scope=scope)
    try:
        return _write_archive(destination, "parquet", con, cancel)
    finally:
        con.close()


def _export_csv(destination: Path, scope: list[str] | None = None,
                cancel: threading.Event | None = None) -> tuple[int, int]:
    """The same rows as CSV, in the shape a Cost Management export writes.

    Why bother, when Parquet is a tenth the size: because a file that Azure's own export could
    have produced is one that every downstream thing already understands. `exports.py` reads it
    back, FinOps tooling ingests it, and it opens in Excel. The archive keeps Parquet for its
    own use; this exists for handing to someone else.

    The column order is `warehouse.COLUMNS`, which is FOCUS naming — the same header a FOCUS
    export produces, so the two are interchangeable.
    """
    from .warehouse import warehouse

    con = warehouse.connect(read_only=True, scope=scope)
    try:
        return _write_archive(destination, "csv", con, cancel)
    finally:
        con.close()


# What a scoped export can be written as. Parquet for the archive's own comparisons, CSV for
# handing to anything that expects what Azure's export produces.
FORMATS = {
    "parquet": (_export_parquet, ".parquet", "application/vnd.apache.parquet"),
    "csv": (_export_csv, ".csv", "text/csv"),
}


async def archive_current(metric: str, *, when: date | None = None,
                          source: str | None = None,
                          scope: list[str] | None = None,
                          suffix: str | None = None,
                          fmt: str = "parquet") -> dict[str, Any]:
    """Write today's copy of the warehouse for this dataset.

    Called after a refresh finishes. Returns a description of what was written; raises
    ArchiveError with a sentence if it could not be.

    `scope` narrows it to a set of subscriptions, and `suffix` distinguishes the resulting blob
    from the daily whole-estate one — a scoped export is a different artefact answering a
    different question, so it must not overwrite the archive that exists to preserve the whole.
    """
    if not enabled():
        raise ArchiveError("No archive account is configured (ARCHIVE_ACCOUNT).")
    if fmt not in FORMATS:
        raise ArchiveError(f"Unknown format {fmt!r}. Use {' or '.join(FORMATS)}.")

    write, ext, content_type = FORMATS[fmt]
    name = blob_name(metric, when)
    if suffix:
        name = name.replace(".parquet", f"-{_SAFE.sub('-', suffix.lower()).strip('-')}.parquet")
    if ext != ".parquet":
        name = name.replace(".parquet", ext)

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / f"archive{ext}"
        cancel = threading.Event()
        extraction = asyncio.create_task(asyncio.to_thread(write, path, scope=scope, cancel=cancel))
        try:
            rows, size = await asyncio.shield(extraction)
        except asyncio.CancelledError:
            # The worker owns the open file and cursor; join it before deleting its directory.
            cancel.set()
            await _join_cancelled_worker(extraction)
            raise
        if not rows:
            raise ArchiveError(
                "The warehouse is empty for that selection, so there is nothing to export."
                if scope else
                "The warehouse is empty, so there is nothing to archive. The refresh that "
                "triggered this loaded no rows."
            )
        _job_progress(phase="uploading", rows=rows, total_bytes=size, uploaded_bytes=0)
        writer = BlobWriter()
        try:
            async with asyncio.timeout(_archive_limits()[1]):
                written = await writer.put_file(name, path, content_type)
        finally:
            await writer.close()

    log.info("archived %s rows to %s/%s/%s", rows, ACCOUNT, CONTAINER, name)
    return {
        **written,
        "rows": rows,
        "metric": metric,
        "format": fmt,
        "dataset": dataset_folder(metric),
        "account": ACCOUNT,
        "container": CONTAINER,
        "source": source,
        "scope": scope,
        "compressed_bytes": size,
    }


async def status(limit: int = 20) -> dict[str, Any]:
    """What the archive holds, and whether it can be reached at all.

    `reachable` is reported separately from the file list because the two failures need
    different actions: an empty archive means no refresh has run yet, while an unreachable one
    means the network or the role is wrong and no refresh will ever populate it.
    """
    base = {"enabled": enabled(), "account": ACCOUNT, "container": CONTAINER}
    if not enabled():
        return {**base, "reachable": False, "error": "No archive account configured.", "files": []}

    writer = BlobWriter()
    try:
        files = await writer.list(limit=limit)
    except Exception as exc:  # noqa: BLE001 - the reason is the point of this endpoint
        return {**base, "reachable": False, "error": str(exc)[:300], "files": []}
    finally:
        await writer.close()

    latest: dict[str, dict[str, Any]] = {}
    for f in files:
        folder = (f["name"] or "").split("/")[0]
        if folder not in latest:
            latest[folder] = f
    # Which days each dataset has, so a comparison can offer real choices rather than a date
    # picker that mostly points at days with nothing behind them.
    days = {d: _dataset_days(files, d) for d in {(f["name"] or "").split("/")[0] for f in files}}
    return {
        **base,
        "reachable": True,
        "count": len(files),
        "files": files,
        "latest": latest,
        "days": {k: v for k, v in days.items() if v},
        "comparable": sorted(k for k, v in days.items() if len(v) > 1),
        "today": blob_name("FocusCost").rsplit("/", 1)[-1],
    }


# --------------------------------------------------------------- comparing two days
#
# Restatement is the point. Azure revises cost data for days after the fact, so the same period
# read a week apart is not the same number -- and the warehouse only ever holds the latest read.
# Two archives are the only way to see that movement, which is why they are kept.


def _dataset_days(files: list[dict[str, Any]], dataset: str) -> list[str]:
    """The dates available for one dataset, newest first.

    The day has to parse as a real date, not merely look like one. Without that check the
    scheduled-export folder leaked in: `exports/.../000001.csv` has a ten-character stem, which
    a length test happily accepted and offered as a snapshot from the year 0000.
    """
    out = []
    for f in files:
        name = f.get("name") or ""
        if not name.startswith(f"{dataset}/"):
            continue
        stem = name.rsplit("/", 1)[-1]
        day = stem.replace(f"{dataset}-", "").replace(".parquet", "")
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        out.append(day)
    return sorted(set(out), reverse=True)


async def compare(dataset: str, earlier: str, later: str,
                  group_by: str = "ServiceName", top: int = 15,
                  scope: list[str] | None = None) -> dict[str, Any]:
    """What changed between two archived snapshots of the same dataset.

    Both files are aggregated column-wise in Arrow rather than row-wise in Python: they are
    columnar and compressed, and the whole question is one grouped sum per file. A comparison
    that materialised two months of rows as Python objects would be the one thing on a 1.75 GB
    instance that could not survive a large estate.

    The join is done on the grouped results -- at most a few thousand keys -- so the full outer
    join stays exact while never touching the underlying rows.

    The two figures are not "before and after a change we made". They are what Azure said on two
    different days about *the same period* -- so a difference is a restatement, and that is
    exactly what nobody can see from the dashboard alone.
    """
    if not enabled():
        raise ArchiveError("No archive account is configured.")
    if scope == []:
        raise ArchiveError("No subscriptions are authorized for this comparison.")
    for day in (earlier, later):
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            raise ArchiveError("Snapshot days must be dates like 2026-08-01.") from None
    if earlier == later:
        raise ArchiveError("Pick two different days to compare.")
    # Validated before anything is downloaded. Restricting the column keeps the comparison to
    # dimensions the dashboard already reasons about — doing it after two blob reads just means
    # a rejected request costs a megabyte of transfer and several seconds first.
    if group_by not in _COMPARE_COLUMNS:
        raise ArchiveError(f"Cannot group by {group_by!r}.")
    # Order them rather than trusting the caller: reversed inputs would report every increase as
    # a decrease, which is worse than an error because it looks like an answer.
    earlier, later = sorted([earlier, later])

    folder = dataset_folder(dataset)
    writer = BlobWriter()
    blobs: dict[str, bytes] = {}
    try:
        for day in (earlier, later):
            y, m = day[:4], day[5:7]
            name = f"{folder}/{y}/{m}/{folder}-{day}.parquet"
            try:
                blobs[day] = await writer.get(name)
            except ArchiveError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ArchiveError(f"Could not read the {day} archive: {str(exc)[:160]}") from exc
    finally:
        await writer.close()

    def aggregate() -> tuple[dict, dict, dict, dict]:
        import pyarrow as pa
        import pyarrow.compute as pc

        tables = {day: _read_snapshot(raw, scope) for day, raw in blobs.items()}
        full = {day: _snapshot_totals(table) for day, table in tables.items()}
        scoped_keys = all("SubAccountId" in table.column_names for table in tables.values())
        keys = {}
        key_sets = {}
        for day, table in tables.items():
            dates = pc.utf8_slice_codeunits(pc.cast(table["ChargePeriodStart"], pa.string()), 0, 10)
            if dates.null_count:
                raise ArchiveError("Snapshot usage dates are incomplete; restatements cannot be established.")
            if scoped_keys:
                subs = pc.utf8_lower(pc.fill_null(pc.cast(table["SubAccountId"], pa.string()), ""))
                keys[day] = pc.binary_join_element_wise(subs, dates, "|")
            else:
                keys[day] = dates
            key_sets[day] = set(pc.unique(keys[day]).to_pylist())
        common = key_sets[earlier] & key_sets[later]
        added, removed = key_sets[later] - common, key_sets[earlier] - common

        def select(day: str, wanted: set[str]) -> Any:
            return tables[day].filter(pc.is_in(keys[day], value_set=pa.array(sorted(wanted), type=pa.string())))

        matched = {day: select(day, common) for day in tables}
        dates = sorted({key.rsplit("|", 1)[-1] for key in common})
        coverage = {
            "comparison_period": {"from": dates[0], "to": dates[-1], "days": len(dates)}
                                 if dates else None,
            "coverage_basis": "subscription_day" if scoped_keys else "usage_day",
            "coverage_changed": bool(added or removed),
            "coverage_added": {**_snapshot_totals(select(later, added)), "coverage_keys": len(added)},
            "coverage_removed": {**_snapshot_totals(select(earlier, removed)), "coverage_keys": len(removed)},
        }
        return (
            {day: _snapshot_totals(table) for day, table in matched.items()},
            {day: _grouped_cost(table, group_by) for day, table in matched.items()},
            full, coverage)

    totals, grouped, full, coverage = await asyncio.to_thread(aggregate)
    currencies = {v["currency"] for v in full.values() if v["rows"]}
    if len(currencies) > 1:
        raise ArchiveError("The snapshots use different currencies and cannot be compared as one total.")

    # A full outer join, so a thing that appeared or vanished between the two reads is
    # reported rather than silently dropped -- an inner join would hide exactly the
    # movements worth looking at.
    a, b = grouped[earlier], grouped[later]
    changes = []
    for key in a.keys() | b.keys():
        was, now = a.get(key, 0.0), b.get(key, 0.0)
        delta = now - was
        if abs(delta) <= 0.005:
            continue
        changes.append({
            "key": key,
            "was": round(was, 2),
            "now": round(now, 2),
            "delta": round(delta, 2),
            "percent": round((delta / was) * 100, 1) if was else None,
        })
    changes.sort(key=lambda c: abs(c["delta"]), reverse=True)
    changes = changes[: int(top)]

    was, now = totals[earlier]["cost"], totals[later]["cost"]
    return {
        "dataset": folder,
        "scope": scope,
        "earlier": {"day": earlier, **totals[earlier]},
        "later": {"day": later, **totals[later]},
        "group_by": group_by,
        "total_delta": round(now - was, 2) if coverage["comparison_period"] else None,
        "total_percent": round(((now - was) / was) * 100, 1) if was else None,
        "row_delta": totals[later]["rows"] - totals[earlier]["rows"],
        "changes": changes,
        "restated": bool(changes),
        "currency": next(iter(currencies), None),
        "full_snapshot_totals": {
            "earlier": {"day": earlier, **full[earlier]},
            "later": {"day": later, **full[later]},
        },
        **coverage,
    }


def _read_snapshot(raw: Any, scope: list[str] | None = None) -> Any:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = raw if isinstance(raw, pa.Table) else pq.read_table(io.BytesIO(raw))
    if scope is not None:
        if "SubAccountId" not in table.column_names:
            raise ArchiveError("The snapshot has no subscription IDs to enforce access.")
        ids = pc.utf8_lower(pc.cast(table["SubAccountId"], pa.string()))
        table = table.filter(pc.is_in(ids, value_set=pa.array([s.lower() for s in scope])))
    return table


def _snapshot_totals(raw: bytes, scope: list[str] | None = None) -> dict[str, Any]:
    """Row count, total cost, period covered and currency for one archived snapshot."""
    import pyarrow.compute as pc
    table = _read_snapshot(raw, scope)
    if table.num_rows == 0:
        return {"rows": 0, "cost": 0.0, "from": None, "to": None, "currency": "USD"}

    cost = pc.sum(table["BilledCost"]).as_py()
    start = table["ChargePeriodStart"]
    first, last = pc.min(start).as_py(), pc.max(start).as_py()

    currency = "USD"
    if "BillingCurrency" in table.column_names:
        codes = table["BillingCurrency"].drop_null()
        if len(codes):
            named = codes.filter(pc.not_equal(codes, ""))
            if len(named):
                if len(pc.unique(named)) > 1:
                    raise ArchiveError(
                        "The selected snapshots contain multiple currencies. "
                        "Choose subscriptions billed in a single currency.")
                currency = pc.max(named).as_py()

    return {
        "rows": table.num_rows,
        "cost": round(cost or 0.0, 2),
        "from": str(first) if first else None,
        "to": str(last) if last else None,
        "currency": currency,
    }


def _grouped_cost(raw: bytes, group_by: str, scope: list[str] | None = None) -> dict[str, float]:
    """Total cost per value of `group_by`, computed in Arrow.

    Empty and missing values collapse to '(none)' so that a blank service name is one
    reported bucket rather than disappearing from the comparison.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    table = _read_snapshot(raw, scope).select([group_by, "BilledCost"])
    if table.num_rows == 0:
        return {}

    keys = table[group_by]
    # A column that is entirely null arrives as null-typed, which cannot be compared to a
    # string; casting first keeps both that case and the ordinary one on one path.
    if not pa.types.is_string(keys.type):
        keys = keys.cast(pa.string())
    # Kleene logic, because `equal(null, "")` is null rather than false: plain `or_` would
    # propagate that null through the condition and leave a genuinely missing service name
    # as its own `None` bucket alongside '(none)', splitting one group into two.
    keys = pc.if_else(
        pc.or_kleene(pc.is_null(keys), pc.equal(keys, "")), pa.scalar("(none)"), keys
    )

    agg = (
        pa.table({"k": keys, "cost": table["BilledCost"]})
        .group_by("k")
        .aggregate([("cost", "sum")])
    )
    return {
        k: (v or 0.0)
        for k, v in zip(
            agg["k"].to_pylist(), agg["cost_sum"].to_pylist(), strict=True
        )
    }


# What a comparison may group by. A fixed list, because the column name goes into SQL: these are
# the dimensions the dashboard already reasons about, and anything else is a typo or an attempt.
_COMPARE_COLUMNS = (
    "ServiceName", "SubAccountName", "ResourceGroup", "RegionName",
    "ResourceName", "MeterName", "ChargeCategory", "PricingModel",
)


async def build_selection(*, scope: list[str] | None = None, fmt: str = "csv",
                          label: str = "", since: str | None = None,
                          until: str | None = None, source: str | None = None,
                          dataset: str = "actual") -> dict[str, Any]:
    """Build the selected export in memory and hand back the bytes.

    Split out from `export_selection` so the same file can be sent two places. Writing it to the
    archive is one destination; the browser is the other, and it is the one people ask for first
    — an export you cannot open is a strange kind of export. Both go through this, so a
    downloaded file and an archived one are byte-identical rather than two code paths that agree
    by accident.

    Two different meanings of "date" have to be kept apart, because confusing them produces a
    file that looks right and answers the wrong question:

      * **since / until** narrow the *cost period* — which days of spend are in the file.
      * **source** picks *which read* to take them from — today's warehouse, or an archived
        snapshot from an earlier day. Azure restates cost data for days after the fact, so
        August as read on the 27th is not August as read on the 28th.

    Omitting `source` uses the live warehouse, which is what most people want most of the time.
    """
    if fmt not in FORMATS:
        raise ArchiveError(f"Unknown format {fmt!r}. Use {' or '.join(FORMATS)}.")

    since = _check_day(since, "Start date")
    until = _check_day(until, "End date")
    from .reporting import month_bounds

    bounds = month_bounds()
    if bounds:
        since = max(since or bounds[0].isoformat(), bounds[0].isoformat())
        until = min(until or bounds[1].isoformat(), bounds[1].isoformat())
    if since and until and since > until:
        raise ArchiveError("The start date must be on or before the end date.")

    _, ext, content_type = FORMATS[fmt]
    folder = dataset_folder(dataset)
    today = datetime.now(UTC).date()

    # The name records everything that shaped the file. A directory of exports called
    # "selection-1.csv" is unusable a week later; one that says what it holds is self-describing.
    bits = [folder, today.isoformat()]
    if source:
        bits.append(f"as-read-{source}")
    if since or until:
        bits.append(f"{since or 'start'}_to_{until or 'end'}")
    if label:
        bits.append(_SAFE.sub("-", label.lower()).strip("-"))
    stem = "-".join(b for b in bits if b)
    name = f"selection/{today.year:04d}/{today.month:02d}/{stem}{ext}"

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / f"export{ext}"

        if source:
            # From an archived snapshot rather than the live warehouse: the same rows Azure
            # reported on that day, filtered the same way a live export would be.
            day = _check_day(source, "Snapshot date")
            blob = f"{folder}/{day[:4]}/{day[5:7]}/{folder}-{day}.parquet"
            writer = BlobWriter()
            try:
                raw = await writer.get(blob)
            finally:
                await writer.close()

            rows, size = _write_snapshot(path, fmt, raw,
                                         scope=scope, since=since, until=until)
        else:
            from .warehouse import warehouse

            # Scope goes through the warehouse's own shadow table rather than a WHERE clause, so
            # it is enforced the same way every other scoped read in the app is.
            con = warehouse.connect(read_only=True, scope=scope)
            try:
                rows, size = _write_rows(path, fmt, source_sql="costs",
                                         scope=None, since=since, until=until, con=con)
            finally:
                con.close()

        if not rows:
            raise ArchiveError(
                "Nothing matched that selection — no rows for those subscriptions in that date "
                "range. Widen the dates, or check the snapshot you picked covers them."
            )
        data = path.read_bytes()

    return {
        "data": data,
        "name": name,
        # What a browser should call it. The blob path is a filing decision — year and month
        # folders — and makes a poor filename on someone's desktop.
        "filename": f"{stem}{ext}",
        "content_type": content_type,
        "rows": rows,
        "bytes": len(data),
        "format": fmt,
        "dataset": folder,
        "scope": scope,
        "since": since,
        "until": until,
        "read_on": source or "live warehouse",
        "uncompressed_bytes": size,
    }


async def export_selection(*, scope: list[str] | None = None, fmt: str = "csv",
                           label: str = "", since: str | None = None,
                           until: str | None = None, source: str | None = None,
                           dataset: str = "actual") -> dict[str, Any]:
    """Build the selected export and keep a copy in the archive container.

    The durable half of the same operation: the file lands in storage under a name that records
    what shaped it, so it can be found again next quarter. `build_selection` is what decides the
    contents; this only decides where a copy lives.
    """
    if not enabled():
        raise ArchiveError("No archive account is configured (ARCHIVE_ACCOUNT).")

    built = await build_selection(scope=scope, fmt=fmt, label=label, since=since,
                                  until=until, source=source, dataset=dataset)
    writer = BlobWriter()
    try:
        written = await writer.put(built["name"], built["data"], built["content_type"])
    finally:
        await writer.close()

    log.info("exported %s rows to %s/%s/%s", built["rows"], ACCOUNT, CONTAINER, built["name"])
    return {
        **written,
        "rows": built["rows"],
        "format": built["format"],
        "dataset": built["dataset"],
        "account": ACCOUNT,
        "container": CONTAINER,
        "scope": built["scope"],
        "since": built["since"],
        "until": built["until"],
        "read_on": built["read_on"],
        "uncompressed_bytes": built["uncompressed_bytes"],
    }
