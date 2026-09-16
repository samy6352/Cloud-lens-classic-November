/**
 * Performance audit for CloudLens: load, motion, scrolling and interaction latency.
 *
 * The accessibility audit next door answers "is this correct". This answers "does this feel
 * fast", which is a different question with different numbers behind it, and one that is very
 * easy to lie to yourself about by eye. Every figure here is measured on a live instance.
 *
 *   CLOUDLENS_USER=... CLOUDLENS_PASSWORD=... node tests/audit_perf.mjs
 *   PERF_CPU=4 node tests/audit_perf.mjs        # throttle to a mid-range laptop
 *   PERF_JSON=before.json node tests/audit_perf.mjs   # write a file to diff against later
 *
 * What it reports, and why each one is here:
 *
 *   load       FCP, LCP, CLS, and the blocking time of long tasks. CLS is the one that makes an
 *              app feel amateur -- content moving under a reader's eyes -- and it is invisible
 *              in a screenshot.
 *   weight     bytes over the wire before first interaction, split by type. What is downloaded
 *              but not used is what a slow connection pays for and gets nothing back.
 *   tabs       time from click to the tab's panel being painted. This is the app's single most
 *              repeated interaction.
 *   scroll     frame intervals during a programmatic scroll. A 60 Hz frame is nominally 16.7ms
 *              and requestAnimationFrame routinely reports 16.8; counting those as drops made
 *              a perfectly smooth scroll look like it dropped a quarter of its frames. The
 *              threshold here is 20ms — late enough to mean the frame genuinely slipped.
 *   interact   time from a click on ordinary controls to the next paint -- the INP proxy.
 */
import { readFile, writeFile } from "node:fs/promises";

import { chromium } from "playwright";

const BASE = process.env.CLOUDLENS_URL || "http://localhost:8100";
const CPU = Number(process.env.PERF_CPU || 1);
// A 60 Hz frame is 16.7ms and rAF reports 16.8 for a frame that was not late at all.
const LATE_FRAME_MS = 20;
const TABS = ["overview", "billing", "resources", "savings", "monitor", "governance", "reports"];
const READY = () => document.documentElement.dataset.dashboardReady === "true";

const round = (n, places = 1) => Math.round(n * 10 ** places) / 10 ** places;

/** Collected as the page loads, so the observers have to be installed before it does. */
const OBSERVE = () => {
  window.__perf = { lcp: 0, cls: 0, shifts: [], longTasks: [], fcp: 0 };
  const p = window.__perf;
  new PerformanceObserver((l) => {
    for (const e of l.getEntries()) if (e.name === "first-contentful-paint") p.fcp = e.startTime;
  }).observe({ type: "paint", buffered: true });
  new PerformanceObserver((l) => {
    for (const e of l.getEntries()) p.lcp = Math.max(p.lcp, e.startTime);
  }).observe({ type: "largest-contentful-paint", buffered: true });
  new PerformanceObserver((l) => {
    for (const e of l.getEntries()) {
      if (e.hadRecentInput) continue;
      p.cls += e.value;
      // The node is what makes a shift fixable; a score on its own names no culprit.
      const source = (e.sources || []).find((s) => s.node) || {};
      const node = source.node;
      p.shifts.push({
        value: e.value,
        node: node ? `${node.tagName || node.nodeName}${node.id ? "#" + node.id : ""}`
          + `${node.className && typeof node.className === "string"
            ? "." + node.className.trim().split(/\s+/).slice(0, 2).join(".") : ""}` : "?",
      });
    }
  }).observe({ type: "layout-shift", buffered: true });
  new PerformanceObserver((l) => {
    for (const e of l.getEntries()) p.longTasks.push(e.duration);
  }).observe({ type: "longtask", buffered: true });
};

/** Frame intervals over `ms`, while `during` runs. See LATE_FRAME_MS for what counts as slipped. */
const FRAMES = (ms, during) => new Promise((resolve) => {
  const times = [];
  let last = performance.now();
  const stop = performance.now() + ms;
  const tick = (now) => {
    times.push(now - last);
    last = now;
    if (now < stop) requestAnimationFrame(tick);
    else resolve(times.slice(1));
  };
  requestAnimationFrame(tick);
  if (during) during();
});

async function login(context) {
  if (!process.env.CLOUDLENS_USER) return;
  const r = await context.request.post(`${BASE}/api/auth/login`, {
    data: { username: process.env.CLOUDLENS_USER, password: process.env.CLOUDLENS_PASSWORD },
  });
  if (!r.ok()) throw new Error(`sign-in failed: ${r.status()}`);
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
await login(context);

const report = { base: BASE, cpu: CPU, at: new Date().toISOString() };

// ---- load -------------------------------------------------------------------------------
const page = await context.newPage();
if (CPU > 1) {
  const cdp = await context.newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU });
}
await page.addInitScript(OBSERVE);

const weight = new Map();
page.on("response", async (response) => {
  const url = response.url();
  if (!url.startsWith(BASE)) return;
  const kind = /\.css($|\?)/.test(url) ? "css"
    : /\.(js|mjs)($|\?)/.test(url) ? "js"
      : /\.(svg|png|jpg|woff2?)($|\?)/.test(url) ? "media"
        : url.includes("/api/") ? "api" : "doc";
  const length = Number(response.headers()["content-length"] || 0)
    || (await response.body().catch(() => Buffer.alloc(0))).length;
  const row = weight.get(kind) || { bytes: 0, count: 0 };
  row.bytes += length; row.count += 1;
  weight.set(kind, row);
});

const started = Date.now();
await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.waitForFunction(READY, null, { timeout: 120000 });
const ready = Date.now() - started;
// Let late work (charts, deferred modules) land in the numbers rather than escape them.
await page.waitForTimeout(6000);

const load = await page.evaluate(() => {
  const p = window.__perf;
  const nav = performance.getEntriesByType("navigation")[0] || {};
  const blocking = p.longTasks.reduce((sum, d) => sum + Math.max(0, d - 50), 0);
  const worst = p.shifts.sort((a, b) => b.value - a.value).slice(0, 6);
  return {
    fcp: p.fcp, lcp: p.lcp, cls: p.cls,
    domContentLoaded: nav.domContentLoadedEventEnd || 0,
    longTasks: p.longTasks.length, blockingMs: blocking,
    worstTask: Math.max(0, ...p.longTasks),
    shifts: worst.map((s) => ({ value: Math.round(s.value * 1000) / 1000, node: s.node })),
  };
});
report.load = { ...load, fcp: round(load.fcp), lcp: round(load.lcp), cls: round(load.cls, 3),
  blockingMs: round(load.blockingMs), worstTask: round(load.worstTask), readyMs: ready };
report.weight = Object.fromEntries([...weight].map(([k, v]) => [k, { kb: round(v.bytes / 1024), n: v.count }]));

// ---- tab switching ----------------------------------------------------------------------
report.tabs = [];
for (const tab of TABS) {
  const button = page.locator(`#primary-${tab}`);
  if (!(await button.count())) continue;
  const result = await page.evaluate(async (id) => {
    const el = document.getElementById(`primary-${id}`);
    const t0 = performance.now();
    el.click();
    // Two frames: the first is when the click's own work is done, the second is the paint.
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const painted = performance.now() - t0;
    return { painted };
  }, tab);
  const frames = await page.evaluate(FRAMES, 700);
  await page.waitForFunction(READY, null, { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2500);
  report.tabs.push({
    tab, paintedMs: round(result.painted),
    dropped: frames.filter((f) => f > LATE_FRAME_MS).length,
    worstFrameMs: round(Math.max(0, ...frames)),
  });
}

// ---- scrolling --------------------------------------------------------------------------
report.scroll = [];
for (const tab of ["overview", "resources", "reports"]) {
  const button = page.locator(`#primary-${tab}`);
  if (!(await button.count())) continue;
  await button.click();
  await page.waitForFunction(READY, null, { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(3000);
  const frames = await page.evaluate(async (source) => {
    const measure = new Function(`return (${source})`)();
    const target = document.scrollingElement;
    let y = 0;
    return await measure(1400, () => {
      const step = () => {
        y += 34;
        target.scrollTop = y;
        if (y < 1600) setTimeout(step, 16);
      };
      step();
    });
  }, FRAMES.toString());
  report.scroll.push({
    tab, frames: frames.length,
    dropped: frames.filter((f) => f > LATE_FRAME_MS).length,
    p95FrameMs: round(frames.sort((a, b) => a - b)[Math.floor(frames.length * 0.95)] || 0),
    worstFrameMs: round(Math.max(0, ...frames)),
  });
  await page.evaluate(() => { document.scrollingElement.scrollTop = 0; });
}

// ---- interaction latency ----------------------------------------------------------------
await page.locator("#primary-overview").click().catch(() => {});
await page.waitForTimeout(2500);
report.interact = await page.evaluate(async () => {
  const out = [];
  const targets = [
    ["period 7d", '#period button[data-days="7"]'],
    ["period 30d", '#period button[data-days="30"]'],
    ["theme", "#themeBtn, [data-theme-toggle], #themeToggle"],
    ["tab search", "#tabSearchBtn"],
  ];
  for (const [name, selector] of targets) {
    const el = document.querySelector(selector);
    if (!el || el.disabled) { out.push({ name, skipped: true }); continue; }
    const t0 = performance.now();
    el.click();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    out.push({ name, ms: Math.round((performance.now() - t0) * 10) / 10 });
    await new Promise((r) => setTimeout(r, 400));
    document.querySelector("#tabSearchDialog")?.close?.();
    document.body.click();
  }
  return out;
});

// ---- animation hygiene ------------------------------------------------------------------
// Transitions on width/height/top/left run on the layout thread and cannot hit 60 fps on a
// busy page. This finds them in the stylesheet rather than waiting to feel them.
const css = await readFile(new URL("../web/assets/app.css", import.meta.url), "utf8");
const LAYOUT_PROPS = /\b(width|height|top|left|right|bottom|margin|padding|font-size|line-height)\b/;
const offenders = [];
for (const match of css.matchAll(/transition:\s*([^;{}]+);/g)) {
  const value = match[1].replace(/\s+/g, " ").trim();
  if (!LAYOUT_PROPS.test(value) || /^all\b/.test(value)) continue;
  const line = css.slice(0, match.index).split("\n").length;
  offenders.push({ line, value: value.slice(0, 90) });
}
const alls = [...css.matchAll(/transition:\s*all\b[^;{}]*;/g)].map((m) => ({
  line: css.slice(0, m.index).split("\n").length, value: m[0].slice(0, 90),
}));
report.animation = { layoutTransitions: offenders, transitionAll: alls };

await context.close().catch(() => {});
await browser.close().catch(() => {});

// ---- print ------------------------------------------------------------------------------
const L = report.load;
console.log(`\nLOAD  (cpu x${CPU})`);
console.log(`  FCP ${L.fcp} ms   LCP ${L.lcp} ms   ready ${L.readyMs} ms`);
console.log(`  CLS ${L.cls}${L.cls > 0.1 ? "  <-- over the 0.1 'good' threshold" : ""}`);
console.log(`  long tasks ${L.longTasks}, blocking ${L.blockingMs} ms, worst ${L.worstTask} ms`);
for (const s of L.shifts) console.log(`    shift ${s.value}  ${s.node}`);

console.log(`\nWEIGHT to first interaction`);
for (const [kind, v] of Object.entries(report.weight)) {
  console.log(`  ${kind.padEnd(6)} ${String(v.kb).padStart(8)} KB over ${v.n} request(s)`);
}

console.log(`\nTAB SWITCH`);
for (const t of report.tabs) {
  console.log(`  ${t.tab.padEnd(12)} paint ${String(t.paintedMs).padStart(7)} ms`
    + `   late ${String(t.dropped).padStart(3)} frames   worst ${t.worstFrameMs} ms`);
}

console.log(`\nSCROLL`);
for (const s of report.scroll) {
  console.log(`  ${s.tab.padEnd(12)} ${String(s.dropped).padStart(3)}/${s.frames} frames late`
    + `   p95 ${String(s.p95FrameMs).padStart(6)} ms   worst ${s.worstFrameMs} ms`);
}

console.log(`\nINTERACTION`);
for (const i of report.interact) {
  console.log(`  ${i.name.padEnd(12)} ${i.skipped ? "(not present)" : `${i.ms} ms`}`);
}

console.log(`\nANIMATION HYGIENE`);
console.log(`  transitions on layout properties: ${report.animation.layoutTransitions.length}`);
for (const o of report.animation.layoutTransitions.slice(0, 12)) {
  console.log(`    app.css:${o.line}  ${o.value}`);
}
console.log(`  'transition: all': ${report.animation.transitionAll.length}`);
for (const o of report.animation.transitionAll.slice(0, 8)) {
  console.log(`    app.css:${o.line}  ${o.value}`);
}

if (process.env.PERF_JSON) {
  await writeFile(process.env.PERF_JSON, JSON.stringify(report, null, 2));
  console.log(`\nwrote ${process.env.PERF_JSON}`);
}
