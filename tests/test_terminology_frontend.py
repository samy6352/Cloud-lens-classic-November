"""Billing and inventory terminology must not assert runtime or verified savings."""
from tests.test_tags_frontend import HARNESS, run_node


def test_scenario_requires_vm_resource_shape_and_server_capability():
    run_node(r"""
import assert from "node:assert/strict";
import {scheduleAssessment,illustrativeSaving} from "./web/assets/billing-usage.js";
const vm="/subscriptions/a/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm";
const row={id:vm,service:"Virtual Machines",cost:168,uptime_pct:100,schedulable:true};

// The server's capability is required, and only VM compute resources are offered for review.
assert.equal(scheduleAssessment(row),"needs_review");
assert.ok(Math.abs(illustrativeSaving(row,60)-108)<1e-9);
assert.equal(illustrativeSaving({...row,uptime_pct:25},60),0);

const plan={...row,id:"/subscriptions/a/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan",
  service:"Azure App Service"};
assert.equal(scheduleAssessment(plan),"not_assessed");
assert.equal(illustrativeSaving(plan,48),0);

// A family the server excluded is never second-guessed here.
assert.equal(scheduleAssessment({...row,service:"Virtual Machines Licenses",schedulable:false}),
  "not_assessed");

// Row-shape guards: only the resource itself, never a child or an unparseable id.
for(const other of [
  {...row,id:"unknown"},
  {...row,id:vm+"/extensions/test"},
  {...row,id:""},
  {...row,schedulable:false},
  {...row,schedulable:undefined},
]) {
  assert.equal(scheduleAssessment(other),"not_assessed");
  assert.equal(illustrativeSaving(other,60),0);
}
for(const cost of [-1,NaN,Infinity,null])assert.equal(illustrativeSaving({...row,cost},60),0);
for(const hours of [-1,169,NaN])assert.equal(illustrativeSaving(row,hours),0);
""")


def test_budget_header_reserves_none_set_for_confirmed_empty():
    run_node(r"""
import assert from "node:assert/strict";
import {budgetPresentation} from "./web/assets/overview-status.js";
assert.equal(budgetPresentation({budget:null,budget_status:"none"}).value,"none set");
for(const status of ["unavailable","partial",undefined]) {
  assert.equal(budgetPresentation({budget:null,budget_status:status}).value,"Unavailable");
}
assert.equal(budgetPresentation({budget:null,budget_status:"unknown"}).value,"Unknown");
const zero=budgetPresentation({budget:{name:"Dev",percent_used:0,status:"ok"},budget_status:"ok"});
assert.equal(zero.value,"0%");assert.equal(zero.percent,0);
const partial=budgetPresentation({budget:{name:"Dev",percent_used:12,status:"ok"},budget_status:"partial"});
assert.match(partial.sub,/incomplete/);assert.equal(partial.percent,12);
assert.equal(budgetPresentation({budget:{percent_used:null}}).value,"Unavailable");
""")


def test_hourly_labels_explain_billing_not_availability_and_exclude_app_service():
    run_node(HARNESS + r"""
Element.prototype.remove=function(){};
const vm={id:"/subscriptions/a/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
  name:"VM",service:"Virtual Machines",meter:"Compute",key:"vm",cost:168,hours:168,uptime_pct:100,always_on:true,units:1,schedulable:true};
const plan={...vm,id:"/subscriptions/a/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan",
  name:"Plan",service:"Azure App Service",key:"plan",cost:1000};
ctx.get=async()=>({currency:"USD",days:7,resources:[vm,plan],dates:["2026-09-01","2026-09-02"],
  daily:{vm:[[0,24,24],[1,24,24]],plan:[[0,24,100],[1,24,100]]},total_spend:1168,
  schedules:[{id:"business",label:"Business",hours:60}]});
await createTabs(ctx).renderUptime(1);
assert.match(root.innerHTML,/Hourly Billing/);
assert.match(root.innerHTML,/Hourly average/);
assert.match(root.innerHTML,/High billed coverage/);
assert.match(root.innerHTML,/Illustrative savings/);
assert.match(root.innerHTML,/stopping an app does not stop its plan charges/);
assert.doesNotMatch(root.innerHTML,/Never switched off|24\/7|already off enough|cannot be stopped|measured uptime/);
const table=tables.get("Hourly meters");
const coverage=table.columns.find(c=>c.label==="Billed uptime");
assert.match(coverage.description,/not service availability/);
assert.equal(coverage.value(vm),100);
const savings=table.columns.find(c=>c.label==="Illustrative savings");
// Stopping an app does not stop plan charges; no VM-style proportional saving is offered.
assert.equal(savings.value(plan),0);
assert.match(savings.cell(plan),/Not assessed/);
assert.match(savings.cell(vm),/Needs review/);
// A family the server excluded still reads as not assessed.
assert.equal(savings.value({...plan,schedulable:false}),0);
assert.match(savings.cell({...plan,schedulable:false}),/Not assessed/);
assert.equal(charts[0].datasets[0].data[0],+(124/24).toFixed(4));
assert.match(charts[1].title,/Billed hours by weekday/);
""")


def test_aks_unavailable_and_detected_stopped_clusters_have_honest_labels():
    run_node(HARNESS + r"""
ctx.sections=[{id:"aks",currency:"USD"},{id:"overview",cost:100}];
ctx.get=async()=>({days:7,currency:"USD",total:10,detected_clusters:null,discovery_status:"unavailable",
  discovery_note:"Inventory unavailable",clusters:[{cluster:"Old",cost:10,live:false,inventory_status:"unknown"}],
  unmatched_groups:[{cluster:"Old"}]});
await createTabs(ctx).renderAksView(1);
assert.match(root.innerHTML,/Clusters shown/);assert.match(root.innerHTML,/Unmatched groups/);
assert.doesNotMatch(root.innerHTML,/running now|still charging|Clusters with spend|Orphaned/);
assert.match(tables.get("Clusters").columns[0].cell({cluster:"Old",live:false,inventory_status:"unknown"}),/Unknown/);
assert.doesNotMatch(tables.get("Clusters").columns[0].cell({cluster:"Old",live:false}),/deleted/);
ctx.get=async()=>({days:7,currency:"USD",billing_empty:true,detected_clusters:1,discovery_status:"ok",
  clusters:[{cluster:"Stopped",cost:0,live:true,power_state:"Stopped",inventory_status:"detected"}]});
await createTabs(ctx).renderAksView(1);
assert.match(root.innerHTML,/1 detected clusters/);
assert.match(root.innerHTML,/Not loaded/);
const state=tables.get("Clusters").columns.find(c=>c.label==="Reported state");
assert.equal(state.cell({live:true,power_state:"Stopped"}),"Stopped");
""")
