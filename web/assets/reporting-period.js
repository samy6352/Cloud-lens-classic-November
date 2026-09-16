/** Calendar selection is explicit; rolling dates come from the warehouse, not the browser clock. */
export const ROLLING_DAYS = [7, 30, 60, 90];

export function currentMonth(now = new Date()) {
  return now.toISOString().slice(0, 7);
}

export function validMonth(value, now = new Date()) {
  return typeof value === "string" && /^[1-9]\d{3}-(0[1-9]|1[0-2])$/.test(value)
    && value <= currentMonth(now);
}

export function periodParams(days, month = "") {
  const params = new URLSearchParams({ days: String(days) });
  if (month) params.set("period_month", month);
  return params;
}

function dateLabel(value, options) {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined,
    { timeZone: "UTC", ...options });
}

export function periodName(days, month = "") {
  return month
    ? dateLabel(`${month}-01`, { month: "long", year: "numeric" })
    : `last ${days} days`;
}

export function periodLabel(period, selected) {
  const name = periodName(selected.days, period.month || selected.month);
  const range = period.from && period.to
    ? `${dateLabel(period.from, { day: "numeric", month: "short", year: "numeric" })} \u2013 ${
      dateLabel(period.to, { day: "numeric", month: "short", year: "numeric" })}`
    : "Dates unavailable";
  return `${name[0].toUpperCase()}${name.slice(1)} \u00b7 ${range}${
    period.empty || period.no_data ? " \u00b7 No cost data loaded" : ""}`;
}

export function rollingRangeLabel(period, days) {
  if (!period?.from || !period?.to) return `Last ${days} days`;
  const sameYear = period.from.slice(0, 4) === period.to.slice(0, 4);
  return `${dateLabel(period.from, { day: "numeric", month: "short", ...(!sameYear ? { year: "numeric" } : {}) })} \u2013 ${
    dateLabel(period.to, { day: "numeric", month: "short", year: "numeric" })}`;
}

export function wireMonthCalendar(input, now = () => new Date()) {
  const parent = input.parentElement;
  const range = parent.querySelector(".reporting-range");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "month-calendar-toggle";
  toggle.innerHTML = `<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" focusable="false">
    <rect x="3" y="4.5" width="14" height="12.5" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
    <path d="M6.5 3v3M13.5 3v3M3 8h14M7 11h.01M10 11h.01M13 11h.01M7 14h.01M10 14h.01"
      fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
  </svg>`;
  toggle.setAttribute("aria-label", "Choose reporting month");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-haspopup", "dialog");
  const popup = document.createElement("div");
  popup.className = "month-calendar";
  popup.id = "monthCalendar";
  popup.hidden = true;
  popup.setAttribute("role", "dialog");
  popup.setAttribute("aria-label", "Choose reporting month");
  toggle.setAttribute("aria-controls", popup.id);
  if (range) {
    range.setAttribute("aria-controls", popup.id);
    range.setAttribute("aria-haspopup", "dialog");
    range.setAttribute("aria-expanded", "false");
  }
  parent.append(toggle, popup);
  let year = Number(currentMonth(now()).slice(0, 4));
  const close = (focus = false) => {
    popup.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    range?.setAttribute("aria-expanded", "false");
    if (focus) toggle.focus();
  };
  const button = (text, label, handler) => {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = text;
    element.setAttribute("aria-label", label);
    element.onclick = event => { event.preventDefault(); handler(); };
    return element;
  };
  const render = () => {
    popup.replaceChildren();
    const head = document.createElement("div");
    head.className = "month-calendar-head";
    const previous = button("\u2039", "Previous year", () => { year--; render(); });
    previous.disabled = year <= 1000;
    const title = document.createElement("strong");
    title.textContent = String(year);
    title.setAttribute("aria-live", "polite");
    const next = button("\u203a", "Next year", () => { year++; render(); });
    next.disabled = year >= now().getUTCFullYear();
    head.append(previous, title, next);
    const grid = document.createElement("div");
    grid.className = "month-calendar-grid";
    for (let i = 1; i <= 12; i++) {
      const value = `${year}-${String(i).padStart(2, "0")}`;
      const month = button(dateLabel(`${value}-01`, { month: "short" }), periodName(30, value), () => {
        input.value = value;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        close(true);
      });
      month.disabled = !validMonth(value, now());
      month.setAttribute("aria-pressed", String(input.dataset.periodMode !== "rolling" && input.value === value));
      month.dataset.month = value;
      grid.append(month);
    }
    popup.append(head, grid);
  };
  const open = () => {
    year = Number((input.value || currentMonth(now())).slice(0, 4));
    render();
    popup.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    range?.setAttribute("aria-expanded", "true");
    (popup.querySelector('[aria-pressed="true"]')
      || popup.querySelector(`[data-month="${currentMonth(now())}"]`)
      || popup.querySelector(".month-calendar-grid button:not(:disabled)"))?.focus();
  };
  toggle.onclick = event => { event.preventDefault(); if (popup.hidden) open(); else close(); };
  input.onclick = event => { event.preventDefault(); open(); };
  if (range) range.onclick = event => { event.preventDefault(); open(); };
  input.addEventListener("keydown", event => {
    if (event.key === "ArrowDown" && event.altKey) { event.preventDefault(); open(); }
  });
  popup.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); }
  });
  document.addEventListener("click", event => { if (!parent.contains(event.target)) close(); });
  parent.addEventListener("focusout", event => { if (!parent.contains(event.relatedTarget)) close(); });
}

export function wireReportingPeriod({ root, input, caption, range, getState, onChange, getMetadata,
  now = () => new Date() }) {
  let generation = 0;
  let controller;
  let metadata = null;
  let metadataDays = null;
  const buttons = [...root.querySelectorAll("[data-days]")];
  const paint = () => {
    const state = getState();
    input.max = currentMonth(now());
    // A default calendar value is a suggestion until explicitly selected, not a rolling-filter change.
    input.value = state.month || input.max;
    input.setAttribute("data-period-mode", state.month ? "month" : "rolling");
    if (range) {
      range.hidden = Boolean(state.month);
      range.textContent = rollingRangeLabel(metadataDays === state.days ? metadata : null, state.days);
      range.setAttribute("aria-label", `${range.textContent}. Last ${state.days} days. Choose a calendar month.`);
    }
    input.title = state.month ? `Calendar month: ${periodName(state.days, state.month)}`
      : `Choose a calendar month; currently showing the last ${state.days} days.`;
    for (const button of buttons) {
      const active = !state.month && Number(button.dataset.days) === state.days;
      button.classList.toggle("on", active);
      button.setAttribute("aria-pressed", String(active));
      button.title = `Last ${button.dataset.days} days, ending at the latest loaded cost date`;
    }
  };
  for (const button of buttons) {
    button.onclick = () => {
      metadata = null;
      input.setCustomValidity("");
      onChange({ days: Number(button.dataset.days), month: "" });
      paint();
    };
  }
  input.onchange = () => {
    if (input.value && !validMonth(input.value, now())) {
      input.setCustomValidity("Choose the current month or an earlier month.");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    metadata = null;
    onChange({ ...getState(), month: input.value });
    paint();
  };
  input.oninput = () => input.setCustomValidity("");
  paint();
  return {
    async refresh() {
      const mine = ++generation;
      controller?.abort();
      controller = new AbortController();
      const selected = { ...getState() };
      metadata = null;
      paint();
      caption.hidden = false;
      caption.textContent = "Loading reporting dates...";
      caption.removeAttribute("title");
      try {
        const period = await getMetadata(controller.signal);
        const current = getState();
        if (mine !== generation || current.month !== selected.month || current.days !== selected.days) return;
        metadata = period;
        metadataDays = selected.days;
        paint();
        caption.textContent = periodLabel(period, selected);
        caption.title = "Inclusive cost dates (UTC). Live metrics and summary tiles retain their own labeled periods.";
      } catch (error) {
        const current = getState();
        if (mine !== generation || current.month !== selected.month || current.days !== selected.days
          || error?.name === "AbortError") return;
        caption.textContent = "";
        caption.hidden = true;
        console.warn("Could not load reporting dates:", error.message || error);
      }
    },
  };
}
