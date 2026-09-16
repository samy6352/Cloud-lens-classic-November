"""Offline production Savings rendering and selected-chip contrast regressions."""
import json
import runpy
from pathlib import Path

from app import savings

ROOT = Path(__file__).resolve().parents[1]
run_node = runpy.run_path(str(ROOT / "tests" / "test_tags_frontend.py"))["run_node"]


def test_savings_rendering_evidence_counts_filters_and_theme_contrast():
    recommendation = {
        "problem": "Review disks that are not attached to a VM",
        "resource": "disk", "resourceGroup": "group", "region": "southindia", "currency": "USD",
    }
    payload = savings.assemble({"advisor": {"recommendations": [
        {**recommendation, "resource_id": f"/disks/{name}", "resource": name, **fields}
        for name, fields in [
            ("unknown", {}), ("null", {"annual_savings": None}),
            ("zero", {"annual_savings": 0}), ("priced", {"annual_savings": 365}),
        ]
    ]}}, {}, 30)
    payload["scan"] = {"status": "complete", "errors": {}}
    run_node("const payload = " + json.dumps(payload) + ";\n" + r"""
import assert from "node:assert/strict";
import {readFile, mkdir, rm} from "node:fs/promises";
import {join, basename} from "node:path";
import {randomUUID} from "node:crypto";
const workspace = join(process.cwd(), ".pytest_cache", "savings-browser-" + randomUUID());
await mkdir(workspace, {recursive:true});
process.env.TEMP = process.env.TMP = process.env.TMPDIR = workspace;
let browser;
try {
  const {chromium} = await import("playwright");
  browser = await chromium.launch({headless:true});
  const page = await browser.newPage({reducedMotion:"reduce"});
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const assets = new Set(["app.css","dashboard-optimize.js","dashboard-savings-scan.js","dashboard-currencies.js","cost-totals.js"]);
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== "savings.test") {
      errors.push("No external network permitted"); return route.abort();
    }
    const name = basename(url.pathname);
    if (assets.has(name)) return route.fulfill({
      contentType:name.endsWith(".css") ? "text/css" : "text/javascript",
      body:await readFile(join(process.cwd(),"web","assets",name),"utf8"),
    });
    if (url.pathname === "/") return route.fulfill({contentType:"text/html",
      body:'<!doctype html><html data-theme="dark"><head><link rel="stylesheet" href="/assets/app.css"></head><body><main id="tabBody"></main></body></html>'});
    errors.push("Unexpected request: " + url); return route.abort();
  });
  await page.goto("http://savings.test/");
  await page.evaluate(async data => {
    const {createTabs} = await import("/assets/dashboard-optimize.js");
    const esc = value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;");
    const money = (value, currency, precision=2) => {
      if (typeof value !== "number" || !Number.isFinite(value)) throw Error("Unavailable value sent to money");
      return `${currency} ${value.toFixed(precision)}`;
    };
    window.renderSavings = async data => {
      document.querySelector("#tabBody").innerHTML = "";
      const tabs = createTabs({
        $:id=>document.getElementById(id), isCurrent:()=>true, scopeIds:()=>[], portalScopeIds:()=>[],
        esc, money, moneyHtml:money, stableKey:String, portalTargets:()=>[],
        portalLinkMarkup:(_,__,name)=>esc(name), qs:()=>"", key:()=>"", selectTab:()=>{},
        get:async url=>{
          if (!url.startsWith("/api/dashboard/savings/progress")) throw Error("Unexpected API " + url);
          return data;
        },
        setContent:(node,html)=>{node.innerHTML=html;},
        kpiRow:rows=>rows.map(row=>`<div class="test-kpi"><b>${esc(row.label)}</b><span>${esc(row.value)}</span><small>${esc(row.sub)}</small></div>`).join(""),
      });
      try { await tabs.renderSavings(1); } finally { tabs.stop(); }
    };
    await window.renderSavings(data);
  }, payload);
  const card = name => page.locator(".sv-card").filter({has:page.locator(".sv-where").filter({hasText:new RegExp("^"+name+" ·")})});
  assert.equal(await page.locator(".sv-card").count(),4);
  for (const name of ["unknown","null"]) {
    assert.equal(await card(name).locator(".sv-window").textContent(),"Not available");
    assert.match(await card(name).locator(".sv-window-note").textContent(),/Provider saving estimate unavailable/);
    await card(name).locator(".sv-head").click();
    assert.equal(await card(name).locator(".sv-body").isVisible(),true);
    assert.match(await card(name).locator(".sv-src-claim").textContent(),/Provider saving estimate unavailable/);
    assert.doesNotMatch(await card(name).textContent(),/about 0|USD 0/);
  }
  assert.equal(await card("zero").locator(".sv-window").textContent(),"USD 0.00");
  assert.match(await card("zero").locator(".sv-annual").textContent(),/USD 0 a year/);
  assert.equal(await card("priced").locator(".sv-window").textContent(),"USD 30.00");
  assert.match(await card("priced").locator(".sv-annual").textContent(),/USD 365 a year/);
  assert.match(await page.locator(".test-kpi").filter({hasText:"Additional estimate"}).textContent(),/USD 30.00.*2 unpriced, excluded/);
  assert.match(await page.locator('button[data-cat="all"]').textContent(),/Everything.*4.*USD 30 \(partial\)/s);
  assert.match(await page.locator("#tabBody").textContent(),/2 opportunities have no available saving estimate/);
  for (const theme of ["dark","light","vivid","paper"]) {
    await page.evaluate(async theme => {
      document.documentElement.dataset.theme = theme;
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      await Promise.all(document.getAnimations().map(animation=>animation.finished.catch(()=>{})));
    }, theme);
    for (const category of ["all","orphaned"]) {
      await page.locator(`button[data-cat="${category}"]`).click();
      await page.evaluate(async () => {
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        await Promise.all(document.getAnimations().map(animation=>animation.finished.catch(()=>{})));
      });
      const result = await page.locator(`button[data-cat="${category}"] .sv-chip-m`).evaluate(element=>{
        const style=getComputedStyle(element), parent=getComputedStyle(element.parentElement);
        const channels=value=>value.match(/[\d.]+/g).slice(0,3).map(Number);
        const bg=channels(parent.backgroundColor), fg=channels(style.color);
        const opacity=Number(style.opacity);
        const composite=fg.map((value,index)=>value*opacity+bg[index]*(1-opacity));
        const luminance=rgb=>rgb.map(value=>value/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4)
          .reduce((sum,value,index)=>sum+value*[.2126,.7152,.0722][index],0);
        const a=luminance(composite),b=luminance(bg);
        return {ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),fg:style.color,bg:parent.backgroundColor,opacity};
      });
      assert.ok(result.ratio>=4.5,`${theme}/${category}: ${JSON.stringify(result)}`);
      assert.equal(await page.locator(".sv-card").count(),4,"filters retain unpriced opportunities");
      await page.mouse.move(0,0);
      await page.evaluate(async () => {
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        await Promise.all(document.getAnimations().map(animation=>animation.finished.catch(()=>{})));
      });
      const resting = await page.locator(`button[data-cat="${category}"] .sv-chip-m`).evaluate(element=>({
        fg:getComputedStyle(element).color,bg:getComputedStyle(element.parentElement).backgroundColor,
        opacity:Number(getComputedStyle(element).opacity),
      }));
      assert.deepEqual(resting,{fg:result.fg,bg:result.bg,opacity:result.opacity},
        `${theme}/${category}: selected rest and hover must use the same readable palette`);
      console.log(`${theme}/${category}: ${result.ratio.toFixed(2)}:1`);
    }
  }
  const unknownOnly={...payload,opportunities:payload.opportunities.filter(row=>row.window==null),
    total:null,projected_total:null,count:2,categories:[{id:"orphaned",label:"Orphaned",count:2,total:null}]};
  await page.evaluate(data=>window.renderSavings(data),unknownOnly);
  assert.match(await page.locator(".test-kpi").filter({hasText:"Additional estimate"}).textContent(),/Not available/);
  assert.match(await page.locator('button[data-cat="all"]').textContent(),/Everything.*2.*Not available/s);
  const missing={...unknownOnly,opportunities:unknownOnly.opportunities.map(row=>{
    const copy={...row};delete copy.window;delete copy.annual;return copy;
  })};
  await page.evaluate(data=>window.renderSavings(data),missing);
  assert.deepEqual(await page.locator(".sv-window").allTextContents(),["Not available","Not available"]);
  const folded={...payload,opportunities:[{...payload.opportunities[0],folded:2,options:[]}]};
  await page.evaluate(data=>window.renderSavings(data),folded);
  await page.locator(".sv-head").click();
  assert.match(await page.locator(".sv-folded").textContent(),/evaluated this 3 times/);
  const options={...payload,opportunities:[{...payload.opportunities[0],options:[
    {label:"priced",annual:365},{label:"zero",annual:0},{label:"unknown",annual:null},
  ]}],commitment_count:1,commitment_annual:null};
  await page.evaluate(data=>window.renderSavings(data),options);
  await page.locator(".sv-head").click();
  assert.deepEqual((await page.locator(".sv-opt-money").allTextContents()).map(value=>value.replace(/\s+/g," ").trim()),
    ["USD 365 a year","USD 0 a year","Not available a year"]);
  assert.match(await page.locator("#cmtPointer").textContent(),/Provider saving estimate unavailable/);
  assert.doesNotMatch(await page.locator("#cmtPointer").textContent(),/USD 0/);
  assert.deepEqual(errors,[]);
} finally {
  if (browser) await browser.close();
  await rm(workspace,{recursive:true,force:true});
}
""")
