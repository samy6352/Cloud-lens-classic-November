/**
 * The shape behind a budget's one number.
 *
 * Azure reports a budget as a single figure with no history, so a meter can say "93%" without
 * answering the question that actually follows: is that a steady climb that lands just inside
 * the limit, or did it breach three weeks ago and keep going? Cumulative spend against a flat
 * budget line answers both, and where the two cross is the day it went over.
 *
 * Cumulative rather than the daily bars the Overview draws, because a budget is a running total
 * by definition — a daily line makes you integrate it in your head to answer the only question
 * the chart exists for.
 *
 * Shared by Budgets, Applications & Tags, and Hourly Billing when a tag selection
 * matches a budget. `drawChart` and `money` are passed in rather than imported so this stays a
 * leaf — the shell owns those, and a reverse import would pull the whole dashboard into any
 * test that wanted to check a budget line.
 */
const GRAIN_WORD = { Monthly: "month", Quarterly: "quarter", Annually: "year" };

export async function loadBudgetTrend(host, budget, {
  get, currency = "", drawBudgetChart, esc, options = {}, isCurrent = () => true,
}) {
  if (!host) return false;
  const draw = () => drawBudgetChart(host, budget, options);
  // drawChart appends, so a redraw over the snapshot must clear it first or the card shows both.
  const redraw = () => {
    for (const canvas of host.querySelectorAll?.("canvas") ?? []) globalThis.Chart?.getChart?.(canvas)?.destroy();
    host.innerHTML = "";
    return draw();
  };
  if (budget.trend || budget.filter_exact === false || budget.trend_note) {
    draw();
    return true;
  }

  if (options.snapshot) draw();
  else host.innerHTML = '<p class="muted note">Loading budget trend…</p>';

  if (!budget._trendPending) {
    const subscription = budget.subscription_id || budget.subscription;
    if (!subscription || !budget.name) {
      host.innerHTML = '<p class="muted note">Budget identity is unavailable; the trend cannot be loaded.</p>';
      return false;
    }
    const query = new URLSearchParams({ subscription_id: subscription, name: budget.name });
    if (currency) query.set("currency", currency);
    budget._trendPending = get(`/api/budgets/trend?${query}`, { timeout: 180000 })
      .then(result => {
        Object.assign(budget, result);
        if (!budget.trend && !budget.trend_note) {
          budget.trend_note = "No comparable trend data is available for this budget period.";
        }
      })
      .catch(error => {
        budget._trendError = error?.message || String(error);
      })
      .finally(() => {
        budget._trendPending = null;
      });
  }

  await budget._trendPending;
  if (!isCurrent()) return false;
  if (budget._trendError) {
    host.innerHTML = `<p class="bad">Could not load budget trend: ${esc(budget._trendError)}</p>`;
    delete budget._trendError;
    return false;
  } else if (budget.trend) {
    redraw();
  } else if (options.snapshot) {
    redraw();
  } else {
    host.innerHTML = `<p class="muted note">${esc(budget.trend_note)}</p>`;
  }
  return true;
}

export function drawBudgetChart(host, b, { drawChart, money, snapshot = false, compact = false }) {
  const t = b.trend;
  if (!host) return;
  if (b.filter_exact === false || b.filtered_exactly === false || t?.filtered_exactly === false) {
    host.innerHTML = '<p class="muted note">Trend comparison unavailable: local data cannot reproduce this budget filter exactly. Use Azure for the authoritative filtered spend.</p>';
    return;
  }
  if (!t) {
    if (!snapshot) return;
    if (!b.currency || !Number.isFinite(b.current_spend) || !Number.isFinite(b.amount)) {
      host.innerHTML = '<p class="muted note">A comparison needs a reported spend, budget limit, and currency.</p>';
      return;
    }
    return drawChart(host, {
      type: "hbar", title: "Azure spend vs budget", height: compact ? 100 : 140,
      labels: ["Spent", "Budget"],
      datasets: [{ label: "Amount", data: [b.current_spend, b.amount] }],
      currency: b.currency, categorical: false,
      note: "Azure-reported spend for the budget's current period. Daily history is unavailable; this is a snapshot, not a trend.",
    });
  }
  if (!(t.labels || []).length || !b.currency || typeof b.amount !== "number" || !Number.isFinite(b.amount)) {
    host.innerHTML = '<p class="muted note">No comparable trend data is available for this budget period.</p>';
    return;
  }

  const cur = b.currency;
  const amount = b.amount;
  const spent = t.total;

  // The warehouse and Azure are two sources for the same money and will differ a little: the
  // warehouse holds the last refresh, Azure's figure is live. A small gap is expected and not
  // worth a caption. A large one means the chart and the headline above it genuinely disagree,
  // and saying so is better than letting someone find it themselves.
  const azure = b.current_spend;
  const gap = typeof azure === "number" && typeof spent === "number"
    ? Math.abs(spent - azure) / Math.max(Math.abs(azure), 0.01) : 0;
  const notes = [];
  if (gap > 0.15) {
    notes.push(`Warehouse cost data totals ${money(spent, cur, 2)} for this period against ` +
               `Azure's ${money(azure, cur, 2)}. Source timing or coverage may differ; refreshing budgets may not close the gap.`);
  }
  if (t.labels.length) {
    notes.push(`${t.labels[0]} to ${t.labels[t.labels.length - 1]}.`);
  }

  return drawChart(host, {
    type: "area",
    title: `Spend this ${GRAIN_WORD[b.time_grain] || "period"}`,
    // Shorter than a standalone chart. Ten budgets at the default height make a page five
    // thousand pixels long, and nobody scrolls to the tenth one — the shape of a cumulative
    // line against a flat limit reads perfectly well at this size.
    height: compact ? 120 : 150,
    labels: t.labels,
    datasets: [
      { label: "Spent", data: t.cumulative, pointLabels: t.labels },
      // A flat reference at the limit. Dashed and unlabelled-by-hue for the same reason the
      // Overview's previous-period line is: it is the backdrop being read against, not a
      // second measurement.
      { label: `Budget ${money(amount, cur)}`,
        data: t.labels.map(() => amount), dash: true },
    ],
    currency: cur,
    categorical: false,
    note: notes.join(" "),
  });
}
