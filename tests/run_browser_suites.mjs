/**
 * Run the browser_*.js suites against a live CloudLens instance.
 *
 * Each browser_*.js file is a bare `async (page, options) => {...}` function literal. They were
 * written to be handed an already-open, already-signed-in CloudLens page, and until now nothing
 * in the repository did that -- twenty-seven suites that never ran. This is that harness.
 *
 * A suite reports failure two ways, and both are honoured: by throwing, or by returning an
 * object carrying a non-empty `failures` / `errors` array (or a `failure` string with the
 * `phase` it reached).
 *
 *   node tests/run_browser_suites.mjs                  # every suite
 *   node tests/run_browser_suites.mjs overview tooltips # only matching names
 *   CLOUDLENS_URL=http://localhost:8100 node tests/run_browser_suites.mjs
 *   HEADED=1 node tests/run_browser_suites.mjs          # watch it work
 *
 * Sign-in uses a local account (AUTH_ALLOW_LOCAL): set CLOUDLENS_USER and CLOUDLENS_PASSWORD.
 * Create one with `python -m app.auth add <name>`. Entra SSO is deliberately not attempted --
 * it is interactive, and a test run must not be.
 */
import { createReadStream } from "node:fs";
import { readFile, readdir, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";
import { createCostDataFixture } from "./cost_data_browser_fixture.mjs";
import { waitForDashboard } from "./browser_runner_readiness.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = join(HERE, "..", "web");
const BASE = process.env.CLOUDLENS_URL || "http://localhost:8100";
const USER = process.env.CLOUDLENS_USER || "";
const PASSWORD = process.env.CLOUDLENS_PASSWORD || "";
// Suites open their own contexts and drive layout at five widths; a slow one is normal.
const SUITE_TIMEOUT_MS = Number(process.env.SUITE_TIMEOUT_MS || 300000);

// Several suites mock every API call and want only the raw asset files, so they read them from
// a plain static server rather than the app -- no auth, no middleware, no reporting-period
// header rewriting. The ports are written into the suites themselves ("Serve web/ locally on
// 18433"), so they are fixed here rather than chosen.
const STATIC_PORTS = [8021, 18433, 18439];

const TYPES = { ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
  ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json" };

const filters = process.argv.slice(2);

async function suiteFiles() {
  const names = (await readdir(HERE))
    .filter((name) => name.startsWith("browser_") && name.endsWith(".js"))
    .sort();
  if (!filters.length) return names;
  return names.filter((name) => filters.some((f) => name.includes(f)));
}

/** A read-only static server over `web/`, for the suites that ask for one by port. */
function serveWeb(port) {
  const server = createServer(async (request, response) => {
    const path = normalize(decodeURIComponent(new URL(request.url, "http://x").pathname));
    // Nothing outside web/ is served, even to a test.
    const file = join(WEB, path.replace(/^([\\/]|\.\.)+/, ""));
    if (!file.startsWith(WEB)) { response.writeHead(403).end(); return; }
    try {
      const info = await stat(file);
      const target = info.isDirectory() ? join(file, "index.html") : file;
      const extension = target.slice(target.lastIndexOf("."));
      response.writeHead(200, { "content-type": TYPES[extension] || "application/octet-stream" });
      createReadStream(target).pipe(response);
    } catch {
      response.writeHead(404).end();
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

/** Compile a bare function literal. `import()` cannot: the file exports nothing. */
async function loadSuite(name) {
  const source = await readFile(join(HERE, name), "utf8");
  return new Function(`"use strict"; return (${source});`)();
}

async function signIn(context) {
  if (!USER) return;
  const response = await context.request.post(`${BASE}/api/auth/login`, {
    data: { username: USER, password: PASSWORD },
  });
  if (!response.ok()) {
    throw new Error(`Sign-in failed (${response.status()}). Set CLOUDLENS_USER / CLOUDLENS_PASSWORD `
      + `to a local account; create one with 'python -m app.auth add <name>'.`);
  }
}

/** A suite's own verdict, whether it threw or returned one. */
function verdict(result) {
  if (result && typeof result === "object") {
    const failures = Array.isArray(result.failures) ? result.failures : [];
    const errors = Array.isArray(result.errors) ? result.errors : [];
    if (result.failure) {
      return { ok: false, reasons: [`${result.phase ? `[${result.phase}] ` : ""}${result.failure}`, ...failures] };
    }
    if (failures.length || errors.length) return { ok: false, reasons: [...failures, ...errors] };
  }
  return { ok: true, reasons: [] };
}

async function main() {
  const names = await suiteFiles();
  if (!names.length) {
    console.error(filters.length ? `No suite matches ${filters.join(", ")}` : "No browser_*.js suites found.");
    process.exit(2);
  }

  // A suite that leaves a promise rejecting after it has returned would otherwise take the whole
  // run down with it -- one aborted the first full run at suite 16 of 27, and the remaining
  // eleven never reported. Attribute it to whichever suite is in flight and keep going.
  let running = null;
  const stray = [];
  process.on("unhandledRejection", (reason) => {
    stray.push(`${running || "after the run"}: unhandled rejection: ${
      String(reason && reason.message || reason).split("\n")[0]}`);
  });

  const servers = [];
  for (const port of STATIC_PORTS) {
    try { servers.push(await serveWeb(port)); }
    catch (error) { console.log(`  note  port ${port} unavailable (${error.code}); suites needing it will fail`); }
  }

  const browser = await chromium.launch({ headless: !process.env.HEADED });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const results = [];
  try {
    await signIn(context);
    const page = await context.newPage();
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    if (new URL(page.url()).pathname.startsWith("/login")) {
      throw new Error(`${BASE} redirected to the login page. Set CLOUDLENS_USER / CLOUDLENS_PASSWORD.`);
    }
    // The dashboard shell has to exist before a suite can borrow the page.
    await waitForDashboard(page);

    for (const name of names) {
      running = name;
      const started = Date.now();
      let outcome;
      try {
        const suite = await loadSuite(name);
        const result = await Promise.race([
          suite(page, { createCostDataFixture }),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error(`suite exceeded ${SUITE_TIMEOUT_MS} ms`)), SUITE_TIMEOUT_MS)),
        ]);
        outcome = verdict(result);
      } catch (error) {
        outcome = { ok: false, reasons: [String(error && error.message || error).split("\n")[0]] };
      }
      const seconds = ((Date.now() - started) / 1000).toFixed(1);
      results.push({ name, ...outcome, seconds });
      console.log(`${outcome.ok ? "  ok  " : " FAIL "} ${name.padEnd(32)} ${seconds.padStart(6)}s`);
      for (const reason of outcome.reasons.slice(0, 10)) console.log(`         - ${reason}`);
    }
    running = null;
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    for (const server of servers) server.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} suites passed`);
  if (stray.length) {
    console.log("\nstray rejections (a suite left work running after it returned):");
    for (const line of stray.slice(0, 10)) console.log(`  - ${line}`);
  }
  if (failed.length) {
    console.log(`failed: ${failed.map((r) => r.name).join(", ")}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(2);
});
