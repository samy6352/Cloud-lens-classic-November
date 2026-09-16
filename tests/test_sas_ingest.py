"""Does the SAS-only ingest path work without any ARM call?"""
# cloudlens-test: requires-azure
#
# Reads a container SAS from CLOUDLENS_SAS_URL and lists a real storage account.
# Run it with CLOUDLENS_AZURE_TEST=1 and that variable set.
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# The SAS used to be read from a file in %TEMP% that only existed on the machine
# this was written on, so the test could not be run by anyone else -- and reported
# the resulting KeyError as a failure of the ingest path rather than of its own setup.
SAS = os.environ.get("CLOUDLENS_SAS_URL", "").strip()
if not SAS:
    print("SKIP: set CLOUDLENS_SAS_URL to a container SAS to run this test.")
    raise SystemExit(0)


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    from app.exports import BlobReader, ExportError, ingest_export
    from app.main import _sas_source

    print("=" * 72)
    print("SAS URL is self-describing")
    r = BlobReader.from_sas_url(SAS)
    check("account parsed", r.account == "clcost452f1h", r.account)
    check("container parsed", r.container == "costexports", r.container)

    label = _sas_source(SAS)
    check("source label identifies the container", "costexports" in label, label)
    check("source label does not leak the token",
          "sig=" not in label and "se=" not in label, label)

    print("\n" + "=" * 72)
    print("listing the container with only the SAS")
    try:
        blobs = await r.list(prefix="")
        check("listing succeeds without ARM", True, f"{len(blobs)} blob(s)")
    except Exception as exc:
        check("listing succeeds without ARM", False, str(exc)[:200])

    print("\n" + "=" * 72)
    print("ingest reports an empty export honestly rather than pretending")
    try:
        await ingest_export(sas_url=SAS, max_files=60)
        check("ingest returned", True, "data was present")
    except ExportError as exc:
        check("empty export raises a clear ExportError", "No CSV files" in str(exc),
              str(exc)[:160])
    except Exception as exc:
        check("empty export raises a clear ExportError", False,
              f"{type(exc).__name__}: {str(exc)[:160]}")

    print("\n" + "=" * 72)
    print("the endpoint falls back to a configured SAS")
    import inspect

    from app import main as appmain

    src = inspect.getsource(appmain.ingest_from_export)
    check("the handler reads COST_EXPORT_SAS_URL", "COST_EXPORT_SAS_URL" in src)
    check("an explicit SAS in the request takes precedence",
          'body.sas_url or os.getenv("COST_EXPORT_SAS_URL"' in src)
    check("the SAS branch runs before export discovery",
          src.index("COST_EXPORT_SAS_URL") < src.index("discover_exports()"))
    check("a blind app is told what to grant, not to retry the same way",
          "needs Reader on the subscription" in src)

    # Reading the source proves the code says the right thing; it does not prove the code runs.
    # A NameError on the discovery path survived exactly that gap once already, so the handler
    # is actually executed here -- with auth and the background task stubbed, since neither is
    # what is under test.
    print("\n" + "=" * 72)
    print("the handler actually executes, on both branches")

    real_admin = appmain.require_admin
    real_task = asyncio.create_task

    class Body:
        def __init__(self, sas=None, eid=None):
            self.sas_url, self.export_id, self.max_files = sas, eid, 5

    def fake_task(coro):
        coro.close()  # never run the ingest; only record that we got that far
        class T:
            def done(self):
                return True
        return T()

    appmain.require_admin = lambda *a, **k: None
    appmain._ingest_task = None
    asyncio.create_task = fake_task
    try:
        os.environ["COST_EXPORT_SAS_URL"] = SAS
        r = await appmain.ingest_from_export(Body(), object())
        import json as _json
        payload = _json.loads(bytes(r.body).decode())
        check("a configured SAS starts an ingest", payload.get("type") == "sas", str(payload))
        check("the response does not leak the token", "sig=" not in str(payload), str(payload))

        os.environ.pop("COST_EXPORT_SAS_URL", None)
        try:
            await appmain.ingest_from_export(Body(), object())
            check("no SAS falls through to discovery without raising NameError", True)
        except Exception as exc:
            name_error = isinstance(exc, NameError)
            check("no SAS falls through to discovery without raising NameError",
                  not name_error, f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        appmain.require_admin = real_admin
        asyncio.create_task = real_task
        os.environ.pop("COST_EXPORT_SAS_URL", None)

    print("\n" + "=" * 72)
    print("startup ingest prefers the configured export")
    startup_src = inspect.getsource(appmain._run_ingest)
    check("_run_ingest reads COST_EXPORT_SAS_URL", "COST_EXPORT_SAS_URL" in startup_src)
    check("it uses the export before touching ARM",
          startup_src.index("COST_EXPORT_SAS_URL") < startup_src.index("list_subscriptions"))
    check("it returns rather than falling through to ARM as well",
          "return" in startup_src.split("list_subscriptions")[0])

    os.environ["COST_EXPORT_SAS_URL"] = SAS
    try:
        await appmain._run_ingest(3)
        from app.warehouse import warehouse as wh
        st = wh.state.get("status")
        check("a configured-but-empty export reports failure, not silent success",
              st == "failed", f"status={st}")
        check("and says why", "No CSV files" in str(wh.state.get("detail", "")),
              str(wh.state.get("detail", ""))[:120])
    finally:
        os.environ.pop("COST_EXPORT_SAS_URL", None)

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
