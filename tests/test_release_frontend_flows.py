"""Offline production event handlers; every request uses deterministic in-memory replies."""
import runpy
from pathlib import Path

_helpers = runpy.run_path(str(Path(__file__).with_name("test_tags_frontend.py")))
run_node = _helpers["run_node"]
HARNESS = _helpers["HARNESS"]


def test_budget_payload_uses_the_manually_reviewed_native_amount():
    run_node(HARNESS + r"""
payload=sample();
payload.currency="EUR";
await tabs.renderTags(1);
click("data-tag","Environment",$("tagChips"));
$("tagBudgetBtn").onclick();
assert.match($("bAmountLabel").textContent,/USD/);
assert.equal($("bAmount").value,"");
$("bAmount").value="110";
$("bEmails").value="reviewer@example.test";
let submitted;
globalThis.fetch=async(url,options)=>{
  assert.equal(url,"/api/budgets");
  submitted=JSON.parse(options.body);
  return {ok:false,json:async()=>({detail:"Offline test: no Azure operation"})};
};
await $("bCreate").onclick();
assert.equal(submitted.subscription,"sub-a");
assert.equal(submitted.amount,110,"manual native amount is not converted using the display currency");
assert.match($("bAmountLabel").textContent,/USD/);
assert.match($("bNote").textContent,/Offline test/);
""")


def test_schedule_completions_do_not_reopen_a_departed_view():
    reports_harness = runpy.run_path(str(Path(__file__).with_name("test_focus_workflow_frontend.py")))["HARNESS"]
    run_node(reports_harness + r"""
await reports.renderSettings(1);await $("cdReview").onclick();
let resolve;
writeReply=()=>new Promise(done=>resolve=done);
const pending=$("cdStart").onclick(),reads=requests.length;
ctx.loadToken++;root.innerHTML="<section>New view</section>";
resolve(reply({job_id:"durable",status:"queued"}));await pending;
assert.equal(root.innerHTML,"<section>New view</section>");
assert.equal(requests.length,reads);assert.equal(emitted.length,0);
await reports.renderSettings(ctx.loadToken);
writeReply=()=>{throw Error("offline");};
await $("cdReview").onclick();
assert.match($("cdNotice").textContent,/offline/);
reports.stop();
""")


def test_comparison_only_latest_success_or_error_updates_scoped_results():
    run_node(HARNESS + r"""
import {createTabs as createReports} from "./web/assets/dashboard-reports.js";
ctx.readStore=()=>null;
const pending=[];
ctx.get=async url=>{
  requests.push(url);
  if (url.startsWith("/api/archive/compare")) return new Promise((resolve,reject)=>pending.push({resolve,reject,url}));
  return {enabled:true,reachable:true,comparable:["focus"],days:{focus:["2026-09-06","2026-09-05","2026-09-04"]}};
};
const reports=createReports(ctx);
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));};
const result=label=>({currency:"USD",earlier:{day:label,cost:10,rows:1},
  later:{day:"later",cost:11,rows:1},total_delta:1,total_percent:10,row_delta:0,changes:[]});
const first=reports.renderHistory(1);
await flush();
$("hGroup").value="ResourceGroup"; $("hGroup").onchange();
assert.equal(pending.length,2);
assert.equal(new URL("https://local"+pending[1].url).searchParams.get("scope"),"sub-a");
pending[1].resolve(result("LATEST")); await flush();
const latest=$("hResult").innerHTML;
assert.match(latest,/LATEST/);
pending[0].resolve(result("STALE")); await first;
assert.equal($("hResult").innerHTML,latest);
$("hEarlier").value="2026-09-04"; $("hEarlier").onchange();
$("hGroup").value="RegionName"; $("hGroup").onchange();
pending[3].resolve(result("NEWEST")); await flush();
pending[2].reject(Error("stale error")); await flush();
assert.match($("hResult").innerHTML,/NEWEST/);
assert.doesNotMatch($("hResult").innerHTML,/stale error/);
$("hGroup").value="ServiceName"; $("hGroup").onchange();
$("hEarlier").value=$("hLater").value; $("hEarlier").onchange();
pending[4].resolve(result("INVALIDATED")); await flush();
assert.match($("hResult").innerHTML,/Pick two different days/);
$("hEarlier").value="2026-09-04"; $("hEarlier").onchange();
ctx.loadToken++;
root.innerHTML="<div id=\"hResult\">Different view</div>";
pending[5].reject(Error("departed failure")); await flush();
assert.equal($("hResult").innerHTML,"");
assert.match(root.innerHTML,/Different view/);
""")


def test_hourly_clear_resets_preserved_controls_and_pending_search():
    run_node(HARNESS + r"""
Element.prototype.remove=function(){};
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));};
ctx.get=async()=>({currency:"USD",resources:[
  {id:"a",name:"Alpha",service:"Compute",resource_group:"rg",region:"east",cost:100,hours:24,per_day:24},
  {id:"b",name:"Beta",service:"Storage",resource_group:"other",region:"west",cost:50,hours:12,per_day:12},
],dates:[],schedules:[]});
ctx.setContent=(host,markup)=>{
  // The shell preserves interaction state across replacement renders.
  const values=new Map(host.querySelectorAll("*").filter(e=>e.id).map(e=>[e.id,e.value]));
  host.innerHTML=markup;
  for(const [id,value] of values) if($(id)) $(id).value=value;
};
const hourly=createTabs(ctx);
await hourly.renderUptime(1);
assert.match(root.innerHTML,/<h3>Hourly Billing<\/h3>/);
const coverage=tables.get("Hourly meters").columns.find(column=>column.label==="Billed uptime");
assert.ok(coverage,"the billing-based percentage uses the requested label");
assert.equal(coverage.value({uptime_pct:75}),75,"the label change preserves the calculation");
$("upService").value="Compute"; $("upService").onchange({target:$("upService")});
await flush();
assert.equal($("upService").value,"Compute");
assert.match(root.innerHTML,/1 of 2 meters/);
$("upFind").value="Alpha"; $("upFind").oninput({target:$("upFind")});
$("uptimeClear").onclick(); await flush();
for(const id of ["upService","upGroup","upRegion","upFind"]) assert.equal($(id).value,"");
assert.equal($("upState").value,"all");
assert.match(root.innerHTML,/2 hourly meters/);
await new Promise(resolve=>setTimeout(resolve,350));
assert.equal($("upFind").value,"","old debounced search must not undo Clear");
assert.match(root.innerHTML,/2 hourly meters/);
""")
