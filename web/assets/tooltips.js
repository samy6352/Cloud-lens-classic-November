/** One local-only tooltip surface for controls, SVGs and Chart.js. */
const ID = "cl-tooltip";
// Half the box kept clear around a line chart's caret and around the pointer itself: a little
// wider than the cursor hotspot, so the tooltip clears the hand without being flung across the
// plot. The caret sits on the data point, which is not where the pointer necessarily is — on a
// flat series you can be hovering the bottom of the plot while the caret is up on the line.
const CARET_CLEARANCE = 20;
// Last mouse position over each chart canvas. Chart.js's external callback reports the caret,
// never the cursor, and a tooltip placed clear of one can still land squarely on the other.
const pointers = new WeakMap();
let tip;
let owner;
let dismissed;
let hideTimer;
let wired = false;

function surface() {
  if (!tip) {
    tip = document.createElement("div");
    tip.id = ID;
    tip.className = "cl-tooltip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    tip.addEventListener("pointerenter", () => clearTimeout(hideTimer));
    tip.addEventListener("pointerleave", () => hideTooltip());
    document.body.appendChild(tip);
  }
  return tip;
}

export function hideTooltip(target) {
  if (target && target !== owner) return;
  clearTimeout(hideTimer);
  if (owner) {
    const ids = (owner.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== ID);
    if (ids.length) owner.setAttribute("aria-describedby", ids.join(" "));
    else owner.removeAttribute("aria-describedby");
  }
  owner = null;
  if (tip) tip.hidden = true;
}

export function showTooltip(target, text, point) {
  if (!target?.isConnected || !text || dismissed === target) return;
  clearTimeout(hideTimer);
  const el = surface();
  if (owner !== target) {
    hideTooltip();
    owner = target;
    const ids = new Set((target.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    ids.add(ID);
    target.setAttribute("aria-describedby", [...ids].join(" "));
  }
  if (el.textContent !== text) el.textContent = text;
  el.hidden = false;
  const rect = target.getBoundingClientRect();
  const x = point?.x ?? rect.left + rect.width / 2;
  const below = point?.y ?? rect.bottom;
  const margin = 8;
  let left = Math.max(margin, Math.min(x - el.offsetWidth / 2, innerWidth - el.offsetWidth - margin));
  let top = below + margin + el.offsetHeight <= innerHeight - margin
    ? below + margin
    : Math.max(margin, (point?.y ?? rect.top) - el.offsetHeight - margin);
  if (!point && target.matches("[data-main-section]")
      && target.closest('[aria-orientation="vertical"]')) {
    // A rail tooltip belongs beside the rail, not over the next destination.
    left = Math.max(margin, Math.min(rect.right + margin, innerWidth - el.offsetWidth - margin));
    top = Math.max(margin, Math.min(rect.top + (rect.height - el.offsetHeight) / 2,
      innerHeight - el.offsetHeight - margin));
  }
  if (point?.avoid) {
    // Keep chart tooltips outside the bar column so neither the current segment
    // nor its neighbours lose pointer events to the HTML overlay.
    const area = point.avoid, width = el.offsetWidth, height = el.offsetHeight;
    const clampX = value => Math.max(margin, Math.min(value, innerWidth - width - margin));
    const clampY = value => Math.max(margin, Math.min(value, innerHeight - height - margin));
    const candidates = [
      { left, top: area.top - height - margin },
      { left, top: area.bottom + margin },
      { left: area.right + margin, top: clampY((area.top + area.bottom - height) / 2) },
      { left: area.left - width - margin, top: clampY((area.top + area.bottom - height) / 2) },
    ];
    const placed = candidates.find(candidate => candidate.left >= margin && candidate.top >= margin
      && candidate.left + width <= innerWidth - margin && candidate.top + height <= innerHeight - margin);
    if (placed) ({ left, top } = placed);
    else {
      left = clampX(left);
      top = clampY(area.top - height - margin);
    }
  }
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  // Hover is worth having only when there is something to scroll to. Granting it to a two-line
  // hint costs a click on whatever the tooltip happens to be covering.
  el.style.pointerEvents = el.scrollHeight > el.clientHeight ? "auto" : "none";
}

function adoptTitle(el) {
  if (!el.hasAttribute("title")) return;
  const text = el.getAttribute("title");
  if (text) {
    el.dataset.tooltip = text;
    // Icon-only controls may have relied on the native title as their accessible name.
    if (!el.getAttribute("aria-label") && !el.getAttribute("aria-labelledby")
        && !el.textContent.trim() && el.matches("button,a,[role=button],[tabindex]")) {
      el.setAttribute("aria-label", text);
    }
  } else {
    delete el.dataset.tooltip;
  }
  el.removeAttribute("title");
}

function adoptTree(root) {
  if (!(root instanceof Element)) return;
  adoptTitle(root);
  root.querySelectorAll("[title]").forEach(adoptTitle);
}

function trigger(event) {
  if (!(event.target instanceof Element)) return null;
  // A node inserted a moment ago may not have been adopted yet: the observer that moves `title`
  // into `data-tooltip` runs as a microtask, while a re-render that restores focus does it
  // synchronously -- so a focusin can arrive before its own trigger has a `data-tooltip` to be
  // found by. The element ends up focused, described, and silent. Adopting on demand here makes
  // the lookup independent of that ordering rather than dependent on winning the race.
  const pending = event.target.closest("[title]");
  if (pending) adoptTitle(pending);
  return event.target.closest("[data-tooltip]");
}

export function wireTooltips() {
  if (wired) return;
  wired = true;
  adoptTree(document.body);
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "attributes") {
        adoptTitle(record.target);
        if (record.target === owner) {
          if (owner.dataset.tooltip) showTooltip(owner, owner.dataset.tooltip);
          else hideTooltip();
        }
      }
      else record.addedNodes.forEach(adoptTree);
    }
    if (owner && !owner.isConnected) hideTooltip();
  });
  observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["title"] });
  document.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    const target = trigger(event);
    if (target && !target.contains(event.relatedTarget)) {
      dismissed = null;
      showTooltip(target, target.dataset.tooltip);
    }
  });
  document.addEventListener("pointerout", (event) => {
    const target = trigger(event);
    if (target && !target.contains(event.relatedTarget)) {
      dismissed = null;
      hideTimer = setTimeout(() => hideTooltip(target), 120);
    }
  });
  document.addEventListener("focusin", (event) => {
    const target = trigger(event);
    dismissed = null;
    if (target) showTooltip(target, target.dataset.tooltip);
  });
  document.addEventListener("focusout", (event) => {
    dismissed = null;
    hideTooltip(event.target);
  });
  document.addEventListener("pointerdown", (event) => {
    const target = trigger(event);
    if (event.pointerType === "touch" && target && target !== owner) {
      dismissed = null;
      showTooltip(target, target.dataset.tooltip);
    } else if (!tip?.contains(event.target)) {
      hideTooltip();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dismissed = owner;
      hideTooltip();
    }
  });
  document.addEventListener("scroll", (event) => {
    if (event.target === tip) return;
    // A scroll event is delivered at the next rendering opportunity rather than when the
    // scrolling happened, so the scroll that brings a control into view lands *after* the focus
    // that opened its tooltip -- and dismissed it. Keyboard navigation does exactly that to
    // every control below the fold: tab to it, read nothing.
    //
    // Hiding on scroll exists because the tooltip is positioned against a rectangle that moves.
    // When the thing it describes still has focus, that rectangle is the only thing that went
    // stale: re-anchor it rather than take it away. A hover tooltip has no focused owner and
    // still hides, which is what should happen when the pointer has been left behind.
    if (owner?.isConnected && owner === document.activeElement && owner.dataset.tooltip) {
      showTooltip(owner, owner.dataset.tooltip);
      return;
    }
    hideTooltip();
  }, true);
  window.addEventListener("resize", () => hideTooltip());
}

export function chartTooltip({ chart, tooltip }) {
  if (!tooltip.opacity) {
    clearTimeout(hideTimer);
    // Chart.js handles mouseout on its next animation frame, possibly after the
    // pointer has already entered the tooltip and cancelled the previous timer.
    if (owner !== chart.canvas || !tip?.matches(":hover")) {
      hideTimer = setTimeout(() => hideTooltip(chart.canvas), 120);
    }
    dismissed = null;
    return;
  }
  const lines = [
    ...(tooltip.title || []),
    ...(tooltip.beforeBody || []),
    ...(tooltip.body || []).flatMap((body) => [...(body.before || []), ...body.lines, ...(body.after || [])]),
    ...(tooltip.afterBody || []),
    ...(tooltip.footer || []),
  ];
  const rect = chart.canvas.getBoundingClientRect();
  const caret = {
    x: rect.left + tooltip.caretX * rect.width / chart.width,
    y: rect.top + tooltip.caretY * rect.height / chart.height,
  };
  const isBar = element =>
    Number.isFinite(element?.base) && Number.isFinite(element.width) && Number.isFinite(element.height);
  const points = (tooltip.dataPoints || []).filter(item => isBar(item.element));
  const bars = chart.data && chart.getDatasetMeta
    ? [...new Set(points.map(item => item.dataIndex))].flatMap(index =>
      chart.data.datasets.flatMap((_, datasetIndex) => chart.isDatasetVisible(datasetIndex)
        ? [chart.getDatasetMeta(datasetIndex).data[index]].filter(isBar) : []))
    : points.map(item => item.element);
  const bounds = bars.map(element => {
    const { x, y, base, width, height, horizontal } = element;
    return {
      left: rect.left + (horizontal ? Math.min(x, base) : x - width / 2) * rect.width / chart.width,
      right: rect.left + (horizontal ? Math.max(x, base) : x + width / 2) * rect.width / chart.width,
      top: rect.top + (horizontal ? y - height / 2 : Math.min(y, base)) * rect.height / chart.height,
      bottom: rect.top + (horizontal ? y + height / 2 : Math.max(y, base)) * rect.height / chart.height,
    };
  });
  const avoid = bounds.length ? {
    left: Math.min(...bounds.map(bound => bound.left)), right: Math.max(...bounds.map(bound => bound.right)),
    top: Math.min(...bounds.map(bound => bound.top)), bottom: Math.max(...bounds.map(bound => bound.bottom)),
  } : {
    // A line or area chart has no bar to clear, so the caret is the only anchor there is.
    left: caret.x - CARET_CLEARANCE, right: caret.x + CARET_CLEARANCE,
    top: caret.y - CARET_CLEARANCE, bottom: caret.y + CARET_CLEARANCE,
  };
  const cursor = pointers.get(chart.canvas);
  if (cursor) {
    avoid.left = Math.min(avoid.left, cursor.x - CARET_CLEARANCE);
    avoid.right = Math.max(avoid.right, cursor.x + CARET_CLEARANCE);
    avoid.top = Math.min(avoid.top, cursor.y - CARET_CLEARANCE);
    avoid.bottom = Math.max(avoid.bottom, cursor.y + CARET_CLEARANCE);
  }
  showTooltip(chart.canvas, lines.join("\n"), { x: caret.x, y: caret.y, avoid });
}

export function wireChartTooltip(chart, onSelect) {
  const canvas = chart.canvas;
  canvas.addEventListener("pointermove", (event) => {
    if (event.pointerType !== "touch") pointers.set(canvas, { x: event.clientX, y: event.clientY });
  }, { passive: true });
  // Keyboard navigation moves the caret with no cursor involved; a stale position would push
  // those tooltips away from wherever the mouse was last left.
  canvas.addEventListener("pointerleave", () => pointers.delete(canvas));
  canvas.addEventListener("pointerenter", () => {
    if (dismissed === canvas) dismissed = null;
  });
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-label", `${canvas.getAttribute("aria-label")} Use arrow keys for values.${onSelect ? " Press Enter for details." : ""}`);
  let index = 0;
  const show = () => {
    const active = chart.data.datasets.flatMap((series, datasetIndex) =>
      chart.isDatasetVisible(datasetIndex) && series.data[index] != null
        ? [{ datasetIndex, index }] : []);
    if (!active.length) return;
    dismissed = null;
    chart.setActiveElements(active);
    chart.tooltip.setActiveElements(active, { x: chart.width / 2, y: chart.height / 2 });
    chart.update("none");
  };
  canvas.addEventListener("focus", show);
  canvas.addEventListener("keydown", (event) => {
    const last = chart.data.labels.length - 1;
    if (last < 0) return;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") index = Math.min(last, index + 1);
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") index = Math.max(0, index - 1);
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = last;
    else if ((event.key === "Enter" || event.key === " ") && onSelect) {
      event.preventDefault();
      hideTooltip(canvas);
      onSelect(index);
      return;
    }
    else return;
    event.preventDefault();
    show();
  });
  canvas.addEventListener("blur", () => {
    chart.setActiveElements([]);
    chart.tooltip.setActiveElements([], { x: 0, y: 0 });
    hideTooltip(canvas);
    chart.update("none");
  });
}
