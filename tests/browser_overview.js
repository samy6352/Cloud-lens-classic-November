async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before Overview checks.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(), viewport: { width: 1600, height: 1000 },
    reducedMotion: "reduce",
  });
  const test = await context.newPage(), failures = [], errors = [], layouts = [], assets = [];
  let executiveRequests = 0, analysisRequests = 0;
  let phase = "opening Overview";
  const check = (name, pass) => { if (!pass) failures.push(name); };
  const services = [
    {name:"Virtual Machines",cost:147,previous:98,delta:49,share_pct:70},
    {name:"Storage",cost:63,previous:42,delta:21,share_pct:30},
  ];
  const data = {
    days:7,as_of:"2026-09-07",currency:"USD",
    kpis:{total:210,previous:140,change_pct:50,daily_avg:30,resources:2,services:2,regions:1,subscriptions:1,top5_share_pct:100},
    trend:{labels:["2026-09-01","2026-09-02","2026-09-03","2026-09-04","2026-09-05","2026-09-06","2026-09-07"],
      values:[20,40,20,40,20,60,10],previous:[20,20,20,20,20,20,20],
      previous_labels:["2026-08-25","2026-08-26","2026-08-27","2026-08-28","2026-08-29","2026-08-30","2026-08-31"],partial_last:true},
    monthly:{months:["2026-06","2026-07","2026-08","2026-09"],currency:"USD",
      areas:[{id:"compute",label:"Compute & Web",data:[100,120,140,210]}]},
    areas:[{id:"compute",label:"Compute & Web",cost:210,previous:140,delta:70,share_pct:100}],
    services,movers:services,subscriptions:[{name:"Demo",cost:210,previous:140,delta:70,share_pct:100}],
    regions:[{name:"eastus",cost:210,previous:140,delta:70,share_pct:100,resources:2}],
    resource_groups:[{name:"demo",cost:210,previous:140,delta:70,share_pct:100}],
  };
  test.on("pageerror", error => errors.push(error.message));
  test.on("request", request => { if (request.url().includes("/assets/")) assets.push(request.url()); });
  const ready = () => test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true", null, {timeout:120000});
  try {
    await context.addInitScript(() => {
      localStorage.setItem("costDays","7");
      localStorage.setItem("costPeriodMonth",'""');
      localStorage.setItem("costCurrency",'""');
    });
    await context.route("**/api/dashboard/executive?**", route => {
      executiveRequests++;
      return route.fulfill({json:data});
    });
    await context.route("**/api/dashboard/resources?**", route => route.fulfill({json:{resources:[],days:7}}));
    await context.route("**/api/insights?**", route => {
      analysisRequests++;
      return route.fulfill({json:{days:7,currency:"USD",findings:[],summary:{headline:"No recommendations in fixture"}}});
    });
    await test.goto(origin);
    await ready();
    check("five aligned summary metrics", await test.locator(".overview-summary .kpi").count() === 5);
    check("no automatic analysis", analysisRequests === 0);
    check("one executive read", executiveRequests === 1);
    check("estate modules are deferred", !assets.some(url => /\/(?:regionmap|treemap)\.js$/.test(url)));
    check("detailed sections start closed", await test.locator(".overview-detail[open]").count() === 0);
    check("data-backed signals preserve amounts and names", (await test.locator(".overview-focus").innerText()).includes("49.00")
      && (await test.locator(".overview-focus").innerText()).includes("Virtual Machines"));
    // Scoped to the Cost drivers panel: the summary KPI row now offers the same jump, so
    // `#overviewHome [data-overview-jump=...]` matches two buttons and Playwright refuses it.
    await test.locator('.overview-focus [data-overview-jump="Biggest movers"]').click();
    check("driver link opens and focuses its breakdown", await test.evaluate(() =>
      document.querySelector("#overviewBreakdowns").open && document.activeElement.textContent === "Biggest movers"));
    check("breakdown navigation reuses data", executiveRequests === 1);
    phase = "opening estate disclosure";
    await test.locator("#overviewEstate > summary").click();
    await test.locator("#spendTreemap h3").waitFor();
    check("estate charts load on expansion", assets.some(url => url.endsWith("/treemap.js"))
      && assets.some(url => url.endsWith("/regionmap.js")));
    phase = "cached navigation";
    await test.locator('#overviewHome [data-overview-tab="resources"]').click();
    await ready();
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab",{detail:"overview"})));
    await ready();
    check("cached Overview retains both disclosures", await test.locator(".overview-detail[open]").count() === 2 && executiveRequests === 1);
    phase = "opening Analysis";
    await test.locator("#overviewAnalysis").click();
    await test.locator("#analysisBody .an-clear").waitFor();
    check("AI shortcut opens Analysis on demand", analysisRequests === 1 && await test.locator("#analysisView").isVisible());
    await test.locator("#analysisClose").click();
    phase = "closing estate after Analysis";
    await test.locator("#overviewEstate > summary").click();
    await test.locator("#overviewBreakdowns > summary").click();
    await test.locator("#trendChart").scrollIntoViewIfNeeded();
    await test.waitForFunction(() => {
      const canvas=document.querySelector("#trendChart canvas");
      return canvas && window.Chart?.getChart(canvas);
    });
    const trend = await test.evaluate(() => {
      const chart=Chart.getChart(document.querySelector("#trendChart canvas"));
      return {labels:chart.data.labels,values:chart.data.datasets.map(set=>set.data)};
    });
    check("partial day stays out of the chart, not the total", trend.labels.length === 6
      && trend.values[0].join(",") === "20,40,20,40,20,60"
      && (await test.locator(".overview-summary").textContent()).includes("210.00")
      && (await test.locator("#trendChart .chart-note").textContent()).includes("included in totals"));
    phase = "opening spend patterns";
    // Month-over-month sits inside the "Spend patterns" disclosure, which is closed by
    // default -- that row is the tallest thing on the page and was pushing the figures people
    // come here for below the fold. Closed means never drawn, because whenNear cannot fire on
    // a card that is not displayed, so the chart has to be revealed before it can be asserted.
    await test.locator("#overviewPatterns > summary").click();
    await test.locator("#monthOverMonth").scrollIntoViewIfNeeded();
    await test.waitForFunction(() => {
      const canvas=document.querySelector("#monthOverMonth canvas");return canvas && window.Chart?.getChart(canvas);
    });
    check("monthly chart still shows only the latest three months", await test.evaluate(() =>
      Chart.getChart(document.querySelector("#monthOverMonth canvas")).data.labels.join(",")==="2026-07,2026-08,2026-09"));
    phase = "responsive layouts";
    for (const theme of ["light","dark","vivid"]) {
      await test.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      for (const width of [1600,1200,800,390,320]) {
        await test.setViewportSize({width,height:1000});
        await test.locator("#tabBody").evaluate(body=>{body.scrollTop=0;});
        await test.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        const layout=await test.evaluate(()=>{
          const cards=[...document.querySelectorAll(".overview-summary .kpi")].map(node=>node.getBoundingClientRect());
          const chart=document.querySelector("#trendChart").getBoundingClientRect();
          const focus=document.querySelector(".overview-focus").getBoundingClientRect();
          const canvas=document.querySelector("#trendChart canvas").getBoundingClientRect();
          const body=document.querySelector("#tabBody");
          const bounds=body.getBoundingClientRect();
          return {width:innerWidth,overflow:body.scrollWidth>body.clientWidth+1||document.documentElement.scrollWidth>innerWidth+1,
            overflowNodes:body.scrollWidth>body.clientWidth+1?[...body.querySelectorAll("*")].filter(node=>{
              const r=node.getBoundingClientRect();return r.width>0&&(r.right>bounds.right+1||r.left<bounds.left-1);
            }).slice(0,8).map(node=>({tag:node.tagName,cls:node.className,width:node.getBoundingClientRect().width})):[],
            metricsAligned:(()=>{
              // Compared within each row, not against the first tile on the page. Below 620px
              // the grid is two columns, where a fifth tile would sit half-width beside a gap,
              // so it deliberately spans the row (.kpis > .kpi:last-child:nth-child(odd)).
              // That is alignment rather than a mismatch; what must never happen is a tile
              // that is ragged against the ones beside it.
              const rows=new Map();
              for(const card of cards){
                const key=Math.round(card.top);
                if(!rows.has(key)) rows.set(key,[]);
                rows.get(key).push(card);
              }
              return [...rows.values()].every(row=>row.every(card=>
                Math.abs(card.width-row[0].width)<2&&Math.abs(card.height-row[0].height)<2));
            })(),
            primaryAligned:Math.abs(chart.top-focus.top)>2||Math.abs(chart.height-focus.height)<2,
            chartClipped:canvas.bottom>chart.bottom+2,
            buttonsFit:[...document.querySelectorAll(".overview-actions button")].every(button=>{
              const rect=button.getBoundingClientRect();return rect.left>=0&&rect.right<=innerWidth+1
                &&rect.height>=35.5&&parseFloat(getComputedStyle(button).minHeight)>=36;
            })};
        });
        layouts.push({theme,...layout});
        check(`${theme}/${width}: coherent responsive layout`, !layout.overflow&&layout.metricsAligned&&layout.primaryAligned&&!layout.chartClipped&&layout.buttonsFit);
      }
    }
    check("layout and disclosure changes do not refetch", executiveRequests===1&&analysisRequests===1);
    return {failures,errors,layouts,executiveRequests,analysisRequests};
  } catch(error) {
    const geometry=[];
    for(let i=0;i<3;i++){
      await test.waitForTimeout(150);
      geometry.push(await test.evaluate(()=>Object.fromEntries(
        ["overviewHome","trendChart","monthOverMonth","overviewBreakdowns","overviewEstate"].map(id=>{
          const node=document.getElementById(id),rect=node?.getBoundingClientRect();
          return[id,rect?{top:rect.top,height:rect.height}:null];
        }))));
    }
    return {failure:error.message,phase,failures,errors,geometry};
  } finally { await context.close(); }
}
