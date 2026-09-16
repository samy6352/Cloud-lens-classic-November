"""Offline tag explorer behavior; no warehouse, browser dependency, or Azure writes."""
import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_node(script):
    node = runpy.run_path(str(ROOT / "tests" / "test_dashboard_frontend.py"))["node"]
    result = subprocess.run(
        [node(), "--input-type=module", "-"], input=script, cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_resource_views_and_key_value_selection():
    run_node(r"""
import assert from "node:assert/strict";
import {filterTagResources, resourceTagStatus} from "./web/assets/dashboard-spend.js";
const data = {
  values: ["prod", "dev", "Ana", "Ben", ""],
  resources: [
    {id:"a", keys:["Environment","Owner"], v:[0,2], cost:100},
    {id:"b", keys:["Environment","Owner"], v:[1,3], cost:50},
    {id:"c", keys:["Owner"], v:[2], cost:25},
    {id:"d", keys:[], v:[], cost:10},
    {id:"e", cost:7},
    {id:"blank", keys:["Environment"], v:[4], cost:0},
  ],
};
const ids = state => filterTagResources(data, state).map(r => r.id);
assert.deepEqual(ids(), ["a","b","c","d","e","blank"], "initial exploration includes all resources");
assert.deepEqual(ids({resourceView:"tagged"}), ["a","b","c","blank"]);
assert.deepEqual(ids({resourceView:"untagged"}), ["d"], "missing tag metadata is not untagged");
assert.deepEqual(ids({resourceView:"unknown"}), ["e"]);
assert.equal(resourceTagStatus(data.resources[4],data), "unknown");
const selected = new Set(["Environment","Owner"]);
const values = new Map([["Environment",new Set(["prod","dev"])],["Owner",new Set(["Ana"])]]);
assert.deepEqual(ids({selected,values}), ["a"], "AND across keys, OR within each key");
assert.deepEqual(ids({selected,values,mode:"any"}), ["a","b","c"]);
assert.deepEqual(ids({selected:new Set(["owner"])}), [], "keys remain case sensitive");
assert.deepEqual(ids({selected:new Set(["Environment"]), values:new Map([["Environment",new Set([""]) ]])}), ["blank"]);
assert.deepEqual(ids({selected:new Set(["Environment"]), values:new Map([["Environment",new Set()]])}), ["a","b","blank"]);
assert.deepEqual(ids({selected,values,resourceView:"untagged"}), ["d"], "untagged pauses key filters");
assert.deepEqual(ids({selected,values,resourceView:"unknown"}), ["e"], "unknown pauses key filters");
assert.equal(selected.size,2, "view switching does not mutate selection");
assert.equal(values.get("Environment").size,2);
assert.deepEqual(ids({selected:new Set(["Missing"])}), [], "an unavailable selected key never broadens the result");
const unknown = {...data, tags_not_loaded:true};
assert.equal(resourceTagStatus(data.resources[3], unknown), "unknown");
assert.equal(resourceTagStatus({...data.resources[3],tag_status:"untagged"},unknown),"untagged",
  "a confirmed source row is not overridden by another resource's missing tags");
assert.equal(filterTagResources(unknown,{resourceView:"untagged"}).length,0);
assert.equal(filterTagResources(unknown).length,6);
assert.equal(filterTagResources({}).length,0);
assert.equal(filterTagResources(data,{selected,values,mode:"any"}).reduce((n,r)=>n+r.cost,0),175,
  "a resource matching multiple keys counts only once");
""")


# A deliberately small DOM adapter exercises production event handlers and redraws.
# Browser layout and native focus behavior are covered by the browser smoke harness.
HARNESS = r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
const decode = s => s.replaceAll("&quot;",'"').replaceAll("&#39;","'").replaceAll("&lt;","<").replaceAll("&gt;",">").replaceAll("&amp;","&");
const escape = s => String(s ?? "").replaceAll("&","&amp;").replaceAll('"',"&quot;").replaceAll("<","&lt;").replaceAll(">","&gt;");
const registry = new Map();
class Element {
  constructor(tag = "div", attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs; this.id = attrs.id;
    this.children = []; this.value = attrs.value || ""; this.hidden = "hidden" in attrs;
    this.options = [];
    this.isConnected = true; this.selectionStart = null; this.selectionEnd = null;
    this.dataset = Object.fromEntries(Object.entries(attrs).filter(([k]) => k.startsWith("data-"))
      .map(([k,v]) => [k.slice(5).replace(/-([a-z])/g, (_,c) => c.toUpperCase()),v]));
    this.classList = {toggle:()=>{}};
    if (this.id) registry.set(this.id,this);
  }
  set innerHTML(markup) {
    for (const child of this.querySelectorAll("*")) {
      child.isConnected = false;
      if (registry.get(child.id) === child) registry.delete(child.id);
    }
    this.html = markup; this.children = [];
    for (const match of markup.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*)>/gi)) {
      const attrs = {};
      for (const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[a[1]] = decode(a[2] || "");
      this.children.push(new Element(match[1],attrs));
    }
    let select;
    for (const child of this.children) {
      if (child.tagName === "SELECT") select = child;
      if (child.tagName === "OPTION" && select) {
        select.options.push(child);
        if (select.options.length === 1 || child.hasAttribute("selected")) select.value = child.value;
      }
    }
  }
  get innerHTML() { return this.html || ""; }
  replaceChildren(...children) {
    for (const child of this.querySelectorAll("*")) {
      child.isConnected = false;
      if (registry.get(child.id) === child) registry.delete(child.id);
    }
    this.children = [...children];
    this.html = "";
    for (const child of this.querySelectorAll("*")) {
      child.isConnected = this.isConnected;
      if (child.id && child.isConnected) registry.set(child.id, child);
    }
  }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap(c => [c,...c.querySelectorAll("*")]);
    if (selector === "*") return descendants;
    if (selector === "button, input") return descendants.filter(c => ["BUTTON","INPUT"].includes(c.tagName));
    const attribute = /^\[([^\]]+)\]$/.exec(selector)?.[1];
    return attribute ? descendants.filter(c => c.hasAttribute(attribute)) : [];
  }
  hasAttribute(key) { return key in this.attrs; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  setAttribute(key,value) { this.attrs[key] = value; }
  focus() { document.activeElement = this; }
  setSelectionRange(start,end) { this.selectionStart = start; this.selectionEnd = end; }
  scrollIntoView() {}
}
const $ = id => registry.get(id);
globalThis.document = {activeElement:null};
globalThis.fetch = () => { throw Error("No API writes permitted in explorer tests"); };
const root = new Element("main",{id:"tabBody"});
const tables = new Map(), charts = [], requests = [], wired = [];
const budgetRequests = [], budgetCache = new Map();
let budgetPayload = {budgets:[]};
let signature = "tags|30|scope-a", payload;
const ctx = {
  $, loadToken:1, key:name=>`${signature}|${name}`, qs:()=>"", scopeIds:()=>["sub-a"],
  cached:async (key,fn)=>{
    if(!key.includes("tag-budgets"))return fn();
    if(!budgetCache.has(key))budgetCache.set(key,Promise.resolve().then(fn).catch(error=>{budgetCache.delete(key);throw error;}));
    return budgetCache.get(key);
  },
  get:async url=>{
    if(url.startsWith("/api/budgets")){budgetRequests.push(url);return budgetPayload;}
    requests.push(url); assert.match(url,/^\/api\/dashboard\/tags/); return payload;
  },
  setContent:(host,markup)=>{host.innerHTML = markup;},
  esc:escape, title:s=>s[0].toUpperCase()+s.slice(1),
  money:n=>String(n), moneyHtml:n=>String(n),
  kpiRow:rows=>rows.map(r=>`${r.label} ${r.value} ${r.sub || ""}`).join(""),
  table:(heading,rows,columns,note,opts)=>{
    tables.set(heading,{rows,columns,note,opts});
    return `<section><h3>${heading}</h3></section>`;
  },
  wireTables:host=>wired.push(host), drawChart:(_,config)=>charts.push(config),
  portalLinkMarkup:(row,scope,name)=>`portal:${row.id}:${name}`,
  allSubscriptions:()=>[{id:"sub-a",name:"Subscription A"}],
  whoAmI:async()=>({}), toast:()=>{}, selectTab:()=>{}, bar:()=>"",
};
const tabs = createTabs(ctx);
const sample = () => ({
  currency:"USD", native_currencies:{"sub-a":["USD"]}, total:185, days:30, dates:[], values:["prod","dev","Ana","Ben"],
  tagged:{cost:175,resources:3}, untagged:{cost:10,resources:1},
  keys:[{key:"Environment",cost:150,resources:2,values:2},{key:"Owner",cost:175,resources:3,values:2}],
  resources:[
    {id:"a",name:"Alpha",service:"Compute",keys:["Environment","Owner"],v:[0,2],cost:100,subscription:"Subscription A",subscription_id:"sub-a"},
    {id:"b",name:"Beta",service:"Compute",keys:["Environment","Owner"],v:[1,3],cost:50,subscription:"Subscription A",subscription_id:"sub-a"},
    {id:"c",name:"Gamma",service:"Storage",keys:["Owner"],v:[2],cost:25,subscription:"Subscription A",subscription_id:"sub-a"},
    {id:"d",name:"Delta",service:"Storage",keys:[],v:[],cost:10,subscription:"Subscription A",subscription_id:"sub-a"},
  ],
});
const find = (attr,value,host=root) => host.querySelectorAll(`[${attr}]`).find(e=>e.getAttribute(attr)===value);
const click = (attr,value,host=root) => { const node=find(attr,value,host); assert.ok(node,`${attr}=${value}`); node.focus(); node.onclick(); };
const matching = () => tables.get("Matching resources").rows.map(r=>r.id);
"""


def test_initial_render_filters_search_undo_and_focus():
    run_node(HARNESS + r"""
payload = sample();
await tabs.renderTags(1);
assert.deepEqual(matching(),["a","b","c","d"]);
assert.match($("tagSumSub").textContent,/4 of 4 loaded resources/);
assert.equal($("tagBudgetBtn").hidden,true);
assert.equal($("tagBudget").innerHTML,"");
const resourceTable = tables.get("Matching resources");
assert.equal(resourceTable.opts.pageSize,15);
assert.equal(resourceTable.opts.key,"tags-resources");
assert.match(resourceTable.columns[0].cell(payload.resources[0]),/^portal:a:/);

click("data-tag","Environment",$("tagChips"));
assert.deepEqual(matching(),["a","b"]);
assert.equal(document.activeElement.dataset.tag,"Environment","chip focus survives redraw");
click("data-vv","prod",$("tagValueGroups"));
assert.deepEqual(matching(),["a"]);
assert.equal(document.activeElement.dataset.vv,"prod");
click("data-tag","Owner",$("tagChips"));
click("data-vv","Ben",$("tagValueGroups"));
assert.match($("tagResults").innerHTML,/No resource carries all/);
click("data-mode","any");
assert.deepEqual(matching(),["a","b"]);
assert.match($("tagModeHint").textContent,/ANY.*OR/);
assert.match($("tagModeHint").textContent,/within one key always match ANY/);
let search = find("data-value-search","Owner",$("tagValueGroups"));
search.value="not present"; search.focus(); search.setSelectionRange(4,4); search.oninput();
search = find("data-value-search","Owner",$("tagValueGroups"));
assert.equal(document.activeElement,search);
assert.equal(search.selectionStart,4,"value search caret survives redraw");
assert.match($("tagValueGroups").innerHTML,/No values match this search/);
assert.deepEqual(matching(),["a","b"],"value discovery search never changes selection");
$("tagFilter").value="own"; $("tagFilter").oninput();
assert.match($("tagKeyCount").textContent,/1 of 2 keys shown/);
assert.match($("tagSelected").innerHTML,/Environment/,"search never hides the selected summary");

click("data-resource-view","untagged");
assert.deepEqual(matching(),["d"]);
assert.match($("tagModeHint").textContent,/paused/);
assert.equal($("tagBudgetBtn").hidden,true);
click("data-resource-view","all");
assert.deepEqual(matching(),["a","b"],"selection is retained when switching resource views");
$("tagClear").onclick();
assert.deepEqual(matching(),["a","b","c","d"]);
assert.equal($("tagFilter").value,"");
assert.equal($("tagUndo").hidden,false);
$("tagUndo").onclick();
assert.deepEqual(matching(),["a","b"]);
assert.equal($("tagFilter").value,"own");
assert.equal(find("data-mode","any").getAttribute("aria-checked"),"true");
assert.equal(find("data-value-search","Owner",$("tagValueGroups")).value,"not present");

await tabs.renderTags(1);
assert.deepEqual(matching(),["a","b"]);
assert.equal(find("data-mode","any").getAttribute("aria-checked"),"true","mode is restored after full render");
assert.equal($("tagFilter").value,"own");
click("data-tagview","chargeback");
assert.ok(tables.has("Spend breakdown by Environment"));
click("data-tagview","explorer");
assert.deepEqual(matching(),["a","b"]);
assert.equal(find("data-mode","any").getAttribute("aria-checked"),"true");
signature="tags|90|scope-a"; await tabs.renderTags(1);
assert.deepEqual(matching(),["a","b","c","d"],"scope/period changes reset old filters");
assert.equal(find("data-mode","all").getAttribute("aria-checked"),"true");
assert.equal(requests.length,3,"local interactions do not fetch or write");
""")


def test_values_dropdown_requires_keys_and_replaces_the_uncosted_dropdown():
    run_node(HARNESS + r"""
payload = {...sample(),uncosted:[{key:"No cost",resources:4},{key:'Owner "legacy"',resources:2}]};
await tabs.renderTags(1);
assert.equal($("tagKeysToggle").getAttribute("popovertarget"),"tagKeysMenu");
assert.equal($("tagKeysMenu").getAttribute("popover"),"auto");
assert.equal($("tagKeysMenu").getAttribute("role"),"dialog");
assert.equal($("tagKeysToggle").getAttribute("aria-expanded"),"false");
assert.match($("tagKeysLabel").textContent,/Select tag keys \(2\)/);
assert.equal($("tagValuesToggle").getAttribute("popovertarget"),"tagValuesMenu");
assert.equal($("tagValuesMenu").getAttribute("popover"),"auto");
assert.equal($("tagValuesToggle").disabled,true);
assert.equal($("tagValuesLabel").textContent,"Select a tag first");
assert.doesNotMatch(root.innerHTML,/uncostedTags|uncostedTagFilter|keys without spend|data-uncosted-key/);
const key = find("data-tag","Environment",$("tagChips"));
assert.equal(key.tagName,"INPUT");
assert.equal(key.getAttribute("type"),"checkbox");
click("data-tag","Environment",$("tagChips"));
assert.equal(find("data-tag","Environment",$("tagChips")).hasAttribute("checked"),true);
assert.equal($("tagKeysLabel").textContent,"Environment");
assert.equal($("tagValuesToggle").disabled,false);
assert.equal($("tagValuesLabel").textContent,"All values");
assert.doesNotMatch($("tagChips").innerHTML,/data-vk=/,"keys menu no longer contains value controls");
assert.match($("tagValueGroups").innerHTML,/data-vk="Environment"/);
click("data-tag","Owner",$("tagChips"));
assert.equal($("tagKeysLabel").textContent,"2 tag keys selected");
assert.deepEqual(matching(),["a","b"]);
assert.match($("tagValueGroups").innerHTML,/data-vk="Owner"/,"each selected key gets its own value choices");
assert.equal(root.querySelectorAll("[data-tag]").some(node=>node.dataset.tag==="No cost"),false);
$("tagFilter").value = "absent"; $("tagFilter").oninput();
assert.match($("tagChips").innerHTML,/No tag key matches/);
assert.equal($("tagKeysLabel").textContent,"2 tag keys selected","a search must not erase selections");
assert.match($("tagValueGroups").innerHTML,/data-vk="Environment"/,"key search does not hide selected value choices");
assert.deepEqual(matching(),["a","b"]);
$("tagClear").onclick();
assert.match($("tagKeysLabel").textContent,/Select tag keys \(2\)/);
assert.equal($("tagValuesToggle").disabled,true);
assert.equal($("tagValueGroups").innerHTML,"");
$("tagUndo").onclick();
assert.equal($("tagKeysLabel").textContent,"2 tag keys selected");
assert.equal($("tagValuesToggle").disabled,false);
assert.equal(requests.length,1);
""")


def test_dependent_values_preserve_exact_values_empty_values_and_selection_reset():
    run_node(HARNESS + r"""
payload=sample();
payload.values.push("", "Prod", 'a "<&>');
payload.resources.push(
  {id:"empty",keys:["Environment"],v:[4],cost:5},
  {id:"case",keys:["Environment"],v:[5],cost:7},
  {id:"escaped",keys:["Environment"],v:[6],cost:9},
);
await tabs.renderTags(1);
assert.doesNotMatch(root.innerHTML,/id="tagValues"/,"no separate value-panel container");
click("data-tag","Environment",$("tagChips"));
assert.match($("tagValueGroups").innerHTML,/class="tag-key-values"/);
assert.doesNotMatch($("tagValueGroups").innerHTML,/class="card tagvals"/);
assert.equal(find("data-vv","",$("tagValueGroups")).getAttribute("type"),"checkbox");
click("data-vv","",$("tagValueGroups"));
assert.deepEqual(matching(),["empty"]);
assert.match($("tagSelected").innerHTML,/\(no value\)/);
assert.equal($("tagValuesLabel").textContent,"1 value selected");
click("data-vv","Prod",$("tagValueGroups"));
assert.deepEqual(matching().sort(),["case","empty"],"different values of the same key match OR");
assert.equal(find("data-vv","prod",$("tagValueGroups")).hasAttribute("checked"),false,
  "tag values remain case sensitive");
click("data-vv",'a "<&>',$("tagValueGroups"));
assert.deepEqual(matching().sort(),["case","empty","escaped"]);
assert.equal($("tagValuesLabel").textContent,"3 values selected");
assert.match($("tagValueGroups").innerHTML,/a &quot;&lt;&amp;&gt;/);
click("data-allvals","Environment",$("tagValueGroups"));
assert.deepEqual(matching().sort(),["a","b","case","empty","escaped"]);
assert.equal(document.activeElement.dataset.valueSearch,"Environment","All values keeps keyboard focus in the value dropdown");
assert.equal(find("data-tag","Environment",$("tagChips")).hasAttribute("checked"),true);
assert.equal($("tagValuesLabel").textContent,"All values");
click("data-vv","prod",$("tagValueGroups"));
let search=find("data-value-search","Environment",$("tagValueGroups"));
search.value="absent"; search.oninput();
assert.deepEqual(matching(),["a"],"search does not change the existing value filter");
click("data-tag","Environment",$("tagChips"));
assert.equal($("tagValueGroups").innerHTML,"");
assert.equal($("tagValuesToggle").disabled,true);
click("data-tag","Environment",$("tagChips"));
assert.deepEqual(matching().sort(),["a","b","case","empty","escaped"],"reselecting a key starts with all values");
assert.equal(find("data-value-search","Environment",$("tagValueGroups")).value,"");
assert.equal(requests.length,1);
""")


def test_unknown_untagged_empty_and_partial_states():
    run_node(HARNESS + r"""
payload={...sample(),keys:[],values:[],tagged:{cost:0,resources:0},untagged:{cost:185,resources:4},
  resources:sample().resources.map(r=>({...r,keys:[],v:[]})),tags_not_loaded:true,
  truncated:true,mixed:true,daily_dropped:true};
await tabs.renderTags(1);
assert.deepEqual(matching(),["a","b","c","d"],"unknown tags must not hide billed resources");
assert.match(root.innerHTML,/unknown tag status, not confirmed untagged/);
assert.match(root.innerHTML,/Unknown tag spend 185/);
assert.match(root.innerHTML,/Untagged spend 0 0 confirmed resources/);
assert.match(root.innerHTML,/partial/);
assert.match(root.innerHTML,/more than one currency/);
assert.match(root.innerHTML,/Untagged · 0/);
assert.match(root.innerHTML,/Unknown · 4/);
click("data-resource-view","unknown");
assert.deepEqual(matching(),["a","b","c","d"]);
assert.match($("tagModeHint").textContent,/Unknown: key\/value filters paused/);
assert.match($("tagTrend").innerHTML,/Too many resource-days/);
assert.match($("tagKeyCount").textContent,/0 of 0 keys/);
const tagsColumn=tables.get("Matching resources").columns.find(c=>c.label==="Tags");
assert.match(tagsColumn.cell(payload.resources[0]),/Tag status unknown/);
click("data-resource-view","untagged");
assert.match($("tagResults").innerHTML,/cannot be confirmed/);

signature="known"; payload={...payload,tags_not_loaded:false};
await tabs.renderTags(1);
assert.match(root.innerHTML,/Untagged · 4/);
assert.match(root.innerHTML,/Unknown · 0/);
click("data-resource-view","untagged");
assert.deepEqual(matching(),["a","b","c","d"]);

signature="empty"; payload={...sample(),keys:[],resources:[],total:0,tagged:{cost:0,resources:0},untagged:{cost:0,resources:0},
  uncosted:[{key:"Owner",resources:2}]};
await tabs.renderTags(1);
assert.match(root.innerHTML,/No cost data is loaded/);
assert.doesNotMatch(root.innerHTML,/uncostedTags|Tags without spend/);
assert.doesNotMatch(root.innerHTML,/None of .* carry a tag/);
""")


def test_tag_counts_partition_all_resources_and_share_filter_precision():
    run_node(r"""
import assert from "node:assert/strict";
import {tagResourceTotals,filterTagResources} from "./web/assets/dashboard-spend.js";
const data={tags_not_loaded:true,resources:Array.from({length:271},(_,i)=>({
  id:String(i),keys:i<49?["Owner"]:[],cost:0.00491
}))};
for(const incomplete of [true,false]){
  data.tags_not_loaded=incomplete;
  const totals=tagResourceTotals(data);
  assert.equal(totals.all.resources,271);
  assert.equal(totals.tagged.resources,49);
  assert.equal(totals.untagged.resources,incomplete?0:222);
  assert.equal(totals.unknown.resources,incomplete?222:0);
  assert.equal(totals.all.resources,totals.tagged.resources+totals.untagged.resources+totals.unknown.resources);
  for(const [view,bucket] of Object.entries(totals)){
    const rows=filterTagResources(data,{resourceView:view});
    assert.equal(bucket.resources,rows.length);
    assert.equal(bucket.cost,rows.reduce((n,r)=>n+r.cost,0));
  }
  assert.equal(totals.all.cost.toFixed(2),"1.33");
}
const mixed=tagResourceTotals({resources:[
  {keys:["Owner"],cost:10},{keys:[],cost:2},{cost:3},{keys:[],cost:-1},{keys:[],cost:0}
]});
assert.deepEqual(mixed,{all:{resources:5,cost:14},tagged:{resources:1,cost:10},
  untagged:{resources:3,cost:1},unknown:{resources:1,cost:3}});
assert.equal(tagResourceTotals({}).all.resources,0);
""")


def test_unknown_filter_preserves_selection_undo_and_aggregate_precision():
    run_node(HARNESS + r"""
payload={...sample(),tags_not_loaded:true};
payload.resources.forEach((r,i)=>r.cost=[100.004,50.004,25.004,10.004][i]);
await tabs.renderTags(1);
assert.match(root.innerHTML,/All · 4/);
assert.match(root.innerHTML,/Tagged · 3/);
assert.match(root.innerHTML,/Untagged · 0/);
assert.match(root.innerHTML,/Unknown · 1/);
assert.match(root.innerHTML,/Unknown tag spend 10.004/);
click("data-tag","Environment",$("tagChips"));
click("data-vv","prod",$("tagValueGroups"));
click("data-resource-view","unknown");
assert.deepEqual(matching(),["d"]);
assert.equal($("tagBudgetBtn").hidden,true);
assert.equal($("tagBudget").innerHTML,"");
assert.match($("tagModeHint").textContent,/paused/);
$("tagClear").onclick();
assert.equal(matching().length,4);
$("tagUndo").onclick();
assert.deepEqual(matching(),["d"]);
assert.equal(find("data-resource-view","unknown").getAttribute("aria-pressed"),"true");
click("data-resource-view","tagged");
assert.deepEqual(matching(),["a"]);
click("data-tagview","chargeback");
const rows=tables.get("Spend breakdown by Environment").rows;
assert.equal(rows.reduce((n,r)=>n+r.cost,0).toFixed(2),"185.02","allocation totals round only for display");
assert.equal(requests.length,1,"status changes and allocation reuse the loaded resources");
""")


def test_budget_navigation_replaces_filters_without_writes():
    run_node(HARNESS + r"""
payload=sample(); await tabs.renderTags(1);
click("data-tag","Owner",$("tagChips"));
click("data-vv","Ben",$("tagValueGroups"));
click("data-mode","any");
click("data-resource-view","untagged");
click("data-tagview","chargeback");
tabs.showTagsFor([["Environment",["prod"]]],"Production");
await tabs.renderTags(1);
assert.deepEqual(matching(),["a"],"budget navigation replaces old filters and opens explorer");
assert.equal(find("data-mode","all").getAttribute("aria-checked"),"true");
assert.equal(find("data-resource-view","all").getAttribute("aria-pressed"),"true");
assert.equal($("tagBudget").innerHTML,"","navigation never opens/submits a budget form");
assert.doesNotMatch($("tagSelected").innerHTML,/data-drop="Owner"/);
tabs.showTagsFor([["Environment",["prod"]],["Missing",["value"]]],"Missing tag budget");
await tabs.renderTags(1);
assert.match($("tagSelected").innerHTML,/Missing/);
assert.match($("tagValueGroups").innerHTML,/No values recorded/);
assert.match($("tagResults").innerHTML,/No resource carries all/,"a missing budget key must not silently widen");
""")


def test_budget_form_remains_user_initiated_and_keeps_edits_on_filtering():
    run_node(HARNESS + r"""
payload=sample(); await tabs.renderTags(1);
click("data-tag","Environment",$("tagChips"));
assert.equal($("tagBudget").innerHTML,"");
$("tagBudgetBtn").onclick();
assert.match($("tagBudget").innerHTML,/Creates a real Azure budget/);
assert.equal(typeof $("bCreate").onclick,"function","only the explicit create action can submit");
$("bName").value="my-reviewed-budget";
$("bAmount").value="42";
click("data-vv","prod",$("tagValueGroups"));
assert.equal($("bName").value,"my-reviewed-budget");
assert.equal($("bAmount").value,"42");
assert.equal(requests.length,1);
""")


def test_budget_identity_native_currency_and_explicit_unknown_target():
    run_node(HARNESS + r"""
ctx.allSubscriptions=()=>[{id:"sub-a",name:"Shared"},{id:"sub-b",name:"Shared"},{id:"sub-c",name:"Mixed"}];
const budgetTabs=createTabs(ctx);
payload=sample();
payload.currency="EUR";
payload.native_currencies={"sub-a":["EUR"],"sub-b":["USD"],"sub-c":["EUR","USD"]};
payload.resources=payload.resources.map(r=>({...r,subscription:"Shared",subscription_id:"sub-b"}));
await budgetTabs.renderTags(1);
click("data-tag","Environment",$("tagChips")); $("tagBudgetBtn").onclick();
assert.equal($("bSub").value,"sub-b","duplicate names cannot redirect a budget to subscription A");
assert.match($("bAmountLabel").textContent,/USD/);
assert.match($("bSuggest").textContent,/displayed in EUR/);
assert.equal($("bAmount").value,"","display-converted spend is not suggested in native units");
assert.equal($("bCreate").disabled,false);
$("bAmount").value="110";
click("data-vv","prod",$("tagValueGroups"));
assert.equal($("bAmount").value,"110");
assert.match($("bAmountLabel").textContent,/USD/,"redraw keeps amount label aligned with retained target");
$("bSub").value="sub-a"; $("bSub").onchange();
assert.equal($("bAmount").value,"","changing currency cannot reuse the previous amount");
assert.match($("bAmountLabel").textContent,/EUR/);
$("bAmount").value="35";
click("data-vv","prod",$("tagValueGroups"));
assert.equal($("bSub").value,"sub-a","manual target survives filtering even when another subscription ranks first");
assert.equal($("bAmount").value,"35");
assert.match($("bAmountLabel").textContent,/EUR/);
$("bAmount").value="40"; $("bGrain").value="Annually"; $("bGrain").onchange();
assert.equal($("bAmount").value,"","changing period requires reviewing its amount");
$("bSub").value="sub-c"; $("bSub").onchange();
assert.equal($("bCreate").disabled,true);
assert.match($("bSuggest").textContent,/unknown or mixed/);
await $("bCreate").onclick(); // Validation must block even programmatic invocation; fetch throws.
signature="arm-fallback";
payload.resources=payload.resources.map(r=>({...r,subscription_id:"",id:"/SUBSCRIPTIONS/SUB-B/resourceGroups/rg/providers/test/"+r.id}));
await budgetTabs.renderTags(1);
click("data-tag","Environment",$("tagChips")); $("tagBudgetBtn").onclick();
assert.equal($("bSub").value,"sub-b");
signature="unknown";
payload.resources=payload.resources.map(r=>({...r,subscription_id:"",id:"non-arm-name"}));
await budgetTabs.renderTags(1);
click("data-tag","Environment",$("tagChips")); $("tagBudgetBtn").onclick();
assert.equal($("bSub").value,"","display name alone must not select any subscription");
assert.equal($("bCreate").disabled,true);
$("bSub").value="sub-missing"; $("bSub").onchange();
assert.equal($("bCreate").disabled,true);
""")


def test_spend_breakdown_wires_tables_on_every_render_path():
    run_node(HARNESS + r"""
payload=sample();
payload.values=Array.from({length:35},(_,i)=>"value-"+i);
payload.resources=payload.values.map((_,i)=>({id:String(i),name:String(i),keys:["Environment","Owner"],v:[i,34-i],cost:i+1}));
await tabs.renderTags(1);
wired.length=0;
click("data-tagview","chargeback");
assert.equal(tables.get("Spend breakdown by Environment").rows.length,35);
assert.equal(wired.at(-1),root,"view toggle wires sorting/search/paging/details on the new table");
wired.length=0;
$("chargebackKey").value="Owner"; $("chargebackKey").onchange();
assert.equal(tables.get("Spend breakdown by Owner").rows.length,35);
assert.equal(wired.at(-1),root,"changing allocation key wires the replacement table");
wired.length=0;
await tabs.renderTags(1);
assert.equal(wired.at(-1),root,"full render also wires the breakdown table");
""")
