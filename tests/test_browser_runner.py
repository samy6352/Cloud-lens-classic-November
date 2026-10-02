"""The browser harness must surface a failed dashboard rather than wait for success."""
from tests.test_tags_frontend import run_node


def test_readiness_wait_honours_success_error_and_pending_states():
    run_node(r"""
import assert from "node:assert/strict";
import {waitForDashboard} from "./tests/browser_runner_readiness.mjs";
const root = {dataset:{dashboardReady:"false"}};
let detail = null;
globalThis.document = {
  documentElement:root,
  querySelector:selector => selector === "#tabBody .banner.err" && detail ? {textContent:detail} : null,
};
const page = {
  async waitForFunction(predicate, argument, options) {
    assert.equal(argument,null);
    assert.equal(options.timeout,120000);
    root.dataset.dashboardReady="false";
    assert.equal(predicate(),false);
    root.dataset.dashboardReady="error";
    assert.equal(predicate(),true);
    root.dataset.dashboardReady=this.next;
    assert.equal(predicate(),true);
  },
  evaluate:fn => fn(),
};
page.next="true";
await waitForDashboard(page);
page.next="error";
detail="The request timed out. Please retry.";
await assert.rejects(waitForDashboard(page),/Dashboard startup failed: The request timed out/);
detail=null;
await assert.rejects(waitForDashboard(page),/The view entered an error state/);
page.waitForFunction=async()=>{throw new Error("Readiness timeout");};
await assert.rejects(waitForDashboard(page),/Readiness timeout/);
""")
