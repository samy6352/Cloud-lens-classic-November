/** Lazy reports views. Dependencies are supplied by the shell; no reverse imports. */
export function createTabs(ctx) {
  const { $, allSubscriptions, cached, days, esc, get, key, kpiRow, money, moneyHtml, qs, readStore, scopeIds, setContent, table, toast, whoAmI, wireDownloadLink, wireTables, writeStore } = ctx;

// -------------------------------------------------------------- report builder
// What the Export data tab has selected. Kept outside the render so switching away and back does
// not silently discard a selection somebody has just spent a minute making.
let reportChoice = readStore(key("costReport"), null);
let scheduleChoice = null;
const scheduleCadences = new Map();
const lastImports = new Map();
const focusTargets = new Map();
let settingsData = null;
let settingsToken = null;
let settingsSequence = 0;
let settingsBusy = false;
let settingsNotice = "";
let focusTimer = null;
let focusController = null;
let focusGeneration = 0;
let focusPending = false;
let focusNotify = false;
let focusCompleted = "";
let paintSettingsControls = () => {};
let headerRefresh = null;
let headerStarting = false;
let workflowTimer = null;
let workflowGeneration = 0;
let workflowCleanup = () => {};
const workflowJobs = new Map();
const workflowNotified = new Set();
let permissionController = null;
let permissionGeneration = 0;
let ingestSelection = null;
let ingestCatalog = [];
let ingestDraft = null;
let ingestTab = "first";
// The picker is a menu, and ticking a box re-renders the card to read status for the new
// scope. Both survive that rebuild here, or the menu would shut after every single tick.
let ingestMenuOpen = false;
let ingestQuery = "";
const ingestIds = () => ingestSelection ? [...ingestSelection].sort() : [];
let ingestDismissalWired = false;

/* Bound once for the life of the page, not per render: the card is rebuilt on every scope
   change, and re-binding here would add a listener each time and never remove one. Both
   handlers read the live DOM, so they do nothing once the card is gone. */
function wireIngestDismissal() {
  if (ingestDismissalWired || typeof document.addEventListener !== "function") return;
  ingestDismissalWired = true;
  const close = () => {
    ingestMenuOpen = false;
    const menu = document.getElementById("cdSubsMenu");
    if (!menu) return;
    menu.hidden = true;
    document.getElementById("cdSubsToggle")?.setAttribute("aria-expanded", "false");
  };
  document.addEventListener("click", () => close());
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !ingestMenuOpen) return;
    const toggle = document.getElementById("cdSubsToggle");
    close();
    toggle?.focus();
  });
}
const ingestKey = () => ingestIds().join(",");

function permissionPanel() {
  return `<details class="view-help" id="pPanel" data-state-key="permission-readiness">
    <summary>Access readiness and required permissions</summary>
    <p class="muted note">Read-only declarations for the selected subscriptions.
      This does not grant roles, run exports, change alerts or prove storage-network access.</p>
    <div class="build-bar">
      <button type="button" class="ghost" id="pInspect">Inspect my access</button>
      <button type="button" class="ghost" id="pWorkload" hidden>Inspect background identity</button>
    </div>
    <p id="pStatus" class="muted note" role="status" aria-live="polite"></p>
    <div id="pResult"></div>
  </details>`;
}

function wirePermissions(scopeReader = scopeIds) {
  const host = $("pResult"), status = $("pStatus"), mine = $("pInspect"), workload = $("pWorkload");
  if (!host || !status || !mine || !workload) return;
  const scope = [...scopeReader()].sort().join(",");
  const local = scopeReader === ingestIds;
  const mounted = () => $("pResult") === host && [...scopeReader()].sort().join(",") === scope;
  mine.disabled = workload.disabled = local && !scope;
  const panel = $("pPanel");
  let roleRead = false;
  const readRole = async () => {
    if (!panel?.open || typeof whoAmI !== "function" || roleRead) return;
    roleRead = true;
    try {
      const me = await whoAmI();
      if (mounted()) workload.hidden = !me?.user?.admin;
    } catch {
      if (!mounted()) return;
      roleRead = false;
      workload.hidden = true;
      status.textContent = "Could not read your account role. Caller diagnostics remain available.";
    }
  };
  if (panel) panel.ontoggle = readRole;
  void readRole();
  const inspect = async (mode) => {
    if (!mounted() || (local && !scope)) return;
    permissionController?.abort();
    const controller = new AbortController();
    permissionController = controller;
    const generation = ++permissionGeneration, token = ctx.loadToken;
    const current = () => mounted() && token === ctx.loadToken && generation === permissionGeneration;
    mine.disabled = workload.disabled = true;
    status.textContent = `Reading ${mode === "workload" ? "background-identity" : "caller"} permissions; any previous result remains labelled below...`;
    try {
      const query = new URLSearchParams({ scope, mode, refresh: "true" });
      const data = await get(`/api/capabilities?${query}`, { signal: controller.signal, timeout: 20000 });
      if (!current()) return;
      if (!Array.isArray(data.capabilities)) throw new Error("The service returned no capability list.");
      const title = mode === "workload" ? "Background identity declarations" : "Caller access declarations";
      setContent(host, table(title, data.capabilities, [
        { label: "Capability", value: (row) => row.feature, cell: (row) => esc(row.feature) },
        { label: "Status", value: (row) => row.status,
          cell: (row) => esc(String(row.status || "unknown").replaceAll("-", " ")) },
        { label: "Principal", cell: (row) => esc(row.principal?.id || "Not resolved") },
        { label: "Required role", cell: (row) => esc(row.required_role || "Review configuration") },
        { label: "Scope", cell: (row) => `<code>${esc(row.scope || "Not configured")}</code>` },
        { label: "Reason and next step",
          cell: (row) => `${esc(row.reason || "")}<p class="muted note">${esc(row.next_step || "")}</p>` },
      ], "Declared access is not proof that a write or data-plane operation will succeed."));
      wireTables(host);
      const instructions = data.capabilities.map(row =>
        `${row.feature}: ${row.status || "unknown"}\nPrincipal: ${row.principal?.id || "Not resolved"}\n`
        + `Required role: ${row.required_role || "Review configuration"}\nScope: ${row.scope || "Not configured"}\n`
        + `${row.reason || ""}\nNext step: ${row.next_step || ""}`).join("\n\n");
      const links = [...scopeReader()].filter(id => /^[0-9a-f-]{36}$/i.test(id)).map(id =>
        `<a target="_blank" rel="noopener noreferrer" href="https://portal.azure.com/#resource/subscriptions/${encodeURIComponent(id)}/overview">Open subscription ${esc(id)} in Azure</a>`).join("<br>");
      const help = document.createElement?.("div");
      if (help) {
        help.innerHTML = `<p class="muted note">Unknown is not denied. An administrator must verify the listed principal and narrow scope. No roles are granted here.</p>${links}
          <button type="button" class="ghost" id="pCopy">Copy administrator instructions</button>
          <details class="view-help"><summary>Administrator instructions</summary><pre>${esc(instructions)}</pre></details>`;
        host.appendChild(help);
        $("pCopy").onclick = async () => {
          try { await navigator.clipboard.writeText(instructions); status.textContent = "Administrator instructions copied. No permissions were changed."; }
          catch { status.textContent = "Clipboard unavailable. Expand Administrator instructions and copy the text."; }
        };
      }
      const unknown = data.capabilities.filter((row) => ["unknown", "denied"].includes(row.status)).length;
      status.textContent = `${title} read at ${data.generated_at || "an unreported time"}. `
        + `${unknown} capability item(s) need attention. No permissions were changed.`
        + (data.read_only ? " This deployment is read-only." : "");
    } catch (error) {
      if (!current() || error?.name === "AbortError") return;
      status.textContent = `Could not inspect permissions: ${error?.message || "request failed"}. `
        + "The previous result, if any, has not been updated.";
    } finally {
      if (permissionController === controller) permissionController = null;
      if (generation === permissionGeneration) mine.disabled = workload.disabled = local && !scope;
    }
  };
  mine.onclick = () => inspect("caller");
  workload.onclick = () => inspect("workload");
}

async function renderReport(token) {
  // The raw-data export below needs to know what the warehouse holds and which snapshots were
  // archived; both are fetched alongside the builder's options rather than after them, so the
  // tab still costs one round trip.
  const [opts, arch, wh] = await Promise.all([
    cached(key("reportOptions"), () => get(`/api/report/options${qs()}`, { timeout: 30000 })),
    get("/api/archive?limit=200", { timeout: 30000 }).catch(() => ({})),
    get("/api/warehouse", { timeout: 20000 }).catch(() => ({})),
  ]);
  if (token !== ctx.loadToken) return;

  const areaIds = opts.areas.map((a) => a.id);
  // Default to everything from the warehouse and nothing live: live datasets call Azure and
  // turn a one-second export into a slow one, so they should be a deliberate choice.
  const fresh = () => ({
    v: 2,
    summary: true,
    sections: areaIds,
    blocks: [...opts.blocks],
    live: [],
  });
  const c = reportChoice || fresh();
  // An area that has since dropped out of scope must not linger in the selection.
  c.sections = c.sections.filter((id) => areaIds.includes(id));
  // Before v2 a mislabelled "Select all" could clear the whole list on the first click and
  // persist it, so every later export came out near-empty. Repair that once; after the marker
  // is written a deliberately narrow selection is left alone.
  if (c.v !== 2) {
    if (areaIds.length && !c.sections.length && !c.live.length) {
      c.sections = areaIds;
      if (!c.blocks.length) c.blocks = [...opts.blocks];
    }
    c.v = 2;
  }

  const BLOCK_LABEL = {
    trend: "Daily trend",
    services: "By service",
    regions: "By region",
    resources: "Top resources",
  };

  const checkbox = (group, id, label, sub, checked) => `
    <label class="opt">
      <input type="checkbox" data-group="${group}" value="${esc(id)}" ${checked ? "checked" : ""} />
      <span class="opt-text"><span class="opt-label">${esc(label)}</span>
      ${sub ? `<span class="opt-sub">${esc(sub)}</span>` : ""}</span>
    </label>`;

  setContent($("tabBody"), `
    <p class="muted note report-lede portal-ribbon">Reports use the selected period and subscriptions. Choose content and download.</p>

    <div class="grid-2">
      <section class="card">
        <h3>Cost areas <button class="link" data-all="sections">Select all</button></h3>
        <p class="muted note">Source: warehouse.</p>
        <div class="opt-list">
          ${checkbox("summary", "summary", "Summary",
                     "Totals and the by-area breakdown", c.summary)}
          ${opts.areas
            .map((a) =>
              checkbox("sections", a.id, a.label, money(a.cost, opts.currency),
                       c.sections.includes(a.id))
            )
            .join("")}
        </div>
      </section>

      <section class="card">
        <h3>Area details</h3>
        <p class="muted note">For each selected area.</p>
        <div class="opt-list">
          ${opts.blocks
            .map((b) => checkbox("blocks", b, BLOCK_LABEL[b] || b, "", c.blocks.includes(b)))
            .join("")}
        </div>

        <h3 style="margin-top:18px">Live findings
          <button class="link" data-all="live">Select all</button></h3>
        <p class="muted note">Queries Azure directly; exports take longer.</p>
        <div class="opt-list">
          ${opts.live
            .map((l) => checkbox("live", l.id, l.label, "", c.live.includes(l.id)))
            .join("")}
        </div>
      </section>
    </div>

    <div class="build-bar">
      <!-- The format lives on the download control rather than in a panel of its own. A whole
           section repeating the choices already in the header Export menu was duplication;
           attaching them here keeps the choice where the action is. The items are real links
           for the same reason the header's are: a scripted download is an *automatic* one, and
           managed browsers block those silently. -->
      <div class="menu-wrap" id="buildWrap">
        <button class="primary" id="buildBtn" aria-haspopup="menu" aria-expanded="false"
                aria-controls="buildMenu">Download report<span class="caret" aria-hidden="true">▾</span></button>
        <div class="drop" id="buildMenu" role="menu" hidden>
          <div class="head">Download this selection</div>
          ${opts.formats
            .map(
              (f) => `<a data-fmt="${esc(f.id)}" role="menuitem" href="#">${esc(f.label)}</a>`
            )
            .join("")}
        </div>
      </div>
      <span class="muted" id="buildNote"></span>
    </div>

    <!-- The second half of this tab, and it has to look like one. The builder above produces a
         *report* — chosen areas, chosen detail, formatted to be read. This produces the raw
         rows behind it: a different question, a different audience, and a different set of
         controls that must not read as more options for the Download report button sitting
         directly above them. -->
    <hr class="tab-split">

    <section class="card section-lead">
      <div class="tagpick-head">
        <h3>Custom cost export</h3>
        <span class="muted note">
          <button type="button" class="linkish" id="xAll">All</button> ·
          <button type="button" class="linkish" id="xNone">None</button>
        </span>
      </div>
      <p class="muted note">Uses subscriptions selected below, independently of the header. Saves a separate file; the daily archive is unchanged.</p>

      <div class="subpick" id="xSubs">
        ${allSubscriptions().map((s) => `<label class="subpick-item">
          <input type="checkbox" value="${esc(s.id)}" ${scopeIds().length === 0
            || scopeIds().includes(s.id) ? "checked" : ""}>
          <span class="subpick-name">${esc(s.name || s.id)}</span>
        </label>`).join("")}
      </div>

      <div class="budget-grid">
        <label class="bfield"><span>Cost period from</span>
          <input id="xFrom" type="date" value="${esc(wh.from || "")}"
                 min="${esc(wh.from || "")}" max="${esc(wh.to || "")}"></label>
        <label class="bfield"><span>to</span>
          <input id="xTo" type="date" value="${esc(wh.to || "")}"
                 min="${esc(wh.from || "")}" max="${esc(wh.to || "")}"></label>
        <label class="bfield"><span>Data as read on</span>
          <select id="xSource">
            <option value="">Current warehouse</option>
            ${snapshotOptions(arch)}
          </select>
        </label>
        <label class="bfield"><span>Format</span>
          <select id="xFmt">
            <option value="csv">CSV — same shape a FOCUS export writes</option>
            <option value="parquet">Parquet — smaller, for tooling</option>
          </select>
        </label>
        <label class="bfield"><span>Label</span>
          <input id="xLabel" type="text" maxlength="40" placeholder="e.g. finance-review"
                 autocomplete="off" spellcheck="false"></label>
      </div>

      <p class="muted note">Warehouse: ${wh.from ? `${esc(wh.from)} → ${esc(wh.to)}` : "no data"}.
        Azure may restate costs; totals can differ by snapshot date.</p>
      <div class="build-bar">
        <a class="btn primary" id="xDownload" role="button">Download</a>
        <button class="ghost" id="xExport">Save to storage</button>
        <span class="muted" id="xNote"></span>
      </div>
      <details class="view-help" data-state-key="custom-export-about">
        <summary>About this view</summary>
        <p>The daily archive covers the whole estate for comparisons across snapshot dates.
          A custom export contains only the subscriptions selected here, saved as its own
          labelled file without overwriting that archive.</p>
        <p>The cost period selects days of spend. “Data as read on” selects the snapshot:
          Azure can restate those costs on later days. Use today's warehouse unless reproducing
          an earlier reported figure.</p>
        <p>CSV uses the same columns and order as a FOCUS export for interchange with Azure-produced files.</p>
      </details>
    </section>`);

  const save = () => {
    reportChoice = c;
    writeStore(key("costReport"), c);
    updateNote();
    syncAll();
  };

  const allOf = (g) => (g === "sections" ? areaIds : opts.live.map((l) => l.id));

  // The label has to describe what the next click does, otherwise the first click on a
  // fully-selected list silently clears it.
  const syncAll = () => {
    for (const b of $("tabBody").querySelectorAll("[data-all]")) {
      const all = allOf(b.dataset.all);
      b.disabled = !all.length;
      b.textContent = all.length && c[b.dataset.all].length === all.length
        ? "Clear all" : "Select all";
    }
  };

  const updateNote = () => {
    const parts = [];
    if (c.summary) parts.push("summary");
    parts.push(`${c.sections.length} area${c.sections.length === 1 ? "" : "s"}`);
    if (c.live.length) parts.push(`${c.live.length} live`);
    const nothing = !c.summary && !c.sections.length && !c.live.length;
    const thin = c.summary && !c.sections.length && !c.live.length;
    $("buildNote").textContent = nothing
      ? "Nothing selected."
      : thin
        ? "Summary only — tick some cost areas for a fuller report."
        : parts.join(" · ") + (c.live.length ? " — live findings take up to a minute." : "");
    $("buildNote").classList.toggle("warn", nothing || thin);
    // Links have no disabled state, so an empty selection has to lose its destination
    // instead — otherwise it would download a file with nothing in it.
    const btn = $("buildBtn");
    btn.disabled = nothing;
    for (const a of $("buildMenu").querySelectorAll("a[data-fmt]")) {
      if (nothing) a.removeAttribute("href");
      else wireDownloadLink(a, reportUrl(c, a.dataset.fmt));
    }
  };

  // `input[data-group]`, not every checkbox on the tab. The raw-data export below owns a
  // subscription list of its own, and those boxes carry no group — claiming them here would
  // both overwrite their handler and index the selection object with `undefined`.
  for (const input of $("tabBody").querySelectorAll("input[data-group]")) {
    input.onchange = () => {
      const g = input.dataset.group;
      if (g === "summary") c.summary = input.checked;
      else if (input.checked) c[g] = [...new Set([...c[g], input.value])];
      else c[g] = c[g].filter((v) => v !== input.value);
      save();
    };
  }
  for (const btn of $("tabBody").querySelectorAll("[data-all]")) {
    btn.onclick = () => {
      const g = btn.dataset.all;
      const all = allOf(g);
      const full = c[g].length === all.length;
      c[g] = full ? [] : [...all];
      for (const i of $("tabBody").querySelectorAll(`input[data-group="${g}"]`)) {
        i.checked = !full;
      }
      save();
    };
  }
  // The download control is a menu of format links. Opening it is the only scripted part;
  // the download itself is the browser following whichever link was clicked.
  const menu = $("buildMenu");
  const trigger = $("buildBtn");
  const openMenu = (show) => {
    if (show) {
      // Open downward when there is room and upward when there is not. The bar is the last
      // thing on the tab, so a menu that always dropped down would be clipped by the scroll
      // container; one that always went up would cover the choices just made.
      const below = window.innerHeight - trigger.getBoundingClientRect().bottom;
      menu.classList.toggle("up", below < 240);
    }
    menu.hidden = !show;
    trigger.setAttribute("aria-expanded", String(show));
  };
  trigger.onclick = (e) => {
    e.stopPropagation();
    openMenu(menu.hidden);
  };
  menu.onclick = (e) => {
    if (e.target.closest("a[data-fmt]")) openMenu(false);
  };
  document.addEventListener("click", (e) => {
    if (!$("buildWrap")?.contains(e.target)) openMenu(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") openMenu(false);
  });

  updateNote();
  syncAll();
  save();
  wireSelectionExport();
}

/** The link that downloads a report: everything the builder needs, as query parameters. */
function reportUrl(choice, format) {
  const p = new URLSearchParams(qs().slice(1));
  const scope = scopeIds();
  if (scope.length) p.set("scope", scope.join(","));
  p.set("summary", choice.summary ? "1" : "0");
  // An absent `sections` means "every area" to the server, so an explicit empty value is
  // needed to mean "none" — hence always sending it.
  p.set("sections", (choice.sections || []).join(","));
  p.set("blocks", (choice.blocks || []).join(","));
  p.set("live", (choice.live || []).join(","));
  // The file has to match the screen it was exported from.
  const cur = readStore("costCurrency", "");
  if (cur) p.set("currency", cur);
  return `/api/report/${format}?${p}`;
}

/**
 * The download itself is the browser following a link the person clicked — there is no
 * scripted click anywhere in the path.
 *
 * That distinction matters more than it looks. A download started from JavaScript is an
 * *automatic* download as far as the browser is concerned, and automatic downloads are
 * exactly what download-restriction policy, SmartScreen and the per-site "automatic
 * downloads" permission are there to block. When they block one they do it quietly: no file,
 * no dialog, no console error. A managed browser can therefore refuse a download that works
 * perfectly everywhere else, which is indistinguishable from a broken button.
 *
 * Following a real link is a user gesture the browser trusts, so all of that goes away. The
 * job of the code here is only to keep the link's href current.
 */

/** Load the accessible subscription catalog and the selected Cost data setup. */
async function renderSettings(token, reloadCatalog = true) {
  stop();
  const sequence = ++settingsSequence;
  if (!reloadCatalog && $("costData")) {
    // Keep the picker usable while a scoped status read is pending; never show old-scope results.
    for (const input of $("cdSubs").querySelectorAll("[data-ingest-sub]"))
      input.checked = ingestSelection.has(input.dataset.ingestSub);
    $("cdSubsSummary").textContent = ingestIds().length
      ? `${ingestIds().length} of ${ingestCatalog.length} selected` : "No subscription selected";
    $("cdScopeNote").textContent = ingestIds().length ? `${ingestIds().length} selected. Reading their saved status…` : "Choose at least one subscription.";
    $("cdStatus").innerHTML = $("pResult").innerHTML = "";
    $("cdProgress").textContent = "Reading saved status for the new selection…";
    $("pStatus").textContent = "";
    for (const id of ["cdReview", "cdRefresh", "cdChange", "cdRetry", "cdCheck", "cdLoadExports", "pInspect", "pWorkload"])
      $(id).disabled = true;
  } else setContent($("tabBody"), `<section class="card"><h3>Cost data</h3><p role="status">Loading accessible subscriptions and saved setup…</p></section>`);
  try {
    if (reloadCatalog) {
      const catalog = await get("/api/cost-data/subscriptions", { timeout: 20000 });
      if (token !== ctx.loadToken || sequence !== settingsSequence) return;
      if (!Array.isArray(catalog.subscriptions)) throw Error("The accessible subscription catalog is not ready.");
      ingestCatalog = catalog.subscriptions;
    }
    const allowed = new Set(ingestCatalog.map(s => s.id));
    if (ingestSelection === null) {
      const header = scopeIds();
      ingestSelection = new Set((header.length ? header : [...allowed]).filter(id => allowed.has(id)));
    } else ingestSelection = new Set(ingestIds().filter(id => allowed.has(id)));
    const scope = ingestIds(), selected = ingestCatalog.filter(s => ingestSelection.has(s.id));
    const excluded = ingestCatalog.filter(s => !ingestSelection.has(s.id));
    let loadError = "";
    let data = { subscriptions: [], setup_needed: true, can_review: false };
    if (scope.length) {
      try { data = await get(`/api/cost-data?scope=${encodeURIComponent(scope.join(","))}&include_export_months=1`, { timeout: 20000 }); }
      catch (error) {
        loadError = error.message || "Could not load status.";
        if (settingsData?.costData && settingsData.subscriptions.map(s => s.id).sort().join(",") === scope.join(","))
          data = { ...settingsData, can_review: false };
      }
    }
    if (token !== ctx.loadToken || sequence !== settingsSequence) return;
    settingsToken = token;
    settingsData = { ...data, costData: true };
    const today = new Date();
    const end = today.toISOString().slice(0, 10);
    const start = new Date(today.getTime() - 89 * 86400000).toISOString().slice(0, 10);
    const choices = (data.subscriptions || []).map(s => s.config?.cadence).filter(Boolean);
    if (!ingestDraft) {
      ingestDraft = { start, end, days: "90", preset: "90",
        cadence: choices.length && choices.every(c => c === choices[0]) ? choices[0] : "Daily" };
      const histories = (data.subscriptions || []).map(s => s.config?.history);
      const saved = histories[0];
      const count = saved ? (Date.parse(saved.to) - Date.parse(saved.from)) / 86400000 + 1 : 0;
      if (Number.isInteger(count) && count >= 1 && count <= 365 && saved.to <= end &&
          histories.every(h => h?.from === saved.from && h?.to === saved.to))
        Object.assign(ingestDraft, { start: saved.from, end: saved.to, days: String(count),
          preset: [30, 90, 180, 365].includes(count) ? String(count) : "custom" });
    }
    const cadence = ingestDraft.cadence;
    setContent($("tabBody"), `<section class="card cost-data" id="costData">
      <div class="tagpick-head"><h3>Cost data</h3><button type="button" class="linkish" id="cdCheck">Check status</button></div>
      <section class="view-help" aria-labelledby="cdScopeTitle">
        <h4 id="cdScopeTitle">Subscriptions to ingest</h4>
        <p class="muted note">Choose the subscriptions for all three tabs. Dashboard filters stay unchanged.</p>
        <div class="subpick-dd">
          <button type="button" class="subpick-trigger" id="cdSubsToggle" aria-haspopup="true"
            aria-expanded="${ingestMenuOpen}" aria-controls="cdSubsMenu">
            <span class="subpick-trigger-text" id="cdSubsSummary">${scope.length
              ? `${scope.length} of ${ingestCatalog.length} selected`
              : ingestCatalog.length ? "No subscription selected" : "No readable subscriptions"}</span>
            <span class="subpick-caret" aria-hidden="true">▾</span>
          </button>
          <div class="subpick-menu" id="cdSubsMenu"${ingestMenuOpen ? "" : " hidden"}>
            <label class="subpick-search"><span class="sr-only">Find a subscription</span>
              <input type="search" id="cdSearch" value="${esc(ingestQuery)}"
                placeholder="Search name or subscription ID" autocomplete="off"></label>
            <div class="subpick-tools">
              <button type="button" class="linkish" id="cdAll">Select all</button>
              <button type="button" class="linkish" id="cdNone">Clear</button>
              <button type="button" class="linkish" id="cdCatalogRefresh">Reload accessible subscriptions</button>
            </div>
            <div class="subpick cost-ingest-picker" id="cdSubs">${ingestCatalog.map((s, i) => `<label class="opt" data-ingest-row="${esc(s.id)}">
          <input type="checkbox" id="cdSub${i}" data-ingest-sub="${esc(s.id)}"${ingestSelection.has(s.id) ? " checked" : ""}>
          <span class="opt-text"><span class="opt-label">${esc(s.name || s.id)}</span><span class="opt-sub">${esc(s.id)} · Read access</span></span></label>`).join("")}</div>
            <p class="muted note" id="cdSubsEmpty" hidden>No subscription matches that search.</p>
          </div>
        </div>
        <p id="cdScopeNote" class="sr-only" role="status">${scope.length ? `${scope.length} selected: ${selected.slice(0, 3).map(s => esc(s.name || s.id)).join(", ")}${selected.length > 3 ? ` (+${selected.length - 3} more)` : ""}` : "Choose at least one subscription."}
          ${!ingestCatalog.length ? "No readable subscriptions were returned for your account." : ""}</p>
        <p id="cdExcluded" class="muted note">${excluded.length ? `Excluded from this Cost data setup: ${excluded.slice(0, 4).map(s => esc(s.name || s.id)).join(", ")}${excluded.length > 4 ? ` (+${excluded.length - 4} more)` : ""}.` : "No subscriptions are excluded from this Cost data setup."}</p>
      </section>
      <p id="cdProgress" class="muted note" role="status" aria-live="polite"></p>
      <div class="cost-data-tabs" role="tablist" aria-label="Cost import">
        ${[["first", "First-time import"], ["schedule", "Scheduled updates"], ["access", "Access & fixes"]].map(([id, title]) =>
          `<button type="button" role="tab" id="cdTab-${id}" aria-controls="cdPanel-${id}" aria-selected="${ingestTab === id}" tabindex="${ingestTab === id ? "0" : "-1"}">${esc(title)}</button>`).join("")}
      </div>
      ${data.read_only ? '<p class="muted note">This deployment is read-only. You can inspect saved setup and progress, but changes are disabled.</p>'
        : !data.can_review ? '<p class="muted note">You can view progress. An administrator with Azure export write access must review setup or retry missing work.</p>' : ""}
      ${loadError ? `<p role="alert">${esc(loadError)} Last known costs remain available.</p><button type="button" class="ghost" id="cdLoadRetry">Retry status</button>` : ""}
      <section role="tabpanel" id="cdPanel-first" aria-labelledby="cdTab-first">
      <div id="cdSetup">
        <h4 id="cdSetupTitle">Choose how much history to import</h4>
        <div id="cdHistory">
            <div class="budget-grid"><label class="bfield"><span>Import history</span><select id="cdHistoryPreset">
              ${[["30","30 days"],["90","90 days — recommended"],["180","180 days"],["365","1 year (365 days)"],["custom","Custom days"]].map(([v, title]) =>
                `<option value="${v}"${v === ingestDraft.preset ? " selected" : ""}>${title}</option>`).join("")}
            </select></label><label class="bfield" id="cdDaysField"><span>Number of days (1–365)</span><input type="number" id="cdHistoryDays" min="1" max="365" step="1" value="${esc(ingestDraft.days)}"></label></div>
            <p class="muted note" id="cdHistoryRange"></p><p class="muted note">Existing data is not deleted. This chooses history to import, not a storage-retention or deletion policy.
              Complete reports are reused; missing history loads in the background.</p>
            <details class="view-help" id="cdHistoryDates"><summary>Choose exact historical dates</summary><div class="budget-grid">
              <label class="bfield"><span>From (UTC)</span><input type="date" id="cdFrom" value="${esc(ingestDraft.start)}" max="${end}"></label>
              <label class="bfield"><span>Through (UTC)</span><input type="date" id="cdTo" value="${esc(ingestDraft.end)}" max="${end}"></label>
            </div></details>
        </div>
        <p id="cdInitialCadence" class="muted note"></p>
        <button type="button" class="linkish" id="cdEditCadence">Choose automatic frequency in Scheduled updates</button>
        <p class="muted note">Review import, then confirm once to authorize both this history and the selected automatic updates. Switching tabs makes no changes.</p>
        <button type="button" class="primary" id="cdReview">Review import</button>
      </div><div id="cdFirstReview"></div></section>
      <section role="tabpanel" id="cdPanel-schedule" aria-labelledby="cdTab-schedule">
        <h4>Keep costs up to date</h4>
        <p class="muted note">Choose a frequency, review it, then confirm. No need to repeat First-time import.</p>
        <fieldset id="cdFrequency" class="cost-frequency" aria-describedby="cdCadenceHelp cdDraftNote">
          <legend>Update frequency</legend>
          <div class="cost-frequency-options">
            ${[["Daily", "Every day", "Recommended"], ["Weekly", "Every Monday", "Weekly reviews"],
              ["Monthly", "1st of each month", "Completed-month billing"]].map(([c, timing, hint]) =>
              `<label class="cost-frequency-choice"><input type="radio" name="cdCadence" id="cdCadence-${c}" value="${c}"${c === cadence ? " checked" : ""}>
                <span><strong>${c}</strong><span>${timing}</span><small>${hint}</small></span></label>`).join("")}
          </div>
        </fieldset>
        <p class="muted note" id="cdCadenceHelp"></p>
        <div class="cost-schedule-actions">
          <button type="button" class="primary" id="cdChange">Review schedule</button>
          <button type="button" class="linkish" id="cdFixes">Access &amp; fixes</button>
        </div>
        <p class="muted note" id="cdDraftNote" role="status"></p>
        <div id="cdScheduleReview"></div>
        <p id="cdScheduleState" role="status"></p>
        <details id="cdStatusDetails" class="view-help"><summary>Subscription details and previous errors</summary>
          <div id="cdStatus" aria-live="polite"></div>
        </details>
      <div class="build-bar" id="cdMaintenance">
        <button type="button" class="ghost" id="cdRefresh">Refresh now</button>
        <button type="button" class="ghost" id="cdRetry">Retry missing work</button>
      </div>
      <details class="view-help" id="cdAdvanced"><summary>Advanced: exports and reconciliation</summary>
        <p class="muted note">Only changed day records are applied, with correction lookbacks and missed-run catch-up. Azure may deliver wider month-to-date files, not incremental files.</p>
        <p class="muted note">${esc(data.note || "Existing costs remain available while complete FOCUS reports are imported.")}</p>
        <div id="cdTechnical"></div>
        <details class="view-help"><summary>Reconcile exact historical dates</summary>
          <p class="muted note">Recheck the history dates above without changing automatic frequency.</p>
          <button type="button" class="ghost" id="cdReconcile">Choose reconciliation dates</button>
        </details>
        <details class="view-help" id="cdInventory"><summary>Legacy and external export inventory (read only)</summary>
          <button type="button" class="ghost" id="cdLoadExports">Load exports</button><div id="cdExports"></div>
        </details>
      </details>
      </section>
      <section role="tabpanel" id="cdPanel-access" aria-labelledby="cdTab-access">
        <h4>Access &amp; fixes</h4><p class="muted note">Check the same selected subscriptions before or after import. These read-only checks never grant roles.
        Unknown evidence is not a denial; your administrator may need to provision the export or fix its narrow storage access.</p>
        ${permissionPanel()}
        <button type="button" class="primary" id="cdBackToSchedule">Return to schedule</button>
        <div id="cdAccessReview"></div>
      </section>
      <div id="cdReviewArea"><div id="cdPlan"></div><p class="muted note" id="cdNotice" role="status" aria-live="polite"></p></div>
    </section>`);
    wireCostData();
  } catch (error) {
    if (token !== ctx.loadToken || sequence !== settingsSequence) return;
    setContent($("tabBody"), `<section class="card"><h3>Cost data</h3><p role="alert">Could not load accessible subscriptions. ${esc(error.message || "")} No import selection has been assumed.</p>
      <button type="button" class="ghost" id="cdLoadRetry">Retry accessible subscriptions</button></section>`);
    $("cdLoadRetry").onclick = () => renderSettings(ctx.loadToken);
  }
}

function wireCostData() {
  workflowCleanup();
  const host = $("costData");
  if (!host || !settingsData?.costData) return;
  const generation = ++workflowGeneration, token = settingsToken;
  const selected = ingestKey();
  const scope = ingestIds();
  const active = () => host === $("costData") && host.isConnected && generation === workflowGeneration &&
    token === ctx.loadToken && selected === ingestKey();
  let sequence = 0, controller = null, pollAgain = false, resumeAfterAbort = false;
  let action = "setup";
  const cadenceValue = () => ingestDraft.cadence;
  const notice = $("cdNotice");
  const invalidate = () => {
    sequence++;
    $("cdPlan").innerHTML = "";
  };
  const tabs = ["first", "schedule", "access"];
  const selectTab = (tab, focus = false) => {
    ingestTab = tab;
    for (const id of tabs) {
      const button = $("cdTab-" + id);
      button.setAttribute("aria-selected", String(id === tab));
      button.setAttribute("tabindex", id === tab ? "0" : "-1");
      $("cdPanel-" + id).hidden = id !== tab;
    }
    $(tab === "schedule" ? "cdScheduleReview" : tab === "access" ? "cdAccessReview" : "cdFirstReview")
      .appendChild($("cdReviewArea"));
    if (focus) $("cdTab-" + tab).focus();
    if (tab === "access") { $("pPanel").open = true; void $("pPanel").ontoggle?.(); }
  };
  for (const [index, tab] of tabs.entries()) {
    $("cdTab-" + tab).onclick = () => { invalidate(); selectTab(tab); };
    $("cdTab-" + tab).onkeydown = event => {
      const next = event.key === "Home" ? 0 : event.key === "End" ? 2 :
        ["ArrowRight", "ArrowDown"].includes(event.key) ? (index + 1) % 3 :
        ["ArrowLeft", "ArrowUp"].includes(event.key) ? (index + 2) % 3 : null;
      if (next !== null) { event.preventDefault(); invalidate(); selectTab(tabs[next], true); }
    };
  }
  const chooseScope = async ids => {
    invalidate();
    ingestSelection = new Set(ids.filter(id => ingestCatalog.some(s => s.id === id)));
    await renderSettings(ctx.loadToken, false);
  };
  const exportedMonthsText = row => {
    const months = Array.isArray(row.exported_months) ? row.exported_months : [];
    if (months.length) return months.join(", ");
    return row.exported_months_status === "missing" ? "None yet"
      : row.exported_months_status === "error" ? "Unavailable"
      : "None yet";
  };
  const monthWindow = row => {
    const imported = (Array.isArray(row.exported_months) ? row.exported_months : [])
      .filter(m => /^\d{4}-\d{2}$/.test(m)).sort();
    if (!imported.length) return "";
    const [y, m] = imported.at(-1).split("-").map(Number);
    const labels = [];
    let year = y, month = m;
    for (let i = 0; i < 12; i++) {
      labels.push(`${year}-${String(month).padStart(2, "0")}`);
      month--;
      if (month < 1) { month = 12; year--; }
    }
    labels.reverse();
    const importedSet = new Set(imported);
    const chips = labels.map(key => {
      const [yy, mm] = key.split("-").map(Number);
      const name = new Date(Date.UTC(yy, mm - 1, 1))
        .toLocaleDateString(undefined, { month: "short", year: "2-digit", timeZone: "UTC" });
      const on = importedSet.has(key);
      return `<span class="cd-month ${on ? "on" : "off"}" title="${esc(key)}">${esc(name)}</span>`;
    }).join("");
    return `<div class="cd-months">
      <p class="muted note"><strong>Synced months (latest 12):</strong></p>
      <div class="cd-month-grid" role="list" aria-label="Synced month coverage">${chips}</div>
    </div>`;
  };
  const applyIngestQuery = () => {
    const query = ingestQuery.toLowerCase();
    let shown = 0;
    for (const row of $("cdSubs").querySelectorAll("[data-ingest-row]")) {
      const s = ingestCatalog.find(s => s.id === row.dataset.ingestRow);
      row.hidden = !`${s?.name} ${s?.id}`.toLowerCase().includes(query);
      if (!row.hidden) shown++;
    }
    $("cdSubsEmpty").hidden = !!shown || !ingestCatalog.length;
  };
  const setMenu = open => {
    ingestMenuOpen = open;
    $("cdSubsMenu").hidden = !open;
    $("cdSubsToggle").setAttribute("aria-expanded", String(open));
  };
  $("cdSubsToggle").onclick = event => {
    event.stopPropagation();
    setMenu(!ingestMenuOpen);
    if (ingestMenuOpen) $("cdSearch").focus();
  };
  // Clicks inside must not close it: that is where the checkboxes and the search box are.
  $("cdSubsMenu").onclick = event => event.stopPropagation();
  wireIngestDismissal();
  for (const input of $("cdSubs").querySelectorAll("[data-ingest-sub]")) input.onchange = async () => {
    const next = new Set(ingestIds());
    if (input.checked) next.add(input.dataset.ingestSub); else next.delete(input.dataset.ingestSub);
    await chooseScope([...next]);
    $(input.id)?.focus();
  };
  $("cdAll").onclick = () => chooseScope(ingestCatalog.map(s => s.id));
  $("cdNone").onclick = () => chooseScope([]);
  $("cdCatalogRefresh").onclick = () => renderSettings(ctx.loadToken);
  $("cdSearch").oninput = () => { ingestQuery = $("cdSearch").value.trim(); applyIngestQuery(); };
  applyIngestQuery();
  const dates = () => {
    const start = $("cdFrom").value, end = $("cdTo").value;
    const lo = Date.parse(start + "T00:00:00Z"), hi = Date.parse(end + "T00:00:00Z");
    if (![start, end].every(v => /^\d{4}-\d{2}-\d{2}$/.test(v)) || !Number.isFinite(lo) ||
        !Number.isFinite(hi) || new Date(lo).toISOString().slice(0, 10) !== start ||
        new Date(hi).toISOString().slice(0, 10) !== end || end > new Date().toISOString().slice(0, 10)) return null;
    const count = (hi - lo) / 86400000 + 1;
    return Number.isInteger(count) && count >= 1 ? count : null;
  };
  const validHistory = () => {
    const count = dates();
    return !!count && count <= (action === "reconcile" ? 366 : 365) &&
      (action === "reconcile" || (Number($("cdHistoryDays").value) === count && /^\d+$/.test($("cdHistoryDays").value)));
  };
  const busyStates = ["queued", "running", "waiting-for-Azure", "downloading", "importing", "validating", "authenticating"];
  const cadenceSummaries = {
    Daily: "Best for an active dashboard. Checks every day and picks up recent charges and corrections.",
    Weekly: "Checks every Monday, including recent corrections. Data can be up to a week behind.",
    Monthly: "For month-end billing reviews. Checks completed months; data can be up to a month behind.",
  };
  const displayTime = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) :
      date.toLocaleString(undefined, { timeZone: "UTC", dateStyle: "medium", timeStyle: "short" }) + " UTC";
  };
  const blocked = () => !active() || !scope.length || settingsData.subscriptions.map(s => s.id).sort().join(",") !== selected ||
    !settingsData.can_review || settingsData.subscriptions.some(s =>
    busyStates.includes(s.status));
  const label = row => {
    return ({ idle: "Not set up", ready: "Last update completed", running: "Importing", queued: "Queued",
      "waiting-for-Azure": "Awaiting Azure", partial: "Previous work incomplete", failed: "Previous attempt failed",
      downloading: "Importing", validating: "Validating", importing: "Importing",
      authenticating: "Checking access and configuring" })[row.status] || row.status;
  };
  const fullyConfigured = () => settingsData.subscriptions.length > 0 &&
    settingsData.subscriptions.every(s => s.configured);
  const paint = () => {
    const rows = settingsData.subscriptions, completed = rows.reduce((n, s) => n + s.coverage.authoritative_days, 0);
    const total = rows.reduce((n, s) => n + s.coverage.requested_days, 0);
    $("cdProgress").textContent = !scope.length ? "Choose at least one subscription." : !total ? "Saved progress is not available yet. Check status or use Access & fixes."
      : `Initial history: ${completed} of ${total} subscription-days verified (${Math.floor(100 * completed / total)}%). `
        + (rows.some(s => busyStates.includes(s.status)) ? "Background work continues; existing costs stay available." : "");
    const through = rows.every(s => s.through) ? rows.map(s => s.through).sort()[0] : null;
    $("cdStatus").innerHTML = `<h4>Costs available through ${esc(through || "— awaiting complete reports")}</h4>
      <p class="muted note">Initial FOCUS coverage: ${total && completed === total ? "complete" : "partial / not yet complete"}
        · ${completed} of ${total} subscription-days verified${total ? ` (${Math.floor(100 * completed / total)}%)` : ""}.
        Quick data remains available but does not count toward this coverage.</p>
      ${rows.some(s => s.coverage.current_day_provisional) ? '<p class="muted note">Today’s costs are provisional. Automatic updates pick up later charges and corrections.</p>' : ""}
      ${settingsData.auto_enabled === false ? '<p class="banner warn">Automatic checks are disabled or deployment storage is unavailable. Ask the deployment administrator to enable the backend pipeline.</p>' : ""}
      <div class="cost-data-subscriptions">${rows.map(s => `<details class="view-help"><summary><strong>${esc(s.name || s.id)}</strong>
        · ${esc(label(s))} · ${esc(s.configured ? s.config?.cadence : "Schedule not enabled")}</summary>
        <p class="muted note">Available through: ${esc(s.through || "Not yet reported")}<br>
        Automatic updates: ${s.configured && s.config?.enabled !== false ? "Enabled" : "Not enabled"} · ${esc(s.config?.cadence || "Not configured")}<br>
        Exported months in storage: ${esc(exportedMonthsText(s))}<br>
        Last successful update: ${esc(s.last_success ? displayTime(s.last_success) : "None yet")}<br>
        Next scheduled check: ${esc(s.next_check ? displayTime(s.next_check) : "Not enabled")}<br>
        Initial history: ${esc(s.history?.from || "")} → ${esc(s.history?.to || "")}
        · ${s.coverage.authoritative_days}/${s.coverage.requested_days} days verified.</p>
        ${monthWindow(s)}
        ${s.exported_months_detail ? `<p class="muted note">${esc(s.exported_months_detail)}</p>` : ""}
        <p class="muted note">${esc(s.detail || "")}</p>
        ${(s.tasks || []).filter(t => t.status !== "ready").map(t => `<p class="muted note">${esc(t.from ? `${t.from} → ${t.to}` : "Automatic updates")}:
          ${esc(t.status)} — ${esc(t.detail || "")}</p>`).join("")}
        <p><button type="button" class="linkish" data-cd-exclude="${esc(s.id)}">Exclude from this Cost data setup</button></p>
        </details>`).join("")}</div>`;
    const last = rows.length && rows.every(s => s.last_success) ? displayTime(rows.map(s => s.last_success).sort()[0]) : "None for the full selection yet";
    const next = rows.map(s => s.next_check).filter(Boolean).sort()[0];
    const enabled = rows.filter(s => s.configured).length;
    const savedCadences = [...new Set(rows.filter(s => s.configured).map(s => s.config?.cadence))].filter(Boolean);
    const failed = rows.some(s => ["failed", "partial"].includes(s.status));
    $("cdScheduleState").textContent = rows.length
      ? `${enabled} of ${rows.length} schedules enabled${savedCadences.length ? ` (${savedCadences.join(", ")})` : ""}. Next scheduled check: ${next ? displayTime(next) : "Not enabled"}.`
        + (failed ? " Previous work needs attention; expand the details below." : "")
      : "No subscription status available.";
    $("cdRefresh").hidden = !fullyConfigured();
    $("cdRetry").hidden = !rows.some(s => s.retry_job_id);
    $("cdMaintenance").hidden = $("cdRefresh").hidden && $("cdRetry").hidden;
    $("cdStatus").innerHTML += `<p class="muted note">Last successful update: ${esc(last)}<br>Next scheduled check: ${esc(next ? displayTime(next) : "Not enabled")}.</p>`;
    if (next && Date.parse(next) < Date.now() - 900000)
      $("cdStatus").innerHTML += '<p class="muted note">A scheduled check is overdue or waiting for current work. Keep the backend and database running so the pipeline can catch up.</p>';
    for (const button of $("cdStatus").querySelectorAll("[data-cd-exclude]")) {
      button.onclick = async () => {
        const id = button.dataset.cdExclude;
        await chooseScope(ingestIds().filter(x => x !== id));
      };
    }
    $("cdTechnical").innerHTML = `<p class="muted note"><strong>Selected timing and coverage:</strong>
      ${esc(settingsData.cadences?.[cadenceValue()] || "")}</p>` + rows.map(s => `<p class="muted note"><strong>${esc(s.name)}</strong>:
      ${esc(s.config?.optimization || (s.config?.mode === "native-month-to-date" ? "Legacy native Daily MTD compatibility" : "Existing configuration unchanged until approved"))}
      <br>Export: ${esc(s.config?.export || "Discovered during review")} · ${esc(s.config?.identity || "Not yet authorized")}
      <br>Format: ${esc(s.config?.format || "Discovered during review")}
      ${s.config?.destination ? `<br>Destination: ${esc(s.config.destination.account_id)}/${esc(s.config.destination.container)}/${esc(s.config.destination.folder)}` : ""}
      ${s.job_id ? `<br>Job: <code>${esc(s.job_id)}</code>` : ""}</p>`).join("");
    $("cdReview").disabled = blocked() || !validHistory();
    $("cdRefresh").disabled = blocked() || rows.some(s => !s.configured);
    $("cdChange").disabled = blocked();
    $("cdRetry").disabled = !settingsData.can_review || !rows.some(s => s.retry_job_id);
    $("cdReconcile").disabled = blocked() || rows.some(s => !s.configured);
    $("cdCheck").disabled = !scope.length;
    $("cdLoadExports").disabled = !scope.length;
    $("cdCadenceHelp").textContent = (cadenceSummaries[cadenceValue()] ||
      settingsData.cadences?.[cadenceValue()] || "") + " Runs at 02:00 UTC.";
    $("cdHistoryRange").textContent = validHistory() ? `${dates()} days · ${$("cdFrom").value} → ${$("cdTo").value} · inclusive UTC`
      : `Choose valid, non-future dates covering 1–${action === "reconcile" ? 366 : 365} whole days.`;
    $("cdDaysField").hidden = $("cdHistoryPreset").value !== "custom";
    const cadence = cadenceValue();
    $("cdInitialCadence").textContent = `This import also authorizes ${cadence} automatic updates${cadence === "Daily" ? " (recommended)" : ""}.`;
    if (action === "setup") {
      $("cdSetupTitle").textContent = "Choose how much history to import";
      $("cdReview").textContent = "Review import";
    }
    $("cdChange").textContent = `Review ${cadence.toLowerCase()} schedule`;
    $("cdDraftNote").textContent = !scope.length ? "Select at least one subscription above."
      : !settingsData.can_review ? "An application administrator must review and enable schedules."
      : rows.some(s => busyStates.includes(s.status)) ? "Work is in progress. Check status before changing the schedule."
      : fullyConfigured() && rows.every(s => s.config?.cadence === cadence) && !failed
      ? `${cadence} is already enabled. Choose another frequency and review to change it. This does not start a historical import.`
      : `Not saved yet. Review ${cadence.toLowerCase()}, then confirm below. This does not start a historical import.`
        + (failed ? " The review rechecks current access; previous errors do not prevent a new review." : "");
  };
  const queue = delay => {
    clearTimeout(workflowTimer);
    workflowTimer = null;
    if (active() && !document.hidden && pollAgain) workflowTimer = setTimeout(() => void load(), delay);
  };
  async function load() {
    if (!active() || !scope.length || document.hidden || controller) return;
    clearTimeout(workflowTimer);
    workflowTimer = null;
    const requestController = controller = new AbortController();
    try {
      const result = await get(`/api/cost-data?scope=${encodeURIComponent(scope.join(","))}`,
        { timeout: 20000, signal: requestController.signal });
      if (!active() || requestController.signal.aborted || document.hidden) return;
      if (result.subscriptions.map(s => s.id).sort().join(",") !== selected)
        throw Error("The accessible scope changed. Reload accessible subscriptions.");
      const signature = rows => JSON.stringify(rows.map(s => [
        s.last_success, s.coverage.authoritative_days, s.status === "ready" ? "ready" : "pending"]));
      const before = signature(settingsData.subscriptions);
      settingsData = { ...result, costData: true };
      paint();
      const after = signature(settingsData.subscriptions);
      if (before !== after) window.dispatchEvent(new CustomEvent("cloudlens:focus-synced", {
        detail: { status: result.subscriptions.every(s => s.status === "ready") ? "ready" : "partial",
          started: result.subscriptions.find(s => s.job_id)?.job_id || after,
          finished: result.checked_at || after } }));
      pollAgain = settingsData.subscriptions.some(s => busyStates.includes(s.status));
      queue(10000);
    } catch (error) {
      if (active() && !requestController.signal.aborted) {
        invalidate();
        notice.textContent = "Could not check progress. Last known costs and setup are retained. Retry status or reload accessible subscriptions if access changed.";
        pollAgain = true; queue(30000);
      }
    } finally {
      if (controller === requestController) controller = null;
      if (resumeAfterAbort && active() && !document.hidden) {
        resumeAfterAbort = false; pollAgain = true; queue(0);
      }
    }
  }
  async function review(intent) {
    if (!active() || blocked()) return;
    invalidate();
    const revision = sequence;
    if (["setup", "reconcile"].includes(intent) && !validHistory()) {
      notice.textContent = "Choose valid history dates before review."; return;
    }
    const body = { scope, action: intent, cadence: cadenceValue() };
    if (["setup", "reconcile"].includes(intent)) Object.assign(body, { start: $("cdFrom").value, end: $("cdTo").value });
    if (intent === "setup") body.history_days = dates();
    notice.textContent = "Checking workload identity, destination and owned exports… No changes are being made.";
    try {
      const plan = await focusPost("/api/cost-data/preview", body);
      if (!active() || sequence !== revision) return;
      $("cdPlan").innerHTML = `<div class="view-help cost-data-review"><h4 id="cdReviewTitle" tabindex="-1">${intent === "schedule" ? `Confirm ${esc(body.cadence.toLowerCase())} schedule` : "Review " + (intent === "setup" ? "setup" : intent)}</h4>
        <p>${esc(plan.explanation)}${intent === "setup" || intent === "reconcile" ? `<br>History: ${esc(plan.from)} → ${esc(plan.to)} (inclusive UTC)` : ""}</p>
        ${intent === "setup" ? `<p><strong>This approval imports ${esc(body.history_days)} days and enables ${esc(body.cadence)} automatic updates.</strong> Existing data is not deleted. Available reports may be reused; missing reports may require an Azure export.</p>` : ""}
        ${intent === "schedule" ? `<p><strong>${esc(body.cadence)} updates for ${scope.length} selected subscription${scope.length === 1 ? "" : "s"}.</strong>
          This configures ongoing updates only. Existing history and recovery progress are preserved; no historical import starts now.</p>` : ""}
        <p><strong>Subscriptions:</strong> ${plan.subscriptions.map(s => esc(s.name || s.subscription)).join(", ")}</p>
        <p class="muted note">${esc(plan.identity)}</p>
        ${plan.subscriptions.filter(s => s.blockers?.length).map(s => `<p class="banner warn"><strong>${esc(s.name || s.subscription)}</strong><br>${s.blockers.map(esc).join("<br>")}</p>`).join("")}
        <p class="muted note">Existing costs stay available. Only this deployment's exports are affected.
          Azure may take time to prepare new reports; the work continues after you close this page.</p>
        <details class="view-help" id="cdReviewDetails"><summary>Azure changes, permissions and data coverage</summary>
        ${plan.subscriptions.map(s => `<p class="muted note"><strong>${esc(s.name || s.subscription)}</strong>: ${esc(s.action)}
          ${s.window ? `<br>Coverage: ${esc(s.window.from)} → ${esc(s.window.to)} (inclusive UTC)` : ""}
          <br>${esc(s.export?.name || "New deployment-owned FOCUS export")} · ${esc(s.destination.account_id)}/${esc(s.destination.container)}/${esc(s.destination.folder)}
          ${(s.blockers || []).map(b => `<br><strong>${esc(b)}</strong>`).join("")}</p>`).join("")}
        ${plan.notes.map(n => `<p class="muted note">${esc(n)}</p>`).join("")}</details>
        <div class="build-bar"><button type="button" class="primary" id="cdStart"${plan.can_start ? "" : " disabled"}>${
          intent === "schedule" ? `Enable ${esc(body.cadence.toLowerCase())} updates` : intent === "setup" ? "Start import and updates" : "Confirm " + intent
        }</button>
          <button type="button" class="ghost" id="cdCancel">Cancel</button>
          ${!plan.can_start ? '<button type="button" class="ghost" id="cdRecheck">Recheck readiness</button><button type="button" class="linkish" id="cdReviewFixes">Access &amp; fixes</button>' : ""}
        </div></div>`;
      notice.textContent = plan.can_start ? "Review scope and permissions, then confirm once. No data changes before confirmation."
        : "Cannot enable yet. Resolve the specific issues above, then select Recheck readiness. No changes made.";
      $("cdCancel").onclick = () => { invalidate(); notice.textContent = "Cancelled. No changes made."; };
      if ($("cdRecheck")) $("cdRecheck").onclick = () => review(intent);
      if ($("cdReviewFixes")) $("cdReviewFixes").onclick = () => { invalidate(); selectTab("access", true); };
      $("cdReviewTitle").focus();
      $("cdReviewTitle").scrollIntoView?.({ block: "nearest" });
      const idempotency = crypto.randomUUID(), button = $("cdStart");
      button.onclick = async () => {
        if (!active() || sequence !== revision || !plan.can_start || button.disabled) return;
        button.disabled = true;
        try {
          const job = await focusPost("/api/cost-data/setup", { ...body, preview_token: plan.preview_token, idempotency_key: idempotency });
          if (!job.job_id) throw Error("Setup response was incomplete. Check status before retrying.");
          if (!active() || sequence !== revision) return;
          invalidate();
          notice.textContent = intent === "schedule"
            ? "Schedule configuration queued, not yet enabled. Check status here for completion or the exact error."
            : "Setup queued. You can close this browser; the authorized backend continues. Azure may take hours to generate reports.";
          pollAgain = true;
          await load();
          queue(10000);
        } catch (error) {
          if (active() && sequence === revision) {
            notice.textContent = `${error.message} Check status before retrying. The same approval key will be reused.`;
            button.disabled = false;
          }
        }
      };
    } catch (error) {
      if (active() && revision === sequence) notice.textContent = error.message || "Could not review setup. Existing data is unchanged.";
    }
  }
  $("cdReview").onclick = () => review(action);
  $("cdRefresh").onclick = () => review("refresh");
  $("cdEditCadence").onclick = () => { invalidate(); selectTab("schedule", true); };
  $("cdFixes").onclick = () => { invalidate(); selectTab("access", true); };
  $("cdBackToSchedule").onclick = () => { invalidate(); selectTab("schedule", true); };
  $("cdReconcile").onclick = () => {
    if (blocked()) return;
    invalidate(); action = "reconcile";
    selectTab("first", true);
    $("cdSetup").hidden = false; $("cdHistory").hidden = false;
    $("cdHistoryDates").open = true;
    $("cdSetupTitle").textContent = "Reconcile historical cost data";
    $("cdReview").textContent = "Review reconciliation";
    $("cdFrom").focus?.();
  };
  $("cdChange").onclick = () => review("schedule");
  const saveDraft = () => {
    ingestDraft = { start: $("cdFrom").value, end: $("cdTo").value, days: $("cdHistoryDays").value,
      preset: $("cdHistoryPreset").value, cadence: cadenceValue() };
    invalidate(); paint();
  };
  const chooseDays = () => {
    const value = $("cdHistoryDays").value, count = Number(value);
    if (/^\d+$/.test(value) && Number.isInteger(count) && count >= 1 && count <= 365) {
      const today = new Date().toISOString().slice(0, 10);
      $("cdTo").value = today;
      $("cdFrom").value = new Date(Date.parse(today + "T00:00:00Z") - (count - 1) * 86400000).toISOString().slice(0, 10);
    }
    saveDraft();
  };
  $("cdHistoryPreset").onchange = () => {
    if ($("cdHistoryPreset").value !== "custom") $("cdHistoryDays").value = $("cdHistoryPreset").value;
    chooseDays();
  };
  $("cdHistoryDays").oninput = $("cdHistoryDays").onchange = chooseDays;
  for (const id of ["cdFrom", "cdTo"]) $(id).oninput = $(id).onchange = () => {
    $("cdHistoryPreset").value = "custom"; $("cdHistoryDays").value = String(dates() || "");
    saveDraft();
  };
  for (const cadence of ["Daily", "Weekly", "Monthly"]) $("cdCadence-" + cadence).onchange = () => {
    ingestDraft.cadence = cadence;
    for (const choice of ["Daily", "Weekly", "Monthly"]) $("cdCadence-" + choice).checked = choice === cadence;
    saveDraft();
  };
  $("cdCheck").onclick = load;
  if ($("cdLoadRetry")) $("cdLoadRetry").onclick = load;
  $("cdRetry").onclick = async () => {
    if (!active() || !scope.length || $("cdRetry").disabled) return;
    $("cdRetry").disabled = true;
    const ids = [...new Set(settingsData.subscriptions.map(s => s.retry_job_id).filter(Boolean))];
    try {
      for (const id of ids) {
        if (!active()) return;
        await focusPost(`/api/focus/jobs/${encodeURIComponent(id)}/resume?scope=${encodeURIComponent(scope.join(","))}`, {});
      }
      if (!active()) return;
      notice.textContent = "Retry queued; already completed windows will not run again.";
      pollAgain = true; await load(); queue(10000);
    } catch (error) { if (active()) { notice.textContent = error.message; paint(); } }
  };
  $("cdLoadExports").onclick = async () => {
    if (!active() || !scope.length) return;
    const button = $("cdLoadExports");
    button.disabled = true;
    try {
      const result = await get(`/api/schedules?scope=${encodeURIComponent(scope.join(","))}&fresh=true`, { timeout: 45000 });
      if (active()) $("cdExports").innerHTML = (result.schedules || []).map(s => scheduleRow(s, false)).join("") ||
        '<p class="muted note">No exports found.</p>';
    } catch (error) { if (active()) notice.textContent = scheduleReadError(error); }
    finally { if (active()) button.disabled = false; }
  };
  const visibility = () => {
    if (!active()) return;
    if (document.hidden) { clearTimeout(workflowTimer); workflowTimer = null; controller?.abort(); }
    else {
      pollAgain = true;
      if (controller?.signal.aborted) resumeAfterAbort = true;
      else void load();
    }
  };
  document.addEventListener?.("visibilitychange", visibility);
  workflowCleanup = () => {
    sequence++;
    clearTimeout(workflowTimer); workflowTimer = null;
    controller?.abort();
    document.removeEventListener?.("visibilitychange", visibility);
    if (host === $("costData")) $("cdPlan").innerHTML = "";
  };
  paint();
  wirePermissions(ingestIds);
  selectTab(ingestTab);
  pollAgain = settingsData.subscriptions.some(s => busyStates.includes(s.status));
  queue(10000);
}

async function focusPost(url, body) {
  const response = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "FOCUS request failed.");
  return result;
}

function wireFocusWorkflow(sched) {
  workflowCleanup();
  const host = $("focusWorkflow");
  if (!host) return;
  const generation = ++workflowGeneration;
  const token = settingsToken;
  const scope = scheduleSubscriptions(sched).map((s) => s.id);
  const scopeKey = scopeIds().slice().sort().join(",");
  const active = () => generation === workflowGeneration && host === $("focusWorkflow") &&
    host.isConnected && token === settingsToken && token === ctx.loadToken &&
    scopeIds().slice().sort().join(",") === scopeKey;
  const writer = scope.length > 0 && scheduleSubscriptions(sched).every((s) => s.can_write === true);
  const notice = $("fNotice");
  const showError = (error) => { if (active()) notice.textContent = error.message || "Could not reach the server; existing data is unchanged."; };
  let jobsController = null, needsJobs = true, resumeJobs = false, previewSequence = 0;
  const invalidatePreview = () => {
    ++previewSequence;
    if (host === $("focusWorkflow") && ($("fStart") || $("fDailySave"))) {
      $("fPlan").innerHTML = '<p class="muted note">Previous approval expired. Preview the current scope and dates again before approving a change.</p>';
    }
  };
  const queueJobs = (delay) => {
    clearTimeout(workflowTimer);
    workflowTimer = null;
    if (active() && needsJobs && !document.hidden)
      workflowTimer = setTimeout(() => { workflowTimer = null; void loadJobs(); }, delay);
  };
  const visibility = () => {
    if (!active()) return;
    if (document.hidden) {
      clearTimeout(workflowTimer);
      workflowTimer = null;
      resumeJobs = needsJobs;
      jobsController?.abort();
    } else if (needsJobs) void loadJobs();
  };
  document.addEventListener?.("visibilitychange", visibility);
  workflowCleanup = () => {
    needsJobs = false;
    clearTimeout(workflowTimer);
    workflowTimer = null;
    jobsController?.abort();
    document.removeEventListener?.("visibilitychange", visibility);
    invalidatePreview();
  };
  invalidatePreview();
  const today = new Date();
  if (!$("fTo").value) $("fTo").value = today.toISOString().slice(0, 10);
  if (!$("fFrom").value) $("fFrom").value = new Date(today.getTime() - 89 * 86400000).toISOString().slice(0, 10);
  $("fDailyPreview").disabled = !writer;

  async function loadJobs() {
    if (!active()) return;
    clearTimeout(workflowTimer);
    workflowTimer = null;
    needsJobs = true;
    if (document.hidden || jobsController) return;
    resumeJobs = false;
    const controller = jobsController = new AbortController();
    try {
      const result = await get(`/api/focus/jobs?scope=${encodeURIComponent(scope.join(","))}`, {
        timeout: 20000, signal: controller.signal,
      });
      if (!active() || controller.signal.aborted || document.hidden) return;
      const list = result.jobs || [];
      const updates = [];
      for (const job of list) {
        const ready = (job.tasks || []).filter((task) => task.status === "ready");
        const signature = JSON.stringify([job.generation || job.result?.generation || "",
          job.status === "ready" ? job.finished || "ready" : "",
          ready.map(task => [task.task_id || `${task.subscription}|${task.from}|${task.to}`,
            task.finished || "", task.result?.changed_days ?? null]).sort()]);
        const previous = workflowJobs.get(job.job_id);
        workflowJobs.set(job.job_id, signature);
        const completion = `${job.job_id}|${signature}`;
        // First-seen completed jobs are history, not newly observed imports.
        if (previous !== undefined && previous !== signature && ready.length && !workflowNotified.has(completion)) {
          workflowNotified.add(completion);
          updates.push({ ...job, started: job.job_id, finished: job.finished || signature,
            status: job.status === "ready" ? "ready" : "partial", completed_chunks: ready.length });
        }
      }
      while (workflowJobs.size > 100) workflowJobs.delete(workflowJobs.keys().next().value);
      while (workflowNotified.size > 200) workflowNotified.delete(workflowNotified.values().next().value);
      const openJobs = new Set([...host.querySelectorAll("[data-focus-job]")].filter((el) => el.open).map((el) => el.dataset.focusJob));
      $("fJobs").innerHTML = list.length ? list.map((job) => {
        const tasks = job.tasks || [];
        const ready = tasks.filter((t) => t.status === "ready").length;
        const retry = writer && job.status !== "ready";
        return `<details class="view-help" data-focus-job="${esc(job.job_id)}"${openJobs.has(job.job_id) ? " open" : ""}><summary>${esc(job.kind)} · ${esc(job.status)} · ${ready}/${tasks.length} chunks complete</summary>
          <p class="muted note">Job ${esc(job.job_id)} · ${esc(job.payload?.from || "")} → ${esc(job.payload?.to || "")}</p>
          ${ready && job.status !== "ready" ? `<p class="muted note" role="status">Partial import: ${ready} completed chunks are available. Remaining coverage is not complete.</p>` : ""}
          ${tasks.map((t) => `<p class="muted note">${esc(t.subscription)} · ${esc(t.from)} → ${esc(t.to)}:
            ${esc(t.status)} — ${esc(t.detail || "")}
            ${t.result ? ` · ${Number(t.result.source_bytes || 0).toLocaleString()} bytes read · ${Number(t.result.rows_inspected ?? t.result.rows ?? 0).toLocaleString()} rows inspected · ${Number(t.result.changed_days || 0)} changed days · ${Number(t.result.unchanged_days || 0)} unchanged days` : ""}</p>`).join("")}
          ${retry ? `<button type="button" class="ghost" data-focus-resume="${esc(job.job_id)}">Resume failed or pending chunks</button>` : ""}
        </details>`;
      }).join("") : '<p class="muted note">No FOCUS window jobs for this selection yet. Preview shows authoritative coverage.</p>';
      for (const button of host.querySelectorAll("[data-focus-resume]")) {
        button.onclick = async () => {
          if (!active() || !writer || button.disabled) return;
          button.disabled = true;
          try {
            await focusPost(`/api/focus/jobs/${encodeURIComponent(button.dataset.focusResume)}/resume`, {});
            await loadJobs();
          } catch (error) { showError(error); }
          finally { if (active()) button.disabled = false; }
        };
      }
      needsJobs = list.some((j) => !["ready", "failed", "partial", "cancelled"].includes(j.status));
      if (needsJobs) queueJobs(10000);
      for (const update of updates) {
        if (!active()) break;
        notice.textContent = update.status === "ready" ? "FOCUS import completed. Cost views are being refreshed."
          : "Partial FOCUS import: completed chunks are available; remaining coverage is pending or failed. Cost views are being refreshed.";
        window.dispatchEvent(new CustomEvent("cloudlens:focus-synced", { detail: update }));
      }
    } catch (error) {
      if (!active() || controller.signal.aborted) return;
      showError(error);
      queueJobs(30000);
    } finally {
      if (jobsController === controller) jobsController = null;
      if (resumeJobs && active() && !document.hidden) queueJobs(0);
    }
  }

  async function preview(purpose) {
    if (!active()) return;
    invalidatePreview();
    const request = previewSequence;
    const body = { scope, purpose, ...(purpose === "reconcile" ? {
      start: $("fFrom").value, end: $("fTo").value } : {}) };
    notice.textContent = "Checking owned manifests, coverage and capabilities…";
    try {
      const plan = await focusPost("/api/focus/preview", body);
      if (!active() || request !== previewSequence) return;
      $("fPlan").innerHTML = `<h4>${plan.days} inclusive UTC days: ${esc(plan.from)} → ${esc(plan.to)}</h4>
        <p class="muted note">${esc(plan.identity)}</p>
        ${plan.subscriptions.map((s) => `<p class="muted note"><strong>${esc(s.name)}</strong> · tenant ${esc(s.tenant || "not reported")}
          · ${s.coverage.authoritative_days}/${s.coverage.requested_days} days with authoritative receipts
          · ${esc(s.destination.account_id)}/${esc(s.destination.container)}/${esc(s.destination.folder)}
          ${s.tasks.map((t) => `<br>${esc(t.from)} → ${esc(t.to)}: ${esc(t.action)}`).join("")}
          ${s.missing_capabilities.map((c) => `<br>${esc(c.capability)}: ${esc(c.detail)}`).join("")}
          ${s.write_reason ? `<br>${esc(s.write_reason)}` : ""}</p>`).join("")}
        <button type="button" class="primary" id="fStart"${writer && plan.can_start ? "" : " disabled"}>Start approved FOCUS load</button>`;
      notice.textContent = "Review the exact scope and dates. Only complete FOCUS reports replace costs; current-day costs are provisional.";
      const idempotencyKey = crypto.randomUUID();
      const button = $("fStart");
      button.onclick = async () => {
        if (!active() || request !== previewSequence || $("fStart") !== button ||
            button.disabled || !writer || !plan.can_start) return;
        button.disabled = true;
        try {
          const job = await focusPost("/api/focus/jobs", { ...body,
            preview_token: plan.preview_token, idempotency_key: idempotencyKey });
          if (!job.job_id) throw Error("No job identifier was returned. Check import status before retrying.");
          workflowJobs.set(job.job_id, "");
          if (!active()) return;
          notice.textContent = `Job ${job.job_id} accepted. Azure generation can take hours; completed chunks are retained on resume.`;
          await loadJobs();
        } catch (error) {
          if (active() && request === previewSequence) {
            invalidatePreview();
            showError(new Error(`${error.message || "Request failed"} Check job status, then preview again before retrying.`));
          }
        }
      };
    } catch (error) { if (request === previewSequence) showError(error); }
  }
  $("fPreview").onclick = () => preview("bootstrap");
  $("fReconcile").onclick = () => preview("reconcile");
  $("fVerify").onclick = () => preview("verify");
  $("fJobsReload").onclick = loadJobs;
  for (const id of ["fFrom", "fTo", "fDailyMode"]) $(id).onchange = invalidatePreview;
  $("fDailyPreview").onclick = async () => {
    if (!active() || !writer) return;
    invalidatePreview();
    const request = previewSequence;
    const body = { scope, mode: $("fDailyMode").value, lookback: 7 };
    try {
      const plan = await focusPost("/api/focus/daily/preview", body);
      if (!active() || request !== previewSequence) return;
      $("fPlan").innerHTML = `<h4>Daily mode: ${esc(plan.mode)}</h4><p class="muted note">${esc(plan.identity)}
        <br>${esc(plan.periodic_reconciliation)}<br>${esc(plan.retention)}</p>
        ${plan.subscriptions.map((s) => `<p class="muted note">${esc(s.subscription)}: ${esc(s.action)}
          · custom coverage ${s.custom_coverage_verified ? "verified" : "not yet verified"}${s.blocked ? " — blocked" : ""}</p>`).join("")}
        <button type="button" class="primary" id="fDailySave"${writer && !plan.subscriptions.some((s) => s.blocked) ? "" : " disabled"}>Approve workload identity and cadence change</button>`;
      const button = $("fDailySave");
      button.onclick = async () => {
        if (!active() || request !== previewSequence || $("fDailySave") !== button ||
            button.disabled || !writer || plan.subscriptions.some((s) => s.blocked)) return;
        button.disabled = true;
        try {
          const saved = await focusPost("/api/focus/daily", { ...body, preview_token: plan.preview_token });
          if (active() && request === previewSequence)
            notice.textContent = saved.subscriptions.map((s) => `${s.subscription}: ${s.detail}`).join(" · ");
        } catch (error) {
          if (active() && request === previewSequence) {
            invalidatePreview();
            showError(new Error(`${error.message || "Request failed"} Inspect the saved configuration, then preview again before retrying.`));
          }
        }
      };
    } catch (error) { if (request === previewSequence) showError(error); }
  };
  void loadJobs();
}

function scheduleReadError(error, statusOnly = false) {
  const subject = statusOnly ? "import status" : "subscriptions and export schedules";
  if (error?.forbidden || error?.status === 403)
    return `Access to ${subject} was denied. Check your subscription permissions and try again.`;
  if (error?.status === 401)
    return `Your session expired. Sign in again to load ${subject}.`;
  if (error?.name === "TimeoutError" || error?.name === "AbortError" || /timed? ?out/i.test(error?.message || ""))
    return `Loading ${subject} timed out. Your saved schedules are unchanged; try again.`;
  return `Could not load ${subject}. Check the connection and try again; your saved schedules are unchanged.`;
}

function scheduleSubscriptions(sched) {
  const scope = scopeIds().map((id) => id.toLowerCase());
  const errors = Array.isArray(sched.errors) ? sched.errors : [];
  return (Array.isArray(sched.subscriptions) ? sched.subscriptions : [])
    .filter((s) => typeof s.id === "string" && (!scope.length || scope.includes(s.id.toLowerCase())))
    .map((s) => errors.some((error) => String(error.subscription || "").toLowerCase() === s.id.toLowerCase()) ||
        (sched.error && !errors.length)
      ? { ...s, can_write: false, write_reason: "The export list could not be read. Check subscription access and reload before making changes." }
      : s);
}

function ownedSchedule(sched, subscription) {
  return (sched.schedules || []).find((s) => s.owned && s.subscription_id === subscription);
}

function pickedSubscriptions(sched) {
  return scheduleSubscriptions(sched).filter((s) => s.can_write === true &&
    (scheduleChoice === "*" || s.id === scheduleChoice));
}

function scheduleCoverage(recurrence) {
  return recurrence === "Monthly" ? "Previous calendar month (TheLastMonth)" :
    "Current month to date (MonthToDate), not a rolling day or week";
}

function scheduleDestination(dest, subscription) {
  const folder = (dest.folder || "focus/<subscription-id>").replace("<subscription-id>", subscription.toLowerCase());
  return `${dest.account_id || "Storage not configured"}/${dest.container || "focus-exports"}/${folder}`;
}

/**
 * The archived days available as an export source, newest first.
 *
 * Grouped by dataset because "2026-08-27" alone is ambiguous once more than one report type is
 * archived — an amortized snapshot and an actual one from the same day are different numbers.
 */
function snapshotOptions(arch) {
  const days = arch.days || {};
  const label = { focus: "FOCUS", amortized: "Amortized", actual: "Actual" };
  return Object.keys(days)
    .sort()
    .map((set) => `<optgroup label="${esc(label[set] || set)}">${
      days[set].map((d) => `<option value="${esc(d)}" data-set="${esc(set)}">${esc(d)}</option>`)
        .join("")}</optgroup>`)
    .join("");
}

function scheduleRow(s, admin = false) {
  const managed = s.identity;
  return `<details class="sched ${s.status === "Active" ? "on" : ""}"
      data-state-key="${esc(`schedule-${JSON.stringify([s.subscription_id || s.subscription || "", s.name || ""])}`)}">
    <summary>
      <strong>${esc(s.name || "Unnamed schedule")}</strong>
      <span class="pill${s.status === "Active" ? " ok" : ""}">${esc(s.status || "unknown")}</span>
      <span class="muted"> · ${esc(s.metric || "—")} · ${esc(s.recurrence || "—")}
        · ${s.owned ? "Owned by this deployment" : "Legacy / external — not changed automatically"}
        · ${esc(s.subscription || s.subscription_id || "Unknown subscription")}
        · Last run: ${esc(s.last_status || "Not reported by Azure")}</span>
      ${managed ? "" : `<span class="no-alerts"> · no identity — cannot write to a key-disabled account</span>`}
    </summary>
    <div class="budget-meta">
      <span>→ ${esc(s.account || "?")}/${esc(s.container || "?")}/${esc(s.folder || "")}</span>
      <span>Coverage: ${esc(s.coverage || s.timeframe || "Not reported")}</span>
      <span>${s.retention === "OverwritePreviousReport" ? "Overwrites previous report" :
        "All runs retained (storage costs grow)"}</span>
      ${s.last_run ? `<span>Last run date: ${esc(s.last_run)}</span>` : ""}
      ${managed ? "<span>managed identity</span>" : ""}
    </div>
    <div class="budget-foot">
      <span class="muted">${[
        s.starts ? `from ${String(s.starts).slice(0, 10)}` : "",
        // Azure returns this and it used to be dropped. On a daily export it is the single
        // most useful fact about a schedule: when it next produces something.
        s.next_run ? `next ${String(s.next_run).slice(0, 10)}` : "",
      ].filter(Boolean).map(esc).join(" · ")}</span>
      ${admin ? `<span>
        ${s.owned ? `<button type="button" class="linkish" data-edit="${esc(s.name)}"
                data-sub="${esc(s.subscription_id)}">Edit cadence</button>` : ""}
        <button type="button" class="linkish" data-run="${esc(s.name)}"
                data-sub="${esc(s.subscription_id)}">Run now</button>
        <button type="button" class="linkish" data-del="${esc(s.name)}"
                data-sub="${esc(s.subscription_id)}">Delete</button>
      </span>` : ""}
    </div>
  </details>`;
}

/**
 * The raw-data export at the foot of the Export data tab.
 *
 * Separate from `wireSettings` because the section it drives is: this moved to sit under the
 * report builder, where someone looking for "get me the rows" actually goes, and the schedules
 * tab kept only the things that create and run exports in Azure.
 */
function wireSelectionExport() {
  const ticked = () =>
    [...$("tabBody").querySelectorAll("#xSubs input:checked")].map((i) => i.value);

  const setAll = (on) => {
    for (const i of $("tabBody").querySelectorAll("#xSubs input")) i.checked = on;
    paintExportNote();
  };
  // Guarded throughout: this runs on every render of the tab, and a section that failed to
  // draw — or a future tab that wires this without one — must not throw and blank everything
  // above it, which is how a working report builder used to disappear.
  const all = $("xAll");
  if (all) all.onclick = () => setAll(true);
  const none = $("xNone");
  if (none) none.onclick = () => setAll(false);
  for (const i of $("tabBody").querySelectorAll("#xSubs input")) i.onchange = paintExportNote;

  function selectionQuery() {
    const src = $("xSource");
    const opt = src?.selectedOptions?.[0];
    return `scope=${encodeURIComponent(ticked().join(","))}` +
      `&fmt=${encodeURIComponent($("xFmt")?.value || "csv")}` +
      `&since=${encodeURIComponent($("xFrom")?.value || "")}` +
      `&until=${encodeURIComponent($("xTo")?.value || "")}` +
      `&source=${encodeURIComponent(src?.value || "")}` +
      `&dataset=${encodeURIComponent(opt?.dataset?.set || "actual")}` +
      `&label=${encodeURIComponent($("xLabel")?.value || "")}`;
  }

  function paintExportNote() {
    const note = $("xNote");
    const btn = $("xExport");
    const dl = $("xDownload");
    const n = ticked().length;
    const from = $("xFrom")?.value;
    const to = $("xTo")?.value;
    const bad = from && to && from > to;
    // The button is the thing that would otherwise fail server-side with "select at least one",
    // so it says no first.
    if (btn) btn.disabled = n === 0 || bad;
    // A link has no disabled state, so an unusable selection has to lose its destination
    // instead — the same treatment the report builder's download links get, and for the same
    // reason: a link that is still clickable would download an error page.
    if (dl) {
      const off = n === 0 || bad;
      dl.classList.toggle("disabled", off);
      dl.setAttribute("aria-disabled", String(off));
      // Removing href is what actually stops it: an anchor with no href is not focusable and
      // does nothing when clicked, so the control cannot be reached round the back by keyboard
      // while it looks unavailable.
      if (off) dl.removeAttribute("href");
      else wireDownloadLink(dl, `/api/archive/export/download?${selectionQuery()}`);
    }
    if (note) {
      note.textContent = bad
        ? "The start date must be on or before the end date."
        : n
          ? `${n} subscription${n === 1 ? "" : "s"}${from || to
            ? `, ${from || "start"} → ${to || "end"}` : ""}`
          : "Tick at least one subscription.";
    }
  }
  // The label is typed rather than picked, so it needs `input` too — the download URL carries it
  // and would otherwise keep whatever the label was when the tab rendered.
  for (const id of ["xFrom", "xTo", "xSource", "xFmt"]) {
    const el = $(id);
    if (el) el.onchange = paintExportNote;
  }
  const lbl = $("xLabel");
  if (lbl) lbl.oninput = paintExportNote;
  paintExportNote();

  const exp = $("xExport");
  if (exp) {
    exp.onclick = async () => {
      const chosen = ticked();
      if (!chosen.length) return;
      exp.disabled = true;
      const before = exp.textContent;
      const note = $("xNote");
      exp.textContent = "Saving…";
      if (note) note.textContent = "Reading the warehouse and writing to storage…";
      try {
        const res = await fetch(`/api/archive/export?${selectionQuery()}`, { method: "POST" });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          toast(typeof d.detail === "string" ? d.detail : "Could not export.", "err");
          return;
        }
        const kb = Math.round((d.bytes || 0) / 1024).toLocaleString();
        const span = d.since || d.until ? ` for ${d.since || "start"} → ${d.until || "end"}` : "";
        toast(`Exported ${(d.rows || 0).toLocaleString()} rows${span} (${kb} KB) to ${d.name}.`);
      } catch (error) {
        toast("Could not reach the server. Check storage before retrying; the export may have completed.", "err");
      } finally {
        exp.disabled = false;
        exp.textContent = before;
        paintExportNote();
      }
    };
  }
}

/** All write targets come from the detected, authorized subscription list. */
function wireSettings(sched) {
  const schedules = sched.schedules || [];
  const dest = sched.destination || {};
  const selector = $("sSub");
  const cadence = $("sEvery");
  const create = $("sCreate");
  const refresh = $("sRefresh");
  const notice = $("sNotice");
  const reload = $("sReload");
  const controls = [...$("tabBody").querySelectorAll("[data-edit]"),
    ...$("tabBody").querySelectorAll("[data-run]"), ...$("tabBody").querySelectorAll("[data-del]")];
  const active = () => settingsData === sched && $("sNotice") === notice;
  const currentCadence = () => scheduleCadences.get(scheduleChoice) ||
    ownedSchedule(sched, scheduleChoice)?.recurrence || "Daily";
  const paint = () => {
    if (!active()) return;
    reload.disabled = settingsBusy;
    controls.forEach((button) => { button.disabled = settingsBusy; });
    if (!selector) return;
    const picked = pickedSubscriptions(sched);
    const blocked = picked.some((s) => ownedSchedule(sched, s.id)?.can_write === false);
    const missingStorage = !dest.account_id || !dest.container;
    const unscheduled = picked.some((s) => !ownedSchedule(sched, s.id));
    selector.disabled = cadence.disabled = settingsBusy;
    create.disabled = settingsBusy || !picked.length || blocked || missingStorage;
    refresh.disabled = settingsBusy || !picked.length || blocked || missingStorage || unscheduled ||
      focusPending || sched.sync?.status === "running";
    const existing = ownedSchedule(sched, scheduleChoice);
    $("sCoverage").textContent = scheduleCoverage(cadence.value) + ".";
    $("sNote").textContent = missingStorage ? "Configure the export Blob destination before saving." :
      blocked ? "This schedule is read only. Choose another subscription or check permissions." :
      picked.length > 1 ? `${picked.length} subscriptions will be listed in the review.` :
      existing ? `Editing ${existing.recurrence || "saved"} cadence; other subscriptions are unchanged.` :
      "Creates one FOCUS export. Nothing changes until you approve.";
  };
  paintSettingsControls = paint;
  reload.onclick = () => {
    // Explicit reload bypasses the server's export cache; see renderSettings.
    if (!settingsBusy) return renderSettings(ctx.loadToken, true);
  };
  $("sCheck").onclick = () => checkFocusStatus(true);
  if (selector) {
    selector.value = scheduleChoice;
    cadence.value = currentCadence();
    selector.onchange = () => {
      scheduleChoice = selector.value;
      cadence.value = currentCadence();
      paint();
    };
    cadence.onchange = () => {
      scheduleCadences.set(scheduleChoice, cadence.value);
      paint();
    };
  }
  paint();
  for (const b of $("tabBody").querySelectorAll("[data-edit]")) {
    b.onclick = () => {
      if (settingsBusy || !active()) return;
      const existing = schedules.find((s) => s.name === b.dataset.edit && s.subscription_id === b.dataset.sub);
      if (!existing?.owned || existing.can_write === false || !selector) return;
      scheduleChoice = existing.subscription_id;
      scheduleCadences.set(scheduleChoice, existing.recurrence || "Daily");
      selector.value = scheduleChoice;
      cadence.value = currentCadence();
      paint();
      cadence.focus();
    };
  }
  if (create) {
    create.onclick = async () => {
      if (settingsBusy || create.disabled || !active()) return;
      const originToken = ctx.loadToken;
      const recurrence = cadence.value;
      if (!["Daily", "Weekly", "Monthly"].includes(recurrence)) return;
      const plans = pickedSubscriptions(sched).map((sub) => ({
        sub, existing: ownedSchedule(sched, sub.id),
      }));
      if (!plans.length) return;
      if (plans.some(({ existing }) => existing && !existing.etag)) {
        notice.textContent = "The saved schedule version is unavailable. Reload the list before editing.";
        return;
      }
      const review = plans.map(({ sub, existing }) =>
        `${sub.name || sub.id} (${sub.id}): ${existing ? `update ${existing.recurrence} → ${recurrence}` : `create ${recurrence}`}` +
        `\nDestination: ${scheduleDestination(dest, sub.id)}`).join("\n\n");
      if (!window.confirm(`Save FOCUS schedules for ${plans.length} subscription${plans.length === 1 ? "" : "s"}?\n\n${review}\n\nCoverage: ${scheduleCoverage(recurrence)}.\nSaving may create a managed identity and grant Storage Blob Data Contributor on the destination storage account.\nAll runs retained; storage costs grow. No automatic deletion. First run requested now; import waits for a complete Blob report.\nOnly the subscriptions above change. Legacy and external schedules are untouched.`)) return;
      scheduleCadences.set(scheduleChoice, recurrence);
      settingsBusy = true;
      paint();
      const before = create.textContent;
      create.textContent = "Saving…";
      const errors = [];
      const unstarted = [];
      let saved = 0;
      try {
        for (const { sub, existing } of plans) {
          if (active()) notice.textContent = `Saving ${sub.name || sub.id}… Azure configures export identity and storage access.`;
          try {
            const res = await fetch("/api/schedules", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                subscription: sub.id, metric: "FocusCost", recurrence,
                run_now: true, etag: existing?.etag || null,
              }),
            });
            const d = await res.json().catch(() => ({}));
            if (!res.ok) {
              errors.push(`${sub.name || sub.id}: ${writeError(d, "Could not save the schedule.", res.status)}`);
              continue;
            }
            saved++;
            scheduleCadences.set(sub.id, recurrence);
            if (d.first_run_started === false) {
              const reason = typeof d.first_run_error === "string" && d.first_run_error.trim()
                ? d.first_run_error : "Check export storage access.";
              errors.push(`${sub.name || sub.id}: schedule saved, but the first export did not start. ${reason} Use Refresh latest to retry.`);
              unstarted.push(sub.id);
            } else {
              markFocusRequested([sub.id]);
              if (d.sync_started === false && active())
                notice.textContent = d.sync_note || "Report requested; automatic ingestion will pick it up after the active import.";
            }
          } catch {
            errors.push(`${sub.name || sub.id}: could not reach the server. Reload the list to check whether the save completed before retrying.`);
          }
        }
        if (focusPending && unstarted.length) markFocusRequested(unstarted);
        settingsNotice = `${saved} of ${plans.length} schedule${plans.length === 1 ? "" : "s"} saved.` +
          (focusPending ? " Export requested; waiting for a complete report and import." : "") +
          (errors.length ? ` ${errors.join(" ")}` : "");
        if (errors.length) toast(errors.join("\n"), "err");
        if (originToken === ctx.loadToken) await renderSettings(originToken);
      } finally {
        settingsBusy = false;
        create.textContent = before;
        paintSettingsControls();
      }
    };
  }
  if (refresh) refresh.onclick = () => refreshLatest({ globalScope: false });

  for (const b of $("tabBody").querySelectorAll("[data-run]")) {
    b.onclick = async () => {
      if (settingsBusy || b.disabled || !active()) return;
      const schedule = schedules.find((s) => s.name === b.dataset.run && s.subscription_id === b.dataset.sub);
      if (!window.confirm(`Run ${b.dataset.run} on ${b.dataset.sub} now?\nDestination: ${schedule?.account}/${schedule?.container}/${schedule?.folder}\nCoverage: ${schedule?.coverage || "as configured"}\nRetention: ${schedule?.retention || "as configured"}. Azure and storage charges may apply.`)) return;
      settingsBusy = true;
      paint();
      try {
        const q = `subscription=${encodeURIComponent(b.dataset.sub)}&name=${encodeURIComponent(b.dataset.run)}`;
        const res = await fetch(`/api/schedules/run?${q}`, { method: "POST" });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          settingsNotice = `${b.dataset.run} requested. This runs its configured coverage; no new import is confirmed yet.`;
          if (schedule?.owned) {
            markFocusRequested([b.dataset.sub]);
            queueFocusCheck();
          }
          if (active()) notice.textContent = settingsNotice;
        } else {
          toast(writeError(d, "Could not start the export.", res.status, false), "err");
        }
      } catch {
        toast("Could not reach the server.", "err");
      } finally {
        settingsBusy = false;
        paintSettingsControls();
      }
    };
  }

  for (const b of $("tabBody").querySelectorAll("[data-del]")) {
    b.onclick = async () => {
      if (settingsBusy || b.disabled || !active()) return;
      const originToken = ctx.loadToken;
      if (!window.confirm(`Delete schedule ${b.dataset.del} on ${b.dataset.sub}?\nFuture runs stop. All reports already stored remain untouched and continue incurring storage charges.`)) return;
      settingsBusy = true;
      paint();
      try {
        const q = `subscription=${encodeURIComponent(b.dataset.sub)}&name=${encodeURIComponent(b.dataset.del)}`;
        const res = await fetch(`/api/schedules?${q}`, { method: "DELETE" });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          toast(`${b.dataset.del} deleted. Data it already wrote is untouched.`);
          if (originToken === ctx.loadToken) await renderSettings(originToken);
        } else {
          toast(writeError(d, "Could not delete the schedule.", res.status), "err");
        }
      } catch {
        toast("Could not reach the server.", "err");
      } finally {
        settingsBusy = false;
        paintSettingsControls();
      }
    };
  }
}

function writeError(data, fallback, status, scheduleEdit = true) {
  if (scheduleEdit && (status === 409 || status === 412))
    return "The schedule changed. Reload the list and review its current cadence before saving again.";
  return typeof data?.detail === "string" ? data.detail : fallback;
}

function focusIdentity(sync) {
  return `${sync?.started || ""}|${sync?.finished || ""}|${sync?.status || "idle"}`;
}

function normalizeFocusSync(data) {
  const sync = data || { status: "idle" };
  const rows = Array.isArray(sync.subscriptions) ? sync.subscriptions : [];
  const latest = (field) => rows.map((row) => row[field]).filter((value) => typeof value === "string").sort().at(-1);
  return { ...sync, started: sync.started || latest("started"),
    finished: sync.status === "running" ? null : sync.finished || latest("finished") };
}

function focusRowIdentity(row, sync) {
  return `${row?.started || sync?.started || ""}|${row?.finished || row?.last_ingested || sync?.finished || ""}`;
}

function markFocusRequested(subscriptions) {
  for (const id of subscriptions) {
    if (!focusTargets.has(id)) {
      const row = settingsData?.sync?.subscriptions?.find((item) => (item.subscription_id || item.subscription) === id);
      focusTargets.set(id, focusRowIdentity(row, settingsData?.sync));
    }
  }
  focusPending = focusNotify = true;
}

async function refreshLatest(options = {}) {
  if (options.globalScope !== false) return refreshGlobalScope(options);
  const sched = settingsData;
  if (!sched || settingsBusy || focusPending || sched.sync?.status === "running") return;
  const selected = pickedSubscriptions(sched);
  if (!selected.length || !sched.destination?.account_id || !sched.destination?.container ||
      selected.some((s) => ownedSchedule(sched, s.id)?.can_write === false)) return;
  if (selected.some((s) => !ownedSchedule(sched, s.id))) {
    toast("Save a FOCUS schedule for each selected subscription before Refresh latest.", "err");
    return;
  }
  const review = selected.map((sub) => `${sub.name || sub.id} (${sub.id})` +
    `\nDestination: ${scheduleDestination(sched.destination, sub.id)}`).join("\n\n");
  if (!window.confirm(`Refresh latest FOCUS costs?\n\n${review}\n\nCoverage: current month to date (MonthToDate), including subscriptions scheduled Monthly. Saved cadences do not change.\nAzure generates a new report in Blob; the backend waits for completion and imports changed days only.\nThe on-demand export may need a managed identity and Storage Blob Data Contributor on the destination storage account. Reports are retained; Azure and storage charges may apply.\nNo legacy or external schedules are changed.`)) return;
  const originToken = ctx.loadToken;
  settingsBusy = true;
  paintSettingsControls();
  try {
    const res = await fetch("/api/schedules/refresh", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: selected.map((s) => s.id) }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(writeError(data, "Could not start Refresh latest.", res.status, false), "err");
      return;
    }
    markFocusRequested(selected.map((s) => s.id));
    settingsNotice = data.status === "already running"
      ? "A refresh is already running. Tracking export and import progress."
      : "Refresh requested. Waiting for Azure to finish the report, then importing changed days.";
    if (originToken === ctx.loadToken && $("sNotice")) {
      $("sNotice").textContent = settingsNotice;
      paintFocusStatus();
      await checkFocusStatus();
    }
  } catch {
    toast("Could not reach the server. Check import status before retrying Refresh latest.", "err");
  } finally {
    settingsBusy = false;
    paintSettingsControls();
  }
}

/** Header-owned tracking is separate from tab stop/resume; it never needs a settings DOM. */
async function refreshGlobalScope({ onStatus = () => {}, isCurrent = () => ctx.loadToken !== -1 } = {}) {
  if (!isCurrent()) return { status: "cancelled", scope: [] };
  window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "settings" }));
  onStatus({ status: "idle" });
  return { status: "setup", scope: scopeIds() };
}

async function refreshLegacyGlobalScope({ onStatus = () => {}, isCurrent = () => ctx.loadToken !== -1 } = {}) {
  if (headerStarting) return { status: "busy", scope: [] };
  if (headerRefresh?.isActive()) return headerRefresh;
  headerRefresh?.stop();
  const originToken = ctx.loadToken;
  const current = () => ctx.loadToken !== -1 && isCurrent();
  if (!current()) return { status: "cancelled", scope: [] };
  headerStarting = true;
  try {
    let sched;
    try {
      sched = await get(`/api/schedules${qs()}`, { timeout: 45000 });
    } catch (error) {
      if (current()) toast(scheduleReadError(error), "err");
      return { status: "failed", scope: [] };
    }
    // Discovery belongs to the scope captured by loadFamily, not whichever selection
    // happens to be visible when an earlier request finishes.
    if (!current() || ctx.loadToken !== originToken) return { status: "cancelled", scope: [] };
    const detected = scheduleSubscriptions(sched);
    const selected = detected.filter((sub) => sub.can_write === true &&
      ownedSchedule(sched, sub.id)?.can_write !== false);
    if (!selected.length) {
      toast("No selected subscriptions have verified export write access. Check access in Cost exports.", "err");
      return { status: "unavailable", scope: [] };
    }
    const scope = selected.map((sub) => sub.id);
    const missing = selected.filter((sub) => !ownedSchedule(sched, sub.id));
    if (missing.length || !sched.destination?.account_id || !sched.destination?.container) {
      toast(missing.length ? `Save a FOCUS schedule first: ${missing.map((sub) => sub.name || sub.id).join(", ")}.`
        : "Configure the FOCUS Blob destination in Cost exports before Refresh latest.", "err");
      return { status: "unavailable", scope };
    }
    const initial = normalizeFocusSync(sched.sync);
    if (initial.status === "running") {
      return trackHeaderRefresh(scope, initial, { onStatus, current, alreadyRunning: true });
    }
    const skipped = detected.filter((sub) => !scope.includes(sub.id));
    const review = selected.map((sub) => `${sub.name || sub.id} (${sub.id})` +
      `\nDestination: ${scheduleDestination(sched.destination, sub.id)}`).join("\n\n");
    if (!window.confirm(`Refresh latest FOCUS costs for the selected writable subscriptions?\n\n${review}` +
      (skipped.length ? `\n\nNot included (read only or export list unavailable): ${skipped.map((sub) => sub.name || sub.id).join(", ")}.` : "") +
      "\n\nCoverage: current month to date (MonthToDate), including Monthly schedules. Saved cadences do not change." +
      "\nAzure generates new Blob reports; the backend waits for completion and imports changed days only." +
      "\nExports use managed identities and Storage Blob Data Contributor access. Reports are retained; Azure and storage charges may apply." +
      "\nNo legacy or external schedules are changed.")) {
      return { status: "cancelled", scope };
    }
    if (!current() || ctx.loadToken !== originToken) return { status: "cancelled", scope };
    try {
      const res = await fetch("/api/schedules/refresh", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (current()) toast(writeError(data, "Could not start Refresh latest.", res.status, false), "err");
        return { status: "failed", scope };
      }
      if (!current()) return { status: "started", scope };
      return trackHeaderRefresh(scope, initial, {
        onStatus, current, alreadyRunning: data.status === "already running",
      });
    } catch {
      if (current()) toast("Could not reach the server. Check import status before requesting another refresh.", "err");
      return { status: "failed", scope };
    }
  } finally {
    headerStarting = false;
  }
}

function trackHeaderRefresh(scope, initial, { onStatus, current, alreadyRunning }) {
  let timer = null;
  let controller = null;
  let stopped = false;
  const initialRows = Array.isArray(initial.subscriptions) ? initial.subscriptions : [];
  const active = initialRows.filter((row) => ["running", "waiting", "reading"].includes(row.status))
    .map((row) => row.subscription_id || row.subscription);
  const awaited = alreadyRunning && active.length ? scope.filter((id) => active.includes(id)) : scope;
  const baselines = new Map(awaited.map((id) => [id, focusRowIdentity(
    initialRows.find((row) => (row.subscription_id || row.subscription) === id), initial)]));
  const handle = {
    status: alreadyRunning ? "already running" : "started", scope: [...scope],
    isActive: () => !stopped && current(),
    stop() {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      controller?.abort();
      controller = null;
      if (headerRefresh === handle) headerRefresh = null;
    },
  };
  headerRefresh = handle;
  const valid = () => !stopped && current();
  const schedule = (delay) => {
    if (!valid()) { handle.stop(); return; }
    timer = setTimeout(() => { timer = null; void poll(); }, delay);
  };
  const poll = async () => {
    if (!valid()) { handle.stop(); return; }
    controller = new AbortController();
    try {
      const data = await get(`/api/schedules/refresh?scope=${encodeURIComponent(scope.join(","))}`, {
        timeout: 20000, signal: controller.signal,
      });
      if (!valid()) { handle.stop(); return; }
      let sync = normalizeFocusSync(data.sync || data);
      if (!["idle", "running", "ready", "partial", "failed"].includes(sync.status))
        throw Error("Invalid import status");
      const rows = Array.isArray(sync.subscriptions) ? sync.subscriptions : [];
      const targets = rows.filter((row) => baselines.has(row.subscription_id || row.subscription));
      const complete = awaited.length > 0 && targets.length === awaited.length && targets.every((row) =>
        ["ready", "failed"].includes(row.status) && (row.finished || row.last_ingested || sync.finished) &&
        focusRowIdentity(row, sync) !== baselines.get(row.subscription_id || row.subscription));
      if (complete) {
        const failed = targets.filter((row) => row.status === "failed").length;
        sync = { ...sync, status: failed === targets.length ? "failed" : failed ? "partial" : "ready" };
        handle.status = sync.status;
        onStatus(sync);
        const identity = focusIdentity(sync);
        if (identity !== focusCompleted) {
          focusCompleted = identity;
          const message = sync.status === "ready" ? "Refresh completed. Latest available costs have been imported." :
            "Refresh finished with issues. Check per-subscription import status in Cost exports.";
          toast(message, sync.status === "ready" ? "ok" : "err");
          if (sync.status !== "failed" && typeof window.dispatchEvent === "function" &&
              typeof CustomEvent === "function")
            window.dispatchEvent(new CustomEvent("cloudlens:focus-synced", { detail: sync }));
        }
        handle.stop();
        return;
      }
      onStatus({ ...sync, status: "running", finished: null });
      schedule(sync.status === "running" ? 3000 : 15000);
    } catch (error) {
      if (!valid() || controller?.signal.aborted) { handle.stop(); return; }
      onStatus({ status: "running", subscriptions: [], detail: scheduleReadError(error, true), read_error: true });
      schedule(10000);
    } finally {
      controller = null;
    }
  };
  onStatus({ ...initial, status: "running", finished: null,
    detail: alreadyRunning ? "Tracking the active FOCUS refresh." : "Refresh requested; waiting for completed Azure reports." });
  schedule(0);
  return handle;
}

function paintFocusStatus() {
  if (!$("sSyncDetails") || !settingsData) return;
  const sync = settingsData.sync || { status: "idle" };
  const status = focusPending ? "running" : sync.status;
  const labels = { idle: "No refresh running", running: "Refreshing latest — export → Blob → import",
    ready: "Latest refresh completed", partial: "Refresh completed with issues", failed: "Refresh failed" };
  $("sSyncSummary").textContent = labels[status] || "Import status";
  const results = Array.isArray(sync.subscriptions) ? sync.subscriptions : [];
  $("sSyncDetails").innerHTML = `${sync.detail ? `<p class="muted note">${esc(sync.detail)}</p>` : ""}
    ${sync.started ? `<p class="muted note">Started: ${esc(sync.started)}${sync.finished ? ` · Finished: ${esc(sync.finished)}` : ""}</p>` : ""}
    ${scheduleSubscriptions(settingsData).map((sub) => {
      const result = results.find((item) => (item.subscription_id || item.subscription) === sub.id) || {};
      const imported = result.last_import || result.last_imported_at || result.last_ingested ||
        (["ready", "imported", "unchanged", "skipped"].includes(result.status) ? result.finished || sync.finished : null);
      if (imported) lastImports.set(sub.id, imported);
      const counts = [["changed_days", "changed days"], ["unchanged_days", "unchanged days"], ["rows", "rows"]]
        .filter(([field]) => Number.isFinite(result[field]))
        .map(([field, label]) => `${result[field].toLocaleString()} ${label}`).join(" · ");
      return `<p class="muted note"><strong>${esc(sub.name || sub.id)}</strong> · ${esc(result.status || "No import reported")}
        · Last import: ${esc(lastImports.get(sub.id) || "Not yet reported")}
        ${counts ? `<br>${esc(counts)}` : ""}
        ${result.detail ? `<br>${esc(result.detail)}` : ""}
        ${sub.can_write === true ? "" : `<br>${esc(sub.write_reason || "Read-only subscription; export writes are unavailable.")}`}</p>`;
    }).join("")}`;
  paintSettingsControls();
}

function stop() {
  ++permissionGeneration;
  permissionController?.abort();
  permissionController = null;
  if ($("pInspect")) $("pInspect").disabled = false;
  if ($("pWorkload")) $("pWorkload").disabled = false;
  ++workflowGeneration;
  workflowCleanup();
  ++focusGeneration;
  if (focusTimer !== null) clearTimeout(focusTimer);
  focusTimer = null;
  focusController?.abort();
  focusController = null;
  if ($("sCheck")) $("sCheck").disabled = false;
}

function resume(token, tab) {
  if (tab && tab !== "settings") return;
  if ($("costData") && settingsData?.costData) {
    settingsToken = token;
    wireCostData();
    return;
  }
  wirePermissions();
  if (!$("sSyncDetails") || !settingsData) return;
  settingsToken = token;
  paintSettingsControls();
  wireFocusWorkflow(settingsData);
  if (focusPending || settingsData.sync?.status === "running") return checkFocusStatus();
}

function queueFocusCheck(delay = 3000) {
  if (focusTimer !== null) clearTimeout(focusTimer);
  focusTimer = null;
  if (settingsToken !== ctx.loadToken || !$("sSyncDetails")) return;
  focusTimer = setTimeout(() => {
    focusTimer = null;
    void checkFocusStatus();
  }, delay);
}

async function checkFocusStatus(manual = false) {
  if (!settingsData || settingsToken !== ctx.loadToken || !$("sSyncDetails") || focusController) return;
  if (focusTimer !== null) clearTimeout(focusTimer);
  focusTimer = null;
  const generation = focusGeneration;
  const controller = new AbortController();
  focusController = controller;
  const check = $("sCheck");
  if (check) check.disabled = true;
  try {
    const scope = scheduleSubscriptions(settingsData).map((s) => s.id);
    if (!scope.length) return;
    const data = await get(`/api/schedules/refresh?scope=${encodeURIComponent(scope.join(","))}`, {
      timeout: 20000, signal: controller.signal,
    });
    if (generation !== focusGeneration || settingsToken !== ctx.loadToken || !$("sSyncDetails")) return;
    let sync = normalizeFocusSync(data.sync || data);
    if (!["idle", "running", "ready", "partial", "failed"].includes(sync.status))
      throw new Error("Invalid import status");
    if (focusTargets.size) {
      const results = Array.isArray(sync.subscriptions) ? sync.subscriptions : [];
      // A batch can queue behind another import. Old ready rows must not complete a
      // newly requested refresh, nor may one ready subscription stand in for the rest.
      const pending = [...focusTargets].filter(([id, baseline]) => {
        const row = results.find((item) => (item.subscription_id || item.subscription) === id);
        return !row || !["ready", "failed"].includes(row.status) ||
          !(row.finished || row.last_ingested || sync.finished) || focusRowIdentity(row, sync) === baseline;
      });
      if (pending.length) {
        const names = pending.map(([id]) => scheduleSubscriptions(settingsData).find((sub) => sub.id === id)?.name || id);
        settingsData.sync = { ...sync, status: "running", finished: null,
          detail: sync.status === "running" ? sync.detail :
            `Waiting for complete reports and imports for ${names.join(", ")}. Automatic ingestion checks every ${Math.round((sync.poll_seconds || 900) / 60)} minutes.` };
        focusPending = true;
        paintFocusStatus();
        queueFocusCheck(sync.status === "running" ? 3000 : 15000);
        return;
      }
      const targetRows = results.filter((row) => focusTargets.has(row.subscription_id || row.subscription));
      const failed = targetRows.filter((row) => row.status === "failed").length;
      sync = { ...sync, status: failed === targetRows.length ? "failed" : failed ? "partial" : "ready" };
      focusTargets.clear();
    }
    settingsData.sync = sync;
    focusPending = sync.status === "running";
    paintFocusStatus();
    const completed = sync.finished && ["ready", "partial", "failed"].includes(sync.status);
    if (completed) {
      settingsNotice = sync.status === "ready" ? "Refresh completed. Latest available costs have been imported." :
        "Refresh finished with issues. Open import status for each subscription's result.";
      $("sNotice").textContent = settingsNotice;
      const identity = focusIdentity(sync);
      if (identity !== focusCompleted) {
        focusCompleted = identity;
        if (focusNotify) toast(settingsNotice, sync.status === "ready" ? "ok" : "err");
        if (sync.status !== "failed" && typeof window.dispatchEvent === "function" &&
            typeof CustomEvent === "function") {
          window.dispatchEvent(new CustomEvent("cloudlens:focus-synced", { detail: sync }));
        }
      }
      focusNotify = false;
    } else if (manual && sync.status === "idle") {
      $("sNotice").textContent = "No refresh is running. Existing costs and saved cadences are unchanged.";
    }
    if (sync.status === "running") queueFocusCheck();
  } catch (error) {
    if (generation !== focusGeneration || controller.signal.aborted) return;
    $("sNotice").textContent = scheduleReadError(error, true);
    if (focusPending || settingsData.sync?.status === "running") queueFocusCheck(10000);
  } finally {
    if (focusController === controller) focusController = null;
    if (check && generation === focusGeneration) check.disabled = false;
  }
}

// ================================================================ history

/**
 * What Azure changed its mind about.
 *
 * The warehouse only ever holds the latest read, and Azure restates cost data for days after
 * the fact — so the dashboard physically cannot answer "what did we think this cost when we
 * reported it". Two archived snapshots can, which is the reason they are kept.
 *
 * Deliberately framed as restatement rather than as change: both figures describe the *same*
 * period. A difference here is not spend that happened, it is spend that was already there and
 * has only now been counted.
 */
let historyState = null;
let comparisonSequence = 0;
let historySequence = 0;

async function renderHistory(token) {
  const sequence = ++historySequence;
  ++comparisonSequence;
  const a = await get("/api/archive?limit=200", { timeout: 45000 });
  if (token !== ctx.loadToken || sequence !== historySequence) return;

  if (!a.enabled || !a.reachable) {
    setContent($("tabBody"), `<section class="card">
      <h3>Archive unavailable</h3>
      <div class="banner warn">${esc(a.error || "The archive is not configured.")}</div>
      <p class="muted">Refresh creates dated snapshots. Compare costs once two snapshots exist for the same dataset.</p></section>`);
    return;
  }

  const sets = a.comparable || [];
  if (!sets.length) {
    const have = Object.entries(a.days || {})
      .map(([k, v]) => `${k} (${v.length} day${v.length === 1 ? "" : "s"})`).join(", ");
    setContent($("tabBody"), `<section class="card">
      <h3>More history needed</h3>
      <p class="muted">A comparison needs two snapshots of the same dataset on different days.
        The archive currently holds ${esc(have || "nothing")}. Refresh again tomorrow and this
        tab will show what Azure restated overnight.</p></section>`);
    return;
  }

  const pick = historyState && sets.includes(historyState.dataset)
    ? historyState
    : { dataset: sets[0], group_by: "ServiceName" };
  const days = a.days[pick.dataset] || [];
  historyState = {
    ...pick,
    later: days.includes(pick.later) ? pick.later : days[0],
    earlier: days.includes(pick.earlier) && pick.earlier !== days[0] ? pick.earlier : days[1],
    _days: a.days,
    _sets: sets,
  };

  setContent($("tabBody"), historyMarkup(a));
  wireHistory();
  await loadComparison(token);
}

const HISTORY_DIMENSIONS = [
  ["ServiceName", "Service"],
  ["SubAccountName", "Subscription"],
  ["ResourceGroup", "Resource group"],
  ["ResourceName", "Resource"],
  ["RegionName", "Region"],
  ["MeterName", "Meter"],
];

function historyMarkup(a) {
  const s = historyState;
  const days = a.days[s.dataset] || [];
  const label = { focus: "FOCUS", amortized: "Amortized", actual: "Actual" };

  return `
    <section class="card">
      <div class="tagpick-head">
        <h3>Cost restatements</h3>
        <span class="muted note">${a.count} snapshot(s) in ${esc(a.account)}/${esc(a.container)}</span>
      </div>
      <p class="muted note">Compare equivalent usage coverage across snapshot dates. Newly covered days are separate from restatements.</p>

      <div class="budget-grid">
        <label class="bfield"><span>Dataset</span>
          <select id="hSet">${s._sets.map((k) =>
            `<option value="${esc(k)}" ${k === s.dataset ? "selected" : ""}>${esc(label[k] || k)}</option>`).join("")}</select>
        </label>
        <label class="bfield"><span>Read on</span>
          <select id="hEarlier">${days.map((d) =>
            `<option value="${esc(d)}" ${d === s.earlier ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
        </label>
        <label class="bfield"><span>Compared with</span>
          <select id="hLater">${days.map((d) =>
            `<option value="${esc(d)}" ${d === s.later ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
        </label>
        <label class="bfield"><span>Broken down by</span>
          <select id="hGroup">${HISTORY_DIMENSIONS.map(([v, t]) =>
            `<option value="${esc(v)}" ${v === s.group_by ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
        </label>
      </div>
      <details class="view-help" data-state-key="history-about">
        <summary>About this view</summary>
        <p>Azure can revise cost data for several days after usage. Both snapshots describe the
          same spend period as read on different days, showing revisions to previously incurred costs.
          The warehouse holds only the latest read; archived snapshots preserve earlier reported figures.</p>
      </details>
    </section>
    <div id="hResult"></div>`;
}

function wireHistory() {
  const set = $("hSet");
  if (set) {
    set.onchange = () => {
      // Changing dataset invalidates the day choices, so they are re-derived rather than kept.
      historyState = { dataset: set.value, group_by: historyState.group_by,
                       _days: historyState._days, _sets: historyState._sets };
      renderHistory(ctx.loadToken);
    };
  }
  for (const id of ["hEarlier", "hLater", "hGroup"]) {
    const el = $(id);
    if (!el) continue;
    el.onchange = () => {
      historyState = {
        ...historyState,
        earlier: $("hEarlier").value,
        later: $("hLater").value,
        group_by: $("hGroup").value,
      };
      loadComparison(ctx.loadToken);
    };
  }
}

async function loadComparison(token) {
  const sequence = ++comparisonSequence;
  const host = $("hResult");
  if (!host) return;
  const s = historyState;
  if (!s.earlier || !s.later || s.earlier === s.later) {
    host.innerHTML = `<section class="card"><p class="muted empty">Pick two different days.</p></section>`;
    return;
  }
  host.innerHTML = `<section class="card"><p class="muted empty">Reading both snapshots…</p></section>`;
  try {
    const q = `dataset=${encodeURIComponent(s.dataset)}&earlier=${encodeURIComponent(s.earlier)}` +
      `&later=${encodeURIComponent(s.later)}&group_by=${encodeURIComponent(s.group_by)}&top=20` +
      `&scope=${encodeURIComponent(scopeIds().join(","))}`;
    const d = await get(`/api/archive/compare?${q}`, { timeout: 60000 });
    if (token !== ctx.loadToken || sequence !== comparisonSequence || host !== $("hResult")) return;
    host.innerHTML = comparisonMarkup(d);
    wireTables(host);
  } catch (err) {
    if (token !== ctx.loadToken || sequence !== comparisonSequence || host !== $("hResult")) return;
    host.innerHTML = `<section class="card">
      <div class="banner err">${esc(err.message || String(err))}</div></section>`;
  }
}

function comparisonMarkup(d) {
  const cur = d.currency;
  const comparable = d.total_delta != null;
  const up = d.total_delta > 0;
  const same = comparable && Math.abs(d.total_delta) < 0.005;

  return `
    ${!comparable ? '<div class="banner err">No shared subscription/day usage coverage. A restatement cannot be calculated.</div>' : ""}
    ${d.coverage_changed ? `<div class="banner">Coverage changed separately from restatements:
      ${Number(d.coverage_added?.coverage_keys || 0)} new covered subscription/day keys
      (${esc(money(d.coverage_added?.cost, cur))});
      ${Number(d.coverage_removed?.coverage_keys || 0)} keys no longer covered
      (${esc(money(d.coverage_removed?.cost, cur))}). These amounts are excluded from the comparison below.</div>` : ""}
    ${d.comparison_period ? `<p class="muted note">Shared usage period:
      ${esc(d.comparison_period.from)} → ${esc(d.comparison_period.to)} (${d.comparison_period.days} usage dates).</p>` : ""}
    ${kpiRow([
      { label: `Comparable total as read ${d.earlier.day}`, value: comparable ? esc(money(d.earlier.cost, cur)) : "—",
        sub: `${d.earlier.rows.toLocaleString()} rows` },
      { label: `Comparable total as read ${d.later.day}`, value: comparable ? esc(money(d.later.cost, cur)) : "—",
        sub: `${d.later.rows.toLocaleString()} rows` },
      { label: "Restated by",
        value: comparable ? `${up ? "+" : ""}${esc(money(d.total_delta, cur, 2))}` : "—",
        cls: same ? "" : up ? "up" : "down",
        sub: d.total_percent == null ? "" : `${d.total_percent > 0 ? "+" : ""}${d.total_percent}%` },
      { label: "Rows added", value: `${d.row_delta > 0 ? "+" : ""}${d.row_delta.toLocaleString()}`,
        sub: "record-count change within comparable coverage" },
    ])}

    ${same ? `<div class="banner ok">Nothing moved. The two reads of
      ${esc(d.earlier.from || "")} → ${esc(d.earlier.to || "")} agree within display precision.
      Azure may still restate this period later.</div>` : ""}

    ${d.changes.length
      ? table(`Top restatements by ${esc((HISTORY_DIMENSIONS.find((x) => x[0] === d.group_by) || [, d.group_by])[1])}`,
          d.changes,
          [
            { label: (HISTORY_DIMENSIONS.find((x) => x[0] === d.group_by) || [, "Key"])[1],
              cell: (r) => esc(r.key || "—") },
            { label: `As read ${d.earlier.day}`, num: true, value: (r) => r.was,
              cell: (r) => moneyHtml(r.was, cur, 2) },
            { label: `As read ${d.later.day}`, num: true, value: (r) => r.now,
              cell: (r) => moneyHtml(r.now, cur, 2) },
            { label: "Change", num: true, value: (r) => r.delta,
              cell: (r) => `<span class="delta ${r.delta > 0 ? "up" : "down"}">${
                r.delta > 0 ? "+" : ""}${esc(money(r.delta, cur, 2))}</span>` },
            { label: "%", num: true, value: (r) => r.percent ?? 0,
              cell: (r) => r.percent == null ? "—"
                : `<span class="delta ${r.percent > 0 ? "up" : "down"}">${
                  r.percent > 0 ? "+" : ""}${r.percent}%</span>` },
          ],
          "Changed rows only. Resources absent from one snapshot show zero on that side.")
      : ""}`;
}


  return { renderReport, renderSettings, renderHistory, refreshLatest, stop, resume };
}
