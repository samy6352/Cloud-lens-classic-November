import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_fast_filter_dimensions_render_before_environment_lookup():
    source = (ROOT / "web" / "assets" / "filters.js").read_text(encoding="utf-8")
    source = source.replace("import { esc, get, money, onScopeChange, readStore, scopeIds, writeStore } from \"./app.js\";", "")
    source = source.replace('import { periodParams } from "./reporting-period.js";', "")
    source = source.replace("export function ", "function ")
    source = source.replace("export async function ", "async function ")
    script = r"""
import assert from "node:assert/strict";
const requests = [];
const timeouts = [];
let releaseEnvironment;
const esc = value => String(value ?? "");
const money = value => String(value);
const onScopeChange = () => {};
const readStore = () => ({});
const scopeIds = () => ["sub-a"];
const writeStore = () => {};
const periodParams = days => `days=${days}`;
const get = async (url, options = {}) => {
  requests.push(url);
  timeouts.push(options.timeout);
  if (url.includes("env_only=true")) {
    return new Promise(resolve => {
      releaseEnvironment = () => resolve({
      dimensions: { env: [
        { value: "prod", cost: 5, currency: "USD" },
        { value: "dev", cost: 3, currency: "USD" },
      ] },
        currency: "USD",
      });
    });
  }
  return {
    dimensions: { service: [{ value: "Storage" }, { value: "Compute" }], env: [] },
    deferred_dimensions: ["env"],
    currency: "USD",
  };
};
const nodes = {
  filterBar: { hidden: true },
  fbSlots: { innerHTML: "" },
  fbClear: { hidden: false },
  fbStatus: { innerHTML: "" },
};
globalThis.document = {
  getElementById: id => nodes[id],
  querySelectorAll: () => [],
  removeEventListener: () => {},
  addEventListener: () => {},
};
""" + source + r"""
ready = true;
await loadFilters(30);
assert.match(requests[0], /include_env=false/);
assert.match(requests[1], /env_only=true/);
assert.equal(timeouts[0], 45000);
assert.equal(timeouts[1], 18000);
assert.match(nodes.fbSlots.innerHTML, /data-dim="service"/);
assert.doesNotMatch(nodes.fbSlots.innerHTML, /data-dim="env"/);
assert.equal(typeof releaseEnvironment, "function");
releaseEnvironment();
await new Promise(resolve => setTimeout(resolve, 0));
assert.match(nodes.fbSlots.innerHTML, /data-dim="env"/);
"""
    result = subprocess.run(
        [node(), "--input-type=module", "-e", script], cwd=ROOT,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def node():
    executable = shutil.which("node")
    bundled = Path(os.environ.get("PROGRAMFILES(X86)", "")) / "scout" / "resources" / "node" / "node.exe"
    if not executable and bundled.is_file():
        executable = str(bundled)
    if not executable:
        pytest.skip("Node.js is needed for frontend execution tests")
    return executable
