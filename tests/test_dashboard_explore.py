"""Execute pure contextual drill-down helpers without a browser or new dependencies."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_node(script):
    executable = shutil.which("node")
    bundled = Path(os.environ.get("PROGRAMFILES(X86)", "")) / "scout" / "resources" / "node" / "node.exe"
    if not executable and bundled.is_file():
        executable = str(bundled)
    if not executable:
        pytest.skip("Node.js is needed for frontend execution tests")
    result = subprocess.run(
        [executable, "--input-type=module", "-"],
        input='import assert from "node:assert/strict";\n'
        'import { portalTargets, portalLinkMarkup, rowDetailsMarkup } '
        'from "./web/assets/dashboard-explore.js";\n'
        'const sub = "12345678-1234-abcd-9876-1234567890ab";\n'
        'const other = "87654321-4321-abcd-9876-1234567890ab";\n'
        'const arm = `/subscriptions/${sub}/resourceGroups/production/providers/Microsoft.Compute/virtualMachines/vm1`;\n'
        + script,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_exact_resource_paths_ancestors_case_and_segment_encoding():
    run_node(r"""
for (const key of ["resource_id", "resourceId", "ResourceId", "id"]) {
  const targets = portalTargets({[key]:arm});
  assert.deepEqual(targets.map(x => x.label), ["Resource", "Resource group", "Subscription"]);
  assert.equal(targets[0].url, `https://portal.azure.com/#resource${arm}/overview`);
  assert.equal(targets[1].url, `https://portal.azure.com/#resource/subscriptions/${sub}/resourceGroups/production/overview`);
  assert.equal(targets[2].url, `https://portal.azure.com/#resource/subscriptions/${sub}/overview`);
}
const mixed = `/SUBSCRIPTIONS/${sub.toUpperCase()}/RESOURCEGROUPS/Team & <East>#?/PROVIDERS/Microsoft.Compute/virtualMachines/VM '"!%/extensions/child/providers/Microsoft.Authorization/locks/nested`;
const exact = portalTargets({id:mixed})[0].url;
assert.match(exact, /\/subscriptions\/12345678-1234-ABCD-9876-1234567890AB\/resourceGroups\//);
assert.match(exact, /Team%20%26%20%3CEast%3E%23%3F/);
assert.match(exact, /VM%20%27%22%21%25\/extensions\/child\/providers\/Microsoft.Authorization\/locks\/nested\/overview$/);
assert.equal(new URL(exact).origin, "https://portal.azure.com");
assert.equal(new URL(exact).search, "");
const scoped = `/subscriptions/${sub}/providers/Microsoft.Authorization/policyAssignments/assignment`;
assert.deepEqual(portalTargets({id:scoped}).map(t => t.label), ["Resource", "Subscription"]);
assert.equal(portalTargets({id:"aggregate-1", resource_id:arm})[0].label, "Resource");
""")


def test_group_and_subscription_identity_without_name_guessing():
    run_node(r"""
for (const key of ["subscription_id", "subscriptionId", "subscription"]) {
  for (const value of [sub, `/SUBSCRIPTIONS/${sub}`]) {
    assert.equal(portalTargets({[key]:value})[0].url,
      `https://portal.azure.com/#resource/subscriptions/${sub}/overview`);
    for (const groupKey of ["resource_group", "group", "grp"]) {
      assert.equal(portalTargets({[key]:value, [groupKey]:"Team A & B"})[0].url,
        `https://portal.azure.com/#resource/subscriptions/${sub}/resourceGroups/Team%20A%20%26%20B/overview`);
    }
  }
}
assert.deepEqual(portalTargets({id:`/subscriptions/${sub}`}).map(x => x.label), ["Subscription"]);
assert.equal(portalTargets({id:`/subscriptions/${sub}`, group:"rg"})[0].label, "Resource group");
assert.deepEqual(portalTargets({id:`/subscriptions/${sub}/resourceGroups/rg`}).map(x => x.label),
  ["Resource group", "Subscription"]);
assert.equal(portalTargets({grp:`/subscriptions/${sub}/resourceGroups/rg`})[0].label, "Resource group");
assert.equal(portalTargets({group:"rg"}, [sub])[0].label, "Resource group");
assert.equal(portalTargets({service:"Storage"}, [sub])[0].label, "Subscription");
assert.deepEqual(portalTargets({group:"rg", subscription:"Production"}), []);
assert.deepEqual(portalTargets({id:"vm1", name:"vm1"}, [sub, other]), []);
assert.deepEqual(portalTargets({group:"rg"}, [sub, sub]), []);
assert.deepEqual(portalTargets({group:"rg"}, [`/subscriptions/${sub}`]), []);
assert.deepEqual(portalTargets({subscription_id:sub, subscriptionId:other, group:"rg"}, [sub]), []);
assert.equal(portalTargets({resource_id:arm}, [other])[0].url,
  `https://portal.azure.com/#resource${arm}/overview`);
""")


def test_invalid_ids_untrusted_urls_and_mixed_scopes_never_invent_resources():
    run_node(r"""
const invalid = [
  null, 0, 42, "vm1", sub, "", "javascript:alert(1)", "https://evil.example/a",
  `https://management.azure.com${arm}`, `//portal.azure.com${arm}`,
  `/subscriptions/not-a-guid/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm`,
  `/subscriptions/${sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines`,
  `/subscriptions/${sub}/resourceGroups/rg/providers/Microsoft.Compute`,
  `/subscriptions/${sub}/resourceGroups/rg/virtualMachines/vm`,
  `/subscriptions/${sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/..`,
  `/subscriptions/${sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/a\\b`,
  `/subscriptions/${sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/\u0000`,
  `/subscriptions/${sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/\ud800`,
  `${arm}/`, `${arm}/providers/Microsoft.Authorization/locks`,
];
for (const id of invalid) {
  assert.deepEqual(portalTargets({id, portal_url:"javascript:alert(1)", href:arm}), [], String(id));
  assert.deepEqual(portalTargets({id}, [sub, other]), [], String(id));
  assert.deepEqual(portalTargets({id}, [sub]).map(x => x.label), ["Subscription"], String(id));
}
for (const id of ["Production", ` ${sub}`, sub.replaceAll("-", ""), `${sub}/extra`, `{${sub}}`]) {
  assert.deepEqual(portalTargets({subscription_id:id, group:"rg"}), []);
  assert.deepEqual(portalTargets({}, [id]), []);
}
for (const group of ["javascript:alert(1)", "https://evil.example", "../rg", "a/b", ".."]) {
  assert.deepEqual(portalTargets({group, subscription_id:sub}).map(x => x.label), ["Subscription"]);
}
assert.deepEqual(portalTargets({portal_url:"https://portal.azure.com/#resource"+arm}), []);
assert.deepEqual(portalTargets(Object.create({resource_id:arm})), []);
""")


def test_resource_label_links_are_escaped_and_only_exact_resources_link():
    run_node(r"""
const label = `<img src=x onerror="alert(1)"> & 'bad'`;
const link = portalLinkMarkup({resource_id:arm}, [], label);
assert.match(link, /^<a href="https:\/\/portal\.azure\.com\/#resource/);
assert.match(link, /target="_blank" rel="noopener noreferrer"/);
assert.match(link, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt; &amp; &#39;bad&#39;/);
assert.doesNotMatch(link, /<img/);
for (const row of [{id:"vm1"}, {group:"rg"}, {subscription_id:sub}, {id:`/subscriptions/${sub}`}]) {
  assert.doesNotMatch(portalLinkMarkup(row, [sub], label), /<a|<img/);
}
assert.equal(portalLinkMarkup({portal_url:"javascript:alert(1)"}, [], "safe"), "safe");
assert.equal(portalLinkMarkup({name:0}, [sub]), "0");
assert.equal(portalLinkMarkup({}, [], ""), "");
assert.match(portalLinkMarkup({resourceId:arm, name:"VM & DB"}), />VM &amp; DB<\/a>$/);
""")


def test_details_escape_nested_values_labels_title_and_preserve_falsy_data():
    run_node(r"""
assert.equal(typeof globalThis.document, "undefined", "module loads without a DOM");
const row = {
  name:"<script>alert('x')</script>", ['<img src=x onerror="x">']:"&",
  monthly_cost:0, count:12345, active:false, enabled:true, missing:null, empty:"",
  resource_id:arm, subscription_id:sub, cpu_pct:0.000000001,
  tags:{owner:"<a href=javascript:bad>owner</a>", environment:"production"},
  evidence:[{metric:"CPU", samples:[0, false, null]}, {message:"<svg/onload=x>"}],
};
const html = rowDetailsMarkup(row, {currency:"USD", title:'Details <img src=x onerror="x">'});
assert.match(html, /class="explore-fields"/);
assert.match(html, /<details class="explore-nested">/);
assert.match(html, /<summary>2 items<\/summary>/);
assert.match(html, /Monthly cost<\/dt><dd><span class="explore-value">\$0/);
assert.match(html, /12,345/);
assert.match(html, /False/); assert.match(html, /True/);
assert.match(html, /Not provided \(null\)/); assert.match(html, /\(empty string\)/);
assert.match(html, /0\.000000001/);
assert.match(html, /Resource ID/); assert.match(html, /Subscription ID/);
assert.match(html, /&lt;script&gt;/); assert.match(html, /&lt;svg\/onload=x&gt;/);
assert.match(html, /&lt;img src=x onerror=&quot;x&quot;&gt;/);
assert.doesNotMatch(html, /<script|<img|<svg|<a /);
assert.doesNotMatch(html, /JSON\.stringify|\{"metric"/);
assert.match(rowDetailsMarkup({cost:123.5}, {currency:"<svg/onload=x>"}), /123\.5/);
assert.match(rowDetailsMarkup({cost:0.00000001}, {currency:"USD"}), /0\.00000001/);
for (const [value, text] of [[0,"0"], [false,"False"], [null,"Not provided (null)"]]) {
  assert.ok(rowDetailsMarkup(value).includes(text));
}
""")


def test_sensitive_keys_are_excluded_recursively_but_subscription_ids_remain():
    run_node(r"""
const denied = ["access_token", "refreshToken", "id_token", "password", "passwd", "pwd",
  "clientSecret", "credentials", "credential", "authorization", "Authentication",
  "api_key", "accessKey", "private_key", "signingKey", "connection_string", "cookie",
  "session", "session_id", "sessionKey", "sas_token", "sas", "accountKey",
  "storage_key", "shared_access_signature", "bearer", "jwt", "token", "tokens",
  "accessTokens", "api_keys", "access_key_1", "refresh_token_value"];
const privateFields = Object.fromEntries(denied.map(key => [key, `DO_NOT_SHOW_${key}`]));
const row = {
  ...privateFields, subscription_id:sub, resource_id:arm, session_count:0,
  tags:{...privateFields, owner:"safe owner", environment:"production"},
  evidence:[{...privateFields, recommendation:"safe recommendation"},
    {name:"Authorization", value:"PAIR_SECRET"},
    {key:"api_token", value:"PAIR_TOKEN"},
    {key:"owner", value:"safe tag"}],
};
const html = rowDetailsMarkup(row);
assert.doesNotMatch(html, /DO_NOT_SHOW|PAIR_SECRET|PAIR_TOKEN/);
assert.ok(html.includes(sub));
assert.match(html, /safe owner|safe recommendation/);
assert.match(html, /safe tag/);
assert.match(html, /Session count/);
let getterCalled = false;
Object.defineProperty(row, "dangerous", {enumerable:true, get() { getterCalled = true; throw Error(); }});
rowDetailsMarkup(row);
assert.equal(getterCalled, false);
assert.match(rowDetailsMarkup({password:"DO_NOT_SHOW"}), /No displayable fields/);
assert.doesNotMatch(rowDetailsMarkup({name:"password", value:"DO_NOT_SHOW"}), /DO_NOT_SHOW/);
""")


def test_details_bound_arrays_depth_cycles_wide_payloads_and_long_text():
    run_node(r"""
const html = rowDetailsMarkup({evidence:Array.from({length:253}, (_,i) => ({name:`item-${i}`}))});
assert.match(html, /item-249/);
assert.doesNotMatch(html, /item-250/);
assert.match(html, /3 more items not shown \(display limit\)/);
const deep = {leaf:"DEEP_VALUE_NOT_SHOWN"};
let root = deep;
for (let i = 0; i < 20; i++) root = {nested:root};
const deepHtml = rowDetailsMarkup(root);
assert.match(deepHtml, /fields not shown \(depth limit\)/);
assert.doesNotMatch(deepHtml, /DEEP_VALUE_NOT_SHOWN/);
const cyclic = {name:"safe"};
cyclic.self = cyclic;
assert.match(rowDetailsMarkup(cyclic), /Circular reference not expanded/);
const shared = {name:"reused"};
assert.equal((rowDetailsMarkup({a:shared,b:shared}).match(/reused/g)||[]).length, 2);
const wide = Object.fromEntries(Array.from({length:253}, (_,i) => [`field_${i}`, i]));
assert.match(rowDetailsMarkup(wide), /3 more fields not shown/);
const huge = rowDetailsMarkup({rows:Array.from({length:250}, () => Array(250).fill("hello"))});
assert.ok(huge.length < 250000, "global budget bounds the whole tree, not just each array");
assert.match(huge, /more items not shown/);
const text = rowDetailsMarkup({description:"x".repeat(20003)+"<script>"});
assert.match(text, /11 more characters not shown/);
assert.doesNotMatch(text, /<script>/);
assert.match(rowDetailsMarkup({empty:{}, list:[]}), /No displayable fields/);
assert.match(rowDetailsMarkup({empty:{}, list:[]}), /No items/);
""")
