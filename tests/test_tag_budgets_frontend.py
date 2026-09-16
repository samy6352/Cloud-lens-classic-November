"""Budget context follows the current tag selection without inventing historical spend."""
from tests.test_tags_frontend import HARNESS, run_node


def test_budget_matching_handles_key_only_values_and_boolean_selection():
    run_node(r"""
import assert from "node:assert/strict";
import {matchingTagBudgets} from "./web/assets/budget-context.js";
const budget=(name,tags,extra={})=>({name,tag_filter:tags,filter_exact:true,percent_used:10,...extra});
const rows=[
  budget("prod",[["Env",["prod"]]]),
  budget("dev",[["env",["dev"]]]),
  budget("both",[["env",["prod","dev"]]],{percent_used:120}),
  budget("narrow",[["env",["prod"]],["Owner",["Ana"]]]),
  budget("other-owner",[["env",["prod"]],["Owner",["Ben"]]]),
  budget("just-owner",[["Owner",["Ana"]]]),
  budget("empty-value",[["Env",[""]]]),
  budget("named-prod",[]),
  budget("inexact",[["Env",["prod"]]],{filter_exact:false}),
  budget("malformed",[["Env","prod"]]),
];
const names=(selection,mode)=>matchingTagBudgets(rows,selection,mode).map(match=>match.budget.name);
assert.deepEqual(names([{key:"env",values:[]}]),["both","prod","dev","narrow","other-owner","empty-value"]);
assert.deepEqual(names([{key:"ENV",values:["prod"]}]),["both","prod","narrow","other-owner"]);
assert.deepEqual(names([{key:"Env",values:["Prod"]}]),[]);
assert.deepEqual(names([{key:"env",values:[""]}]),["empty-value"]);
assert.deepEqual(names([{key:"env",values:["prod"]},{key:"Owner",values:["Ana"]}],"all"),
  ["both","prod","narrow","just-owner"]);
assert.deepEqual(names([{key:"env",values:["prod"]},{key:"Owner",values:["Ana"]}],"any"),
  ["both","prod","narrow","other-owner","just-owner"]);
assert.deepEqual(names([]),[]);
assert.deepEqual(matchingTagBudgets(null,[{key:"env",values:["prod"]}]),[]);
assert.equal(matchingTagBudgets(rows,[{key:"env",values:["prod"]}])[0].partial,true);
assert.deepEqual(matchingTagBudgets(rows,[{key:"env",values:["prod"]}]).find(m=>m.budget.name==="narrow").extra,
  [{key:"Owner",values:["Ana"]}]);
""")


def test_chart_uses_history_or_an_explicit_azure_snapshot_without_fabricating_dates():
    run_node(r"""
import assert from "node:assert/strict";
import {drawBudgetChart} from "./web/assets/budget-chart.js";
const charts=[],host={innerHTML:""},ctx={drawChart:(_,chart)=>charts.push(chart),money:(v,c)=>`${c} ${v}`,snapshot:true};
const budget={currency:"USD",amount:10,current_spend:21.78,time_grain:"Monthly",filter_exact:true};
drawBudgetChart(host,budget,ctx);
assert.equal(charts[0].type,"hbar");
assert.deepEqual(charts[0].labels,["Spent","Budget"]);
assert.deepEqual(charts[0].datasets[0].data,[21.78,10]);
assert.match(charts[0].note,/snapshot, not a trend/);
drawBudgetChart(host,{...budget,current_spend:0},ctx);
assert.equal(charts[1].datasets[0].data[0],0);
drawBudgetChart(host,{...budget,current_spend:null},ctx);
assert.match(host.innerHTML,/needs a reported spend/);
assert.equal(charts.length,2);
drawBudgetChart(host,{...budget,filter_exact:false},ctx);
assert.match(host.innerHTML,/cannot reproduce/);
const trend={labels:["2026-09-01","2026-09-02"],cumulative:[2,5],total:5,filtered_exactly:true};
drawBudgetChart(host,{...budget,trend},ctx);
assert.equal(charts[2].type,"area");
assert.deepEqual(charts[2].datasets.map(d=>d.data),[[2,5],[10,10]]);
assert.deepEqual(charts[2].labels,trend.labels);
drawBudgetChart(host,budget,{...ctx,snapshot:false});
assert.equal(charts.length,3,"existing callers do not opt into the snapshot automatically");
assert.equal(charts[0].height,140);
assert.equal(charts[2].height,150);
drawBudgetChart(host,budget,{...ctx,compact:true});
assert.equal(charts[3].height,100);
assert.deepEqual(charts[3].datasets,charts[0].datasets,"compaction changes size, not financial data");
drawBudgetChart(host,{...budget,trend},{...ctx,compact:true});
assert.equal(charts[4].height,120);
assert.deepEqual(charts[4].datasets,charts[2].datasets);
""")


BUDGET_HARNESS = HARNESS + r"""
const settled=()=>new Promise(resolve=>setTimeout(resolve,0));
const budgetDraws=[];
const budget=(name,values,extra={})=>({
  name,subscription_id:"sub-a",currency:"USD",amount:100,current_spend:20,percent_used:20,
  filter_exact:true,tag_filter:[["Environment",values]],trend:{labels:["2026-09-01","2026-09-02"],cumulative:[10,20],total:20},
  ...extra,
});
ctx.qs=()=>"?days=30&period_month=2026-08&scope=sub-a&currency=EUR&service=Compute";
ctx.budgetCard=(b,index,options)=>{
  assert.equal(options.snapshotChart,true);
  assert.equal(options.chartAside,true);
  return `<article>${escape(b.name)}<div class="budget-chart" data-chart="${index}"></div>
    <button data-tagsfor="${index}">See what is in it</button></article>`;
};
ctx.whenNear=(host,prepare)=>prepare()();
ctx.drawBudgetChart=(host,b,options)=>budgetDraws.push({name:b.name,snapshot:options.snapshot,compact:options.compact});
const tagJumps=[];
ctx.showTagsFor=(...args)=>tagJumps.push(args);
const budgetTabs=createTabs(ctx);
"""


def test_matching_cards_draw_automatically_and_reuse_the_scope_read():
    run_node(BUDGET_HARNESS + r"""
payload=sample(); budgetPayload={budgets:[budget("Prod budget",["prod"]),budget("Dev budget",["dev"])]};
await budgetTabs.renderTags(1); await settled();
assert.equal(budgetRequests.length,0);
assert.equal($("tagBudgets").hidden,true);
click("data-tag","Environment",$("tagChips")); await settled();
assert.match($("tagBudgets").innerHTML,/Prod budget/); assert.match($("tagBudgets").innerHTML,/Dev budget/);
assert.deepEqual(budgetDraws.map(d=>d.name),["Prod budget","Dev budget"]);
assert.ok(budgetDraws.every(d=>d.snapshot));
assert.ok(budgetDraws.every(d=>d.compact));
assert.equal(budgetRequests.length,1);
assert.match(budgetRequests[0],/scope=sub-a/); assert.match(budgetRequests[0],/currency=EUR/);
assert.doesNotMatch(budgetRequests[0],/days=|period_month=|service=/);
click("data-vv","prod",$("tagValueGroups")); await settled();
assert.match($("tagBudgets").innerHTML,/Prod budget/); assert.doesNotMatch($("tagBudgets").innerHTML,/Dev budget/);
assert.equal(budgetRequests.length,1);
click("data-tagsfor","0",$("tagBudgets"));
assert.deepEqual(tagJumps,[[budgetPayload.budgets[0].tag_filter,"Prod budget"]]);
$("tagFilter").value="nothing"; $("tagFilter").oninput(); await settled();
assert.equal(budgetRequests.length,1,"discovery search does not refetch budgets");
click("data-resource-view","untagged"); await settled();
assert.equal($("tagBudgets").hidden,true);
click("data-resource-view","all"); await settled();
assert.match($("tagBudgets").innerHTML,/Prod budget/);
$("tagClear").onclick(); await settled(); assert.equal($("tagBudgets").hidden,true);
$("tagUndo").onclick(); await settled(); assert.match($("tagBudgets").innerHTML,/Prod budget/);
budgetPayload={budgets:[]}; await $("tagBudgetRefresh").onclick(); await settled();
assert.equal(budgetRequests.length,2); assert.match(budgetRequests[1],/refresh=true/);
assert.match($("tagBudgets").innerHTML,/No budgets match/);
""")


def test_late_response_clear_partial_errors_and_cached_resume():
    run_node(BUDGET_HARNESS + r"""
payload=sample();
let resolve;
budgetPayload=new Promise(done=>resolve=done);
await budgetTabs.renderTags(1);
click("data-tag","Environment",$("tagChips"));
click("data-vv","prod",$("tagValueGroups"));
$("tagClear").onclick();
resolve({budgets:[budget("Stale budget",["prod"])]}); await settled();
assert.equal($("tagBudgets").hidden,true,"a late read cannot revive a cleared selection");
assert.equal($("tagBudgets").innerHTML,"");
signature="new-context"; budgetPayload={budgets:[budget("Partial budget",["prod"])],partial:true};
await budgetTabs.renderTags(1); click("data-tag","Environment",$("tagChips")); await settled();
assert.match($("tagBudgets").innerHTML,/Results may be incomplete/);
assert.match($("tagBudgets").innerHTML,/Partial budget/);
budgetPayload={budgets:{bad:"shape"}}; await $("tagBudgetRefresh").onclick(); await settled();
assert.match($("tagBudgets").innerHTML,/readable list/);
assert.match($("tagBudgets").innerHTML,/could not be confirmed/);
budgetPayload={budgets:[],error:"Access denied"}; await $("tagBudgetRefresh").onclick(); await settled();
assert.match($("tagBudgets").innerHTML,/Access denied/);
signature="slow-context"; budgetPayload=new Promise(done=>resolve=done);
await budgetTabs.renderTags(1); click("data-tag","Environment",$("tagChips"));
const host=$("tagBudgets"); host.isConnected=false; ctx.loadToken=2;
resolve({budgets:[budget("Resumed budget",["prod"])]}); await settled();
assert.doesNotMatch(host.innerHTML,/Resumed budget/,"detached view is not painted by an obsolete response");
const reads=budgetRequests.length;
host.isConnected=true; ctx.loadToken=1; budgetTabs.resume(1,"tags"); await settled();
assert.match(host.innerHTML,/Resumed budget/);
assert.equal(budgetRequests.length,reads,"return uses the completed cached read");
""")


def test_unknown_resources_pause_budget_context_and_keep_tag_selection():
    run_node(BUDGET_HARNESS + r"""
payload={...sample(),tags_not_loaded:true};
budgetPayload={budgets:[budget("Prod budget",["prod"])]};
await budgetTabs.renderTags(1);
click("data-tag","Environment",$("tagChips"));
click("data-vv","prod",$("tagValueGroups")); await settled();
assert.match($("tagBudgets").innerHTML,/Prod budget/);
click("data-resource-view","unknown"); await settled();
assert.equal($("tagBudgets").hidden,true);
assert.equal($("tagBudgetBtn").hidden,true);
assert.deepEqual(matching(),["d"]);
click("data-resource-view","all"); await settled();
assert.match($("tagBudgets").innerHTML,/Prod budget/);
assert.deepEqual(matching(),["a"]);
assert.equal(budgetRequests.length,1);
""")
