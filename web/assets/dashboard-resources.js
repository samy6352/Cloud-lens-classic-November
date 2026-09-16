import { railIcon } from "./icons.js";
import { periodName } from "./reporting-period.js";
import { captureInteractions, restoreInteractions } from "./dashboard-state.js";
import { sumCostsByCurrency } from "./cost-totals.js";

const emptyFilters = () => ({ query: "", service: "", change: "", tags: "", currency: "" });
const normalized = value => String(value ?? "").trim().toLowerCase();
const facetKey = value => normalized(value) ? `value:${normalized(value)}` : "missing";
const currencyOf = row => String(row.currency || "").trim().toUpperCase() || "UNKNOWN";
const servicesOf = row => Array.isArray(row.service_names) && row.service_names.length ? row.service_names : [row.service];

export function resourceFacets(rows, field) {
  const values = new Map();
  for (const row of rows) {
    const entries = field === "service" ? servicesOf(row) : [field === "currency" ? currencyOf(row) : row[field]];
    for (const value of entries) values.set(facetKey(value), String(value || "Not recorded"));
  }
  return [...values].map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label));
}

export function resourceChange(row) {
  if (!Number.isFinite(row.cost) || !Number.isFinite(row.prior_cost)) return "unavailable";
  if (row.prior_cost === 0) return "no-baseline";
  const delta = row.cost - row.prior_cost;
  return Math.abs(delta) < 0.01 ? "unchanged" : delta > 0 ? "increased" : "decreased";
}

export function filterResources(rows, filters = {}) {
  const query = normalized(filters.query);
  return rows.filter(row => {
    if (filters.service && !servicesOf(row).some(service => facetKey(service) === filters.service)) return false;
    if (filters.currency && facetKey(currencyOf(row)) !== filters.currency) return false;
    if (filters.change && resourceChange(row) !== filters.change) return false;
    const tags = Array.isArray(row.tags) ? row.tags : [];
    if (filters.tags === "recorded" && !tags.length) return false;
    if (filters.tags === "missing" && tags.length) return false;
    return !query || normalized([row.name, row.id, row.resource_group, row.subscription,
      row.subscription_id, ...servicesOf(row), row.area, row.region,
      ...tags.flatMap(tag => [tag.key, tag.value])].join(" ")).includes(query);
  });
}

export function resourceSummary(rows) {
  return {
    resources: new Set(rows.map(row => normalized(row.id))).size,
    groups: new Set(rows.filter(row => row.resource_group).map(row =>
      `${normalized(row.subscription_id || row.subscription)}|${normalized(row.resource_group)}`)).size,
    services: new Set(rows.flatMap(servicesOf).filter(Boolean).map(normalized)).size,
    costs: sumCostsByCurrency(rows),
  };
}

export function createTabs(ctx) {
  const { $, cached, esc, get, key, kpiRow, moneyHtml, portalLinkMarkup, qs, setContent, table, wireTables } = ctx;
  let filters = emptyFilters(), timer = null, pending = false, repaint = null;
  const amount = (value, currency) => {
    if (!Number.isFinite(value)) return '<span class="muted">Unavailable</span>';
    return currency === "UNKNOWN"
      ? `${esc(value.toLocaleString(undefined, { maximumFractionDigits: 2 }))} <small class="muted">currency unknown</small>`
      : moneyHtml(value, currency, 2);
  };
  const costTotals = costs => costs.map(total =>
    `<span class="resource-total">${amount(total.amount, total.currency)}${total.missing ? ' <small class="muted">(partial)</small>' : ""}</span>`
  ).join("") || '<span class="muted">No matches</span>';
  const select = (field, label, options) => {
    if (filters[field] && !options.some(option => option.value === filters[field])) {
      options = [...options, { value: filters[field], label: `${filters[field].replace(/^value:/, "")} (not in this load)` }];
    }
    return `<label>${esc(label)}
    <select data-resource-filter="${field}" aria-label="${esc(label)}">
      <option value="">All</option>${options.map(option => `<option value="${esc(option.value)}"${
        filters[field] === option.value ? " selected" : ""}>${esc(option.label)}</option>`).join("")}
    </select></label>`;
  };
  // An absent value reads as a dash with the reason kept for assistive technology, rather than
  // as prose that repeats down the column and outweighs the figures beside it.
  const absent = (reason = "Not recorded") =>
    `<span class="cell-empty" title="${esc(reason)}"><span aria-hidden="true">—</span><span class="sr-only">${esc(reason)}</span></span>`;
  const secondary = text => text
    ? `<span class="resource-secondary" title="${esc(text)}">${esc(text)}</span>`
    : `<span class="resource-secondary">${absent()}</span>`;

  async function renderResources(token) {
    const data = await cached(key("resources"), () => get(`/api/dashboard/resources${qs()}`, { timeout: 45000 }));
    if (token !== ctx.loadToken) return;
    if (!data || !Array.isArray(data.resources)) throw new Error("Resource data is unavailable or incomplete. Refresh to retry.");
    const rows = data.resources;
    const period = periodName(data.days || ctx.days, ctx.periodMonth);
    const fixed = values => values.map(([value, label]) => ({ value, label }));
    const previous = $("resourceExplorer");
    const interactions = previous ? captureInteractions(previous) : null;
    setContent($("tabBody"), `<div class="resource-explorer" id="resourceExplorer">
      <section class="portal-workbench" aria-label="Resource summary and filters">
      <div class="resource-context"><span>Cost-backed resources</span><span>${esc(period)}</span>
        ${data.as_of ? `<span>Data through ${esc(data.as_of)}</span>` : ""}</div>
      ${data.truncated ? `<p class="banner warn" role="status">Partial results: ${rows.length.toLocaleString()} resource cost records loaded. Narrow the subscription or global filters to see the rest.</p>` : ""}
      ${resourceSummary(rows).costs.length > 1 ? '<p class="resource-notice">Multiple currencies: totals stay separate. Select a billing currency to compare resource costs.</p>' : ""}
      <div class="resource-summary" id="resourceSummary"></div>
      <div class="resource-toolbar" role="search" aria-label="Find resources">
        <label class="resource-search">Search resources<input type="search" id="resourceSearch"
          aria-label="Search resources" placeholder="Name, group, subscription, ID or tag"
          maxlength="200" value="${esc(filters.query)}"></label>
        ${select("service", "Service", resourceFacets(rows, "service"))}
        ${select("change", "Cost change", fixed([["increased", "Increased"], ["decreased", "Decreased"],
          ["unchanged", "Unchanged"], ["no-baseline", "No prior spend"], ["unavailable", "Unknown"]]))}
        ${select("tags", "Tag evidence", fixed([["recorded", "Tags recorded"], ["missing", "No tags recorded"]]))}
        ${select("currency", "Billing currency", resourceFacets(rows, "currency"))}
      </div>
      <div class="resource-result-bar"><span id="resourceResultCount" role="status" aria-live="polite"></span>
        <button type="button" class="ghost" id="resourceClear">Clear filters</button></div>
      </section>
      <div class="resource-results" id="resourceResults"></div>
      <details class="view-help" data-state-key="resource-coverage"><summary>About these resources</summary>
        <p>Resources observed in cost data for the selected and comparison periods, not live Azure inventory.
          Unbilled resources may be absent. Global subscription, period and dimension filters still apply.</p>
        <p>No prior spend means no non-zero baseline, not that a resource was newly created.
          No tags recorded means tags were absent from the loaded cost data, not confirmed missing in Azure.
          Open Details for resource IDs, tags and billing dates.</p>
      </details></div>`);
    const root = $("resourceExplorer");
    const costChange = row => {
      const change = resourceChange(row);
      if (change === "unavailable") return '<span class="muted">Unknown</span>';
      if (change === "no-baseline") return '<span class="muted">No prior spend</span>';
      if (change === "unchanged") return '<span class="delta flat">Unchanged</span>';
      const delta = row.cost - row.prior_cost;
      return `<span class="delta ${delta > 0 ? "up" : "down"}">${delta > 0 ? "+" : "-"}${amount(Math.abs(delta), currencyOf(row))}</span>
        ${secondary(Number.isFinite(row.change_pct) ? `${row.change_pct > 0 ? "+" : ""}${row.change_pct}% vs prior` : "vs prior period")}`;
    };
    repaint = (resetPage = false) => {
      pending = false;
      if (!root.isConnected) return;
      const filtered = filterResources(rows, filters), summary = resourceSummary(filtered);
      const totalMarkup = costTotals(summary.costs);
      setContent(root.querySelector("#resourceSummary"), kpiRow([
        { label: "Resources", value: summary.resources.toLocaleString(), sub: data.truncated ? "in partial results" : "matching this view" },
        { label: "Period cost", value: totalMarkup, sub: "resource-attributed cost" },
        { label: "Resource groups", value: summary.groups.toLocaleString(), sub: "across selected subscriptions" },
        { label: "Services", value: summary.services.toLocaleString(), sub: "in the matching resources" },
      ]));
      root.querySelector("#resourceResultCount").textContent = `${filtered.length.toLocaleString()} of ${rows.length.toLocaleString()} resource cost records`;
      root.querySelector("#resourceClear").disabled = !Object.values(filters).some(Boolean);
      const results = root.querySelector("#resourceResults");
      setContent(results, filtered.length ? table("Resource explorer", filtered, [
        { label: "Resource", value: row => `${row.name} ${row.service || ""}`, cell: row =>
          `<div class="resource-identity"><span class="resource-icon" aria-hidden="true">${railIcon(row.area_id || "resources")}</span>
            <span class="resource-name">${portalLinkMarkup(row, ctx.portalScopeIds(), row.name || row.id)}${secondary(row.service)}</span></div>` },
        { label: "Group / region", value: row => `${row.resource_group || ""} ${row.region || ""}`,
          cell: row => `<span class="resource-primary" title="${esc(row.resource_group || "")}">${row.resource_group ? esc(row.resource_group) : absent()}</span>${secondary(row.region)}` },
        { label: "Subscription", value: row => row.subscription || row.subscription_id || "",
          cell: row => `<span class="resource-primary" title="${esc(row.subscription || row.subscription_id || "")}">${row.subscription || row.subscription_id ? esc(row.subscription || row.subscription_id) : absent()}</span>` },
        { label: "Period cost", num: true, value: row => row.cost, cell: row => amount(row.cost, currencyOf(row)),
          total: () => totalMarkup },
        { label: "Change", num: true, value: row => Number.isFinite(row.cost) && Number.isFinite(row.prior_cost) ? row.cost - row.prior_cost : null,
          cell: costChange },
        { label: "Tags", value: row => Array.isArray(row.tags) ? row.tags.length : 0,
          cell: row => Array.isArray(row.tags) && row.tags.length ? `<span class="resource-tag-count">${row.tags.length} ${row.tags.length === 1 ? "tag" : "tags"}</span>`
            : absent("No tags recorded") },
      ], null, { key: "resource-explorer", pageSize: 20, search: false, scroll: false, resetPage })
        : `<section class="card resource-empty"><h3>${rows.length ? "No matching resources" : "No resource cost records"}</h3>
          <p class="muted">${rows.length ? "Change or clear the resource filters to broaden the results." :
            "No resource-attributed cost records were found for the selected scope and periods. Review the global filters or load cost data."}</p></section>`);
      wireTables(results);
    };
    root.querySelector("#resourceSearch").oninput = event => {
      filters.query = event.target.value;
      clearTimeout(timer);
      pending = true;
      timer = setTimeout(() => repaint(true), 120);
    };
    for (const control of root.querySelectorAll("[data-resource-filter]")) {
      control.onchange = () => {
        clearTimeout(timer);
        filters[control.dataset.resourceFilter] = control.value;
        repaint(true);
      };
    }
    root.querySelector("#resourceClear").onclick = () => {
      clearTimeout(timer);
      filters = emptyFilters();
      root.querySelector("#resourceSearch").value = "";
      for (const control of root.querySelectorAll("[data-resource-filter]")) control.value = "";
      repaint(true);
      root.querySelector("#resourceSearch").focus();
    };
    repaint();
    // Restore once both the controls and the shared table's expanded rows exist.
    restoreInteractions(root, interactions);
  }
  return { renderResources, stop: () => clearTimeout(timer), resume: () => { if (pending) repaint?.(true); } };
}
