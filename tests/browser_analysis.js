async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before the analysis checks.");
  const response = await page.request.get(origin + "/");
  if (!response.ok()) throw new Error("CloudLens HTML is unavailable.");
  const markup = (await response.text()).replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "");
  const context = await page.context().browser().newContext({
    viewport: { width: 1440, height: 900 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  test.setDefaultTimeout(10000);
  const failures = [], errors = [], layouts = [];
  let requests = 0, failRequest = false;
  const check = (name, passed) => { if (!passed) failures.push(name); };
  const findings = Array.from({ length: 8 }, (_, index) => ({
    title: index === 7 ? 'Review <untrusted> & unusually-long-resource-identifiers-with-no-spaces-in-the-original-resource-name'
      : ["Review idle virtual machines", "Investigate the spend increase", "Review budget coverage",
        "Check unused public IPs", "Consider commitment options", "Review tag coverage", "Check SQL licensing"][index],
    kind: index === 0 ? "rightsizing" : "waste", tab: "savings", source: "Savings",
    detail: `Detailed recommendation ${index + 1}.`,
    because: `Evidence-based reasoning ${index + 1}.`, risk: "Review workload requirements before acting.",
    steps: ["Confirm ownership", "Review the supporting evidence"], caveat: "Illustrative; validate commercial terms.",
    basis: "Source costs over the selected reporting period.", impact: index === 3 ? 0 : 40,
    impact_period: index === 4 ? "year" : "window", currency: "USD", confidence: "medium",
    severity: index === 2 ? "critical" : "high", effort: "Review required",
    evidence: [{ name: "vm-demo", cost: 40, detail: "Read-only evidence" }],
  }));
  let data = {
    findings, days: 30, currency: "USD", addressable: 120, subscriptions: 2,
    generated_at: "2026-09-08T10:00:00Z", method: "Measured source evidence with explicit assumptions.",
    unavailable: { governance: "Source unavailable" },
    summary: {
      headline: "Start with the highest-confidence opportunities", situation: "Detailed portfolio context.",
      priority: "Validate before changing a workload.",
      sequence: [{ title: "Review idle VMs", why: "Confirm workload requirements first", impact: 40 }],
    },
  };
  test.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const url = { pathname: route.request().url().split("?")[0].replace(/^https?:\/\/[^/]+/, "") };
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: markup.replace("</body>", `
      <script type="module">
        import * as analysis from "/assets/analysis.js";
        window.testAnalysis = analysis;
        analysis.setAnalysisContext(() => ({days:30,scope:["scope-a"],currency:"USD"}));
        window.addEventListener("cl:ask", event => {window.lastQuestion = event.detail;});
        window.addEventListener("cl:goto-tab", event => {window.lastTab = event.detail;});
        analysis.wireAnalysis();
        analysis.openAnalysis();
      </script></body>`) });
    if (url.pathname === "/api/insights") {
      requests++;
      return route.fulfill(failRequest
        ? { status: 503, json: { detail: "Analysis source temporarily unavailable" } }
        : { json: data });
    }
    if (url.pathname === "/assets/app.js") return route.fulfill({
      contentType: "text/javascript",
      body: `export const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
        export const money=(value,currency="USD")=>currency+" "+Number(value).toFixed(2);`,
    });
    if (url.pathname === "/assets/bot.js") return route.fulfill({
      contentType: "text/javascript", body: "export const setBotCount=()=>{},setBotState=()=>{};",
    });
    if (url.pathname.startsWith("/assets/")) {
      const asset = await page.request.get(origin + url.pathname);
      return route.fulfill({ response: asset });
    }
    return route.fulfill({ status: 404, body: "Unexpected test request" });
  });
  try {
    await test.goto(origin);
    await test.locator(".an-card-summary").first().waitFor();
    check("all recommendations start collapsed", await test.locator(".an-card[open]").count() === 0);
    check("collapsed rows show titles, not details or figures",
      (await test.locator(".an-card").first().innerText()).replace(/\s+/g, " ").trim() === "01 Review idle virtual machines");
    check("overview and priorities are available only on demand", !await test.locator(".an-overview").evaluate(node => node.open));
    check("missing sources remain visible", await test.locator(".an-gap").isVisible()
      && (await test.locator(".an-gap").textContent()).includes("governance"));
    const initialRequests = requests;
    const first = test.locator(".an-card").nth(0), second = test.locator(".an-card").nth(1);
    await first.locator(":scope > summary").focus();
    await test.keyboard.press("Enter");
    check("keyboard opens the recommendation", await first.locator(".an-main").isVisible());
    check("impact, reasoning, precautions and steps remain intact",
      (await first.innerText()).includes("USD 40.00") && (await first.innerText()).includes("Evidence-based reasoning")
      && (await first.innerText()).includes("Review workload requirements") && (await first.innerText()).includes("Confirm ownership"));
    await first.locator(".an-how > summary").click();
    check("calculation evidence remains available", await first.locator(".an-evidence").isVisible());
    await second.locator(":scope > summary").focus();
    await test.keyboard.press("Space");
    check("opening another recommendation closes the previous one",
      await test.locator(".an-card[open]").count() === 1 && await second.evaluate(node => node.open));
    await test.locator("#analysisClose").click();
    await test.evaluate(() => window.testAnalysis.openAnalysis());
    check("reopening preserves the selected recommendation",
      await test.locator(".an-card").nth(1).evaluate(node => node.open));
    await test.locator(".an-card").nth(1).getByRole("button", { name: "Ask about this" }).click();
    check("follow-up opens the existing conversation in place", await test.locator("#anConversation").isVisible()
      && await test.evaluate(() => window.lastQuestion?.inPlace === true));
    await test.locator("#anBack").click();
    check("return from conversation preserves the expanded recommendation",
      await test.locator(".an-card").nth(1).evaluate(node => node.open));
    await test.locator("#anAsk").fill("Which recommendation should I review first?");
    check("the unified composer provides a focus indicator", await test.locator("#anAskForm").evaluate(form =>
      getComputedStyle(form).boxShadow !== "none"));
    await test.keyboard.press("Enter");
    check("the composer submits through the existing conversation", await test.evaluate(() =>
      window.lastQuestion?.q === "Which recommendation should I review first?" && window.lastQuestion.inPlace)
      && await test.locator("#anConversation").isVisible());
    await test.locator("#anBack").click();
    check("expanding, navigating and returning make no analysis requests", requests === initialRequests);
    await test.locator(".an-card").nth(3).locator(":scope > summary").click();
    check("no savings claim is not shown as zero savings",
      (await test.locator(".an-card").nth(3).innerText()).includes("No saving claimed"));
    await test.locator(".an-card").nth(4).locator(":scope > summary").click();
    check("annual and reporting-window impacts keep their units",
      (await test.locator(".an-card").nth(4).innerText()).includes("per year"));
    await test.locator(".an-card").nth(4).locator(":scope > summary").click();
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const width of [1699, 1440, 800, 390, 320]) {
        await test.setViewportSize({ width, height: 900 });
        await test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const layout = await test.locator("#analysisBody").evaluate(body => {
          const cards = [...body.querySelectorAll(".an-card")];
          return {
            width: innerWidth, maxRow: Math.max(...cards.map(card => card.getBoundingClientRect().height)),
            pageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
            bodyOverflow: body.scrollWidth > body.clientWidth + 1,
            fullWidth: body.querySelector(".an-list").getBoundingClientRect().width / body.clientWidth,
            allVisible: cards.at(-1).getBoundingClientRect().bottom <= body.getBoundingClientRect().bottom,
            composerFits: document.querySelector("#anAskForm").scrollWidth <= document.querySelector("#anAskForm").clientWidth + 1,
            sendHeight: document.querySelector("#anAskSend").getBoundingClientRect().height,
            titleFont: getComputedStyle(cards.at(-1).querySelector("h3")).fontSize,
            rowPadding: getComputedStyle(cards.at(-1).querySelector("summary")).paddingBlock,
            narrow: matchMedia("(max-width: 390px)").matches,
          };
        });
        layouts.push({ theme, ...layout });
        check(`${theme}/${width}: compact rows and no horizontal overflow`,
          !layout.pageOverflow && !layout.bodyOverflow && layout.composerFits && layout.sendHeight >= 36
          && layout.maxRow <= (width >= 800 ? 76 : 120));
        if (width >= 1440) check(`${theme}/${width}: uses available width and fits eight recommendations`,
          layout.fullWidth > 0.9 && layout.allVisible);
        await test.locator(".an-card").first().locator(":scope > summary").click();
        const expanded = await test.locator(".an-card").first().evaluate(card => {
          const main = card.querySelector(".an-main");
          const reason = card.querySelector(".an-reasoning").getBoundingClientRect();
          const risk = card.querySelector(".an-block-risk").getBoundingClientRect();
          const caveat = card.querySelector(".an-caveat").getBoundingClientRect();
          const footer = card.querySelector(".an-foot").getBoundingClientRect();
          const style = getComputedStyle(main);
          const width = main.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
          return {
            fullWidth: Math.abs(reason.width - width) < 2 && Math.abs(risk.width - width) < 2,
            warningBelow: risk.top >= caveat.bottom && risk.bottom <= footer.top,
            overflow: main.scrollWidth > main.clientWidth + 1,
            detailUncapped: getComputedStyle(card.querySelector(".an-detail")).maxWidth === "none",
          };
        });
        check(`${theme}/${width}: full-width content with caution below`,
          expanded.fullWidth && expanded.warningBelow && !expanded.overflow && expanded.detailUncapped);
        await test.locator(".an-card").first().locator(":scope > summary").click();
      }
    }
    await test.locator(".an-card").last().locator(":scope > summary").click();
    check("untrusted titles stay text", await test.locator(".an-card img,.an-card untrusted").count() === 0);
    check("expanded mobile details do not overflow", await test.locator("#analysisBody").evaluate(body =>
      body.scrollWidth <= body.clientWidth + 1));
    await test.locator(".an-overview > summary").click();
    check("full overview and priorities are preserved",
      (await test.locator(".an-overview").innerText()).includes(data.summary.situation)
      && (await test.locator(".an-overview").innerText()).includes("Confirm workload requirements"));
    await test.locator("#rerun").click();
    await test.waitForFunction(() => document.querySelectorAll(".an-card").length === 8);
    check("explicit refresh resets expansion", requests === initialRequests + 1 && await test.locator(".an-card[open]").count() === 0);
    await test.locator(".an-card").first().locator(":scope > summary").click();
    await test.locator(".an-card").first().locator("[data-goto]").click();
    check("source navigation still closes analysis and selects its tab",
      await test.evaluate(() => window.lastTab === "savings" && !window.testAnalysis.isOpen()));
    await test.evaluate(() => { window.testAnalysis.invalidate(); window.testAnalysis.openAnalysis(); });
    await test.locator(".an-card-summary").first().waitFor();
    check("scope invalidation resets expanded recommendations", await test.locator(".an-card[open]").count() === 0);
    failRequest = true;
    await test.locator("#rerun").click();
    await test.locator(".an-error").waitFor();
    check("failed refresh remains explicit", (await test.locator(".an-error").innerText()).includes("temporarily unavailable"));
    failRequest = false;
    data = { ...data, findings: [], summary: { headline: "No recommendations in scope", situation: "No supported opportunities found." } };
    await test.locator("#rerun").click();
    await test.locator(".an-clear").waitFor();
    check("empty results still disclose unavailable sources", await test.locator(".an-clear .an-gap").isVisible());
    return { failures, errors, layouts, requests };
  } finally { await context.close(); }
}
