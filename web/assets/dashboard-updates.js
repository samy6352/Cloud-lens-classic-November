/** Public Azure announcements/status and private, subscription-scoped Service Health. */
const SOURCES = [
  { id: "updates", title: "Azure Updates", endpoint: "azure-updates", audience: "Public announcements",
    url: "https://azure.microsoft.com/en-us/updates/",
    empty: "No announcements were returned by the official feed." },
  { id: "status", title: "Azure Status", endpoint: "azure-status", audience: "Public platform status",
    url: "https://azure.status.microsoft/en-us/status/",
    empty: "No active public incidents were reported by this feed. This is not a guarantee that every service or subscription is unaffected." },
  { id: "health", title: "Service Health", endpoint: "service-health", audience: "Selected subscriptions",
    url: "https://portal.azure.com/",
    empty: "No Service Health events were returned for the selected subscriptions." },
];
const OFFICIAL_HOSTS = new Set([
  "azure.microsoft.com", "www.microsoft.com", "learn.microsoft.com", "portal.azure.com",
  "azure.status.microsoft", "status.azure.com",
]);
const PAGE_SIZE = 10;

export function officialUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port
      && OFFICIAL_HOSTS.has(url.hostname.toLowerCase()) ? url.href : null;
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    return null;
  }
}

export function needsAttention(item) {
  return /\b(retir\w*|deprecat\w*|security|action.required|breaking|critical|end.of.support)\b/i
    .test(`${item.category || ""} ${item.title || ""} ${item.customer_impact || ""}`);
}

function dimensionValues(item, field) {
  const raw = item[field];
  return (Array.isArray(raw) ? raw : typeof raw === "string" ? [raw] : [])
    .filter(value => typeof value === "string")
    .map(value => value.trim().replace(/\s+/g, " ")).filter(Boolean);
}

function dimensionKey(value, field) {
  const key = value.trim().replace(/\s+/g, " ").toLowerCase();
  return field === "regions" ? key.replace(/[\s_-]/g, "") : key;
}

function matchesDimension(item, field, selected) {
  if (selected === "") return true;
  const values = dimensionValues(item, field);
  if (selected === null) return values.length === 0;
  return values.some(value => dimensionKey(value, field) === dimensionKey(selected, field));
}

export function feedDimensionOptions(items, field, selected = "") {
  const options = new Map();
  let missing = 0;
  for (const item of items) {
    const values = dimensionValues(item, field), seen = new Set();
    if (!values.length) missing++;
    for (const value of values) {
      const key = dimensionKey(value, field);
      const option = options.get(key) || { value, label: value, count: 0 };
      if (!seen.has(key)) option.count++;
      if (field === "regions" && !/\s/.test(option.label) && /\s/.test(value)) {
        option.value = option.label = value;
      }
      options.set(key, option);
      seen.add(key);
    }
  }
  if (selected && !options.has(dimensionKey(selected, field))) {
    options.set(dimensionKey(selected, field), { value: selected, label: selected, count: 0 });
  }
  const result = [...options.values()].sort((a, b) => a.label.localeCompare(b.label));
  if (missing || selected === null) result.push({ value: null, label: "Not specified", count: missing });
  return result;
}

export function filterUpdates(items, {
  query = "", category = "", status = "", attention = false, service = "", region = "",
} = {}) {
  const needle = query.trim().toLowerCase();
  return items.filter(item => (!category || (item.category || item.type || "Other") === category)
    && (!status || item.status === status)
    && matchesDimension(item, "services", service)
    && matchesDimension(item, "regions", region)
    && (!attention || needsAttention(item))
    && (!needle || [item.title, item.summary, item.category, item.status, item.id, item.tracking_id,
      item.actions, item.customer_impact,
      ...dimensionValues(item, "services"), ...dimensionValues(item, "regions")]
      .join(" ").toLowerCase().includes(needle)));
}

export function createTabs(ctx) {
  const { $, esc, get, setContent, stableKey, portalLinkMarkup } = ctx;
  const state = Object.fromEntries(SOURCES.map(source => [source.id, {
    data: null, error: null, loading: false, interrupted: false, query: "", category: "", status: "", attention: false,
    service: "", region: "",
    page: 0, expanded: false, opens: new Set(), controller: null, searchTimer: null, waitingForScope: false,
  }]));
  let generation = 0;
  let clock = null;
  let currentToken = null;
  const active = (token, version = generation) => version === generation && token === ctx.loadToken;
  const date = value => {
    const parsed = value ? new Date(value) : null;
    return parsed && Number.isFinite(parsed.getTime())
      ? parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "Not provided";
  };
  const identity = (source, item) => `${source.id}-${stableKey(item.id || `${item.title}|${item.published_at}|${item.updated_at}`)}`;
  const count = source => {
    const current = state[source.id];
    if (!current.data || (current.data.status === "error" && !current.data.items.length)) {
      return current.error ? "Unavailable" : "Loading...";
    }
    const total = current.data.total;
    return Number.isInteger(total) && total >= current.data.items.length
      ? total : current.data.items.length;
  };
  const label = source => `${source.title} (${count(source)})`;
  const sourceLink = source => officialUrl(state[source.id].data?.source?.url) || source.url;
  const categoryLabel = value => String(value).replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ");

  function freshness(source) {
    const current = state[source.id];
    const info = current.data?.freshness;
    if (!info?.fetched_at) return current.error ? "No successful retrieval available." : "Waiting for the first retrieval.";
    const fetched = Date.parse(info.fetched_at);
    const age = Number.isFinite(fetched) ? Math.max(0, (Date.now() - fetched) / 1000) : info.age_seconds;
    const stale = current.error || info.stale
      || (Number.isFinite(info.max_age_seconds) && age >= info.max_age_seconds);
    const ageText = age == null ? "" : age < 60 ? `${Math.floor(age)}s ago`
      : age < 3600 ? `${Math.floor(age / 60)}m ago` : `${Math.floor(age / 3600)}h ago`;
    return `${stale ? "Stale" : info.cached ? "Cached" : "Fetched"} · ${date(info.fetched_at)}${ageText ? ` · ${ageText}` : ""}`;
  }

  function updateClock() {
    for (const source of SOURCES) {
      const target = $(`azure-${source.id}-freshness`);
      if (target) target.textContent = freshness(source);
    }
  }

  function expandSource(source, expanded) {
    state[source.id].expanded = expanded;
    const toggle = $(`azure-${source.id}-toggle`);
    const panel = $(`azure-${source.id}-content`);
    if (toggle && panel) {
      toggle.setAttribute("aria-expanded", String(expanded));
      panel.hidden = !expanded;
    }
  }

  function controls(source) {
    const current = state[source.id];
    const items = current.data?.items || [];
    const categories = [...new Set(items.map(item => item.category || item.type || "Other"))].sort();
    const statuses = [...new Set(items.map(item => item.status).filter(Boolean))].sort();
    if (current.category && !categories.includes(current.category)) categories.push(current.category);
    if (current.status && !statuses.includes(current.status)) statuses.push(current.status);
    const field = source.id === "health" ? "regions" : "services";
    const selection = source.id === "health" ? "region" : "service";
    const heading = source.id === "health" ? "Region" : "Service type";
    const options = feedDimensionOptions(items, field, current[selection]);
    const filtered = current.query || current.category || current.status || current.attention ||
      current.service !== "" || current.region !== "";
    return `<div class="azure-feed-controls">
      <label class="azure-filter azure-filter-search"><span>Search</span>
        <input type="search" id="azure-${source.id}-search" value="${esc(current.query)}"
          placeholder="Search ${esc(source.title.toLowerCase())}, products or regions"
          aria-label="Search ${esc(source.title)}" maxlength="200"></label>
      <label class="azure-filter azure-dimension-filter"><span>${heading}</span>
        <select id="azure-${source.id}-${selection}" aria-label="Filter ${esc(source.title)} by ${heading.toLowerCase()}">
          <option value="">All ${source.id === "health" ? "regions" : "service types"}</option>
          ${options.map(option => `<option value="${esc(JSON.stringify(option.value))}"${
            option.value === null ? current[selection] === null ? " selected" : "" :
              current[selection] && dimensionKey(option.value, field) === dimensionKey(current[selection], field) ? " selected" : ""
          }>${esc(option.label)} (${option.count})</option>`).join("")}
        </select>
      </label>
      <label class="azure-filter"><span>Category</span>
        <select id="azure-${source.id}-category" aria-label="Filter ${esc(source.title)} by category">
          <option value="">All categories</option>
          ${categories.map(category => `<option value="${esc(category)}"${category === current.category ? " selected" : ""}>${esc(categoryLabel(category))}</option>`).join("")}
        </select>
      </label>
      ${source.id === "health" ? `<label class="azure-filter"><span>Status</span>
        <select id="azure-health-status" aria-label="Filter Service Health by status">
          <option value="">All statuses</option>
          ${statuses.map(status => `<option value="${esc(status)}"${status === current.status ? " selected" : ""}>${esc(status)}</option>`).join("")}
        </select>
      </label>` : ""}
      ${source.id === "updates" ? `<label class="azure-attention"><input type="checkbox" id="azure-attention"${current.attention ? " checked" : ""}>Customer attention</label>` : ""}
    ${filtered ? `<button type="button" class="ghost" id="azure-${source.id}-clear">Clear filters</button>` : ""}
    </div>`;
  }

  function itemMarkup(source, item) {
    const id = identity(source, item);
    const link = officialUrl(item.url);
    const category = item.category || item.type || "Other";
    const products = (item.services || []).join(", ");
    const regions = (item.regions || []).join(", ");
    const resources = Array.isArray(item.resources) ? item.resources : [];
    const subscriptions = (item.subscription_ids || []).map(id =>
      ctx.allSubscriptions().find(sub => sub.id.toLowerCase() === id.toLowerCase())?.name || id);
    return `<details class="azure-feed-item${needsAttention(item) ? " azure-item-attention" : ""}"
      data-update-id="${id}" data-state-key="${id}" ${state[source.id].opens.has(id) ? "open" : ""}>
      <summary><span class="azure-item-meta">
        <span class="azure-category">${esc(categoryLabel(category))}</span>
        ${item.status ? `<span>${esc(item.status)}</span>` : ""}
        ${item.level ? `<span>${esc(item.level)}</span>` : ""}
        <time>${esc(date(item.updated_at || item.published_at))}</time>
      </span><strong>${esc(item.title || "Untitled Azure notice")}</strong>
      ${products ? `<span class="azure-item-products">${esc(products)}</span>` : ""}
      </summary>
      <div class="azure-item-body">
        <p>${esc(item.summary || "No summary was supplied. Open the official source for details.")}</p>
        ${item.tracking_id ? `<p><strong>Tracking ID:</strong> <code>${esc(item.tracking_id)}</code></p>` : ""}
        ${item.published_at ? `<p><strong>${source.id === "health" ? "Impact start" : "Published"}:</strong> ${esc(date(item.published_at))}</p>` : ""}
        ${item.updated_at ? `<p><strong>Updated:</strong> ${esc(date(item.updated_at))}</p>` : ""}
        ${item.impact_end_at ? `<p><strong>Impact end / mitigation:</strong> ${esc(date(item.impact_end_at))}</p>` : ""}
        ${item.customer_impact ? `<p class="azure-impact"><strong>Customer relevance:</strong> ${esc(item.customer_impact)}</p>` : ""}
        ${item.actions ? `<p><strong>Recommended actions:</strong> ${esc(item.actions)}</p>` : ""}
        ${regions ? `<p><strong>Regions:</strong> ${esc(regions)}</p>` : ""}
        ${subscriptions.length ? `<p><strong>Subscriptions:</strong> ${esc(subscriptions.join(", "))}</p>` : ""}
        ${resources.length ? `<div class="azure-impacted"><strong>Impacted resources (${esc(item.resource_count ?? resources.length)})</strong>
          <ul>${resources.map(resource => `<li>${portalLinkMarkup(resource, ctx.portalScopeIds(),
            resource.name || resource.id || "Resource")}${resource.type ? ` <span class="muted">${esc(resource.type)}</span>` : ""}</li>`).join("")}</ul>
          ${item.resource_count > resources.length ? '<p class="muted">The source has more impacted resources than are included in this response.</p>' : ""}
        </div>` : ""}
        ${source.id === "health" && !resources.length ? '<p class="muted">No resource-level impact details were returned. Review the subscription-level notice; this does not establish that no resources are affected.</p>' : ""}
        ${item.resources_truncated ? '<p class="muted">The resource list may be incomplete. Open Service Health for the full details.</p>' : ""}
        <div class="azure-item-links">
          ${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open official details ↗</a>` : ""}
          <a href="${esc(sourceLink(source))}" target="_blank" rel="noopener noreferrer">Open ${esc(source.title)} source ↗</a>
        </div>
      </div></details>`;
  }

  function renderSource(source, token) {
    const host = $(`azure-${source.id}`);
    if (!host || !active(token)) return;
    const current = state[source.id];
    const items = current.data?.items || [];
    const visible = filterUpdates(items, current);
    const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
    current.page = Math.min(current.page, pages - 1);
    const filtered = current.query || current.category || current.status || current.attention ||
      current.service !== "" || current.region !== "";
    const stateText = current.loading ? items.length ? "Refreshing; previous results remain available." : "Loading this source..."
      : current.error ? "Source unavailable" : `${items.length} item${items.length === 1 ? "" : "s"} retrieved`;
    setContent(host, `<div class="azure-source-head">
      <h3><button type="button" class="azure-source-toggle" id="azure-${source.id}-toggle"
        aria-expanded="${current.expanded}" aria-controls="azure-${source.id}-content">
        <span class="azure-source-title"><span class="view-kicker">${esc(source.audience)}</span>
          <span>${esc(source.title)}</span></span>
        <span class="azure-source-count">${esc(count(source))}</span>
        <span class="azure-source-chevron" aria-hidden="true"></span>
      </button></h3>
      <button type="button" class="ghost" data-refresh-source="${source.id}"${current.loading ? " disabled" : ""}>${current.loading ? "Refreshing..." : "Refresh"}</button>
      </div><div class="azure-source-meta">
        <span role="status">${esc(stateText)}</span>
        <span class="muted" id="azure-${source.id}-freshness">${esc(freshness(source))}</span>
      </div>
      ${current.error ? `<p class="azure-source-warning">${esc(current.error)}${items.length ? " Showing the last retrieved results, not a current status." : " This does not mean there are no updates or incidents."}</p>` : ""}
      ${current.data?.truncated ? '<p class="azure-source-warning">This response is limited. Open the official source for the complete listing.</p>' : ""}
      <div id="azure-${source.id}-content"${current.expanded ? "" : " hidden"}>
      ${current.data?.notice ? `<p class="azure-source-note">${esc(current.data.notice)}</p>` : ""}
      ${controls(source)}
      <p class="azure-source-note">${source.id === "health"
        ? "Regions are source-reported. Global and unspecified notices are not assigned to individual regions."
        : "Service types use the official feed's product tags. Untagged notices are listed as Not specified."}</p>
      ${source.id === "updates" ? '<p class="azure-source-note">Attention flags are keyword hints, not confirmed subscription impact.</p>' : ""}
      <div class="azure-feed-list">${visible.slice(current.page * PAGE_SIZE, (current.page + 1) * PAGE_SIZE)
        .map(item => itemMarkup(source, item)).join("")}
      ${!visible.length ? `<p class="muted empty">${esc(current.loading ? "Waiting for this source; the other sections load independently."
        : filtered ? "No retrieved items match these filters."
        : current.error ? "No previously retrieved items are available." : source.empty)}</p>` : ""}</div>
      <div class="azure-source-foot">
        <span>${visible.length ? `${current.page * PAGE_SIZE + 1}-${Math.min((current.page + 1) * PAGE_SIZE, visible.length)} of ${visible.length}` : "0 displayed"}</span>
        ${pages > 1 ? `<div><button class="ghost" data-feed-page="-1"${current.page === 0 ? " disabled" : ""}>Previous</button>
          <button class="ghost" data-feed-page="1"${current.page === pages - 1 ? " disabled" : ""}>Next</button></div>` : ""}
        <a href="${esc(sourceLink(source))}" target="_blank" rel="noopener noreferrer">Official source ↗</a>
      </div></div>`);
    host.querySelector(".azure-source-toggle").onclick = () => expandSource(source, !current.expanded);
    host.querySelector("[data-refresh-source]").onclick = () => loadSource(source, token, true);
    const search = host.querySelector("input[type=search]");
    search.oninput = () => {
      current.query = search.value;
      current.page = 0;
      clearTimeout(current.searchTimer);
      current.searchTimer = setTimeout(() => { if (active(token)) renderSource(source, token); }, 120);
    };
    host.querySelector(`#azure-${source.id}-category`).onchange = event => {
      current.category = event.target.value; current.page = 0; renderSource(source, token);
    };
    const selection = source.id === "health" ? "region" : "service";
    host.querySelector(`#azure-${source.id}-${selection}`).onchange = event => {
      // JSON distinguishes the missing-value choice from a literal service named "null".
      current[selection] = event.target.value === "" ? "" : JSON.parse(event.target.value);
      current.page = 0;
      renderSource(source, token);
    };
    host.querySelector(`#azure-${source.id}-clear`)?.addEventListener("click", () => {
      clearTimeout(current.searchTimer);
      Object.assign(current, { query: "", category: "", status: "", attention: false,
        service: "", region: "", page: 0 });
      for (const control of host.querySelectorAll(".azure-feed-controls input,.azure-feed-controls select")) {
        if (control.type === "checkbox") control.checked = false;
        else control.value = "";
      }
      renderSource(source, token);
      host.querySelector("input[type=search]").focus({ preventScroll: true });
    });
    host.querySelector("#azure-health-status")?.addEventListener("change", event => {
      current.status = event.target.value; current.page = 0; renderSource(source, token);
    });
    host.querySelector("#azure-attention")?.addEventListener("change", event => {
      current.attention = event.target.checked; current.page = 0; renderSource(source, token);
    });
    for (const button of host.querySelectorAll("[data-feed-page]")) {
      button.onclick = () => { current.page += Number(button.dataset.feedPage); renderSource(source, token); };
    }
    const jump = $(`azure-jump-${source.id}`);
    if (jump) jump.textContent = label(source);
    updateRefreshAll();
  }

  function updateRefreshAll() {
    const button = $("azureRefreshAll");
    if (button) button.disabled = SOURCES.some(source => state[source.id].loading);
  }

  async function loadSource(source, token, force = false) {
    const current = state[source.id];
    current.controller?.abort();
    const controller = new AbortController();
    current.controller = controller;
    const version = generation;
    current.interrupted = false;
    current.loading = true;
    current.error = null;
    renderSource(source, token);
    try {
      const query = new URLSearchParams();
      if (source.id === "health") {
        const scopes = ctx.portalScopeIds();
        current.waitingForScope = !scopes.length;
        if (!scopes.length) throw new Error("No readable subscriptions are available yet. Select subscriptions and refresh Service Health.");
        query.set("scope", scopes.join(","));
      }
      if (force) query.set("refresh", "true");
      const url = `/api/dashboard/${source.endpoint}${query.size ? `?${query}` : ""}`;
      const data = await get(url, { signal: controller.signal, timeout: source.id === "health" ? 60000 : 30000 });
      if (!active(token, version) || controller.signal.aborted) return;
      if (!Array.isArray(data.items) || !["ok", "error"].includes(data.status)) {
        throw new Error("The source returned an invalid response.");
      }
      current.data = data.status === "error" && !data.items.length && current.data?.items?.length
        ? { ...current.data, freshness: { ...current.data.freshness, stale: true } } : data;
      current.error = data.status === "error" ? data.error || "The latest retrieval failed." : null;
    } catch (error) {
      if (!active(token, version) || controller.signal.aborted) return;
      current.error = error?.name === "AbortError" ? "This source timed out. Retry this section."
        : error?.message || "Could not retrieve this source.";
    } finally {
      if (active(token, version) && !controller.signal.aborted) {
        current.loading = false;
        renderSource(source, token);
      }
    }
  }

  function start(token, resume = false) {
    currentToken = token;
    window.removeEventListener("cl:subscriptions-ready", subscriptionsReady);
    window.addEventListener("cl:subscriptions-ready", subscriptionsReady);
    clearInterval(clock);
    clock = setInterval(() => {
      if (active(token)) updateClock();
    }, 15000);
    updateClock();
    return Promise.all(SOURCES
      .filter(source => !resume || state[source.id].interrupted)
      .map(source => loadSource(source, token)));
  }

  function subscriptionsReady() {
    if (currentToken != null && active(currentToken) && state.health.waitingForScope && ctx.portalScopeIds().length) {
      loadSource(SOURCES.find(source => source.id === "health"), currentToken);
    }
  }

  async function renderAzureUpdates(token) {
    setContent($("tabBody"), `<section class="azure-updates-intro">
      <p class="muted">Azure sources. Only Service Health follows subscription selection; cost filters do not apply. Results are kept until refresh.</p>
      <details class="view-help" data-state-key="azure-source-scope"><summary>Source scope</summary>
        <p>Product updates and Azure Status are global. Counts cover the retrieved listing, not just the displayed page or search results.</p>
      </details>
      <div class="azure-jump-links">${SOURCES.map(source => `<button type="button" class="ghost" data-jump="${source.id}" id="azure-jump-${source.id}">${esc(label(source))}</button>`).join("")}
        <button type="button" class="primary" id="azureRefreshAll">Refresh all sources</button></div>
      </section>${SOURCES.map(source => `<section class="card azure-source" id="azure-${source.id}" aria-label="${esc(source.title)}"></section>`).join("")}`);
    for (const source of SOURCES) {
      const host = $(`azure-${source.id}`);
      host.addEventListener("toggle", event => {
        const id = event.target.dataset.updateId;
        if (!id) return;
        if (event.target.open) state[source.id].opens.add(id);
        else state[source.id].opens.delete(id);
      }, true);
      renderSource(source, token);
    }
    for (const button of $("tabBody").querySelectorAll("[data-jump]")) {
      button.onclick = () => {
        const source = SOURCES.find(source => source.id === button.dataset.jump);
        expandSource(source, true);
        $(`azure-${source.id}`).scrollIntoView({ block: "start" });
        $(`azure-${source.id}-toggle`).focus({ preventScroll: true });
      };
    }
    $("azureRefreshAll").onclick = () => Promise.all(SOURCES.map(source => loadSource(source, token, true)));
    await start(token);
  }

  function stop() {
    generation++;
    currentToken = null;
    window.removeEventListener("cl:subscriptions-ready", subscriptionsReady);
    clearInterval(clock);
    clock = null;
    for (const current of Object.values(state)) {
      current.interrupted ||= current.loading;
      current.controller?.abort();
      clearTimeout(current.searchTimer);
      current.loading = false;
    }
  }

  return {
    renderAzureUpdates, stop,
    resume(token, tab) { if (tab === "azure-updates") start(token, true); },
  };
}
