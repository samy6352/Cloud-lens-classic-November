/**
 * CloudLens — chat client.
 *
 * Design notes:
 *  - Chart.js (200KB) is fetched on demand the first time a chart arrives, so a session that
 *    only asks text questions never pays for it.
 *  - Every fetch has a timeout and the summary strip retries, because a throttled Azure call
 *    should degrade to "—" rather than hang the page forever.
 *  - Streaming output is announced to screen readers once per step, not per token.
 */

import { chartTooltip, wireChartTooltip, wireTooltips } from "./tooltips.js";
import { budgetPresentation } from "./overview-status.js";
import { glanceDestination } from "./dashboard-drilldowns.js";
import { createSavingsScan } from "./dashboard-savings-scan.js";
import { createViewRevalidation, recordSnapshot } from "./dashboard-freshness.js";
export { readSnapshotHeaders } from "./dashboard-freshness.js";

const $ = (id) => document.getElementById(id);
const chat = $("chat");
const liveRegion = $("live");

let history = [];
let busy = false;
let controller = null;
let conversationGeneration = 0;

// Which subscriptions to analyse. Empty = all. Persisted across reloads.
let allSubs = [];
let selected = new Set(readStore("costScope", []));
let introTemplate = "";
let pinned = true;
// Who is signed in, once known. The picker's caption depends on it, so it lives here rather
// than inside loadIdentity — two functions writing the same line is how the caption ended up
// silently reverting to the generic wording.
let identity = null;

// ------------------------------------------------------------------- utils
// Exported because the filter bar keeps its selection the same way everything else does, and a
// third copy of six lines is how two of them end up disagreeing about the try/catch.
export function readStore(key, fallback) {
  // Private browsing and some hardened configs throw on access rather than returning null.
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}
export function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* not fatal — the choice just won't survive a reload */
  }
}

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const numberFormats = new Map();
function currencyFormat(cur = "USD", digits = 0) {
  const currency = cur || "USD";
  const key = `${currency}|${digits}`;
  let fmt = numberFormats.get(key);
  if (!fmt) {
    fmt = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: digits,
    });
    numberFormats.set(key, fmt);
  }
  return fmt;
}

const money = (n, cur = "USD", digits = 0) =>
  n == null || Number.isNaN(n) ? "—" : currencyFormat(cur, digits).format(n);

/**
 * The same figure, with the currency symbol marked up separately.
 *
 * A symbol set at the same size and weight as the digits competes with them, and in a column
 * it is the symbol that lines up rather than the numerals. Splitting it lets CSS hold it back
 * a shade so the quantity reads first — the detail that separates a financial tool from a
 * spreadsheet dump.
 *
 * Safe to insert as HTML: every part comes from Intl's own formatter, not from user input.
 */
const moneyHtml = (n, cur = "USD", digits = 0) => {
  if (n == null || Number.isNaN(n)) return "—";
  const parts = currencyFormat(cur, digits).formatToParts(n);
  return parts
    .map((p) =>
      p.type === "currency" ? `<span class="cur">${esc(p.value)}</span>` : esc(p.value)
    )
    .join("");
};

// The dashboard needs the same formatting, fetching and charting as the chat. Exporting them
// keeps one implementation rather than two that drift.
export { esc, money, moneyHtml };

const announce = (text) => {
  if (liveRegion) liveRegion.textContent = text;
};

/** Send the person to the login page, remembering where they were trying to be. */
function toLogin() {
  const here = location.pathname + location.search;
  location.replace(`/login?next=${encodeURIComponent(here)}`);
}

/** fetch with a timeout, so a stalled request can't wedge the UI. */
export async function get(url, { timeout = 20000, signal, ...opts } = {}) {
  const ac = new AbortController();
  const abort = () => ac.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => ac.abort(), timeout);
  try {
    const r = await fetch(url, { ...opts, signal: ac.signal });
    // An expired session shouldn't look like a broken app: bounce to the login page instead
    // of painting every tile with an error.
    if (r.status === 401) {
      toLogin();
      throw new Error("Signed out");
    }
    if (r.status === 403) {
      const body = await r.json().catch(() => ({}));
      const err = new Error(body.detail || "You don't have access to that.");
      err.forbidden = true;
      throw err;
    }
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return recordSnapshot(await r.json(), url, r.headers, opts.method || "GET");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

/**
 * A write, with the server's own explanation preserved.
 *
 * `get` turns anything that is not 401/403 into "409 Conflict" — the status text and nothing
 * else. That is fine for reads, where a failure means a panel does not draw, but these
 * endpoints refuse with a sentence worth showing: which subscription was not permitted, or
 * that setup has already been done. Throwing away the body would replace all of that with a
 * number.
 */
export async function post(url, body = null, { timeout = 30000 } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeout);
  try {
    const r = await fetch(url, {
      method: "POST",
      signal: ac.signal,
      ...(body === null ? {} : {
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }),
    });
    if (r.status === 401) {
      toLogin();
      throw new Error("Signed out");
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(data.detail || `${r.status} ${r.statusText}`);
      err.status = r.status;
      throw err;
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

// marked + DOMPurify are 57 KB and only matter once the agent answers, so they load on first
// use. Streaming starts before they arrive, hence the escaped-text fallback below: early tokens
// render as plain text and the first full re-render after loading upgrades them to markdown.
let mdLib = null;

function loadMarkdown() {
  if (mdLib) return mdLib;
  mdLib = Promise.all([
    loadScript("/assets/vendor/marked.min.js"),
    loadScript("/assets/vendor/purify.min.js"),
  ]).catch(() => null); // a failed fetch must degrade to plain text, never break the answer
  return mdLib;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(s);
  });
}

function render(md) {
  if (!window.marked || !window.DOMPurify) {
    loadMarkdown();
    return esc(md).replace(/\n/g, "<br>");
  }
  return DOMPurify.sanitize(marked.parse(md || ""));
}

/** Wrap tables so they scroll independently instead of stretching the page on mobile. */
function enhance(container) {
  for (const t of container.querySelectorAll("table")) {
    if (t.parentElement?.classList.contains("table-wrap")) continue;
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "Table, scrollable");
    t.replaceWith(wrap);
    wrap.appendChild(t);
  }
}

const TOOL_LABEL = {
  query_costs: "warehouse SQL",
  render_chart: "chart",
  warehouse_status: "warehouse status",
  find_waste: "idle resources",
  vm_utilisation: "VM utilisation",
  advisor_recommendations: "Advisor",
  list_subscriptions: "subscriptions",
  cost_summary: "live spend",
  cost_trend: "live trend",
  cost_changes: "live changes",
  cost_forecast: "forecast",
  budgets: "budgets",
  top_resources: "top resources",
};

// -------------------------------------------------------------------- boot
async function boot() {
  wireTooltips();
  introTemplate = document.querySelector(".intro").outerHTML;
  wire();
  wireTheme();
  loadIdentity();
  loadHealth();
  loadSubscriptions().then(() => loadGlance());
  loadWarehouse();
  loadFirstRun();
}

// ---------------------------------------------------------------- first run
/**
 * The empty-deployment case, which is the one moment nobody has any context.
 *
 * A warehouse with no rows and an estate that genuinely spent nothing draw exactly the same
 * dashboard — six zeroes and some empty tables — and only one of those is a fact about
 * anyone's spend. Left alone it reads as a broken product, and the actual fix (create a Cost
 * Management export, point it at storage, grant its identity a role, wait, then load the
 * blobs) is spread across two tabs and a form.
 *
 * So when there is nothing at all, say so and offer to go and find it. The offer is shown
 * only while the deployment is genuinely empty and unanswered: once rows land, the condition
 * that produced it is false forever, which is why "only once" needs almost no bookkeeping.
 */
let firstRunPoll = null;

function placeAccount(onboarding) {
  const account = $("who");
  const destination = $(onboarding ? "onboardingAccount" : "sidebarAccount");
  if (account && destination && account.parentElement !== destination) destination.appendChild(account);
  const dock = $("onboardingAccount");
  if (dock) dock.hidden = !onboarding;
}

async function loadFirstRun() {
  let s;
  try {
    s = await get("/api/onboarding", { timeout: 30000 });
  } catch {
    return; // never let this be the reason the page fails to render
  }
  paintFirstRun(s);
}

function paintFirstRun(s) {
  const first = document.getElementById("firstRun");
  const board = document.getElementById("shell");
  if (!first) return;
  if (board) board.hidden = false;
  placeAccount(false);
  if (firstRunPoll) { clearTimeout(firstRunPoll); firstRunPoll = null; }
  first.hidden = !s?.first_run;
  first.innerHTML = s?.first_run ? `<section class="card fr-card">
    <h2>Connect your cost data</h2>
    <p>Review your selected subscriptions, 90 days of history and automatic updates in one place.
      Existing costs stay available while Azure prepares complete reports.</p>
    <button type="button" id="frScan" class="primary">Review cost-data setup</button>
    </section>` : "";
  const open = document.getElementById("frScan");
  if (open) open.onclick = () => {
    first.hidden = true;
    window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "settings" }));
  };
}

function paintLegacyFirstRun(s) {
  const host = document.getElementById("firstRun");
  const shell = document.getElementById("shell");
  if (!host) return;

  // Nothing to say once there is data, or once the question has been answered. Restores the
  // board in case a scan just finished under a card that was covering it.
  if (!s || (!s.first_run && !s.running)) {    host.hidden = true;
    host.innerHTML = "";
    if (shell) shell.hidden = false;
    placeAccount(false);
    if (firstRunPoll) { clearTimeout(firstRunPoll); firstRunPoll = null; }
    return;
  }

  const steps = s.progress?.steps || [];
  const running = !!s.running;
  // Somebody else's ingest — the boot-time one, or a colleague's refresh. The card waits it
  // out rather than offering to do the same job again.
  const waiting = !!s.ingesting;

  // The board is hidden while this is up. A row of zeroes behind an offer to go and find the
  // numbers invites reading the zeroes as the answer.
  if (shell) shell.hidden = true;
  placeAccount(true);
  host.hidden = false;

  const icon = { done: "✓", running: "…", failed: "✕", skipped: "–" };
  const stepList = steps.length
    ? `<ol class="fr-steps">${steps
        .map(
          (st) => `<li class="fr-step ${esc(st.status)}">
            <span class="fr-mark" aria-hidden="true">${icon[st.status] || "·"}</span>
            <span><strong>${esc(st.name)}</strong>${
              st.detail ? ` — ${esc(st.detail)}` : ""
            }</span></li>`
        )
        .join("")}</ol>`
    : "";

  if (running) {
    host.innerHTML = `<section class="card fr-card">
        <h2>Setting up your cost data</h2>
        <p class="muted">This runs against your own Azure access. Cost reports take a few
          minutes to generate — you can leave this page open.</p>
        ${stepList}
      </section>`;
  } else if (waiting) {
    host.innerHTML = `<section class="card fr-card">
        <h2>Loading your cost data</h2>
        <p class="muted">${esc(s.blocked || "This takes a few minutes.")}</p>
        ${stepList}
      </section>`;
  } else if (s.blocked) {
    host.innerHTML = `<section class="card fr-card">
        <h2>No cost data yet</h2>
        <p class="muted">${esc(s.blocked)}</p>
        ${stepList}
      </section>`;
  } else if (s.can_scan) {
    // Named for what it does to their Azure, not for what it does to this page. "Scan" alone
    // sounds read-only, and this may create an export.
    host.innerHTML = `<section class="card fr-card">
        <h2>Welcome — there is no cost data loaded yet</h2>
        <p>CloudLens can set itself up against the
          ${s.subscriptions} subscription${s.subscriptions === 1 ? "" : "s"} your Azure
          account can see. It will look for FOCUS exports, create a daily FOCUS schedule
          if none are readable, and use Quick estimates where reports are not available.</p>
        <p class="muted">Everything runs with your own access, and nothing is deleted or
          changed in your estate beyond adding a cost export and its managed-identity storage
          access. New reports use the dedicated FOCUS container; all runs are retained and
          storage charges grow. This is offered once.</p>
        <p class="muted">Destination: ${esc(s.destination?.account_id || "not configured")} /
          ${esc(s.destination?.container || "focus-exports")} /
          ${esc(s.destination?.folder || "focus/subscription-id")} (one prefix per subscription).
          Daily coverage is current month to date, not a single day.</p>
        ${stepList}
        <div class="fr-actions">
          <button type="button" id="frScan" class="primary">Set up my cost data</button>
          <button type="button" id="frSkip" class="ghost">Not now</button>
        </div>
        <p class="muted note">Choosing "not now" hides this for good — you can still load data
          any time from Refresh, or the Cost exports tab.</p>
      </section>`;
  } else {
    host.innerHTML = `<section class="card fr-card">
        <h2>No cost data yet</h2>
        <p class="muted">Nothing has been loaded into this deployment.</p>
        ${stepList}
      </section>`;
  }

  const scan = document.getElementById("frScan");
  if (scan) {
    scan.onclick = async () => {
      if (!window.confirm(`Set up daily FOCUS reports for your accessible subscriptions?\nDestination: ${s.destination?.account_id}/${s.destination?.container}/${s.destination?.folder} (per subscription)\nCoverage: current month to date.\nAll runs are retained, with growing storage costs. Azure exports and storage role assignments may be created. Quick estimates fill missing report days; they are not full billing evidence.`)) return;
      scan.disabled = true;
      scan.textContent = "Starting…";
      try {
        await post("/api/onboarding/scan");
      } catch (err) {
        scan.disabled = false;
        scan.textContent = "Set up my cost data";
        const host2 = document.getElementById("firstRun");
        host2.insertAdjacentHTML(
          "beforeend",
          `<div class="banner warn">${esc(err?.message || "Could not start setup.")}</div>`
        );
        return;
      }
      loadFirstRun();
    };
  }

  const skip = document.getElementById("frSkip");
  if (skip) {
    skip.onclick = async () => {
      skip.disabled = true;
      try {
        await post("/api/onboarding/dismiss");
      } catch {
        skip.disabled = false;
        return;
      }
      paintFirstRun(null);
    };
  }

  // Poll while anything is in flight — this card's scan, or an ingest it is waiting on. Stops
  // as soon as it settles, so an idle page costs nothing — and reloads the rest of the
  // dashboard once the data is actually there.
  if ((running || waiting) && !firstRunPoll) {
    firstRunPoll = setTimeout(async () => {
      firstRunPoll = null;
      const before = !!document.getElementById("firstRun")?.hidden;
      await loadFirstRun();
      const after = !!document.getElementById("firstRun")?.hidden;
      // The card came down: the page was drawn against an empty warehouse, so everything on
      // it is now stale.
      if (!before && after) {
        loadSubscriptions().then(() => loadGlance());
        loadWarehouse();
        window.dispatchEvent(new CustomEvent("cost:data-loaded"));
      }
    }, 5000);
  }
}

/** The caption under the heading. One writer, so a later render can't clobber it. */
function describeScope(all, label) {
  const intro = $("introScope");
  if (!intro) return;
  if (!all) {
    intro.textContent = `Scoped to ${label}. Read-only.`;
    return;
  }
  // An SSO sign-in is already limited to their own access, so "every subscription" would
  // overstate it — say how many they actually have.
  if (identity?.user?.source === "entra") {
    const n = identity.subscriptions ?? 0;
    intro.textContent =
      `Covers the ${n} subscription${n === 1 ? "" : "s"} your Azure access allows. Read-only.`;
  } else {
    intro.textContent = "Covers every subscription your signed-in account can see. Read-only.";
  }
}

/**
 * Who is signed in, fetched at most once.
 *
 * Two modules need this — the header chip and the admin-only refresh control — and both used
 * to ask for it independently, which cost a duplicate request on every load. The promise is
 * shared rather than the result, so a second caller arriving mid-flight waits on the first
 * request instead of starting a second.
 */
let identityPromise = null;
export function whoAmI() {
  identityPromise = identityPromise || get("/api/auth/me", { timeout: 30000 });
  return identityPromise;
}

/** Show who is signed in. `/api/auth/me` is public, so this never bounces on its own. */
async function loadIdentity() {
  try {
    // Resolving someone's subscriptions means asking Azure about their role assignments, which
    // is several calls on a cold session — hence a longer deadline than the other boot requests.
    const me = await whoAmI();
    identity = me;
    if (!me.required) return; // auth switched off — no chip, nothing to sign out of
    if (!me.authenticated) return toLogin();
    const name = me.user.name || me.user.email;
    const email = me.user.email || "";
    const words = name.split("@")[0].trim().split(/\s+/).filter(Boolean);
    const initials = words.length > 1 ? words.slice(0, 2).map(word => Array.from(word)[0]).join("")
      : Array.from(words[0] || "?").slice(0, 2).join("");
    $("whoName").textContent = name;
    $("whoName").title = email && email !== name ? `${name} (${email})` : name;
    $("whoAvatar").textContent = initials.toUpperCase();
    $("whoEmail").textContent = email;
    $("whoEmail").hidden = !email || email === name;
    $("whoEmail").title = email;
    $("whoAdmin").hidden = !me.user.admin;
    $("who").setAttribute("aria-label", `Signed in as ${name}${email && email !== name ? ` (${email})` : ""}${me.user.admin ? ", admin" : ""}`);
    $("accountTrigger").setAttribute("aria-label", `Account options for ${name}`);
    $("who").hidden = false;

    if (me.user.source === "entra") {
      describeScope(isAllSelected(), "");
      if (me.problem) banner(me.problem);
      else if ((me.subscriptions ?? 0) === 0) {
        banner(
          "Your account can't see any Azure subscription here, so there is nothing to report. " +
            "Ask a subscription owner for the Cost Management Reader role."
        );
      }
    }
  } catch {
    /* the other loaders will surface a connection problem */
  }
}

function wireAccount() {
  const account = $("who");
  document.addEventListener("click", event => {
    if (account.open && !account.contains(event.target)) account.open = false;
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && account.open) {
      const hadFocus = account.contains(document.activeElement);
      account.open = false;
      if (hadFocus) $("accountTrigger").focus();
    }
  });
}

/** A page-level notice, for things that aren't tied to one answer. */
function banner(text) {
  const el = document.createElement("div");
  el.className = "banner warn";
  el.textContent = text;
  chat.prepend(el);
}

async function loadHealth() {
  try {
    const h = await get("/api/health");
    const ok = h.status === "ok";
    $("dot").className = `dot ${ok ? "ok" : "err"}`;
    $("dot").setAttribute("aria-label", ok ? `Connected, model ${h.model}` : "Service degraded");
    $("dot").title = ok ? `model ${h.model}` : h.detail || "degraded";
  } catch {
    $("dot").className = "dot err";
    $("dot").setAttribute("aria-label", "API unreachable");
    $("dot").title = "API unreachable";
  }
}

let glanceGeneration = 0;
let glanceController = null;
const headerContext = () => `${scopeIds().slice().sort().join(",")}|${readStore("costCurrency", "")}`;
const headerSnapshots = createViewRevalidation({
  request: get, isCurrent: context => context === headerContext(),
  statusHost: () => $("headerFreshness"), onRefresh: async () => {
    if (!await refreshHeader()) throw new Error("Header refresh did not complete.");
  },
});
async function headerGet(url, options) {
  const context = headerContext();
  headerSnapshots.activate(context);
  const data = await headerSnapshots.read(url, options);
  if (context === headerContext()) headerSnapshots.observe(data, context);
  return data;
}
async function loadGlance(attempt = 0) {
  const generation = ++glanceGeneration;
  glanceController?.abort();
  const requestController = glanceController = new AbortController();
  const tiles = $("glance").children;
  for (const index of [0, 1, 3, 4, 5]) {
    linkTile(tiles[index], glanceDestination(index));
  }
  tiles[2].classList.add("tile-link");
  tiles[2].title = "Open the Azure forecast details";
  tiles[2].onclick = () => showForecast(null);
  const scope = scopeIds();
  const want = readStore("costCurrency", "");
  const current = () => generation === glanceGeneration
    && scope.join(",") === scopeIds().join(",") && want === readStore("costCurrency", "");
  const qs = "?" + [
    scope.length ? `scope=${encodeURIComponent(scope.join(","))}` : "",
    want ? `currency=${encodeURIComponent(want)}` : "",
  ].filter(Boolean).join("&");
  try {
    const o = await headerGet(`/api/overview${qs}`, { timeout: 30000, signal: requestController.signal });
    if (!current()) return false;

    // The unscoped snapshot is built on a background timer; poll gently until it lands.
    if (o.status === "loading") {
      if (attempt < 40) setTimeout(() => { if (current()) loadGlance(attempt + 1); }, 3000);
      return false;
    }
    if (o.status === "error") throw new Error(o.detail);

    const cur = o.currency;
    tiles[2].onclick = () => showForecast(o);
    // A month the data does not cover is not a month that cost nothing. The warehouse holds a
    // fixed window, and summing a month outside it returns 0.0 — which the header then stated
    // as "$0" in the largest type on the page. Null now means "no answer", and the tile says
    // which, because "—" with no reason is indistinguishable from a bug.
    if (o.month_to_date == null) {
      setTile(tiles[0], "Month to date", "—", o.stale_note || "not loaded yet", "warn");
    } else {
      setKpi(tiles[0], {
        key: "Month to date",
        value: money(o.month_to_date, cur),
        sub: o.stale_note ? "from Azure, not the local data" : monthLabel(0),
        spark: glanceSpark,
      });
    }
    if (o.last_month == null) {
      setTile(tiles[1], "Last month", "—", "not loaded yet", "warn");
    } else {
      setKpi(tiles[1], { key: "Last month", value: money(o.last_month, cur), sub: monthLabel(-1) });
    }
    setKpi(tiles[2], {
      key: "Forecast 30d",
      value: money(o.forecast_30d, cur),
      sub: "next 30 days, from Azure",
    });

    // Month on month, paced.
    //
    // The obvious comparison — this month's total against last month's — is wrong for most of
    // the month, and wrong in a way that always flatters: on the 3rd it reports a 92% saving
    // that is really just 27 days that have not happened yet. So the elapsed part of this month
    // is scaled to a full month before being compared, and the tile says it is a projection.
    // Only from the second day, because one day extrapolated thirty times is noise with a
    // decimal point on it.
    const now = new Date();
    const elapsed = now.getDate();
    const inMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    if (o.month_to_date != null && o.last_month) {
      if (elapsed < 2) {
        setKpi(tiles[3], { key: "Month on month", value: "—", sub: "too early in the month" });
      } else {
        const paced = (o.month_to_date / elapsed) * inMonth;
        const pct = ((paced - o.last_month) / o.last_month) * 100;
        // Under 2% either way is not a change, it is the shape of the month. Naming it as one
        // sends people looking for a cause that is not there.
        const mood = Math.abs(pct) < 2 ? "flat" : pct > 0 ? "bad" : "good";
        setKpi(tiles[3], {
          key: "Month on month",
          value: `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`,
          tone: mood === "bad" ? "warn" : "",
          delta: { text: money(Math.abs(paced - o.last_month), cur), up: pct > 0, mood },
          sub: `${money(paced, cur)} projected · ${elapsed}/${inMonth} days`,
        });
      }
    } else {
      setKpi(tiles[3], { key: "Month on month", value: "—", sub: "needs both months" });
    }

    const budget = budgetPresentation(o);
    setKpi(tiles[4], {
      key: "Budget", value: budget.value, tone: budget.tone, sub: budget.sub,
      ...(budget.percent == null ? {} : {
        bar: { pct: budget.percent, tone: o.budget.status === "over" ? "bad" : budget.tone,
          label: `${budget.percent}% of budget used` },
      }),
    });
    linkTile(tiles[4], "budgets");
    return scheduleSavingsTile(cur);
  } catch (err) {
    if (!current() || requestController.signal.aborted) return false;
    for (const t of tiles) t.classList.remove("loading");
    // Being told "you can see nothing" is a settled answer, not a hiccup: retrying it wastes
    // two round trips and still ends with blank tiles. Say so once and stop.
    if (err && err.forbidden) {
      savingsScheduled?.scan?.stop();
      savingsScheduled?.finish?.(false);
      if (savingsScheduled?.run) window.removeEventListener("cl:dashboard-ready", savingsScheduled.run);
      clearTimeout(savingsScheduled?.timer);
      savingsScheduled = null;
      setTile(tiles[0], "Month to date", "—", "no Azure access");
      setTile(tiles[1], "Last month", "—");
      setTile(tiles[2], "Forecast 30d", "—");
      setTile(tiles[3], "Month on month", "—");
      setTile(tiles[4], "Budget", "—");
      setTile(tiles[5], "Savings found", "—");
      return false;
    }
    // One quiet retry — these calls can be throttled by Azure.
    if (attempt < 2) setTimeout(() => { if (current()) loadGlance(attempt + 1); }, 8000);
    return false;
  }
}

/** The month a figure covers, named. "Month to date" does not say *which* month, and the
 *  difference matters on the 1st. */
function monthLabel(offset) {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + offset);
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

/** Daily spend behind the month-to-date tile, handed over by the Overview tab once its own
 *  chart has the series. Kept here so the tile can be repainted without refetching it. */
let glanceSpark = null;

/** Called by the dashboard when the trend lands. Repaints only if the tile is already drawn,
 *  so a slow trend never blanks a figure that is on screen. */
export function setGlanceSpark(values) {
  glanceSpark = Array.isArray(values) && values.length >= 3 ? values.slice(-30) : null;
  const tile = $("glance")?.children[0];
  if (glanceSpark && tile && !tile.classList.contains("loading")
      && !tile.querySelector(".spark")) {
    tile.insertAdjacentHTML("beforeend", sparkline(glanceSpark));
  }
}

/**
 * What the estate could stop paying for.
 *
 * Its own request because it is the slow one — waste rules query Azure Resource Graph across
 * every subscription — and the other five figures should not wait behind it. The tile says
 * "checking" rather than "—" while it runs, so a slow answer does not read as no answer.
 */
function loadSavingsTile(cur, request) {
  if (!request.current()) { request.finish?.(false); return; }
  const tile = $("glance").children[5];
  if (!request.preserve) setTile(tile, "Savings found", "checking…");
  tile.setAttribute("aria-busy", "true");
  linkTile(tile, "savings");
  request.scan = createSavingsScan({
    request: ({ signal }) => headerGet(`/api/dashboard/savings?${request.query}`, { timeout: 120000, signal }),
    isCurrent: request.current,
    onData: s => {
      const pending = s.scan?.status === "running" || Boolean(s.freshness?.refreshing);
      const failed = s.scan?.status === "error" || s.scan?.status === "unavailable";
      const unavailable = failed || s.partial || s.unpriced_count
        || s.unavailable?.length || Object.keys(s.scan?.errors || {}).length;
      tile.setAttribute("aria-busy", String(pending));
      if (!s.count) {
        if (!((pending || unavailable) && request.preserve)) setTile(tile, "Savings found", pending ? "checking…" : "—",
          pending ? "scan in progress" : unavailable ? "assessment unavailable" : "none identified");
      } else {
        setKpi(tile, {
          key: "Savings found",
          value: Number.isFinite(s.total) ? money(s.total, s.currency || cur) : "Not available",
          tone: Number.isFinite(s.total) ? "good" : "warn",
          sub: `${s.count} finding${s.count === 1 ? "" : "s"} · last ${s.days || 30} days${pending || unavailable ? " · partial" : ""}`,
        });
      }
      if (!pending && !failed) request.preserve = true;
      linkTile(tile, "savings");
      tile.title = failed && request.preserve ? "Assessment unavailable. Previous result shown."
        : s.unpriced_count ? "Some saving estimates are unavailable. Partial assessment shown."
        : unavailable ? "Some sources are unavailable. Partial assessment shown." : "";
      // A completed partial assessment is freshly painted, not a failed refresh.
      if (!pending) request.finish?.(!failed);
    },
    onError: () => {
      tile.setAttribute("aria-busy", "false");
      if (!request.preserve) setTile(tile, "Savings found", "—", "could not refresh");
      linkTile(tile, "savings");
      tile.title = "Could not refresh savings." + (request.preserve ? " Previous result shown." : "");
      request.finish?.(false);
    },
  });
  void request.scan.start(request.key);
}

let savingsScheduled = null;
const savingsContext = () => `${scopeIds().slice().sort().join(",")}|${readStore("costCurrency", "")}`;
function scheduleSavingsTile(cur) {
  const key = savingsContext();
  const previous = savingsScheduled;
  if (previous?.key === key && !previous.refresh) return previous.painted;
  previous?.scan?.stop();
  previous?.finish?.(false);
  if (previous?.run) window.removeEventListener("cl:dashboard-ready", previous.run);
  clearTimeout(previous?.timer);
  const query = new URLSearchParams({ days: "30" });
  if (scopeIds().length) query.set("scope", scopeIds().join(","));
  const currency = readStore("costCurrency", "");
  if (currency) query.set("currency", currency);
  const request = savingsScheduled = { key, query, preserve: previous?.key === key && previous.preserve };
  request.painted = new Promise(resolve => { request.finish = resolve; });
  request.current = () => savingsScheduled === request && savingsContext() === key;
  const tile = $("glance").children[5];
  if (!request.preserve) setTile(tile, "Savings found", "waiting…", "loads after the dashboard");
  linkTile(tile, "savings");
  const run = () => {
    if (request.started || !request.current()) return;
    request.started = true;
    clearTimeout(request.timer);
    window.removeEventListener("cl:dashboard-ready", run);
    const start = () => loadSavingsTile(cur, request);
    if ("requestIdleCallback" in window) requestIdleCallback(start, { timeout: 3000 });
    else setTimeout(start, 0);
  };
  request.run = run;
  if (document.documentElement.dataset.dashboardReady === "true") run();
  else {
    window.addEventListener("cl:dashboard-ready", run, { once: true });
    request.timer = setTimeout(run, 3000);
  }
  return request.painted;
}

async function loadWarehouse(attempt = 0) {
  // A strip under the summary rather than a tile inside it. What the warehouse holds is a fact
  // about this deployment, not about anyone's spend — worth stating, not worth a sixth of the
  // most valuable band on the page.
  const el = $("dataState");
  if (!el) return;
  const show = (html) => { el.hidden = false; el.innerHTML = html; };
  try {
    const w = await get("/api/warehouse");
    const ing = w.ingest || {};
    if (ing.status === "running") {
      show(`<span class="ds-dot busy"></span><span>Loading cost data — `
         + `<strong>${ing.done || 0}</strong> of ${ing.total || 0} periods</span>`);
      if (attempt < 300) setTimeout(() => loadWarehouse(attempt + 1), 5000);
      return;
    }
    if (!w.rows) {
      return show('<span class="ds-dot warn"></span><span>No local data — '
        + 'reading the live Azure API, which is slower and carries no tags</span>');
    }
    show(`<span class="ds-dot ok"></span><span>Local data `
       + `<strong>${(w.rows / 1000).toFixed(1)}k rows</strong> · ${esc(w.from)} → ${esc(w.to)}</span>`);
  } catch {
    el.hidden = true;
  }
}

/**
 * One tile in the estate strip.
 *
 * `tone` is "", "warn" or "bad" rather than a boolean, because a budget at 105% and one at 85%
 * are different answers and colouring both amber loses the only distinction that matters.
 */
function setTile(tile, key, value, sub, tone) {
  tile.classList.remove("loading");
  const cls = tone === true ? "warn" : tone || "";
  tile.innerHTML =
    `<span class="k">${esc(key)}</span>` +
    `<span class="v${cls ? ` ${cls}` : ""}">${esc(value)}</span>` +
    (sub ? `<span class="sub">${esc(sub)}</span>` : "");
}

/** Native buttons keep their single navigation handler when their figures are repainted. */
function linkTile(tile, tabId) {
  tile.classList.add("tile-link");
  tile.onclick = () => window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: tabId }));
}

function showForecast(overview) {
  const dialog = document.createElement("dialog");
  dialog.className = "forecast-dialog";
  dialog.setAttribute("aria-labelledby", "forecastTitle");
  const available = Number.isFinite(overview?.forecast_30d);
  dialog.innerHTML = `<h2 id="forecastTitle">Forecast for the next 30 days</h2>
    <p class="forecast-value">${available ? moneyHtml(overview.forecast_30d, overview.currency, 2) : "Not available"}</p>
    <p>${available ? "Azure Cost Management forecast for the selected subscriptions, not recorded spend or a budget limit."
      : "The Azure forecast has not loaded. Try again after the summary finishes loading."}</p>
    <p class="muted">This estimate retains its own next-30-day window. Dashboard period and dimension filters do not change it. No daily forecast breakdown is supplied.</p>
    <div class="forecast-actions"><button type="button" class="ghost" data-forecast-budgets>Open budgets</button>
      <button type="button" class="primary" data-forecast-close autofocus>Close</button></div>`;
  dialog.querySelector("[data-forecast-close]").onclick = () => dialog.close();
  dialog.querySelector("[data-forecast-budgets]").onclick = () => {
    dialog.close();
    window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: "budgets" }));
  };
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.appendChild(dialog);
  dialog.showModal();
}

/**
 * A tile carrying a comparison as well as a figure.
 *
 * `setTile` states a number. This states what the number is doing, which is the part anyone
 * reads a dashboard for: $130 is meaningless until you know whether it is ahead of or behind
 * the month it is being compared with.
 *
 * `delta.dir` is which way is *good*, not which way the arrow points — spend falling is good,
 * savings found rising is good, and a single "green means up" rule gets one of them wrong.
 */
function setKpi(tile, { key, value, tone, delta, sub, bar, spark }) {
  tile.classList.remove("loading");
  const cls = tone ? ` ${tone}` : "";

  let deltaHtml = "";
  if (delta && delta.text) {
    // Good, bad, or neither. Neutral is a real answer: a 0.4% move is not a story, and
    // colouring it green implies one.
    const mood = delta.mood || "flat";
    const arrow = delta.up == null ? "" : delta.up ? "▲" : "▼";
    deltaHtml = `<span class="d ${mood}">${arrow ? `<span class="arw" aria-hidden="true">${arrow}</span>` : ""}${esc(delta.text)}</span>`;
  }

  let barHtml = "";
  if (bar) {
    // Capped at 100 for the fill so an overspent budget does not draw outside its track, while
    // the figure above still reads the true percentage.
    const pct = Math.max(0, Math.min(100, bar.pct));
    barHtml = `<span class="tbar" role="img" aria-label="${esc(bar.label || `${bar.pct}%`)}">`
      + `<span class="tbar-fill${bar.tone ? ` ${bar.tone}` : ""}" style="width:${pct}%"></span></span>`;
  }

  tile.innerHTML =
    `<span class="k">${esc(key)}</span>`
    + `<span class="vrow"><span class="v${cls}">${esc(value)}</span>${deltaHtml}</span>`
    + (spark ? sparkline(spark) : "")
    + barHtml
    + (sub ? `<span class="sub">${esc(sub)}</span>` : "");
}

/**
 * A sparkline, as inline SVG.
 *
 * Not a chart library: this is one path and a `preserveAspectRatio` of none, so it stretches to
 * whatever width the tile happens to have without measuring anything or redrawing on resize.
 * A tile is too small for axes, a legend or a tooltip, and adding them is how a sparkline stops
 * being a sparkline.
 */
function sparkline(values) {
  const pts = (values || []).filter((v) => Number.isFinite(v));
  if (pts.length < 3) return "";                 // two points is a line segment, not a shape
  const lo = Math.min(...pts);
  const hi = Math.max(...pts);
  const span = hi - lo || 1;                     // a flat series still draws, down the middle
  const W = 100;
  const H = 24;
  const step = W / (pts.length - 1);
  const d = pts
    .map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(2)} ${(H - ((v - lo) / span) * (H - 4) - 2).toFixed(2)}`)
    .join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
       aria-hidden="true" focusable="false"><path d="${d}" fill="none"
       stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
       stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`;
}

// ------------------------------------------------------- subscription picker
async function loadSubscriptions() {
  let backfill = null;
  try {
    const r = await get("/api/subscriptions", { timeout: 30000 });
    allSubs = r.subscriptions || [];
    backfill = r.backfill || null;
  } catch {
    allSubs = [];
  }
  const known = new Set(allSubs.map((s) => s.id));
  for (const id of [...selected]) if (!known.has(id)) selected.delete(id);
  renderPicker();
  paintBackfill(backfill);
  scopeChanged();
  window.dispatchEvent(new Event("cl:subscriptions-ready"));
}

// While a backfill is running the warehouse gains rows the page has already drawn without, so
// it is re-checked until it settles. Cheap: one small request, and only while something is
// actually outstanding.
let backfillPoll = null;

/**
 * Say which of this person's subscriptions are still loading, and which could not be loaded.
 *
 * A dashboard that is simply missing a subscription looks identical to one whose spend is
 * genuinely zero, and the difference matters enormously — the first is "wait", the second is
 * "you are done". Neither is safe to infer from silence.
 */
function paintBackfill(state) {
  const host = document.getElementById("backfill");
  if (!host) return;

  const pending = state?.pending || [];
  const failed = state?.failed || [];
  if (!pending.length && !failed.length) {
    host.hidden = true;
    host.innerHTML = "";
    if (backfillPoll) { clearTimeout(backfillPoll); backfillPoll = null; }
    return;
  }

  const name = (s) => esc(s.name || s.id);
  const parts = [];
  if (pending.length) {
    parts.push(`<div class="banner info">
      <strong>Loading ${pending.length} subscription${pending.length === 1 ? "" : "s"}</strong>
      you can see but this dashboard had not pulled yet:
      ${pending.map(name).join(", ")}. Cost reports take a few minutes; the numbers below
      exclude them until they land.</div>`);
  }
  if (failed.length) {
    parts.push(`<div class="banner warn">
      <strong>Could not load ${failed.length} subscription${failed.length === 1 ? "" : "s"}</strong>
      ${failed.map((f) => `${name(f)}${f.detail ? ` — ${esc(f.detail)}` : ""}`).join("; ")}.
      Cost is read with this app's identity, so a subscription you can see but it cannot needs
      the app's managed identity granted access. Totals below exclude these.</div>`);
  }

  host.innerHTML = parts.join("");
  host.hidden = false;

  if (pending.length && !backfillPoll) {
    backfillPoll = setTimeout(() => {
      backfillPoll = null;
      loadSubscriptions().then(() => loadGlance());
    }, 30000);
  }
}

export function scopeIds() {
  // All (or none) selected means "everything" — send an empty scope so the server can serve the
  // fast unscoped snapshot instead of recomputing an identical filtered view.
  return selected.size === 0 || selected.size === allSubs.length ? [] : [...selected];
}

/**
 * Every subscription this person can pick, whatever the current scope.
 *
 * The picker filters by *what to analyse*; anything that needs to name a subscription — the
 * budget form, principally — needs the full list and its ids, because the tag data carries
 * subscription names only. A copy, so a caller cannot reorder the picker's own array.
 */
export function allSubscriptions() {
  return allSubs.map((s) => ({ ...s }));
}

export function selectSubscriptions(ids, { notify = true } = {}) {
  if (!ids.length || ids.some(id => !allSubs.some(sub => sub.id === id))) return false;
  selected = new Set(ids);
  writeStore("costScope", [...selected]);
  renderPicker();
  loadGlance();
  if (notify) scopeChanged();
  else lastScopeKey = [...scopeIds()].sort().join(",");
  return true;
}

// Anything else that depends on the chosen subscriptions — the dashboard, principally — has to
// redraw when the picker changes. A subscription list rather than a direct call keeps app.js
// unaware of what else is on the page.
const scopeListeners = [];
let lastScopeKey = [...scopeIds()].sort().join(",");

export function onScopeChange(fn) {
  scopeListeners.push(fn);
}

function scopeChanged() {
  const next = [...scopeIds()].sort().join(",");
  if (next === lastScopeKey) return;
  lastScopeKey = next;
  for (const fn of scopeListeners) {
    try {
      fn(scopeIds());
    } catch {
      /* one bad listener must not stop the others */
    }
  }
}

function isAllSelected() {
  return selected.size === 0 || selected.size === allSubs.length;
}

function toggleSub(id) {
  if (selected.size === 0) allSubs.forEach((x) => selected.add(x.id)); // "all" -> explicit set
  selected.has(id) ? selected.delete(id) : selected.add(id);
  if (selected.size === 0) allSubs.forEach((x) => selected.add(x.id)); // never end up with none
  writeStore("costScope", [...selected]);
  renderPicker();
  loadGlance();
  scopeChanged();
}

function renderPicker() {
  const list = $("pickerList");
  const focused = list.contains?.(document.activeElement) ? document.activeElement?.dataset?.subscriptionId : null;
  list.innerHTML = "";

  if (!allSubs.length) {
    list.innerHTML = `<p class="muted" style="padding:10px;font-size:13px">No subscriptions found.</p>`;
  }

  allSubs.forEach((s) => {
    const on = selected.size === 0 || selected.has(s.id);
    const b = document.createElement("button");
    b.className = "pick" + (s.in_warehouse ? "" : " missing");
    b.type = "button";
    b.setAttribute("role", "option");
    b.dataset.subscriptionId = s.id;
    b.setAttribute("aria-checked", String(on));
    b.setAttribute("aria-selected", String(on));
    b.title = s.in_warehouse ? s.id : `${s.id} — no local data yet`;
    b.innerHTML =
      `<span class="box" aria-hidden="true">${on ? "✓" : ""}</span>` +
      `<span class="nm">${esc(s.name || s.id)}</span>` +
      `<span class="amt">${s.cost != null ? money(s.cost) : "—"}</span>`;
    b.onclick = () => toggleSub(s.id);
    list.appendChild(b);
  });

  const all = isAllSelected();
  const n = selected.size;
  const label = all
    ? `All subscriptions${allSubs.length ? ` (${allSubs.length})` : ""}`
    : n === 1
      ? allSubs.find((s) => selected.has(s.id))?.name || "1 subscription"
      : `${n} of ${allSubs.length} subscriptions`;

  $("pickerLabel").textContent = label;
  $("pickerBtn").setAttribute("aria-label", `Subscriptions to analyse: ${label}`);
  $("pickerBtn").classList.toggle("narrowed", !all);
  $("pickAll").textContent = all ? "Clear" : "Select all";

  describeScope(all, label);
  if (focused) [...list.querySelectorAll(".pick")].find(node => node.dataset.subscriptionId === focused)?.focus();
}

function openPicker(open) {
  const menu = $("pickerMenu");
  const btn = $("pickerBtn");
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) menu.querySelector(".pick")?.focus();
}

// -------------------------------------------------------------------- wiring
/** Re-read the header figures. Exported so the refresh control can update them when an
    ingest finishes, rather than leaving yesterday's numbers above fresh tabs. */
export function refreshHeader() {
  if (savingsScheduled) savingsScheduled.refresh = true;
  loadWarehouse();
  return loadGlance();
}

// ------------------------------------------------------------------- theme
// Order is the click cycle: dark -> light -> vivid -> dark. "vivid" is the internal name for
// the third, saturated-teal option offered in the UI as "Colourful" — never reached by
// following the system's own light/dark preference, only by asking for it explicitly, so a
// browser or OS switching modes underneath a page never surprises someone into it.
const THEMES = ["dark", "light", "vivid", "paper"];
const THEME_TITLE = {
  dark: "Switch to light", light: "Switch to colourful",
  vivid: "Switch to Kindle E-Ink", paper: "Switch to dark",
};
const THEME_METACOLOR = {
  dark: "#111315", light: "#F4F5F7", vivid: "#0C191C", paper: "#DCD7CC",
};

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return THEMES.includes(t) ? t : "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("costTheme", theme);
  } catch {
    /* private browsing — the choice just won't survive a reload */
  }
  // The meta tag drives the browser's own chrome on mobile; leaving it dark under a light
  // page is the kind of seam that makes a theme feel half-finished.
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_METACOLOR[theme] || THEME_METACOLOR.dark);
  document.getElementById("themeBtn")?.setAttribute("title", THEME_TITLE[theme] || THEME_TITLE.dark);
  scheduleRetint();
}

/**
 * Re-tint the canvases on the next frame rather than inside the click.
 *
 * Everything drawn in CSS changes the instant the `data-theme` attribute above does. The charts
 * are canvases, so they have to be re-coloured in JavaScript — every dataset of every live chart,
 * which measured 94ms of blocking work on the theme button. All of it landed before the browser
 * was allowed to paint the new theme at all, so the whole interface waited on the charts.
 *
 * Deferred by one frame the page repaints immediately and the canvases follow, which is the
 * order someone reading the page would expect anyway. Coalescing matters because the button
 * cycles three themes: clicking through them quickly should re-tint once, at the end, not once
 * per click.
 */
let retintFrame = 0;
function scheduleRetint() {
  if (retintFrame) return;
  retintFrame = requestAnimationFrame(() => {
    retintFrame = 0;
    retintCharts();
  });
}

function wireTheme() {
  const btn = $("themeBtn");
  if (!btn) return;
  applyTheme(currentTheme()); // sync the title and meta with whatever the inline script chose

  btn.onclick = () => {
    const cur = currentTheme();
    applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
  };

  // Follow the system only while the user hasn't expressed a preference of their own. Only
  // ever moves between dark and light — "vivid" and "paper" are choices made once, by clicking,
  // and nothing here should silently move someone out of one because their OS changed
  // appearance. Picking either writes to storage, so the guard below already covers them.
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    try {
      if (localStorage.getItem("costTheme")) return;
    } catch {
      /* fall through and follow the system */
    }
    document.documentElement.dataset.theme = e.matches ? "light" : "dark";
    retintCharts();
  });
}

function wire() {
  watchScroll();

  const menu = $("pickerMenu");
  const btn = $("pickerBtn");

  btn.onclick = (e) => {
    e.stopPropagation();
    openPicker(menu.hidden);
  };
  menu.onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => openPicker(false));

  // Roving keyboard navigation inside the picker.
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll(".pick")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = e.key === "ArrowDown" ? i + 1 : i - 1;
      items[(next + items.length) % items.length]?.focus();
    } else if (e.key === "Escape") {
      openPicker(false);
      btn.focus();
    } else if (e.key === "Tab") {
      openPicker(false);
    }
  });

  $("pickAll").onclick = () => {
    selected = isAllSelected() ? new Set([allSubs[0]?.id].filter(Boolean)) : new Set();
    writeStore("costScope", [...selected]);
    renderPicker();
    loadGlance();
    scopeChanged();
  };

  $("form").onsubmit = (e) => {
    e.preventDefault();
    ask($("q").value.trim());
  };

  wireAccount();
  $("signout").onclick = async () => {
    // Clear the remembered scope too: the next person at this browser shouldn't inherit
    // a filter they never chose.
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch {
      /* signing out locally still matters */
    }
    try {
      localStorage.removeItem("costScope");
    } catch {
      /* private browsing */
    }
    location.replace("/login");
  };

  $("reset").onclick = () => {
    conversationGeneration++;
    controller?.abort();
    controller = null;
    busy = false;
    $("send").disabled = false;
    $("stop").hidden = true;
    window.dispatchEvent(new CustomEvent("cl:agent", { detail: "done" }));
    history = [];
    chat.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.innerHTML = introTemplate;
    chat.appendChild(wrap.firstElementChild);
    $("q").focus();
    announce("Conversation cleared.");
  };
  $("stop").onclick = () => controller?.abort();
  $("jump").onclick = () => {
    pinned = true;
    chat.scrollTo({ top: chat.scrollHeight, behavior: "smooth" });
    $("jump").hidden = true;
  };

  chat.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) ask(chip.textContent.trim());
  });

  const q = $("q");
  const autoGrow = () => {
    q.style.height = "auto";
    q.style.height = Math.min(q.scrollHeight, 160) + "px";
  };
  q.addEventListener("input", autoGrow);
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(q.value.trim());
    }
  });
  q.focus();
}

/** Follow new content unless the user has scrolled away.
 *
 *  Auto-follow uses instant scrolling so the position we read back is always the final one;
 *  with smooth scrolling the mid-animation values look like the user scrolling away and the
 *  view detaches itself. Only the explicit Jump button animates.
 */
function watchScroll() {
  const near = () => chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  chat.addEventListener(
    "scroll",
    () => {
      pinned = near();
      $("jump").hidden = pinned;
    },
    { passive: true }
  );
}

function scroll(force = true) {
  if (force) chat.scrollTop = chat.scrollHeight;
}

// --------------------------------------------------------------------- ask
/**
 * Put a question to the agent from outside this module.
 *
 * The analysis view has its own ask bar — a reader who has just been told about an idle VM
 * should not have to find the side panel to ask which one. It calls this rather than filling
 * the textarea and firing a synthetic submit, which would break the moment the form changes.
 */
export function askAgent(text) {
  const q = String(text || "").trim();
  if (!q) return;
  ask(q);
}

async function ask(text) {
  if (!text || busy) return;
  busy = true;
  pinned = true;
  // Start fetching the markdown renderer now rather than when the first token lands: the
  // model takes seconds to think, which is ample time for 57 KB to arrive unnoticed.
  loadMarkdown();
  $("send").disabled = true;
  $("stop").hidden = false;
  document.querySelector(".intro")?.remove();

  const q = $("q");
  q.value = "";
  q.style.height = "auto";

  addYou(text);
  const requestController = controller = new AbortController();
  const generation = conversationGeneration;
  const current = () => generation === conversationGeneration && controller === requestController;
  const view = addAgent(current);
  const started = performance.now();
  announce("Working on your question.");
  // The analyst reflects what the model is doing. Announced as an event rather than by calling
  // into bot.js, so this module keeps knowing nothing about the launcher — app.js is imported
  // *by* the dashboard, and a dependency back the other way would be a cycle.
  window.dispatchEvent(new CustomEvent("cl:agent", { detail: "thinking" }));

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text, history, scope: scopeIds() }),
      signal: requestController.signal,
    });
    if (!current()) return;
    if (res.status === 401) {
      view.note("Your session expired. Taking you back to sign in…");
      setTimeout(() => { if (generation === conversationGeneration) toLogin(); }, 1200);
    } else if (res.status === 403) {
      // Almost always "you have no Azure subscriptions", not "this app forbids you". Saying
      // the latter sends people to ask the wrong person for the wrong thing.
      const body = await res.json().catch(() => ({}));
      view.fail(body.detail || "Your account isn't allowed to do that.", null);
    } else if (!res.ok) {
      view.fail(`Request failed: ${res.status} ${res.statusText}`, () => ask(text));
    } else {
      await consume(res.body, view, current);
    }
  } catch (err) {
    if (err.name === "AbortError") view.note("Stopped.");
    else view.fail(String(err.message || err), () => ask(text));
  } finally {
    if (!current()) return;
    view.finish(performance.now() - started);
    busy = false;
    controller = null;
    $("send").disabled = false;
    $("stop").hidden = true;
    q.focus();
    window.dispatchEvent(new CustomEvent("cl:agent", { detail: "done" }));
  }
}

async function consume(body, view, current = () => true) {
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let speaking = false;
  while (true) {
    const { done, value } = await reader.read();
    if (!current()) {
      await reader.cancel().catch(() => {});
      return;
    }
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop() || "";
    for (const f of frames) {
      let type = "message";
      const lines = [];
      for (const line of f.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7).trim();
        else if (line.startsWith("data: ")) lines.push(line.slice(6));
      }
      if (!lines.length) continue;
      // The moment the answer starts arriving the analyst stops thinking and starts explaining.
      // Fired once: the stream sends hundreds of `delta` frames and re-announcing on each would
      // restart the animation on every one.
      if (!speaking && type === "delta") {
        speaking = true;
        window.dispatchEvent(new CustomEvent("cl:agent", { detail: "explaining" }));
      }
      try {
        view.on(type, JSON.parse(lines.join("\n")));
      } catch {
        /* ignore a malformed frame rather than killing the stream */
      }
    }
  }
}

// ------------------------------------------------------------------ render
function addYou(text) {
  const n = document.createElement("div");
  n.className = "msg you";
  n.innerHTML = `<div class="who">You</div><div class="body"></div>`;
  n.querySelector(".body").textContent = text;
  chat.appendChild(n);
  scroll();
}

function addAgent(isCurrent = () => true) {
  const n = document.createElement("article");
  n.className = "msg agent";
  n.innerHTML = `
    <div class="who">CloudLens</div>
    <div class="steps"></div>
    <div class="charts"></div>
    <div class="live"><div class="thinking"><span class="spin"></span><span>Thinking…</span></div></div>
    <div class="banners"></div>
    <div class="body"></div>
    <div class="tools-row"><button class="copy ghost" hidden>Copy</button><span class="elapsed"></span></div>`;
  chat.appendChild(n);

  const steps = n.querySelector(".steps");
  const charts = n.querySelector(".charts");
  const live = n.querySelector(".live");
  const banners = n.querySelector(".banners");
  const body = n.querySelector(".body");
  const copy = n.querySelector(".copy");
  const elapsed = n.querySelector(".elapsed");

  const open = new Map();
  let md = "";
  let announcedAnswer = false;

  const say = (t) =>
    (live.innerHTML = t
      ? `<div class="thinking"><span class="spin"></span><span>${esc(t)}</span></div>`
      : "");

  return {
    on(type, d) {
      if (!isCurrent()) return;
      if (type === "tool") {
        const label = TOOL_LABEL[d.name] || d.name;
        if (d.status === "running") {
          say(`Reading ${label}…`);
          announce(`Reading ${label}`);
          const s = document.createElement("details");
          s.className = "step";
          s.dataset.id = d.id || d.name;
          s.innerHTML = `
            <summary>
              <span class="spin"></span>
              <span class="tname">${esc(label)}</span>
            </summary>
            <div class="inner">
              <div class="label">Arguments</div><pre>${esc(JSON.stringify(d.arguments, null, 2))}</pre>
              <div class="out"></div>
            </div>`;
          steps.appendChild(s);
          open.set(s.dataset.id, s);
        } else {
          const s = open.get(d.id || d.name);
          if (s) {
            const bad = d.status === "failed";
            s.querySelector("summary").innerHTML =
              `<span class="tick">${bad ? "✕" : "✓"}</span>` +
              `<span class="tname">${esc(label)}</span>` +
              `<span class="ms">${d.ms != null ? (d.ms / 1000).toFixed(1) + "s" : ""}</span>`;
            if (bad) {
              s.classList.add("failed");
              s.open = true;
            }
            s.querySelector(".out").innerHTML =
              `<div class="label">${bad ? "Error" : "Result"}</div>` +
              `<pre>${esc(JSON.stringify(d.result, null, 2))}</pre>`;
            open.delete(d.id || d.name);
          }
          say("Writing the answer…");
        }
        scroll(pinned);
      } else if (type === "chart") {
        say("");
        drawChart(charts, d).catch(() =>
          this.warn("The chart could not be drawn, but the figures above are unaffected.")
        );
        announce(`Chart ready: ${d.title || ""}`);
        scroll(pinned);
      } else if (type === "delta") {
        say("");
        md += d.text || "";
        body.innerHTML = render(md);
        body.classList.add("cursor");
        if (!announcedAnswer) {
          announce("Answer is being written.");
          announcedAnswer = true;
        }
        scroll(pinned);
      } else if (type === "done") {
        history = d.messages || history;
        say("");
      } else if (type === "error") {
        this.fail(d.message);
      }
    },
    note(msg) {
      if (!isCurrent()) return;
      live.innerHTML = `<div class="thinking">${esc(msg)}</div>`;
    },
    warn(msg) {
      if (!isCurrent()) return;
      const b = document.createElement("div");
      b.className = "banner warn";
      b.textContent = msg;
      banners.appendChild(b);
    },
    fail(msg, retry) {
      if (!isCurrent()) return;
      live.innerHTML = "";
      const b = document.createElement("div");
      b.className = "banner err";
      b.textContent = msg;
      if (retry) {
        const again = document.createElement("button");
        again.textContent = "Try again";
        again.onclick = () => {
          b.remove();
          retry();
        };
        b.appendChild(again);
      }
      banners.appendChild(b);
      announce(`Error: ${msg}`);
      scroll(pinned);
    },
    finish(ms) {
      if (!isCurrent()) return;
      say("");
      body.classList.remove("cursor");
      enhance(body);
      if (md) {
        copy.hidden = false;
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(md);
            copy.textContent = "Copied";
            setTimeout(() => (copy.textContent = "Copy"), 1400);
          } catch {
            copy.textContent = "Press Ctrl+C";
          }
        };
        elapsed.textContent = `${(ms / 1000).toFixed(1)}s`;
        announce("Answer complete.");
      }
      scroll(pinned);
    },
  };
}

// ------------------------------------------------------------------ charts
// A single-hue ramp, not a rainbow. Chart libraries default to maximally distinct hues so that
// twelve arbitrary categories stay separable — but these categories are not arbitrary, they are
// *ordered by cost*, and a sequence that steps monotonically encodes that ordering in the colour
// itself. The eye reads "biggest" before it reads any label.
//
// Distinct hues would say each area is a different kind of thing. They are not: they are all
// spend. One hue also keeps the chart from competing with the accent, which is reserved for
// things a person can act on.
//
// The ramp runs in opposite directions per theme, because "prominent" is light on a dark
// surface and dark on a light one. A single fixed ramp would put the largest slice closest to
// the background in one of the two themes, which is exactly backwards.
//
// Both are one blue family — the same hue the mark and the accent use, so a chart and the logo
// above it are the same blue rather than two that nearly match, which is worse than not matching
// at all. The last two entries are neutral, for the long tail of small slices that should
// recede rather than compete.
const RAMP_DARK = [
  "#9FCFFA", "#8AC2F8", "#74B5F7", "#5FA8F6", "#479EF5",
  "#3590E8", "#2886DE", "#1E76C8", "#175FA3", "#124B80",
  "#A19F9D", "#797775",
];
const RAMP_LIGHT = [
  "#004578", "#005A9E", "#106EBE", "#0078D4", "#2B88D8",
  "#4FA0DD", "#7BB8E5", "#A6D0EE", "#CCE4F6", "#E5F1FB",
  "#605E5C", "#A19F9D",
];
// Colourful is a single dark surface (there is no light-mode "colourful" to invert into), so it
// needs only one ramp — brightest first, in the theme's own teal rather than Fluent blue, ending
// in the same two neutral greys every ramp reserves for a tail nobody is meant to read closely.
const RAMP_VIVID = [
  "#96F3DC", "#7CEAD0", "#62E0C3", "#48D6B6", "#2ECCA9",
  "#20BA97", "#1AA485", "#158E73", "#107861", "#0C6250",
  "#93AFA8", "#6E8983",
];
// Kindle E-Ink has one axis to separate series on, and far less of it than the themes above
// think they have: an ink has to clear 3:1 against the page to read as a mark at all, which on
// a substrate this light confines every usable value to the darker third of the range. So this
// ramp is compressed rather than spread — darkest first, like the light theme, ending well
// short of the page instead of fading into it. Adjacent steps are close by necessity; the
// point styles below are what actually tell two series apart here, which is the same order of
// priority the comment on CATEGORICAL_DARK already sets out. The last two are the neutral tail
// every ramp reserves, placed inside the same band.
const RAMP_PAPER = [
  "#1E180D", "#282217", "#373126", "#453F34", "#534D42",
  "#5F594E", "#6C665B", "#787267", "#837D72", "#8F897E",
  "#615B50", "#827C71",
];
const RAMP_BY_THEME = { dark: RAMP_DARK, light: RAMP_LIGHT, vivid: RAMP_VIVID, paper: RAMP_PAPER };
const palette = () => RAMP_BY_THEME[currentTheme()] || RAMP_DARK;

// The ordered ramp above is right for slices ranked by cost and wrong for named categories.
// "On demand", "Committed" and "Spot" are not more and less of one thing, they are three
// different things — and rendered from a single-hue ramp they came out as three barely
// distinguishable blues, which is a legend you have to decode rather than read.
//
// Hue order is chosen for colour vision deficiency, not just for contrast against each other:
// blue and amber are the one pair that stays separable under every common form of CVD, so they
// take the first two slots. Green and cyan follow, then a violet and a rose that lean toward
// blue rather than toward red — the safer side of the wheel for red-green deficiency, and far
// enough from amber/green in both hue and lightness to still read as their own thing. Eight
// slots, not four: the cost-area taxonomy ("Compute & Web" through "Other services") is a fixed
// seven-way split that recurs across the dashboard, and a palette for it has to seat all seven
// or two of them silently share a colour. Two greys close the ramp for the same reason they
// always did — past a named palette this size, the honest signal for "everything else" is grey,
// not a eighth hue competing for attention it was never meant to have.
//
// Series also get their own point style (circle / triangle / rectangle …), so the legend does
// not rely on colour alone. That is the actual fix: colour is a hint, shape is the answer.
const CATEGORICAL_DARK = [
  "#479EF5", "#FFB900", "#6BB700", "#2ED2E0", "#B58EEA", "#F072A8", "#A19F9D", "#797775",
];
const CATEGORICAL_LIGHT = [
  "#0078D4", "#8A6100", "#107C10", "#0E7C86", "#6B3FA0", "#A23368", "#605E5C", "#8A8886",
];
// Same eight-slot shape and the same blue/amber-first CVD ordering, in the wider, more
// saturated hue set the rest of this theme is built from — this is the single change that
// makes "Colourful" actually colourful where it matters most: the charts, not just the chrome.
const CATEGORICAL_VIVID = [
  "#4C8DFF", "#FFB020", "#34D399", "#2FE0D1", "#B084F0", "#FF6F91", "#8FBF9F", "#93A29D",
];
// The same eight slots with no hue to spend, so they alternate ends of the usable band instead:
// neighbouring categories are as far apart in lightness as the page allows, and the point style
// carries the rest. Slots 6 and 7 stay the quietest pair, keeping "Other" and "Unknown"
// recessive exactly as CATEGORY_SLOT below expects them to be.
const CATEGORICAL_PAPER = [
  "#211B10", "#807A6F", "#403A2F", "#8F897E", "#2E281D", "#716B60", "#514B40", "#625C51",
];
const CATEGORICAL_BY_THEME = {
  dark: CATEGORICAL_DARK, light: CATEGORICAL_LIGHT, vivid: CATEGORICAL_VIVID,
  paper: CATEGORICAL_PAPER,
};
const categorical = () => CATEGORICAL_BY_THEME[currentTheme()] || CATEGORICAL_DARK;

// A second, named categorical set for one chart only: "Month-over-month spend" always shows
// the same seven cost areas in the same order (rail order, "Other services" last — see
// `_group_for_family` in dashboard.py), so it can afford a fixed palette tuned for exactly that
// shape rather than the general-purpose one above, which has to seat anything from three
// pricing models to eight ad-hoc categories.
//
// Built from a five-swatch teal-to-coral reference: Midnight green and Sky blue anchor the cool
// end, Tea rose and Light coral the warm end. The reference's near-white fifth swatch
// (Seashell) is not used here — a chart series has to hold a crisp edge against the card behind
// it, and a near-white fill does that on a dark surface but disappears on this same chart's
// light-theme card, so it is better spent as a page neutral than as a data colour. Two more
// hues (a deeper teal, a deeper coral) extend the pair to six named areas, keeping to the
// reference's own two-family logic rather than introducing a third; "Other services" keeps the
// muted grey every categorical set in this app reserves for it, so it still recedes rather than
// competing with six colours that were actually asked for.
const MOM_DARK = [
  "#1C90AC", "#E37C78", "#82BAC4", "#F2A9A4", "#045C71", "#B0504A", "#8A9296",
];
const MOM_LIGHT = [
  "#00647B", "#C65750", "#4A8C9A", "#D98883", "#024859", "#8C3934", "#6B7176",
];
const MOM_VIVID = [
  "#2FE0D1", "#FF6F91", "#82BAC4", "#FFB4AE", "#16A085", "#C2544E", "#8FBF9F",
];
// Seven fixed cost areas, alternating the band the same way the general set does. "Other
// services" keeps the last slot and the quietest value, as in every theme.
const MOM_PAPER = [
  "#211B10", "#807A6F", "#403A2F", "#8F897E", "#2E281D", "#625C51", "#716B60",
];
const MOM_BY_THEME = { dark: MOM_DARK, light: MOM_LIGHT, vivid: MOM_VIVID, paper: MOM_PAPER };
const monthOverMonthPalette = () => MOM_BY_THEME[currentTheme()] || MOM_DARK;

// Every named categorical set a chart can ask for, keyed by the string `spec.categoricalPalette`
// call sites pass to `drawChart`. Adding a future chart-specific palette means adding one more
// entry here, not touching the drawing or re-tinting code that reads it.
const PALETTE_REGISTRY = {
  default: categorical,
  monthOverMonth: monthOverMonthPalette,
};
const paletteFor = (key) => (PALETTE_REGISTRY[key] || PALETTE_REGISTRY.default)();

// Categories that appear in more than one chart get a fixed slot, so "Spot" is the same colour
// in the doughnut and in the stacked bar below it — even though it is the third series in one
// and whichever position the data happened to yield in the other. Two charts of the same three
// things in different colours is worse than no colour coding at all.
//
// "Other" and "Unknown" are pinned to the two greys at the end of the ramp rather than to
// whichever position they happen to fall in — they are the one pair of categories that *should*
// recede, being explicitly the leftovers a real name could not be found for, and grey is what
// makes that legible at a glance instead of competing with the named slices for attention.
//
// Anything not named here falls back to its position, which is correct for one-off series.
const CATEGORY_SLOT = new Map([
  ["on demand", 0], ["ondemand", 0],
  ["committed", 1], ["reserved", 1],
  ["spot", 2],
  ["other", 6],
  ["unknown", 7],
]);
const slotFor = (label, fallback) => {
  const key = String(label ?? "").trim().toLowerCase();
  return CATEGORY_SLOT.has(key) ? CATEGORY_SLOT.get(key) : fallback;
};

// Redundant encoding for the legend and the hover marker. Ordered so the first two — the most
// common case — are the most different from each other. Eight, matching the eight colour slots
// above, so a chart with every named cost area still tells its slices apart by shape as well as
// hue rather than the eighth series quietly reusing the first series' marker.
const POINT_STYLES = [
  "circle", "triangle", "rect", "rectRot", "star", "crossRot", "rectRounded", "cross",
];

const TICK_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * Axis labels, shortened to what a reader actually needs.
 *
 * A daily series arrives as ISO dates, which are 10 characters that collide at any useful
 * density — so the previous version tilted them 45°. Rotated text is slower to read and eats
 * a third of the plot height. "12 Aug" is unambiguous in context and fits horizontally, which
 * buys back the space and the legibility at once.
 */
function shortenTick(raw) {
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  if (iso) return `${Number(iso[3])} ${TICK_MONTHS[Number(iso[2]) - 1]}`;
  return raw.length > 16 ? raw.slice(0, 15) + "\u2026" : raw;
}

const CHART_KIND = {
  line: "line", area: "line", bar: "bar", hbar: "bar",
  stacked_bar: "bar", pie: "pie", doughnut: "doughnut",
};

let chartLib = null;
/** Load Chart.js on first use — it is 200KB and most questions never need it. */
function loadChartLib() {
  if (chartLib) return chartLib;
  chartLib = new Promise((resolve, reject) => {
    if (window.Chart) return resolve(window.Chart);
    const s = document.createElement("script");
    s.src = "/assets/vendor/chart.umd.min.js";
    s.onload = () => (window.Chart ? resolve(window.Chart) : reject(new Error("Chart.js missing")));
    s.onerror = () => reject(new Error("Chart.js failed to load"));
    document.head.appendChild(s);
  });
  return chartLib;
}

/** Read a design token at draw time, so charts follow the theme without duplicating its palette. */
function token(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Live charts, so a theme change can restyle them in place. A WeakSet would not let us iterate,
// and re-fetching every tab to recolour a canvas would be absurd.
const liveCharts = new Set();

/**
 * Destroy the charts whose canvas has left the page, and forget them.
 *
 * Dropping our own reference is not enough, which is what this used to do. Chart.js keeps every
 * instance in a registry of its own, with a resize observer and pointer listeners attached to
 * each — so a chart we merely stopped pointing at stayed alive, listening, for as long as the
 * tab was open. Tabs re-render by replacing `innerHTML`, which orphans their canvases, so the
 * count grew with every switch on a dashboard that is meant to be left open all day.
 *
 * `destroy()` is what releases the listeners and lets the canvas be collected.
 */
function reapDetachedCharts() {
  for (const chart of liveCharts) {
    if (chart.canvas?.isConnected) continue;
    try {
      chart.destroy();
    } catch {
      // Already disposed, or the library is mid-teardown. Either way it is no longer ours.
    }
    liveCharts.delete(chart); // the tab it belonged to has been replaced
  }
}

/** Restyle every chart on screen for the current theme. Options only — no data is refetched. */
/**
 * The colour a dataset should be, given how many there are and what it is.
 *
 * One function for both the first draw and every re-tint. These were two implementations —
 * `drawChart` chose categorical hues and spread ordered series across the ramp; `retintCharts`
 * assigned `ramp[i]` — so a theme switch silently converted a legible chart into two adjacent
 * shades of the same blue. A chart that changes meaning when someone toggles dark mode is worse
 * than one that was never right.
 */
function seriesColour({ index, label, count, categorical: cat, isPie, paletteKey }) {
  const ramp = palette();
  const cats = paletteFor(paletteKey);
  if (cat) return cats[slotFor(label, index) % cats.length];
  // Adjacent entries of the ordered ramp are one step apart and barely distinguishable — the
  // point when they are ranked slices of one series, a defect when they are separate lines.
  // Spread them across the legible first half instead; the tail is deliberately close to the
  // background so small slices recede, and a whole line rendered there disappears.
  const BAND = 5;
  const spread = !isPie && count > 1
    ? Math.round((index * BAND) / Math.max(1, count - 1))
    : index;
  return ramp[spread % ramp.length];
}

export function retintCharts() {
  const grid = token("--grid-line", "rgba(255,255,255,.06)");
  const tick = token("--muted", "#8f98a6");
  const surface = token("--surface", "#14181e");
  const surface2 = token("--surface-2", "#1b2027");
  const borderStrong = token("--border-strong", "#333b47");
  const text = token("--text", "#e9ebef");

  reapDetachedCharts();
  for (const chart of liveCharts) {
    const o = chart.options;
    if (o.plugins?.legend?.labels) o.plugins.legend.labels.color = tick;
    if (o.plugins?.tooltip) {
      Object.assign(o.plugins.tooltip, {
        backgroundColor: surface2, borderColor: borderStrong,
        titleColor: text, bodyColor: text,
      });
    }
    for (const axis of Object.values(o.scales || {})) {
      if (axis.grid) axis.grid.color = grid;
      if (axis.ticks) axis.ticks.color = tick;
    }
    // Slices and series are re-coloured in place — still no refetch, just a different set of
    // strings assigned to the same data. Ranked/categorical series go through `seriesColour`, so
    // a re-tint produces exactly what the first draw produced, in the other theme's values; a
    // dashed reference line keeps its fixed amber regardless of theme, matching the first draw.
    const ramp = palette();
    // Recorded at draw time: which named categorical set this chart uses, so a chart drawn with
    // `categoricalPalette: "monthOverMonth"` re-tints from that same set rather than silently
    // falling back to the shared one once the theme changes.
    const cats = paletteFor(chart.$clPaletteKey);
    // Recorded at draw time: whether this chart's series are categories or a ranked sequence.
    // Re-deriving it here would guess, and guessing is what made the two paths disagree.
    const isCat = chart.$clCategorical === true;
    const isPieChart = chart.config?.type === "pie" || chart.config?.type === "doughnut";
    const count = chart.data.datasets.length;
    for (const [i, ds] of chart.data.datasets.entries()) {
      if (Array.isArray(ds.backgroundColor)) {
        const labels = chart.data.labels || [];
        ds.backgroundColor = ds.backgroundColor.map((_, j) =>
          isCat ? cats[slotFor(labels[j], j) % cats.length] : ramp[j % ramp.length]);
      } else if (ds.borderColor && !Array.isArray(ds.borderColor)) {
        const colour = Array.isArray(ds.borderDash)
          ? token("--warn", "#FFB900")
          : seriesColour({
              index: i, label: ds.label, count, categorical: isCat, isPie: isPieChart,
              paletteKey: chart.$clPaletteKey,
            });
        ds.borderColor = colour;
        if (typeof ds.backgroundColor === "string") {
          ds.backgroundColor = ds.backgroundColor.length === 9 ? colour + "33" : colour;
        }
      }
      if (ds.borderWidth === 2 && Array.isArray(ds.backgroundColor)) ds.borderColor = surface;
    }
    // Not `chart.update("none")`. That mode is Chart.js's cheap path -- meant for resize and
    // other structural updates -- and for a bar/stacked-bar controller it can leave each
    // element's own cached style object holding its *previous* colour even though
    // `chart.data.datasets[i].backgroundColor` above already holds the new one; the mismatch
    // is invisible on a two-theme palette that only shifts a shade, and obvious on this app's
    // third theme, whose hues are nowhere near dark/light's. A full update forces every
    // element to re-resolve its style from the data just mutated; animation is switched off
    // for just this call so that resolve is instant rather than a 400ms tween, then restored
    // so a real, later data change still animates as designed.
    const savedAnimation = o.animation;
    o.animation = false;
    chart.update();
    o.animation = savedAnimation;
  }
}

export async function drawChart(container, spec) {
  // Accept an element or an id. Every call site but one passed an element; the odd one out
  // passed a string and threw `container.appendChild is not a function` at runtime — invisible
  // to every static check, and only on a tab most people open occasionally. An implicit
  // contract that fails this quietly is better made explicit.
  const host = typeof container === "string" ? document.getElementById(container) : container;
  if (!host) return null;

  const kind = CHART_KIND[spec.type] || "bar";
  const isPie = kind === "pie" || kind === "doughnut";
  const stacked = spec.type === "stacked_bar";
  const horizontal = spec.type === "hbar";

  const card = document.createElement("figure");
  card.className = "chart chart-pending";
  card.innerHTML = `
    <div class="chart-head">
      <figcaption>${esc(spec.title || "")}</figcaption>
      <div class="chart-actions">
        <button type="button" data-act="png" title="Download as PNG">PNG</button>
        <button type="button" data-act="csv" title="Download the underlying data">CSV</button>
      </div>
    </div>
    <div class="canvas-wrap"><canvas role="img"></canvas></div>
    ${spec.note ? `<p class="chart-note">${esc(spec.note)}</p>` : ""}`;
  host.appendChild(card);

  const bars = spec.labels.length;
  // `spec.height` for callers that know better than the default. A chart repeated once per
  // item in a list — one per budget, say — is read as a row in a list rather than as a panel,
  // and at the standard height ten of them make a page nobody scrolls to the end of.
  //
  // `spec.fillHeight` is the opposite problem: a chart sharing a CSS Grid row with a taller
  // neighbour (see .grid-bridge) gets stretched by the grid regardless of what height this
  // function picks, so a fixed pixel height here just becomes the size of the gap left below
  // the note. Opt-in, not the default -- most charts (a budget's own mini trend, repeated once
  // per row) must keep a height chosen for what they are, not inherit whatever a stray
  // neighbour happens to be tall enough to make the row.
  if (spec.fillHeight) {
    card.classList.add("chart-fill");
  } else {
    const height = spec.height
      ? spec.height
      : horizontal
        ? Math.max(160, Math.min(bars * 24 + 56, 560))
        : isPie
          ? 220
          : Math.min(240, Math.max(180, window.innerHeight * 0.24));
    card.querySelector(".canvas-wrap").style.height = `${height}px`;
  }

  const canvas = card.querySelector("canvas");
  canvas.setAttribute(
    "aria-label",
    `${spec.title}. ${spec.datasets.length} series across ${bars} categories.`
  );

  if (!(await whenVisible(card)) || !card.isConnected) return null;
  const Chart = await loadChartLib();
  if (!card.isConnected) return null;
  card.classList.remove("chart-pending");

  // Several series means several *things*, so they need distinct hues; one series split into
  // slices means one thing ranked, so it keeps the ordered ramp. Either can say so explicitly:
  // a period-over-period comparison is two series of the *same* thing, and giving the reference
  // line its own hue would promote it from context to peer.
  const manySeries = !isPie && spec.datasets.length > 1;
  const useCategorical = spec.categorical ?? manySeries;

  const datasets = spec.datasets.map((ds, i) => {
    const ramp = palette();
    const cats = paletteFor(spec.categoricalPalette);
    const slot = useCategorical ? slotFor(ds.label, i) : i;
    // A dashed reference line (previous-period, budget limit) is a backdrop being read against,
    // not a peer series, so it gets one fixed, deliberate colour — the same amber/orange the app
    // already uses for "needs attention" — instead of a ramp slot that would leave it some
    // unnamed shade of blue depending on how many series happen to be on the chart.
    const colour = ds.dash
      ? token("--warn", "#FFB900")
      : seriesColour({
          index: i, label: ds.label, count: spec.datasets.length,
          categorical: useCategorical, isPie, paletteKey: spec.categoricalPalette,
        });
    if (isPie) {
      const slice = (j) =>
        useCategorical
          ? cats[slotFor(spec.labels[j], j) % cats.length]
          : ramp[j % ramp.length];
      return {
        label: ds.label,
        data: ds.data,
        backgroundColor: spec.labels.map((_, j) => slice(j)),
        // Slices are separated by a ring the colour of the card behind them, so it reads as a
        // gap rather than an outline.
        borderColor: token("--surface", "#14181e"),
        borderWidth: 2,
      };
    }
    return {
      label: ds.label,
      data: ds.data,
      // Per-point labels for series aligned by position rather than by date. Carried on the
      // dataset so the tooltip can name the day each number actually came from.
      pointLabels: ds.pointLabels,
      borderColor: colour,
      backgroundColor: kind === "line" ? colour + "33" : colour,
      fill: spec.type === "area" && !ds.dash,
      // Gentler than a spline: enough to read as a trend, not so much that it invents peaks
      // between two points that were never measured.
      tension: 0.22,
      pointRadius: bars > 40 ? 0 : 2.5,
      pointHoverRadius: 4,
      borderWidth: ds.dash ? 1.5 : 2,
      borderRadius: kind === "bar" ? 3 : 0,
      // Shape as well as colour, so the legend survives being printed in grey or read by
      // someone who cannot tell the hues apart.
      pointStyle: POINT_STYLES[slot % POINT_STYLES.length],
      // A comparison series has to recede: same shape, dashed and unfilled, so the eye reads
      // it as the reference rather than as a second thing of equal weight.
      ...(ds.dash ? { borderDash: [4, 4], pointRadius: 0, pointHoverRadius: 3 } : {}),
    };
  });

  const fmt = (v) =>
    typeof v === "number"
      ? v.toLocaleString(undefined, { maximumFractionDigits: 2 })
      : v;

  const gridColour = token("--grid-line", "rgba(255,255,255,.06)");
  const tickColour = token("--muted", "#8f98a6");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const chart = new Chart(canvas, {
    type: kind,
    data: { labels: spec.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduceMotion ? false : { duration: 400 },
      indexAxis: horizontal ? "y" : "x",
      // "index" is right for series meant to be read together at one point -- the daily-spend
      // comparison line and its previous-period twin, say, where the question is "how did they
      // differ that day" and the answer needs both numbers in the one tooltip. A stacked bar
      // asks the opposite question: which single category is this segment. Spec can opt out of
      // the group behaviour with `hoverSingleSegment` so hovering one slice of the stack shows
      // only that slice, not all seven stacked on top of it.
      interaction: isPie || spec.hoverSingleSegment
        ? { mode: "nearest", intersect: true }
        : { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: isPie || datasets.length > 1,
          position: isPie ? "right" : "top",
          align: isPie ? "center" : "end",
          labels: {
            color: tickColour, boxWidth: 8, boxHeight: 8,
            font: { size: 12 },
            // Each series keeps its own marker shape here rather than every entry being a
            // circle — the point of the shapes is that the legend does not depend on colour.
            // Pie slices stay circular: they share one dataset and are told apart by position.
            usePointStyle: true,
            ...(isPie ? { pointStyle: "circle" } : {}),
            padding: 14,
          },
        },
        tooltip: {
          enabled: false,
          external: chartTooltip,
          backgroundColor: token("--surface-2", "#1b2027"),
          borderColor: token("--border-strong", "#333b47"),
          borderWidth: 1,
          titleColor: token("--muted", "#8f98a6"),
          bodyColor: token("--text", "#e9ebef"),
          padding: 10,
          cornerRadius: 4,
          displayColors: !isPie,
          boxWidth: 8,
          boxHeight: 8,
          usePointStyle: true,
          titleFont: { size: 11, weight: "600" },
          bodyFont: { size: 12 },
          callbacks: {
            label: (c) => {
              const v = fmt(isPie ? c.parsed : horizontal ? c.parsed.x : c.parsed.y);
              // A dataset can carry its own per-point labels — used where two series are
              // aligned by *position* rather than by date, as the daily-spend comparison is.
              // Without this the tooltip shows one date above two numbers, and a reader quite
              // reasonably assumes both were measured on it. They were not: the second is the
              // same day of the previous window, which is the whole point of the comparison.
              const own = c.dataset.pointLabels?.[c.dataIndex];
              const name = c.dataset.label || c.label;
              return ` ${own ? `${name} (${own})` : name}: ${v}`;
            },
          },
        },
      },
      scales: isPie
        ? {}
        : {
            // With indexAxis:'y' the categories sit on y and the values on x, so the number
            // formatter has to follow the value axis or the labels become indices.
            x: {
              stacked,
              // Gridlines only on the axis that carries quantity. Lines running down through
              // a time series are a grid for its own sake: nobody reads a value off them, and
              // they cut every shape on the chart into slices.
              grid: { display: horizontal, color: gridColour, drawBorder: false },
              border: { color: gridColour },
              ticks: {
                color: tickColour, font: { size: 12 },
                maxRotation: 0, autoSkipPadding: 12, padding: 6,
                ...(horizontal
                  ? { callback: fmt }
                  : {
                      // Chart.js drops category labels when they collide. On a short
                      // series that silently hides which bar is which, so keep every
                      // label and shorten the long ones instead.
                      autoSkip: spec.labels.length > 8,
                      callback(value, index) {
                        const raw = String(spec.labels[index] ?? "");
                        return shortenTick(raw);
                      },
                    }),
              },
            },
            y: {
              stacked,
              // Money starts at zero. A baseline cropped to the data makes a 5% wobble look
              // like a crisis, which on a cost chart is not a style choice but a wrong answer.
              beginAtZero: !horizontal,
              grid: { display: !horizontal, color: gridColour, drawBorder: false },
              border: { display: false },
              ticks: {
                color: tickColour, font: { size: 12 }, padding: 8, maxTicksLimit: 6,
                ...(horizontal ? { autoSkip: false, crossAlign: "far" } : { callback: fmt }),
              },
            },
          },
    },
  });

  // Recorded on the chart so a re-tint can reproduce exactly this decision rather than guessing
  // it back from the rendered colours.
  chart.$clCategorical = useCategorical;
  chart.$clPaletteKey = spec.categoricalPalette;
  const selectPoint = (index) => {
    if (typeof spec.onSelect === "function") return spec.onSelect(index, spec.labels[index]);
    let panel = card.querySelector(".chart-point");
    if (!panel) {
      panel = document.createElement("section");
      panel.className = "chart-point";
      panel.id = `chart-point-${chart.id}`;
      panel.setAttribute("aria-live", "polite");
      card.appendChild(panel);
      canvas.setAttribute("aria-controls", panel.id);
    }
    const rows = spec.datasets.map(series => {
      const value = series.data[index];
      const formatted = value == null ? "No data"
        : typeof value === "number" && spec.currency ? money(value, spec.currency, 2)
        : typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 2 })
        : String(value);
      return `<tr><th scope="row">${esc(series.label || "Value")}</th>
        <td>${esc(series.pointLabels?.[index] || spec.labels[index])}</td>
        <td class="num">${esc(formatted)}</td></tr>`;
    }).join("");
    panel.innerHTML = `<div class="chart-point-head">
      <strong>${esc(spec.labels[index])}</strong>
      <button type="button" class="ghost" aria-label="Close chart details">Close</button>
      </div><table><thead><tr><th>Series</th><th>Period / category</th><th>Value</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
    panel.querySelector("button").onclick = () => {
      panel.remove();
      canvas.removeAttribute("aria-controls");
      canvas.focus({ preventScroll: true });
    };
    panel.scrollIntoView({ block: "nearest" });
  };
  wireChartTooltip(chart, selectPoint);
  // Exposed so another panel can open the same day's detail rather than building a rival one.
  // The calendar heatmap on Overview is an index into this chart; two panels claiming the same
  // date would eventually disagree about it.
  chart.$clSelectPoint = selectPoint;
  liveCharts.add(chart);
  // Sweep here as well as on a theme change. Re-tinting is the only other place that walks this
  // set, and it only runs when somebody toggles dark mode — which most people never do, so the
  // charts a day of tab-switching left behind were never collected at all.
  reapDetachedCharts();

  canvas.style.cursor = "pointer";
  canvas.onclick = (ev) => {
    const hit = chart.getElementsAtEventForMode(ev, isPie ? "nearest" : "index",
      { intersect: isPie }, true);
    if (hit.length) selectPoint(hit[0].index);
  };

  card.querySelector('[data-act="png"]').onclick = () => {
    const a = document.createElement("a");
    a.href = chart.toBase64Image("image/png", 1);
    a.download = `${slug(spec.title)}.png`;
    a.click();
  };
  card.querySelector('[data-act="csv"]').onclick = () => downloadCsv(spec);
  return chart;
}

function whenVisible(element) {
  if (!element.isConnected) return Promise.resolve(false);
  const rect = element.getBoundingClientRect();
  if (rect.bottom >= -300 && rect.top <= window.innerHeight + 300) return Promise.resolve(true);
  if (!("IntersectionObserver" in window)) return Promise.resolve(true);
  return new Promise((resolve) => {
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        clearInterval(detachedCheck);
        observer.disconnect();
        resolve(true);
      },
      { rootMargin: "300px 0px" }
    );
    const detachedCheck = setInterval(() => {
      if (element.isConnected) return;
      clearInterval(detachedCheck);
      observer.disconnect();
      resolve(false);
    }, 2000);
    observer.observe(element);
  });
}

function slug(s) {
  return (s || "chart").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
}

function downloadCsv(spec) {
  const cell = (v) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = ["label", ...spec.datasets.map((d) => d.label)];
  const lines = [header.map(cell).join(",")];
  spec.labels.forEach((lab, i) => {
    lines.push([lab, ...spec.datasets.map((d) => d.data[i] ?? "")].map(cell).join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${slug(spec.title)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

boot();
