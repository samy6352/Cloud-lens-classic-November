"""Tab search uses the live navigation registry and keeps browser history account-specific."""
from tests.test_tags_frontend import run_node


def test_searchable_tabs_preserve_available_views_and_match_labels_and_aliases():
    run_node(r"""
import assert from "node:assert/strict";
import {PRIMARY_SECTIONS} from "./web/assets/dashboard-navigation.js";
import {searchableTabs,matchingTabs} from "./web/assets/dashboard-search.js";
const views=PRIMARY_SECTIONS.flatMap(section=>section.members).map(id=>({id,label:id}));
views.push({id:"compute",label:"Compute & Web",area:true},{id:"aks",label:"Kubernetes / AKS",area:true,overlay:true});
const before=JSON.stringify(views), tabs=searchableTabs(views);
assert.equal(tabs.length,views.length);
assert.equal(new Set(tabs.map(tab=>tab.id)).size,views.length);
assert.ok(tabs.some(tab=>tab.id==="advisor-score"));
assert.equal(tabs.find(tab=>tab.id==="tags").label,"Applications & Tags");
assert.equal(tabs.find(tab=>tab.id==="compute").section,"Overview");
assert.equal(matchingTabs(tabs,"APPLICATION")[0].id,"tags");
assert.equal(matchingTabs(tabs,"  COST   HOUR  ")[0].id,"uptime");
assert.equal(matchingTabs(tabs,"rightsizing")[0].id,"savings");
assert.equal(matchingTabs(tabs,"Kubernetes")[0].id,"aks");
assert.equal(matchingTabs(tabs,"compliance")[0].id,"governance");
assert.equal(matchingTabs(tabs,"download")[0].id,"report");
assert.equal(matchingTabs(tabs,"service health")[0].id,"azure-updates");
assert.deepEqual(matchingTabs(tabs,"unmatched wording"),[]);
assert.equal(matchingTabs(tabs,"").length,views.length);
const restricted=searchableTabs(views.filter(view=>!["audit","aks"].includes(view.id)));
assert.deepEqual(matchingTabs(restricted,"audit"),[]);
assert.deepEqual(matchingTabs(restricted,"Kubernetes"),[]);
assert.equal(JSON.stringify(views),before);
""")


def test_recent_searches_are_bounded_deduplicated_and_filtered_by_current_access():
    run_node(r"""
import assert from "node:assert/strict";
import {searchableTabs,recentTabSearches,rememberTabSearch} from "./web/assets/dashboard-search.js";
const tabs=searchableTabs([{id:"resources",label:"All resources"},{id:"tags",label:"Tags"}]);
let history=[];
for(let index=0;index<10;index++) history=rememberTabSearch(history,`query ${index}`,"resources");
assert.equal(history.length,6);
assert.equal(history[0].query,"query 9");
history=rememberTabSearch(history," QUERY   7 ","resources");
assert.equal(history[0].query,"QUERY   7");
assert.equal(history.filter(item=>item.query.trim().replace(/\s+/g," ").toLowerCase()==="query 7").length,1);
assert.deepEqual(recentTabSearches(null,tabs),[]);
const raw=[null,{}, {id:"audit",query:"privileged"}, {id:"gone",query:"stale"},
  {id:"resources",query:"resource search"},{id:"tags",query:42},{id:"tags",query:"x".repeat(81)},
  {id:"tags",query:" "},{id:"tags",query:'<img src=x onerror=alert(1)>'}];
const before=JSON.stringify(raw), recent=recentTabSearches(raw,tabs);
assert.deepEqual(recent.map(item=>item.id),["resources","tags"]);
assert.equal(recent[0].tab.label,"All resources");
assert.equal(JSON.stringify(raw),before);
assert.equal(rememberTabSearch([], "x".repeat(120),"tags")[0].query.length,80);
assert.equal(rememberTabSearch(history, " ", "tags").length,6);
""")


def test_history_is_partitioned_by_user_and_auth_mode():
    run_node(r"""
import assert from "node:assert/strict";
import {tabSearchHistoryKey} from "./web/assets/dashboard-search.js";
assert.equal(tabSearchHistoryKey(null),null);
assert.equal(tabSearchHistoryKey({}),null);
assert.equal(tabSearchHistoryKey({name:""}),null);
assert.equal(tabSearchHistoryKey({email:" ALICE@example.test ",source:"entra"}),
  tabSearchHistoryKey({email:"alice@example.test",source:"entra"}));
assert.notEqual(tabSearchHistoryKey({name:"alice",source:"file"}),tabSearchHistoryKey({name:"alice",source:"entra"}));
assert.notEqual(tabSearchHistoryKey({name:"alice"}),tabSearchHistoryKey({name:"bob"}));
""")
