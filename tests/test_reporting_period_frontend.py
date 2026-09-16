"""Reporting-period controls and request propagation without Azure or a database."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
run_node = runpy.run_path(str(ROOT / "tests" / "test_tags_frontend.py"))["run_node"]


def test_month_validation_query_and_utc_date_labels():
    run_node(r"""
import assert from "node:assert/strict";
import {validMonth, currentMonth, periodParams, periodLabel, periodName, rollingRangeLabel} from "./web/assets/reporting-period.js";
const now=new Date("2026-09-06T00:05:00Z");
assert.equal(currentMonth(now),"2026-09");
for (const month of ["2026-09","2024-02","2020-01"]) assert.equal(validMonth(month,now),true);
for (const month of ["2026-10","2026-13","2026-00","2026-9","2026-09junk","0000-01","",null])
  assert.equal(validMonth(month,now),false);
assert.equal(periodParams(30,"2024-02").get("period_month"),"2024-02");
assert.equal(periodParams(30).has("period_month"),false);
process.env.TZ="America/Los_Angeles";
const rolling=periodLabel({from:"2026-08-08",to:"2026-09-06"},{days:30,month:""});
assert.match(rolling,/Last 30 days/);
assert.match(rolling,/Aug/); assert.match(rolling,/Sep/);
assert.match(rolling,/8/); assert.match(rolling,/6/);
const month=periodLabel({month:"2024-02",from:"2024-02-01",to:"2024-02-29"},{days:30,month:"2024-02"});
assert.match(month,/February 2024/); assert.match(month,/29/); assert.doesNotMatch(month,/Last 30/);
assert.match(periodLabel({from:"2025-12-20",to:"2026-01-18"},{days:30}),/2025.*2026/);
assert.match(periodLabel({from:"2026-07-01",to:"2026-07-31",empty:true},{days:30,month:"2026-07"}),/No cost data loaded/);
assert.equal(periodName(30,"2026-08"),"August 2026");
assert.match(rollingRangeLabel({from:"2026-07-10",to:"2026-09-07"},60),/Jul.*Sep.*2026/);
assert.match(rollingRangeLabel({from:"2025-12-20",to:"2026-01-18"},30),/2025.*2026/);
assert.equal(rollingRangeLabel(null,60),"Last 60 days");
""")


def test_month_rolling_switches_and_stale_metadata():
    run_node(r"""
import assert from "node:assert/strict";
import {wireReportingPeriod} from "./web/assets/reporting-period.js";
const buttons=[7,30,60,90].map(days=>({
  dataset:{days:String(days)},attrs:{},
  classList:{toggle(name,on){this.on=on}},
  setAttribute(name,value){this.attrs[name]=value},
}));
const input={value:"",attrs:{},setAttribute(name,value){this.attrs[name]=value},
  setCustomValidity(value){this.error=value},reportValidity(){this.reported=true}};
const caption={textContent:"",removeAttribute(name){delete this[name]}};
const range={textContent:"",attrs:{},setAttribute(name,value){this.attrs[name]=value}};
let state={days:30,month:"2024-02"};
const pending=[];
const control=wireReportingPeriod({
  root:{querySelectorAll:()=>buttons},input,caption,range,
  getState:()=>state,onChange:value=>{state=value},
  now:()=>new Date("2026-09-06T12:00:00Z"),
  getMetadata:signal=>new Promise((resolve,reject)=>pending.push({resolve,reject,signal})),
});
assert.equal(input.value,"2024-02","saved month is restored");
assert.equal(input.max,"2026-09");
assert.equal(range.hidden,true,"a calendar month hides the rolling range control");
assert(buttons.every(b=>b.attrs["aria-pressed"]==="false"));
const first=control.refresh();
input.value="2026-08";input.onchange();
const second=control.refresh();
assert.equal(pending[0].signal.aborted,true);
pending[1].resolve({month:"2026-08",from:"2026-08-01",to:"2026-08-31"});
await second;
assert.match(caption.textContent,/August 2026/);
pending[0].resolve({month:"2024-02",from:"2024-02-01",to:"2024-02-29"});
await first;
assert.match(caption.textContent,/August 2026/,"late old-month metadata cannot repaint");
buttons[1].onclick();
assert.deepEqual(state,{days:30,month:""});
assert.equal(input.value,"2026-09","rolling mode still shows a usable current-month default");
assert.equal(input.attrs["data-period-mode"],"rolling");
assert.equal(range.hidden,false);
assert.equal(range.textContent,"Last 30 days","do not keep the previous calendar dates");
assert.match(input.title,/last 30 days/);
assert.equal(buttons[1].attrs["aria-pressed"],"true");
input.value="2027-01";input.onchange();
assert.equal(state.month,"");assert(input.error);assert(input.reported);
input.value="2026-07";input.oninput();input.onchange();
assert.equal(input.error,"");assert.equal(state.month,"2026-07");
assert.equal(input.attrs["data-period-mode"],"month");
assert.match(input.title,/July 2026/);
const failed=control.refresh();
pending[2].reject(new Error("Source unavailable"));
await failed;
assert.equal(caption.textContent,"");
assert.equal(caption.hidden,true);
const recovered=control.refresh();
assert.equal(caption.hidden,false);
pending[3].resolve({month:"2026-07",from:"2026-07-01",to:"2026-07-31"});
await recovered;
assert.match(caption.textContent,/July 2026/);
input.value="";input.onchange();
assert.equal(state.month,"","clearing the calendar returns to rolling mode");
assert.equal(input.value,"2026-09","clearing never leaves a dashed calendar");
buttons[2].onclick();
const sixty=control.refresh();
assert.equal(range.textContent,"Last 60 days");
pending[4].resolve({from:"2026-07-10",to:"2026-09-07"});
await sixty;
assert.match(range.textContent,/Jul.*Sep.*2026/);
assert.match(range.attrs["aria-label"],/Last 60 days/);
const old=control.refresh();
buttons[3].onclick();
pending[5].resolve({from:"2026-07-10",to:"2026-09-07"});
await old;
assert.equal(range.textContent,"Last 90 days","late metadata cannot overwrite the newly selected preset");
const missing=control.refresh();
pending[6].reject(new Error("No dates"));
await missing;
assert.equal(range.textContent,"Last 90 days","date errors do not pretend the current month is selected");
""")


def test_all_reporting_requests_carry_calendar_selection():
    shell = (ROOT / "web/assets/dashboard.js").read_text(encoding="utf-8")
    filters = (ROOT / "web/assets/filters.js").read_text(encoding="utf-8")
    analysis = (ROOT / "web/assets/analysis.js").read_text(encoding="utf-8")
    reports = (ROOT / "web/assets/dashboard-reports.js").read_text(encoding="utf-8")
    assert "periodParams(days, periodMonth)" in shell
    assert "${days}|${periodMonth}|" in shell
    assert "periodMonth: month" in shell
    assert 'writeStore("costPeriodMonth", periodMonth)' in shell
    assert "month: periodMonth" in shell
    assert "periodParams(queuedDays || 30, queuedMonth)" in filters
    assert "periodParams(days || 30, month)" in analysis
    assert "new URLSearchParams(qs().slice(1))" in reports
    export = shell[shell.index("function refreshExportLinks()"):shell.index("function budgetCard(")]
    assert "new URLSearchParams(qs().slice(1))" in export
    assert "new URLSearchParams(p)" in export
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    assert html.count('id="reportingMonth"') == 1
    assert html.count('id="period"') == 1
    assert 'type="month"' in html and 'class="reporting-caption" role="status"' in html
    assert html.index('id="reportingMonth"') < html.index("</header>")
    assert html.index('id="periodRange"') < html.index("</header>")


def test_filter_options_use_month_and_discard_old_requests():
    run_node(r"""
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {periodParams} from "./web/assets/reporting-period.js";
const nodes=new Map(["filterBar","fbClear","fbSlots"].map(id=>[id,{innerHTML:"",hidden:false}]));
globalThis.document={
  documentElement:{dataset:{dashboardReady:"true"}},
  getElementById:id=>nodes.get(id), querySelectorAll:()=>[],
  addEventListener(){},removeEventListener(){},
};
const pending=[],saved=[];
globalThis.window={addEventListener(){}};
globalThis.bindings={
  esc:String, money:String,onScopeChange(){},scopeIds:()=>["sub-a"],periodParams,
  readStore:()=>({service:["Old service"]}),writeStore:(key,value)=>saved.push(JSON.stringify(value)),
  get:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve})),
};
let source=readFileSync("./web/assets/filters.js","utf8").replace(/^import .+;$/gm,"");
source="const {esc,get,money,onScopeChange,readStore,scopeIds,writeStore,periodParams}=globalThis.bindings;\n"+source;
const filters=await import("data:text/javascript;base64,"+Buffer.from(source).toString("base64"));
let changed=0;
filters.wireFilters(()=>changed++,30,"2026-08");
assert.match(pending[0].url,/period_month=2026-08/);
const newer=filters.loadFilters(30,"2024-02");
assert.equal(pending[0].options.signal.aborted,true);
assert.match(pending[1].url,/period_month=2024-02/);
pending[1].resolve({dimensions:{}});
await newer;
assert.equal(changed,1,"pruned dimension selection refreshes the newly selected month");
pending[0].resolve({dimensions:{service:[{value:"Old service",cost:1},{value:"Other",cost:2}]}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(nodes.get("filterBar").hidden,true,"late old-month options cannot restore stale filters");
assert.equal(saved.at(-1),"{}");
""")


def test_changing_month_invalidates_inflight_analysis():
    run_node(r"""
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {periodParams} from "./web/assets/reporting-period.js";
const nodes=new Map();
globalThis.document={getElementById:id=>{
  if(!nodes.has(id)) nodes.set(id,{innerHTML:"",querySelectorAll:()=>[],style:{}});
  return nodes.get(id);
}};
const pending=[];
globalThis.fetch=(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}));
globalThis.analysisBindings={esc:String,money:String,setBotCount(){},setBotState(){},periodParams};
let source=readFileSync("./web/assets/analysis.js","utf8").replace(/^import .+;$/gm,"");
source="const {esc,money,setBotCount,setBotState,periodParams}=globalThis.analysisBindings;\n"+source;
const analysis=await import("data:text/javascript;base64,"+Buffer.from(source).toString("base64"));
analysis.setAnalysisContext(()=>({days:30,month:"2024-02",scope:["sub-a"]}));
const first=analysis.run();
assert.match(pending[0].url,/period_month=2024-02/);
analysis.invalidate();
assert.equal(pending[0].options.signal.aborted,true);
analysis.setAnalysisContext(()=>({days:30,month:"2026-08",scope:["sub-a"]}));
const second=analysis.run();
assert.match(pending[1].url,/period_month=2026-08/);
pending[0].resolve({ok:true,json:async()=>({findings:[{title:"Stale month"}]})});
await first;
assert.equal(analysis.hasAnalysis(),false);
await analysis.run();
assert.equal(pending.length,2,"the old request cannot clear the new request's running state");
analysis.invalidate();
pending[1].resolve({ok:true,json:async()=>({findings:[]})});
await second;
assert.equal(analysis.hasAnalysis(),false);
""")
