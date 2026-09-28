/**
 * The tabbed dashboard: the answers people want before they think of a question.
 *
 * Importing app.js also boots the chat — the two share the subscription picker, the formatting
 * and the chart renderer, so there is one implementation of each rather than two that drift.
 *
 * The shell owns navigation, shared tables, scoped caching and interaction-preserving commits.
 * Tab families load on demand through an explicit context, never by importing this shell.
 * Savings joins four independent live scans and paints their progress without blocking edits.
 */
import { allSubscriptions, askAgent, drawChart, esc, get, money, moneyHtml, onScopeChange, post, refreshHeader, scopeIds, selectSubscriptions, setGlanceSpark, whoAmI } from "./app.js";
import { close as closeAnalysis, invalidate as invalidateAnalysis, isOpen as isAnalysisOpen, openAnalysis, setAnalysisContext, wireAnalysis } from "./analysis.js";
import { mountBot, setBotState } from "./bot.js";
import { railIcon } from "./icons.js";
import { activeFilterCount, applyFilter, clearAllFilters, filterHasValue, filterQuery, loadFilters, setFilterCurrency, wireFilters } from "./filters.js";
import { createViewCache, replaceContent, stableKey } from "./dashboard-state.js";
import { drawBudgetChart as drawBudgetTrend } from "./budget-chart.js";
import { beginNavigation, finishNavigation, measurePhase, navigationFeedback, reportReadiness } from "./dashboard-performance.js";
import { portalLinkMarkup, portalTargets, rowDetailsMarkup } from "./dashboard-explore.js";
import { periodParams, ROLLING_DAYS, validMonth, wireMonthCalendar, wireReportingPeriod } from "./reporting-period.js";
import { PRIMARY_SECTIONS, groupedNavigation, keyboardDestination, railGroupFor, sectionDestination, sectionFor, sectionViews } from "./dashboard-navigation.js";
import { outlierThreshold, tableRowKeys, tableRows as view } from "./dashboard-table-model.js";
import { createModuleLoader } from "./dashboard-module-loader.js";
import { wireTabSearch } from "./dashboard-search.js";
import { hasSectionSpend, sectionCostLabel } from "./dashboard-currencies.js";
import { createViewRevalidation } from "./dashboard-freshness.js";

const $ = (id) => document.getElementById(id);

// Always Overview on a fresh load — a dashboard opens on its headline the same way a site
// opens on its front page, regardless of which tab a previous visit happened to end on. Only
// the period and scope are remembered across a reload; which tab is showing is not.
let current = "overview";
let days = Number(readStore("costDays", 30));
if (!ROLLING_DAYS.includes(days)) days = 30;
let periodMonth = readStore("costPeriodMonth", "");
if (!validMonth(periodMonth)) periodMonth = "";
let periodControl;
let sections = [];
let tabSearch;
let loadToken = 0; // guards against a slow tab landing after a faster one
let renderSequence = 0;
let renderedKey = null;
let renderedAt = 0;
let renderedComplete = false;
let renderErrorToken = null;
let renderErrorMessage = null;
let activeTabs = null;
let activePerformance = null;
let arrivalFrame = null;
let navigationIntent = 0;
let tabRendering = Promise.resolve();
// Enough for every dashboard view in one reporting context, without timed eviction.
const views = createViewCache(40);
const familyInstances = new Map();
const TAB_MODULES = {
  spend: () => import("./dashboard-spend.js"),
  optimize: () => import("./dashboard-optimize.js"),
  monitor: () => import("./dashboard-monitor.js"),
  reports: () => import("./dashboard-reports.js"),
  updates: () => import("./dashboard-updates.js"),
  billing: () => import("./dashboard-billing.js"),
  resources: () => import("./dashboard-resources.js"),
  evidence: () => import("./dashboard-evidence.js"),
  ai: () => import("./dashboard-ai.js"),
};
const TAB_RENDERERS = {
  overview: ["spend", "renderOverview"], tags: ["spend", "renderTags"],
  resources: ["resources", "renderResources"], uptime: ["spend", "renderUptime"],
  aks: ["spend", "renderAksView"], savings: ["optimize", "renderSavings"],
  waste: ["optimize", "renderWaste"], rightsizing: ["optimize", "renderRightsizing"],
  shutdown: ["optimize", "renderShutdown"], advisor: ["optimize", "renderAdvisor"],
  commitments: ["optimize", "renderCommitments"], esu: ["optimize", "renderEsu"],
  governance: ["monitor", "renderGovernance"], health: ["monitor", "renderHealth"],
  audit: ["monitor", "renderAuditLog"], budgets: ["monitor", "renderBudgets"],
  anomalies: ["monitor", "renderAnomalies"], history: ["reports", "renderHistory"],
  report: ["reports", "renderReport"], settings: ["reports", "renderSettings"],
  "azure-updates": ["updates", "renderAzureUpdates"],
  billing: ["billing", "renderBilling"],
  changes: ["evidence", "renderChanges"],
  performance: ["evidence", "renderPerformance"],
  "ai-billing": ["ai", "renderAiBilling"],
};
const modules = createModuleLoader(TAB_MODULES);

function warmTabOnIntent(button, viewId) {
  let timer;
  const warm = () => {
    if (navigator.connection?.saveData || document.visibilityState === "hidden") return;
    const id = viewId();
    if (!id || id === current) return;
    const [family] = TAB_RENDERERS[id] || ["spend"];
    modules.warm(family);
  };
  button.onpointerenter = event => {
    if (event.pointerType === "touch") return;
    clearTimeout(timer);
    timer = setTimeout(() => { if (button.isConnected) warm(); }, 80);
  };
  button.onpointerleave = () => clearTimeout(timer);
  button.onfocus = () => { clearTimeout(timer); warm(); };
}

// Snapshot revalidation updates these promises without blanking populated views.
const CACHE_MAX = 40;
const cache = new Map();
let sectionSnapshot = null;
const snapshotViews = createViewRevalidation({
  request: get, cache, isCurrent: context => context === key(current),
  statusHost: () => $("viewFreshness"), onRefresh: async context => {
    if (!renderedComplete && tabRendering) await tabRendering;
    if (context === key(current)) {
      await loadTabs({ preserve: true });
      if (context === key(current) && !renderedComplete) throw new Error("View refresh did not complete.");
    }
  },
});
function watchSnapshot(promise, cacheKey, signature = key("")) {
  const context = key(current);
  return snapshotViews.watch(promise, context, loadToken,
    token => token === loadToken && context === key(current) && signature === key(""), cacheKey);
}

async function cached(key, fetcher) {
  const hit = cache.get(key);
  if (hit) {
    cache.delete(key);
    cache.set(key, hit);
    return hit.value;
  }
  // Share in-flight reads and make failures retryable.
  const pending = Promise.resolve().then(fetcher).catch(err => {
    if (cache.get(key)?.value === pending) cache.delete(key);
    throw err;
  });
  cache.set(key, { value: pending });
  while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
  return pending;
}

/** Explicit refresh or imported data invalidates every visited view. */
function clearCache() {
  cache.clear();
  views.clear();
  snapshotViews.clear();
}

function readStore(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return v ?? fallback;
  } catch {
    return fallback;
  }
}
function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private browsing — the choice just won't survive a reload */
  }
}

const qs = () => {
  const scope = scopeIds();
  const cur = readStore("costCurrency", "");
  const filters = filterQuery();
  return `?${periodParams(days, periodMonth)}` +
    (scope.length ? `&scope=${encodeURIComponent(scope.join(","))}` : "") +
    (cur ? `&currency=${encodeURIComponent(cur)}` : "") +
    (filters.length ? `&${filters.join("&")}` : "");
};

/** A view's cache and revalidation context includes every global financial filter. */
const key = (name) => `${name}|${days}|${periodMonth}|${scopeIds().join(",")}|${readStore("costCurrency", "")}`
  + `|${filterQuery().join("&")}`;

const sectionsData = (signal) => watchSnapshot(cached(key("sections"), () =>
  snapshotViews.read(`/api/dashboard/sections${qs()}`, { timeout: 30000, signal })), key("sections"));

function setContent(root, html, options) {
  const started = performance.now();
  replaceContent(root, html, options);
  measurePhase(activePerformance, "render", started);
  performance.measure(`cloudlens:${current}:render`, {
    start: started, end: performance.now(), detail: { target: root?.id || root?.className },
  });
}

async function loadFamily(family) {
  const signature = key("");
  const instanceKey = `${family}${signature}`;
  if (familyInstances.has(instanceKey)) return familyInstances.get(instanceKey);
  const query = qs();
  const pickedScope = [...scopeIds()];
  const period = days;
  const month = periodMonth;
  const currency = readStore("costCurrency", "");
  const record = activePerformance;
  const started = performance.now();
  const module = await modules.load(family);
  measurePhase(record, "module", started);
  if (familyInstances.has(instanceKey)) return familyInstances.get(instanceKey);
  const contextKey = name => `${name}${signature}`;
  const scopedGet = async (...args) => {
    const record = activePerformance;
    const started = performance.now();
    try { return await watchSnapshot(snapshotViews.read(...args), undefined, signature); }
    finally {
      measurePhase(record, "api", started);
      performance.measure(`cloudlens:${family}:api`, {
        start: started, end: performance.now(), detail: { url: args[0] },
      });
    }
  };
  const ctx = {
    $, activeFilterCount, allSubscriptions, applyFilter, bar, budgetCard, budgetPortalUrl,
    cached: (id, fetcher) => watchSnapshot(cached(id, fetcher), id, signature),
    cardHead, clearAllFilters, days: period, periodMonth: month, drawBudgetChart, drawChart, esc, fail, get: scopedGet,
    key: contextKey, kpiRow, money, moneyHtml, pct, portalLinkMarkup, portalTargets, post, qs: () => query,
    readStore: (name, fallback) => name === "costCurrency" ? currency : readStore(name, fallback),
    scopeIds: () => [...pickedScope],
    portalScopeIds: () => pickedScope.length ? [...pickedScope] : allSubscriptions().map(sub => sub.id),
    selectTab, navigate, setContent, setGlanceSpark, openAnalysis,
    showTagsFor, stableKey, subName, table, title, toast, trendClass, whenNear, whoAmI,
    wireBudgetLinks, wireDownloadLink, wireTables, writeStore,
    executiveData: signal => ctx.cached(contextKey("executive"), () =>
      scopedGet(`/api/dashboard/executive${query}`, { timeout: 30000, signal })),
    live: async (token, url, timeout = 180000) => {
      try {
        const data = await scopedGet(url, { timeout });
        return token === ctx.loadToken ? data : null;
      } catch (err) {
        if (token === ctx.loadToken) throw err;
        return null;
      }
    },
    get loadToken() { return key("") === signature ? loadToken : -1; },
    get sections() { return sections; },
    get tabsController() { return tabsController; },
    isCurrent: token => token === loadToken && key("") === signature && current === "savings",
  };
  const instance = module.createTabs(ctx);
  familyInstances.set(instanceKey, instance);
  while (familyInstances.size > 32) familyInstances.delete(familyInstances.keys().next().value);
  return instance;
}

async function showTagsFor(tags, name) {
  const signature = key("");
  const tabs = await loadFamily("spend");
  if (signature !== key("")) return;
  // A cross-tab request is intentionally a new selection, not the previous cached Tags DOM.
  views.clear();
  tabs.showTagsFor(tags, name);
}

const pct = (n) => (n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(1)}%`);

/** Rising cost is bad, falling is good — the opposite of most dashboards, so be explicit. */
const trendClass = (n) => (n == null ? "" : n > 1 ? "up" : n < -1 ? "down" : "flat");

/** The tools name findings in snake_case; a heading should read as English. */
const title = (s) =>
  String(s || "Finding")
    .replace(/_/g, " ")
    // Uppercase the acronym but not its plural: "IPs", not "IPS".
    .replace(/\b(vm|ip|nic|nsg|sql)(s?)\b/gi, (_, word, plural) => word.toUpperCase() + plural)
    .replace(/^./, (c) => c.toUpperCase());

// ------------------------------------------------------------------- tab bar
let tabsRequest = 0;
let tabsController = null;
async function loadTabs({ preserve = false } = {}) {
  snapshotViews.activate(key(current));
  document.documentElement.dataset.dashboardReady = "false";
  const request = ++tabsRequest;
  tabsController?.abort();
  tabsController = new AbortController();
  const controller = tabsController;
  const body = $("tabBody");
  if (renderedKey && renderedKey !== key(current)) {
    activeTabs?.stop?.();
    loadToken = ++renderSequence;
    renderedComplete = false;
    views.clear();
  }
  if (preserve && body.childElementCount) {
    body.classList.add("refreshing");
    body.setAttribute("aria-busy", "true");
  }
  // The header's download links carry the period and scope, so they have to be re-pointed
  // whenever either changes — before anyone can click them.
  refreshExportLinks();
  periodControl?.refresh();
  let data, me;
  try {
    me = await whoAmI();
    if (request !== tabsRequest || controller.signal.aborted) return;
    if (me?.required && !me.authenticated) return;
    // The canonical destinations do not depend on cost data. Keep account-gated navigation
    // usable while the optional cost-family picker and the selected view load independently.
    updateSections(sectionSnapshot?.signature === key("") ? sectionSnapshot.data : {}, me,
      { retainCurrent: true });
    setSectionsNotice("");
    const rendering = renderTab(preserve);
    data = await sectionsData(controller.signal);
    if (request !== tabsRequest || controller.signal.aborted) return;
    sectionSnapshot = { signature: key(""), data };
    const previous = current;
    updateSections(data, me);
    if (previous !== current) await renderTab();
    else {
      renderViewActions();
      await rendering;
    }
  } catch (err) {
    if (request !== tabsRequest || controller.signal.aborted) return;
    if (!me) {
      fail(err?.message || "Could not verify your account.");
      reportReadiness(false, err?.message);
      return;
    }
    setSectionsNotice(err.forbidden ? err.message
      : `Cost breakdown navigation could not load. ${err?.message || "Please retry."}`);
  }
}

function setSectionsNotice(message) {
  let notice = $("sectionsNotice");
  if (!notice) {
    notice = document.createElement("div");
    notice.id = "sectionsNotice";
    $("tabBody").before(notice);
  }
  notice.hidden = !message;
  notice.innerHTML = message ? `<div class="banner err" role="alert">${esc(message)}
    <button type="button" class="ghost" id="sectionsRetry">Retry dashboard</button></div>` : "";
  if ($("sectionsRetry")) $("sectionsRetry").onclick = () => {
    clearCache();
    void loadTabs({ preserve: true });
  };
}

function updateSections(data, me, { retainCurrent = false } = {}) {
  const previous = sections.find(section => section.id === current);
  sections = [
    { id: "overview", label: "Overview", cost: data.total, currency: data.currency,
      cost_by_currency: data.total_by_currency },
    { id: "ai-billing", label: "AI Billing", area: true, overlay: true },
    { id: "billing", label: "Billing", analysis: true },
    // Families live in the Overview picker; retain native-currency buckets and deep links.
    ...(data.sections || []).filter(hasSectionSpend).map((s) => ({ ...s, area: true })),
    // Overlays overlap families and must never be added to their totals.
    ...(hasSectionSpend(data.aks)
      ? [{ ...data.aks, id: "aks", label: "Kubernetes / AKS", area: true, overlay: true }]
      : []),
    ...(hasSectionSpend(data.marketplace)
      ? [{ ...data.marketplace, id: "marketplace", label: "Marketplace & ISV", area: true, overlay: true }]
      : []),
    { id: "uptime", label: "Hourly Billing", analysis: true },
    { id: "tags", label: "Cost by Application / Tags", analysis: true },
    { id: "resources", label: "All resources", analysis: true },
    { id: "anomalies", label: "Anomalies", analysis: true },
    { id: "history", label: "History", analysis: true },
    { id: "budgets", label: "Budgets", live: true },
    { id: "azure-updates", label: "Azure Updates", live: true },
    { id: "changes", label: "Change Analysis", live: true },
    { id: "performance", label: "Performance vs Spend", live: true },
    { id: "savings", label: "Savings", live: true },
    // Purchase commitments stay separate from reversible operational savings.
    { id: "commitments", label: "Reservation & Savings", live: true },
    { id: "governance", label: "Governance", live: true },
    { id: "esu", label: "End of support", live: true },
    { id: "health", label: "Retirements", live: true },
    // Admin-only, and left off the rail entirely for anyone else rather than shown and then
    // refused: the endpoint already enforces this server-side (require_admin), so hiding it
    // here is about not offering a door that opens onto a 403, not the actual access control.
    ...(me?.user?.admin ? [{ id: "audit", label: "Audit log", live: true }] : []),
    { id: "report", label: "Export data", build: true },
    { id: "settings", label: "Cost data", build: true },
  ];
  // A cost family already open can finish its own request before its new menu values arrive.
  if (retainCurrent && previous && !sections.some(section => section.id === current)) sections.push(previous);

  tabSearch?.update(sections, me?.user);

  // A tab that has gone. Someone whose last visit left them on Rightsizing should land on the
  // list that now contains it, not be silently dropped back to Overview.
  if (MERGED_TABS[current]) current = MERGED_TABS[current];
  if (!sections.some((s) => s.id === current)) current = "overview";

  // The button doubles as the freshness indicator: how current the data is, is exactly what
  // tells you whether you need to press it. Remembered rather than applied immediately —
  // the control appears only once the admin check returns, which can land after this.
  lastAsOf = data.as_of || lastAsOf;
  applyFreshness();

  renderRail();
  wireRail();

}

// --------------------------------------------------------------- the rail
/**
 * Tabs that were folded into another one, and where they went.
 *
 * Five separate entries under Optimize became one. They are kept here rather than deleted so a
 * bookmark, a deep link from a finding, or a hash someone pasted into a ticket still lands
 * somewhere useful instead of falling back to Overview — and so the AI analysis, which names a
 * tab for each finding it reports, does not have to know the rail was rearranged.
 *
 * `commitments` is deliberately not among them any more: it is a purchase, not a cleanup, and
 * it has its own tab again. `aks` is not here either — it is a segment in the area picker
 * inside Overview, so the id is a real destination.
 */
const MERGED_TABS = {
  waste: "savings",
  rightsizing: "savings",
  shutdown: "savings",
  rates: "savings",
  advisor: "savings",
};

const lastSectionView = new Map();
let groupedRail = readStore("railLayout", "sections") === "grouped";
const storedRailGroups = readStore("railGroupedOpen", ["spend"]);
const expandedRailGroups = new Set(Array.isArray(storedRailGroups) ? storedRailGroups : ["spend"]);
const narrowNavigation = matchMedia("(max-width: 1100px)");
const usesGroupedRail = () => groupedRail && !narrowNavigation.matches;
narrowNavigation.addEventListener("change", () => {
  renderRail();
  wireRail();
  renderViewActions();
});

/**
 * The area picker that replaced seven rail entries.
 *
 * Compute & Web, Networking, Storage, AI & Data, Integration & IoT, Security & Ops and Other
 * services were seven buttons that all reached `renderSection` with a different id — the same
 * view of a different slice. On the rail they read as seven places to go; as a row of segments
 * above the numbers they read as what they are, which is one view with a filter on it.
 *
 * Overview is the first segment rather than a separate destination, because Overview *is* the
 * unfiltered case: the sum of the families listed beside it.
 *
 * Kubernetes sits second, directly after All spend and before the families. It is the one
 * segment that overlaps the others — an AKS bill is node VMs already counted under Compute &
 * Web and load balancers already counted under Networking — so it is separated by a rule and
 * marked, and nothing ever totals the strip. It is placed there rather than at the end because
 * it answers a question about the whole estate, not about one family of services.
 */
function areaPicker(currentId) {
  const areas = sections.filter((s) => s.area && !s.overlay);
  const overlays = sections.filter((s) => s.area && s.overlay);
  if (!areas.length && !overlays.length) return "";
  const seg = (id, label, cost, cur, cls = "", buckets = []) => `<button class="ar-seg ${cls}"
      data-area="${esc(id)}" aria-pressed="${currentId === id}" type="button">
      <span class="ar-name">${esc(label)}</span>
      ${cost == null && !buckets?.length ? "" : `<span class="ar-cost">${
        esc(sectionCostLabel({ cost, currency: cur, cost_by_currency: buckets }, money))}</span>`}
    </button>`;
  const all = sections.find((s) => s.id === "overview");
  return `<div class="ar-picker subsection-nav" role="group" aria-label="Overview sections">
      ${seg("overview", "All spend", all?.cost, all?.currency, "", all?.cost_by_currency)}
      ${overlays.map((a) => seg(a.id, a.label, a.cost, a.currency, "ar-overlay", a.cost_by_currency)).join("")}
      ${areas.map((a) => seg(a.id, a.label, a.cost, a.currency, "", a.cost_by_currency)).join("")}
    </div>`;
}

/** The picker lives in the persistent header, outside the loading/cached view body. */
function wireAreaPicker() {
  for (const b of $("viewActions").querySelectorAll(".ar-seg")) {
    warmTabOnIntent(b, () => b.dataset.area);
    b.onclick = () => {
      const id = b.dataset.area;
      selectTab(id);
      $("viewActions").querySelector(`[data-area="${id}"]`)?.focus({ preventScroll: true });
    };
  }
}

function renderViewActions() {
  const host = $("viewActions");
  if (!host) return;
  const actions = $("statusActions");
  const primary = sectionFor(current, sections);
  const siblings = sectionViews(primary?.id, sections);
  const label = usesGroupedRail()
    ? current === "ai-billing" ? "AI Billing" : title(current) : primary?.label || title(current);
  const subscriptions = allSubscriptions();
  const picked = scopeIds();
  const destinations = (picked.length ? picked : subscriptions.map(sub => sub.id)).flatMap(id => {
    const target = portalTargets({ subscription_id: id })[0];
    if (!target) return [];
    const name = subscriptions.find(sub => sub.id.toLowerCase() === id.toLowerCase())?.name || id;
    return [{ name, url: current === "budgets" ? budgetPortalUrl(id) : target.url }];
  });
  const related = {
    overview: "resources", resources: "tags", savings: "commitments",
    commitments: "savings", anomalies: "history", governance: "esu",
    esu: "governance", history: "overview", report: "settings", settings: "report",
    "azure-updates": "health", health: "azure-updates",
    billing: "savings",
    changes: "performance", performance: "changes",
  }[current];
  const relatedSection = sectionFor(related, sections)?.id !== primary?.id
    ? sections.find(section => section.id === related) : null;
  const subsections = !usesGroupedRail() && primary?.id !== "overview" && siblings.length > 1 ? siblings : [];
  actions.innerHTML = `${relatedSection ? `<button type="button" class="ghost" data-related-tab="${esc(related)}">${esc(relatedSection.label)}</button>` : ""}
    ${destinations.length ? `<details class="view-portal">
      <summary>Open ${current === "budgets" ? "budgets" : "subscription"} in Azure</summary>
      <div class="view-portal-menu">
      ${destinations.map(item => `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.name)}</a>`).join("")}
      <p class="muted">Opens in a new tab. CloudLens date and dimension filters are not transferred.</p>
      </div></details>` : ""}
    <button type="button" class="ghost" id="refreshViews"
      title="Reload this view; no billing import.">Refresh views</button>`.trim();
  host.innerHTML = `<div class="view-title"><h2>${esc(label)}</h2></div>
      ${subsections.length ? `<div class="subsection-nav" role="tablist" aria-label="${esc(label)} sections" aria-orientation="horizontal">
        ${subsections.map(section => `<button type="button" role="tab" class="subsection-tab${section.id === current ? " on" : ""}"
          id="subsection-${esc(section.id)}" data-subsection="${esc(section.id)}"
          aria-controls="tabBody" aria-selected="${section.id === current}" tabindex="${section.id === current ? 0 : -1}"
          >${esc(section.label)}</button>`).join("")}
      </div>` : primary?.id === "overview" ? areaPicker(current) : ""}`;
  wireAreaPicker();
  const panel = $("tabBody");
  panel?.setAttribute("role", usesGroupedRail() ? "region" : "tabpanel");
  panel?.setAttribute("aria-label", sections.find(section => section.id === current)?.label || label);
  panel?.setAttribute("aria-labelledby", usesGroupedRail()
    ? `rail-view-${sections.find(view => view.id === current)?.area ? "overview" : current}`
    : subsections.length ? `subsection-${current}` : `primary-${primary?.id || "overview"}`);
  for (const button of host.querySelectorAll("[data-subsection]")) {
    warmTabOnIntent(button, () => button.dataset.subsection);
    button.onclick = () => {
      const id = button.dataset.subsection;
      selectTab(id);
      host.querySelector(`[data-subsection="${id}"]`)?.focus({ preventScroll: true });
    };
  }
  actions.querySelector("[data-related-tab]")?.addEventListener("click", event =>
    selectTab(event.currentTarget.dataset.relatedTab));
  actions.querySelector("#refreshViews").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    activeTabs?.stop?.();
    loadToken = ++renderSequence;
    renderedComplete = false;
    clearCache();
    invalidateAnalysis();
    let retry;
    try {
      if (current === "ai-billing") retry = (await loadFamily("ai")).refreshOnce();
      await loadTabs({ preserve: true });
    } finally { retry?.(); button.disabled = false; }
  };
  host.onkeydown = event => {
    const tab = event.target.closest("[data-subsection], [data-area]");
    if (tab) {
      const attribute = tab.hasAttribute("data-area") ? "data-area" : "data-subsection";
      const ids = [...host.querySelectorAll(`[${attribute}]`)].map(item => item.getAttribute(attribute));
      const destination = keyboardDestination(ids, tab.getAttribute(attribute), event.key);
      if (!destination) return;
      event.preventDefault();
      event.stopPropagation();
      selectTab(destination);
      const selected = host.querySelector(`[${attribute}="${destination}"]`);
      selected?.focus({ preventScroll: true });
      selected?.scrollIntoView({ inline: "nearest", block: "nearest" });
      return;
    }
  };
  actions.onkeydown = event => {
    const menu = actions.querySelector(".view-portal");
    if (event.key === "Escape" && menu?.open) {
      menu.open = false;
      menu.querySelector("summary").focus();
    }
  };
}

// The pre-paint script sets this on <html> so the rail never renders wide and snaps narrow.
// Mirror it onto <body>, which is what the rest of the code and the stylesheet key off.
if (document.documentElement.classList.contains("rail-mini")) {
  document.body.classList.add("rail-mini");
}

function selectPrimary(id) {
  const destination = sectionDestination(id, sections, lastSectionView.get(id));
  if (!destination) return;
  selectTab(destination);
  $("tabs").querySelector(`[data-main-section="${id}"]`)?.focus({ preventScroll: true });
}

function wireRail() {
  const rail = $("tabs");
  for (const btn of rail.querySelectorAll("[data-main-section]")) {
    warmTabOnIntent(btn, () => sectionDestination(btn.dataset.mainSection, sections,
      lastSectionView.get(btn.dataset.mainSection)));
    btn.onclick = () => selectPrimary(btn.dataset.mainSection);
  }
  const mini = $("railMini");
  if (mini) {
    mini.onclick = () => setRailMini(!document.body.classList.contains("rail-mini"));
  }
  const layout = $("railLayout");
  if (layout) layout.onclick = () => setRailLayout(!groupedRail);
  for (const button of rail.querySelectorAll("[data-rail-group]")) {
    button.onclick = () => setRailGroup(button.dataset.railGroup, button.getAttribute("aria-expanded") !== "true");
  }
  for (const button of rail.querySelectorAll("[data-rail-view]")) {
    warmTabOnIntent(button, () => button.dataset.railView);
    button.onclick = () => {
      const id = button.dataset.railView;
      selectTab(id);
      $(`rail-view-${id}`)?.focus({ preventScroll: true });
    };
  }
  rail.onkeydown = event => {
    if (usesGroupedRail()) {
      const button = event.target.closest("[data-rail-group], [data-rail-view]");
      if (!button) return;
      const group = button.closest(".rail-group").querySelector("[data-rail-group]");
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (button === group) setRailGroup(group.dataset.railGroup, false);
        group.focus({ preventScroll: true });
      } else if (event.key === "ArrowRight" && button === group) {
        event.preventDefault();
        if (group.getAttribute("aria-expanded") === "true") {
          group.closest(".rail-group").querySelector("[data-rail-view]")?.focus({ preventScroll: true });
        } else setRailGroup(group.dataset.railGroup, true);
      } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const buttons = [...rail.querySelectorAll("[data-rail-group], [data-rail-view]")]
          .filter(node => !node.closest("[hidden]"));
        const id = keyboardDestination(buttons.map(node => node.id), button.id, event.key);
        $(id)?.focus({ preventScroll: true });
        $(id)?.scrollIntoView({ block: "nearest" });
      }
      return;
    }
    const tab = event.target.closest("[data-main-section]");
    if (!tab) return;
    const ids = [...rail.querySelectorAll("[data-main-section]")].map(button => button.dataset.mainSection);
    const destination = keyboardDestination(ids, tab.dataset.mainSection, event.key);
    if (!destination) return;
    event.preventDefault();
    selectPrimary(destination);
    rail.querySelector(`[data-main-section="${destination}"]`)?.scrollIntoView({ inline: "nearest", block: "nearest" });
  };
}

function setRailGroup(id, open) {
  if (open) expandedRailGroups.add(id);
  else expandedRailGroups.delete(id);
  writeStore("railGroupedOpen", [...expandedRailGroups]);
  $(`rail-group-${id}`).hidden = !open;
  $(`group-${id}`).setAttribute("aria-expanded", String(open));
}

function setRailLayout(grouped) {
  groupedRail = grouped;
  writeStore("railLayout", grouped ? "grouped" : "sections");
  if (grouped) expandedRailGroups.add(railGroupFor(current, sections));
  renderRail();
  wireRail();
  renderViewActions();
  $("railLayout")?.focus({ preventScroll: true });
  window.dispatchEvent(new Event("resize"));
}

function groupedRailMarkup() {
  const activeGroup = railGroupFor(current, sections);
  const activeView = sections.find(view => view.id === current)?.area ? "overview" : current;
  return groupedNavigation(sections).map(group => {
    const open = expandedRailGroups.has(group.id);
    const overview = group.views.find(view => view.id === "overview");
    const summary = sectionCostLabel(overview, money) || group.views.length;
    return `<section class="rail-group${group.id === activeGroup ? " has-current" : ""}">
      <button type="button" class="rail-group-toggle" id="group-${group.id}" data-rail-group="${group.id}"
        aria-expanded="${open}" aria-controls="rail-group-${group.id}" title="${esc(group.label)}">
        <svg class="rail-group-chevron" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
          <path d="m7 5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg><span class="rail-group-icon">${railIcon(group.icon)}</span>
        <span class="rail-name">${esc(group.label)}</span><span class="rail-group-count">${esc(summary)}</span>
      </button>
      <div class="rail-group-views" id="rail-group-${group.id}"${open ? "" : " hidden"}>
        ${group.views.map(view => `<button type="button" class="rail-item rail-view${view.id === activeView ? " on" : ""}"
          id="rail-view-${view.id}" data-rail-view="${view.id}" aria-controls="tabBody"
          ${view.id === activeView ? 'aria-current="page"' : ""} title="${esc(view.label)}">
          ${railIcon(view.id)}<span class="rail-name">${esc(view.label)}</span>
          ${sectionCostLabel(view, money) ? `<span class="rail-sub">${esc(sectionCostLabel(view, money))}</span>` : ""}
        </button>`).join("")}
      </div></section>`;
  }).join("");
}

function renderRail() {
  const iconOnly = document.body.classList.contains("rail-mini");

  // The collapse control sits above the navigation, where Azure's does. At the foot it was
  // below the fold on a short window with every group open — a control you have to scroll to
  // find is a control nobody finds. Here it is the first thing in the rail and always visible.
  const top = `<div class="rail-top">
        <button class="rail-layout-btn" id="railLayout" type="button" aria-pressed="${groupedRail}"
          aria-label="${groupedRail ? "Use current section navigation" : "Use grouped sidebar navigation"}"
          title="${groupedRail ? "Use current section navigation" : "Use grouped sidebar navigation"}">
          <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
            <path d="${groupedRail ? "M4 5h12M4 10h12M4 15h12" : "M5 4v12M10 4v12M15 4v12"}"
              fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </button>
        <button class="rail-mini-btn" id="railMini" type="button"
                aria-pressed="${iconOnly}"
                aria-label="${iconOnly ? "Show labels" : "Collapse to icons"}">
          <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
            <path d="${iconOnly ? "M5 5l5 5-5 5m6-10 5 5-5 5" : "M9 5l-5 5 5 5m6-10-5 5 5 5"}" fill="none"
                  stroke="currentColor" stroke-width="1.6"
                  stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="rail-name">${iconOnly ? "Expand" : "Collapse"}</span>
        </button>
      </div>`;

  const primary = sectionFor(current, sections);
  const groups = PRIMARY_SECTIONS.map(section => {
    if (!sectionViews(section.id, sections).length) return "";
    const on = section.id === primary?.id;
    return `<button type="button" role="tab" data-main-section="${section.id}" id="primary-${section.id}"
      aria-selected="${on}" aria-controls="tabBody" aria-label="${esc(section.label)}"
      class="rail-item${on ? " on" : ""}" tabindex="${on ? 0 : -1}" title="${esc(section.label)}"
      ><span class="rail-icon-frame">${railIcon(section.icon)}</span><span class="rail-name">${esc(section.label)}</span>
      <svg class="rail-chevron" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
        <path d="m8 5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.6"
          stroke-linecap="round" stroke-linejoin="round"/>
      </svg></button>`;
  }).join("");

  if (usesGroupedRail()) {
    $("tabs").innerHTML = top + groupedRailMarkup();
    $("tabs").setAttribute("role", "navigation");
    $("tabs").setAttribute("aria-label", "Grouped navigation");
    $("tabs").removeAttribute("aria-orientation");
    return;
  }
  // The tabs sit in their own wrapper, not directly in the rail: a `tablist` may contain only
  // tabs, and the rail also holds the layout and collapse controls, which axe reported as a
  // critical `aria-required-children` on every tab. `display: contents` leaves the rail's flex
  // layout untouched; only the accessibility tree gains the level it was missing.
  $("tabs").innerHTML = top + `<div class="rail-tabs" role="tablist" aria-label="Main sections"`
    + ` aria-orientation="${narrowNavigation.matches ? "horizontal" : "vertical"}">${groups}</div>`;
  $("tabs").setAttribute("role", "navigation");
  $("tabs").setAttribute("aria-label", "Main sections");
  $("tabs").removeAttribute("aria-orientation");
}

/**
 * Icon-only navigation, as Azure's portal does it.
 *
 * The rail is the widest fixed thing on the page, and on a laptop next to the agent panel it is
 * competing with the numbers for room. Collapsing it returns width to the board while keeping
 * every destination one click away — which is the trade only worth making because the icons
 * carry meaning on their own.
 *
 * The choice is remembered, and accessible names remain available in icon-only mode.
 */
function setRailMini(on) {
  document.documentElement.classList.toggle("rail-mini", on);
  document.body.classList.toggle("rail-mini", on);
  writeStore("railMini", on);
  renderRail();
  wireRail();
  $("railMini")?.focus({ preventScroll: true });
  // Charts measure their container, and the board just changed width. Without this the canvas
  // keeps its old size until something else forces a resize.
  window.dispatchEvent(new Event("resize"));
}

function selectTab(id, { reload = false } = {}) {
  navigationIntent++;
  if (!id) return;
  // A tab that has been folded into another sends you to its replacement rather than nowhere.
  // Deep links from findings, the AI analysis and anything anyone bookmarked all still name the
  // six ids that used to sit under Optimize.
  id = MERGED_TABS[id] || id;
  if (!sections.some(section => section.id === id)) {
    toast("This view is not available for the current account or data scope.", "err");
    return;
  }
  // Choosing a tab is a request to look at that tab. The analysis covers the board, so leaving
  // it open would change the page underneath and show the reader nothing — the click would
  // appear to have done nothing at all. Closing happens even when the tab is unchanged, because
  // clicking the tab you are already on is exactly how someone asks to get back to it.
  closeAnalysis();
  if (id === current && !reload) return tabRendering;
  current = id;
  if (groupedRail) expandedRailGroups.add(railGroupFor(id, sections));
  lastSectionView.set(sectionFor(id, sections).id, id);
  // Not persisted. A fresh load always opens on Overview by design — see where `current` is
  // initialised — so remembering the tab someone left on would just be a write nothing reads.
  // Name the tab on the panel itself, so a screen reader announces what was opened rather than
  // an unlabelled region — and so the skip link lands somewhere that identifies itself.
  const label = (sections.find((s) => s.id === id) || {}).label || id;
  const panel = $("tabBody");
  if (panel) panel.setAttribute("aria-label", label);

  renderRail();
  wireRail();
  tabRendering = reload ? loadTabs() : renderTab();
  return tabRendering;
}

async function navigate(destination) {
  if (typeof destination === "string") destination = { tab: destination };
  const id = MERGED_TABS[destination?.tab] || destination?.tab;
  if (!sections.some(section => section.id === id)) {
    toast("This view is not available for the current account or data scope.", "err");
    return false;
  }
  const intent = ++navigationIntent;
  const before = key(id);
  if (destination.month != null && !validMonth(destination.month)) {
    toast("That reporting month is not available.", "err");
    return false;
  }
  if (destination.filter) {
    const { dimension, value } = destination.filter;
    if (!filterHasValue(dimension, value)) {
      await loadFilters(days, periodMonth);
      if (intent !== navigationIntent) return false;
    }
    if (!applyFilter(dimension, value, { notify: false })) {
      toast(`${value || "Unassigned"} is not a filterable value in this period.`);
      return false;
    }
  }
  if (destination.subscriptions && !selectSubscriptions(destination.subscriptions, { notify: false })) {
    toast("Those subscriptions are not available in the current picker.", "err");
    return false;
  }
  if (destination.month != null) {
    periodMonth = destination.month;
    writeStore("costPeriodMonth", periodMonth);
  }
  const expected = key(id);
  const changed = before !== expected;
  if (changed) invalidateAnalysis();
  const pending = selectTab(id, { reload: changed });
  const selectedIntent = navigationIntent;
  await pending;
  if (selectedIntent !== navigationIntent || current !== id || key(id) !== expected) return false;
  if (changed) loadFilters(days, periodMonth);
  if (destination.target) {
    if (!activeTabs?.reveal?.(destination.target)) {
      toast("That breakdown is not available for this period.");
      $("tabBody").focus({ preventScroll: true });
    }
  } else {
    $("tabBody").focus({ preventScroll: true });
  }
  return true;
}

function fail(message) {
  renderErrorToken = loadToken;
  renderErrorMessage = message;
  document.documentElement.dataset.dashboardReady = "error";
  if ($("tabBody").classList.contains("refreshing") && !$("tabBody").querySelector(".sk-wrap")) {
    toast(message, "err");
    return;
  }
  $("tabBody").innerHTML = `<div class="banner err" role="alert">${esc(message)}
    <button type="button" class="ghost" id="viewRetry">Retry view</button></div>`;
  $("viewRetry").onclick = () => {
    clearCache();
    void loadTabs();
  };
}

/**
 * A floating notice, for feedback about an action rather than about the tab. Exports are the
 * reason this exists: a banner inside the tab body can sit above the fold or be wiped by the
 * next render, so a failed download looked exactly like nothing happening at all.
 */
function toast(message, kind = "ok", action) {
  let host = $("toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "toasts";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "err" ? "alert" : "status");

  const text = document.createElement("span");
  text.textContent = message;
  el.appendChild(text);

  if (action) {
    const a = document.createElement("a");
    a.className = "toast-action";
    a.textContent = action.label;
    a.href = action.href;
    el.appendChild(a);
  }
  const close = document.createElement("button");
  close.className = "toast-x";
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.onclick = () => dismissToast(el);
  el.appendChild(close);

  host.appendChild(el);
  setTimeout(() => dismissToast(el), action ? 20000 : 6000);
  return el;
}

/**
 * Fade a toast out before removing it, rather than snapping it out of the DOM mid-read. It
 * arrived on its own entrance; leaving with none was the one asymmetry left in an otherwise
 * animated notice. `dismissed` guards the two paths that can both reach here for the same
 * element — the close button and the auto-dismiss timer — so a click right as the timer fires
 * cannot restart an animation on a node already on its way out. The timeout is a safety net for
 * `prefers-reduced-motion`, where the global rule collapses the animation to near-zero and
 * still fires `animationend`, but is cheap enough to leave in either way.
 */
function dismissToast(el) {
  if (!el.isConnected || el.classList.contains("leaving")) return;
  el.classList.add("leaving");
  el.addEventListener("animationend", () => el.remove(), { once: true });
  setTimeout(() => el.remove(), 220);
}


// -------------------------------------------------------------------- render
const LIVE_WAIT = {
  changes: "Reading recent Azure resource changes and their billed-cost context...",
  performance: "Reading supported Azure Monitor metrics; unavailable telemetry will be labeled...",
  billing: "Preparing the last calendar month's statement and current savings opportunities...",
  "azure-updates": "Loading Azure announcements, public status and subscription health independently...",
  savings: "Reading four Azure scans and merging what they each found…",
  waste: "Scanning Azure for idle and orphaned resources…",
  budgets: "Reading budgets and current spend from Azure…",
  history: "Reading the archived snapshots…",
  anomalies: "Comparing every day against its own baseline…",
  shutdown: "Reading hourly CPU to find machines only used in the daytime…",
  rightsizing: "Reading CPU metrics from Azure Monitor…",
  esu: "Checking operating system and SQL versions against their support dates…",
  governance: "Checking tag coverage and network configuration across the estate…",
  rates: "Reading Advisor's reservation and savings plan recommendations…",
  commitments: "Reading your commitments and what Advisor would recommend instead…",
  aks: "Matching node resource groups back to the clusters that own them…",
  health: "Reading Azure Service Health for retirements and required migrations…",
  advisor: "Fetching Advisor recommendations…",
  report: "Loading what you can include…",
};

function renderTab(preserve = false) {
  tabRendering = renderTabContent(preserve);
  return tabRendering;
}

async function renderTabContent(preserve = false) {
  snapshotViews.activate(key(current));
  if (sectionSnapshot?.signature === key("")) snapshotViews.observe(sectionSnapshot.data, key(current));
  document.documentElement.dataset.dashboardReady = "false";
  renderViewActions();
  const previousToken = loadToken;
  const mine = ++renderSequence;
  loadToken = mine;
  const body = $("tabBody");
  if (arrivalFrame != null) cancelAnimationFrame(arrivalFrame);
  arrivalFrame = null;
  body.classList.remove("tab-enter");
  const nextKey = key(current);
  const sameView = renderedKey === nextKey;
  activeTabs?.stop?.();
  const record = beginNavigation(current);
  activePerformance = record;
  if (!sameView && renderedComplete) {
    views.save(renderedKey, body, {
      token: previousToken, tables: new Map(TABLES), tabs: activeTabs, at: renderedAt,
    });
  }
  if (!sameView) {
    rememberTables();
    TABLES.clear();
    body.onclick = null;
  }
  const saved = !preserve ? views.take(nextKey, body) : null;
  renderedKey = nextKey;
  renderedComplete = false;
  if (saved) {
    loadToken = saved.token;
    activeTabs = saved.tabs;
    renderedAt = saved.at;
    renderedComplete = true;
    for (const [id, state] of saved.tables) TABLES.set(id, state);
    record.cached = true;
    navigationFeedback(record);
    body.classList.remove("refreshing");
    body.removeAttribute("aria-busy");
    activeTabs?.resume?.(loadToken, current);
    record.status = "ready";
    finishNavigation(record);
    reportReadiness(true);
    return;
  }
  preserve = preserve && sameView;
  if (!preserve || !body.childElementCount) {
    body.innerHTML = skeleton(current);
    startSkeletonClock();
  } else {
    body.classList.add("refreshing");
    body.setAttribute("aria-busy", "true");
  }
  navigationFeedback(record);

  try {
    const [family, renderer] = TAB_RENDERERS[current] || ["spend", "renderSection"];
    const tab = current;
    const tabs = await loadFamily(family);
    if (mine !== loadToken) return;
    activeTabs = tabs;
    await tabs[renderer](mine, tab);
    if (mine === loadToken) renderedComplete = renderErrorToken !== mine;
  } catch (err) {
    if (mine !== loadToken) return;
    record.error = err?.message || "Something went wrong loading this tab.";
    if (preserve && body.childElementCount) {
      toast(err?.message || "Something went wrong refreshing this tab.", "err");
    } else {
      fail(err?.message || "Something went wrong loading this tab.");
    }
  } finally {
    if (mine === loadToken) {
      clearInterval(skTimer);
      body.classList.remove("refreshing");
      body.removeAttribute("aria-busy");
      renderedAt = Date.now();
      pruneTables(body);
      wireTables(body);
      record.status = renderedComplete ? "ready" : "error";
      if (!renderedComplete && !record.error) record.error = renderErrorMessage;
      finishNavigation(record);
      // The one-shot arrival animation — only for a genuine tab switch or the first load.
      // Removing then re-adding the class on the next frame is what lets it restart when
      // someone switches tabs twice in quick succession; a filter or period change never
      // reaches this branch at all, so narrowing a filter can never replay it.
      if (!preserve) {
        body.classList.remove("tab-enter");
        arrivalFrame = requestAnimationFrame(() => {
          arrivalFrame = null;
          if (mine !== loadToken) return;
          body.classList.add("tab-enter");
          body.addEventListener("animationend", () => body.classList.remove("tab-enter"),
                                 { once: true });
        });
      }
      reportReadiness(renderedComplete, record.error);
    }
  }
}

/**
 * A placeholder shaped like the answer.
 *
 * A line of "Loading…" tells you nothing about what is coming and makes the page jump when it
 * lands. Blocks the size of the real panels hold the layout still and make the wait legible —
 * particularly on the live tabs, which take real seconds because they are querying Azure.
 */
function skeleton(tab) {
  const wait = LIVE_WAIT[tab];
  const bar = (h) => `<div class="sk" style="height:${h}px"></div>`;
  return `<div class="sk-wrap" role="status" aria-label="${esc(wait || "Loading")}">
      ${wait ? `<p class="sk-note">${esc(wait)}
        <span class="sk-clock" id="skClock" aria-hidden="true"></span></p>` : ""}
      ${bar(74)}
      ${bar(240)}
      <div class="sk-row">${bar(200)}${bar(200)}</div>
    </div>`;
}

/**
 * A clock on the slow tabs.
 *
 * Waste takes 7.6s and Governance 6.7s because both scan live Azure inventory. A static
 * skeleton for that long is indistinguishable from a hang — the honest fix is not to pretend it
 * is fast but to show that it is still working, and after ten seconds to say so in words.
 */
let skTimer = null;
function startSkeletonClock() {
  clearInterval(skTimer);
  const started = Date.now();
  skTimer = setInterval(() => {
    const el = $("skClock");
    if (!el) return clearInterval(skTimer);
    const s = Math.round((Date.now() - started) / 1000);
    if (s < 3) return;
    el.textContent = s < 10 ? ` ${s}s` : ` ${s}s · Azure is slow to answer, still waiting`;
  }, 1000);
}

function kpiRow(items) {
  return `<div class="kpis">${items
    .map(
      (k) => `<${k.tab || k.jump ? "button" : "div"} class="kpi"${k.id ? ` id="${esc(k.id)}"` : ""}${k.tab || k.jump ? ' type="button"' : ""}${
        k.tab ? ` data-overview-tab="${esc(k.tab)}"` : k.jump ? ` data-overview-jump="${esc(k.jump)}"` : ""}>
        <span class="k">${esc(k.label)}</span>
        <span class="v">${k.value}</span>
        ${k.sub ? `<span class="s ${k.cls || ""}">${k.sub}</span>` : ""}
      </${k.tab || k.jump ? "button" : "div"}>`
    )
    .join("")}</div>`;
}

/** Render a scoped table with local search, sorting, paging and resource details. */
function table(title, rows, columns, note, opts = {}) {
  if (!rows.length) {
    return `<section class="card"><h3>${esc(title)}</h3>
      <p class="muted empty">Nothing in this period.</p></section>`;
  }
  const identity = opts.key || `${title}|${columns.map(c => c.key || c.label).join("|")}`;
  const id = `t-${stableKey(`${key(current)}|${identity}`)}`;
  const previous = TABLES.get(id) || TABLE_STATE.get(id);
  const pageSize = opts.pageSize ?? 10;
  TABLES.set(id, {
    rows, columns, pageSize, page: opts.resetPage ? 0 : previous?.page ?? 0,
    sort: previous?.sort ?? null, dir: previous?.dir ?? 1, title,
    explore: opts.explore !== false,
    expanded: new Set(previous?.expanded || []),
    rowKeys: tableRowKeys(rows),
    scope: scopeIds().length ? [...scopeIds()] : allSubscriptions().map(sub => sub.id),
    total: opts.total !== false,
    query: opts.search === false ? "" : previous?.query ?? "",
    searchRows: null,
    viewCache: null,
    outlier: opts.highlightOutliers === false ? null : outlierThreshold(rows, columns),
  });
  const state = TABLES.get(id);
  state.page = Math.min(state.page, Math.max(0, Math.ceil(view(state).length / pageSize) - 1));
  // A retained query must remain editable after a refresh reduces the row count.
  const searchable = opts.search !== false && (rows.length > pageSize || state.query);
  const boxed = opts.scroll !== false;
  return `<section class="card" data-table="${id}">
      <div class="card-head">
        <h3>${esc(title)}</h3>
        ${searchable ? `<div class="tsearch">
          <input type="search" data-tsearch="${id}" placeholder="Search ${esc(title.toLowerCase())}…"
                 value="${esc(state.query)}"
                 aria-label="Search ${esc(title.toLowerCase())}" />
        </div>` : ""}
      </div>
      ${note ? `<p class="muted note">${esc(note)}</p>` : ""}
      <div class="table-wrap${boxed ? "" : " flush"}" tabindex="0" role="region" aria-label="${esc(title)}, scrollable">
        ${tableMarkup(id)}
      </div>
      ${pagerMarkup(id)}
      ${totalMarkup(id)}
    </section>`;
}

/** Totals cover matching rows across all pages, and only explicitly summable columns. */
function totalMarkup(id) {
  const t = TABLES.get(id);
  if (!t.total) return "";
  const cells = t.columns.filter((c) => c.total);
  if (!cells.length) return "";
  const rows = view(t);
  return `<div class="tfoot">
      <span class="tfoot-k">${rows.length} ${rows.length === 1 ? "row" : "rows"}</span>
      ${cells
        .map(
          (c) =>
            `<span class="tfoot-v"><span class="tfoot-lbl">${esc(c.label)}</span>${c.total(
              rows
            )}</span>`
        )
        .join("")}
    </div>`;
}

/** Table identity includes the complete data context and a stable title/schema or explicit key. */
const TABLES = new Map();
const TABLE_STATE = new Map();

function rememberTables() {
  for (const [id, { page, sort, dir, query, expanded }] of TABLES) {
    TABLE_STATE.delete(id);
    TABLE_STATE.set(id, { page, sort, dir, query, expanded: new Set(expanded) });
  }
  while (TABLE_STATE.size > 200) TABLE_STATE.delete(TABLE_STATE.keys().next().value);
}

function pruneTables(root) {
  rememberTables();
  for (const id of TABLES.keys()) {
    if (!root.querySelector(`[data-table="${id}"]`)) TABLES.delete(id);
  }
}

function tableMarkup(id) {
  const t = TABLES.get(id);
  const head = t.columns
    .map((c, i) => {
      const sortable = c.sort !== false;
      const cls = [c.num ? "num" : "", sortable ? "sortable" : "",
                   t.sort === i ? (t.dir === 1 ? "sort-asc" : "sort-desc") : ""]
        .filter(Boolean).join(" ");
      const aria = t.sort === i ? (t.dir === 1 ? "ascending" : "descending") : "none";
      return `<th${cls ? ` class="${cls}"` : ""}${c.description ? ` title="${esc(c.description)}"` : ""}${sortable ? ` data-col="${i}" role="columnheader"
        aria-sort="${aria}" tabindex="0"` : ""}>${esc(c.label)}</th>`;
    })
    .join("") + (t.explore ? '<th scope="col" class="row-actions">Explore</th>' : "");

  const start = t.page * t.pageSize;
  const rows = view(t);
  const body = rows
    .slice(start, start + t.pageSize)
    .map((r) => {
      // Marked, not recoloured. A red row says "this is wrong"; an expensive resource is
      // usually not wrong, it is simply the one worth looking at first.
      const big = t.outlier && t.outlier.col.value(r) >= t.outlier.cut;
      const rowKey = t.rowKeys.get(r);
      const expanded = t.expanded.has(rowKey);
      const portal = t.explore ? portalTargets(rowContext(t, r), t.scope)[0] : null;
      const controls = t.explore ? `<td class="row-actions">
        <button type="button" class="linkish" data-row-detail="${rowKey}"
          aria-expanded="${expanded}" aria-controls="${id}-detail-${rowKey}">${expanded ? "Hide" : "Details"}</button>
        ${portal ? `<a href="${esc(portal.url)}" target="_blank" rel="noopener noreferrer"
          title="Open ${esc(portal.label.toLowerCase())} in Azure Portal">${esc(portal.label)} ↗</a>` : ""}
        </td>` : "";
      return `<tr${t.explore ? ` data-explore-row="${rowKey}"` : ""}${big ? ' class="row-high" title="Unusually high for this table"' : ""}>${
        t.columns.map((c) => `<td${c.num ? ' class="num"' : ""}>${c.cell(r)}</td>`).join("")
      }${controls}</tr>${t.explore && expanded ? rowDetailMarkup(id, r, rowKey) : ""}`;
    })
    .join("");
  if (!rows.length) {
    return `<table><thead><tr>${head}</tr></thead><tbody>`
      + `<tr><td class="tempty" colspan="${t.columns.length + (t.explore ? 1 : 0)}">`
      + `No rows match “${esc(t.query)}”.</td></tr></tbody></table>`;
  }
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function rowContext(t, row) {
  if (t.title === "By subscription") return { ...row, subscription_id: row.subscription_id || row.id };
  if (t.title === "Top resource groups") return { ...row, resource_group: row.resource_group || row.name };
  return row;
}

function rowDetailMarkup(id, row, rowKey) {
  const t = TABLES.get(id);
  const links = portalTargets(rowContext(t, row), t.scope);
  const area = sections.find(section => section.area && section.id === (row.area_id || row.id));
  return `<tr class="row-detail" data-row-detail-panel="${rowKey}"><td colspan="${t.columns.length + 1}">
    <section id="${id}-detail-${rowKey}" data-state-key="${rowKey}" aria-label="Row details">
      <div class="explore-links">
      ${links.map(link => `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">Open ${esc(link.label.toLowerCase())} in Azure ↗</a>`).join("")}
      ${area ? `<button type="button" class="linkish" data-explore-tab="${esc(area.id)}">Explore ${esc(area.label)}</button>` : ""}
      </div>${rowDetailsMarkup(row, { currency: row.currency, stateKey: `${id}-${rowKey}` })}
    </section></td></tr>`;
}

function toggleRowDetail(section, id, rowKey) {
  const t = TABLES.get(id);
  const row = t?.rows.find(item => t.rowKeys.get(item) === rowKey);
  if (!row) return;
  const button = section.querySelector(`[data-row-detail="${rowKey}"]`);
  if (t.expanded.has(rowKey)) {
    t.expanded.delete(rowKey);
    section.querySelector(`[data-row-detail-panel="${rowKey}"]`)?.remove();
  } else {
    t.expanded.add(rowKey);
    section.querySelector(`[data-explore-row="${rowKey}"]`)
      .insertAdjacentHTML("afterend", rowDetailMarkup(id, row, rowKey));
  }
  button.setAttribute("aria-expanded", String(t.expanded.has(rowKey)));
  button.textContent = t.expanded.has(rowKey) ? "Hide" : "Details";
}

function pagerMarkup(id) {
  const t = TABLES.get(id);
  // Counted over what is actually being shown, so a search that narrows 400 rows to 3 says
  // "1–3 of 3" rather than paging through 397 rows it is no longer displaying.
  const shown = view(t).length;
  if (shown <= t.pageSize) return "";
  const pages = Math.ceil(shown / t.pageSize);
  const from = t.page * t.pageSize + 1;
  const to = Math.min(shown, from + t.pageSize - 1);
  return `<div class="pager">
      <span class="pager-count">${from}–${to} of ${shown}</span>
      <span class="pager-btns">
        <button type="button" data-page="prev" ${t.page === 0 ? "disabled" : ""}>Previous</button>
        <button type="button" data-page="next" ${t.page >= pages - 1 ? "disabled" : ""}>Next</button>
      </span>
    </div>`;
}

/** Delegate table interactions so replacing rows does not discard their handlers. */
function wireTables(root) {
  for (const section of root.querySelectorAll("[data-table]")) {
    const id = section.dataset.table;
    if (!TABLES.has(id)) continue;
    if (section.dataset.tableWired) continue;
    section.dataset.tableWired = "true";

    const redraw = () => {
      setContent(section.querySelector(".table-wrap"), tableMarkup(id));
      const pager = section.querySelector(".pager");
      const footer = section.querySelector(".tfoot");
      const markup = pagerMarkup(id);
      if (pager) pager.outerHTML = markup;
      else if (markup && footer) footer.insertAdjacentHTML("beforebegin", markup);
      else if (markup) section.insertAdjacentHTML("beforeend", markup);
      if (footer) footer.outerHTML = totalMarkup(id);
    };

    const search = section.querySelector("[data-tsearch]");
    if (search) {
      let searchTimer = null;
      search.oninput = () => {
        clearTimeout(searchTimer);
        const t = TABLES.get(id);
        if (!t) return;
        t.query = search.value.trim();
        t.viewCache = null;
        t.page = 0;
        searchTimer = setTimeout(() => {
          if (!TABLES.has(id) || !section.isConnected) return;
          // Back to the first page: staying on page 4 of a result set that is now one page
          // long shows an empty table and looks like the search found nothing.
          redraw();
        }, 120);
      };
    }

    section.addEventListener("click", (e) => {
      const th = e.target.closest("th[data-col]");
      if (th) return sortBy(id, Number(th.dataset.col), redraw);
      const related = e.target.closest("[data-explore-tab]");
      if (related) return selectTab(related.dataset.exploreTab);
      const detail = e.target.closest("[data-row-detail]");
      if (detail) return toggleRowDetail(section, id, detail.dataset.rowDetail);
      const row = e.target.closest("tr[data-explore-row]");
      if (row && !e.target.closest("a,button,input,select,textarea,summary,details")
          && !window.getSelection()?.toString()) {
        return toggleRowDetail(section, id, row.dataset.exploreRow);
      }
      const btn = e.target.closest("[data-page]");
      if (!btn) return;
      const t = TABLES.get(id);
      if (!t) return;
      t.page += btn.dataset.page === "next" ? 1 : -1;
      redraw();
    });
    section.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const th = e.target.closest("th[data-col]");
      if (!th) return;
      e.preventDefault();
      sortBy(id, Number(th.dataset.col), redraw);
    });
  }
}

function sortBy(id, col, redraw) {
  const t = TABLES.get(id);
  if (!t) return;
  // Same column toggles direction; a new column starts descending for numbers (the largest
  // cost is what someone is looking for) and ascending for text.
  if (t.sort === col) t.dir = -t.dir;
  else {
    t.sort = col;
    t.dir = t.columns[col].num ? -1 : 1;
  }
  t.page = 0;
  t.viewCache = null;
  redraw();
}

/** A share-of-total bar, so a table of numbers reads as proportions at a glance. */
function bar(value, max) {
  const w = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  return `<span class="meter"><span style="width:${w}%"></span></span>`;
}

function whenNear(element, prepare) {
  const root = $("tabBody");
  let near = !("IntersectionObserver" in window);
  let loading = false;
  let finished = false;
  let commit = null;
  let observer = null;
  const cleanup = () => {
    finished = true;
    clearInterval(detachedCheck);
    observer?.disconnect();
    window.removeEventListener("cl:dashboard-ready", advance);
  };
  // Importing can outlive a tab switch. Keep the prepared renderer until its own cached
  // host returns, rather than consuming the observer and dropping the visual permanently.
  const advance = async () => {
    if (finished || !near || !root.contains(element)) return;
    if (loading) return;
    loading = true;
    try {
      if (!commit) commit = await prepare();
      if (finished || !near || !root.contains(element)) return;
      await commit();
      cleanup();
    } catch (err) {
      cleanup();
      if (element.isConnected) {
        element.innerHTML = `<p class="banner err">Could not load this visual: ${esc(err?.message || err)}. Reopen the tab to retry.</p>`;
      }
    } finally {
      loading = false;
    }
  };
  const detachedCheck = setInterval(() => {
    if (!element.isConnected) cleanup();
  }, 2000);
  window.addEventListener("cl:dashboard-ready", advance);
  if ("IntersectionObserver" in window) {
    observer = new IntersectionObserver((entries) => {
      near = entries.some(entry => entry.isIntersecting);
      if (near) advance();
    }, { root, rootMargin: "320px 0px" });
    observer.observe(element);
  } else advance();
}

// ------------------------------------------------------------- refresh data
// Pulling newer cost data is the one write this app can make, so it is admin-only, explicit,
// and reports what it is doing. Two routes: the Cost Details API (works anywhere, slow) and a
// Cost Management export already being written to storage (the scalable path on a big tenant).
let lastAsOf = null;
let focusHeaderTracker = null;
let focusHeaderSequence = 0;
let lastFocusCompletion = "";

/** Show how current the data is on the button, whenever both facts are known. */
function applyFreshness() {
  if (!lastAsOf || $("refresh").hidden || $("refreshBtn").classList.contains("busy")) return;
  const when = new Date(`${lastAsOf}T00:00:00`);
  setRefreshLabel(
    `Refresh · to ${when.toLocaleDateString(undefined, { day: "numeric", month: "short" })}`
  );
}

function setRefreshLabel(text, busy = false) {
  $("refreshLabel").textContent = text;
  $("refreshBtn").classList.toggle("busy", busy);
  $("refreshBtn").disabled = busy;
  if (!busy && text === "Refresh data") applyFreshness();
}

function openRefresh(open) {
  $("refreshMenu").hidden = !open;
  $("refreshBtn").setAttribute("aria-expanded", String(open));
  if (open) buildRefreshMenu();
}

/** Two sources: a FOCUS export for detail, or a live query without replacing richer history. */
function buildRefreshMenu() {
  const menu = $("refreshMenu");
  if (menu.dataset.built === "1") return;

  menu.innerHTML = `
    <div class="head">Reload cost data</div>
    <button data-act="latest">Refresh latest
      <span class="sub">Open Cost data to review setup, refresh the saved cadence window,
        or retry missing history in one place</span></button>
    <button data-act="focus">FOCUS report
      <span class="sub">Loads an existing FOCUS export with actual and amortized cost, benefits
        and tags. Requires a readable FOCUS export; for WebDirect, use Quick refresh</span></button>
    <button data-act="quick">Quick refresh
      <span class="sub">Live actual-cost query for the last 3 months, capped at about a minute.
        Fewer columns; days with FOCUS or richer detail are kept unchanged</span></button>
    <p class="note">Selected subscriptions only. FOCUS reconciles changed days; Quick refresh
      updates query-only days and fills gaps. Repeating either does not add duplicate costs.</p>
    <div class="archive-note" id="archiveNote"></div>`;

  // The same endpoint, asked to use the synchronous Query API instead of a generated report.
  // Actual rather than amortized: a query cannot return benefit attribution either way, and
  // actual is the metric that needs no reservation spreading to be meaningful without it.
  menu.querySelector("[data-act='quick']").onclick = () =>
    startIngest("/api/ingest?months=3&metric=ActualCost&quick=true", "POST");
  // Explicitly require FOCUS; another export type must never masquerade as this choice.
  menu.querySelector("[data-act='focus']").onclick = () =>
    startIngest("/api/exports/ingest", "POST", { max_files: 60, metric: "FocusCost" });
  menu.querySelector("[data-act='latest']").onclick = startLatestFocus;

  menu.dataset.built = "1";
  paintArchiveNote();
}

async function startLatestFocus() {
  const sequence = ++focusHeaderSequence;
  const signature = key("");
  focusHeaderTracker?.stop?.();
  focusHeaderTracker = null;
  openRefresh(false);
  setRefreshLabel("Checking exports...", true);
  const current = () => sequence === focusHeaderSequence && key("") === signature;
  try {
    const reports = await loadFamily("reports");
    if (!current()) return;
    const tracker = await reports.refreshLatest({
      isCurrent: () => {
        const valid = current();
        if (!valid && sequence === focusHeaderSequence) setRefreshLabel("Refresh data");
        return valid;
      },
      onStatus: state => {
        if (current()) setRefreshLabel(
          state.status === "running" ? "Refreshing FOCUS..." : "Refresh data",
          state.status === "running");
      },
    });
    if (sequence === focusHeaderSequence) focusHeaderTracker = tracker;
    else tracker?.stop?.();
  } catch (error) {
    if (current()) toast(error?.message || "Could not open Refresh latest.", "err");
  } finally {
    if (sequence === focusHeaderSequence && !focusHeaderTracker?.isActive?.()) setRefreshLabel("Refresh data");
  }
}

/**
 * Where the archive goes, and whether it is working.
 *
 * Inside the refresh menu because that is the moment it matters: every option above this line
 * writes a dated copy, and a refresh that silently stopped archiving would otherwise be
 * invisible until someone went looking for a file that was never written.
 */
async function paintArchiveNote() {
  const host = $("archiveNote");
  if (!host) return;
  host.innerHTML = `<span class="muted">Checking the archive…</span>`;
  try {
    const a = await get("/api/archive?limit=6", { timeout: 30000 });
    if (!a.enabled) {
      host.innerHTML = `<span class="muted">No archive configured.</span>`;
      return;
    }
    if (!a.reachable) {
      host.innerHTML = `<span class="bad">Archive unreachable</span>
        <span class="muted">${esc(a.error || "")}</span>`;
      return;
    }
    const names = Object.keys(a.latest || {});
    const last = names.length
      ? names.map((k) => `${esc(k)} · ${esc((a.latest[k].name || "").split("/").pop())}`).join("<br>")
      : "nothing archived yet";
    host.innerHTML = `<span class="ok">Archiving to ${esc(a.account)}/${esc(a.container)}</span>
      <span class="muted">One file per dataset per day. Latest:<br>${last}</span>`;
  } catch {
    host.innerHTML = `<span class="muted">Could not read the archive status.</span>`;
  }
}

async function startIngest(url, method, body) {
  const scope = scopeIds();
  if (url.startsWith("/api/ingest?")) url += `&scope=${encodeURIComponent(scope.join(","))}`;
  if (url === "/api/exports/ingest") body = { ...body, scope };
  openRefresh(false);
  setRefreshLabel("Starting…", true);
  try {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setRefreshLabel("Refresh data");
      showIngest(false);
      toast(
        typeof data.detail === "string"
          ? data.detail
          : `Could not start the refresh (${res.status}).`,
        "err"
      );
      return;
    }
    // Say which source is actually being used. "FOCUS report" resolves to a specific export
    // server-side, and knowing which one it picked is the difference between a number you can
    // trust and one you have to go and verify.
    if (data.export) toast(`Loading from ${data.export} (${data.type || "export"}).`);
    watchIngest(data.job_id || data.started);
  } catch (err) {
    setRefreshLabel("Refresh data");
    showIngest(false);
    toast("Could not reach the server to start a refresh.", "err");
  }
}

/**
 * Poll until the ingest stops, then rebuild everything from the new data.
 *
 * Pin the immutable job ID so a subsequent run cannot replace this run's terminal result.
 */
async function watchIngest(runStarted) {
  const maxPolls = 800; // six months across a dozen subscriptions
  const query = runStarted ? `?job_id=${encodeURIComponent(runStarted)}` : "";
  showIngest(true);
  for (let i = 0; i < maxPolls; i++) {
    // Look almost at once: a silent strip invites a second press.
    await new Promise((r) => setTimeout(r, i ? 3000 : 400));
    let w;
    try {
      w = await get(`/api/warehouse${query}`, { timeout: 20000 });
    } catch {
      continue; // a restart mid-ingest shouldn't abandon the watch
    }
    const state = w.ingest || {};
    if (runStarted && (state.job_id || state.started) !== runStarted) continue;
    if (state.status === "running") {
      // Clamped, not raw: progress dips as a subscription leaves its loading phase.
      setRefreshLabel(`Loading ${Math.round(paintIngest(state) * 100)}%…`, true);
      continue;
    }
    showIngest(false);
    // Say what was just loaded, not what was loaded before it. `setRefreshLabel` re-applies the
    // freshness date, and until now that date was only updated later by `loadTabs()` — so the
    // button kept yesterday's date after a refresh that had plainly worked, and only corrected
    // itself on the next full page load. The refresh knows the answer; it should not wait to be
    // told by a re-render.
    if (w.to) lastAsOf = w.to;
    setRefreshLabel("Refresh data");

    // Outcomes go to a toast, not a banner. The old path prepended into #tabBody, which is the
    // wrong place for feedback about a header button: it can sit below the fold, and on a
    // partial refresh the loadTabs() below wiped it before anyone read it. toast() floats and
    // survives the re-render — it exists because a failed export "looked exactly like nothing
    // happening at all".
    if (state.status === "failed") {
      toast(`Refresh failed. ${state.detail || state.error || "No reason was reported."}`, "err");
      return;
    }
    // A partial ingest loaded something, so the new data is worth showing — but saying nothing
    // would leave a hole in the numbers that nobody knows about.
    if (state.status === "partial") {
      toast(`Some data could not be loaded. ${state.detail || "See the server log."}`, "err");
    }
    // Nothing came back and nothing was replaced. Distinct from a failure — no request errored —
    // and distinct from success, because the numbers on screen are the ones that were already
    // there. Reporting it as "ready" is what used to make an empty refresh look like a working
    // one, right up until someone noticed the figures had not moved in a week.
    if (state.status === "empty") {
      toast(
        state.detail ||
          "The refresh returned no rows, so nothing changed. The existing data is unchanged.",
        "warn"
      );
    }
    if (state.mode === "quick" && (state.status === "ready" || state.status === "partial")) {
      toast(
        `Loaded ${(state.loaded_rows ?? state.rows ?? 0).toLocaleString()} rows. A quick refresh carries ` +
          "cost by day, resource, service and region — quantities, unit prices, reservation " +
          "benefits and tags need FOCUS. " +
          `${state.preserved_days || 0} subscription-days with richer data were kept unchanged.`,
        "warn"
      );
    }
    // The archive is a side effect of the refresh, so it reports separately. A silent failure
    // here is the one that matters: the dashboard looks correct, and the daily copy that was
    // supposed to preserve these numbers was never written.
    const arch = state.archive;
    if (arch?.error) {
      toast(`Data loaded, but the daily archive failed. ${arch.error}`, "err");
    } else if (arch?.name) {
      toast(`Archived ${(arch.rows || 0).toLocaleString()} rows to ${arch.name}.`);
    }
    clearCache();
    refreshHeader();
    await loadTabs({ preserve: true });
    return;
  }
  setRefreshLabel("Refresh data");
  showIngest(false);
  // Giving up silently is how this looked like "the button does nothing": the spinner simply
  // stopped and the numbers were unchanged, with no way to tell whether it had worked.
  toast(
    "Still loading after 40 minutes, so this page stopped watching. The refresh is probably " +
      "still running on the server — reload in a few minutes to see the result.",
    "err"
  );
}

// ------------------------------------------------------------- the progress strip
// A refresh is minutes of waiting on Azure. A control that merely looks busy is what makes
// someone press it a second time, or reload the page mid-ingest and assume it failed — so the
// wait gets a bar that actually moves, a clock, and a sentence saying it is safe to walk away.
let ingestStarted = 0;
let ingestTick = null;

function showIngest(on) {
  const host = $("ingest");
  if (!host) return;
  host.hidden = !on;
  if (on) {
    ingestStarted = ingestStarted || Date.now();
    // The clock runs on its own timer rather than on the poll, so it counts smoothly between
    // the three-second polls instead of jumping.
    ingestTick = ingestTick || setInterval(paintElapsed, 1000);
  } else {
    clearInterval(ingestTick);
    ingestTick = null;
    ingestStarted = 0;
    setIngestFill(0);
  }
}

function paintElapsed() {
  const el = $("ingestElapsed");
  if (!el || !ingestStarted) return;
  const s = Math.floor((Date.now() - ingestStarted) / 1000);
  el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The bar only ever moves forwards. A phase count that dips must not drag it backwards. */
let ingestSeen = 0;
function setIngestFill(fraction) {
  if (fraction === 0) ingestSeen = 0;
  ingestSeen = Math.max(ingestSeen, fraction);
  const whole = Math.round(ingestSeen * 100);
  const fill = $("ingestFill");
  if (!fill) return ingestSeen;
  fill.style.width = `${whole}%`;
  const pct = $("ingestPct");
  if (pct) pct.textContent = `${whole}%`;
  const bar = fill.parentElement;
  if (bar) {
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuenow", String(whole));
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
  }
  return ingestSeen;
}

function paintIngest(state) {
  showIngest(true);
  const shown = setIngestFill(state.progress || 0);
  const done = state.done ?? 0;
  const total = state.total ?? 0;
  const gen = state.generating || 0;
  const sub = state.submitting || 0;
  const load = state.loading || 0;

  // Name the wait rather than the step number. "2 of 3" says nothing about why nothing has
  // happened for four minutes; "Azure is building 2 reports" says exactly why.
  let what;
  if (state.fetching) {
    // An export is a couple of very large blobs, and the whole wait is one download. Saying
    // which file, and how big, is the difference between a slow refresh and an apparently
    // dead one — the bar cannot move within a single fetch, so the words have to.
    const mb = state.fetching_bytes ? ` (${(state.fetching_bytes / 1048576).toFixed(1)} MB)` : "";
    const name = state.fetching_name ? ` ${state.fetching_name}` : "";
    what = `Downloading file ${state.fetching} of ${state.total || "?"}${name}${mb}…`;
  } else if (state.writing) {
    // The longest single step, and the one that used to look like a hang: tens of thousands of
    // rows going into the warehouse. Named so the wait is legible rather than a bar stuck at 90%.
    what = `Writing ${state.writing.toLocaleString()} rows into the warehouse…`;
  } else if (load) what = `Loading ${load} report${load === 1 ? "" : "s"} into the warehouse…`;
  else if (gen) what = `Azure is building ${gen} report${gen === 1 ? "" : "s"}…`;
  else if (sub) what = `Queuing ${sub} request${sub === 1 ? "" : "s"} with Azure…`;
  else if (done && done === total) what = "Finishing up…";
  else what = "Starting…";

  // Named as the menu names it, and with the route it took. "API data" was this option's old
  // name, and leaving it here made the *slow* full-detail path read like the fast one someone
  // had meant to pick — a nine-period, six-minute run labelled as if it were the live query.
  // The route is what actually predicts the wait, so it is what the strip says.
  const metric = { FocusCost: "FOCUS", AmortizedCost: "Amortized", ActualCost: "Actual" }[
    state.metric
  ];
  const route = state.mode === "quick"
    ? "quick"
    : state.metric && state.metric !== "FocusCost" && state.unit === "period"
      ? "full detail"
      : "";
  const named = metric ? `${metric}${route ? ` · ${route}` : ""}: ` : "";
  // The two routes count different things. Reading an export counts blobs; the API route counts
  // the periods it asked Azure to build. Calling both "periods" put "0/2 periods loaded" over a
  // FOCUS run that has no periods in it.
  const unit = state.unit || "period";
  const scope = total && !state.fetching
    ? ` · ${done}/${total} ${unit}${total === 1 ? "" : "s"} loaded`
    : "";
  const el = $("ingestWhat");
  if (el) el.textContent = `${named}${what}${scope}`;

  // The note is the expectation being set, so it has to match the route. "A few minutes" over
  // a quick refresh is wrong in one direction and over a full-detail run — nine periods Azure
  // builds one at a time — it is wrong in the other. Both were the same sentence.
  const note = $("ingestNote");
  if (note) {
    note.textContent = state.mode === "quick"
      ? "A live query, capped at about a minute. Whatever comes back in that time is kept."
      : state.unit === "period"
        ? "Azure builds these reports on request, which usually takes ten minutes or more. "
          + "You can keep using the dashboard — the numbers update on their own when it finishes."
        : "Reading an export Azure has already written. You can keep using the dashboard — "
          + "the numbers update on their own when it finishes.";
  }
  return shown;
}

function wireDownloadLink(a, url) {
  a.href = url;
  a.removeAttribute("target");
  // No `download` attribute: the filename comes from Content-Disposition, and leaving it off
  // means an expired session redirects to the sign-in page (which the browser can show)
  // rather than saving the redirect as a file.
  a.onclick = () => {
    toast("Your download is starting. Check your browser's downloads if you don't see it.");
    return true;
  };
}

// -------------------------------------------------------------------- export
/** Download the current view as a file. The server builds it from the same data and scope. */
function wireExport() {
  const wrap = $("exportWrap");
  const menu = $("exportMenu");
  const btn = $("exportBtn");
  if (!wrap) return;

  const open = (show) => {
    menu.hidden = !show;
    btn.setAttribute("aria-expanded", String(show));
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    open(menu.hidden);
  };
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) open(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") open(false);
  });

  for (const item of menu.querySelectorAll("[data-fmt]")) {
    const fmt = item.dataset.fmt;
    if (fmt === "print") {
      item.onclick = () => { open(false); window.print(); };
      continue;
    }
    if (fmt === "custom") {
      item.onclick = () => { open(false); selectTab("report"); };
      continue;
    }
    // A real link: the browser downloads it because the person clicked it, not because
    // script asked it to. The menu closes on the way through.
    item.addEventListener("click", () => open(false));
  }
  for (const item of menu.querySelectorAll("[data-raw]")) {
    item.addEventListener("click", () => open(false));
  }
  refreshExportLinks();
}

/**
 * Point the header menu's links at the current period, scope and display currency.
 *
 * Called whenever any of them changes, because the href has to be right *before* the click —
 * there is no handler that could fix it up afterwards.
 */
function refreshExportLinks() {
  const menu = $("exportMenu");
  if (!menu) return;
  const p = new URLSearchParams(qs().slice(1));
  const scope = scopeIds();
  if (scope.length) p.set("scope", scope.join(","));
  const cur = readStore("costCurrency", "");
  if (cur) p.set("currency", cur);
  for (const item of menu.querySelectorAll("a[data-fmt]")) {
    wireDownloadLink(item, `/api/report/${item.dataset.fmt}?${p}`);
  }
  // The raw rows are not a report format, so they have their own endpoint — but they take the
  // same period and scope, which is the whole point of offering them here. They are deliberately
  // *not* converted: raw rows are the billed record, and rewriting them would make the export
  // disagree with the invoice it is meant to reconcile against.
  const raw = new URLSearchParams(p);
  raw.delete("currency");
  for (const item of menu.querySelectorAll("a[data-raw]")) {
    wireDownloadLink(item, `/api/costs/raw.csv?${raw}`);
  }
}

function budgetCard(b, idx, { compact = false, expanded = false, snapshotChart = false, chartAside = false } = {}) {
  const aside = chartAside && !compact;
  const known = value => typeof value === "number" && Number.isFinite(value);
  const pct = known(b.current_spend) && known(b.percent_used) ? b.percent_used : null;
  const status = pct == null ? "unknown"
    : ["over", "near", "ok"].includes(b.status) ? b.status : "unknown";
  const cur = b.currency;
  const cash = (value, decimals = 2) => !known(value) ? "Not reported"
    : cur ? money(value, cur, decimals) : `${value.toLocaleString(undefined, { maximumFractionDigits: decimals })} (currency unknown)`;
  const exact = b.filter_exact !== false && b.filtered_exactly !== false && b.trend?.filtered_exactly !== false;
  const period = b.trend?.from
    ? `${b.trend.from}${b.trend.to ? ` → ${b.trend.to} (to date)` : ""}`
    : b.start && b.end ? `${b.start} → ${b.end} (configured dates)` : "Dates not reported";
  // Under budget, the track is the budget and the fill is what has been spent. Over budget,
  // the track becomes the *spend* — the solid part is the budget, the hatched part is the
  // overspend — so 199% draws as a bar that is half overrun instead of a full bar identical
  // to one sitting at exactly 100%.
  const blown = pct != null && pct > 100;
  const fill = pct == null ? 0 : blown ? (100 / pct) * 100 : Math.max(0, pct);
  const spill = blown ? 100 - fill : 0;

  const label = { over: "Over budget", near: "Near the limit", ok: "On track",
                  unknown: "Unknown status" }[status];

  return `<${compact ? "details" : "article"} class="budget ${esc(status)}${compact ? " budget-compact" : ""}${aside ? " budget-chart-aside" : ""}" id="budget-row-${esc(idx)}"${
    compact ? ` data-budget-index="${esc(idx)}"${expanded ? " open" : ""}` : ""}>
    ${compact ? '<summary class="budget-summary">' : ""}
    ${aside ? '<div class="budget-info">' : ""}
    <div class="budget-head">
      <div class="budget-id">
        <span class="budget-name">${esc(b.name || "—")}</span>
        <span class="budget-sub">${esc(b.subscription || "")}</span>
      </div>
      <div class="budget-figs">
        <span class="budget-pct">${pct == null ? "—" : `${pct}%`}</span>
        <span class="bstatus ${esc(status)}">${esc(label)}</span>
      </div>
    </div>

    <div class="bmeter" role="img"
         aria-label="${esc(`${b.name}: ${pct == null ? "no spend reported" : `${pct}% of budget used`}`)}">
      <span class="fill" style="width:${fill}%"
            title="${esc(`Budget: ${cash(b.amount)}`)}"></span>
      ${spill ? `<span class="spill" style="width:${spill}%"
            title="${esc(known(b.remaining) ? `Over by ${cash(-b.remaining)}` : "Over budget")}"></span>` : ""}
    </div>

    <div class="budget-meta">
      <span>Spent <strong>${esc(cash(b.current_spend))}</strong> · Budget
        ${esc(cash(b.amount))} ${esc(String(b.time_grain || "").toLowerCase())}</span>
      <span>${!known(b.current_spend) || !known(b.remaining) ? "Remaining not reported"
        : b.remaining >= 0
          ? `${esc(cash(b.remaining))} left`
          : `<span class="over-by">${esc(cash(-b.remaining))} over</span>`}</span>
    </div>
    <div class="budget-meta"><span>Period: ${esc(period)}</span>${
      compact ? '<span class="muted">Details · trend, alerts and filters</span>' : ""}</div>
    ${compact ? '</summary><div class="budget-details">' : ""}
    <div class="budget-meta">
      <span>${b.alerts ? `${esc(b.alerts)} notification rule(s)${(b.alerting || []).length
        ? ` · Thresholds: ${esc(b.alerting.slice(0, 4).map((t) => `${t}%`).join(", "))}${b.alerting.length > 4 ? ` +${b.alerting.length - 4}` : ""}` : ""}`
        : `<span class="no-alerts">No alerts set</span>`}</span>
    </div>

    ${b.filter ? `<div class="budget-filter sm"><span class="k">Tracks</span>
      <span class="v">${esc(b.filter)}</span>${
        exact && (b.tag_filter || []).length ? `<button type="button" class="linkish"
          data-tagsfor="${esc(idx)}">See what is in it</button>` : ""}</div>`
      : `<p class="muted note">${b.filter_raw || !exact ? "Budget filter details are not available locally." : "Tracks all spend on the subscription."}</p>`}
    ${!exact ? '<p class="muted note">This filter cannot be reproduced exactly in local cost data. Review its full scope in Azure; an exact Tags drilldown is unavailable.</p>' : ""}
    ${aside ? "</div>" : ""}

    ${b.trend || snapshotChart ? `<div class="budget-chart" data-chart="${esc(idx)}"></div>` : `<p class="muted note">${esc(b.trend_note || "Trend unavailable. Azure-reported spend above is unchanged.")}</p>`}

    <div class="budget-foot">
      <span class="muted">${b.start && b.end ? esc(`${b.start} → ${b.end}`) : ""}</span>
      <a class="linkish" href="${budgetPortalUrl(b.subscription_id)}" target="_blank"
         rel="noopener">Edit in the portal</a>
    </div>
    ${compact ? "</div>" : ""}
  </${compact ? "details" : "article"}>`;
}

/**
 * The budget trend, bound to the shell's own chart and money formatting.
 *
 * The shared renderer serves Budgets and both tag-filtered spend views through ctx.
 */
function drawBudgetChart(host, b, options = {}) {
  return drawBudgetTrend(host, b, { ...options, drawChart, money });
}

function cardHead({ name, sub, figure, figureClass = "", status, statusClass = "" }) {
  return `<div class="anom-head">
    <div class="anom-id">
      <span class="anom-key">${esc(name || "—")}</span>
      ${sub ? `<span class="anom-sub">${esc(sub)}</span>` : ""}
    </div>
    <div class="anom-figs">
      ${figure ? `<span class="anom-delta ${esc(figureClass)}">${figure}</span>` : ""}
      ${status ? `<span class="bstatus ${esc(statusClass)}">${esc(status)}</span>` : ""}
    </div>
  </div>`;
}
function wireBudgetLinks() {
  for (const b of $("tabBody").querySelectorAll("[data-goto]")) {
    b.onclick = () => selectTab(b.dataset.goto);
  }
}

/**
 * The portfolio at a glance: every budget as one slim bar each, worst first.
 *
 * The card list below is built to be opened one at a time — a trend chart, alert thresholds,
 * a portal link. That is the wrong shape for "how are we doing overall", which this answers
 * in the time it takes to scan a column: a bar that has burst is red and visibly past its own
 * end, one closing in is amber, and the rest recede. `rows` arrives already sorted worst-first,
 * so this is a slice and a redraw, not a second sort to keep in sync with the one in cost.py.
 */
// --------------------------------------------------- what was actually created

/** Azure's own Budgets blade for a subscription, for anyone who wants to see it there. */
function budgetPortalUrl(sub) {
  const scope = encodeURIComponent(`/subscriptions/${sub}`);
  return `https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/budgets/scope/${scope}`;
}

/**
 * The budget as Azure now holds it.
 *
 * Deliberately the same facts the form asked for, read back from the response rather than from
 * the inputs: a budget is a PUT, so the amount that matters is the one Azure returned. The list
 * of every budget on that subscription is underneath because the write is an upsert — seeing
 * one row where two were expected is how you find out a name collided and overwrote something.
 */
function subName(id) {
  return (allSubscriptions().find((s) => s.id === id) || {}).name || id || "—";
}

function wireShell() {
  if ($("tabSearchBtn")) {
    tabSearch = wireTabSearch({
      button: $("tabSearchBtn"), dialog: $("tabSearchDialog"), esc, getCurrent: () => current,
      navigate, readStore, writeStore, toast,
    });
  }
  // The one click that always leads home, the way a site's own logo conventionally behaves —
  // wherever a reader has wandered off to, from any tab in any group.
  const brand = $("brand");
  if (brand) {
    brand.onclick = () => selectTab("overview");
    brand.onkeydown = (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault(); // " " otherwise scrolls the page, same as it would on a real button
      selectTab("overview");
    };
  }

  // The filter bar narrows every tab, so a change to it invalidates the same things a change
  // of period does: the cache, the analysis, and whatever is on screen.
  wireFilters(() => {
    invalidateAnalysis();
    loadTabs({ preserve: true });
  }, days, periodMonth);

  periodControl = wireReportingPeriod({
    root: $("period"), input: $("reportingMonth"), caption: $("periodRange"), range: $("reportingRange"),
    getState: () => ({ days, month: periodMonth }),
    getMetadata: signal => get(`/api/reporting-period${qs()}`, { timeout: 30000, signal }),
    onChange: selected => {
      navigationIntent++;
      days = selected.days;
      periodMonth = selected.month;
      writeStore("costDays", days);
      writeStore("costPeriodMonth", periodMonth);
      invalidateAnalysis();
      loadTabs({ preserve: true }).then(() => loadFilters(days, periodMonth));
    },
  });
  wireMonthCalendar($("reportingMonth"));

  // Display currency. Only shown when there is a real choice — a lone "USD" dropdown is a
  // control that does nothing, and one of those is worse than none.
  get("/api/currency", { timeout: 15000 })
    .then((c) => {
      const sel = $("currency");
      if (!sel || (c.supported || []).length < 2) return;
      const saved = readStore("costCurrency", "");
      sel.innerHTML =
        `<option value="">As billed</option>` +
        c.supported
          .map((code) => `<option value="${esc(code)}"${code === saved ? " selected" : ""}>${
            esc(code)}</option>`)
          .join("");
      sel.hidden = false;
      sel.title = c.source ? `Rates: ${c.source}` : "";
      sel.onchange = () => {
        writeStore("costCurrency", sel.value);
        setFilterCurrency(sel.value);
        clearCache();
        invalidateAnalysis();
        refreshHeader();
        loadTabs({ preserve: true });
      };
      setFilterCurrency(saved);
    })
    .catch(() => {})
    // Its width is held until this answers either way; see `[data-pending]` in app.css.
    .finally(() => $("currency")?.removeAttribute("data-pending"));

  // Refreshing writes to the shared warehouse, so it is offered only to those allowed to do it.
  // Shares the header's identity request rather than making its own.
  whoAmI()
    .then((me) => {
      if (!me?.user?.admin) return;
      $("refresh").hidden = false;
      applyFreshness();
      $("refreshBtn").onclick = (e) => {
        e.stopPropagation();
        openRefresh($("refreshMenu").hidden);
      };
      document.addEventListener("click", (e) => {
        if (!$("refresh").contains(e.target)) openRefresh(false);
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") openRefresh(false);
      });
    })
    .catch(() => {
      /* the control simply stays hidden */
    })
    .finally(() => $("refresh")?.removeAttribute("data-pending"));

  const side = $("side");
  const setOpen = (open) => {
    document.body.classList.toggle("side-closed", !open);
    $("askToggle").setAttribute("aria-expanded", String(open));
    writeStore("costAskOpen", open);
    if (open) $("q")?.focus();
  };
  // Closed by default. It is an overlay now rather than a column, so leaving it open would
  // cover the dashboard someone just arrived to read — and the cost of opening it is one click
  // on a button that is always visible. Reopened automatically only if this reader had it open
  // when they left, which is a deliberate choice rather than a default.
  setOpen(readStore("costAskOpen", false));

  // The analyst opens the analysis over the board — the full width, because the findings are
  // arguments rather than table rows and a 400px column cannot carry one.
  mountBot($("chatFab"), () => (isAnalysisOpen() ? closeAnalysis() : openAnalysis()));
  // The analysis asks the same question the dashboard is showing — same period, same
  // subscriptions, same currency — so a finding can be checked against the tab beside it.
  setAnalysisContext(() => ({
    days,
    month: periodMonth,
    scope: scopeIds(),
    currency: readStore("costCurrency", ""),
  }));
  wireAnalysis();

  // A question asked from the analysis. `inPlace` keeps the analysis open and lets the answer
  // stream into the panel's own conversation area — the finding being asked about stays on
  // screen, which is the whole reason for asking from there. Anything else opens the side panel
  // as before.
  window.addEventListener("cl:ask", (e) => {
    const detail = e.detail;
    const q = String(typeof detail === "string" ? detail : detail?.q || "").trim();
    if (!q) return;
    if (!(typeof detail === "object" && detail.inPlace)) {
      closeAnalysis();
      setOpen(true);
    }
    askAgent(q);
  });
  // A finding names the tab its number came from; clicking it goes there.
  window.addEventListener("cl:goto-tab", (e) => navigate(e.detail));
  window.addEventListener("cloudlens:focus-synced", event => {
    const identity = `${event.detail?.started || ""}|${event.detail?.finished || ""}`;
    if (identity === lastFocusCompletion) return;
    lastFocusCompletion = identity;
    clearCache();
    invalidateAnalysis();
    refreshHeader();
    loadTabs({ preserve: true });
  });
  window.addEventListener("cl:subscriptions-ready", renderViewActions);

  // The analyst reflects the conversation. app.js announces what the model is doing rather than
  // calling in here, so the chat stays unaware of the launcher entirely.
  window.addEventListener("cl:agent", (e) => {
    if (e.detail === "done") setBotState("idle");
    else setBotState(e.detail);
  });

  $("askToggle").onclick = () => setOpen(document.body.classList.contains("side-closed"));
  $("sideClose").onclick = () => setOpen(false);
  side.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !window.matchMedia("(min-width: 1100px)").matches) setOpen(false);
  });

  wireExport();

  // The picker drives both halves of the page — and invalidates the analysis, which was
  // computed against the subscriptions that were selected when it ran.
  onScopeChange(() => {
    focusHeaderSequence++;
    focusHeaderTracker?.stop?.();
    focusHeaderTracker = null;
    setRefreshLabel("Refresh data");
    invalidateAnalysis();
    loadTabs({ preserve: true });
  });
}

wireShell();
loadTabs();
