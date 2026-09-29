"""Offline hourly-billing filtering: tag selection, untagged meters and value breakdowns."""
from tests.test_tags_frontend import run_node


def test_tag_filter_narrows_meters_without_hiding_untagged_spend():
    run_node(r"""
import assert from "node:assert/strict";
import {uptimeKeep, uptimeTagValues, UPTIME_UNTAGGED}
  from "./web/assets/dashboard-spend.js";

const values = ["web", "batch", "prod", ""];
const meter = (id, extra) => ({
  id, name: id, service: "Virtual Machines", meter: "vm", resource_group: "rg", region: "eastus",
  cost: 10, hours: 24, always_on: true, schedule_status: "not_assessed", ...extra,
});
const rows = [
  meter("a", {keys: ["app", "env"], v: [0, 2], cost: 100, hours: 100}),
  meter("b", {keys: ["app"], v: [1], cost: 40, hours: 40}),
  meter("c", {keys: ["app"], v: [0], cost: 20, hours: 20}),
  meter("blank", {keys: ["app"], v: [3], cost: 5, hours: 5}),
  meter("none", {keys: [], v: [], cost: 7, hours: 7}),
  meter("legacy", {cost: 3, hours: 3}),
];
const base = {service: "", group: "", region: "", state: "all", find: "", tagKey: "", tagValue: ""};
const keep = filter => rows.filter(r => uptimeKeep(r, {...base, ...filter}, values)).map(r => r.id);

assert.deepEqual(keep({}), ["a", "b", "c", "blank", "none", "legacy"], "no tag filter keeps all");
assert.deepEqual(keep({tagKey: "app"}), ["a", "b", "c", "blank"]);
assert.deepEqual(keep({tagKey: "env"}), ["a"]);
assert.deepEqual(keep({tagKey: "app", tagValue: "web"}), ["a", "c"]);
assert.deepEqual(keep({tagKey: "app", tagValue: "batch"}), ["b"]);

// An empty value is a value, not a wildcard: `app=""` is one meter, not four.
assert.deepEqual(keep({tagKey: "app", tagValue: ""}), ["a", "b", "c", "blank"],
  "no value chosen means every value under the key");

// Untagged and never-loaded both read as untagged here; the card says which, not the filter.
assert.deepEqual(keep({tagKey: UPTIME_UNTAGGED}), ["none", "legacy"]);

// Tag filters compose with the existing dimensions rather than replacing them.
assert.deepEqual(keep({tagKey: "app", region: "eastus"}), ["a", "b", "c", "blank"]);
assert.deepEqual(keep({tagKey: "app", region: "westus"}), []);
assert.deepEqual(keep({tagKey: "app", find: "b"}), ["b", "blank"]);

const breakdown = uptimeTagValues(rows, "app", values);
assert.deepEqual(breakdown.map(v => v.value), ["web", "batch", ""], "largest value first");
assert.deepEqual(breakdown[0], {value: "web", cost: 120, hours: 120, meters: 2});
assert.equal(breakdown.reduce((n, v) => n + v.meters, 0), keep({tagKey: "app"}).length,
  "the breakdown and the filtered table count the same meters");
assert.deepEqual(uptimeTagValues(rows, "missing", values), []);
assert.deepEqual(uptimeTagValues([], "app", values), []);
""")


def test_a_meter_with_no_tag_array_is_not_treated_as_matching_every_key():
    run_node(r"""
import assert from "node:assert/strict";
import {uptimeKeep, UPTIME_UNTAGGED} from "./web/assets/dashboard-spend.js";
const base = {service: "", group: "", region: "", state: "all", find: "", tagKey: "", tagValue: ""};
// A response predating the tag dimension, or one where tags could not be loaded.
const legacy = {name: "x", service: "", meter: "", resource_group: "", region: "",
  cost: 1, hours: 1, schedule_status: "not_assessed"};
assert.equal(uptimeKeep(legacy, base), true);
assert.equal(uptimeKeep(legacy, {...base, tagKey: "app"}), false);
assert.equal(uptimeKeep(legacy, {...base, tagKey: UPTIME_UNTAGGED}), true);
// Missing value pool must not crash or silently match.
assert.equal(uptimeKeep({...legacy, keys: ["app"], v: [0]}, {...base, tagKey: "app", tagValue: "web"}),
  false);
""")


def test_tag_key_and_value_pickers_replace_the_cost_by_tag_breakdown():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
const nodes=new Map();
const node=id=>{
  if(!nodes.has(id))nodes.set(id,{id,outerHTML:"",value:"",remove(){},
    querySelector:()=>({}),querySelectorAll:()=>[]});
  return nodes.get(id);
};
let html="";
const data={currency:"USD",dates:["2026-09-01","2026-09-02"],daily:{},schedules:[],
  tagged:{cost:69},untagged:{cost:393},
  tag_values:["prod","dev"],
  tag_keys:[{key:"environment",cost:37,hours:404,meters:5,values:2},
            {key:"owner",cost:37,hours:399,meters:3,values:1}],
  resources:[
    {id:"a",name:"a",service:"VM",meter:"m",resource_group:"rg",region:"eastus",
     cost:37,hours:404,schedule_status:"not_assessed",keys:["environment"],v:[0]},
    {id:"b",name:"b",service:"VM",meter:"m",resource_group:"rg",region:"eastus",
     cost:37,hours:399,schedule_status:"not_assessed",keys:["environment"],v:[1]}]};
const tabs=createTabs({
  $:node,loadToken:1,periodMonth:null,setContent:(_,markup)=>{html=markup;},
  cached:async(_,load)=>load(),key:id=>id,get:async()=>data,qs:()=>"?days=30",
  esc:v=>String(v??""),money:(v,c,dp)=>`${c} ${Number(v).toFixed(dp??2)}`,
  moneyHtml:(v,c)=>`${c} ${v}`,kpiRow:()=>"",table:()=>"",bar:()=>"",drawChart:()=>{},
  whenNear:()=>{},
  wireTables:()=>{},title:()=>"",portalLinkMarkup:()=>"",
});

await tabs.renderUptime(1);
// The pair is the tag control now: no bar list, and both pickers are always in the filter row.
assert.doesNotMatch(html,/Cost by tag/);
assert.doesNotMatch(html,/uptime-tagbars/);
assert.match(html,/id="upTagKey"/);
assert.match(html,/id="upTagValue"/);
assert.match(html,/All tags \(2\)/);
assert.match(html,/environment/);
assert.match(html,/Untagged/);
// Without a key there is nothing to choose between, so the value picker says so rather than
// offering an empty list.
assert.match(html,/id="upTagValue"[^>]*disabled/);
assert.match(html,/Pick a tag key first/);
// The attribution figures the removed panel carried survive as one line.
assert.match(html,/69 tagged/);
assert.match(html,/393 unattributed/);

// Choosing a key fills the value picker with that key's values, costs and meter counts.
node("upTagKey").onchange({target:{value:"environment"}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.doesNotMatch(html,/id="upTagValue"[^>]*disabled/);
assert.match(html,/All values \(2\)/);
assert.match(html,/prod/);
assert.match(html,/dev/);
assert.match(html,/1 meter</);
""")


def test_the_tag_key_picker_names_the_application_it_usually_carries():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
const nodes=new Map();
const node=id=>{
  if(!nodes.has(id))nodes.set(id,{id,outerHTML:"",value:"",innerHTML:"",remove(){},
    querySelector:()=>({}),querySelectorAll:()=>[]});
  return nodes.get(id);
};
let html="";
const data={currency:"USD",dates:["2026-09-01","2026-09-02"],daily:{},schedules:[],
  tag_values:["prod"],tag_keys:[{key:"Application",cost:9,hours:9,meters:1,values:1}],
  resources:[{id:"a",name:"a",service:"VM",meter:"m",resource_group:"rg",region:"eastus",
    cost:9,hours:9,schedule_status:"not_assessed",keys:["Application"],v:[0]}]};
const tabs=createTabs({
  $:node,loadToken:1,periodMonth:null,setContent:(_,markup)=>{html=markup;},
  cached:async(_,load)=>load(),key:id=>id,get:async()=>data,qs:()=>"?days=30",
  esc:v=>String(v??""),money:(v,c,dp)=>`${c} ${Number(v).toFixed(dp??2)}`,
  moneyHtml:(v,c)=>`${c} ${v}`,kpiRow:()=>"",table:()=>"",bar:()=>"",drawChart:()=>{},
  whenNear:()=>{},
  wireTables:()=>{},title:()=>"",portalLinkMarkup:()=>"",
});
await tabs.renderUptime(1);
// "Tag key" alone means nothing to someone who was sent here to find one application's cost.
assert.match(html,/Tag key \/ Application/);
assert.doesNotMatch(html,/>Tag key\s*\n?\s*</);
""")


def test_a_budget_is_matched_to_the_selected_tag_value_not_to_its_name():
    run_node(r"""
import assert from "node:assert/strict";
import {uptimeBudgetMatches, UPTIME_UNTAGGED} from "./web/assets/dashboard-spend.js";

const budget = (name, extra) => ({
  name, percent_used: 10, filter_exact: true, tag_filter: [["Application", ["payments"]]], ...extra,
});
const names = rows => rows.map(m => m.budget.name);

const rows = [
  budget("payments-monthly"),
  budget("payments-and-prod", {tag_filter: [["Application", ["payments"]], ["env", ["prod"]]]}),
  budget("several-apps", {tag_filter: [["Application", ["payments", "billing"]]]}),
  budget("other-app", {tag_filter: [["Application", ["billing"]]]}),
  budget("untranslatable", {filter_exact: false}),
  budget("subscription-wide", {tag_filter: []}),
];

const hit = uptimeBudgetMatches(rows, "Application", "payments");
assert.deepEqual(names(hit),
  ["payments-monthly", "payments-and-prod", "several-apps"],
  "only budgets whose tag filter actually covers this key and value");

// Azure matches tag keys without case; two spellings of one key are one key.
assert.deepEqual(names(uptimeBudgetMatches(rows, "application", "payments")), names(hit));
// Tag *values* are not case-insensitive: a different case is a different value in a cost report.
assert.deepEqual(uptimeBudgetMatches(rows, "Application", "Payments"), []);

// A budget that also filters on something else covers this selection, but watches less of it.
const narrower = hit.find(m => m.budget.name === "payments-and-prod");
assert.deepEqual(narrower.extra, [{key: "env", values: ["prod"]}]);
assert.equal(narrower.partial, false);
// One that lists several values of the key covers more than this selection.
assert.equal(hit.find(m => m.budget.name === "several-apps").partial, true);
assert.deepEqual(hit.find(m => m.budget.name === "payments-monthly").extra, []);

// Worst first, so a breached budget is the one in view when several match.
const ordered = uptimeBudgetMatches(
  [budget("calm", {percent_used: 12}), budget("burst", {percent_used: 140})],
  "Application", "payments");
assert.deepEqual(names(ordered), ["burst", "calm"]);

// Nothing selected, or a selection a budget cannot express, asks Azure for nothing.
assert.deepEqual(uptimeBudgetMatches(rows, "", ""), []);
assert.deepEqual(uptimeBudgetMatches(rows, "Application", ""), []);
assert.deepEqual(uptimeBudgetMatches(rows, UPTIME_UNTAGGED, "x"), []);
assert.deepEqual(uptimeBudgetMatches(null, "Application", "payments"), []);
assert.deepEqual(uptimeBudgetMatches([null, {}], "Application", "payments"), []);
""")


def test_the_budget_panel_appears_only_once_a_tag_value_is_chosen():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";

const nodes=new Map();
// The panel finds its chart hosts and its links through the DOM after writing innerHTML, so the
// stub has to answer those two selectors rather than returning nothing for everything.
const charts=[{dataset:{chart:"0"}}];
const links=[];
const node=id=>{
  if(!nodes.has(id))nodes.set(id,{id,outerHTML:"",value:"",innerHTML:"",remove(){},
    querySelector:()=>({}),
    querySelectorAll:sel=>sel.includes("budget-chart")?charts:sel.includes("tagsfor")?links:[]});
  return nodes.get(id);
};
let html="";
const data={currency:"USD",dates:["2026-09-01","2026-09-02"],daily:{},schedules:[],
  tag_values:["payments","billing"],
  tag_keys:[{key:"Application",cost:9,hours:9,meters:2,values:2}],
  resources:[
    {id:"a",name:"a",service:"VM",meter:"m",resource_group:"rg",region:"eastus",
     cost:9,hours:9,schedule_status:"not_assessed",keys:["Application"],v:[0]},
    {id:"b",name:"b",service:"VM",meter:"m",resource_group:"rg",region:"eastus",
     cost:4,hours:4,schedule_status:"not_assessed",keys:["Application"],v:[1]}]};
let budgets={budgets:[{name:"payments-monthly",subscription:"Prod",amount:500,
  current_spend:250,percent_used:50,status:"ok",currency:"USD",time_grain:"Monthly",
  filter:"Application = payments",filter_exact:true,tag_filter:[["Application",["payments"]]],
  trend:{labels:["2026-09-01","2026-09-02"],cumulative:[10,20],total:20}}]};

const asked=[];
let drawn=0;
const tabs=createTabs({
  $:node,loadToken:1,periodMonth:null,setContent:(_,markup)=>{html=markup;},
  cached:async(_,load)=>load(),key:id=>id,qs:()=>"?days=30&scope=s1",
  get:async url=>{asked.push(url); return url.startsWith("/api/budgets")?budgets:data;},
  esc:v=>String(v??""),money:(v,c,dp)=>`${c} ${Number(v).toFixed(dp??2)}`,
  moneyHtml:(v,c)=>`${c} ${v}`,kpiRow:()=>"",table:()=>"",bar:()=>"",drawChart:()=>{},
  budgetCard:b=>`<article class="budget">${b.name}<div class="budget-chart" data-chart="0"></div></article>`,
  drawBudgetChart:()=>{drawn+=1;},budgetPortalUrl:()=>"#",showTagsFor:()=>{},
  whenNear:(host,prepare)=>Promise.resolve(prepare()).then(commit=>commit()),
  wireTables:()=>{},title:()=>"",portalLinkMarkup:()=>"",
});

// A key on its own is not a selection a budget can answer for, so nothing is fetched.
await tabs.renderUptime(1);
node("upTagKey").onchange({target:{value:"Application"}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(node("uptimeBudget").innerHTML,"");
assert.equal(asked.some(u=>u.startsWith("/api/budgets")),false,
  "no live Azure read until there is a value to match");

// Picking the value brings its budget in, with the caption that stops the two totals being
// read as the same money.
node("upTagValue").onchange({target:{value:"payments"}});
await new Promise(resolve=>setTimeout(resolve,0));
const panel=node("uptimeBudget").innerHTML;
assert.match(panel,/Budget for Application = payments/);
assert.match(panel,/payments-monthly/);
assert.match(panel,/only the hour-priced ones/);
assert.equal(drawn,1,"the budget's trend is drawn once its host is in the document");
assert.equal(asked.filter(u=>u.startsWith("/api/budgets")).length,1);

// A value with no budget behind it leaves no empty shell on the page.
node("upTagValue").onchange({target:{value:"billing"}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(node("uptimeBudget").innerHTML,"");
budgets={budgets:[],demo_mode:true,
  note:"Budgets are not part of the PostgreSQL demo data."};
node("upTagValue").onchange({target:{value:"payments"}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.match(node("uptimeBudget").innerHTML,/PostgreSQL demo data/);
""")
