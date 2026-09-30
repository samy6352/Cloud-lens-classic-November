"""Offline browser-driven private-cache warming; no application/database imports."""
import ast
import os
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
run_node = runpy.run_path(str(Path(__file__).with_name("test_tags_frontend.py")))["run_node"]

HARNESS = r"""
import assert from "node:assert/strict";
import {createCacheWarmer, warmingURL} from "./web/assets/cache-warming.js";
const origin="https://cloudlens.example";
const urls=["/api/dashboard/sections?scope=s1&days=30",
  "/api/dashboard/executive?scope=s1&currency=USD&period_month=2026-08&rg=a%2Cb",
  "/api/dashboard/billing?month=2026-08&scope=s1",
  "/api/dashboard/resources?days=7&scope=s1", "/api/filters?scope=s1"];
const response=(generation="1", extra={})=>({status:200,headers:new Headers({
  "X-CloudLens-Source":"warehouse","X-CloudLens-Source-Generation":generation,
  "X-CloudLens-Stale":"false","X-CloudLens-Revalidating":"false", ...extra,
})});
let clock=0, id=0, calls=[], warnings=0, listeners=new Map(), timers=new Map();
const document={hidden:false};
const page={addEventListener:(key,fn)=>listeners.set(key,fn),
  removeEventListener:key=>listeners.delete(key)};
const schedule=(fn,delay)=>{const key=++id;timers.set(key,{fn,delay});return key;};
const cancel=key=>timers.delete(key);
const tick=async()=>{const [key,task]=timers.entries().next().value;timers.delete(key);
  clock+=task.delay;await task.fn();};
const options={origin,now:()=>clock,schedule,cancel,page,document,delay:1,
  warn:()=>warnings++, request:async(url,options)=>{calls.push({url,options});return response("2");}};
const used=(warmer,url,generation="1",extra={})=>{
  const key=warmer.begin(url);warmer.observe(key,response(generation,extra));warmer.end(key);
};
"""


def test_exact_allowlist_and_query_secrets_are_rejected_without_normalization():
    run_node(HARNESS + r"""
assert.equal(warmingURL(urls[1],origin),urls[1]);
assert.equal(warmingURL(origin+urls[1],origin),urls[1]);
for(const url of ["/api/overview","/api/dashboard/tags","/api/dashboard/aks",
  "/api/dashboard/rates","/api/dashboard/ai-billing","/api/dashboard/health",
  "/api/auth/me","/api/jobs/1","/api/costs/raw.csv","/api/ask",
  "//elsewhere.example/api/filters", "https://u:password@cloudlens.example/api/filters",
  "/api/filters#token","/api/filters?access_token=secret","/api/filters?scope=s1&fresh=true",
  "/api/filters?rg=Bearer%20secret", "/api/filters?env=password%3Dsecret",
  "/api/filters?scope=eyJxx.yyyyy.zzzzz", "/api/filters?rg=%0Asecret",
  "/api/filters?anything=else", "/api/filters?rg="+"a".repeat(1025)]) {
  assert.equal(warmingURL(url,origin),null,url);
}
const warmer=createCacheWarmer(options);warmer.configure(true);
for(const options of [{method:"POST"}, {body:"secret"}, {headers:{Authorization:"Bearer secret"}},
  {headers:{Cookie:"secret"}},{credentials:"omit"},{credentials:"include"}]) {
  assert.equal(warmer.begin(urls[0],options),null);
}
warmer.close();
""")


def test_default_off_explicit_flag_first_generation_baseline_and_three_request_budget():
    run_node(HARNESS + r"""
const warmer=createCacheWarmer(options);
used(warmer,urls[0]);used(warmer,urls[1],"2");assert.equal(timers.size,0);
warmer.configure("true");assert.equal(warmer.begin(urls[0]),null);
warmer.configure(true);
for(const url of urls)used(warmer,url);
assert.equal(timers.size,0,"first observed generation is a baseline, not a publication");
used(warmer,urls[0],"2");
while(timers.size)await tick();
assert.equal(calls.length,3);
assert.deepEqual(calls.map(c=>c.url),[...urls].reverse().slice(0,3).map(u=>u+"&fresh=true"));
assert.ok(calls.every(c=>c.options.credentials==="same-origin" && !c.options.headers));
used(warmer,urls[1],"2");used(warmer,urls[1],"1");assert.equal(timers.size,0);
used(warmer,urls[1],"pending:3:2");assert.equal(timers.size,0);
warmer.configure(false);used(warmer,urls[0],"3");assert.equal(timers.size,0);
warmer.close();
""")


def test_current_inflight_duplicate_inflight_and_queued_inflight_are_skipped():
    run_node(HARNESS + r"""
const warmer=createCacheWarmer(options);warmer.configure(true);
urls.forEach(url=>used(warmer,url));
const held=warmer.begin(urls[4]),heldAgain=warmer.begin(urls[4]);
warmer.end(heldAgain);
used(warmer,urls[0],"2");
const late=warmer.begin(urls[3]);
while(timers.size)await tick();
assert.deepEqual(calls.map(c=>c.url),[urls[2],urls[1]].map(u=>u+"&fresh=true"));
warmer.end(held);warmer.end(late);warmer.close();
""")


def test_newer_generation_seen_by_warm_consumes_batch_without_rescheduling_or_extending_recency():
    run_node(HARNESS + r"""
const warmer=createCacheWarmer({...options,ttl:10,request:async(url)=>{
  calls.push(url);return response("3");
}});warmer.configure(true);
urls.forEach(url=>used(warmer,url));
used(warmer,urls[0],"2");
while(timers.size)await tick();
assert.equal(calls.length,3,"generation 3 arrives during the generation 2 batch");
used(warmer,urls[0],"3");
assert.equal(timers.size,0,"foreground observation must not schedule generation 3 again");
used(warmer,urls[0],"2");
assert.equal(timers.size,0,"late foreground responses cannot roll the generation back");
clock=10;used(warmer,urls[0],"4");
assert.equal(timers.size,0,"warm responses must not extend other URLs' original recency");
warmer.close();
""")


def test_registry_size_ttl_and_warm_responses_do_not_extend_recent_usage():
    run_node(HARNESS + r"""
const warmer=createCacheWarmer({...options,maxEntries:2,ttl:10});warmer.configure(true);
used(warmer,urls[0]);used(warmer,urls[1]);used(warmer,urls[2],"2");
while(timers.size)await tick();
assert.deepEqual(calls.map(c=>c.url),[urls[1]+"&fresh=true"]);
clock=11;used(warmer,urls[2],"3");assert.equal(timers.size,0);
warmer.close();
assert.throws(()=>createCacheWarmer({...options,budget:4}),/bounds/);
assert.throws(()=>createCacheWarmer({...options,ttl:Infinity}),/bounds/);
""")


def test_pending_failed_and_uncached_responses_never_register_or_trigger():
    run_node(HARNESS + r"""
const warmer=createCacheWarmer(options);warmer.configure(true);
used(warmer,urls[0]);
for(const extra of [{"X-CloudLens-Stale":"true"},{"X-CloudLens-Revalidating":"true"},
  {"X-CloudLens-Source-State":"pending"},{"X-CloudLens-Source":"monitor"},
  {"X-CloudLens-Cache-Error":"write_failed"},{"X-CloudLens-Refresh-Error":"source_failed"}]) {
  used(warmer,urls[1],"2",extra);
}
for(const status of [401,403,500,202]) {
  const key=warmer.begin(urls[1]);
  warmer.observe(key,{...response("2"),status});warmer.end(key);
}
warmer.observe(urls[2],response("2")); // unsolicited URLs are not successful request records
assert.equal(timers.size,0);
used(warmer,urls[0],"2");assert.equal(timers.size,0);
warmer.close();
""")


def test_failure_clears_batch_backs_off_and_does_not_retry_same_generation():
    run_node(HARNESS + r"""
const warmer=createCacheWarmer({...options,backoff:100,request:async(url)=>{
  calls.push(url);return response("2",{"X-CloudLens-Stale":"true"});
}});warmer.configure(true);
urls.forEach(url=>used(warmer,url));used(warmer,urls[0],"2");await tick();
assert.equal(calls.length,1);assert.equal(warnings,1);assert.equal(timers.size,0);
used(warmer,urls[0],"3");assert.equal(timers.size,0,"backoff suppresses a new publication");
clock=200;used(warmer,urls[0],"3");assert.equal(timers.size,0,"no repeat publication scheduling");
used(warmer,urls[0],"4");await tick();assert.equal(calls.length,2);
warmer.close();
""")


def test_revocation_and_logout_stop_all_warming_without_retaining_credentials():
    run_node(HARNESS + r"""
for(const status of [401,403]) {
  calls=[];const warmer=createCacheWarmer({...options,request:async(url,options)=>{
    calls.push({url,options});throw Object.assign(Error("denied"),{status});
  }});warmer.configure(true);
  urls.forEach(url=>used(warmer,url));used(warmer,urls[0],"2");await tick();
  assert.equal(calls.length,1);assert.equal(warmer.begin(urls[0]),null);
  assert.equal(timers.size,0);warmer.close();
}
""")


def test_pagehide_aborts_single_inflight_request_and_hidden_pages_do_not_warm():
    run_node(HARNESS + r"""
let active=0,signal,finish;
const warmer=createCacheWarmer({...options,request:async(url,options)=>{
  calls.push(url);active++;signal=options.signal;
  await new Promise(resolve=>{finish=resolve;signal.addEventListener("abort",resolve);});
  active--;return response("2");
}});warmer.configure(true);
urls.forEach(url=>used(warmer,url));used(warmer,urls[0],"2");
const running=tick();await Promise.resolve();assert.equal(active,1);
used(warmer,urls[1],"3");assert.equal(timers.size,0);
listeners.get("pagehide")();assert.equal(signal.aborted,true);
await running;assert.equal(active,0);assert.equal(timers.size,0);
warmer.configure(true);assert.equal(warmer.begin(urls[0]),null);
const hidden=createCacheWarmer(options);hidden.configure(true);
urls.forEach(url=>used(hidden,url));document.hidden=true;used(hidden,urls[0],"2");
assert.equal(timers.size,0);hidden.close();
""")


def test_real_central_helper_parses_before_recording_and_reuses_current_browser_credentials():
    run_node(HARNESS + r"""
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/app.js","utf8");
let login=0,cookie="current-session", seen=[];
const fetch=async(url,opts)=>{
  seen.push({url,opts,cookie});
  return {ok:true,...response("2"),json:async()=>({cost:3})};
};
const location={origin};
const recordSnapshot=data=>data;
const toLogin=()=>login++;
const text=source.slice(source.indexOf("let cacheWarmer ="),
  source.indexOf("\n/**\n * A write,")).replace("export async function get","async function get");
const factory=new Function("location","recordSnapshot","toLogin","fetch",
  text+"\nreturn {get,setWarmer:warmer=>{cacheWarmer=warmer;}};");
const client=factory(location,recordSnapshot,toLogin,fetch);
const warmer=createCacheWarmer({...options,
  request:(url,opts)=>client.get(url,{...opts,cacheWarming:true})});
client.setWarmer(warmer);warmer.configure(true);
await client.get(urls[0]); // establishes generation 2
cookie="new-session-after-role-change";
// This uses the same helper as the warmer, not a synthetic identity, cookie or bearer header.
const warmed=await client.get(urls[0]+"&fresh=true",
  {cacheWarming:true,credentials:"same-origin",timeout:10});
assert.equal(warmed.status,200);assert.equal(timers.size,0);
assert.equal(seen[1].cookie,"new-session-after-role-change");
assert.equal(seen[1].opts.headers,undefined);
assert.equal(seen[1].opts.cacheWarming,undefined);
assert.equal(login,0);
warmer.close();
assert.ok(source.includes("cacheWarmer?.stop();\n    // Clear the remembered scope"));
assert.ok(source.includes("me.required && me.authenticated && me.features?.snapshot_warming === true"));
""")


def test_fresh_reads_scope_adds_fresh_only_to_interactive_cacheable_gets():
    run_node(HARNESS + r"""
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/app.js","utf8");
const text=source.slice(source.indexOf("let cacheWarmer ="),
  source.indexOf("\n/**\n * A write,")).replace("export async function get","async function get");
let seen=[], delays=[];
globalThis.setTimeout=(fn,delay)=>{delays.push(delay);return delays.length;};
globalThis.clearTimeout=()=>{};
const fetch=async(url,opts)=>{
  seen.push({url,opts});
  return {ok:true,status:200,statusText:"OK",headers:new Headers(),json:async()=>({total:1})};
};
const client=new Function("location","recordSnapshot","toLogin","fetch",text+
  "\nreturn {get,freshReads};")( {origin}, data=>data, ()=>{}, fetch);
await client.freshReads(async()=>{
  await client.get("/api/dashboard/executive?scope=s1", {timeout:20});
  await client.get("/api/filters?scope=s1", {cacheWarming:true,timeout:20});
  await client.get("/api/auth/me", {timeout:20});
  await client.get("/api/dashboard/ai-billing?force=true", {timeout:20});
});
assert.equal(seen[0].url,"/api/dashboard/executive?scope=s1&fresh=true");
assert.equal(delays[0],180000);
assert.equal(seen[1].url,"/api/filters?scope=s1");
assert.equal(delays[1],20);
assert.equal(seen[2].url,"/api/auth/me");
assert.equal(seen[3].url,"/api/dashboard/ai-billing?force=true&fresh=true");
assert.equal(delays[3],180000);
""")


def test_central_helper_timeout_failed_json_and_warm_auth_failure_do_not_redirect_or_register():
    run_node(HARNESS + r"""
import fs from "node:fs";
const source=fs.readFileSync("./web/assets/app.js","utf8");
let mode="bad-json", observations=0, stops=0, redirects=0, ended=0;
const fetch=async(url,opts)=>{
  if(mode==="timeout")return new Promise((_,reject)=>
    opts.signal.addEventListener("abort",()=>reject(Error("aborted"))));
  return {ok:mode==="bad-json",...response("2"),
    status:mode==="denied"?401:200,json:async()=>{throw Error("bad json");}};
};
const text=source.slice(source.indexOf("let cacheWarmer ="),
  source.indexOf("\n/**\n * A write,")).replace("export async function get","async function get");
const client=new Function("recordSnapshot","toLogin","fetch",text+
  "\nreturn {get,setWarmer:warmer=>{cacheWarmer=warmer;}};")(
    d=>d,()=>redirects++,fetch);
client.setWarmer({begin:()=>urls[0],observe:()=>observations++,end:()=>ended++,stop:()=>stops++});
await assert.rejects(client.get(urls[0]),/bad json/);
mode="timeout";await assert.rejects(client.get(urls[0],{timeout:2}),/aborted/);
mode="denied";await assert.rejects(client.get(urls[0],{cacheWarming:true}),e=>e.status===401);
assert.equal(redirects,0);assert.equal(stops,1);
await assert.rejects(client.get(urls[0]),e=>e.status===401);
assert.equal(redirects,1);assert.equal(stops,2);
assert.equal(observations,0);assert.equal(ended,4);
""")


def test_feature_config_defaults_and_validation_without_importing_app(monkeypatch):
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "snapshot_warming_enabled")
    namespace = {"os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<warming-config>", "exec"), namespace)
    enabled = namespace["snapshot_warming_enabled"]
    monkeypatch.delenv("SNAPSHOT_WARMING_ENABLED", raising=False)
    assert enabled() is False
    for value in ("true", "1", " TRUE "):
        monkeypatch.setenv("SNAPSHOT_WARMING_ENABLED", value)
        assert enabled() is True
    for value in ("false", "0"):
        monkeypatch.setenv("SNAPSHOT_WARMING_ENABLED", value)
        assert enabled() is False
    monkeypatch.setenv("SNAPSHOT_WARMING_ENABLED", "perhaps")
    with pytest.raises(ValueError, match="SNAPSHOT_WARMING_ENABLED"):
        enabled()


def test_snapshot_cache_bounds_defaults_and_validation_without_importing_app(monkeypatch):
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    wanted = {"_positive_int_env", "snapshot_cache_settings"}
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"os": os}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<cache-config>", "exec"),
         namespace)
    settings = namespace["snapshot_cache_settings"]
    for name in (
        "SNAPSHOT_MAX_BYTES", "SNAPSHOT_MAX_ENTRIES", "SNAPSHOT_MAX_PAYLOAD",
        "SNAPSHOT_ORIGIN_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert settings() == {
        "max_bytes": 1024 * 1024 * 1024,
        "max_entries": 4096,
        "max_payload": 32 * 1024 * 1024,
        "origin_timeout": 300,
    }
    monkeypatch.setenv("SNAPSHOT_MAX_BYTES", "123")
    monkeypatch.setenv("SNAPSHOT_MAX_ENTRIES", "4")
    monkeypatch.setenv("SNAPSHOT_MAX_PAYLOAD", "5")
    monkeypatch.setenv("SNAPSHOT_ORIGIN_TIMEOUT", "6")
    assert settings() == {"max_bytes": 123, "max_entries": 4,
                          "max_payload": 5, "origin_timeout": 6}
    monkeypatch.setenv("SNAPSHOT_MAX_ENTRIES", "0")
    with pytest.raises(ValueError, match="SNAPSHOT_MAX_ENTRIES"):
        settings()
