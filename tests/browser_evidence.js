async (page) => {
  const context = await page.context().browser().newContext();
  const test = await context.newPage();
  const base = "http://127.0.0.1:8100/assets/";
  const sources = {};
  for (const name of ["dashboard-evidence.js", "dashboard-explore.js", "dashboard-state.js"]) {
    const response = await context.request.get(base + name);
    if (!response.ok()) throw new Error(`Local module unavailable: ${name}`);
    sources[name] = await response.text();
  }
  const scope = "11111111-1111-1111-1111-111111111111";
  const rid = `/subscriptions/${scope}/resourceGroups/rg/providers/Microsoft.Web/sites/app`;
  const common = { status: "ok", source: "Azure fixture", generated_at: "2026-09-07T10:00:00Z",
    window: { start: "2026-08-31T10:00:00Z", end: "2026-09-07T10:00:00Z", days: 7 },
    notices: ["Missing observations are not zero."], truncated: false };
  const changes = { ...common, changes: [{
    id: "change-1", resource_id: rid, resource_name: "<img src=x onerror=window.injected=1>",
    resource_type: "Microsoft.Web/sites", subscription_id: scope,
    changed_at: "2026-09-07T09:00:00Z", change_type: "Update", actor: null,
    operation: "Microsoft.Web/sites/write", properties: [
      { name: "sku.capacity", before: 1, after: 2 },
      { name: "properties.password", before: "[redacted]", after: "[redacted]" },
    ], properties_truncated: false, cost: { by_currency: [
      { currency: "USD", cost: 4 }, { currency: "EUR", cost: 2 }], from: "2026-09-01", to: "2026-09-05" },
  }] };
  const performance = { ...common, status: "partial", truncated: true, inventory_count: null,
    sampled_count: 1, resources: [{ id: rid, name: "app", type: "microsoft.web/sites",
      subscription_id: scope, region: "eastus", status: "partial", cost: null, notes: [],
      metrics: [
        { name: "Requests", label: "Requests", value: 0, unit: "Count", status: "ok",
          samples: 24, aggregation: "Total", last_sample: "2026-09-07T09:00:00Z", note: "Actual zero." },
        { name: "Latency", label: "Latency", value: null, unit: "Seconds", status: "no_data",
          samples: 0, aggregation: "Average", last_sample: null, note: "No observations." },
      ],
    }] };
  changes.changes.push({ ...changes.changes[0], id: "change-2",
    resource_id: rid.replace("/rg/", "/other/") + "-second", resource_name: "second",
    actor: "bob@example.invalid", change_type: "Create", properties: [], cost: null });
  performance.resources.push({ ...performance.resources[0], id: rid + "-second", name: "second",
    region: "westus", status: "unavailable",
    cost: { by_currency: [{ currency: "USD", cost: 0 }], from: "2026-09-01", to: "2026-09-05" },
    metrics: [{ name: "CPU", label: "CPU", status: "no_data", value: null, samples: 0 }] });
  const calls = [], errors = [];
  let deny = false;
  test.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const request = route.request(), url = request.url();
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    const parameter = name => decodeURIComponent(url.match(new RegExp(`[?&]${name}=([^&]*)`))?.[1] || "");
    const module = path.slice(1);
    if (sources[module]) return route.fulfill({ contentType: "text/javascript", body: sources[module] });
    if (path.startsWith("/api/")) {
      calls.push({ method: request.method(), path, scope: parameter("scope"), days: parameter("window_days") });
      return route.fulfill({ status: deny ? 403 : 200, contentType: "application/json",
        body: JSON.stringify(deny ? { detail: "Denied" } : path.endsWith("change-analysis") ? changes : performance) });
    }
    return route.fulfill({ contentType: "text/html", body: '<main id="tabBody"></main>' });
  });
  let passed = 0;
  const assert = (condition, message) => { if (!condition) throw new Error(message); passed++; };
  try {
    await test.goto("http://evidence-fixture.invalid");
    await test.evaluate(async ({ scope }) => {
      const module = await import("/dashboard-evidence.js");
      const { portalLinkMarkup } = await import("/dashboard-explore.js");
      const { replaceContent } = await import("/dashboard-state.js");
      const esc = value => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
      window.token = 1; window.scope = [scope]; window.module = module;
      window.tabs = module.createTabs({
        $: id => document.getElementById(id), esc, portalLinkMarkup,
        portalScopeIds: () => window.scope,
        get loadToken() { return window.token; },
        get: async (url, options) => {
          const response = await fetch(url, { signal: options.signal });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        },
        setContent: replaceContent,
        wireTables: element => {
          if (element !== document.getElementById("tabBody")) throw new Error("Table wiring needs its root");
          element.querySelectorAll("[data-table]");
        },
        kpiRow: items => items.map(item => `<p>${esc(item.label)}: ${esc(item.value)}</p>`).join(""),
        table: (title, rows, columns) => `<section><h3>${esc(title)}</h3><table>${rows.map(row =>
          `<tr>${columns.map(column => `<td>${column.cell(row)}</td>`).join("")}</tr>`).join("")}</table></section>`,
      });
      await window.tabs.renderChanges(window.token);
    }, { scope });
    assert(calls.length === 1 && calls[0].scope === scope && calls[0].days === "7", "Explicit scoped default query");
    assert((await test.textContent("#tabBody")).includes("not the global cost-period"), "Independent live window disclosed");
    assert((await test.textContent("#tabBody")).includes("USD 4"), "USD shown");
    assert((await test.textContent("#tabBody")).includes("EUR 2"), "Currencies not combined");
    assert(await test.locator("img").count() === 0, "Resource names escaped");
    assert(await test.evaluate(() => !window.injected), "No script execution");
    assert((await test.locator("a").first().getAttribute("href")).startsWith("https://portal.azure.com/"), "Safe resource link");
    assert((await test.textContent("#tabBody")).includes("Not supplied by Azure"), "Unknown actor disclosed");
    await test.getByRole("searchbox", { name: "Search evidence", exact: true }).fill("second");
    assert((await test.textContent("#tabBody")).includes("Showing 1 of 2 loaded changes"), "Search narrows loaded changes");
    assert((await test.textContent("#tabBody")).includes("Observed changes: 1"), "Change KPI follows search");
    await test.getByRole("button", { name: "Clear filters", exact: true }).click();
    assert(await test.getByRole("searchbox", { name: "Search evidence", exact: true }).inputValue() === "", "Clear overrides preserved search state");
    await test.getByRole("combobox", { name: "Change type", exact: true }).selectOption("value:create");
    await test.getByRole("combobox", { name: "Changed by", exact: true }).selectOption("__not_supplied__");
    assert((await test.textContent("#tabBody")).includes("No loaded changes match"), "Combined filters explain empty result");
    assert((await test.textContent("#tabBody")).includes("Observed changes: 0"), "Empty filter result has zero observed rows");
    await test.getByRole("button", { name: "Clear filters", exact: true }).click();
    assert(await test.getByRole("combobox", { name: "Change type", exact: true }).inputValue() === "", "Clear overrides preserved select state");
    await test.getByRole("combobox", { name: "Resource group", exact: true }).selectOption("value:other");
    assert((await test.textContent("#tabBody")).includes("Showing 1 of 2 loaded changes"), "Resource group filters by ARM ID");
    await test.getByRole("combobox", { name: "Billing currency", exact: true }).selectOption("value:usd");
    assert((await test.textContent("#tabBody")).includes("Showing 0 of 2 loaded changes"), "Currency filter excludes missing costs");
    await test.getByRole("button", { name: "Clear filters", exact: true }).click();
    assert(calls.length === 1, "Loaded-sample filters make no extra API calls");
    assert((await test.textContent("#tabBody")).includes("do not search beyond source or sample limits"), "Sample boundary disclosed");
    await test.getByRole("combobox", { name: "Evidence window" }).selectOption("1");
    await test.waitForFunction(() => !document.querySelector("#tabBody #evidence-changes-refresh").disabled);
    assert(calls.at(-1).days === "1", "24-hour evidence window reaches backend");
    await test.getByRole("combobox", { name: "Evidence window" }).selectOption("14");
    await test.waitForFunction(() => !document.querySelector("#tabBody #evidence-changes-refresh").disabled);
    assert(calls.at(-1).days === "14", "Window control changes request");
    await test.evaluate(async () => { window.token++; await window.tabs.renderPerformance(window.token); });
    assert((await test.textContent("#tabBody")).includes("Partial coverage"), "Sample limit disclosed");
    assert((await test.textContent("#tabBody")).includes("No billing rows"), "Missing cost is not zero");
    assert((await test.textContent("#tabBody")).includes("0 Count"), "Measured zero retained");
    assert((await test.textContent("#tabBody")).includes("No observations"), "Missing metric distinct from zero");
    await test.getByRole("combobox", { name: "Metric coverage", exact: true }).selectOption("measured");
    assert((await test.textContent("#tabBody")).includes("Showing 1 of 2 loaded resources"), "Measured zero counts as an observation");
    assert((await test.textContent("#tabBody")).includes("Resources shown: 1"), "Performance KPI follows filters");
    await test.getByRole("combobox", { name: "Region", exact: true }).selectOption("value:westus");
    assert((await test.textContent("#tabBody")).includes("No sampled resources match"), "Region combines with coverage");
    await test.getByRole("button", { name: "Clear filters", exact: true }).click();
    await test.getByRole("combobox", { name: "Cost evidence", exact: true }).selectOption("zero");
    assert((await test.textContent("#tabBody")).includes("Showing 1 of 2 loaded resources"), "Recorded zero excludes missing cost");
    await test.getByRole("button", { name: "Clear filters", exact: true }).click();
    deny = true;
    await test.getByRole("button", { name: "Refresh evidence" }).click();
    await test.waitForFunction(() => !document.querySelector("#tabBody #evidence-performance-refresh").disabled);
    assert((await test.getByRole("alert").textContent()).includes("HTTP 403"), "Source denial displayed");
    assert((await test.getByRole("alert").textContent()).includes("stale"), "Retained evidence marked stale");
    const before = calls.length;
    await test.evaluate(async () => { window.scope = []; await window.tabs.renderChanges(window.token); });
    assert(calls.length === before, "Empty scope does not call backend");
    assert((await test.textContent("#tabBody")).includes("Select at least one"), "Empty scope explains action");
    assert(calls.every(call => call.method === "GET"), "No API mutations");
    assert(errors.length === 0, `No browser errors: ${errors.join(", ")}`);
    return { passed, apiCalls: calls.length };
  } finally {
    await context.close();
  }
}
