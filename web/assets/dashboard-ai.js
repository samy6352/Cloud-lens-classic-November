/** AI billing keeps financial attribution separate from measured model usage. */
const numeric = value => typeof value === "number" && Number.isFinite(value);
const number = value => numeric(value)
  ? value.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "Unavailable";

export function validAiBilling(data) {
  return data && ["ok", "partial"].includes(data.status) && Array.isArray(data.services)
    && Array.isArray(data.models) && Array.isArray(data.totals) && Array.isArray(data.notices)
    && data.window && typeof data.generated_at === "string";
}

export function aiMetricText(metric, percent = false) {
  if (metric?.status === "not_applicable") return "Not applicable";
  if (!["ok", "partial"].includes(metric?.status) || !numeric(metric.value)) return "Unavailable";
  return percent ? `${metric.value.toFixed(1)}%` : number(metric.value);
}

export function filterAiRows(rows, query = "") {
  const find = query.trim().toLowerCase();
  if (!find) return rows;
  return rows.filter(row => [row.service, row.resource_name, row.resource_id, row.subscription,
    row.model, row.deployment, row.version, ...(row.models || [])]
    .some(value => typeof value === "string" && value.toLowerCase().includes(find)));
}

export function aiTokenSummary(rows) {
  const observed = rows.filter(row => ["ok", "partial"].includes(row.tokens?.status) && numeric(row.tokens.value));
  return { value: observed.length ? observed.reduce((sum, row) => sum + row.tokens.value, 0) : null,
    measured: observed.length, total: rows.length,
    partial: observed.length !== rows.length || observed.some(row => row.tokens.status === "partial") };
}

export function aiAnomalyDirection(row) {
  const directions = new Set((row.anomalies || []).map(item => item.direction)
    .filter(direction => direction === "up" || direction === "down"));
  return directions.size > 1 ? "mixed" : [...directions][0] || "";
}

export function aiAnomalySummary(rows) {
  return {
    flagged: rows.filter(row => aiAnomalyDirection(row)).length,
    assessed: rows.filter(row => ["checked", "partial"].includes(row.anomaly_status)).length,
    incomplete: rows.filter(row => row.anomaly_status !== "checked").length,
    total: rows.length,
  };
}

export function prioritizeAiAnomalies(rows, only = false) {
  return rows.filter(row => !only || aiAnomalyDirection(row)).map((row, index) => ({ row, index }))
    .sort((a, b) => Number(!!aiAnomalyDirection(b.row)) - Number(!!aiAnomalyDirection(a.row)) || a.index - b.index)
    .map(item => item.row);
}

export function aiAnomalyRuleText(config) {
  if (!config) return "";
  const { cost, tokens, utilization } = config.materiality || {};
  if (![config.window_days, config.min_history, config.min_relative_change,
    cost?.delta, tokens?.delta, utilization?.delta].every(numeric)) return config.method || "";
  return `${config.window_days}-day baseline; at least ${config.min_history} complete prior days. `
    + `Changes must be at least ${(config.min_relative_change * 100).toFixed(0)}% and meet the statistical threshold, `
    + `plus a daily minimum of ${cost.delta} billing-currency units, ${number(tokens.delta)} tokens, `
    + `or ${utilization.delta} percentage points. `
    + (numeric(config.min_same_kind_history)
      ? `${config.min_same_kind_history} same-kind weekday/weekend observations may be used once the baseline is sufficient. ` : "")
    + "A zero baseline has no percentage change; the absolute and statistical thresholds still apply.";
}

export function createTabs(ctx) {
  const { $, esc, get, cached, key, qs, kpiRow, money, table, wireTables, setContent } = ctx;
  let data = null, mode = "services", query = "", onlyAnomalies = false;
  let refreshIntent = null;
  let refreshRevision = 0;
  function refreshOnce() {
    refreshRevision++;
    const intent = refreshIntent = {};
    return () => { if (refreshIntent === intent) refreshIntent = null; };
  }
  const moneyList = amounts => (amounts || []).length
    ? amounts.map(item => numeric(item.cost)
      ? esc(item.currency && item.currency !== "unknown"
        ? `${item.currency} ${money(item.cost, item.currency, 2)}`
        : `${item.cost.toFixed(2)} (currency unknown)`) : "Unavailable").join("<br>")
    : "Unavailable";
  const label = (value, fallback = "Not reported") => esc(value || fallback);
  const metric = (item, percent = false) => {
    const note = item?.note || (percent ? "No comparable capacity-utilization measurement." : "No measured token count.");
    const detail = percent && numeric(item?.value) ? (item.aggregation || "Measured") : "";
    return `<span${item?.status === "partial" ? ' class="warn"' : ""}>${esc(aiMetricText(item, percent))}</span>
      ${detail ? `<small class="ai-cell-note">${esc(detail)}</small>` : ""}
      <details class="ai-metric-note"><summary aria-label="${percent ? "Utilization" : "Token consumption"} source">Source</summary>
        <span>${esc(note)}</span>${item?.source ? `<span>${esc(item.source)}</span>` : ""}</details>`;
  };
  const resource = row => ctx.portalLinkMarkup(
    { id: row.resource_id || row.id, subscription_id: row.subscription_id },
    ctx.scopeIds(), row.resource_name || row.service || row.model || "AI resource");
  const costCell = row => moneyList(row.cost_by_currency);
  const anomalyNumber = (value, flag) => {
    if (!numeric(value)) return "Unavailable";
    if (flag.metric === "cost" && flag.currency) return `${flag.currency} ${money(value, flag.currency, 2)}`;
    if (flag.metric === "utilization") return `${value.toFixed(1)}%`;
    return number(value);
  };
  function anomalyMarkup(row) {
    const direction = aiAnomalyDirection(row);
    if (!direction) {
      const message = { checked: "No significant change", partial: "Partial assessment",
        insufficient_history: "Insufficient history", unavailable: "Not assessed",
        not_applicable: "Not applicable" }[row.anomaly_status] || "Not assessed";
      return `<small class="ai-cell-note">${esc(message)}</small>`;
    }
    const labels = { up: "Spike", down: "Drop", mixed: "Spike and drop" };
    const names = { cost: "Daily cost", tokens: "Daily tokens", utilization: "Daily utilization" };
    return `<span class="ai-anomaly-label" data-ai-anomaly="${direction}">${labels[direction]}</span>
      <details class="ai-anomaly-details"><summary>Why flagged</summary>
      ${(row.anomalies || []).filter(flag => ["up", "down"].includes(flag.direction)).map(flag => {
        const change = numeric(flag.percent) ? `${flag.percent > 0 ? "+" : ""}${flag.percent.toFixed(1)}%`
          : "from a zero baseline";
        const amount = flag.metric === "utilization" && numeric(flag.delta)
          ? `${flag.delta > 0 ? "+" : ""}${flag.delta.toFixed(1)} percentage points`
          : change;
        return `<div class="ai-anomaly-event">
          <strong>${esc(names[flag.metric] || flag.metric)} ${flag.direction === "up" ? "spike" : "drop"} · ${esc(flag.day)}</strong>
          <span>${esc(anomalyNumber(flag.actual, flag))} vs typical ${esc(anomalyNumber(flag.baseline, flag))} (${esc(amount)})</span>
          <span>${esc(flag.baseline_samples)} baseline days · ${esc(flag.baseline_from)} to ${esc(flag.baseline_to)}</span>
          ${flag.note ? `<span>${esc(flag.note)}</span>` : ""}
        </div>`;
      }).join("")}
      ${row.anomaly_note ? `<p>${esc(row.anomaly_note)}</p>` : ""}</details>`;
  }

  function paintRows(resetPage = false) {
    const source = data[mode];
    const rows = prioritizeAiAnomalies(filterAiRows(source, query), onlyAnomalies);
    const models = mode === "models";
    const anomalies = aiAnomalySummary(source);
    $("aiResultCount").textContent = `${rows.length} of ${source.length} ${models ? "model deployments" : "AI resources"}`;
    $("aiAnomalySummary").textContent = `${anomalies.flagged} ${models ? "model deployments" : "AI resources"} flagged`
      + (anomalies.incomplete ? ` · ${anomalies.incomplete} with incomplete or unavailable assessment`
        : " · All loaded rows assessed");
    $("aiAnomalySummary").classList.toggle("has-findings", anomalies.flagged > 0);
    for (const button of $("tabBody").querySelectorAll("[data-ai-mode]")) {
      const on = button.dataset.aiMode === mode;
      button.setAttribute("aria-pressed", String(on));
      button.classList.toggle("on", on);
    }
    $("aiAttribution").textContent = models
      ? "Model usage comes from Azure Monitor. Resource-level cost is not divided between deployments."
      : "Billed cost by AI resource. Token and utilization metrics keep their own source and coverage.";
    const columns = [
      models
        ? { label: "Model / deployment", value: row => row.model || row.deployment,
          cell: row => `<strong>${label(row.model, "Model not reported")}</strong>
            <small class="ai-cell-note">${label(row.deployment)}${row.version ? ` · ${esc(row.version)}` : ""}</small>
            <small class="ai-cell-note">${resource(row)}</small>${anomalyMarkup(row)}` }
        : { label: "Service / resource", value: row => row.service,
          cell: row => `<strong>${label(row.service, "AI service")}</strong>
            <small class="ai-cell-note">${resource(row)}</small>
            ${row.models?.length ? `<small class="ai-cell-note">${esc(row.models.join(", "))}</small>` : ""}
            <small class="ai-cell-note">${label(row.subscription)}</small>${anomalyMarkup(row)}` },
      { label: "Token consumption", num: true, value: row => row.tokens?.value,
        cell: row => metric(row.tokens) + (
          numeric(row.tokens?.input) || numeric(row.tokens?.output)
            ? `<small class="ai-cell-note">In ${number(row.tokens.input)} · Out ${number(row.tokens.output)}</small>` : "") },
      { label: "Billed cost", num: true, sort: false,
        cell: row => models && !(row.cost_by_currency || []).length
          ? `<span class="muted">Not allocated</span>
              <button type="button" class="linkish ai-cell-note" data-ai-resource="${esc(row.resource_id || row.resource_name || "")}">View resource cost</button>
              <details class="ai-metric-note"><summary>Why?</summary>
              <span>${esc(row.cost_note || "Billing does not identify this deployment. Review its resource cost in Services.")}</span></details>`
          : costCell(row) },
      { label: "Utilization", num: true, value: row => row.utilization?.value,
        cell: row => metric(row.utilization, true) },
      { label: "Recommendation", sort: false,
        cell: row => `<span class="ai-recommendation">${label(row.recommendation, "Review available billing and usage evidence.")}</span>` },
    ];
    setContent($("aiResults"), rows.length
      ? table(models ? "Model usage" : "AI service billing", rows, columns,
        models ? "No inferred model costs. Token counts are not inferred from spend."
          : "Expand a row for billing products, meters and source evidence. Costs overlap the main Overview, not additional spend.",
        { key: `ai-${mode}`, pageSize: 10, search: false, highlightOutliers: false, resetPage })
      : `<section class="card"><h3>${models ? "Model usage" : "AI service billing"}</h3>
          <p class="muted empty">${onlyAnomalies ? "No flagged rows match this selection. Unassessed data is not proof of normal activity."
            : query ? "No results match this search."
            : models ? "No model-level usage is available for this selection. Service billing remains available."
            : "No AI billing rows were found in the selected cost data."}</p></section>`);
    wireTables($("aiResults"));
    $("aiResults").onclick = event => {
      const button = event.target.closest("[data-ai-resource]");
      if (!button) return;
      query = button.dataset.aiResource;
      mode = "services";
      onlyAnomalies = false;
      $("aiOnlyAnomalies").checked = false;
      $("aiBillingSearch").value = query;
      paintRows(true);
    };
  }

  async function renderAiBilling(token) {
    if (token !== ctx.loadToken) return;
    const response = await cached(`${key("ai-billing")}:${refreshRevision}`, () => {
      const query = qs();
      const refresh = refreshIntent && token === ctx.loadToken;
      if (refresh) refreshIntent = null;
      const force = refresh ? `${query ? "&" : "?"}force=true` : "";
      return get(`/api/dashboard/ai-billing${query}${force}`, { timeout: 75000 });
    });
    if (token !== ctx.loadToken) return;
    if (!validAiBilling(response)) throw new Error("AI billing returned incomplete data. Refresh this view to retry.");
    data = response;
    const tokens = aiTokenSummary(data.services);
    const coverage = data.metric_coverage;
    const period = `${data.window.from || "No start date"} to ${data.window.to || "No end date"}`;
    setContent($("tabBody"), `<div class="ai-billing-view">
      <section class="portal-workbench">
        <div class="ai-billing-head"><div><h3>AI Billing</h3>
          <p class="muted">${esc(period)} · Selected subscriptions and cost filters</p>
          <p class="muted">Daily evidence · Read at ${esc(data.generated_at)} · UTC day-end billing window</p></div>
          <span class="ai-evidence-label">Billing + usage evidence</span></div>
        ${kpiRow([
          { label: "AI billed cost", value: moneyList(data.totals), sub: "Separate totals for each returned currency" },
          { label: "AI resources", value: String(data.services.length), sub: "In the loaded billing selection" },
          { label: tokens.partial ? "Observed tokens · partial" : "Observed tokens",
            value: tokens.value == null ? "Unavailable" : number(tokens.value),
            sub: `${tokens.measured} of ${tokens.total} resources measured · not billing quantities` },
          { label: "Model deployments", value: String(data.models.length), sub: "With returned model-level evidence" },
        ])}
        <div class="ai-billing-controls">
          <div class="seg" role="group" aria-label="AI billing detail">
            <button type="button" class="seg-btn" data-ai-mode="services">Services</button>
            <button type="button" class="seg-btn" data-ai-mode="models">Models</button>
          </div>
          <input id="aiBillingSearch" type="search" aria-label="Search AI services and models"
            placeholder="Search service, model or resource" value="${esc(query)}">
          <label class="ai-anomaly-filter"><input id="aiOnlyAnomalies" type="checkbox"${onlyAnomalies ? " checked" : ""}>Only anomalies</label>
          <span class="muted" id="aiResultCount" role="status"></span>
        </div>
        <p class="ai-anomaly-summary" id="aiAnomalySummary" role="status"></p>
        <p class="muted note" id="aiAttribution"></p>
      </section>
      ${coverage && coverage.accounts_with_tokens < coverage.eligible_accounts
        ? `<p class="banner warn">${coverage.accounts_with_tokens} of ${coverage.eligible_accounts} AI accounts returned token observations.
          Review source details, or narrow the subscription scope and use Refresh views to retry missing metrics.</p>` : ""}
      ${data.notices.length ? `<div class="ai-billing-notices" role="status">
        ${data.notices.map(note => `<p class="muted">${esc(note)}</p>`).join("")}</div>` : ""}
      <div id="aiResults"></div>
      <details class="view-help" data-state-key="ai-billing-methodology">
        <summary>Sources &amp; definitions</summary>
        <p>Billed costs use the selected warehouse period. Model names and measured tokens come from
          Azure's deployment metadata and Azure Monitor, not a guess from resource names or prices.
          Input plus output tokens are counted once; a total-token metric is not added again.</p>
        <p>Utilization is the provisioned-capacity metric Azure exposes, with its aggregation shown.
          Pay-as-you-go capacity utilization may not apply. Missing telemetry is never zero or proof of idle capacity.
          Generic billing units and token-price meter names do not establish token consumption.</p>
        <p>One-line recommendations are evidence-based review prompts, not guaranteed savings or automatic changes.
          Service and model tables describe overlapping usage; do not add their token totals.</p>
        <p>Spike/drop highlights compare daily observations with a trailing baseline. Missing days and incomplete
          telemetry are never filled with zero, and recent unsettled billing days are not judged.
          Account and model flags can describe the same underlying activity; they are not additive.</p>
        ${data.anomaly_detection?.method ? `<p>${esc(data.anomaly_detection.method)}. ${esc(aiAnomalyRuleText(data.anomaly_detection))}</p>` : ""}
        <p>Read at ${esc(data.generated_at)}. Completed daily snapshots are reused until a newer billing data day
          is available for this subscription/filter selection, subject to cache retention.
          Same-day imports and corrections do not automatically repeat Azure reads.
          Use Refresh views to retry missing evidence or request a new read now.
          These daily amounts can differ from the latest Billing view; the dates above describe the evidence shown.</p>
      </details></div>`);
    $("aiBillingSearch").oninput = event => {
      query = event.target.value;
      paintRows(true);
    };
    $("aiOnlyAnomalies").onchange = event => {
      onlyAnomalies = event.target.checked;
      paintRows(true);
    };
    for (const button of $("tabBody").querySelectorAll("[data-ai-mode]")) {
      button.onclick = () => { mode = button.dataset.aiMode; paintRows(); };
    }
    paintRows();
  }
  return { renderAiBilling, refreshOnce };
}
