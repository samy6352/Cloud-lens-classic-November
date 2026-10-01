async (page) => {
  const origin=page.url().match(/^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?/)?.[0];
  if(!origin)throw new Error("Open local CloudLens before checking tag budgets.");
  const context=await page.context().browser().newContext({
    storageState:await page.context().storageState(),viewport:{width:1440,height:1000},reducedMotion:"reduce",
  });
  const test=await context.newPage(), errors=[], reads=[];
  test.setDefaultTimeout(15000);
  let passed=0, phase="opening", hold=null, release;
  const check=(ok,label)=>{if(!ok)throw new Error(label);passed++;};
  const ready=()=>test.waitForFunction(()=>document.documentElement.dataset.dashboardReady==="true",null,{timeout:60000});
  const go=async id=>{await test.evaluate(id=>window.dispatchEvent(new CustomEvent("cl:goto-tab",{detail:id})),id);await ready();};
  const selectKey=async key=>{
    await test.locator("#tagKeysToggle").click();await test.locator("#tagFilter").fill(key);
    await test.getByRole("checkbox",{name:key,exact:true}).check();await test.keyboard.press("Escape");
  };
  const selectValue=async(key,value)=>{
    await test.locator("#tagValuesToggle").click();await test.locator(`[data-vk="${key}"][data-vv="${value}"]`).check();
    await test.keyboard.press("Escape");
  };
  const tags={days:30,currency:"USD",total:150,dates:[],values:["prod","dev","Ana","Ben"],
    tagged:{cost:150,resources:2},untagged:{cost:0,resources:0},
    keys:[{key:"Env",cost:150,resources:2,values:2},{key:"Owner",cost:150,resources:2,values:2}],
    resources:[{id:"a",name:"Prod VM",cost:100,keys:["Env","Owner"],v:[0,2]},
      {id:"b",name:"Dev VM",cost:50,keys:["Env","Owner"],v:[1,3]}]};
  const make=(name,values,extra={})=>({
    name,subscription:"Demo",subscription_id:"sub-a",currency:"USD",amount:10,current_spend:21.78,remaining:-11.78,
    percent_used:217.8,status:"over",time_grain:"Monthly",filter_exact:true,filter:`Env = ${values.join(", ")}`,
    tag_filter:[["env",values]],start:"2026-09-01",end:"2027-09-01",
    trend:{from:"2026-09-01",to:"2026-09-10",labels:["2026-09-01","2026-09-02"],cumulative:[10,20],total:20,filtered_exactly:true},
    ...extra});
  let payload={budgets:[make("Prod budget",["prod"]),make("Dev budget",["dev"],{trend:null}),
    make("Other owner",["prod"],{tag_filter:[["env",["prod"]],["Owner",["Ben"]]]}),
    make("Not a tag budget",[],{tag_filter:[]})]};
  test.on("pageerror",error=>errors.push(error.message));
  const charts=async()=>{
    for(const host of await test.locator("#tagBudgets .budget-chart").all()){
      await host.scrollIntoViewIfNeeded();
      await test.waitForFunction(host=>{
        const canvas=host.querySelector("canvas");return canvas&&window.Chart?.getChart(canvas);
      },await host.elementHandle());
    }
  };
  try {
    await context.addInitScript(()=>{localStorage.setItem("costFilters","{}");localStorage.setItem("costPeriodMonth",'""');});
    await context.route("**/api/dashboard/tags?**",route=>route.fulfill({json:tags}));
    await context.route("**/api/budgets**",async route=>{
      if(route.request().method()!=="GET")throw new Error("No budget writes are permitted.");
      // Budgets without a trend fetch it lazily (e7b08fb); this one has no history to give.
      if(new URL(route.request().url()).pathname==="/api/budgets/trend")
        return route.fulfill({json:{trend:null}});
      reads.push(route.request().url());
      if(hold)await hold;
      return route.fulfill({json:payload});
    });
    await test.goto(origin);await ready();await go("tags");
    check(reads.length===0,"no budget API read before choosing a tag");
    await selectKey("Env");
    await test.locator("#tagBudgets .budget").first().waitFor();
    check(await test.locator("#tagBudgets .budget").count()===3,"key-only selection shows budgets for all its values");
    await charts();
    check(await test.locator("#tagBudgets canvas").count()===3,"matching budgets draw charts without a second click");
    check(!/[?&](days|period_month|service)=/.test(reads[0]),"budget graphs retain the budget's full period and scope");
    phase="value and key narrowing";
    await selectValue("Env","prod");
    await test.waitForFunction(()=>document.querySelectorAll("#tagBudgets .budget").length===2);
    await selectKey("Owner");await selectValue("Owner","Ana");
    await test.waitForFunction(()=>document.querySelectorAll("#tagBudgets .budget").length===1);
    check((await test.locator("#tagBudgets .budget-name").innerText())==="Prod budget","AND selection excludes a conflicting owner budget");
    await charts();
    const trend=await test.locator("#tagBudgets canvas").evaluate(canvas=>{
      const chart=Chart.getChart(canvas);return{labels:chart.data.labels,values:chart.data.datasets.map(d=>d.data)};
    });
    check(JSON.stringify(trend.values)==="[[10,20],[10,10]]","trend uses the budget's cumulative data and limit");
    check(await test.locator("#tagBudgets canvas").evaluate(canvas=>canvas.getBoundingClientRect().height<=121),
      "tag-budget trend uses the compact 120px canvas");
    check(reads.length===1,"key and value changes reuse the budget response");
    phase="snapshot and clear";
    await test.locator("#tagClear").click();
    check(await test.locator("#tagBudgets").isHidden(),"clearing tags hides their budgets");
    await selectKey("Env");await selectValue("Env","dev");
    await test.waitForFunction(()=>document.querySelectorAll("#tagBudgets .budget").length===1);
    await charts();
    const snapshot=await test.locator("#tagBudgets canvas").evaluate(canvas=>{
      const chart=Chart.getChart(canvas);return{labels:chart.data.labels,values:chart.data.datasets[0].data};
    });
    check(JSON.stringify(snapshot.labels)==='["Spent","Budget"]'&&JSON.stringify(snapshot.values)==="[21.78,10]",
      "missing history uses actual Azure spend and budget, not invented dates");
    check((await test.locator("#tagBudgets").innerText()).includes("snapshot, not a trend"),"snapshot source is explicit");
    check(await test.locator("#tagBudgets .budget").evaluate(card=>{
      const info=card.querySelector(".budget-info").getBoundingClientRect();
      const chart=card.querySelector(".budget-chart").getBoundingClientRect();
      return chart.left>=info.right&&card.querySelector("canvas").getBoundingClientRect().height<=101
        &&card.getBoundingClientRect().height<300;
    }),"single budget uses a compact chart alongside its details");
    phase="errors, refresh and cached resume";
    payload={budgets:[],error:"Read denied"};
    await test.locator("#tagBudgetRefresh").click();
    await test.getByText("Read denied Results may be incomplete.",{exact:true}).waitFor();
    check((await test.locator("#tagBudgets").innerText()).includes("could not be confirmed"),"read failure is not reported as no budgets");
    payload={budgets:[make("Partial dev",["dev"],{trend:null})],partial:true};
    await test.locator("#tagBudgetRefresh").click();
    await test.getByText("Partial dev",{exact:true}).waitFor();
    check((await test.locator("#tagBudgets").innerText()).includes("incomplete"),"partial budgets retain warning and matching results");
    hold=new Promise(done=>{release=done;});
    await test.locator("#tagBudgetRefresh").click();
    await test.getByText("Looking for matching budgets...",{exact:true}).waitFor();
    await go("overview");
    payload={budgets:[make("Resumed dev",["dev"],{trend:null})]};
    hold=null;release();
    await go("tags");
    await test.getByText("Resumed dev",{exact:true}).waitFor();
    await charts();
    check(reads.length===4,"returning after an in-flight read does not refetch");
    check(await test.locator("#tagValuesLabel").innerText()==="1 value selected","cached return retains tag/value selection");
    phase="responsive budget card";
    for(const width of [1440,390,320]){
      await test.setViewportSize({width,height:900});
      await test.locator("#tagBudgets").scrollIntoViewIfNeeded();
      check(await test.evaluate(()=>{
        const element=document.querySelector("#tagBudgets .budget"),card=element.getBoundingClientRect();
        const info=element.querySelector(".budget-info").getBoundingClientRect();
        const chart=element.querySelector(".budget-chart").getBoundingClientRect();
        return document.documentElement.scrollWidth<=innerWidth+1 && card.left>=0 && card.right<=innerWidth+1
          &&(innerWidth>=1100?chart.left>=info.right:chart.top>=info.bottom);
      }),`budget card fits ${width}px`);
    }
    check(errors.length===0,`no page errors: ${errors.join("; ")}`);
    return{passed,reads:reads.length,errors};
  }catch(error){return{failure:error.message,phase,passed,reads,errors};}
  finally{if(release)release();await context.close();}
}
