async (page) => {
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(), viewport: { width: 1600, height: 1000 },
    reducedMotion: "reduce",
  });
  const test = await context.newPage();
  const errors = [], results = [];
  test.on("pageerror", error => errors.push(error.message));
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before running subsection checks.");
  const ready = () => test.waitForFunction(
    () => document.documentElement.dataset.dashboardReady === "true", null, { timeout: 120000 });
  const styles = (container, selected, other) => test.evaluate(({ container, selected, other }) => {
    const values = selector => {
      const element = document.querySelector(selector);
      if (!element) throw new Error(`Missing selector: ${selector}`);
      const style = getComputedStyle(element);
      return Object.fromEntries(["height", "borderTopWidth", "borderTopColor", "borderRadius",
        "paddingTop", "paddingBottom", "paddingLeft", "paddingRight", "backgroundColor",
        "color", "fontSize", "lineHeight", "gap"].map(name => [name, style[name]]));
    };
    return { container: values(container), selected: values(selected), other: values(other),
      overflow: document.documentElement.scrollWidth > innerWidth + 1 };
  }, { container, selected, other });
  try {
    const response = await page.request.get("http://127.0.0.1:8021/assets/app.css");
    if (!response.ok()) throw new Error("Local stylesheet unavailable.");
    const css = await response.body();
    await context.route("**/assets/app.css", route => route.fulfill({ body: css, contentType: "text/css" }));
    await test.goto(origin);
    await ready();
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const width of [1600, 1200, 390]) {
        await test.setViewportSize({ width, height: 1000 });
        await test.locator('#tabs [data-main-section="overview"]').click();
        await ready();
        const area = await styles("#viewActions > .ar-picker",
          '#viewActions .ar-seg[aria-pressed="true"]', '#viewActions .ar-seg[aria-pressed="false"]');
        const header = await test.locator("#viewActions").evaluate(el => {
          const title = el.querySelector("h2"), picker = el.querySelector(".ar-picker");
          const a = title.getBoundingClientRect(), b = picker.getBoundingClientRect();
          return { title: title.textContent, sameRow: Math.abs((a.top + a.bottom - b.top - b.bottom) / 2) < 2,
            areaLabels: el.querySelectorAll(".ar-label").length };
        });
        if (header.title !== "Overview" || header.areaLabels || (width > 1100 && !header.sameRow)
          || await test.locator("#tabBody .ar-picker").count()) {
          throw new Error(`Overview header layout differs from Billing: ${JSON.stringify(header)}`);
        }
        await test.locator('#viewActions [data-area="overview"]').focus();
        await test.keyboard.press("ArrowRight");
        await ready();
        if (await test.locator('#viewActions [data-area="overview"]').getAttribute("aria-pressed") !== "false") {
          throw new Error("Overview subsection keyboard selection failed.");
        }
        await test.locator('#viewActions [data-area="overview"]').click();
        await ready();
        await test.locator('#tabs [data-main-section="billing"]').click();
        await ready();
        const subsection = await styles("#viewActions .subsection-nav",
          '#viewActions .subsection-tab[aria-selected="true"]', '#viewActions .subsection-tab[aria-selected="false"]');
        const comparable = value => ({ ...value, container: { ...value.container, height: undefined } });
        if (JSON.stringify(comparable(area)) !== JSON.stringify(comparable(subsection))) {
          throw new Error(JSON.stringify({ theme, width, area, subsection }));
        }
        if (area.overflow || parseFloat(area.container.borderTopWidth) !== 0
            || parseFloat(area.selected.borderTopWidth) !== 0 || parseFloat(area.container.borderRadius) !== 0
            || parseFloat(area.selected.height) < 39 || parseFloat(area.selected.height) > 41
            || parseFloat(area.container.height) > 54
            || Math.abs(parseFloat(area.container.height) - parseFloat(subsection.container.height)) > 16) {
          throw new Error(`Subsection size, boundary or overflow failed: ${JSON.stringify(area)}`);
        }
        await test.locator('#viewActions [data-subsection="billing"]').focus();
        await test.keyboard.press("ArrowRight");
        if (await test.locator('#viewActions [data-subsection="uptime"]').getAttribute("aria-selected") !== "true") {
          throw new Error("Subsection keyboard selection failed.");
        }
        await test.locator('#viewActions [data-subsection="billing"]').click();
        results.push({ theme, width, containerHeight: area.container.height, buttonHeight: area.selected.height });
      }
    }
    if (errors.length) throw new Error(errors.join("\n"));
    return { matchedStyles: results, errors };
  } finally {
    await context.close();
  }
}
