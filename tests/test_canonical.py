"""Primary-origin routing stays offline and does not import the database-backed app."""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote, unquote, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from app.canonical import CanonicalOriginMiddleware, validate_public_base_url
from app.job_policy import read_only_mode

PRIMARY = "https://cloudlens.site"
ALIASES = ["https://www.cloudlens.site", "https://old-app.azurewebsites.net"]
MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _client(origin=None):
    async def echo(request):
        return JSONResponse(
            {"path": request.scope["path"], "query": request.scope["query_string"].decode()},
            headers={"X-Reached-App": "true"},
        )

    app = Starlette(routes=[
        Route("/{path:path}", echo, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
                                            "OPTIONS", "TRACE"]),
    ])
    return TestClient(CanonicalOriginMiddleware(app, origin), follow_redirects=False)


@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_default_off_preserves_requests(value, method):
    with _client(value) as client:
        response = client.request(method, ALIASES[0] + "/auth/callback?code=code&state=state")
    assert response.status_code == 200
    assert response.headers["X-Reached-App"] == "true"
    assert "location" not in response.headers


@pytest.mark.parametrize(("value", "expected"), [
    (PRIMARY, PRIMARY),
    ("HTTPS://CloudLens.SITE", PRIMARY),
    ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
    ("https://my-app.region.example", "https://my-app.region.example"),
])
def test_valid_origins(value, expected):
    assert validate_public_base_url(value) == expected


@pytest.mark.parametrize("value", [
    "http://cloudlens.site", "//cloudlens.site", "cloudlens.site",
    "https://cloudlens.site/", "https://cloudlens.site/dashboard",
    "https://cloudlens.site?", "https://cloudlens.site?next=//evil.example",
    "https://cloudlens.site#", "https://cloudlens.site#fragment",
    "https://user@cloudlens.site", "https://user:password@cloudlens.site",
    "https://cloudlens.site:443", "https://cloudlens.site:8443",
    "https://cloudlens.site:", "https://cloudlens.site\\evil.example",
    "https://cloudlens.site%2fevil.example", "https://cloud_lens.site",
    "https://-cloudlens.site", "https://cloudlens-.site", "https://cloudlens..site",
    "https://cloudlens.site.", "https://.cloudlens.site", "https://localhost",
    "https://127.0.0.1", "https://127.1", "https://0x7f.0x1", "https://2130706433",
    "https://[::1]", "https://bücher.example", "https://cloudlenſ.site", "https://K.example",
    " https://cloudlens.site",
    "https://cloudlens.site ", "https://cloudlens.site\n", "https://cloud\tlens.site", " ",
    "https://" + "a" * 64 + ".example",
    "https://" + ".".join(["a" * 63] * 4),
])
def test_invalid_origins_fail_closed(value):
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        CanonicalOriginMiddleware(Mock(), value)


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_alias_preserves_normal_path_and_raw_query(alias, method):
    path = "/reports/cost%20details?period=2026-09&tag=a%2Fb&tag=c+d&next=%2Fcosts"
    with _client(PRIMARY) as client:
        response = client.request(method, alias + path)
    assert response.status_code == 307
    assert response.headers["location"] == PRIMARY + path
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "X-Reached-App" not in response.headers
    assert "set-cookie" not in response.headers
    if method == "HEAD":
        assert not response.content


@pytest.mark.parametrize("path", [
    "//evil.example/steal", "///evil.example/steal", "/\\evil.example/steal",
    "/%2f%2fevil.example/steal", "/@evil.example", "/https://evil.example/steal",
])
def test_request_paths_cannot_replace_configured_origin(path):
    with _client(PRIMARY) as client:
        response = client.get(ALIASES[0] + path + "?next=//evil.example")
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc) == ("https", "cloudlens.site")
    assert target.query == "next=//evil.example"


@pytest.mark.parametrize("path", ["/auth/callback", "/auth/callback/", "/auth/%63allback",
                                  "/auth%2fcallback", "//auth/callback",
                                  "/assets/%2e%2e/auth/callback"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_old_callback_restarts_without_forwarding_any_query(path, method):
    with _client(PRIMARY) as client:
        response = client.request(
            method, ALIASES[1] + path + "?code=sensitive-code&state=sensitive-state&error=denied")
    assert response.status_code == 307
    assert response.headers["location"] == PRIMARY + "/login"
    assert "sensitive" not in str(response.headers)
    assert "X-Reached-App" not in response.headers


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("path", ["/api/auth/login", "/api/ask", "/auth/callback", "/healthz"])
def test_alias_unsafe_methods_are_not_replayed(method, path):
    with _client(PRIMARY) as client:
        response = client.request(method, ALIASES[0] + path, content=b"private-credentials",
                                  headers={"Authorization": "Bearer private-token"})
    assert response.status_code == 421
    assert response.json()["public_base_url"] == PRIMARY
    assert PRIMARY in response.json()["detail"]
    assert "location" not in response.headers
    assert "X-Reached-App" not in response.headers
    assert "private" not in response.text


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_health_probes_stay_on_azure_host(path, method):
    with _client(PRIMARY) as client:
        response = client.request(method, ALIASES[1] + path + "?probe=1")
    assert response.status_code == 200
    assert response.headers["X-Reached-App"] == "true"
    assert "location" not in response.headers


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "OPTIONS"])
@pytest.mark.parametrize("host", ["cloudlens.site", "CLOUDLENS.SITE", "cloudlens.site:443"])
def test_canonical_host_is_unchanged(method, host):
    with _client(PRIMARY) as client:
        response = client.request(method, PRIMARY + "/auth/callback?code=code&state=state",
                                  headers={"Host": host})
    assert response.status_code == 200
    assert response.headers["X-Reached-App"] == "true"
    assert "location" not in response.headers
    if method != "HEAD":
        assert response.json()["query"] == "code=code&state=state"


def test_ignores_untrusted_forwarded_origin_headers():
    with _client(PRIMARY) as client:
        alias = client.get(ALIASES[0], headers={
            "X-Forwarded-Host": "cloudlens.site", "Forwarded": "host=cloudlens.site;proto=https"})
        canonical = client.get(PRIMARY, headers={
            "X-Forwarded-Host": "evil.example", "Forwarded": "host=evil.example;proto=http"})
    assert alias.headers["location"] == PRIMARY + "/"
    assert canonical.status_code == 200


def test_preserves_platform_tls_termination_without_rewriting_scope():
    with _client(PRIMARY) as client:
        response = client.get("http://cloudlens.site/")
    assert response.status_code == 200


@pytest.mark.parametrize("raw", [b"/a%23b%3fc", None])
def test_path_delimiters_are_not_confused_with_query_or_fragment(raw):
    scope = {
        "type": "http", "method": "GET", "path": "/a#b?c",
        "query_string": b"x=%23&literal=#", "headers": [(b"host", b"alias.example")],
    }
    if raw is not None:
        scope["raw_path"] = raw
    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(CanonicalOriginMiddleware(Mock(), PRIMARY)(scope, AsyncMock(), send))
    location = dict(messages[0]["headers"])[b"location"].decode()
    target = urlsplit(location)
    assert unquote(target.path) == "/a#b?c"
    assert target.query == "x=%23&literal=%23"
    assert not target.fragment


def _main_stack(monkeypatch, value):
    """Execute actual middleware wiring and auth entrypoint without importing app.main."""
    if value is None:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("PUBLIC_BASE_URL", value)
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    names = {"RequireAuth", "CacheHeaders", "_wants_html", "safe_next", "sso_start"}
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in names:
                node.decorator_list = []
                selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id in {"PUBLIC_PATHS", "PUBLIC_PREFIXES", "SLOW_API_LOG_MS"}
            for target in node.targets
        ):
            selected.append(node)
    auth = SimpleNamespace(
        identify=Mock(return_value=None),
        sso=SimpleNamespace(enabled=True, start=AsyncMock(return_value="https://login.example/")),
        cookie_kwargs=lambda secure: {
            "secure": secure, "httponly": True, "samesite": "lax", "path": "/"},
    )
    namespace = {
        "BaseHTTPMiddleware": BaseHTTPMiddleware, "Request": Request, "Response": Response,
        "JSONResponse": JSONResponse, "RedirectResponse": RedirectResponse, "quote": quote,
        "auth_service": lambda: auth, "COOKIE": "cloudlens_session", "secrets": secrets,
        "time": time,
        "entra_email": lambda value: "", "log": logging.getLogger("test.canonical"),
        "_content_security_policy": lambda: "default-src 'self'",
        "read_only_mode": read_only_mode,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(MAIN), "exec"), namespace)

    async def login(request):
        return await namespace["sso_start"](request, next=request.query_params.get("next", "/"))

    app = Starlette(routes=[Route("/auth/login", login)])
    namespace.update({
        "app": app, "os": os, "GZipMiddleware": GZipMiddleware,
        "CanonicalOriginMiddleware": CanonicalOriginMiddleware,
        "validate_public_base_url": validate_public_base_url,
    })
    registrations = [
        node for node in tree.body if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "app" and node.value.func.attr == "add_middleware"
    ]
    exec(compile(ast.Module(body=registrations, type_ignores=[]), str(MAIN), "exec"), namespace)
    assert [entry.cls.__name__ for entry in app.user_middleware] == [
        "CacheHeaders", "CanonicalOriginMiddleware", "RequireAuth", "GZipMiddleware",
    ]
    return app, auth


def test_real_registration_rejects_invalid_configuration_before_serving(monkeypatch):
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        _main_stack(monkeypatch, "http://cloudlens.site")


@pytest.mark.parametrize("path", ["/auth/login?next=%2Freports", "/api/ask",
                                  "/assets/vendor/example.js", "/assets/example.css"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_canonical_runs_before_auth_with_outer_security_headers(monkeypatch, path, method):
    app, auth = _main_stack(monkeypatch, PRIMARY)
    with TestClient(app, follow_redirects=False) as client:
        response = client.request(method, ALIASES[1] + path)
    if method == "GET":
        assert response.status_code == 307
        assert response.headers["location"] == PRIMARY + path
    else:
        assert response.status_code == 421
        assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'self'"
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert "set-cookie" not in response.headers
    auth.identify.assert_not_called()
    auth.sso.start.assert_not_called()


def test_alias_signin_sets_binding_only_after_reaching_primary(monkeypatch):
    app, auth = _main_stack(monkeypatch, PRIMARY)
    with TestClient(app, follow_redirects=False) as client:
        first = client.get(ALIASES[0] + "/auth/login?next=%2Freports")
        assert "set-cookie" not in first.headers
        auth.sso.start.assert_not_called()
        primary = client.get(first.headers["location"])
        assert primary.status_code == 303
        cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "cloudlens_oauth")
        assert cookie.domain == "cloudlens.site"
        assert not cookie.domain_specified
        assert cookie.secure
        assert auth.sso.start.call_args.kwargs["browser_binding"] == cookie.value
        assert auth.sso.start.call_args.args == ("/reports",)
    assert "HttpOnly" in primary.headers["set-cookie"]
    assert "SameSite=lax" in primary.headers["set-cookie"]


def test_auth_gate_still_applies_on_primary(monkeypatch):
    app, auth = _main_stack(monkeypatch, PRIMARY)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(PRIMARY + "/api/ask")
    assert response.status_code == 401
    assert response.json()["auth"] == "required"
    auth.identify.assert_called_once()


@pytest.mark.parametrize("value", [None, ""])
def test_unconfigured_real_stack_keeps_old_host_signin(monkeypatch, value):
    app, auth = _main_stack(monkeypatch, value)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(ALIASES[1] + "/auth/login")
        cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "cloudlens_oauth")
    assert response.status_code == 303
    assert cookie.domain == "old-app.azurewebsites.net"
    assert not cookie.domain_specified
    auth.identify.assert_called_once()
    auth.sso.start.assert_awaited_once()


@pytest.mark.parametrize(("path", "asset_cache", "cache"), [
    ("/assets/vendor/example.js", None, "no-cache"),
    ("/assets/example.css", None, "no-cache"),
    ("/assets/vendor/example.js?v=content-version", "public, max-age=31536000, immutable",
     "public, max-age=31536000, immutable"),
    ("/assets/example.css?v=content-version", "public, max-age=31536000, immutable",
     "public, max-age=31536000, immutable"),
])
@pytest.mark.parametrize("status", [200, 206, 304])
def test_canonical_asset_cache_policy_is_preserved(monkeypatch, path, asset_cache, cache, status):
    app, _ = _main_stack(monkeypatch, PRIMARY)

    async def asset(request):
        return Response(status_code=status, headers={"Cache-Control": asset_cache} if asset_cache else {})

    app.router.routes.append(Route(urlsplit(path).path, asset))
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(PRIMARY + path)
    assert response.status_code == status
    assert response.headers["cache-control"] == cache
