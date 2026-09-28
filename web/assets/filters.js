/**
 * The filter bar.
 *
 * Six dimensions in one row, each a multi-select populated from the data in view. The estate
 * decides what is offered: a filter listing regions nobody runs in, or an `env` tag nobody
 * sets, invites someone to narrow to nothing and then wonder which of the two is broken. So a
 * dimension with no values does not render at all.
 *
 * The selection lives in the query string of every dashboard request and is applied by the
 * warehouse's scope view — the same mechanism that enforces subscription entitlement. That is
 * why filtering did not need two dozen query modules to learn about it: every tab, the export,
 * the report and the agent read through the same view.
 *
 * Ordered by spend, not alphabetically. The resource group costing the most is the one someone
 * is most likely to be looking for, and an alphabetical list buries it among a hundred others.
 */
import { esc, get, money, onScopeChange, readStore, scopeIds, writeStore } from "./app.js";
import { periodParams } from "./reporting-period.js";

const $ = (id) => document.getElementById(id);

/** Dimension order and labels. `env` is last because plenty of estates do not tag for it. */
const DIMS = [
  { key: "service", label: "Service" },
  { key: "rg", label: "Resource group" },
  { key: "region", label: "Region" },
  { key: "charge", label: "Cost type" },
  { key: "pricing", label: "Pricing" },
  { key: "env", label: "Environment" },
];

let available = {};                                   // dimension -> [{value, cost}]
let unavailableDimensions = [];
let chosen = readStore("costFilters", {}) || {};      // dimension -> [value]
let onChange = () => {};
let currency = "";
let availableCurrency = "";
let filterError = "";
let ready = false;
let queuedDays;
let queuedMonth = "";
let changeTimer = null;
let requestController = null;
let pendingKey = "";
let pendingRequest = null;

/** The selection as query-string pairs, for whoever is building a dashboard request. */
export function filterQuery() {
  return DIMS
    .map(({ key }) => {
      const picked = chosen[key] || [];
      return picked.length ? `${key}=${encodeURIComponent(picked.join(","))}` : "";
    })
    .filter(Boolean);
}

/** How many dimensions are narrowed. Used to decide whether "Clear all" is worth offering. */
export function activeFilterCount() {
  return DIMS.reduce((n, { key }) => n + ((chosen[key] || []).length ? 1 : 0), 0);
}

/** Drop every selection. Offered from the bar and from the empty state a filter can cause —
 *  a dead end that does not carry its own exit is only half an answer. */
export function clearAllFilters() {
  chosen = {};
  writeStore("costFilters", chosen);
  render();
  notifyChange(true);
}

/**
 * Narrow one dimension to a single value, from somewhere other than the bar itself.
 *
 * The point of exposing this is that a chart is a better filter control than a dropdown: seeing
 * "Azure AI Search fell $141" and clicking it is one gesture, where finding the same service in
 * a list of forty is a search. Written through the same `chosen` map the bar renders from, so
 * the bar immediately shows the narrowing and its "Clear all" undoes it — a filter applied from
 * a chart must not become a state the visible controls disagree with.
 *
 * Returns false when the estate has no such value, so a caller can leave the row inert rather
 * than offering a click that narrows to nothing.
 */
export function applyFilter(dim, value, { notify = true } = {}) {
  if (!DIMS.some(d => d.key === dim)) return false;
  const match = (available[dim] || [])
    .find(o => String(o.value).toLowerCase() === String(value).toLowerCase());
  if (!match) return false;
  chosen = { ...chosen, [dim]: [String(match.value)] };
  writeStore("costFilters", chosen);
  render();
  if (notify) notifyChange(true);
  return true;
}

/** Whether `applyFilter` would find something — asked before a click is offered. */
export function filterHasValue(dim, value) {
  return (available[dim] || [])
    .some(o => String(o.value).toLowerCase() === String(value).toLowerCase());
}

export function wireFilters(handler, days, month = "") {
  onChange = handler || (() => {});
  queuedDays = days;
  queuedMonth = month;
  $("fbClear").onclick = () => clearAllFilters();
  // The values on offer depend on the subscriptions in view, so they are re-read when the
  // picker changes. Without this, deselecting a subscription leaves its resource groups in the
  // list and narrowing by one returns nothing.
  onScopeChange(() => {
    if (!ready) return;
    // Scope changes also redraw the dashboard. Let that critical read finish before asking the
    // same database for secondary menu values.
    afterDashboard(() => loadFilters(queuedDays));
  });
  const start = () => {
    ready = true;
    loadFilters(queuedDays);
  };
  if (["true", "error"].includes(document.documentElement.dataset.dashboardReady)) start();
  else afterDashboard(start);
}

function afterDashboard(callback) {
  const done = () => {
    window.removeEventListener("cl:dashboard-ready", done);
    window.removeEventListener("cl:dashboard-error", done);
    callback();
  };
  window.addEventListener("cl:dashboard-ready", done, { once: true });
  window.addEventListener("cl:dashboard-error", done, { once: true });
}

export async function loadFilters(days, month = queuedMonth) {
  queuedDays = days ?? queuedDays;
  queuedMonth = month;
  if (!ready) return;
  const scope = scopeIds();
  const requestedCurrency = currency;
  const requestKey = `${queuedDays || ""}|${queuedMonth}|${scope.join(",")}|${requestedCurrency}`;
  // The owning call renders failures; duplicate callers must not leak the raw rejection.
  if (pendingRequest && pendingKey === requestKey) return pendingRequest.catch(() => {});
  requestController?.abort();
  requestController = new AbortController();
  const controller = requestController;
  pendingKey = requestKey;
  const qs = "?" + [
    String(periodParams(queuedDays || 30, queuedMonth)),
    scope.length ? `scope=${encodeURIComponent(scope.join(","))}` : "",
    requestedCurrency ? `currency=${encodeURIComponent(requestedCurrency)}` : "",
  ].filter(Boolean).join("&");
  pendingRequest = get(`/api/filters${qs}`, {
    timeout: 45000,
    signal: requestController.signal,
  });
  try {
    const r = await pendingRequest;
    if (controller !== requestController) return;
    if (r.unavailable) throw new Error("Filter choices are unavailable.");
    available = r.dimensions || {};
    unavailableDimensions = (r.unavailable_dimensions || []).filter(key => DIMS.some(dim => dim.key === key));
    availableCurrency = r.currency || "";
    filterError = "";
    const previous = filterQuery().join("&");
    // Anything selected that the estate no longer contains is dropped rather than kept.
    // A filter that cannot match is indistinguishable from a broken dashboard.
    for (const { key } of DIMS) {
      if (unavailableDimensions.includes(key)) continue;
      const have = new Set((available[key] || []).map((o) => String(o.value).toLowerCase()));
      if (chosen[key]) {
        chosen[key] = chosen[key].filter((v) => have.has(String(v).toLowerCase()));
        if (!chosen[key].length) delete chosen[key];
      }
    }
    writeStore("costFilters", chosen);
    render();
    if (previous !== filterQuery().join("&")) notifyChange(true);
  } catch (err) {
    if (controller !== requestController || controller.signal.aborted) return;
    filterError = "Filter choices could not refresh. Existing selections still apply.";
    render();
  } finally {
    if (controller === requestController) {
      pendingKey = "";
      pendingRequest = null;
    }
  }
}

function notifyChange(immediate = false) {
  clearTimeout(changeTimer);
  if (immediate) return onChange();
  changeTimer = setTimeout(onChange, 180);
}

function render() {
  const bar = $("filterBar");
  const usable = DIMS.filter(({ key }) => (available[key] || []).length > 1 || chosen[key]?.length);
  if (!usable.length && !filterError && !unavailableDimensions.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  $("fbSlots").innerHTML = usable.map(slot).join("");
  $("fbClear").hidden = activeFilterCount() === 0;
  const status = $("fbStatus");
  if (status) {
    const unavailable = unavailableDimensions.map(key => DIMS.find(dim => dim.key === key).label);
    const notice = filterError || (unavailable.length
      ? `${unavailable.join(", ")} filter choices are temporarily unavailable, not necessarily empty. Other filters remain usable.${unavailableDimensions.some(key => chosen[key]?.length) ? " Existing selections still apply." : ""}` : "");
    status.innerHTML = notice
      ? `${esc(notice)} <button type="button" class="linkish" id="fbRetry">Retry filters</button>` : "";
    if ($("fbRetry")) $("fbRetry").onclick = () => loadFilters(queuedDays, queuedMonth);
  }
  wireSlots();
}

function slot({ key, label }) {
  const picked = chosen[key] || [];
  const n = picked.length;
  // The button says what is selected, not just that something is. One value is named; several
  // are counted, because three resource-group names do not fit and truncating them tells you
  // less than the number does.
  const text = n === 0 ? label : n === 1 ? picked[0] : `${label} · ${n}`;
  return `<div class="fb-slot${n ? " on" : ""}" data-dim="${esc(key)}">
      <button type="button" class="fb-btn" aria-haspopup="listbox" aria-expanded="false"
              title="${esc(n ? `${label}: ${picked.join(", ")}` : `Filter by ${label.toLowerCase()}`)}">
        <span class="fb-text">${esc(text)}</span>
        <span class="fb-caret" aria-hidden="true">▾</span>
      </button>
      <div class="fb-menu" role="listbox" aria-multiselectable="true"
           aria-label="${esc(label)}" hidden>
        <div class="fb-search">
          <input type="search" placeholder="Search ${esc(label.toLowerCase())}…"
                 aria-label="Search ${esc(label.toLowerCase())}" />
        </div>
        <div class="fb-list"></div>
      </div>
    </div>`;
}

export function filterCostText(option, fallbackCurrency = "") {
  const buckets = option.currency_buckets && typeof option.currency_buckets === "object"
    ? Object.entries(option.currency_buckets).map(([currency, cost]) => ({ currency, cost }))
    : [{ cost: option.cost, currency: option.currency || fallbackCurrency }];
  return buckets.map(bucket => {
    if (!Number.isFinite(bucket.cost)) return "Not available";
    return /^[A-Z]{3}$/.test(bucket.currency || "")
      ? `${bucket.currency} ${money(bucket.cost, bucket.currency)}`
      : `${bucket.cost.toLocaleString()} (currency unknown)`;
  }).join(" · ") || "Not available";
}

function options(key) {
  const picked = new Set((chosen[key] || []).map((v) => String(v).toLowerCase()));
  const rows = [...(available[key] || [])];
  for (const value of chosen[key] || []) {
    if (!rows.some(row => String(row.value).toLowerCase() === String(value).toLowerCase())) rows.push({ value });
  }
  return rows.map((o) => {
    const v = String(o.value);
    const on = picked.has(v.toLowerCase());
    return `<label class="fb-opt${on ? " on" : ""}">
        <input type="checkbox" ${on ? "checked" : ""} value="${esc(v)}" />
        <span class="fb-opt-name">${esc(v)}</span>
        <span class="fb-opt-cost">${esc(filterCostText(o, availableCurrency))}</span>
      </label>`;
  }).join("");
}

function wireSlots() {
  for (const el of document.querySelectorAll(".fb-slot")) {
    const dim = el.dataset.dim;
    const btn = el.querySelector(".fb-btn");
    const menu = el.querySelector(".fb-menu");
    const search = el.querySelector("input[type=search]");
    const list = el.querySelector(".fb-list");
    menu.onclick = (e) => e.stopPropagation();

    btn.onclick = (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      closeAll();
      if (open && !list.childElementCount) {
        list.innerHTML = options(dim);
        wireBoxes(el, dim);
      }
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      if (open) search.focus();
    };

    let searchFrame = 0;
    search.oninput = () => {
      cancelAnimationFrame(searchFrame);
      searchFrame = requestAnimationFrame(() => {
        const q = search.value.trim().toLowerCase();
        for (const opt of el.querySelectorAll(".fb-opt")) {
          const name = opt.querySelector(".fb-opt-name").textContent.toLowerCase();
          opt.hidden = q && !name.includes(q);
        }
      });
    };
  }

  // One document-level handler rather than one per menu, and registered once per render.
  document.removeEventListener("click", closeAll);
  document.addEventListener("click", closeAll);
  document.removeEventListener("keydown", onEsc);
  document.addEventListener("keydown", onEsc);
}

function wireBoxes(el, dim) {
  for (const box of el.querySelectorAll("input[type=checkbox]")) {
    box.onchange = () => {
      const set = new Set(chosen[dim] || []);
      if (box.checked) set.add(box.value);
      else set.delete(box.value);
      if (set.size) chosen[dim] = [...set];
      else delete chosen[dim];
      writeStore("costFilters", chosen);
      // Keep the menu open so several values can be selected in one pass.
      box.closest(".fb-opt").classList.toggle("on", box.checked);
      paintSlot(el, dim);
      $("fbClear").hidden = activeFilterCount() === 0;
      notifyChange();
    };
  }
}

function paintSlot(el, dim) {
  const meta = DIMS.find((d) => d.key === dim);
  const picked = chosen[dim] || [];
  const n = picked.length;
  el.classList.toggle("on", n > 0);
  el.querySelector(".fb-text").textContent =
    n === 0 ? meta.label : n === 1 ? picked[0] : `${meta.label} · ${n}`;
}

function onEsc(e) {
  if (e.key === "Escape") closeAll();
}

function closeAll() {
  for (const m of document.querySelectorAll(".fb-menu")) {
    if (!m.hidden) {
      m.hidden = true;
      m.parentElement.querySelector(".fb-btn").setAttribute("aria-expanded", "false");
    }
  }
}

/** The display currency, so option costs read in the same units as the rest of the page. */
export function setFilterCurrency(code) {
  const next = code || "";
  if (currency === next) return;
  currency = next;
  if (ready) loadFilters(queuedDays, queuedMonth);
}
