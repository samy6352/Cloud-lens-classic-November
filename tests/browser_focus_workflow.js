async (page) => {
  // Serve web/ on localhost:18439. All browser traffic, including writes, is intercepted.
  const context = await page.context().browser().newContext();
  const test = await context.newPage();
  const sourceResponse = await context.request.get("http://127.0.0.1:18439/assets/dashboard-reports.js");
  if (!sourceResponse.ok()) throw Error("Local static assets are unavailable");
  const source = await sourceResponse.text();
  const cssResponse = await context.request.get("http://127.0.0.1:18439/assets/app.css");
  const css = cssResponse.ok() ? await cssResponse.text() : "";
  const writes = [], reads = [], errors = [];
  test.on("pageerror", error => errors.push(error.message));
  const owned = (id, recurrence) => ({
    name: `cloudlens-focus-${id}`, subscription_id: id, subscription: id === "sub-a" ? "Alpha" : "Beta",
    recurrence, metric: "FocusCost", status: "Active", owned: true, can_write: true,
    identity: true, account: "offline", container: "focus-exports", folder: `focus/${id}`,
    etag: `etag-${id}`, retention: "CreateNewReport", coverage: "MonthToDate",
  });
  const fixture = {
    can_write: true,
    subscriptions: [{ id: "sub-a", name: "Alpha", can_write: true },
      { id: "sub-b", name: "Beta", can_write: true },
      { id: "sub-c", name: "Read-only subscription", can_write: false, write_reason: "Export write access is required." }],
    schedules: [],
    destination: { account_id: "/subscriptions/storage/storageAccounts/offline",
      container: "focus-exports", folder: "focus/<subscription-id>" },
    sync: { status: "idle", subscriptions: [], poll_seconds: 900 },
  };
  let job = 0;
  await context.route("**/*", async route => {
    const request = route.request();
    const pathname = request.url().replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    if (pathname === "/reports.js")
      return route.fulfill({ contentType: "text/javascript", body: source });
    if (pathname === "/app.css")
      return route.fulfill({ contentType: "text/css", body: css });
    if (pathname.startsWith("/api/")) {
      if (request.method() === "GET") {
        reads.push(pathname);
        return route.fulfill({ contentType: "application/json", body: JSON.stringify(
          pathname === "/api/schedules/refresh" ? fixture.sync : fixture) });
      }
      const data = request.postDataJSON();
      writes.push({ path: pathname, method: request.method(), data });
      if (pathname === "/api/schedules") {
        const existing = fixture.schedules.find(row => row.owned && row.subscription_id === data.subscription);
        if (existing) Object.assign(existing, { recurrence: data.recurrence, etag: `version-${writes.length}` });
        else fixture.schedules.push(owned(data.subscription, data.recurrence));
      }
      fixture.sync = { status: "running", started: `2026-09-10T09:${30 + ++job}:00Z`,
        subscriptions: (data.scope || [data.subscription]).map(subscription => ({
          subscription, status: "running", detail: "Waiting for completed Azure report",
        })) };
      return route.fulfill({ contentType: "application/json",
        body: JSON.stringify({ status: "started", first_run_started: true }) });
    }
    return route.fulfill({ contentType: "text/html",
      body: '<html><head><link rel="stylesheet" href="/app.css"></head><body><main id="tabBody" style="max-width:1100px;margin:20px auto"></main></body></html>' });
  });
  let passed = 0;
  const assert = (value, message) => { if (!value) throw Error(message); passed++; };
  try {
    await test.goto("http://focus-workflow-fixture.invalid/");
    await test.evaluate(async () => {
      const { createTabs } = await import("/reports.js");
      window.feedback = [];
      window.events = [];
      window.addEventListener("cloudlens:focus-synced", event => window.events.push(event.detail));
      window.ctx = {
        $: id => document.getElementById(id), loadToken: 1, scopeIds: () => [], qs: () => "",
        key: value => value, readStore: (_, fallback) => fallback,
        get: async (url, options) => (await fetch(url, { signal: options?.signal })).json(),
        setContent: (host, markup) => { host.innerHTML = markup; },
        esc: value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
        whoAmI: async () => ({ user: { admin: false }, azure_mode: "app" }),
        toast: (...args) => window.feedback.push(args), allSubscriptions: () => [],
      };
      window.reports = createTabs(window.ctx);
      await window.reports.renderSettings(1);
    });
    assert(await test.inputValue("#sEvery") === "Daily", "Daily defaults for a new schedule");
    assert((await test.textContent("#sEvery")).includes("Daily — recommended"), "Daily recommendation is explicit");
    assert(await test.locator("#sCreate").isVisible(), "App identity does not hide API-authorized controls");
    assert(await test.locator('#sSub option[value="sub-c"]').evaluate(option => option.disabled),
      "Read-only subscription cannot be selected");
    assert(writes.length === 0, "Opening the view never changes Azure");
    await test.selectOption("#sSub", "*");
    await test.selectOption("#sEvery", "Weekly");
    test.once("dialog", dialog => dialog.dismiss());
    await test.click("#sCreate");
    assert(writes.length === 0, "Cancelled batch review performs no writes");
    let preview = "";
    test.once("dialog", async dialog => { preview = dialog.message(); await dialog.accept(); });
    await test.click("#sCreate");
    await test.waitForFunction(() => !document.getElementById("sCreate").disabled);
    assert(writes.length === 2 && writes.every(write => write.path === "/api/schedules"), "Only two authorized batch saves");
    assert(writes.every(write => write.data.metric === "FocusCost" && write.data.recurrence === "Weekly"),
      "Batch writes preserve FOCUS and reviewed cadence");
    assert(preview.includes("Alpha") && preview.includes("Beta") && !preview.includes("sub-c"), "Review names exactly match write targets");
    assert(preview.includes("MonthToDate") && preview.includes("Storage Blob Data Contributor") &&
      preview.includes("focus/sub-a") && preview.includes("storage costs grow"), "Coverage, destination and side effects are reviewed");
    assert((await test.evaluate(() => window.feedback)).length === 0, "Started exports are never shown as completed imports");
    assert(await test.inputValue("#sSub") === "*" && await test.inputValue("#sEvery") === "Weekly", "Save preserves form state");

    fixture.sync = { ...fixture.sync, status: "ready", finished: "2026-09-10T09:35:00Z",
      subscriptions: ["sub-a", "sub-b"].map(subscription => ({
        subscription, status: "ready", changed_days: 2, unchanged_days: 8, rows: 27,
      })) };
    await test.evaluate(() => document.getElementById("sCheck").onclick());
    assert((await test.textContent("#sSyncDetails")).includes("2 changed days"), "Delta counts are exposed");
    assert((await test.textContent("#sSyncDetails")).includes("Last import: 2026-09-10T09:35:00Z"), "Completed import timestamps are exposed");
    assert((await test.evaluate(() => window.feedback)).length === 1, "Only completed imports trigger success feedback");

    fixture.schedules[0].recurrence = "Monthly";
    fixture.schedules.push(...Array.from({ length: 7 }, (_, i) => ({
      ...owned("sub-a", "Daily"), name: `legacy-${i}`, owned: false, metric: "AmortizedCost",
    })));
    await test.evaluate(() => window.reports.renderSettings(1));
    await test.locator("details.sched").first().locator("summary").click();
    await test.locator("[data-edit]").first().click();
    assert(await test.inputValue("#sEvery") === "Monthly", "Edit loads current cadence");
    await test.selectOption("#sSub", "sub-b");
    assert(await test.inputValue("#sEvery") === "Weekly", "Changing subscription does not copy another cadence");
    await test.selectOption("#sSub", "sub-a");
    const readsBeforePage = reads.length;
    await test.click("#sNext");
    assert(await test.textContent("#sScheduleCount") === "Showing 6–9 of 9 schedules", "Existing schedules remain paginated");
    await test.click("#sPrev");
    assert(reads.length === readsBeforePage, "Paging is entirely local");
    assert(await test.inputValue("#sSub") === "sub-a" && await test.inputValue("#sEvery") === "Monthly",
      "Paging preserves the edit form");
    let refreshPreview = "";
    test.once("dialog", async dialog => { refreshPreview = dialog.message(); await dialog.accept(); });
    await test.click("#sRefresh");
    await test.waitForFunction(() => document.getElementById("sSyncSummary").textContent.includes("Refreshing latest"));
    assert(writes.at(-1).path === "/api/schedules/refresh" &&
      JSON.stringify(writes.at(-1).data) === '{"scope":["sub-a"]}', "Refresh targets only the chosen subscription");
    assert(refreshPreview.includes("current month") && refreshPreview.includes("Saved cadences do not change"),
      "Monthly refresh explicitly covers the current month without changing cadence");
    assert(fixture.schedules[0].recurrence === "Monthly", "Refresh does not mutate the saved monthly schedule");
    const listingReads = reads.filter(path => path === "/api/schedules").length;
    await test.evaluate(() => {
      window.reports.stop();
      window.saved = document.createDocumentFragment();
      window.saved.append(...document.getElementById("tabBody").childNodes);
      document.getElementById("tabBody").innerHTML = "<p>Another cached tab</p>";
      window.ctx.loadToken = 2;
    });
    fixture.sync = { ...fixture.sync, status: "ready", finished: "2026-09-10T09:40:00Z",
      subscriptions: [{ subscription: "sub-a", status: "ready", changed_days: 1, unchanged_days: 9, rows: 30 }] };
    await test.evaluate(async () => {
      document.getElementById("tabBody").replaceChildren(window.saved);
      window.ctx.loadToken = 1;
      await window.reports.resume(1, "settings");
    });
    assert(reads.filter(path => path === "/api/schedules").length === listingReads, "Cached return polls status without reloading exports");
    assert(await test.inputValue("#sEvery") === "Monthly", "Cached return preserves cadence draft");
    assert((await test.textContent("#sSyncDetails")).includes("1 changed days"), "Cached return observes completed import");
    await test.setViewportSize({ width: 390, height: 844 });
    assert(await test.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
      "Compact form fits a mobile viewport");
    test.once("dialog", dialog => dialog.accept());
    await test.evaluate(async () => {
      window.reports.stop();
      document.getElementById("tabBody").innerHTML = "<section>Cached overview</section>";
      window.ctx.loadToken = 2;
      window.headerStatuses = [];
      window.headerTracker = await window.reports.refreshLatest({
        onStatus: status => window.headerStatuses.push(status),
      });
    });
    assert(writes.at(-1).path === "/api/schedules/refresh" &&
      JSON.stringify(writes.at(-1).data) === '{"scope":["sub-a","sub-b"]}',
      "Header resolves all selected writable subscriptions instead of the settings form selection");
    assert(await test.locator("#sSub").count() === 0, "Header does not require the settings DOM");
    fixture.sync = { ...fixture.sync, status: "ready", finished: "2026-09-10T10:00:00Z",
      subscriptions: ["sub-a", "sub-b"].map(subscription => ({
        subscription, status: "ready", changed_days: 1, unchanged_days: 9, rows: 30,
      })) };
    await test.waitForFunction(() => window.headerTracker.status === "ready");
    assert(await test.textContent("#tabBody") === "Cached overview", "Header tracking preserves the active cached tab");
    assert(await test.evaluate(() => !window.headerTracker.isActive()), "Completed header tracker owns and clears its timer");
    assert((await test.evaluate(() => window.headerStatuses)).at(-1).status === "ready",
      "Header callback receives terminal import status");
    assert(errors.length === 0, errors.join("\n"));
    return { passed, mockedWrites: writes.length, liveAzureWrites: 0, errors };
  } finally {
    await test.evaluate(() => { window.reports?.stop(); window.headerTracker?.stop(); }).catch(() => {});
    await context.close();
  }
}
