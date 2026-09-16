"""Source-backed service/region filtering without new Azure calls."""
import subprocess

import pytest

from app import azure_updates
from tests.test_dashboard_frontend import ROOT, node


def run_js(source):
    result = subprocess.run(
        [node(), "--input-type=module", "-"], input=source, cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_service_and_region_filters_compose_and_match_any_reported_value():
    run_js(r"""
import assert from "node:assert/strict";
import {filterUpdates} from "./web/assets/dashboard-updates.js";
const items=[
  {id:"a",title:"Retirement",category:"Retirement",status:"Active",services:["Azure Functions","Azure Storage"],regions:["Central India"]},
  {id:"b",title:"Available",category:"GA",status:"Active",services:["Azure Storage"],regions:["West Europe"]},
  {id:"c",title:"Unspecified",category:"GA",status:"Resolved",services:[],regions:[]},
];
const ids=filters=>filterUpdates(items,filters).map(item=>item.id);
assert.deepEqual(ids({service:" azure functions "}),["a"]);
assert.deepEqual(ids({service:"Azure Storage",category:"GA"}),["b"]);
assert.deepEqual(ids({service:"Azure Storage",region:"centralindia",status:"Active",attention:true}),["a"]);
assert.deepEqual(ids({region:"CENTRAL-INDIA",query:"Retirement"}),["a"]);
assert.deepEqual(ids({region:"west_europe",service:"Azure Functions"}),[]);
assert.deepEqual(ids({service:null}),["c"]);
assert.deepEqual(ids({region:null,status:"Resolved"}),["c"]);
assert.deepEqual(ids({}),["a","b","c"]);
""")


def test_facets_cover_all_pages_deduplicate_metadata_and_retain_unavailable_selections():
    run_js(r"""
import assert from "node:assert/strict";
import {feedDimensionOptions,filterUpdates} from "./web/assets/dashboard-updates.js";
const items=Array.from({length:25},(_,i)=>({services:i===24?["Azure Functions"]:["Azure Storage"]}));
assert.equal(feedDimensionOptions(items,"services").find(x=>x.value==="Azure Functions").count,1);
const regions=feedDimensionOptions([
  {regions:["centralindia","Central India","CENTRAL-INDIA"]},
  {regions:["Central India"]},
  {regions:["West Europe"]},
  {regions:[]},
],"regions");
assert.deepEqual(regions.map(x=>[x.value,x.count]),[["Central India",2],["West Europe",1],[null,1]]);
assert.equal(feedDimensionOptions(items,"services","No longer supplied").at(-1).count,0);
assert.deepEqual(feedDimensionOptions([],"services",null),[{value:null,label:"Not specified",count:0}]);
assert.deepEqual(filterUpdates([{regions:["Global"]},{regions:[]}],{region:"eastus"}),[]);
""")


def test_missing_metadata_and_literal_null_do_not_share_a_selection():
    run_js(r"""
import assert from "node:assert/strict";
import {feedDimensionOptions,filterUpdates} from "./web/assets/dashboard-updates.js";
const items=[
  {id:"missing"}, {id:"blank",services:["  ",null,7]},
  {id:"literal",services:["null"]}, {id:"string",services:"Azure Functions"},
];
assert.deepEqual(filterUpdates(items,{service:null}).map(x=>x.id),["missing","blank"]);
assert.deepEqual(filterUpdates(items,{service:"null"}).map(x=>x.id),["literal"]);
assert.deepEqual(filterUpdates(items,{service:"Azure Functions"}).map(x=>x.id),["string"]);
const values=feedDimensionOptions(items,"services").map(x=>JSON.stringify(x.value));
assert.ok(values.includes("null") && values.includes('"null"'));
""")


@pytest.mark.parametrize("kind", ["updates", "status"])
def test_explicit_bare_product_tags_are_retained_without_inference(kind):
    payload = b"""<rss><channel><item><title>Azure Kubernetes Service appears only in the title</title>
      <category>Launched</category><category>Networking</category><category>Security</category>
      <category>Application Gateway</category><category>Virtual Machines</category>
      <category>App Service</category><category>Azure App Service</category>
      <category>Features</category></item></channel></rss>"""
    item = azure_updates.parse_feed(payload, kind)["items"][0]
    assert item["services"] == [
        "Azure Application Gateway", "Azure Virtual Machines", "Azure App Service",
    ]
    assert "Azure Kubernetes Service" not in item["services"]
    assert "Networking" in item["tags"]


def test_untagged_feed_notices_remain_explicitly_unclassified():
    item = azure_updates.parse_feed(
        b"<rss><channel><item><title>Azure service issue</title></item></channel></rss>", "status",
    )["items"][0]
    assert item["services"] == []
