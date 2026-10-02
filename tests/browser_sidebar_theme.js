async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open a signed-in CloudLens page first.");
  const context = await page.context().browser().newContext({
    storageState: await page.context().storageState(),
    viewport: { width: 1440, height: 900 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  const errors = [], results = [];
  test.on("pageerror", error => errors.push(error.message));
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  try {
    const response = await context.request.get("http://127.0.0.1:8021/assets/app.css");
    assert(response.ok(), "Local sidebar stylesheet is available");
    const css = await response.body();
    await context.route(origin + "/assets/app.css", route => route.fulfill({ body: css, contentType: "text/css" }));
    await test.goto(origin);
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    await test.locator("#accountTrigger").waitFor({ state: "visible" });
    const palettes = [];
    for (let cycle = 0; cycle < 4; cycle++) {
      await test.locator("#themeBtn").click();
      await test.locator("#accountTrigger").click();
      await test.waitForFunction(() => {
        const probe = document.createElement("span");
        probe.style.backgroundColor = "var(--rail-surface)";
        document.body.appendChild(probe);
        const expected = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return getComputedStyle(document.querySelector(".account-popover")).backgroundColor === expected;
      });
      await test.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const palette = await test.evaluate(() => {
        const root = getComputedStyle(document.documentElement);
        const luminance = token => {
          const hex = root.getPropertyValue(token).trim().replace("#", "");
          const rgb = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
            .map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
          return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
        };
        const contrast = (a, b) => {
          a = luminance(a); b = luminance(b);
          return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
        };
        return {
          theme: document.documentElement.dataset.theme,
          saved: localStorage.getItem("costTheme"),
          sidebar: getComputedStyle(document.querySelector(".sidebar")).backgroundImage,
          active: getComputedStyle(document.querySelector(".rail-item.on")).backgroundImage,
          icon: getComputedStyle(document.querySelector(".rail-item.on .rail-icon-frame")).color,
          collapse: getComputedStyle(document.querySelector(".rail-mini-btn")).backgroundImage,
          account: getComputedStyle(document.getElementById("who")).backgroundColor,
          popover: getComputedStyle(document.querySelector(".account-popover")).backgroundColor,
          contrast: [
            contrast("--rail-label", "--rail-bg"), contrast("--rail-text", "--rail-selected"),
            contrast("--rail-muted", "--rail-depth"), contrast("--rail-accent", "--rail-selected"),
            contrast("--rail-icon", "--rail-raised"),
          ],
        };
      });
      assert(palette.saved === palette.theme, "Theme button persists the selected theme");
      assert(palette.contrast.every(value => Number.isFinite(value) && value >= 4.5),
        `Readable sidebar contrast: ${JSON.stringify(palette)}`);
      palettes.push(palette);
      await test.keyboard.press("Escape");
      for (const width of [1440, 390]) {
        await test.setViewportSize({ width, height: 900 });
        await test.locator("#accountTrigger").click();
        assert(await test.locator("#signout").isVisible(), "Account menu works in each theme");
        await test.locator("#signout").focus();
        await test.keyboard.press("Escape");
        const layout = await test.evaluate(() => ({
          theme: document.documentElement.dataset.theme, width: innerWidth,
          overflow: document.documentElement.scrollWidth > innerWidth,
        }));
        assert(!layout.overflow, `Theme preserves layout: ${JSON.stringify(layout)}`);
        results.push(layout);
      }
      await test.setViewportSize({ width: 1440, height: 900 });
    }
    assert(new Set(palettes.map(p => p.theme)).size === 4, "Theme button cycles all four themes");
    for (const surface of ["sidebar", "active", "icon", "collapse", "account", "popover"]) {
      assert(new Set(palettes.map(p => p[surface])).size === 4,
        `${surface} follows every theme: ${JSON.stringify(palettes.map(p => ({ theme: p.theme, value: p[surface] })))}`);
    }
    const selectedTheme = palettes.at(-1).theme;
    await test.reload();
    await test.waitForFunction(() => document.documentElement.dataset.dashboardReady === "true");
    assert(await test.evaluate(() => document.documentElement.dataset.theme) === selectedTheme,
      "Sidebar theme survives reload");
    assert(errors.length === 0, errors.join("\n"));
    return { themes: palettes.map(p => p.theme), surfaces: 6, layouts: results.length, errors };
  } finally {
    await context.close();
  }
}
