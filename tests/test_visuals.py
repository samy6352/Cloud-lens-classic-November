# cloudlens-test: requires-offline
"""Static contracts for dashboard wiring, visual assets and shared styles.

Run with pytest or directly as ``python tests/test_visuals.py``. Browser suites cover
rendered layout and interaction behavior.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "web", "assets")

checks = 0
failures: list[str] = []

def shares_reader(source: str, method_name: str, *assignments: str) -> bool:
    """Check query placement without depending on a with-statement's formatting."""
    for method in ast.walk(ast.parse(source)):
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name != method_name:
            continue
        for context in ast.walk(method):
            if not isinstance(context, ast.With) or not any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "reader" for item in context.items
            ):
                continue
            names = {
                target.id for node in ast.walk(context) if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)
            }
            if set(assignments) <= names:
                return True
    return False


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def read(name: str) -> str:
    with open(os.path.join(ASSETS, name), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------- world geometry
print("\nworldmap.js — generated geometry")

world_src = read("worldmap.js")

for const in ("WORLD", "MAP_W", "MAP_H", "LAT_TOP", "LAT_BOTTOM"):
    check(f"exports {const}", re.search(rf"export const {const}\s*=", world_src) is not None)

m = re.search(r'export const WORLD = "([^"]*)"', world_src)
check("WORLD path is present and non-empty", bool(m and len(m.group(1)) > 1000))

path = m.group(1) if m else ""
map_w = float(re.search(r"export const MAP_W = ([\d.]+)", world_src).group(1))
map_h = float(re.search(r"export const MAP_H = ([\d.]+)", world_src).group(1))

# Walk every subpath, accumulating the relative linetos into absolute positions.
subpaths = list(path.split("M")[1:])
check("path contains many subpaths", len(subpaths) > 100, f"got {len(subpaths)}")

worst_dx = 0.0
worst_sub = -1
min_x = min_y = float("inf")
max_x = max_y = float("-inf")
points_total = 0

for i, sub in enumerate(subpaths):
    head, _, body = sub.partition("l")
    start = head.strip().split()
    x, y = float(start[0]), float(start[1])
    min_x, max_x = min(min_x, x), max(max_x, x)
    min_y, max_y = min(min_y, y), max(max_y, y)
    for dx, dy in re.findall(r"(-?\d+)\s*(-?\d+)", body.rstrip("Z")):
        dxf = float(dx)
        if abs(dxf) > worst_dx:
            worst_dx, worst_sub = abs(dxf), i
        x += dxf
        y += float(dy)
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)
        points_total += 1

# The regression: a segment crossing the antimeridian spans nearly the whole width. Real
# coastline steps at 110m resolution are a small fraction of that. A quarter of the map is a
# generous ceiling that no genuine simplified segment comes close to.
check(
    "no segment spans the map horizontally (antimeridian)",
    worst_dx < map_w / 4,
    f"largest dx {worst_dx:.0f} of {map_w:.0f} in subpath {worst_sub}",
)

check("geometry stays within the viewBox horizontally", min_x >= -1 and max_x <= map_w + 1,
      f"x range {min_x:.0f}..{max_x:.0f}")
check("geometry stays within the viewBox vertically", min_y >= -1 and max_y <= map_h + 1,
      f"y range {min_y:.0f}..{max_y:.0f}")
check("geometry spans most of the width", (max_x - min_x) > map_w * 0.9)
check("Antarctica is excluded", max_y < map_h + 1)
check("point count is reasonable for 110m", 2000 < points_total < 20000, f"{points_total} points")

# Size is a product constraint, not a detail: the whole app is about 50 KB over the wire.
raw = len(world_src.encode())
check("world geometry stays under 32 KB raw", raw < 32_000, f"{raw:,} bytes")

check("carries a provenance note", "Natural Earth" in world_src)
check("is marked generated", "Do not hand-edit" in world_src or "regenerate" in world_src.lower())


# ------------------------------------------------------------------- region map
print("\nregionmap.js — plotting")

rm = read("regionmap.js")
check("imports the generated geometry", "from \"./worldmap.js\"" in rm)
check("does not ship a hand-traced coastline", "MERIDIANS" not in rm and "PARALLELS" not in rm)
check("separates overlapping bubbles", "function separate" in rm)
check("scales circles by area, not radius", "Math.sqrt" in rm)
check("counts rows that are not places", "NON_PLACES" in rm)
check("reports unmapped regions rather than dropping them silently",
      "unrecognised" in rm or "unmapped" in rm)
# A region with no coordinates must never be plotted at (0,0), which is in the Atlantic.
check("filters to regions with known coordinates", ".filter((r) => r.at)" in rm)


# ---------------------------------------------------------------------- treemap
print("\ntreemap.js — layout")

tm = read("treemap.js")
check("uses squarified layout", "function squarify" in tm)
# The bug: laying out against a notional rectangle then stretching with percentages discards
# the aspect ratios squarify exists to control.
check("measures the real container", "clientWidth" in tm and "clientHeight" in tm)
check("does not lay out against a fixed rectangle", "const W = 1000" not in tm)
check("re-lays out on resize", "ResizeObserver" in tm)
check("disconnects the observer when detached", "isConnected" in tm)
check("rolls the tail into one tile", "rolled" in tm)
check("drops labels that would not fit", "tight" in tm)


# ------------------------------------------------------------------ stylesheet
print("\napp.css — the new blocks")

css = read("app.css")

defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
# Properties set from markup rather than the stylesheet. `--i` is the finding's index, written
# as an inline style so each analysis card can stagger its own entrance — it is defined at every
# use site, just not in a file this check can see.
from_markup = {"--i"}
missing = sorted(used - defined - from_markup)
check(
    "every custom property used is defined",
    not missing,
    "undefined: " + ", ".join(missing) if missing else "",
)

for cls in ("grid-viz", "rg-land", "rg-dot", "rg-halo", "rg-legend", "rg-leg-bar",
            "tm-plot", "tm-tile", "tm-inner", "tm-val"):
    check(f"styles .{cls}", f".{cls}" in css)

# Every swatch the JS can emit must exist, in both themes, or a tile renders transparent.
swatch_count = len(re.findall(r"\.tm-c(\d+)\s*\{", css))
light_swatches = len(re.findall(r'\[data-theme="light"\]\s*\.tm-c\d+', css))
js_swatches = int(re.search(r"const SWATCHES = (\d+)", tm).group(1))
check("dark swatches cover every index the JS emits", swatch_count >= js_swatches,
      f"{swatch_count} css vs {js_swatches} js")
check("light swatches cover every index the JS emits", light_swatches >= js_swatches,
      f"{light_swatches} css vs {js_swatches} js")

# The treemap gap is drawn as a border so tile areas stay exactly proportional; a margin would
# shrink small tiles proportionally more and misstate the shares. Comments are stripped first:
# the explanatory comment in that block contains the words "not a margin:", which an earlier
# version of this check happily matched.
css_bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
tile_block = re.search(r"\.tm-tile\s*\{([^}]*)\}", css_bare)
check("tile spacing uses border, not margin",
      bool(tile_block) and "border:" in tile_block.group(1) and "margin:" not in tile_block.group(1))

check("panels stretch to equal height", "align-items: stretch" in css)
check("captions pin to the bottom of the panel", ".grid-viz .chart-note" in css
      and "margin-top: auto" in css)

# The merged savings list. Six rail entries became one, so the styles that carry the merge's
# reasoning — where a figure came from, and whether more than one scan agreed — have to exist,
# or the list looks like an ordinary table of numbers with no way to check them.
for cls in ("sv-list", "sv-card", "sv-head", "sv-body", "sv-chip", "sv-sources",
            "sv-badge", "sv-options", "sv-disputed", "sv-folded"):
    check(f"styles .{cls}", f".{cls}" in css)
check("a billed figure looks different from a projected one",
      ".sv-basis-billed" in css)
check("the corroboration badge is the one coloured thing on a collapsed row",
      ".sv-badge.good" in css and "var(--ok)" in css)

# The launcher is fixed to the bottom-right; so is the analysis panel's ask bar. At 1280 wide the
# 56px circle covered 46% of the Ask button, so a click meant to send a question shut the panel.
# Nothing about that is visible until the two happen to be on screen together.
check("the launcher gets out of the way once the analysis is open",
      bool(re.search(r"body\.analysing\s+\.bot\s*\{[^}]*opacity:\s*0", css_bare)))
check("and stops taking clicks while it is invisible",
      bool(re.search(r"body\.analysing\s+\.bot\s*\{[^}]*pointer-events:\s*none", css_bare)))


# ------------------------------------------------------------------- dashboard
print("\ndashboard.js — wiring")

from dashboard_source import read_dashboard

dash = read_dashboard()
check("lazy-loads the treemap", 'import("./treemap.js")' in dash)
check("lazy-loads the region map", 'import("./regionmap.js")' in dash)
check("renders the treemap", "drawTreemap(" in dash)
check("renders the map", "drawRegionMap(" in dash)
check("pairs them in one row", 'class="grid-viz"' in dash)
check("treemap is fed subscriptions",
      re.search(r'drawTreemap\(\s*host\.querySelector\("#spendTreemap"\),\s*data\.subscriptions', dash) is not None)
check("only one region map container", dash.count('id="regionMap"') == 1)
check("only one treemap container", dash.count('id="spendTreemap"') == 1)


# ----------------------------------------------------------------- the rail
print("\ndashboard.js — navigation hierarchy")

navigation = read("dashboard-navigation.js")
primary_navigation = navigation.split("export const PRIMARY_SECTIONS =", 1)[1].split("];", 1)[0]
groups = re.findall(r'\{\s*id:\s*"(\w+)",\s*label:\s*"([^"]+)"',
                   primary_navigation)
check("the rail has seven primary sections", len(groups) == 7, str([g[1] for g in groups]))
check("they are named for what they answer",
      [g[1] for g in groups] == ["Overview", "Billing", "All resources", "Savings", "Monitor", "Governance", "Reports"])
check("Billing groups the existing cost views, history and budgets",
      'members: ["billing", "uptime", "tags", "history", "budgets"]' in navigation)
check("primary sections render in their declared order", "PRIMARY_SECTIONS.map(" in dash)

members = re.findall(r'members:\s*\[([^\]]*)\]', primary_navigation)
listed = {m.strip().strip('"') for group in members for m in group.split(",") if m.strip()}
# Every fixed tab must be reachable. Either it is filed under a group, or it is a retired id
# that redirects to one — anything in neither would silently fall through to Spend and sit among
# the service families, which is where it would never be found.
redirects = re.search(r"const MERGED_TABS = \{(.*?)\};", dash, re.S).group(1)
retired = set(re.findall(r"^\s*(\w+):\s*\"savings\",", redirects, re.M))
fixed = {"overview", "uptime", "tags", "resources", "savings", "waste", "rightsizing", "shutdown",
         "rates", "commitments", "advisor", "anomalies", "budgets", "history", "governance", "esu",
         "health", "report", "settings", "azure-updates", "billing", "changes", "performance", "audit"}
check("every fixed tab is either filed under a group or redirected",
      fixed <= (listed | retired), f"missing: {sorted(fixed - listed - retired)}")
check("and none is both", not (listed & retired), f"both: {sorted(listed & retired)}")
check("no tab is filed twice",
      sum(len([m for m in g.split(",") if m.strip()]) for g in members) == len(listed))
# The rail was cut from 16 entries to 11, and this exists to stop it creeping back one
# well-argued tab at a time. Cost by hour took it to 12: it is the only view that reads the
# hours behind the money, so it could not be folded into another tab the way the six Optimize
# entries were. Commitments took it to 13, and paid for the seat by taking AKS off the rail and
# into Overview and by removing the two duplicate renderings of itself. All resources took it to
# 14: every other tab answers "how much" at some aggregate, and this is the grain underneath
# all of them, so there was no existing tab it could have been a view of instead. Audit log took
# it to 15, but not the same way the others did: `loadTabs()` only ever lists it for someone
# `me.user.admin` is true for, so unlike every entry above it, this one is invisible to the
# population the ceiling below actually protects the experience of.
# Azure Updates adds one requested destination, consolidating announcements, status and health.
check("nineteen views are organized under seven primary destinations",
      len(listed) == 19 and len(groups) == 7)
check("the alternative grouped rail reuses available section views",
      "group.sections.flatMap(id => sectionViews(id, views))" in navigation)
check("the layout toggle is placed before the separate collapse control",
      dash.index('id="railLayout"') < dash.index('id="railMini"'))
check("Azure Updates has its own lazy renderer",
      '"azure-updates": ["updates", "renderAzureUpdates"]' in dash
      and 'import("./dashboard-updates.js")' in dash)
check("Billing is a named lazy primary destination",
      'billing: ["billing", "renderBilling"]' in dash
      and '{ id: "billing", label: "Billing"' in dash
      and 'members: ["billing", "uptime"' in navigation)

check("service families resolve to Overview", 'view.area ? PRIMARY_SECTIONS[0]' in navigation)
# The rail can be collapsed to icons, and the preference survives a reload: set on <html> by
# the pre-paint script (so the rail never renders wide and snaps narrow), then mirrored onto
# <body> once it exists, which is what the rest of the code and the stylesheet key off.
check("the icon-only rail preference is honoured on load",
      'document.body.classList.add("rail-mini")' in dash)
check("and it can be toggled from the rail itself",
      'classList.toggle("rail-mini"' in dash)
# Every drawChart call must pass an element. One passed a string and threw at runtime — on a tab
# most people open occasionally, and invisible to every static check until now.
check("drawChart is never handed a bare id",
      not re.search(r'drawChart\(\s*"', dash))

# Five tabs were folded into one, and AKS moved inside Overview. Anything still naming the old
# ids — a bookmark, a deep link from a finding, the AI analysis, or simply the tab someone was
# last on — has to land where the content went rather than falling back to Overview by accident.
check("retired tabs are redirected rather than dropped", "MERGED_TABS" in dash)
for old in ("waste", "rightsizing", "shutdown", "rates", "advisor"):
    check(f"{old} redirects to the merged list",
          bool(re.search(rf"{old}:\s*\"savings\"", redirects)))
check("aks is a destination again, not a redirect",
      not re.search(r"aks:\s*\"overview\"", redirects))
check("the redirect happens on selection", 'MERGED_TABS[id] || id' in dash)
check("and on restoring the remembered tab", "MERGED_TABS[current]" in dash)
check("the merged list has a renderer", "renderSavings" in dash)

# Commitments came back out of that merge, and this is the assertion that keeps it out.
# Everything under Savings is a cleanup somebody can do this afternoon and undo tomorrow; a
# reservation removes nothing, re-prices what stays, and runs for three years on a signature.
# Folded together, one Advisor recommendation rendered as a card, a table row and a share of the
# headline — and the "recoverable" total included money that needed a purchase order.
check("commitments is a tab again, not a redirect",
      not re.search(r"commitments:\s*\"savings\"", redirects))
check("and it is a Savings subsection",
      bool(re.search(r'id: "savings".*?members: \["savings", "commitments"\]', navigation, re.S)))
check("the Savings tab points at it rather than reprinting it",
      "function commitmentPointer" in dash and "commitmentsHtml" not in dash)
check("the server keeps commitments out of the savings totals",
      "commitment_count" in dash)
check("and the old third copy of it is gone", "renderRates" not in dash)

# Pay-as-you-go, savings plan and reserved instance, side by side. A saving on its own cannot be
# compared with anything — the comparison needs the annual cost under each model.
check("the comparison is drawn as columns, one per pricing model",
      "function comparisonPanel" in dash and 'class="cmp-grid"' in dash)
check("each column states what a year costs, not only what it saves",
      "m.annual_cost" in dash)
check("and the same three figures are drawn as bars",
      "function drawComparisonChart" in dash)

# AKS is one more way to slice the same spend, so it belongs in the segment strip beside All
# spend rather than on the rail. The thing that makes it different from the service families is
# that it *overlaps* them, and the strip must never read as parts that add up.
check("AKS has its own view", "function renderAksView" in dash)
check("reached from the area picker, not the rail",
      'aks: ["spend", "renderAksView"]' in dash)
check("it is an area, so the rail leaves it out and Overview stays selected",
      bool(re.search(r'id: "aks", label: "Kubernetes / AKS".*?area: true', dash, re.S)))
check("and it is not filed under any rail group", not re.search(r'"tags", "aks"', dash))
check("the overlap is marked rather than assumed", "overlay: true" in dash)
check("overlapping segments are kept out of the family list",
      's.area && !s.overlay' in dash)
check("and are drawn straight after All spend",
      bool(re.search(r'seg\("overview", "All spend".*?overlays\.map', dash, re.S)))
check("its figure comes from the warehouse, so an unreachable Resource Graph costs names "
      "rather than money", "hasSectionSpend(data.aks)" in dash
      and re.search(r"\{\s*\.\.\.data\.aks,\s*id:", dash) is not None)

# The seven service families were seven rail entries reaching one renderer with a different id.
# As segments above the numbers they are what they always were: one view with a filter.
check("service families are marked so the rail leaves them out", "area: true" in dash)
check("only explicit primary destinations render on the rail", "PRIMARY_SECTIONS.map(" in dash)
check("there is a picker to reach them by", "function areaPicker" in dash)
check("Overview stays selected while you are inside one",
      'sectionFor(current, sections)' in dash)
# renderRail and selectTab's fast path both decide what is marked, and they have to agree.
# When only the first knew the rule, the rail showed nothing selected on Compute & Web.
check("and the fast path agrees with the rail about it",
      'lastSectionView.set(sectionFor(id, sections).id, id)' in dash)
check("the picker is wired from the shared header",
      '$("viewActions").querySelectorAll(".ar-seg")' in dash
      and 'primary?.id === "overview" ? areaPicker(current)' in dash)
check("All spend is the first segment, not a separate destination",
      bool(re.search(r'seg\("overview",\s*"All spend"', dash)))
for cls in ("ar-picker", "ar-seg", "ar-name", "ar-cost"):
    check(f"styles .{cls}", f".{cls}" in css)

# "Custom export the cost data" wrote to blob storage and reported a name nobody could reach
# from a browser — correct data, entirely out of reach. It downloads now, through a real link
# for the same reason the report builder uses one: that is what makes the browser save a file
# natively rather than the page having to synthesise a click.
check("the selection can be downloaded", 'id="xDownload"' in dash)
check("through a real link, not a scripted click",
      bool(re.search(r'<a class="btn primary" id="xDownload"', dash)))
check("wired with the shared download helper",
      'wireDownloadLink(dl,' in dash and "/api/archive/export/download" in dash)
check("saving to storage is still offered", 'id="xExport"' in dash)
check("and both build their URL from one place", "function selectionQuery" in dash
      and dash.count("selectionQuery()") >= 2)
# A link has no :disabled, so an unusable selection has to lose its destination instead.
check("an unusable selection loses the link's destination",
      'dl.removeAttribute("href")' in dash)
check("and is marked unavailable to assistive tech",
      'dl.setAttribute("aria-disabled"' in dash)
# The label is typed, so it needs `input` — on `change` alone the URL kept whatever the label
# was when the tab rendered.
check("typing a label updates the download URL", "lbl.oninput = paintExportNote" in dash)

# The raw-row export sat on the schedules tab, which is where exports are *created in Azure* —
# not where someone wanting the rows would look. It belongs under the report builder, and the
# tab it left is now named for what it actually holds.
check("the schedules tab is named for its contents",
      '{ id: "settings", label: "Cost data"' in dash)
check("and no longer calls itself a settings page",
      'label: "Export settings"' not in dash)
check("the selection export is built by the Export data tab",
      dash.index('id="xDownload"') > dash.index("async function renderReport")
      and dash.index('id="xDownload"') < dash.index("async function renderSettings"))
check("and is wired from there",
      "wireSelectionExport();" in dash
      and dash.index("wireSelectionExport();") < dash.index("async function renderSettings"))
check("its wiring is its own function, not the schedules one",
      "function wireSelectionExport()" in dash)
check("the schedules tab no longer draws it",
      dash[dash.index("async function renderSettings"):dash.index("function wireSelectionExport")]
      .count('id="xSubs"') == 0)
# It is named for what it does — a custom cut of the cost data — rather than for the widget
# above it, which is what "the current selection" described.
# A menu item is a menu item because it says so, not because its data attribute was remembered
# in the stylesheet. Selecting `a[data-fmt]` silently excluded the raw-rows link, which carries
# `data-raw` — it rendered as a bare underlined browser link in the middle of the menu.
_index_html = read("../index.html")
_export_menu = _index_html.split('id="exportMenu"')[1].split("</div>")[0]
check("dropdown items are styled by role, not by data attribute",
      '.drop [role="menuitem"] {' in css
      and '.drop a[data-fmt] {' not in css)
check("every clickable in the export menu declares the role that styles it",
      _export_menu.count('role="menuitem"')
      == _export_menu.count("<a ") + _export_menu.count("<button "))

# The section says what it produces, rather than naming the widget above it.
check("the section says what it produces",
      "<h3>Custom cost export</h3>" in dash)
check("and drops the name that described a control instead",
      "Export the current selection" not in dash)
# Two jobs on one tab. Without a break the export card read as a third panel of the builder,
# with the Download report button stranded between them looking like it applied to both.
check("a rule separates the two halves of the tab", '<hr class="tab-split">' in dash)
check("the rule is placed after the builder's download bar, not before it",
      dash.index('<hr class="tab-split">') > dash.index('id="buildNote"'))
check("and the split is styled", ".tab-split {" in css)
# A separator the same weight as the hairlines inside the panels reads as another panel edge,
# not as a break between two halves of the page.
check("the rule is stronger than the borders it sits between",
      bool(re.search(r"\.tab-split \{[^}]*background: var\(--border-strong\)", css, re.S)))
check("the section break uses the compact spacing scale",
      bool(re.search(r"\.tab-split \{[^}]*margin: var\(--s4\) 0 var\(--s3\)", css, re.S)))
check("the panel that opens the section is marked as doing so",
      '<section class="card section-lead">' in dash and ".card.section-lead" in css)

# The create form is why someone opens Cost exports; the list is the record of having used it.
# With fourteen schedules the form sat below a screenful of cards.
_settings = dash[dash.index("async function renderSettings"):dash.index("function snapshotOptions")]
check("the create form comes before the list of schedules",
      _settings.index('id="sCreate"') < _settings.index('class="schedules"'))
check("and the list is labelled, now that it is no longer the first thing",
      '<h4 class="sub-head">Existing schedules</h4>' in dash and ".sub-head {" in css)
check("the label is a step down from the panel title, not another one",
      bool(re.search(r"\.sub-head \{[^}]*font-size: var\(--t-micro\)", css, re.S)))

# Azure reports a budget as one number with no history, so the meter can say 93% without
# answering what follows: a steady climb landing just inside, or a breach three weeks ago?
# The drawing is its own leaf module now that Hourly Billing draws it too for a tag selection
# that matches a budget; the shell binds it to its own chart and money formatting.
budget_chart = read("budget-chart.js")
check("each budget carries a chart of its own",
      "export function drawBudgetChart" in budget_chart
      and "function drawBudgetChart" in dash
      and "drawBudgetTrend(host, b, { ...options, drawChart, money })" in dash)
check("one implementation, reached by both tabs through the context",
      dash.count("function drawBudgetChart") == 1
      and '"./budget-chart.js"' in dash
      and "drawBudgetChart, drawChart, esc, fail" in dash)
check("drawn per budget only after expanding its details",
      'querySelectorAll("[data-budget-index]")' in dash
      and 'detail.querySelector(".budget-chart")' in dash
      and "if (detail.open) draw();" in dash)
check("cumulative, because a budget is a running total by definition",
      "data: t.cumulative" in budget_chart)
check("against a flat line at the limit, so the crossing point is the day it went over",
      "t.labels.map(() => amount)" in budget_chart and "dash: true" in budget_chart)
# The reference line is the backdrop, not a second measurement — same reasoning as the
# Overview's previous-period line.
check("the limit line is told apart by style rather than hue",
      "categorical: false" in budget_chart)
check("and the chart is styled as card content, not a panel inside a panel",
      ".budget-chart figure.chart" in css and "border: 0" in css)
# `.card` is content-visibility: auto so long tables are not laid out until scrolled to. The
# Hourly Billing budget panel is written after the page and measured immediately — Chart.js
# needs real dimensions — so it has to opt out or it paints as a blank box.
check("the tag-selected budget panel opts out of skipped layout",
      bool(re.search(r"\.uptime-budget \{[^}]*content-visibility: visible", css, re.S)))
# A tag-scoped budget drawn over whole-subscription spend would contradict its own headline:
# one budget here tracks $14 while its subscription spends $466.
check("a filter the local data cannot reproduce is admitted, not drawn over",
      "t?.filtered_exactly === false" in budget_chart
      and "Trend comparison unavailable" in budget_chart
      and budget_chart.index("t?.filtered_exactly === false")
      < budget_chart.index("drawChart(host"))
check("and a materially different total is named rather than smoothed away",
      "gap > 0.15" in budget_chart)

# Ten budgets is more than a screen and a large estate has dozens, so the tab opens on a
# question — which ones need me, or the one called X — rather than on a scroll.
check("budgets can be filtered by state", "data-bstatus" in dash)
check("and found by name, subscription or what they track",
      "function paintBudgetList" in dash
      and "filterBudgets(rows, budgetFilter)" in dash
      and "[b.name, b.subscription, b.subscription_id, b.filter]" in dash)
check("filtering redraws rather than refetching the live Azure call",
      dash.index("function paintBudgetList") > dash.index("async function renderBudgets")
      and "paintBudgetList(rows)" in dash)
check("the filter survives leaving the tab and coming back",
      "let budgetFilter = {" in dash)
check("an empty result says so instead of showing a blank list",
      "No budget matches" in dash)
# Cards are indexed into the unfiltered list, or a filtered view would draw the wrong budget's
# chart under the right budget's name.
check("a filtered card still finds its own chart",
      "rows.indexOf(b)" in dash)
check("the toolbar is styled and wraps rather than overflowing",
      ".budget-tools {" in css and "flex-wrap: wrap" in css)

# A tag budget is created from the Tags tab; this is the return leg of that journey.
check("a tag budget links back to the selection behind it",
      "data-tagsfor" in dash and "function showTagsFor" in dash)
check("and the Tags tab applies it case-insensitively, as Azure filters",
      "pendingTagSelection" in dash and "lower.get(String(rawKey).toLowerCase())" in dash)
check("arriving from a budget is explained rather than silently pre-selected",
      "tagState?.fromBudget" in dash and "Showing the tags" in dash)
# The two tabs count differently and both are right: Tags attributes a resource's whole cost
# to its tags, a budget counts only the charges carrying them. A $14 budget landing on a $44
# page with no explanation is the kind of thing that derails a demo.
check("and the difference in what each counts is stated",
      "attributes a resource's whole cost" in dash)
# The Refresh menu used to promise "Seconds, not minutes" for the quick path. It is not: the
# Query API is rate-limited hard enough that a busy day still takes minutes, and a control that
# lies about its own cost is worse than a slow one.
check("the quick option no longer promises seconds",
      "Seconds, not minutes" not in dash)
check("the quick option states its time bound and evidence tradeoff",
      "capped at about a minute" in dash and "Fewer columns" in dash)
check("the export path leads, being the only one with no report to build and no quota",
      dash.index('data-act="focus"') < dash.index('data-act="quick"'))
check("and the full-detail options are honest about ten minutes",
      "ten minutes or more" in dash)

# The button doubles as the freshness indicator, and after a refresh it kept yesterday's date
# until the next full page load — so a refresh that had plainly worked looked like it had not.
check("a finished refresh updates the date on the button",
      "if (w.to) lastAsOf = w.to;" in dash)

# Every route to fresh data is slow in its own way, and none of it needs watching — the data
# changes once a day, overnight. So it reloads itself.
_main = open(os.path.join(HERE, "app", "main.py"), encoding="utf-8").read()
check("the server hands over the parsed filter for that link",
      'b["filter_exact"] = exact' in _main
      and 'b["tag_filter"] = [[k, v] for k, v in tags or []] if exact else []' in _main)
check("the warehouse reloads itself on a schedule",
      "async def _daily_refresh" in _main)
check("started with the app and stopped with it",
      "asyncio.create_task(_daily_refresh()" in _main
      and "task.cancel()" in _main and "await asyncio.gather(*pending" in _main)
check("at an hour after Azure writes its exports",
      'AUTO_REFRESH_HOUR' in _main)
check("and it can be turned off",
      'os.getenv("AUTO_REFRESH", "true")' in _main)
check("it prefers the export, which touches no rate limit",
      "_refresh_primary, subs" in _main
      and re.search(r'_load_export_pair\(\s*ok,\s*scope=scope,\s*required_metric="FocusCost"', _main))
check("and it has somewhere to read from",
      "await discover_exports(subscription_ids=scope, deployment_only=True)" in _main
      and "ingest_export(max_files=60)" not in _main)
check("it never fights a refresh someone is watching",
      "an ingest is already running" in _main)
check("nor repeats work the warehouse already holds",
      "warehouse already holds" in _main)
check("and a bad night does not stop the next one",
      "must not stop tomorrow" in _main)

# renderReport used to claim every checkbox on the tab. The moved section brings its own, which
# carry no data-group -- so an unscoped selector would both steal their handler and index the
# report selection with `undefined`.
check("the report builder only claims its own checkboxes",
      'querySelectorAll("input[data-group]")' in dash
      and 'querySelectorAll("input[type=checkbox]")' not in dash)
# The section needs what the warehouse holds and which snapshots exist; fetching those after the
# builder's options would add a round trip to a tab that already waits on one.
check("the data it needs is fetched alongside the builder's options",
      bool(re.search(r"const \[opts, arch, wh\] = await Promise\.all", dash)))
check("the schedules tab fetches its data and permissions together",
      "scheduleSubscriptions(sched)" in dash and "sub.can_write === true" in dash)
# An anchor styled as a button has to stay identical to one, so it joins the same rules rather
# than copying them.
check("a.btn shares the button rules rather than duplicating them",
      "button, a.btn {" in css)
check("including the primary treatment", "button.primary, a.btn.primary {" in css)
check("and has a disabled state, which :disabled cannot give a link",
      "a.btn.disabled" in css)
# Navigation moved back from two tiers across the top to a 208px column down the left. A
# horizontal strip put the tab-shaped area picker directly beneath two more rows that also
# looked like tabs, and three stacked bands of the same flat, bold-label language read as
# three tiers of navigation rather than two tabs and a filter underneath them. The hierarchy
# is unchanged — same five groups, same order, same members in the order each declares, and
# the checks above still hold it to that. What changed is the axis, back to what it was.
check("obsolete accordion state cannot hide primary navigation", "openGroups" not in dash)
check("main sections remember their last accessible view", "sectionDestination(id, sections, lastSectionView.get(id))" in dash)
check("subsections live outside the cached view body", 'class="subsection-nav"' in dash)
check("the whole rail can also collapse to icons",
      "function setRailMini(on)" in dash)
check("and that preference is remembered", 'writeStore("railMini", on);' in dash)
check("the active parent stays selected", 'section.id === primary?.id' in dash)
# Overview is the sum of the families listed beneath it, so adding the group up reports twice
# the estate's spend — which it did, until it was measured against the API.
check("navigation never sums overlapping area figures", 'roll.cost' not in dash)
# Keyboard nav reads the DOM rather than group state, because a closed group's items are not
# on screen even though they still exist in the markup.
check("both levels have keyboard navigation", dash.count("keyboardDestination(") >= 2)
check("subsections report selection", 'aria-selected="${section.id === current}"' in dash)
check("both levels identify their content", 'aria-controls="tabBody"' in dash)

# The rail is back, not merely styled to look like one — the rules that actually draw it
# have to exist, or the grid column has nothing in it.
check("primary and subsection rules exist", ".rail-item" in css and ".subsection-tab" in css)
check("with its own width token", "--nav-w:" in css)
check("and the icon-only collapse mode", "rail-mini" in css)
check("and the shell gives it a real column",
      "grid-template-columns: var(--nav-w) minmax(0, 1fr);" in css)
# The agent panel covers the board instead of flanking it, so it costs nothing until it is asked
# for. Off-screen it must also be unreachable — a panel that keeps its tab stops while invisible
# is worse than one that is simply gone.
check("the agent panel is an overlay", ".side {" in css and "position: fixed;" in css)
check("and is inert when closed", "body.side-closed .side" in css and "visibility: hidden" in css)
check("closed is the default", 'readStore("costAskOpen", false)' in dash)

# The filter chips and the data-freshness line used to be two full-width rows; they now share
# one, with the freshness line pinned to the far end regardless of how wide the chips are.
check("the filter row and the data-freshness line share one wrapper",
      ".statusrow {" in css)
check("the wrapper only appears when it has something to show",
      ":has(#filterBar:not([hidden]))" in css and ":has(#dataState:not([hidden]))" in css)
check("the freshness line is pinned to the far end, not among the chips",
      re.search(r"\.datastate\s*\{[^}]*margin-left:\s*auto", css) is not None)
check("hiding the freshness line still hides it once nested in the shared row",
      ".datastate[hidden] { display: none; }" in css)



# ---------------------------------------------------------------- chart colour
print("\napp.js — telling series apart")

app = read("app.js")

# The ordered blue ramp is right for slices ranked by cost and wrong for named categories:
# "On demand", "Committed" and "Spot" came out as three barely distinguishable blues, which is
# a legend you decode rather than read.
check("there is a categorical palette as well as an ordered ramp",
      "CATEGORICAL_DARK" in app and "CATEGORICAL_LIGHT" in app)
check("it is used when a chart has several series", "spec.categorical ?? manySeries" in app)
check("blue and amber lead it — the pair that survives colour vision deficiency",
      '"#479EF5", "#FFB900"' in app)
# Colour alone is never the answer: shapes carry the same information for anyone who cannot
# separate the hues, and for anything printed in grey.
check("series also carry a marker shape", "POINT_STYLES" in app)
check("the legend shows each series' own shape", "isPie ? { pointStyle" in app)
# A category must be the same colour in every chart it appears in, or two views of the same
# three things disagree.
check("shared categories have a fixed colour slot", "CATEGORY_SLOT" in app)
check("Spot is the same colour wherever it appears", '["spot", 2]' in app)
# The ordered ramp's neighbours are nearly identical — correct for ranked slices, useless for
# two separate lines.
check("multi-series ordered charts spread across the ramp", "const BAND = 5" in app)
check("and stay inside the legible half of it",
      "Math.round((index * BAND)" in app)
# Drawing and re-tinting used to choose colours by different rules, so a theme switch turned a
# legible chart into two adjacent shades of one blue.
check("one function decides every series colour", "function seriesColour" in app)
check("drawChart tolerates being handed an id", 'typeof container === "string"' in app)
check("the re-tint uses it too", "categorical: isCat, isPie: isPieChart" in app)
check("and remembers what the chart was", "chart.$clCategorical = useCategorical" in app)


print("\ndashboard.js — performance contracts")

filters = read("filters.js")
check("opening data starts alongside navigation",
      re.search(r"executiveData\(\s*(?:ctx\.)?tabsController\??\.signal\s*\)", dash) is not None)
check("secondary filters wait for the dashboard", '"cl:dashboard-ready"' in filters)
check("duplicate filter requests share one promise", "pendingKey === requestKey" in filters)
check("superseded filter requests are aborted", "requestController?.abort()" in filters)
check("filter changes are debounced", "changeTimer = setTimeout(onChange, 180)" in filters)
check("filter options render only when opened", '<div class="fb-list"></div>' in filters)
check("refreshes preserve the rendered dashboard", "loadTabs({ preserve: true })" in dash)
check("large tables page instead of rendering every row", ".slice(start, start + t.pageSize)" in dash)
check("table search is debounced", "}, 120);" in dash)
check("table views are memoized", "state.viewCache?.key === cacheKey" in dash)
check("currency formatters are reused", "const numberFormats = new Map()" in app)
check("off-screen charts wait for visibility", "await whenVisible(card)" in app)
check("the live savings scan waits for dashboard readiness", "scheduleSavingsTile(cur)" in app)


print("\ndashboard.js — comparison charts name both dates")

# One date above two numbers reads as though both were measured on it. They were not: the
# second is the same day of the previous window.
check("the trend chart passes per-point dates", "pointLabels: labels" in dash)
check("including the previous window's own dates", "previous_labels" in dash)
check("the tooltip prints them", "c.dataset.pointLabels?.[c.dataIndex]" in app)
check("the comparison keeps the ordered ramp rather than a rival hue",
      "categorical: false" in dash)
check("pricing models are marked categorical", "categorical: true" in dash)

py = open(os.path.join(HERE, "app", "dashboard.py"), encoding="utf-8").read()
check("the server sends the previous window's dates", '"previous_labels"' in py)


print("\nnew Overview visuals — calendar heatmap and cost bridge")

heatcal = read("calendarheat.js")
check("the heatmap is its own module", "export function drawCalendarHeatmap" in heatcal)
check("a day with no data is a distinct class from a real zero-cost day",
      'class="heat-cell heat-none"' in heatcal
      and 'heat-cell heat-${level}' in heatcal
      and "const level = has ?" in heatcal)
# The calendar answers "which days"; a square that colours itself expensive and cannot then say
# what it was compared against only raises the question. Clicking opens the day on the line
# chart above rather than drawing a second panel that could disagree about the same date.
check("a real day is a button, a blank one is not",
      "data-heat-day" in heatcal and "opts.onPick" in heatcal
      and 'class="heat-cell heat-none"' in heatcal.split("if (!has)")[1].split("}")[0] + "}")
check("the day opens on the chart that already owns it, not in a rival panel",
      "chart.$clSelectPoint" in app and "$clSelectPoint" in dash
      and "function openTrendDay" in dash)
check("intensity is bucketed against the busiest day in the window, not a fixed scale",
      "Math.round((value / max) * LEVELS)" in heatcal)
check("weeks fill as columns, not rows",
      "grid-auto-flow: column" in read("app.css"))

waterfall = read("waterfall.js")
check("the chart is its own module", "export function drawWaterfall" in waterfall)
check("the top movers alone are reconciled against an 'other services' remainder",
      "otherDelta = currentTotal - previousTotal - namedDelta" in waterfall)
check("colour follows the page's cost-change convention, not a generic red/green",
      "const up = m.delta > 0" in waterfall and 'wf-${up ? "up" : "down"}' in waterfall)

css = read("app.css")
check("standalone chart cards cannot shrink and clip the zero baseline",
      re.search(r"\.board-body\s*>\s*\.chart-card\s*\{\s*flex:\s*none\s*;", css) is not None)
for cls in (".heatcal", ".heat-cell", ".wf-rows", ".wf-row-bar", ".wf-up", ".wf-down"):
    check(f"{cls} is styled", cls in css)

check("Overview wires the heatmap in", "function renderSpendHeatmap" in dash)
check("Overview wires the bridge in", "function renderSpendWaterfall" in dash)
check("both are lazy, like the treemap and region map beside them",
      'const host = $("spendHeatmap");' in dash and 'const host = $("spendWaterfall");' in dash
      and dash.count("whenNear(host, async () => {") >= 3)


print("\nDaily spend + What changed — sharing a row instead of the calendar sitting on its own")

check("the calendar and the bridge are paired in one row, not stacked full-width cards",
      '<div class="grid-bridge">' in dash
      and re.search(r'grid-bridge">\s*<div id="spendHeatmap">', dash) is not None)
check("the row gets its own grid, capped to the calendar's real size rather than an even split",
      ".grid-bridge {" in css and "minmax(220px, 280px) 1fr" in css)

print("\nWaterfall — a grouped list instead of floating columns")

check("every service is its own row, label beside the bar rather than rotated below it",
      "const row = (m) => {" in waterfall and "wf-row-label" in waterfall)
check("rows are split by sign, not left interleaved in absolute-magnitude order",
      "const ups = moves.filter((m) => m.delta > 0)" in waterfall
      and "const downs = moves.filter((m) => m.delta <= 0).reverse()" in waterfall)
check("a shared scale sizes every bar by its own delta, not by the (much larger) period total",
      "const maxAbs = Math.max(0.01, ...moves.map((m) => Math.abs(m.delta)))" in waterfall)
check("the divider between the two groups only appears when both groups are non-empty",
      "const divider = ups.length && downs.length" in waterfall)
check("a swing too small to size proportionally still gets a visible minimum-width bar",
      "min-width: 5px" in css.split(".wf-row-bar {")[1].split("}")[0])
check("the label column is one shared width the divider re-uses to line up with the bars",
      "--wf-label-w: 150px" in css and "calc(var(--wf-label-w)" in css)
check("labels truncate with an ellipsis instead of colliding with a neighbour, full name on hover",
      "text-overflow: ellipsis" in css.split(".wf-row-label {")[1].split("}")[0]
      and ('title="${esc(m.label)}' in waterfall
           or 'data-tooltip="${esc(m.label)}"' in waterfall))
# Seeing "Azure AI Search fell $141" and clicking it is one gesture; finding the same service
# among forty in a dropdown is a search. The remainder row is not a service, so it stays inert
# rather than offering a click that would narrow the dashboard to nothing.
check("a named mover filters the dashboard, the remainder row does not",
      "data-wf-service" in waterfall and "m.pick" in waterfall
      and 'pick: false' in waterfall and 'pick: true' in waterfall)
check("the narrowing goes through the filter bar, so Clear all can undo it",
      "export function applyFilter" in read("filters.js")
      and 'applyFilter("service"' in dash)

print("\nDashed reference lines — a backdrop, not a competing series")

check("a dashed dataset gets the app's amber, not a ramp slot, on first paint",
      'ds.dash\n      ? token("--warn", "#FFB900")' in app)
check("...and keeps it through a theme switch, detected the same way Chart.js remembers dashes",
      'Array.isArray(ds.borderDash)\n          ? token("--warn", "#FFB900")' in app)



print("\nBudgets — the portfolio rollup")

check("one bar per budget, worst first", "function budgetPortfolioMarkup" in dash)
check("skipped entirely for a single budget, where a shape says nothing a number does not",
      "if (rows.length < 2) return" in dash)
check("clicking a row clears whatever filter is hiding its card",
      'budgetFilter = { status: "all", q: "" }' in dash)
check("and scrolls to it rather than only naming it",
      'card.scrollIntoView({ behavior: "smooth", block: "center" })' in dash)
check("the burst-bar convention is reused, not reinvented",
      css.count(".bp-bar.bmeter") == 1)


print("\nCost by Application / Tags — chargeback view")

check("explorer and chargeback are independent view state, not one filter of the other",
      "let tagViewMode" in dash and "let chargebackState" in dash)
check("the allocation key defaults to the costliest one in the estate, not a hardcoded field",
      "d.keys[0]?.key" in dash or 'chargebackState = { sig, key: d.keys[0]?.key' in dash)
check("a resource missing the chosen key is counted, not dropped",
      "unallocCost += r.cost;" in dash)
check("switching keys re-aggregates in memory rather than refetching",
      "function chargebackBuckets" in dash and "d.resources || []" in dash)
check("Unallocated is visually distinct, not just another row",
      ".chargeback-unalloc" in css)


print("\nGovernance — empty resource groups")

gov = open(os.path.join(HERE, "app", "governance.py"), encoding="utf-8").read()
check("resource groups are found by an anti-join against Resources, not a filter on Resources",
      "ORPHANED_RG_QUERY" in gov and "resourceCount) or resourceCount == 0" in gov)
check("the join key is lower-cased on both sides against the same casing defence ACCEL_QUERY needs",
      gov.count("tolower(name)") >= 1 and gov.count("tolower(resourceGroup)") >= 1)
check("the function is exported for main.py to gather alongside the other two checks",
      "async def orphaned_resource_groups" in gov)

main_py = open(os.path.join(HERE, "app", "main.py"), encoding="utf-8").read()
check("the governance route gathers all three checks independently",
      "orphaned_resource_groups(subscription_ids=picked or None)" in main_py
      and main_py.count("return_exceptions=True") >= 1)
check("a failure in one check cannot lose the other two", '"orphaned_resource_groups": unwrap(' in main_py)
check("the tab renders a third card alongside tagging and accelerated networking",
      "const rg = d.orphaned_resource_groups" in dash)


print("\nAll resources — every billed resource at the grain a bill actually names")

# Every other tab answers "how much" at some aggregate; this one is the grain underneath all of
# them, so it is the one place a regression would be easy to miss in the others' own checks.
res = open(os.path.join(HERE, "app", "resources.py"), encoding="utf-8").read()
check("it reads the warehouse, not a live Resource Graph scan — same speed guarantee as tags",
      "from .warehouse import warehouse" in res and "_arg(" not in res)
check("the tab has a named ceiling rather than an unbounded response, like tags' own",
      "MAX_RESOURCES" in res)
inventory = next(node for node in ast.walk(ast.parse(res))
                 if isinstance(node, ast.FunctionDef) and node.name == "inventory")
resource_queries = [
    node for node in ast.walk(inventory)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    and isinstance(node.func.value, ast.Name) and node.func.value.id == "r" and node.func.attr == "rows"
]
resource_sql = ast.get_source_segment(res, resource_queries[0].args[0]) if resource_queries else ""
check("cost and its prior-period figure come from one pass over the rows, not two queries",
      len(resource_queries) == 1 and resource_sql.count("FROM costs") == 1
      and re.search(r'SUM\(CASE WHEN \{period\} THEN "BilledCost" ELSE 0 END\)\s+AS cost', resource_sql)
      and re.search(r'SUM\(CASE WHEN \{previous\} THEN "BilledCost" ELSE 0 END\)\s+AS prior_cost', resource_sql))
check("first/last seen are bounded to the same combined window, not a full-history scan",
      '"ChargePeriodStart" > {prev} AND "ChargePeriodStart" <= DATE' in res
      and 'FROM costs WHERE {combined}' in res
      and 'MIN("ChargePeriodStart")' in res and 'MAX("ChargePeriodStart")' in res)
check("the cost-area label reuses the taxonomy every other tab already uses",
      "from .dashboard import _group_for_family" in res)
check("tags are parsed with the one parser every other tab trusts, not a second implementation",
      "from .tags import _pairs" in res)
check("a resource with no charge in the window cannot read as zero rather than absent",
      "change_pct" in res and "if prior else None" in res)
check("it is a singleton getter, matching commitments.py's own pattern",
      "def get_resources() -> Resources" in res)

check("the route exists and is offloaded off the event loop like every other warehouse read",
      '@app.get("/api/dashboard/resources")' in main_py
      and "await offload(\n        get_resources().inventory" in main_py)
# tags.py's own route does not do this — noted as a gap when this tab was designed, and one
# this tab was built not to repeat.
check("unlike Cost by Tag, this tab actually respects the global filter bar",
      "filters=request_filters(request)" in main_py
      and main_py.index('@app.get("/api/dashboard/resources")') <
          main_py.index("class TagFilter"))

check("the tab is registered as warehouse-only, not live — it must render in milliseconds",
      bool(re.search(r'\{ id: "resources", label: "[^"]+", analysis: true \}', dash)))
check("and is not accidentally also marked live",
      not re.search(r'\{ id: "resources"[^}]*live: true', dash))
check("All resources has its own primary destination",
      'label: "All resources", icon: "resources", members: ["resources"]' in navigation)
check("the renderer is dispatched from the tab switch, not left unreachable",
      'resources: ["resources", "renderResources"]' in dash
      and "await tabs[renderer](mine, tab)" in dash)
check("it is cached like every other analysis tab, not refetched on every tab switch",
      'cached(key("resources")' in dash)
# A net-total footer for this column was tried and dropped: summed across the whole, unfiltered
# estate it mixes organic drift with whatever got decommissioned or onboarded mid-window into
# one number that means nothing coherent, unlike changeCol's own total elsewhere on a narrower,
# homogeneous slice. This pins the removal so it is not quietly re-added the same way.
_resource_source = read("dashboard-resources.js")
_resources_col = _resource_source[_resource_source.index('label: "Change"'):_resource_source.index('label: "Tags"')]
check("the per-resource change has no misleading estate-wide net total",
      "total:" not in _resources_col)
# The tab is the only thing on it, so its table gets its own capped inner scroll on top of the
# tab body's own scroll — two scrollbars for one list. `scroll: false` opts a table out of the
# height cap so the tab's own scroll is the only one.
check("table() can opt a table out of its own capped scroll",
      'const boxed = opts.scroll !== false;' in dash
      and '"table-wrap${boxed ? "" : " flush"}"' in dash)
check("and the resources table actually asks for that",
      "scroll: false" in _resource_source)
check("the opt-out only cancels the height cap, not the horizontal scroll wide rows still need",
      ".table-wrap.flush { max-height: none; }" in css)


print("\nMarketplace & ISV — a cost-area overlay, not a family")

# Same shape the "All resources" checks above use: read the module's own source rather than
# exercise a live warehouse, since the interesting facts here are structural (is this wired the
# way AKS already proved out) rather than numeric.
dash_py = open(os.path.join(HERE, "app", "dashboard.py"), encoding="utf-8").read()

check("the predicate is PublisherName, not a guess dressed up as one",
      'MARKETPLACE_CLAUSE = (' in dash_py and '"PublisherName"' in dash_py)
check("Microsoft's own rows are excluded alongside null/empty, not just null/empty",
      "NOT IN ('microsoft', 'microsoft corporation')" in dash_py)

check("sections() computes it on the same connection as AKS, not a second round trip",
      shares_reader(dash_py, "sections", "aks_rows", "marketplace_rows"))
check("and returns it in the same currency-separated overlay shape as AKS",
      bool(re.search(r'"marketplace":\s*\{"cost":\s*market_total,\s*"currency":\s*'
                      r'market_currency,\s*"cost_by_currency":\s*self\._money_totals\(marketplace_rows\),'
                      r'\s*"overlaps": True\}', dash_py)))

check("section() recognises it without needing a GROUPS entry, like \"other\" does",
      'section_id not in ("other", "marketplace")' in dash_py)
check("and gives it the same six queries every family tab gets, just a different WHERE",
      'where = MARKETPLACE_CLAUSE' in dash_py)

check("the picker shows it as a second overlay segment beside Kubernetes / AKS, not a family",
      bool(re.search(r'hasSectionSpend\(data\.aks\).*?hasSectionSpend\(data\.marketplace\)', dash, re.S))
      and re.search(r"\{\s*\.\.\.data\.marketplace,\s*id:", dash) is not None)
check("carries area+overlay so areaPicker() and the rail treat it exactly like AKS",
      bool(re.search(r'id: "marketplace"[^}]*area: true, overlay: true', dash)))
check("it is not a rail destination — no group lists it in members",
      not re.search(r'members:\s*\[[^\]]*"marketplace"', dash))
check("falls through to the generic per-family renderer rather than a dedicated one",
      'if (current === "marketplace")' not in dash)
check("the heuristic is disclosed on the tab itself, not left to be discovered",
      'd.section === "marketplace"' in dash and re.search(r"billing[- ]publisher", dash, re.I))


print("\nCommitments — which resources a benefit actually covers, not just how many")

com_py = open(os.path.join(HERE, "app", "commitments.py"), encoding="utf-8").read()

check("the drill-down rides the same connection inventory() already opens, not a second query round trip",
      shares_reader(com_py, "inventory", "services", "resource_rows"))
check("capped per commitment, like services already are, so one plan cannot become the whole table",
      "MAX_RESOURCES_SHOWN = 8" in com_py
      and "len(bucket) < MAX_RESOURCES_SHOWN" in com_py)
check("ranked by what each resource costs against the benefit, not left in scan order",
      'GROUP BY 1, 2, 3 ORDER BY cost DESC' in com_py)
check("the count left out is reported rather than a silent truncation",
      '"resources_more": max(0, resource_count - len(shown))' in com_py)
check("an idle commitment's drill-down cannot KeyError on there being nothing to show",
      "resources_by_id.get(key, [])" in com_py)

check("the table shows it in the same Resources column, not a new one to scan for",
      'cell: resourcesCell' in dash)
check("collapsed to a bare count until asked, same disclosure language as .an-how elsewhere",
      '<details class="cmt-drill">' in dash)
check("each row names the resource group beside the resource, not just a bare name",
      "x.resource_group" in dash)
check("sorting the column still sorts by the real count, not by the HTML string",
      bool(re.search(r'label: "Resources",\s*\n\s*num: true,\s*\n\s*value: \(r\) => r\.resources',
                      dash)))
check("the cell's own ellipsis is lifted only while that row's drill-down is open",
      ".cmt-drill[open]" in css and ":has(.cmt-drill[open])" in css)


print("\nAudit log — who looked at what, admin-only")

main_py = open(os.path.join(HERE, "app", "main.py"), encoding="utf-8").read()
wh_py = open(os.path.join(HERE, "app", "warehouse.py"), encoding="utf-8").read()

check("the table is created the same idempotent way as ingest_log and meta",
      'CREATE TABLE IF NOT EXISTS access_log' in wh_py)
check("a write is fired from the auth middleware, not from a page that could be client-cached",
      main_py.index("class RequireAuth") < main_py.index("_log_access(request.state.user")
      < main_py.index("app.add_middleware(RequireAuth)"))
check("sections is explicitly excluded, since it repaints the rail on every navigation and "
      "is not itself a view of anything",
      'path != "/api/dashboard/sections"' in main_py)
check("the write is fire-and-forget so a slow or failing log entry cannot slow a tab down",
      "asyncio.create_task(" in main_py.split("def _log_access")[1].split("class RequireAuth")[0])
check("the task is kept alive rather than risking mid-write garbage collection, like the "
      "currency warm-up at startup already does for the same reason",
      "_audit_tasks.add(task)" in main_py and "_audit_tasks.discard" in main_py)

check("the read route is admin-only", 'require_admin(request)' in main_py.split(
      '@app.get("/api/dashboard/audit")')[1].split("@app.get")[0])
check("it is not narrowed to the caller's own subscriptions like every other tab, on purpose",
      "not scoped by subscription" in main_py.lower())

check("the tab is only ever listed for an admin, and left off the rail entirely for anyone else",
      bool(re.search(r'me\?\.user\?\.admin\s*\?\s*\[\{ id: "audit"', dash)))
check("filed under Govern, beside the other lifecycle-and-compliance tabs",
      bool(re.search(r'members:\s*\["governance",\s*"esu",\s*"health",\s*"audit"\]', dash)))
check("the renderer is dispatched from the tab switch, not left unreachable",
      'audit: ["monitor", "renderAuditLog"]' in dash)
check("it has its own rail icon rather than falling back to the placeholder dot",
      "audit: `<path" in open(os.path.join(ASSETS, "icons.js"), encoding="utf-8").read())


print("\nAI spend recommendations — a narrative over the same data, not a third scan")

rec_py = open(os.path.join(HERE, "app", "recommendations.py"), encoding="utf-8").read()

check("the anti-hallucination rule is explicit in the prompt, not left implied",
      "must appear" in rec_py and "verbatim" in rec_py)
check("provenance detail is trimmed before the model sees it -- sources/items are for a "
      "person looking at the row already, not something a summary should quote back",
      '"sources"' not in rec_py.split("_trim_opportunities")[1].split("def _trim_anomalies")[0])
check("a failed model call is swallowed into a result, not raised into the route",
      "except Exception as exc:" in rec_py and "synthesis failed" in rec_py)
check("an empty scope is said plainly rather than spending a model call on nothing",
      'if not opportunities and not' in rec_py)

check("the recommendations route reuses the same cached scan the Savings tab reads, "
      "rather than repeating six live Azure calls",
      "_get_savings(user, picked, window, currency)" in main_py
      and main_py.count("_get_savings(user, picked, window, currency)") >= 2)
check("anomalies are read fresh (a warehouse-only read, not a live scan) rather than cached",
      bool(re.search(r'offload\(anomalies_mod\.analyse,\s*picked or None', main_py)))
check("a failed gather of source data cannot look like an outage",
      'except Exception as exc:  # noqa: BLE001 - a failed read must not break the summary card'
      in main_py)

check("the card is filled in after the ranked list, not blocking it",
      "loadRecommendation();" in dash
      and "paintSavings(data, token);" in dash
      and 'data.scan?.status !== "running"' in dash
      and "await loadRecommendation()" not in dash)
check("late recommendations update only their retained card, never a replacement view",
      bool(re.search(r'async function loadRecommendation\(\).*?const body = \$\("recBody"\);.*?'
                      r'if \(!body\?\.isConnected\) return;', dash, re.S)))
check("a real narrative loses the placeholder's muted/note styling",
      'body.classList.remove("muted", "note")' in dash)


print("\nWhat-if — a resize or shutdown priced on request, before doing either")

wf_py = open(os.path.join(HERE, "app", "whatif.py"), encoding="utf-8").read()

check("errors are a class a route can catch by type, not a bare ValueError every other "
      "bad-input path might also raise",
      "class WhatIfError(ValueError):" in wf_py)
check("Spot and Low Priority rows are excluded, not just the plain on-demand row picked first "
      "-- the catalogue lists the cheaper ones before it",
      '"spot" not in str(it.get("meterName"' in wf_py
      and '"low priority" not in str(it.get("meterName"' in wf_py)
check("Windows is told apart from Linux by productName, not meterName -- the two are identical "
      "for a Windows and a Linux row of the same size",
      'is_windows = "windows" in str(it.get("productName"' in wf_py)
check("the saving compares today's real bill against the target, not one retail rate against "
      "another -- current_retail exists to show the gap, not to replace billed in the sum",
      bool(re.search(r'"annual_saving":\s*\(\s*\n\s*round\(\(annualise\(billed_daily\) or 0\) '
                      r'- \(annualise\(target_daily\) or 0\)', wf_py)))
check("a resize names the discount it might be hiding; a shutdown names the disks that keep "
      "billing -- the two caveats are not interchangeable",
      # Split across the source's own adjacent string literals, so this checks for the two
      # halves rather than a phrase that only exists once Python joins them at parse time.
      "and any Hybrid " in wf_py
      and "Benefit or reservation discount the current VM may already carry" in wf_py
      and "its disks keep billing until they are deleted too" in wf_py)
check("a resource outside the caller's own subscriptions is refused before Resource Graph is "
      "ever asked, the same boundary every other live tab holds",
      "This resource is not in a subscription you can access." in wf_py)
check("an unpriced target SKU is refused rather than silently priced at $0, which a missing "
      "retail row would otherwise look exactly like",
      "No retail price found for" in wf_py)

check("the route is a POST, not a GET -- a typed SKU guess needs the server's real sentence "
      "back, which only post()'s error handling keeps and get()'s discards",
      '@app.post("/api/dashboard/whatif")' in main_py)
check("resource_id and target_sku come from the request body, not the query string",
      bool(re.search(r'async def dashboard_whatif\(request: Request.*?'
                      r'body = await request\.json\(\)', main_py, re.S)))
check("a bad scenario is a 400 with the model's own sentence, not a generic 500",
      "except WhatIfError as exc:\n        raise HTTPException(400, str(exc)) from exc" in main_py)

check("every VM row carries an id to hang the control off of, not just the fields the metrics "
      "join needed for itself",
      bool(re.search(r'"id":\s*\w', open(os.path.join(HERE, "app", "waste.py"),
                                          encoding="utf-8").read())))

sav_py = open(os.path.join(HERE, "app", "savings.py"), encoding="utf-8").read()
check("the utilisation scan's own ARM id reaches the merged Opportunity, not just the name and "
      "group used to key it against the other scans -- a control that prices a real VM needs a "
      "real id, and the row already carries one",
      'resource=name or "", resource_id=vm.get("id") or "", resource_group=group or "",\n'
      '            subscription=sub,' in sav_py and 'sub = _source_subscription(vm)' in sav_py)

check("the Rightsizing table's own cell still gets a Scenario column -- unreachable like its "
      "waste/shutdown/advisor siblings after the Optimize-tab merge folded it into Savings, but "
      "left in place rather than torn out, the same call this codebase already made for those",
      '{ label: "Scenario", cell: (r) => whatIfCell(r) }' in dash)
check("the reachable path is a Savings opportunity card, not the tab MERGED_TABS quietly "
      "retired -- an idle-category card gets the same control, keyed off its own resource_id",
      'const whatIf = o.category === "idle" ? whatIfMarkup(o.resource_id) : "";' in dash
      and "${whatIf}" in dash)
check("a VM the orphan half of this same scan already calls stopped is excluded -- comparing "
      "a $0 real bill against any running size always prices as a cost, never a saving",
      "already deallocated" in dash and 'o.category === "idle"' in dash)
check("bound once per render, not once per card -- a chip filter replaces #svList wholesale, "
      "which would silently orphan a listener bound to any element inside it",
      bool(re.search(r'wireSavingsCards\(\);\s*\n\s*(?:wireScanRefresh\(token\);\s*\n\s*)?wireCommitmentPointer\(\);\s*\n'
                      r'(?:.*\n)*?.*wireWhatIf\(\);\s*\n\}', dash)))
check("the markup itself takes a bare resource id, not a row shape -- the table cell's row and "
      "the card's Opportunity disagree on what the id field is even called",
      "function whatIfMarkup(resourceId) {" in dash
      and 'function whatIfCell(r) {\n  return whatIfMarkup(r.id) || "—";' in dash)
check("wired with .onclick, not addEventListener -- revisiting this tab has to overwrite the "
      "same handler, not stack a second one behind it",
      bool(re.search(r'function wireWhatIf\(\) \{\s*\n\s*\$\("tabBody"\)\.onclick = ', dash)))
check("the comparison itself is a post(), so a wrong SKU name comes back as a sentence rather "
      "than \"400 Bad Request\"",
      'await post(`/api/dashboard/whatif${qs()}`,' in dash)
check("the control lives inside the row or card it is about, same disclosure shape as the "
      "commitments drill-down, not a shape invented a second time",
      '<details class="wf-drill" data-rid=' in dash)

check("a positive and a negative saving are coloured apart, not left the same shade of text",
      ".wf-saving.good" in css and ".wf-saving.bad" in css)


print("\nHybrid Benefit — which Windows Server VMs already use it, and what the rest could save")

hb_py = open(os.path.join(HERE, "app", "hybrid_benefit.py"), encoding="utf-8").read()

check("whether a VM already has the benefit is read from its own licenseType, not guessed "
      "from a meter name the way an OS elsewhere in this app sometimes has to be",
      'v.get("licenseType") == "Windows_Server"' in hb_py)
check("Windows Server is told apart from Windows client the same way esu.py already does it, "
      "not by a second heuristic invented here",
      "from .esu import _is_windows_server" in hb_py)
check("a VM that already has the benefit is never also given a saving estimate -- there is "
      "nothing left for it to switch on",
      bool(re.search(r'if not has_benefit and windows_rate and linux_rate', hb_py)))
check("the total is summed only from uncovered machines, so a covered VM's None can't be "
      "added in as if it were a real zero",
      bool(re.search(r'if has_benefit:\s*\n\s*covered \+= 1\s*\n\s*else:\s*\n\s*uncovered \+= 1'
                      r'\s*\n\s*total_saving \+= saving or 0\.0', hb_py)))
check("uncovered and priced machines are sorted to the top, not left in whatever order "
      "Resource Graph happened to return",
      bool(re.search(r'vms\.sort\(key=lambda r: \(r\["hybrid_benefit"\], '
                      r'r\["estimated_annual_savings"\] is None', hb_py)))
check("the response uses the same field names Savings/Advisor already use for money "
      "(cost, estimated_annual_savings), so currency conversion applies without main.py or "
      "currency.py needing to learn a new key",
      '"cost":' in hb_py and '"estimated_annual_savings"' in hb_py)

check("the route reuses convert_money the same way ESU and every other live tab does",
      bool(re.search(r'async def dashboard_hybrid_benefit.*?fx\.convert_money\(report, currency\)',
                      main_py, re.S)))
check("it is a GET, not a POST -- unlike What-If there is no typed input here, just scope/"
      "days/currency like every other tab load",
      '@app.get("/api/dashboard/hybrid-benefit")' in main_py)

check("it fills in the same tab as end-of-support rather than claiming a 16th rail entry -- "
      "the two read the same kind of estate and the rail is actively kept from growing "
      "one well-argued tab at a time",
      'id="ahbCard"' in dash and 'esu: ["optimize", "renderEsu"]' in dash)
render_esu = re.search(r"function renderEsu\([^)]*\)\s*\{(.*?)\n\}", dash, re.S)
esu_body = render_esu.group(1) if render_esu else ""
check("fetched and filled in after the ESU report, not blocking it, in both the empty-report "
      "and populated-report branches of renderEsu",
      esu_body.count("loadHybridBenefit(token);") == 2 and "await loadHybridBenefit(" not in esu_body)
check("a table built after the fact wires itself, since the generic post-render wireTables() "
      "in the tab dispatcher already ran before this card had anything in it",
      bool(re.search(r'setContent\(card, `.*?wireTables\(card\);', dash, re.S)))
check("a failed or empty lookup leaves the ESU report exactly as it was, not blanked out",
      'if (d.error || !vms.length) {' in dash)

check("the 'already on' pill gets its own colour rather than reusing the warning shade for a "
      "state that is the opposite of a warning",
      ".pill.good" in css and "--ok-wash" in css)


print("\nAnomaly root cause — what changed in Activity Log, near the day it broke pattern")

rc_py = open(os.path.join(HERE, "app", "root_cause.py"), encoding="utf-8").read()

check("only writes and deletes are kept -- a read cannot explain a cost that outlives it",
      '_CHANGE_SUFFIXES = ("/write", "/delete")' in rc_py)
check("only an operation's terminal status counts, not the Started row logged before it",
      '_TERMINAL_STATUSES = {"Succeeded", "Failed"}' in rc_py)
check("the window spans the day before through the day after, not just the anomaly's own day "
      "-- a change made late in one timezone can bill starting only the next day",
      bool(re.search(r'start = f"\{\(center - timedelta\(days=1\)\).*?\n\s*'
                      r'end = f"\{\(center \+ timedelta\(days=2\)\)', rc_py, re.S)))
check("resourceGroupName is matched server-side in the $filter, not fetched whole and sieved "
      "in Python afterwards",
      "and resourceGroupName eq '{escaped}'" in rc_py)
check("grouped by correlationId, so one operation logged three times over its lifecycle is "
      "one finding, not three",
      'groups.setdefault(cid, []).append(e)' in rc_py)
check("one subscription refusing Activity Log does not blank the whole answer -- the count of "
      "how many did is disclosed rather than the answer going empty",
      'except CostError as exc:' in rc_py and 'unreadable += 1' in rc_py)
check("says outright that a change found here is a candidate and not a verdict, rather than "
      "letting a bare list imply more certainty than a time correlation actually has",
      "is a candidate, not a verdict" in rc_py and "correlation in time is not causation" in rc_py)

check("the route is a POST, the same reasoning as What-If: a refused subscription deserves the "
      "server's real sentence back, not a bare status code",
      '@app.post("/api/dashboard/anomalies/root-cause")' in main_py)
check("resource_group and day come from the request body, not the query string",
      bool(re.search(r'async def dashboard_anomaly_root_cause\(request: Request.*?'
                      r'body = await request\.json\(\)', main_py, re.S)))

check("offered only on a resource-group finding -- Activity Log's own filter can match that "
      "dimension's key server-side, and a resource-level finding's display name is not "
      "reliably the same string as the resourceId Activity Log actually indexes by",
      'a.dimension === "resource group" ? rootCauseMarkup(a) : ""' in dash)
check("fetched once per card, the first time it opens, not on every toggle -- <details> fires "
      "toggle on closing too",
      "if (!details.open || details.dataset.loaded) return;" in dash)
check("the comparison itself is a post(), matching What-If, so a refused subscription's real "
      "reason reaches the reader",
      'await post(`/api/dashboard/anomalies/root-cause${qs()}`,' in dash)
check("wired after render the same way the alerts panel already is, not left for the generic "
      "post-render pass to find",
      bool(re.search(r'wireAnomalyAlerts\(\);\s*\n\s*wireRootCause\(\);', dash)))
check("a failed operation in a group is marked, not shown identically to one that succeeded",
      "'<span class=\"pill bad\">failed</span>'" in dash)


print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)
