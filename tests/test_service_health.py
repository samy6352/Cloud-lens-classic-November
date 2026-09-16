"""Offline Service Health scope, token isolation, pagination and resource associations."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

os.environ.setdefault("CLOUDLENS_ENV_FILE", str(Path(__file__).parent / "absent.env"))

from fastapi import HTTPException
from starlette.requests import Request

from app import azure_updates as feeds
from app import cost, main
from app import service_health as health
from app.auth import User

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
USER = User("alice", source="entra", email="alice@example.invalid", sid="session-a")


def event(sub=A, tracking="ABCD-123", event_type="ServiceIssue", status="Active"):
    return {
        "id": f"/subscriptions/{sub}/providers/Microsoft.ResourceHealth/events/{tracking}",
        "eventId": tracking, "tracking": tracking, "subscriptionId": sub,
        "headline": "<p>Reported service event</p>", "summary": "Source summary",
        "eventType": event_type, "status": status, "level": "Warning",
        "updatedTicks": "2026-09-05T09:00:00Z", "startTicks": "2026-09-05T08:00:00Z",
        "actions": "<p>Follow the official mitigation.</p>",
        "impact": '[{"ImpactedService":"Azure Storage","ImpactedRegions":[{"RegionName":"West US"}]}]',
    }


def resource(sub=A, name="disk-a", tracking="ABCD-123"):
    return {
        "subscriptionId": sub, "tracking": tracking,
        "resourceId": f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Compute/disks/{name}",
        "resourceName": name, "resourceType": "Microsoft.Compute/disks", "region": "West US",
    }


class Normalization(unittest.TestCase):
    def test_all_types_and_resolved_events_are_preserved(self):
        types = ["ServiceIssue", "PlannedMaintenance", "HealthAdvisory", "SecurityAdvisory", "Billing"]
        rows = [event(tracking=str(index), event_type=kind,
                      status="Resolved" if index % 2 else "Active") for index, kind in enumerate(types)]
        items = health.normalize_events(rows, [], [A])
        self.assertEqual({item["category"] for item in items}, set(types))
        self.assertEqual({item["status"] for item in items}, {"Active", "Resolved"})
        self.assertNotIn("where tostring(p.EventType)", health.EVENTS_QUERY)
        self.assertEqual(items[0]["services"], ["Azure Storage"])
        self.assertEqual(items[0]["regions"], ["West US"])
        self.assertEqual(items[0]["actions"], "Follow the official mitigation.")

    def test_tracking_subscription_join_cannot_mix_resources(self):
        rows = [event(A), event(B)]
        resources = [resource(A), resource(A), resource(B, "disk-b"), resource(C, "hidden")]
        wrong_sub_resource = resource(A, "cross-sub")
        wrong_sub_resource["resourceId"] = resource(B, "cross-sub")["resourceId"]
        resources.append(wrong_sub_resource)
        items = health.normalize_events(rows, resources, [A, B])
        by_sub = {item["subscription_ids"][0]: item for item in items}
        self.assertEqual(len(items), 2)
        self.assertEqual(by_sub[A]["resource_count"], 1)
        self.assertEqual(by_sub[B]["resource_count"], 1)
        self.assertEqual(by_sub[A]["resources"][0]["name"], "disk-a")
        self.assertEqual(by_sub[B]["resources"][0]["name"], "disk-b")

    def test_event_name_alias_join_and_latest_revision(self):
        old = event()
        old["eventId"] = "azure-event-name"
        newer = dict(old, updatedTicks="2026-09-06T09:00:00Z", status="Resolved")
        items = health.normalize_events([old, newer], [resource(tracking="AZURE-EVENT-NAME")], [A])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "Resolved")
        self.assertEqual(items[0]["resource_count"], 1)

    def test_resource_caps_and_observed_counts(self):
        with patch.object(health, "MAX_RESOURCES", 1):
            item = health.normalize_events(
                [event()], [resource(name="a"), resource(name="b")], [A])[0]
        self.assertEqual(item["resource_count"], 2)
        self.assertEqual(len(item["resources"]), 1)
        self.assertTrue(item["resources_truncated"])
        self.assertEqual(item["resource_count_kind"], "observed")

    def test_ticks_preserve_time_and_invalid_dates_are_null(self):
        now = datetime(2026, 9, 5, 10, 23, 15, tzinfo=UTC)
        ticks = (now - datetime(1, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 10
        self.assertEqual(health.health_date(str(ticks)), "2026-09-05T10:23:15Z")
        for value in ("99999999999999999999", "wrong", "0", None):
            self.assertIsNone(health.health_date(value))

    def test_scope_validation(self):
        self.assertEqual(health.normalize_scopes([" " + B + " ", A, B]), [A, B])
        for scopes in ([], [""], ["not-guid"], [A, ""], [A] * (health.MAX_SCOPES + 1)):
            with self.assertRaises(feeds.SourceError):
                health.normalize_scopes(scopes)


class QueriesAndCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache_patch = patch.object(health, "_cache", feeds.SourceCache(health.HEALTH_TTL, 32))
        self.cache_patch.start()

    async def asyncTearDown(self):
        self.cache_patch.stop()

    async def test_pagination_continuation_and_totals(self):
        pages = [
            {"data": [event()], "totalRecords": 2, "$skipToken": "next", "resultTruncated": "true"},
            {"data": [event(tracking="second")], "totalRecords": 2, "resultTruncated": "false"},
        ]
        with patch.object(health, "_arg_page", AsyncMock(side_effect=pages)) as query:
            result = await health._query(health.EVENTS_QUERY, [("token", [A])])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(query.await_args_list[1].args[3]["$skipToken"], "next")

    async def test_pagination_skip_fallback_and_cap(self):
        pages = [
            {"data": [event()], "totalRecords": 3, "resultTruncated": "true"},
            {"data": [event(tracking="second")], "totalRecords": 3, "resultTruncated": "true"},
        ]
        with patch.object(health, "MAX_PAGES", 2), patch.object(
            health, "_arg_page", AsyncMock(side_effect=pages)
        ) as query:
            result = await health._query(health.EVENTS_QUERY, [("token", [A])])
        self.assertEqual(query.await_args_list[1].args[3]["$skip"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 3)

    async def test_out_of_scope_rows_and_broken_continuation_are_errors(self):
        with (
            patch.object(health, "_arg_page", AsyncMock(return_value={"data": [event(B)]})),
            self.assertRaises(feeds.SourceError),
        ):
            await health._query(health.EVENTS_QUERY, [("token", [A])])
        repeated = {"data": [event()], "totalRecords": 5, "$skipToken": "same"}
        with (
            patch.object(health, "_arg_page", AsyncMock(return_value=repeated)),
            self.assertRaises(feeds.SourceError),
        ):
            await health._query(health.EVENTS_QUERY, [("token", [A])])

    async def test_token_groups_use_each_subscription_tenant(self):
        caller = cost.Caller("home", {A: "tenant-a", B: "tenant-b", C: "tenant-a"})
        reset = cost.act_as(caller, "actor")
        try:
            groups = await health._groups([A, B, C])
        finally:
            cost.stop_acting(reset)
        self.assertEqual(groups, [("tenant-a", [A, C]), ("tenant-b", [B])])

    async def test_arg_http_uses_explicit_scope_token_and_stream_bounds(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [event()], "totalRecords": 1})

        client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(health.httpx, "AsyncClient",
                          lambda **kwargs: client(transport=transport, **kwargs)):
            result = await health._arg_page(health.EVENTS_QUERY, [A], "delegated-token", {})
            with self.assertRaises(feeds.SourceError):
                await health._arg_page(health.EVENTS_QUERY, [], "delegated-token", {})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer delegated-token")
        self.assertEqual(json.loads(requests[0].content)["subscriptions"], [A])
        self.assertEqual(result["totalRecords"], 1)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Length": str(health.MAX_ARG_BYTES + 1)}))
        with (
            patch.object(health.httpx, "AsyncClient",
                         lambda **kwargs: client(transport=transport, **kwargs)),
            self.assertRaises(feeds.SourceError),
        ):
            await health._arg_page(health.EVENTS_QUERY, [A], "delegated-token", {})

    async def test_resource_query_failure_does_not_claim_empty_health(self):
        events = {"rows": [event()], "pages": 1, "total": 1, "truncated": False}
        with patch.object(health, "_groups", AsyncMock(return_value=[("token", [A])])), patch.object(
            health, "_query", AsyncMock(side_effect=[events, TimeoutError()])
        ):
            result = await health.service_health(USER, [A])
        self.assertEqual(result["status"], "error")
        self.assertIn("timed out", result["error"])
        self.assertNotIn("No Service Health events", result["notice"])

    async def test_scope_actor_and_session_isolated_caches(self):
        async def load(scopes):
            return {"items": health.normalize_events([event(scopes[0])], [], scopes)}

        users = [
            USER, User("bob", source="entra", email="bob@example.invalid", sid="session-b"),
            User("alice", source="entra", email=USER.email, sid="new-session"),
            User("alice", source="file", email=USER.email, sid=USER.sid),
        ]
        with patch.object(health, "_load", AsyncMock(side_effect=load)) as query:
            for user in users:
                await health.service_health(user, [A])
            await health.service_health(USER, [B])
            hit = await health.service_health(USER, [A])
            empty = await health.service_health(USER, [])
        self.assertEqual(query.await_count, 5)
        self.assertTrue(hit["freshness"]["cached"])
        self.assertEqual(empty["status"], "error")
        self.assertEqual(hit["items"][0]["subscription_ids"], [A])

    async def test_health_stale_cache_keeps_error_explicit(self):
        data = {"items": health.normalize_events([event()], [], [A]), "notice": health.NOTICE}
        with patch.object(health, "_load", AsyncMock(side_effect=[data, TimeoutError()])):
            first = await health.service_health(USER, [A])
            stale = await health.service_health(USER, [A], True)
        self.assertEqual(stale["items"], first["items"])
        self.assertEqual(stale["status"], "error")
        self.assertTrue(stale["freshness"]["stale"])
        self.assertEqual(stale["freshness"]["max_age_seconds"], 120)

    async def test_load_coverage_capped_items_and_resources(self):
        events = {"rows": [event(), event(tracking="other")], "pages": 1,
                  "total": 2, "truncated": False}
        resources = {"rows": [], "pages": 10, "total": 4000, "truncated": True}
        with patch.object(health, "_groups", AsyncMock(return_value=[("token", [A])])), patch.object(
            health, "_query", AsyncMock(side_effect=[events, resources])
        ), patch.object(health, "MAX_EVENTS", 1):
            data = await health._load([A])
        self.assertTrue(data["truncated"])
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["items"]), 1)
        self.assertTrue(data["items"][0]["resources_truncated"])
        self.assertTrue(data["coverage"]["resources_truncated"])


class Routes(unittest.IsolatedAsyncioTestCase):
    def request(self, user=USER):
        request = Request({"type": "http", "method": "GET", "path": "/"})
        if user is not None:
            request.state.user = user
        return request

    async def test_public_routes_require_auth(self):
        for route in (main.dashboard_azure_updates, main.dashboard_azure_status):
            with self.assertRaises(HTTPException) as caught:
                await route(self.request(None))
            self.assertEqual(caught.exception.status_code, 401)

    async def test_routes_preserve_error_envelopes_and_independence(self):
        success = feeds.envelope({"items": [], "truncated": False, "notice": "Public"},
                                 feeds.SOURCES["status"], 600)
        failed = feeds.envelope(None, feeds.SOURCES["updates"], 600, error="Unavailable")

        async def reader(kind, refresh):
            return success if kind == "status" else failed

        with patch.object(feeds, "public_feed", AsyncMock(side_effect=reader)) as fetch:
            updates = await main.dashboard_azure_updates(self.request(), True)
            status = await main.dashboard_azure_status(self.request(), False)
        self.assertEqual(json.loads(updates.body)["status"], "error")
        self.assertEqual(json.loads(status.body)["status"], "ok")
        self.assertEqual(updates.headers["cache-control"], "no-store")
        self.assertEqual(fetch.await_args_list[0].args, ("updates", True))
        self.assertEqual(fetch.await_args_list[1].args, ("status", False))

    async def test_empty_forbidden_or_invalid_scopes_never_reach_fetch(self):
        with patch.object(main, "permitted", AsyncMock(return_value=[A])), patch.object(
            health, "service_health", AsyncMock()
        ) as fetch:
            for scope, status in (("", 400), (B, 403), (A + "," + B, 403), ("nope", 400)):
                response = await main.dashboard_service_health(self.request(), scope)
                self.assertEqual(response.status_code, status)
                self.assertEqual(json.loads(response.body)["status"], "error")
            fetch.assert_not_called()

    async def test_non_entra_empty_scope_never_expands_to_all(self):
        user = User("local", source="file")
        with patch.object(main, "permitted", AsyncMock(return_value=None)), patch.object(
            health, "service_health", AsyncMock()
        ) as fetch:
            response = await main.dashboard_service_health(self.request(user), "")
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()

    async def test_route_narrows_and_uses_real_as_user_context(self):
        original_actor = cost.acting_as()
        caller = cost.Caller("home", {A: "token-a", B: "token-b"})
        session = SimpleNamespace(oid="user-oid", sid=USER.sid)
        auth = SimpleNamespace(sso=SimpleNamespace(
            delegated=True, get=Mock(return_value=session), caller=AsyncMock(return_value=caller)))
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def load(scopes):
            calls.append((cost.acting_as(), cost.caller_token(f"{cost.ARM}/subscriptions/{A}"), scopes))
            entered.set()
            await release.wait()
            return {"items": [], "notice": health.NOTICE, "truncated": False}

        with patch.object(main, "permitted", AsyncMock(return_value=[A, B])), patch.object(
            main, "auth_service", return_value=auth
        ), patch.object(main, "narrow", wraps=main.narrow) as narrow, patch.object(
            health, "_load", load
        ), patch.object(health, "_cache", feeds.SourceCache(120, 32)):
            first = asyncio.create_task(main.dashboard_service_health(self.request(), A, True))
            await entered.wait()
            other = asyncio.create_task(main.dashboard_service_health(self.request(), A, True))
            await asyncio.sleep(0)
            release.set()
            responses = await asyncio.gather(first, other)
        self.assertEqual(calls, [("user-oid", "token-a", [A])])
        narrow.assert_called_with([A], [A, B])
        self.assertEqual(cost.acting_as(), original_actor)
        for response in responses:
            self.assertEqual(json.loads(response.body)["status"], "ok")
            self.assertEqual(response.headers["cache-control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
