async (page) => {
  // Serve the local web directory on port 8021; all API responses are mocked below.
  const source = "http://127.0.0.1:8021";
  const context = await page.context().browser().newContext({
    viewport: { width: 1440, height: 1000 }, timezoneId: "America/Los_Angeles",
  });
  let reading = Promise.resolve();
  const sources = new Map();
  const read = path => {
    if (!sources.has(path)) {
      reading = reading.then(async () => {
        const response = await context.request.get(source + path);
        if (!response.ok()) throw new Error(`Could not load local source: ${path}`);
        return response.body();
      });
      sources.set(path, reading);
    }
    return sources.get(path);
  };
  const test = await context.newPage();
  await test.clock.install({ time: new Date("2026-09-07T13:40:00Z") });
  const errors = [], requests = [];
  test.on("pageerror", error => errors.push(error.message));
  const appStub = `
    export const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    export const money=(v,c='USD')=>c+' '+Number(v||0).toFixed(2),moneyHtml=money;
    export const get=async(url,options={})=>{const r=await fetch(url,{signal:options.signal});if(!r.ok)throw new Error('Fixture request failed');return r.json()};
    export const freshReads=async fn=>fn();
    export const post=async()=>({});
    export const scopeIds=()=>['fixture-sub'],allSubscriptions=()=>[{id:'fixture-sub',name:'Fixture'}],selectSubscriptions=()=>true;
    export const whoAmI=async()=>({authenticated:true,user:{admin:false}});
    export const onScopeChange=()=>{},askAgent=()=>{},drawChart=async()=>null,refreshHeader=()=>{},setGlanceSpark=()=>{};
    export const readStore=(key,fallback)=>{const v=localStorage.getItem(key);return v?JSON.parse(v):fallback};
    export const writeStore=(key,value)=>localStorage.setItem(key,JSON.stringify(value));
  `;
  const stubs = {
    "app.js": appStub,
    "analysis.js": "export const close=()=>{},invalidate=()=>{},isOpen=()=>false,openAnalysis=()=>{},setAnalysisContext=()=>{},wireAnalysis=()=>{};",
    "bot.js": "export const mountBot=()=>{},setBotState=()=>{};",
  };
  await context.route("**/*", async route => {
    const [base, query = ""] = route.request().url().split("?");
    const params = Object.fromEntries(query.split("&").filter(Boolean).map(pair => {
      const [key, value = ""] = pair.split("=");
      return [key, decodeURIComponent(value)];
    }));
    const url = { pathname: base.replace(/^https?:\/\/[^/]+/, ""),
      search: query ? `?${query}` : "", searchParams: { get: key => params[key] || null } };
    requests.push(url.pathname + url.search);
    if (url.pathname === "/") return route.fulfill({
      contentType: "text/html", body: await read("/index.html"),
    });
    if (url.pathname.startsWith("/assets/")) {
      const name = url.pathname.split("/").pop();
      return route.fulfill({
        body: stubs[name] ?? await read(url.pathname),
        contentType: name.endsWith(".js") ? "text/javascript"
          : name.endsWith(".css") ? "text/css" : "image/svg+xml",
      });
    }
    let data = {};
    if (url.pathname === "/api/reporting-period") {
      const month = url.searchParams.get("period_month");
      const days = Number(url.searchParams.get("days"));
      const end = month ? new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5)), 0))
        : new Date("2026-09-07T00:00:00Z");
      data = { month, days, from: month ? month + "-01"
        : new Date(end.getTime() - (days - 1) * 86400000).toISOString().slice(0, 10),
        to: end.toISOString().slice(0, 10), empty: true };
    } else if (url.pathname === "/api/currency") data = { supported: ["USD"] };
    else if (url.pathname.endsWith("/sections")) data = { total: 0, sections: [], currency: "USD" };
    else if (url.pathname.endsWith("/executive")) data = { empty: true };
    else if (url.pathname === "/api/filters") data = { dimensions: {} };
    return route.fulfill({ json: data });
  });
  const assert = (value, message) => { if (!value) throw new Error(message); };
  try {
    await test.goto("http://cloudlens-period.test/");
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    await test.locator("#periodRange").filter({ hasText: "Last 30 days" }).waitFor();
    const rolling = await test.locator("#periodRange").innerText();
    assert(rolling.includes("Aug") && rolling.includes("Sep"), "Rolling range must name both months");
    assert(await test.locator("#reportingMonth").inputValue() === "2026-09", "Calendar opens on the current month");
    assert(await test.locator("#reportingMonth").isHidden(), "Rolling mode must not imply a calendar month filter");
    assert((await test.locator("#reportingRange").textContent()).includes("Aug"), "Rolling field shows actual start date");
    assert(await test.locator("#reportingMonth").getAttribute("data-period-mode") === "rolling",
      "Calendar suggestion does not silently switch the active rolling filter");
    assert(requests.filter(url => url.includes("/sections?")).every(url => !url.includes("period_month=")),
      "Default calendar display does not add a calendar filter to rolling requests");
    await test.getByRole("button", { name: "Choose reporting month", exact: true }).click();
    assert(await test.locator('#monthCalendar [aria-pressed="true"]').count() === 0,
      "Rolling mode does not mark September as an active month filter");
    await test.getByRole("button", { name: "August 2026", exact: true }).click();
    await test.locator("#periodRange").filter({ hasText: "August 2026" }).waitFor();
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    assert(await test.locator("#reportingRange").isHidden() && await test.locator("#reportingMonth").isVisible(),
      "Selecting a month replaces the rolling range display");
    assert(requests.some(url => url.includes("/sections?") && url.includes("period_month=2026-08")), "Month missing from dashboard");
    assert(requests.some(url => url.includes("/api/filters?") && url.includes("period_month=2026-08")), "Month missing from dimensions");
    const link = await test.locator("#exportMenu a[data-fmt]").first().getAttribute("href");
    assert(link.includes("period_month=2026-08"), "Month missing from report download");
    await test.reload();
    await test.locator("#periodRange").filter({ hasText: "August 2026" }).waitFor();
    assert(await test.getByLabel("Calendar month", { exact: true }).inputValue() === "2026-08", "Month must survive reload");
    await test.getByLabel("Calendar month", { exact: true }).fill("2024-02");
    await test.locator("#periodRange").filter({ hasText: "February 2024" }).waitFor();
    assert((await test.locator("#periodRange").innerText()).includes("29"), "Leap-year month must include February 29");
    await test.locator("#period [data-days='30']").click();
    await test.locator("#periodRange").filter({ hasText: "Last 30 days" }).waitFor();
    assert(await test.getByLabel("Calendar month", { exact: true }).inputValue() === "2026-09",
      "Rolling preset keeps a current-month suggestion for the picker");
    assert(await test.locator("#period [data-days='30']").getAttribute("aria-pressed") === "true", "Rolling selection not announced");
    for (const [days, start] of [[60, "Jul"], [90, "Jun"], [7, "Sep"], [30, "Aug"]]) {
      await test.locator(`#period [data-days='${days}']`).click();
      await test.locator("#periodRange").filter({ hasText: `Last ${days} days` }).waitFor();
      assert((await test.locator("#reportingRange").textContent()).includes(start),
        `${days}d field reflects dates from the API`);
      assert(await test.locator("#reportingMonth").isHidden(), "No misleading month-only display");
    }
    await test.locator("#reportingRange").click();
    await test.getByRole("button", { name: "September 2026", exact: true }).click();
    await test.locator("#periodRange").filter({ hasText: "September 2026" }).waitFor();
    assert(await test.locator("#period .on").count() === 0, "Choosing default September explicitly activates month mode");
    await test.locator("#period [data-days='60']").click();
    await test.locator("#periodRange").filter({ hasText: "Last 60 days" }).waitFor();
    const sixtyRange = await test.locator("#reportingRange").textContent();
    assert(sixtyRange.includes("10") && sixtyRange.includes("Jul") && sixtyRange.includes("Sep"),
      "60d matches the screenshot's 10 July through 7 September");
    await test.reload();
    await test.locator("#periodRange").filter({ hasText: "Last 60 days" }).waitFor();
    assert(await test.locator("#reportingRange").textContent() === sixtyRange, "Rolling range is restored on reload");
    await test.setViewportSize({ width: 390, height: 844 });
    await test.locator(".reporting-controls").scrollIntoViewIfNeeded();
    assert(await test.locator("#period").isVisible(), "Rolling controls must remain available on mobile");
    assert(await test.locator("#reportingRange").isVisible(), "Rolling range remains available on mobile");
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      await test.getByRole("button", { name: "Choose reporting month", exact: true }).click();
      const themed = await test.evaluate(() => {
        const popup = document.querySelector(".month-calendar");
        const header = document.querySelector(".bar");
        const rect = popup.getBoundingClientRect();
        return { match: getComputedStyle(popup).backgroundColor === getComputedStyle(header).backgroundColor,
          inside: rect.left >= 0 && rect.right <= innerWidth };
      });
      assert(themed.match && themed.inside, `Calendar theme/layout failed: ${theme}`);
      await test.keyboard.press("Escape");
      assert(await test.locator("#monthCalendar").isHidden(), "Escape must dismiss calendar");
    }
    const overflow = await test.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert(!overflow, "Period controls overflow mobile layout");
    await test.evaluate(() => {
      const currency = document.getElementById("currency");
      currency.innerHTML = '<option value="">As billed</option><option value="USD">USD</option>';
      currency.hidden = false;
      document.getElementById("refresh").hidden = false;
      document.getElementById("refreshLabel").textContent = "Refresh \u00b7 to 7 Sept";
      document.getElementById("pickerLabel").textContent = "All subscriptions (3)";
    });
    const layouts = [];
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const width of [1600, 1440, 1200, 390, 320]) {
        await test.setViewportSize({ width, height: 900 });
        const layout = await test.evaluate(() => {
          const selectors = ["#period", ".reporting-month", "#currency", "#pickerBtn", "#exportBtn", "#refreshBtn", "#themeBtn"];
          const rects = selectors.map(selector => document.querySelector(selector).getBoundingClientRect()).filter(r => r.height > 0);
          const refresh = document.getElementById("refreshLabel").getBoundingClientRect();
          return { width: innerWidth, theme: document.documentElement.dataset.theme,
            heights: rects.map(r => r.height), tops: rects.map(r => r.top),
            refreshSingleLine: refresh.height <= 18.5,
            overflow: document.documentElement.scrollWidth > innerWidth };
        });
        assert(layout.heights.every(height => Math.abs(height - 36) < 1),
          `Header controls have a uniform 36px height: ${JSON.stringify(layout)}`);
        assert(width <= 1200 || Math.max(...layout.tops) - Math.min(...layout.tops) < 1,
          `Desktop header controls align on the same row: ${JSON.stringify(layout)}`);
        assert(!layout.overflow, `Header does not overflow: ${JSON.stringify(layout)}`);
        assert(layout.refreshSingleLine, `Refresh label does not wrap: ${JSON.stringify(layout)}`);
        layouts.push(layout);
      }
    }
    assert(errors.length === 0, errors.join("\n"));
    return { rolling, monthSelection: true, leapYear: true, persisted: true, mobile: true,
      defaultMonth: "2026-09", uniformLayouts: layouts.length, errors };
  } finally {
    await context.close();
  }
}
