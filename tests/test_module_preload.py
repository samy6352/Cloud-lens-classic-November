"""Module warming must remain data-free, deduplicated and retryable."""
import re
from pathlib import Path

from tests.test_tags_frontend import run_node

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"


def _static_imports(source: str) -> set[str]:
    """Only `import ... from "./x.js"`. A dynamic import() is a deliberate deferral."""
    return set(re.findall(r"^\s*import[^;]*?from\s*[\"']\./([^\"']+)[\"']", source, re.M))


def _always_fetched() -> set[str]:
    """Every module reachable from dashboard.js without a dynamic import in the way."""
    seen: set[str] = set()
    queue = ["dashboard.js"]
    while queue:
        name = queue.pop()
        if name in seen or not (ASSETS / name).exists():
            continue
        seen.add(name)
        queue.extend(_static_imports((ASSETS / name).read_text(encoding="utf-8")))
    return seen


def test_every_unconditional_module_is_preloaded_and_no_lazy_one_is():
    """The preload list is a fact about the import graph, so it is derived from the graph here.

    Left to be maintained by hand it drifts both ways: a new static import arrives and quietly
    costs an extra round trip, or a tab module is added to the list and is downloaded by every
    session that never opens that tab -- which is precisely what the on-demand loader exists to
    avoid.
    """
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    preloaded = set(re.findall(r'<link rel="modulepreload" href="/assets/([^"]+)"', html))
    # dashboard.js is the entry point: it has its own <script> tag and must not be listed twice.
    expected = _always_fetched() - {"dashboard.js"}
    assert preloaded == expected, (
        f"missing from index.html: {sorted(expected - preloaded)}; "
        f"listed but not statically imported: {sorted(preloaded - expected)}"
    )
    assert "dashboard.js" not in preloaded


def test_tab_modules_stay_on_demand():
    """The heavy per-tab modules are fetched when a tab is opened, or warmed on intent."""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    for lazy in ("dashboard-spend.js", "dashboard-reports.js", "dashboard-billing.js",
                 "dashboard-monitor.js", "dashboard-optimize.js", "dashboard-resources.js",
                 "chart.umd.min.js"):
        assert lazy not in html, f"{lazy} is loaded on demand and must not be preloaded"


def test_warming_and_navigation_share_a_module_without_creating_a_view():
    run_node(r"""
import assert from "node:assert/strict";
import {createModuleLoader} from "./web/assets/dashboard-module-loader.js";
let loads=0,instances=0,release;
const wait=new Promise(resolve=>{release=resolve});
const instance={createTabs(){instances++}};
const loader=createModuleLoader({resources:()=>{loads++;return wait}});
const warm=loader.warm("resources");
const first=loader.load("resources"),second=loader.load("resources");
assert.equal(first,second);
release(instance);
await warm;
assert.equal(await first,instance);
assert.equal(loads,1);
assert.equal(instances,0,"warming must not fetch data or create a controller");
assert.equal(await loader.load("resources"),instance);
assert.equal(loads,1);
""")


def test_failed_warmup_is_reported_once_and_navigation_can_retry():
    run_node(r"""
import assert from "node:assert/strict";
import {createModuleLoader} from "./web/assets/dashboard-module-loader.js";
const warnings=[];
let calls=0;
const loader=createModuleLoader({billing:()=>{
  if(++calls===1)throw new Error("temporary module failure");
  return {ready:true};
}},error=>warnings.push(error.message));
await loader.warm("billing");
assert.deepEqual(warnings,["temporary module failure"]);
assert.equal((await loader.load("billing")).ready,true);
assert.equal(calls,2);
await assert.rejects(loader.load("missing"),/Unknown tab family/);
""")
