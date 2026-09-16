"""Offline snapshot metadata and active-context revalidation regressions."""
import runpy
from pathlib import Path

run_node = runpy.run_path(str(Path(__file__).with_name("test_tags_frontend.py")))["run_node"]

HARNESS = r"""
import assert from "node:assert/strict";
import {recordSnapshot,snapshotOf,readSnapshotHeaders,createSnapshotFlow,createViewRevalidation}
  from "./web/assets/dashboard-freshness.js";
const url="/api/dashboard/resources?days=30&currency=USD";
const oldStamp="2026-09-14T10:00:00Z",newStamp="2026-09-14T11:00:00Z";
const response=(amount,{stale=false,error="",stamp=newStamp,generation="2"}={})=>
  recordSnapshot({amount},url,new Headers({
    "X-CloudLens-Cache":stale?"stale":"hit","X-CloudLens-As-Of":stamp,
    "X-CloudLens-Stale":String(stale),"X-CloudLens-Revalidating":String(stale&&!error),
    "X-CloudLens-Source-Generation":generation,"X-CloudLens-Refresh-Error":error,
  }));
let clock=0,next=0;
const scheduled=new Map(),delays=[];
const schedule=(callback,delay)=>{const id=++next;scheduled.set(id,callback);delays.push(delay);return id;};
const cancel=id=>scheduled.delete(id);
const tick=()=>{const [id,callback]=scheduled.entries().next().value;scheduled.delete(id);clock+=100;return callback();};
const listeners=new Map();
const doc={hidden:false,addEventListener:(name,fn)=>listeners.set(name,fn),
  removeEventListener:name=>listeners.delete(name)};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
"""


def test_snapshot_headers_do_not_change_json_or_track_writes_jobs_auth():
    run_node(HARNESS + r"""
const data=response(10,{stale:true,stamp:oldStamp,generation:"one"});
assert.equal(JSON.stringify(data),'{"amount":10}');
assert.equal(snapshotOf(data).generation,"one");
assert.equal(snapshotOf(data).stale,true);
assert.equal(snapshotOf(data).url,"/api/dashboard/resources?currency=USD&days=30");
const headers=new Headers({"X-CloudLens-Cache":"stale"});
for(const path of ["/api/jobs/a","/api/warehouse?job_id=a","/api/auth/me","/api/ask","/api/schedules/refresh"]) {
  assert.equal(snapshotOf(recordSnapshot({},path,headers)),undefined);
}
assert.equal(snapshotOf(recordSnapshot({},url,headers,"POST")),undefined);
assert.equal(readSnapshotHeaders(new Headers()),null);
assert.equal(readSnapshotHeaders(headers).cache,"stale","Reports can parse headers without JSON mutation");
""")


def test_cached_content_and_asof_wait_for_fresh_data_and_successful_paint():
    run_node(HARNESS + r"""
let owner="A",shown=10,resolveRead,resolvePaint;
const states=[];
const flow=createSnapshotFlow({
  request:()=>new Promise(resolve=>{resolveRead=resolve;}),
  isCurrent:context=>context===owner,onData:(_,data)=>{assert.equal(shown,10);},
  onRefresh:()=>new Promise(resolve=>{resolvePaint=()=>{shown=20;resolve();};}),
  onStatus:state=>states.push(state),document:doc,schedule,cancel,now:()=>clock,
});
flow.activate("A");flow.observe(response(10,{stale:true,stamp:oldStamp}));
const pending=tick();await flush();
assert.equal(shown,10);assert.equal(states.at(-1).asOf,oldStamp);
resolveRead(response(20));await flush();
assert.equal(shown,10);assert.equal(states.at(-1).asOf,oldStamp);
assert.equal(states.at(-1).updating,true);
resolvePaint();await pending;
assert.equal(shown,20);assert.equal(states.at(-1).asOf,newStamp);
assert.equal(states.at(-1).updating,false);
assert.equal(scheduled.size,0,"fresh snapshots stop polling");
flow.stop();
""")


def test_revalidation_updates_existing_promise_cache_and_one_shot_live_reads():
    run_node(HARNESS + r"""
const old=response(10,{stale:true,stamp:oldStamp});
const cache=new Map([["resource-context",{value:Promise.resolve(old)}]]);
let owner="A",painted,reads=0;
const view=createViewRevalidation({
  request:async()=>{reads++;return response(20);},cache,isCurrent:context=>context===owner,
  statusHost:()=>null,onRefresh:async()=>{
    painted=await cache.get("resource-context").value;
    assert.equal((await view.read(url)).amount,20);
  },document:doc,schedule,cancel,now:()=>clock,
});
view.activate("A");
await view.watch(cache.get("resource-context").value,"A",1,token=>token===1,"resource-context");
await tick();
assert.equal(painted.amount,20);assert.equal(reads,1,"no stale in-memory replay or redundant GET on repaint");
assert.equal((await cache.get("resource-context").value).amount,20);
view.stop();
""")


def test_hidden_or_departed_views_do_not_poll_or_accept_late_results():
    run_node(HARNESS + r"""
let owner="A",resolveRead,signal,paints=0,reads=0;
const flow=createSnapshotFlow({
  request:(_,options)=>{reads++;signal=options.signal;return new Promise(resolve=>{resolveRead=resolve;});},
  isCurrent:context=>context===owner,onRefresh:()=>{paints++;},
  document:doc,schedule,cancel,now:()=>clock,
});
doc.hidden=true;flow.activate("A");flow.observe(response(10,{stale:true,stamp:oldStamp}));
assert.equal(scheduled.size,0);
doc.hidden=false;listeners.get("visibilitychange")();
const pending=tick();await flush();assert.equal(reads,1);
owner="B";flow.activate("B");
assert.equal(signal.aborted,true);
resolveRead(response(999));await pending;
assert.equal(paints,0);assert.equal(flow.take(url),undefined);
flow.stop();
""")


def test_failed_sources_back_off_and_stop_without_replacing_last_good():
    run_node(HARNESS + r"""
let reads=0,paints=0;
const states=[];
const flow=createSnapshotFlow({
  request:async()=>{reads++;return response(10,{stale:true,error:"source_failed",stamp:oldStamp});},
  isCurrent:()=>true,onData:()=>{paints++;},onStatus:state=>states.push(state),
  document:doc,schedule,cancel,now:()=>clock,maxAttempts:2,
});
flow.activate("A");flow.observe(response(10,{stale:true,stamp:oldStamp}));
await tick();await tick();
assert.equal(reads,2);assert.equal(paints,0);
assert.equal(scheduled.size,0);assert.equal(states.at(-1).exhausted,true);
assert.equal(states.at(-1).error,true);assert.equal(states.at(-1).asOf,oldStamp);
assert.ok(delays[1]>delays[0],"failed sources back off");
flow.retry();assert.equal(scheduled.size,1);
flow.stop();
""")


def test_repaint_failure_retains_displayed_timestamp_and_cached_revisit_probes():
    run_node(HARNESS + r"""
let fail=true,reads=0,paints=0,owner="A";
const states=[];
const flow=createSnapshotFlow({
  request:async()=>{reads++;return response(20);},isCurrent:context=>context===owner,
  onRefresh:()=>{paints++;if(fail)throw Error("Failed to repaint");},
  onStatus:state=>states.push(state),document:doc,schedule,cancel,now:()=>clock,
});
flow.activate("A");flow.observe(response(10,{stale:true,stamp:oldStamp}));
await tick();
assert.equal(states.at(-1).error,true);assert.equal(states.at(-1).asOf,oldStamp);
assert.equal(flow.take(url),undefined,"staged bodies expire with the repaint");
fail=false;await tick();
assert.equal(states.at(-1).asOf,newStamp);assert.equal(states.at(-1).error,false);
owner="B";flow.activate("B");owner="A";flow.activate("A");
await tick();
assert.equal(reads,3);assert.equal(paints,2,"unchanged revisit does not rebuild the view");
flow.stop();
""")


def test_newer_same_context_response_wins_and_freshness_episodes_restart():
    run_node(HARNESS + r"""
let resolveRead,paints=0;
const states=[];
const flow=createSnapshotFlow({
  request:()=>new Promise(resolve=>{resolveRead=resolve;}),isCurrent:()=>true,
  onData:()=>{paints++;},onStatus:state=>states.push(state),
  document:doc,schedule,cancel,now:()=>clock,maxElapsed:1000,
});
flow.activate("A");
clock=10000;
flow.observe(response(10,{stale:true,stamp:oldStamp}));
const pending=tick();await flush();
assert.ok(resolveRead,"late first stale response starts its own bounded episode");
flow.observe(response(30,{stamp:"2026-09-14T12:00:00Z"}));
resolveRead(response(20));await pending;
assert.equal(paints,0,"slower poll cannot overwrite a newer response in the same context");
assert.equal(states.at(-1).asOf,"2026-09-14T12:00:00Z");
clock+=10000;
flow.observe(response(30,{stamp:"2026-09-14T12:00:00Z",stale:true}));
assert.equal(scheduled.size,1,"a settled view can revalidate again later");
flow.stop();
""")
