"""Manual-refresh cache policy, request deduplication and failed-request recovery."""
from tests.test_tags_frontend import run_node


def test_requests_survive_age_until_cleared_and_failures_remain_retryable():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source = fs.readFileSync("./web/assets/dashboard.js", "utf8");
const cache = new Map();
const make = new Function("cache", "CACHE_MAX",
  source.match(/async function cached\(key, fetcher\) \{[\s\S]*?\n\}/)[0] + ";return cached;");
const cached = make(cache, 40);
let calls = 0, release;
const first = cached("resources|scope-a|USD", () => {
  calls++;
  return new Promise(resolve => { release = resolve; });
});
const concurrent = cached("resources|scope-a|USD", () => { throw new Error("Duplicate request"); });
await Promise.resolve();
release({total: 123});
assert.deepEqual(await first, await concurrent);
const now = Date.now;
try {
  Date.now = () => now() + 86400000;
  assert.deepEqual(await cached("resources|scope-a|USD", () => { calls++; return {total:999}; }), {total:123});
  assert.equal(calls, 1, "successful requests have no five-minute expiry");
  assert.deepEqual(await cached("resources|scope-b|USD", () => ({total:456})), {total:456});
  cache.clear();
  assert.deepEqual(await cached("resources|scope-a|USD", () => { calls++; return {total:999}; }), {total:999});
  assert.equal(calls, 2, "explicit refresh invalidates results");
} finally { Date.now = now; }
await assert.rejects(cached("failure", () => {throw new Error("Unavailable");}), /Unavailable/);
assert.equal(await cached("failure", () => "recovered"), "recovered");
let rejectOld;
const old = cached("race", () => new Promise((resolve,reject) => {rejectOld=reject;}));
await Promise.resolve();
cache.clear();
assert.equal(await cached("race", () => "new"), "new");
rejectOld(new Error("old request failed"));
await assert.rejects(old, /old request failed/);
assert.equal(await cached("race", () => {throw new Error("New result was lost");}), "new");
for(let i=0;i<45;i++) await cached(`extra-${i}`, () => i);
assert.equal(cache.size, 40, "request memory remains bounded");
""")


def test_late_recommendations_belong_to_their_snapshot_not_a_replacement_view():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source = fs.readFileSync("./web/assets/dashboard-optimize.js", "utf8");
const make = new Function("$", "get", "qs",
  source.match(/async function loadRecommendation\(\) \{[\s\S]*?\n\}/)[0] + ";return loadRecommendation;");
for (const connected of [true, false]) {
  for (const failed of [true, false]) {
    const old = {isConnected:true, textContent:"pending", classList:{remove(){},add(){}}};
    const replacement = {textContent:"different scope"};
    let visible = old, finish;
    const load = make(() => visible, () => new Promise((resolve,reject) => {
      finish = () => failed ? reject(new Error("Unavailable")) : resolve({narrative:"Summary"});
    }), () => "?scope=a");
    const pending = load();
    visible = replacement;
    old.isConnected = connected;
    finish();
    await pending;
    assert.equal(replacement.textContent,"different scope", "an old response cannot touch a replacement card");
    assert.equal(old.textContent, connected ? failed ? "Unavailable" : "Summary" : "pending");
  }
}
""")
