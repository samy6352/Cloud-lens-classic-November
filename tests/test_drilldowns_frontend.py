"""Summary drilldowns keep calendar periods, subscription identity, and disclosure focus honest."""
from tests.test_tags_frontend import run_node


def test_header_destinations_cover_calendar_rollover_without_mutating_the_clock():
    run_node(r"""
import assert from "node:assert/strict";
import {glanceDestination} from "./web/assets/dashboard-drilldowns.js";
const now=new Date("2026-01-31T23:59:59Z"), original=now.toISOString();
assert.deepEqual(glanceDestination(0,now),{tab:"overview",month:"2026-01",target:"trendChart"});
assert.deepEqual(glanceDestination(1,now),{tab:"overview",month:"2025-12",target:"trendChart"});
assert.equal(glanceDestination(2,now),null,"a forecast opens its own evidence, not historical spend");
assert.deepEqual(glanceDestination(3,now),{tab:"overview",month:"2026-01",target:"monthOverMonth"});
assert.deepEqual(glanceDestination(4,now),{tab:"budgets"});
assert.deepEqual(glanceDestination(5,now),{tab:"savings"});
assert.equal(glanceDestination(1,new Date("2024-03-31T00:00:00Z")).month,"2024-02");
assert.equal(now.toISOString(),original);
""")


def test_subscription_name_aggregates_never_pick_one_arbitrary_id_or_broaden_scope():
    run_node(r"""
import assert from "node:assert/strict";
import {subscriptionDrillIds} from "./web/assets/dashboard-drilldowns.js";
const subs=[{id:"one",name:"Shared"},{id:"two",name:"Shared"},{id:"three",name:"Other"},{id:"bare"}];
const before=JSON.stringify(subs);
assert.deepEqual(subscriptionDrillIds({name:"Shared"},subs),["one","two"]);
assert.deepEqual(subscriptionDrillIds({name:"Shared"},subs,["two","three"]),["two"]);
assert.deepEqual(subscriptionDrillIds({id:"three",name:"Shared"},subs),["three"]);
assert.deepEqual(subscriptionDrillIds({id:"three"},subs,["one"]),[]);
assert.deepEqual(subscriptionDrillIds({name:"bare"},subs),["bare"]);
assert.deepEqual(subscriptionDrillIds({name:"Removed"},subs),[]);
assert.equal(JSON.stringify(subs),before);
""")


def test_revealing_breakdowns_opens_ancestors_and_moves_keyboard_focus():
    run_node(r"""
import assert from "node:assert/strict";
import {revealOverviewTarget} from "./web/assets/dashboard-drilldowns.js";
const outer={open:false,parentElement:null};
const inner={open:false,parentElement:{closest:()=>outer}};
let scrolled=0,focused=0;
const heading={id:"",textContent:"Top services",closest:()=>inner,
  scrollIntoView:()=>scrolled++,focus:options=>{assert.equal(options.preventScroll,true);focused++;}};
const chart={...heading,id:"trendChart"};
const root={querySelectorAll:selector=>selector==="[id]"?[chart]:[heading]};
assert.equal(revealOverviewTarget(root,"Top services"),true);
assert.equal(inner.open,true); assert.equal(outer.open,true);
assert.equal(heading.tabIndex,-1); assert.equal(scrolled,1); assert.equal(focused,1);
assert.equal(revealOverviewTarget(root,"trendChart"),true);
assert.equal(revealOverviewTarget(root,'missing"]'),false,"unknown targets are data, not selectors");
assert.equal(focused,2);
""")
