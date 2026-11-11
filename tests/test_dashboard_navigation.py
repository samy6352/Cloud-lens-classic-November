"""Primary sections organize stable view IDs without widening access."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_dashboard_frontend import node
from tests.test_tags_frontend import run_node

ROOT = Path(__file__).resolve().parents[1]


def test_primary_sections_scope_labels_and_keyboard():
    script = r"""
import assert from "node:assert/strict";
import {PRIMARY_SECTIONS, sectionFor, sectionViews, sectionDestination, keyboardDestination}
  from "./web/assets/dashboard-navigation.js";
import {hasRailIcon} from "./web/assets/icons.js";
const expected = {
  overview:["overview"],
  billing:["billing","uptime","tags","history","budgets"],
  resources:["resources"],
  savings:["savings","commitments"],
  monitor:["changes","performance","anomalies","azure-updates"],
  governance:["governance","advisor-score","esu","health","audit"],
  reports:["report","settings"],
};
assert.deepEqual(PRIMARY_SECTIONS.map(s=>s.label),["Overview","Billing","All resources","Savings","Monitor","Governance","Reports"]);
assert.deepEqual(Object.fromEntries(PRIMARY_SECTIONS.map(s=>[s.id,s.members])),expected);
const ids=PRIMARY_SECTIONS.flatMap(s=>s.members);
assert.equal(ids.length,20);
assert.equal(new Set(ids).size,20);
assert.ok(PRIMARY_SECTIONS.every(section=>hasRailIcon(section.icon)));
assert.equal(PRIMARY_SECTIONS.find(section=>section.id==="governance").icon,"security");
assert.equal(PRIMARY_SECTIONS.find(section=>section.id==="reports").icon,"reports");
const views=ids.map(id=>({id,label:id}));
views.push({id:"compute",label:"Compute",area:true},{id:"aks",label:"AKS",area:true,overlay:true});
for(const section of PRIMARY_SECTIONS) {
  assert.deepEqual(sectionViews(section.id,views).map(v=>v.id),section.members);
  for(const id of section.members)assert.equal(sectionFor(id,views).id,section.id);
  assert.equal(sectionDestination(section.id,views,null),section.members[0]);
}
assert.equal(sectionFor("compute",views).id,"overview");
assert.equal(sectionFor("aks",views).id,"overview");
assert.equal(sectionFor("missing",views),null);
assert.equal(sectionDestination("overview",views,"compute"),"compute");
assert.equal(sectionDestination("billing",views,"tags"),"tags");
assert.equal(sectionDestination("billing",views,"changes"),"billing");
assert.equal(sectionDestination("billing",views,"resources"),"billing");
assert.equal(sectionDestination("resources",views,"resources"),"resources");
assert.equal(sectionViews("billing",views).some(view=>view.id==="resources"),false);
assert.equal(sectionDestination("missing",views,"overview"),null);
const member=views.filter(v=>v.id!=="audit");
assert.equal(sectionViews("governance",member).some(v=>v.id==="audit"),false);
assert.equal(sectionFor("audit",member),null);
assert.equal(sectionDestination("governance",member,"audit"),"governance");
assert.equal(sectionViews("billing",views)[0].label,"Billing summary");
assert.equal(sectionViews("billing",views)[2].label,"Applications & Tags");
assert.equal(sectionViews("governance",views)[0].label,"Compliance");
const keys=PRIMARY_SECTIONS.map(section=>section.id);
assert.equal(keyboardDestination(keys,"monitor","ArrowDown"),"governance");
assert.equal(keyboardDestination(keys,"overview","ArrowDown"),"billing");
assert.equal(keyboardDestination(keys,"billing","ArrowDown"),"resources");
assert.equal(keyboardDestination(keys,"reports","ArrowRight"),"overview");
assert.equal(keyboardDestination(keys,"overview","ArrowLeft"),"reports");
assert.equal(keyboardDestination(keys,"savings","Home"),"overview");
assert.equal(keyboardDestination(keys,"savings","End"),"reports");
assert.equal(keyboardDestination(keys,"savings","Tab"),null);
assert.equal(keyboardDestination([],"savings","ArrowDown"),null);
"""
    result = subprocess.run([node(), "--input-type=module", "-e", script], cwd=ROOT,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_old_accordion_state_does_not_control_new_navigation():
    source = (ROOT / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
    assert 'readStore("railOpen"' not in source
    assert "openGroups" not in source
    assert 'data-main-section=' in source and 'data-subsection=' in source
    assert 'window.addEventListener("cl:goto-tab"' in source
    assert "sectionDestination(id, sections, lastSectionView.get(id))" in source
    assert "panel?.setAttribute(\"aria-labelledby\"" in source
    assert 'document.documentElement.classList.toggle("rail-mini", on)' in source


def test_subsection_rows_omit_duplicate_section_labels_without_empty_columns():
    """The subsection nav already names the section, so the h2 beside it is visual repetition.

    It is hidden but not removed: `display: none` took the only h2 off the page and left every
    h3 below it following the h1 directly, which axe reports as a broken heading order. The
    visually-hidden form keeps the heading in the accessibility tree while still claiming no
    grid cell, so the single-column row this test guards is unaffected either way.
    """
    css = (ROOT / "web" / "assets" / "app.css").read_text(encoding="utf-8")
    hidden = css.split(".view-actions:has(.subsection-nav) .view-title {")[1].split("}")[0]
    assert "position: absolute;" in hidden and "clip: rect(0 0 0 0);" in hidden
    assert "display: none" not in hidden
    assert ".view-actions:has(.subsection-nav) { grid-template-columns: minmax(0, 1fr); }" in css
    assert ".view-title { display: none;" not in css


def test_removed_resiliency_has_no_module_route_or_navigation():
    for relative in ("web/assets/dashboard.js", "web/assets/dashboard-navigation.js", "app/main.py"):
        assert "resiliency" not in (ROOT / relative).read_text(encoding="utf-8").lower()
    assert not (ROOT / "app/resiliency.py").exists()
    assert not (ROOT / "web/assets/dashboard-resiliency.js").exists()


def test_grouped_navigation_reuses_available_views_without_restoring_removed_permissions():
    run_node(r"""
import assert from "node:assert/strict";
import {PRIMARY_SECTIONS,groupedNavigation,railGroupFor} from "./web/assets/dashboard-navigation.js";
import {hasRailIcon} from "./web/assets/icons.js";
const ids=PRIMARY_SECTIONS.flatMap(section=>section.members);
const views=ids.map(id=>({id,label:id,cost:id==="overview"?840:undefined}));
views.push({id:"compute",label:"Compute",area:true});
const before=JSON.stringify(views);
const groups=groupedNavigation(views);
assert.deepEqual(groups.map(group=>group.label),["Spend","Monitor","Optimize","Govern","Reports"]);
assert.deepEqual(groups[0].views.map(view=>view.id),["overview","billing","uptime","tags","history","budgets","resources"]);
const groupedIds=groups.flatMap(group=>group.views.map(view=>view.id));
assert.equal(new Set(groupedIds).size,ids.length);
assert.deepEqual([...groupedIds].sort(),[...ids].sort());
assert.equal(groups[0].views[0].cost,840);
assert.equal(groups[0].views[3].label,"Applications & Tags");
assert.ok(groups.every(group=>hasRailIcon(group.icon)));
assert.equal(railGroupFor("compute",views),"spend");
assert.equal(railGroupFor("resources",views),"spend");
assert.equal(railGroupFor("anomalies",views),"monitor");
assert.equal(railGroupFor("commitments",views),"optimize");
assert.equal(railGroupFor("missing",views),null);
const memberViews=views.filter(view=>view.id!=="audit");
assert.equal(groupedNavigation(memberViews).flatMap(group=>group.views).some(view=>view.id==="audit"),false);
assert.equal(railGroupFor("audit",memberViews),null);
assert.deepEqual(groupedNavigation([]),[]);
assert.equal(JSON.stringify(views),before);
""")
