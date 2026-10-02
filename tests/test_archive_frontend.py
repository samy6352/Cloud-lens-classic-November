"""Real-browser archive controls with local module bytes and fully mocked APIs."""
from tests.test_tags_frontend import run_node


def test_archive_controls_poll_independently_and_preserve_approval_keys():
    run_node(r"""
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {chromium} from "playwright";
const source=await readFile("./web/assets/dashboard-archive.js","utf8");
const browser=await chromium.launch({headless:true});
const context=await browser.newContext();
const page=await context.newPage();
let state={enabled:true,jobs:[],read_only:false}, admin=true, fail=true;
const writes=[],reads=[];
await context.route("**/*",async route=>{
  const req=route.request(), url=new URL(req.url());
  if(url.pathname==="/assets/dashboard-archive.js")
    return route.fulfill({body:source,contentType:"text/javascript"});
  if(url.pathname==="/")return route.fulfill({contentType:"text/html",body:"<div id='archiveNote'></div>"});
  if(req.method()==="GET"){
    reads.push(url.search);
    if(url.searchParams.has("jobs_only"))return route.fulfill({json:{...state,admin}});
    return route.fulfill({json:{enabled:true,reachable:false,error:"Storage blocked"}});
  }
  writes.push(url.href);
  if(fail){fail=false;return route.fulfill({status:503,json:{detail:"Temporary queue failure"}});}
  state={...state,jobs:[{job_id:"job/1",status:"queued",can_cancel:true,progress:{phase:"queued"}}]};
  return route.fulfill({status:202,json:{job_id:"job/1"}});
});
try {
  await page.goto("https://archive-fixture.test/");
  await page.evaluate(async()=>{
    const {createArchiveStatus}=await import("/assets/dashboard-archive.js");
    window.feedback=[];window.timers=new Map();window.sequence=0;
    window.setTimeout=(fn,delay)=>{window.timers.set(++window.sequence,{fn,delay});return window.sequence;};
    window.clearTimeout=id=>window.timers.delete(id);
    window.view=createArchiveStatus({
      $:id=>document.getElementById(id),
      get:async url=>{const r=await fetch(url);if(!r.ok)throw Error("Read failed");return r.json();},
      whoAmI:async()=>{const r=await fetch("/api/archive?jobs_only=true");const a=await r.json();return{user:{admin:a.admin}};},
      esc:v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
      toast:(...args)=>window.feedback.push(args),
    });
    await window.view.render();
  });
  await page.locator("#archiveNow").waitFor();
  assert.match(await page.locator("[data-archive-storage]").innerText(),/Storage blocked/);
  await page.locator("#archiveNow").click();
  await page.waitForFunction(()=>window.feedback.some(f=>f[0]==="Temporary queue failure"));
  assert.equal(await page.locator("#archiveNow").isEnabled(),true);
  await page.locator("#archiveNow").click();
  await page.waitForFunction(()=>document.querySelector("[data-archive-jobs]")?.textContent.includes("queued"));
  assert.equal(writes.length,2);
  assert.equal(new URL(writes[0]).searchParams.get("idempotency_key"),
    new URL(writes[1]).searchParams.get("idempotency_key"));
  assert.ok(new URL(writes[0]).searchParams.get("idempotency_key"));
  assert.equal(await page.evaluate(()=>window.feedback.some(f=>f[0].includes("Archive queued (job/1)"))),true);
  assert.equal(await page.evaluate(()=>[...window.timers.values()].some(t=>t.delay===5000)),true);
  const storageReads=reads.filter(q=>q.includes("limit")).length;
  state={...state,jobs:[{job_id:"job/1",status:"ready",result:{name:"actual/today.parquet"},
    progress:{rows:123,uploaded_bytes:999,phase:"complete"}}]};
  await page.evaluate(()=>{const [id,t]=[...window.timers].find(([,t])=>t.delay===5000);window.timers.delete(id);t.fn();});
  await page.waitForFunction(()=>document.querySelector("[data-archive-jobs]")?.textContent.includes("actual/today.parquet"));
  assert.equal(reads.filter(q=>q.includes("limit")).length,storageReads);
  assert.equal(await page.evaluate(()=>window.timers.size),0);
  assert.match(await page.locator("[data-archive-jobs]").innerText(),/123 rows/);
  state={...state,jobs:[{job_id:"job/1",status:"failed",can_resume:true,
    detail:"<img src=x onerror=window.injected=1>",progress:{phase:"uploading"}}]};
  await page.evaluate(()=>window.view.render());
  await page.locator("[data-archive-resume]").waitFor();
  assert.equal(await page.locator("[data-archive-jobs] img").count(),0);
  assert.equal(await page.evaluate(()=>!!window.injected),false);
  await page.locator("[data-archive-resume]").click();
  await page.locator("[data-archive-cancel]").waitFor();
  assert.ok(writes.at(-1).includes("/api/jobs/job%2F1/resume"));
  await page.locator("[data-archive-cancel]").click();
  assert.ok(writes.at(-1).includes("/api/archive/jobs/job%2F1/cancel"));
  state={...state,read_only:true};
  await page.evaluate(()=>window.view.render());
  await page.waitForFunction(()=>!document.getElementById("archiveNow"));
  assert.equal(await page.locator("[data-archive-resume],[data-archive-cancel]").count(),0);
  state={...state,read_only:false};admin=false;
  await page.evaluate(()=>window.view.render());
  await page.waitForFunction(()=>!document.querySelector("[data-archive-cancel]"));
  assert.equal(await page.locator("#archiveNow").count(),0);
} finally {await context.close();await browser.close();}
""")


def test_repaint_does_not_allow_old_poll_to_overwrite_new_controls():
    run_node(r"""
import assert from "node:assert/strict";
import {createArchiveStatus} from "./web/assets/dashboard-archive.js";
const oldPanel={isConnected:true,innerHTML:"",insertAdjacentHTML(){},querySelectorAll:()=>[]};
const newPanel={...oldPanel};
const storage={isConnected:true,innerHTML:""};
let panel=oldPanel;
const host={isConnected:true,innerHTML:"",querySelector:selector=>selector.includes("storage")?storage:panel};
const pending=[];
const view=createArchiveStatus({
  $:()=>host,esc:String,toast(){},whoAmI:async()=>({user:{admin:false}}),
  get:url=>url.includes("jobs_only")?new Promise(resolve=>pending.push(resolve))
    :Promise.resolve({enabled:false}),
});
await view.render();
panel=newPanel;
await view.render();
assert.equal(pending.length,2);
pending[1]({enabled:false,jobs:[{status:"ready",result:{name:"new"}}]});
await new Promise(resolve=>setImmediate(resolve));
assert.match(newPanel.innerHTML,/new/);
pending[0]({enabled:false,jobs:[{status:"ready",result:{name:"old"}}]});
await new Promise(resolve=>setImmediate(resolve));
assert.doesNotMatch(newPanel.innerHTML,/old/);
assert.equal(host.archivePolling,null);
""")
