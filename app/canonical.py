"""Optional primary-origin routing, before authentication creates host-only cookies."""
from __future__ import annotations

import re
from posixpath import normpath
from urllib.parse import quote, quote_from_bytes

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I | re.ASCII)
_HEALTH_PATHS = {"/healthz", "/readyz"}
_SAFE_METHODS = {"GET", "HEAD"}


def validate_public_base_url(value: str | None) -> str | None:
    """Accept only an HTTPS DNS origin; absent/empty preserves existing routing."""
    if value is None or value == "":
        return None
    match = re.fullmatch(r"https://([a-z0-9.-]+)", value, re.I | re.ASCII)
    host = match[1].lower() if match else ""
    labels = host.split(".")
    if (not host or len(host) > 253 or len(labels) < 2
            or not all(_LABEL.fullmatch(label) for label in labels)
            # Exclude IP literals and browser-interpreted numeric addresses.
            or not labels[-1][0].isalpha()):
        raise ValueError(
            "PUBLIC_BASE_URL must be an HTTPS DNS origin (https://app.example.com), "
            "without a port, userinfo, path, trailing slash, query or fragment."
        )
    return f"https://{host}"


def _request_path(scope: Scope) -> str:
    raw = scope.get("raw_path")
    if raw is None:
        return "/" + quote(scope["path"].lstrip("/"), safe="/:@!$&'()*+,;=-._~")
    # Concatenate onto the configured origin, never urljoin an attacker-controlled path.
    return "/" + quote_from_bytes(raw.lstrip(b"/"), safe="/%:@!$&'()*+,;=-._~")


class CanonicalOriginMiddleware:
    def __init__(self, app: ASGIApp, public_base_url: str | None = None) -> None:
        self.app = app
        self.origin = validate_public_base_url(public_base_url)
        self.host = self.origin.removeprefix("https://") if self.origin else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.origin is None:
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        # Use the actual Host only. TLS termination/HTTPS enforcement remains the
        # platform's job; trusting client Forwarded headers would bypass this gate.
        hosts = Headers(scope=scope).getlist("host")
        canonical = len(hosts) == 1 and hosts[0].lower() in {self.host, f"{self.host}:443"}
        if canonical or (method in _SAFE_METHODS and path in _HEALTH_PATHS):
            await self.app(scope, receive, send)
            return

        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if method not in _SAFE_METHODS:
            response = JSONResponse(
                {"detail": f"Use {self.origin} for this request; it was not processed.",
                 "public_base_url": self.origin},
                status_code=421, headers=headers,
            )
        else:
            # A flow started on an old host cannot satisfy the primary host's cookie
            # binding. Restart cleanly, including encoded/slash-normalized callbacks.
            callback = normpath(path.replace("\\", "/")).strip("/") == "auth/callback"
            target = self.origin + ("/login" if callback else _request_path(scope))
            if not callback and scope.get("query_string"):
                target += "?" + quote_from_bytes(
                    scope["query_string"], safe="!$&'()*+,-./:;=?@_%~")
            response = RedirectResponse(target, status_code=307, headers=headers)
        await response(scope, receive, send)
