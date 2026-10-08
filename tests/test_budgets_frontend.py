"""Offline budget presentation and interaction regression tests."""
import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
node = runpy.run_path(str(ROOT / "tests" / "test_dashboard_frontend.py"))["node"]


def test_budget_cards_filters_lazy_charts_refresh_and_errors():
    script = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import { createTabs, budgetStatus, filterBudgets, budgetReadIssue } from "./web/assets/dashboard-monitor.js";
import { drawBudgetChart as drawBudgetTrend } from "./web/assets/budget-chart.js";
const source = fs.readFileSync("./web/assets/dashboard.js", "utf8");
const cardSource = source.slice(source.indexOf("function budgetCard("), source.indexOf("\nfunction cardHead("));
const esc = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const moneyCalls = [];
const money = (value, currency) => { moneyCalls.push([value, currency]); return `${currency} ${value}`; };
const budgetCard = new Function("esc", "money", "budgetPortalUrl", `${cardSource}; return budgetCard;`)(
  esc, money, sub => `https://portal.azure.com/${sub}`);
const trend = {labels:["2026-09-01","2026-09-02"], cumulative:[10,20], total:20,
  filtered_exactly:true, from:"2026-09-01", to:"2026-09-06"};
const base = {name:"Application A", subscription_id:"one", subscription:"Production", amount:100,
  currency:"USD", current_spend:20, remaining:80, percent_used:20, status:"ok", trend,
  alerts:1, alerting:[80], filter:"App = A", tag_filter:[["App",["A"]]], time_grain:"Monthly",
  start:"2026-01-01", end:"2028-12-31"};
const rows = [base, {...base, name:"Application B", subscription_id:"two", currency:"EUR",
  current_spend:110, remaining:-10, percent_used:110, status:"over"},
  {...base, name:"Unknown", current_spend:null, remaining:null, percent_used:null, status:"unknown"},
  {...base, name:"Dimension", filter_exact:false, tag_filter:[["App",["A"]]],
    trend:{...trend, filtered_exactly:false}}];
assert.equal(budgetStatus({...base,current_spend:null}), "unknown");
assert.equal(budgetStatus({...base,current_spend:0,percent_used:0}), "ok");
assert.equal(budgetStatus({...base,percent_used:NaN}), "unknown");
assert.equal(filterBudgets(rows,{status:"unknown"}).length, 1);
assert.equal(filterBudgets(rows,{status:"attention"}).length, 1);
assert.equal(filterBudgets(rows,{q:"  PRODUCTION  "}).length, 4);
assert.equal(filterBudgets(rows,{q:"two"}).length, 1);
assert.equal(filterBudgets(rows,{status:"near"}).length, 0);
assert.equal(budgetReadIssue({budgets:[]}), "");
for (const data of [null, {}, {budgets:[],error:"Read denied"}, {budgets:[],partial:true},
  {budgets:[],errors:["one"]}, {budgets:[],failed_subscriptions:1},
  {budgets:[],unavailable_subscriptions:[{id:"one"}]}]) assert.ok(budgetReadIssue(data));
let markup = budgetCard(rows[2], 2, {compact:true});
assert.match(markup, /^<details /);
assert.doesNotMatch(markup.split(">")[0], / open/);
assert.match(markup, /Spent <strong>Not reported/);
assert.match(markup, /Remaining not reported/);
assert.ok(markup.indexOf("Period:") < markup.indexOf("</summary>"));
assert.ok(markup.indexOf("notification rule") > markup.indexOf("</summary>"));
assert.ok(markup.indexOf("budget-chart") > markup.indexOf("</summary>"));
assert.match(budgetCard(base,0), /^<article /, "creation confirmation stays compatible");
assert.match(budgetCard({...base,current_spend:0},0), /Spent <strong>USD 0/);
assert.match(budgetCard({...base,currency:null},0), /currency unknown/);
assert.doesNotMatch(budgetCard(rows[3],0), /data-tagsfor/);
assert.doesNotMatch(budgetCard({...base,filtered_exactly:false},0), /data-tagsfor/);
assert.match(budgetCard({...base,trend:null,trend_note:null},0,{lazyTrend:true}),/class="budget-chart"/);
const unsupported = budgetCard({...base,filter_exact:false,tag_filter:[],trend:null,
  trend_note:"Unsupported OR filter; review Azure scope."},0,{compact:true});
assert.doesNotMatch(unsupported,/data-tagsfor|class="budget-chart"/);
assert.match(unsupported,/Unsupported OR filter; review Azure scope/);
assert.ok(unsupported.indexOf("Unsupported OR filter") > unsupported.indexOf("</summary>"));
assert.match(budgetCard({...base,name:'<img src=x>'},0), /&lt;img src=x&gt;/);
assert.match(budgetCard(base,0), /https:\/\/portal.azure.com\/one/);

// A small DOM double executes the real render and event handlers without any network or dependencies.
let elements = {}, listDetails = [], buttons = [], jumps = [], tagButtons = [], charts = [];
const element = (props={}) => Object.defineProperties({dataset:{},classList:{toggle(){},add(){},remove(){}},
  setAttribute(){}, focus(){this.focused=true;}, scrollIntoView(){}, addEventListener(){},
  isConnected:true}, Object.getOwnPropertyDescriptors(props));
const list = element({
  _html:"",
  set innerHTML(html) {
    this._html=html;
    for (const old of listDetails) old.isConnected=false;
    listDetails = [...html.matchAll(/<details\b([^>]*)>([\s\S]*?)<\/details>/g)].map(([,attrs,body]) => {
      const idx = attrs.match(/data-budget-index="(\d+)"/)[1];
      const chart = body.includes('class="budget-chart"') ? element({innerHTML:""}) : null;
      const detail = element({dataset:{budgetIndex:idx},open:/\sopen\b/.test(attrs),
        querySelector:selector=>selector===".budget-chart"?chart:null, chart});
      elements[`budget-row-${idx}`]=detail;
      return detail;
    });
    tagButtons = [...html.matchAll(/data-tagsfor="(\d+)"/g)].map(m=>element({dataset:{tagsfor:m[1]}}));
  },
  get innerHTML(){return this._html;},
  querySelectorAll(selector){
    return selector==="[data-budget-index]"?listDetails:selector==="[data-tagsfor]"?tagButtons:[];
  }
});
const root=element({querySelectorAll(selector){
  return selector==="[data-bstatus]"?buttons:selector==="[data-jump-budget]"?jumps:[];
}});
let rootMarkup="", gets=[], tagCalls=[], lazyTrendDraws=0, payload={budgets:rows}, token=1;
const ctx = {
  $:id=>elements[id], get loadToken(){return token;}, budgetCard, esc, money,
  kpiRow:items=>items.map(x=>`${x.label}: ${x.value} ${x.sub}`).join("\n"),
  scopeIds:()=>["one","two"], readStore:()=>"", wireBudgetLinks(){}, selectTab(){},
  showTagsFor:(...args)=>tagCalls.push(args), drawChart:(host,config)=>charts.push(config),
  // The chart renderer moved to its own leaf module when Hourly Billing started drawing budgets
  // too. Wired to the real one rather than a stub, so this still checks the chart it produces.
  drawBudgetChart:(host,b)=>drawBudgetTrend(host,b,{drawChart:(h,c)=>{
    charts.push(c);
    if(b.name==="Application A Lazy") lazyTrendDraws++;
  },money}),
  get:async url=>{
    gets.push(url);
    if(url.startsWith("/api/budgets/trend")) return {trend,filter_exact:true,tag_filter:[["App",["A"]]]};
    return payload;
  },
  setContent(host, html){
    rootMarkup=html; elements={tabBody:root,budgetList:list};
    for(const id of ["budgetRefresh","budgetFind","budgetReset","budgetMatchCount","budgetPortfolio"]) {
      if(html.includes(`id="${id}"`)) elements[id]=element({value:"",textContent:"",open:false});
    }
    buttons=[...html.matchAll(/data-bstatus="([^"]+)"/g)].map(m=>element({dataset:{bstatus:m[1]}}));
    jumps=[...html.matchAll(/data-jump-budget="(\d+)"/g)].map(m=>element({dataset:{jumpBudget:m[1]}}));
  }
};
elements.tabBody=root;
const tabs=createTabs(ctx);
await tabs.renderBudgets(1);
assert.equal(gets.length,1);
assert.match(gets[0], /scope=one%2Ctwo/);
assert.doesNotMatch(gets[0],/refresh=/,"normal load need not bypass Azure cache");
// The tab dispatcher calls every renderer as (token, tabId). Live Scale sent refresh=true on
// every Budgets visit because the id "budgets" was taken as the refresh flag.
await tabs.renderBudgets(1, "budgets");
assert.doesNotMatch(gets.at(-1),/refresh=/,"tab dispatch is a plain load, not a forced re-read");
gets.length = 1;
assert.equal(charts.length,0,"collapsed cards never draw charts");
assert.equal(listDetails.length,4);
assert.match(rootMarkup,/Overlapping budgets and different currencies are not summed/);
assert.doesNotMatch(rootMarkup,/Budgeted|spent so far/);
assert.doesNotMatch(rootMarkup, /data-bstatus="[^"]+"[^>]*disabled/);
assert.match(rootMarkup, /id="budgetPortfolio"/);
assert.equal(elements.budgetMatchCount.textContent,"4 of 4 returned budgets match");
listDetails[0].open=true; listDetails[0].ontoggle();
assert.equal(charts.length,1);
listDetails[0].ontoggle();
assert.equal(charts.length,1,"same mounted chart is not redrawn");
listDetails[1].open=true; listDetails[1].ontoggle();
assert.equal(charts.length,2);
assert.equal(listDetails[0].open,true,"budgets expand independently");
listDetails[1].open=false; listDetails[1].ontoggle();
elements.budgetFind.value="Application A"; elements.budgetFind.oninput();
assert.equal(gets.length,1,"search does not refetch");
assert.equal(listDetails.length,1);
assert.equal(listDetails[0].open,true,"expanded state survives a list redraw");
assert.equal(elements.budgetMatchCount.textContent,"1 of 4 returned budgets match");
tagButtons[0].onclick();
assert.deepEqual(tagCalls[0],[base.tag_filter,base.name]);
elements.budgetReset.onclick();
assert.equal(listDetails[0].open,true);
assert.equal(listDetails[1].open,false);
assert.equal(elements.budgetFind.focused,true);
buttons.find(b=>b.dataset.bstatus==="unknown").onclick();
assert.equal(listDetails.length,1);
assert.match(list.innerHTML,/Unknown/);
buttons.find(b=>b.dataset.bstatus==="near").onclick();
assert.equal(listDetails.length,0);
assert.match(list.innerHTML,/Clear filters/);
payload={budgets:[...rows].reverse(),partial:true,subscriptions_requested:3,subscriptions_read:2,
  unavailable_subscriptions:[{id:"three",name:"Unavailable subscription"}],
  error:"Could not read budgets for 1 of 3 selected subscriptions."};
await elements.budgetRefresh.onclick();
assert.match(gets.at(-1), /[?&]refresh=true$/,"explicit refresh bypasses Azure cache");
assert.match(rootMarkup,/Incomplete budget data/);
assert.match(rootMarkup,/Could not read budgets for 1 of 3/);
assert.equal(listDetails.length,0,"refresh preserves status even with no matches");
elements.budgetReset.onclick();
assert.equal(listDetails[3].open,true,"identity, not row position, retains expansion");
assert.equal(listDetails[2].open,false);
const before=charts.length;
listDetails[0].open=true; listDetails[0].ontoggle();
assert.equal(charts.length,before,"inexact scope never draws misleading comparison");
assert.match(listDetails[0].chart.innerHTML,/cannot reproduce/);
elements.budgetFind.value="Application A"; elements.budgetFind.oninput();
await elements.budgetRefresh.onclick();
assert.match(rootMarkup,/value="Application A"/,"search text survives refresh");
assert.equal(listDetails.length,1);
payload={budgets:[],error:"Access denied"};
await elements.budgetRefresh.onclick();
assert.match(rootMarkup,/Budget data incomplete/);
assert.doesNotMatch(rootMarkup,/<h3>No budgets/);
assert.match(rootMarkup,/Access denied/);
payload={budgets:[]};
await elements.budgetRefresh.onclick();
assert.match(rootMarkup,/<h3>No budgets in scope/);
assert.match(gets.at(-1), /[?&]refresh=true$/,"retry also bypasses Azure cache");
const lazyBudget={...base,name:"Application A Lazy",status:"near",percent_used:90,
  trend:null,trend_note:null};
payload={budgets:[lazyBudget]};
await tabs.renderBudgets(1);
assert.equal(gets.filter(url=>url.startsWith("/api/budgets/trend")).length,0,
  "collapsed cards do not request their chart data");
listDetails[0].open=true;
await listDetails[0].ontoggle();
assert.equal(gets.filter(url=>url.startsWith("/api/budgets/trend")).length,1,
  "opening a card fetches its trend");
assert.match(gets.at(-1),/subscription_id=one/);
assert.match(gets.at(-1),/name=Application\+A\+Lazy/);
assert.equal(lazyTrendDraws,1);
payload={budgets:[],demo_mode:true,note:"Budgets are not part of the PostgreSQL demo data."};
await tabs.renderBudgets(1);
assert.match(rootMarkup,/No budgets in demo mode/);
assert.match(rootMarkup,/PostgreSQL demo data/);
assert.doesNotMatch(rootMarkup,/id="budgetRefresh"|Create a budget/);
const previous=rootMarkup;
token=2;
await tabs.renderBudgets(1);
assert.equal(rootMarkup,previous,"stale responses cannot repaint the next tab");

const errors=createTabs({...ctx,loadToken:2,get:()=>Promise.reject(new Error("Offline"))});
await errors.renderBudgets(2);
assert.match(rootMarkup,/Offline/);
assert.doesNotMatch(rootMarkup,/<h3>No budgets/);
let pending=[];
const races=createTabs({...ctx,loadToken:2,get:()=>new Promise(resolve=>pending.push(resolve))});
const older=races.renderBudgets(2), newer=races.renderBudgets(2);
pending[1]({budgets:[],error:"New response"}); await newer;
pending[0]({budgets:[]}); await older;
assert.match(rootMarkup,/New response/,"older overlapping refresh cannot replace a newer result");
console.log("budget cards, filters, lazy charts, refresh and failures passed");
"""
    result = subprocess.run(
        [node(), "--input-type=module", "-"], input=script, cwd=ROOT,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
