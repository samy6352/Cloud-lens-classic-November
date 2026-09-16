async (page) => {
  // Serve web/ locally on 18433. Every API and write below is fulfilled by this fixture.
  const context = await page.context().browser().newContext();
  const test = await context.newPage();
  const response = await context.request.get("http://127.0.0.1:18433/assets/dashboard-reports.js");
  if (!response.ok()) throw new Error("Local source server is unavailable");
  const source = await response.text();
  const writes = [], reads = [], errors = [];
  test.on("pageerror", e => errors.push(e.message));
  const owned = { name: "cloudlens-focus-owned", subscription: "A", subscription_id: "sub-a",
    metric: "FocusCost", recurrence: "Daily", status: "Active", identity: true, owned: true,
    account: "offline", container: "focus-exports", folder: "focus/sub-a", etag: "v1",
    coverage: "Current month to date", retention: "CreateNewReport", last_status: "Completed" };
  const fixture = { can_write: true, schedules: [owned,
    { ...owned, name: "cloudlens-amortized-daily", metric: "AmortizedCost", owned: false,
      legacy: true, retention: "OverwritePreviousReport" }],
    subscriptions: [{ id: "sub-a", name: "A" }],
    destination: { account_id: "/subscriptions/storage/storageAccounts/offline",
      container: "focus-exports", folder: "focus/<subscription-id>" } };
  for (let i = 1; i <= 10; i++) {
    fixture.schedules.push({ ...owned, name: `cloudlens-focus-page-${i}`, subscription: `Subscription ${i}`,
      subscription_id: `sub-${i}`, recurrence: "Weekly", folder: `focus/sub-${i}`, etag: `page-${i}` });
    fixture.subscriptions.push({ id: `sub-${i}`, name: `Subscription ${i}` });
  }
  await context.route("**/*", async route => {
    const request = route.request(), pathname = request.url().replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    if (pathname === "/reports.js")
      return route.fulfill({ contentType: "text/javascript", body: source });
    if (pathname.startsWith("/api/")) {
      if (request.method() === "GET") reads.push(pathname);
      if (request.method() !== "GET") writes.push({ method: request.method(),
        path: pathname, body: request.postDataJSON() });
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(
        request.method() === "GET" ? fixture : { name: owned.name, storage_access: "granted" }) });
    }
    return route.fulfill({ contentType: "text/html", body: '<div id="tabBody"></div>' });
  });
  try {
    await test.goto("http://focus-fixture.invalid/");
    await test.evaluate(async () => {
      const { createTabs } = await import("/reports.js");
      window.admin = true;
      window.scope = [];
      const ctx = { $: id => document.getElementById(id), loadToken: 1,
        allSubscriptions: () => [], cached: (_, f) => f(), cardHead: s => `<h3>${s.name}</h3>`,
        esc: v => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;"),
        get: async url => (await fetch(url)).json(), key: s => s, readStore: (_, d) => d,
        scopeIds: () => window.scope, qs: () => "?scope=" + window.scope.join(","),
        setContent: (el, html) => { el.innerHTML = html; }, toast: () => {},
        whoAmI: async () => ({ user: { admin: window.admin } }) };
      window.tabs = createTabs(ctx);
      await window.tabs.renderSettings(1);
    });
    let passed = 0;
    const assert = (ok, text) => { if (!ok) throw new Error(text); passed++; };
    const count = () => test.textContent("#sScheduleCount");
    const visibleNames = () => test.locator("#sScheduleList summary > strong").allTextContents();
    assert(await test.locator("details.sched").count() === 5, "Only five schedules rendered initially");
    assert(await test.locator("details.sched[open]").count() === 0, "Schedules start collapsed");
    assert(await count() === "Showing 1–5 of 12 schedules", "Honest first-page count");
    assert(await test.locator("#sPrev").isDisabled() && await test.locator("#sNext").isEnabled(),
      "First-page navigation bounds");
    assert(await test.locator("#sMetric option").count() === 1, "Only FOCUS is offered");
    assert(await test.locator("#sEvery option").count() === 3, "Three cadences");
    assert((await test.textContent("#tabBody")).includes("Legacy / external"), "Legacy visible");
    assert((await test.textContent("#tabBody")).includes("Completed"), "Run status visible");
    await test.locator("details.sched").first().locator("summary").click();
    await test.locator("[data-edit]").first().click();
    await test.locator("#sEvery").selectOption("Monthly");
    test.once("dialog", d => d.dismiss());
    await test.locator("#sCreate").click();
    assert(writes.length === 0, "Cancelled review must not write");
    let preview = "";
    test.once("dialog", async d => { preview = d.message(); await d.accept(); });
    await test.locator("#sCreate").click();
    await test.waitForFunction(() => !document.getElementById("sCreate").disabled);
    assert(writes.length === 1, "Exactly one approved write");
    assert(writes[0].body.metric === "FocusCost" && writes[0].body.recurrence === "Monthly",
      "FOCUS monthly payload");
    assert(writes[0].body.etag === "v1", "Cadence edit keeps reviewed eTag");
    assert(preview.includes("TheLastMonth") && preview.includes("focus/sub-a"), "Coverage/destination review");
    for (const selector of ["[data-run]", "[data-del]"]) {
      const schedule = test.locator("details.sched").first();
      if (!(await schedule.evaluate(element => element.open))) await schedule.locator("summary").click();
      test.once("dialog", d => d.dismiss());
      await test.locator(selector).first().click();
    }
    assert(writes.length === 1, "Run/delete cancellation prevents writes");
    const readsBeforePaging = reads.length;
    const seen = await visibleNames();
    await test.locator("#sNext").click();
    assert(await count() === "Showing 6–10 of 12 schedules", "Second-page count");
    seen.push(...await visibleNames());
    const paged = fixture.schedules[5];
    assert((await test.locator("details.sched").first().textContent()).includes(paged.subscription),
      "Paged summaries keep subscription identity");
    await test.locator("details.sched").first().locator("summary").click();
    await test.locator("[data-edit]").first().click();
    assert(await test.inputValue("#sSub") === paged.subscription_id
      && await test.inputValue("#sEvery") === paged.recurrence, "Paged edit uses the correct schedule");
    await test.locator("#sEvery").selectOption("Monthly");
    await test.locator("#sPrev").click();
    assert(await test.inputValue("#sSub") === paged.subscription_id
      && await test.inputValue("#sEvery") === "Monthly", "Paging preserves the approval form");
    await test.locator("#sNext").click();
    assert(await test.locator("details.sched").first().evaluate(element => element.open),
      "Paging preserves open schedule details");
    let runPreview = "", deletePreview = "";
    test.once("dialog", async d => { runPreview = d.message(); await d.dismiss(); });
    await test.locator("[data-run]").first().click();
    test.once("dialog", async d => { deletePreview = d.message(); await d.dismiss(); });
    await test.locator("[data-del]").first().click();
    assert(runPreview === `Run ${paged.name} on ${paged.subscription_id} now?\nDestination: ${paged.account}/${paged.container}/${paged.folder}\nCoverage: ${paged.coverage}\nRetention: ${paged.retention}. Azure and storage charges may apply.`,
      "Paged run preserves the exact confirmation and destination");
    assert(deletePreview === `Delete schedule ${paged.name} on ${paged.subscription_id}?\nFuture runs stop. All reports already stored remain untouched and continue incurring storage charges.`,
      "Paged delete preserves the exact confirmation");
    assert(writes.length === 1, "Paged action cancellation prevents writes");
    await test.locator("#sNext").click();
    seen.push(...await visibleNames());
    assert(await count() === "Showing 11–12 of 12 schedules"
      && await test.locator("details.sched").count() === 2, "Final partial page");
    assert(await test.locator("#sNext").isDisabled() && await test.locator("#sPrev").isEnabled(),
      "Last-page navigation bounds");
    assert(JSON.stringify(seen) === JSON.stringify(fixture.schedules.map(s => s.name)),
      "Every configured schedule is reachable exactly once");
    assert(reads.length === readsBeforePaging, "Paging makes no API requests");
    await test.evaluate(async () => window.tabs.renderSettings(1));
    assert(await count() === "Showing 11–12 of 12 schedules", "Refresh preserves the current page");
    fixture.schedules = fixture.schedules.slice(0, 6);
    await test.evaluate(async () => window.tabs.renderSettings(1));
    assert(await count() === "Showing 6–6 of 6 schedules"
      && await test.locator("details.sched").count() === 1, "Refresh clamps pages after removals");
    await test.evaluate(async () => { window.admin = false; await window.tabs.renderSettings(1); });
    assert(await test.locator("#sCreate,[data-edit],[data-run],[data-del]").count() === 0,
      "Read-only users have no write controls");
    await test.evaluate(async () => { window.scope = ["sub-a"]; await window.tabs.renderSettings(1); });
    assert(await count() === "Showing 1–5 of 6 schedules", "Changing subscription scope resets paging");
    fixture.schedules = [];
    await test.evaluate(async () => window.tabs.renderSettings(1));
    assert(await test.locator("#sScheduleList,#sPrev,#sNext").count() === 0
      && (await test.textContent("#tabBody")).includes("Nothing scheduled yet."), "Empty schedules have no pager");
    assert(errors.length === 0, errors.join("\n"));
    return { passed, writes: "one mocked approved update; zero live requests", errors };
  } finally {
    await context.close();
  }
}
