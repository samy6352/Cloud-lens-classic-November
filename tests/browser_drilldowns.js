async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before drilldown checks.");
  const param = (url, name) => decodeURIComponent(url.match(new RegExp(`[?&]${name}=([^&]*)`))?.[1] || "");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(), viewport: { width: 1366, height: 900 },
    reducedMotion: "reduce",
  });
  const test = await context.newPage(), failures = [], errors = [], requests = [];
  const check = (label, pass) => { if (!pass) failures.push(label); };
  const ready = () => test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
  const home = async () => {
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "overview" })));
    await ready();
    await test.locator("#overviewHome").waitFor();
  };
  const estate = async () => {
    if (!await test.locator("#overviewEstate").evaluate(node => node.open)) {
      await test.locator("#overviewEstate > summary").click();
    }
  };
  const currentMonth = new Date().toISOString().slice(0, 7);
  const monthOffset = offset => {
    const date = new Date(`${currentMonth}-01T00:00:00Z`);
    date.setUTCMonth(date.getUTCMonth() + offset);
    return date.toISOString().slice(0, 7);
  };
  const subscriptions = [{id:"sub-a",name:"Alpha",in_warehouse:true},{id:"sub-b",name:"Beta",in_warehouse:true}];
  const services = [{name:"Compute",cost:210,previous:140,delta:70,share_pct:100}];
  const executive = {
    days:30,currency:"USD",as_of:`${currentMonth}-09`,
    kpis:{total:210,previous:140,change_pct:50,daily_avg:7,resources:2,services:1,regions:3,subscriptions:2},
    areas:[{id:"compute",label:"Compute",cost:210,previous:140,delta:70,share_pct:100}],
    services,movers:services,resource_groups:[{name:"Demo",cost:210,previous:140,delta:70,share_pct:100}],
    subscriptions:[{name:"Alpha",cost:180,previous:120,delta:60,share_pct:86},
      {name:"Beta",cost:30,previous:20,delta:10,share_pct:14}],
    regions:[{name:"East US 2",cost:180,previous:120,delta:60,share_pct:86,resources:1},
      {name:"East US",cost:20,previous:15,delta:5,share_pct:9,resources:1},
      {name:"Global",cost:10,previous:5,delta:5,share_pct:5,resources:0}],
    trend:{labels:[1,2,3].map(day=>`${currentMonth}-0${day}`),values:[60,70,80],previous:[40,50,50]},
    monthly:{months:[monthOffset(-2),monthOffset(-1),currentMonth],currency:"USD",
      areas:[{id:"compute",label:"Compute",data:[90,140,210]}]},
  };
  test.on("pageerror", error => errors.push(error.message));
  let phase = "loading";
  try {
    await context.addInitScript(() => {
      localStorage.setItem("costScope","[]");
      localStorage.setItem("costDays","30");
      localStorage.setItem("costPeriodMonth",'""');
      localStorage.setItem("costCurrency",'"USD"');
      localStorage.setItem("costFilters",'{"service":["Compute"]}');
    });
    await context.route("**/api/**", route => {
      const request = route.request(), url = request.url().replace(/^https?:\/\/[^/]+/, "");
      const pathname = url.split("?")[0];
      requests.push(url);
      if (request.method() !== "GET") {
        failures.push(`Unexpected write: ${request.method()} ${pathname}`);
        return route.abort();
      }
      const data = {
        "/api/auth/me":{required:false,authenticated:true,user:{name:"Demo",admin:false}},
        "/api/subscriptions":{subscriptions},
        "/api/currency":{supported:["USD"]},
        "/api/overview":{currency:"USD",month_to_date:318,last_month:1605,forecast_30d:1337},
        "/api/health":{status:"ok",model:"fixture"},
        "/api/warehouse":{rows:100,from:`${currentMonth}-01`,to:`${currentMonth}-09`},
        "/api/dashboard/sections":{total:210,currency:"USD",sections:executive.areas},
        "/api/dashboard/executive":executive,
        "/api/dashboard/anomalies":{currency:"USD",days:30,anomalies:[]},
        "/api/anomalies/alerts":{alerts:[]},
        "/api/budgets":{budgets:[]},
        "/api/dashboard/savings":{count:0,total:0},
        "/api/dashboard/savings/progress":{opportunities:[],scan:{status:"done",completed:[],total:4}},
        "/api/dashboard/resources":{days:30,currency:"USD",resources:[]},
        "/api/filters":{dimensions:{service:[{value:"Compute"},{value:"Storage"}],
          region:executive.regions.map(row=>({value:row.name,cost:row.cost}))}},
        "/api/reporting-period":{from:`${currentMonth}-01`,to:`${currentMonth}-09`,month:param(url,"period_month")},
      };
      return route.fulfill({json:data[pathname] || {}});
    });
    await test.goto(origin);
    await ready();
    await test.waitForFunction(() => document.querySelector("#glance .tile")?.textContent.includes("318"));
    check("all eleven KPI cards are native buttons", await test.locator("#glance button.tile, .overview-summary button.kpi").count() === 11);
    check("no nested buttons", await test.locator(".kpi button").count() === 0);
    const tiles = test.locator("#glance .tile");
    phase = "header months";
    await tiles.nth(0).click(); await ready();
    check("month-to-date chooses the current month", await test.locator("#reportingMonth").inputValue() === currentMonth);
    check("month-to-date focuses the daily chart", await test.evaluate(() => document.activeElement.id === "trendChart"));
    await tiles.nth(1).focus(); await test.keyboard.press("Enter"); await ready();
    check("last month chooses the previous calendar month", await test.locator("#reportingMonth").inputValue() === monthOffset(-1));
    check("header navigation preserves dimension filters", await test.evaluate(() => JSON.parse(localStorage.getItem("costFilters")).service[0] === "Compute"));
    phase = "forecast";
    await tiles.nth(2).focus(); await test.keyboard.press("Space");
    await test.locator("dialog[open]").waitFor();
    // `dialog[open]`: the shell always carries the closed tab-search dialog as well.
    check("forecast opens the exact supplied estimate and source", /1,337/.test(await test.locator("dialog[open]").innerText())
      && (await test.locator("dialog[open]").innerText()).includes("Azure Cost Management"));
    await test.keyboard.press("Escape");
    check("closing forecast returns focus to its tile", await tiles.nth(2).evaluate(node => document.activeElement === node));
    phase = "month comparison and destination tabs";
    await tiles.nth(3).click(); await ready();
    check("month comparison opens the monthly chart disclosure", await test.locator("#overviewPatterns").evaluate(node => node.open)
      && await test.evaluate(() => document.activeElement.id === "monthOverMonth"));
    await tiles.nth(4).click(); await ready();
    check("budget opens Budgets", await test.locator("#tabBody").getAttribute("aria-label") === "Budgets");
    await tiles.nth(5).click(); await ready();
    check("savings opens Savings", await test.locator("#tabBody").getAttribute("aria-label") === "Savings");
    await home();
    phase = "Overview KPIs";
    for (const [index, target] of [[0,"Top services"],[1,"Biggest movers"],[2,"Daily spend"]]) {
      await test.locator(".overview-summary .kpi").nth(index).focus();
      await test.keyboard.press(index === 1 ? "Space" : "Enter");
      check(`Overview metric ${index} reveals its detail`, await test.evaluate(target =>
        document.activeElement.textContent.includes(target), target));
    }
    const executiveReads = requests.filter(url => url.startsWith("/api/dashboard/executive")).length;
    await test.locator(".overview-summary .kpi").nth(3).click(); await ready();
    check("billed resources opens resource explorer", await test.locator("#resourceExplorer").count() === 1);
    await home();
    await test.locator(".overview-summary .kpi").nth(4).focus(); await test.keyboard.press("Space"); await ready();
    check("entire anomaly tile navigates", await test.locator("#tabBody").getAttribute("aria-label") === "Anomalies");
    await home();
    check("cached drilldowns do not refetch Overview", requests.filter(url => url.startsWith("/api/dashboard/executive")).length === executiveReads);
    phase = "subscription hierarchy";
    await estate();
    await test.locator("#spendTreemap button.tm-tile").first().waitFor();
    await test.locator("#spendTreemap button.tm-tile").first().focus(); await test.keyboard.press("Enter"); await ready();
    check("subscription tile scopes resource navigation by ID", requests.some(url =>
      url.startsWith("/api/dashboard/resources?") && param(url,"scope") === "sub-a"));
    check("subscription selection is visible and persisted", (await test.locator("#pickerLabel").innerText()).includes("Alpha"));
    await home();
    phase = "regions";
    await estate();
    await test.locator('.rg-pt[data-region="East US 2"]').waitFor();
    await test.locator('.rg-pt[data-region="East US 2"]').focus(); await test.keyboard.press("Space"); await ready();
    await test.locator("#resourceExplorer").waitFor();
    check("SVG region keyboard activation uses the canonical region name", requests.some(url =>
      url.startsWith("/api/dashboard/resources?") && param(url,"region") === "East US 2"));
    check("region drilldown preserves the subscription and service", await test.evaluate(() =>
      JSON.parse(localStorage.getItem("costScope"))[0] === "sub-a"
      && JSON.parse(localStorage.getItem("costFilters")).service[0] === "Compute"));
    await home();
    await estate();
    await test.locator('button.rg-leg-item[data-region="Global"]').click(); await ready();
    await test.locator("#resourceExplorer").waitFor();
    check("unmapped/global regions have navigable legend entries", requests.some(url =>
      url.startsWith("/api/dashboard/resources?") && param(url,"region") === "Global"));
    await home();
    phase = "responsive keyboard targets";
    for (const width of [390,1366,1920]) {
      await test.setViewportSize({width,height:900});
      await test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      check(`no horizontal page overflow at ${width}`, await test.evaluate(() =>
        document.documentElement.scrollWidth <= innerWidth + 1
        && document.querySelector("#tabBody").scrollWidth <= document.querySelector("#tabBody").clientWidth + 1));
    }
    phase = "aggregate and off-map chart controls";
    await estate();
    await test.evaluate(async () => {
      const {drawTreemap} = await import("/assets/treemap.js");
      const {drawRegionMap} = await import("/assets/regionmap.js");
      window.chartPicks = [];
      drawTreemap(document.querySelector("#spendTreemap"),
        [{name:"Largest",cost:100},{name:"Small A",cost:2},{name:"Small B",cost:1}], "USD",
        {limit:1,onPick:rows=>window.chartPicks.push(rows.map(row=>row.name))});
      drawRegionMap(document.querySelector("#regionMap"),[{name:"Global",cost:5}], "USD",
        {onPick:row=>window.chartPicks.push(row.name)});
    });
    await test.locator("button.tm-rolled").focus(); await test.keyboard.press("Space");
    check("aggregate tile passes its actual members", await test.evaluate(() =>
      JSON.stringify(window.chartPicks[0]) === '["Small A","Small B"]'));
    await test.locator("button.rg-leg-item").focus(); await test.keyboard.press("Enter");
    check("a map with no plotted regions still offers the global row", await test.evaluate(() => window.chartPicks[1] === "Global"));
    await test.locator("button.tm-rolled").focus();
    await test.setViewportSize({width:1500,height:900});
    await test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await test.waitForFunction(() => document.activeElement.classList.contains("tm-rolled"));
    check("resize preserves the focused hierarchy tile", await test.locator("button.tm-rolled").evaluate(node => document.activeElement === node));
    check("no browser errors", errors.length === 0);
    return {failures,errors,requests:requests.filter(url => /executive|resources/.test(url))};
  } catch (error) {
    return {failure:error.message,phase,failures,errors};
  } finally { await context.close(); }
}
