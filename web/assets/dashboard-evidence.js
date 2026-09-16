/** Read-only live evidence, with windows and data gaps separate from historical billing. */
export function metricText(metric) {
  if (metric.status !== "ok" || !Number.isFinite(metric.value)) {
    return { unsupported: "Not supported", no_data: "No observations", unavailable: "Unavailable" }[metric.status]
      || "Unavailable";
  }
  return `${metric.value.toLocaleString(undefined, { maximumFractionDigits: 3 })} ${metric.unit || ""}`.trim();
}

export function validEvidence(data, kind) {
  return data && ["ok", "partial", "unavailable"].includes(data.status)
    && Array.isArray(data[kind === "changes" ? "changes" : "resources"])
    && Array.isArray(data.notices) && data.window && typeof data.generated_at === "string";
}

const UNKNOWN = "__not_supplied__";
const emptyFilters = () => ({ query: "", type: "", group: "", currency: "", cost: "",
  actor: "", change: "", region: "", coverage: "" });

export function evidenceNotices(notices = []) {
  const background = [
    "Read-only Azure Resource Graph Change Analysis; change records are retained",
    "Only allowlisted infrastructure property values are shown;",
    "Cost uses billed calendar dates for the evidence window,",
    "Operation is the recorded RBAC authorization action,",
    "Deterministic ARM-ID sample of at most ",
  ];
  return {
    methodology: notices.filter(note => background.some(prefix => note.startsWith(prefix))),
    warnings: notices.filter(note => !background.some(prefix => note.startsWith(prefix))),
  };
}

export function evidenceField(row, field) {
  if (field === "type") return row.resource_type || row.type || "";
  if (field === "group") return (row.resource_id || row.id || "").match(/\/resourceGroups\/([^/]+)/i)?.[1] || "";
  if (field === "change") return row.change_type || "";
  return row[field] || "";
}

export function evidenceFacets(rows, field) {
  const options = new Map();
  for (const row of rows) {
    const values = field === "currency"
      ? (row.cost?.by_currency || []).map(entry => entry.currency)
      : [evidenceField(row, field)];
    for (const raw of values) {
      const value = String(raw || "").trim();
      options.set(value ? `value:${value.toLowerCase()}` : UNKNOWN, value || "Not supplied");
    }
  }
  return [...options].map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

export function filterEvidence(rows, filters = {}) {
  const query = (filters.query || "").trim().toLowerCase();
  const matches = (raw, selected) => !selected || selected === (
    String(raw || "").trim() ? `value:${String(raw).trim().toLowerCase()}` : UNKNOWN);
  return rows.filter(row => {
    if (["type", "group", "actor", "change", "region"]
      .some(field => !matches(evidenceField(row, field), filters[field]))) return false;
    const amounts = (row.cost?.by_currency || []).filter(entry => matches(entry.currency, filters.currency));
    if (filters.currency && !amounts.length) return false;
    const recorded = amounts.some(entry => Number.isFinite(entry.cost));
    if (filters.cost === "present" && !recorded) return false;
    if (filters.cost === "missing" && recorded) return false;
    if (filters.cost === "positive" && !amounts.some(entry => Number.isFinite(entry.cost) && entry.cost > 0)) return false;
    if (filters.cost === "zero" && (!amounts.length
      || !amounts.every(entry => Number.isFinite(entry.cost) && entry.cost === 0))) return false;
    const metrics = row.metrics || [];
    const measured = metrics.some(metric => metric.status === "ok" && Number.isFinite(metric.value));
    if (filters.coverage === "measured" && !measured) return false;
    if (filters.coverage === "missing" && measured) return false;
    if (filters.coverage === "partial" && row.status !== "partial") return false;
    if (filters.coverage === "failed" && !metrics.some(metric => metric.status === "unavailable")) return false;
    if (!query) return true;
    const searchable = [row.name, row.resource_name, row.resource_id, row.id,
      evidenceField(row, "type"), evidenceField(row, "group"), row.region, row.actor, row.operation,
      row.change_type, ...(row.properties || []).flatMap(property => [property.name, property.before, property.after]),
      ...metrics.map(metric => metric.label || metric.name)];
    return searchable.map(value => typeof value === "object" ? JSON.stringify(value) : value || "")
      .join(" ").toLowerCase().includes(query);
  });
}

export function createTabs(ctx) {
  const { $, esc, get, kpiRow, portalLinkMarkup, setContent, table } = ctx;
  const state = {
    changes: { days: 7, data: null, busy: false, error: "", filters: emptyFilters() },
    performance: { days: 7, data: null, busy: false, error: "", filters: emptyFilters() },
  };
  const names = { changes: "Change Analysis", performance: "Performance vs Spend" };
  const endpoints = { changes: "change-analysis", performance: "performance" };
  let generation = 0, controller = null, currentKind = null, currentToken = null;
  const active = (version, token) => version === generation && token === ctx.loadToken;
  const date = value => {
    const parsed = new Date(value);
    return value && Number.isFinite(parsed.getTime())
      ? parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "Not provided";
  };
  const text = value => value == null ? "Not provided"
    : typeof value === "object" ? JSON.stringify(value) : String(value);
  const number = value => Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "Unavailable";
  function costMarkup(cost) {
    if (!cost?.by_currency?.length) return '<span class="muted">No billing rows in window</span>';
    return cost.by_currency.map(item => `${esc(item.currency)} ${esc(number(item.cost))}`).join("<br>")
      + `<br><small class="muted">${esc(cost.from)} to ${esc(cost.to)}</small>`;
  }

  function filterControls(kind, item, rows, filtered) {
    const f = item.filters;
    const select = (field, label, options) => {
      const values = [...options];
      if (f[field] && !values.some(option => option.value === f[field])) {
        values.push({ value: f[field], label: `${f[field].replace(/^value:/, "")} (not in this load)` });
      }
      return `<label>${esc(label)}<select data-evidence-filter="${field}" aria-label="${esc(label)}">
        <option value="">All</option>${values.map(option => `<option value="${esc(option.value)}"${
          option.value === f[field] ? " selected" : ""}>${esc(option.label)}</option>`).join("")}</select></label>`;
    };
    const fixed = entries => entries.map(([value, label]) => ({ value, label }));
    return `<section class="card evidence-filters" aria-label="${names[kind]} filters">
      <div class="azure-feed-controls">
        <label>Search evidence<input type="search" data-evidence-search aria-label="Search evidence"
          placeholder="${kind === "changes" ? "Resource, actor, operation or changed property" : "Resource, group or metric"}"
          maxlength="200" value="${esc(f.query)}"></label>
        ${select("type", "Resource type", evidenceFacets(rows, "type"))}
        ${select("group", "Resource group", evidenceFacets(rows, "group"))}
        ${kind === "changes"
          ? select("change", "Change type", evidenceFacets(rows, "change"))
            + select("actor", "Changed by", evidenceFacets(rows, "actor"))
          : select("region", "Region", evidenceFacets(rows, "region"))
            + select("coverage", "Metric coverage", fixed([
              ["measured", "With measurements"], ["missing", "No measurements"],
              ["partial", "Partial coverage"], ["failed", "Denied / failed reads"],
            ]))}
        ${select("currency", "Billing currency", evidenceFacets(rows, "currency"))}
        ${select("cost", "Cost evidence", fixed([
          ["present", "With cost amounts"], ["missing", "Missing cost amounts"],
          ["positive", "Positive billed spend"], ["zero", "Recorded zero cost"],
        ]))}
        <button type="button" data-evidence-clear${Object.values(f).some(Boolean) ? "" : " disabled"}>Clear filters</button>
      </div>
      <div class="evidence-filter-foot">
        <p class="muted" role="status">Showing ${filtered.length} of ${rows.length} loaded ${
          kind === "changes" ? "changes" : "resources"}.</p>
        <span class="muted">Loaded sample only</span>
        <details class="view-help" data-state-key="evidence-${kind}-filter-help"><summary>Filter scope</summary>
          <p>Filters narrow the loaded evidence only; they do not search beyond source or sample limits.
            To query a different subscription sample, use the subscription picker and refresh.
            A currency filter selects matching records; it never converts or combines currencies.</p>
        </details>
      </div>
    </section>`;
  }

  function header(kind, item) {
    const data = item.data;
    const notices = evidenceNotices(data?.notices);
    const range = data ? `${esc(date(data.window.start))} &ndash; ${esc(date(data.window.end))} &middot; Updated ${esc(date(data.generated_at))}` : "";
    return `<section class="card evidence-header">
      <div class="evidence-toolbar"><h3>${names[kind]}</h3>
        <span class="pill">${item.busy ? "Loading" : item.error ? "Unavailable" : esc(data?.status || "Not loaded")}</span>
        <label>Window
          <select id="evidence-${kind}-days" aria-label="Evidence window">
            ${[1, 7, 14].map(days => `<option value="${days}"${item.days === days ? " selected" : ""}>${
              days === 1 ? "Last 24 hours" : `Last ${days} days`}</option>`).join("")}
          </select>
        </label>
        <button type="button" id="evidence-${kind}-refresh"${item.busy ? " disabled" : ""}>${item.busy ? "Loading..." : "Refresh evidence"}</button>
      </div>
      <p class="evidence-scope muted">Read-only &middot; Selected subscriptions &middot; Native currencies${range ? ` &middot; ${range}` : ""}</p>
      ${item.busy ? `<p role="status">${data ? "Refreshing; previously retrieved evidence is shown below." : "Loading Azure evidence..."}</p>` : ""}
      ${item.error ? `<p role="alert">${esc(item.error)}${data ? " Previously retrieved evidence below may be stale." : ""}</p>` : ""}
      ${data?.status === "unavailable" ? '<p role="alert">Source unavailable. Missing data is not zero.</p>' : ""}
      ${data?.truncated || data?.status === "partial" ? '<p class="evidence-scope muted">Partial coverage. Some records, properties or measurements are unavailable.</p>' : ""}
      ${notices.warnings.length ? `<ul class="evidence-alerts">${notices.warnings.map(note => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
      <details class="view-help" data-state-key="evidence-${kind}-source-help"><summary>Source &amp; calculation details</summary>
        ${data ? `<p>Source: ${esc(data.source)}.</p>` : ""}
        <p>This live view uses its own evidence window, not the global cost-period, dimension or display-currency filters.
          Billed costs retain their original currencies.</p>
        ${notices.methodology.map(note => `<p>${esc(note)}</p>`).join("")}
      </details>
    </section>`;
  }

  function changesMarkup(data, changes) {
    const summary = kpiRow([
      { label: "Observed changes", value: number(changes.length) },
      { label: "Affected resources", value: number(new Set(changes.map(row => row.resource_id.toLowerCase())).size) },
      { label: "With cost data", value: number(changes.filter(row => row.cost?.by_currency?.length).length),
        sub: "correlation, not proof of cause" },
    ]);
    if (!changes.length) return summary + `<section class="card"><h3>Resource changes</h3><p class="muted empty">${
      data.changes.length ? "No loaded changes match these filters. Clear or adjust the filters."
        : data.status === "ok" ? "No retained changes were returned for the selected subscriptions and evidence window."
          : "No complete change list is available. This does not establish that nothing changed."}</p></section>`;
    return summary + table("Recent resource changes", changes, [
      { label: "When", value: row => row.changed_at, cell: row => esc(date(row.changed_at)) },
      { label: "Resource", cell: row => portalLinkMarkup(row, [], row.resource_name || row.resource_id) },
      { label: "Change", cell: row => `${esc(row.change_type)}<br><small>${esc(row.operation || "Operation not supplied")}</small>` },
      { label: "Actor", cell: row => esc(row.actor || "Not supplied by Azure") },
      { label: "Changed properties", cell: row => row.properties?.length
        ? `<details data-state-key="change-${esc(row.id)}"><summary>${row.properties.length} properties</summary>
            ${row.properties.map(property => `<p><strong>${esc(property.name)}</strong><br>
              ${esc(text(property.before))} &rarr; ${esc(text(property.after))}</p>`).join("")}
            ${row.properties_truncated ? "<p>Additional properties omitted.</p>" : ""}</details>`
        : '<span class="muted">No property details supplied</span>' },
      { label: "Billed cost", cell: row => costMarkup(row.cost) },
    ], "Cost covers the evidence dates and may lag; non-allowlisted property values are withheld. Timing does not prove cause.",
    { key: "change-evidence", total: false, pageSize: 10, scroll: false, search: false });
  }

  function performanceMarkup(data, resources) {
    const measured = resources.filter(row => row.metrics?.some(metric => metric.status === "ok"));
    const metrics = row => `<details data-state-key="metrics-${esc(row.id)}"><summary>${
      row.metrics.slice(0, 2).map(metric => `${esc(metric.label || metric.name)}: ${esc(metricText(metric))}`).join(" &middot; ")}
      <br><small>${row.metrics.filter(metric => metric.status === "ok").length} of ${row.metrics.length} metrics available</small></summary>
      ${row.metrics.map(metric => `<p><strong>${esc(metric.label || metric.name)}: ${esc(metricText(metric))}</strong><br>
        <small>${esc(metric.aggregation || "No aggregation")} &middot; ${esc(number(metric.samples))} observations
        &middot; latest ${esc(date(metric.last_sample))}</small>
        ${metric.note ? `<br><small>${esc(metric.note)}</small>` : ""}</p>`).join("")}</details>`;
    const summary = kpiRow([
      { label: "Resources shown", value: number(resources.length),
        sub: `from ${data.resources.length} sampled resources` },
      { label: "With measured performance", value: number(measured.length) },
      { label: "Without observations", value: number(resources.length - measured.length),
        sub: "not classified as idle" },
    ]);
    if (!resources.length) return summary + `<section class="card"><h3>Performance evidence</h3><p class="muted empty">${
      data.resources.length ? "No sampled resources match these filters. Clear or adjust the filters."
        : data.status === "ok" ? "No resources supported by this release were returned in the selected subscriptions."
          : "Performance evidence is unavailable. Missing observations are not zero utilization."}</p></section>`;
    return summary + table("Performance and billed spend", resources, [
      { label: "Resource", cell: row => portalLinkMarkup(row, [], row.name) },
      { label: "Type / region", cell: row => `${esc(row.type)}<br><small>${esc(row.region || "Not provided")}</small>` },
      { label: "Billed cost", cell: row => costMarkup(row.cost) },
      { label: "Metrics", cell: metrics },
      { label: "Coverage", cell: row => `${esc(row.status)}${(row.notes || [])
        .filter(note => note !== "Observations support manual review, not automatic resizing or guaranteed savings.")
        .map(note => `<p class="muted">${esc(note)}</p>`).join("")}` },
    ], "Review before resizing. Missing billing or metrics are not zero; hourly averages are not percentiles.",
    { key: "performance-evidence", total: false, pageSize: 10, scroll: false, search: false });
  }

  function paint(kind, token) {
    if (token !== ctx.loadToken) return;
    const item = state[kind];
    const root = $("tabBody");
    const rows = item.data?.[kind === "changes" ? "changes" : "resources"] || [];
    const filtered = filterEvidence(rows, item.filters);
    setContent(root, header(kind, item) + (item.data ? filterControls(kind, item, rows, filtered)
      + (kind === "changes" ? changesMarkup(item.data, filtered) : performanceMarkup(item.data, filtered)) : ""));
    root.querySelector(`#evidence-${kind}-refresh`).onclick = () => load(kind, token);
    root.querySelector(`#evidence-${kind}-days`).onchange = event => {
      item.days = Number(event.target.value);
      item.data = null;
      load(kind, token);
    };
    for (const control of root.querySelectorAll("[data-evidence-filter]")) {
      control.value = item.filters[control.dataset.evidenceFilter];
      control.onchange = event => {
        item.filters[event.target.dataset.evidenceFilter] = event.target.value;
        paint(kind, token);
      };
    }
    const search = root.querySelector("[data-evidence-search]");
    if (search) {
      if (search.value !== item.filters.query) search.value = item.filters.query;
      search.oninput = event => {
        const focused = document.activeElement === event.target;
        const start = event.target.selectionStart, end = event.target.selectionEnd;
        item.filters.query = event.target.value;
        paint(kind, token);
        if (focused) {
          const replacement = root.querySelector("[data-evidence-search]");
          replacement.focus({ preventScroll: true });
          replacement.setSelectionRange(start, end);
        }
      };
    }
    const clear = root.querySelector("[data-evidence-clear]");
    if (clear) clear.onclick = () => { item.filters = emptyFilters(); paint(kind, token); };
    ctx.wireTables?.(root);
  }

  async function load(kind, token) {
    currentKind = kind;
    currentToken = token;
    const version = ++generation, item = state[kind];
    controller?.abort();
    controller = new AbortController();
    const scope = ctx.portalScopeIds();
    item.error = "";
    if (!scope.length) {
      item.busy = false;
      item.data = null;
      item.error = "Select at least one accessible subscription before loading evidence.";
      paint(kind, token);
      return;
    }
    item.busy = true;
    paint(kind, token);
    try {
      const data = await get(`/api/dashboard/${endpoints[kind]}?scope=${encodeURIComponent(scope.join(","))}&window_days=${item.days}`,
        { timeout: 120000, signal: controller.signal });
      if (!active(version, token)) return;
      if (!validEvidence(data, kind)) throw new Error("The source returned an incomplete evidence response.");
      item.data = data;
    } catch (error) {
      if (!active(version, token) || error.name === "AbortError") return;
      item.error = error.message || "Azure evidence could not be read.";
    } finally {
      if (active(version, token)) {
        item.busy = false;
        paint(kind, token);
      }
    }
  }

  return {
    renderChanges: token => load("changes", token),
    renderPerformance: token => load("performance", token),
    stop() { generation++; controller?.abort(); if (currentKind) state[currentKind].busy = false; },
    resume(token, tab) {
      if (tab !== "changes" && tab !== "performance") return;
      currentToken = token;
      currentKind = tab;
      if (!state[tab].data) load(tab, currentToken);
      else paint(tab, currentToken);
    },
  };
}
