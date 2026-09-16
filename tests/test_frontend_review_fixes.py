"""Review regressions with existing DOM adapters; all API reads/writes are synthetic."""
import runpy
from pathlib import Path

_reports = runpy.run_path(str(Path(__file__).with_name("test_focus_workflow_frontend.py")))
run_node = _reports["run_node"]
REPORTS = _reports["HARNESS"].replace("const reports=createReports(ctx);", r"""
const visibilityListeners=new Set();
document.hidden=false;
document.addEventListener=(name,fn)=>{if(name==="visibilitychange")visibilityListeners.add(fn);};
document.removeEventListener=(name,fn)=>{if(name==="visibilitychange")visibilityListeners.delete(fn);};
const visibility=()=>{for(const fn of [...visibilityListeners])fn();};
// The first render now reads scoped status through the same URL as a poll, because the
// shared ingest picker decides the scope. Counting polls therefore means counting reads
// after that render, not distinguishing them by URL shape.
let statusReads=0,readStatus=async()=>structuredClone(fixture);
const originalGet=ctx.get;
ctx.get=async(url,options)=>{
  if(url.startsWith("/api/cost-data?scope=")){
    requests.push(url);statusReads++;return readStatus(options);
  }
  return originalGet(url,options);
};
fixture.subscriptions=[sub("sub-a","Alpha")];
fixture.subscriptions[0].status="waiting-for-Azure";
fixture.subscriptions[0].job_id="durable";
selectedScope=["sub-a"];
const reports=createReports(ctx);
""")


def test_window_job_polling_resumes_visibility_without_parallel_reads_or_stale_ownership():
    run_node(REPORTS + r"""
await reports.renderSettings(1);statusReads=0;
assert.equal(statusReads,0);assert.equal(timers.size,1);
document.hidden=true;visibility();assert.equal(timers.size,0);
let finish;readStatus=()=>new Promise(resolve=>{finish=resolve;});
document.hidden=false;visibility();visibility();await flush();
assert.equal(statusReads,1,"visibility resumes one coalesced request");
finish(structuredClone(fixture));await flush();assert.equal(timers.size,1);
let stale;readStatus=()=>new Promise(resolve=>{stale=resolve;});
const pending=$("cdCheck").onclick();await flush();reports.stop();
assert.equal(visibilityListeners.size,0);
stale(structuredClone(fixture));await pending;
assert.equal(emitted.length,0);assert.equal(timers.size,0);
ctx.loadToken=2;reports.resume(2,"settings");assert.equal(timers.size,1);
// The ingest selection is deliberately independent of the header filter, so changing the
// header must neither stop the poll nor retarget it at a scope the user did not choose.
selectedScope=["sub-b"];visibility();assert.equal(statusReads,3);
assert.match(requests.at(-1),/scope=sub-a$/);
reports.stop();
""")


def test_window_import_events_track_new_generations_once_and_label_partial_commits():
    run_node(REPORTS + r"""
await reports.renderSettings(1);assert.equal(emitted.length,0);
fixture.subscriptions[0].coverage.authoritative_days=30;
fixture.subscriptions[0].last_success="2026-09-15T00:01:00Z";
await advance();
assert.equal(emitted.length,1);assert.equal(emitted[0].detail.started,"durable");
assert.equal(emitted[0].detail.status,"partial");
assert.match($("cdStatus").innerHTML,/30 of 90/);
await $("cdCheck").onclick();reports.stop();reports.resume(1,"settings");
assert.equal(emitted.length,1);
fixture.subscriptions[0].status="ready";
fixture.subscriptions[0].coverage={authoritative_days:90,requested_days:90,missing_days:0};
await $("cdCheck").onclick();
assert.equal(emitted.length,2);assert.equal(emitted[1].detail.status,"ready");
assert.equal(timers.size,0);await $("cdCheck").onclick();assert.equal(emitted.length,2);
assert.equal(writes.length,0);reports.stop();
""")


def test_visibility_return_during_abort_restarts_one_owned_status_read():
    run_node(REPORTS + r"""
await reports.renderSettings(1);statusReads=0;
readStatus=({signal})=>new Promise((resolve,reject)=>{
  signal.addEventListener("abort",()=>reject(Error("Read aborted")),{once:true});
});
const pending=$("cdCheck").onclick();await flush();
document.hidden=true;visibility();document.hidden=false;visibility();visibility();
await pending;await flush();
assert.equal(statusReads,1);assert.equal(timers.size,1);
readStatus=async()=>structuredClone(fixture);
await advance();assert.equal(statusReads,2);assert.equal(timers.size,1);
assert.equal(visibilityListeners.size,1);
reports.stop();assert.equal(timers.size,0);assert.equal(visibilityListeners.size,0);
""")


def test_cached_approval_and_preview_callbacks_cannot_submit_after_resume():
    run_node(REPORTS + r"""
fixture.subscriptions[0].status="idle";
await reports.renderSettings(1);await $("cdReview").onclick();
const oldStart=$("cdStart").onclick,oldPreview=$("cdReview").onclick;
reports.stop();reports.resume(1,"settings");
assert.equal($("cdStart"),undefined);
await oldStart();await oldPreview();assert.equal(writes.length,1);
await $("cdReview").onclick();const start=$("cdStart").onclick;await start();await start();
assert.equal(writes.filter(c=>c.url.endsWith("/setup")).length,1);reports.stop();
""")


def test_stale_reload_cannot_cancel_new_poll_and_failed_reload_rebinds_workflow():
    run_node(REPORTS + r"""
await reports.renderSettings(1);statusReads=0;const obsolete=$("cdCheck").onclick;
reports.stop();reports.resume(1,"settings");await obsolete();
assert.equal(statusReads,0);assert.equal(timers.size,1);
readStatus=async()=>{throw Error("Status unavailable");};await reports.renderSettings(1);
assert.equal(timers.size,1);assert.equal(visibilityListeners.size,1);
// A failed status read on render keeps the saved setup on screen and offers a retry.
assert.match($("tabBody").innerHTML,/Status unavailable Last known costs remain available\./);
assert.ok($("cdLoadRetry"));reports.stop();
""")


def test_fast_import_completion_reloads_cost_views_once_not_on_acceptance():
    run_node(REPORTS + r"""
fixture.subscriptions[0].status="idle";
let accepted;
writeReply=call=>call.url.endsWith("/preview")?reply(previewPlan()):new Promise(resolve=>accepted=resolve);
await reports.renderSettings(1);await $("cdReview").onclick();
const pending=$("cdStart").onclick();await flush();
fixture.subscriptions[0].last_success="2026-09-15T00:10:00Z";
fixture.subscriptions[0].coverage.authoritative_days=30;
accepted(reply({job_id:"durable"}));await pending;
assert.equal(emitted.length,1);assert.equal(emitted[0].type,"cloudlens:focus-synced");
await $("cdCheck").onclick();assert.equal(emitted.length,1);reports.stop();
""")


ANOMALIES = r"""
import assert from "node:assert/strict";
import {createTabs as createSpend} from "./web/assets/dashboard-spend.js";
import {createTabs as createMonitor} from "./web/assets/dashboard-monitor.js";
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
const build=payload=>{
  const nodes=new Map(),tones=new Set();
  const tile={classList:{remove:(...names)=>names.forEach(name=>tones.delete(name)),add:name=>tones.add(name)}};
  const node=id=>{
    if(id==="anomAlerts")return null;
    if(!nodes.has(id))nodes.set(id,{id,innerHTML:"",outerHTML:"",remove(){},
      querySelector:()=>({}),querySelectorAll:()=>[],closest:()=>tile});
    return nodes.get(id);
  };
  let html="";
  const data={days:30,currency:"USD",kpis:{total:1,previous:1,change_pct:0,daily_avg:1,
    resources:1,services:1,regions:1,subscriptions:1},areas:[],services:[],subscriptions:[],
    regions:[],resource_groups:[],movers:[],trend:{labels:[],values:[],previous:[]}};
  const ctx={$:node,loadToken:1,days:30,periodMonth:null,executiveData:async()=>data,
    cached:(_,load)=>load(),key:name=>name,scopeIds:()=>[],
    setContent:(_,markup)=>{html=markup;},whenNear:()=>{},table:()=>"",
    kpiRow:items=>items.map(item=>`${item.label} ${item.value} ${item.sub??""}`).join("|"),
    esc:value=>String(value??""),moneyHtml:(value,currency)=>`${currency} ${value}`,
    money:(value,currency)=>`${currency} ${value}`,trendClass:()=>"",pct:()=>"",
    cardHead:({name})=>name,drawChart:()=>{},qs:()=>"?days=30",
    get:async url=>{if(url.startsWith("/api/anomalies/alerts"))throw Error("Alerts unavailable");return payload;},
    navigate:()=>{},openAnalysis:()=>{}};
  return {ctx,node,tones,html:()=>html};
};
"""


def test_unavailable_or_partial_anomalies_never_claim_a_green_zero():
    run_node(ANOMALIES + r"""
for(const coverage of [
  {unavailable:true,partial:true,unavailable_currencies:{INR:"A verified exchange rate is required."}},
  {partial:true,note:"Missing settled-day coverage prevents a complete assessment."},
  {partial:true,coverage_reason:"Missing settled-day coverage prevents a complete assessment.",
    note:"No anomalies detected in observed data."},
]){
  const payload={currency:"USD",anomalies:[],...coverage};
  const view=build(payload);
  await createSpend(view.ctx).renderOverview(1);await flush();
  assert.equal(view.node("overviewAnomsValue").innerHTML,"—");
  assert.match(view.node("overviewAnomsBody").outerHTML,/Not assessed|Partial assessment/);
  assert.doesNotMatch(view.node("overviewAnomsBody").outerHTML,/Nothing unusual/);
  assert.equal(view.tones.has("kpi-tone-good"),false);
  await createMonitor(view.ctx).renderAnomalies(1);
  assert.match(view.html(),/Not assessed|Partial assessment/);
  assert.match(view.html(),/exchange rate|Missing settled-day coverage/);
  assert.doesNotMatch(view.html(),/No anomalies detected|nothing unusual/);
}
""")


def test_partial_anomaly_findings_remain_visible_but_not_a_clean_assessment():
    run_node(ANOMALIES + r"""
const view=build({currency:"USD",partial:true,note:"One currency could not be assessed.",
  count:1,increases:1,decreases:0,high:1,impact:10,anomalies:[
    {key:"Compute",day:"2026-09-10",dimension:"service",direction:"up",currency:"USD",
      delta:10,cost:20,baseline:10,percent:100,score:9,severity:"high"},
  ]});
await createSpend(view.ctx).renderOverview(1);await flush();
assert.match(view.node("overviewAnomsValue").innerHTML,/1/);
assert.match(view.node("overviewAnomsBody").outerHTML,/Partial assessment/);
assert.equal(view.tones.size,0,"an incomplete overview assessment remains neutral");
await createMonitor(view.ctx).renderAnomalies(1);
assert.match(view.html(),/Partial assessment/);
assert.match(view.html(),/Compute/);
assert.match(view.html(),/Observed findings remain visible/);
""")
