"""Offline regressions for audit fixes; all network reads and writes are mocked."""
import runpy
from pathlib import Path

_helpers = runpy.run_path(str(Path(__file__).with_name("test_tags_frontend.py")))
run_node = _helpers["run_node"]
HARNESS = _helpers["HARNESS"]


def test_hourly_option_identities_empty_values_and_self_excluding_groups():
    run_node(HARNESS + r"""
Element.prototype.remove=function(){};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const rows=[
  {id:"none",resource_group:"one",keys:[],v:[]},
  {id:"real",resource_group:"two",keys:["untagged"],v:[0]},
  {id:"blank",resource_group:"one",keys:["app"],v:[1]},
  {id:"named",resource_group:"two",keys:["app"],v:[0]},
].map(row=>({name:row.id,service:"Compute",region:"east",cost:10,hours:24,per_day:24,...row}));
ctx.get=async()=>({currency:"USD",resources:rows,dates:[],schedules:[],
  tag_keys:[{key:"untagged",cost:10},{key:"app",cost:20}],tag_values:["prod",""]});
const hourly=createTabs(ctx);
await hourly.renderUptime(1);
assert.doesNotMatch(root.innerHTML,/\u0000|\ufffd/);
assert.ok($("upTagKey").options.some(option=>option.value==="untagged"));
assert.ok($("upTagKey").options.some(option=>option.value==="key:untagged"));
const choose=async(id,value)=>{ $(id).value=value; $(id).onchange({target:$(id)}); await flush(); };
const visible=()=>tables.get("Selected hourly meters").rows.map(row=>row.id);
await choose("upTagKey","key:untagged");
assert.deepEqual(visible(),["real"]);
await choose("upTagKey","untagged");
assert.deepEqual(visible(),["none"]);
await choose("upTagKey","key:app");
await choose("upTagValue","value:");
assert.deepEqual(visible(),["blank"],"literal empty value is not All values");
await choose("upTagValue","");
assert.deepEqual(visible(),["blank","named"]);
$("uptimeClear").onclick(); await flush();
await choose("upGroup","one");
assert.ok($("upGroup").options.some(option=>option.value==="two"),"own facet remains switchable");
""")


def test_vm_savings_require_supported_compute_resource_and_capability():
    run_node(r"""
import assert from "node:assert/strict";
import {scheduleAssessment,illustrativeSaving} from "./web/assets/billing-usage.js";
const vm={id:"/subscriptions/a/resourceGroups/r/providers/Microsoft.Compute/virtualMachines/v",
  schedulable:true,schedule_status:"needs_review",cost:100,uptime_pct:100};
assert.equal(scheduleAssessment(vm),"needs_review");
assert.ok(illustrativeSaving(vm,60)>0);
for(const row of [
  {...vm,id:"/subscriptions/a/resourceGroups/r/providers/Microsoft.Web/serverfarms/p"},
  {...vm,id:vm.id+"/extensions/license"},
  {...vm,id:"/providers/Microsoft.Compute/virtualMachines/v"},
  {...vm,id:"/subscriptions/a/providers/Microsoft.Compute/virtualMachines/v"},
  {...vm,schedulable:false},
  {...vm,schedule_status:"not_assessed"},
]) {
  assert.equal(scheduleAssessment(row),"not_assessed");
  assert.equal(illustrativeSaving(row,60),0);
}
""")


def test_budget_completion_does_not_replace_another_context_or_selection():
    run_node(HARNESS + r"""
for(const change of ["scope","selection"]) {
  payload=sample();
  const own=createTabs(ctx);
  await own.renderTags(ctx.loadToken);
  click("data-tag","Environment",$("tagChips"));
  $("tagBudgetBtn").onclick();
  $("bAmount").value="110"; $("bEmails").value="offline@example.test";
  let resolve;
  globalThis.fetch=()=>new Promise(done=>{resolve=done;});
  const work=$("bCreate").onclick();
  if(change==="scope") {
    ctx.loadToken++;
    const other=createTabs(ctx);
    await other.renderTags(ctx.loadToken);
    click("data-tag","Owner",$("tagChips"));
    $("tagBudgetBtn").onclick();
  } else {
    click("data-tag","Owner",$("tagChips"));
  }
  const before=$("tagBudget").innerHTML;
  resolve({ok:true,json:async()=>({name:"old-budget",amount:110,subscription:"sub-a",
    time_grain:"Monthly",notifications:1})});
  await work;
  assert.equal($("tagBudget").innerHTML,before,change);
  assert.doesNotMatch($("tagBudget").innerHTML,/old-budget/);
}
""")


def test_whatif_only_latest_success_or_failure_can_paint():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/dashboard-optimize.js","utf8");
const pending=[];
const post=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
const run=new Function("post","qs","esc","money",
  source.slice(source.indexOf("const whatIfRequests"),source.indexOf("async function renderAdvisor"))+
  ";return runWhatIf;")(post,()=>"",String,String);
const result={}; const details={dataset:{rid:"vm"},querySelector:()=>result};
const data=name=>({currency:"USD",days:30,billed:{annual:100},current_retail:{annual:110},
  target:{annual:90},target_size:name,annual_saving:20});
const first=run(details,"old"), second=run(details,"new");
pending[1].resolve(data("new")); await second;
const latest=result.innerHTML;
pending[0].resolve(data("old")); await first;
assert.equal(result.innerHTML,latest);
const third=run(details,"old-error"), fourth=run(details,"newest");
pending[3].resolve(data("newest")); await fourth;
pending[2].reject(Error("stale failure")); await third;
assert.match(result.innerHTML,/newest/);
assert.doesNotMatch(result.innerHTML,/stale failure/);
""")


def test_pending_secondary_panels_resume_from_the_request_cache():
    run_node(HARNESS + r"""
import {createTabs as createOptimize} from "./web/assets/dashboard-optimize.js";
Element.prototype.querySelector=function(selector){return selector.startsWith("#")?$(selector.slice(1)):null;};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const cache=new Map();
ctx.cached=(key,load)=>{
  if(!cache.has(key))cache.set(key,Promise.resolve().then(load));
  return cache.get(key);
};
ctx.whenNear=()=>{}; ctx.trendClass=()=>"";ctx.pct=()=>"";ctx.navigate=()=>{};
ctx.executiveData=async()=>({currency:"USD",days:30,kpis:{total:1,previous:1},
  trend:{labels:[],values:[],previous:[]},areas:[],services:[],regions:[],movers:[],subscriptions:[],resource_groups:[]});
for(const view of ["overview","esu"]) {
  let resolve; let reads=0;
  ctx.get=async()=>{reads++;return new Promise(done=>{resolve=done;});};
  ctx.live=async()=>({currency:"USD",machines:[]});
  const family=view==="overview"?createTabs(ctx):createOptimize(ctx);
  const render=view==="overview"?"renderOverview":"renderEsu";
  ctx.loadToken=1;
  await family[render](1); await flush();
  const original=root.innerHTML;
  ctx.loadToken=2; root.innerHTML="<div>Different view</div>";
  resolve(view==="overview"?{currency:"USD",anomalies:[]}:{currency:"USD",vms:[]});
  await flush();
  assert.equal(root.innerHTML,"<div>Different view</div>");
  root.innerHTML=original;ctx.loadToken=1;
  family.resume(1,view);await flush();
  assert.equal(reads,1,"resume reuses the compatible cached request");
  if(view==="overview")assert.equal($("overviewAnomsValue").innerHTML,"0");
  else assert.match($("ahbBody").textContent,/No Windows Server/);
}
""")


def test_filter_errors_keep_active_choices_and_currency_buckets_are_honest():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import {periodParams} from "./web/assets/reporting-period.js";
const source=fs.readFileSync("./web/assets/filters.js","utf8")
  .replace(/^import .*;\r?$/mg,"").replace(/\bexport /g,"");
const nodes=new Map(["filterBar","fbSlots","fbClear","fbStatus","fbRetry"].map(id=>[id,{hidden:false,innerHTML:""}]));
const doc={getElementById:id=>nodes.get(id),querySelectorAll:()=>[],addEventListener:()=>{},removeEventListener:()=>{}};
let currency="USD", failure=false;
const calls=[];
const get=async url=>{
  calls.push(url);
  if(failure)return {unavailable:true,dimensions:{}};
  return {currency,dimensions:{service:[
    {value:"A",cost:10,currency},{value:"B",cost:20,currency}
  ]}};
};
const api=new Function("document","window","readStore","writeStore","scopeIds","onScopeChange",
  "esc","get","money","periodParams",source+
  ";ready=true;return {loadFilters,setFilterCurrency,filterCostText,filterQuery};")(
    doc,{addEventListener:()=>{}},()=>({service:["A"]}),()=>{},()=>["sub-a"],()=>{},
    String,get,(n,c)=>`${c}:${n}`,periodParams);
await api.loadFilters(30);
currency="INR";api.setFilterCurrency("INR");
await new Promise(resolve=>setImmediate(resolve));
assert.equal(new URL("https://offline"+calls.at(-1)).searchParams.get("currency"),"INR");
assert.equal(api.filterCostText({cost:290,currency:"USD"}),"USD USD:290");
assert.match(api.filterCostText({currency_buckets:{USD:1,EUR:2}}),/USD.*1.*EUR.*2/);
assert.equal(api.filterCostText({cost:3}),"3 (currency unknown)");
failure=true;await api.loadFilters(30);
assert.equal(nodes.get("filterBar").hidden,false);
assert.equal(nodes.get("fbClear").hidden,false);
assert.match(nodes.get("fbSlots").innerHTML,/A/);
assert.match(nodes.get("fbStatus").innerHTML,/Existing selections still apply/);
assert.match(api.filterQuery().join("&"),/service=A/);
assert.equal(typeof nodes.get("fbRetry").onclick,"function");
""")


def test_savings_header_is_keyed_scoped_and_retains_compatible_results():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import {createSavingsScan} from "./web/assets/dashboard-savings-scan.js";
const source=fs.readFileSync("./web/assets/app.js","utf8");
assert.match(source,/loadSubscriptions\(\)\.then\(\(\) => loadGlance\(\)\)/);
const pending=[];const get=url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}));
let scope=["a"],currency="USD";
const tile={setAttribute:()=>{}};
const doc={documentElement:{dataset:{dashboardReady:"true"}}};
const win={requestIdleCallback:fn=>fn(),removeEventListener:()=>{}};
const setTile=(tile,key,value,sub)=>Object.assign(tile,{key,value,sub});
const api=new Function("$","headerGet","scopeIds","readStore","money","setTile","setKpi","linkTile",
  "createSavingsScan","document","window","requestIdleCallback",
  source.slice(source.indexOf("function loadSavingsTile"),source.indexOf("async function loadWarehouse"))+
  ";return {schedule:scheduleSavingsTile,refresh:()=>{savingsScheduled.refresh=true;},stop:()=>savingsScheduled.scan.stop()};")(
    ()=>({children:[null,null,null,null,null,tile]}),get,()=>scope,()=>currency,
    (n,c)=>`${c}:${n}`,setTile,(tile,data)=>Object.assign(tile,data),()=>{},
    createSavingsScan,doc,win,win.requestIdleCallback);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
api.schedule("USD");api.schedule("USD");
assert.equal(pending.length,1);
assert.match(pending[0].url,/scope=a/);assert.match(pending[0].url,/currency=USD/);
scope=["b"];currency="INR";api.schedule("INR");
assert.equal(pending.length,2);
pending[1].resolve({count:1,total:10,currency:"INR",days:30,scan:{status:"ready"}});await flush();
assert.equal(tile.value,"INR:10");
pending[0].resolve({count:1,total:999,currency:"USD",scan:{status:"ready"}});await flush();
assert.equal(tile.value,"INR:10");
api.schedule("INR");
assert.equal(tile.value,"INR:10","settled tile does not regress to waiting");
assert.equal(pending.length,2);
api.refresh();api.schedule("INR");
assert.equal(tile.value,"INR:10","compatible value remains during refresh");
pending[2].reject(Error("offline"));await flush();
assert.equal(tile.value,"INR:10");
assert.match(tile.title,/Previous result shown/);
scope=["c"];api.schedule("INR");
pending[3].resolve({count:0,total:0,scan:{status:"error",errors:{advisor:"unavailable"}}});await flush();
assert.equal(tile.sub,"assessment unavailable");
scope=["partial"];
const partial=api.schedule("INR");
pending[4].resolve({count:1,total:12,currency:"INR",days:30,
  unavailable:["waste"],scan:{status:"ready",errors:{waste:"price unavailable"}}});
assert.equal(await partial,true,"completed partial coverage is a successful header repaint");
assert.equal(tile.value,"INR:12");
assert.match(tile.sub,/partial/);
assert.match(tile.title,/Partial assessment shown/);
assert.doesNotMatch(tile.title,/Previous result/);
api.refresh();
let settled=false;
const running=api.schedule("INR").then(result=>{settled=true;return result;});
assert.equal(tile.value,"INR:12","a completed partial result is retained during refresh");
pending[5].resolve({count:1,total:12,currency:"INR",scan:{status:"running"},
  freshness:{refreshing:true},unavailable:["waste"]});
await flush();
assert.equal(settled,false,"running scans must not settle the repaint promise");
api.refresh();
const recovered=api.schedule("INR");
assert.equal(await running,false,"superseded scans do not certify freshness");
pending[6].resolve({count:1,total:15,currency:"INR",days:30,scan:{status:"ready"}});
assert.equal(await recovered,true);
assert.equal(tile.value,"INR:15");
assert.equal(tile.title,"","a recovered assessment clears the obsolete warning");
api.refresh();
const failed=api.schedule("INR");
pending[7].resolve({count:1,total:15,currency:"INR",
  scan:{status:"error",errors:{advisor:"failed"}}});
assert.equal(await failed,false,"genuine source failures still fail header revalidation");
assert.match(tile.title,/Previous result shown/);
scope=["unpriced"];
const unpriced=api.schedule("INR");
pending[8].resolve({count:2,total:null,currency:"INR",unpriced_count:2,partial:true,
  scan:{status:"ready"}});
assert.equal(await unpriced,true);
assert.equal(tile.value,"Not available","unpriced totals never render as money or zero");
assert.match(tile.sub,/partial/);
assert.match(tile.title,/saving estimates are unavailable/);
scope=["zero"];
const zero=api.schedule("INR");
pending[9].resolve({count:1,total:0,currency:"INR",unpriced_count:0,partial:false,
  scan:{status:"ready"}});
assert.equal(await zero,true);
assert.equal(tile.value,"INR:0","genuine known zero remains a numeric amount");
assert.doesNotMatch(tile.sub,/partial/);
assert.equal(tile.title,"");
api.stop();
""")


def test_new_chat_aborts_and_ignores_old_done_and_finally():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/app.js","utf8");
const makeNode=()=>({innerHTML:"",hidden:false,disabled:false,value:"",style:{},
  classList:{remove:()=>{}},focus:()=>{},appendChild:()=>{},firstElementChild:{},
  querySelector:()=>makeNode()});
const nodes=new Map(["reset","send","stop","q"].map(id=>[id,makeNode()]));
const doc={querySelector:()=>null,createElement:()=>makeNode()};
const requests=[];const fetch=(url,opts)=>new Promise(resolve=>requests.push({opts,resolve}));
const reset=source.slice(source.indexOf('  $("reset").onclick ='),source.indexOf('  $("stop").onclick ='));
const ask=source.slice(source.indexOf("async function ask("),source.indexOf("// ------------------------------------------------------------------ render"));
const agent=source.slice(source.indexOf("function addAgent("),source.indexOf("// ------------------------------------------------------------------ charts"));
const api=new Function("$","document","chat","window","fetch","scopeIds","announce","addYou",
  "loadMarkdown","scroll","enhance","esc","introTemplate","toLogin",
  "let history=['prior'];let busy=false;let controller=null;let conversationGeneration=0;let pinned=true;"+
  reset+ask+agent+";return {ask,reset:()=>$('reset').onclick(),history:()=>history,busy:()=>busy};")(
    id=>nodes.get(id),doc,makeNode(),{dispatchEvent:()=>{}},fetch,()=>[],()=>{},()=>{},
    ()=>{},()=>{},()=>{},String,"intro",()=>{});
let resolveFrame;
const old=api.ask("old");
requests[0].resolve({ok:true,status:200,body:{getReader:()=>({
  read:()=>new Promise(resolve=>{resolveFrame=resolve;}),cancel:async()=>{}
})}});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
await flush();
api.reset();
assert.equal(requests[0].opts.signal.aborted,true);
const next=api.ask("new");
assert.deepEqual(JSON.parse(requests[1].opts.body).history,[]);
resolveFrame({done:false,value:new TextEncoder().encode('event: done\ndata: {"messages":["old hidden"]}\n\n')});
await old;
assert.equal(api.busy(),true,"old finally cannot release a new request");
assert.equal(nodes.get("send").disabled,true);
let frame=0;
requests[1].resolve({ok:true,status:200,body:{getReader:()=>({
  read:async()=>++frame===1?{done:false,value:new TextEncoder().encode('event: done\ndata: {"messages":["new only"]}\n\n')}:{done:true},
  cancel:async()=>{}
})}});
await next;
assert.deepEqual(api.history(),["new only"]);
assert.equal(api.busy(),false);
assert.equal(nodes.get("send").disabled,false);
""")


def test_money_columns_sort_raw_values_and_keep_unknown_last():
    run_node(HARNESS + r"""
import {createTabs as createOptimize} from "./web/assets/dashboard-optimize.js";
import {tableRows} from "./web/assets/dashboard-table-model.js";
ctx.live=async()=>({currency:"USD",machines:[{name:"vm",monthly_esu_cost:1200}]});
ctx.get=async()=>({currency:"USD",vms:[{name:"vm",cost:900,estimated_annual_savings:1200}]});
const optimize=createOptimize(ctx);
await optimize.renderEsu(1);
await new Promise(resolve=>setImmediate(resolve));
for(const [heading,label,field] of [
  ["Support lifecycle","Est. / month","monthly_esu_cost"],
  ["Windows Server VMs","Billed, 30d","cost"],
  ["Windows Server VMs","Est. saving / year","estimated_annual_savings"],
]) {
  const column=tables.get(heading).columns.find(column=>column.label===label);
  assert.equal(column.value({[field]:10000}),10000);
  assert.equal(column.value({[field]:null}),null);
  const rows=[900,10000,1200,null].map(n=>({[field]:n}));
  assert.deepEqual(tableRows({rows,columns:[column],sort:0,dir:-1,query:"",page:0})
    .map(row=>row[field]),[10000,1200,900,null]);
}
""")


def test_subscription_picker_retains_focus_on_the_toggled_option():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/app.js","utf8");
const doc={activeElement:null};
const list={children:[],contains(node){return this.children.includes(node);},
  appendChild(node){this.children.push(node);},querySelectorAll(){return this.children;},
  set innerHTML(value){this.children=[];}};
const node=()=>({dataset:{},classList:{toggle:()=>{}},setAttribute:()=>{},
  focus(){doc.activeElement=this;}});
doc.createElement=node;
const nodes=new Map(["pickerLabel","pickerBtn","pickAll"].map(id=>[id,node()]));
nodes.set("pickerList",list);
const picked=new Set(["a"]);
const render=new Function("$","document","allSubs","selected","esc","money","toggleSub",
  "isAllSelected","describeScope",
  source.slice(source.indexOf("function renderPicker"),source.indexOf("function openPicker"))+
  ";return renderPicker;")(id=>nodes.get(id),doc,[{id:"a"},{id:"b"}],picked,String,String,()=>{},()=>false,()=>{});
render();list.children[1].focus();const old=doc.activeElement;
picked.add("b");render();
assert.notEqual(doc.activeElement,old);
assert.equal(doc.activeElement.dataset.subscriptionId,"b");
assert.equal(list.contains(doc.activeElement),true,"Escape remains within the menu key handler");
""")


def test_mixed_native_views_never_format_aggregate_null_or_add_currencies():
    run_node(HARNESS + r"""
const formatted=[];
ctx.money=(amount,currency)=>{
  assert.ok(Number.isFinite(amount));
  assert.match(currency,/^(USD|EUR)$/);
  formatted.push([amount,currency]);return `${currency}:${amount}`;
};
const bucket=(currency,total)=>({currency,days:30,kpis:{total,previous:total-1,resources:2},
  services:[{name:"Compute",cost:total}],
  resources_top:[{name:"vm",grp:"rg",cost:total}],
  trend:{labels:["2026-09-01","2026-09-02"],values:[1,total-1]}});
const mixed={mixed_currency:true,currency:null,kpis:{total:null},services:[],
  by_currency:[bucket("USD",100),bucket("EUR",200),bucket(null,3)]};
ctx.executiveData=async()=>mixed;
ctx.get=async()=>mixed;
const spend=createTabs(ctx);
for(const render of [()=>spend.renderOverview(1),()=>spend.renderSection(1,"compute")]) {
  await render();
  assert.match(root.innerHTML,/Spend by billing currency/);
  assert.match(root.innerHTML,/USD:100/);
  assert.match(root.innerHTML,/EUR:200/);
  assert.match(root.innerHTML,/3 \(currency unknown\)/);
  assert.doesNotMatch(root.innerHTML,/USD:300|EUR:300|largest billing currency/);
  assert.equal(tables.get("Services · USD").rows[0].cost,100);
  assert.equal(tables.get("Services · EUR").rows[0].cost,200);
}
assert.deepEqual(new Set(charts.map(chart=>chart.currency)),new Set(["USD","EUR"]));
assert.ok(formatted.length>0);
Object.assign(mixed,{mixed_currency:false,currency:"unknown",by_currency:[],kpis:{total:3}});
await spend.renderOverview(1);
assert.match(root.innerHTML,/3 \(currency unknown\)/);
""")


def test_mixed_native_sections_remain_available_without_a_fake_scalar_total():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import {hasSectionSpend,sectionCostLabel,nativeAmount} from "./web/assets/dashboard-currencies.js";
const buckets=[{currency:"USD",cost:100},{currency:"EUR",cost:200}];
const section={id:"compute",label:"Compute",area:true,cost:null,currency:null,cost_by_currency:buckets};
assert.equal(hasSectionSpend(section),true);
assert.equal(hasSectionSpend({cost:null,cost_by_currency:[{currency:"USD",cost:0}]}),false);
assert.equal(sectionCostLabel(section,()=>{throw Error("Must not format a mixed total");}),"2 currencies");
assert.equal(nativeAmount(4,null,()=>{throw Error("Cannot guess USD");}),"4 (currency unknown)");
const source=fs.readFileSync("./web/assets/dashboard.js","utf8");
assert.match(source,/filter\(hasSectionSpend\)/);
assert.match(source,/hasSectionSpend\(data\.aks\)/);
assert.match(source,/hasSectionSpend\(data\.marketplace\)/);
const picker=new Function("sections","esc","money","sectionCostLabel",
  source.slice(source.indexOf("function areaPicker("),source.indexOf("function wireAreaPicker("))+
  ";return areaPicker;")([{...section,id:"overview",area:false},section],String,String,sectionCostLabel);
assert.match(picker("compute"),/data-area="compute"/);
assert.match(picker("compute"),/2 currencies/);
""")


def test_ingest_poll_is_pinned_to_its_job_and_ignores_newer_progress():
    run_node(r"""
import assert from "node:assert/strict";
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/dashboard.js","utf8");
assert.match(source,/watchIngest\(data\.job_id \|\| data\.started\)/);
const job="job-a&test";
const replies=[
  {ingest:{status:"running",job_id:"newer-job",progress:.9}},
  {ingest:{status:"running",job_id:job,started:job,progress:.5}},
  {ingest:{status:"ready",job_id:job,started:job},to:"2026-09-14"},
];
const calls=[],painted=[],refreshes=[],labels=[];
const get=async url=>{calls.push(url);assert.ok(replies.length);return replies.shift();};
const watch=new Function("get","showIngest","setRefreshLabel","paintIngest","toast",
  "clearCache","refreshHeader","loadTabs","setTimeout",
  "let lastAsOf=null;"+source.slice(source.indexOf("async function watchIngest("),
    source.indexOf("// ------------------------------------------------------------- the progress strip"))+
  ";return watchIngest;")(get,()=>{},label=>labels.push(label),
    state=>{painted.push(state.job_id);return state.progress;},()=>{},()=>{},()=>{},
    async options=>refreshes.push(options),fn=>fn());
await watch(job);
assert.equal(calls.length,3);
for(const url of calls)assert.equal(new URL("https://offline"+url).searchParams.get("job_id"),job);
assert.deepEqual(painted,[job],"another job must not replace progress");
assert.deepEqual(refreshes,[{preserve:true}],"completion revalidates without blanking compatible data");
assert.equal(labels.at(-1),"Refresh data");
""")


def test_resource_service_facets_and_search_use_association_membership():
    run_node(r"""
import assert from "node:assert/strict";
import {filterResources,resourceFacets,resourceSummary} from "./web/assets/dashboard-resources.js";
const rows=[{id:"vm",service:"Virtual Machines",service_names:["Virtual Machines","Storage"],currency:"USD",cost:100}];
assert.deepEqual(filterResources(rows,{service:"value:storage"}),rows);
assert.deepEqual(filterResources(rows,{query:"storage"}),rows);
assert.ok(resourceFacets(rows,"service").some(value=>value.value==="value:storage"));
assert.equal(resourceSummary(rows).services,2);
assert.equal(filterResources(rows,{service:"value:networking"}).length,0);
""")


def test_shutdown_and_commitment_native_money_remains_separate_and_unknown():
    run_node(HARNESS + r"""
import {createTabs as createOptimize} from "./web/assets/dashboard-optimize.js";
Element.prototype.remove=function(){};
ctx.money=(amount,currency)=>{
  assert.ok(Number.isFinite(amount),`unknown amount ${amount}`);
  assert.match(currency,/^(USD|EUR)$/);return `${currency}:${amount}`;
};
ctx.cardHead=parts=>`${parts.name} ${parts.figure}`;
const candidates=["USD","EUR"].map(currency=>({currency,saving:10,current:100,scheduled:90,name:currency}));
let response={currency:null,total_current:null,total_saving:null,candidates,count:2,
  totals_by_currency:{USD:{total_current:100,total_saving:10},EUR:{total_current:100,total_saving:10}}};
ctx.get=async()=>response;
const optimize=createOptimize(ctx);
await optimize.renderShutdown(1);
assert.match(root.innerHTML,/USD:100/);assert.match(root.innerHTML,/EUR:100/);
const group=(currency,total)=>({currency,total,compute_total:total,split:{unknown:total},
  compute_split:{unknown:total},coverage_pct:null,compute_coverage_pct:null,
  coverage_status:"unknown",compute_coverage_status:"unknown"});
const comparison=currency=>({currency,models:[
  {baseline:true,label:"Baseline",annual_cost:100},
  {label:"Reservation",annual_cost:null,annual_saving:null}
],best:{annual_saving:null}});
response={
  coverage:{currency:null,mixed_currency:true,total:null,compute_total:null,
    coverage_status:"unknown",compute_coverage_status:"unknown",
    by_currency:{USD:group("USD",100),EUR:group("EUR",200)},
    trend_by_currency:{USD:[{month:"Jan",total:100,on_demand:0,committed:0,spot:0},
      {month:"Feb",total:110,on_demand:0,committed:0,spot:0}]}},
  inventory:{count:300,returned_count:2,truncated:true,days:30,
    commitments:[{name:"u",currency:"USD",cost:10},{name:"e",currency:"EUR",cost:20}]},
  comparison:{currency:null,by_currency:{USD:comparison("USD"),EUR:comparison("EUR")}},
  spot:{currency:null,total_saved:null,
    totals_by_currency:{USD:{total_saved:null},EUR:{total_saved:20}},
    trend_by_currency:{
      USD:[{month:"Jan",spot_cost:10,would_have_cost:null,saved:null,priced_saving:5}],
      EUR:[{month:"Jan",spot_cost:20,would_have_cost:40,saved:20}],
    }},
};
await optimize.renderCommitments(1);
assert.match(root.innerHTML,/Active commitments 300/);
assert.match(root.innerHTML,/Coverage · USD/);
assert.match(root.innerHTML,/Coverage · EUR/);
assert.match(root.innerHTML,/Saving unavailable/);
assert.doesNotMatch(root.innerHTML,/class="cmp-col best"/);
assert.match(tables.get("Existing commitments").note,/Showing 2 of 300/);
const spot=tables.get("Monthly Spot savings");
const saved=spot.columns.find(column=>column.label==="Saved");
assert.match(saved.cell(spot.rows[0]),/Not available.*assessed portion USD:5/);
assert.match(saved.total(spot.rows),/USD Not available \(partial\)/);
const costs=tables.get("Existing commitments").columns.find(column=>column.label==="Cost, 30d");
assert.match(costs.total(tables.get("Existing commitments").rows),/EUR EUR:20.*USD USD:10/);
assert.ok(charts.some(chart=>chart.datasets.some(dataset=>dataset.label==="Unclassified")));
""")


def test_anomaly_impact_and_daily_charts_are_currency_scoped():
    run_node(HARNESS + r"""
import {createTabs as createMonitor} from "./web/assets/dashboard-monitor.js";
import {overviewAnomalySummary} from "./web/assets/dashboard-spend.js";
ctx.money=(amount,currency)=>{assert.ok(Number.isFinite(amount));assert.match(currency,/^(USD|EUR)$/);return `${currency}:${amount}`;};
ctx.cardHead=parts=>parts.figure;
const rows=[
  {currency:"USD",day:"2026-09-10",key:"Compute",dimension:"service",direction:"up",delta:10,cost:110,baseline:100,severity:"low"},
  {currency:"EUR",day:"2026-09-11",key:"Storage",dimension:"service",direction:"up",delta:20,cost:220,baseline:200,severity:"low"},
];
ctx.get=async url=>{
  if(url.startsWith("/api/anomalies/alerts"))throw Error("Offline alerts unavailable");
  return {days:7,anomalies:rows,currency:null,impact:null,count:2,increases:2,decreases:0,
    impact_by_currency:{USD:10,EUR:20},
    daily_by_currency:Object.fromEntries(["USD","EUR"].map(currency=>[currency,[
      {day:"2026-09-10",cost:100},{day:"2026-09-11",cost:110}]]))};
};
await createMonitor(ctx).renderAnomalies(1);
assert.match(root.innerHTML,/USD USD:10.*EUR EUR:20/);
assert.equal(overviewAnomalySummary({currency:null,anomalies:rows}).net,null);
assert.deepEqual(charts.map(chart=>chart.currency),["USD","EUR"]);
assert.deepEqual(charts[0].pointColors,["up",null]);
assert.deepEqual(charts[1].pointColors,[null,"up"]);
""")


def test_aks_full_totals_and_capped_details_remain_currency_separate():
    run_node(HARNESS + r"""
ctx.money=(amount,currency)=>{assert.ok(Number.isFinite(amount));assert.match(currency,/^(USD|EUR)$/);return `${currency}:${amount}`;};
ctx.sections=[{id:"overview",cost:null,currency:null},{id:"aks",cost:null,currency:null}];
ctx.get=async()=>({
  days:30,currency:null,total:null,mixed_currency:true,total_by_currency:{USD:1000,EUR:2000},
  clusters:[{cluster:"u",cost:10,currency:"USD",live:true},{cluster:"e",cost:20,currency:"EUR",live:true}],
  total_cluster_records:250,truncated:true,discovery_status:"ok",detected_clusters:2,
  categories_by_currency:{USD:{Compute:1000},EUR:{Compute:2000}},
  trend_by_currency:{USD:[{day:"a",cost:400},{day:"b",cost:600}],EUR:[{day:"a",cost:900},{day:"b",cost:1100}]},
});
await createTabs(ctx).renderAksView(1);
assert.match(root.innerHTML,/AKS spend USD USD:1000.*EUR EUR:2000/);
assert.match(root.innerHTML,/Showing 2 of 250 cluster records/);
assert.match(root.innerHTML,/Share of the estate —/);
const state=tables.get("Clusters");
const column=state.columns.find(column=>column.label==="Cost");
assert.match(column.total(state.rows),/EUR EUR:20.*USD USD:10/);
assert.deepEqual(charts.map(chart=>chart.currency),["USD","EUR"]);
""")
