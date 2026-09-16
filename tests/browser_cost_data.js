async (page) => {
  // Real browser and real CloudLens assets; every HTTP request is fulfilled offline.
  const context = await page.context().browser().newContext();
  const sourceResponse = await context.request.get("http://127.0.0.1:18439/assets/dashboard-reports.js");
  if (!sourceResponse.ok()) throw Error("Serve web/ on localhost:18439 for this offline fixture.");
  const source = await sourceResponse.text();
  const cssResponse = await context.request.get("http://127.0.0.1:18439/assets/app.css");
  const css = await cssResponse.text();
  const test = await context.newPage();
  const reads = [], readUrls = [], writes = [], errors = [];
  test.on("pageerror", error => errors.push(error.message));
  const fixture = {
    can_review: true, auto_enabled: true, setup_needed: true,
    subscriptions: ["Alpha", "Beta", "Gamma"].map((name, i) => ({
      id: `sub-${i}`, name, status: "idle", configured: false, config: {}, tasks: [],
      coverage: { requested_days: 90, authoritative_days: 0, missing_days: 90 },
      history: { from: "2026-06-18", to: "2026-09-15" },
    })),
    cadences: {
      Daily: "Check daily at 02:00 UTC; refresh seven recent days.",
      Weekly: "Mondays at 02:00 UTC; refresh fourteen recent days.",
      Monthly: "Previous completed calendar month plus corrections; up to a month lag.",
    },
    note: "Existing costs stay available. Quick estimates do not establish FOCUS coverage.",
  };
  let blocked = false;
  await context.route("**/*", async route => {
    const request = route.request(), path = request.url().replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    if (path === "/reports.js") return route.fulfill({ contentType: "text/javascript", body: source });
    if (path === "/app.css") return route.fulfill({ contentType: "text/css", body: css });
    if (path.startsWith("/api/")) {
      if (request.method() === "GET") {
        reads.push(path);
        readUrls.push(request.url());
        if (path === "/api/capabilities") return route.fulfill({ json: {
          generated_at: "2026-09-15T10:00:00Z", capabilities: [{
            feature: "Export execution", status: "unknown", principal: { id: "offline-workload" },
            required_role: "Cost Management Contributor", scope: "/subscriptions/sub-0",
            reason: "Not yet verified", next_step: "Ask the administrator to verify this principal and subscription.",
          }],
        } });
        if (path === "/api/cost-data") {
          const scopes = decodeURIComponent(/[?&]scope=([^&]+)/.exec(request.url())?.[1] || "").split(",");
          return route.fulfill({ json: { ...fixture, subscriptions: fixture.subscriptions.filter(s => scopes.includes(s.id)) } });
        }
        return route.fulfill({ json: path === "/api/schedules" ? {
          schedules: Array.from({ length: 27 }, (_, i) => ({ name: `external-${i}`, owned: false, status: "Active" })),
        } : fixture });
      }
      const body = request.postDataJSON();
      writes.push({ path, body });
      if (path.endsWith("/preview")) return route.fulfill({ json: {
        can_start: !blocked, from: body.start, to: body.end, preview_token: "owned-etag-v1",
        identity: "Persistent application workload identity", explanation: fixture.cadences[body.cadence],
        notes: ["External and legacy exports are untouched. No broad roles are granted.",
          "Azure generation can take hours. Browser closure is safe; keep the backend running."],
        subscriptions: fixture.subscriptions.map(s => ({
          subscription: s.id, name: s.name, action: "Reuse owned export and pause native recurrence",
          export: { name: `owned-${s.id}`, etag: "v1" }, window: { from: body.start, to: body.end },
          destination: { account_id: "deployment-storage", container: "focus", folder: s.id },
          blockers: blocked ? ["Workload identity needs Blob read access on this destination."] : [],
        })),
      } });
      if (path.endsWith("/setup")) {
        for (const s of fixture.subscriptions) {
          s.status = "waiting-for-Azure"; s.job_id = "durable-offline-job";
          s.configured = true; s.config = { cadence: body.cadence, enabled: true };
        }
      }
      return route.fulfill({ json: { job_id: "durable-offline-job", status: "queued" } });
    }
    return route.fulfill({ contentType: "text/html", body:
      '<html><head><link rel="stylesheet" href="/app.css"></head><body><main id="tabBody" style="max-width:1100px;margin:20px auto"></main></body></html>' });
  });
  let assertions = 0;
  const assert = (value, message) => { if (!value) throw Error(message); assertions++; };
  try {
    await test.goto("http://127.0.0.1:18439/cost-data-fixture");
    await test.evaluate(async () => {
      const { createTabs } = await import("/reports.js");
      const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      })[c]);
      window.ctx = {
        $: id => document.getElementById(id), esc, scopeIds: () => ["sub-0"],
        allSubscriptions: () => [], readStore: (_, value) => value,
        writeStore: () => { throw Error("No persistent browser cost data"); }, key: name => name,
        loadToken: 1, qs: () => "?scope=sub-0,sub-1,sub-2",
        get: async (url, options) => { const r = await fetch(url, { signal: options?.signal }); if (!r.ok) throw Error("read"); return r.json(); },
        setContent: (host, markup) => { host.innerHTML = markup; }, toast: () => {},
        table: (title, rows, columns) => `<h4>${esc(title)}</h4>${rows.map(row =>
          `<dl>${columns.map(col => `<dt>${esc(col.label)}</dt><dd>${col.cell(row)}</dd>`).join("")}</dl>`).join("")}`,
        wireTables: () => {},
        whoAmI: async () => ({ user: { admin: true } }),
      };
      window.reports = createTabs(window.ctx);
      await window.reports.renderSettings(1);
    });
    assert(reads.length === 2 && reads[0] === "/api/cost-data/subscriptions" && reads[1] === "/api/cost-data", "First paint loads entitled catalog and cached status, not exports");
    assert(writes.length === 0, "Opening setup is read-only");
    assert(await test.locator("#cdCadence-Daily").isChecked(), "Daily is recommended");
    assert(await test.locator("#cdSubsMenu").isHidden(), "The subscription list is a menu, closed until asked for");
    assert((await test.locator("#cdSubsSummary").innerText()).includes("1 of 3 selected"), "The closed trigger states the selection");
    await test.locator("#cdSubsToggle").click();
    assert(await test.locator("#cdSubsMenu").isVisible(), "The trigger opens the menu");
    assert(await test.locator("#cdSearch").evaluate(el => el === document.activeElement), "Opening the menu focuses its search box");
    assert(await test.locator("[data-ingest-sub]").count() === 3, "Catalog shows all readable subscriptions despite narrow header");
    assert(await test.locator("[data-ingest-sub]:checked").count() === 1, "Initial ingest selection follows the narrow header");
    await test.locator("#cdSearch").fill("sub-2");
    assert(await test.locator("[data-ingest-row]:visible").count() === 1, "Search narrows the list in place");
    await test.locator("#cdSearch").fill("no such subscription");
    assert(await test.locator("#cdSubsEmpty").isVisible(), "An empty search result says so");
    await test.locator("#cdSearch").fill("");
    // Ticking a box re-reads status, which rebuilds the card. Closing the menu there would
    // mean reopening it for every subscription after the first.
    const beforeTick = reads.length;
    await test.locator("#cdSub1").check();
    // #cdProgress holds the interim "reading saved status" note and is only rewritten with the
    // verified-days line once the scoped read has landed and the card has been rebuilt. The
    // day count is per subscription, so it also proves the new scope is the one on screen.
    const settled = (summary, days) => test.waitForFunction(
      ({ summary, days }) => document.querySelector("#cdProgress")?.textContent.includes(`of ${days} subscription-days`)
        && document.querySelector("#cdSubsSummary")?.textContent.includes(summary)
        && document.activeElement?.id === "cdSub1",
      { summary, days });
    await settled("2 of 3", 180);
    assert(reads.length > beforeTick, "A new selection reads its own saved status");
    assert(await test.locator("#cdSubsMenu").isVisible(), "The menu survives its own selection");
    await test.locator("#cdSub1").uncheck();
    await settled("1 of 3", 90);
    await test.keyboard.press("Escape");
    assert(await test.locator("#cdSubsMenu").isHidden(), "Escape closes the menu");
    assert(await test.locator("#cdSubsToggle").evaluate(el => el === document.activeElement), "Escape returns focus to the trigger");
    await test.locator("#cdSubsToggle").click();
    await test.locator("#cdScopeTitle").click();
    assert(await test.locator("#cdSubsMenu").isHidden(), "A click outside closes the menu");
    assert(await test.locator("#sCreate,#fVerify,#fDailyPreview").count() === 0, "No competing technical creation controls");
    for (const width of [1280, 390]) {
      await test.setViewportSize({ width, height: 900 });
      assert(await test.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `No horizontal overflow at ${width}px`);
    }
    await test.locator("#cdTab-first").focus();
    await test.keyboard.press("ArrowRight");
    assert(await test.locator("#cdTab-schedule").evaluate(el => el === document.activeElement), "Arrow key moves tab focus");
    await test.keyboard.press("End");
    assert(await test.locator("#cdPanel-access").isVisible(), "End selects Access & fixes");
    assert(!reads.includes("/api/capabilities"), "Opening Access does not scan Azure permissions");
    await test.locator("#pInspect").click();
    await test.waitForFunction(() => document.querySelector("#pStatus").textContent.includes("No permissions were changed"));
    assert(readUrls.at(-1).includes("scope=sub-0"), "Access uses local ingest scope");
    assert(await test.locator("#pResult").innerText().then(t => t.includes("Unknown is not denied")), "Unknown permission evidence is not a denial");
    assert(await test.locator("#pCopy").count() === 1, "Administrator instructions are available to copy");
    await test.locator("#cdSubsToggle").click();
    await test.locator("#cdNone").click();
    await test.waitForFunction(() => document.querySelector("#cdScopeNote")?.textContent.includes("Choose at least one"));
    await test.keyboard.press("Escape");
    const afterClear = reads.length;
    assert(await test.locator("#pInspect").isDisabled(), "Empty ingest selection disables permission requests");
    await test.locator("#cdTab-first").click();
    assert(await test.locator("#cdReview").isDisabled(), "Empty ingest selection cannot expand into all");
    assert(reads.length === afterClear, "Empty selection and tab changes make no Azure requests");
    await test.locator("#cdSubsToggle").click();
    await test.locator("#cdAll").click();
    await test.waitForFunction(() => document.querySelector("#cdScopeNote")?.textContent.includes("3 selected"));
    await test.keyboard.press("Escape");
    await test.locator("#cdHistoryPreset").selectOption("custom");
    await test.locator("#cdHistoryDays").fill("366");
    assert(await test.locator("#cdReview").isDisabled(), "Client rejects 366 days");
    await test.locator("#cdHistoryDays").fill("1");
    assert(await test.locator("#cdReview").isEnabled(), "One day is valid");
    await test.locator("#cdHistoryPreset").selectOption("365");
    const chosenFrom = await test.locator("#cdFrom").inputValue();
    await test.locator("#cdCheck").click();
    await test.waitForFunction(() => document.querySelector("#cdHistoryDays")?.value === "365");
    assert(await test.locator("#cdFrom").inputValue() === chosenFrom, "Status polling preserves history choice");
    await test.locator("#cdReview").focus();
    await test.keyboard.press("Enter");
    await test.locator("#cdStart").waitFor();
    assert(writes.length === 1 && writes[0].path.endsWith("/preview"), "Keyboard review is pure");
    assert(writes[0].body.history_days === 365, "Preview binds selected history length");
    await test.locator("#cdCancel").click();
    assert(writes.length === 1, "Cancel does not write");
    await test.locator("#cdTab-schedule").click();
    await test.locator("#cdCadence-Weekly").check();
    await test.locator("#cdTab-first").click();
    await test.locator("#cdReview").click();
    await test.locator("#cdStart").click();
    await test.waitForFunction(() => document.querySelector("#cdNotice").textContent.includes("queued"));
    assert(writes.filter(w => w.path.endsWith("/setup")).length === 1, "One confirmed durable setup write");
    assert(writes.at(-1).body.scope.length === 3 && writes.at(-1).body.preview_token === "owned-etag-v1", "Approval binds scope and ETag");
    await test.waitForFunction(() => document.querySelector("#cdStatus").textContent.includes("Awaiting Azure"));
    assert((await test.locator("#cdStatus").innerText()).includes("Awaiting Azure"), "Azure acceptance is not completion");
    assert(await test.locator("#cdStatus").innerText().then(t => !t.includes("durable-offline-job")), "Raw job IDs stay advanced");
    assert(await test.locator("#cdReview").isDisabled(), "Pending setup cannot be duplicated");
    for (const s of fixture.subscriptions) { s.status = "partial"; s.coverage.authoritative_days = 30; }
    await test.locator("#cdCheck").click();
    await test.waitForFunction(() => document.querySelector("#cdStatus").textContent.includes("90 of 270"));
    assert((await test.locator("#cdStatus").innerText()).includes("90 of 270"), "Partial coverage stays visible");
    blocked = true;
    await test.locator("#cdReview").click();
    assert(await test.locator("#cdStart").isDisabled(), "Missing workload permission blocks approval");
    await test.locator("#cdCancel").click();

    for (const s of fixture.subscriptions) {
      s.configured = false; s.status = "partial";
      s.detail = "Waiting for automatic-update configuration; retry missing work after fixing access.";
      s.coverage = {requested_days: 90, authoritative_days: 90, missing_days: 0};
    }
    await test.evaluate(() => window.reports.renderSettings(1));
    await test.locator("#cdTab-schedule").click();
    for (const width of [1280, 390]) {
      await test.setViewportSize({width, height: 900});
      for (const cadence of ["Daily", "Weekly", "Monthly"])
        assert(await test.locator("#cdCadence-" + cadence).isVisible(), `${cadence} option is visible at ${width}px`);
      const layout = await test.evaluate(() => {
        const controls = document.querySelector("#cdFrequency").getBoundingClientRect();
        const action = document.querySelector("#cdChange").getBoundingClientRect();
        const status = document.querySelector("#cdStatusDetails").getBoundingClientRect();
        return {gap: action.top - controls.bottom, actionBeforeStatus: action.bottom <= status.top,
          fits: document.documentElement.scrollWidth <= innerWidth};
      });
      assert(layout.fits && layout.actionBeforeStatus && layout.gap < 180,
        `Primary action sits beside the choices, before details, at ${width}px`);
    }
    await test.locator("#cdCadence-Monthly").check();
    assert((await test.locator("#cdChange").innerText()) === "Review monthly schedule", "Next action names the chosen cadence");
    assert(await test.locator("#cdChange").isEnabled(), "Previous errors do not hide schedule review");
    const beforeSchedule = writes.length;
    await test.locator("#cdChange").click();
    assert(writes.length === beforeSchedule + 1 && writes.at(-1).body.action === "schedule",
      "Schedule review is pure and separate from historical import");
    assert(writes.at(-1).body.start === undefined && writes.at(-1).body.history_days === undefined,
      "Schedule approval carries no hidden history values");
    assert(await test.locator("#cdPanel-schedule").isVisible(), "Review stays on Scheduled updates");
    assert(await test.locator("#cdScheduleReview #cdStart").count() === 1, "Confirmation is next to schedule controls");
    assert(await test.locator("#cdStart").isDisabled(), "Current readiness blocker still prevents confirmation");
    await test.locator("#cdReviewFixes").click();
    assert(await test.locator("#cdPanel-access").isVisible(), "Readiness fixes are reachable");
    await test.locator("#cdBackToSchedule").click();
    assert(await test.locator("#cdCadence-Monthly").isChecked(), "Returning from fixes preserves the frequency");
    await test.locator("#cdChange").click();
    blocked = false;
    await test.locator("#cdRecheck").click();
    assert(await test.locator("#cdStart").isEnabled(), "Fresh recheck clears the old failure without an import detour");
    assert((await test.locator("#cdStart").innerText()) === "Enable monthly updates", "Final action explicitly enables the selected cadence");
    await test.locator("#cdStart").click();
    await test.waitForFunction(() => document.querySelector("#cdNotice").textContent.includes("queued"));
    assert(writes.filter(w => w.path.endsWith("/setup") && w.body.action === "schedule").length === 1,
      "Only the confirmed schedule is submitted");
    fixture.can_review = false;
    await test.evaluate(() => window.reports.renderSettings(1));
    assert(await test.locator("#cdReview").isDisabled(), "Readers retain status without writes");
    assert(await test.locator("#cdChange").isDisabled(), "Readers cannot enable schedules");
    assert(errors.length === 0, errors.join("; "));
    return { assertions, reads: reads.length, writes: writes.map(w => w.path), errors };
  } finally {
    await test.evaluate(() => window.reports?.stop()).catch(() => {});
    await context.close();
  }
}
