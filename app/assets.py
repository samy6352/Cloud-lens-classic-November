"""Static assets, compressed once rather than on every request.

`GZipMiddleware` compresses a response as it goes out, which is the right behaviour for an API
answer that is different every time and the wrong one for a file that is identical until the next
deploy. Measured here, per cold request:

    dashboard.js   276 KB -> 84 KB   11.7 ms
    app.css        174 KB -> 45 KB    6.7 ms
    app.js          77 KB -> 26 KB    4.1 ms

That is ~23 ms of CPU burned re-deriving the same bytes, on a dev laptop; the B1 instance this
runs on is several times slower, and it is spent inside the event loop where it delays every
other request in flight. Repeat visits already cost nothing -- they revalidate to a 304 and never
reach this -- so the waste falls entirely on first loads and cold caches, which is exactly the
visit where it is most felt.

Compression is kept keyed on the file's mtime and size, so editing a file during development
recompresses it and a deploy invalidates everything without anything having to be cleared.
"""
from __future__ import annotations

import gzip
import hashlib
import mimetypes
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# Below roughly a packet there is nothing to win: gzip's own framing can make a small file
# larger, and the transfer is latency-bound either way. Same threshold the middleware used when
# it was handling these, so no file changes behaviour by moving here.
MIN_SIZE = 1024

# Compressing an already-compressed format wastes CPU to produce something slightly bigger. Fonts
# and images are the usual offenders; everything this app serves is text, but the check belongs
# here rather than in the reader's head.
COMPRESSIBLE = (
    "text/",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
)

# (path, mtime, size) -> compressed bytes. Bounded in practice by the contents of web/assets,
# which is a fixed directory of about a megabyte; this is not a general-purpose file cache.
_store: dict[tuple[str, int, int], bytes] = {}


def clear() -> None:
    """Drop everything held. For tests, and for anything that rewrites assets in place."""
    _store.clear()


def _worth_compressing(media_type: str, size: int) -> bool:
    kind = media_type.partition(";")[0].strip().lower()
    return size >= MIN_SIZE and kind.startswith(COMPRESSIBLE)


def _compressed(full_path: str, stat_result: os.stat_result) -> bytes:
    key = (full_path, stat_result.st_mtime_ns, stat_result.st_size)
    hit = _store.get(key)
    if hit is not None:
        return hit

    with open(full_path, "rb") as fh:
        raw = fh.read()
    # Level 9 because this is paid once and then read many thousands of times. The middleware
    # could not afford it per request and used the default; here the trade runs the other way.
    body = gzip.compress(raw, 9)

    # A file that gains nothing from compression is stored as-is rather than re-tested on every
    # request, and the caller sends the original.
    for previous in tuple(_store):
        if previous[0] == full_path and previous != key:
            _store.pop(previous, None)
    _store[key] = body if len(body) < len(raw) else raw
    return _store[key]


class PrecompressedStatic(StaticFiles):
    """`StaticFiles`, but the gzip is computed once and kept.

    Subclassed rather than rewritten so that path resolution -- the part with a traversal bug in
    it if anyone gets it wrong -- stays Starlette's.
    """

    def __init__(self, *args, max_version_bytes: int = 32 * 1024 * 1024, **kwargs):
        super().__init__(*args, **kwargs)
        self._version_lock = threading.Lock()
        self._version = ""
        self._version_files: dict[str, bytes] = {}
        self._version_gzip: dict[str, bytes] = {}
        self._max_version_bytes = max_version_bytes

    def _build_version(self) -> str:
        # A bundle hash, not per-file hashes: relative ES-module imports and CSS
        # URLs then stay under the same version prefix without rewriting source.
        # Retain the exact hashed bytes. An in-place edit can never make a known
        # immutable URL serve different bytes (a new mount produces a new version).
        with self._version_lock:
            if self._version:
                return self._version
            if self.directory is None:
                raise ValueError("Versioned assets require one explicit directory.")
            root = Path(self.directory).resolve()
            files, size = {}, 0
            digest = hashlib.sha256()
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                relative = path.relative_to(root).as_posix()
                if any(part.startswith(".") for part in relative.split("/")):
                    continue
                if size + path.stat().st_size > self._max_version_bytes or len(files) >= 4096:
                    raise ValueError("Versioned assets exceed the configured byte limit.")
                raw = path.read_bytes()
                size += len(raw)
                if size > self._max_version_bytes:
                    raise ValueError("Versioned assets exceed the configured byte limit.")
                files[relative] = raw
                digest.update(relative.encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(raw).digest())
            self._version_files = files
            self._version = digest.hexdigest()
            return self._version

    def asset_url(self, name: str, *, prefix: str = "/assets") -> str:
        """Stamp an existing asset; call once while building the HTML shell."""
        if not _safe_asset_name(name):
            raise ValueError("Invalid asset name.")
        version = self._build_version()
        if name not in self._version_files:
            raise ValueError("Unknown asset.")
        return f"{prefix}/v/{version}/{quote(name, safe='/.-_')}"

    async def get_response(self, path: str, scope: Any) -> Response:
        normalized = path.replace(os.sep, "/")
        if normalized.startswith("v/"):
            if scope["method"] not in {"GET", "HEAD"}:
                return Response(status_code=405, headers={"Allow": "GET, HEAD"})
            _, _, rest = normalized.partition("/")
            version, _, name = rest.partition("/")
            if not re.fullmatch(r"[a-f0-9]{64}", version) or not _safe_asset_name(name):
                return Response(status_code=404, headers={"Cache-Control": "no-cache"})
            current = await anyio.to_thread.run_sync(self._build_version)
            if version != current or name not in self._version_files:
                return Response(status_code=404, headers={"Cache-Control": "no-cache"})
            return await anyio.to_thread.run_sync(self._version_response, name, scope)
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

    def _version_response(self, name: str, scope: Any) -> Response:
        raw = self._version_files[name]
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        request = Headers(scope=scope)
        use_gzip = _accepts_gzip(request.get("accept-encoding", "")) and _worth_compressing(
            media_type, len(raw))
        body = raw
        if use_gzip:
            with self._version_lock:
                if name not in self._version_gzip:
                    self._version_gzip[name] = gzip.compress(raw, 9, mtime=0)
                body = self._version_gzip[name]
            if len(body) >= len(raw):
                use_gzip, body = False, raw
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        headers = {"Cache-Control": "public, max-age=31536000, immutable",
                   "ETag": etag, "Vary": "Accept-Encoding",
                   "X-Content-Type-Options": "nosniff"}
        if use_gzip:
            headers["Content-Encoding"] = "gzip"
        if any(tag.strip().removeprefix("W/") in {etag, "*"}
               for tag in request.get("if-none-match", "").split(",")):
            return Response(status_code=304, headers=headers)
        headers["Content-Length"] = str(len(body))
        # A normal 200 is valid for Range requests; do not accidentally gzip a 206.
        return Response(b"" if scope["method"] == "HEAD" else body,
                        media_type=media_type, headers=headers)

    def file_response(self, full_path: Any, stat_result: os.stat_result,
                      scope: Any, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)

        # 304, or a range request: no body of ours to replace. The middleware leaves these alone,
        # so declaring `Vary` here neither duplicates nor gets duplicated -- and a 304 is supposed
        # to carry the header a 200 would have.
        if response.status_code != 200:
            response.headers.setdefault("vary", "Accept-Encoding")
            return response

        media_type = response.headers.get("content-type", "")
        if not _worth_compressing(media_type, stat_result.st_size):
            return response
        request_headers = Headers(scope=scope)
        if "range" in request_headers:
            return response
        if not _accepts_gzip(request_headers.get("accept-encoding", "")):
            return response

        body = _compressed(str(full_path), stat_result)
        if len(body) >= stat_result.st_size:
            return response  # stored uncompressed; sending it as a file is cheaper

        headers = {
            # Set here and nowhere else. `GZipMiddleware` adds this itself on the responses it
            # handles, and its `add_vary_header` concatenates without checking, so declaring it
            # on every path produced a literal `Vary: Accept-Encoding, Accept-Encoding`. On this
            # path the middleware sees `Content-Encoding` already set and passes straight
            # through, which leaves us as the only writer.
            "Vary": "Accept-Encoding",
            "Content-Encoding": "gzip",
        }
        # The validators have to survive, or every repeat visit becomes a full download. Both
        # encodings deliberately share one ETag: `Vary` is what tells a cache to keep them apart,
        # and a per-encoding tag would have to be undone again before comparing an incoming
        # `If-None-Match`, which is a subtlety with nothing to show for it.
        for carried in ("etag", "last-modified"):
            value = response.headers.get(carried)
            if value:
                headers[carried] = value

        return Response(body, status_code=200, headers=headers, media_type=media_type)


def _accepts_gzip(value: str) -> bool:
    encodings = {}
    for entry in value.lower().split(","):
        coding, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        encodings[coding] = quality
    return encodings.get("gzip", encodings.get("*", 0)) > 0


def _safe_asset_name(name: str) -> bool:
    return bool(name) and not any(ord(char) < 32 for char in name) and not any(
        char in name for char in ("\\", "%", "?", "#", "\0", ":")
    ) and all(part and not part.startswith(".") for part in name.split("/"))


def stamp_asset_urls(html: str, static: PrecompressedStatic) -> str:
    """Version local src/href references, including stylesheet/modulepreload links."""
    pattern = re.compile(r"""(\b(?:src|href)\s*=\s*)(["'])(/assets/[^"'?#]+)\2""",
                         re.IGNORECASE)

    def replace(match):
        name = match.group(3).removeprefix("/assets/")
        if name.startswith("v/"):
            return match.group(0)
        return match.group(1) + match.group(2) + static.asset_url(name) + match.group(2)

    return pattern.sub(replace, html)
