/** Lazy optimize views. Dependencies are supplied by the shell; no reverse imports. */
import { createSavingsClock, createSavingsScan, savingsPresentation } from "./dashboard-savings-scan.js";
import { currencyAmountList, nativeAmount, nativeRowTotal } from "./dashboard-currencies.js";
const finite = value => typeof value === "number" && Number.isFinite(value);

export function createTabs(ctx) {
  const { $, bar, cached, cardHead, drawChart, esc, fail, get, key, kpiRow, live, money, moneyHtml, portalLinkMarkup, portalTargets, post, qs, selectTab, setContent, stableKey, table, title, wireBudgetLinks, wireTables } = ctx;

/**
 * One savings list, with the duplicates collapsed.
 *
 * Replaces six tabs — Orphaned resources, Rightsizing, Shutdown savings, Rate optimization,
 * Advisor and Commitments — that answered the same question separately and overlapped while
 * doing it. Each had its own total, and those totals shared resources, so the only way to know
 * what the estate could actually save was to work out by hand which rows were the same VM.
 *
 * The server does that reconciliation now. What is left for this to do is show the result in a
 * way that keeps the reader's trust: say how much is billed today versus estimated, mark the
 * rows that more than one scan agrees on, and never present a number without saying where it
 * came from.
 */
let savingsFilter = "all";
let savingsData = null;
let savingsError = null;
let savingsFingerprint = "";
let recommendationFor = null;
const statusClock = createSavingsClock({
  isCurrent: token => ctx.isCurrent(token),
  onTick: token => {
    if (savingsData?.freshness?.generated_at) paintSavingsStatus(token);
  },
});
const scan = createSavingsScan({
  request: ({ refresh, signal }) => get(`/api/dashboard/savings/progress${qs()}${refresh ? "&refresh=true" : ""}`,
    { timeout: 30000, signal }),
  isCurrent: token => ctx.isCurrent(token),
  onData: (data, token) => {
    savingsError = null;
    savingsData = data;
    const fingerprint = JSON.stringify([
      data.opportunities, data.categories, data.billed_total, data.projected_total,
      data.unavailable, data.commitment_count, data.commitment_annual, data.commitment_unpriced_count, data.note,
      !(data.opportunities || []).length ? data.scan?.status : null,
      !(data.opportunities || []).length ? data.scan?.errors : null,
    ]);
    if (fingerprint !== savingsFingerprint || !$("svStatus")) {
      savingsFingerprint = fingerprint;
      paintSavings(data, token);
    } else paintSavingsStatus(token);
    if (data.scan?.status !== "running" && data.freshness?.generated_at !== recommendationFor) {
      recommendationFor = data.freshness?.generated_at;
      if ((data.opportunities || []).length) loadRecommendation();
    }
  },
  onError: (err, token) => {
    savingsError = err?.message || "Could not refresh the savings scan.";
    paintSavingsStatus(token);
  },
});

async function renderSavings(token) {
  savingsError = null;
  recommendationFor = null;
  if (savingsData) paintSavings(savingsData, token);
  else {
    setContent($("tabBody"), `<section class="portal-workbench">${savingsStatusMarkup()}</section>
      <div id="svPending" class="card" role="status">
        <h3>Scanning for savings</h3>
        <p class="muted">Four sources; results appear as each scan completes.</p>
      </div>`);
    wireScanRefresh(token);
  }
  statusClock.start(token);
  await scan.start(token);
}

function savingsStatusMarkup() {
  const data = savingsData || {};
  const { generated, stamp, validStamp, ageText, running, state, failed, warning, access, provisional } =
    savingsPresentation(data, savingsError);
  return `<section class="card sv-status${failed || warning ? " sv-status-error" : ""}" id="svStatus">
    <div class="fr-actions">
      <div>
        <strong role="status" aria-live="polite" id="svProgress">${esc(state)}</strong>
        <span class="muted sv-age">${generated && validStamp
          ? `Generated <time datetime="${esc(generated)}">${esc(stamp.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC"))}</time> · ${esc(ageText)}`
          : "No completed scan yet"}</span>
      </div>
      <button type="button" id="svRefresh" class="ghost" ${running ? "disabled" : ""}>${running ? "Refreshing…" : "Refresh scan"}</button>
    </div>
    ${failed ? `<p class="muted warn">${esc(failed)}</p>` : ""}
    ${warning && warning !== failed ? `<p class="muted warn">${access
      ? "Azure refused this identity, so these sources are unreadable until it can read a selected subscription —"
      : "Latest scan source failures:"} ${esc(warning)}</p>` : ""}
    ${provisional ? '<p class="muted note">Provisional totals: incoming findings are deduplicated; counts and totals may change.</p>' : ""}
    ${running && (data.opportunities || []).length ? '<p class="muted note">Cards and inputs remain available during the scan.</p>' : ""}
  </section>`;
}

function paintSavingsStatus(token) {
  const status = $("svStatus");
  if (!status) return;
  const template = document.createElement("template");
  template.innerHTML = savingsStatusMarkup();
  // Restore keyboard focus even on metadata-only polls.
  const next = template.content.firstElementChild;
  const age = status.querySelector(".sv-age");
  const nextAge = next.querySelector(".sv-age");
  if (age && nextAge && age.innerHTML !== nextAge.innerHTML) {
    age.replaceChildren(...nextAge.cloneNode(true).childNodes);
  }
  if (status.innerHTML === next.innerHTML && status.className === next.className) return;
  setContent(status, next.innerHTML);
  status.className = next.className;
  wireScanRefresh(token);
}

function wireScanRefresh(token) {
  const refresh = $("svRefresh");
  if (!refresh) return;
  refresh.onclick = () => {
    savingsError = null;
    if (savingsData) savingsData = {
      ...savingsData,
      scan: { ...savingsData.scan, status: "running", completed: [], errors: {} },
      freshness: { ...savingsData.freshness, refreshing: true },
    };
    refresh.disabled = true;
    refresh.textContent = "Refreshing…";
    paintSavingsStatus(token);
    scan.start(token, true);
  };
}

function paintSavings(d, token) {

  const opps = d.opportunities || [];
  const cur = d.currency;
  const cmtHtml = commitmentPointer(d);

  if (!opps.length) {
    setContent($("tabBody"), `<section class="portal-workbench" aria-label="Savings summary">
      ${savingsStatusMarkup()}${kpiRow([{ label: "Opportunities", value: "0" }])}</section>
      <p class="muted empty">${d.scan?.status === "running"
        ? "Scanning — opportunities will appear as sources finish."
        : d.partial_basis === "access"
          ? "Azure refused this identity for some sources; the result covers only what it can read."
        : d.scan?.status === "error" || Object.keys(d.scan?.errors || {}).length
          ? "Some sources could not be read; the result is incomplete."
        : "No savings opportunities found in this scope."}
      ${d.error ? esc(d.error) : ""}</p>
      ${cmtHtml}`);
    wireCommitmentPointer();
    wireScanRefresh(token);
    return;
  }

  const cats = (d.categories || []).filter((c) => c.count > 0);
  const unpriced = opps.filter(o => !finite(o.window)).length;
  const projectedUnpriced = opps.filter(o => !finite(o.window)
    && !(o.sources || []).some(s => s.basis === "billed")).length;
  const shown = () => (savingsFilter === "all"
    ? opps : opps.filter((o) => o.category === savingsFilter));

  const kpis = [
    {
      label: "Potential savings", value: finite(d.billed_total) ? money(d.billed_total, cur, 2) : "Not available",
      sub: `billing-based estimate · ${d.days}d source window`, cls: "good",
    },
    {
      label: "Additional estimate", value: finite(d.projected_total) ? money(d.projected_total, cur, 2) : "Not available",
      sub: `projected by Advisor${projectedUnpriced ? ` · ${projectedUnpriced} unpriced, excluded` : ""}`,
    },
    { label: "Opportunities", value: String(d.count) },
  ];
  // Only worth a tile when there is something to report; a permanent "0 corroborated" reads as
  // a fault rather than as an estate whose scans happen not to overlap.
  if (d.merged) {
    kpis.push({
      label: "Confirmed twice", value: String(d.merged),
      sub: "found by two scans", cls: "good",
    });
  }
  if (d.variants_folded) {
    kpis.push({
      label: "Duplicates folded", value: String(d.variants_folded),
      sub: "re-estimates of the same choice",
    });
  }

  const chip = (id, label, n, total, missing) => `<button class="sv-chip" data-cat="${esc(id)}"
      aria-pressed="${savingsFilter === id}">
      ${esc(label)} <span class="sv-chip-n">${n}</span>
      <span class="sv-chip-m">${finite(total) ? esc(money(total, cur, 0)) : "Not available"}${missing && finite(total) ? " (partial)" : ""}</span>
    </button>`;

  const filters = `<div class="sv-filters" role="group" aria-label="Filter by kind">
      ${chip("all", "Everything", opps.length, d.total, unpriced)}
      ${cats.map((c) => chip(c.id, c.label, c.count, c.total,
        opps.filter(o => o.category === c.id && !finite(o.window)).length)).join("")}
    </div>`;

  const unavailable = Object.keys(d.unavailable || {});
  const warn = unavailable.length
    ? `<p class="muted warn">Could not read ${esc(unavailable.join(", "))}, so the total
       is incomplete.</p>`
    : "";

  setContent($("tabBody"), `<section class="portal-workbench" aria-label="Savings summary">
    ${savingsStatusMarkup()}${kpiRow(kpis)}</section>
    ${warn}
    ${unpriced ? `<p class="muted note">${unpriced} opportunit${unpriced === 1 ? "y has" : "ies have"} no available saving estimate.
      Totals include known amounts only; unavailable estimates are not zero.</p>` : ""}
    <p class="muted">${esc(d.note || "")}</p>
    <details class="card view-help savings-priorities" id="recCard" data-preserve-live>
      <summary>Priorities</summary>
      <p class="muted note" id="recBody">Summary available after the scan.</p>
    </details>
    ${filters}
    <div class="sv-list" id="svList">${shown().map((o) => savingsCard(o, d)).join("")}</div>
    ${cmtHtml}`);

  // Fire-and-forget, deliberately: the ranked list above already answers the question on its
  // own (it is the same data this reads), so a slow or failed summary must never hold up
  // anything the reader can already read without it.
  const redraw = () => {
    setContent($("svList"), shown().map((o) => savingsCard(o, d)).join(""));
    wireSavingsCards();
    wireScanRefresh(token);
  };
  for (const b of $("tabBody").querySelectorAll(".sv-chip")) {
    b.onclick = () => {
      savingsFilter = b.dataset.cat;
      for (const other of $("tabBody").querySelectorAll(".sv-chip")) {
        other.setAttribute("aria-pressed", String(other.dataset.cat === savingsFilter));
      }
      redraw();
    };
  }
  wireSavingsCards();
  wireScanRefresh(token);
  wireCommitmentPointer();
  // Delegated on tabBody, not on individual cards: chip-filter clicks replace #svList's whole
  // innerHTML (see `redraw` above), which would silently orphan a per-card listener. tabBody
  // itself is never replaced, so binding here once survives every filter change.
  wireWhatIf();
}

/**
 * Fills in `#recCard` once the model has judged the same list the ranked table already shows.
 *
 * Separate from `renderSavings` itself so a slow or failed model call never delays the ranked
 * list — that list is already the full, correct answer on its own; this only orders and
 * explains it. The reply belongs to this card's snapshot, including while cached off-screen;
 * a replaced or discarded card must never update a different view.
 */
async function loadRecommendation() {
  const body = $("recBody");
  let r;
  try {
    r = await get(`/api/dashboard/recommendations${qs()}`, { timeout: 45000 });
  } catch (err) {
    if (body?.isConnected) body.textContent = err?.message || "Could not generate a summary right now.";
    return;
  }
  if (!body?.isConnected) return;

  if (r.narrative) {
    body.textContent = r.narrative;
    body.classList.remove("muted", "note");
    body.classList.add("rec-narrative");
  } else {
    body.textContent = r.note || r.error || "Nothing to summarise for this period and scope.";
  }
}

/**
 * The Savings tab pointing at the Commitments tab, rather than reprinting it.
 *
 * This card used to be the whole commitments panel: inventory, coverage and the pay-as-you-go
 * comparison, fetched a second time and drawn a second way. Between it, the Commitments tab and
 * the old Rate optimization tab, one reservation recommendation was rendered three times with
 * three totals — which is what made the estate look like it had three times the opportunity it
 * has. The server now keeps commitments out of this list entirely, so all this has to do is say
 * how much is sitting behind that decision and where to go and make it.
 */
function commitmentPointer(d) {
  const n = d.commitment_count || 0;
  if (!n) return "";
  const cur = d.currency;
  return `
    <section class="card" id="cmtPointer">
      <h3>Commitment options</h3>
      <p class="muted">${n} Azure Advisor recommendation${n === 1 ? "" : "s"} in scope ·
        ${finite(d.commitment_annual) ? `up to <strong>${moneyHtml(d.commitment_annual, cur, 0)}</strong>/year${d.commitment_unpriced_count ? " (known estimates only)" : ""}.`
          : "Provider saving estimate unavailable."}
        ${esc(d.commitment_note || "")}</p>
      <div class="fr-actions">
        <button type="button" id="cmtGo" class="primary">Compare against pay-as-you-go</button>
      </div>
    </section>`;
}

/** One button, wired after every render of the Savings tab. */
function wireCommitmentPointer() {
  const b = $("cmtGo");
  if (b) b.onclick = () => selectTab("commitments");
}

/** Expanding a card is the only interaction, so it is wired from one place after every redraw. */
function wireSavingsCards() {
  for (const head of $("tabBody").querySelectorAll(".sv-head[aria-expanded]")) {
    head.onclick = () => {
      const open = head.getAttribute("aria-expanded") === "true";
      head.setAttribute("aria-expanded", String(!open));
      const body = head.parentElement.querySelector(".sv-body");
      if (body) body.hidden = open;
    };
  }
}

function savingsCard(o, d) {
  const cur = o.currency || d.currency;
  const money2 = (v) => finite(v) ? money(v, cur, 2) : "Not available";

  // Corroboration is the one thing the old six tabs could not show, because it only exists once
  // the rows are put side by side. It is also the most useful signal on the card: a resource
  // three independent checks agree on is a safer place to start than a larger number one
  // heuristic guessed at.
  const badge = o.corroborated
    ? `<span class="sv-badge good" title="${esc(o.sources.map((s) => s.label).join(" and "))}
        both found this">${o.sources.length} scans agree</span>`
    : "";
  const conf = `<span class="sv-conf sv-conf-${esc(o.confidence)}">${esc(o.confidence)}
     confidence</span>`;

  const count = o.count > 1 ? `<span class="sv-count">${o.count} items</span>` : "";

  // Where the figure came from, always. A saving without a provenance is a number to argue with.
  const sources = `<ul class="sv-sources">${o.sources.map((s) => `<li>
      <span class="sv-src-label">${esc(s.label)}</span>
      <span class="sv-src-basis sv-basis-${esc(s.basis)}">${
        s.basis === "billed" ? "from the bill" : "estimate"}</span>
      <span class="sv-src-claim">${esc(s.claim)}</span></li>`).join("")}</ul>`;

  // Two scans that priced the same resource differently. Shown rather than averaged: the gap is
  // usually one scan seeing only part of the resource, and that is worth knowing before acting.
  const disputed = o.disputed
    ? `<p class="sv-disputed">The scans priced this differently —
       ${esc(money2(o.disputed.low))} against ${esc(money2(o.disputed.high))}.
       The larger is shown.</p>`
    : "";

  const options = o.options.length > 1
    ? `<div class="sv-options"><h4>Purchase options</h4>
       <p class="muted">You take one of these, not all of them.</p>
       <ul>${o.options.map((x, i) => `<li class="${i === 0 ? "best" : ""}">
          <span class="sv-opt-label">${esc(x.label)}</span>
          <span class="sv-opt-money">${finite(x.annual) ? esc(money(x.annual, cur, 0)) : "Not available"}<span class="sv-opt-per">
            a year</span></span>
          ${x.detail ? `<span class="sv-opt-note">${esc(x.detail)}</span>` : ""}
        </li>`).join("")}</ul></div>`
    : "";

  const folded = o.folded
    ? `<p class="sv-folded">Azure evaluated this ${o.folded + Math.max(1, o.options.length)} times over
       different lookback windows; those are re-estimates of the same choice, not separate
       opportunities.</p>`
    : "";

  const items = (o.items || []).length
    ? `<table class="sv-items"><thead><tr><th>Resource</th><th>Group</th><th>Region</th>
        <th class="num">Cost</th></tr></thead><tbody>
        ${o.items.map((i) => `<tr><td>${portalLinkMarkup(i, ctx.scopeIds(), i.name || "—")}</td>
          <td>${esc(i.group || "—")}</td><td>${esc(i.region || "—")}</td>
          <td class="num">${esc(money2(i.cost))}</td></tr>`).join("")}
      </tbody></table>
      ${o.more ? `<p class="muted">and ${o.more} more</p>` : ""}`
    : "";

  const where = [o.resource, o.resource_group, o.region].filter(Boolean).join(" · ");
  const annual = finite(o.annual)
    ? `<span class="sv-annual">${esc(money(o.annual, cur, 0))} a year</span>` : "";

  // Only a running, under-loaded VM can be resized or shut down as an alternative — a machine
  // already deallocated (the "orphaned" half of this same scan) is off already, so pricing a
  // resize or a shutdown against it would compare against a state that no longer applies.
  const whatIf = o.category === "idle" ? whatIfMarkup(o.resource_id) : "";

  return `<article class="sv-card sv-${esc(o.category)}" data-state-key="${esc(stableKey(o.id || `${o.category}|${o.resource_id || ""}|${o.title}|${o.resource_group || ""}`))}">
    <button class="sv-head" aria-expanded="false">
      <span class="sv-main">
        <span class="sv-title">${esc(o.title)}</span>
        <span class="sv-meta">
          <span class="sv-cat">${esc(o.category_label)}</span>
          ${count}${badge}${conf}
        </span>
        ${where ? `<span class="sv-where">${esc(where)}</span>` : ""}
      </span>
      <span class="sv-money">
        <span class="sv-window">${esc(money2(o.window))}</span>
        <span class="sv-window-note">${finite(o.window) ? `over ${d.days} days` : "Provider saving estimate unavailable"}</span>
        ${annual}
      </span>
      <span class="sv-caret" aria-hidden="true">▾</span>
    </button>
    <div class="sv-body" hidden>
      <p class="sv-why">${esc(o.why || "")}</p>
      <div class="explore-links">${portalTargets(o, ctx.portalScopeIds()).map(link =>
        `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">Open ${esc(link.label.toLowerCase())} in Azure ↗</a>`).join("")}</div>
      ${disputed}
      <h4>Evidence</h4>
      ${sources}
      ${folded}
      ${options}
      ${o.action ? `<p class="sv-action"><strong>What to do:</strong> ${esc(o.action)}</p>` : ""}
      ${whatIf}
      ${items}
    </div>
  </article>`;
}

async function renderWaste(token) {
  let d;
  try {
    // Live Azure inventory across every subscription in scope — minutes on a large estate.
    d = await get(`/api/dashboard/waste${qs()}`, { timeout: 180000 });
  } catch (err) {
    if (token !== ctx.loadToken) return;
    fail(err?.message || "Could not scan for orphaned resources.");
    return;
  }
  if (token !== ctx.loadToken) return;

  const findings = (d.findings || []).filter((f) => (f.count || 0) > 0);
  const cur = d.currency;
  if (!findings.length) {
    setContent($("tabBody"), `${kpiRow([
      { label: "Idle or orphaned items", value: "0" },
    ])}<p class="muted empty">Nothing idle or orphaned found in this scope. ${
      d.error ? esc(d.error) : ""
    }</p>`);
    return;
  }

  const cards = findings
    .map((f) => {
      const rows = (f.items || []).slice(0, 12);
      const cols = [
        { label: "Resource", cell: (r) => esc(r.name || r.id || "—") },
        { label: "Resource group", cell: (r) => esc(r.resource_group || "—") },
        { label: "Region", cell: (r) => esc(r.location || r.region || "—") },
        {
          label: `Cost, ${d.period_days}d`,
          num: true,
          cell: (r) => (r.cost == null ? "—" : esc(money(r.cost, r.currency || cur, 2))),
        },
      ];
      const heading = `${title(f.category)} — ${f.count} item${f.count === 1 ? "" : "s"}`;
      const note = f.cost
        ? `${money(f.cost, cur, 2)} over the last ${d.period_days} days. ${f.why || ""}`
        : f.why || "No direct cost — clutter rather than spend.";
      return table(heading, rows, cols, note);
    })
    .join("");

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "Idle or orphaned items", value: String(d.total_items ?? 0) },
      {
        label: `Cost, last ${d.period_days} days`,
        value: d.total_cost == null ? "mixed currency" : esc(money(d.total_cost, cur, 2)),
      },
      { label: "Subscriptions scanned", value: String(d.subscriptions ?? 0) },
    ])}
    <p class="muted note">Live inventory joined to what each item actually cost. Nothing here is
      deleted for you, and some idle resources exist deliberately — treat it as a list to review.</p>
    ${cards}`);
}


async function renderRightsizing(token) {
  const d = await live(token, `/api/dashboard/rightsizing${qs()}`);
  if (!d) return;

  const vms = d.vms || [];
  const cur = d.currency;
  const idle = vms.filter((v) => v.cpu_avg != null && v.cpu_avg < (d.cpu_threshold ?? 5));
  const worst = Math.max(...vms.map((v) => v.cost || 0), 0);

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "Virtual machines", value: String(d.count ?? vms.length) },
      { label: "Running", value: String(d.running ?? "—") },
      {
        label: `Idle (under ${d.cpu_threshold ?? 5}% CPU)`,
        value: String(d.idle_count ?? idle.length),
        cls: (d.idle_count ?? idle.length) > 0 ? "up" : "",
      },
      {
        label: `Cost of idle, ${d.period_days}d`,
        value: d.idle_cost == null ? "—" : esc(money(d.idle_cost, cur, 2)),
      },
      { label: "Stopped", value: String(d.stopped_count ?? "—") },
    ])}
    <p class="muted note">CPU average and peak from Azure Monitor over the last
      ${d.period_days} days, joined to what each machine actually cost. A low average with a high
      peak is bursty, not idle — resize those, don't stop them.</p>
    ${table(
      "Virtual machines by cost",
      vms,
      [
        { label: "Name", cell: (r) => `${esc(r.name || "—")} ${bar(r.cost || 0, worst)}` },
        { label: "Size", cell: (r) => esc(r.size || r.vm_size || "—") },
        { label: "Power state", cell: (r) => esc(r.power_state || r.state || "—") },
        {
          label: "CPU avg",
          num: true,
          cell: (r) => (r.cpu_avg == null ? "—" : `${Number(r.cpu_avg).toFixed(1)}%`),
        },
        {
          label: "CPU peak",
          num: true,
          cell: (r) => (r.cpu_max == null ? "—" : `${Number(r.cpu_max).toFixed(1)}%`),
        },
        {
          label: `Cost, ${d.period_days}d`,
          num: true,
          cell: (r) => (r.cost == null ? "—" : esc(money(r.cost, cur, 2))),
        },
        { label: "Scenario", cell: (r) => whatIfCell(r) },
      ],
      d.note || null
    )}`);
  wireWhatIf();
}

/**
 * A resize or shutdown, priced on request rather than for every row nobody asked about.
 *
 * Takes a bare resource id rather than a row shape, because the two places this renders —
 * the Rightsizing table's cell and a Savings opportunity card — disagree on what the VM's id
 * field is called (`id` on a raw utilisation row, `resource_id` on a merged `Opportunity`).
 */
function whatIfMarkup(resourceId) {
  if (!resourceId) return "";
  return `<details class="wf-drill" data-rid="${esc(resourceId)}">
    <summary>What if…</summary>
    <div class="wf-form">
      <input type="text" class="wf-sku" placeholder="target SKU, e.g. Standard_B2s"
             aria-label="Target VM size" />
      <button type="button" class="wf-go">Compare</button>
      <button type="button" class="wf-shutdown ghost">Shut down instead</button>
    </div>
    <div class="wf-result" hidden></div>
  </details>`;
}

function whatIfCell(r) {
  return whatIfMarkup(r.id) || "—";
}

/**
 * One delegated handler on the tab body, not one binding per row.
 *
 * The table this lives in redraws its own body on every sort, page and search keystroke
 * (`wireTables`'s `redraw()`), which would silently orphan a listener attached to any element
 * inside it. `tabBody` itself is never replaced, only its contents, so binding here survives
 * every one of those redraws — and assigning `.onclick` rather than `addEventListener` means
 * revisiting this tab later overwrites the same handler instead of stacking a second one.
 */
function wireWhatIf() {
  $("tabBody").onclick = (e) => {
    const shutdown = e.target.closest(".wf-shutdown");
    const go = e.target.closest(".wf-go");
    if (!shutdown && !go) return;
    const details = e.target.closest(".wf-drill");
    if (!details) return;
    const input = details.querySelector(".wf-sku");
    const sku = shutdown ? "shutdown" : input.value.trim();
    if (!sku) {
      input.focus();
      return;
    }
    runWhatIf(details, sku);
  };
}

const whatIfRequests = new WeakMap();
async function runWhatIf(details, sku) {
  const request = (whatIfRequests.get(details) || 0) + 1;
  whatIfRequests.set(details, request);
  const result = details.querySelector(".wf-result");
  result.hidden = false;
  result.innerHTML = `<p class="muted">Comparing…</p>`;
  try {
    const r = await post(`/api/dashboard/whatif${qs()}`,
      { resource_id: details.dataset.rid, target_sku: sku }, { timeout: 30000 });
    if (whatIfRequests.get(details) !== request) return;
    const cur = r.currency || "USD";
    const saving = r.annual_saving;
    const savingCls = saving > 0 ? "good" : saving < 0 ? "bad" : "";
    const savingText =
      saving == null ? "" :
      saving > 0 ? `Saves about ${esc(money(saving, cur, 0))} a year` :
      saving < 0 ? `Costs about ${esc(money(-saving, cur, 0))} a year more` :
      "No change";
    result.innerHTML = `
      <table class="wf-table">
        <tr><td>Billed, last ${esc(String(r.days))}d, annualised</td>
            <td>${esc(money(r.billed.annual, cur, 0))}</td></tr>
        <tr><td>${esc(r.current_size || "current size")}, at today's retail rate</td>
            <td>${r.current_retail.annual == null ? "not priced"
                  : esc(money(r.current_retail.annual, cur, 0))}</td></tr>
        <tr><td>${esc(r.target_size)}</td>
            <td>${esc(money(r.target.annual, cur, 0))}</td></tr>
      </table>
      ${savingText ? `<p class="wf-saving ${savingCls}">${savingText}</p>` : ""}
      <p class="muted note">${esc(r.note || "")}</p>`;
  } catch (err) {
    if (whatIfRequests.get(details) !== request) return;
    result.innerHTML = `<p class="bad">${esc(err?.message || "Could not compare right now.")}</p>`;
  }
}

async function renderAdvisor(token) {
  const d = await live(token, `/api/dashboard/advisor${qs()}`, 90000);
  if (!d) return;

  const recs = d.recommendations || [];
  setContent($("tabBody"), `
    ${kpiRow([
      { label: `${esc(d.category)} recommendations`, value: String(d.count ?? recs.length) },
      {
        label: "Estimated annual saving",
        value:
          d.estimated_annual_savings == null
            ? "not estimated"
            : esc(money(d.estimated_annual_savings, d.currency, 2)),
        cls: d.estimated_annual_savings ? "down" : "",
      },
    ])}
    <p class="muted note">Recommendations and savings estimates from Azure Advisor. ${esc(d.note || "")}</p>
    ${table("Recommendations", recs, [
      { label: "Impact", cell: (r) => esc(r.impact || "—") },
      { label: "Recommendation", cell: (r) => esc(r.problem || r.solution || "—") },
      { label: "Resource", cell: (r) => esc(r.resource || "—") },
      {
        label: "Annual saving",
        num: true,
        cell: (r) => (r.annual_savings == null ? "—" : esc(money(r.annual_savings, r.currency, 2))),
      },
    ])}`);
}


async function renderShutdown(token) {
  const d = await get(`/api/dashboard/shutdown${qs()}`, { timeout: 120000 });
  if (token !== ctx.loadToken) return;

  const cur = d.currency;
  if (!d.candidates.length) {
    setContent($("tabBody"), `
      ${kpiRow([
        { label: "Examined", value: String(d.examined || 0), sub: "running VMs" },
        { label: "Schedulable", value: "0", sub: "none with a clear rhythm" },
        { label: "Busy or idle throughout", value: String(d.skipped_steady || 0),
          sub: "no daily pattern" },
        { label: "Tagged production", value: String(d.skipped_production || 0),
          sub: "never suggested" },
      ])}
      <section class="card">
        <h3>${d.partial ? "Scheduling assessment incomplete" : "Nothing safe to schedule"}</h3>
        ${d.partial ? '<p class="banner warn">Some machines could not be assessed because currency or pricing evidence was unavailable.</p>' : ""}
        <p class="muted">${esc(d.note || "")}</p>
        <p class="muted note">A machine that is idle around the clock is waste rather than a
          scheduling opportunity — look at
          <button type="button" class="linkish" data-goto="waste">Orphaned resources</button>
          instead.</p>
      </section>`);
    wireBudgetLinks();
    return;
  }

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "Could be scheduled", value: String(d.count), sub: `of ${d.examined} running VMs` },
      { label: "Costing now", value: esc(currencyAmountList(d.totals_by_currency, "total_current", money)
        || nativeAmount(d.total_current, cur, money)), sub: "per month, 24×7" },
      { label: "Up to", value: esc(currencyAmountList(d.totals_by_currency, "total_saving", money)
        || nativeAmount(d.total_saving, cur, money)), cls: "down",
        sub: "per month if scheduled" },
      { label: "Window", value: `${d.hours_kept}h`, sub: `of ${168}h a week` },
    ])}

    <section class="card">
      <div class="tagpick-head">
        <h3>Scheduling candidates</h3>
        <span class="muted note">${esc(d.window)} · ${d.days_analysed} days of hourly CPU</span>
      </div>
      <p class="muted note"><strong>“Up to” is meant literally.</strong> The figure is the
        compute share of the bill for the hours saved. Disks and reserved IPs keep charging while
        a machine is deallocated, and a reservation or savings plan already discounts the hours —
        so the realised saving is lower. ${d.skipped_production
          ? `${d.skipped_production} VM${d.skipped_production === 1 ? " is" : "s are"} tagged
             production and excluded outright.` : ""}</p>
      <div class="anoms">${d.candidates.map(shutdownCard).join("")}</div>
    </section>`);
}

function shutdownCard(c) {
  const cur = c.currency;
  return `<article class="anom">
    ${cardHead({
      name: c.name,
      sub: `${c.size || ""} · ${c.resource_group || ""} · ${c.location || ""}`,
      figure: `−${esc(nativeAmount(c.saving, cur, money))}`,
      figureClass: "down",
      status: `per month · ${cur || "currency unknown"}`,
      statusClass: "ok",
    })}
    <div class="budget-meta">
      <span><strong>${c.avg_business}%</strong> CPU in hours ·
        <strong>${c.avg_offhours}%</strong> outside</span>
      <span>${esc(nativeAmount(c.current, cur, money))} → ${esc(nativeAmount(c.scheduled, cur, money))}</span>
      <span>${c.hours_saved}h a week off</span>
    </div>
    <p class="muted note">Busy ${c.ratio}× more during ${esc(c.window)}.
      ${c.busy_offhours_hours
        ? `Active in ${c.busy_offhours_hours} off-hours sample(s) — check nothing runs overnight.`
        : "Never active outside those hours in the period sampled."}</p>
  </article>`;
}

// ================================================================ anomalies

/**
 * The days that did not look like the others.
 *
 * Framed as events rather than as a table of numbers, because the question is never "what was
 * the modified z-score" — it is "what changed, when, and how much did it cost me". The
 * statistic earns its place by choosing the rows; it does not need to be on screen.
 */

async function renderCommitments(token) {
  const d = await cached(key("commitments"), () =>
    get(`/api/dashboard/commitments${qs()}`, { timeout: 60000 }));
  if (token !== ctx.loadToken) return;

  const c = d.coverage || {};
  const s = d.spot || {};
  const inv = d.inventory || {};
  const cmp = d.comparison || {};
  const cur = c.currency || inv.currency || cmp.currency;

  if (c.empty) {
    setContent($("tabBody"), `<p class="muted empty">No cost data loaded for this period yet.</p>`);
    return;
  }

  const split = c.split || {};
  const compute = c.compute_split || {};

  const mix = (obj, total, currency = cur) =>
    Object.entries(obj)
      .sort((a, b) => b[1] - a[1])
      .map(
        ([k, v]) => `<tr>
          <td>${esc(k)}</td>
          <td class="num">${total ? ((v / total) * 100).toFixed(1) : "—"}%</td>
          <td class="num">${esc(nativeAmount(v, currency, money))}</td>
        </tr>`
      )
      .join("");

  const coverageGroups = c.mixed_currency ? Object.entries(c.by_currency || {}) : [];
  const coveragePanels = coverageGroups.map(([currency, group], index) => `<section class="card">
    <h3>Coverage · ${esc(currency)}</h3>
    ${kpiRow([
      { label: "Spend", value: esc(nativeAmount(group.total, currency, money)) },
      { label: "Compute coverage", value: group.compute_coverage_pct == null ? "—" : `${group.compute_coverage_pct}%`,
        sub: `${esc(group.compute_coverage_status || "unknown")} attribution` },
      { label: "Overall coverage", value: group.coverage_pct == null ? "—" : `${group.coverage_pct}%`,
        sub: `${esc(group.coverage_status || "unknown")} attribution` },
    ])}
    <h4>Pricing mix</h4><div class="table-wrap"><table>
      <thead><tr><th>Pricing model</th><th>Share</th><th>Cost</th></tr></thead>
      <tbody>${mix(group.split || {}, group.total, currency)}</tbody></table></div>
    <h4>Compute pricing</h4><div class="table-wrap"><table>
      <thead><tr><th>Pricing model</th><th>Share</th><th>Cost</th></tr></thead>
      <tbody>${mix(group.compute_split || {}, group.compute_total, currency)}</tbody></table></div>
    <div id="coverageTrend${index}" class="chart-card"></div>
  </section>`).join("");

  // The bare count on its own answers "how many" but not "which ones", and the second question
  // is the one that actually matters once a commitment turns out to be idle or oddly shared. A
  // <details> keeps that answer one click away on the same row rather than a second view for a
  // handful of names that only mean anything next to the benefit they belong to.
  const resourcesCell = (r) => {
    if (!r.resources) return "0";
    const top = r.resources_top || [];
    if (!top.length) return String(r.resources);
    const rows = top.map((x) => `<li><span>${esc(x.name)} <span class="grp">${esc(x.resource_group)}</span></span>
        <span>${esc(nativeAmount(x.cost, r.currency || cur, money))}</span></li>`).join("");
    const more = r.resources_more
      ? `<p class="cmt-drill-more">and ${r.resources_more} more</p>` : "";
    return `<details class="cmt-drill"><summary>${r.resources}</summary>
        <ul class="cmt-drill-list">${rows}</ul>${more}</details>`;
  };

  setContent($("tabBody"), `
    ${comparisonPanel(cmp, cur)}

    ${kpiRow([
      {
        label: "Compute coverage",
        value: c.compute_coverage_pct == null ? "—" : `${c.compute_coverage_pct}%`,
        cls: (c.compute_coverage_pct || 0) > 0 ? "down" : "",
        sub: c.compute_coverage_pct == null ? `${esc(c.compute_coverage_status || "unknown")} attribution`
          : `of ${esc(nativeAmount(c.compute_total, cur, money))} compute spend`,
      },
      {
        label: "Overall coverage",
        value: c.coverage_pct == null ? "—" : `${c.coverage_pct}%`,
        sub: c.coverage_pct == null ? `${esc(c.coverage_status || "unknown")} attribution`
          : `of ${esc(nativeAmount(c.total, cur, money))} total`,
      },
      {
        label: "Spot",
        value: c.spot_pct == null ? "—" : `${c.spot_pct}%`,
        sub: s.error ? "Spot savings unavailable" : s.empty ? "no Spot usage" : `saved ${esc(currencyAmountList(s.totals_by_currency, "total_saved", money)
          || nativeAmount(s.total_saved, s.currency || cur, money))}`,
      },
      {
        label: "Active commitments",
        value: inv.error ? "—" : String(inv.count ?? (inv.commitments || []).length),
        sub: inv.error ? "inventory unavailable" : inv.truncated ? `${inv.returned_count ?? (inv.commitments || []).length} records shown`
          : (inv.count ?? (inv.commitments || []).length)
          ? "reservations and plans" : "none returned by inventory",
      },
    ])}
    ${s.error ? `<p class="muted warn" data-commitment-gap>${esc(s.error)}</p>` : ""}

    <div id="mixChart" class="card chart-card"></div>

    ${c.mixed_currency ? coveragePanels : `<div class="grid-2">
      <section class="card">
        <h3>Pricing mix</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Pricing model</th><th class="num">Share</th><th class="num">Cost</th></tr></thead>
            <tbody>${mix(split, c.total)}</tbody>
          </table>
        </div>`}
      </section>
      <section class="card">
        <h3>Compute pricing</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Pricing model</th><th class="num">Share</th><th class="num">Cost</th></tr></thead>
            <tbody>${mix(compute, c.compute_total)}</tbody>
          </table>
        </div>
        <details class="view-help" data-state-key="commitments-help">
          <summary>About this view</summary>
          <p>Compute coverage separates commitment-covered compute from the rest of the bill.
            Storage-heavy estates can have low overall coverage even when compute is covered.</p>
          <p>The comparison shows annual costs against the pay-as-you-go baseline, not additive
            savings. Models unavailable from Advisor remain listed with their reason.</p>
        </details>
      </section>
    </div>

    ${
      (inv.commitments || []).length
        ? table(
            "Existing commitments",
            inv.commitments,
            [
              { label: "Benefit", cell: (r) =>
                  `${esc(r.name || "—")}${r.idle
                    ? ' <span class="pill bad">nothing using it</span>' : ""}` },
              { label: "Kind", cell: (r) => esc(r.kind || "—") },
              { label: "Currency", cell: (r) => esc(r.currency || cur || "Unknown") },
              { label: "Covers", cell: (r) =>
                  esc((r.services || []).join(", ") || "—") },
              {
                label: "Resources",
                num: true,
                value: (r) => r.resources,
                cell: resourcesCell,
              },
              {
                label: `Cost, ${inv.days}d`,
                num: true,
                value: (r) => r.cost,
                cell: (r) => esc(nativeAmount(r.cost, r.currency || cur, money)),
                total: (rows) => esc(nativeRowTotal(rows, "cost", cur, money)),
              },
            ],
            `${inv.basis || ""} ${inv.term_note || ""}${inv.truncated
              ? ` Showing ${inv.returned_count ?? inv.commitments.length} of ${inv.count} commitment records; table totals cover shown records.` : ""}`,
            { key: `What you already hold|Benefit|Kind|Currency|Covers|Resources|Cost, ${inv.days}d` }
          )
        : inv.error
        ? `<section class="card"><h3>Existing commitments</h3>
           <p class="muted empty">${esc(inv.error)}</p></section>`
        : `<section class="card"><h3>Existing commitments</h3>
           <p class="muted empty">No commitment records were returned. Missing billing attribution is not
             proof of no reservations or savings plans. Refresh FOCUS report to load effective-cost
             and commitment evidence.</p></section>`
    }

    ${
      (inv.idle || []).length
        ? `<div class="banner warn"><strong>${(inv.idle || []).length} commitment${
             (inv.idle || []).length === 1 ? " is" : "s are"} being charged for with no
             resources attached:</strong> ${esc((inv.idle || []).join(", "))}. That is money
             leaving every month for no workload.</div>`
        : ""
    }

    ${
      !s.empty && !s.error
        ? table(
            "Monthly Spot savings",
            Object.keys(s.trend_by_currency || {}).length
              ? Object.entries(s.trend_by_currency).flatMap(([currency, rows]) => rows.map(row => ({ ...row, currency })))
              : s.trend || [],
            [
              { label: "Month", cell: (r) => esc(r.month) },
              { label: "Currency", cell: (r) => esc(r.currency || s.currency || cur || "Unknown") },
              {
                label: "Paid",
                num: true,
                value: (r) => r.spot_cost,
                cell: (r) => esc(nativeAmount(r.spot_cost, r.currency || s.currency || cur, money)),
              },
              {
                label: "On-demand equivalent",
                num: true,
                value: (r) => r.would_have_cost,
                cell: (r) => esc(nativeAmount(r.would_have_cost, r.currency || s.currency || cur, money)),
              },
              {
                label: "Saved",
                num: true,
                value: (r) => r.saved,
                cell: (r) =>
                  r.saved == null ? `<span class="muted">Not available${Number.isFinite(r.priced_saving)
                    ? ` · assessed portion ${esc(nativeAmount(r.priced_saving, r.currency || s.currency || cur, money))}` : ""}</span>`
                    : esc(nativeAmount(r.saved, r.currency || s.currency || cur, money)),
                total: (rows) =>
                  esc(nativeRowTotal(rows, "saved", s.currency || cur, money)),
              },
              {
                label: "Discount",
                num: true,
                value: (r) => r.saved_pct,
                cell: (r) => (r.saved_pct == null ? "—" : `${r.saved_pct}%`),
              },
            ],
            s.note,
            { key: "Spot savings, month by month|Month|Currency|Paid|On-demand equivalent|Saved|Discount" }
          )
        : ""
    }

    <div id="trendChart" class="card chart-card"></div>`);

  drawComparisonChart(cmp, cur);

  const drawCoverageTrend = (host, trend, currency) => {
    if (!host) return;
    if (trend.length < 2 || !/^[A-Z]{3}$/.test(currency || "")) { host.remove(); return; }
    const datasets = [
      { label: "On demand", data: trend.map(t => t.on_demand) },
      { label: "Committed", data: trend.map(t => t.committed) },
      { label: "Spot", data: trend.map(t => t.spot) },
    ];
    const unclassified = trend.map(t => Number.isFinite(t.total)
      ? t.total - (t.on_demand || 0) - (t.committed || 0) - (t.spot || 0) : null);
    if (unclassified.some(value => Number.isFinite(value) && Math.abs(value) > 0.01)) {
      datasets.push({ label: "Unclassified", data: unclassified });
    }
    drawChart(host, {
      type: "stacked_bar",
      title: `Monthly pricing mix · ${currency}`,
      labels: trend.map((t) => t.month),
      datasets,
      currency,
    });
  };
  drawCoverageTrend($("trendChart"), c.trend || [], cur);
  coverageGroups.forEach(([currency], index) =>
    drawCoverageTrend($(`coverageTrend${index}`), c.trend_by_currency?.[currency] || [], currency));

  // A doughnut of one meaningful slice is a circle. Only draw the mix when there is genuinely
  // a mix — below 1% the second slice is invisible anyway and the table says it better.
  const entries = Object.entries(compute).filter(([, v]) => v > 0);
  const smallest = entries.length ? Math.min(...entries.map(([, v]) => v)) : 0;
  if (entries.length > 1 && c.compute_total && smallest / c.compute_total >= 0.01) {
    drawChart($("mixChart"), {
      type: "doughnut",
      title: `Compute pricing mix — ${c.days} days`,
      labels: entries.map(([k]) => k),
      datasets: [{ label: "Cost", data: entries.map(([, v]) => v) }],
      // Pricing models are three different things, not one thing ranked, so they take the
      // categorical hues — and they match the stacked bar directly below, where the same three
      // names appear. Two charts of the same data in different colours is its own bug.
      categorical: true,
      currency: cur,
    });
  } else {
    $("mixChart")?.remove();
  }
}

/**
 * Pay-as-you-go, savings plan and reserved instance, side by side.
 *
 * The old version of this listed what each option *saved* and nothing else. A saving on its own
 * cannot be compared with anything: "$558 a year" says nothing about whether that is a rounding
 * error or a third of the bill, and it certainly does not say what the estate would then cost.
 * Three columns, one per pricing model, each stating the annual bill under it — because the
 * question is which of three prices to pay, not how much three discounts are worth.
 *
 * A model Azure will not recommend gets a column too, saying so. Omitting it makes the page look
 * like there were only ever two choices; showing it at zero makes a product Advisor declined to
 * offer look like one worth nothing.
 */
function comparisonPanel(cmp, cur, suffix = "") {
  if (Object.keys(cmp.by_currency || {}).length) {
    return `<p class="muted">Pricing comparisons are separate by currency; there is no cross-currency best offer.</p>${
      Object.entries(cmp.by_currency).map(([currency, group], index) =>
        comparisonPanel(group, currency, `-${index}`)).join("")}`;
  }
  const models = cmp.models || [];
  const currency = cmp.currency || cur;
  if (!models.length) {
    return `<section class="card"><h3>Pricing comparison</h3>
      <p class="muted empty">${esc(cmp.error
        || "Advisor did not return a commitment recommendation for this scope.")}</p></section>`;
  }

  const baseline = models.find((m) => m.baseline) || {};
  const card = (m) => {
    if (m.unavailable) {
      return `<article class="cmp-col off">
        <h4>${esc(m.label)}</h4>
        <div class="cmp-figure muted">not offered</div>
        <p class="muted note">${esc(m.reason || "")}</p>
      </article>`;
    }
    const best = !m.baseline && Number.isFinite(m.annual_saving) && m.annual_saving === (cmp.best || {}).annual_saving;
    return `<article class="cmp-col${m.baseline ? " base" : ""}${best ? " best" : ""}">
      <h4>${esc(m.label)}${best ? ' <span class="pill good">best offer</span>' : ""}</h4>
      <div class="cmp-figure">${m.annual_cost == null
        ? "—" : esc(nativeAmount(m.annual_cost, currency, money))}<span class="cmp-unit">a year</span></div>
      <div class="cmp-save">${m.baseline
        ? '<span class="muted">comparison baseline</span>'
        : m.annual_saving == null ? '<span class="muted">Saving unavailable</span>'
        : `<span class="delta down">saves ${esc(nativeAmount(m.annual_saving, currency, money))}</span>${
            m.saving_pct != null ? ` <span class="muted">· ${m.saving_pct}% off</span>` : ""}`}
      </div>
      <dl class="cmp-facts">
        <dt>Commitment</dt><dd>${esc(m.commits_to || "—")}</dd>
        <dt>Term</dt><dd>${esc(m.term || "—")}</dd>
        ${m.lookback_spread
          ? `<dt>Confidence</dt><dd><span class="pill warn">${m.lookback_spread}× spread</span>
             between Advisor's lookback windows</dd>`
          : ""}
      </dl>
      <p class="muted note">${esc(m.flexibility || "")}</p>
      ${(m.terms || []).length > 1
        ? `<p class="muted note">${m.terms.map((t) =>
            `${esc(t.term)}: ${esc(nativeAmount(t.annual_saving, currency, money))}`).join(" · ")}</p>`
        : ""}
    </article>`;
  };

  return `
    <section class="card" id="cmpPanel${suffix}">
      <div class="tagpick-head">
        <h3>Pricing comparison · ${esc(currency || "Currency unknown")}</h3>
        <span class="muted note">${esc(cmp.source || "Azure Advisor")}${
          baseline.measured_over_days
            ? ` · ${baseline.measured_over_days} days of on-demand spend` : ""}</span>
      </div>

      ${cmp.verdict ? `<div class="banner">${esc(cmp.verdict)}</div>` : ""}

      <div class="cmp-grid">${models.map(card).join("")}</div>
      ${cmp.baseline && cmp.baseline.annual == null && cmp.baseline.unavailable_reason
        ? `<p class="muted note" id="cmpBaselineReason${suffix}">Annual costs are not shown: ${
            esc(cmp.baseline.unavailable_reason)}</p>`
        : ""}

      <div id="cmpChart${suffix}" class="chart-card"></div>

      <p class="muted note">${esc(cmp.note || "")}</p>
    </section>`;
}

/**
 * The same three numbers as bars, because a price comparison is a length comparison.
 *
 * Only drawn when there is a baseline to draw against: three annual costs mean something as
 * bars, three savings do not — the tallest bar would be the cheapest option, which is exactly
 * backwards from how a bar chart reads.
 */
function drawComparisonChart(cmp, cur, suffix = "") {
  if (Object.keys(cmp.by_currency || {}).length) {
    Object.entries(cmp.by_currency).forEach(([currency, group], index) =>
      drawComparisonChart(group, currency, `-${index}`));
    return;
  }
  const host = $(`cmpChart${suffix}`);
  if (!host) return;
  const models = (cmp.models || []).filter((m) => !m.unavailable && m.annual_cost != null);
  if (models.length < 2 || !/^[A-Z]{3}$/.test(cmp.currency || cur || "")) {
    host.remove();
    return;
  }
  drawChart(host, {
    type: "hbar",
    title: "Annual cost by pricing model",
    labels: models.map((m) => `${m.label}${m.term && !m.baseline ? ` (${m.term})` : ""}`),
    datasets: [{ label: "Annual cost", data: models.map((m) => m.annual_cost) }],
    currency: cmp.currency || cur,
  });
}


async function renderEsu(token) {
  const d = await live(token, `/api/dashboard/esu${qs()}`, 120000);
  if (!d) return;

  const machines = d.machines || [];
  const cur = d.currency;
  const status = (m) =>
    m.status === "out of support"
      ? '<span class="pill bad">out of support</span>'
      : '<span class="pill warn">ending soon</span>';

  // A second, independent card on the same tab: Hybrid Benefit reads a different Resource Graph
  // query and prices a different thing than end-of-support does, so it is fetched and filled in
  // separately rather than folded into the one report above -- a slow retail lookup here must
  // never hold up a page that is already complete without it.
  const ahbPlaceholder = `
    <section class="card" id="ahbCard" data-preserve-live>
      <h3>Windows Server Hybrid Benefit</h3>
      <p class="muted note" id="ahbBody">Checking the Windows Server estate…</p>
    </section>`;

  if (!machines.length) {
    setContent($("tabBody"), `
      ${kpiRow([
        { label: "Out of support", value: "0" },
        { label: "Support ending soon", value: "0" },
        { label: "ESU billed to date", value: esc(money(d.billed_esu || 0, cur, 2)) },
      ])}
      <p class="muted empty">Nothing in scope is out of support or close to it. Checked
        ${d.scanned?.vms ?? 0} virtual machine(s), ${d.scanned?.arc ?? 0} Arc-connected server(s)
        and ${d.scanned?.sql ?? 0} SQL Server VM(s) across ${d.subscriptions ?? 0} subscription(s).
        ${d.error ? esc(d.error) : ""}</p>
      <p class="muted note">${esc(d.note || "")}</p>
      ${ahbPlaceholder}`);
    loadHybridBenefit(token);
    return;
  }

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "Out of support", value: String(d.out_of_support), cls: d.out_of_support ? "up" : "" },
      { label: "Ending soon", value: String(d.ending_soon) },
      {
        label: "Uncovered",
        value: String(d.exposed),
        cls: d.exposed ? "up" : "down",
        sub: d.exposed ? "would need an ESU licence" : "all covered",
      },
      {
        label: "Estimated ESU / month",
        value: d.priced ? esc(money(d.estimated_monthly_cost, cur, 2)) : "no price list",
        sub: "for uncovered machines",
      },
      { label: "ESU billed to date", value: esc(money(d.billed_esu || 0, cur, 2)) },
    ])}
    <p class="muted note">${esc(d.note || "")}</p>
    ${table(
      "Support lifecycle",
      machines,
      [
        { label: "Name", cell: (r) => esc(r.name || "—") },
        { label: "Product", cell: (r) => `${esc(r.product)} ${status(r)}` },
        { label: "Where", cell: (r) => esc(r.kind) },
        { label: "Support ended", cell: (r) => esc(r.support_ends) },
        { label: "ESU position", cell: (r) => esc(r.coverage) },
        {
          label: "Est. / month",
          num: true,
          value: (r) => r.monthly_esu_cost,
          cell: (r) =>
            r.monthly_esu_cost == null
              ? "—"
              : r.monthly_esu_cost === 0
                ? "included"
                : esc(money(r.monthly_esu_cost, cur, 2)),
        },
      ],
      `Checked ${d.scanned?.vms ?? 0} VM(s), ${d.scanned?.arc ?? 0} Arc server(s), ` +
        `${d.scanned?.sql ?? 0} SQL VM(s).`,
      { key: "Machines past or near end of support|Name|Product|Where|Support ended|ESU position|Est. / month" }
    )}
    ${ahbPlaceholder}`);
  loadHybridBenefit(token);
}

/**
 * Fire-and-forget, same shape as `loadRecommendation`: the report above already stands on its
 * own, so a slow or failed Hybrid Benefit lookup must never delay or blank out what it already
 * showed.
 */
let hybridBenefitPending = false;
let hybridBenefitRequest = 0;
async function loadHybridBenefit(token) {
  const card = $("ahbCard");
  if (!card) return;
  const request = ++hybridBenefitRequest;
  hybridBenefitPending = true;
  const current = () => request === hybridBenefitRequest && token === ctx.loadToken && $("ahbCard") === card;
  let d;
  try {
    d = await cached(key("hybrid-benefit"), () => get(`/api/dashboard/hybrid-benefit${qs()}`, { timeout: 60000 }));
  } catch (err) {
    if (!current()) return;
    hybridBenefitPending = false;
    const body = $("ahbBody");
    if (body) body.textContent = err?.message || "Could not check Hybrid Benefit right now.";
    return;
  }
  if (!current()) return;
  hybridBenefitPending = false;

  const vms = d.vms || [];
  const cur = d.currency;
  if (d.error || !vms.length) {
    const body = $("ahbBody");
    if (body) {
      body.textContent = d.error
        || d.note
        || "No Windows Server virtual machines found in this scope.";
    }
    return;
  }

  setContent(card, `
    <h3>Windows Server Hybrid Benefit</h3>
    ${kpiRow([
      { label: "Windows Server VMs", value: String(d.count) },
      {
        label: "Covered",
        value: String(d.covered),
        cls: d.covered ? "down" : "",
      },
      {
        label: "Uncovered",
        value: String(d.uncovered),
        cls: d.uncovered ? "up" : "down",
      },
      {
        label: "Estimated saving / year",
        value: esc(money(d.estimated_annual_savings || 0, cur, 0)),
        sub: "if every uncovered VM switched",
      },
    ])}
    <p class="muted note">${esc(d.note || "")}</p>
    ${table(
      "Windows Server VMs",
      vms,
      [
        { label: "Name", cell: (r) => esc(r.name || "—") },
        { label: "Resource group", cell: (r) => esc(r.resource_group || "—") },
        { label: "Size", cell: (r) => esc(r.size || "—") },
        {
          label: "Hybrid Benefit",
          cell: (r) => (r.hybrid_benefit
            ? '<span class="pill good">already on</span>'
            : '<span class="pill warn">not on</span>'),
        },
        {
          label: `Billed, ${d.days || 30}d`,
          num: true,
          value: (r) => r.cost,
          cell: (r) => (r.cost == null ? "—" : esc(money(r.cost, cur, 2))),
        },
        {
          label: "Est. saving / year",
          num: true,
          value: (r) => r.estimated_annual_savings,
          cell: (r) => (r.estimated_annual_savings == null
            ? "—" : esc(money(r.estimated_annual_savings, cur, 0))),
        },
      ],
      null,
      { key: `Windows Server virtual machines|Name|Resource group|Size|Hybrid Benefit|Billed, ${d.days || 30}d|Est. saving / year` }
    )}`);
  // The generic post-render wireTables(body) in the tab-switch dispatcher already ran by the
  // time this table exists -- it fetches after the main report, on purpose, so it has to wire
  // its own table rather than rely on that pass catching a card that did not exist yet.
  wireTables(card);
}




  return {
    renderSavings, renderWaste, renderRightsizing, renderAdvisor, renderShutdown,
    renderCommitments, renderEsu,
    stop: () => {
      scan.stop();
      statusClock.stop();
    },
    resume: (token, tab) => {
      if (tab === "esu" && hybridBenefitPending) loadHybridBenefit(token);
      if (tab === "savings") {
        paintSavingsStatus(token);
        statusClock.start(token);
        if (!savingsData || savingsPresentation(savingsData, savingsError).running) scan.start(token);
      }
    },
  };
}
