import assert from "node:assert/strict";
import { mountAdvisorScore, scoreMarkup, scoreText, scoreDateRange, checkedTime } from "../web/assets/advisor-score.js";
import { sectionFor, groupedNavigation } from "../web/assets/dashboard-navigation.js";
assert.equal(scoreText({ status: "available", score: 0 }), "0.0 / 100");
assert.equal(scoreText({ status: "unavailable", score: null }), "Not available");
const categories = ["Cost", "Security", "HighAvailability", "OperationalExcellence", "Performance"]
  .map(id => ({ id, label: id }));
const data = { categories, fetched_at: "2026-11-11", subscriptions: [
  { subscription_id: "one", status: "available", scores: Object.fromEntries(
    categories.map(c => [c.id, { status: "available", score: 80, date: "2026-11-10" }])) },
  { subscription_id: "two", status: "forbidden", message: "Reader required", scores: {} },
] };
const html = scoreMarkup(data, String, String, true);
assert.match(html, /<footer class="advisor-score-meta">/);
assert.match(html, /<details><summary>Per subscription · ranges across subscriptions<\/summary>/);
assert.match(html, /not an aggregated Advisor score/);
assert.equal(scoreDateRange(["2026-11-09T00:00:00.0000000Z", "2026-11-10T00:00:00.0000000Z"]).replace(/\s/g, " "), "9–10 Nov 2026");
assert.equal(scoreDateRange(["2026-11-10"]), "10 Nov 2026");
assert.equal(scoreDateRange(["bad"]), "not available");
assert.match(scoreDateRange(["2025-12-31", "2026-01-01"]), /2025.*2026/);
assert.equal(checkedTime("bad"), "unknown");
assert.equal(checkedTime("2026-11-11T06:50:17"), "unknown");
const stamp = "2026-11-11T06:50:17.024895+00:00";
assert.equal(checkedTime(stamp), new Date(stamp).toLocaleTimeString("en-GB", {
  hour: "2-digit", minute: "2-digit", timeZoneName: "short",
}));
assert.ok(scoreMarkup({ ...data, fetched_at: stamp }, String, String, true).includes(stamp));
assert.ok(!html.includes('<p class="muted">'));
for (const c of categories) assert.ok(html.includes(c.label));
assert.ok(html.includes('80.0–80.0<span class="advisor-pillar-scale"> / 100 range'));
assert.ok(html.includes("1/2 subscriptions"));
assert.ok(html.includes("Reader required"));
assert.ok(html.includes("data-advisor-score-tab"));
assert.ok(!html.includes("NaN"));
assert.ok(html.includes("Partial coverage"));
assert.equal((html.match(/class="advisor-pillar-heading"/g) || []).length, 5);
assert.ok(html.includes('aria-hidden="true"'));
const single = scoreMarkup({ categories, subscriptions: [data.subscriptions[0]] }, String, String, true);
assert.ok(single.includes('80.0<span class="advisor-pillar-scale"> / 100'));
assert.ok(!single.includes(" / 100 range"));
const missing = scoreMarkup({ categories, subscriptions: [data.subscriptions[1]] }, String, String, true);
assert.ok(missing.includes("Not available"));
assert.ok(missing.includes("Unavailable"));
assert.ok(!missing.includes('class="advisor-pillar-fill"'));
const ranged = structuredClone(data);
ranged.subscriptions[1] = { ...data.subscriptions[0], scores: { Cost: { status: "available", score: 95 } } };
assert.ok(scoreMarkup(ranged, String, String, true).includes("80.0–95.0"));
const views = [{ id: "overview" }, { id: "advisor-score" }];
assert.equal(sectionFor("advisor-score", views).id, "governance");
assert.ok(groupedNavigation(views).some(g => g.views.some(v => v.id === "advisor-score")));
let navigated;
const button = {};
const root = { isConnected: true, innerHTML: "", querySelector: () => button };
const ctx = { loadToken: 1, qs: () => "?scope=one,two", esc: String, subName: String,
  navigate: value => { navigated = value; },
  get: async url => { assert.equal(url, "/api/dashboard/advisor-score?scope=one,two"); return data; } };
await mountAdvisorScore(ctx, 1, root, true);
assert.ok(root.innerHTML.includes("2026-11-11"));
button.onclick();
assert.equal(navigated, "advisor-score");
ctx.get = async () => { throw new Error("Permission denied"); };
await mountAdvisorScore(ctx, 1, root, true);
assert.ok(root.innerHTML.includes("Permission denied"));
assert.ok(root.innerHTML.includes("export reporting access does not grant Advisor access"));
root.innerHTML = "newer navigation";
ctx.get = async () => data;
await mountAdvisorScore(ctx, 0, root, true);
assert.equal(root.innerHTML.includes("Cost"), false, "an older request cannot paint scores");
console.log("Advisor score frontend assertions passed");
