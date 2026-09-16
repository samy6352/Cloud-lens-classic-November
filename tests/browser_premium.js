async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before running the appearance checks.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1600, height: 1050 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  const errors = [];
  test.on("pageerror", error => errors.push(error.message));
  try {
    await test.goto(origin);
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    const results = [];
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(async theme => {
        document.documentElement.dataset.theme = theme;
        const { retintCharts } = await import("/assets/app.js");
        retintCharts();
      }, theme);
      for (const size of [{ width: 1600, height: 1050 }, { width: 1200, height: 800 }, { width: 390, height: 844 }]) {
        await test.setViewportSize(size);
        results.push(await test.evaluate(() => {
          const style = getComputedStyle(document.documentElement);
          const luminance = name => {
            const hex = style.getPropertyValue(name).trim().replace("#", "");
            const channels = [0, 2, 4].map(index => parseInt(hex.slice(index, index + 2), 16) / 255)
              .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
            return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
          };
          const contrast = (foreground, background) => {
            const a = luminance(foreground), b = luminance(background);
            return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
          };
          const rail = document.querySelector("#tabs").getBoundingClientRect();
          const header = document.querySelector(".bar").getBoundingClientRect();
          return {
            theme: document.documentElement.dataset.theme, width: innerWidth,
            overflow: document.documentElement.scrollWidth > innerWidth,
            boardHeight: document.querySelector("#tabBody").clientHeight,
            railAligned: Math.abs(rail.top - header.bottom) < 2,
            cardRadius: parseFloat(getComputedStyle(document.querySelector(".card")).borderRadius),
            contrast: {
              text: contrast("--text", "--surface"), muted: contrast("--muted", "--surface"),
              faint: contrast("--faint", "--surface-2"), accent: contrast("--accent", "--surface"),
              primary: contrast("--accent-ink", "--accent-fill"),
            },
          };
        }));
      }
    }
    const failures = results.filter(result => result.overflow || !result.railAligned
      || result.boardHeight < 200 || result.cardRadius < 8 || result.cardRadius > 12
      || Object.values(result.contrast).some(value => !Number.isFinite(value) || value < 4.5));
    if (failures.length || errors.length) throw new Error(JSON.stringify({ failures, errors }));
    return { results, errors };
  } finally {
    await context.close();
  }
}
