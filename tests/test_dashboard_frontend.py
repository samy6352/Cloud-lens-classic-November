"""Dashboard module boundaries and deterministic progressive-scan lifecycle tests."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"


def test_summary_is_beside_the_full_height_rail():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert html.index('id="firstRun"') < html.index('id="shell"')
    assert html.index('id="shell"') < html.index('id="tabs"') < html.index('<main class="board">')
    assert html.index('class="board-summary"') < html.index('id="glance"')
    assert html.index('id="glance"') < html.index('id="statusRow"') < html.index('id="tabBody"')
    assert html.index('id="statusRow"') < html.index('id="statusActions"') < html.index('id="dataState"') < html.index('id="viewActions"')
    assert html.count('id="statusActions"') == 1
    assert html.count('id="tabs"') == 1
    assert html.count('id="firstRun"') == 1


def test_azure_updates_links_and_customer_filters():
    script = r"""
import assert from "node:assert/strict";
import { officialUrl, filterUpdates, needsAttention } from "./web/assets/dashboard-updates.js";
assert.equal(officialUrl("https://azure.microsoft.com/en-us/updates/new-service"), "https://azure.microsoft.com/en-us/updates/new-service");
assert.equal(officialUrl("https://azure.status.microsoft/en-us/status/"), "https://azure.status.microsoft/en-us/status/");
for (const url of ["javascript:alert(1)", "http://azure.microsoft.com/", "https://azure.microsoft.com.evil.test/",
  "https://user:password@portal.azure.com/", "https://portal.azure.com:444/"]) assert.equal(officialUrl(url), null);
const items = [
  {title:"API retirement",category:"Retirement",services:["API Management"],regions:[],summary:"Review migration options"},
  {title:"New feature",category:"Generally available",services:["Storage"],regions:["West Europe"],summary:"New capacity"},
  {title:"Security notice",category:"Security",services:[],regions:[],summary:"Action required"},
];
assert.equal(needsAttention(items[0]), true);
assert.equal(needsAttention(items[1]), false);
assert.equal(filterUpdates(items, {attention:true}).length, 2);
assert.equal(filterUpdates(items, {query:"west europe"}).length, 1);
assert.equal(filterUpdates(items, {query:"api",category:"Retirement"}).length, 1);
assert.equal(filterUpdates(items, {category:"Generally available",attention:true}).length, 0);
assert.equal(filterUpdates([{status:"Active"},{status:"Inactive"},{status:"Resolved"}], {status:"Active"}).length, 1);
"""
    result = subprocess.run(
        [node(), "--input-type=module", "-e", script], cwd=ROOT, stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def node():
    executable = shutil.which("node")
    bundled = Path(os.environ.get("PROGRAMFILES(X86)", "")) / "scout" / "resources" / "node" / "node.exe"
    if not executable and bundled.is_file():
        executable = str(bundled)
    if not executable:
        pytest.skip("Node.js is needed for frontend execution tests")
    return executable


def test_evidence_filters_keep_zero_unknown_currency_and_facets_distinct():
    script = r"""
import assert from "node:assert/strict";
import { evidenceFacets, filterEvidence, evidenceNotices } from "./web/assets/dashboard-evidence.js";
const notices=evidenceNotices([
  "Read-only Azure Resource Graph Change Analysis; change records are retained for up to 14 days.",
  "3 resources had denied metric reads.",
  "2 resources have no matching cost evidence.",
  "An unrecognized source warning must stay visible.",
]);
assert.equal(notices.methodology.length,1);
assert.equal(notices.warnings.length,3);
assert.match(notices.warnings.join(" "),/denied.*no matching cost.*unrecognized/);
const root="/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/";
const rows=[
  {id:root+"Prod/providers/Microsoft.Web/sites/app",name:"app",type:"microsoft.web/sites",
   region:"EastUS",actor:"Alice@Example.com",change_type:"Update",status:"partial",
   properties:[{name:"sku.capacity",before:1,after:2}],
   metrics:[{name:"Requests",status:"ok",value:0}],
   cost:{by_currency:[{currency:"USD",cost:0},{currency:"EUR",cost:3}]}},
  {id:root+"dev/providers/Microsoft.Compute/virtualMachines/vm",name:"vm",
   type:"microsoft.compute/virtualmachines",region:"westus",actor:null,change_type:"Create",
   status:"unavailable",metrics:[{name:"CPU",status:"no_data",value:null}],cost:null},
  {id:root+"PROD/providers/Microsoft.Web/sites/other",name:"other",type:"microsoft.web/sites",
   region:"eastus",actor:"alice@example.com",change_type:"Delete",status:"partial",
   metrics:[{name:"CPU",status:"unavailable",value:null}],
   cost:{by_currency:[{currency:"USD",cost:-3},{currency:"EUR",cost:4}]}},
];
assert.equal(evidenceFacets(rows,"group").length,2);
assert.equal(evidenceFacets(rows,"actor").length,2);
assert.equal(filterEvidence(rows,{group:"value:prod"}).length,2);
assert.equal(filterEvidence(rows,{region:"value:eastus",change:"value:update"}).length,1);
assert.equal(filterEvidence(rows,{type:"value:microsoft.web/sites",actor:"value:alice@example.com"}).length,2);
assert.deepEqual(filterEvidence(rows,{actor:"__not_supplied__"}).map(x=>x.name),["vm"]);
assert.deepEqual(filterEvidence(rows,{coverage:"measured"}).map(x=>x.name),["app"]);
assert.equal(filterEvidence(rows,{coverage:"missing"}).length,2);
assert.deepEqual(filterEvidence(rows,{coverage:"failed"}).map(x=>x.name),["other"]);
assert.equal(filterEvidence(rows,{coverage:"partial"}).length,2);
assert.deepEqual(filterEvidence(rows,{cost:"missing"}).map(x=>x.name),["vm"]);
assert.equal(filterEvidence(rows,{cost:"present"}).length,2);
assert.deepEqual(filterEvidence(rows,{currency:"value:usd",cost:"zero"}).map(x=>x.name),["app"]);
assert.equal(filterEvidence(rows,{cost:"zero"}).length,0);
assert.equal(filterEvidence(rows,{currency:"value:usd",cost:"positive"}).length,0);
assert.equal(filterEvidence(rows,{currency:"value:eur",cost:"positive"}).length,2);
assert.equal(filterEvidence(rows,{currency:"value:jpy"}).length,0);
assert.deepEqual(filterEvidence(rows,{query:"SKU.capacity"}).map(x=>x.name),["app"]);
assert.deepEqual(filterEvidence(rows,{query:"  Alice@Example.COM  "}).map(x=>x.name),["app","other"]);
assert.equal(filterEvidence(rows,{query:"not-found"}).length,0);
assert.equal(filterEvidence(rows,{}).length,3);
const unknown={metrics:[{status:"ok",value:null}],cost:{by_currency:[{currency:"USD",cost:null}]}};
assert.equal(filterEvidence([unknown],{coverage:"measured"}).length,0);
assert.equal(filterEvidence([unknown],{cost:"zero"}).length,0);
assert.equal(filterEvidence([unknown],{cost:"missing"}).length,1);
"""
    result = subprocess.run([node(), "--input-type=module", "-e", script], cwd=ROOT,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_refresh_menu_executes_latest_focus_and_quick_actions():
    script = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/dashboard.js","utf8");
const menu={dataset:{},innerHTML:"",querySelector(selector){return buttons[selector];}};
const buttons={"[data-act='latest']":{},"[data-act='focus']":{},"[data-act='quick']":{}};
const calls=[];
const startIngest=(...args)=>calls.push(args);
const startLatestFocus=()=>calls.push(["latest"]);
const build=new Function("$","startIngest","paintArchiveNote","startLatestFocus",
  source.match(/function buildRefreshMenu\(\)\s*\{[\s\S]*?\n\}/)[0]+"; return buildRefreshMenu;");
build(()=>menu,startIngest,()=>{},startLatestFocus)();
assert.deepEqual([...menu.innerHTML.matchAll(/data-act="([^"]+)"/g)].map(m=>m[1]),["latest","focus","quick"]);
buttons["[data-act='latest']"].onclick();
buttons["[data-act='focus']"].onclick();
buttons["[data-act='quick']"].onclick();
assert.deepEqual(calls,[
  ["latest"],
  ["/api/exports/ingest","POST",{max_files:60,metric:"FocusCost"}],
  ["/api/ingest?months=3&metric=ActualCost&quick=true","POST"]
]);
assert.match(menu.innerHTML,/WebDirect/);
assert.match(menu.innerHTML,/kept unchanged/);
"""
    result = subprocess.run(
        [node(), "--input-type=module", "-e", script], cwd=ROOT, stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_lazy_modules_have_no_reverse_imports_and_parse():
    shell = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    assert len(shell) < 100_000
    render = shell[shell.index("async function renderTabContent("):shell.index("function skeleton(")]
    assert render.index('body.classList.remove("tab-enter")') < render.index("if (saved)")
    assert "cancelAnimationFrame(arrivalFrame)" in render
    assert 'if (mine !== loadToken) return;\n          body.classList.add("tab-enter")' in render
    for family in ("spend", "optimize", "monitor", "reports"):
        source = (ASSETS / f"dashboard-{family}.js").read_text(encoding="utf-8")
        assert f'import("./dashboard-{family}.js")' in shell
        assert "export function createTabs(ctx)" in source
        assert 'from "./dashboard.js"' not in source
        assert 'import("./dashboard.js")' not in source
        assert len(source) > 20_000
    for path in ASSETS.glob("dashboard*.js"):
        result = subprocess.run(
            [node(), "--check", str(path)], stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


def test_progress_scan_force_once_abort_resume_error_and_bound():
    script = r"""
import assert from "node:assert/strict";
import { createSavingsScan } from "./web/assets/dashboard-savings-scan.js";
import { stableKey } from "./web/assets/dashboard-state.js";
import { reportReadiness } from "./web/assets/dashboard-performance.js";
import { createTabs as createSpendTabs } from "./web/assets/dashboard-spend.js";

const readiness = [];
const booting = new Set(["cl-booting"]);
globalThis.document = {documentElement:{dataset:{}, classList:{remove: c => booting.delete(c)}}};
globalThis.window = {dispatchEvent: event => readiness.push(event.type)};
reportReadiness(false, "renderOverview is not defined");
assert.equal(document.documentElement.dataset.dashboardReady, "error");
assert.deepEqual(readiness, ["cl:dashboard-error"]);
assert.equal(booting.size, 0, "a board that failed still releases the boot space reservation");
reportReadiness(true);
assert.equal(document.documentElement.dataset.dashboardReady, "true");
assert.deepEqual(readiness, ["cl:dashboard-error", "cl:dashboard-ready"]);
let overviewMarkup = "";
const overview = createSpendTabs({
  loadToken: 1, executiveData: async () => ({empty:true}),
  activeFilterCount: () => 0, esc: value => String(value),
  $: id => id === "tabBody" ? {} : null,
  setContent: (root, markup) => { overviewMarkup = markup; },
});
await overview.renderOverview(1);
assert.match(overviewMarkup, /No data for this period/);

let token = 1;
const requests = [], values = [], errors = [], timers = new Map();
let nextTimer = 0;
let reply = () => Promise.resolve({scan:{status:"running"}, freshness:{refreshing:true}});
const scan = createSavingsScan({
  request: args => { requests.push(args); return reply(args); },
  isCurrent: value => value === token,
  onData: value => values.push(value),
  onError: error => errors.push(error.message),
  schedule: fn => { timers.set(++nextTimer,fn); return nextTimer; },
  cancel: id => timers.delete(id),
  maxPolls: 3,
});
const tick = async () => {
  const [id, callback] = timers.entries().next().value;
  timers.delete(id);
  await callback();
};
await scan.start(1, true);
assert.equal(requests[0].refresh, true);
assert.equal(timers.size, 1);
await tick();
assert.equal(requests[1].refresh, false);
scan.stop();
assert.equal(timers.size, 0);
assert.equal(requests[1].signal.aborted, true);
assert.equal(values.length, 2);

let resolvePending;
reply = () => new Promise(resolve => { resolvePending = resolve; });
const pending = scan.start(1);
token = 2;
scan.stop();
resolvePending({scan:{status:"complete"}});
await pending;
assert.equal(values.length, 2, "late responses cannot repaint another view");

reply = () => Promise.resolve({scan:{status:"running"}});
await scan.start(2);
assert.equal(requests.at(-1).refresh, false, "returning joins the running scan");
await tick();
await tick();
assert.equal(timers.size, 0, "polling is bounded");
assert.match(errors.at(-1), /still running/);

reply = () => Promise.reject(new Error("service unavailable"));
await scan.start(2, true);
assert.equal(errors.at(-1), "service unavailable");
assert.equal(timers.size, 0);
reply = () => Promise.resolve({scan:{status:"complete"}});
await scan.start(2);
assert.equal(timers.size, 0, "complete scans never keep polling");
assert.notEqual(stableKey("resources|30|scope-a|USD|"), stableKey("resources|30|scope-b|USD|"));
assert.notEqual(stableKey("resources|30|scope-a|USD|"), stableKey("resources|30|scope-a|INR|"));
assert.equal(stableKey("same table"), stableKey("same table"));
console.log("progress lifecycle and context identity passed");
"""
    result = subprocess.run(
        [node(), "--input-type=module", "-"], input=script, cwd=ROOT,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_savings_local_age_staleness_provisional_and_source_failures():
    script = r"""
import assert from "node:assert/strict";
import { createSavingsClock, createSavingsScan, savingsPresentation } from "./web/assets/dashboard-savings-scan.js";

const generated = Date.parse("2026-09-06T00:00:00Z");
const cold = {
  scan:{status:"running",completed:["advisor"],total:4,errors:{}},
  freshness:{cached:false,stale:true,refreshing:true,generated_at:null,max_age_seconds:60},
};
let status = savingsPresentation(cold, null, generated);
assert.match(status.state, /^Scanning/);
assert.doesNotMatch(status.state, /cached|stale/i);
assert.equal(status.provisional, true, "cold totals require a dedup/provisional note");
const warm = {...cold, freshness:{...cold.freshness,cached:true,generated_at:new Date(generated).toISOString()}};
status = savingsPresentation(warm, null, generated + 61000);
assert.match(status.state, /Stale cached results.*refreshing/);
assert.equal(status.provisional, false, "previous complete cached totals are not partial totals");

const complete = {
  scan:{status:"complete",completed:["advisor","rightsizing","shutdown","waste"],total:4,errors:{}},
  freshness:{cached:true,stale:false,refreshing:false,generated_at:new Date(generated).toISOString(),max_age_seconds:60},
};
status = savingsPresentation(complete, null, generated + 1000);
assert.equal(status.ageText, "1s ago");
assert.equal(status.stale, false);
status = savingsPresentation(complete, null, generated + 60000);
assert.equal(status.ageText, "1m ago");
assert.equal(status.stale, true);
assert.match(status.state, /Stale results.*refresh needed/);
const longerTtl = {...complete, freshness:{...complete.freshness,max_age_seconds:120}};
assert.equal(savingsPresentation(longerTtl, null, generated + 61000).stale, false,
  "the backend TTL, not a hardcoded UI minute, determines staleness");
assert.equal(savingsPresentation({...complete,freshness:{...complete.freshness,max_age_seconds:undefined}},null,generated+60000).stale,false);
const failedSources = {...complete, unavailable:{waste:"old cached failure"},
  scan:{...complete.scan,errors:{advisor:"Access denied",rightsizing:"Timed out"}}};
status = savingsPresentation(failedSources, null, generated + 1000);
assert.match(status.state, /complete with warnings/);
assert.match(status.state, /advisor, rightsizing/);
assert.equal(status.warning, "advisor: Access denied; rightsizing: Timed out");
assert.doesNotMatch(status.warning, /waste|old cached/);
assert.equal(status.running, false, "source warnings still allow Refresh scan");
assert.equal(status.provisional, false);

let current = 1, now = generated, requests = 0, nextId = 0;
const timers = new Map(), seen = [];
const scan = createSavingsScan({
  request: async () => { requests++; return complete; },
  isCurrent: token => token === current,
  onData: () => {}, onError: error => { throw error; },
});
await scan.start(1);
const clock = createSavingsClock({
  isCurrent: token => token === current,
  onTick: () => seen.push(savingsPresentation(complete, null, now)),
  schedule: fn => { timers.set(++nextId,fn); return nextId; },
  cancel: id => timers.delete(id),
});
const tick = () => {
  const [id,fn] = timers.entries().next().value;
  timers.delete(id);
  fn();
};
clock.start(1);
assert.equal(timers.size, 1);
now += 59000;
tick();
assert.equal(seen.at(-1).ageText, "59s ago");
assert.equal(seen.at(-1).stale, false);
now += 1000;
tick();
assert.equal(seen.at(-1).stale, true);
assert.equal(requests, 1, "age/staleness ticks never poll the backend");
const lateTick = timers.values().next().value;
clock.stop();
assert.equal(timers.size, 0);
const seenBefore = seen.length;
lateTick();
assert.equal(seen.length, seenBefore, "cancelled ticks cannot update a departed tab");
current = 2;
clock.start(2);
assert.equal(timers.size, 1, "resume starts exactly one local clock");
clock.start(2);
assert.equal(timers.size, 1);
current = 3;
tick();
assert.equal(timers.size, 0, "context changes prevent re-scheduling");
assert.equal(requests, 1);
clock.stop();
scan.stop();
"""
    result = subprocess.run(
        [node(), "--input-type=module", "-"], input=script, cwd=ROOT,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source = (ASSETS / "dashboard-optimize.js").read_text(encoding="utf-8")
    assert source.count("statusClock.start(token)") == 2
    assert "statusClock.stop();" in source
    assert "Provisional totals: incoming findings are deduplicated" in source
    assert "counts and totals may change." in source
    assert "Latest scan source failures:" in source
