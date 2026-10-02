async (page, { createCostDataFixture }) => {
  const fixture = await createCostDataFixture(page, { configured: true, scope: ["sub-0"] });
  const { test, state, reads, previews, writes, errors, unexpected } = fixture;
  let passed = 0;
  const assert = (ok, message) => { if (!ok) throw new Error(message); passed++; };
  const review = async () => {
    await test.locator("#cdChange").click();
    await test.locator("#cdStart").waitFor();
  };
  try {
    assert(writes.length === 0 && previews.length === 0, "Opening Cost data makes no changes");
    assert(reads.length === 2 && !reads.some(r => r.path === "/api/schedules"),
      "Opening reads the catalog and selected status, not Azure export inventory");
    await test.locator("#cdTab-schedule").click();
    assert(await test.locator("#cdFrequency input").count() === 3, "Three automatic update cadences");
    assert(await test.locator("#cdCadence-Daily").isChecked(), "Saved daily cadence is restored");
    assert((await test.locator("#cdFrequency").innerText()).includes("Recommended"), "Daily recommendation is explicit");
    assert(await test.locator("#sCreate,#sSub,[data-edit],[data-run],[data-del]").count() === 0,
      "No obsolete schedule creation or direct Azure mutation controls");
    await test.locator("#cdCadence-Monthly").check();
    await review();
    assert(JSON.stringify(previews.at(-1)) === '{"scope":["sub-0"],"action":"schedule","cadence":"Monthly"}',
      "Schedule review binds only the selected subscription and cadence, not historical dates");
    await test.locator("#cdReviewDetails > summary").click();
    const plan = await test.locator("#cdPlan").innerText();
    assert(plan.includes("Alpha") && !plan.includes("Beta") && plan.includes("deployment-storage/focus/sub-0"),
      "Review names the exact subscription and destination");
    assert(plan.includes("no historical import starts now") && plan.includes("External and legacy exports are untouched"),
      "Review distinguishes ongoing checks from history and external exports");
    await test.locator("#cdCancel").click();
    assert(writes.length === 0 && await test.locator("#cdStart").count() === 0, "Cancel never configures a schedule");
    await review();
    await test.locator("#cdCadence-Weekly").check();
    assert(await test.locator("#cdStart").count() === 0 && writes.length === 0,
      "Changing cadence invalidates the reviewed approval");
    await review();
    const token = `review-${previews.length}`;
    await test.locator("#cdStart").click();
    await test.waitForFunction(() => document.getElementById("cdNotice").textContent.includes("queued"));
    assert(writes.length === 1 && writes[0].preview_token === token && !!writes[0].idempotency_key,
      "One explicit approval carries the reviewed token and idempotency key");
    assert(writes[0].action === "schedule" && writes[0].cadence === "Weekly" && !("start" in writes[0]),
      "Confirmed schedule does not silently import history");
    assert((await test.locator("#cdNotice").innerText()).includes("not yet enabled"),
      "Queued schedule is not reported as enabled");
    assert(await test.locator("#cdChange").isDisabled(), "In-flight setup cannot be duplicated");
    state.subscriptions[0].status = "ready";
    state.subscriptions[0].config.cadence = "Weekly";
    await test.locator("#cdCheck").click();
    await test.waitForFunction(() => document.getElementById("cdScheduleState").textContent.includes("(Weekly)"));
    fixture.blocked = true;
    await test.locator("#cdCadence-Monthly").check();
    await review();
    assert(await test.locator("#cdStart").isDisabled(), "Missing workload access prevents confirmation");
    assert((await test.locator("#cdPlan").innerText()).includes("Workload Blob read access is missing"),
      "Blocker is visible beside the approval");
    await test.locator("#cdCancel").click();
    await test.locator("#cdAdvanced > summary").click();
    await test.locator("#cdInventory > summary").click();
    fixture.inventory = Array.from({ length: 12 }, (_, index) => ({
      name: `external-${index}`, subscription_id: "sub-0", subscription: "Alpha", owned: false,
      metric: "AmortizedCost", recurrence: "Monthly", status: "Active", identity: true,
      last_status: "Completed", account: "offline", container: "focus", folder: `external/${index}`,
    }));
    await test.locator("#cdLoadExports").click();
    await test.waitForFunction(() => document.querySelectorAll("#cdExports details.sched").length === 12);
    assert(await test.locator("#cdExports details[open]").count() === 0, "Export inventory starts collapsed");
    assert(JSON.stringify(await test.locator("#cdExports summary > strong").allTextContents())
      === JSON.stringify(fixture.inventory.map(s => s.name)), "Every external export is reachable");
    assert(await test.locator("#cdExports [data-edit],#cdExports [data-run],#cdExports [data-del]").count() === 0,
      "Inventory is read only, including for administrators");
    assert(reads.at(-1).scope.join(",") === "sub-0" && writes.length === 1, "Inventory respects scope and makes no writes");
    fixture.inventory = [];
    await test.locator("#cdLoadExports").click();
    await test.waitForFunction(() => document.getElementById("cdExports").textContent.includes("No exports found"));
    state.can_review = false;
    await test.evaluate(() => window.reports.renderSettings(1));
    assert(await test.locator("#cdChange").isDisabled() && await test.locator("#cdReview").isDisabled(),
      "Read-only users can inspect but cannot approve changes");
    await test.locator("#cdSubsToggle").click();
    await test.locator("#cdNone").click();
    await test.waitForFunction(() => document.getElementById("cdProgress").textContent.includes("Choose at least one"));
    await test.keyboard.press("Escape");
    assert(await test.locator("#cdChange").isDisabled() && await test.locator("#cdLoadExports").isDisabled(),
      "Empty selection never expands to all subscriptions");
    assert(errors.length === 0 && unexpected.length === 0, [...errors, ...unexpected].join("\n"));
    return { passed, mockedApprovals: writes.length, previews: previews.length, liveAzureWrites: 0, errors };
  } finally {
    await test.evaluate(() => window.reports.stop());
    await fixture.context.close();
  }
}
