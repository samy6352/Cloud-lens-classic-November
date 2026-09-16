"""Resource explorer filtering and money semantics without Azure or PostgreSQL."""
import subprocess

from tests.test_dashboard_frontend import ROOT, node


def test_resource_filters_summary_and_unknowns():
    script = r"""
import assert from "node:assert/strict";
import {filterResources,resourceFacets,resourceSummary,resourceChange,createTabs}
  from "./web/assets/dashboard-resources.js";
const rows=[
  {id:"/sub/a",name:"API <test>",service:"Compute",subscription_id:"one",resource_group:"Prod",
    currency:"USD",cost:20,prior_cost:10,tags:[{key:"owner",value:"Alice"}],region:"eastus"},
  {id:"/SUB/A",name:"API <test>",service:"compute",subscription_id:"one",resource_group:"prod",
    currency:"EUR",cost:5,prior_cost:0,tags:[]},
  {id:"/sub/b",name:"Disk",service:"Storage",subscription_id:"two",resource_group:"Prod",
    currency:"USD",cost:-3,prior_cost:10,tags:[]},
  {id:"/sub/c",name:"Idle",service:"",subscription_id:"one",currency:"USD",cost:0,prior_cost:4},
  {id:"/sub/d",name:"Unobserved",currency:null,cost:null,prior_cost:null},
];
assert.equal(filterResources(rows,{query:"  ALICE "}).length,1);
assert.equal(filterResources(rows,{query:"<test>"}).length,2);
assert.equal(filterResources(rows,{query:"/sub/a"}).length,2);
assert.equal(filterResources(rows,{service:"value:compute"}).length,2);
assert.equal(filterResources(rows,{service:"missing"}).length,2);
assert.equal(filterResources(rows,{currency:"value:usd",change:"decreased"}).length,2);
assert.equal(filterResources(rows,{tags:"recorded"}).length,1);
assert.equal(filterResources(rows,{tags:"missing"}).length,4);
assert.equal(filterResources(rows,{query:"impossible"}).length,0);
assert.equal(resourceFacets(rows,"service").length,3);
assert.equal(resourceFacets(rows,"currency").length,3);
assert.equal(resourceChange(rows[0]),"increased");
assert.equal(resourceChange(rows[1]),"no-baseline");
assert.equal(resourceChange(rows[3]),"decreased");
assert.equal(resourceChange(rows[4]),"unavailable");
assert.equal(resourceChange({cost:0,prior_cost:0}),"no-baseline");
assert.equal(resourceChange({cost:10,prior_cost:10}),"unchanged");
const summary=resourceSummary(rows);
assert.equal(summary.resources,4,"one resource can have multiple currency rows");
assert.equal(summary.groups,2,"same group name in separate subscriptions is not one group");
assert.equal(summary.services,2);
assert.deepEqual(summary.costs.map(({currency,amount})=>({currency,amount})),
  [{currency:"EUR",amount:5},{currency:"UNKNOWN",amount:null},{currency:"USD",amount:17}]);
const partial=resourceSummary([{id:"a",currency:"USD",cost:10},{id:"b",currency:"USD",cost:null}]);
assert.equal(partial.costs[0].amount,10); assert.equal(partial.costs[0].missing,1);
assert.equal(resourceSummary([]).costs.length,0);
let painted=false;
const tabs=createTabs({loadToken:2,cached:async(k,fetcher)=>fetcher(),key:x=>x,qs:()=>"",
  get:async()=>({resources:[]}),setContent:()=>{painted=true}});
await tabs.renderResources(1);
assert.equal(painted,false,"a stale response cannot paint another scope");
const invalid=createTabs({loadToken:1,cached:async(k,fetcher)=>fetcher(),key:x=>x,qs:()=>"",get:async()=>({})});
await assert.rejects(()=>invalid.renderResources(1),/unavailable or incomplete/);
"""
    result = subprocess.run([node(), "--input-type=module", "-e", script], cwd=ROOT,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
