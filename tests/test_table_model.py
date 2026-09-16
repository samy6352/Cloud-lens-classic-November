"""Shared table behavior without a browser, Azure, or PostgreSQL."""
import subprocess

from tests.test_dashboard_frontend import ROOT, node


def run_node(script):
    result = subprocess.run(
        [node(), "--input-type=module", "-e", script], cwd=ROOT,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_search_decodes_visible_text_and_preserves_filtered_sorting():
    run_node(r"""
import assert from "node:assert/strict";
import {cellText,tableRows} from "./web/assets/dashboard-table-model.js";
assert.equal(cellText('<strong>A&amp;B &lt;Prod&gt;</strong>'),"A&B <Prod>");
assert.equal(cellText("&#39;one&#x20;two&#39;&nbsp;&quot;three&quot;"),"'one two' \"three\"");
assert.equal(cellText("&amp;lt;"),"&lt;","decode exactly once");
assert.equal(cellText("&#x110000;"),"\ufffd");
const rows=[{name:"A&amp;B",cost:10},{name:"Other",cost:100},{name:"A&amp;B group",cost:3}];
const state={rows,columns:[{cell:r=>r.name},{cell:r=>String(r.cost),value:r=>r.cost}],
  query:"a&b",sort:1,dir:-1};
const result=tableRows(state);
assert.deepEqual(result.map(r=>r.cost),[10,3]);
assert.equal(tableRows(state),result,"reuse the derived view until filters or ordering change");
state.dir=1;
assert.deepEqual(tableRows(state).map(r=>r.cost),[3,10]);
assert.deepEqual(rows.map(r=>r.cost),[10,100,3],"never mutate source rows");
state.query="other";
assert.deepEqual(tableRows(state).map(r=>r.cost),[100]);
""")


def test_unknown_numbers_sort_after_known_values_in_both_directions():
    run_node(r"""
import assert from "node:assert/strict";
import {tableRows} from "./web/assets/dashboard-table-model.js";
const rows=[{value:null},{value:0},{value:NaN},{value:-3},{value:10},{value:undefined},{value:Infinity}];
const state={rows,columns:[{cell:r=>String(r.value),value:r=>r.value}],query:"",sort:0,dir:-1};
assert.deepEqual(tableRows(state).slice(0,3).map(r=>r.value),[10,0,-3]);
state.dir=1;
assert.deepEqual(tableRows(state).slice(0,3).map(r=>r.value),[-3,0,10]);
state.sort=2;
assert.throws(()=>tableRows(state),/Unknown table sort column/);
""")


def test_currency_is_part_of_stable_resource_row_identity():
    run_node(r"""
import assert from "node:assert/strict";
import {tableRowKeys} from "./web/assets/dashboard-table-model.js";
const old=[{id:"vm-a",currency:"USD",cost:5},{id:"vm-a",currency:"EUR",cost:10}];
const fresh=[{id:"vm-a",currency:"EUR",cost:12},{id:"vm-a",currency:"USD",cost:8}];
const before=tableRowKeys(old),after=tableRowKeys(fresh);
assert.notEqual(before.get(old[0]),before.get(old[1]));
assert.equal(before.get(old[0]),after.get(fresh[1]));
assert.equal(before.get(old[1]),after.get(fresh[0]));
const identical=[{id:"same",cost:1},{id:"same",cost:1}];
assert.equal(new Set(tableRowKeys(identical).values()).size,2);
""")


def test_cost_totals_keep_currency_and_missing_evidence_separate():
    run_node(r"""
import assert from "node:assert/strict";
import {sumCostsByCurrency} from "./web/assets/cost-totals.js";
const result=sumCostsByCurrency([
  {cost:7,currency:"USD"},{cost:-2,currency:"usd"},{cost:9,currency:"EUR"},
  {cost:null,currency:"USD"},{cost:undefined},{cost:0,currency:"GBP"},
]);
assert.deepEqual(result.map(({currency,amount})=>({currency,amount})),[
  {currency:"EUR",amount:9},{currency:"GBP",amount:0},{currency:"UNKNOWN",amount:null},{currency:"USD",amount:5},
]);
assert.equal(result.at(-1).missing,1);
assert.equal(sumCostsByCurrency([{cost:5}],"EUR")[0].currency,"EUR");
assert.equal(sumCostsByCurrency([{cost:5},{cost:9}])[0].amount,null,
  "unknown currency amounts cannot be assumed to share a unit");
assert.deepEqual(sumCostsByCurrency([]),[]);
""")


def test_keys_remain_byte_identical_for_ascii_unicode_and_surrogates():
    run_node(r"""
import assert from "node:assert/strict";
import {stableKey} from "./web/assets/dashboard-state.js";
const original=value=>Array.from(new TextEncoder().encode(String(value)),
  byte=>byte.toString(16).padStart(2,"0")).join("");
for(const value of ["",null,undefined,0,false,"ASCII \u0000\x7f",
  "日本語","\u{1f680}","e\u0301","\ud800","\udfff","/subscriptions/a/resourceGroups/Example"]) {
  assert.equal(stableKey(value),original(value));
}
let seed=7;
for(let sample=0;sample<100;sample++) {
  let value="";
  for(let index=0;index<200;index++) {
    seed=(seed*1664525+1013904223)>>>0;
    value+=String.fromCharCode(seed%65536);
  }
  assert.equal(stableKey(value),original(value));
}
""")


def test_sort_computes_each_cell_value_once():
    run_node(r"""
import assert from "node:assert/strict";
import {tableRows} from "./web/assets/dashboard-table-model.js";
let calls=0;
const rows=Array.from({length:1000},(_,value)=>({value:1000-value}));
const state={rows,columns:[{cell:row=>{calls++;return `<b>${row.value}</b>`}}],
  query:"",sort:0,dir:1};
const ordered=tableRows(state);
assert.equal(calls,rows.length);
assert.equal(ordered[0].value,1);
assert.equal(ordered.at(-1).value,1000);
assert.equal(rows[0].value,1000);
assert.equal(tableRows(state),ordered);
assert.equal(calls,rows.length,"cached views do not reformat cells");
""")
