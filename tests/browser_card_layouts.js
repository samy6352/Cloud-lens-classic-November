async (page, { stylesheetUrl } = {}) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before the shared card checks.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1600, height: 1000 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  const errors = [], results = [];
  test.on("pageerror", error => errors.push(error.message));
  const ready = () => test.waitForFunction(
    () => document.documentElement.dataset.dashboardReady === "true", null, { timeout: 120000 });
  try {
    if (stylesheetUrl) {
      const response = await page.request.get(stylesheetUrl);
      if (!response.ok()) throw new Error("Requested card stylesheet is unavailable.");
      const body = await response.body();
      await context.route("**/assets/app.css", route => route.fulfill({ body, contentType: "text/css" }));
    }
    await test.goto(origin);
    await ready();
    for (const tab of ["overview", "billing", "commitments", "governance", "report"]) {
      const parent = await test.evaluate(async tab => {
        const { PRIMARY_SECTIONS } = await import("/assets/dashboard-navigation.js");
        return PRIMARY_SECTIONS.find(section => section.members.includes(tab))?.id;
      }, tab);
      await test.locator(`#tabs [data-main-section="${parent}"]`).click();
      if (parent !== "overview") {
        await test.locator(`#viewActions [data-subsection="${tab}"]`).click();
      } else {
        await test.locator('#viewActions [data-area="overview"]').click();
      }
      await ready();
      if (tab === "billing") {
        await test.waitForFunction(() => document.querySelector("#billRefresh")?.disabled === false);
      }
      for (const theme of ["light", "dark", "vivid"]) {
        await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
        for (const width of [1600, 1200, 390]) {
          await test.setViewportSize({ width, height: 1000 });
          const result = await test.evaluate(async () => {
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const groups = [...document.querySelectorAll("#tabBody :is(.grid-2, .grid-viz, .kpis)")];
            const mismatches = [];
            let pairs = 0;
            for (const group of groups) {
              const children = [...group.children].map(child => child.getBoundingClientRect());
              for (let index = 1; index < children.length; index++) {
                const previous = children[index - 1], current = children[index];
                if (Math.abs(previous.top - current.top) > 1) continue;
                pairs++;
                if (Math.abs(previous.width - current.width) > 1 || Math.abs(previous.height - current.height) > 1) {
                  mismatches.push({ group: group.className,
                    previous: { width: previous.width, height: previous.height },
                    current: { width: current.width, height: current.height } });
                }
              }
            }
            return { groups: groups.length, pairs, mismatches,
              overflow: document.documentElement.scrollWidth > innerWidth + 1 };
          });
          if (result.overflow || result.mismatches.length) throw new Error(JSON.stringify({ tab, theme, width, ...result }));
          results.push({ tab, theme, width, ...result });
        }
      }
      await test.setViewportSize({ width: 1600, height: 1000 });
    }
    if (!results.some(result => result.pairs > 0)) throw new Error("No adjacent cards were measured.");
    if (errors.length) throw new Error(errors.join("\n"));
    return { results, errors };
  } finally {
    await context.close();
  }
}
