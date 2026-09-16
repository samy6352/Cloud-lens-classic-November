async (page, { assetOrigin } = {}) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before terminology checks.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1600, height: 1000 }, reducedMotion: "reduce",
  });
  const test = await context.newPage(), errors = [];
  test.on("pageerror", error => errors.push(error.message));
  let passed = 0;
  const check = (ok, message) => { if (!ok) throw new Error(message); passed++; };
  const vm = { id: "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Compute/virtualMachines/vm",
    name: "VM", service: "Virtual Machines", key: "vm", meter: "Compute", cost: 168, hours: 168,
    uptime_pct: 100, units: 1, always_on: true, schedulable: true };
  const plan = { ...vm, id: "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Web/serverfarms/plan",
    name: "Plan", service: "Azure App Service", key: "plan", cost: 1000 };
  let budgetStatus = "unavailable", aksStatus = "unavailable";
  const ready = () => test.waitForFunction(
    () => document.documentElement.dataset.dashboardReady === "true", null, { timeout: 120000 });
  try {
    if (assetOrigin) {
      for (const name of ["app.js", "dashboard.js", "dashboard-spend.js", "dashboard-optimize.js",
        "dashboard-billing.js", "dashboard-reports.js", "billing-usage.js", "overview-status.js"]) {
        const response = await page.request.get(`${assetOrigin}/assets/${name}`);
        if (!response.ok()) throw new Error(`Missing staged asset: ${name}`);
        const body = await response.body();
        await context.route(`**/assets/${name}`, route => route.fulfill({ body, contentType: "text/javascript" }));
      }
    }
    await context.route("**/api/overview?**", route => route.fulfill({ json: {
      currency: "USD", month_to_date: 10, last_month: 20, forecast_30d: 30,
      budget: null, budget_status: budgetStatus, budget_note: "Fixture budget coverage",
    } }));
    await context.route("**/api/dashboard/sections?**", route => route.fulfill({ json: {
      total: 1168, currency: "USD", aks: { cost: 10, currency: "USD" },
      sections: [{ id: "compute", label: "Compute & Web", cost: 1168, currency: "USD" }],
    } }));
    await context.route("**/api/dashboard/uptime?**", route => route.fulfill({ json: {
      days: 7, currency: "USD", resources: [vm, plan], dates: ["2026-09-01", "2026-09-02"],
      daily: { vm: [[0, 24, 24], [1, 24, 24]], plan: [[0, 24, 100], [1, 24, 100]] },
      total_spend: 1168, schedules: [{ id: "business", label: "Business", hours: 60 }],
    } }));
    await context.route("**/api/dashboard/aks?**", route => route.fulfill({ json: {
      days: 7, currency: "USD", total: 10, discovery_status: aksStatus,
      detected_clusters: aksStatus === "unavailable" ? null : 1,
      discovery_note: "Fixture inventory state", unmatched_groups: [{ cluster: "No match" }],
      clusters: [{ cluster: "No match", cost: 10, live: false, inventory_status: aksStatus === "ok" ? "not_found" : "unknown" },
        ...(aksStatus === "ok" ? [{ cluster: "Stopped", live: true, cost: 0, power_state: "Stopped", inventory_status: "detected" }] : [])],
    } }));
    await test.goto(origin);
    await ready();
    const budgetTile = test.locator("#glance > *").nth(4);
    await budgetTile.getByText("Unavailable", { exact: true }).waitFor();
    check(!(await budgetTile.textContent()).includes("none set"), "Failed budget read must not claim none set.");
    await test.locator('#tabs [data-main-section="billing"]').click();
    await test.locator('#viewActions [data-subsection="uptime"]').click();
    await ready();
    check(await test.locator('#tabBody > .portal-workbench .kpi').count() === 6,
      "Hourly Billing keeps its six metrics in the shared summary panel.");
    check((await test.locator('#tabBody > .portal-workbench').innerText()).includes("not service availability"),
      "Billing methodology remains visible alongside the grouped metrics.");
    check(await test.locator('#viewActions [data-subsection="uptime"]').textContent() === "Hourly Billing", "Tab label changed unexpectedly.");
    const heading = test.getByRole("columnheader", { name: "Billed uptime", exact: true });
    await heading.scrollIntoViewIfNeeded();
    await heading.focus();
    await test.locator("#cl-tooltip").waitFor({ state: "visible" });
    check((await test.locator("#cl-tooltip").textContent()).includes("not service availability"), "Billed uptime tooltip must explain its basis.");
    await test.keyboard.press("Escape");
    const body = await test.locator("#tabBody").innerText();
    check(!/Never switched off|running 24\/7|cannot be stopped|already off enough/.test(body), "Operational claims remain in Hourly Billing.");
    check(body.includes("High billed coverage") && body.includes("Hourly average"), "Billing-based headline labels are missing.");
    const planRow = test.locator("#tabBody tr[data-explore-row]").filter({ hasText: "Azure App Service" });
    check((await planRow.innerText()).includes("Not assessed"), "App Service must not show a schedule saving.");
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const width of [1600, 1200, 390]) {
        await test.setViewportSize({ width, height: 1000 });
        check(await test.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
          `Hourly Billing overflows at ${width}/${theme}`);
      }
    }
    await test.setViewportSize({ width: 1600, height: 1000 });
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "aks" })));
    await ready();
    check((await test.locator("#tabBody").innerText()).includes("Unknown"), "Failed inventory must show unknown.");
    check(!/running now|still charging|\bdeleted\b|\bOrphaned\b/.test(await test.locator("#tabBody").innerText()),
      "AKS inferred deletion or ongoing charges.");
    aksStatus = "ok";
    budgetStatus = "none";
    await test.reload();
    await ready();
    await budgetTile.getByText("none set", { exact: true }).waitFor();
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "aks" })));
    await ready();
    const aks = await test.locator("#tabBody").innerText();
    check(/Not found/i.test(aks) && aks.includes("Stopped") && aks.includes("1 detected"),
      `AKS detection and power state must remain separate: ${aks}`);
    check(errors.length === 0, errors.join("\n"));
    return { passed, errors };
  } finally {
    await context.close();
  }
}
