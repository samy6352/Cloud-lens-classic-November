async (page) => {
  const origin=page.url().match(/^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?/)?.[0];
  if(!origin) throw new Error("Open the local CloudLens app before tab search checks.");
  const context=await page.context().browser().newContext({
    storageState:await page.context().storageState(),viewport:{width:1920,height:1000},reducedMotion:"reduce",
  });
  const test=await context.newPage(), errors=[], reads=[], layouts=[];
  test.setDefaultTimeout(10000);
  let passed=0, phase="opening", email="searcher@example.test";
  const check=(ok,label)=>{if(!ok)throw new Error(label);passed++;};
  const ready=()=>test.waitForFunction(()=>document.documentElement.dataset.dashboardReady==="true",null,{timeout:60000});
  const open=async()=>{await test.locator("#tabSearchBtn").click();await test.locator("#tabSearchDialog").waitFor();};
  const input=test.locator("#tabSearchInput");
  test.on("pageerror",error=>errors.push(error.message));
  const executive={days:7,currency:"USD",kpis:{total:210,previous:140,change_pct:50,daily_avg:30,
    resources:2,services:1,regions:1,subscriptions:1},areas:[],services:[],subscriptions:[],regions:[],resource_groups:[],
    movers:[],trend:{labels:[],values:[],previous:[]}};
  try {
    await context.addInitScript(()=>{
      if(sessionStorage.getItem("searchTestStarted"))return;
      sessionStorage.setItem("searchTestStarted","true");
      for(const key of Object.keys(localStorage))if(key.startsWith("cloudlens.tabSearch:"))localStorage.removeItem(key);
      localStorage.setItem("costScope",'["sub-a"]');localStorage.setItem("costDays","7");
      localStorage.setItem("costPeriodMonth",'""');localStorage.setItem("costCurrency",'"USD"');
      localStorage.setItem("costFilters",'{"service":["Compute"]}');
      localStorage.setItem("railLayout",'"grouped"');
    });
    await context.route("**/api/**",route=>{
      const request=route.request(), url=request.url().replace(/^https?:\/\/[^/]+/,""),path=url.split("?")[0];
      if(request.method()!=="GET")throw new Error(`Unexpected write: ${path}`);
      if(/\/api\/dashboard\/(sections|executive|resources)/.test(path))reads.push(url);
      const responses={
        "/api/auth/me":{required:false,authenticated:true,user:{name:"Search tester",email,source:"entra",admin:false}},
        "/api/subscriptions":{subscriptions:[{id:"sub-a",name:"Demo",in_warehouse:true},{id:"sub-b",name:"Other",in_warehouse:true}]},
        "/api/overview":{currency:"USD",month_to_date:210,last_month:140,forecast_30d:240},
        "/api/dashboard/sections":{total:210,currency:"USD",sections:[{id:"compute",label:"Compute & Web",cost:200},
          {id:"storage",label:"Storage",cost:10}],aks:{cost:1,currency:"USD"}},
        "/api/dashboard/executive":executive,
        "/api/dashboard/anomalies":{anomalies:[],currency:"USD"},
        "/api/dashboard/resources":{resources:[],days:7},
        "/api/dashboard/tags":{keys:[],resources:[],days:7},
        "/api/dashboard/savings":{count:0,total:0},
        "/api/filters":{dimensions:{service:[{value:"Compute"},{value:"Storage"}]}},
        "/api/reporting-period":{from:"2026-09-01",to:"2026-09-07",days:7},
        "/api/currency":{supported:["USD"]},
        "/api/warehouse":{rows:100,from:"2026-09-01",to:"2026-09-07"},
        "/api/health":{status:"ok",model:"fixture"},
      };
      return route.fulfill({json:responses[path]||{}});
    });
    await test.goto(origin);await ready();
    check(await test.locator("#tabSearchBtn").isEnabled(),"search is enabled once available tabs load");
    check(await test.evaluate(()=>{
      const search=document.querySelector("#tabSearchBtn").getBoundingClientRect();
      const day=document.querySelector('[data-days="7"]').getBoundingClientRect();
      return search.right<=day.left && day.left-search.right<=12 && Math.abs(search.top-day.top)<=4;
    }),"small magnifier is immediately left of 7d");
    check((await test.locator("#tabSearchBtn").textContent()).trim()===""
      && await test.locator("#tabSearchBtn svg").count()===1,"only the magnifier is visible, not a search field or label");
    await test.locator("#tabSearchBtn").hover();
    await test.locator("#tabSearchBtn").focus();
    check(await test.locator("#tabSearchDialog").isHidden(),"hover and focus do not open search");
    const initialReads=reads.length;
    await open();
    check(await input.evaluate(node=>node===document.activeElement),"opening search focuses its input");
    check(await test.locator("#tabSearchResults [role=option]").count()===22,"All tabs includes available secondary and area views");
    check(await test.locator("#tabSearchResults").innerText().then(text=>text.includes("Kubernetes / AKS")&&!text.includes("Audit log")),
      "conditional views are present and unauthorized tabs are absent");
    check(await test.locator("#tabSearchRecentTitle").count()===0,"no invented recent searches");
    await input.fill("hour");
    check((await test.locator("#tabSearchResults").innerText()).includes("Hourly Billing"),"aliases locate hourly billing");
    await input.fill("not a real tab");
    await test.keyboard.press("Enter");
    check((await test.locator("#tabSearchStatus").innerText()).includes("No matching tabs")
      && await test.locator("#tabSearchDialog").isVisible(),"unmatched search is explained without navigating");
    check(reads.length===initialReads,"opening and searching use loaded metadata without requests");
    phase="jumping and recent searches";
    await input.fill("resources");await test.keyboard.press("Enter");await ready();
    await test.locator("#resourceExplorer").waitFor();
    check(await test.locator("#tabSearchDialog").isHidden(),"choosing a result closes search");
    check(reads.some(url=>url.startsWith("/api/dashboard/resources?")&&url.includes("days=7")&&url.includes("scope=sub-a")
      &&url.includes("service=Compute")),"search navigation preserves the reporting context");
    await test.evaluate(()=>{window.savedResourceView=document.querySelector("#resourceExplorer");});
    await open();
    check((await test.locator('[role=group][aria-labelledby="tabSearchRecentTitle"]').innerText()).includes("resources"),
      "opening search shows the last executed query");
    check(await test.locator('[role=group][aria-labelledby="tabSearchAllTitle"] [role=option]').count()===22,
      "all tabs remain available below recent searches");
    await input.fill("application");
    await test.locator("#tabSearchResults [role=option]").first().click();await ready();
    check(await test.locator("#rail-view-tags").getAttribute("aria-current")==="page","jump synchronizes the grouped sidebar");
    const beforeReturn=reads.length;
    await open();
    await test.locator('[role=group][aria-labelledby="tabSearchRecentTitle"] [role=option]').filter({hasText:"resources"}).click();
    await ready();
    check(await test.evaluate(()=>window.savedResourceView===document.querySelector("#resourceExplorer")) && reads.length===beforeReturn,
      "recent search reuses the existing tab snapshot");
    await test.reload();await ready();await open();
    check(await test.locator("#tabSearchRecentTitle").count()===1,"recent searches persist across reloads");
    phase="keyboard and history controls";
    await input.fill("");
    const first=await input.getAttribute("aria-activedescendant");
    await test.keyboard.press("ArrowDown");
    check(await input.getAttribute("aria-activedescendant")!==first,"arrow keys change the active result");
    await test.keyboard.press("ArrowUp");
    check(await input.getAttribute("aria-activedescendant")===first,"Up returns to the preceding result");
    await test.keyboard.press("Escape");
    check(await test.locator("#tabSearchBtn").evaluate(node=>node===document.activeElement),"Escape restores focus to the opener");
    await test.evaluate(()=>document.dispatchEvent(new KeyboardEvent("keydown",{key:"k",ctrlKey:true,bubbles:true})));
    check(await test.locator("#tabSearchDialog").isHidden(),"global shortcut no longer opens search");
    await test.locator("#tabSearchBtn").focus();await test.keyboard.press("Enter");
    check(await test.locator("#tabSearchDialog").isVisible(),"native keyboard activation of the icon remains accessible");
    await test.keyboard.press("Escape");
    check(await test.locator("#tabSearchBtn").evaluate(node=>node===document.activeElement),"dismissal restores icon focus");
    await open();await test.locator("#tabSearchClear").click();
    check(await test.locator("#tabSearchRecentTitle").count()===0&&await test.locator("#tabSearchClear").isHidden(),"Clear recent removes search history");
    await input.fill("resources");await test.keyboard.press("Enter");await ready();
    email="other@example.test";await test.reload();await ready();await open();
    check(await test.locator("#tabSearchRecentTitle").count()===0,"a different account cannot see the first account's history");
    await test.keyboard.press("Escape");
    phase="responsive header and dialog";
    for(const theme of ["light","dark","vivid"]){
      await test.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      for(const width of [1920,1366,1200,800,390,320]){
        await test.setViewportSize({width,height:720});
        await test.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        await open();
        const layout=await test.evaluate(()=>{
          const search=document.querySelector("#tabSearchBtn").getBoundingClientRect();
          const day=document.querySelector('[data-days="7"]').getBoundingClientRect();
          const dialog=document.querySelector("#tabSearchDialog").getBoundingClientRect();
          return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth+1,
            buttonFits:search.left>=0&&search.right<=innerWidth&&search.width>=31&&search.width<=33,
            beside7d:search.right<=day.left&&day.left-search.right<=12&&Math.abs(search.top-day.top)<=4,
            dialogFits:dialog.left>=0&&dialog.right<=innerWidth&&dialog.top>=0&&dialog.bottom<=innerHeight,
            resultScroll:document.querySelector("#tabSearchResults").scrollHeight>document.querySelector("#tabSearchResults").clientHeight};
        });
        layouts.push({theme,...layout});
        check(!layout.overflow&&layout.buttonFits&&layout.beside7d&&layout.dialogFits&&layout.resultScroll,
          `header and dropdown fit ${theme}/${width}: ${JSON.stringify(layout)}`);
        await test.keyboard.press("Escape");
      }
    }
    check(errors.length===0,`no page errors: ${errors.join("; ")}`);
    return {passed,layouts,errors};
  }catch(error){return {failure:error.message,phase,passed,errors,layouts};}
  finally{await context.close();}
}
