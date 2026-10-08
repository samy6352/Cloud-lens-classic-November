/** Lazy spend views. Dependencies are supplied by the shell; no reverse imports. */
import { periodName } from "./reporting-period.js";
import { illustrativeSaving, scheduleAssessment } from "./billing-usage.js";
import { railIcon } from "./icons.js";
import { revealOverviewTarget, subscriptionDrillIds } from "./dashboard-drilldowns.js";
import { budgetReadIssue, matchingTagBudgets } from "./budget-context.js";
import { currencyAmountList, nativeAmount, nativeRowTotal } from "./dashboard-currencies.js";
import { loadBudgetTrend as loadBudgetTrendChart } from "./budget-chart.js";

/** Select existing cost signals without treating missing values as zero or changing source order. */
export function overviewHighlights(data) {
  const largest = (rows, field, accept = () => true) => (rows || []).reduce((best, row) =>
    Number.isFinite(row[field]) && accept(row[field]) && (!best || row[field] > best[field]) ? row : best, null);
  return {
    increase: largest(data.movers, "delta", value => value > 0),
    service: largest(data.services, "cost"),
    concentration: Number.isFinite(data.kpis?.top5_share_pct) ? data.kpis.top5_share_pct : null,
  };
}

/**
 * The overview's one-line answer to "is anything unusual". Counts only, never a diagnosis.
 *
 * Increases and decreases are kept apart because they are not the same news, and the headline
 * figure is the net movement the anomalies account for rather than a sum of absolute deltas —
 * a fortnight where one service doubled and another halved has not moved the bill, and saying
 * it did would send someone looking for money that is still there.
 */
export function overviewAnomalySummary(data) {
  const rows = data?.anomalies || [];
  const currencies = new Set(rows.map(row => row.currency || data?.currency || null));
  const up = rows.filter(a => a.direction === "up");
  const down = rows.filter(a => a.direction !== "up");
  const largest = rows.reduce(
    (best, a) => (!best || Math.abs(a.delta) > Math.abs(best.delta) ? a : best), null);
  return {
    count: rows.length,
    high: rows.filter(a => a.severity === "high").length,
    increases: up.length,
    decreases: down.length,
    net: currencies.size > 1 ? null : rows.reduce((n, a) => n + a.delta, 0),
    largest,
    currency: currencies.size > 1 ? null : data?.currency || [...currencies][0] || null,
    // The judged window, which the settle delay makes shorter than the requested one.
    to: data?.window_to || data?.settled_to || data?.to || null,
  };
}

export function resourceTagStatus(resource, data) {
  if ((resource.keys || []).length) return "tagged";
  if (["untagged", "unknown"].includes(resource.tag_status)) return resource.tag_status;
  return data.tags_not_loaded || !Array.isArray(resource.keys) ? "unknown" : "untagged";
}

export function tagResourceTotals(data) {
  const totals = Object.fromEntries(["all", "tagged", "untagged", "unknown"]
    .map(status => [status, { resources: 0, cost: 0 }]));
  for (const resource of data.resources || []) {
    for (const status of ["all", resourceTagStatus(resource, data)]) {
      totals[status].resources++;
      totals[status].cost += resource.cost;
    }
  }
  return totals;
}

function tagFiltersPaused(view) {
  return view === "untagged" || view === "unknown";
}

export function filterTagResources(data, { selected = new Set(), values = new Map(), mode = "all", resourceView = "all" } = {}) {
  const picked = [...selected];
  return (data.resources || []).filter(resource => {
    const status = resourceTagStatus(resource, data);
    if (resourceView !== "all" && status !== resourceView) return false;
    // Preserve the selection when inspecting resources without observed tags.
    if (tagFiltersPaused(resourceView) || !picked.length) return true;
    const matches = key => {
      const index = (resource.keys || []).indexOf(key);
      if (index < 0) return false;
      const wanted = values.get(key);
      return !wanted?.size || wanted.has((data.values || [])[(resource.v || [])[index]] ?? "");
    };
    return mode === "all" ? picked.every(matches) : picked.some(matches);
  });
}

/** Internal identity is separate from both real tag keys and HTML option values. */
export const UPTIME_UNTAGGED = Symbol("untagged");

/** Whether an hourly meter survives the current filter. Every panel on the tab reads through
 *  this, so the table, the charts and the KPI row cannot describe different selections. */
export function uptimeKeep(r, f, tagValues = []) {
  if (f.service && r.service !== f.service) return false;
  if (f.group && r.resource_group !== f.group) return false;
  if (f.region && r.region !== f.region) return false;
  if (f.state === "always" && !r.always_on) return false;
  const review = scheduleAssessment(r) === "needs_review";
  if (f.state === "schedulable" && !review) return false;
  if (f.state === "idle" && !(review && r.always_on)) return false;
  if (f.state === "part" && r.always_on) return false;
  if (f.state === "unassessed" && review) return false;
  if (f.tagKey === UPTIME_UNTAGGED) {
    if ((r.keys || []).length) return false;
  } else if (f.tagKey) {
    const at = (r.keys || []).indexOf(f.tagKey);
    if (at < 0) return false;
    // An empty value is a value: `env=""` is tagged, and must not match every other meter.
    if ((f.tagValue || f.tagValueSet) && (tagValues[(r.v || [])[at]] ?? "") !== f.tagValue) return false;
  }
  if (f.find) {
    const hay = `${r.name} ${r.service} ${r.meter} ${r.resource_group} ${r.region}`.toLowerCase();
    if (!hay.includes(f.find.toLowerCase())) return false;
  }
  return true;
}

/** Hour-billed cost and hours per value of one tag key, largest first. */
export function uptimeTagValues(rows, key, tagValues = []) {
  const totals = new Map();
  for (const r of rows) {
    const at = (r.keys || []).indexOf(key);
    if (at < 0) continue;
    const value = tagValues[(r.v || [])[at]] ?? "";
    const slot = totals.get(value) || { value, cost: 0, hours: 0, meters: 0 };
    slot.cost += r.cost;
    slot.hours += r.hours;
    slot.meters += 1;
    totals.set(value, slot);
  }
  return [...totals.values()].sort((a, b) => b.cost - a.cost);
}

/**
 * Budgets that watch the tag key and value currently selected on Hourly Billing.
 *
 * Matching is on the *tag filter Azure actually holds*, read back as `tag_filter` pairs, not on
 * the budget's name — a budget called "prod-vms" that filters on nothing is not a budget for
 * this selection, and saying it is would be worse than showing none.
 *
 * Tag keys are compared case-insensitively because Azure preserves the case a tag was created
 * with but matches without it, so `Application` and `application` are one key to a budget and
 * would otherwise look like two. Values are compared exactly: `prod` and `Prod` are genuinely
 * different tag values in a cost report, and treating them as one would draw a budget over
 * spend it does not track.
 *
 * A budget filtered on more than the chosen key still matches — it does cover this selection —
 * but carries `extra`, the other clauses, so the panel can say the budget is narrower than what
 * is on screen instead of implying the two measure the same money.
 */
export function uptimeBudgetMatches(budgets, tagKey, tagValue, hasValue = Boolean(tagValue)) {
  if (!tagKey || !hasValue || tagKey === UPTIME_UNTAGGED) return [];
  return matchingTagBudgets(budgets, [{ key: tagKey, values: [tagValue] }]);
}

export function createTabs(ctx) {
  const { $, activeFilterCount, allSubscriptions, applyFilter, bar, budgetCard, budgetPortalUrl, cached, clearAllFilters, days, drawBudgetChart, drawChart, esc, executiveData, get, key, kpiRow, money, moneyHtml, pct, portalLinkMarkup, qs, selectTab, setContent, setGlanceSpark, subName, table, title, toast, trendClass, whenNear, whoAmI, wireBudgetLinks, wireTables } = ctx;
  const periodText = count => periodName(count, ctx.periodMonth);

  // The Daily spend chart's own day index and detail opener, so the calendar heatmap below can
  // open a day on it instead of drawing a second panel about the same date.
  let trendDays = [];
  let trendSelect = null;

function renderNativeCurrencies(data) {
  if (!data.mixed_currency && (data.empty || /^[A-Z]{3}$/.test(data.currency || ""))) return false;
  const buckets = data.by_currency?.length ? data.by_currency : data.mixed_currency ? [] : [data];
  setContent($("tabBody"), `<section class="card">
    <h3>Spend by billing currency</h3>
    <p class="muted">Native billing currencies are shown separately, never added together.
      Choose a display currency in the toolbar for a combined view with unified charts and drilldowns.</p>
    ${buckets.length ? "" : '<p role="status">Per-currency data is unavailable. Select a display currency to continue.</p>'}
    </section><div class="grid-2">${buckets.map((bucket, index) => {
      const currency = bucket.currency;
      const label = /^[A-Z]{3}$/.test(currency || "") ? currency : "Unknown currency";
      const amount = value => esc(nativeAmount(value, currency, (n, cur) => money(n, cur, 2)));
      return `<section class="card" aria-label="${esc(label)} billing">
        <h3>${esc(label)}</h3>
        ${kpiRow([
          { label: `Spend, ${periodText(bucket.days || data.days)}`, value: amount(bucket.kpis?.total) },
          { label: "Previous period", value: amount(bucket.kpis?.previous) },
          { label: "Billed resources", value: Number.isFinite(bucket.kpis?.resources)
            ? String(bucket.kpis.resources) : "Unknown" },
        ])}
        <div id="nativeTrend${index}" class="chart-card"></div>
        ${table(`Services · ${label}`, bucket.services || [], [
          { label: "Service", cell: row => esc(row.name || "—") },
          { label: `Cost · ${label}`, num: true, value: row => row.cost, cell: row => amount(row.cost) },
        ], undefined, { currency })}
        ${bucket.resources_top?.length ? table(`Resources · ${label}`, bucket.resources_top, [
          { label: "Resource", cell: row => esc(row.name || "—") },
          { label: "Resource group", cell: row => esc(row.grp || "—") },
          { label: `Cost · ${label}`, num: true, value: row => row.cost, cell: row => amount(row.cost) },
        ], undefined, { currency }) : ""}
      </section>`;
    }).join("")}</div>`);
  buckets.forEach((bucket, index) => {
    const host = $(`nativeTrend${index}`);
    if (bucket.trend?.labels?.length > 1 && /^[A-Z]{3}$/.test(bucket.currency || "")) {
      drawChart(host, { type: "area", title: `Daily spend · ${bucket.currency}`,
        labels: bucket.trend.labels, datasets: [{ label: bucket.currency, data: bucket.trend.values }],
        currency: bucket.currency });
    } else if (host) host.hidden = true;
  });
  return true;
}

async function renderSection(token, id) {
  const d = await cached(key(id), () => get(`/api/dashboard/${id}${qs()}`, { timeout: 30000 }));
  if (token !== ctx.loadToken) return;
  if (renderNativeCurrencies(d)) return;
  if (d.empty) {
    setContent($("tabBody"), emptyState());
    wireEmptyState();
    return;
  }

  const cur = d.currency;
  const k = d.kpis;
  const topService = d.services[0];
  const maxService = topService ? topService.cost : 0;
  const maxResource = d.resources_top[0]?.cost || 0;

  setContent($("tabBody"), `
    ${kpiRow([
      { label: `Spend, ${periodText(d.days)}`, value: moneyHtml(k.total, cur, 2) },
      {
        label: "vs previous period",
        value: pct(k.change_pct),
        cls: trendClass(k.change_pct),
        sub: k.previous ? `was ${esc(money(k.previous, cur))}` : "no prior period",
      },
      { label: "Share of all spend", value: k.share_pct == null ? "—" : `${k.share_pct}%` },
      { label: "Billed resources", value: String(k.resources) },
      { label: "Services", value: String(k.services) },
    ])}
    <div id="secChart" class="card chart-card"></div>
    <div class="grid-2">
      ${table(
        "By service",
        d.services,
        [
          { label: "Service", cell: (r) => `${esc(r.name || "—")} ${bar(r.cost, maxService)}` },
          { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        ],
        null
      )}
      ${table("By region", d.regions, [
        { label: "Region", cell: (r) => esc(r.name) },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ])}
    </div>
    ${table(
      "Top resources",
      d.resources_top,
      [
        { label: "Resource", cell: (r) => `${esc(r.name)} ${bar(r.cost, maxResource)}` },
        { label: "Resource group", cell: (r) => esc(r.grp) },
        { label: "Service", cell: (r) => esc(r.service) },
        { label: "Region", cell: (r) => esc(r.region) },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      "Ranked by period spend."
    )}
    ${d.mixed_currency ? '<p class="muted note">This estate bills in more than one currency; figures show the largest.</p>' : ""}
    ${d.section === "marketplace" ? '<p class="muted note">Billing-publisher attribution may miss '
      + 'products resold under an Azure service name. Billed usage only, not a catalogue.</p>' : ""}
  `);

  if (d.trend.labels.length > 1) {
    drawChart($("secChart"), {
      type: "area",
      title: `${d.label} — daily spend`,
      labels: d.trend.labels,
      datasets: [{ label: d.label, data: d.trend.values }],
      currency: cur,
    });
  } else {
    $("secChart").remove();
  }
}

/**
 * The opening view.
 *
 * This is the first thing anyone sees, so it answers the four questions people actually arrive
 * with — what am I spending, what is it going on, who is spending it, and what changed — rather
 * than showing one chart and a lot of white space.
 *
 * It is one request. Everything below comes from a single warehouse pass, so a page this dense
 * still lands in milliseconds and touches Azure not at all.
 */
/**
 * Nothing to show, and why.
 *
 * "No data loaded for this period yet" and "your filters exclude everything" are two different
 * facts with two different fixes, and printing the first when the second is true sends someone
 * to the Refresh button to solve a problem that a click on Clear all would fix.
 *
 * So the filter case is detected and named, and it carries the way out with it — an empty state
 * that describes a dead end without offering an exit is only half an answer.
 */
function emptyState({ title, detail, action } = {}) {
  const filtered = activeFilterCount() > 0;
  const heading = title || (filtered
    ? "No matching data"
    : "No data for this period");
  const body = detail || (filtered
    ? "Widen or clear the filters to see results."
    : "Load cost data with Refresh, or pick a longer period.");
  const button = action || (filtered
    ? `<button type="button" class="btn" id="emptyClear">Clear all filters</button>`
    : "");
  return `<div class="emptystate">
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M3 6h18M6 12h12M10 18h4" fill="none" stroke="currentColor"
              stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <h3>${esc(heading)}</h3>
      <p>${esc(body)}</p>
      ${button}
    </div>`;
}

/** Wire whatever the empty state offered. Called after it is written into the DOM. */
function wireEmptyState() {
  const btn = $("emptyClear");
  if (btn) btn.onclick = () => clearAllFilters();
}

async function renderOverview(token) {
  const d = await executiveData(ctx.tabsController?.signal);
  if (token !== ctx.loadToken) return;
  if (renderNativeCurrencies(d)) return;
  if (d.empty) {
    setContent($("tabBody"), emptyState());
    wireEmptyState();
    return;
  }

  const cur = d.currency;
  const k = d.kpis;
  const maxArea = d.areas.reduce((m, a) => Math.max(m, a.cost), 0);
  const maxService = d.services.reduce((m, s) => Math.max(m, s.cost), 0);
  const maxSub = d.subscriptions.reduce((m, s) => Math.max(m, s.cost), 0);
  const maxRegion = (d.regions || []).reduce((m, s) => Math.max(m, s.cost), 0);
  const highlights = overviewHighlights(d);
  const amount = (value, digits = 2) => Number.isFinite(value) ? moneyHtml(value, cur, digits) : "Not available";
  const count = value => Number.isFinite(value) ? value.toLocaleString() : "Unknown";
  const previous = Number.isFinite(k.previous);
  const delta = previous && Number.isFinite(k.total) ? k.total - k.previous : null;
  const footprint = [
    `${count(k.subscriptions)} subscriptions`,
    `${count(k.services)} services`,
    `${count(k.regions)} regions`,
  ].join(" · ");

  /** A change, written the way someone reads it: direction, amount, and what it means. */
  const move = (r) => {
    if (r.previous === 0) return `<span class="delta new">new</span>`;
    if (Math.abs(r.delta) < 0.01) return `<span class="delta flat">—</span>`;
    const up = r.delta > 0;
    return `<span class="delta ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${esc(
      money(Math.abs(r.delta), cur)
    )}</span>`;
  };

  const changeCol = {
    label: `vs prev ${d.days}d`,
    num: true,
    value: (r) => r.delta,
    cell: move,
    // Summing deltas is the one total that means something here: what the rows *net* to.
    // Summing their costs would just restate the cost column.
    total: (rows) => {
      const net = rows.reduce((n, r) => n + r.delta, 0);
      if (Math.abs(net) < 0.01) return `<span class="delta flat">—</span>`;
      const up = net > 0;
      return `<span class="delta ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${esc(
        money(Math.abs(net), cur)
      )}</span>`;
    },
  };

  setContent($("tabBody"), `
    <div class="overview-home" id="overviewHome">
    <section class="overview-summary" aria-label="Selected-period cost summary">
      <div class="overview-toolbar">
        <div class="overview-context">
          <strong>${esc(periodText(d.days))}</strong>
          ${d.as_of ? `<span>Through ${esc(d.as_of)}</span>` : ""}
          <span>${esc(cur || "Currency not specified")}</span>
          <span class="overview-footprint" title="Subscriptions, services and regions represented in these cost records">${esc(footprint)}</span>
        </div>
        <nav class="overview-actions" aria-label="Overview actions">
          <button type="button" class="primary" id="overviewAnalysis">${railIcon("anomalies")}AI Analysis</button>
          <button type="button" class="ghost" data-overview-tab="savings">${railIcon("savings")}Savings</button>
          <button type="button" class="ghost" data-overview-tab="budgets">${railIcon("budgets")}Budgets</button>
          <button type="button" class="ghost" data-overview-tab="report">${railIcon("report")}Export</button>
        </nav>
      </div>
      ${kpiRow([
      {
        label: "Period spend",
        jump: "Top services",
        value: amount(k.total),
        sub: previous ? `Previously ${amount(k.previous)}` : "Previous spend unavailable",
      },
      {
        label: "vs previous period",
        jump: "Biggest movers",
        value: Number.isFinite(k.change_pct) ? pct(k.change_pct) : "Not comparable",
        cls: trendClass(k.change_pct),
        sub:
          delta == null ? "Comparison unavailable" : delta === 0 ? "No net change"
            : `${delta > 0 ? "+" : "−"}${amount(Math.abs(delta))}`,
      },
      { label: "Average per day", value: amount(k.daily_avg), sub: "Across the selected period", jump: "trendChart" },
      {
        label: "Billed resources",
        tab: "resources",
        value: count(k.resources),
        sub: '<span class="overview-text-action">Explore resources →</span>',
      },
      // Filled after the page: the anomaly pass reads a longer window than the spend figures and
      // must never hold them up. Placeholder ids are what the later fill writes into.
      {
        label: "Anomalies",
        tab: "anomalies",
        id: "overviewAnomsOpen",
        value: '<span id="overviewAnomsValue">…</span>',
        sub: '<span id="overviewAnomsBody">Checking for unusual spend…</span>',
      },
    ])}
    </section>
    ${d.mixed_currency ? `<p class="overview-notice">Multiple billing currencies. Amounts show the largest billing currency, ${esc(cur)}; other currencies are not added together.</p>` : ""}

    <div class="overview-main">
      <div id="trendChart" class="card chart-card"></div>
      <section class="card overview-focus" aria-label="Cost drivers">
        <div class="overview-focus-head">
          <h3>Cost drivers</h3>
          <span title="Signals from the selected reporting period and filters, not savings estimates.">This period</span>
        </div>
        <button type="button" class="overview-signal" data-overview-jump="Biggest movers">
          <span class="overview-signal-label">Largest listed increase</span>
          <strong>${highlights.increase ? `+${amount(highlights.increase.delta)}` : "No listed increase"}</strong>
          <span class="overview-signal-detail">${esc(highlights.increase?.name || "Open the comparison to review changes")}</span>
          <span class="overview-signal-arrow" aria-hidden="true">→</span>
        </button>
        <button type="button" class="overview-signal" data-overview-jump="Top services">
          <span class="overview-signal-label">Top service</span>
          <strong>${highlights.service ? amount(highlights.service.cost) : "Not available"}</strong>
          <span class="overview-signal-detail">${esc(highlights.service?.name || "No service cost available")}</span>
          <span class="overview-signal-arrow" aria-hidden="true">→</span>
        </button>
        <button type="button" class="overview-signal" data-overview-jump="Top services">
          <span class="overview-signal-label">Top 5 services</span>
          <strong>${highlights.concentration == null ? "Not available" : `${esc(highlights.concentration)}%`}</strong>
          <span class="overview-signal-detail">Share of selected-period spend</span>
          <span class="overview-signal-arrow" aria-hidden="true">→</span>
        </button>
      </section>
    </div>

    <!-- Same measure as the line above, reshaped, beside what moved and how it moved over
         months: the calendar answers "which days" (weekday-versus-weekend and month-end
         patterns a line compresses into one slope), the bridge answers "which services moved
         this window", month-over-month answers "is that a one-window blip or the trend".

         On demand rather than always open, because this row is the tallest thing on the page
         and it was pushing the figures people actually come here for below the fold. All three
         draw through whenNear, so nothing is fetched or rendered until it is opened. -->
    <details class="overview-detail" id="overviewPatterns">
      <summary><span>Spend patterns</span><span class="overview-detail-hint">Daily pattern, what changed, month over month</span></summary>
      <div class="grid-bridge">
        <div id="spendHeatmap"></div>
        <div id="spendWaterfall"></div>
        <div id="monthOverMonth" class="card chart-card"></div>
      </div>
    </details>

    <details class="overview-detail" id="overviewEstate">
      <summary><span>Estate view</span><span class="overview-detail-hint">Spend hierarchy and regional distribution</span></summary>
      <div class="grid-viz" id="overviewVisuals">
        <div id="spendTreemap"><section class="card viz-placeholder" aria-label="Spend hierarchy loading"></section></div>
        <div id="regionMap"><section class="card viz-placeholder" aria-label="Region map loading"></section></div>
      </div>
    </details>

    <details class="overview-detail" id="overviewBreakdowns">
      <summary><span>Cost breakdowns</span><span class="overview-detail-hint">Subscriptions, services, groups and regions</span></summary>
      <div class="overview-detail-body">
    ${table(
      "By subscription",
      d.subscriptions,
      [
        { label: "Subscription", cell: (r) => `${esc(r.name)} ${bar(r.cost, maxSub)}` },
        changeCol,
        {
          label: "Share",
          num: true,
          value: (r) => r.cost,
          cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
        },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      null
    )}

    <div class="grid-2">
      ${table(
        "Spend by area",
        d.areas,
        [
          { label: "Area", cell: (r) => `${esc(r.label)} ${bar(r.cost, maxArea)}` },
          {
            label: "Share",
            num: true,
            value: (r) => r.cost,
            cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
          },
          changeCol,
          { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        ],
        null,
        { key: `Where the money goes|Area|Share|vs prev ${d.days}d|Cost` }
      )}
      ${table(
        "Biggest movers",
        d.movers,
        [
          { label: "Service", cell: (r) => esc(r.name || "—") },
          changeCol,
          { label: "Now", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2) },
        ],
        `Largest changes vs previous ${d.days} days.`
      )}
    </div>

    <!-- Paired by length rather than by topic: both of these run to eight rows, so the row
         has no slack in it. -->
    <div class="grid-2">
      ${table(
        "Top services",
        d.services,
        [
          { label: "Service", cell: (r) => `${esc(r.name || "—")} ${bar(r.cost, maxService)}` },
          {
            label: "Share",
            num: true,
            value: (r) => r.cost,
            cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
          },
          { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        ],
        null
      )}
      ${
        d.resource_groups.length
          ? table(
              "Top resource groups",
              d.resource_groups,
              [
                { label: "Resource group", cell: (r) => esc(r.name) },
                changeCol,
                {
                  label: "Share",
                  num: true,
                  value: (r) => r.cost,
                  cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
                },
                { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
              ],
              null
            )
          : ""
      }
    </div>

    <!-- The map above answers "where is it concentrated" at a glance; this answers "exactly how
         much, and is it moving". -->

    ${table(
      "By region",
      d.regions || [],
      [
        { label: "Region", cell: (r) => `${esc(r.name)} ${bar(r.cost, maxRegion)}` },
        {
          label: "Resources",
          num: true,
          value: (r) => r.resources || 0,
          cell: (r) => String(r.resources || 0),
          total: (rows) => String(rows.reduce((n, r) => n + (r.resources || 0), 0)),
        },
        changeCol,
        {
          label: "Share",
          num: true,
          value: (r) => r.cost,
          cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
        },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      null
    )}
      </div>
    </details>
    </div>
  `);

  const home = $("overviewHome");
  home.querySelector("#overviewAnalysis").onclick = () => ctx.openAnalysis();
  loadOverviewAnomalies(token, cur);
  for (const button of home.querySelectorAll("[data-overview-tab]")) {
    button.onclick = () => ctx.navigate(button.dataset.overviewTab);
  }
  for (const button of home.querySelectorAll("[data-overview-jump]")) {
    button.onclick = () => reveal(button.dataset.overviewJump);
  }
  renderOverviewVisuals(token, d, cur);
  renderSpendHeatmap(token, d, cur);
  renderSpendWaterfall(token, d, cur);
  renderMonthOverMonth(token, d, cur);

  if (d.trend.labels.length > 1) {
    // The final day of an export is usually still being written. Left in, the line dives to
    // near zero and the opening screen reads as "spend collapsed" — so it comes out of the
    // chart and the caption says so. It stays in every total: it is real money, just not a
    // whole day of it.
    const cut = d.trend.partial_last ? -1 : undefined;
    const labels = cut ? d.trend.labels.slice(0, cut) : d.trend.labels;
    const values = cut ? d.trend.values.slice(0, cut) : d.trend.values;
    const last = d.trend.labels[d.trend.labels.length - 1];

    // The month-to-date tile gets the same series as a sparkline. Handed over rather than
    // fetched again: this is the only place the daily shape is already in memory, and a second
    // request for a figure that sits in a 190px tile would not be worth the round trip.
    setGlanceSpark(values);

    // Remembered so the calendar below can open the same day on this chart. Reset on every
    // render: a stale index from the previous period would open the wrong date.
    trendDays = labels;
    trendSelect = null;

    drawChart($("trendChart"), {
      type: "area",
      title: `Daily spend — ${periodText(d.days)}`,
      labels,
      datasets: [
        { label: "This period", data: values, pointLabels: labels },
        // Aligned by position rather than by date, so day 1 sits above day 1 — which is why
        // each point carries its own date for the tooltip. Without it the hover shows one date
        // above both numbers and silently misattributes the comparison.
        {
          label: "Previous period",
          data: d.trend.previous.slice(0, values.length),
          pointLabels: (d.trend.previous_labels || []).slice(0, values.length),
          dash: true,
        },
      ],
      currency: cur,
      fillHeight: true,
      // Two views of one measure, not two measures: the dashed reference line is told apart by
      // its line style, and giving it a rival hue would make last month look like a second
      // subject rather than the backdrop this month is being read against.
      categorical: false,
      note: d.trend.partial_last
        ? `${last} is partial: excluded from the chart, included in totals.`
        : "",
    }).then(chart => {
      // Only adopt it if this render is still the current one: an await that resolves after a
      // period change would leave the calendar opening days on a chart that has been replaced.
      if (token === ctx.loadToken && chart) trendSelect = chart.$clSelectPoint;
    });
  } else {
    $("trendChart").innerHTML = `<h3>Daily spend</h3>
      <p class="muted empty">Not enough daily records to draw a comparison for this period.</p>`;
  }
}

function reveal(target) {
  const home = $("overviewHome");
  return Boolean(home && revealOverviewTarget(home, target));
}

/**
 * Fill the overview's anomaly KPI after the page is up.
 *
 * Its own request, not a field on the executive payload: the anomaly pass reads a longer
 * history than the overview does, and hanging the opening screen on it would trade the thing
 * people come here for against a figure most days has nothing to say. A failure leaves a note
 * and the link, never an error banner over the spend figures.
 */
let overviewAnomalyPending = null;
let overviewAnomalyRequest = 0;
async function loadOverviewAnomalies(token, currency) {
  const body = $("overviewAnomsBody");
  if (!body) return;
  const request = ++overviewAnomalyRequest;
  overviewAnomalyPending = { currency };
  const current = () => request === overviewAnomalyRequest && token === ctx.loadToken
    && $("overviewAnomsBody") === body;

  // `tone` washes the whole tile by what the period means — "bad" for a rise or anything high
  // severity, "good" for a fall or a clean period, neutral when the movement nets out. The tile
  // is read from across a desk before its numbers are; the colour has to carry the same verdict.
  const finish = (value, sub, tone = "") => {
    if (!current()) return;
    overviewAnomalyPending = null;
    const host = $("overviewAnomsBody");
    if (!host) return;
    const figure = $("overviewAnomsValue");
    if (figure) figure.innerHTML = value;
    const tile = typeof host.closest === "function" ? host.closest(".kpi") : null;
    if (tile) {
      tile.classList.remove("kpi-tone-bad", "kpi-tone-good");
      if (tone) tile.classList.add(`kpi-tone-${tone}`);
    }
    host.outerHTML = `<span id="overviewAnomsBody">${sub}</span>`;
    const open = $("overviewAnomsOpen");
    if (open) open.onclick = () => ctx.navigate("anomalies");
  };
  const review = label =>
    `<span class="overview-text-action">${label} →</span>`;

  let d;
  try {
    d = await cached(key("overview-anomalies"), () => get(`/api/dashboard/anomalies${qs()}`, { timeout: 45000 }));
  } catch {
    finish("—", `Check unavailable · ${review("Open Anomalies")}`);
    return;
  }
  if (token !== ctx.loadToken) return;

  if (!current()) return;
  const s = overviewAnomalySummary(d);
  const cur = s.currency || currency;
  const reasons = [d.coverage_reason || d.coverage?.reason,
    ...Object.entries(d.unavailable_currencies || {}).map(([code, reason]) => `${code}: ${reason}`)].filter(Boolean);
  const assessment = d.unavailable || !Array.isArray(d.anomalies) ? "Not assessed"
    : d.partial || reasons.length ? "Partial assessment" : "";
  const coverageNote = [...new Set(reasons)].join(" ") || d.note
    || "Complete anomaly coverage is unavailable; no all-clear conclusion can be made.";
  if (assessment && !s.count) {
    finish("—", `${assessment} · ${esc(coverageNote)} · ${review("Open Anomalies")}`);
    return;
  }
  if (!s.count) {
    finish("0", `Nothing unusual${s.to ? ` through ${esc(s.to)}` : " in this period"} ·
      ${review("Open Anomalies")}`, "good");
    return;
  }

  // Net effect, not the sum of absolute movements: a rise offset by a fall has not moved the
  // bill, and the KPI must not imply money to go looking for. The largest single mover and its
  // day live one click away rather than being crammed into a tile.
  const net = s.currency && Number.isFinite(s.net)
    ? `<span class="${s.net > 0 ? "up" : s.net < 0 ? "down" : "flat"}">${
      s.net > 0 ? "+" : ""}${moneyHtml(s.net, cur, 0)}</span> net`
    : "Separate billing currencies";
  finish(
    `<span${s.to ? ` title="Judged through ${esc(s.to)}"` : ""}>${s.count}${
      s.high ? ` <span class="anom-flag high">${s.high} high</span>` : ""}</span>`,
    `${assessment ? `${assessment} · ${esc(coverageNote)} · ` : ""}${net} · ${s.increases} up · ${s.decreases} down · ${review("Review")}`,
    // A high-severity finding stays bad news even where the net washes out: a doubled service
    // paid for by a halved one is still something someone has to look at.
    assessment ? "" : s.high || s.net > 0 ? "bad" : s.net < 0 ? "good" : ""
  );
}

function renderOverviewVisuals(token, data, currency) {
  const host = $("overviewVisuals");
  if (!host) return;
  whenNear(host, async () => {
    const [{ drawRegionMap }, { drawTreemap }] = await Promise.all([
      import("./regionmap.js"),
      import("./treemap.js"),
    ]);
    return () => {
      drawRegionMap(host.querySelector("#regionMap"), data.regions, currency, {
        onPick: async row => {
          if (!await ctx.navigate({ tab: "resources", filter: { dimension: "region", value: row.name } })
              && host.isConnected) reveal("By region");
        },
      });
      drawTreemap(host.querySelector("#spendTreemap"), data.subscriptions, currency, {
        title: "Spend hierarchy",
        kind: "Subscription",
        onPick: rows => {
          if (rows.length > 1) return reveal("By subscription");
          const ids = subscriptionDrillIds(rows[0], allSubscriptions(), ctx.scopeIds());
          if (ids.length) return ctx.navigate({ tab: "resources", subscriptions: ids });
          toast("This subscription is not available in the picker. Showing its cost breakdown.");
          reveal("By subscription");
        },
      });
    };
  });
}

function renderSpendHeatmap(token, data, currency) {
  const host = $("spendHeatmap");
  if (!host) return;
  whenNear(host, async () => {
    const { drawCalendarHeatmap } = await import("./calendarheat.js");
    return () => drawCalendarHeatmap(host, data.trend, currency, {
      // The calendar is an index into the line chart above it, not a second source of truth.
      // A day that colours itself "expensive" and cannot then say what it was compared against
      // only raises the question; this answers it with the panel the chart already builds.
      onPick: (iso) => openTrendDay(iso),
    });
  });
}

/**
 * Show one day on the Daily spend chart: this period against the same slot in the previous one.
 *
 * Reaches the chart rather than drawing its own panel, so a day opened from the calendar and a
 * day clicked on the line are the same object with the same numbers — two panels claiming the
 * same date would eventually disagree.
 */
function openTrendDay(iso) {
  const card = $("trendChart");
  const index = trendDays.indexOf(iso);
  if (!card || index < 0) return;
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  trendSelect?.(index);
}

function renderSpendWaterfall(token, data, currency) {
  const host = $("spendWaterfall");
  if (!host) return;
  whenNear(host, async () => {
    const { drawWaterfall } = await import("./waterfall.js");
    return () => drawWaterfall(host, data.movers, data.kpis, currency, {
      days: data.days,
      // A chart is a better filter control than a dropdown: seeing "Azure AI Search fell $141"
      // and clicking it is one gesture, where finding that service among forty in a list is a
      // search. Goes through the filter bar, so the narrowing is visible and its Clear all
      // undoes it — a filter applied from a chart must not be state the controls disagree with.
      onPick: (service) => {
        if (applyFilter("service", service)) {
          toast(`Filtered to ${service}. Clear it from the filter bar.`);
        } else {
          toast(`${service} is not a filterable service in this period.`);
        }
      },
    });
  });
}

function renderMonthOverMonth(token, data, currency) {
  const host = $("monthOverMonth");
  if (!host) return;
  const m = data.monthly;
  // A single bar cannot show a direction, so nothing is drawn. The card itself stays put
  // rather than being removed, unlike the daily line chart: it is the drill-down target for
  // the month-over-month summary tile (glanceDestination), and it holds one column of the
  // .grid-bridge row it shares with two neighbours. Removing it would send that tile to a
  // dead target and leave the row a column short; an explanatory empty state says why the
  // chart is absent, where a missing card would just read as a rendering failure.
  if (!m || !m.months?.length || m.months.length < 2) {
    host.innerHTML = `<h3>Month-over-month spend</h3>
      <p class="muted empty">At least two months of recorded spend are needed for this comparison.</p>`;
    return;
  }
  const firstMonth = Math.max(0, m.months.length - 3);
  const months = m.months.slice(firstMonth);
  whenNear(host, () => () => {
    return drawChart(host, {
      type: "stacked_bar",
      title: "Month-over-month spend",
      labels: months,
      datasets: m.areas.map((a) => ({ label: a.label, data: a.data.slice(firstMonth) })),
      // Areas are named categories (Compute & Web, Storage, ...), not ranked slices of one
      // thing, so they take a categorical palette rather than the ordered ramp a single ranked
      // series would use. This chart gets its own named set rather than the shared one other
      // categorical charts (the commitments tab's pricing-model mix) still use — a teal-to-coral
      // pair tuned specifically for the fixed, seven-area shape this chart always has.
      categorical: true,
      categoricalPalette: "monthOverMonth",
      // Seven areas stacked in one bar: hovering anywhere on a month with the chart's default
      // "whole column" tooltip would list all seven regardless of which one the pointer is
      // actually over, which is exactly backwards for a stack -- the segment under the cursor
      // is the one thing being pointed at. One category at a time instead.
      hoverSingleSegment: true,
      currency: m.currency || currency,
      // This card shares a CSS Grid row with "What changed" (see .grid-bridge), whose own
      // height varies with how many services moved -- a fixed chart height here would just
      // become the size of the gap left below once the row stretches this card to match.
      fillHeight: true,
      note: `Last ${months.length} months available in the warehouse; maximum 3 shown, no missing months added. These are recorded costs; the summary tile compares this month's full-month projection with last month's actual.`,
    });
  });
}


// ------------------------------------------------------------------ cost by tag

/**
 * Cost grouped by the tags on the resources that incurred it.
 *
 * The whole tab runs on one request. The server sends every tagged resource with its cost and
 * its tag keys; selecting, deselecting and switching between all/any is then set arithmetic
 * over an array already in memory, so a click repaints in a frame rather than waiting on a
 * round trip. On a few thousand resources the filter is well under a millisecond.
 *
 * Two selection modes, because both are legitimate questions and they are not interchangeable:
 * "all" answers *what does this combination cost* (resources carrying every selected tag);
 * "any" answers *what is covered by these tags at all*. Reading one as the other silently
 * changes the number, so the mode is a visible control rather than an assumption.
 */
let tagState = null;

/**
 * Which of the tab's two views is on screen: the open-ended explorer above, or the fixed
 * chargeback report below. Independent of `tagState` on purpose — switching views is not a
 * selection, and the explorer's chip selection should still be exactly as left when someone
 * switches back to it.
 */
let tagViewMode = "explorer";

/** The chargeback report's own state: which key it allocates by. Survives a re-render of the
 *  same period; reset when the period or scope changes, in step with `tagState`. */
let chargebackState = null;

/**
 * A selection handed to the Tags tab by another tab, applied on its next render.
 *
 * Budgets created here are tag budgets, and the tab above them is where the tag lives — so a
 * budget saying `Tracks contact = support@…` should be able to show what is actually inside
 * that figure. Passed as state rather than a URL because the tab's selection has never been
 * addressable, and inventing a deep-link format for one button is more surface than it earns.
 */
let pendingTagSelection = null;

/** Open the Tags tab already narrowed to a budget's own filter. */
function showTagsFor(tags, name) {
  if (!tags || !tags.length) return;
  pendingTagSelection = tags;
  pendingTagName = name || null;
  selectTab("tags");
}
let pendingTagName = null;

// Billing filters and the conditional VM scenario persist within this view's scope.
let uptimeSchedule = "business";
let uptimeFilter = { service: "", group: "", region: "", state: "all", find: "", tagKey: "", tagValue: "" };
let uptimeFindTimer = null;

/** A week is 168 hours. Everything on this tab that talks about a schedule divides by it. */
const WEEK_HOURS = 168;

const UPTIME_STATES = [
  ["all", "All meters"],
  ["always", "High billed coverage"],
  ["schedulable", "VMs: needs review"],
  ["idle", "VMs: high coverage"],
  ["part", "Lower billed coverage"],
  ["unassessed", "Not assessed"],
];

/**
 * The values a dimension actually takes, with what each is worth.
 *
 * Built from the rows rather than from a fixed list, so the dropdown can only ever offer a
 * choice that has something behind it — and each option carries its cost, because choosing
 * between eleven service names is a different task from choosing between eleven numbers.
 *
 * Each dimension is counted against the *other* filters but not against itself, which is what
 * lets someone see the alternatives to their current choice rather than only the choice they
 * already made. Counting a dimension against itself would leave every dropdown showing exactly
 * one option the moment it was used.
 */
function uptimeFacet(rows, f, field, tagValues = []) {
  const others = { ...f, [field === "resource_group" ? "group" : field]: "" };
  const totals = new Map();
  for (const r of rows) {
    if (!uptimeKeep(r, others, tagValues)) continue;
    const v = r[field] || "";
    if (!v) continue;
    totals.set(v, (totals.get(v) || 0) + r.cost);
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1]);
}

/**
 * A typical recent day, resistant to the partial one at the end.
 *
 * The mirror of `_recent_daily` on the server, and it exists for the same reason: cost exports
 * restate for a day or two, so the newest day is routinely a fraction of a real one and a mean
 * carries that straight into the headline. Duplicated deliberately — the server cannot compute
 * this for a filter it does not know about, and a round trip per dropdown change is the thing
 * this whole tab is built to avoid.
 */
function recentDaily(series) {
  const tail = series.slice(-7).map(Number).sort((a, b) => a - b);
  if (!tail.length) return 0;
  const mid = Math.floor(tail.length / 2);
  return tail.length % 2 ? tail[mid] : (tail[mid - 1] + tail[mid]) / 2;
}

/** Daily hours and cost for a set of rows, summed from their per-meter cells. */
function uptimeSeries(rows, daily, dates) {
  const hours = new Array(dates.length).fill(0);
  const cost = new Array(dates.length).fill(0);
  for (const r of rows) {
    for (const [i, h, c] of daily[r.key] || []) {
      hours[i] += h;
      cost[i] += c;
    }
  }
  return { hours, cost };
}

/**
 * The headline figures for whatever is currently selected.
 *
 * Computed here even when nothing is filtered, rather than using the server's totals for the
 * unfiltered case and these for the rest. Two implementations of one number is how they come
 * to disagree, and the disagreement would show up only under a filter — which is exactly where
 * nobody has a second source to check it against.
 */
function uptimeTotals(rows, series, totalSpend) {
  const sum = (f) => rows.reduce((n, r) => n + f(r), 0);
  const hours = sum((r) => r.hours);
  const cost = sum((r) => r.cost);
  const alwaysOn = rows.filter((r) => r.always_on);
  const week = series.hours.slice(-7);
  return {
    hours,
    cost,
    coverage_pct: totalSpend > 0 ? (cost / totalSpend) * 100 : 0,
    avg_rate: hours > 0 ? cost / hours : 0,
    run_rate: recentDaily(series.cost) / 24,
    run_hours: recentDaily(series.hours),
    // How many of the last seven days this selection ran at all. The median is the right
    // defence against a partial final day across a whole estate, where most things run daily
    // — but on an intermittent subset it collapses to zero, and "$0.00 an hour" next to "98
    // hours billed" reads as a broken figure rather than a true one. This is what lets the
    // KPI say which it is.
    active_days: week.filter((h) => h > 0).length,
    week_days: week.length,
    resources: new Set(rows.map((r) => r.id)).size,
    meters: rows.length,
    always_on: alwaysOn.length,
    always_on_cost: alwaysOn.reduce((n, r) => n + r.cost, 0),
  };
}

/** Daily billing quantities and derived rates, not measured uptime or hourly spend. */
async function renderUptime(token) {
  const d = await cached(key("uptime"), () =>
    get(`/api/dashboard/uptime${qs()}`, { timeout: 45000 }));
  if (token !== ctx.loadToken) return;

  const all = d.resources || [];
  if (!all.length) {
    setContent($("tabBody"), `<section class="card"><h3>Hourly Billing</h3>
      <p class="muted empty">No hour-based usage records are available for this period.</p>
      <details class="view-help" data-state-key="uptime-help"><summary>About this view</summary>
      <p class="muted">This view requires billing quantities and pure hour units. A Quick refresh
        may not include these fields. Missing records do not prove that services were stopped
        or that no hourly charges exist.</p></details></section>`);
    return;
  }

  const cur = d.currency;
  const dates = d.dates || [];
  const daily = d.daily || {};
  const schedules = d.schedules || [];
  const preset = schedules.find((s) => s.id === uptimeSchedule) || schedules[0];
  const weekly = preset ? preset.hours : WEEK_HOURS;

  const tagValues = d.tag_values || [];
  const tagKeys = d.tag_keys || [];
  // A key that has dropped out of the data — a different period, or a narrower scope — must not
  // leave the tab filtering on something the picker can no longer show.
  if (uptimeFilter.tagKey && uptimeFilter.tagKey !== UPTIME_UNTAGGED
      && !tagKeys.some((k) => k.key === uptimeFilter.tagKey)) {
    uptimeFilter.tagKey = "";
    uptimeFilter.tagValue = "";
    uptimeFilter.tagValueSet = false;
  }

  const f = uptimeFilter;
  const rows = all.filter((r) => uptimeKeep(r, f, tagValues));
  const narrowed = rows.length !== all.length;

  // With the per-meter detail dropped for size there is nothing to rebuild a filtered chart
  // from, so the charts stay estate-wide and say so rather than being quietly wrong.
  const canChart = !d.daily_dropped && dates.length > 1;
  const series = canChart
    ? uptimeSeries(rows, daily, dates)
    : { hours: d.trend?.hours || [], cost: d.trend?.cost || [] };

  const t = uptimeTotals(rows, series, d.total_spend ?? d.totals?.total_spend ?? 0);

  // Only identified VM compute meters enter this conditional scenario; eligibility is unverified.
  const candidates = rows.filter((r) => scheduleSaving(r, weekly) > 0);
  const modelled = candidates.reduce((n, r) => n + scheduleSaving(r, weekly), 0);
  const candidateCost = candidates.reduce((n, r) => n + r.cost, 0);
  const maxCost = rows.reduce((m, r) => Math.max(m, r.cost), 0);

  // A rate is not a total: four decimals, because the interesting ones are fractions of a
  // penny and rounding them to two turns every small meter into "$0.00".
  const rate = (n) => (n ? esc(money(n, cur, 4)) : "—");
  const hours = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });

  // A steady rate is only meaningful for something that runs steadily. Where the selection ran
  // on fewer than half of the last seven days the median is zero and truthful but useless as a
  // headline, so the KPI stops asserting a rate and reports the intermittency instead — that
  // is the real answer to "what does an hour cost" for a workload that is usually off.
  const intermittent = t.run_hours === 0 && t.hours > 0;
  const ranOn = `billed on ${t.active_days} of the last ${t.week_days} recorded days`;
  const scenarioMeters = rows.filter(r => scheduleAssessment(r) === "needs_review");
  const aggregateSeries = narrowed && !canChart;

  const picker = (id, label, field, options) => `
    <label class="muted note">${esc(label)}
      <select id="${id}" data-field="${field}" aria-label="Filter by ${esc(label)}">
        <option value="">All ${esc(label.toLowerCase())} (${options.length})</option>
        ${options
          .map(
            ([v, c]) => `<option value="${esc(v)}"${v === f[field] ? " selected" : ""}>
              ${esc(v)} — ${esc(money(c, cur, 0))}</option>`
          )
          .join("")}
      </select>
    </label>`;

  // The values behind the chosen key, counted against every other filter but not against the
  // value itself, so switching value stays possible after one is picked.
  const chosenKey = f.tagKey && f.tagKey !== UPTIME_UNTAGGED ? f.tagKey : "";
  const valueRows = chosenKey
    ? uptimeTagValues(all.filter((r) => uptimeKeep(r, { ...f, tagValue: "", tagValueSet: false }, tagValues)),
                      chosenKey, tagValues)
    : [];

  // What the tag pickers cannot say themselves: how much spend they can attribute at all, and
  // whether unattributed spend means "untagged resources" or "this load carried no tags".
  const tagNote = !tagKeys.length
    ? d.tags_not_loaded
      ? `Quick refresh does not carry tags, so hour-billed spend cannot be attributed. These
         meters are unclassified, not untagged.`
      : `No tags were recorded against hour-billed meters in this period. A tag added after the
         last metered hour is not visible here.`
    : `${esc(money(d.tagged?.cost || 0, cur, 0))} tagged · ${esc(money(d.untagged?.cost || 0, cur, 0))} unattributed
       across ${tagKeys.length} tag key${tagKeys.length === 1 ? "" : "s"}.${
      d.tags_not_loaded
        ? " The most recent load did not carry tags, so newer spend may count as unattributed."
        : ""
    }`;

  setContent($("tabBody"), `
    <!-- Above the figures, because it changes them. A control that alters the numbers sitting
         above it reads as broken the first time someone uses it. -->
    <section class="portal-workbench" aria-label="Hourly billing summary and filters">
    <section class="card uptime-header">
      <div class="tagpick-head">
        <h3>Hourly Billing</h3>
        ${
          narrowed
            ? `<span class="muted note">${rows.length} of ${all.length} meters ·
                 <button type="button" id="uptimeClear" class="linkish">clear filters</button></span>`
            : `<span class="muted note">${all.length} hourly meters</span>`
        }
      </div>
      <div class="uptime-filters">
        ${picker("upService", "Service", "service", uptimeFacet(all, f, "service", tagValues))}
        ${picker("upGroup", "Resource group", "group", uptimeFacet(all, f, "resource_group", tagValues))}
        ${picker("upRegion", "Region", "region", uptimeFacet(all, f, "region", tagValues))}
        <label class="muted note">Tag key / Application
          <select id="upTagKey" aria-label="Filter by tag key or application">
            <option value="">All tags (${tagKeys.length})</option>
            ${tagKeys
              .map(
                (k) => `<option value="key:${esc(k.key)}"${k.key === f.tagKey ? " selected" : ""}>
                  ${esc(k.key)} — ${esc(money(k.cost, cur, 0))}</option>`
              )
              .join("")}
            <option value="untagged"${f.tagKey === UPTIME_UNTAGGED ? " selected" : ""}>
              Untagged — ${esc(money(d.untagged?.cost || 0, cur, 0))}</option>
          </select>
        </label>
        <!-- Always present, so the pair reads as one control: a value picker that appears only
             after a key is chosen looks like the page moved under you. -->
        <label class="muted note">Tag value
          <select id="upTagValue" aria-label="Filter by tag value"${chosenKey ? "" : " disabled"}>
            ${
              chosenKey
                ? `<option value="">All values (${valueRows.length})</option>
                   ${valueRows
                     .map(
                       (v) => `<option value="value:${esc(v.value)}"${(f.tagValue || f.tagValueSet) && v.value === f.tagValue ? " selected" : ""}>
                         ${esc(v.value || "(empty)")} — ${esc(money(v.cost, cur, 0))} ·
                         ${v.meters} meter${v.meters === 1 ? "" : "s"}</option>`
                     )
                     .join("")}`
                : `<option value="">${
                    f.tagKey === UPTIME_UNTAGGED ? "No values — untagged" : "Pick a tag key first"
                  }</option>`
            }
          </select>
        </label>
        <label class="muted note">Billing pattern
          <select id="upState" aria-label="Filter by billing pattern">
            ${UPTIME_STATES.map(
              ([v, l]) =>
                `<option value="${v}"${v === f.state ? " selected" : ""}>${esc(l)}</option>`
            ).join("")}
          </select>
        </label>
        <label class="muted note">Find
          <input id="upFind" type="search" value="${esc(f.find)}" placeholder="name or meter"
            aria-label="Search resources by name or meter">
        </label>
      </div>
      <p class="muted note uptime-tagnote">${tagNote}</p>
    </section>

    <p class="muted note">Billing-based estimates, not service availability or hour-of-day spend.
      Stopped VMs can remain billable; stopping an App Service app does not stop its plan charges.</p>

    ${
      rows.length
        ? ""
        : `<section class="card"><p class="muted empty">No hourly meter matches these
             filters.</p></section>`
    }

    ${kpiRow([
      {
        label: "Hourly average",
        value: aggregateSeries || intermittent ? "—" : moneyHtml(t.run_rate, cur, 2),
        sub: aggregateSeries ? "selection daily detail unavailable" : intermittent ? ranOn : "recent median daily cost ÷ 24",
      },
      {
        label: "Average hourly rate",
        value: moneyHtml(t.avg_rate, cur, 4),
        sub: `blended across ${t.meters} hourly meter${t.meters === 1 ? "" : "s"}`,
      },
      {
        label: "Hours billed",
        value: hours(t.hours),
        sub: `${t.resources} resource${t.resources === 1 ? "" : "s"}, ${periodText(d.days)}`,
      },
      {
        label: "Hour-billed spend",
        value: moneyHtml(t.cost, cur, 2),
        sub: `${t.coverage_pct.toFixed(1)}% of all spend in this period`,
      },
      {
        label: "High billed coverage",
        value: String(t.always_on),
        sub: `${esc(money(t.always_on_cost, cur, 0))} · estimated coverage ≥97%`,
      },
      {
        label: "Illustrative savings",
        value: scenarioMeters.length ? moneyHtml(modelled, cur, 0) : "Not assessed",
        cls: modelled > 0 ? "down" : "flat",
        sub: scenarioMeters.length ? `${candidates.length} VM compute meters · ${periodText(d.days)}` : "no supported VM compute meters",
      },
    ])}
    </section>

    <!-- Outside the workbench frame, which clips its contents: this is a panel in its own right,
         not part of the summary-and-filters block. Filled after the page by loadUptimeBudget,
         because budgets are a live Azure read and most selections have none. Placed straight
         after the hourly figures, where "and is that within what we said it could spend" is the
         question people actually ask next. -->
    <div id="uptimeBudget"></div>

    <section class="card">
      <div class="tagpick-head">
        <h3>Illustrative savings</h3>
        <label class="muted note">Schedule
          <select id="uptimeSchedule" aria-label="Weekly billed-hour assumption">
            ${schedules
              .map(
                (s) => `<option value="${esc(s.id)}"${s.id === preset?.id ? " selected" : ""}>
                  ${esc(s.label)} — ${s.hours}h/week</option>`
              )
              .join("")}
          </select>
        </label>
      </div>
      <p class="muted">${weekly}h/week · ${Math.round((weekly / WEEK_HOURS) * 100)}% of a full week.
        ${
          candidates.length
            ? `${candidates.length} VM compute meter${candidates.length === 1 ? "" : "s"} needing review
               ${narrowed ? "in this selection" : "in scope"}:
               ${esc(money(candidateCost, cur, 0))} → ${esc(money(candidateCost - modelled, cur, 0))}
              for ${esc(periodText(d.days))}; conditional estimate ${esc(money(modelled, cur, 0))}.`
            : scenarioMeters.length ? "No positive reduction under this billed-hour assumption."
              : "No supported VM compute meters in this view; savings are not assessed."
        }</p>
      <p class="muted note">Not a forecast or verified scheduling recommendation. This assumes VM
        compute charges fall proportionally when billed hours are reduced through deallocation.
        Workload requirements, pricing and commitment charges must be reviewed first. Disks and
        network resources may keep charging. App Service plans and other service types are not
        assessed; stopping an app does not stop its plan charges.</p>
    </section>

    <div id="uptimeTrend" class="card chart-card"></div>
    <div id="uptimeWeek" class="card chart-card"></div>

    ${table(
      narrowed ? "Selected hourly meters" : "Hourly meters",
      rows,
      [
        {
          label: "Resource",
          cell: (r) => `${esc(r.name)} ${bar(r.cost, maxCost)}`,
        },
        { label: "Service", cell: (r) => esc(r.service || "—") },
        { label: "Meter", cell: (r) => esc(r.meter || "—") },
        {
          label: "Hours",
          num: true,
          value: (r) => r.hours,
          cell: (r) => hours(r.hours),
          total: (rows) => hours(rows.reduce((n, r) => n + r.hours, 0)),
        },
        {
          label: "Billed uptime",
          description: "Billed hours ÷ estimated capacity hours between first and last billed dates. Capacity is inferred from peak daily usage; not service availability.",
          num: true,
          value: (r) => r.uptime_pct,
          cell: (r) =>
            `${r.uptime_pct}%${r.units > 1 ? ` <span class="muted">×${r.units}</span>` : ""}${
              r.always_on ? ' <span class="muted">high</span>' : ""
            }`,
        },
        {
          label: "Effective rate",
          num: true,
          value: (r) => r.rate,
          cell: (r) => rate(r.rate),
        },
        {
          label: `Cost, ${d.days}d`,
          num: true,
          value: (r) => r.cost,
          cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2),
        },
        {
          label: "Illustrative savings",
          num: true,
          value: (r) => scheduleSaving(r, weekly),
          cell: (r) => {
            const s = scheduleSaving(r, weekly);
            if (scheduleAssessment(r) !== "needs_review") return '<span class="muted">Not assessed</span>';
            if (s <= 0) return '<span class="muted">No modelled reduction</span>';
            return `<span class="delta down">−${esc(money(s, cur, 2))}</span><span class="anom-sub">Needs review</span>`;
          },
          total: (rows) =>
            rows.some(r => scheduleAssessment(r) === "needs_review") ? `<span class="delta down">−${esc(
              money(rows.reduce((n, r) => n + scheduleSaving(r, weekly), 0), cur, 0)
            )}</span>` : '<span class="muted">Not assessed</span>',
        },
      ],
      `${
          d.truncated ? "Showing the largest meters only. " : ""
        }${d.mixed_currency ? "This estate bills in more than one currency; figures show the largest. " : ""}`,
      { pageSize: 20, key: `${narrowed ? "selected" : "all"}-hourly-billing` }
    )}
    <details class="view-help" data-state-key="uptime-help">
      <summary>About this view</summary>
      <p>Hours are billing quantities from pure hour-priced meters. Billed uptime is billed hours
        divided by estimated capacity between the first and last billed dates, not the resource's
        lifetime. Instance count is inferred from peak daily usage. Neither this percentage nor
        high billed coverage proves that an application, OS or service was available.</p>
      <p>Effective rate is cost divided by billed hours. Hourly average is the median daily cost
        over up to seven recorded days divided by 24, not spend at a particular hour.
        Composite units such as GiB-hours, egress and per-request charges are excluded.</p>
    </details>
  `);

  wireUptimeControls(token);
  loadUptimeBudget(token, f.tagKey, f.tagValue, Boolean(f.tagValue || f.tagValueSet));

  if (canChart && rows.length) {
    // Daily costs are averaged across 24 hours; there is no hour-of-day series.
    drawChart($("uptimeTrend"), {
      type: "area",
      title: narrowed
        ? "Hourly average — selection"
        : "Hourly average — estate",
      labels: dates,
      datasets: [{ label: "Daily cost ÷ 24", data: series.cost.map((c) => +(c / 24).toFixed(4)) }],
      currency: cur,
      note: "Daily spend ÷ 24. The latest export day may be partial; a dip is not evidence of savings.",
    });

    // Weekday patterns describe billing, not runtime or business-hour suitability.
    const week = weekdayProfile(dates, series.hours);
    if (week.some((w) => w.hours > 0)) {
      drawChart($("uptimeWeek"), {
        type: "bar",
        title: "Billed hours by weekday",
        labels: week.map((w) => w.short),
        datasets: [{ label: "Average billed hours", data: week.map((w) => +w.hours.toFixed(1)) }],
        note: "Average billed hours for each weekday in the selected period.",
      });
    } else {
      $("uptimeWeek").remove();
    }
  } else {
    $("uptimeTrend").remove();
    $("uptimeWeek").remove();
  }
}

const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/**
 * Show the budget standing behind the chosen tag value, where one exists.
 *
 * The point of the pairing: this tab answers "what does this application cost per hour", and
 * the question immediately after is "and is that within what we said it could spend". That
 * answer already exists as an Azure budget filtered on the same tag — it was just two tabs
 * away, which is far enough that nobody crossed the gap.
 *
 * What it must not do is imply the two figures are the same money. The budget tracks *all*
 * spend carrying that tag; this tab shows only hour-priced meters, which is a subset. The
 * caption says so, because a budget bar sitting under an hourly total invites exactly that
 * reading.
 */
async function loadUptimeBudget(token, tagKey, tagValue, hasValue = Boolean(tagValue)) {
  const host = $("uptimeBudget");
  if (!host) return;
  if (!tagKey || !hasValue || tagKey === UPTIME_UNTAGGED) {
    host.innerHTML = "";
    return;
  }

  let rows, demoMode = false;
  try {
    // Cached per scope and period like every other read on the tab, so switching between values
    // of the same key does not re-run a multi-second Azure call each time.
    const d = await cached(key("uptime-budgets"), () =>
      get(`/api/budgets${qs()}`, { timeout: 45000 }));
    rows = d?.budgets || [];
    demoMode = d?.demo_mode === true;
  } catch {
    // Silent by design. A budget is context here, not the subject of the tab, and an error
    // banner over the meter table would be a worse answer than no panel at all.
    host.innerHTML = "";
    return;
  }
  if (token !== ctx.loadToken) return;
  if (demoMode) {
    host.innerHTML = '<p class="muted note">Budgets are not included in the PostgreSQL demo data; no Azure budget was requested.</p>';
    return;
  }

  const matches = uptimeBudgetMatches(rows, tagKey, tagValue, hasValue);
  if (!matches.length) {
    host.innerHTML = "";
    return;
  }

  const selection = `${tagKey} = ${tagValue}`;
  host.innerHTML = `<section class="card uptime-budget">
    <div class="tagpick-head">
      <h3>Budget for ${esc(selection)}</h3>
      <span class="muted note">${matches.length === 1 ? "1 budget tracks" : `${matches.length} budgets track`}
        this tag value</span>
    </div>
    <p class="muted note">Azure budgets track every cost carrying this tag. The meters above are
      only the hour-priced ones, so the budget covers more spend than the figures on this tab.</p>
    <div class="budget-list">${matches.map((m, i) => `
      ${budgetCard(m.budget, i, { lazyTrend: true })}
      ${m.extra.length || m.partial ? `<p class="muted note uptime-budget-scope">${
        m.extra.length
          ? `Also filtered on ${esc(m.extra.map(e => `${e.key} = ${e.values.join(", ")}`).join("; "))
            }, so it watches less than this selection.`
          : ""
      }${m.partial ? " This budget covers other values of the same key as well." : ""}</p>` : ""}
    `).join("")}</div>
  </section>`;

  // After the markup, because each chart needs its host in the document. Keyed on the index the
  // card was rendered with rather than on position: a budget with no trend renders no chart
  // host at all, and counting elements would then draw the wrong budget's line.
  for (const chart of host.querySelectorAll(".budget-chart[data-chart]")) {
    const m = matches[Number(chart.dataset.chart)];
    if (m) whenNear(chart, async () => async () => {
      await loadBudgetTrendChart(chart, m.budget, {
        get, currency: ctx.readStore?.("costCurrency", "") || "", drawBudgetChart, esc,
        isCurrent: () => token === ctx.loadToken && chart.isConnected,
      });
    });
  }
  for (const btn of host.querySelectorAll("[data-tagsfor]")) {
    btn.onclick = () => {
      const m = matches[Number(btn.dataset.tagsfor)];
      if (m) ctx.showTagsFor(m.budget.tag_filter, m.budget.name);
    };
  }
}
/**
 * Average hours per day of the week, from a daily series.
 *
 * Dates are plain YYYY-MM-DD, so they are read in UTC: parsing them as local time would shift
 * the whole series by a day for anyone west of Greenwich and silently rotate the chart.
 */
function weekdayProfile(dates, hours) {
  const bucket = WEEKDAY_NAMES.map(() => ({ hours: 0, n: 0 }));
  dates.forEach((day, i) => {
    const parsed = new Date(`${day}T00:00:00Z`);
    if (Number.isNaN(parsed.getTime())) return;
    // JS weeks start on Sunday; this chart starts on Monday, as the server's does.
    const slot = (parsed.getUTCDay() + 6) % 7;
    bucket[slot].hours += hours[i] || 0;
    bucket[slot].n += 1;
  });
  return WEEKDAY_NAMES.map((short, i) => ({
    short,
    hours: bucket[i].n ? bucket[i].hours / bucket[i].n : 0,
    days: bucket[i].n,
  }));
}

/** Re-render on any change: one path, so the panels cannot disagree about what is selected. */
function wireUptimeControls(token) {
  const again = async () => {
    await renderUptime(token);
    // renderTab wires the tables it renders; a re-render from here has to do it itself, or
    // sorting and paging quietly stop working after the first filter change.
    if (token === ctx.loadToken) wireTables($("tabBody"));
  };

  for (const id of ["upService", "upGroup", "upRegion"]) {
    const el = $(id);
    if (el) el.onchange = (e) => { uptimeFilter[e.target.dataset.field] = e.target.value; again(); };
  }
  if ($("upState")) $("upState").onchange = (e) => { uptimeFilter.state = e.target.value; again(); };
  if ($("upTagKey")) {
    // A value only means something under its own key, so changing the key drops the value.
    $("upTagKey").onchange = (e) => {
      const value = e.target.value;
      uptimeFilter.tagKey = value === "untagged" ? UPTIME_UNTAGGED
        : value.startsWith("key:") ? value.slice(4) : value;
      uptimeFilter.tagValue = "";
      uptimeFilter.tagValueSet = false;
      again();
    };
  }
  if ($("upTagValue")) {
    $("upTagValue").onchange = (e) => {
      const value = e.target.value;
      uptimeFilter.tagValue = value.startsWith("value:") ? value.slice(6) : value;
      uptimeFilter.tagValueSet = value !== "";
      again();
    };
  }
  if ($("uptimeSchedule")) {
    $("uptimeSchedule").onchange = (e) => { uptimeSchedule = e.target.value; again(); };
  }
  if ($("uptimeClear")) {
    $("uptimeClear").onclick = () => {
      clearTimeout(uptimeFindTimer);
      uptimeFilter = { service: "", group: "", region: "", state: "all", find: "", tagKey: "", tagValue: "" };
      for (const [id, value] of Object.entries({
        upService: "", upGroup: "", upRegion: "", upState: "all", upFind: "", upTagKey: "",
      })) {
        if ($(id)) $(id).value = value;
      }
      again();
    };
  }

  const find = $("upFind");
  if (find) {
    // Debounced, and the caret restored afterwards: re-rendering on every keystroke would
    // rebuild the input under the person typing into it.
    find.oninput = (e) => {
      const { value, selectionStart } = e.target;
      clearTimeout(uptimeFindTimer);
      uptimeFindTimer = setTimeout(async () => {
        if (token !== ctx.loadToken) return;
        uptimeFilter.find = value;
        await again();
        const back = $("upFind");
        if (back) {
          back.focus();
          back.setSelectionRange(selectionStart, selectionStart);
        }
      }, 250);
    };
  }
}

/** Conditional VM-compute estimate; not a verified shutdown saving. */
function scheduleSaving(r, weeklyHours) {
  return illustrativeSaving(r, weeklyHours);
}

async function renderTags(token) {
  const d = await cached(key("tags"), () =>
    get(`/api/dashboard/tags${qs()}`, { timeout: 45000 }));
  if (token !== ctx.loadToken) return;

  const keys = d.keys || [];
  if (!keys.length && !(d.resources || []).length) {
    // Three different situations, and only one of them is "you have no tags". A quick refresh
    // loads costs through the Query API, which cannot return tags at all — so the warehouse
    // legitimately holds spend with no tag column, and saying the estate is untagged would be
    // stating something false about it with complete confidence.
    let why;
    let fix = "";
    if (d.tags_not_loaded) {
      why = "Quick refresh does not include tags. Tag status is unknown, not untagged.";
      fix = `<p class="muted">Load tags with <strong>Refresh → FOCUS report</strong>.</p>`;
    } else if ((d.untagged?.resources || 0) > 0) {
      why = `None of the ${d.untagged.resources.toLocaleString()} resources in this period carry a tag.`;
    } else {
      why = "No cost data is loaded for this period yet.";
    }
    setContent($("tabBody"), `<section class="card"><h3>Applications &amp; tags</h3>
      <p class="muted empty">${esc(why)}</p>${fix}</section>`);
    return;
  }

  // Selection survives a re-render of the same view but not a change of period or scope, where
  // the tags on offer may genuinely differ.
  const sig = key("tags");
  if (!tagState || tagState.sig !== sig) {
    // `values` maps a selected tag key to the set of its values the user has narrowed to.
    // Absent or empty means "every value of that key", which is what selecting a key alone
    // has always meant.
    tagState = { sig, selected: new Set(), values: new Map(), mode: "all", filter: "", day: null,
                 resourceView: "all", valueFilters: new Map(), undo: null,
                 budget: { open: false } };
  }

  tagState.data = d;

  // Replace, rather than merge with, the previous exploration. Keep missing keys selected:
  // dropping one from an AND budget would silently widen its filter.
  if (pendingTagSelection) {
    tagViewMode = "explorer";
    tagState.selected.clear();
    tagState.values.clear();
    tagState.valueFilters.clear();
    tagState.resourceView = "all";
    tagState.mode = "all";
    tagState.filter = "";
    tagState.undo = null;
    const have = new Set(d.keys.map((k) => k.key));
    // Case-insensitively, because Azure filters budgets that way even though it stores the
    // keys case-sensitively: this estate has `Contact` and `contact` as separate keys and a
    // budget on one is answered by both.
    const lower = new Map(d.keys.map((k) => [k.key.toLowerCase(), k.key]));
    let applied = 0;
    for (const [rawKey, values] of pendingTagSelection) {
      const k = have.has(rawKey) ? rawKey : lower.get(String(rawKey).toLowerCase()) || String(rawKey);
      tagState.selected.add(k);
      if (values && values.length) {
        tagState.values.set(k, new Set(values));
      }
      applied += 1;
    }
    tagState.fromBudget = applied ? pendingTagSelection : null;
    tagState.fromBudgetName = applied ? pendingTagName : null;
    pendingTagSelection = null;
    pendingTagName = null;
  }

  // Same reset condition as the explorer's own state: a new period or scope can offer entirely
  // different keys, so a chosen allocation key that no longer exists must not linger silently.
  if (!chargebackState || chargebackState.sig !== sig) {
    chargebackState = { sig, key: d.keys[0]?.key || null };
  }

  if (tagViewMode === "chargeback") {
    setContent($("tabBody"), tagViewToggle(d) + chargebackMarkup(d));
    wireTagViewToggle(d);
    wireChargeback(d);
    return;
  }

  setContent($("tabBody"), tagViewToggle(d) + tagsMarkup(d));
  wireTagViewToggle(d);
  wireTags();
  paintTags();
}

/**
 * Explorer versus chargeback, above either view rather than inside it.
 *
 * The two answer different questions from the same data — "what does this combination of tags
 * cost" against "what does each business unit owe" — and neither is a mode of the other, so this
 * is a switch between two whole sections rather than a filter kept within one of them.
 */
function tagViewToggle(d) {
  return `<div class="seg tagview-toggle" role="radiogroup" aria-label="View">
    <button type="button" class="seg-btn${tagViewMode === "explorer" ? " on" : ""}"
            data-tagview="explorer" role="radio"
            aria-checked="${tagViewMode === "explorer"}">Explorer</button>
    <button type="button" class="seg-btn${tagViewMode === "chargeback" ? " on" : ""}"
            data-tagview="chargeback" role="radio"
            aria-checked="${tagViewMode === "chargeback"}">Spend breakdown</button>
  </div>${tagFilterNote(d)}`;
}

/**
 * Names the filter-bar choices this tab did not apply. Its per-resource money is summed
 * at a grain without charge type, pricing model or environment; applying those would put
 * the tab on the full detail table. A filter that silently does nothing reads as a wrong
 * number, so the skip is said where the figures are.
 */
const FILTER_LABELS = { charge: "Cost type", pricing: "Pricing", env: "Environment" };
function tagFilterNote(d) {
  const skipped = (d?.filters_skipped || []).map(k => FILTER_LABELS[k] || k);
  if (!skipped.length) return "";
  return `<p class="banner" data-tag-filters-skipped>${esc(skipped.join(" and "))}
    ${skipped.length === 1 ? "filter does" : "filters do"} not apply on this tab. Resource group,
    service and region filters do.</p>`;
}

function wireTagViewToggle(d) {
  for (const b of $("tabBody").querySelectorAll("[data-tagview]")) {
    b.onclick = () => {
      if (tagViewMode === b.dataset.tagview) return;
      closeTagDropdowns();
      tagViewMode = b.dataset.tagview;
      if (tagViewMode === "chargeback") {
        setContent($("tabBody"), tagViewToggle(d) + chargebackMarkup(d));
        wireTagViewToggle(d);
        wireChargeback(d);
      } else {
        setContent($("tabBody"), tagViewToggle(d) + tagsMarkup(d));
        wireTagViewToggle(d);
        wireTags();
        paintTags();
      }
    };
  }
}

function tagsMarkup(d) {
  const cur = d.currency;
  const totals = tagResourceTotals(d);
  const { all, tagged, untagged, unknown } = totals;
  const coverage = all.cost ? (tagged.cost / all.cost) * 100 : null;
  const uncertain = d.tags_not_loaded || unknown.resources > 0;

  return `
    ${d.mixed ? `<div class="banner warn">This estate bills in more than one currency.
        Showing ${esc(cur || "")} only — the rest is excluded rather than converted.</div>` : ""}
    ${d.truncated ? `<div class="banner warn">Showing the first 8,000 resources.
        Resource counts and selection totals are partial. Narrow the subscription scope for a complete figure.</div>` : ""}
    ${uncertain ? `<div class="banner warn">Tag data is not fully loaded. Resources without
        observed tags have unknown tag status, not confirmed untagged status.
        Load tags with <strong>Refresh → FOCUS report</strong>.
        ${unknown.resources.toLocaleString()} resources are in Unknown below.</div>` : ""}
    ${tagState?.fromBudget ? `<div class="banner">Showing the tags
        <strong>${esc(tagState.fromBudgetName || "a budget")}</strong> tracks. Figures here can
        be higher than the budget's: this tab attributes a resource's whole cost to its tags,
        while a budget counts only the charges carrying them. Clear a chip to widen.</div>` : ""}

    ${kpiRow([
      { label: "Matching spend", value: `<span id="tagSum">—</span>`,
        sub: `<span id="tagSumSub">All loaded resources</span>` },
      { label: "Tagged spend", value: esc(money(tagged.cost, cur, 2)),
        sub: `${tagged.resources.toLocaleString()} resource${tagged.resources === 1 ? "" : "s"}` },
      { label: "Untagged spend", value: esc(money(untagged.cost, cur, 2)),
        cls: untagged.cost > 0 ? "up" : "",
        sub: `${untagged.resources.toLocaleString()} confirmed resource${untagged.resources === 1 ? "" : "s"}` },
      ...(unknown.resources ? [{ label: "Unknown tag spend", value: esc(money(unknown.cost, cur, 2)),
        sub: `${unknown.resources.toLocaleString()} resource${unknown.resources === 1 ? "" : "s"} · tags unavailable` }] : []),
      { label: "Tag coverage", value: uncertain || coverage === null ? "—" : `${coverage.toFixed(0)}%`,
        sub: uncertain ? "requires complete tag data" : coverage === null ? "no net spend to measure" : "of spend carries a tag" },
    ])}

    <section class="card tagpick tag-workbench" id="tagWorkbench" aria-labelledby="tagWorkbenchTitle">
      <div class="tagpick-head">
        <h3 id="tagWorkbenchTitle">Resource &amp; tag filters</h3>
        <div class="seg" role="group" aria-label="Resource tag status">
          ${Object.keys(totals).map(view => `<button type="button"
            class="seg-btn${tagState.resourceView === view ? " on" : ""}" data-resource-view="${view}"
            aria-pressed="${tagState.resourceView === view}">${title(view)} · ${totals[view].resources.toLocaleString()}</button>`).join("")}
        </div>
      </div>
      <div class="tag-filter-controls">
        <div class="tag-key-row">
          ${tagDropdown("tagKeys", "Select tag keys", `
            <input id="tagFilter" type="search" class="tagsearch" placeholder="Search tag keys…"
              aria-label="Search tag keys" autocomplete="off">
            <p class="muted tag-key-count" id="tagKeyCount" role="status"></p>
            <div class="tag-key-options" id="tagChips" role="group" aria-label="Available tags"></div>
          `)}
          ${tagDropdown("tagValues", "Select a tag first", `
            <div class="tag-key-options" id="tagValueGroups"></div>
          `, true)}
        </div>
        <div class="seg" role="radiogroup" aria-label="How to combine selected tags">
          <button type="button" class="seg-btn on" data-mode="all" role="radio"
                  aria-checked="true">Has all (AND)</button>
          <button type="button" class="seg-btn" data-mode="any" role="radio"
                  aria-checked="false">Has any (OR)</button>
        </div>
      </div>
      <div class="tag-filter-selection" role="group" aria-labelledby="tagSelectedTitle">
        <span class="tag-selection-label" id="tagSelectedTitle">Selected filters</span>
        <div class="tagchips selected" id="tagSelected" aria-live="polite"></div>
        <div class="tagsel-actions">
          <button type="button" class="linkish" id="tagClear" hidden>Clear filters</button>
          <button type="button" class="linkish" id="tagUndo" hidden>Undo clear</button>
          <button type="button" class="primary sm" id="tagBudgetBtn" hidden>Create budget</button>
        </div>
      </div>
      <div class="tag-filter-foot">
        <p class="muted" id="tagModeHint"></p>
        <details class="view-help" data-state-key="tags-help">
          <summary>About this view</summary>
          <p>All = Tagged + Untagged + Unknown. Counts cover loaded resources before key and value
            filters, not the number of tag keys. Unknown means tags were not available in the loaded data.</p>
          <p>Select keys, then values, to narrow resources. Has all (AND) requires every selected
            key; Has any (OR) requires at least one. Multiple values within a key match ANY (OR).</p>
          <p>Key and value counts and costs are independent, before other filters.
            Matching resources count once, even when they carry several selected tags.</p>
        </details>
      </div>
    </section>

    <div id="tagBudgets" hidden></div>
    <div id="tagBudget"></div>
    <div id="tagTrend"></div>
    <div id="tagDay"></div>
    <div id="tagResults"></div>`;
}

function tagDropdown(id, label, contents, disabled = false) {
  return `<div class="tag-key-picker">
    <button type="button" class="tag-key-toggle" id="${id}Toggle" data-tag-key-toggle="${id}"${disabled ? " disabled" : ""}
      popovertarget="${id}Menu" aria-haspopup="dialog" aria-expanded="false" aria-controls="${id}Menu">
      <span class="tag-key-label" id="${id}Label">${esc(label)}</span>
      <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">
        <path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
    <div popover="auto" class="tag-key-menu" id="${id}Menu" role="dialog" aria-label="${esc(label)}">
      ${contents}
    </div>
  </div>`;
}

let tagDropdownCleanups = [];

function closeTagDropdowns() {
  for (const cleanup of tagDropdownCleanups) cleanup();
}

function wireTagDropdowns() {
  closeTagDropdowns();
  tagDropdownCleanups = [];
  for (const button of $("tabBody").querySelectorAll("[data-tag-key-toggle]")) {
    const menu = $(`${button.dataset.tagKeyToggle}Menu`);
    let listeners;
    let isOpen = false;
    tagDropdownCleanups.push(() => {
      listeners?.abort();
      if (isOpen && menu.isConnected) menu.hidePopover();
      isOpen = false;
      button.setAttribute("aria-expanded", "false");
    });
    menu.onbeforetoggle = event => {
      listeners?.abort();
      const open = event.newState === "open";
      isOpen = open;
      button.setAttribute("aria-expanded", String(open));
      if (!open) return;
      // The top layer avoids clipping by cards and scroll containers. Place it beside its
      // trigger, preferring the space below but keeping short viewports and long lists usable.
      const box = button.getBoundingClientRect();
      const gap = 6, inset = 12;
      const width = Math.min(Math.max(box.width, 360), window.innerWidth - inset * 2);
      const below = window.innerHeight - box.bottom - gap - inset;
      const above = box.top - gap - inset;
      const up = below < 240 && above > below;
      Object.assign(menu.style, {
        width: `${width}px`, maxHeight: `${Math.max(0, Math.min(400, up ? above : below))}px`,
        left: `${Math.max(inset, Math.min(box.left, window.innerWidth - width - inset))}px`,
        top: `${up ? box.top - gap : box.bottom + gap}px`,
        transform: up ? "translateY(-100%)" : "none",
      });
      listeners = new AbortController();
      const close = () => menu.hidePopover();
      window.addEventListener("resize", close, { signal: listeners.signal });
      document.addEventListener("scroll", event => {
        if (menu.contains(event.target)) return;
        // A click can scroll its trigger into view before opening, with the scroll event
        // delivered afterward. Only dismiss if the trigger moved since the menu opened.
        const current = button.getBoundingClientRect();
        if (Math.abs(current.top - box.top) > 1 || Math.abs(current.left - box.left) > 1) close();
      }, { capture: true, signal: listeners.signal });
    };
    menu.ontoggle = event => {
      if (event.newState === "open" && menu.isConnected) menu.querySelector("input[type=search]")?.focus({ preventScroll: true });
    };
    menu.onkeydown = event => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
      const scope = event.target.hasAttribute("data-value-search")
        ? event.target.closest(".tag-key-values") : menu;
      const options = [...scope.querySelectorAll("input[data-tag], input[data-vk]")];
      if (!options.length) return;
      event.preventDefault();
      const index = options.indexOf(document.activeElement);
      const next = index < 0 ? (event.key === "ArrowDown" ? 0 : options.length - 1)
        : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options[next].focus({ preventScroll: true });
      options[next].scrollIntoView({ block: "nearest" });
    };
  }
}

/**
 * Cost allocated to one chosen tag key, with an explicit Unallocated bucket.
 *
 * The explorer above answers "what does this combination of tags cost" for whichever tags
 * someone selects. A chargeback report asks a narrower, fixed question — "what does each value
 * of *one* key cost" — and needs the resources that carry no value for it counted too, or the
 * report silently omits whatever nobody remembered to tag. No new request: every figure here
 * comes from the same `d.resources` the explorer already has in memory, so switching the
 * allocation key is a re-aggregation, not a round trip.
 *
 * Defaults to the costliest key in the estate (`d.keys[0]`) rather than a hardcoded name like
 * `CostCenter`: that field is an EA billing concept most subscriptions never populate, while the
 * convention an estate actually uses — `Environment`, `Owner`, `Team`, whatever it is — is
 * exactly what `d.keys` already ranks by spend.
 */
function chargebackMarkup(d) {
  const cur = d.currency;
  const keys = d.keys || [];
  if (!keys.length) {
    return `<section class="card"><h3>Spend breakdown</h3>
      <p class="muted empty">No tag keys with spend in this window.</p></section>`;
  }

  const chosen = chargebackState.key && keys.some((k) => k.key === chargebackState.key)
    ? chargebackState.key
    : keys[0].key;
  chargebackState.key = chosen;

  const rows = chargebackBuckets(d, chosen);
  const total = rows.reduce((n, r) => n + r.cost, 0);
  const unallocated = rows.find((r) => r.unallocated);
  const maxCost = rows.reduce((m, r) => Math.max(m, r.cost), 0);

  return `
    ${kpiRow([
      { label: "Allocated by", value: esc(chosen) },
      { label: "Values", value: String(rows.length - (unallocated ? 1 : 0)) },
      { label: "Total", value: esc(money(total, cur, 2)) },
      { label: "Unallocated", value: esc(money(unallocated?.cost || 0, cur, 2)),
        cls: unallocated?.cost ? "up" : "",
        sub: unallocated
          ? `${unallocated.resources.toLocaleString()} resource${unallocated.resources === 1 ? "" : "s"}`
          : "every resource is covered" },
    ])}

    <div class="chargeback-key">
      <label for="chargebackKey">Allocate by</label>
      <select id="chargebackKey">
        ${keys.map((k) => `<option value="${esc(k.key)}"${
          k.key === chosen ? " selected" : ""}>${esc(k.key)}
          (${esc(money(k.cost, cur, 0))})</option>`).join("")}
      </select>
    </div>

    ${table(
      `Spend breakdown by ${chosen}`,
      rows,
      [
        {
          label: "Value",
          cell: (r) => r.unallocated
            ? `<span class="chargeback-unalloc">Unallocated</span> ${bar(r.cost, maxCost)}`
            : `${esc(r.value)} ${bar(r.cost, maxCost)}`,
        },
        { label: "Resources", num: true, value: (r) => r.resources, cell: (r) => r.resources.toLocaleString() },
        {
          label: "Share",
          num: true,
          value: (r) => r.cost,
          cell: (r) => (total ? `${((r.cost / total) * 100).toFixed(1)}%` : "—"),
        },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (all) => moneyHtml(all.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      "Whole resource costs, counted once. Missing values are Unallocated."
    )}
    <details class="view-help" data-state-key="chargeback-help">
      <summary>About this view</summary>
      <p>Each resource's whole cost is allocated to its value for the selected key.
        Resources without that value remain in Unallocated, so they are not omitted from totals.
        Change the key to compare another allocation; Explorer filters are independent.</p>
    </details>`;
}

/**
 * Group `d.resources` by their value for `keyName`, plus an Unallocated bucket.
 *
 * Values are read back from the interned pool (`d.values[v[i]]`) rather than re-parsing tags,
 * because the explorer already paid that cost once when the response arrived.
 */
function chargebackBuckets(d, keyName) {
  const values = d.values || [];
  const totals = new Map();
  let unallocCost = 0;
  let unallocCount = 0;

  for (const r of d.resources || []) {
    const i = (r.keys || []).indexOf(keyName);
    if (i === -1) {
      unallocCost += r.cost;
      unallocCount += 1;
      continue;
    }
    const value = values[r.v[i]] ?? "(blank)";
    const slot = totals.get(value) || { value, cost: 0, resources: 0 };
    slot.cost += r.cost;
    slot.resources += 1;
    totals.set(value, slot);
  }

  const rows = [...totals.values()].sort((a, b) => b.cost - a.cost);

  if (unallocCount) {
    rows.push({ value: "Unallocated", cost: unallocCost, resources: unallocCount, unallocated: true });
    rows.sort((a, b) => b.cost - a.cost);
  }
  return rows;
}

function wireChargeback(d) {
  wireTables($("tabBody"));
  const select = $("chargebackKey");
  if (select) {
    select.onchange = () => {
      chargebackState.key = select.value;
      setContent($("tabBody"), tagViewToggle(d) + chargebackMarkup(d));
      wireTagViewToggle(d);
      wireChargeback(d);
    };
  }
}

function wireTags() {
  wireTagDropdowns();
  for (const b of $("tabBody").querySelectorAll("[data-resource-view]")) {
    b.onclick = () => {
      tagState.resourceView = b.dataset.resourceView;
      paintTags();
    };
  }
  for (const b of $("tabBody").querySelectorAll("[data-mode]")) {
    b.onclick = () => {
      tagState.mode = b.dataset.mode;
      for (const other of $("tabBody").querySelectorAll("[data-mode]")) {
        const on = other === b;
        other.classList.toggle("on", on);
        other.setAttribute("aria-checked", String(on));
      }
      paintTags();
    };
  }
  const filter = $("tagFilter");
  if (filter) {
    filter.value = tagState.filter;
    filter.oninput = () => {
      tagState.filter = filter.value;
      paintTagKeys();
      $("tagClear").hidden = false;
    };
  }
  const clear = $("tagClear");
  if (clear) {
    clear.onclick = () => {
      tagState.undo = {
        selected: new Set(tagState.selected),
        values: new Map([...tagState.values].map(([k, values]) => [k, new Set(values)])),
        valueFilters: new Map(tagState.valueFilters),
        mode: tagState.mode, filter: tagState.filter, resourceView: tagState.resourceView,
        day: tagState.day,
      };
      tagState.selected.clear();
      tagState.values.clear();
      tagState.valueFilters.clear();
      tagState.filter = "";
      tagState.resourceView = "all";
      tagState.mode = "all";
      tagState.day = null;
      if (filter) filter.value = "";
      paintTags();
      $("tagUndo")?.focus({ preventScroll: true });
    };
  }
  const undo = $("tagUndo");
  if (undo) undo.onclick = () => {
    if (!tagState.undo) return;
    Object.assign(tagState, tagState.undo, { undo: null });
    if (filter) filter.value = tagState.filter;
    paintTags();
    ($("tagClear").hidden ? filter : $("tagClear"))?.focus({ preventScroll: true });
  };
  const budget = $("tagBudgetBtn");
  if (budget) {
    budget.onclick = () => {
      // A result on screen means the button reopens the form rather than toggling it shut,
      // which is what "Create budget" says it does.
      const open = !!tagState.budget?.created || !tagState.budget?.open;
      tagState.budget = { open };
      paintBudget();
      if (open) {
        $("tagBudget")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    };
  }
}

function toggleTag(k) {
  if (tagState.selected.has(k)) {
    tagState.selected.delete(k);
    // Deselecting a key drops the values narrowed under it. Keeping them would silently
    // re-apply a filter the user cannot see when they pick the key up again later.
    tagState.values.delete(k);
    tagState.valueFilters.delete(k);
  } else {
    tagState.selected.add(k);
  }
  paintTags();
}

/** The value a resource carries for one tag key, or null if it does not carry that key. */
function tagValue(resource, k) {
  const i = (resource.keys || []).indexOf(k);
  if (i < 0) return null;
  const pool = tagState.data.values || [];
  return pool[(resource.v || [])[i]] ?? "";
}

/**
 * A resource's tags as `key=value` pills.
 *
 * The key alone was ambiguous in exactly the way the value picker exists to fix: a row tagged
 * `Owner` tells you nothing about whose it is. Long values are truncated visually but kept
 * whole in the tooltip, because an app-insights resource id is a legitimate tag value and
 * would otherwise push every other column off the table.
 */
function tagPills(resource) {
  if (!(resource.keys || []).length) {
    return `<span class="muted">${resourceTagStatus(resource, tagState.data) === "unknown"
      ? "Tag status unknown" : "Untagged"}</span>`;
  }
  return (resource.keys || [])
    .map((k) => {
      const v = tagValue(resource, k);
      const shown = v === "" || v === null ? k : `${k}=${v}`;
      const on = tagState.selected.has(k)
        && (() => {
          const wanted = tagState.values.get(k);
          return !wanted || !wanted.size || wanted.has(v);
        })();
      return `<span class="minitag${on ? " on" : ""}" title="${esc(shown)}">${esc(shown)}</span>`;
    })
    .join("");
}

/**
 * Does this resource satisfy a selected key, including any value narrowing on it?
 *
 * Values within a key are OR: `Owner` narrowed to Deb and Ravi means either, because they are
 * alternatives for the same field. That is the ordinary meaning of a facet, and the AND/OR
 * toggle stays where it is — across keys, where the ambiguity actually was.
 */
function matchesKey(resource, k) {
  const v = tagValue(resource, k);
  if (v === null) return false;
  const wanted = tagState.values.get(k);
  return !wanted || wanted.size === 0 || wanted.has(v);
}

/** Key discovery is local; searching the dropdown never changes the resource filters. */
function paintTagKeys() {
  const d = tagState.data;
  const cur = d.currency;
  const q = tagState.filter.trim().toLowerCase();
  const shown = d.keys.filter((k) => !q || k.key.toLowerCase().includes(q));
  $("tagKeyCount").textContent = `${shown.length} of ${d.keys.length} keys shown · ${tagState.selected.size} selected · counts before filters`;
  $("tagKeysLabel").textContent = tagState.selected.size === 1 ? [...tagState.selected][0]
    : tagState.selected.size ? `${tagState.selected.size} tag keys selected` : `Select tag keys (${d.keys.length})`;

  $("tagChips").innerHTML = shown.length
    ? shown
        .map((k) => {
          const on = tagState.selected.has(k.key);
          const many = k.values > 1;
          return `<label class="tag-key-option${on ? " on" : ""}"
              title="${esc(k.key)} — ${esc(money(k.cost, cur))} across ${k.resources} resource${k.resources === 1 ? "" : "s"}${
                many ? `, ${k.values} different values` : ""}">
              <input type="checkbox" data-tag="${esc(k.key)}" ${on ? "checked" : ""} aria-label="${esc(k.key)}">
              <span class="tag-key-name">${esc(k.key)}</span>
              <span class="tag-key-meta">${k.resources} resource${k.resources === 1 ? "" : "s"}${
                many ? ` · ${k.values} values` : ""}</span>
            </label>`;
        })
        .join("")
    : `<p class="muted empty">${d.keys.length ? `No tag key matches “${esc(q)}”. Clear the key search to show all keys.`
      : d.tags_not_loaded ? "Tag keys are unavailable until tag data is loaded."
      : "No tag keys observed in the loaded cost data. You can still explore resources below."}</p>`;

  for (const b of $("tagChips").querySelectorAll("[data-tag]")) {
    b.onclick = () => toggleTag(b.dataset.tag);
  }
}

function paintTagValues() {
  const keys = [...tagState.selected];
  const button = $("tagValuesToggle");
  button.disabled = keys.length === 0;
  const selected = keys.reduce((count, k) => count + (tagState.values.get(k)?.size || 0), 0);
  const label = !keys.length ? "Select a tag first"
    : selected ? `${selected} value${selected === 1 ? "" : "s"} selected` : "All values";
  $("tagValuesLabel").textContent = label;
  button.title = !keys.length ? label : keys.length === 1 ? `Select values for ${keys[0]}` : "Select values for the selected tags";
  $("tagValuesMenu").setAttribute("aria-label", "Select tag values");
  if (button.disabled && button.getAttribute("aria-expanded") === "true") $("tagValuesMenu").hidePopover();
  $("tagValueGroups").innerHTML = keys.map(tagValuesMarkup).join("");
  wireTagValues();
}

/**
 * Values stay grouped by selected key in their own dropdown, not in a set of page cards.
 * Counts and costs describe that value alone, before other key filters.
 */
function tagValuesMarkup(k) {
  const d = tagState.data;
  const cur = d.currency;
  const totals = new Map();
  for (const r of d.resources) {
    const v = tagValue(r, k);
    if (v === null) continue;
    const slot = totals.get(v) || { cost: 0, n: 0 };
    slot.cost += r.cost;
    slot.n += 1;
    totals.set(v, slot);
  }
  const wanted = tagState.values.get(k);
  const rows = [...totals.entries()]
    .map(([value, t]) => ({ value, ...t }))
    .sort((a, b) => b.cost - a.cost);
  const query = tagState.valueFilters.get(k) || "";
  const shown = rows.filter(r => !query.trim() || (r.value || "(no value)").toLowerCase().includes(query.trim().toLowerCase()));

  return `<div class="tag-key-values" role="group" aria-label="Values for ${esc(k)}">
    <strong class="tag-value-key">${esc(k)}</strong>
    <div class="tag-value-head">
      <span>${wanted?.size ? `${wanted.size} selected` : "All values included"} · ANY (OR)</span>
      ${wanted?.size ? `<button type="button" class="linkish" data-allvals="${esc(k)}">All values</button>` : ""}
    </div>
    <input type="search" class="tagsearch" data-value-search="${esc(k)}"
      value="${esc(query)}" placeholder="Search values…"
      aria-label="Search values for ${esc(k)}" autocomplete="off">
    <p class="muted tag-key-count">${shown.length} of ${rows.length} values · counts and costs before other filters.</p>
    <div class="tag-value-options" data-value-list="${esc(k)}">${shown.map(r => {
      const on = !!wanted?.has(r.value);
      const label = r.value === "" ? "(no value)" : r.value;
      return `<label class="tag-key-option${on ? " on" : ""}"
        title="${esc(k)}=${esc(label)} — ${esc(money(r.cost, cur))} across ${r.n} resource${r.n === 1 ? "" : "s"}">
        <input type="checkbox" data-vk="${esc(k)}" data-vv="${esc(r.value)}" ${on ? "checked" : ""}
          aria-label="${esc(label)}">
        <span class="tag-key-name">${esc(label)}</span>
        <span class="tag-key-meta">${r.n} resource${r.n === 1 ? "" : "s"} · ${esc(money(r.cost, cur))}</span>
      </label>`;
    }).join("") || `<p class="muted empty">${rows.length ? "No values match this search."
      : "No values recorded for this tag in this period."} Your selected values are unchanged.</p>`}</div>
  </div>`;
}

function wireTagValues() {
  const host = $("tagValueGroups");
  for (const input of host.querySelectorAll("[data-value-search]")) {
    input.oninput = () => {
      tagState.valueFilters.set(input.dataset.valueSearch, input.value);
      preserveTagFocus(paintTagValues);
    };
  }
  for (const b of host.querySelectorAll("[data-vk]")) {
    b.onclick = () => {
      const k = b.dataset.vk;
      const set = tagState.values.get(k) || new Set();
      if (set.has(b.dataset.vv)) set.delete(b.dataset.vv);
      else set.add(b.dataset.vv);
      if (set.size) tagState.values.set(k, set);
      else tagState.values.delete(k);
      paintTags();
    };
  }
  for (const b of host.querySelectorAll("[data-allvals]")) {
    b.onclick = () => {
      tagState.values.delete(b.dataset.allvals);
      paintTags();
    };
  }
}

  let tagBudgetRequest = 0, tagBudgetRefresh = 0, tagBudgetPendingPaint = false;
  let tagBudgetHost = null, tagBudgetSelection = "";

  async function loadTagBudgets(refresh = false) {
    const host = $("tagBudgets");
    if (!host) return;
    const selected = [...tagState.selected].map(key => ({
      key, values: [...(tagState.values.get(key) || [])],
    }));
    const paused = tagFiltersPaused(tagState.resourceView);
    const signature = JSON.stringify([selected, tagState.mode, paused]);
    if (!refresh && tagBudgetHost === host && tagBudgetSelection === signature) return;
    tagBudgetHost = host;
    tagBudgetSelection = signature;
    const request = ++tagBudgetRequest;
    const token = ctx.loadToken;
    if (!selected.length || paused) {
      host.hidden = true;
      host.innerHTML = "";
      tagBudgetPendingPaint = false;
      return;
    }
    const mode = tagState.mode;
    const current = () => request === tagBudgetRequest && token === ctx.loadToken
      && host === $("tagBudgets") && host.isConnected;
    tagBudgetPendingPaint = true;
    host.hidden = false;
    host.innerHTML = '<p class="muted tag-budget-loading" role="status">Looking for matching budgets...</p>';

    // Budgets keep their own periods and full scope; dashboard day/service filters must not
    // be applied to the chart as if it measured the same money as the selected resources.
    const source = new URLSearchParams(qs());
    const query = new URLSearchParams();
    for (const field of ["scope", "currency"]) if (source.has(field)) query.set(field, source.get(field));
    if (refresh) tagBudgetRefresh++;
    if (refresh) query.set("refresh", "true");
    let data;
    try {
      data = await cached(key(`tag-budgets|${tagBudgetRefresh}`), () =>
        get(`/api/budgets${query.size ? `?${query}` : ""}`, { timeout: 45000 }));
    } catch (error) {
      data = { budgets: [], error: error?.message || "Could not read budgets." };
    }
    if (!current()) return;
    tagBudgetPendingPaint = false;
    const demoMode = data?.demo_mode === true;
    const issue = demoMode ? "" : budgetReadIssue(data);
    const rows = Array.isArray(data?.budgets) ? data.budgets : [];
    const matches = matchingTagBudgets(rows, selected, mode);
    const unavailable = rows.some(b => b?.filter_exact === false || b?.filtered_exactly === false);
    host.innerHTML = `<section class="card tag-matched-budgets">
      <div class="tagpick-head"><h3>${demoMode ? "Budgets not available in demo mode"
        : `Related budgets${matches.length ? ` · ${matches.length}` : ""}`}</h3>
        ${demoMode ? "" : '<button type="button" class="linkish" id="tagBudgetRefresh">Refresh budgets</button>'}</div>
      ${demoMode ? `<p class="muted note">${esc(data.note
        || "Budgets are not part of the PostgreSQL demo data; no Azure budget was requested.")}</p>` : ""}
      ${issue ? `<p class="banner warn" role="status">${esc(issue)} Results may be incomplete.</p>` : ""}
      ${demoMode ? "" : matches.length ? `<p class="muted note">Budgets use their own period, subscription and full tag filter.
        Their figures can differ from the resource selection; overlapping budgets are not added together.</p>
        <div class="budget-list">${matches.map((match, index) =>
          budgetCard(match.budget, index, { snapshotChart: true, chartAside: true })).join("")}</div>`
        : `<p class="muted note">${issue ? "Matching budgets could not be confirmed."
          : "No budgets match the selected tags and values."}</p>`}
      ${!demoMode && unavailable ? '<p class="muted note">Some budget filters cannot be matched locally. Open Budgets to review their scope.</p>' : ""}
    </section>`;
    const refreshButton = $("tagBudgetRefresh");
    if (refreshButton) refreshButton.onclick = () => loadTagBudgets(true);
    for (const chart of demoMode ? [] : host.querySelectorAll("[data-chart]")) {
      const budget = matches[Number(chart.dataset.chart)]?.budget;
      if (budget) whenNear(chart, async () => async () => {
        await loadBudgetTrendChart(chart, budget, {
          get, currency: ctx.readStore?.("costCurrency", "") || "", drawBudgetChart, esc,
          options: { snapshot: true, compact: true },
          isCurrent: () => chart.isConnected,
        });
      });
    }
    for (const button of host.querySelectorAll("[data-tagsfor]")) {
      const budget = matches[Number(button.dataset.tagsfor)]?.budget;
      button.onclick = () => {
        if (budget) ctx.showTagsFor(budget.tag_filter, budget.name);
      };
    }
  }

  function resumeSpendView(token, view) {
    if (view === "overview" && overviewAnomalyPending) loadOverviewAnomalies(token, overviewAnomalyPending.currency);
    if (view === "tags" && (budgetCompletionPending || subBudgetsPending)) {
      budgetCompletionPending = false;
      subBudgetsPending = false;
      paintBudget();
    }
    if (view === "tags" && tagBudgetPendingPaint) {
      tagBudgetSelection = "";
      loadTagBudgets();
    }
  }

// ------------------------------------------------------- budget from a selection

/**
 * Turn the tag selection into an Azure budget.
 *
 * The tab already answers "what does this combination cost". A budget is the standing version
 * of the same question, and until now the only way to get one was to retype the selection into
 * the portal's Create budget form — where the tag filter has to be rebuilt from memory, which
 * is exactly where it stops matching the number that prompted it.
 *
 * Three things do not survive the translation, and all three are shown rather than smoothed
 * over, because each one silently changes what gets tracked:
 *
 *   * a budget filter is a conjunction, so "Has any" across several tags has no equivalent;
 *   * a tag filter lists values explicitly, so a key selected whole is frozen to the values
 *     that exist today;
 *   * a budget belongs to one subscription, so a selection spanning several is not one budget.
 */
function budgetSelection() {
  const d = tagState.data;
  const picked = [...tagState.selected];
  const all = tagState.mode === "all";

  const matched = picked.length
    ? d.resources.filter((r) =>
        all ? picked.every((k) => matchesKey(r, k)) : picked.some((k) => matchesKey(r, k)))
    : [];

  // Values per key: the ones narrowed to, or every value that key carries. Enumerated from all
  // resources holding the key rather than from `matched`, so the filter describes the same set
  // the tab does instead of a snapshot narrowed by the other keys.
  const tags = picked.map((k) => {
    const chosen = tagState.values.get(k);
    if (chosen && chosen.size) return { key: k, values: [...chosen], whole: false };
    const seen = new Set();
    for (const r of d.resources) {
      const v = tagValue(r, k);
      if (v !== null) seen.add(v);
    }
    return { key: k, values: [...seen], whole: true };
  });

  const subs = allSubscriptions();
  const byId = new Map();
  for (const r of matched) {
    const resourceId = r.subscription_id || /^\/subscriptions\/([^/]+)\//i.exec(r.id || "")?.[1];
    const sub = resourceId && subs.find(s => s.id.toLowerCase() === resourceId.toLowerCase());
    const id = sub?.id || "";
    const group = byId.get(id) || {id, name: sub?.name || r.subscription || "(unknown)", cost: 0};
    group.cost += r.cost;
    byId.set(id, group);
  }
  const spread = [...byId.values()].sort((a, b) => b.cost - a.cost);

  const days = d.dates?.length || d.days || 30;
  const sum = matched.reduce((t, r) => t + r.cost, 0);

  return { picked, tags, matched, sum, spread, subs, days, mode: tagState.mode, cur: d.currency,
    nativeCurrencies: d.native_currencies || {} };
}

/** A budget name that Azure will accept, derived from what was selected. */
function budgetName(sel) {
  const first = sel.tags[0];
  const value = first && !first.whole && first.values.length === 1 ? first.values[0] : "";
  const raw = [first ? first.key : "tags", value].filter(Boolean).join("-");
  const clean = raw.replace(/[^A-Za-z0-9_.-]/g, "-").replace(/-+/g, "-").slice(0, 40);
  return `${/^[A-Za-z0-9]/.test(clean) ? clean : `budget-${clean}`}-budget`.slice(0, 63);
}

function paintBudget() {
  const host = $("tagBudget");
  if (!host) return;

  const btn = $("tagBudgetBtn");
  if (tagFiltersPaused(tagState.resourceView)) {
    if (btn) btn.hidden = true;
    host.innerHTML = "";
    return;
  }

  // A budget that was just written is the one thing on this tab the tab cannot recompute, so
  // it is rendered rather than announced. A toast is gone in six seconds and takes the amount,
  // the reset period and the filter with it; what was actually created then exists only in the
  // portal, which is the round trip this form was built to avoid.
  const made = tagState.budget?.created;
  if (made) {
    if (btn) {
      btn.hidden = tagState.selected.size === 0;
      btn.textContent = "Create budget";
    }
    host.innerHTML = budgetResultMarkup(made);
    wireBudgetResult(made);
    return;
  }

  const open = tagState.budget?.open && tagState.selected.size > 0;
  if (btn) {
    btn.hidden = tagState.selected.size === 0;
    btn.textContent = open ? "Cancel" : "Create budget";
  }
  if (!open) {
    host.innerHTML = "";
    return;
  }

  const sel = budgetSelection();
  const cur = sel.cur;

  // Azure has no OR in a budget filter. Say so and stop, rather than offering a form whose
  // result would track a different set of resources than the figure above it.
  if (sel.mode === "any" && sel.picked.length > 1) {
    host.innerHTML = `<section class="card budgetform">
      <h3>New tag budget</h3>
      <div class="banner warn">An Azure budget can only combine tags with <strong>AND</strong>.
        “Has any” across ${sel.picked.length} tags cannot be expressed as one budget — switch to
        <strong>Has all</strong>, or select a single tag and create one budget per tag.</div>
    </section>`;
    return;
  }

  const spread = sel.spread.filter((s) => s.id);
  const missing = sel.spread.filter((s) => !s.id);
  const frozen = sel.tags.filter((t) => t.whole);
  const empty = sel.tags.filter((t) => !t.values.length);
  // Subscriptions that carry some of this selection first, then the rest. A combination with no
  // spend today is still a legitimate thing to budget for, so the picker never ends up empty
  // just because nothing matched.
  const others = sel.subs.filter((s) => s.id && !spread.some((p) => p.id === s.id));
  const canPick = spread.length + others.length > 0;

  const start = new Date();
  const startText = new Date(start.getFullYear(), start.getMonth(), 1)
    .toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  // Formatted from the local parts, not via toISOString: east of UTC, midnight local is the
  // previous day in UTC, and the default expiry silently became the last of the month.
  const endDefault = isoDay(new Date(start.getFullYear() + 2, start.getMonth(), 1));

  const filterText = sel.tags
    .map((t) => `${t.key} = ${t.values.length > 3
      ? `${t.values.slice(0, 3).map(labelOf).join(", ")} +${t.values.length - 3}`
      : t.values.map(labelOf).join(", ")}`)
    .join("  AND  ");

  host.innerHTML = `<section class="card budgetform">
    <h3>New tag budget</h3>

    ${empty.length ? `<div class="banner warn">No values in this period for
      ${empty.map((t) => `<strong>${esc(t.key)}</strong>`).join(", ")}. An Azure budget matches
      tag values explicitly, so there is nothing to filter on yet.</div>` : ""}

    ${!sel.matched.length && !empty.length ? `<div class="banner warn">Nothing in this period
      carries ${sel.picked.length > 1 ? "all of these tags together" : "this tag"}, so the budget
      would start out tracking no spend. That is fine for something being set up ahead of time —
      choose the subscription it should watch.</div>` : ""}

    ${sel.matched.length && !spread.length ? `<div class="banner warn">The matched resources sit
      in ${missing.length ? esc(missing.map((m) => m.name).join(", ")) : "a subscription"} which
      this page cannot match to one you can pick, so choose the target subscription yourself.</div>` : ""}

    ${!canPick ? `<div class="banner warn">No subscription is available to create a budget
      on.</div>` : ""}

    <p class="muted note">Creates a real Azure budget
      (<code>Microsoft.Consumption/budgets</code>) filtered to these tags. It needs Cost Management
      Contributor on the subscription — held by your own account when delegated Azure access is
      enabled, and otherwise by the app's managed identity.</p>

    <div class="budget-filter">
      <span class="k">Filter</span>
      <span class="v">${esc(filterText || "—")}</span>
    </div>

    ${frozen.length ? `<p class="muted note">${frozen.map((t) => `<strong>${esc(t.key)}</strong>`).join(", ")}
      ${frozen.length === 1 ? "was" : "were"} selected without narrowing, so
      ${frozen.length === 1 ? "its" : "their"} current
      ${frozen.reduce((n, t) => n + t.values.length, 0)} value(s) are listed in the filter. A
      value created later is not covered until you update the budget.</p>` : ""}

    <div class="budget-grid">
      <label class="bfield"><span>Subscription</span>
        <select id="bSub" ${canPick ? "" : "disabled"}>
          <option value="">Choose a subscription</option>
          ${spread.map((s, i) => `<option value="${esc(s.id)}" ${i === 0 ? "selected" : ""}>
            ${esc(s.name)} — ${esc(money(s.cost, cur))} of the selection</option>`).join("")}
          ${others.map((s) => `<option value="${esc(s.id)}">${esc(s.name || s.id)}</option>`).join("")}
        </select>
      </label>

      <label class="bfield"><span>Budget name</span>
        <input id="bName" type="text" maxlength="63" value="${esc(budgetName(sel))}"
               autocomplete="off" spellcheck="false">
      </label>

      <label class="bfield"><span>Reset period</span>
        <select id="bGrain">
          <option value="Monthly" selected>Monthly</option>
          <option value="Quarterly">Quarterly</option>
          <option value="Annually">Annually</option>
        </select>
      </label>

      <label class="bfield"><span id="bAmountLabel">Amount (native billing currency)</span>
        <input id="bAmount" type="number" min="1" step="1" value="">
      </label>

      <label class="bfield"><span>Alert at (% of budget)</span>
        <input id="bThresholds" type="text" value="80, 100" autocomplete="off">
      </label>

      <label class="bfield"><span>Notify</span>
        <input id="bEmails" type="text" placeholder="you@example.com"
               autocomplete="off" spellcheck="false">
      </label>

      <label class="bfield"><span>Expires</span>
        <input id="bEnd" type="date" value="${esc(endDefault)}">
      </label>

      <div class="bfield static"><span>Starts</span><div class="bstatic">${esc(startText)}</div></div>
    </div>

    <p class="muted note" id="bSuggest"></p>

    <div class="build-bar">
      <button class="primary" id="bCreate" ${canPick && !empty.length ? "" : "disabled"}>
        Create budget</button>
      <span class="muted" id="bNote"></span>
    </div>
  </section>`;

  $("bSub").value = spread[0]?.id || "";
  wireBudget(sel);
}

// ================================================================ shutdown savings

/**
 * Machines billed for 168 hours a week that are wanted for about 60.
 *
 * The honest framing matters more here than anywhere else in the app, because this is the one
 * tab that proposes turning something off. Every figure is a ceiling, every exclusion is stated,
 * and anything tagged production never appears — a saving that causes an outage is not a saving.
 */

function budgetResultMarkup(b) {
  const cur = b.currency || tagState.data?.currency;
  const alerts = (b.thresholds || []).length
    ? `${b.thresholds.map((t) => `${t}%`).join(", ")} to ${esc((b.emails || []).join(", ") || "nobody")}`
    : "none — this budget tracks spend but will not notify anyone";

  return `<section class="card budgetform budgetdone">
    <h3>Budget created</h3>
    <div class="banner ok"><strong>${esc(b.name)}</strong> created in Azure.
      Spend remains ${esc(money(0, cur))} until matching tag usage records arrive.</div>

    <div class="budget-filter">
      <span class="k">Filter</span>
      <span class="v">${esc(b.filter || "—")}</span>
    </div>

    <div class="budget-grid">
      <div class="bfield static"><span>Amount</span>
        <div class="bstatic">${esc(money(b.amount, cur))} ${esc(String(b.time_grain || "").toLowerCase())}</div></div>
      <div class="bfield static"><span>Spent so far</span>
        <div class="bstatic">${esc(money(b.current_spend || 0, cur, 2))}</div></div>
      <div class="bfield static"><span>Subscription</span>
        <div class="bstatic">${esc(subName(b.subscription))}</div></div>
      <div class="bfield static"><span>Starts</span><div class="bstatic">${esc(b.start || "—")}</div></div>
      <div class="bfield static"><span>Expires</span><div class="bstatic">${esc(b.end || "—")}</div></div>
      <div class="bfield static"><span>Alerts</span><div class="bstatic">${alerts}</div></div>
    </div>

    ${b.id ? `<p class="muted note">Resource <code>${esc(b.id)}</code></p>` : ""}

    <div class="build-bar">
      <button class="primary" id="bAgain">Create another</button>
      <button class="ghost" id="bDone">Done</button>
      <button type="button" class="linkish" data-goto="budgets">See all budgets</button>
      <a class="linkish" href="${budgetPortalUrl(b.subscription)}" target="_blank"
         rel="noopener">Open in the Azure portal</a>
    </div>

    <div class="budgetlist" id="bExisting">
      <h4>Subscription budgets</h4>
      <p class="muted note">Loading…</p>
    </div>
  </section>`;
}

/** A subscription's name if the picker knows it, otherwise the id — never nothing. */

function wireBudgetResult(made) {
  const again = $("bAgain");
  if (again) {
    again.onclick = () => {
      tagState.budget = { open: true };
      paintBudget();
      $("tagBudget")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
  }
  const done = $("bDone");
  if (done) {
    done.onclick = () => {
      tagState.budget = { open: false };
      paintBudget();
    };
  }
  loadSubBudgets(made.subscription, made.name);
  wireBudgetLinks();
}

/**
 * Every budget Azure holds on that subscription, read back live.
 *
 * Read rather than assumed: the created panel above is what we sent, and this is what Azure
 * has. If a name collided, the upsert replaced a budget someone else was relying on, and the
 * only way that becomes visible is by listing them.
 *
 * Cached on the state, because every tag click repaints this panel and a fresh ARM round trip
 * per click would rate-limit the tab for a list that cannot have changed in between.
 */
let subBudgetsRequest = 0;
let subBudgetsPending = false;
async function loadSubBudgets(sub, highlight) {
  const host = $("bExisting");
  if (!host) return;
  const budget = tagState.budget;
  const token = ctx.loadToken;
  const request = ++subBudgetsRequest;
  const current = () => request === subBudgetsRequest && budget === tagState.budget
    && token === ctx.loadToken && host === $("bExisting");

  const cached = tagState.budget?.list;
  if (cached && cached.sub === sub) {
    subBudgetsPending = false;
    host.innerHTML = subBudgetsMarkup(cached.data, highlight);
    return;
  }
  subBudgetsPending = true;
  try {
    const data = await get(`/api/budgets?scope=${encodeURIComponent(sub)}`);
    if (request === subBudgetsRequest && budget === tagState.budget && budget?.created) budget.list = { sub, data };
    if (!current()) return;
    subBudgetsPending = false;
    host.innerHTML = subBudgetsMarkup(data, highlight);
  } catch (err) {
    if (!current()) return;
    subBudgetsPending = false;
    if (host) {
      host.innerHTML = `<h4>Subscription budgets</h4>
        <p class="muted note">Could not read them back: ${esc(err.message || String(err))}</p>`;
    }
  }
}

function subBudgetsMarkup(data, highlight) {
  if (data.demo_mode) {
    return `<h4>Subscription budgets</h4>
      <p class="muted note">${esc(data.note
        || "Budgets are not included in the PostgreSQL demo data; no Azure budget was requested.")}</p>`;
  }
  const rows = (data.budgets || []).filter((b) => b.name);
  if (!rows.length) {
    return `<h4>Subscription budgets</h4>
      <p class="muted note">${esc(data.error
        ? `Could not read them back: ${data.error}`
        : "Azure reports none yet — a budget can take a moment to appear in the list.")}</p>`;
  }
  // The same cards the Budgets tab draws, so an over-budget neighbour is as obvious here as it
  // is there — and so there is one place to change when the treatment changes.
  return `<h4>Subscription budgets</h4>
    <div class="budgets">${rows.map((b) => b.name === highlight
      ? budgetCard(b).replace('<article class="budget ', '<article class="budget is-new ')
      : budgetCard(b)).join("")}</div>`;
}

const labelOf = (v) => (v === "" ? "(no value)" : v);

/** `YYYY-MM-DD` from a date's local parts, which is what a date input expects. */
function isoDay(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function wireBudget(sel) {
  const grain = $("bGrain");
  const amount = $("bAmount");
  updateBudgetCurrency(sel);
  if (grain && amount) grain.onchange = () => {
    amount.value = "";
    updateBudgetCurrency(sel);
  };
  if ($("bSub")) $("bSub").onchange = () => {
    amount.value = "";
    updateBudgetCurrency(sel);
  };

  const create = $("bCreate");
  if (create) create.onclick = () => submitBudget(sel);

  whoAmI().then((me) => {
    const box = $("bEmails");
    if (box && !box.value && me?.user?.email) box.value = me.user.email;
  }).catch(() => {});
}

function budgetCurrency(sel, subscription) {
  const currencies = sel.nativeCurrencies[String(subscription || "").toLowerCase()];
  return Array.isArray(currencies) && currencies.length === 1 && /^[A-Z]{3}$/.test(currencies[0])
    ? currencies[0] : null;
}

function updateBudgetCurrency(sel) {
  const sub = $("bSub")?.value;
  const currency = budgetCurrency(sel, sub);
  const amount = $("bAmount");
  if (!amount) return;
  $("bAmountLabel").textContent = currency ? `Amount (${currency} · native billing currency)`
    : "Amount (native billing currency unavailable)";
  amount.disabled = !currency;
  $("bCreate").disabled = !currency || !sub || sel.tags.some(t => !t.values.length);
  $("bSuggest").textContent = !sub ? "Choose a subscription to determine its native billing currency."
    : !currency ? "Cannot create a budget: this subscription's native billing currency is unknown or mixed."
    : `Enter the amount in ${currency}, the subscription's native billing currency.${
      currency !== sel.cur ? ` Dashboard costs are displayed in ${sel.cur || "another currency"}; do not copy a converted amount.` : ""}`;
}

let budgetCompletionPending = false;
async function submitBudget(sel) {
  const btn = $("bCreate");
  const host = $("tagBudget");
  const budget = tagState.budget;
  const token = ctx.loadToken;
  const ownsForm = () => budget === tagState.budget
    && (host?.contains?.(btn) ?? btn === $("bCreate"));
  const current = () => ownsForm() && token === ctx.loadToken && host === $("tagBudget");
  const note = $("bNote");
  const sub = $("bSub")?.value;
  const name = $("bName")?.value.trim();
  const amount = Number($("bAmount")?.value);
  const grain = $("bGrain")?.value || "Monthly";
  const end = $("bEnd")?.value || "";
  const thresholds = ($("bThresholds")?.value || "")
    .split(/[,\s]+/).map((t) => Number(t)).filter((t) => t > 0);
  const emails = ($("bEmails")?.value || "").split(/[,;\s]+/).filter(Boolean);

  if (!sub) return toast("Choose a subscription for the budget.", "err");
  const currency = budgetCurrency(sel, sub);
  if (!currency) return toast("The subscription's native billing currency is unknown or mixed.", "err");
  if (!name) return toast("Give the budget a name.", "err");
  if (!(amount > 0)) return toast("Enter a budget amount greater than zero.", "err");
  if (thresholds.length && !emails.length) {
    return toast("An alert threshold needs an email address to notify.", "err");
  }

  btn.disabled = true;
  const before = btn.textContent;
  btn.textContent = "Creating…";
  if (note) note.textContent = "";

  try {
    const res = await fetch("/api/budgets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription: sub,
        name,
        amount,
        time_grain: grain,
        end,
        mode: sel.mode,
        tags: sel.tags.map((t) => ({ key: t.key, values: t.values })),
        thresholds,
        emails,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const why = typeof data.detail === "string" ? data.detail : `Azure refused it (${res.status}).`;
      if (note) note.textContent = why;
      toast(why, "err");
      return;
    }
    toast(`Budget “${data.name}” created in ${sub} — ${money(data.amount, currency)} ${
      String(data.time_grain || "").toLowerCase()}, alerting ${data.notifications} threshold(s).`);
    // Keep the panel open and show what Azure returned. Closing the form on success threw away
    // the only record of the budget the moment the toast expired.
    if (ownsForm()) {
      Object.assign(budget, { open: true, created: { ...data, currency } });
      budgetCompletionPending = true;
      if (current()) {
        budgetCompletionPending = false;
        paintBudget();
        loadTagBudgets(true);
        host?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
  } catch {
    const why = `Could not confirm creation of “${name}” in ${sub}. Check subscription budgets before retrying.`;
    if (note) note.textContent = why;
    toast(why, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = before;
  }
}

function preserveTagFocus(paint) {
  const active = document.activeElement;
  const lists = ["tagChips", "tagValueGroups"].map($).filter(Boolean);
  const scrollPositions = lists.map(node => [node, node.scrollTop]);
  const valueScroll = new Map([...($("tagValueGroups")?.querySelectorAll("[data-value-list]") || [])]
    .map(node => [node.dataset.valueList, node.scrollTop]));
  const attributes = ["data-tag", "data-drop", "data-vk", "data-vv", "data-value-search", "data-allvals"];
  const identity = attributes.filter(a => active?.hasAttribute?.(a)).map(a => [a, active.getAttribute(a)]);
  const id = active?.id;
  const start = active?.selectionStart;
  const end = active?.selectionEnd;
  paint();
  for (const [node, top] of scrollPositions) node.scrollTop = top;
  for (const node of $("tagValueGroups")?.querySelectorAll("[data-value-list]") || []) {
    if (valueScroll.has(node.dataset.valueList)) node.scrollTop = valueScroll.get(node.dataset.valueList);
  }
  if (!active || active.isConnected) return;
  const replacement = id ? $(id) : identity.length
    ? [...$("tabBody").querySelectorAll("button, input")].find(node =>
      identity.every(([name, value]) => node.getAttribute(name) === value))
    : null;
  // All values removes its own reset button; keep keyboard focus inside that value group.
  const resetKey = active.getAttribute?.("data-allvals");
  const valueSearch = resetKey != null
    ? [...($("tagValueGroups")?.querySelectorAll("[data-value-search]") || [])]
      .find(node => node.dataset.valueSearch === resetKey)
    : null;
  const target = replacement || valueSearch || (identity.length ? $("tagKeysToggle") : null);
  target?.focus({ preventScroll: true });
  if (target === replacement && start != null && target?.setSelectionRange) {
    target.setSelectionRange(start, end);
  }
}

/** Everything that depends on the selection. Pure arithmetic over data already in memory. */
function paintTags() {
  preserveTagFocus(paintTagsContent);
}

function paintTagsContent() {
  const d = tagState.data;
  const cur = d.currency;
  const picked = [...tagState.selected];
  const all = tagState.mode === "all";
  const paused = tagFiltersPaused(tagState.resourceView);

  for (const b of $("tabBody").querySelectorAll("[data-mode]")) {
    const on = b.dataset.mode === tagState.mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  }
  for (const b of $("tabBody").querySelectorAll("[data-resource-view]")) {
    const on = b.dataset.resourceView === tagState.resourceView;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  }

  paintTagKeys();
  paintTagValues();
  loadTagBudgets();
  const draft = new Map(["bSub", "bName", "bGrain", "bAmount", "bThresholds", "bEmails", "bEnd"]
    .filter(id => $(id)).map(id => [id, $(id).value]));
  paintBudget();
  for (const [id, value] of draft) {
    const input = $(id);
    if (input && (input.tagName !== "SELECT" || [...input.options].some(o => o.value === value))) input.value = value;
  }
  if ($("bAmount")) {
    if (draft.has("bSub") && draft.get("bSub") !== $("bSub")?.value) $("bAmount").value = "";
    updateBudgetCurrency(budgetSelection());
  }

  const narrowed = picked.filter((k) => (tagState.values.get(k) || new Set()).size);
  const narrowNote = narrowed.length
    ? ` Narrowed by value on ${narrowed.map((k) => `“${k}”`).join(", ")}.`
    : "";
  $("tagModeHint").textContent = (paused
    ? `${title(tagState.resourceView)}: key/value filters paused. Choose All or Tagged to apply.`
    : picked.length < 2
    ? "Select keys and values to filter."
    : all
      ? `Match ALL ${picked.length} selected keys (AND).`
      : `Match ANY of the ${picked.length} selected keys (OR).`) + narrowNote
      + " Values within one key always match ANY (OR).";

  // The selected line, kept separate from the picker so what is chosen is readable at a glance
  // rather than hidden among everything that was not.
  $("tagClear").hidden = picked.length === 0 && !tagState.filter && !tagState.valueFilters.size
    && tagState.resourceView === "all" && !tagState.day && tagState.mode === "all";
  $("tagUndo").hidden = !tagState.undo;
  $("tagSelected").innerHTML = picked.length
    ? picked
        .map((k) => {
          const wanted = tagState.values.get(k);
          const suffix = wanted && wanted.size
            ? ` = ${[...wanted].map((v) => (v === "" ? "(no value)" : v)).join(", ")}`
            : "";
          return `<button type="button" class="chip on removable" data-drop="${esc(k)}"
            aria-label="Remove ${esc(k)}"><span class="chip-k">${esc(k + suffix)}</span>
            <span class="chip-x" aria-hidden="true">×</span></button>`;
        })
        .join("")
    : `<p class="muted empty">No key filters — showing ${tagState.resourceView === "all"
      ? "all loaded" : tagState.resourceView} resources.</p>`;
  for (const b of $("tagSelected").querySelectorAll("[data-drop]")) {
    b.onclick = () => toggleTag(b.dataset.drop);
  }

  const matched = filterTagResources(d, tagState);
  const sum = matched.reduce((t, r) => t + r.cost, 0);

  $("tagSum").innerHTML = moneyHtml(sum, cur, 2);
  $("tagSumSub").textContent = `${matched.length.toLocaleString()} of ${(d.resources || []).length.toLocaleString()} loaded resources · ${
        d.total ? `${((sum / d.total) * 100).toFixed(1)}% of loaded spend` : "no net spend to measure"}${
        d.truncated ? " · partial data" : ""}`;

  // The trend covers the whole estate when nothing is picked, and the selection once there is
  // one. Drawn either way: a tab that shows no chart until you happen to click a tag hides the
  // one view most people came for.
  const filtered = picked.length > 0 || tagState.resourceView !== "all";
  paintTagTrend(filtered ? matched : null);
  paintTagDay(filtered ? matched : null);

  if (!matched.length) {
    $("tagResults").innerHTML = `<section class="card"><h3>Matching resources</h3>
      <p class="muted empty">No resource carries ${all ? "all" : "any"} of these tags${
        narrowed.length ? " with the values you have narrowed to" : ""}${
        all && picked.length > 1 ? " — try “Has any”." : "."}</p></section>`;
    if (paused || !picked.length) {
      $("tagResults").innerHTML = `<section class="card"><h3>Matching resources</h3>
        <p class="muted empty">${tagState.resourceView === "untagged" && d.tags_not_loaded
          ? "Untagged status cannot be confirmed from this load. Choose Unknown to explore resources with unavailable tags."
          : `No ${tagState.resourceView === "all" ? "" : tagState.resourceView + " "}resources in the loaded data.`}
          Switch resource view or clear filters to widen the results.</p></section>`;
    }
    return;
  }

  // What the money is actually going on, at two levels: the services, then the resources.
  const byService = new Map();
  for (const r of matched) {
    const k = r.service || "(unknown)";
    byService.set(k, (byService.get(k) || 0) + r.cost);
  }
  const services = [...byService.entries()]
    .map(([name, cost]) => ({ name, cost }))
    .sort((a, b) => b.cost - a.cost);

  $("tagResults").innerHTML =
    table(
      "By service",
      services,
      [
        { label: "Service", cell: (r) => esc(r.name) },
        { label: "Cost", num: true, value: (r) => r.cost,
          cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        { label: "Share", num: true, sort: false,
          cell: (r) => `${sum ? ((r.cost / sum) * 100).toFixed(1) : "0"}%` },
      ],
      null,
      { pageSize: 10, key: "tags-services" }
    ) +
    table(
      "Matching resources",
      matched.slice().sort((a, b) => b.cost - a.cost),
      [
        { label: "Resource", value: r => r.name, cell: r => portalLinkMarkup(r, ctx.scopeIds(), r.name) },
        { label: "Service", cell: (r) => esc(r.service || "—") },
        { label: "Resource group", cell: (r) => esc(r.group || "—") },
        { label: "Subscription", cell: (r) => esc(r.subscription || "—") },
        { label: "Tags", sort: false, cell: (r) => tagPills(r) },
        { label: "Cost", num: true, value: (r) => r.cost,
          cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      "Counted once · search and totals use the current resource view and tag filters.",
      { pageSize: 15, key: "tags-resources" }
    );

  wireTables($("tagResults"));
}

/** Daily totals for a set of resources, aligned to `dates`. Sparse pairs summed into a series. */
function tagSeries(resources) {
  const d = tagState.data;
  const out = new Array((d.dates || []).length).fill(0);
  for (const r of resources) {
    for (const [i, c] of r.d || []) out[i] += c;
  }
  return out;
}

/**
 * Daily spend across the window.
 *
 * A bar per day rather than a line: these are discrete daily charges, and a line drawn between
 * them implies a continuous rate that was never measured. Bars also give the drill-down an
 * obvious target — clicking a day is a normal thing to do to a bar and a strange thing to do
 * to a point on a line.
 */
function paintTagTrend(matched) {
  const d = tagState.data;
  const host = $("tagTrend");
  if (!host) return;
  host.innerHTML = "";

  const dates = d.dates || [];
  if (d.daily_dropped) {
    host.innerHTML = `<section class="card"><h3>Daily spend</h3>
      <p class="muted empty">Too many resource-days in this window to chart without
      truncating them. Narrow the subscription scope or shorten the period.</p></section>`;
    return;
  }
  if (!dates.length) return;

  const series = matched ? tagSeries(matched) : (d.daily_total || []);
  const label = matched ? "Matching resources" : "All spend";

  drawChart(host, {
    type: "bar",
    title: `Daily spend · last ${dates.length} day${dates.length === 1 ? "" : "s"}`,
    labels: dates,
    datasets: [{ label, data: series.map((v) => Math.round(v * 100) / 100) }],
    note: "Select a day for resource costs. "
        + (matched ? "Current resource view and tag filters." : "All spend; select tags to narrow."),
    onSelect: (i) => {
      // Clicking the day already open closes it, so the chart is a toggle rather than a
      // one-way trip that needs a separate control to undo.
      tagState.day = tagState.day === dates[i] ? null : dates[i];
      paintTagDay(matched);
      $("tagDay")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    },
  });
}

/**
 * What ran on one particular day.
 *
 * The window totals answer "what does this cost"; this answers "what did I spend on the 24th",
 * which is the question people actually ask when a number moves. Same data, no extra request —
 * the daily cells are already in memory, so picking a day is a filter, not a fetch.
 */
function paintTagDay(matched) {
  const d = tagState.data;
  const host = $("tagDay");
  if (!host) return;

  const dates = d.dates || [];
  const day = tagState.day;
  if (!day || !dates.length) {
    host.innerHTML = "";
    return;
  }

  const i = dates.indexOf(day);
  if (i < 0) {
    // The selected day is not in this window any more — a period or scope change can do that.
    tagState.day = null;
    host.innerHTML = "";
    return;
  }

  const cur = d.currency;
  const pool = matched || d.resources;
  const rows = [];
  for (const r of pool) {
    const hit = (r.d || []).find(([idx]) => idx === i);
    if (hit && hit[1]) rows.push({ ...r, dayCost: hit[1] });
  }
  rows.sort((a, b) => b.dayCost - a.dayCost);
  const total = rows.reduce((t, r) => t + r.dayCost, 0);

  const pretty = new Date(day + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });

  host.innerHTML = `
    <section class="card tagday">
      <div class="tagsel-head">
        <h3>${esc(pretty)}</h3>
        <button type="button" class="linkish" id="tagDayClose">Close</button>
      </div>
      <p class="muted tagpick-hint">
        ${moneyHtml(total, cur, 2)} across ${rows.length.toLocaleString()} resource${
          rows.length === 1 ? "" : "s"}${matched ? " in the current resource view and tag filters" : ""}.
      </p>
    </section>` +
    (rows.length
      ? table(
          "Daily resources",
          rows,
          [
            { label: "Resource", value: r => r.name, cell: r => portalLinkMarkup(r, ctx.scopeIds(), r.name) },
            { label: "Service", cell: (r) => esc(r.service || "—") },
            { label: "Resource group", cell: (r) => esc(r.group || "—") },
            { label: "Subscription", cell: (r) => esc(r.subscription || "—") },
            { label: "Tags", sort: false, cell: (r) => tagPills(r) },
            { label: "Cost", num: true, value: (r) => r.dayCost,
              cell: (r) => moneyHtml(r.dayCost, cur, 2),
              total: (rs) => moneyHtml(rs.reduce((n, r) => n + r.dayCost, 0), cur, 2) },
          ],
          null,
          { pageSize: 15, key: "Resources that billed this day|Resource|Service|Resource group|Subscription|Tags|Cost" }
        )
      : `<section class="card"><p class="muted empty">Nothing billed on this day${
          matched ? " for the current resource view and tag filters" : ""}.</p></section>`);

  const close = $("tagDayClose");
  if (close) {
    close.onclick = () => {
      tagState.day = null;
      paintTagDay(matched);
    };
  }
  wireTables(host);
}

/**
 * Being told about the next anomaly, rather than finding it on this page.
 *
 * The alert is Azure's, not ours: a Cost Management scheduled action, the same mechanism a
 * budget uses to mail its thresholds. Nothing here sends mail, so nothing here stops sending it
 * when the app is restarted, when it is redeployed, or when nobody has opened this page in a
 * month — which is the whole reason it is configured rather than implemented.
 *
 * The panel is built into the tab's markup rather than appended, because an appended card lands
 * outside the flex layout the tab body establishes and collapses to a one-pixel strip.
 */
/** The alert panel as HTML, or a card explaining why not. */

async function renderAksView(token) {
  let d;
  try {
    d = await cached(key("aks"), () =>
      get(`/api/dashboard/aks${qs()}`, { timeout: 60000 }));
  } catch (err) {
    if (token !== ctx.loadToken) return;
    // Named rather than blank. This view is reached by clicking a segment showing a real
    // figure, so an empty page here reads as the figure having been a lie.
    setContent($("tabBody"), `<p class="muted empty">Could not read the AKS breakdown.
      ${esc(err?.message || "")}</p>`);
    return;
  }
  if (token !== ctx.loadToken) return;

  const clusters = d.clusters || [];
  const discovery = d.discovery_status || "unavailable";
  const detected = Number.isFinite(d.detected_clusters) ? d.detected_clusters : null;
  const discoveryNote = d.discovery_note || "Cluster discovery status is unavailable; absence does not establish deletion.";
  const cur = d.currency || (ctx.sections.find((s) => s.id === "aks") || {}).currency;
  const total = d.total;
  const overview = ctx.sections.find((s) => s.id === "overview") || {};
  const estate = overview.cost;
  // Divided by the same total the Overview picker itself shows for "All spend", so the share
  // cannot disagree with the total it is a share of even though this view no longer draws the
  // picker to compare it against directly. Small shares are floored rather than rounded to
  // "0.0%", which reads as "AKS costs nothing" when it costs something.
  const pctOf = Number.isFinite(total) && Number.isFinite(estate) && estate
    && overview.currency === cur ? (total / estate) * 100 : null;
  const share = pctOf == null ? "—" : pctOf > 0 && pctOf < 0.1 ? "<0.1%" : `${pctOf.toFixed(1)}%`;

  if (!clusters.length) {
    setContent($("tabBody"), `<section class="card"><h3>AKS</h3>
      <p class="muted empty">${discovery === "ok"
        ? "No AKS cost records or detected clusters in the selected view."
        : "No AKS cost records are available. Cluster inventory could not be fully confirmed."}</p>
      <p class="muted note">${esc(discoveryNote)}</p></section>`);
    return;
  }

  const categories = Object.keys(d.categories_by_currency || {}).length
    ? Object.entries(d.categories_by_currency) : [[cur, d.by_category || {}]];
  const unmatched = d.unmatched_groups || clusters.filter(c => !c.live);
  const maxClusters = new Map();
  for (const cluster of clusters) {
    const code = cluster.currency || cur;
    maxClusters.set(code, Math.max(maxClusters.get(code) || 0, cluster.cost || 0));
  }
  // Decided before the markup rather than by deleting a node afterwards. Removing the chart from
  // a two-column grid leaves the surviving card at half width with dead space beside it, which
  // reads as something that failed to load.
  const series = Object.keys(d.trend_by_currency || {}).length
    ? Object.entries(d.trend_by_currency) : [[cur, d.trend || []]];
  const trends = series.map(([currency, rows]) => [currency, rows.filter(row => Number.isFinite(row.cost))])
    .filter(([currency, rows]) => /^[A-Z]{3}$/.test(currency || "") && rows.length > 1);
  const hasChart = trends.length > 0;
  const chartId = index => index ? `aksChart${index}` : "aksChart";

  const catRows = categories.map(([currency, cats]) => {
    const basis = d.total_by_currency?.[currency] ?? (currency === cur ? total : null);
    return Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([category, cost]) => `<tr>
      <td>${esc(category)}${categories.length > 1 ? ` · ${esc(currency)}` : ""}</td>
      <td class="num">${Number.isFinite(basis) && basis ? `${((cost / basis) * 100).toFixed(1)}%` : "—"}</td>
      <td class="num">${esc(nativeAmount(cost, currency, money))}</td>
    </tr>`).join("");
  }).join("");

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "AKS spend", value: d.billing_empty ? "Not loaded"
        : esc(currencyAmountList(d.total_by_currency, null, money) || nativeAmount(total, cur, money)),
        sub: periodText(d.days) },
      { label: "Share of the estate", value: d.billing_empty ? "—" : share, sub: "already counted in the families" },
      { label: "Clusters shown", value: String(clusters.length),
        sub: detected == null ? "detected clusters unavailable" : `${detected} detected clusters${discovery === "partial" ? " · partial inventory" : ""}` },
      { label: "Unmatched groups", value: String(unmatched.length),
        sub: discovery === "ok" ? "not found in searched inventory" : "inventory status unknown" },
    ])}

    <p class="muted note">${esc(d.note || "")} AKS overlaps service-family totals; do not add them together.</p>
    ${d.truncated ? `<p class="banner warn">Showing ${clusters.length} of ${esc(String(d.total_cluster_records))} cluster records.
      KPI totals cover all matching billing; table totals cover only the records shown.</p>` : ""}
    <p class="${discovery === "ok" ? "muted note" : "banner warn"}">${esc(discoveryNote)}</p>

    ${unmatched.length
      ? `<div class="banner warn"><strong>${unmatched.length} billing group${
           unmatched.length === 1 ? "" : "s"} without a detected cluster match.</strong>
           ${esc(d.unmatched_note || "No match does not prove deletion or ongoing charges.")}</div>`
      : ""}

    ${table(
      "Clusters",
      clusters,
      [
        { label: "Cluster", cell: (r) =>
            `${esc(r.cluster || "—")} ${bar(r.cost || 0, maxClusters.get(r.currency || cur) || 0)}` +
            (r.live ? "" : r.inventory_status === "not_found" ? ' <span class="pill">Not found</span>'
              : ' <span class="pill">Unknown</span>') +
            (r.certain ? "" : ' <span class="pill">name inferred</span>') +
            (r.node_resource_group
              ? `<span class="anom-sub">${esc(r.node_resource_group)}</span>` : "") },
        { label: "Region", cell: (r) => esc(r.region || "—") },
        { label: "Tier", cell: (r) => esc(r.tier || "—") },
        { label: "Version", cell: (r) => esc(r.kubernetes_version || "—") },
        { label: "Reported state", cell: r => esc(r.live ? r.power_state || "Unknown" : "Unknown") },
        { label: "Currency", cell: r => esc(r.currency || cur || "Unknown") },
        { label: "Resources", num: true, value: (r) => r.resources || 0,
          cell: (r) => String(r.resources || 0) },
        { label: "Cost", num: true, value: (r) => r.cost,
          cell: (r) => d.billing_empty ? "Not loaded" : esc(nativeAmount(r.cost, r.currency || cur, money)),
          total: (rows) => d.billing_empty ? "Not loaded" : esc(nativeRowTotal(rows, "cost", cur, money)) },
      ],
      null
    )}

    <div class="${hasChart ? "grid-2" : ""}">
      <section class="card">
        <h3>Spend by category</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Category</th><th class="num">Share</th>
              <th class="num">Cost</th></tr></thead>
            <tbody>${catRows || '<tr><td colspan="3" class="muted">—</td></tr>'}</tbody>
          </table>
        </div>
        <details class="view-help" data-state-key="aks-help">
          <summary>About this view</summary>
          <p>AKS node VMs also count under Compute &amp; Web; load balancers count under Networking.
            Categories guide review: node rightsizing, orphaned storage and network architecture.</p>
        </details>
      </section>
      ${hasChart ? `<div>${trends.map((_, index) => `<div id="${chartId(index)}" class="card chart-card"></div>`).join("")}</div>` : ""}
    </div>`);

  trends.forEach(([currency, trend], index) => {
    drawChart($(chartId(index)), {
      type: "area",
      title: `AKS daily spend · ${currency} — ${periodText(d.days)}`,
      labels: trend.map((t) => t.day),
      datasets: [{ label: "AKS", data: trend.map((t) => t.cost) }],
      currency,
      categorical: false,
    });
  });
}



  return { stop: closeTagDropdowns, resume: resumeSpendView, reveal, renderSection, renderOverview, renderOverviewVisuals, renderSpendHeatmap, renderSpendWaterfall, renderMonthOverMonth, showTagsFor, renderUptime, renderTags, renderAksView };
}
