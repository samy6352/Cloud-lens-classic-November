"""AI billing navigation, source honesty and cached local interactions."""
from pathlib import Path

from tests.test_tags_frontend import HARNESS, run_node


def test_metric_zero_unknown_and_exact_token_summary():
    run_node(r"""
import assert from "node:assert/strict";
import {aiMetricText,aiTokenSummary,filterAiRows,validAiBilling} from "./web/assets/dashboard-ai.js";
assert.equal(aiMetricText({status:"ok",value:0}),"0");
assert.equal(aiMetricText({status:"partial",value:1234}),"1,234");
assert.equal(aiMetricText({status:"no_data",value:0}),"Unavailable");
assert.equal(aiMetricText({status:"ok",value:null}),"Unavailable");
assert.equal(aiMetricText({status:"not_applicable",value:null},true),"Not applicable");
assert.equal(aiMetricText({status:"ok",value:32.14},true),"32.1%");
const rows=[{service:"Azure OpenAI",models:["GPT-4.1"],tokens:{status:"ok",value:50}},
 {service:"AI Search",tokens:{status:"not_applicable",value:null}},
 {service:"Foundry",tokens:{status:"partial",value:0}}];
assert.deepEqual(aiTokenSummary(rows),{value:50,measured:2,total:3,partial:true});
assert.equal(aiTokenSummary([]).value,null);
assert.equal(filterAiRows(rows," gPt-4.1 ").length,1);
assert.equal(filterAiRows(rows,"search").length,1);
assert.equal(filterAiRows(rows,"nothing").length,0);
assert.ok(!validAiBilling({status:"ok",services:[]}));
""")


AI_HARNESS = HARNESS + r"""
import {createTabs as createAi} from "./web/assets/dashboard-ai.js";
const sampleAi=()=>({
  status:"partial",generated_at:"2026-09-10T10:00:00Z",window:{from:"2026-09-01",to:"2026-09-10",days:10},
  totals:[{currency:"USD",cost:10},{currency:"EUR",cost:3}],notices:["One account has unavailable metrics."],
  services:[
    {resource_id:"a",resource_name:"Primary",subscription_id:"sub-a",subscription:"A",service:"Azure OpenAI",
     models:["GPT-4.1"],cost_by_currency:[{currency:"USD",cost:10}],tokens:{status:"ok",value:200,input:150,output:50,note:"Measured input and output"},
     utilization:{status:"ok",value:35.5,aggregation:"Average",note:"PTU capacity"},recommendation:"Review provisioned capacity."},
    {resource_id:"b",resource_name:"Search",service:"AI Search",cost_by_currency:[{currency:"EUR",cost:3}],
     tokens:{status:"not_applicable"},utilization:{status:"unavailable"},recommendation:"Review query capacity."}
  ],models:[
    {resource_id:"a",resource_name:"Primary",model:"gpt-4.1",deployment:"production",tokens:{status:"ok",value:200,input:150,output:50},
     utilization:{status:"ok",value:35.5},cost:null,cost_note:"Resource costs cannot be attributed to this model.",recommendation:"Review output size."}
  ],
});
const aiCache=new Map();
ctx.cached=async(k,fn)=>{if(!aiCache.has(k))aiCache.set(k,Promise.resolve().then(fn));return aiCache.get(k)};
ctx.get=async url=>{requests.push(url);return payload};
ctx.qs=()=>"?days=30&period_month=2026-09&scope=sub-a";
ctx.table=(heading,rows,columns,note,opts)=>{tables.set(heading,{rows,columns,note,opts});return `<section>${heading}</section>`;};
const ai=createAi(ctx);
"""


def test_service_model_table_sources_search_and_cached_selection():
    run_node(AI_HARNESS + r"""
payload=sampleAi();
await ai.renderAiBilling(1);
assert.match(root.innerHTML,/AI Billing/);
assert.match(root.innerHTML,/USD 10/);assert.match(root.innerHTML,/EUR 3/);
assert.match(root.innerHTML,/Observed tokens · partial/);
assert.match(root.innerHTML,/Daily evidence · Read at 2026-09-10T10:00:00Z/);
assert.match(root.innerHTML,/Same-day imports and corrections do not automatically repeat Azure reads/);
assert.match(root.innerHTML,/Use Refresh views to retry missing evidence/);
assert.match(root.innerHTML,/daily amounts can differ from the latest Billing view/);
assert.equal(tables.get("AI service billing").rows.length,2);
assert.equal(tables.get("AI service billing").opts.pageSize,10);
assert.equal(tables.get("AI service billing").opts.search,false,"one search control applies to the whole view");
const columns=tables.get("AI service billing").columns;
assert.match(columns[1].cell(payload.services[0]),/200/);
assert.match(columns[3].cell(payload.services[0]),/35.5%/);
assert.match(columns[4].cell({...payload.services[0],recommendation:'<img src=x>'}),/&lt;img/);
$("aiBillingSearch").value="Search";$("aiBillingSearch").oninput({target:$("aiBillingSearch")});
assert.equal(tables.get("AI service billing").rows.length,1);
$("aiBillingSearch").value="";$("aiBillingSearch").oninput({target:$("aiBillingSearch")});
click("data-ai-mode","models");
const models=tables.get("Model usage");
assert.equal(models.rows[0].model,"gpt-4.1");
assert.match(models.columns[2].cell(models.rows[0]),/Not allocated/);
assert.match(models.columns[2].cell(models.rows[0]),/cannot be attributed/);
assert.equal(requests.length,1,"search/mode switches do not refetch Azure");
await ai.renderAiBilling(1);
assert.equal(requests.length,1,"same-context re-render reuses cached response");
assert.match($("aiAttribution").textContent,/not divided between deployments/);
signature="different";payload={...sampleAi(),services:[],models:[],totals:[],notices:[]};
await ai.renderAiBilling(1);
assert.match($("aiResults").innerHTML,/No model-level usage/);
assert.equal(requests.length,2);
""")


def test_late_or_invalid_responses_cannot_paint_false_data():
    run_node(AI_HARNESS + r"""
let resolve;payload=new Promise(done=>resolve=done);
const work=ai.renderAiBilling(1);
await new Promise(done=>setImmediate(done));
ctx.loadToken=2;root.innerHTML="<section>Different tab</section>";
resolve(sampleAi());await work;
assert.equal(root.innerHTML,"<section>Different tab</section>");
signature="invalid";payload={services:[]};
await assert.rejects(ai.renderAiBilling(2),/incomplete data/);
""")


AI_REFRESH_HARNESS = AI_HARNESS + r"""
import fs from "node:fs";
const shell=fs.readFileSync("./web/assets/dashboard.js","utf8");
const start=shell.indexOf('actions.querySelector("#refreshViews").onclick');
const wiring=shell.slice(start,shell.indexOf("  host.onkeydown",start));
const makeControls=new Function("ctx","ai","aiCache",`
  let current="ai-billing", loadToken=ctx.loadToken, renderSequence=loadToken;
  let renderedComplete=true, beforeLoad=async()=>{};
  const key=ctx.key, activeTabs=ai, button={disabled:false};
  const actions={querySelector:()=>button};
  const clearCache=()=>aiCache.clear(), invalidateAnalysis=()=>{};
  const loadFamily=async family=>{
    if(family!=="ai")throw Error("Only the AI module needs explicit retry");
    return ai;
  };
  const loadTabs=async({preserve})=>{
    if(!preserve)throw Error("Refresh must retain last-good display");
    ctx.loadToken=loadToken;
    await beforeLoad();
    if(current==="ai-billing")await ai.renderAiBilling(loadToken);
  };
  ${wiring}
  return {click:()=>button.onclick({currentTarget:button}),button,
    setCurrent:value=>current=value,setBeforeLoad:fn=>beforeLoad=fn};
`);
const refresh=makeControls(ctx,ai,aiCache);
"""


def test_refresh_views_forces_ai_once_and_keeps_normal_visits_daily_cached():
    run_node(AI_REFRESH_HARNESS + r"""
payload=sampleAi();
await ai.renderAiBilling(ctx.loadToken);
await ai.renderAiBilling(ctx.loadToken);
assert.equal(requests.length,1);
assert.ok(!requests[0].includes("force="));
await refresh.click();
assert.equal(refresh.button.disabled,false);
assert.equal(requests.length,2);
const forced=new URL(requests[1],"http://cloudlens.local");
assert.equal(forced.searchParams.get("force"),"true");
assert.equal(forced.searchParams.getAll("force").length,1);
assert.equal(forced.searchParams.get("days"),"30");
assert.equal(forced.searchParams.get("period_month"),"2026-09");
assert.equal(forced.searchParams.get("scope"),"sub-a");
await ai.renderAiBilling(ctx.loadToken);
assert.equal(requests.length,2,"force result replaces the normal in-memory cache entry");
aiCache.clear();
await ai.renderAiBilling(ctx.loadToken);
assert.equal(requests.length,3);
assert.ok(!requests[2].includes("force="),"later normal cache miss uses the durable daily snapshot");
await refresh.click();
assert.equal(requests.filter(url=>url.includes("force=true")).length,2,"another explicit click can retry again");
""")


def test_ai_retry_intent_belongs_to_one_context_and_cannot_be_consumed_by_late_renderer():
    run_node(AI_HARNESS + r"""
payload=sampleAi();
await ai.renderAiBilling(ctx.loadToken);
const cancel=ai.refreshOnce();
const other=createAi({...ctx,key:name=>`${name}|other-scope`,qs:()=>"?days=7&scope=sub-b"});
await other.renderAiBilling(ctx.loadToken);
assert.ok(!requests.at(-1).includes("force="),"another scope has no retry intent");
ctx.loadToken=2;
await ai.renderAiBilling(1);
assert.ok(!requests.at(-1).includes("force="),"late renderer does not consume a pending retry");
await ai.renderAiBilling(ctx.loadToken);
assert.ok(requests.at(-1).includes("force=true"),"retry bypasses an older cached original-context result");
cancel();
aiCache.clear();
await ai.renderAiBilling(ctx.loadToken);
assert.ok(!requests.at(-1).includes("force="));
""")


def test_ai_refresh_intent_does_not_leak_after_failure_navigation_or_other_tab_refresh():
    run_node(AI_REFRESH_HARNESS + r"""
payload=sampleAi();
refresh.setBeforeLoad(async()=>{throw Error("sections failed before AI render")});
await assert.rejects(refresh.click(),/sections failed/);
assert.equal(refresh.button.disabled,false);
refresh.setBeforeLoad(async()=>{});
await ai.renderAiBilling(ctx.loadToken);
assert.ok(!requests.at(-1).includes("force="),"unconsumed failed refresh is cleared");
refresh.setBeforeLoad(async()=>{refresh.setCurrent("overview")});
await refresh.click();
refresh.setCurrent("ai-billing");
await ai.renderAiBilling(ctx.loadToken);
assert.ok(!requests.at(-1).includes("force="),"navigation cancels an unconsumed retry");
refresh.setBeforeLoad(async()=>{});
refresh.setCurrent("overview");
await refresh.click();
refresh.setCurrent("ai-billing");
await ai.renderAiBilling(ctx.loadToken);
assert.ok(requests.every(url=>!url.includes("force=")),"refreshing another tab never forces AI");
""")


def test_anomaly_direction_filter_order_and_uncertain_states():
    run_node(r"""
import assert from "node:assert/strict";
import {aiAnomalyDirection,aiAnomalySummary,prioritizeAiAnomalies,aiAnomalyRuleText} from "./web/assets/dashboard-ai.js";
const rows=[
 {id:"normal",anomalies:[],anomaly_status:"checked"},
 {id:"missing",anomalies:[],anomaly_status:"unavailable"},
 {id:"spike",anomalies:[{direction:"up"}],anomaly_status:"checked"},
 {id:"drop",anomalies:[{direction:"down"}],anomaly_status:"partial"},
 {id:"both",anomalies:[{direction:"down"},{direction:"up"}],anomaly_status:"checked"},
];
assert.equal(aiAnomalyDirection(rows[2]),"up");
assert.equal(aiAnomalyDirection(rows[3]),"down");
assert.equal(aiAnomalyDirection(rows[4]),"mixed");
assert.equal(aiAnomalyDirection(rows[1]),"");
assert.deepEqual(aiAnomalySummary(rows),{flagged:3,assessed:4,incomplete:2,total:5});
assert.deepEqual(prioritizeAiAnomalies(rows).map(r=>r.id),["spike","drop","both","normal","missing"]);
assert.deepEqual(prioritizeAiAnomalies(rows,true).map(r=>r.id),["spike","drop","both"]);
assert.equal(rows[0].id,"normal","ranking does not mutate source data");
assert.match(aiAnomalyRuleText({window_days:14,min_history:7,min_relative_change:.5,
 materiality:{cost:{delta:15},tokens:{delta:1000},utilization:{delta:20}}}),/14-day.*50%.*1,000 tokens.*20 percentage points/);
""")


def test_anomaly_badges_explain_flags_and_filter_without_network_requests():
    run_node(AI_HARNESS + r"""
payload=sampleAi();
payload.services[0].anomaly_status="checked";
payload.services[0].anomalies=[{metric:"cost",direction:"up",day:"2026-09-07",actual:80,baseline:20,delta:60,percent:300,
  currency:"USD",baseline_samples:10,baseline_from:"2026-08-24",baseline_to:"2026-09-06",note:"<script>unsafe</script>"}];
payload.services[1].anomaly_status="insufficient_history";
await ai.renderAiBilling(1);
const table=tables.get("AI service billing");
assert.equal(table.opts.highlightOutliers,false,"cross-resource high values must not masquerade as temporal anomalies");
assert.match(table.columns[0].cell(payload.services[0]),/data-ai-anomaly="up"/);
assert.match(table.columns[0].cell(payload.services[0]),/\+300.0%/);
assert.match(table.columns[0].cell(payload.services[0]),/10 baseline days/);
assert.match(table.columns[0].cell(payload.services[0]),/&lt;script/);
assert.match(table.columns[0].cell(payload.services[1]),/Insufficient history/);
assert.match($("aiAnomalySummary").textContent,/1 AI resources flagged/);
$("aiOnlyAnomalies").checked=true;$("aiOnlyAnomalies").onchange({target:$("aiOnlyAnomalies")});
assert.equal(tables.get("AI service billing").rows.length,1);
click("data-ai-mode","models");
assert.match($("aiResults").innerHTML,/No flagged rows/);
assert.match($("aiResults").innerHTML,/not proof of normal/);
assert.equal(requests.length,1);
await ai.renderAiBilling(1);
assert.equal($("aiOnlyAnomalies").hasAttribute("checked"),true,"cached re-render retains anomaly-only filter");
""")


def test_ai_billing_belongs_to_overview_and_search():
    run_node(r"""
import assert from "node:assert/strict";
import {sectionFor} from "./web/assets/dashboard-navigation.js";
import {hasRailIcon} from "./web/assets/icons.js";
import {searchableTabs,matchingTabs} from "./web/assets/dashboard-search.js";
const views=[{id:"overview",label:"Overview"},{id:"ai-billing",label:"AI Billing",area:true,overlay:true}];
assert.equal(sectionFor("ai-billing",views).id,"overview");
assert.ok(hasRailIcon("ai-billing"));
const rows=searchableTabs(views);
for(const word of ["tokens","models","foundry","utilization"]){
  assert.ok(matchingTabs(rows,word).some(row=>row.id==="ai-billing"),word);
}
""")
    root = Path(__file__).resolve().parents[1]
    shell = (root / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
    assert '{ id: "ai-billing", label: "AI Billing", area: true, overlay: true }' in shell
    assert '"ai-billing": ["ai", "renderAiBilling"]' in shell
