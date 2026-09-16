async (page) => {
  const context = await page.context().browser().newContext({viewport: {width: 1280, height: 900}});
  const files = {};
  for (const name of ["dashboard-updates.js", "dashboard-state.js", "app.css"]) {
    const response = await context.request.get(`http://127.0.0.1:18439/assets/${name}`);
    if (!response.ok()) throw Error("Serve web/ on localhost:18439 for this offline browser fixture.");
    files[name] = await response.text();
  }
  const test = await context.newPage(), requests = [], errors = [];
  test.on("pageerror", error => errors.push(error.message));
  const updates = Array.from({length: 24}, (_, i) => ({
    id: `u${i}`, title: `Update ${i}`, category: i % 2 ? "GA" : "Preview",
    services: i === 23 ? [] : i % 2 ? ["Azure Storage"] : ["Azure Functions"],
    regions: [], summary: "Official product notice",
  }));
  const status = [
    {id: "s1", title: "Storage incident", services: ["Azure Storage"], regions: []},
    {id: "s2", title: "Functions incident", services: ["Azure Functions"], regions: []},
  ];
  const health = [
    {id: "h1", title: "Central issue", services: [], regions: ["centralindia", "Central India"], status: "Active"},
    {id: "h2", title: "Europe issue", services: [], regions: ["West Europe"], status: "Active"},
    {id: "h3", title: "Unspecified issue", services: [], regions: [], status: "Resolved"},
  ];
  let refreshUpdates = false, failHealth = false;
  await context.route("**/*", route => {
    const url = route.request().url().replace(/^https?:\/\/[^/]+/, ""), path = url.split("?")[0];
    if (path.startsWith("/api/")) {
      requests.push(url);
      const items = path.endsWith("azure-updates") ? (refreshUpdates ? updates.filter(x => x.services.includes("Azure Functions")) : updates)
        : path.endsWith("azure-status") ? status : health;
      return route.fulfill({json: {
        status: failHealth && path.endsWith("service-health") ? "error" : "ok",
        error: failHealth ? "Synthetic source unavailable" : null, items,
        total: items.length, truncated: false,
        freshness: {fetched_at: new Date().toISOString(), max_age_seconds: 600, cached: true},
      }});
    }
    const name = path.split("/").at(-1);
    if (files[name]) return route.fulfill({
      contentType: name.endsWith(".css") ? "text/css" : "text/javascript", body: files[name],
    });
    return route.fulfill({contentType: "text/html", body:
      '<html data-theme="light"><head><link rel="stylesheet" href="/app.css"></head><body><main id="tabBody"></main></body></html>'});
  });
  let assertions = 0;
  const assert = (condition, message) => { if (!condition) throw Error(message); assertions++; };
  try {
    await test.goto("http://127.0.0.1:18439/feed-fixture");
    await test.evaluate(async () => {
      const {createTabs} = await import("/dashboard-updates.js");
      const {replaceContent, stableKey} = await import("/dashboard-state.js");
      const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      })[c]);
      window.ctx = {
        $: id => document.getElementById(id), esc, stableKey, loadToken: 1,
        get: async url => (await fetch(url)).json(), setContent: replaceContent,
        portalScopeIds: () => ["sub-a"], allSubscriptions: () => [{id: "sub-a", name: "Example"}],
        portalLinkMarkup: () => "",
      };
      window.feeds = createTabs(window.ctx);
      await window.feeds.renderAzureUpdates(1);
    });
    assert(requests.length === 3, "Sources load independently once");
    await test.locator("#azure-updates-toggle").click();
    await test.locator("#azure-status-toggle").click();
    await test.locator("#azure-health-toggle").click();
    const service = test.getByRole("combobox", {name: "Filter Azure Updates by service type", exact: true});
    await service.selectOption(JSON.stringify("Azure Functions"));
    assert(await test.locator("#azure-updates .azure-feed-item").count() === 10, "Filter applies before paging");
    assert((await test.locator("#azure-updates .azure-source-foot").innerText()).includes("of 12"), "All retrieved pages participate");
    await test.locator("#azure-updates-category").selectOption("GA");
    assert((await test.locator("#azure-updates .empty").innerText()).includes("No retrieved items match"), "Dimensions compose with category");
    await test.locator("#azure-updates-clear").click();
    assert(await service.inputValue() === "" && await test.locator("#azure-updates-category").inputValue() === "", "Clear resets actual DOM and model");
    await service.selectOption("null");
    assert(await test.locator("#azure-updates .azure-feed-item").count() === 1, "Unknown service metadata stays selectable");
    await test.locator("#azure-status-service").selectOption(JSON.stringify("Azure Storage"));
    assert(await test.locator("#azure-status .azure-feed-item").count() === 1, "Status has its own service filter");
    const region = test.getByRole("combobox", {name: "Filter Service Health by region", exact: true});
    assert((await region.locator("option").allTextContents()).filter(x => /central.?india/i.test(x)).length === 1, "Region aliases are deduplicated");
    await region.selectOption(JSON.stringify("Central India"));
    assert(await test.locator("#azure-health .azure-feed-item").count() === 1, "Region matches any reported alias");
    assert(requests.length === 3, "Local filtering never fans out to Azure");
    // Alignment is a measurement, not an opinion: every control in the bar must sit on one
    // bottom edge. A labelled field centred against bare ones is what knocked them out of line.
    // The attention checkbox is measured by its row, since a 13px box is centred in that row
    // rather than dropped onto a 34px select's baseline.
    const bar = await test.evaluate(() => {
      const box = node => node.getBoundingClientRect();
      const controls = [...document.querySelectorAll(
        "#azure-updates .azure-feed-controls :is(input[type=search], select)")];
      const rows = [...controls, ...document.querySelectorAll("#azure-updates .azure-feed-controls .azure-attention")];
      const bottoms = rows.map(node => Math.round(box(node).bottom));
      const check = document.querySelector("#azure-attention");
      const row = box(check.closest(".azure-attention")), tick = box(check);
      return {
        count: rows.length,
        spread: Math.max(...bottoms) - Math.min(...bottoms),
        heights: [...new Set(controls.map(node => Math.round(box(node).height)))],
        checkOffset: Math.abs(Math.round((tick.top - row.top) - (row.bottom - tick.bottom))),
        labelled: [...document.querySelectorAll("#azure-updates .azure-feed-controls .azure-filter")]
          .every(field => field.querySelector("span") && field.querySelector("input, select")),
      };
    });
    assert(bar.count >= 4, "The updates bar has its search, service type, category and attention controls");
    assert(bar.spread <= 1, `Filter controls share one baseline (spread ${bar.spread}px)`);
    assert(bar.heights.length === 1, `Inputs and selects are one height (${bar.heights.join(",")})`);
    assert(bar.checkOffset <= 1, `The attention checkbox is centred in its row (off by ${bar.checkOffset}px)`);
    assert(bar.labelled, "Every filter field pairs a visible label with its control");
    await test.locator("#azure-health-toggle").click();
    await test.locator("#azure-health-toggle").click();
    assert(await region.inputValue() === JSON.stringify("Central India"), "Collapse preserves region selection");
    failHealth = true;
    await test.locator("#azure-health [data-refresh-source]").click();
    await test.waitForFunction(() => !document.querySelector("#azure-health [data-refresh-source]").disabled);
    assert(await region.inputValue() === JSON.stringify("Central India"), "Failed refresh retains the active region");
    assert((await test.locator("#azure-health").innerText()).includes("Synthetic source unavailable"), "Failure is not an empty all-clear");
    failHealth = false;
    await service.selectOption(JSON.stringify("Azure Storage"));
    refreshUpdates = true;
    await test.locator("#azure-updates [data-refresh-source]").click();
    await test.waitForFunction(() => !document.querySelector("#azure-updates [data-refresh-source]").disabled);
    assert(await service.inputValue() === JSON.stringify("Azure Storage"), "A vanished service remains selected rather than silently clearing");
    assert((await service.locator("option:checked").innerText()).includes("(0)"), "Unavailable selection is explicit");
    await test.evaluate(() => { window.feeds.stop(); window.ctx.loadToken = 2; window.feeds.resume(2, "azure-updates"); });
    assert(await region.inputValue() === JSON.stringify("Central India"), "Cached return preserves filters");
    await test.setViewportSize({width: 390, height: 844});
    assert(await test.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Filters fit a narrow viewport");
    assert(requests.filter(url => /azure-(updates|status)/.test(url)).every(url => !/[?&](scope|service|region)=/.test(url)), "Public source requests remain global");
    assert(requests.some(url => url.includes("service-health?scope=sub-a")), "Private health remains subscription scoped");
    assert(errors.length === 0, errors.join("; "));
    return {assertions, requests: requests.length, errors};
  } finally {
    await test.evaluate(() => window.feeds?.stop()).catch(() => {});
    await context.close();
  }
}
