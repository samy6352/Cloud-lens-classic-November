async (page, { stylesheetUrl, assetOrigin } = {}) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open a signed-in CloudLens page before these checks.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1500, height: 1050 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  const errors = [];
  let releaseOpportunities;
  const opportunitiesGate = new Promise(resolve => { releaseOpportunities = resolve; });
  test.on("pageerror", error => errors.push(error.message));
  const statement = {
    period: { month: "2026-08", label: "August 2026 — UI test fixture", previous_month: "2026-07", days: 31 },
    currency: "USD", spend_basis: { code: "invoice", metric: "billed_cost", label: "Total billed" },
    kpis: { billed_cost: 100, recorded_cost: 110, list_cost: 150, contracted_cost: 135,
      effective_cost: 110, azure_discounts: 15, reservation_savings: 10, savings_plan_savings: 15,
      other_commitment_savings: 0, non_commitment_savings: 0, actual_savings: 40,
      effective_discount_pct: 26.67 },
    pricing: { status: "complete", covered_rows: 31, total_rows: 31 },
    coverage: { current: { rows: 31, days_present: 31, calendar_days: 31 }, previous: { rows: 31, days_present: 31, calendar_days: 31 } },
    month_over_month: { current: 100, previous: 80, change_pct: 25, comparable: true },
    trend: { labels: ["2026-07", "2026-08"], values: [80, 100], bases: ["billed_cost", "billed_cost"] },
    breakdowns: { subscriptions: [{ name: "A&B", cost: 8, currency: "USD" },
      ...Array.from({ length: 23 }, (_, index) => ({ name: `Other ${index}`, cost: 4, currency: "USD" }))],
      services: [], resources: [] },
    definitions: [], warnings: ["Synthetic UI fixture, not customer billing data."], per_currency: [], fx: { complete: true },
  };
  try {
    // A running preview freezes its versioned bundle. Exercise current disk assets without
    // restarting that server or modifying its immutable URLs for other browser contexts.
    await context.route("**/assets/**", async route => {
      const path = route.request().url().split("/assets/")[1].replace(/^v\/[a-f0-9]{64}\//, "");
      const response = await page.request.get(`${assetOrigin || origin}/assets/${path}`);
      if (!response.ok()) throw new Error(`Missing local billing asset: ${path}`);
      await route.fulfill({ response });
    });
    if (stylesheetUrl) {
      const response = await page.request.get(stylesheetUrl);
      if (!response.ok()) throw new Error("Requested billing stylesheet is unavailable.");
      const body = await response.body();
      await context.route("**/assets/app.css", route => route.fulfill({ body, contentType: "text/css" }));
    }
    await context.route("**/api/dashboard/billing?**", route => route.fulfill({ json: statement }));
    await context.route("**/api/dashboard/billing", route => route.fulfill({ json: statement }));
    await context.route("**/api/dashboard/sections**", route => route.fulfill({
      json: { total: 100, currency: "USD", sections: [] },
    }));
    await context.route("**/api/dashboard/executive**", route => route.fulfill({
      json: { empty: true },
    }));
    await context.route("**/api/filters**", route => route.fulfill({ json: { dimensions: {} } }));
    await context.route("**/api/dashboard/billing/opportunities**", async route => {
      await opportunitiesGate;
      await route.fulfill({
        json: { status: "ok", opportunities: [], potential: { monthly: 15, annual: 180, currency: "USD" },
          commitments: { items: [], unavailable: [] }, warnings: [] },
      });
    });
    await test.goto(origin);
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "billing" })));
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true"
      && !document.querySelector("#billRefresh")?.disabled
      && document.querySelector("#billPeriod")?.textContent.includes("UI test fixture"));
    for (const id of ["billSavingsChart", "billMomChart"]) {
      await test.locator(`#${id}`).scrollIntoViewIfNeeded();
      await test.waitForFunction(id => {
        const canvas = document.querySelector(`#${id} canvas`);
        return canvas && window.Chart?.getChart(canvas);
      }, id);
    }
    const charts = await test.evaluate(() => ["billSavingsChart", "billMomChart"].map(id => {
      const canvas = document.querySelector(`#${id} canvas`);
      const chart = Chart.getChart(canvas);
      return { id, type: chart.config.type, values: chart.data.datasets[0].data,
        unclipped: canvas.getBoundingClientRect().bottom <= canvas.closest(".card").getBoundingClientRect().bottom + 1 };
    }));
    if (!(await test.locator("#billOpportunities").textContent()).includes("sources load")
      || !await test.locator('[data-bill-breakdown="services"]').isEnabled()) {
      throw new Error("The statement must be interactive while optional savings sources are unresolved.");
    }
    releaseOpportunities();
    const chartSizes = async () => {
      await test.waitForFunction(() => [...document.querySelectorAll(".bill-chart-grid > .card")]
        .every(card => card.querySelector("canvas").getBoundingClientRect().bottom <= card.getBoundingClientRect().bottom + 1),
      null, { timeout: 5000 });
      return test.evaluate(() => [...document.querySelectorAll(".bill-chart-grid > .card")].map(card => {
        const rect = card.getBoundingClientRect();
        const canvas = card.querySelector("canvas").getBoundingClientRect();
        return { width: rect.width, height: rect.height, unclipped: canvas.bottom <= rect.bottom + 1 };
      }));
    };
    const aligned = sizes => Math.abs(sizes[0].height - sizes[1].height) < 1
      && Math.abs(sizes[0].width - sizes[1].width) < 1 && sizes.every(size => size.unclipped);
    const collapsed = await chartSizes();
    const canvas = test.locator("#billMomChart canvas");
    await canvas.focus();
    await test.keyboard.press("Home");
    await test.keyboard.press("Enter");
    await test.locator("#billMomChart .chart-point").waitFor({ state: "visible" });
    const expanded = await chartSizes();
    await test.getByRole("button", { name: "Close chart details" }).click();
    const closed = await chartSizes();
    if (![collapsed, expanded, closed].every(aligned)
      || Math.abs(collapsed[0].height - closed[0].height) > 1) {
      throw new Error(JSON.stringify({ collapsed, expanded, closed }));
    }
    const search = test.locator("#billBreakdown [data-tsearch]");
    await search.fill("A&B");
    await test.waitForFunction(() => document.querySelector("#billBreakdown .tfoot-k")?.textContent === "1 row");
    if (!(await test.locator("#billBreakdown .tfoot-v").textContent()).includes("8.00")) {
      throw new Error("The breakdown footer must total matching rows, not the whole statement.");
    }
    statement.breakdowns.subscriptions = [{ name: "Other only", cost: 5, currency: "USD" }];
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => document.querySelector("#billRefresh")?.disabled === false);
    if (!await search.isVisible() || await search.inputValue() !== "A&B") {
      throw new Error("Refreshing to fewer rows must not hide a retained search.");
    }
    await search.fill("");
    await test.waitForFunction(() => document.querySelector("#billBreakdown .tfoot-k")?.textContent === "1 row");
    if (!(await test.locator("#billBreakdown .tfoot-v").textContent()).includes("5.00")) {
      throw new Error("Clearing search must update the breakdown total.");
    }
    statement.currency = null;
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => document.querySelector("#billRefresh")?.disabled === false);
    if (!(await test.locator("#billSpent .bill-value").textContent()).includes("currency unknown")
      || (await test.locator("#billPotential .bill-value").textContent()).includes("currency unknown")) {
      throw new Error("The statement must not borrow its currency from optimization estimates.");
    }
    statement.currency = "USD";
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => document.querySelector("#billRefresh")?.disabled === false);
    const narrow = [];
    for (const width of [2000, 1500, 1200, 800, 390, 320]) {
      await test.setViewportSize({ width, height: 844 });
      narrow.push(await test.evaluate(() => {
        const cards = [...document.querySelectorAll(".bill-answer, .bill-chip")];
        const rects = cards.map(card => card.getBoundingClientRect());
        const spread = key => Math.max(...rects.map(rect => rect[key])) - Math.min(...rects.map(rect => rect[key]));
        return { width: innerWidth,
          overflow: document.documentElement.scrollWidth > innerWidth + 1,
          answers: document.querySelectorAll(".bill-answer").length, cards: cards.length,
          columns: getComputedStyle(document.querySelector(".bill-answers")).gridTemplateColumns.split(" ").length,
          widthDifference: spread("width"), heightDifference: spread("height"),
          clipped: cards.some(card => card.scrollWidth > card.clientWidth + 1 || card.scrollHeight > card.clientHeight + 1) };
      }));
    }
    if (charts.some(chart => !chart.unclipped) || narrow.some(row => row.overflow || row.answers !== 3
      || row.cards !== 6 || row.widthDifference > 1 || row.heightDifference > 1 || row.clipped
      || row.columns !== (row.width > 1400 ? 6 : row.width > 900 ? 3 : row.width > 360 ? 2 : 1)) || errors.length) {
      throw new Error(JSON.stringify({ charts, narrow, errors }));
    }
    return { charts, collapsed, expanded, closed, narrow, searchTotals: true, currencyIsolation: true,
      statementReadyBeforeOpportunities: true, errors };
  } catch (error) {
    throw new Error(`${error.stack}\nBrowser errors: ${errors.join("; ")}\nReadiness: ${await test.evaluate(() => document.documentElement.dataset.dashboardReady)}\nBody: ${(await test.locator("#tabBody").innerText()).slice(0, 1000)}`);
  } finally {
    releaseOpportunities();
    await context.close();
  }
}
