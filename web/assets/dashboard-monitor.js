/** Lazy monitor views. Dependencies are supplied by the shell; no reverse imports. */
import { budgetReadIssue } from "./budget-context.js";
import { currencyAmountList, nativeAmount } from "./dashboard-currencies.js";
import { loadBudgetTrend as loadBudgetTrendChart } from "./budget-chart.js";
export { budgetReadIssue } from "./budget-context.js";

export function budgetStatus(b) {
  return typeof b.current_spend === "number" && Number.isFinite(b.current_spend)
    && typeof b.percent_used === "number" && Number.isFinite(b.percent_used)
    && ["over", "near", "ok"].includes(b.status) ? b.status : "unknown";
}

export function filterBudgets(rows, { status = "all", q = "" } = {}) {
  const query = q.trim().toLowerCase();
  return rows.filter(b => (status === "all" || (status === "attention"
    ? ["over", "near"].includes(budgetStatus(b)) : budgetStatus(b) === status))
    && (!query || [b.name, b.subscription, b.subscription_id, b.filter]
      .some(value => String(value || "").toLowerCase().includes(query))));
}

export function createTabs(ctx) {
  const { $, bar, budgetCard, budgetPortalUrl, cardHead, days, drawBudgetChart, drawChart, esc, get, kpiRow, live, money, moneyHtml, pct, post, qs, readStore, scopeIds, selectTab, setContent, showTagsFor, subName, table, title, wireBudgetLinks } = ctx;

/**
 * Governance: configuration that is wrong, rather than spend that is wasted.
 *
 * Two independent checks share the tab because they answer the same kind of question. Either
 * can fail without losing the other, so each half renders its own error rather than the tab
 * showing nothing.
 */
async function renderGovernance(token) {
  const d = await live(token, `/api/dashboard/governance${qs()}`, 180000);
  if (!d) return;

  const t = d.tagging || {};
  const a = d.accelerated_networking || {};
  const rg = d.orphaned_resource_groups || {};
  const cur = t.currency;
  const maxUntagged = (t.by_type || []).reduce((m, x) => Math.max(m, x.untagged), 0);

  const section = (title, body) => `<section class="card"><h3>${esc(title)}</h3>${body}</section>`;
  const problem = (msg) =>
    `<p class="muted empty">${esc(msg)}</p>`;

  const tagBody = t.error
    ? section("Tag coverage", problem(t.error))
    : `
    ${kpiRow([
      {
        label: "Tag coverage",
        value: t.coverage_pct == null ? "—" : `${t.coverage_pct}%`,
        cls: t.coverage_pct != null && t.coverage_pct < 60 ? "up" : "",
        sub: `${t.scanned?.tagged ?? 0} of ${t.scanned?.resources ?? 0} taggable`,
      },
      { label: "Untagged resources", value: String(t.untagged_total ?? 0) },
      {
        label: "Untagged spend",
        value: moneyHtml(t.untagged_cost, cur, 2),
        sub: `over the last ${t.days ?? 30} days`,
      },
      {
        label: "Without accelerated networking",
        value: String((a.eligible || []).length),
        sub: a.error ? "check failed" : `${a.enabled ?? 0} already on`,
      },
    ])}

    <div id="tagChart" class="card chart-card"></div>

    <div class="grid-2">
      ${table(
        "Untagged by resource type",
        t.by_type || [],
        [
          { label: "Type", cell: (r) => `${esc(r.type)} ${bar(r.untagged, maxUntagged)}` },
          {
            label: "Coverage",
            num: true,
            value: (r) => r.coverage_pct ?? 0,
            cell: (r) => (r.coverage_pct == null ? "—" : `${r.coverage_pct}%`),
          },
          {
            label: "Untagged",
            num: true,
            value: (r) => r.untagged,
            cell: (r) => String(r.untagged),
            total: (rows) => String(rows.reduce((n, r) => n + r.untagged, 0)),
          },
        ],
        null
      )}
      ${table(
        "Tag keys",
        t.tag_keys || [],
        [
          { label: "Key", cell: (r) => esc(r.key) },
          {
            label: "Resources",
            num: true,
            value: (r) => r.count,
            cell: (r) => String(r.count),
          },
        ],
        null
      )}
    </div>

    ${table(
      "Untagged billed resources",
      t.resources || [],
      [
        { label: "Resource", cell: (r) => esc(r.name) },
        { label: "Type", cell: (r) => esc(String(r.type).split("/").pop()) },
        { label: "Resource group", cell: (r) => esc(r.resourceGroup || "—") },
        { label: "Region", cell: (r) => esc(r.location || "—") },
        {
          label: `Cost, ${t.days ?? 30}d`,
          num: true,
          value: (r) => r.cost || 0,
          cell: (r) => moneyHtml(r.cost, r.currency || cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + (r.cost || 0), 0), cur, 2),
        },
      ],
      null
    )}
    ${t.note ? `<p class="muted note">${esc(t.note)}</p>` : ""}`;

  const accelBody = a.error
    ? section("Accelerated networking", problem(a.error))
    : (a.eligible || []).length
      ? `${table(
          "VMs without accelerated networking",
          a.eligible,
          [
            { label: "VM", cell: (r) => esc(r.vmName || "—") },
            { label: "Size", cell: (r) => esc(r.vmSize || "—") },
            {
              label: "Power state",
              cell: (r) =>
                /deallocated|stopped/i.test(r.powerState || "")
                  ? `<span class="pill">${esc(r.powerState)}</span>`
                  : esc(r.powerState || "—"),
            },
            { label: "Resource group", cell: (r) => esc(r.resourceGroup || "—") },
            { label: "Region", cell: (r) => esc(r.location || "—") },
          ],
          a.note
        )}`
      : section(
          "Accelerated networking",
          problem(
            a.scanned?.nics
              ? `All ${a.scanned.nics} attached network interface(s) either have accelerated networking on, or sit on a size that cannot support it.`
              : "No virtual machines with attached network interfaces in scope."
          )
        );

  const rgBody = rg.error
    ? section("Empty resource groups", problem(rg.error))
    : (rg.groups || []).length
      ? `${table(
          "Empty resource groups",
          rg.groups,
          [
            { label: "Resource group", cell: (r) => esc(r.name || "—") },
            { label: "Subscription", cell: (r) => esc(subName(r.subscriptionId)) },
            { label: "Region", cell: (r) => esc(r.location || "—") },
          ],
          rg.note
        )}`
      : section(
          "Empty resource groups",
          problem(
            rg.scanned?.resource_groups != null
              ? `None of the ${rg.scanned.resource_groups} resource group(s) in scope are empty.`
              : "No resource groups in scope."
          )
        );

  setContent($("tabBody"), tagBody + accelBody + rgBody + `
    <details class="view-help" data-state-key="governance-about">
      <summary>About this view</summary>
      <p>Tag coverage measures whether resources have tags.
        Untagged billed resources have cost but no tags for allocation. Use existing tag keys
        as a starting point for consistent tagging.</p>
      <p>Configuration checks also identify eligible VMs without accelerated networking and
        empty resource groups. Each check reports its own source errors.</p>
    </details>`);

  // Coverage reads better as a shape than a column of percentages: the eye finds the worst
  // offender before it reads a single number.
  const worst = (t.by_type || []).slice(0, 10);
  if (worst.length > 1 && !t.error) {
    drawChart($("tagChart"), {
      type: "hbar",
      title: "Untagged resources by type",
      labels: worst.map((x) => x.type),
      datasets: [{ label: "Untagged", data: worst.map((x) => x.untagged) }],
    });
  } else {
    $("tagChart")?.remove();
  }
}

/**
 * Rate optimisation used to live here, and does not any more.
 *
 * It drew "Reservations and savings plans" from Advisor and an "Existing commitments" card
 * beside it — a third rendering of what the Commitments tab already answers properly and what
 * the Savings list was also carrying as cards. Three views of one purchase, each with its own
 * total. The tab id still redirects to Savings; the commitment half of the question is on the
 * Commitments tab, compared against pay-as-you-go, which is the only frame it means anything in.
 */


/**
 * Retirements and required migrations, from Azure Service Health.
 *
 * The sort is by deadline rather than by severity, because a date you can miss is more
 * actionable than a label. Overdue items lead — Azure leaves advisories active past their
 * deadline, and those are the ones already costing someone something.
 */
async function renderHealth(token) {
  const d = await live(token, `/api/dashboard/health${qs()}`, 120000);
  if (!d) return;

  const all = d.advisories || [];
  const c = d.counts || {};

  if (!all.length) {
    setContent($("tabBody"), `<section class="card"><h3>Retirements and advisories</h3>
      <p class="muted empty">Azure has no active advisories for the subscriptions in scope.</p>
      </section>`);
    return;
  }

  const when = (a) => {
    if (a.days_left == null) return '<span class="muted">no deadline</span>';
    if (a.overdue) return `<span class="delta up">${Math.abs(a.days_left)}d overdue</span>`;
    if (a.days_left <= 30) return `<span class="delta up">${a.days_left}d</span>`;
    if (a.days_left <= 90) return `<span class="delta">${a.days_left}d</span>`;
    return `<span class="muted">${a.days_left}d</span>`;
  };

  const kind = (a) =>
    a.retirement
      ? '<span class="pill bad">retirement</span>'
      : `<span class="pill${a.level === "Warning" ? " warn" : ""}">${esc(
          (a.level || "advisory").toLowerCase()
        )}</span>`;

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "Active advisories", value: String(c.active ?? 0) },
      {
        label: "Retirements",
        value: String(c.retirements ?? 0),
        cls: (c.retirements ?? 0) > 0 ? "up" : "",
        sub: "service or feature retirement",
      },
      {
        label: "Due within 90 days",
        value: String(c.due_90_days ?? 0),
        cls: (c.due_90_days ?? 0) > 0 ? "up" : "",
        sub: d.next_deadline ? `next: ${esc(d.next_deadline)}` : "no dated deadline",
      },
      {
        label: "Past deadline",
        value: String(c.overdue ?? 0),
        cls: (c.overdue ?? 0) > 0 ? "up" : "",
        sub: "still listed as active",
      },
    ])}

    ${table(
      "Azure advisories",
      all,
      [
        { label: "Advisory", cell: (a) => esc(a.headline || "—") },
        { label: "Kind", cell: kind, sort: false },
        { label: "Due", cell: (a) => esc(a.due || "—"), value: (a) => a.due || "" },
        {
          label: "Time left",
          num: true,
          // Sorts by urgency: overdue first, then soonest. Undated go last rather than
          // sorting as zero, which would put them above things that are genuinely due.
          value: (a) => (a.days_left == null ? 99999 : a.days_left),
          cell: when,
        },
        {
          label: "Your resources",
          num: true,
          value: (a) => a.resource_count,
          cell: (a) => (a.resource_count ? String(a.resource_count) : "—"),
        },
        { label: "Tracking", cell: (a) => esc(a.tracking || "—") },
      ],
      "By deadline · impacted resources are not available for every advisory type."
    )}

    ${
      // The detail nobody wants in a table cell, but everybody wants for the two that matter.
      all
        .filter((a) => a.retirement && a.days_left != null && a.days_left <= 120)
        .slice(0, 5)
        .map(
          (a) => `<section class="card">
            <h3>${esc(a.tracking)} — ${esc(a.due || "no deadline")}</h3>
            <p class="muted note"><strong>${esc(a.headline)}</strong></p>
            ${a.summary ? `<p class="muted note">${esc(a.summary)}</p>` : ""}
            ${a.actions ? `<p class="muted note"><strong>Recommended:</strong> ${esc(a.actions)}</p>` : ""}
            ${
              a.resources.length
                ? `<p class="muted note"><strong>Affects:</strong> ${a.resources
                    .slice(0, 12)
                    .map((r) => esc(r.name))
                    .join(", ")}${a.resource_count > 12 ? ` and ${a.resource_count - 12} more` : ""}</p>`
                : ""
            }
          </section>`
        )
        .join("")
    }

    <p class="muted note">${esc(d.note || "")}</p>`);
}

/**
 * Who looked at what, and when. Admin-only -- the endpoint refuses anyone else with a 403, and
 * loadTabs() does not even offer the tab on the rail to a non-admin, so reaching this render at
 * all already establishes the reader is allowed to be here.
 */
async function renderAuditLog(token) {
  const d = await live(token, `/api/dashboard/audit?limit=300`);
  if (!d) return;

  const rows = d.entries || [];

  // Written by now() in the app's own process, which runs in UTC on every deployment shape
  // this ships (App Service, a container, a laptop with no TZ override) -- so the "Z" here is
  // an assumption about the writer, not a guess about the reader, who always sees their own
  // local time either way.
  const whenCell = (r) => {
    if (!r.at) return "—";
    const parsed = new Date(`${r.at.replace(" ", "T")}Z`);
    return isNaN(parsed)
      ? esc(r.at)
      : esc(parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }));
  };

  setContent($("tabBody"), `
    ${kpiRow([
      { label: "Entries shown", value: String(rows.length), sub: `of up to ${300}` },
    ])}
    ${
      rows.length
        ? table(
            "Dashboard access",
            rows,
            [
              { label: "When", cell: whenCell, value: (r) => r.at || "" },
              { label: "User", cell: (r) => esc(r.user || "—") },
              { label: "Email", cell: (r) => esc(r.email || "—") },
              { label: "Source", cell: (r) => esc(r.source || "—") },
              {
                label: "Admin",
                cell: (r) => (r.admin ? '<span class="pill">admin</span>' : "—"),
              },
              { label: "Tab", cell: (r) => esc(r.tab || "—") },
            ],
            "Server fetches · newest first · local time."
          )
        : `<section class="card"><h3>Dashboard access</h3>
           <p class="muted empty">No dashboard tabs have been fetched from the server yet in
             this warehouse.</p></section>`
    }
   <details class="view-help" data-state-key="audit-about">
     <summary>About this view</summary>
     <p>Each row records a dashboard tab fetched from the server. Returning to a tab already
       loaded in this session uses the browser cache and does not create another entry.</p>
   </details>
  `);
}

/**
 * Commitments: reservations, savings plans and Spot.
 *
 * A warehouse query, so it is instant — but what it can *say* depends on how the warehouse was
 * built. An actual-cost ingest bills a reservation as one lump on the day it was bought and
 * carries no benefit attribution, so "what did the commitment save" has no honest answer. The
 * tab explains that rather than drawing a chart of zeroes.
 */

async function renderAnomalies(token) {
  // Started before the anomaly scan is awaited so it costs nothing extra, and built into this
  // tab's markup rather than appended: an appended card lands outside the flex layout the tab
  // body establishes and collapses to a one-pixel strip.
  const alertsPromise = alertsHtml();
  const d = await get(`/api/dashboard/anomalies${qs()}`, { timeout: 45000 });
  if (token !== ctx.loadToken) return;
  const alertPanel = await alertsPromise.catch(() => "");
  if (token !== ctx.loadToken) return;

  const cur = d.currency;
  // The window that was judged, not everything the warehouse holds. These differ whenever the
  // period selector is set to less than the loaded range, and showing the wider one next to a
  // count derived from the narrower one made the two disagree on screen.
  const from = d.window_from || d.from;
  const to = d.window_to || d.to;
  const anomalies = Array.isArray(d.anomalies) ? d.anomalies : [];
  const reasons = [d.coverage_reason || d.coverage?.reason,
    ...Object.entries(d.unavailable_currencies || {}).map(([code, reason]) => `${code}: ${reason}`)].filter(Boolean);
  const assessment = d.unavailable || !Array.isArray(d.anomalies) ? "Not assessed"
    : d.partial || reasons.length ? "Partial assessment" : "";
  const coverageNote = [...new Set(reasons)].join(" ") || d.note
    || "Complete anomaly coverage is unavailable; no all-clear conclusion can be made.";

  if (!anomalies.length) {
    setContent($("tabBody"), `
      ${kpiRow([
        { label: "Anomalies", value: assessment ? "—" : "0", sub: assessment || "nothing unusual" },
        { label: "Period", value: from && to ? `${from} → ${to}` : "—",
          sub: `last ${d.days || days} days` },
      ])}
      <section class="card">
        <h3>${assessment || "No anomalies detected"}</h3>
        <p class="muted">${esc(assessment ? coverageNote : d.note || "")}</p>
      </section>
      ${alertPanel}`);
    wireAnomalyAlerts();
    return;
  }

  setContent($("tabBody"), `
    ${assessment ? `<p class="banner" role="status"><strong>${assessment}</strong> · ${esc(coverageNote)}
      Observed findings remain visible; this is not a complete assessment.</p>` : ""}
    ${d.high ? `<div class="banner err"><strong>${d.high} high-impact
      ${d.high === 1 ? "change" : "changes"}</strong> in the last ${d.days || days} days.</div>` : ""}

    ${kpiRow([
      { label: "Anomalies", value: String(d.count ?? anomalies.length),
        sub: `${assessment ? "observed · partial · " : ""}over ${esc(from || "")} → ${esc(to || "")}` },
      { label: "Increases", value: String(d.increases), cls: d.increases ? "up" : "",
        sub: `${esc(currencyAmountList(d.impact_by_currency, null, money)
          || nativeAmount(d.impact, cur, money))} above baseline` },
      { label: "Decreases", value: String(d.decreases), cls: "down",
        sub: "spend that stopped" },
      { label: "High impact", value: String(d.high || 0),
        cls: d.high ? "up" : "", sub: d.high ? "needs attention" : "none" },
    ])}

    <div id="anomChart"></div>

    <section class="card">
      <div class="tagpick-head">
        <h3>Cost anomalies</h3>
        <span class="muted note">compared with a rolling 14-day median</span>
      </div>
      <p class="muted note">Latest
        ${esc(String(d.daily?.length && d.settled_to ? "two days" : "few days"))} excluded while Azure restates costs.</p>
      <div class="anoms">${anomalies.map(anomalyCard).join("")}</div>
      <details class="view-help" data-state-key="anomalies-about">
        <summary>About this view</summary>
        <p>Each finding is a day's spend outside its usual pattern, compared with a rolling
          14-day median. Recent days are excluded because Azure may revise costs after usage;
          waiting reduces alerts for temporary spikes.</p>
      </details>
    </section>
    ${alertPanel}`);

  wireAnomalyAlerts();
  wireRootCause();

  // The estate's own daily line, so a flagged day can be seen in context rather than trusted.
  const series = Object.keys(d.daily_by_currency || {}).length
    ? Object.entries(d.daily_by_currency) : [[cur, d.daily || []]];
  const drawable = series.filter(([currency, rows]) => /^[A-Z]{3}$/.test(currency || "") && rows.length);
  $("anomChart").innerHTML = drawable.map((_, index) => `<div class="card chart-card" id="anomCurrency${index}"></div>`).join("");
  drawable.forEach(([currency, rows], index) => {
    const flagged = new Set(anomalies.filter(a => (a.currency || cur) === currency).map(a => a.day));
    drawChart($(`anomCurrency${index}`), {
      title: `Daily spend · ${currency} · flagged days`,
      type: "line",
      labels: rows.map((x) => x.day),
      datasets: [{ label: "Daily cost", data: rows.map((x) => x.cost) }],
      pointColors: rows.map((x) => (flagged.has(x.day) ? "up" : null)),
      currency,
    });
  });
}

/**
 * The head every status card shares: a name, a qualifier under it, a figure and a pill.
 *
 * Budgets, anomalies and schedules had three copies of this markup with different class names,
 * which is three places to change when the shape does — and three chances for them to drift
 * apart. They are the same object visually and semantically: a thing, what it is, how it is
 * doing.
 */

function anomalyCard(a) {
  const up = a.direction === "up";
  const cur = a.currency;
  return `<article class="anom ${esc(a.severity)}">
    ${cardHead({
      name: a.key,
      sub: `${a.dimension} · ${a.day} · ${cur || "currency unknown"}`,
      figure: `${up ? "+" : ""}${esc(nativeAmount(a.delta, cur, money))}`,
      figureClass: up ? "up" : "down",
      status: a.severity,
      statusClass: a.severity,
    })}
    <div class="budget-meta">
      <span>${esc(nativeAmount(a.baseline, cur, money))} → <strong>${esc(nativeAmount(a.cost, cur, money))}</strong>
        on the day</span>
      <span>${a.percent == null ? "from nothing" : `${a.percent > 0 ? "+" : ""}${a.percent}%`}</span>
      <span>${a.score >= 50 ? "far outside" : `${a.score}×`} the usual variation</span>
    </div>
    ${a.also?.length ? `<p class="muted note">Also visible as
      ${a.also.map((x) => `${esc(x.dimension)} <strong>${esc(x.key)}</strong>`).join(", ")}.</p>` : ""}
    ${a.dimension === "resource group" ? rootCauseMarkup(a) : ""}
  </article>`;
}

/**
 * "What changed?", offered only for a resource-group finding.
 *
 * Activity Log's own `$filter` can match `resourceGroupName` server-side, so that dimension's
 * `key` is a scope this can actually search with confidence. A resource-level finding's `key`
 * is a display name the warehouse derived from a resourceId after load, not reliably the same
 * string Activity Log indexes by, so it is left without this control rather than searched with
 * a name that may silently not match anything.
 */
function rootCauseMarkup(a) {
  return `<details class="rc-drill" data-rg="${esc(a.key)}" data-day="${esc(a.day)}">
    <summary>What changed?</summary>
    <div class="rc-result"></div>
  </details>`;
}

/**
 * Fetched once, the first time a card is opened, not on every toggle -- `<details>` fires
 * `toggle` on closing too, and `dataset.loaded` is what keeps a second open from asking again.
 */
function wireRootCause() {
  for (const details of $("tabBody").querySelectorAll(".rc-drill")) {
    if (details.dataset.rootCauseWired) continue;
    details.dataset.rootCauseWired = "true";
    details.addEventListener("toggle", () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = "1";
      loadRootCause(details);
    });
  }
}

async function loadRootCause(details) {
  const body = details.querySelector(".rc-result");
  body.innerHTML = `<p class="muted">Checking Activity Log…</p>`;
  try {
    const r = await post(`/api/dashboard/anomalies/root-cause${qs()}`,
      { resource_group: details.dataset.rg, day: details.dataset.day }, { timeout: 30000 });
    const changes = r.changes || [];
    if (!changes.length) {
      body.innerHTML = `<p class="muted note">${esc(r.note
        || "No write or delete operations found in this resource group around that day.")}</p>`;
      return;
    }
    body.innerHTML = `
      <ul class="rc-list">${changes.map((c) => `<li>
        <span class="rc-time">${esc((c.time || "").replace("T", " ").slice(0, 16))}Z</span>
        <span class="rc-ops">${c.operations.map(esc).join(", ")}</span>
        <span class="rc-res">${c.resources.map(esc).join(", ") || "—"}</span>
        <span class="rc-who">${esc(c.caller || "—")}</span>
        ${c.succeeded ? "" : '<span class="pill bad">failed</span>'}
      </li>`).join("")}</ul>
      <p class="muted note">${esc(r.note || "")}</p>`;
  } catch (err) {
    body.innerHTML = `<p class="bad">${esc(err?.message
      || "Could not check Activity Log right now.")}</p>`;
  }
}

// ============================================================== cost exports

/**
 * The scheduled exports Azure writes on our behalf — created, run and deleted here.
 *
 * Why they are worth having: a refresh through the Cost Details API asks Azure to *generate* a
 * report and then waits — about forty seconds each, queued behind a per-subscription rate
 * limit, so minutes for a three-month load. A scheduled export inverts that. Azure writes the
 * data to blob storage overnight and a refresh becomes a blob read.
 *
 * The reason this is possible here at all is managed identity: the tenant blocks storage
 * account keys, which is how a Cost Management export normally authenticates, so an export
 * created without an identity fails. Everything created here gets one.
 *
 * This tab used to carry the raw-row export as well, under the name "Export settings". Two
 * unrelated things sharing a page that described neither: one creates a standing job in Azure,
 * the other hands you a file now. The file half moved to Export data, next to the report
 * builder, and what is left is named for what it is.
 */

// ================================================================ budgets

/**
 * Every budget in scope, worst first.
 *
 * Budgets had no home in this app: they could be created from the Tags tab and then only ever
 * seen again in the portal, which makes the one question a budget exists to answer — "am I
 * over?" — a trip to another product. The list is read live because budgets are not in a cost
 * export, and a stale "you're fine" is the one answer this page must never give.
 */
async function renderBudgets(token, refresh = false) {
  const request = ++budgetRequest;
  let d;
  try {
    const query = budgetScopeQs();
    d = await get(`/api/budgets${query}${refresh ? `${query ? "&" : "?"}refresh=true` : ""}`, { timeout: 45000 });
  } catch (err) {
    d = { budgets: [], error: err?.message || "The budget request failed." };
  }
  if (token !== ctx.loadToken || request !== budgetRequest) return;

  const rows = Array.isArray(d?.budgets) ? d.budgets : [];
  const issue = budgetReadIssue(d);
  const demoMode = d?.demo_mode === true;
  const budgetHelp = `<details class="view-help" data-state-key="budgets-about">
    <summary>About this view</summary>
    <p>Budgets track spending against a limit and support Azure notifications before invoicing.
      Create a budget from a tag selection to use those tags as its filter.</p>
    <p>Spend reflects the budget's current period as last computed by Azure, not the dashboard
      date range. Budgets may overlap or use different currencies, so limits and spend are not
      combined into an estate total.</p>
  </details>`;
  const refreshButton = demoMode ? ""
    : '<button type="button" class="linkish" id="budgetRefresh">Refresh budgets</button>';
  const wireRefresh = () => {
    const button = $("budgetRefresh");
    if (button) button.onclick = async () => {
      if (token !== ctx.loadToken) return;
      button.disabled = true;
      button.textContent = "Refreshing…";
      await renderBudgets(token, true);
    };
  };
  if (!rows.length) {
    setContent($("tabBody"), `
      <section class="card">
        <div class="tagpick-head"><h3>${demoMode ? "No budgets in demo mode"
          : issue ? "Budget data incomplete" : "No budgets in scope"}</h3>${refreshButton}</div>
        ${demoMode ? `<p class="muted note">${esc(d.note
          || "Budgets are not included in the PostgreSQL demo data. No Azure budget was requested.")}</p>` : ""}
        ${issue ? `<div class="banner warn" role="status">${esc(issue)} Retry the read; an empty result does not confirm that no budgets exist.</div>` : ""}
        ${demoMode ? "" : `<p class="muted">Create a budget from your selection in
          <button type="button" class="linkish" data-goto="tags">Cost by Application /
          Tags</button>.</p>${budgetHelp}`}
      </section>`);
    wireBudgetLinks();
    wireRefresh();
    return;
  }

  const over = rows.filter((b) => budgetStatus(b) === "over");
  const near = rows.filter((b) => budgetStatus(b) === "near");
  const unknown = rows.filter((b) => budgetStatus(b) === "unknown");

  setContent($("tabBody"), `
    ${issue ? `<div class="banner warn" role="status"><strong>Incomplete budget data.</strong> ${esc(issue)} Counts below cover only returned budgets.</div>` : ""}
    ${over.length ? `<div class="banner err"><strong>${over.length} budget${
      over.length === 1 ? " is" : "s are"} over.</strong> ${esc(
        over.slice(0, 3).map((b) => `${b.name} at ${b.percent_used}%`).join(", "))}${
      over.length > 3 ? ` and ${over.length - 3} more` : ""}.</div>` : ""}

    ${kpiRow([
      { label: issue ? "Budgets returned" : "Budgets", value: String(rows.length),
        sub: `across ${new Set(rows.map((b) => b.subscription_id || b.subscription)).size} subscription(s)` },
      { label: "Over budget", value: String(over.length),
        cls: over.length ? "up" : "", sub: over.length ? "needs attention" : "none reported over" },
      { label: "Near the limit", value: String(near.length),
        sub: "at or past 80%" },
      { label: "Unknown status", value: String(unknown.length),
        sub: "spend or utilization not reported" },
    ])}

    ${budgetPortfolioMarkup(rows)}

    <section class="card">
      <div class="tagpick-head">
        <h3>Budgets</h3>
        ${refreshButton}
        <button type="button" class="linkish" data-goto="tags">Create one from tags</button>
      </div>

      <!-- Ten budgets is already more than fits on a screen, and on a large estate it is
           dozens. The question someone opens this tab with is almost never "show me all of
           them" — it is "which ones need me", or "the one called X". Both are one click or a
           few keystrokes away rather than a scroll. -->
      <div class="budget-tools">
        <div class="seg" role="group" aria-label="Filter budgets">
          ${[["all", "All", rows.length],
             ["attention", "Needs attention", over.length + near.length],
             ["over", "Over", over.length],
             ["near", "Near", near.length],
             ["ok", "On track", rows.filter((b) => budgetStatus(b) === "ok").length],
             ["unknown", "Unknown", unknown.length]]
            .map(([id, label, n]) => `<button type="button" class="seg-btn${
              budgetFilter.status === id ? " on" : ""}" data-bstatus="${id}"
              aria-pressed="${budgetFilter.status === id}">${
              esc(label)} <span class="seg-n">${n}</span></button>`).join("")}
        </div>
        <input id="budgetFind" type="search" class="tagfind" placeholder="Find a budget…"
               value="${esc(budgetFilter.q)}" autocomplete="off" spellcheck="false"
               aria-label="Find a budget by name, subscription or filter">
        <button type="button" class="linkish" id="budgetReset">Clear filters</button>
      </div>

      <p class="muted note" id="budgetMatchCount" role="status" aria-live="polite"></p>
      <p class="muted note">Azure budget-period spend can lag usage by a day; dashboard dates do not apply.
        Overlapping budgets and different currencies are not summed.</p>
      <div class="budgets" id="budgetList"></div>
      ${budgetHelp}
    </section>`);

  wireBudgetLinks();
  wireRefresh();

  const syncFilterButtons = () => {
    for (const other of $("tabBody").querySelectorAll("[data-bstatus]")) {
      const on = other.dataset.bstatus === budgetFilter.status;
      other.classList.toggle("on", on);
      other.setAttribute("aria-pressed", String(on));
    }
  };
  for (const btn of $("tabBody").querySelectorAll("[data-bstatus]")) {
    btn.onclick = () => {
      budgetFilter.status = btn.dataset.bstatus;
      syncFilterButtons();
      paintBudgetList(rows);
    };
  }
  const find = $("budgetFind");
  if (find) {
    find.oninput = () => {
      budgetFilter.q = find.value;
      paintBudgetList(rows);
    };
  }
  const reset = $("budgetReset");
  if (reset) reset.onclick = () => {
    budgetFilter = { status: "all", q: "" };
    if (find) find.value = "";
    syncFilterButtons();
    paintBudgetList(rows);
    find?.focus();
  };
  const portfolio = $("budgetPortfolio");
  if (portfolio) portfolio.ontoggle = () => { budgetPortfolioOpen = portfolio.open; };

  // The portfolio row is a shortcut to the full card, not a second place to read the same
  // figure — so clicking it clears whatever is hiding that card (a status filter, a search
  // that does not match its name) rather than leaving the jump to land on nothing.
  for (const btn of $("tabBody").querySelectorAll("[data-jump-budget]")) {
    btn.onclick = () => {
      const idx = Number(btn.dataset.jumpBudget);
      budgetFilter = { status: "all", q: "" };
      budgetExpanded.add(budgetIdentity(rows[idx]));
      syncFilterButtons();
      if (find) find.value = "";
      paintBudgetList(rows);
      const card = $(`budget-row-${idx}`);
      if (!card) return;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("flash");
      card.addEventListener("animationend", () => card.classList.remove("flash"), { once: true });
    };
  }

  paintBudgetList(rows);
}

/**
 * Which budgets are on screen. Kept outside the render so a filter survives the tab being
 * left and returned to — losing it on every visit is what makes a filter not worth using.
 */
let budgetFilter = { status: "all", q: "" };
const budgetExpanded = new Set();
let budgetPortfolioOpen = false;
let budgetRequest = 0;
const budgetIdentity = b => JSON.stringify([b?.id || "", b?.subscription_id || b?.subscription || "", b?.name || ""]);

/**
 * Draw the filtered list.
 *
 * Separate from `renderBudgets` because filtering must not refetch: the budgets come from a
 * live Azure call that takes seconds, and re-running it to hide four cards would make the
 * control feel broken. The data is already here; this is only ever a redraw.
 */
function paintBudgetList(rows) {
  const host = $("budgetList");
  if (!host) return;

  const q = budgetFilter.q.trim();
  const wanted = filterBudgets(rows, budgetFilter);
  const count = $("budgetMatchCount");
  if (count) count.textContent = `${wanted.length} of ${rows.length} returned budgets match`;

  if (!wanted.length) {
    host.innerHTML = `<p class="muted empty">No budget matches${
      q ? ` “${esc(q)}”` : ""}${budgetFilter.status === "all" ? "" : " in this state"}. Clear filters to see all returned budgets.</p>`;
    return;
  }

  // The index is into the *unfiltered* list, so the chart and the tag link still find their
  // own budget after a filter has reordered what is on screen.
  host.innerHTML = wanted.map((b) => budgetCard(b, rows.indexOf(b), {
    compact: true, expanded: budgetExpanded.has(budgetIdentity(b)),
  })).join("");

  for (const btn of host.querySelectorAll("[data-tagsfor]")) {
    btn.onclick = () => {
      const b = rows[Number(btn.dataset.tagsfor)];
      if (b && b.filter_exact !== false && b.filtered_exactly !== false && b.trend?.filtered_exactly !== false)
        showTagsFor(b.tag_filter, b.name);
    };
  }
  for (const el of host.querySelectorAll("[data-goto]")) {
    el.onclick = () => selectTab(el.dataset.goto);
  }
  // After the markup, because each chart needs its host in the document. Drawn per budget
  // rather than as one combined chart: budgets have different limits, different periods and
  // sometimes different currencies, so stacking them on one axis would compare quantities
  // that are not comparable.
  for (const detail of host.querySelectorAll("[data-budget-index]")) {
    const b = rows[Number(detail.dataset.budgetIndex)];
    const draw = () => {
      const chart = detail.querySelector(".budget-chart");
      if (chart && !chart.dataset.drawn) {
        chart.dataset.drawn = "true";
        return loadBudgetTrendChart(chart, b, {
          get, currency: readStore("costCurrency", ""), drawBudgetChart, esc,
          options: { compact: true }, isCurrent: () => detail.isConnected,
        }).then(loaded => {
          if (!loaded && detail.isConnected) delete chart.dataset.drawn;
        });
      }
    };
    detail.ontoggle = () => {
      if (!detail.isConnected) return;
      if (detail.open) {
        budgetExpanded.add(budgetIdentity(b));
        return draw();
      } else budgetExpanded.delete(budgetIdentity(b));
    };
    if (detail.open) draw();
  }
}

/** The budget list follows the scope picker, like every other tab. */
function budgetScopeQs() {
  const scope = scopeIds();
  const cur = readStore("costCurrency", "");
  const parts = [
    scope.length ? `scope=${encodeURIComponent(scope.join(","))}` : "",
    cur ? `currency=${encodeURIComponent(cur)}` : "",
  ].filter(Boolean);
  return parts.length ? `?${parts.join("&")}` : "";
}


function budgetPortfolioMarkup(rows) {
  if (rows.length < 2) return "";

  const LIMIT = 15;
  const shown = rows.slice(0, LIMIT);
  const mixed = new Set(rows.map((b) => b.currency).filter(Boolean)).size > 1;

  const row = (b) => {
    const idx = rows.indexOf(b);
    const status = budgetStatus(b);
    const pct = status === "unknown" ? null : b.percent_used;
    const blown = pct != null && pct > 100;
    const fill = pct == null ? 0 : blown ? (100 / pct) * 100 : Math.max(0, pct);
    const spill = blown ? 100 - fill : 0;
    return `<button type="button" class="bp-row" data-jump-budget="${idx}">
      <span class="bp-name">${esc(b.name || "—")}</span>
      <span class="bp-sub">${esc(b.subscription || "")}</span>
      <span class="bmeter bp-bar ${esc(status)}" role="img"
            aria-label="${esc(`${b.name}: ${pct == null ? "no spend reported" : `${pct}% of budget used`}`)}">
        <span class="fill" style="width:${fill}%"></span>
        ${spill ? `<span class="spill" style="width:${spill}%"></span>` : ""}
      </span>
      <span class="bp-pct ${esc(status)}">${pct == null ? "—" : `${pct}%`}</span>
    </button>`;
  };

  return `<details class="card budget-portfolio" id="budgetPortfolio"${budgetPortfolioOpen ? " open" : ""}>
    <summary class="budget-portfolio-summary">
      <strong>Compare utilization</strong>
      <span class="muted note" style="margin:0">${
        mixed ? "Sorted worst first · mixed currencies" : "Sorted worst first"
      }${rows.length > LIMIT ? ` · showing ${LIMIT} of ${rows.length}` : ""}</span>
    </summary>
    <div class="bp-rows">${shown.map(row).join("")}</div>
  </details>`;
}

/**
 * One budget, as a bar rather than a row of numbers.
 *
 * A percentage in a table is read; a bar that has run past its end is *seen*. Over budget gets
 * red and an explicit label, and the overflow is drawn as a distinct segment past the 100%
 * mark, so "20% over" is a visible quantity rather than a number above 100 that has to be
 * mentally subtracted from itself.
 */



/**
 * The shape behind a budget's one number.
 *
 * Azure reports a budget as a single figure with no history, so the meter above can say "93%"
 * without answering the question that actually follows: is that a steady climb that lands just
 * inside the limit, or did it breach three weeks ago and keep going? `drawBudgetChart` lives in
 * the shell and arrives on ctx, because Hourly Billing draws the same chart for a budget that
 * matches its tag selection.
 */



async function alertsHtml() {
  const sub = scopeIds()[0] || "";
  let d;
  try {
    d = await get(`/api/anomalies/alerts${sub ? `?subscription=${encodeURIComponent(sub)}` : ""}`,
                  { timeout: 30000 });
  } catch (err) {
    // Advisory, but not silent. A panel that vanishes when Azure refuses the read is how
    // somebody concludes the feature does not exist, rather than that they lack the permission.
    return `<section class="card"><h3>Anomaly email alerts</h3>
      <p class="muted empty">Could not read the anomaly alerts on this subscription.
      ${esc(err?.message || "")}</p></section>`;
  }
  const alerts = d.alerts || [];
  const subs = d.subscriptions || [];
  // Read from the header the app has already painted rather than re-fetching identity: this
  // panel is part of a tab that is about to render, and a second whoami round trip that fails
  // would take the whole panel with it.
  const admin = !document.getElementById("whoAdmin")?.hidden;
  // An expired alert is the case worth shouting about: the portal still shows it as Enabled, so
  // everyone believes they are covered while nothing has been sent for weeks.
  const dead = alerts.filter((a) => a.expired);
  const live = alerts.find((a) => a.sending) || alerts[0] || null;
  // A scheduled action lives on one subscription, so with more than one in scope the target
  // cannot be implicit: an alert created "here" would silently be created somewhere.
  const target = live?.subscription_id || sub || subs[0]?.id || "";
  const uncovered = subs.filter(
    (x) => !alerts.some((a) => a.subscription_id === x.id && a.sending));

  // Expiry, said as a state rather than a date. "endDate 2026-07-21" is something to work out;
  // "expires in 42 days" is something to act on.
  const life = (a) => {
    if (a.expired) return `<span class="delta up">expired ${Math.abs(a.expires_in_days)}d ago</span>`;
    if (a.expires_in_days == null) return '<span class="muted">no end date</span>';
    if (a.expires_in_days <= 30) return `<span class="delta up">${a.expires_in_days}d left</span>`;
    return `<span class="muted">${a.expires_in_days}d left</span>`;
  };

  const hours = [6, 7, 8, 9, 10, 12, 15, 18]
    .map((h) => `<option value="${h}"${(live?.hour_of_day ?? 8) === h ? " selected" : ""}>${
      String(h).padStart(2, "0")}:00</option>`)
    .join("");

  const subPicker = subs.length > 1
    ? `<label class="muted note" for="anomSub">on</label>
       <select id="anomSub">${subs.map((x) => `<option value="${esc(x.id)}"${
         x.id === target ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select>`
    : `<input type="hidden" id="anomSub" value="${esc(target)}" />`;

  return `
    <section class="card" id="anomAlerts">
      <div class="tagpick-head">
        <h3>Anomaly email alerts</h3>
        <span class="muted note">Azure notifications</span>
      </div>
      ${
        alerts.length
          ? `<div class="table-wrap"><table>
               <thead><tr><th>Alert</th>${subs.length > 1 ? "<th>Subscription</th>" : ""}
                 <th>To</th><th>When</th><th>Status</th>
                 <th>Expires</th>${admin ? "<th></th>" : ""}</tr></thead>
               <tbody>${alerts.map((a) => `<tr>
                 <td>${esc(a.display_name || a.name || "—")}</td>
                 ${subs.length > 1 ? `<td>${esc(a.subscription || "—")}</td>` : ""}
                 <td>${esc((a.emails || []).join(", ") || "—")}</td>
                 <td>${esc(a.frequency || "—")}${a.hour_of_day != null
                      ? ` at ${String(a.hour_of_day).padStart(2, "0")}:00` : ""}</td>
                 <td>${a.sending
                      ? '<span class="pill good">sending</span>'
                      : `<span class="pill bad">${a.expired ? "expired" : "off"}</span>`}</td>
                 <td>${life(a)}</td>
                 ${admin
                    ? `<td class="num"><button type="button" class="anom-del"
                         data-name="${esc(a.name || "")}"
                         data-sub="${esc(a.subscription_id || "")}">Turn off</button></td>`
                    : ""}
               </tr>`).join("")}</tbody></table></div>`
          : `<p class="muted">No anomaly email alerts configured. Azure detection remains active; no email is sent.</p>`
      }
      ${
        // Which subscriptions are genuinely uncovered. With three in scope and one alert, the
        // table above reads as "we are covered" unless the gap is named.
        uncovered.length && subs.length > 1
          ? `<div class="banner warn"><strong>${uncovered.length} of ${subs.length}
               subscription${subs.length === 1 ? "" : "s"} ${uncovered.length === 1
                 ? "has" : "have"} nobody being emailed about anomalies:</strong>
               ${esc(uncovered.map((x) => x.name).join(", "))}.</div>`
          : ""
      }
      ${
        (d.unreadable || []).length
          ? `<div class="banner warn">Could not read the alerts on
               ${esc((d.unreadable || []).join(", "))}. Listing scheduled actions needs Cost
               Management Reader on the subscription.</div>`
          : ""
      }
      ${
        dead.length
          ? `<div class="banner warn">${esc(dead[0].warning || "")} Azure still shows it as
               enabled, so it looks configured. Saving below extends it another year.</div>`
          : ""
      }
      ${
        admin
          ? `<div class="fr-actions">
               <input id="anomEmails" type="text" placeholder="you@company.com, ops@company.com"
                 data-seed="${esc((live?.emails || []).join(", "))}"
                 value="${esc((live?.emails || []).join(", "))}"
                 style="flex:1;min-width:16rem" />
               ${subPicker}
               <label class="muted note" for="anomHour">every day at</label>
               <select id="anomHour">${hours}</select>
               <button type="button" id="anomSave" class="primary">${
                 alerts.length ? "Update alert" : "Create alert"}</button>
             </div>
             <p class="muted note">${esc(d.note || "")}</p>`
          : `<p class="muted note">${esc(d.note || "")} An administrator can change these.</p>`
      }
      <details class="view-help" data-state-key="anomaly-alerts-about">
        <summary>About this view</summary>
        <p>Azure checks each subscription daily using its own anomaly model and sends configured
          email alerts independently of this dashboard. The page does not need to be open.</p>
      </details>
    </section>`;
}

/** The save and turn-off buttons, wired after the tab's markup is in place. */
function wireAnomalyAlerts() {
  const sub = scopeIds()[0] || "";
  const panel = $("anomAlerts");
  if (!panel) return;
  /** One place to say what happened, so repeated attempts replace rather than stack up. */
  const say = (text, bad) => {
    let el = panel.querySelector(".anom-said");
    if (!el) {
      el = document.createElement("div");
      el.className = "anom-said banner";
      panel.append(el);
    }
    el.className = `anom-said banner${bad ? " warn" : ""}`;
    el.textContent = text;
  };

  for (const b of panel.querySelectorAll(".anom-del")) {
    b.onclick = async () => {
      b.disabled = true;
      b.textContent = "Turning off…";
      try {
        const res = await fetch(
          `/api/anomalies/alerts/${encodeURIComponent(b.dataset.name)}` +
          `?subscription=${encodeURIComponent(b.dataset.sub || sub)}`, { method: "DELETE" });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
      } catch (err) {
        b.disabled = false;
        b.textContent = "Turn off";
        say(err?.message || "Could not turn that alert off.", true);
        return;
      }
      b.closest("tr")?.remove();
      say("That alert has been removed. Nobody is being emailed about anomalies on this " +
          "subscription unless another alert is listed above.");
    };
  }

  const btn = $("anomSave");
  if (!btn) return;
  const box = $("anomEmails");
  if (box && !box.value && box.dataset.seed) box.value = box.dataset.seed;

  btn.onclick = async () => {
    const emails = (box?.value || "").split(",").map((x) => x.trim()).filter(Boolean);
    if (!emails.length) {
      say("Give at least one email address to alert.", true);
      box?.focus();
      return;
    }
    const hour = Number($("anomHour")?.value || 8);
    // The subscription the alert is written to. A scheduled action is per subscription, so
    // saving without naming one would create it somewhere the reader did not choose.
    const picker = $("anomSub");
    const target = picker?.value || sub;
    const where = picker?.selectedOptions?.[0]?.textContent || "this subscription";
    if (!target) {
      say("No subscription is in scope to create the alert on.", true);
      return;
    }
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      await post("/api/anomalies/alerts",
                 { subscription_id: target, emails, hour_of_day: hour },
                 { timeout: 60000 });
    } catch (err) {
      btn.disabled = false;
      btn.textContent = label;
      say(err?.message || "Could not save the alert.", true);
      return;
    }
    btn.disabled = false;
    btn.textContent = "Update alert";
    say(`Azure will email ${emails.join(", ")} at ${String(hour).padStart(2, "0")}:00 daily ` +
        `whenever it detects a cost anomaly on ${where}.`);
  };
}


/**
 * AKS, where the bill is not where the cluster is.
 *
 * A cluster resource bills the control plane and nothing else — zero on the Free tier. The node
 * VMs, disks, load balancers and egress live in a managed resource group beside it, so an
 * estate that reads as free is the normal case rather than good news, and every other view in
 * this app reports a Kubernetes bill as unattributed compute and networking.
 *
 * It is a segment of the area picker inside Overview rather than an entry on the rail, because
 * that is what it actually is: one more way to slice the same spend, sitting beside All spend
 * and the service families. The difference from those families — and the reason the segment is
 * marked and ruled off — is that this slice *overlaps* them. The node VMs below are already
 * inside Compute & Web and the load balancers already inside Networking, so the strip must
 * never be read as a set of parts that add up.
 *
 * Resource Graph names the clusters, and the segment's own figure does not depend on it: the
 * total in the picker is a warehouse query, so an unreachable Resource Graph costs the cluster
 * names and the tier, not the money.
 */

  return { renderGovernance, renderHealth, renderAuditLog, renderAnomalies, renderBudgets };
}
