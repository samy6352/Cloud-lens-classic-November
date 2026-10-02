async (page) => {
  // Run with a CloudLens page open. Tests use a separate, fully mocked browser context.
  const sourcePage = page;
  const origin = sourcePage.url().match(/^https?:\/\/[^/]+/)[0];
  const context = await page.context().browser().newContext({
    viewport: { width: 1440, height: 1000 }, timezoneId: "Asia/Kolkata",
  });
  const test = await context.newPage();
  test.setDefaultTimeout(5000);
  const failures = [], errors = [], requests = [];
  const check = (name, ok) => { if (!ok) failures.push(name); };
  let revision = 0, forceCount = 0, failScan = false, completedScanAt = null;
  let releaseCalendar, calendarRequested;
  const calendarGate = new Promise(resolve => { releaseCalendar = resolve; });
  const calendarStarted = new Promise(resolve => { calendarRequested = resolve; });
  let statusAvailable = false, updatesUnavailable = false, releaseHealth;
  const healthGate = new Promise(resolve => { releaseHealth = resolve; });
  let billingLegacy = false, releaseBillingEstimate, budgetsUnavailable = false;
  const billingGate = new Promise(resolve => { releaseBillingEstimate = resolve; });
  let releaseRecommendation, holdBillingRefresh, holdHealthRefresh;
  const recommendationGate = new Promise(resolve => { releaseRecommendation = resolve; });
  const feed = (items, extra = {}) => ({
    status: "ok", items, count: items.length, error: null, notice: "", truncated: false,
    freshness: { fetched_at: "2026-09-05T12:00:00Z", age_seconds: 0, stale: false, cached: false, max_age_seconds: 600 },
    ...extra,
  });
  const opportunity = n => ({
    id: `opportunity-${n}`, title: `Idle virtual machine ${n}`, category: "idle", category_label: "Idle",
    resource_id: `/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Compute/virtualMachines/vm-${n}`,
    resource: `vm-${n}`, resource_group: "demo", region: "eastus", sources: [
      { label: "CPU metrics", basis: "billed", claim: "Below 5% CPU" },
    ], options: [], count: 1, confidence: "high", currency: "USD", window: 12 + revision,
    why: "Check whether this machine can be resized.", action: "Review the workload.",
  });
  const appStub = `
    export const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    export const money = (v,c='USD') => c + ' ' + Number(v || 0).toFixed(2);
    export const moneyHtml = money;
    export const get = async (url, options={}) => {
      const r = await fetch(url,{signal:options.signal});
      if (!r.ok) throw new Error('Test refresh failure');
      return r.json();
    };
    export const freshReads = async fn => fn();
    export const post = async () => ({currency:'USD',days:30,billed:{annual:100},current_retail:{annual:100},target:{annual:40},annual_saving:60,target_size:'Standard_B2s',note:'Fixture comparison'});
    export const allSubscriptions = () => [{id:'11111111-1111-4111-8111-111111111111',name:'Demo'},
      {id:'22222222-2222-4222-8222-222222222222',name:'Second subscription'}];
    export const scopeIds = () => window.testScope || ['11111111-1111-4111-8111-111111111111'];
    export const selectSubscriptions = ids => { window.testScope = [...ids]; return true; };
    export const whoAmI = async () => ({user:{admin:window.testAdmin !== false},authenticated:true});
    export const onScopeChange = fn => window.addEventListener('cl:test-refresh',fn);
    export const askAgent = () => {};
    export const drawChart = async () => null;
    export const refreshHeader = () => {};
    export const setGlanceSpark = () => {};
  `;
  const stubs = {
    "app.js": appStub,
    "analysis.js": "export const close=()=>{},invalidate=()=>{},isOpen=()=>false,openAnalysis=()=>{},setAnalysisContext=()=>{},wireAnalysis=()=>{};",
    "bot.js": "export const mountBot=()=>{},setBotState=()=>{};",
    "filters.js": "export const activeFilterCount=()=>0,applyFilter=()=>{},clearAllFilters=()=>{},filterHasValue=()=>false,filterQuery=()=>window.testFilters||[],loadFilters=()=>{},setFilterCurrency=()=>{},wireFilters=()=>{};",
  };
  test.on("pageerror", e => errors.push(e.message));
  await context.route("**/*", async route => {
    const raw = route.request().url();
    const query = raw.split("?")[1] || "";
    const url = { pathname: raw.split("?")[0].replace(/^https?:\/\/[^/]+/, ""), search: query ? `?${query}` : "",
      searchParams: { get: name => query.split("&").find(p => p.startsWith(`${name}=`))?.split("=")[1],
        has: name => query.split("&").some(p => p.startsWith(`${name}=`)) } };
    requests.push(url.pathname + url.search);
    if (url.pathname.startsWith("/assets/")) {
      const name = url.pathname.split("/").pop();
      if (name === "calendarheat.js") {
        calendarRequested();
        await calendarGate;
      }
      const body = stubs[name] ?? await (await page.request.get(origin + url.pathname)).body();
      return route.fulfill({ body, contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "image/svg+xml" });
    }
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: `<!doctype html>
      <html><head><link rel="stylesheet" href="/assets/app.css">
      <script type="module" src="/assets/dashboard.js"></script></head><body>
      <header class="bar"><button id="brand">CloudLens</button><div class="bar-right"><div id="period"><button data-days="7">7d</button><button data-days="30">30d</button></div>
      <select id="currency"></select><div id="refresh"><button id="refreshBtn"><span id="refreshLabel"></span></button><div id="refreshMenu" hidden></div></div>
      <div id="exportWrap"><button id="exportBtn">Export</button><div id="exportMenu" hidden></div></div>
      <button id="askToggle"></button></div></header>
      <aside id="side" class="side"><button id="sideClose"></button><input id="q"></aside><div id="chatFab"></div>
      <div class="shell" id="shell"><nav id="tabs" class="rail"></nav><main class="board">
      <div class="board-summary"><input type="month" id="reportingMonth"><span id="periodRange"></span>
      <div id="statusRow" class="statusrow"><div class="status-meta"><div id="statusActions" class="view-controls"></div>
      <div id="dataState" class="datastate"><span>Local data <strong>10k rows</strong></span></div></div></div>
      <div id="viewActions" class="view-actions"></div></div>
      <div id="tabBody" class="board-body" role="tabpanel" tabindex="-1"></div></main></div></body></html>` });
    let data = {};
    if (url.pathname === "/api/reporting-period") data = { from: "2026-08-08", to: "2026-09-06", days: 30 };
    else if (url.pathname === "/api/currency") data = { supported: ["USD", "INR"] };
    else if (url.pathname.endsWith("/sections")) data = {
      total: 100, currency: "USD", aks: { cost: 1, currency: "USD" },
      sections: [{ id: "compute", label: "Compute & Web", cost: 50, currency: "USD" },
        { id: "networking", label: "Networking", cost: 50, currency: "USD" }],
    };
    else if (url.pathname.endsWith("/executive")) data = {
      currency: "USD", days: 7,
      kpis: { total: 20, previous: 10, daily_avg: 5, resources: 2, services: 1, regions: 1, subscriptions: 1 },
      areas: [], services: [], subscriptions: [], regions: [], resource_groups: [], movers: [],
      trend: { labels: ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
        values: [1, 2, 0, 17], previous: [0, 0, 0, 0] },
    };
    else if (/\/dashboard\/(compute|networking|aks)$/.test(url.pathname)) data = { empty: true };
    else if (url.pathname.endsWith("/savings/progress")) {
      if (url.searchParams.has("refresh")) { forceCount++; revision = 0; completedScanAt = null; }
      if (failScan) return route.fulfill({ status: 503, body: "unavailable" });
      revision++;
      data = {
        opportunities: Array.from({ length: revision === 1 ? 12 : 13 }, (_, i) => opportunity(i)),
        count: revision === 1 ? 12 : 13, days: Number(url.searchParams.get("days")),
        billed_total: 100 + revision, projected_total: 0, total: 100,
        categories: [{ id: "idle", label: "Idle", count: 13, total: 100 }], currency: "USD",
        scan: { status: revision < 4 ? "running" : "complete", revision,
          completed: ["waste", "rightsizing", "shutdown", "advisor"].slice(0, revision), total: 4, errors: {} },
        freshness: { cached: revision > 4, stale: revision < 4, refreshing: revision < 4,
          generated_at: revision >= 4 ? (completedScanAt ||= new Date().toISOString()) : null, age_seconds: 0, max_age_seconds: 60 },
      };
    } else if (url.pathname.endsWith("/recommendations")) {
      await recommendationGate;
      data = { narrative: "Start with the idle VM." };
    }
    else if (url.pathname.endsWith("/resources")) data = {
      count: 90, currency: "USD", days: Number(url.searchParams.get("days")), total_cost: 1000,
      resource_groups: 1, regions: 1, services: 1, subscriptions: 1,
      resources: Array.from({ length: 90 }, (_, n) => ({
        id: `/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Compute/virtualMachines/resource-${String(n).padStart(2, "0")}`,
        name: `resource-${String(n).padStart(2, "0")}`, cost: n, currency: "USD",
        delta: n, change_pct: null, subscription: "Demo", region: "eastus",
        tags: [{ key: "owner", value: "Demo owner" }],
      })),
    };
    else if (url.pathname.endsWith("/governance")) data = {};
    else if (url.pathname.endsWith("/budgets")) data = budgetsUnavailable
      ? { budgets: [], count: 0, partial: true, error: "Could not read budgets for the selected subscriptions." }
      : {
        budgets: [
          { name: "Production budget", status: "over", amount: 100, current_spend: 125, remaining: -25, percent_used: 125,
            currency: "USD", subscription: "Demo", subscription_id: "11111111-1111-4111-8111-111111111111",
            time_grain: "Monthly", start: "2026-09-01", end: "2027-09-01", alerts: 2, alerting: [80, 100],
            filter: "env = prod", tag_filter: [["env", ["prod"]]], filter_exact: true,
            trend: { labels: ["2026-09-01", "2026-09-02"], values: [60, 65], cumulative: [60, 125],
              total: 125, filtered_exactly: true } },
          { name: "Development budget", status: "near", amount: 100, current_spend: 85, remaining: 15, percent_used: 85,
            currency: "USD", subscription: "Demo", subscription_id: "11111111-1111-4111-8111-111111111111",
            time_grain: "Monthly", start: "2026-09-01", end: "2027-09-01", alerts: 1, alerting: [80],
            filter: "env AND resource group", tag_filter: [], filter_exact: false, trend: null,
            trend_note: "The budget's full filter cannot be reproduced. No approximate trend is shown." },
          { name: "Storage budget", status: "ok", amount: 100, current_spend: 20, remaining: 80, percent_used: 20,
            currency: "USD", subscription: "Demo", subscription_id: "11111111-1111-4111-8111-111111111111",
            time_grain: "Monthly", start: "2026-09-01", end: "2027-09-01", alerts: 0, alerting: [] },
          { name: "Pending spend", status: "unknown", amount: 200, current_spend: null, remaining: null, percent_used: null,
            currency: "EUR", subscription: "Demo", subscription_id: "11111111-1111-4111-8111-111111111111",
            time_grain: "Monthly", start: "2026-09-01", end: "2027-09-01", alerts: 0, alerting: [] },
        ],
        count: 4, partial: true, subscriptions_requested: 2, subscriptions_read: 1,
        unavailable_subscriptions: [{ id: "22222222-2222-4222-8222-222222222222", name: "Second subscription" }],
        error: "Could not read budgets for Second subscription; available results are incomplete.",
      };
    else if (url.pathname.endsWith("/tags")) {
      const rid = name => `/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Compute/virtualMachines/${name}`;
      data = {
        keys: [{ key: "env", cost: 150, resources: 3, values: 3 }, { key: "owner", cost: 100, resources: 1, values: 1 }],
        values: ["prod", "core", "dev", ""], currency: "USD", mixed: false, days: 30, total: 175,
        native_currencies: { "11111111-1111-4111-8111-111111111111": ["USD"] },
        tagged: { cost: 150, resources: 3 }, untagged: { cost: 25, resources: 1 },
        resources: [
          { id: rid("prod-vm"), name: "prod-vm", service: "Virtual Machines", subscription: "Demo", group: "demo", cost: 100, keys: ["env", "owner"], v: [0, 1], d: [[0, 50], [1, 50]] },
          { id: rid("dev-disk"), name: "dev-disk", service: "Storage", subscription: "Demo", group: "demo", cost: 50, keys: ["env"], v: [2], d: [[0, 25], [1, 25]] },
          { id: rid("untagged-ip"), name: "untagged-ip", service: "Networking", subscription: "Demo", group: "demo", cost: 25, keys: [], v: [], d: [[0, 10], [1, 15]] },
          { id: rid("empty-env"), name: "empty-env", service: "Virtual Machines", subscription: "Demo", group: "demo", cost: 0, keys: ["env"], v: [3], d: [] },
        ].map(resource => ({ ...resource, subscription_id: "11111111-1111-4111-8111-111111111111" })),
        truncated: false, tags_not_loaded: false, dates: ["2026-09-01", "2026-09-02"], daily_total: [85, 90],
        daily_dropped: false, latest: "2026-09-02", uncosted: [{ key: "new-tag", resources: 1 }],
      };
    }
    else if (url.pathname.endsWith("/esu")) data = { machines: [], currency: "USD" };
    else if (url.pathname.endsWith("/hybrid-benefit")) data = {};
    else if (url.pathname.endsWith("/azure-updates")) data = updatesUnavailable
      ? feed([], { status: "error", error: "Announcements feed unavailable" })
      : feed(Array.from({ length: 25 }, (_, index) => ({
        id: `notice-${index}`, title: `Azure update ${index}`,
        summary: "<img src=x onerror=alert(1)> Plain text announcement",
        category: index % 5 === 0 ? "Retirement" : "Generally available",
        published_at: "2026-09-05T12:00:00Z", services: ["Virtual machines"], regions: [],
        url: index === 0 ? "javascript:alert(1)" : `https://azure.microsoft.com/en-us/updates/notice-${index}`,
      })), { total: 30, truncated: true });
    else if (url.pathname.endsWith("/azure-status")) data = statusAvailable
      ? feed([]) : feed([], { status: "error", error: "Public status temporarily unavailable" });
    else if (url.pathname.endsWith("/service-health")) {
      await healthGate;
      if (holdHealthRefresh) await holdHealthRefresh;
      data = feed([{ id: "maintenance", title: "Planned database maintenance", category: "PlannedMaintenance",
        summary: "Review the maintenance window", status: "Active", services: ["SQL Database"], regions: ["West Europe"],
        published_at: "2026-09-05T12:00:00Z", subscription_ids: ["11111111-1111-4111-8111-111111111111"],
        resources: [{ id: "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Sql/servers/demo", name: "demo" }],
      }]);
    }
    else if (url.pathname.endsWith("/billing/opportunities")) {
      await billingGate;
      if (holdBillingRefresh) await holdBillingRefresh;
      data = {
        status: "partial",
        potential: { monthly: 20, annual: 240, currency: "USD", partial: true,
          overlap_note: "Reservation and Savings Plan alternatives are not additive." },
        opportunities: [
          { id: "resize", title: "Idle VM", action: "Right-size resources", monthly_savings: 8,
            annual_savings: 96, currency: "USD", included_in_total: true, related_tab: "savings" },
          { id: "ri", title: "Stable compute workload", action: "Buy Reservation", monthly_savings: 12,
            annual_savings: 144, currency: "USD", included_in_total: true, related_tab: "commitments" },
          { id: "sp", title: "Alternative compute offer", action: "Increase Savings Plan coverage",
            monthly_savings: 11, annual_savings: 132, currency: "USD", included_in_total: false,
            exclusion_reason: "Alternative to the reservation; not additive.", related_tab: "commitments" },
        ],
        commitments: {
          items: [{ id: "benefit-one", name: "Compute reservation", kind: "reservation",
            utilization_pct: 62, utilization_period: { days: 7, basis: "Provider rolling aggregate" },
            expires_on: "2026-10-21", days_to_expiry: 45, state: "Active", monthly_unused_cost: null,
            currency: "USD", source: "Azure metadata" }],
          unavailable: [{ source: "Savings Plans", reason: "Lifecycle read permission unavailable" }],
        },
        freshness: { checked_at: "2026-09-06T00:00:00Z", window_days: 30 },
        warnings: ["Some lifecycle metadata is not readable."],
      };
    }
    else if (url.pathname.endsWith("/billing")) data = {
      period: { month: "2026-08", label: "August 2026", start: "2026-08-01", end: "2026-09-01", previous_month: "2026-07", days: 31 },
      currency: "USD", spend_basis: billingLegacy
        ? { code: "recorded", metric: "recorded_cost", label: "Recorded spend" }
        : { code: "billed", metric: "billed_cost", label: "Total billed cost" },
      kpis: { recorded_cost: 125, billed_cost: billingLegacy ? null : 100,
        list_cost: billingLegacy ? null : 150, contracted_cost: billingLegacy ? null : 135,
        effective_cost: billingLegacy ? null : 110, azure_discounts: billingLegacy ? null : 15,
        reservation_savings: billingLegacy ? null : 10, savings_plan_savings: billingLegacy ? null : 15,
        other_commitment_savings: 0, non_commitment_savings: 0,
        actual_savings: billingLegacy ? null : 40, effective_discount_pct: billingLegacy ? null : 26.6667 },
      pricing: { status: billingLegacy ? "unavailable" : "complete", note: billingLegacy ? "Pricing fields were not retained in this legacy import." : "" },
      coverage: { current: { rows: 31, days_present: 31, calendar_days: 31 },
        previous: { rows: 31, days_present: 31, calendar_days: 31 } },
      month_over_month: { current: billingLegacy ? 125 : 100, previous: billingLegacy ? 100 : 80,
        change_pct: billingLegacy ? null : 25, comparable: !billingLegacy },
      trend: { labels: ["2026-07", "2026-08"], values: billingLegacy ? [100, 125] : [80, 100] },
      breakdowns: {
        subscriptions: [{ id: "11111111-1111-4111-8111-111111111111", name: "Demo", cost: billingLegacy ? 125 : 100 }],
        services: [{ id: "compute", name: "Virtual Machines", cost: billingLegacy ? 125 : 100 }],
        resources: [{ id: "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.Compute/virtualMachines/vm", name: "VM", resource_group: "demo", cost: billingLegacy ? 125 : 100 }],
      },
      definitions: [{ label: "Measured savings", explanation: "Comparable usage, not the cash invoice difference." }],
      warnings: [], per_currency: [],
    };
    else if (url.pathname.endsWith("/anomalies")) data = { anomalies: [], currency: "USD" };
    else if (url.pathname.endsWith("/commitments")) data = { coverage: { empty: true } };
    else if (url.pathname === "/api/report/options") data = { areas: [], blocks: [], live: [], formats: [], currency: "USD" };
    else if (url.pathname === "/api/exports") data = { exports: [] };
    else if (url.pathname === "/api/archive") data = { enabled: false };
    return route.fulfill({ json: data });
  });
  const goto = async name => {
    await test.evaluate(tab => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: tab })), name);
  };
  try {
    await test.goto("http://dashboard.test/");
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true", null, { timeout: 5000 });
    check("initial navigation loads only Spend", !requests.some(x => /dashboard-(optimize|monitor|reports)\.js/.test(x)));
    check("tab actions link only the selected Azure subscription", await test.locator("#statusActions .view-portal-menu a").count() === 1);
    check("expanded navigation tooltips match their accessible labels", await test.locator("#tabs [data-main-section]")
      .evaluateAll(buttons => buttons.every(button => (button.dataset.tooltip || button.title) === button.getAttribute("aria-label"))));
    check("seven primary sections exclude Resiliency", (await test.locator("#tabs [data-main-section]").allTextContents())
      .map(value => value.trim()).join("|") === "Overview|Billing|All resources|Savings|Monitor|Governance|Reports");
    check("left tabs retain accessible labels", await test.locator('#tabs [data-main-section="overview"]').getAttribute("aria-label") === "Overview");
    await test.locator("#railMini").click();
    check("icon-only navigation retains discoverable labels", await test.locator("#tabs [data-main-section]")
      .evaluateAll(buttons => buttons.every(button => (button.dataset.tooltip || button.title) === button.getAttribute("aria-label"))));
    check("icon-only navigation keeps accessible control names", await test.locator("#railMini").getAttribute("aria-label") === "Show labels"
      && await test.locator('#tabs [data-main-section="billing"]').getAttribute("aria-label") === "Billing");
    await test.locator("#railMini").click();
    const headerHeight = await test.locator("#viewActions").evaluate(el => el.getBoundingClientRect().height);
    check(`desktop title/action strip is compact (${headerHeight}px)`, headerHeight <= 56);
    check("header has no extra branding or description rows", await test.locator("#viewActions .view-kicker, #viewActions .view-hint").count() === 0);
    const actionPosition = await test.evaluate(() => {
      const actions = document.getElementById("statusActions").getBoundingClientRect();
      const local = document.getElementById("dataState").getBoundingClientRect();
      return { left: actions.right <= local.left, gap: local.left - actions.right,
        aligned: Math.abs((actions.top + actions.bottom - local.top - local.bottom) / 2) < 2 };
    });
    check("actions sit immediately left of Local data", actionPosition.left && actionPosition.aligned && actionPosition.gap <= 12);
    check("subsection heading no longer contains action buttons", await test.locator("#viewActions .view-controls, #viewActions .view-portal").count() === 0);
    await test.locator("#statusActions [data-related-tab='resources']").click();
    await test.locator("#tabBody [data-table]").waitFor();
    check("moved All resources control opens the resource view",
      await test.locator('#tabs [data-main-section="resources"]').getAttribute("aria-selected") === "true"
      && await test.locator("#viewActions [data-subsection]").count() === 0);
    await goto("overview");
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    await test.locator("#statusActions .view-portal > summary").focus();
    await test.keyboard.press("Enter");
    check("status row retains accessible Azure actions", await test.locator("#statusActions .view-portal-menu a").isVisible());
    await test.keyboard.press("Escape");
    check("compact Azure action menu closes with Escape", !await test.locator("#statusActions .view-portal").evaluate(el => el.open));
    await test.setViewportSize({ width: 390, height: 844 });
    const mobileHeader = await test.locator("#viewActions").evaluate(el => ({
      height: el.getBoundingClientRect().height,
      navigationHeight: el.querySelector(".subsection-nav").getBoundingClientRect().height,
      controlsFit: [...el.querySelectorAll(".view-title h2,.view-controls button,.view-controls summary")]
        .every(control => control.getBoundingClientRect().right <= window.innerWidth),
    }));
    check(`mobile title/actions stay compact above subtabs (${mobileHeader.height}px)`,
      mobileHeader.height - mobileHeader.navigationHeight <= 100 && mobileHeader.controlsFit);
    const statusBounds = await test.locator("#statusRow").evaluate(el => ({
      fits: [...el.querySelectorAll("button,summary,#dataState")].every(control => {
        const r = control.getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth;
      }),
      visible: getComputedStyle(el).display !== "none",
    }));
    check("moved actions and Local data fit on mobile", statusBounds.fits && statusBounds.visible);
    await test.locator("#statusActions .view-portal > summary").click();
    check("moved Azure dropdown stays in the mobile viewport", await test.locator("#statusActions .view-portal-menu").evaluate(el => {
      const r = el.getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth;
    }));
    await test.keyboard.press("Escape");
    await test.evaluate(() => { document.getElementById("dataState").hidden = true; });
    check("actions remain available without Local data", await test.locator("#statusActions").isVisible());
    await test.evaluate(() => { document.getElementById("dataState").hidden = false; });
    await test.setViewportSize({ width: 1440, height: 1000 });
    // The calendar sits in the collapsed "Spend patterns" section and loads only once opened.
    await test.evaluate(() => { document.getElementById("overviewPatterns").open = true; });
    await test.locator("#spendHeatmap").scrollIntoViewIfNeeded();
    await calendarStarted;
    await goto("resources");
    await test.locator("#tabBody [data-table]").waitFor();
    releaseCalendar();
    await test.waitForTimeout(150);
    await goto("overview");
    await test.evaluate(() => { document.getElementById("overviewPatterns").open = true; });
    await test.locator("#tabBody #spendHeatmap .heat-cell[title]").first().waitFor();
    check("a delayed heatmap import survives cached navigation", await test.locator("#tabBody #spendHeatmap h3").textContent() === "Daily pattern");
    check("four days of data are visible", await test.locator("#tabBody #spendHeatmap .heat-cell[title]").count() === 4);
    check("UTC billing dates stay on their weekday in India", (await test.locator("#tabBody .heat-cell[title]").first().getAttribute("title")).includes("Tuesday"));
    const shortWindows = await test.evaluate(async () => {
      const { drawCalendarHeatmap } = await import("/assets/calendarheat.js");
      const host = document.createElement("div");
      drawCalendarHeatmap(host, { labels: [], values: [] }, "USD");
      const empty = host.textContent.includes("Daily pattern") && host.textContent.includes("No daily spend data");
      drawCalendarHeatmap(host, { labels: ["2026-09-03"], values: [0] }, "USD");
      return { empty, zero: host.querySelectorAll(".heat-0").length === 1,
        weekday: host.querySelector(".heat-cell[title]").title.includes("Thursday") };
    });
    check("empty calendar explains the missing data", shortWindows.empty);
    check("one zero-cost day remains visible on the correct weekday", shortWindows.zero && shortWindows.weekday);
    for (const area of ["compute", "networking", "aks", "overview"]) {
      await test.locator(`#viewActions [data-area="${area}"]`).click();
      check(`${area} retains Overview sections during navigation`, await test.locator("#viewActions .ar-picker").count() === 1);
      await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
      check(`${area} retains Overview sections after loading`, await test.locator("#viewActions .ar-picker").count() === 1
        && await test.locator("#tabBody .ar-picker").count() === 0);
      check(`${area} is selected`, await test.locator(`#viewActions [data-area="${area}"]`).getAttribute("aria-pressed") === "true");
    }
    check("Removed Resiliency is not loaded or requested", !requests.some(url => url.includes("resiliency")));
    const initialTheme = await test.evaluate(() => document.documentElement.dataset.theme);
    const unifiedHeaders = [];
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const width of [1440, 900, 390]) {
        await test.setViewportSize({ width, height: 1000 });
        for (const section of ["overview", "billing", "savings", "monitor", "governance", "reports"]) {
          await test.locator(`#tabs [data-main-section="${section}"]`).click();
          await test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const header = await test.locator("#viewActions").evaluate(el => {
            const nav = el.querySelector(".subsection-nav"), title = el.querySelector("h2");
            const active = nav.querySelector('[aria-selected="true"], [aria-pressed="true"]');
            const style = getComputedStyle(nav), selected = getComputedStyle(active);
            const marker = getComputedStyle(active, "::before");
            const a = title.getBoundingClientRect(), b = nav.getBoundingClientRect();
            return {
              title: title.textContent, width: innerWidth, theme: document.documentElement.dataset.theme,
              height: el.getBoundingClientRect().height, titleSize: parseFloat(getComputedStyle(title).fontSize),
              borderless: parseFloat(style.borderTopWidth) === 0 && parseFloat(selected.borderTopWidth) === 0,
              noNestedPanel: style.backgroundColor === "rgba(0, 0, 0, 0)" && parseFloat(style.borderRadius) === 0,
              sharedSurface: getComputedStyle(el).backgroundColor === getComputedStyle(document.getElementById("statusRow")).backgroundColor,
              buttonHeight: active.getBoundingClientRect().height,
              underline: Math.abs(parseFloat(marker.height) - 2) < .1 && marker.transform === "matrix(1, 0, 0, 1, 0, 0)",
              sameRow: Math.abs((a.top + a.bottom - b.top - b.bottom) / 2) < 2,
              // With a subsection nav the h2 is screen-reader only (app.css .view-title rule).
              titleHidden: title.parentElement.getBoundingClientRect().width <= 1,
              overflow: document.documentElement.scrollWidth > innerWidth,
            };
          });
          check(`integrated ${section} header ${theme}/${width}`, header.borderless && header.noNestedPanel
            && header.sharedSurface && header.titleHidden && header.underline && !header.overflow
            && header.buttonHeight >= 39 && header.buttonHeight <= 41
            && header.height <= (width > 620 ? 54 : 80));
          unifiedHeaders.push(header);
        }
      }
    }
    await test.evaluate(theme => {
      if (theme) document.documentElement.dataset.theme = theme;
      else delete document.documentElement.dataset.theme;
    }, initialTheme);
    await test.setViewportSize({ width: 1440, height: 1000 });
    await goto("overview");
    await test.evaluate(() => {
      window.timings = [];
      window.addEventListener("cl:dashboard-performance", e => window.timings.push(e.detail));
    });
    await goto("savings");
    await test.locator(".sv-card").first().waitFor();
    await test.locator(".sv-head").first().click();
    check("Savings links exact resources from expanded cards",
      (await test.locator("#tabBody .sv-body .explore-links a").first().getAttribute("href")).includes("/virtualMachines/vm-0/overview"));
    await test.locator(".wf-drill summary").first().click();
    const sku = test.locator(".wf-sku").first();
    await sku.fill("Standard_B2s");
    await sku.evaluate(el => { el.focus(); el.setSelectionRange(2, 7); window.liveDrill = el.closest("details"); });
    await test.waitForFunction(() => document.querySelectorAll(".sv-card").length === 13);
    const state = await test.evaluate(() => ({
      open: document.querySelector(".sv-head").getAttribute("aria-expanded"),
      drill: document.querySelector(".wf-drill") === window.liveDrill,
      detailsOpen: document.querySelector(".wf-drill").open,
      value: document.querySelector(".wf-sku").value,
      focused: document.activeElement === document.querySelector(".wf-sku"),
      selection: document.querySelector(".wf-sku").selectionStart,
    }));
    check("partial result preserves card and What-If", state.open === "true" && state.drill && state.detailsOpen);
    check("partial result preserves value, focus and selection", state.value === "Standard_B2s" && state.focused && state.selection === 2);
    await test.locator(".wf-go").first().click();
    await test.waitForFunction(() => document.querySelector(".wf-result").textContent.includes("Fixture comparison"));
    await goto("resources");
    await test.locator("[data-table]:visible").waitFor();
    const pollsAfterLeave = requests.filter(x => x.includes("/savings/progress")).length;
    await test.waitForTimeout(1500);
    check("Savings stops polling off-tab", requests.filter(x => x.includes("/savings/progress")).length === pollsAfterLeave);
    await goto("savings");
    check("cached Savings keeps What-If results", await test.evaluate(() =>
      document.querySelector(".wf-drill") === window.liveDrill &&
      document.querySelector(".wf-result").textContent.includes("Fixture comparison")));
    await goto("resources");
    await test.locator('th[data-col="3"]').click();
    await test.locator('[data-page="next"]').click();
    const selectedName = await test.locator("#tabBody .resource-name > :first-child").first().textContent();
    const requestsBeforeDetails = requests.length;
    await test.locator("#tabBody [data-row-detail]").first().focus();
    await test.keyboard.press("Enter");
    check("keyboard drill-down matches the sorted paged row", (await test.locator("#tabBody .row-detail").innerText()).includes(selectedName));
    check("row details use already loaded data", requests.length === requestsBeforeDetails);
    const resourceLink = test.locator("#tabBody .row-detail .explore-links a").first();
    check("row details link the exact resource", (await resourceLink.getAttribute("href")).endsWith(`/virtualMachines/${selectedName}/overview`));
    await resourceLink.evaluate(link => link.addEventListener("click", event => event.preventDefault(), { once: true }));
    await resourceLink.click();
    check("portal links do not toggle row expansion", await test.locator("#tabBody .row-detail").count() === 1);
    const rowCell = test.locator("#tabBody tr[data-explore-row]").first().locator("td").nth(1);
    await rowCell.click();
    check("clicking ordinary row content collapses details", await test.locator("#tabBody .row-detail").count() === 0);
    await rowCell.click();
    await test.locator("#tabBody .row-detail details summary").first().click();
    const before = await test.locator("[data-table]:visible").getAttribute("data-table");
    await test.evaluate(() => { window.resourceNode = [...document.querySelectorAll("[data-table]")].find(table => table.offsetParent); });
    const resourcesBeforeReturn = requests.filter(url => url.startsWith("/api/dashboard/resources")).length;
    await test.evaluate(() => { window.realNow = Date.now; Date.now = () => window.realNow() + 86400000; });
    await goto("overview");
    await goto("resources");
    check("day-old tab snapshots do not refetch", requests.filter(url => url.startsWith("/api/dashboard/resources")).length === resourcesBeforeReturn);
    await test.evaluate(() => { Date.now = window.realNow; delete window.realNow; });
    check("cached navigation reuses real DOM", await test.evaluate(() => window.resourceNode === [...document.querySelectorAll("[data-table]")].find(table => table.offsetParent)));
    check("cached navigation retains page and sort", (await test.locator(".pager-count").textContent()).includes("21") &&
      await test.locator('th[data-col="3"]').getAttribute("aria-sort") === "descending");
    const cachedDetail = await test.locator("#tabBody .row-detail").evaluate(element => ({
      text: element.innerText, content: element.textContent, hidden: element.querySelector("section")?.hidden,
      bounds: element.getBoundingClientRect().toJSON(),
    }));
    check(`cached navigation retains expanded row${cachedDetail.text.includes(selectedName) ? "" : `: ${JSON.stringify(cachedDetail)}`}`,
      cachedDetail.text.includes(selectedName));
    await test.locator("#refreshViews").click();
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    check("Refresh views fetches new results", requests.filter(url => url.startsWith("/api/dashboard/resources")).length === resourcesBeforeReturn + 1);
    check("refresh uses stable table identity", await test.locator("[data-table]:visible").getAttribute("data-table") === before);
    check("refresh retains page and sort", (await test.locator(".pager-count").textContent()).includes("21") &&
      await test.locator('th[data-col="3"]').getAttribute("aria-sort") === "descending");
    check("refresh retains expanded row identity", (await test.locator("#tabBody .row-detail").innerText()).includes(selectedName));
    check("refresh retains nested detail expansion", await test.locator("#tabBody .row-detail details").first().evaluate(el => el.open));
    await test.locator("#resourceSearch").fill("resource-7");
    await test.waitForTimeout(150);
    await test.evaluate(() => { window.testScope = ["b"]; window.dispatchEvent(new Event("cl:test-refresh")); });
    await test.waitForTimeout(200);
    check("changed scope does not leak table query", await test.locator("#resourceSearch").inputValue() === "");
    check("changed scope changes table key", await test.locator("[data-table]:visible").getAttribute("data-table") !== before);
    check("changed scope does not inherit row expansion", await test.locator("#tabBody .row-detail").count() === 0);
    for (const change of ["period", "currency", "filter"]) {
      await test.locator("#resourceSearch").fill("resource-7");
      await test.waitForTimeout(140);
      if (change === "period") await test.locator('[data-days="7"]').click();
      else if (change === "currency") await test.locator("#currency").selectOption("INR");
      else await test.evaluate(() => {
        window.testFilters = ["region=eastus"];
        window.dispatchEvent(new Event("cl:test-refresh"));
      });
      await test.waitForTimeout(160);
      check(`changed ${change} does not leak table query`, await test.locator("#resourceSearch").inputValue() === "");
    }
    await test.evaluate(() => { window.retainedResources = [...document.querySelectorAll("[data-table]")].find(table => table.offsetParent); });
    const retainedResourceRequests = requests.filter(url => url.startsWith("/api/dashboard/resources")).length;
    await goto("savings");
    await test.waitForFunction(() => document.querySelector("#svRefresh") && !document.querySelector("#svRefresh").disabled, { timeout: 10000 });
    check("completed scan shows generated time", await test.locator("#svStatus time").count() === 1);
    const clockRequests = requests.filter(x => x.includes("/savings/progress")).length;
    const ageState = await test.evaluate(async () => {
      const now = Date.now;
      const button = document.querySelector("#svRefresh");
      try {
        Date.now = () => now() + 2000;
        await new Promise(resolve => setTimeout(resolve, 1200));
        const retained = button === document.querySelector("#svRefresh");
        Date.now = () => now() + 61000;
        await new Promise(resolve => setTimeout(resolve, 1200));
        return {
          retained, stale: document.querySelector("#svProgress").textContent.includes("Stale"),
          quietAge: !document.querySelector(".sv-age").closest("[aria-live]"),
        };
      } finally { Date.now = now; }
    });
    check("age-only ticks preserve the refresh control", ageState.retained);
    check("completed results age into stale without network", ageState.stale &&
      requests.filter(x => x.includes("/savings/progress")).length === clockRequests);
    check("age ticks are outside the live announcement", ageState.quietAge);
    await goto("resources");
    releaseRecommendation();
    await test.waitForFunction(() => document.querySelector("[data-dashboard-cache] #recBody")?.textContent === "Start with the idle VM.");
    await goto("savings");
    await test.waitForTimeout(150);
    check("completed Savings resumes its clock without starting a scan",
      requests.filter(url => url.includes("/savings/progress")).length === clockRequests);
    check("late recommendations populate their cached card",
      await test.locator("#recBody").textContent() === "Start with the idle VM.");
    failScan = true;
    await test.locator("#svRefresh").click();
    await test.waitForFunction(() => document.querySelector("#svStatus").textContent.includes("Refresh failed"));
    check("failed refresh keeps previous opportunities", await test.locator(".sv-card").count() === 13);
    check("refresh is forced only once", forceCount === 1);
    failScan = false;
    for (const name of ["governance", "anomalies", "budgets", "commitments", "esu", "report", "settings", "history"]) {
      await goto(name);
      await test.waitForTimeout(150);
      const banner = (await test.locator("#tabBody > .banner.err").allTextContents())
        .filter(text => name !== "budgets" || !text.startsWith("1 budget is over.")).join("; ");
      check(`${name} renderer has no error banner${banner ? `: ${banner}` : ""}`, !banner);
    }
    await goto("resources");
    check("more than eight visited tabs retain their original DOM", await test.evaluate(() =>
      window.retainedResources === [...document.querySelectorAll("[data-table]")].find(table => table.offsetParent)));
    check("more than eight visited tabs retain results without refetching",
      requests.filter(url => url.startsWith("/api/dashboard/resources")).length === retainedResourceRequests);
    const timings = await test.evaluate(() => window.timings);
    check("cached changes finish under 200ms", timings.filter(t => t.cached).every(t => t.total_ms < 200));
    check("navigation feedback under 100ms", timings.every(t => t.feedback_ms < 100));
    check("no browser exceptions", errors.length === 0);
    await test.evaluate(() => {
      window.testScope = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"];
      window.dispatchEvent(new Event("cl:test-refresh"));
    });
    await test.waitForFunction(() => document.querySelectorAll("#statusActions .view-portal-menu a").length === 2);
    check("multiple scopes offer separate portal choices", await test.locator("#statusActions .view-portal-menu a").count() === 2);
    await goto("azure-updates");
    await test.locator("#azure-updates .azure-feed-item").first().waitFor({ state: "attached" });
    check("all three sources initially visible and collapsed", await test.locator(".azure-source-toggle").count() === 3
      && await test.locator('.azure-source-toggle[aria-expanded="false"]').count() === 3
      && await test.locator("#azure-updates-toggle").isVisible()
      && await test.locator("#azure-status-toggle").isVisible()
      && await test.locator("#azure-health-toggle").isVisible()
      && await test.locator("#azure-updates-search").isHidden()
      && await test.locator("#azure-health-status").isHidden());
    await test.setViewportSize({ width: 390, height: 844 });
    check("collapsed source headers fit narrow screens", await test.evaluate(() =>
      [...document.querySelectorAll(".azure-source-toggle")].every(toggle => {
        const box = toggle.getBoundingClientRect();
        return box.width > 0 && box.left >= 0 && box.right <= window.innerWidth;
      })));
    await test.setViewportSize({ width: 1440, height: 1000 });
    check("collapsed announcements show total, not page count", await test.locator("#azure-updates .azure-source-count").textContent() === "30");
    check("limited listing is disclosed while collapsed", (await test.locator("#azure-updates").innerText()).includes("This response is limited"));
    check("Azure Updates is a Monitor subsection, not a left tab", await test.locator('#viewActions [data-subsection="azure-updates"]').count() === 1
      && await test.locator('#tabs [data-tab="azure-updates"]').count() === 0
      && await test.locator('#tabs [data-main-section="monitor"]').getAttribute("aria-selected") === "true");
    check("announcements render while private health is pending", (await test.locator("#azure-health").textContent()).includes("Loading this source"));
    await test.waitForFunction(() => document.querySelector("#azure-status").textContent.includes("Public status temporarily unavailable"));
    check("public-status failure does not hide announcements", await test.locator("#azure-updates .azure-feed-item").count() === 10);
    check("a failed status source does not claim no incidents", !(await test.locator("#azure-status").innerText()).includes("No active public incidents"));
    check("a failed source has unknown count, not zero", await test.locator("#azure-status .azure-source-count").textContent() === "Unavailable");
    releaseHealth();
    await test.locator("#azure-health .azure-feed-item").waitFor({ state: "attached" });
    check("health count loads without opening the source", await test.locator("#azure-health .azure-source-count").textContent() === "1"
      && await test.locator("#azure-health-content").isHidden());
    const requestsBeforeExpand = requests.length;
    await test.locator("#azure-updates-toggle").focus();
    await test.keyboard.press("Enter");
    check("keyboard expands only the selected source", await test.locator("#azure-updates-search").isVisible()
      && await test.locator("#azure-health-content").isHidden()
      && await test.locator("#azure-status-content").isHidden());
    check("expanding uses already loaded data", requests.length === requestsBeforeExpand);
    await test.locator("#azure-updates .azure-feed-item > summary").first().click();
    check("announcement HTML is shown as safe text", await test.locator("#azure-updates img").count() === 0
      && (await test.locator("#azure-updates .azure-item-body").first().textContent()).includes("<img"));
    check("unsafe announcement URLs are not links", await test.locator('#azure-updates a[href^="javascript:"]').count() === 0);
    await test.locator("#azure-updates-search").fill("update 1");
    await test.waitForTimeout(180);
    await test.locator("#azure-attention").check();
    check("customer-attention filter searches all loaded pages", await test.locator("#azure-updates .azure-feed-item").count() === 2);
    check("filtering leaves the source total unchanged", await test.locator("#azure-updates .azure-source-count").textContent() === "30");
    await test.locator("#azure-updates .azure-feed-item > summary").first().click();
    await test.locator("#azure-updates-toggle").click();
    check("source collapses without losing expanded items", await test.locator("#azure-updates-content").isHidden()
      && await test.locator("#azure-updates .azure-feed-item").first().evaluate(el => el.open));
    updatesUnavailable = true;
    await test.locator('#azure-updates [data-refresh-source]').click();
    await test.waitForFunction(() => document.querySelector("#azure-updates").textContent.includes("Announcements feed unavailable"));
    check("failed refresh keeps prior announcements and filters", await test.locator("#azure-updates .azure-feed-item").count() === 2
      && await test.locator("#azure-updates-search").inputValue() === "update 1");
    check("refresh preserves an expanded announcement", await test.locator("#azure-updates .azure-feed-item").first().evaluate(el => el.open));
    check("failed refresh preserves collapsed state and total", await test.locator("#azure-updates-toggle").getAttribute("aria-expanded") === "false"
      && await test.locator("#azure-updates-content").isHidden()
      && await test.locator("#azure-updates .azure-source-count").textContent() === "30");
    await test.locator("#azure-updates-toggle").click();
    check("reopening restores the filtered information", await test.locator("#azure-updates-search").inputValue() === "update 1"
      && await test.locator("#azure-updates .azure-feed-item").count() === 2);
    const newsRequests = requests.filter(url => url.startsWith("/api/dashboard/azure-updates")).length;
    statusAvailable = true;
    await test.locator('#azure-status [data-refresh-source]').click();
    await test.waitForFunction(() => document.querySelector("#azure-status").textContent.includes("No active public incidents"));
    check("successful empty source shows zero and stays collapsed", await test.locator("#azure-status .azure-source-count").textContent() === "0"
      && await test.locator("#azure-status-content").isHidden());
    check("individual refresh leaves other sources alone", requests.filter(url => url.startsWith("/api/dashboard/azure-updates")).length === newsRequests);
    check("global sources do not receive subscription/cost filters", requests.filter(url => /^\/api\/dashboard\/azure-(updates|status)/.test(url))
      .every(url => !/[?&](scope|days|currency|region)=/.test(url)));
    check("private Service Health receives selected scope", requests.some(url => url.startsWith("/api/dashboard/service-health?scope=")));
    await test.locator("#azure-jump-health").click();
    check("jump link expands health independently", await test.locator("#azure-health-status").isVisible()
      && await test.locator("#azure-updates-toggle").getAttribute("aria-expanded") === "true");
    await test.locator("#azure-health-status").selectOption("Active");
    check("Service Health has an independent status filter", await test.locator("#azure-health .azure-feed-item").count() === 1);
    const feedRequests = requests.filter(url => /\/api\/dashboard\/(azure-updates|azure-status|service-health)/.test(url)).length;
    await goto("resources");
    await goto("azure-updates");
    await test.waitForTimeout(150);
    check("Azure Updates keeps completed sources on return",
      requests.filter(url => /\/api\/dashboard\/(azure-updates|azure-status|service-health)/.test(url)).length === feedRequests);
    let releaseHealthRefresh;
    holdHealthRefresh = new Promise(resolve => { releaseHealthRefresh = resolve; });
    await test.locator('#azure-health [data-refresh-source]').click();
    await test.waitForFunction(() => document.querySelector('#azure-health [data-refresh-source]').disabled);
    await goto("resources");
    holdHealthRefresh = null;
    releaseHealthRefresh();
    const otherFeedRequests = requests.filter(url => /\/api\/dashboard\/(azure-updates|azure-status)/.test(url)).length;
    await goto("azure-updates");
    await test.waitForFunction(() => !document.querySelector('#azure-health [data-refresh-source]').disabled);
    check("interrupted Service Health refresh resumes without reloading completed public feeds",
      requests.filter(url => /\/api\/dashboard\/(azure-updates|azure-status)/.test(url)).length === otherFeedRequests);
    await test.evaluate(() => {
      window.testScope = [];
      window.dispatchEvent(new Event("cl:test-refresh"));
    });
    await test.waitForFunction(() => document.querySelector("#azure-health .azure-feed-item"));
    const latestHealth = requests.filter(url => url.startsWith("/api/dashboard/service-health")).at(-1);
    check("All subscriptions expands to explicit health scope", latestHealth.includes("11111111-1111-4111-8111-111111111111")
      && latestHealth.includes("22222222-2222-4222-8222-222222222222"));
    await goto("billing");
    await test.waitForFunction(() => document.querySelector("#billSpent .bill-value")?.textContent.includes("100.00"));
    check("Billing is named exactly in the rail", await test.locator('#tabs [data-main-section="billing"] .rail-name').textContent() === "Billing");
    check("statement renders before live opportunities finish", (await test.locator("#billPotential").textContent()).includes("Scanning"));
    check("achieved savings are distinct from billed spend", (await test.locator("#billSaved .bill-value").textContent()).includes("40.00"));
    check("Billing uses last calendar month", (await test.locator("#billPeriod").textContent()).includes("August 2026"));
    releaseBillingEstimate();
    await test.waitForFunction(() => !document.querySelector("#billRefresh").disabled
      && document.querySelector("#billPotential .bill-value")?.textContent.includes("20.00"));
    check("potential is the compatible estimate, not alternatives added together", (await test.locator("#billPotential .bill-value").textContent()).includes("20.00"));
    check("clear recommendation action labels are present", (await test.locator(".bill-recommendation").allTextContents()).join("|")
      === "Right-size resources|Buy Reservation|Increase Savings Plan coverage");
    check("overlapping alternatives are marked", (await test.locator("#billOpportunities").textContent()).includes("not additive"));
    check("commitment watch flags utilization and expiry", (await test.locator("#billCommitments").textContent()).includes("Underutilized")
      && (await test.locator("#billCommitments").textContent()).includes("Expires in 45 days"));
    check("provider metadata objects are rendered as readable text", !(await test.locator("#tabBody").textContent()).includes("[object Object]"));
    await test.locator('[data-bill-breakdown="resources"]').click();
    check("resource breakdown is available on the same Billing page", (await test.locator("#billBreakdown").textContent()).includes("VM"));
    await test.locator("#billMethods > summary").click();
    billingLegacy = true;
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => !document.querySelector("#billRefresh").disabled);
    check("legacy imported spend is not mislabeled as invoice", (await test.locator("#billSpent h3").textContent()) === "Recorded spend");
    check("missing prices are unavailable, not zero savings", (await test.locator("#billSaved .bill-value").textContent()) === "Not available");
    check("incomparable months do not imply a valid percentage comparison", (await test.locator("#billMomChart").textContent()).includes("not directly comparable"));
    check("breakdown selection and notes survive refresh", await test.locator('[data-bill-breakdown="resources"]').getAttribute("aria-pressed") === "true"
      && await test.locator("#billMethods").evaluate(el => el.open));
    check("Billing historical requests omit rolling-day selection", requests.filter(url => /^\/api\/dashboard\/billing(?:\?|$)/.test(url))
      .every(url => !/[?&]days=/.test(url)));
    const billingRequests = requests.filter(url => url.startsWith("/api/dashboard/billing")).length;
    await test.evaluate(() => { window.retainedBilling = document.querySelector("#billSpent"); });
    await goto("resources");
    await goto("billing");
    await test.waitForTimeout(150);
    check("Billing keeps both completed responses on return",
      requests.filter(url => url.startsWith("/api/dashboard/billing")).length === billingRequests);
    check("Billing return retains the statement nodes", await test.evaluate(() =>
      window.retainedBilling === document.querySelector("#billSpent")));
    let releaseBillingRefresh;
    holdBillingRefresh = new Promise(resolve => { releaseBillingRefresh = resolve; });
    billingLegacy = false;
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => document.querySelector("#billSpent .bill-value")?.textContent.includes("100.00")
      && !document.querySelector("#billRefresh").disabled
      && document.querySelector("#billPotential .bill-loading-note"));
    check("completed statement stays refreshable while opportunities load",
      await test.locator("#billRefresh").isEnabled());
    await goto("resources");
    holdBillingRefresh = null;
    releaseBillingRefresh();
    const completedStatementRequests = requests.filter(url => /^\/api\/dashboard\/billing(?:\?|$)/.test(url)).length;
    const interruptedOutlookRequests = requests.filter(url => url.startsWith("/api/dashboard/billing/opportunities")).length;
    await goto("billing");
    await test.waitForFunction(() => !document.querySelector("#billRefresh").disabled
      && !document.querySelector("#billPotential .bill-loading-note"));
    check("interrupted Billing resumes only its unfinished opportunities",
      requests.filter(url => /^\/api\/dashboard\/billing(?:\?|$)/.test(url)).length === completedStatementRequests
      && requests.filter(url => url.startsWith("/api/dashboard/billing/opportunities")).length === interruptedOutlookRequests + 1);
    await goto("tags");
    await test.locator("#tagSumSub").waitFor();
    check("Tags initially explores every loaded resource", (await test.locator("#tagSumSub").textContent()).includes("4 of 4")
      && (await test.locator("#tagSum").textContent()).includes("175.00"));
    await test.locator('[data-resource-view="untagged"]').click();
    check("Untagged view exposes otherwise hidden resource costs", (await test.locator("#tagSumSub").textContent()).includes("1 of 4")
      && (await test.locator("#tagSum").textContent()).includes("25.00")
      && (await test.locator("#tagResults").textContent()).includes("untagged-ip"));
    await test.locator('[data-resource-view="tagged"]').click();
    check("Tagged view counts empty-value tags as present", (await test.locator("#tagSumSub").textContent()).includes("3 of 4"));
    await test.locator("#tagClear").click();
    await test.locator("#tagKeysToggle").click();
    await test.locator('#tagChips [data-tag="env"]').click();
    await test.keyboard.press("Escape");
    await test.locator("#tagValuesToggle").click();
    await test.locator('[data-value-search="env"]').fill("prod");
    check("value search leaves current resource selection intact", (await test.locator("#tagSumSub").textContent()).includes("3 of 4"));
    await test.locator('[data-vk="env"][data-vv="prod"]').click();
    await test.keyboard.press("Escape");
    check("key and value selection narrows the resource costs", (await test.locator("#tagSumSub").textContent()).includes("1 of 4")
      && (await test.locator("#tagSum").textContent()).includes("100.00"));
    await test.locator("#tagClear").click();
    check("Clear returns to all resources", (await test.locator("#tagSumSub").textContent()).includes("4 of 4"));
    await test.locator("#tagUndo").click();
    check("Undo restores key/value selection and value search", (await test.locator("#tagSumSub").textContent()).includes("1 of 4")
      && await test.locator('[data-value-search="env"]').inputValue() === "prod");
    await test.setViewportSize({ width: 390, height: 844 });
    check("tag controls fit mobile width", await test.evaluate(() =>
      document.querySelector("#tagFilter").getBoundingClientRect().right <= window.innerWidth));
    await test.setViewportSize({ width: 1440, height: 1000 });
    await test.locator("#tagClear").click();
    await test.locator("#tagKeysToggle").click();
    await test.locator('#tagChips [data-tag="env"]').click();
    await test.keyboard.press("Escape");
    await test.locator("#tagValuesToggle").click();
    await test.locator('[data-vk="env"][data-vv="dev"]').click();
    await test.keyboard.press("Escape");
    await goto("budgets");
    await test.locator("#budgetList .budget-summary").first().waitFor();
    check("budgets open as compact independent cards", await test.locator("#budgetList details").count() === 4
      && await test.locator("#budgetList details[open]").count() === 0);
    check("incomplete budget reads remain visible alongside results", (await test.locator("#tabBody").textContent()).includes("Incomplete budget data"));
    check("budget figures are not summed across overlapping scopes", (await test.locator("#tabBody").textContent()).includes("Overlapping budgets and different currencies are not summed"));
    check("budget charts are not drawn while cards are collapsed", await test.locator("#budgetList .budget-chart[data-drawn]").count() === 0);
    await test.locator('#budgetList [data-budget-index="0"] > summary').focus();
    await test.keyboard.press("Enter");
    await test.waitForFunction(() => document.querySelector('#budgetList [data-budget-index="0"] .budget-chart')?.dataset.drawn === "true");
    check("keyboard expansion draws only the requested budget chart", await test.locator("#budgetList details[open]").count() === 1);
    await test.locator('[data-bstatus="unknown"]').click();
    check("unknown budget spend is not reported as zero", await test.locator("#budgetList details").count() === 1
      && (await test.locator("#budgetList").textContent()).includes("Not reported"));
    await test.locator("#budgetReset").click();
    check("budget expansion survives filtering", await test.locator('#budgetList [data-budget-index="0"]').evaluate(el => el.open));
    await test.locator('#budgetList [data-budget-index="1"] > summary').click();
    check("unsupported budget filter cannot offer a misleading drilldown",
      await test.locator('#budgetList [data-budget-index="1"] [data-tagsfor]').count() === 0
      && (await test.locator('#budgetList [data-budget-index="1"]').textContent()).includes("No approximate trend"));
    await test.locator("#budgetFind").fill("Production");
    check("budget search shows matching count", (await test.locator("#budgetMatchCount").textContent()).includes("1 of 4"));
    await test.locator("#budgetRefresh").click();
    await test.waitForFunction(() => !document.querySelector("#budgetRefresh").disabled);
    check("explicit budget refresh bypasses cached source data", requests.some(url => /^\/api\/budgets\?/.test(url) && url.includes("refresh=true")));
    check("refresh preserves budget search and expansion", await test.locator("#budgetFind").inputValue() === "Production"
      && await test.locator('#budgetList [data-budget-index="0"]').evaluate(el => el.open));
    await test.setViewportSize({ width: 390, height: 844 });
    check("budget filter buttons fit mobile width", await test.evaluate(() =>
      [...document.querySelectorAll(".budget-tools .seg-btn")].every(button => button.getBoundingClientRect().right <= window.innerWidth)));
    await test.setViewportSize({ width: 1440, height: 1000 });
    await test.locator("#budgetList [data-tagsfor]").click();
    await test.waitForFunction(() => document.querySelector("#tagSumSub")?.textContent.includes("1 of 4"));
    check("budget drilldown replaces previous tag values", (await test.locator("#tagSelected").textContent()).includes("prod")
      && !(await test.locator("#tagSelected").textContent()).includes("dev"));
    await goto("budgets");
    await test.locator("#budgetRefresh").waitFor();
    budgetsUnavailable = true;
    await test.locator("#budgetRefresh").click();
    await test.waitForFunction(() => document.querySelector("#tabBody").textContent.includes("Budget data incomplete"));
    check("failed budget read does not claim no budgets", !(await test.locator("#tabBody").textContent()).includes("No budgets on the subscriptions"));
    budgetsUnavailable = false;
    await test.locator("#budgetRefresh").click();
    await test.locator("#budgetFind").waitFor();
    check("budget retry recovers with retained search", await test.locator("#budgetFind").inputValue() === "Production");
    check("Tags and Budgets produce no browser exceptions", errors.length === 0);
    const billingSections = ["Billing summary", "Hourly Billing", "Applications & Tags", "History", "Budgets"];
    check("Billing owns five subsections without Resources", JSON.stringify(await test.locator("#viewActions [data-subsection]").allTextContents())
      === JSON.stringify(billingSections));
    check("nested view keeps Billing selected on the left",
      await test.locator('#tabs [data-main-section="billing"]').getAttribute("aria-selected") === "true");
    check("no former child tabs remain on the left", await test.locator("#tabs [data-tab],#tabs [data-subsection],#tabs [data-group]").count() === 0);
    await test.locator('#tabs [data-main-section="resources"]').click();
    await test.locator("#tabBody [data-table]").waitFor();
    check("Resources panel is labelled by its primary tab", await test.locator("#tabBody").getAttribute("aria-labelledby") === "primary-resources");
    await test.locator('#tabs [data-main-section="savings"]').click();
    await test.locator('#tabs [data-main-section="billing"]').click();
    check("return to Billing restores Budgets rather than Resources", await test.locator('#viewActions [data-subsection="budgets"]').getAttribute("aria-selected") === "true");
    await test.locator('#viewActions [data-subsection="budgets"]').focus();
    await test.keyboard.press("Home");
    check("subsection Home selects Billing summary", await test.locator('#viewActions [data-subsection="billing"]').evaluate(el => el === document.activeElement && el.getAttribute("aria-selected") === "true"));
    await test.keyboard.press("End");
    check("subsection End selects Budgets", await test.locator('#viewActions [data-subsection="budgets"]').evaluate(el => el === document.activeElement && el.getAttribute("aria-selected") === "true"));
    await test.locator('#tabs [data-main-section="billing"]').focus();
    await test.keyboard.press("End");
    check("primary End selects Reports", await test.locator('#tabs [data-main-section="reports"]').evaluate(el => el === document.activeElement && el.getAttribute("aria-selected") === "true"));
    await test.keyboard.press("ArrowDown");
    check("primary arrow wraps to Overview", await test.locator('#tabs [data-main-section="overview"]').evaluate(el => el === document.activeElement && el.getAttribute("aria-selected") === "true"));
    check("Overview has Area selector, not a redundant subsection row", await test.locator("#viewActions [data-subsection]").count() === 0);
    await goto("compute");
    check("cost-area cross-link selects Overview parent", await test.locator('#tabs [data-main-section="overview"]').getAttribute("aria-selected") === "true");
    await goto("audit");
    check("admin Audit cross-link selects Governance", await test.locator('#tabs [data-main-section="governance"]').getAttribute("aria-selected") === "true"
      && await test.locator('#viewActions [data-subsection="audit"]').getAttribute("aria-selected") === "true");
    await test.evaluate(() => { window.testAdmin = false; window.dispatchEvent(new Event("cl:test-refresh")); });
    await test.waitForFunction(() => document.querySelector('#tabs [data-main-section="overview"]')?.getAttribute("aria-selected") === "true");
    await test.locator('#tabs [data-main-section="governance"]').click();
    check("non-admin navigation drops Audit without a stale remembered destination", await test.locator('#viewActions [data-subsection="audit"]').count() === 0);
    const beforeInvalid = requests.length;
    await goto("audit");
    check("unauthorized cross-link does not load a hidden view", requests.length === beforeInvalid
      && (await test.locator("#toasts").textContent()).includes("not available"));
    await test.setViewportSize({ width: 390, height: 844 });
    await test.waitForFunction(() => document.querySelector('#tabs [role="tablist"]')?.getAttribute("aria-orientation") === "horizontal");
    check("mobile primary navigation is horizontal", await test.locator('#tabs [role="tablist"]').getAttribute("aria-orientation") === "horizontal");
    await test.locator('#tabs [data-main-section="billing"]').click();
    await test.locator('#viewActions [data-subsection="billing"]').click();
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    check("mobile has exactly seven primary tabs and no page overflow", await test.locator("#tabs [data-main-section]").count() === 7
      && await test.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await test.locator('#viewActions [data-subsection="billing"]').focus();
    await test.keyboard.press("End");
    check("mobile keyboard reaches last horizontally scrolled subsection",
      await test.locator('#viewActions [data-subsection="budgets"]').evaluate(el => {
        const box = el.getBoundingClientRect();
        return el === document.activeElement && box.left >= 0 && box.right <= innerWidth;
      }));
    await goto("savings");
    await test.waitForFunction(() => !document.querySelector("#svRefresh")?.disabled
      && document.querySelector("#recBody")?.textContent === "Start with the idle VM.");
    const summariesBeforeReload = requests.filter(url => url.startsWith("/api/dashboard/recommendations")).length;
    await test.locator("#refreshViews").click();
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true"
      && document.querySelector("#recBody")?.textContent === "Start with the idle VM.");
    check("Refresh views rebuilds the summary even for the same completed scan",
      requests.filter(url => url.startsWith("/api/dashboard/recommendations")).length === summariesBeforeReload + 1);
    check("Savings priorities start as an on-demand disclosure", await test.locator("#recCard").evaluate(node =>
      node.tagName === "DETAILS" && !node.open));
    const priorityRequests = requests.length;
    await test.locator("#recCard > summary").click();
    check("opening priorities uses the loaded summary", await test.locator("#recBody").isVisible()
      && requests.length === priorityRequests);
    await test.locator("#recCard > summary").click();
    const premiumLayouts = [];
    for (const tab of ["resources", "billing", "savings", "governance", "changes", "report"]) {
      await goto(tab);
      await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
      if (tab === "billing") await test.waitForFunction(() => !document.querySelector("#billRefresh").disabled);
      for (const theme of ["light", "dark", "vivid"]) {
        await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
        for (const width of [1600, 800, 390]) {
          await test.setViewportSize({ width, height: 1000 });
          await test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const layout = await test.evaluate(() => {
            const body = document.querySelector("#tabBody");
            const groups = [...body.querySelectorAll(".kpis")].filter(node => node.getBoundingClientRect().width);
            return {
              width: innerWidth, bands: groups.length,
              workbenches: body.querySelectorAll(".portal-workbench").length,
              overflow: document.documentElement.scrollWidth > innerWidth + 1 || body.scrollWidth > body.clientWidth + 1,
              metricBorders: groups.every(group => [...group.children].every(node =>
                parseFloat(getComputedStyle(node).borderRadius) === 0)),
              controlsFit: [...body.querySelectorAll(".portal-workbench input:not([type=hidden]),.portal-workbench select")].every(node => {
                const rect = node.getBoundingClientRect();
                return !rect.width || (rect.left >= 0 && rect.right <= innerWidth + 1);
              }),
            };
          });
          premiumLayouts.push({ tab, theme, ...layout });
          check(`${tab}/${theme}/${width}: shared premium surfaces`, !layout.overflow && layout.metricBorders && layout.controlsFit);
          if (["resources", "billing", "savings"].includes(tab)) check(`${tab}: grouped summary controls`, layout.workbenches === 1);
        }
      }
    }
    check("navigation hierarchy causes no browser exceptions", errors.length === 0);
    return { failures, errors, unifiedHeaderLayouts: unifiedHeaders.length, headerSample: unifiedHeaders[0],
      premiumLayouts,
      timings: timings.map(({ tab, cached, feedback_ms, total_ms, api_ms, render_ms, module_ms }) =>
      ({ tab, cached, feedback_ms, total_ms, api_ms, render_ms, module_ms })), requests: requests.length };
  } catch (err) {
    return { failure: err.message, errors, failures, requests,
      body: test.isClosed() ? "closed" : (await test.locator("body").innerText()).slice(0, 3000) };
  } finally {
    releaseCalendar();
    releaseHealth();
    releaseBillingEstimate();
    await context.close();
  }
}
