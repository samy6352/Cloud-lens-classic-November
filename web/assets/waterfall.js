/**
 * What moved, as two grouped lists rather than a column of percentages.
 *
 * "Biggest movers" (the table beside this) already lists which services swung the most and by
 * how much. What it adds here is the shape of that swing at a glance: everything that pushed
 * spend up grouped together, everything that pulled it down grouped together, split by a "no
 * change" line so which direction dominates is visible before a single figure is read.
 *
 * Reconciled exactly, not illustrated: the movers passed in are only the top few by absolute
 * change, so their deltas alone would rarely span the real gap between the previous total and
 * the current one. The remainder is folded in as one more row — "All other services" — sorted
 * into whichever group its own sign belongs to, so the two groups' totals still add up to
 * exactly `current - previous` even though no single row claims to be a running total.
 *
 * Colour follows the rest of the page's convention for a cost change, not a generic up/down:
 * spend rising is `--warn`, falling is `--ok` — the same two tokens the table's own delta cells
 * use, so the chart and the numbers beside it never disagree about which way is good.
 *
 * Rows, not floating columns: a label beside its own bar reads at any length without rotating
 * or truncating into its neighbour, and a row chart's natural height is however many services
 * there are times one compact line each -- rather than a fixed plot height that must also leave
 * room under it for a diagonal run of text.
 */
import { esc, money } from "./app.js";

/**
 * Render a cost-change list into `container`.
 *
 * `movers` is the same array behind the "Biggest movers" table: `{ name, cost, previous,
 * delta }`, already capped and ordered by absolute change. `kpis` supplies the true previous
 * and current totals the header reconciles to.
 */
export function drawWaterfall(container, movers, kpis, currency, opts = {}) {
  const previousTotal = kpis?.previous;
  const currentTotal = kpis?.total;
  const days = opts.days;

  if (previousTotal == null || !movers?.length) {
    container.innerHTML = "";
    return;
  }

  const named = movers.filter((m) => Math.abs(m.delta) >= 0.01);
  const namedDelta = named.reduce((n, m) => n + m.delta, 0);
  const otherDelta = currentTotal - previousTotal - namedDelta;

  const moves = [
    ...named.map((m) => ({ label: m.name, delta: m.delta, pick: true })),
    ...(Math.abs(otherDelta) >= 0.01
      ? [{ label: "All other services", delta: otherDelta, pick: false }]
      : []),
  ];

  // Split by sign, not left in "Biggest movers" order: the whole point of grouping is that
  // everything pushing spend up sits together above the line and everything pulling it down
  // sits together below. Each group keeps the incoming absolute-magnitude order, which puts the
  // largest swing at the outer edge of its group and the smallest right next to the line --
  // this tapers toward "no change" the way a diverging chart is supposed to, rather than jumping
  // straight from the biggest swing to the line.
  const ups = moves.filter((m) => m.delta > 0);
  const downs = moves.filter((m) => m.delta <= 0).reverse();
  const maxAbs = Math.max(0.01, ...moves.map((m) => Math.abs(m.delta)));

  const row = (m) => {
    const up = m.delta > 0;
    const pct = Math.min(100, (Math.abs(m.delta) / maxAbs) * 100);
    const sign = up ? "+" : "\u2212";
    const money2 = esc(money(m.delta, currency, 2));
    // "All other services" is a remainder, not a service — there is nothing to narrow to, and
    // a click that filtered to a name no resource carries would empty the dashboard.
    const live = m.pick
      ? `<button type="button" class="wf-row wf-${up ? "up" : "down"} wf-pick"
                 data-wf-service="${esc(m.label)}"
                 title="${esc(m.label)}: ${money2} — click to filter the dashboard to it">`
      : `<div class="wf-row wf-${up ? "up" : "down"}" tabindex="0"
              title="${esc(m.label)}: ${money2}">`;
    return `${live}
      <span class="wf-row-label">${esc(m.label)}</span>
      <div class="wf-row-track"><div class="wf-row-bar" style="width:${pct}%"></div></div>
      <span class="wf-row-val">${sign}${esc(money(Math.abs(m.delta), currency, 0))}</span>
    ${m.pick ? "</button>" : "</div>"}`;
  };

  // The divider only earns its place between two groups that both exist -- with every mover on
  // one side, a "no change" line floating at the very top or bottom would separate the list from
  // nothing.
  const divider = ups.length && downs.length
    ? `<div class="wf-zero-divider"><span>No change</span></div>`
    : "";

  container.innerHTML = `
    <section class="card waterfall">
      <div class="tm-head">
        <h3>What changed</h3>
        <span class="tm-count">${esc(money(previousTotal, currency))} → ${esc(money(currentTotal, currency))}</span>
      </div>
      <div class="wf-legend">
        <span class="wf-key wf-key-up">Added to spend</span>
        <span class="wf-key wf-key-down">Reduced spend</span>
      </div>
      <div class="wf-rows">
        ${ups.map(row).join("")}
        ${divider}
        ${downs.map(row).join("")}
      </div>
      <p class="chart-note">Every service that moved spend against the previous
        ${days ? `${days} days` : "period"}, grouped by direction. "All other services" is the
        remainder of every service not large enough to list on its own, so the two groups
        together still reconcile to the true change.${
          opts.onPick ? " Select a service to narrow the whole dashboard to it." : ""}</p>
    </section>`;

  if (typeof opts.onPick !== "function") return;
  for (const button of container.querySelectorAll("[data-wf-service]")) {
    button.onclick = () => opts.onPick(button.dataset.wfService);
  }
}
