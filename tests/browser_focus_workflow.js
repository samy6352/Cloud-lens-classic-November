async (page, { createCostDataFixture }) => {
  const fixture = await createCostDataFixture(page);
  const { test, state, reads, previews, writes, errors, unexpected } = fixture;
  let passed = 0;
  const assert = (ok, message) => { if (!ok) throw new Error(message); passed++; };
  try {
    assert(writes.length === 0 && previews.length === 0, "Opening the workflow is read only");
    assert(await test.locator("#cdHistoryPreset").inputValue() === "90", "Initial import defaults to ninety days");
    await test.locator("#cdEditCadence").click();
    await test.locator("#cdCadence-Weekly").check();
    await test.locator("#cdTab-first").click();
    await test.locator("#cdReview").click();
    await test.locator("#cdStart").waitFor();
    assert(previews.at(-1).scope.join(",") === "sub-0,sub-1" && previews.at(-1).history_days === 90
      && previews.at(-1).cadence === "Weekly" && previews.at(-1).action === "setup",
    "Initial approval reviews exact history and automatic frequency for both subscriptions");
    assert((await test.locator("#cdPlan").innerText()).includes("Existing data is not deleted"), "History preserves existing costs");
    await test.locator("#cdCancel").click();
    assert(writes.length === 0, "Cancelled setup makes no changes");
    await test.locator("#cdReview").click();
    await test.locator("#cdStart").waitFor();
    fixture.failApproval = true;
    await test.locator("#cdStart").click();
    await test.waitForFunction(() => document.getElementById("cdNotice").textContent.includes("Temporary approval failure"));
    assert(await test.locator("#cdStart").isEnabled(), "Transient approval failure permits an explicit retry");
    await test.locator("#cdStart").click();
    await test.waitForFunction(() => document.getElementById("cdNotice").textContent.includes("Setup queued"));
    assert(writes.length === 2 && writes[0].idempotency_key === writes[1].idempotency_key
      && writes[0].preview_token === writes[1].preview_token, "Retry reuses the exact reviewed approval and idempotency key");
    assert(await test.locator("#cdReview").isDisabled(), "Queued import cannot be approved again");
    for (const sub of state.subscriptions) {
      sub.status = "waiting-for-Azure"; sub.configured = true;
      sub.config = { cadence: "Weekly", enabled: true };
    }
    await test.locator("#cdCheck").click();
    await test.waitForFunction(() => document.getElementById("cdStatus").textContent.includes("Awaiting Azure"));
    assert((await test.locator("#cdProgress").innerText()).includes("0 of 180"), "Azure acceptance is not completed FOCUS coverage");
    for (const sub of state.subscriptions) {
      sub.status = "partial"; sub.coverage.authoritative_days = 30;
    }
    await test.locator("#cdCheck").click();
    await test.waitForFunction(() => document.getElementById("cdProgress").textContent.includes("60 of 180"));
    assert((await test.locator("#cdStatus").innerText()).includes("Quick data remains available"), "Partial coverage keeps quick-data provenance honest");
    const catalogs = reads.filter(r => r.path === "/api/cost-data/subscriptions").length;
    await test.evaluate(() => {
      window.reports.stop();
      window.saved = document.createDocumentFragment();
      window.saved.append(...document.getElementById("tabBody").childNodes);
      document.getElementById("tabBody").innerHTML = "<p>Another cached tab</p>";
      window.ctx.loadToken = 2;
    });
    for (const sub of state.subscriptions) {
      sub.status = "ready"; sub.coverage.authoritative_days = 90;
      sub.last_success = "2026-09-15T11:00:00Z"; sub.through = "2026-09-15";
    }
    await test.evaluate(() => {
      document.getElementById("tabBody").replaceChildren(window.saved);
      window.ctx.loadToken = 3;
      window.reports.resume(3, "settings");
    });
    await test.locator("#cdCheck").click();
    await test.waitForFunction(() => document.getElementById("cdProgress").textContent.includes("180 of 180"));
    assert(reads.filter(r => r.path === "/api/cost-data/subscriptions").length === catalogs,
      "Cached return checks progress without reloading the accessible catalog");
    assert(await test.locator("#cdCadence-Weekly").isChecked(), "Cached return preserves the cadence draft");
    assert((await test.evaluate(() => window.events)).at(-1).status === "ready", "Only completed coverage emits a ready update");
    await test.locator("#cdTab-schedule").click();
    await test.locator("#cdRefresh").click();
    await test.locator("#cdStart").waitFor();
    assert(previews.at(-1).action === "refresh" && !("history_days" in previews.at(-1)),
      "Refresh reviews current data separately from historical setup");
    await test.locator("#cdCancel").click();
    assert(writes.length === 2 && state.subscriptions.every(s => s.config.cadence === "Weekly"),
      "Cancelled refresh preserves saved automatic updates");
    await test.setViewportSize({ width: 390, height: 844 });
    assert(await test.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Workflow fits mobile");
    const beforeHeader = reads.length;
    await test.evaluate(async () => {
      window.reports.stop();
      document.getElementById("tabBody").innerHTML = "<p>Cached overview</p>";
      window.ctx.loadToken = 4;
      window.navigation = [];
      window.addEventListener("cl:goto-tab", event => window.navigation.push(event.detail));
      window.headerResult = await window.reports.refreshLatest({ onStatus: () => {} });
    });
    assert(await test.evaluate(() => window.headerResult.status === "setup" && window.navigation.at(-1) === "settings"),
      "Header refresh routes to Cost data review instead of a legacy unreviewed write");
    assert(reads.length === beforeHeader && writes.length === 2
      && await test.locator("#tabBody").innerText() === "Cached overview", "Header routing does not mutate costs or cached markup");
    assert(errors.length === 0 && unexpected.length === 0, [...errors, ...unexpected].join("\n"));
    return { passed, mockedApprovalAttempts: writes.length, previews: previews.length, liveAzureWrites: 0, errors };
  } finally {
    await test.evaluate(() => window.reports.stop());
    await fixture.context.close();
  }
}
