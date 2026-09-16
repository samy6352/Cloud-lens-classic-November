async (page) => {
  const origin = "http://localhost:8100";
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce",
  });
  const test = await context.newPage(), errors = [], reads = [];
  const resource = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/demo/providers/Microsoft.CognitiveServices/accounts/demo-ai";
  const sample = {
    status: "partial", generated_at: "2026-09-10T10:00:00Z",
    window: { from: "2026-09-01", to: "2026-09-10", days: 10 },
    totals: [{ currency: "USD", cost: 125.25 }, { currency: "EUR", cost: 10 }],
    notices: ["One resource has no token metrics; measured totals are partial."],
    services: [{
      id: resource, resource_id: resource, resource_name: "demo-ai", subscription_id: "11111111-1111-1111-1111-111111111111",
      service: "Azure OpenAI", subscription: "Demo subscription", models: ["GPT-4.1", "GPT-4.1 mini"],
      cost_by_currency: [{ currency: "USD", cost: 125.25 }],
      tokens: { status: "ok", value: 1500000, input: 1000000, output: 500000, source: "Azure Monitor", note: "Measured prompt plus generated tokens." },
      utilization: { status: "ok", value: 43.2, aggregation: "Average", source: "Azure Monitor", note: "Provisioned capacity utilization." },
      recommendation: "Review provisioned capacity against peak demand.",
      meters: [{ product: "Azure OpenAI", meter: "GPT-4.1 input", quantity: 1000000, unit: "Tokens", cost: 125.25 }],
    }, {
      id: "search", resource_id: "", resource_name: "demo-search", service: "AI Search", subscription: "Demo subscription",
      cost_by_currency: [{ currency: "EUR", cost: 10 }], models: [],
      tokens: { status: "not_applicable", value: null, note: "Search is not token billed." },
      utilization: { status: "unavailable", value: null, note: "No supported capacity measurement." },
      recommendation: "Review query and index capacity for this service.",
    }],
    models: [{
      resource_id: resource, resource_name: "demo-ai", service: "Azure OpenAI", model: "gpt-4.1", deployment: "production",
      version: "2025-04-14", cost: null, cost_note: "Billing is resource-level; this deployment has no exact cost allocation.",
      tokens: { status: "ok", value: 1500000, input: 1000000, output: 500000, note: "Deployment-filtered tokens." },
      utilization: { status: "ok", value: 43.2, aggregation: "Average", note: "PTU utilization." },
      recommendation: "Review output limits against response quality.",
    }],
  };
  const flag = (metric, direction, actual, baseline, unit) => ({
    metric, direction, actual, baseline, delta: actual-baseline,
    percent: (actual-baseline)/baseline*100, unit, currency: metric==="cost"?"USD":null,
    day:"2026-09-07",baseline_from:"2026-08-24",baseline_to:"2026-09-06",baseline_samples:10,
    source:metric==="cost"?"Warehouse":"Azure Monitor",severity:"high",
    note:"Compared with complete daily observations; missing days are not zero.",
  });
  sample.services[0].anomalies=[flag("cost","up",80,20,"USD")];
  sample.services[0].anomaly_status="checked";
  sample.services[1].anomalies=[flag("cost","down",10,40,"EUR")];
  sample.services[1].anomalies[0].currency="EUR";
  sample.services[1].anomaly_status="checked";
  sample.services.push({...sample.services[1],id:"steady",resource_name:"steady-search",
    cost_by_currency:[{currency:"USD",cost:0}],anomalies:[],anomaly_status:"insufficient_history"});
  sample.models[0].anomalies=[flag("tokens","up",300000,50000,"tokens")];
  sample.models[0].anomaly_status="checked";
  let passed = 0;
  const check = (ok, detail) => { if (!ok) throw Error(detail); passed++; };
  test.on("pageerror", error => errors.push(error.message));
  try {
    await context.route("**/api/dashboard/ai-billing?**", route => {
      if (route.request().method() !== "GET") throw Error("No writes permitted");
      reads.push(route.request().url());
      return route.fulfill({ json: sample });
    });
    await test.goto(origin);
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true", null, { timeout: 60000 });
    await test.locator('[data-area="ai-billing"]').click();
    await test.locator("#aiResults").waitFor();
    check(await test.locator('[data-main-section="overview"]').getAttribute("aria-selected") === "true",
      "AI Billing belongs to Overview");
    check(await test.locator('[data-area="ai-billing"]').getAttribute("aria-pressed") === "true",
      "AI Billing is selected in Overview's area strip");
    check((await test.locator("#tabBody").innerText()).replaceAll(",", "").includes("1500000"), "measured tokens rendered");
    check((await test.locator("#tabBody").innerText()).includes("43.2%"), "real utilization rendered");
    check((await test.locator("#aiResults").innerText()).includes("Not applicable"), "non-token service not zero");
    check((await test.locator("#tabBody .kpis").innerText()).includes("USD") &&
      (await test.locator("#tabBody .kpis").innerText()).includes("EUR"), "different currencies never summed");
    check(await test.locator('#aiResults [data-ai-anomaly="up"]').count()===1 &&
      await test.locator('#aiResults [data-ai-anomaly="down"]').count()===1,"spikes and drops both highlighted");
    check((await test.locator("#aiAnomalySummary").innerText()).includes("2 AI resources flagged"),
      "anomaly summary counts affected resources");
    check(await test.locator('#aiResults tr:has([data-ai-anomaly="up"])').evaluate(row=>
      getComputedStyle(row).backgroundColor!==getComputedStyle(row.parentElement).backgroundColor),
      "flagged rows have visible highlighting");
    await test.locator("#aiOnlyAnomalies").check();
    check((await test.locator("#aiResultCount").innerText()).includes("2 of 3"),"anomaly filter excludes unassessed rows");
    await test.locator("#aiOnlyAnomalies").uncheck();
    await test.locator("#aiResults .ai-anomaly-details summary").first().click();
    check((await test.locator("#aiResults .ai-anomaly-details").first().innerText()).includes("10 baseline days"),
      "flag explanation shows its baseline evidence");
    await test.locator("#aiBillingSearch").fill("Search");
    await test.locator("#aiResults").scrollIntoViewIfNeeded();
    check((await test.locator("#aiResults").innerText()).includes("demo-search") &&
      !(await test.locator("#aiResults").innerText()).includes("demo-ai"), "search narrows loaded service rows");
    await test.locator("#aiBillingSearch").fill("");
    await test.locator('[data-ai-mode="models"]').click();
    await test.locator("#aiResults").scrollIntoViewIfNeeded();
    check((await test.locator("#aiResults").innerText()).includes("gpt-4.1"), "model metadata rendered");
    check((await test.locator("#aiResults").innerText()).includes("Not allocated"), "no guessed model cost");
    await test.locator("[data-ai-resource]").first().click();
    check(await test.locator('[data-ai-mode="services"]').getAttribute("aria-pressed") === "true",
      "model cost link opens matching resource billing");
    await test.locator("#aiBillingSearch").fill("");
    await test.locator('[data-ai-mode="models"]').click();
    check(reads.length === 1, "switching table and searching reuse loaded data");
    await test.locator('[data-area="overview"]').click();
    await test.locator('[data-area="ai-billing"]').click();
    check(reads.length === 1, "returning to cached AI view does not refetch metrics");
    check(await test.locator('[data-ai-mode="models"]').getAttribute("aria-pressed") === "true", "model selection retained");
    for (const width of [1440, 800, 390, 320]) {
      await test.setViewportSize({ width, height: 900 });
      check(await test.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        `page fits ${width}px with table scrolling contained`);
    }
    await test.setViewportSize({ width: 1440, height: 1000 });
    await test.locator('[data-ai-mode="services"]').click();
    await test.locator("#tabBody").screenshot({
      path: "C:\\Users\\raviverma\\.scout\\browser-output\\853b0ecb-79b5-41e0-b147-44cc4fa82ff4\\ai-billing-preview.png",
      animations: "disabled",
    });
    check(errors.length === 0, errors.join("; "));
    return { passed, apiReads: reads.length, errors, liveWrites: 0 };
  } catch (error) {
    return { failed: error.message, passed, reads, errors,
      text: (await test.locator("#tabBody").innerText()).slice(0,1200),
      hidden: await test.locator("#aiResults").evaluate(e => [...e.querySelectorAll("[hidden]")].map(n=>({tag:n.tagName,id:n.id,class:n.className}))),
      html: (await test.locator("#aiResults").innerHTML()).slice(0,1800) };
  } finally {
    await context.close();
  }
}
