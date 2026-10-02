async (page) => {
  const origin = page.url().match(/^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?/)?.[0];
  if (!origin) throw new Error("Open local CloudLens before checking tag dropdowns.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(), viewport: {width:1440,height:900}, reducedMotion:"reduce",
  });
  const test = await context.newPage(), errors = [], layouts = [];
  test.setDefaultTimeout(10000);
  let passed = 0, tagReads = 0, empty = false, phase = "opening";
  const check = (ok, label) => { if (!ok) throw new Error(label); passed++; };
  const ready = () => test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true", null, {timeout:120000});
  const navigate = async tab => {
    await test.evaluate(tab => window.dispatchEvent(new CustomEvent("cl:goto-tab",{detail:tab})),tab);
    await ready();
  };
  const value = () => test.locator("#tagSum").textContent();
  const keys = [{key:"Environment",cost:150,resources:2,values:2},
    {key:"Owner",cost:175,resources:3,values:2}, {key:"owner",cost:25,resources:1,values:1},
    {key:'team "<&>',cost:25,resources:1,values:1},
    ...Array.from({length:33},(_,i)=>({key:`Extra tag ${i+1}`,cost:25,resources:1,values:1}))];
  const uncosted = [{key:"hidden-title",resources:31},{key:"fastpathenabled",resources:2},
    {key:'hidden "<&> ' + "very-long-tag-".repeat(6),resources:1}];
  const tags = {currency:"USD",total:185,days:30,dates:[],values:["prod","dev","Ana","Ben","lower"],
    tagged:{cost:175,resources:3},untagged:{cost:10,resources:1},keys,uncosted,
    resources:[
      {id:"a",name:"Alpha",service:"Compute",keys:["Environment","Owner"],v:[0,2],cost:100},
      {id:"b",name:"Beta",service:"Compute",keys:["Environment","Owner"],v:[1,3],cost:50},
      {id:"c",name:"Gamma",service:"Storage",keys:["Owner","owner",...keys.slice(3).map(k=>k.key)],v:[2,4,...keys.slice(3).map(()=>4)],cost:25},
      {id:"d",name:"Delta",service:"Storage",keys:[],v:[],cost:10},
    ]};
  test.on("pageerror",error=>errors.push(error.message));
  try {
    await context.addInitScript(() => {
      localStorage.setItem("costFilters","{}"); localStorage.setItem("costPeriodMonth",'""');
      localStorage.setItem("costDays","30");
    });
    await context.route("**/api/dashboard/tags?**",route=>{
      tagReads++;
      return route.fulfill({json:empty ? {...tags,keys:[],resources:[],total:0,tags_not_loaded:false,
        tagged:{cost:0,resources:0},untagged:{cost:0,resources:0}} : tags});
    });
    await test.goto(origin); await ready(); await navigate("tags");
    await test.locator("#tagKeysToggle").waitFor();
    check(await test.locator("#tagKeysMenu").isHidden() && await test.locator("#tagValuesMenu").isHidden(),"both dropdowns start closed");
    check(await test.locator("#tagKeysLabel").innerText() === "Select tag keys (37)","closed key picker summarizes all available keys");
    check(await test.locator("#uncostedTagsToggle, [data-uncosted-key]").count() === 0,"no-spend dropdown is removed");
    check(await test.locator("#tagValuesToggle").isDisabled()
      && await test.locator("#tagValuesLabel").innerText() === "Select a tag first","Values is disabled until a tag is selected");
    const disabledStyle=await test.locator("#tagValuesToggle").evaluate(node=>getComputedStyle(node).backgroundColor);
    await test.locator("#tagValuesToggle").evaluate(node=>node.click());
    check(await test.locator("#tagValuesMenu").isHidden(),"disabled Values cannot open");
    check(await test.locator(".tag-key-row > .tag-key-picker").count() === 2
      && await test.locator(".card.taguncosted").count() === 0,"both dropdowns share one row instead of separate cards");
    check(await test.locator("#tagWorkbench").count() === 1
      && await test.locator(".card.tagsel, #tagWorkbench .card").count() === 0
      && await test.locator("#tagWorkbench").evaluate(panel =>
        ["#tagKeysToggle","#tagValuesToggle","#tagSelected","#tagClear","#tagUndo","#tagBudgetBtn",
          "[data-resource-view]","[data-mode]","#tagModeHint","[data-state-key='tags-help']"]
          .every(selector=>panel.contains(document.querySelector(selector)))),
      "all resource, tag, selection, and help controls belong to one panel without nested cards");
    check(await test.locator("#tagWorkbench").evaluate(panel => panel.getBoundingClientRect().height < 220),
      "the empty-selection workbench stays compact on desktop");
    phase = "selection and keyboard";
    await test.locator("#tagKeysToggle").focus(); await test.keyboard.press("Enter");
    await test.waitForFunction(()=>document.activeElement.id==="tagFilter");
    check(await test.locator("#tagKeysToggle").getAttribute("aria-expanded") === "true","native dropdown exposes expanded state");
    await test.locator("#tagFilter").fill("Environment");
    const total = await value();
    check((await test.locator("#tagKeyCount").innerText()).includes("1 of 37"),"search narrows the available keys");
    await test.keyboard.press("ArrowDown"); await test.keyboard.press("Space");
    check(await test.getByRole("checkbox",{name:"Environment",exact:true}).isChecked(),"Space selects a searched key");
    check(await test.locator("#tagKeysMenu").isVisible(),"multi-select stays open");
    check((await value()).includes("150"),"key selection uses the existing resource filter");
    check(await test.evaluate(()=>document.activeElement.dataset.tag==="Environment"),"checkbox focus survives result repaint");
    check(await test.locator("#tagValues, .card.tagvals").count() === 0
      && await test.locator('#tagKeysMenu [data-vk]').count() === 0
      && await test.locator('#tagValuesMenu [data-vk="Environment"]').count() === 2,
      "values belong only to the second dropdown, not the key list or page panels");
    check(await test.locator("#tagValuesToggle").isEnabled()
      && await test.locator("#tagValuesLabel").innerText() === "All values","selecting a tag enables Values with all values included");
    await test.waitForFunction(disabledStyle =>
      getComputedStyle(document.getElementById("tagValuesToggle")).backgroundColor !== disabledStyle, disabledStyle);
    check(await test.locator("#tagValuesToggle").evaluate((node,disabledStyle)=>getComputedStyle(node).backgroundColor!==disabledStyle,disabledStyle),
      "Values no longer has its disabled grey background");
    await test.keyboard.press("Escape");
    await test.locator("#tagValuesToggle").focus(); await test.keyboard.press("Enter");
    await test.waitForFunction(()=>document.activeElement.dataset.valueSearch==="Environment");
    await test.locator('[data-value-search="Environment"]').fill("prod");
    await test.keyboard.press("ArrowDown"); await test.keyboard.press("Space");
    check(await test.locator('[data-vk="Environment"][data-vv="prod"]').isChecked()
      && (await value()).includes("100"),"value search and keyboard selection filter from the dropdown");
    await test.locator('[data-value-search="Environment"]').fill("");
    await test.locator('[data-vk="Environment"][data-vv="dev"]').click();
    check((await value()).includes("150"),"multiple values within one key match OR");
    await test.locator('[data-allvals="Environment"]').click();
    check(await test.locator('[data-vk="Environment"]:checked').count() === 0
      && (await value()).includes("150"),"All values clears value narrowing but keeps its key selected");
    await test.keyboard.press("Escape");
    check(await test.locator("#tagValuesToggle").evaluate(node=>node===document.activeElement),"Escape restores focus to Values");
    await test.locator("#tagKeysToggle").click();
    await test.locator("#tagFilter").fill("Owner");
    check((await test.locator("#tagKeyCount").innerText()).includes("2 of 37"),"case-distinct keys remain separate matches");
    await test.getByRole("checkbox",{name:"Owner",exact:true}).click();
    check(await test.locator("#tagKeysLabel").innerText() === "2 tag keys selected","multi-selection is visible when closed");
    await test.keyboard.press("Escape");
    check(await test.locator("#tagKeysToggle").evaluate(node=>node===document.activeElement),"Escape returns focus to the dropdown trigger");
    await test.locator("#tagValuesToggle").click();
    check(await test.locator("#tagValueGroups .tag-key-values").count()===2,
      "Values offers separately labelled choices for every selected key");
    await test.locator('[data-vk="Environment"][data-vv="prod"]').click();
    check((await value()).includes("100"),"value filters still narrow selected keys");
    check((await test.locator("#tagSelected").innerText()).includes("Environment = prod"),
      "selected value remains visible outside the dropdown");
    check(await test.locator("#tagValuesLabel").innerText()==="1 value selected",
      "closed Values label reports the selected value count");
    const beforeSearch=await value();
    await test.locator('[data-value-search="Owner"]').fill("not present");
    check((await test.locator("#tagValueGroups").innerText()).includes("No values match")
      && await value()===beforeSearch,"value search is independent and never changes selected filters");
    await test.keyboard.press("Escape");
    await test.locator('[data-mode="any"]').click();
    check((await value()).includes("175"),"OR matching remains available");
    const filtered = await value();
    phase = "clear undo and cached return";
    await test.locator("#tagClear").click();
    check(await value() === total && await test.locator("#tagValuesToggle").isDisabled()
      && await test.locator("#tagValuesLabel").innerText()==="Select a tag first","Clear resets costs and disables Values");
    await test.locator("#tagUndo").click();
    check(await value() === filtered && await test.locator("#tagKeysLabel").innerText() === "2 tag keys selected"
      && await test.locator("#tagValuesToggle").isEnabled(),"Undo restores keys, values, OR mode and Values availability");
    await test.locator("#tagValuesToggle").click();
    await navigate("overview");
    check(await test.locator(".tag-key-menu:popover-open").count() === 0,"navigating away closes dropdowns");
    await navigate("tags");
    check(tagReads===1 && await value()===filtered,"cached return retains selection without fetching tag data");
    check(await test.locator("#tagValuesToggle").getAttribute("aria-expanded")==="false","cached trigger reflects the closed menu");
    await test.locator("#tagValuesToggle").click();
    await navigate("overview"); await navigate("tags");
    check(await test.locator("#tagValuesToggle").getAttribute("aria-expanded")==="false"
      && tagReads===1,"dropdown cleanup remains active after a second cached visit");
    phase = "popup geometry";
    for (const theme of ["light","dark","vivid"]) {
      await test.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      for (const width of [1440,800,390,320]) {
        await test.setViewportSize({width,height:800});
        await test.locator("#tagKeysToggle").scrollIntoViewIfNeeded();
        await test.locator("#tagKeysToggle").click();
        await test.locator("#tagFilter").fill("");
        const layout=await test.evaluate(()=>{
          const menu=document.querySelector("#tagKeysMenu"), box=menu.getBoundingClientRect();
          const options=menu.querySelector(".tag-key-options");
          const first=document.querySelector("#tagKeysToggle").getBoundingClientRect();
          const second=document.querySelector("#tagValuesToggle").getBoundingClientRect();
          return {width:innerWidth,left:box.left,right:box.right,top:box.top,bottom:box.bottom,height:box.height,
            sameRow:Math.abs(first.top-second.top)<1 && first.right<second.left
              && Math.abs(first.width-second.width)<1 && Math.abs(first.height-second.height)<1,
            internalScroll:options.scrollHeight>options.clientHeight,menuOverflow:menu.scrollWidth>menu.clientWidth+1,
            pageOverflow:document.documentElement.scrollWidth>innerWidth+1};
        });
        layouts.push({theme,...layout});
        check(layout.left>=0 && layout.right<=width+1 && layout.top>=0 && layout.bottom<=800
          && layout.height<=401 && layout.sameRow && layout.internalScroll && !layout.menuOverflow && !layout.pageOverflow,
          `bounded dropdown at ${theme}/${width}: ${JSON.stringify(layout)}`);
        await test.locator("#tagChips").evaluate(node=>{node.scrollTop=node.scrollHeight;});
        check(await test.locator("#tagKeysMenu").isVisible(),"scrolling options keeps dropdown open");
        await test.keyboard.press("Escape");
        await test.locator("#tagValuesToggle").click();
        check(await test.locator("#tagValuesMenu").evaluate(menu=>{
          const box=menu.getBoundingClientRect();
          return box.left>=0 && box.right<=innerWidth+1 && box.top>=0 && box.bottom<=innerHeight
            && box.height<=401 && menu.scrollWidth<=menu.clientWidth+1;
        }),`Values dropdown fits ${theme}/${width}`);
        await test.keyboard.press("Escape");
      }
    }
    phase = "single-value tag and empty view";
    await test.setViewportSize({width:1440,height:900});
    await test.locator("#tagKeysToggle").click();
    await test.locator(".tagview-toggle").click();
    check(await test.locator("#tagKeysMenu").isHidden(),"outside click dismisses dropdown");
    await test.locator("#tagClear").click();
    await test.locator("#tagKeysToggle").click();
    await test.locator("#tagFilter").fill("owner");
    await test.getByRole("checkbox",{name:"owner",exact:true}).check();
    await test.keyboard.press("Escape");
    await test.locator("#tagValuesToggle").click();
    check(await test.locator('[data-vk="owner"]').count()===1,"Values includes a single recorded value too");
    await test.locator('[data-vk="owner"]').check();
    check((await value()).includes("25"),"single value option applies the matching resource filter");
    await test.keyboard.press("Escape");
    await test.locator('#tagSelected [data-drop="owner"]').click();
    check(await test.locator("#tagValuesToggle").isDisabled(),"removing the last selected tag greys out Values");
    phase = "unknown tag counts";
    tags.tags_not_loaded=true;
    await test.locator("#refreshViews").click(); await ready();
    check(JSON.stringify(await test.locator("[data-resource-view]").allTextContents())
      === JSON.stringify(["All · 4","Tagged · 3","Untagged · 0","Unknown · 1"]),
      "All is partitioned into Tagged, Untagged and Unknown");
    check((await test.locator("#tabBody").innerText()).includes("UNKNOWN TAG SPEND"),
      "unknown tag spend has its own KPI");
    await test.locator("#tagKeysToggle").click();
    await test.locator("#tagFilter").fill("Environment");
    await test.getByRole("checkbox",{name:"Environment",exact:true}).check();
    await test.keyboard.press("Escape");
    await test.locator('[data-resource-view="unknown"]').click();
    check((await value()).includes("10") && (await test.locator("#tagSumSub").innerText()).includes("1 of 4"),
      "Unknown filters the same resource bucket even with a selected key");
    check(await test.locator("#tagBudgets").isHidden() && await test.locator("#tagBudgetBtn").isHidden(),
      "Unknown does not offer matching or new tag budgets");
    for(const width of [1440,800,390,320]){
      await test.setViewportSize({width,height:900});
      check(await test.locator("#tagWorkbench").evaluate(panel=>{
        const box=panel.getBoundingClientRect();
        return document.documentElement.scrollWidth<=innerWidth+1
          && [...panel.querySelectorAll("[data-resource-view]")].every(button=>{
            const b=button.getBoundingClientRect();
            return b.left>=box.left && b.right<=box.right+1;
          });
      }),`all four tag-status controls fit at ${width}px`);
    }
    await test.locator('[data-resource-view="tagged"]').click();
    check((await value()).includes("150"),"returning to Tagged restores the retained key selection");
    empty=true;
    await test.locator("#refreshViews").click(); await ready();
    check(await test.locator("#uncostedTagsToggle, #tagValuesToggle").count()===0
      && (await test.locator("#tabBody").innerText()).includes("No cost data is loaded"),
      "empty cost data shows an honest empty state, not no-spend controls");
    check(errors.length===0,`no runtime errors: ${errors.join("; ")}`);
    return {passed,layouts,tagReads,errors};
  } catch(error) { return {failure:error.message,phase,passed,errors}; }
  finally { await context.close(); }
}
