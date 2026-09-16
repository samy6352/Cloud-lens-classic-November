async (page) => {
  const origin = page.url().match(/^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?(?=\/|$)/)?.[0];
  if (!origin) {
    throw new Error("Open the local CloudLens preview before running sidebar checks.");
  }
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1440, height: 900 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  test.setDefaultTimeout(8000);
  const errors = [], layouts = [];
  let passed = 0;
  const assert = (ok, name) => { if (!ok) throw new Error(name); passed++; };
  test.on("pageerror", error => errors.push(error.message));
  try {
    await test.goto(origin);
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    if (await test.locator("#railMini").getAttribute("aria-pressed") === "true") {
      await test.locator("#railMini").click();
    }
    await test.waitForFunction(() => document.querySelector(".sidebar").getBoundingClientRect().width > 190);
    const labels = ["Overview", "Billing", "Savings", "Monitor", "Governance", "Reports"];
    assert(JSON.stringify(await test.locator("[data-main-section] .rail-name").allTextContents())
      === JSON.stringify(labels), "All primary labels are retained");
    assert(await test.locator(".rail-icon-frame .rail-icon").count() === labels.length, "All icons are framed");
    assert(await test.locator(".rail-chevron").count() === labels.length, "Each destination has a chevron");
    const density = await test.evaluate(() => ({
      width: document.querySelector(".sidebar").getBoundingClientRect().width,
      rows: [...document.querySelectorAll(".rail-item")].map(el => el.getBoundingClientRect().height),
      labelsFit: [...document.querySelectorAll(".rail-item .rail-name")].every(el => el.scrollWidth <= el.clientWidth),
    }));
    assert(density.width <= 200.5 && density.rows.every(height => height >= 39.5 && height <= 40.5)
      && density.labelsFit, `Compact sidebar retains readable labels: ${JSON.stringify(density)}`);
    await test.locator("#primary-overview").focus();
    await test.keyboard.press("ArrowDown");
    assert(await test.locator("#primary-billing").getAttribute("aria-selected") === "true",
      "Arrow keys still switch main sections");
    await test.keyboard.press("End");
    assert(await test.locator("#primary-reports").getAttribute("aria-selected") === "true",
      "End reaches Reports");
    await test.keyboard.press("Home");
    assert(await test.locator("#primary-overview").getAttribute("aria-selected") === "true",
      "Home returns to Overview");
    await test.locator("#railMini").focus();
    await test.keyboard.press("Enter");
    assert(await test.locator("#railMini").evaluate(el => el === document.activeElement
      && el.getAttribute("aria-pressed") === "true"), "Collapse preserves keyboard focus");
    await test.waitForFunction(() => getComputedStyle(document.getElementById("shell")).gridTemplateColumns
      .split(" ").length > 1 && document.querySelector(".sidebar").getBoundingClientRect().width <= 60.5);
    const collapsedWidth = await test.locator(".sidebar").evaluate(el => el.getBoundingClientRect().width);
    assert(collapsedWidth <= 60.5, `Collapsed rail uses no more than 60px: ${collapsedWidth}`);
    assert(await test.locator("#primary-billing").evaluate(el => (el.dataset.tooltip || el.title) === "Billing"),
      "Collapsed icons retain discoverable labels");
    await test.reload();
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    await test.locator("#accountTrigger").waitFor({ state: "visible" });
    assert(await test.locator("#railMini").getAttribute("aria-pressed") === "true",
      "Collapsed preference survives reload");
    await test.locator("#railMini").click();
    const sizes = [
      { width: 1440, height: 900 }, { width: 1200, height: 700 },
      { width: 1440, height: 480 }, { width: 1100, height: 800 },
      { width: 390, height: 844 }, { width: 320, height: 568 },
    ];
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const size of sizes) {
        await test.setViewportSize(size);
        for (const mini of [false, true]) {
          await test.evaluate(async mini => {
            document.documentElement.classList.toggle("rail-mini", mini);
            document.body.classList.toggle("rail-mini", mini);
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          }, mini);
          const result = await test.evaluate(() => {
            const rail = document.getElementById("tabs").getBoundingClientRect();
            const account = document.getElementById("who").getBoundingClientRect();
            const style = getComputedStyle(document.documentElement);
            const luminance = token => {
              const hex = style.getPropertyValue(token).trim().replace("#", "");
              const rgb = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
                .map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
              return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
            };
            const contrast = (a, b) => {
              a = luminance(a); b = luminance(b);
              return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
            };
            return {
              width: innerWidth, height: innerHeight, theme: document.documentElement.dataset.theme,
              mini: document.documentElement.classList.contains("rail-mini"),
              overflow: document.documentElement.scrollWidth > innerWidth,
              overlap: rail.bottom > account.top + 1,
              accountBottom: account.bottom, pageScroll: scrollY,
              accountDocked: Math.abs(account.bottom - innerHeight) < 2,
              contrast: [
                contrast("--rail-label", "--rail-bg"), contrast("--rail-text", "--rail-selected"),
                contrast("--rail-muted", "--rail-depth"), contrast("--rail-accent", "--rail-selected"),
              ],
            };
          });
          layouts.push(result);
          assert(!result.overflow && !result.overlap && result.accountDocked,
            `No overlap or overflow: ${JSON.stringify(result)}`);
          assert(result.contrast.every(value => Number.isFinite(value) && value >= 4.5),
            `Sidebar contrast: ${JSON.stringify(result)}`);
          await test.locator("#primary-reports").scrollIntoViewIfNeeded();
          assert(await test.locator("#primary-reports").isVisible(), "Last tab remains reachable");
        }
      }
    }
    await test.setViewportSize({ width: 1440, height: 900 });
    await test.evaluate(() => {
      document.documentElement.classList.remove("rail-mini");
      document.body.classList.remove("rail-mini");
      document.getElementById("tabs").scrollTop = 0;
    });
    await test.locator("#accountTrigger").click();
    assert(await test.locator("#who").evaluate(el => el.getBoundingClientRect().height <= 54),
      "Account footer stays compact when its popover opens");
    assert(await test.locator("#signout").isVisible(), "Actual app wires the account disclosure");
    assert(await test.locator(".account-illustration").evaluate(el => el.complete && el.naturalWidth > 0),
      "Supplied illustration renders without a broken image");
    await test.locator("#signout").focus();
    await test.keyboard.press("Escape");
    assert(await test.locator("#accountTrigger").evaluate(el => el === document.activeElement),
      "Actual app restores account focus on Escape");
    assert(errors.length === 0, `No runtime errors: ${errors.join("; ")}`);
    return { passed, layouts: layouts.length, errors };
  } finally {
    await context.close();
  }
}
