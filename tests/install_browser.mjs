// Standalone offline smoke test: no app lifespan, Azure, credentials or deployment.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { chromium } from "playwright";

// Match Python's read_text universal-newline handling in the actual page route.
const html = (await readFile(new URL("../web/install.html", import.meta.url), "utf8")).replace(/\r\n/g, "\n");
const css = await readFile(new URL("../web/assets/install.css", import.meta.url));
const appCss = await readFile(new URL("../web/assets/app.css", import.meta.url));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const hash = createHash("sha256").update(script).digest("base64");
const server = createServer((req, res) => {
  res.setHeader("Content-Security-Policy", `default-src 'self'; script-src 'self' 'sha256-${hash}'; style-src 'self'; object-src 'none'; base-uri 'none'`);
  if (req.url.startsWith("/assets/app.css")) {
    res.setHeader("Content-Type", "text/css");
    res.end(appCss);
  } else if (req.url.startsWith("/assets/install.css")) {
    res.setHeader("Content-Type", "text/css");
    res.end(css);
  } else if (req.url.startsWith("/install")) {
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(html);
  } else {
    res.writeHead(204).end();
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  for (const [query, media, expected] of [
    ["?scoutTheme=light", "dark", "light"],
    ["?scoutTheme=dark", "light", "dark"],
    ["?scoutTheme=vivid", "light", "vivid"],
    ["?scoutTheme=paper", "dark", "paper"],
    ["", "dark", "dark"],
    ["?scoutTheme=invalid", "light", "light"],
  ]) {
    await page.emulateMedia({ colorScheme: media });
    for (const width of [375, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`http://127.0.0.1:${server.address().port}/install${query}`);
      assert.equal(await page.locator("html").getAttribute("data-theme"), expected, errors.join("\n"));
      assert(await page.locator("html").evaluate(el => {
        const style = getComputedStyle(el);
        return style.getPropertyValue("--cp-bg").trim() === style.getPropertyValue("--bg").trim()
          && style.getPropertyValue("--cp-accent").trim() === style.getPropertyValue("--accent").trim();
      }));
      assert(await page.locator("#deploy-azure").isVisible());
      assert.equal(await page.locator("#deploy-azure").getAttribute("target"), null);
      assert.equal(await page.locator("pre code").innerText().then(text => text.split("\n").length), 3);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.locator("#deploy-azure").focus();
      assert.equal(await page.locator("#deploy-azure").evaluate(el => getComputedStyle(el).outlineStyle), "solid");
    }
  }
  await page.selectOption("#install-theme", "vivid");
  await page.reload();
  assert.equal(await page.locator("html").getAttribute("data-theme"), "vivid");
  const azureUrl = await page.locator("#deploy-azure").getAttribute("href");
  await page.route("https://portal.azure.com/**", route => route.fulfill({
    contentType: "text/html", body: "<h1>Azure deployment hand-off</h1>",
  }));
  await page.locator("#deploy-azure").click();
  await page.waitForURL(azureUrl);
  assert.equal(page.context().pages().length, 1, "Deployment must not depend on pop-ups");
  assert.equal(await page.locator("h1").innerText(), "Azure deployment hand-off");
  assert.deepEqual(errors, []);
  console.log("PASS install browser: 12 theme/viewport combinations, saved theme, CSP, commands and same-tab Azure hand-off");
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
