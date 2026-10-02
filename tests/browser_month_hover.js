async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open the local frontend source server first.");
  const context = await page.context().browser().newContext({
    viewport: { width: 1000, height: 700 }, reducedMotion: "reduce",
  });
  const test = await context.newPage();
  test.setDefaultTimeout(5000);
  let requests = 0, passed = 0;
  const errors = [];
  const assert = (ok, message) => { if (!ok) throw new Error(message); passed++; };
  test.on("pageerror", error => errors.push(error.message));
  try {
    await test.route("**/__month-hover__", route => route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css"></head><body>
        <button id="outside" style="position:absolute;left:10px;top:10px" title="Control hint">Outside</button>
        <div id="plot" style="position:absolute;left:430px;top:250px;width:480px;height:320px">
          <canvas id="monthOverMonth" aria-label="Month-over-month spend"></canvas></div>
        <script src="/assets/vendor/chart.umd.min.js"></script></body></html>`,
    }));
    await test.goto(origin + "/__month-hover__");
    await test.evaluate(async () => {
      const { chartTooltip, wireChartTooltip, wireTooltips } = await import("/assets/tooltips.js");
      wireTooltips();
      window.makeChart = horizontal => {
        window.chart?.destroy();
        const token = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        window.chart = new Chart(document.getElementById("monthOverMonth"), {
          type: "bar",
          data: { labels: ["2026-07", "2026-08", "2026-09"], datasets: [
            { label: "Compute & Web", data: [100, 160, 80], backgroundColor: token("--brand-1") },
            { label: "Networking", data: [20, 22, 15], backgroundColor: token("--brand-2") },
            { label: "Storage", data: [2, 4, 1], backgroundColor: token("--brand-3") },
            { label: "Credits", data: [-10, -14, -8], backgroundColor: token("--brand-4") },
          ] },
          options: { animation: false, responsive: true, maintainAspectRatio: false,
            indexAxis: horizontal ? "y" : "x",
            interaction: { mode: "nearest", intersect: true },
            scales: { x: { stacked: true }, y: { stacked: true } },
            plugins: { tooltip: { enabled: false, external: chartTooltip } },
          },
        });
        wireChartTooltip(window.chart);
      };
      window.makeChart(false);
    });
    test.on("request", () => requests++);
    const pointFor = (dataset, index, nearEdge = false) => test.evaluate(({ dataset, index, nearEdge }) => {
      const chart = window.chart, bar = chart.getDatasetMeta(dataset).data[index];
      const rect = chart.canvas.getBoundingClientRect();
      const x = bar.horizontal ? (bar.x + bar.base) / 2 : bar.x;
      const y = bar.horizontal ? bar.y : nearEdge ? bar.y + Math.sign(bar.base - bar.y) * 15 : (bar.y + bar.base) / 2;
      return { x: rect.left + x * rect.width / chart.width, y: rect.top + y * rect.height / chart.height,
        month: chart.data.labels[index], series: chart.data.datasets[dataset].label,
        value: chart.data.datasets[dataset].data[index] };
    }, { dataset, index, nearEdge });
    const hover = async (point, reset = true) => {
      if (reset) await test.mouse.move(410, 200);
      await test.mouse.move(point.x, point.y);
      await test.waitForTimeout(200);
      const state = await test.evaluate(point => {
        const tip = document.getElementById("cl-tooltip"), r = tip.getBoundingClientRect();
        return { visible: !tip.hidden, text: tip.textContent,
          mouseOnCanvas: document.elementFromPoint(point.x, point.y)?.id === "monthOverMonth",
          canvas: window.chart.canvas.getBoundingClientRect().toJSON(),
          active: window.chart.tooltip.getActiveElements().map(item => ({ dataset: item.datasetIndex, index: item.index })),
          bounded: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight };
      }, point);
      assert(state.visible && state.mouseOnCanvas && state.bounded, `Stable unobstructed hover: ${JSON.stringify({ point, state })}`);
      assert(state.text.includes(point.month) && state.text.includes(point.series)
        && state.text.includes(String(point.value)), `Correct hovered segment: ${JSON.stringify(state)}`);
    };
    for (const theme of ["light", "dark", "vivid"]) {
      await test.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (let index = 0; index < 3; index++) await hover(await pointFor(0, index, true));
      for (const dataset of [1, 2, 3]) await hover(await pointFor(dataset, 1));
    }
    await hover(await pointFor(0, 1, true));
    for (const dataset of [1, 2, 1, 0]) await hover(await pointFor(dataset, 1), false);
    await hover(await pointFor(0, 1, true));
    assert(await test.locator("#cl-tooltip").evaluate(tip => getComputedStyle(tip).pointerEvents === "none"),
      "Short chart tooltip stays click-through");
    await test.evaluate(() => {
      window.chart.options.plugins.tooltip.callbacks.footer = () =>
        Array.from({ length: 80 }, (_, index) => `Detail ${index}`);
      window.chart.update("none");
    });
    await hover(await pointFor(0, 1, true));
    assert(await test.locator("#cl-tooltip").evaluate(tip =>
      tip.scrollHeight > tip.clientHeight && getComputedStyle(tip).pointerEvents === "auto"),
    "Scrollable chart tooltip accepts hover");
    await test.locator("#cl-tooltip").hover();
    await test.waitForTimeout(250);
    assert(await test.locator("#cl-tooltip").isVisible(), "Tooltip remains readable while hovered");
    await test.keyboard.press("Escape");
    assert(await test.locator("#cl-tooltip").isHidden(), "Escape dismisses the hovered tooltip");
    await test.evaluate(() => {
      window.chart.options.plugins.tooltip.callbacks.footer = () => [];
      window.chart.update("none");
    });
    await test.mouse.move(410, 200);
    await hover(await pointFor(1, 2));
    await test.mouse.move(410, 200);
    await test.waitForTimeout(160);
    assert(await test.locator("#cl-tooltip").isHidden(), "Leaving both chart and tooltip dismisses it");
    await test.locator("#monthOverMonth").focus();
    await test.keyboard.press("Home");
    assert((await test.locator("#cl-tooltip").textContent()).includes("2026-07"), "Keyboard still reads month values");
    await test.keyboard.press("ArrowRight");
    assert((await test.locator("#cl-tooltip").textContent()).includes("2026-08"), "Keyboard advances to the next month");
    await test.locator("#outside").focus();
    assert(await test.locator("#cl-tooltip").textContent() === "Control hint", "Chart leave does not hide another control's tooltip");
    await test.waitForTimeout(160);
    assert(await test.locator("#cl-tooltip").isVisible(), "Control tooltip survives delayed chart dismissal");
    await test.evaluate(async () => {
      document.getElementById("outside").blur();
      document.getElementById("plot").style.top = "4px";
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    await hover(await pointFor(2, 1));
    await test.evaluate(async () => {
      document.getElementById("plot").style.top = "250px";
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      window.makeChart(true);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    await hover(await pointFor(0, 1));
    assert(errors.length === 0, errors.join("\n"));
    assert(requests === 0, "Hover does not fetch any data");
    return { passed, themes: 3, stackedSegments: true, negativeValues: true, horizontal: true, requests, errors };
  } finally {
    await context.close();
  }
}
