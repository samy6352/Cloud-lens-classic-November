"""Independent, bounded readers for Microsoft's public product and incident feeds."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("cloudlens.azure_updates")

UPDATES_URL = "https://www.microsoft.com/releasecommunications/api/v2/azure/rss"
STATUS_URL = "https://azure.status.microsoft/en-us/status/feed/"
SOURCES = {
    "updates": {"name": "Azure product updates", "url": "https://azure.microsoft.com/en-us/updates/",
                "feed_url": UPDATES_URL, "visibility": "public"},
    "status": {"name": "Azure Status", "url": "https://azure.status.microsoft/en-us/status/",
               "feed_url": STATUS_URL, "visibility": "public"},
}
PUBLIC_TTL = 600
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 200
FETCH_TIMEOUT = 25
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
SERVICE_TAG_ALIASES = {
    "api management": "Azure API Management",
    "app service": "Azure App Service",
    "application gateway": "Azure Application Gateway",
    "expressroute": "Azure ExpressRoute",
    "key vault": "Azure Key Vault",
    "load balancer": "Azure Load Balancer",
    "traffic manager": "Azure Traffic Manager",
    "virtual machines": "Azure Virtual Machines",
    "virtual network": "Azure Virtual Network",
    "vpn gateway": "Azure VPN Gateway",
    "web application firewall": "Azure Web Application Firewall",
}


def _service_tags(tags: list[str]) -> list[str]:
    """Use explicit product tags, not lifecycle/family tags or guesses from the title."""
    values: dict[str, str] = {}
    for tag in tags:
        value = SERVICE_TAG_ALIASES.get(tag.casefold())
        if value is None and tag.casefold().startswith(("azure ", "microsoft ")):
            value = tag
        if value:
            values.setdefault(value.casefold(), value)
    return list(values.values())


class SourceError(Exception):
    """An intentionally public, safe explanation of a source failure."""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style", "iframe", "object"}:
            self.hidden += 1
        elif tag in {"p", "br", "li", "div", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style", "iframe", "object"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: Any, limit: int = 4000) -> str:
    parser = _Text()
    # Feeds sometimes double-encode HTML. Decode before parsing, never after stripping.
    text = str(value or "")[:32000]
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    parser.feed(text)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:limit]


def official_url(value: str | None) -> str | None:
    if not value or len(value) > 4096 or re.search(r'[\s<>"\\\x00-\x1f]', value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in (None, 443)):
            return None
        if any(host == suffix or host.endswith("." + suffix)
               for suffix in ("microsoft.com", "azure.com", "azure.status.microsoft")):
            return value
    except ValueError:
        pass
    return None


def iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _category(title: str, tags: list[str]) -> str:
    text = " ".join([title, *tags]).lower()
    if re.search(r"\b(retir\w*|deprecat\w*|end.of.support|end.of.life|sunset)\b", title, re.I):
        return "Retirement"
    if re.search(r"\b(security advisory|security update|vulnerability|cve-\d)", title, re.I):
        return "Security"
    if "preview" in text:
        return "Preview"
    if re.search(r"\bgenerally available|\bgeneral availability|\bga\b|\blaunched\b", text):
        return "GA"
    if "security" in text:
        return "Security"
    return "Change"


def _text(element: ET.Element, *names: str) -> str:
    for name in names:
        child = element.find(name)
        if child is not None:
            # Atom XHTML content contains child markup rather than a CDATA string.
            if len(child):
                return "".join(ET.tostring(c, encoding="unicode") for c in child)
            if child.text:
                return child.text
    return ""


def parse_feed(data: bytes, kind: str) -> dict[str, Any]:
    if len(data) > MAX_FEED_BYTES:
        raise SourceError("The public feed exceeded the 2 MiB response limit.")
    # Reject declarations before ElementTree sees them, including UTF-16/32 encodings.
    probe = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise SourceError("Unsafe XML declarations in the public feed.")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError) as exc:
        raise SourceError("The public feed did not contain valid XML.") from exc
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise SourceError("The RSS feed has no channel.")
        entries = channel.findall("item")
        built = iso_date(_text(channel, "lastBuildDate", "pubDate"))
    elif root.tag == ATOM + "feed":
        channel = root
        entries = root.findall(ATOM + "entry")
        built = iso_date(_text(root, ATOM + "updated"))
    else:
        raise SourceError("Expected an RSS or Atom feed, not a web page.")
    has_next = any(e.get("rel") == "next" for e in channel.findall(ATOM + "link"))
    seen: dict[str, dict[str, Any]] = {}
    for entry in entries:
        title = plain_text(_text(entry, "title", ATOM + "title"), 500)
        if not title:
            raise SourceError("The public feed contains an item without a title.")
        tags = [plain_text(e.text or e.get("term"), 160)
                for e in [*entry.findall("category"), *entry.findall(ATOM + "category")]]
        tags = list(dict.fromkeys(t for t in tags if t))
        link = _text(entry, "link")
        if not link:
            link = next((e.get("href", "") for e in entry.findall(ATOM + "link")
                         if e.get("rel", "alternate") == "alternate"), "")
        identifier = plain_text(_text(entry, "guid", ATOM + "id"), 512)
        url = official_url(link.strip()) or official_url(identifier)
        summary = plain_text(_text(entry, "description", ATOM + "summary",
                                   CONTENT + "encoded", ATOM + "content"))
        published = iso_date(_text(entry, "pubDate", ATOM + "published"))
        updated = iso_date(_text(entry, ATOM + "updated")) or published
        identifier = identifier or url or hashlib.sha256(
            (title + (published or "")).encode()).hexdigest()[:24]
        category = _category(title, tags) if kind == "updates" else "Public incident"
        impact = {
            "Retirement": "Keyword label: review retirement or migration requirements.",
            "Security": "Keyword label: review the published security information.",
            "Preview": "Keyword label: preview capabilities may be relevant to your plans.",
            "GA": "Keyword label: a generally available capability or release.",
            "Change": "Review the official announcement for applicability.",
        }
        item = {
            "id": identifier, "title": title, "summary": summary, "url": url,
            "published_at": published, "updated_at": updated, "category": category,
            "services": _service_tags(tags),
            "regions": [], "status": tags[0] if kind == "updates" and tags else "Reported",
            "level": "Informational" if kind == "updates" else "Not specified",
            "customer_impact": impact.get(category, "Public report; subscription impact is not verified."),
            "actions": "", "subscription_ids": [], "resources": [], "resource_count": 0,
            "tags": tags, "impact_basis": "keyword" if kind == "updates" else "public_feed",
        }
        prior = seen.get(identifier)
        if prior is None or (updated or "") > (prior["updated_at"] or ""):
            seen[identifier] = item
    items = sorted(seen.values(),
                   key=lambda item: (item["updated_at"] or item["published_at"] or "", item["id"]),
                   reverse=True)
    # Some publishers change GUIDs while retaining the canonical announcement URL.
    unique: list[dict[str, Any]] = []
    urls: set[str] = set()
    for item in items:
        if item["url"] and item["url"] in urls:
            continue
        if item["url"]:
            urls.add(item["url"])
        unique.append(item)
    notice = (
        "Public Microsoft product announcements, not subscription-specific recommendations. "
        "Categories and impact labels are keyword-based; no categories are filtered out. "
        "The publisher's RSS window is not the complete release history; summaries may be excerpts."
        if kind == "updates" else
        "Public Azure Status reports, not subscription-scoped Service Health. "
        "Feed retrieval success does not establish that every Azure service is healthy."
    )
    if not unique and kind == "status":
        notice = "No active public incidents reported by this feed. " + notice
    elif not unique:
        notice = "No product announcements returned by this feed. " + notice
    if has_next:
        notice += " The feed advertises another page; this reader retrieves the current feed only."
    if len(unique) > MAX_ITEMS:
        notice += f" Showing the latest {MAX_ITEMS} items."
    return {
        "items": unique[:MAX_ITEMS], "total": len(unique),
        "truncated": len(unique) > MAX_ITEMS or has_next, "notice": notice,
        "feed_updated_at": built,
        "coverage": {"kind": "publisher_feed_window", "pages_fetched": 1,
                     "pagination": "not_followed" if has_next else "not_advertised",
                     "received_items": len(entries), "item_limit": MAX_ITEMS},
    }


async def read_response(response: httpx.Response, max_bytes: int) -> bytes:
    response.raise_for_status()
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > max_bytes:
        raise SourceError("The source response exceeded the configured size limit.")
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        if len(data) + len(chunk) > max_bytes:
            raise SourceError("The source response exceeded the configured size limit.")
        data.extend(chunk)
    return bytes(data)


async def _download(url: str) -> bytes:
    # Fixed official endpoints only. Do not follow feed-supplied links or redirects.
    if url not in (UPDATES_URL, STATUS_URL):
        raise SourceError("Only the configured official public feeds can be fetched.")
    async with (
        asyncio.timeout(FETCH_TIMEOUT),
        httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
            headers={"User-Agent": "CloudLens/1.0", "Accept": "application/rss+xml, application/atom+xml, text/xml"},
        ) as client,
        client.stream("GET", url) as response,
    ):
        return await read_response(response, MAX_FEED_BYTES)


def error_text(exc: Exception) -> str:
    if isinstance(exc, SourceError):
        return plain_text(str(exc), 300)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "The source request timed out. Try refreshing again."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"The source returned HTTP {exc.response.status_code}."
    return f"The source could not be retrieved ({type(exc).__name__})."


def envelope(data: dict[str, Any] | None, source: dict[str, str], ttl: int, *,
             fetched_at: float | None = None, cached: bool = False,
             error: str | None = None) -> dict[str, Any]:
    result = copy.deepcopy(data) if data is not None else {
        "items": [], "truncated": False,
        "notice": f"No {source.get('visibility', '')} source data is available.",
    }
    age = max(0, time.time() - fetched_at) if fetched_at is not None else None
    result.update({
        "status": "error" if error else "ok", "count": len(result["items"]),
        "source": dict(source), "error": error,
        "freshness": {
            "fetched_at": datetime.fromtimestamp(fetched_at, UTC).isoformat().replace("+00:00", "Z")
            if fetched_at is not None else None,
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": bool(error) or age is None or age >= ttl,
            "cached": cached, "max_age_seconds": ttl,
        },
    })
    if error:
        result["notice"] = (
            "Refresh failed; showing the last successfully fetched snapshot. " if data else
            "Retrieval failed; no conclusion about service health or product updates can be drawn. "
        ) + result["notice"]
    return result


@dataclass
class _Snapshot:
    data: dict[str, Any] | None
    fetched_at: float | None
    attempted_at: float
    error: str | None = None


class SourceCache:
    """Bounded per-process snapshots and single-flight refreshes, including forced refreshes."""

    def __init__(self, ttl: int, limit: int, timeout: int = FETCH_TIMEOUT) -> None:
        self.ttl = ttl
        self.limit = limit
        self.timeout = timeout
        self.entries: OrderedDict[str, _Snapshot] = OrderedDict()
        self.flights: dict[str, asyncio.Task] = {}

    async def close(self) -> None:
        pending = list(self.flights.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.flights.clear()
        self.entries.clear()

    async def _load(self, key: str, loader: Callable[[], Awaitable[dict]],
                    source: dict[str, str]) -> _Snapshot:
        prior = self.entries.get(key)
        try:
            async with asyncio.timeout(self.timeout):
                data = await loader()
            now = time.time()
            snapshot = _Snapshot(data, now, now)
        except Exception as exc:
            error = error_text(exc)
            log.warning("%s retrieval failed: %s", source["name"], error)
            snapshot = _Snapshot(prior.data if prior else None,
                                 prior.fetched_at if prior else None, time.time(), error)
        self.entries[key] = snapshot
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return snapshot

    async def get(self, key: str, source: dict[str, str],
                  loader: Callable[[], Awaitable[dict]], refresh: bool = False) -> dict:
        flight = self.flights.get(key)
        snapshot = self.entries.get(key)
        cached = False
        if flight is not None:
            snapshot = await asyncio.shield(flight)
            cached = True
        elif snapshot is not None and not refresh and (
            time.time() - snapshot.attempted_at < (15 if snapshot.error else self.ttl)
        ):
            self.entries.move_to_end(key)
            cached = True
        else:
            if len(self.flights) >= self.limit:
                return envelope(snapshot.data if snapshot else None, source, self.ttl,
                                fetched_at=snapshot.fetched_at if snapshot else None,
                                cached=snapshot is not None,
                                error="This source is busy. Try refreshing again.")
            flight = asyncio.create_task(self._load(key, loader, source))
            self.flights[key] = flight
            flight.add_done_callback(lambda done: self.flights.pop(key, None))
            snapshot = await asyncio.shield(flight)
        return envelope(snapshot.data, source, self.ttl, fetched_at=snapshot.fetched_at,
                        cached=cached or bool(snapshot.error and snapshot.data is not None),
                        error=snapshot.error)


_public_cache = SourceCache(PUBLIC_TTL, 2)


async def public_feed(kind: str, refresh: bool = False) -> dict[str, Any]:
    source = SOURCES[kind]

    async def load() -> dict:
        return parse_feed(await _download(source["feed_url"]), kind)

    return await _public_cache.get(kind, source, load, refresh)
