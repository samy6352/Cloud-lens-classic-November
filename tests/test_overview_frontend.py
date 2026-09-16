"""Overview highlights use the supplied period data and preserve zero/unknown distinctions."""
from tests.test_tags_frontend import run_node


def test_anomaly_summary_separates_direction_severity_and_net_effect():
    run_node(r"""
import assert from "node:assert/strict";
import {overviewAnomalySummary} from "./web/assets/dashboard-spend.js";
const data={currency:"EUR",window_to:"2026-08-29",anomalies:[
  {day:"2026-08-20",dimension:"service",key:"Storage",delta:600,severity:"high",direction:"up"},
  {day:"2026-08-22",dimension:"subscription",key:"prod",delta:-400,severity:"medium",direction:"down"},
  {day:"2026-08-25",dimension:"service",key:"Compute",delta:120,severity:"high",direction:"up"}]};
const s=overviewAnomalySummary(data);
assert.equal(s.count,3);
assert.equal(s.high,2);
assert.equal(s.increases,2);
assert.equal(s.decreases,1);
assert.equal(s.net,320,"a rise offset by a fall has not moved the bill by their sum");
assert.equal(s.largest.key,"Storage");
assert.equal(s.currency,"EUR");
assert.equal(s.to,"2026-08-29");
// A fall can be the biggest thing that happened.
assert.equal(overviewAnomalySummary({anomalies:[{key:"up",delta:10,direction:"up"},
  {key:"down",delta:-90,direction:"down"}]}).largest.key,"down");
const empty=overviewAnomalySummary({});
assert.equal(empty.count,0);
assert.equal(empty.largest,null);
assert.equal(empty.net,0);
assert.equal(overviewAnomalySummary(undefined).count,0);
// Falls back through the settle-aware window fields rather than inventing a date.
assert.equal(overviewAnomalySummary({settled_to:"2026-08-27",anomalies:[]}).to,"2026-08-27");
assert.equal(overviewAnomalySummary({anomalies:[]}).to,null);
""")


def test_overview_anomaly_panel_loads_after_the_page_and_links_through():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
const nodes=new Map();
const node=id=>{
  if(!nodes.has(id))nodes.set(id,{id,outerHTML:"",remove(){},querySelector:()=>({}),
    querySelectorAll:()=>[]});
  return nodes.get(id);
};
let html="";const selected=[];const asked=[];
const data={days:30,currency:"USD",kpis:{total:1,previous:1,change_pct:0,daily_avg:1,
  resources:1,services:1,regions:1,subscriptions:1},areas:[],services:[],subscriptions:[],
  regions:[],resource_groups:[],movers:[],trend:{labels:[],values:[],previous:[]}};
const anomalies={currency:"USD",window_to:"2026-08-29",anomalies:[
  {day:"2026-08-20",dimension:"service",key:"Storage",delta:600,severity:"high",direction:"up"},
  {day:"2026-08-22",dimension:"subscription",key:"prod",delta:-100,severity:"low",direction:"down"}]};
const tabs=createTabs({
  $:node,loadToken:1,periodMonth:null,executiveData:async()=>data,
  cached:(_,load)=>load(),key:name=>name,
  setContent:(_,markup)=>{html=markup;},whenNear:()=>{},table:()=>"",
  kpiRow:items=>items.map(i=>`${i.label} ${i.value} ${i.sub??""}`).join("|"),
  esc:v=>String(v??""),moneyHtml:(v,c)=>`${c} ${v}`,trendClass:()=>"",pct:()=>"",
  qs:()=>"?days=30",get:async url=>{asked.push(url);return anomalies;},
  selectTab:tab=>selected.push(tab),navigate:tab=>selected.push(tab),openAnalysis:()=>{},
});
await tabs.renderOverview(1);
// The tile is in the first paint, but only as a placeholder: the spend figures never wait.
assert.match(html,/id="overviewAnomsValue"/);
assert.match(html,/Checking for unusual spend/);
assert.ok(html.indexOf('id="overviewAnomsBody"')<html.indexOf('class="overview-main"'),
  "the anomaly figure sits in the KPI strip, above the main row");
await new Promise(resolve=>setTimeout(resolve,0));
assert.deepEqual(asked,["/api/dashboard/anomalies?days=30"]);
const value=nodes.get("overviewAnomsValue").innerHTML;
assert.match(value,/2/,"flagged days is the headline figure");
assert.match(value,/1 high/);
assert.match(value,/through 2026-08-29/,"the judged window stays available on the figure");
const filled=nodes.get("overviewAnomsBody").outerHTML;
assert.match(filled,/1 up . 1 down/);
assert.match(filled,/\+USD 500/,"net effect, not the sum of absolute movements");
assert.match(filled,/Review/);
assert.doesNotMatch(filled,/NaN|undefined/);
assert.doesNotMatch(value,/NaN|undefined/);
nodes.get("overviewAnomsOpen").onclick();
assert.deepEqual(selected,["anomalies"]);
""")


def test_overview_anomaly_panel_survives_a_failed_or_empty_check():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
const build=respond=>{
  const nodes=new Map();
  const node=id=>{
    if(!nodes.has(id))nodes.set(id,{id,outerHTML:"",remove(){},querySelector:()=>({}),
      querySelectorAll:()=>[]});
    return nodes.get(id);
  };
  const selected=[];
  const data={days:30,currency:"USD",kpis:{total:1,previous:1,change_pct:0,daily_avg:1,
    resources:1,services:1,regions:1,subscriptions:1},areas:[],services:[],subscriptions:[],
    regions:[],resource_groups:[],movers:[],trend:{labels:[],values:[],previous:[]}};
  const tabs=createTabs({
    $:node,loadToken:1,periodMonth:null,executiveData:async()=>data,
    cached:(_,load)=>load(),key:name=>name,
    setContent:()=>{},whenNear:()=>{},table:()=>"",kpiRow:()=>"",
    esc:v=>String(v??""),moneyHtml:(v,c)=>`${c} ${v}`,trendClass:()=>"",pct:()=>"",
    qs:()=>"",get:respond,selectTab:tab=>selected.push(tab),navigate:tab=>selected.push(tab),openAnalysis:()=>{},
  });
  return {tabs,nodes,selected};
};
const quiet=build(async()=>({currency:"USD",settled_to:"2026-08-27",anomalies:[]}));
await quiet.tabs.renderOverview(1);
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(quiet.nodes.get("overviewAnomsValue").innerHTML,"0");
assert.match(quiet.nodes.get("overviewAnomsBody").outerHTML,/Nothing unusual/);
assert.match(quiet.nodes.get("overviewAnomsBody").outerHTML,/through 2026-08-27/);
quiet.nodes.get("overviewAnomsOpen").onclick();
assert.deepEqual(quiet.selected,["anomalies"],"a quiet period still offers the full view");

// A failing anomaly pass must not put an error over the spend figures.
const broken=build(async()=>{throw new Error("timeout");});
await broken.tabs.renderOverview(1);
await new Promise(resolve=>setTimeout(resolve,0));
const text=broken.nodes.get("overviewAnomsBody").outerHTML;
assert.equal(broken.nodes.get("overviewAnomsValue").innerHTML,"—");
assert.match(text,/Check unavailable/);
assert.doesNotMatch(text,/timeout/,"the tile reports the gap, not the exception");
broken.nodes.get("overviewAnomsOpen").onclick();
assert.deepEqual(broken.selected,["anomalies"]);
""")


def test_highlights_select_available_values_without_mutation():
    run_node(r"""
import assert from "node:assert/strict";
import {overviewHighlights} from "./web/assets/dashboard-spend.js";
const data={movers:[{name:"Credit",delta:-99},{name:"Unknown",delta:null},
  {name:"Small",delta:2},{name:"Largest",delta:7}],
  services:[{name:"Unknown",cost:null},{name:"Zero",cost:0},{name:"Top",cost:12}],
  kpis:{top5_share_pct:0}};
const before=JSON.stringify(data);
const signals=overviewHighlights(data);
assert.equal(signals.increase.name,"Largest");
assert.equal(signals.service.name,"Top");
assert.equal(signals.concentration,0);
assert.equal(JSON.stringify(data),before);
assert.deepEqual(overviewHighlights({}),{increase:null,service:null,concentration:null});
assert.equal(overviewHighlights({services:[{cost:0}]}).service.cost,0);
assert.equal(overviewHighlights({services:[{cost:-3},{cost:-1}]}).service.cost,-1);
assert.equal(overviewHighlights({movers:[{delta:0},{delta:-1},{delta:NaN}]}).increase,null);
assert.equal(overviewHighlights({kpis:{top5_share_pct:125}}).concentration,125,"credits can change net-spend shares");
""")


def test_overview_preserves_zero_previous_cost_unknown_values_and_period_context():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
let html="",requests=0,analysisOpens=0;
const selected=[];
const control={},buttons=["savings","budgets","report","resources"].map(tab=>({dataset:{overviewTab:tab}}));
const home={querySelector:()=>control,querySelectorAll:selector=>selector==="[data-overview-tab]"?buttons:[]};
const host={remove(){}};
const data={days:31,as_of:"2026-08-31",currency:"EUR",mixed_currency:false,
  kpis:{total:0,previous:0,change_pct:null,daily_avg:null,resources:0,services:0,regions:0,subscriptions:0},
  areas:[],services:[],subscriptions:[],regions:[],resource_groups:[],movers:[],
  trend:{labels:[],values:[],previous:[]}};
const tabs=createTabs({
  $:id=>id==="overviewHome"?home:host,loadToken:1,periodMonth:"2026-08",
  executiveData:async()=>{requests++;return data;},
  setContent:(_,markup)=>{html=markup;},whenNear:()=>{},table:()=>"",
  kpiRow:items=>items.map(item=>`${item.label} ${item.value} ${item.sub}`).join("|"),
  esc:value=>String(value??""),moneyHtml:(value,currency)=>`${currency} ${value}`,
  trendClass:()=>"",pct:()=>{throw new Error("Unknown percentage must not be formatted");},
  selectTab:tab=>selected.push(tab),navigate:tab=>selected.push(tab),openAnalysis:()=>analysisOpens++,
});
await tabs.renderOverview(1);
assert.equal(requests,1);
assert.match(html,/Previously EUR 0/);
assert.match(html,/No net change/);
assert.match(html,/Not comparable/);
assert.match(html,/Average per day Not available/);
assert.match(html,/Billed resources 0/);
assert.match(html,/August 2026/);
assert.doesNotMatch(html,/Multiple billing currencies/);
assert.doesNotMatch(html,/NaN|undefined/);
assert.ok(html.indexOf('id="overviewEstate"') < html.indexOf('id="overviewBreakdowns"'),
  "Estate view precedes Cost breakdowns in reading and keyboard order");
for(const id of ["overviewBreakdowns","overviewEstate"]){
  assert.doesNotMatch(html.match(new RegExp(`<details[^>]*id="${id}"[^>]*>`))[0],/\bopen\b/);
}
buttons.forEach(button=>button.onclick());
control.onclick();
assert.deepEqual(selected,["savings","budgets","report","resources"]);
assert.equal(analysisOpens,1,"Analysis starts only on the explicit shortcut");
assert.equal(requests,1,"shortcuts do not rebuild the executive response");
""")
