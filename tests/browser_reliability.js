async (page) => {
  // All API responses are fixtures. Only assets/HTML are read from the source server.
  const origin = new URL(page.url()).origin;
  const context = await page.context().browser().newContext({ viewport: { width: 1500, height: 1050 } });
  const test = await context.newPage();
  test.setDefaultTimeout(8000);
  const errors = [], checks = [], releases = [];
  const check = (name, value) => { if (!value) throw new Error(name); checks.push(name); };
  let costMode = "timeout", outlookMode = "hold", statementMode = "ok", filterMode = "unavailable";
  let statementRevision = 1, outlookRevision = 1, opportunityRequests = 0, statementRequests = 0;
  let identityMode = "ok", identityRequests = 0;
  const hold = () => new Promise(resolve => releases.push(resolve));
  const releaseAll = () => releases.splice(0).forEach(resolve => resolve());
  const appSource = await (await page.request.get(`${origin}/assets/app.js`)).text();
  const getSource = appSource.slice(appSource.indexOf("export async function get("),
    appSource.indexOf("\n/**", appSource.indexOf("export async function get(")));
  const identitySource = appSource.slice(appSource.indexOf("let identityPromise = null;"),
    appSource.indexOf("/** Show who is signed in."));
  check("exercise the production get timeout/cancellation implementation", getSource.includes("const timer"));
  const appStub = `
    let cacheWarmer = null;
    const recordSnapshot = data => data;
    const toLogin = () => {};
    ${getSource.replace("export async function get(", "async function realGet(")}
    export const get = (url, options = {}) => realGet(url, { ...options,
      timeout: /\\/dashboard\\/(sections|executive)|\\/filters|\\/auth\\/me/.test(url) ? 1000 : options.timeout });
    export const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    export const money = (v,c='USD') => c + ' ' + Number(v || 0).toFixed(2);
    export const moneyHtml = money;
    export const readStore = (key, fallback) => JSON.parse(localStorage.getItem(key) || "null") ?? fallback;
    export const writeStore = (key, value) => localStorage.setItem(key, JSON.stringify(value));
    export const post = async () => { throw new Error("Writes are forbidden in this suite"); };
    export const allSubscriptions = () => [{id:'11111111-1111-4111-8111-111111111111',name:'Fixture'}];
    export const scopeIds = () => ['11111111-1111-4111-8111-111111111111'];
    export const selectSubscriptions = () => true;
    ${identitySource}
    export const onScopeChange = fn => window.addEventListener('cl:test-refresh',fn);
    export const askAgent = () => {};
    export const drawChart = async (host, data) => { host.textContent = data.title; host.dataset.chart = JSON.stringify(data); };
    export const refreshHeader = () => {};
    export const setGlanceSpark = () => {};
  `;
  const html = `<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css">
    <script type="module" src="/assets/dashboard.js"></script></head><body>
    <header class="bar"><button id="brand">CloudLens</button><div class="bar-right">
    <div id="period"><button data-days="7">7d</button><button data-days="30">30d</button></div>
    <select id="currency"></select><div id="refresh"><button id="refreshBtn"><span id="refreshLabel"></span></button>
    <div id="refreshMenu" hidden></div></div><div id="exportWrap"><button id="exportBtn">Export</button>
    <div id="exportMenu" hidden></div></div><button id="askToggle"></button></div></header>
    <aside id="side" class="side"><button id="sideClose"></button><input id="q"></aside><div id="chatFab"></div>
    <div class="shell" id="shell"><nav id="tabs" class="rail"></nav><main class="board">
    <div class="board-summary"><input type="month" id="reportingMonth"><span id="periodRange"></span>
    <div id="statusRow" class="statusrow"><div class="status-meta"><div id="statusActions" class="view-controls"></div>
    <div id="dataState" class="datastate"></div></div></div><div id="viewActions" class="view-actions"></div>
    <div id="filterBar" class="filterbar" hidden><div id="fbSlots"></div><button id="fbClear">Clear all</button>
    <div id="fbStatus" role="status"></div></div></div>
    <div id="tabBody" class="board-body" role="tabpanel" tabindex="-1"></div></main></div></body></html>`;
  const stubs = {
    "app.js": appStub,
    "analysis.js": "export const close=()=>{},invalidate=()=>{},isOpen=()=>false,openAnalysis=()=>{},setAnalysisContext=()=>{},wireAnalysis=()=>{};",
    "bot.js": "export const mountBot=()=>{},setBotState=()=>{};",
  };
  const statement = (query, revision) => ({
    period: { month: query.get("period_month") || "2026-08",
      label: `Fixture statement ${revision} · ${query.get("period_month") || "2026-08"}`,
      previous_month: "2026-07", days: 31 },
    currency: query.get("currency") || "USD", spend_basis: { code: "invoice", metric: "billed_cost", label: "Total billed" },
    kpis: { billed_cost: revision * 100, recorded_cost: 100, list_cost: 150, actual_savings: 40,
      azure_discounts: 15, reservation_savings: 10, savings_plan_savings: 15, effective_discount_pct: 26.67 },
    pricing: { status: "complete" }, fx: { complete: true }, warnings: [], per_currency: [],
    coverage: { current: { rows: 31, days_present: 31, calendar_days: 31 } },
    month_over_month: { current: 100, previous: 80, change_pct: 25, comparable: true },
    trend: { labels: ["2026-07", "2026-08"], values: [80, 100] },
    breakdowns: { subscriptions: [{ name: `Subscription ${revision}`, cost: 100, currency: query.get("currency") || "USD" }],
      services: [{ name: "Storage", cost: 100, currency: "USD" }], resources: [] },
  });
  await context.addInitScript(() => {
    localStorage.setItem("costCurrency", JSON.stringify("INR"));
    localStorage.setItem("costPeriodMonth", JSON.stringify("2026-08"));
    localStorage.setItem("costFilters", JSON.stringify({ env: ["Production"] }));
  });
  test.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/assets/")) {
      const name = url.pathname.split("/").pop();
      if (stubs[name]) return route.fulfill({ body: stubs[name], contentType: "text/javascript" });
      const response = await page.request.get(origin + url.pathname);
      return route.fulfill({ response });
    }
    if (url.pathname === "/") return route.fulfill({ body: html, contentType: "text/html" });
    let data = {};
    if (url.pathname === "/api/auth/me") {
      identityRequests++;
      if (identityMode === "timeout" || identityMode === "hold") await hold();
      if (identityMode === "failure") return route.fulfill({ status: 503, body: "Fixture identity failure" });
      data = await test.evaluate(() => JSON.parse(localStorage.getItem("fixtureIdentity") || "null")
        || { user: { admin: false }, authenticated: true, required: true });
    } else if (url.pathname === "/api/currency") data = { supported: ["USD", "INR", "EUR"] };
    else if (url.pathname === "/api/reporting-period") data = { month: "2026-08", from: "2026-08-01", to: "2026-08-31", days: 31 };
    else if (/\/dashboard\/(sections|executive)$/.test(url.pathname)) {
      if (costMode === "timeout" || costMode === "hold") await hold();
      if (costMode === "failure") return route.fulfill({ status: 503, body: "Fixture cost failure" });
      data = url.pathname.endsWith("/sections") ? { total: 100, currency: "USD", sections: [] } : {
        currency: "USD", days: 31, kpis: { total: 100, previous: 80, daily_avg: 3, resources: 1, services: 1, regions: 1, subscriptions: 1 },
        areas: [], services: [], subscriptions: [], regions: [], resource_groups: [], movers: [],
        trend: { labels: ["2026-08-01"], values: [100], previous: [80] },
      };
    } else if (url.pathname === "/api/filters") {
      if (filterMode === "timeout") await hold();
      data = { dimensions: {
        service: [{ value: "Storage", cost: 100 }, { value: "Compute", cost: 50 }],
        env: filterMode === "ok" ? [{ value: "Production" }, { value: "Development" }] : [],
      }, unavailable_dimensions: filterMode === "ok" ? [] : ["env"] };
    } else if (url.pathname === "/api/dashboard/billing") {
      statementRequests++;
      data = statement(url.searchParams, statementRevision);
      if (statementMode === "hold") await hold();
      if (statementMode === "failure") return route.fulfill({ status: 503, body: "Fixture statement failure" });
      if (statementMode === "mixed") {
        data.currency = null;
        data.kpis = { billed_cost: null, actual_savings: null };
        data.fx = { complete: false, rates: [{ from: "INR", to: url.searchParams.get("currency"), rate: null }] };
        data.per_currency = [{ currency: "INR", cost: 500 }, { currency: "USD", cost: 100 }];
        data.warnings = ["Native currencies could not all be converted. Aggregate money is unavailable; see per-currency totals."];
        data.breakdowns.subscriptions = [{ name: "India", currency: "INR", cost: 500 }, { name: "US", currency: "USD", cost: 100 }];
      }
    } else if (url.pathname === "/api/dashboard/billing/opportunities") {
      opportunityRequests++;
      const revision = outlookRevision;
      const mode = outlookMode;
      if (mode === "hold") await hold();
      if (mode === "failure") return route.fulfill({ status: 503, body: "Fixture optional failure" });
      data = { status: "ok", opportunities: [], potential: { monthly: revision * 10, annual: revision * 120, currency: "USD" },
        commitments: { items: [], unavailable: [] }, warnings: [] };
    }
    await route.fulfill({ json: data }).catch(() => {});
  });
  const ready = () => test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
  const billingReady = revision => test.waitForFunction(revision =>
    document.documentElement.dataset.dashboardReady === "true"
    && document.querySelector("#billRefresh")?.disabled === false
    && document.querySelector("#billPeriod")?.textContent.includes(`Fixture statement ${revision}`), revision);
  try {
    await test.goto(origin);
    await test.locator("#primary-billing").waitFor();
    check("canonical navigation is available before timed-out costs finish",
      await test.locator("#tabs [role=tab]").count() === 7 && !await test.locator("#viewRetry").count());
    await test.locator("#viewRetry").waitFor();
    await test.locator("#sectionsRetry").waitFor();
    check("timeout is actionable, not endless Loading", (await test.locator("#tabBody").textContent()).includes("timed out"));
    check("failed overview publishes error readiness", await test.evaluate(() => document.documentElement.dataset.dashboardReady === "error"));
    await test.locator("#fbRetry").waitFor();
    check("unavailable environment is explained without dropping its selection",
      (await test.locator("#fbStatus").textContent()).includes("Environment filter choices are temporarily unavailable")
      && await test.evaluate(() => JSON.parse(localStorage.costFilters).env[0] === "Production"));
    check("other dimension remains usable", await test.locator('[data-dim="service"] button').isEnabled());
    await test.locator("#primary-billing").click();
    await billingReady(1);
    check("statement and chart render while optional response is still unresolved", opportunityRequests === 1
      && (await test.locator("#billOpportunities").textContent()).includes("sources load")
      && !!await test.locator("#billMomChart").getAttribute("data-chart"));
    await test.locator('[data-bill-breakdown="services"]').click();
    check("breakdown remains interactive during optional load", (await test.locator("#billBreakdown").textContent()).includes("Storage"));
    statementRevision = 2;
    outlookRevision = 2;
    outlookMode = "ok";
    await test.locator("#billRefresh").click();
    await billingReady(2);
    await test.waitForFunction(() => document.querySelector("#billPotential").textContent.includes("USD 20.00"));
    releaseAll();
    await test.waitForTimeout(100);
    check("superseded optional response cannot overwrite newer results", (await test.locator("#billPotential").textContent()).includes("USD 20.00"));
    outlookMode = "failure";
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => document.querySelector("#billOpportunities").textContent.includes("503"));
    check("optional failure stays local and preserves the statement", (await test.locator("#billPeriod").textContent()).includes("Fixture statement 2")
      && !(await test.locator("#billCoverage").textContent()).includes("503"));
    statementMode = "failure";
    await test.locator("#billRefresh").click();
    await test.waitForFunction(() => document.querySelector("#billCoverage").textContent.includes("Last loaded statement shown"));
    check("statement failure retains previous breakdown and usable refresh", await test.locator("#billRefresh").isEnabled()
      && (await test.locator("#billBreakdown").textContent()).includes("Storage"));
    statementMode = "mixed";
    costMode = "ok";
    await test.locator("#currency").selectOption("");
    await test.waitForFunction(() => document.querySelector("#billSaved").textContent.includes("As billed"));
    check("As billed explains separation without invented FX failure",
      !(await test.locator("#tabBody").textContent()).match(/FX incomplete|complete display-currency conversion is unavailable|INR → Unknown|Native currencies could not all be converted/));
    check("native amounts remain separate", (await test.locator("#billNotes").textContent()).includes("INR 500.00")
      && (await test.locator("#billNotes").textContent()).includes("USD 100.00"));
    await test.locator("#currency").selectOption("EUR");
    await test.waitForFunction(() => document.querySelector("#billSaved").textContent.includes("FX incomplete"));
    check("an explicitly requested failed conversion still warns", (await test.locator("#billCoverage").textContent()).includes("complete display-currency conversion is unavailable"));
    filterMode = "ok";
    await test.locator("#fbRetry").click();
    await test.waitForFunction(() => !document.querySelector("#fbStatus").textContent);
    check("retry restores unavailable dimension choices", await test.locator('[data-dim="env"] button').isEnabled());
    filterMode = "timeout";
    await test.evaluate(async () => {
      const filters = await import("/assets/filters.js");
      void filters.loadFilters(7);
      await filters.loadFilters(7);
    });
    check("filter timeout exposes retry rather than silently disappearing", (await test.locator("#fbStatus").textContent()).includes("could not refresh"));
    filterMode = "ok";
    await test.locator("#fbRetry").click();
    await test.waitForFunction(() => !document.querySelector("#fbStatus").textContent);
    statementMode = "hold";
    statementRevision = 3;
    outlookMode = "hold";
    const before = statementRequests;
    await test.locator("#currency").selectOption("INR");
    await test.waitForFunction(() => document.querySelector("#billRefresh")?.disabled === true);
    while (statementRequests === before) await test.waitForTimeout(10);
    statementMode = "ok";
    statementRevision = 4;
    outlookMode = "ok";
    await test.locator("#currency").selectOption("USD");
    await billingReady(4);
    releaseAll();
    await test.waitForTimeout(100);
    check("cancelled context cannot overwrite new statement", (await test.locator("#billSpent").textContent()).includes("USD 400.00")
      && !await test.locator("#viewRetry").count());
    statementRevision = 5;
    await test.evaluate(() => {
      const month = document.querySelector("#reportingMonth");
      month.value = "2026-07";
      month.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await billingReady(5);
    check("reporting month changes recover independently", (await test.locator("#billPeriod").textContent()).includes("2026-07"));
    statementRevision = 6;
    await test.locator('[data-dim="service"] button').click();
    await test.locator('[data-dim="service"] input[value="Compute"]').check();
    await billingReady(6);
    check("usable filters continue to refresh Billing", await test.evaluate(() => JSON.parse(localStorage.costFilters).service[0] === "Compute"));
    costMode = "hold";
    await test.reload();
    await test.locator("#primary-billing").waitFor();
    costMode = "ok";
    await test.locator("#currency").selectOption("");
    await ready();
    releaseAll();
    await test.waitForTimeout(100);
    check("intentional old-context abort produces no error or stale retry", !await test.locator("#viewRetry").count()
      && !await test.locator("#sectionsRetry").count());
    costMode = "failure";
    await test.locator("#currency").selectOption("USD");
    await test.locator("#sectionsRetry").waitFor();
    await test.locator("#viewRetry").waitFor();
    check("HTTP failures retain navigation", await test.locator("#primary-billing").isEnabled());
    costMode = "ok";
    await test.locator("#sectionsRetry").click();
    await ready();
    check("dashboard Retry recovers costs and clears errors", !await test.locator("#sectionsRetry").count()
      && !await test.locator("#viewRetry").count());
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "audit" })));
    check("admin-only navigation remains gated", !(await test.locator("#tabs").textContent()).includes("Audit log"));
    costMode = "hold";
    await test.reload();
    await test.locator("#primary-billing").click();
    await billingReady(6);
    await test.locator("#sectionsRetry").waitFor();
    check("late cost-menu timeout cannot blank a different usable view",
      await test.locator("#billRefresh").isEnabled()
      && await test.evaluate(() => document.documentElement.dataset.dashboardReady === "true"));
    releaseAll();
    costMode = "ok";
    await test.evaluate(() => localStorage.setItem("fixtureIdentity", JSON.stringify({ required: true, authenticated: false })));
    await test.reload();
    await test.waitForFunction(() => document.querySelector("#currency").options.length > 1);
    check("required authentication is not bypassed to render navigation", await test.locator("#tabs [role=tab]").count() === 0);
    await test.evaluate(() => localStorage.setItem("fixtureIdentity", JSON.stringify({ required: false, authenticated: false })));
    await test.reload();
    await ready();
    check("explicitly auth-disabled deployments still render navigation", await test.locator("#tabs [role=tab]").count() === 7);
    await test.evaluate(() => localStorage.removeItem("fixtureIdentity"));
    for (const failure of ["timeout", "failure"]) {
      const before = identityRequests;
      identityMode = failure;
      await test.reload();
      await test.locator("#viewRetry").waitFor();
      check(`transient identity ${failure} exposes retry without bypassing auth`,
        (await test.locator("#tabBody").textContent()).includes(failure === "timeout" ? "timed out" : "503")
        && await test.locator("#tabs [role=tab]").count() === 0 && identityRequests === before + 1);
      releaseAll();
      identityMode = "hold";
      const retryRequested = test.waitForRequest(request => new URL(request.url()).pathname === "/api/auth/me");
      await test.locator("#viewRetry").click();
      await retryRequested;
      const shared = await test.evaluate(async () => {
        const { whoAmI } = await import("/assets/app.js");
        const first = whoAmI(), second = whoAmI();
        first.catch(() => {});
        second.catch(() => {});
        return first === second;
      });
      identityMode = "ok";
      releaseAll();
      await ready();
      check(`identity ${failure} retry refetches once and shares concurrent calls`,
        shared && identityRequests === before + 2
        && await test.locator("#tabs [role=tab]").count() === 7
        && !(await test.locator("#tabs").textContent()).includes("Audit log"));
    }
    check("no unhandled browser errors", errors.length === 0);
    return { checks, errors, fixtures: true };
  } catch (error) {
    throw new Error(`${error.message}\nChecks: ${checks.join("; ")}\nBrowser errors: ${errors.join("; ")}\nBody: ${(await test.locator("body").innerText()).slice(0, 2000)}`);
  } finally {
    releaseAll();
    await context.close();
  }
}
