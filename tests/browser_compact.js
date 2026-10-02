async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before the compact-layout review.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1440, height: 900 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  const errors = [], results = [];
  test.on("pageerror", error => errors.push(error.message));
  const ready = async () => {
    await test.waitForFunction(
      () => ["true", "error"].includes(document.documentElement.dataset.dashboardReady),
      null, { timeout: 120000 });
    const error = await test.evaluate(() => document.documentElement.dataset.dashboardReady === "error"
      ? document.querySelector("#tabBody .banner.err")?.textContent || "View failed to render" : "");
    if (error) throw new Error(error);
  };
  const expand = async () => {
    for (const button of await test.locator("#tabs .rail-group > button[aria-expanded=false]").all()) {
      await button.click();
    }
  };
  const openTab = async id => {
    if (await test.locator("#tabs [data-main-section]").count()) {
      const parent = await test.evaluate(async id => {
        const { PRIMARY_SECTIONS } = await import("/assets/dashboard-navigation.js");
        return PRIMARY_SECTIONS.find(section => section.members.includes(id))?.id;
      }, id);
      await test.locator(`#tabs [data-main-section="${parent}"]`).click();
      if (parent !== "overview" && await test.locator(`#viewActions [data-subsection="${id}"]`).count()) {
        await test.locator(`#viewActions [data-subsection="${id}"]`).click();
      }
      else if (parent === "overview") {
        const all = test.locator('#viewActions > .ar-picker [data-area="overview"]');
        if (await all.count()) await all.click();
      }
    } else {
      await expand();
      await test.locator(`#tabs [data-tab="${id}"]`).click();
    }
    await ready();
    if (id === "changes" || id === "performance") {
      await test.waitForFunction(id => {
        const button = document.querySelector(`#tabBody #evidence-${id}-refresh`);
        return button && !button.disabled;
      }, id, { timeout: 120000 });
    }
  };
  const measure = async () => test.evaluate(() => {
    const body = document.getElementById("tabBody");
    body.scrollTop = 0;
    const first = body.querySelector(":scope > .card");
    return { firstCard: first ? Math.round(first.getBoundingClientRect().height) : null,
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      boardHeight: body.clientHeight,
      chromeHeight: Math.round(document.querySelector(".bar").getBoundingClientRect().height
        + document.querySelector(".board-summary").getBoundingClientRect().height),
      summaryHeight: Math.round(document.querySelector("#glance").getBoundingClientRect().height),
      clippedSummaryValues: [...document.querySelectorAll("#glance .v")].filter(el =>
        el.scrollWidth > el.clientWidth + 2).length,
      clippedSummaryLabels: [...document.querySelectorAll("#glance .k")].filter(el =>
        el.scrollWidth > el.clientWidth + 2).length,
      headings: [...body.querySelectorAll("h3")].slice(0, 6).map(node => node.textContent.trim()),
      help: body.querySelectorAll("details.view-help").length,
      error: /Cannot read properties|is not a function|is not defined|SyntaxError/.test(body.textContent) };
  });
  try {
    await test.goto(origin);
    await ready();
    await openTab("changes");
    const before = await measure();
    await test.reload();
    await ready();
    await openTab("changes");
    const after = await measure();
    if (after.firstCard > 320) {
      throw new Error(`Evidence header exceeds the compact layout budget: ${JSON.stringify({ before, after })}`);
    }
    if (after.chromeHeight > before.chromeHeight + 1) {
      throw new Error(`Reload enlarged the shared chrome: ${JSON.stringify({before,after})}`);
    }
    await expand();
    const tabs = await test.locator("#tabs [data-main-section]").count()
      ? await test.evaluate(async () => {
        const { PRIMARY_SECTIONS } = await import("/assets/dashboard-navigation.js");
        return PRIMARY_SECTIONS.flatMap(section => section.members);
      })
      : await test.locator("#tabs [data-tab]").evaluateAll(nodes => nodes.map(node => node.dataset.tab));
    for (const tab of tabs) {
      await openTab(tab);
      const result = await measure();
      if (result.overflow || result.error || result.boardHeight < 200 || result.clippedSummaryValues || result.clippedSummaryLabels) {
        throw new Error(JSON.stringify({ tab, ...result }));
      }
      results.push({ tab, ...result });
    }
    await openTab("overview");
    const areas = await test.locator("#viewActions > .ar-picker [data-area]").evaluateAll(
      nodes => nodes.map(node => node.dataset.area).filter(id => id !== "overview"));
    for (const area of areas) {
      await test.locator(`#viewActions > .ar-picker [data-area="${area}"]`).click();
      await ready();
      const result = await measure();
      if (result.overflow || result.error) throw new Error(JSON.stringify({ area, ...result }));
      results.push({ area, ...result });
    }
    await test.setViewportSize({ width: 390, height: 844 });
    for (const tab of ["overview", "billing", "changes", "performance", "report", "settings"]) {
      await openTab(tab);
      const result = await measure();
      if (result.overflow || result.error || result.boardHeight < 180 || result.clippedSummaryValues || result.clippedSummaryLabels) {
        throw new Error(JSON.stringify({ mobile: tab, ...result }));
      }
      results.push({ mobile: tab, ...result });
    }
    await test.setViewportSize({ width: 1440, height: 900 });
    await openTab("changes");
    const help = test.locator('#tabBody details.view-help').first();
    await help.locator("summary").focus();
    await test.keyboard.press("Enter");
    if (!(await help.evaluate(node => node.open))) throw new Error("Keyboard cannot open help.");
    await test.keyboard.press("Enter");
    if (await help.evaluate(node => node.open)) throw new Error("Keyboard cannot close help.");
    if (errors.length) throw new Error(JSON.stringify(errors));
    return { before, after, reloadStable: after.chromeHeight <= before.chromeHeight + 1,
      views: results, errors };
  } finally {
    await context.close();
  }
}
