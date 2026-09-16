"""The overview monthly chart shows at most three aligned months without changing history."""
from tests.test_tags_frontend import run_node


def test_monthly_chart_caps_latest_labels_and_every_series_without_mutation():
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
for (const months of [
  ["2026-08","2026-09"],
  ["2026-07","2026-08","2026-09"],
  ["2026-06","2026-07","2026-08","2026-09"],
  ["2025-10","2025-11","2025-12","2026-01","2026-03","2026-09"],
]) {
  let chart;
  const host={remove(){throw new Error("A multi-month chart must remain visible")}};
  const tabs=createTabs({
    $:id=>id==="monthOverMonth"?host:null,
    whenNear:(_host,prepare)=>prepare()(),
    drawChart:(_host,spec)=>{chart=spec},
  });
  const data={monthly:{months, currency:"EUR", areas:[
    {label:"Compute",data:months.map((_,i)=>i*100)},
    {label:"Credits",data:months.map((_,i)=>i===months.length-1?null:-i)},
  ]}};
  const before=JSON.stringify(data);
  tabs.renderMonthOverMonth(1,data,"USD");
  assert.deepEqual(chart.labels,months.slice(-3));
  assert.deepEqual(chart.datasets[0].data,data.monthly.areas[0].data.slice(-3));
  assert.deepEqual(chart.datasets[1].data,data.monthly.areas[1].data.slice(-3));
  assert.equal(chart.currency,"EUR");
  assert.equal(chart.labels.length,Math.min(3,months.length));
  assert.match(chart.note,new RegExp(`Last ${chart.labels.length} months`));
  assert.equal(JSON.stringify(data),before,"the warehouse response and other views remain unchanged");
}
""")


def test_insufficient_history_explains_itself_and_keeps_the_drilldown_target():
    """Too little history draws no chart, but the card stays.

    It is the target the month-over-month summary tile drills into (glanceDestination) and
    one column of the .grid-bridge row, so removing it would break navigation and layout.
    """
    run_node(r"""
import assert from "node:assert/strict";
import {createTabs} from "./web/assets/dashboard-spend.js";
for(const monthly of [null,{}, {months:[]}, {months:["2026-09"],areas:[]}]) {
  let removed=0,drawn=0;
  const host={innerHTML:"",remove:()=>removed++};
  const tabs=createTabs({$:()=>host,drawChart:()=>drawn++,
    whenNear:()=>{throw new Error("No deferred work for insufficient history")}});
  tabs.renderMonthOverMonth(1,{monthly},"USD");
  assert.equal(drawn,0,"a single bar cannot show a direction");
  assert.equal(removed,0,"the summary tile drills into this card; it has to still be there");
  assert.match(host.innerHTML,/At least two months/,"the card says why it is empty");
}
""")
