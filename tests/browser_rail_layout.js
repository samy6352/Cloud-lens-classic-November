async (page) => {
  const origin = page.url().match(/^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?/)?.[0];
  if (!origin) throw new Error("Open local CloudLens before checking rail layouts.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(), viewport: {width:1366,height:900},
    reducedMotion:"reduce",
  });
  const test = await context.newPage(), errors = [], reads = [], layouts = [];
  test.setDefaultTimeout(10000);
  let passed = 0, phase = "opening";
  const check = (ok, label) => { if (!ok) throw new Error(label); passed++; };
  const ready = () => test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true", null, {timeout:120000});
  const frame = () => test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  test.on("pageerror", error => errors.push(error.message));
  test.on("request", request => {
    if (/\/api\/dashboard\/(?:sections|executive|resources)\?/.test(request.url())) reads.push(request.url());
  });
  try {
    await context.addInitScript(() => {
      if (sessionStorage.getItem("railLayoutTest")) return;
      sessionStorage.setItem("railLayoutTest","true");
      localStorage.setItem("railLayout",'"sections"');
      localStorage.setItem("railMini","false");
      localStorage.setItem("railGroupedOpen",'["spend"]');
    });
    await context.route("**/api/dashboard/resources?**", route => route.fulfill({json:{days:30,resources:[]}}));
    await test.goto(origin); await ready();
    check(await test.locator("[data-main-section]").count() === 7,"current navigation retains all seven primary sections");
    check(await test.locator("#railLayout path").getAttribute("d") === "M5 4v12M10 4v12M15 4v12","current layout uses three vertical lines");
    check(await test.evaluate(() => {
      const toggle=document.querySelector("#railLayout").getBoundingClientRect();
      const collapse=document.querySelector("#railMini").getBoundingClientRect();
      return toggle.right<=collapse.left && Math.abs(toggle.top-collapse.top)<2;
    }),"layout toggle is immediately left of Collapse");
    const initialReads=reads.length;
    await test.evaluate(() => { window.originalView=document.querySelector("#overviewHome"); });
    phase = "grouped layout";
    await test.locator("#railLayout").focus(); await test.keyboard.press("Enter");
    check(await test.locator("#railLayout").evaluate(node=>node===document.activeElement && node.getAttribute("aria-pressed")==="true"),
      "layout switch preserves keyboard focus and exposes its state");
    check(await test.locator("#railLayout path").getAttribute("d") === "M4 5h12M4 10h12M4 15h12","grouped layout uses three horizontal lines");
    check(JSON.stringify(await test.locator(".rail-group-toggle .rail-name").allTextContents()) === '["Spend","Monitor","Optimize","Govern","Reports"]',
      "group headings match the requested layout");
    check(await test.evaluate(() => window.originalView===document.querySelector("#overviewHome")) && reads.length===initialReads,
      "layout switch retains the actual view DOM and does not fetch data");
    check(await test.locator("#tabBody").getAttribute("role") === "region","grouped view has region semantics");
    phase = "group keyboard controls";
    await test.locator("#group-monitor").focus(); await test.keyboard.press("ArrowRight");
    check(await test.locator("#group-monitor").getAttribute("aria-expanded") === "true","Right expands a group");
    await test.keyboard.press("ArrowRight");
    check(await test.locator("#rail-view-changes").evaluate(node=>node===document.activeElement),"Right enters the expanded group");
    await test.keyboard.press("ArrowLeft"); await test.keyboard.press("ArrowLeft");
    check(await test.locator("#group-monitor").getAttribute("aria-expanded") === "false","Left returns to and collapses a group");
    await test.keyboard.press("Home");
    check(await test.locator("#group-spend").evaluate(node=>node===document.activeElement),"Home reaches the first group");
    await test.locator("#rail-view-resources").click(); await ready();
    check(await test.locator("#resourceExplorer").count() === 1
      && await test.locator("#rail-view-resources").getAttribute("aria-current") === "page","grouped links navigate to the existing view");
    await test.evaluate(() => { window.originalView=document.querySelector("#resourceExplorer"); });
    const resourceReads=reads.length;
    phase = "collapse and restore";
    await test.locator("#railMini").click(); await frame();
    check(await test.locator("#railMini").getAttribute("aria-pressed") === "true","Collapse remains independent");
    check(await test.locator(".sidebar").evaluate(node=>node.getBoundingClientRect().width<=60),"grouped sidebar supports icon-only width");
    await test.locator("#railLayout").click();
    check(await test.locator("#primary-resources").getAttribute("aria-selected") === "true","switching back retains the selected view");
    check(await test.locator("#railMini").getAttribute("aria-pressed") === "true","switching layout preserves collapsed state");
    check(await test.evaluate(() => window.originalView===document.querySelector("#resourceExplorer")) && reads.length===resourceReads,
      "both layout and collapse switches preserve cached content");
    await test.locator("#railMini").click();
    await test.locator("#railLayout").click();
    phase = "persistence and cross-links";
    await test.reload(); await ready();
    check(await test.locator("#railLayout").getAttribute("aria-pressed") === "true","grouped preference survives reload");
    await test.locator("#group-spend").click();
    await test.evaluate(() => window.dispatchEvent(new CustomEvent("cl:goto-tab",{detail:"resources"})));
    await ready();
    check(await test.locator("#group-spend").getAttribute("aria-expanded") === "true"
      && await test.locator("#rail-view-resources").getAttribute("aria-current") === "page","cross-links reveal the active group");
    phase = "responsive layouts";
    const beforeResize=reads.length;
    for (const theme of ["light","dark","vivid"]) {
      await test.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      for (const width of [1366,390,1200]) {
        await test.setViewportSize({width,height:700}); await frame();
        const result=await test.evaluate(() => {
          const rail=document.querySelector("#tabs"), panel=document.querySelector("#tabBody");
          const tablist=rail.querySelector('[role="tablist"]');
          return {width:innerWidth,role:rail.getAttribute("role"),panelRole:panel.getAttribute("role"),
            tabs:!!tablist,orientation:tablist?.getAttribute("aria-orientation")||null,
            overflow:document.documentElement.scrollWidth>innerWidth+1,
            labelResolves:!!document.getElementById(panel.getAttribute("aria-labelledby")),
            selected:!!document.querySelector(innerWidth<=1100?"#primary-resources[aria-selected='true']":"#rail-view-resources[aria-current='page']")};
        });
        layouts.push({theme,...result});
        check(!result.overflow && result.labelResolves && result.selected,`responsive navigation remains usable: ${JSON.stringify(result)}`);
        // The rail is a navigation landmark at every width; the tablist sits inside it rather
        // than on it, because the rail also holds the layout and collapse controls and a
        // `tablist` may contain only tabs. So the question "are we in tab mode" is answered by
        // whether that tablist exists, not by the role on #tabs.
        check(result.role === "navigation","the rail is a navigation landmark at every width");
        check(result.tabs === (width<=1100),"narrow screens retain the existing horizontal navigation");
        check(!result.tabs || result.orientation === "horizontal","the narrow tablist is oriented horizontally");
      }
    }
    check(reads.length===beforeResize,"viewport and theme changes do not reload tab data");
    await test.setViewportSize({width:1366,height:500}); await frame();
    for (const button of await test.locator("[data-rail-group]").all()) {
      if (await button.getAttribute("aria-expanded") !== "true") await button.click();
    }
    await test.locator("#rail-view-settings").scrollIntoViewIfNeeded();
    check(await test.locator("#rail-view-settings").isVisible(),"last destination remains reachable on a short screen");
    check(await test.evaluate(() => {
      const rail=document.querySelector("#tabs").getBoundingClientRect();
      const footer=document.querySelector(".sidebar-account").getBoundingClientRect();
      return !footer.height || rail.bottom<=footer.top+1;
    }),"grouped navigation does not overlap the account footer");
    check(errors.length===0,`no runtime errors: ${errors.join("; ")}`);
    return {passed,layouts,errors};
  } catch (error) {
    return {failure:error.message,phase,passed,errors};
  } finally { await context.close(); }
}
