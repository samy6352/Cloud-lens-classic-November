"""Static assets are compressed once, and still revalidate correctly.

`GZipMiddleware` used to compress every asset on the way out, which meant re-deriving 84 KB of
`dashboard.js` from 276 KB on every cold request, inside the event loop. What matters is not just
that the bytes are now kept, but that keeping them did not quietly break the things the previous
arrangement got right: conditional requests, clients that cannot take gzip, and files where
compressing is not worth the trouble.

The app is not booted here. `PrecompressedStatic` is mounted on a bare Starlette app with the
same GZip middleware in front of it, which is the part under test.
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import assets  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"


@pytest.fixture
def client():
    assets.clear()
    app = Starlette()
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/assets", assets.PrecompressedStatic(directory=WEB), name="assets")
    with TestClient(app) as c:
        yield c
    assets.clear()


@pytest.mark.parametrize("name", [
    "dashboard.js", "dashboard-spend.js", "dashboard-optimize.js",
    "dashboard-monitor.js", "dashboard-reports.js", "dashboard-state.js",
])
def test_a_large_asset_comes_back_gzipped_and_intact(name):
    """The bytes have to survive the round trip, not merely be smaller."""
    assets.clear()
    app = Starlette()
    app.mount("/assets", assets.PrecompressedStatic(directory=WEB), name="assets")
    with TestClient(app) as c:
        r = c.get(f"/assets/{name}", headers={"Accept-Encoding": "gzip"})

    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["vary"] == "Accept-Encoding"
    # httpx decodes transparently, so this compares the delivered file to the one on disk.
    assert r.content == (WEB / name).read_bytes()


def test_the_file_is_compressed_once_however_often_it_is_read(client):
    client.get("/assets/dashboard.js", headers={"Accept-Encoding": "gzip"})
    assert len(assets._store) == 1
    stored = dict(assets._store)

    for _ in range(5):
        r = client.get("/assets/dashboard.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200

    assert assets._store == stored, "re-reading an asset must not recompress it"


def test_editing_a_file_recompresses_it(tmp_path):
    """The key is mtime and size, so a changed file must not serve its old body."""
    target = tmp_path / "big.js"
    target.write_text("// one\n" + "x = 1;\n" * 500)

    app = Starlette()
    app.mount("/assets", assets.PrecompressedStatic(directory=tmp_path), name="assets")
    assets.clear()
    with TestClient(app) as c:
        first = c.get("/assets/big.js", headers={"Accept-Encoding": "gzip"})
        assert first.headers["content-encoding"] == "gzip"

        target.write_text("// two\n" + "y = 2;\n" * 500)
        second = c.get("/assets/big.js", headers={"Accept-Encoding": "gzip"})

    assert second.content == target.read_bytes()
    assert b"y = 2;" in second.content


def test_a_client_that_cannot_take_gzip_gets_the_file(client):
    r = client.get("/assets/dashboard.js", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content == (WEB / "dashboard.js").read_bytes()
    assert not assets._store, "nothing to store for a client that did not ask for it"


def test_a_repeat_visit_still_revalidates_to_304(client):
    first = client.get("/assets/dashboard.js", headers={"Accept-Encoding": "gzip"})
    etag = first.headers["etag"]

    again = client.get(
        "/assets/dashboard.js",
        headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
    )
    assert again.status_code == 304, "a kept ETag is the whole reason repeat visits are cheap"


def test_a_small_file_is_left_alone(tmp_path):
    """Under the threshold gzip can make a file bigger, and the trip is latency-bound anyway."""
    small = tmp_path / "tiny.js"
    small.write_text("export const x = 1;\n")
    assert small.stat().st_size < assets.MIN_SIZE

    app = Starlette()
    app.mount("/assets", assets.PrecompressedStatic(directory=tmp_path), name="assets")
    assets.clear()
    with TestClient(app) as c:
        r = c.get("/assets/tiny.js", headers={"Accept-Encoding": "gzip"})

    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert not assets._store


def test_the_middleware_does_not_compress_it_a_second_time(client):
    """GZipMiddleware is still mounted in front; it must leave our body alone."""
    r = client.get("/assets/app.css", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    # Decoding once has to yield the file. Double compression would need two passes.
    raw = r.read() if hasattr(r, "read") else r.content
    assert raw == (WEB / "app.css").read_bytes()


def test_vary_is_declared_exactly_once_on_every_path(client):
    """`GZipMiddleware.add_vary_header` concatenates without checking.

    Declaring `Vary` on every response as well produced a literal
    `Vary: Accept-Encoding, Accept-Encoding` for clients that did not take gzip.
    """
    gzipped = client.get("/assets/dashboard.js", headers={"Accept-Encoding": "gzip"})
    assert gzipped.headers["vary"] == "Accept-Encoding"

    plain = client.get("/assets/dashboard.js", headers={"Accept-Encoding": "identity"})
    assert plain.headers["vary"] == "Accept-Encoding"

    not_modified = client.get(
        "/assets/dashboard.js",
        headers={"Accept-Encoding": "gzip", "If-None-Match": gzipped.headers["etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.headers["vary"] == "Accept-Encoding"


def test_stored_bytes_really_are_the_compressed_file(client):
    client.get("/assets/app.css", headers={"Accept-Encoding": "gzip"})
    (stored,) = assets._store.values()
    assert gzip.decompress(stored) == (WEB / "app.css").read_bytes()
    assert len(stored) < (WEB / "app.css").stat().st_size


@pytest.fixture
def versioned(tmp_path):
    (tmp_path / "main.js").write_text("import './child.js';\n" + "// example\n" * 200)
    (tmp_path / "child.js").write_text("export const synthetic = 42;")
    (tmp_path / "app.css").write_text("body { color: black; }")
    static = assets.PrecompressedStatic(directory=tmp_path)
    app = Starlette()
    app.mount("/assets", static)
    with TestClient(app) as client:
        yield static, client, tmp_path


def test_versioned_asset_is_immutable_and_relative_modules_share_bundle(versioned):
    from urllib.parse import urljoin

    static, client, root = versioned
    url = static.asset_url("main.js")
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == (root / "main.js").read_bytes()
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    child_url = urljoin(url, "./child.js")
    assert child_url == static.asset_url("child.js")
    assert client.get(child_url).content == (root / "child.js").read_bytes()
    assert client.get("/assets/main.js").headers["cache-control"] == "no-cache"
    assert client.get("/assets/main.js?v=" + static._version).headers["cache-control"] == "no-cache"


def test_versioned_etag_encoding_and_head_are_correct(versioned):
    static, client, _ = versioned
    url = static.asset_url("main.js")
    compressed = client.get(url, headers={"Accept-Encoding": "gzip"})
    plain = client.get(url, headers={"Accept-Encoding": "identity"})
    assert compressed.headers["etag"] != plain.headers["etag"]
    assert compressed.content == plain.content
    unchanged = client.get(url, headers={"Accept-Encoding": "gzip",
                                        "If-None-Match": compressed.headers["etag"]})
    assert unchanged.status_code == 304
    assert "immutable" in unchanged.headers["cache-control"]
    assert not unchanged.content
    head = client.head(url, headers={"Accept-Encoding": "identity"})
    assert head.status_code == 200
    assert head.headers["content-length"] == plain.headers["content-length"]
    assert not head.content
    rejected_gzip = client.get(url, headers={"Accept-Encoding": "gzip;q=0, identity"})
    assert "content-encoding" not in rejected_gzip.headers


def test_immutable_bytes_never_change_under_existing_hash(versioned):
    static, client, root = versioned
    url = static.asset_url("main.js")
    before = client.get(url).content
    (root / "main.js").write_text("export const changed = true")
    assert client.get(url).content == before
    replacement = assets.PrecompressedStatic(directory=root)
    new_url = replacement.asset_url("main.js")
    assert new_url != url
    app = Starlette()
    app.mount("/assets", replacement)
    with TestClient(app) as restarted:
        assert restarted.get(url).status_code == 404
        assert restarted.get(new_url).content == (root / "main.js").read_bytes()


@pytest.mark.parametrize("suffix", [
    "../secret.txt", "%2e%2e/%2e%2e/secret.txt", "..%5csecret.txt",
    "C:%5csecret.txt", "%00main.js", "missing.js", ".hidden", "child.js%3Ffake",
])
def test_versioned_paths_cannot_escape_or_gain_immutable_cache_on_error(versioned, suffix):
    static, client, _ = versioned
    prefix = static.asset_url("main.js").rsplit("/", 1)[0]
    response = client.get(prefix + "/" + suffix)
    assert response.status_code in {400, 404}
    assert "immutable" not in response.headers.get("cache-control", "")


def test_unknown_version_and_mutations_are_rejected(versioned):
    static, client, _ = versioned
    url = static.asset_url("main.js")
    assert client.get(url.replace(static._version, "0" * 64)).status_code == 404
    assert client.post(url).status_code == 405
    for name in ("../main.js", "/main.js", "a\\b", "main.js?x=1", "missing.js"):
        with pytest.raises(ValueError):
            static.asset_url(name)


def test_html_stamping_includes_modulepreload_without_changing_inline_csp(versioned):
    static, _, _ = versioned
    html = """<link rel="modulepreload" href="/assets/child.js">
    <script type="module" src='/assets/main.js'></script>
    <link href="/assets/app.css" rel="stylesheet">
    <a href="https://example.invalid/assets/file.js">external</a>"""
    stamped = assets.stamp_asset_urls(html, static)
    assert static.asset_url("main.js") in stamped
    assert static.asset_url("child.js") in stamped
    assert static.asset_url("app.css") in stamped
    assert "https://example.invalid/assets/file.js" in stamped
    assert assets.stamp_asset_urls(stamped, static) == stamped


def test_asset_bundle_memory_is_bounded(tmp_path):
    (tmp_path / "large.js").write_text("x" * 11)
    static = assets.PrecompressedStatic(directory=tmp_path, max_version_bytes=10)
    with pytest.raises(ValueError, match="byte limit"):
        static.asset_url("large.js")


def test_versioned_symlink_cannot_publish_outside_directory(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("synthetic-private")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")
    static = assets.PrecompressedStatic(directory=root)
    with pytest.raises(ValueError, match="Unknown asset"):
        static.asset_url("link.txt")
