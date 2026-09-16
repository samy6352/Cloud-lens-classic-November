/**
 * Accessibility audit across every CloudLens tab, using axe-core.
 *
 *   CLOUDLENS_USER=... CLOUDLENS_PASSWORD=... node tests/audit_a11y.mjs
 *
 * Reports violations grouped by rule, with the tab each was seen on and one example target,
 * so a fix can be aimed at the component rather than at a list of duplicate nodes.
 */
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.CLOUDLENS_URL || "http://localhost:8100";
// Contrast is a property of the palette, so the audit has to be able to name which one it is
// auditing. Defaults to whatever the browser would choose, which is what a first visit gets.
const THEME = process.env.CLOUDLENS_THEME || "";
const TABS = ["overview", "billing", "resources", "savings", "monitor", "governance", "reports"];

const axeSource = await readFile(join(HERE, "..", "node_modules", "axe-core", "axe.min.js"), "utf8");

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const findings = new Map();
const contrast = new Set();

try {
  if (process.env.CLOUDLENS_USER) {
    const login = await context.request.post(`${BASE}/api/auth/login`, {
      data: { username: process.env.CLOUDLENS_USER, password: process.env.CLOUDLENS_PASSWORD },
    });
    if (!login.ok()) throw new Error(`sign-in failed: ${login.status()}`);
  }
  const page = await context.newPage();
  if (THEME) await page.addInitScript((t) => localStorage.setItem("costTheme", t), THEME);
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true",
    null, { timeout: 120000 });
  const active = await page.evaluate(() => document.documentElement.dataset.theme);
  console.log(`auditing the "${active}" theme`);

  for (const tab of TABS) {
    const button = page.locator(`#primary-${tab}`);
    if (await button.count()) {
      await button.click();
      await page.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true",
        null, { timeout: 120000 }).catch(() => {});
      await page.waitForTimeout(9000);
    }
    await page.evaluate(axeSource);
    const result = await page.evaluate(async () => await window.axe.run(document, {
      resultTypes: ["violations"],
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"] },
    }));
    for (const violation of result.violations) {
      const seen = findings.get(violation.id) || {
        impact: violation.impact, help: violation.help, tabs: new Set(), nodes: 0, examples: new Set(),
      };
      seen.tabs.add(tab);
      seen.nodes += violation.nodes.length;
      for (const node of violation.nodes.slice(0, 2)) seen.examples.add(node.target.join(" "));
      // Contrast is the one rule where the numbers matter more than the selector: a fix has to
      // know the pair of colours and how far short it fell.
      if (violation.id === "color-contrast") {
        for (const node of violation.nodes) {
          const data = (node.any || []).map(c => c.data).find(d => d && d.contrastRatio) || {};
          contrast.add(JSON.stringify({
            tab, target: node.target.join(" ").slice(0, 70),
            ratio: data.contrastRatio, needs: data.expectedContrastRatio,
            fg: data.fgColor, bg: data.bgColor, size: data.fontSize, weight: data.fontWeight,
          }));
        }
      }
      findings.set(violation.id, seen);
    }
    console.log(`scanned ${tab}: ${result.violations.length} rule(s) violated`);
  }
} finally {
  await context.close().catch(() => {});
  await browser.close().catch(() => {});
}

const order = { critical: 0, serious: 1, moderate: 2, minor: 3 };
const rows = [...findings.entries()].sort((a, b) =>
  (order[a[1].impact] ?? 9) - (order[b[1].impact] ?? 9) || b[1].nodes - a[1].nodes);

console.log(`\n${"impact".padEnd(9)} ${"rule".padEnd(34)} ${"nodes".padStart(5)}  tabs`);
console.log("-".repeat(100));
for (const [id, f] of rows) {
  console.log(`${String(f.impact).padEnd(9)} ${id.padEnd(34)} ${String(f.nodes).padStart(5)}  ${[...f.tabs].join(",")}`);
  console.log(`          ${f.help}`);
  for (const example of [...f.examples].slice(0, 2)) console.log(`          e.g. ${example.slice(0, 110)}`);
}
console.log(`\n${rows.length} distinct rules violated across ${TABS.length} tabs`);

if (contrast.size) {
  const seen = new Set();
  console.log(`\ncontrast detail (unique colour pairs):`);
  for (const line of contrast) {
    const c = JSON.parse(line);
    const key = `${c.fg}|${c.bg}|${c.size}|${c.weight}`;
    if (seen.has(key)) continue;
    seen.add(key);
    console.log(`  ${String(c.ratio).padStart(5)} (needs ${c.needs})  fg ${c.fg} on bg ${c.bg}`
      + `  ${c.size}/${c.weight}  [${c.tab}] ${c.target}`);
  }
}
