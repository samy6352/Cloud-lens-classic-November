async (page) => {
  const origin = page.url().match(/^https?:\/\/[^/]+/)?.[0];
  if (!origin) throw new Error("Open CloudLens before running the tooltip browser checks.");
  const context = await page.context().browser().newContext();
  const fixture = await context.newPage();
  const failures = [];
  const check = (name, ok) => { if (!ok) failures.push(name); };
  try {
    await fixture.route("**/__tooltip-test__", (route) => route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css"></head>
        <body><button id="one" title="First hint" aria-describedby="existing">One</button>
        <button id="two" title="Second hint">Two</button><span id="existing">Context</span>
        <script src="/assets/vendor/chart.umd.min.js"></script>
        <script type="module">import {wireTooltips} from '/assets/tooltips.js';
        wireTooltips(); window.ready = true;</script></body></html>`,
    }));
    await fixture.goto(`${origin}/__tooltip-test__`);
    await fixture.waitForFunction(() => window.ready);
    let requests = 0;
    fixture.on("request", () => requests++);
    const results = await fixture.evaluate(async () => {
      const { showTooltip, hideTooltip, chartTooltip, wireChartTooltip } = await import("/assets/tooltips.js");
      const one = document.querySelector("#one");
      const two = document.querySelector("#two");
      const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
      const start = performance.now();
      one.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, pointerType: "mouse" }));
      const tip = document.querySelector("#cl-tooltip");
      const result = { mouse: !tip.hidden && tip.textContent === "First hint", feedback_ms: performance.now() - start };
      result.nativeSuppressed = !one.hasAttribute("title");
      result.description = one.getAttribute("aria-describedby") === "existing cl-tooltip";
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      result.escape = tip.hidden;
      two.focus();
      result.focus = !tip.hidden && tip.textContent === "Second hint";
      result.oldDescription = one.getAttribute("aria-describedby") === "existing";
      two.title = "Updated hint";
      await tick();
      result.dynamic = !tip.hidden && tip.textContent === "Updated hint";
      showTooltip(two, '<img src=x onerror="throw 1">');
      result.safeText = !tip.querySelector("img") && tip.textContent.startsWith("<img");
      showTooltip(two, "Viewport boundary", { x: innerWidth, y: innerHeight });
      const rect = tip.getBoundingClientRect();
      result.bounded = rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight;
      // A short hint must not intercept the click meant for whatever it covers.
      showTooltip(two, "Short hint");
      const hint = tip.getBoundingClientRect();
      result.clickThrough = getComputedStyle(tip).pointerEvents === "none"
        && !tip.contains(document.elementFromPoint(hint.left + hint.width / 2, hint.top + hint.height / 2));
      // Long enough to scroll, so hovering it is worth the cost of taking the pointer.
      showTooltip(two, Array.from({ length: 80 }, (_, i) => `Line ${i}`).join("\n"));
      result.scrollableHoverable = getComputedStyle(tip).pointerEvents === "auto";
      two.remove();
      await tick();
      result.detached = tip.hidden;
      const canvas = document.createElement("canvas");
      document.body.appendChild(canvas);
      const chart = { canvas, width: 300, height: 150 };
      chartTooltip({ chart, tooltip: { opacity: 1, title: ["Compute"], body: [{ lines: ["Cost: 125.50"] }], caretX: 20, caretY: 30 } });
      result.chart = !tip.hidden && tip.textContent === "Compute\nCost: 125.50";
      // A line chart has no bar bounds, so the caret box is what keeps the tooltip off the cursor.
      const caret = canvas.getBoundingClientRect();
      const placed = tip.getBoundingClientRect();
      const caretX = caret.left + 20 * caret.width / 300, caretY = caret.top + 30 * caret.height / 150;
      result.chartClearsCaret = placed.right < caretX || placed.left > caretX
        || placed.bottom < caretY || placed.top > caretY;
      chartTooltip({ chart, tooltip: { opacity: 0 } });
      await new Promise(resolve => setTimeout(resolve, 160));
      result.chartHide = tip.hidden;
      one.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "touch" }));
      result.touch = !tip.hidden;
      hideTooltip();
      canvas.setAttribute("aria-label", "Cost");
      const actual = new Chart(canvas, {
        type: "bar",
        data: { labels: ["Compute", "Storage"], datasets: [{ label: "Cost USD", data: [125.5, 38] }] },
        options: {
          animation: false, responsive: false, indexAxis: "y",
          plugins: { tooltip: {
            enabled: false, external: chartTooltip,
            callbacks: { label: (item) => `${item.dataset.label}: ${item.parsed.x}` },
          } },
        },
      });
      let selectedIndex = null;
      wireChartTooltip(actual, index => { selectedIndex = index; });
      canvas.focus();
      result.chartKeyboard = tip.textContent === "Compute\nCost USD: 125.5";
      canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      result.chartArrow = tip.textContent === "Storage\nCost USD: 38";
      canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      result.chartSelection = selectedIndex === 1;
      canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      result.chartEscape = tip.hidden;
      actual.destroy();
      hideTooltip();
      // A re-render inserts a node and restores focus to it in the same synchronous block. The
      // observer that moves `title` into `data-tooltip` is a microtask, so it has not run yet:
      // if the focusin handler only looks for `[data-tooltip]`, the element ends up focused,
      // described by a tooltip, and silent. Focus here without awaiting a tick, deliberately.
      const fresh = document.createElement("button");
      fresh.id = "fresh";
      fresh.title = "Adopted on demand";
      fresh.textContent = "Fresh";
      document.body.appendChild(fresh);
      fresh.focus();
      result.focusBeforeAdoption = !tip.hidden && tip.textContent === "Adopted on demand";
      // Scroll events arrive at the next rendering opportunity, so the scroll that brings a
      // control into view is delivered after the focus that opened its tooltip. Tabbing to
      // anything below the fold did exactly this and read nothing.
      document.dispatchEvent(new Event("scroll", { bubbles: false }));
      result.focusSurvivesScroll = !tip.hidden && tip.textContent === "Adopted on demand";
      // A hover tooltip has no focused owner, so scrolling still takes it away: it is anchored
      // to a rectangle the pointer has been left behind by.
      fresh.blur();
      one.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, pointerType: "mouse" }));
      const hovering = !tip.hidden;
      document.dispatchEvent(new Event("scroll", { bubbles: false }));
      result.hoverStillHidesOnScroll = hovering && tip.hidden;
      fresh.remove();
      return result;
    });
    for (const [name, value] of Object.entries(results)) {
      check(name, name === "feedback_ms" ? value < 100 : value);
    }
    check("zero hover requests", requests === 0);
    if (failures.length) throw new Error(`Tooltip regressions: ${failures.join(", ")}; ${JSON.stringify(results)}`);
    return { ...results, hover_requests: requests };
  } finally {
    await context.close();
  }
}
