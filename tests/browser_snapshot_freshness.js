async (page, { assetOrigin = "http://127.0.0.1:18439" } = {}) => {
  // Entire app, fresh browser context, local assets, and synthetic API responses only.
  const context = await page.context().browser().newContext({ viewport: { width: 1440, height: 960 } });
  const test = await context.newPage();
  const errors = [], reads = [], writes = [];
  test.on("pageerror", error => errors.push(error.message));
  let usdReads = 0, overviewReads = 0, firstRelease, lateRelease;
  const oldStamp = "2026-09-14T10:00:00Z", newStamp = "2026-09-14T11:00:00Z";
  const rows = (version, currency, cost) => Array.from({ length: 45 }, (_, index) => ({
    id: `/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-${index}`,
    name: `keep-${version}-${index}`, resource_group: "rg", subscription: "Alpha",
    subscription_id: "sub-a", service: "Virtual Machines", area: "Compute", region: "eastus",
    currency, cost: cost + index, prior_cost: 1, tags: [],
  }));
  const headers = (stale, stamp) => ({
    "X-CloudLens-Cache": stale ? "stale" : "hit", "X-CloudLens-As-Of": stamp,
    "X-CloudLens-Source-Generation": stamp, "X-CloudLens-Stale": String(stale),
    "X-CloudLens-Revalidating": String(stale),
  });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (path.startsWith("/api/")) {
      reads.push(path);
      if (request.method() !== "GET") {
        writes.push(path);
        return route.fulfill({ status: 405, json: { detail: "No writes in this fixture" } });
      }
      if (path === "/api/overview") {
        const stale = ++overviewReads === 1;
        return route.fulfill({
          json: { status: "ready", currency: url.searchParams.get("currency") || "USD",
            month_to_date: stale ? 100 : 200, last_month: 80, forecast_30d: 220 },
          headers: headers(stale, stale ? oldStamp : newStamp),
        });
      }
      if (path === "/api/dashboard/savings") {
        return route.fulfill({
          json: { count: 1, total: 12, days: 30,
            currency: url.searchParams.get("currency") || "USD",
            unavailable: ["waste"],
            scan: { status: "ready", errors: { waste: "Incomplete pricing evidence" } } },
          headers: headers(false, newStamp),
        });
      }
      if (path === "/api/dashboard/resources") {
        const currency = url.searchParams.get("currency") || "USD";
        if (currency === "EUR") return route.fulfill({
          json: { resources: rows("eur", "EUR", 300), days: 30 }, headers: headers(false, newStamp),
        });
        const count = ++usdReads;
        if (count === 2 || count === 4) {
          await new Promise(resolve => { if (count === 2) firstRelease = resolve; else lateRelease = resolve; });
          return route.fulfill({ json: { resources: rows(count === 2 ? "v2" : "late", "USD", 2000), days: 30 },
            headers: headers(false, newStamp) }).catch(() => {});
        }
        return route.fulfill({ json: { resources: rows(count === 1 ? "v1" : "v2", "USD", 10), days: 30 },
          headers: headers(true, oldStamp) });
      }
      const data = path === "/api/dashboard/sections"
        ? { sections: [{ id: "compute", label: "Compute", cost: 100 }], total: 100, currency: "USD" }
        : path === "/api/dashboard/executive" ? { empty: true, currency: "USD" }
        : path === "/api/subscriptions" ? { subscriptions: [{ id: "sub-a", name: "Alpha", in_warehouse: true }] }
        : path === "/api/whoami" ? { user: { username: "offline", admin: false } }
        : path === "/api/currency" ? { supported: ["USD", "EUR"] }
        : path === "/api/filters" ? { dimensions: {} } : {};
      return route.fulfill({ json: data });
    }
    if (path === "/" || path.startsWith("/assets/")) {
      const response = await context.request.get(`${assetOrigin}${path}${url.search}`);
      return route.fulfill({ status: response.status(), body: await response.body(),
        contentType: response.headers()["content-type"] || "application/octet-stream" });
    }
    return route.fulfill({ status: 404, body: "" });
  });
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const waitForPoll = async ready => {
    const deadline = Date.now() + 20000;
    while (!ready()) {
      if (Date.now() > deadline) throw Error("The active-view revalidation poll did not start");
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  };
  try {
    await test.goto("http://snapshot-fixture.invalid/");
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    await test.locator('#tabs [data-main-section="resources"]').click();
    await test.locator("#resourceExplorer").waitFor();
    await test.locator("#resourceSearch").fill("keep");
    await test.locator('#resourceResults th[data-col="3"]').click();
    await test.locator('#resourceResults th[data-col="3"]').click();
    await test.waitForFunction(() => document.querySelector("#viewFreshness")?.textContent.includes("Updating"));
    await waitForPoll(() => firstRelease);
    await test.evaluate(() => {
      const body = document.querySelector("#tabBody");
      body.style.maxHeight = "240px"; body.scrollTop = 100;
    });
    const sort = await test.locator('#resourceResults th[data-col="3"]').getAttribute("aria-sort");
    check((await test.locator("#resourceResults").textContent()).includes("keep-v1"), "Cached content remains while fresh GET is deferred");
    check(await test.locator("#tabBody .sk-wrap").count() === 0, "No global Loading blank");
    check(await test.locator("#tabBody").evaluate(element => element.scrollTop) > 50, "Fixture starts scrolled");
    firstRelease();
    await test.waitForFunction(() => document.querySelector("#resourceResults")?.textContent.includes("keep-v2")
      && document.querySelector("#viewFreshness")?.dataset.freshness === "fresh");
    check(await test.locator("#resourceSearch").inputValue() === "keep", "Filter survives fresh repaint");
    check(await test.locator('#resourceResults th[data-col="3"]').getAttribute("aria-sort") === sort, "Sort survives fresh repaint");
    check(await test.locator("#tabBody").evaluate(element => element.scrollTop) > 50, "Scroll survives fresh repaint");
    await test.waitForFunction(() => document.querySelector("#headerFreshness")?.dataset.freshness === "fresh"
      && document.querySelector("#glance")?.children[0].textContent.includes("200"));
    check((await test.locator("#glance").textContent()).includes("partial"),
      "Fresh header preserves the partial Savings coverage warning");
    await test.locator("#refreshViews").click();
    await waitForPoll(() => lateRelease);
    await test.locator("#currency").selectOption("EUR");
    await test.waitForFunction(() => document.querySelector("#resourceResults")?.textContent.includes("keep-eur"));
    lateRelease();
    await test.waitForTimeout(100);
    check((await test.locator("#resourceResults").textContent()).includes("keep-eur"), "Old context cannot overwrite the current currency");
    check(writes.length === 0, "No writes were attempted");
    check(errors.length === 0, errors.join("; "));
    return { passed: 11, usdReads, overviewReads, writes, errors };
  } finally {
    firstRelease?.(); lateRelease?.();
    await context.close();
  }
}
