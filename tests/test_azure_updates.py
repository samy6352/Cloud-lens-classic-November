"""Offline public-feed normalization, isolation, bounds and cache regressions."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import azure_updates as feeds

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:a10="http://www.w3.org/2005/Atom" version="2.0"><channel>
<title>Azure service updates</title><lastBuildDate>Thu, 03 Sep 2026 17:17:08 Z</lastBuildDate>
<item><guid isPermaLink="false">570557</guid>
<link>https://azure.microsoft.com/updates?id=570557</link>
<category>Launched</category><category>Networking</category>
<category>Azure Virtual Network Manager</category>
<title>[Launched] Generally Available: Azure Virtual Network Manager IPAM</title>
<description><![CDATA[<p>Available &amp; supported.</p><script>alert('bad')</script>]]></description>
<pubDate>Thu, 03 Sep 2026 17:17:08 Z</pubDate>
<a10:updated>2026-09-03T17:17:08Z</a10:updated></item>
<item><guid>older</guid><link>https://azure.microsoft.com/updates?id=older</link>
<title>Public Preview: something new</title><description>Useful</description>
<pubDate>Wed, 02 Sep 2026 17:00:51 Z</pubDate></item></channel></rss>"""
EMPTY_STATUS = b"""<rss version="2.0"><channel><title>Azure Status</title>
<link>https://azure.status.microsoft/en-us/status/</link>
<lastBuildDate>Sat, 05 Sep 2026 22:23:00 Z</lastBuildDate></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<title>Updates</title><updated>2026-09-04T09:00:00+02:00</updated>
<link rel="next" href="https://azure.microsoft.com/updates?page=2"/>
<entry><id>new</id><title>Retirement of an older feature</title>
<link href="https://learn.microsoft.com/azure/retirement"/>
<category term="Azure Test Service"/><summary type="html">&lt;p&gt;Plan &amp;amp; review&lt;/p&gt;</summary>
<published>2026-09-03T07:00:00Z</published><updated>2026-09-04T09:00:00+02:00</updated>
</entry></feed>"""


class Parsing(unittest.TestCase):
    def test_verified_rss_schema(self):
        data = feeds.parse_feed(RSS, "updates")
        self.assertEqual(data["total"], 2)
        item = data["items"][0]
        self.assertEqual(item["id"], "570557")
        self.assertEqual(item["category"], "GA")
        self.assertEqual(item["published_at"], "2026-09-03T17:17:08Z")
        self.assertEqual(item["services"], ["Azure Virtual Network Manager"])
        self.assertEqual(item["summary"], "Available & supported.")
        self.assertIn("Keyword label", item["customer_impact"])
        self.assertEqual(item["subscription_ids"], [])
        self.assertFalse(data["truncated"])
        self.assertEqual(data["coverage"]["pagination"], "not_advertised")

    def test_atom_dates_and_unfollowed_pagination(self):
        data = feeds.parse_feed(ATOM, "updates")
        item = data["items"][0]
        self.assertEqual(item["category"], "Retirement")
        self.assertEqual(item["updated_at"], "2026-09-04T07:00:00Z")
        self.assertEqual(item["summary"], "Plan & review")
        self.assertTrue(data["truncated"])
        self.assertEqual(data["coverage"]["pagination"], "not_followed")

    def test_dedupe_uses_latest_revision_then_canonical_url(self):
        extra = b"""<item><guid>570557</guid><title>Newest revision</title>
        <link>https://azure.microsoft.com/updates?id=570557</link>
        <a10:updated>2026-09-05T00:00:00Z</a10:updated></item>
        <item><guid>different-id</guid><title>Duplicate URL</title>
        <link>https://azure.microsoft.com/updates?id=570557</link>
        <a10:updated>2026-09-04T00:00:00Z</a10:updated></item>"""
        data = feeds.parse_feed(RSS.replace(b"</channel>", extra + b"</channel>"), "updates")
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["items"][0]["title"], "Newest revision")

    def test_no_categories_are_filtered_out(self):
        for title, category in (
            ("Migration notice", "Change"), ("Security update CVE-2026-1234", "Security"),
            ("Public preview release", "Preview"), ("Generally available feature", "GA"),
            ("Feature retirement", "Retirement"),
        ):
            payload = f"<rss><channel><item><title>{title}</title></item></channel></rss>".encode()
            self.assertEqual(feeds.parse_feed(payload, "updates")["items"][0]["category"], category)

    def test_payload_cap_xml_safety_and_invalid_response(self):
        for payload in (
            b"x" * (feeds.MAX_FEED_BYTES + 1),
            b'<!DOCTYPE rss [<!ENTITY a "abc">]><rss><channel>&a;</channel></rss>',
            '<!DOCTYPE rss><rss><channel/></rss>'.encode("utf-16"),
            b"<html><title>Not a feed</title></html>", b"<rss/>", b"<rss>",
            b"<rss><channel><item/></channel></rss>",
        ):
            with self.subTest(payload=payload[:70]), self.assertRaises(feeds.SourceError):
                feeds.parse_feed(payload, "updates")

    def test_output_cap_and_counts(self):
        with patch.object(feeds, "MAX_ITEMS", 1):
            data = feeds.parse_feed(RSS, "updates")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total"], 2)
        self.assertTrue(data["truncated"])

    def test_links_plain_text_and_invalid_dates(self):
        for link in ("javascript:alert(1)", "http://azure.microsoft.com/foo",
                     "https://microsoft.com.evil.invalid/foo", "https://user@microsoft.com/",
                     "https://microsoft.com:8080/", "//microsoft.com/foo",
                     'https://microsoft.com/"onclick="x', "https://127.0.0.1/",
                     "https://microsoft.com\\@evil.invalid/"):
            self.assertIsNone(feeds.official_url(link), link)
        self.assertIsNotNone(feeds.official_url("https://azure.status.microsoft/en-us/status/"))
        self.assertEqual(feeds.plain_text("&lt;p&gt;Hi&lt;/p&gt;<style>hidden</style>"), "Hi")
        self.assertIsNone(feeds.iso_date("not a date"))
        self.assertEqual(feeds.iso_date("2026-09-01"), "2026-09-01T00:00:00Z")
        payload = RSS.replace(b"https://azure.microsoft.com/updates?id=570557", b"javascript:bad")
        self.assertIsNone(feeds.parse_feed(payload, "updates")["items"][0]["url"])


class CacheAndTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache_patch = patch.object(feeds, "_public_cache", feeds.SourceCache(feeds.PUBLIC_TTL, 2))
        self.cache_patch.start()

    async def asyncTearDown(self):
        self.cache_patch.stop()

    async def test_independent_sources_empty_success_and_error(self):
        async def download(url):
            if url == feeds.UPDATES_URL:
                raise httpx.ConnectError("do not expose server addresses or secrets")
            return EMPTY_STATUS

        with patch.object(feeds, "_download", download):
            updates, status = await asyncio.gather(
                feeds.public_feed("updates"), feeds.public_feed("status"))
        self.assertEqual(updates["status"], "error")
        self.assertEqual(updates["items"], [])
        self.assertIsNone(updates["freshness"]["fetched_at"])
        self.assertNotIn("secrets", updates["error"])
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["count"], 0)
        self.assertIn("No active public incidents reported by this feed", status["notice"])
        self.assertFalse(status["freshness"]["stale"])
        self.assertNotIn("all services healthy", status["notice"].lower())

    async def test_stale_snapshot_and_failed_refresh_are_not_success(self):
        with patch.object(feeds, "_download", AsyncMock(side_effect=[RSS, TimeoutError()])) as download:
            first = await feeds.public_feed("updates")
            hit = await feeds.public_feed("updates")
            failed = await feeds.public_feed("updates", refresh=True)
        self.assertEqual(download.await_count, 2)
        self.assertFalse(first["freshness"]["cached"])
        self.assertTrue(hit["freshness"]["cached"])
        self.assertEqual(failed["items"], first["items"])
        self.assertEqual(failed["status"], "error")
        self.assertTrue(failed["freshness"]["stale"])
        self.assertTrue(failed["freshness"]["cached"])
        self.assertEqual(failed["freshness"]["fetched_at"], first["freshness"]["fetched_at"])
        self.assertIn("timed out", failed["error"])
        self.assertIn("last successfully fetched", failed["notice"])

    async def test_ttl_forces_refetch_and_results_are_copies(self):
        with patch.object(feeds, "_download", AsyncMock(return_value=RSS)) as download:
            first = await feeds.public_feed("updates")
            first["items"].clear()
            snapshot = feeds._public_cache.entries["updates"]
            snapshot.attempted_at -= feeds.PUBLIC_TTL + 1
            snapshot.fetched_at -= feeds.PUBLIC_TTL + 1
            second = await feeds.public_feed("updates")
        self.assertEqual(download.await_count, 2)
        self.assertEqual(second["count"], 2)

    async def test_forced_refresh_single_flight_and_cancelled_reader(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def download(url):
            entered.set()
            await release.wait()
            return RSS

        with patch.object(feeds, "_download", AsyncMock(side_effect=download)) as mock:
            first = asyncio.create_task(feeds.public_feed("updates", True))
            await entered.wait()
            other = asyncio.create_task(feeds.public_feed("updates", True))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            result = await other
        self.assertEqual(mock.await_count, 1)
        self.assertEqual(result["status"], "ok")

    async def test_cache_entry_bound(self):
        cache = feeds.SourceCache(600, 2)
        for key in ("a", "b", "c"):
            await cache.get(key, feeds.SOURCES["updates"], AsyncMock(return_value={"items": []}))
        self.assertEqual(list(cache.entries), ["b", "c"])

    async def test_shutdown_cancels_shared_fetches(self):
        entered = asyncio.Event()

        async def download(url):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(feeds, "_download", download):
            reader = asyncio.create_task(feeds.public_feed("updates"))
            await entered.wait()
            await feeds._public_cache.close()
            with self.assertRaises(asyncio.CancelledError):
                await reader
        self.assertFalse(feeds._public_cache.flights)
        self.assertFalse(feeds._public_cache.entries)

    async def test_stream_size_and_redirects_are_bounded(self):
        real_client = httpx.AsyncClient
        for response in (
            httpx.Response(200, headers={"Content-Length": str(feeds.MAX_FEED_BYTES + 1)}),
            httpx.Response(200, content=b"x" * (feeds.MAX_FEED_BYTES + 1)),
            httpx.Response(302, headers={"Location": "https://evil.invalid/"}),
        ):
            calls = []

            def handler(request, response=response, calls=calls):
                calls.append(request.url)
                return response

            transport = httpx.MockTransport(handler)
            with (
                patch.object(feeds.httpx, "AsyncClient",
                             lambda transport=transport, **kwargs: real_client(transport=transport, **kwargs)),
                self.assertRaises((feeds.SourceError, httpx.HTTPStatusError)),
            ):
                await feeds._download(feeds.UPDATES_URL)
            self.assertEqual(len(calls), 1)

    async def test_live_schema_via_mock_http(self):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=RSS))
        with patch.object(feeds.httpx, "AsyncClient",
                          lambda **kwargs: real_client(transport=transport, **kwargs)):
            data = await feeds.public_feed("updates")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["count"], 2)


if __name__ == "__main__":
    unittest.main()
