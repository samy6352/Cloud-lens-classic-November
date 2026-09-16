/**
 * Daily spend, as a calendar rather than a line.
 *
 * Titled "Daily pattern" rather than "Daily spend": the Overview already carries a line chart
 * called "Daily spend", and two panels sharing a name make a reader work out which is which
 * before either can be read. The names now say what each one answers rather than what both
 * are drawn from.
 *
 * The trend chart already answers "is spend rising" — a slope, read once. It cannot answer
 * "which days": a line compresses seven weekdays and a month-end into one shape, and a reader
 * has to mentally divide it back into days to notice that Saturdays are consistently quiet or
 * that the 1st of the month spikes. A calendar keeps every day a cell of its own, coloured by
 * how much it cost, so that pattern is a colour someone notices without reading a figure.
 *
 * Weeks run as columns, Sunday to Saturday down each — the shape everyone already reads off a
 * phone calendar or a GitHub contribution graph, so nothing here needs a legend to be
 * understood before its numbers are.
 *
 * A day the data never mentions is left blank, not coloured as zero. `trend.labels` already
 * only lists days the warehouse actually holds a row for — the same "a gap stays a gap"
 * decision the tag explorer makes about its own daily series — and a blank square here means
 * exactly that: nothing was billed *or* nothing was loaded, distinguishable from a real quiet
 * day by having no fill at all rather than the lightest one.
 */
import { esc, money } from "./app.js";

const DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const LEVELS = 4; // plus level 0 (a real day that billed nothing), so five shades in total.

/**
 * Render a calendar heatmap of daily spend into `container`.
 *
 * `trend` is the same `{ labels, values }` the line chart above it draws from — no second
 * request, just a second shape drawn from the one already fetched.
 */
export function drawCalendarHeatmap(container, trend, currency, opts = {}) {
  const labels = trend?.labels || [];
  const values = trend?.values || [];
  const pickable = typeof opts.onPick === "function";

  if (!labels.length) {
    container.innerHTML = `<section class="card heatcal">
      <div class="tm-head"><h3>Daily pattern</h3></div>
      <p class="chart-note">No daily spend data is available for this selection.</p>
    </section>`;
    return;
  }

  const byDate = new Map(labels.map((d, i) => [d, values[i]]));
  const first = new Date(`${labels[0]}T00:00:00Z`);
  const last = new Date(`${labels[labels.length - 1]}T00:00:00Z`);

  // Pad out to the Sunday on or before the first day and the Saturday on or after the last, so
  // every week drawn is a complete column and the first/last real day lands in its true row
  // rather than at the top of its column regardless of what weekday it fell on.
  const start = new Date(first);
  start.setUTCDate(start.getUTCDate() - start.getUTCDay());
  const end = new Date(last);
  end.setUTCDate(end.getUTCDate() + (6 - end.getUTCDay()));

  const days = [];
  for (let d = new Date(start); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    days.push(new Date(d));
  }
  const weeks = Math.ceil(days.length / 7);

  const present = [...byDate.values()].filter((v) => v != null);
  const max = present.length ? Math.max(...present, 0.01) : 0.01;

  const monthLabel = (d) => d.toLocaleDateString(undefined, { month: "short", timeZone: "UTC" });
  // One label per column, only where a month actually starts within it — repeating "Sep" under
  // every week of September would be noise, not an axis.
  const monthMarks = [];
  let lastMonth = null;
  for (let w = 0; w < weeks; w++) {
    const d = days[w * 7];
    const m = d.getUTCMonth();
    if (m !== lastMonth) {
      monthMarks.push({ week: w, label: monthLabel(d) });
      lastMonth = m;
    }
  }

  const cells = days
    .map((d) => {
      const iso = d.toISOString().slice(0, 10);
      const has = byDate.has(iso);
      const value = has ? byDate.get(iso) : null;
      const level = has ? Math.min(LEVELS, Math.round((value / max) * LEVELS)) : null;
      const inRange = d >= first && d <= last;
      const spoken = has
        ? `${d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" })} — ${money(value, currency, 2)}`
        : "";
      if (!has) {
        return `<span class="heat-cell heat-none"${inRange ? "" : ' aria-hidden="true"'}></span>`;
      }
      // A real day is a button when there is somewhere to go: a square that colours itself to
      // say "this day was expensive" and then cannot answer "why" is a tease. Falls back to the
      // focusable span it has always been when no handler was supplied.
      if (!pickable) {
        return `<span class="heat-cell heat-${level}" tabindex="0"
                  title="${esc(spoken)}" aria-label="${esc(spoken)}"></span>`;
      }
      return `<button type="button" class="heat-cell heat-${level} heat-pick"
                data-heat-day="${esc(iso)}"
                title="${esc(spoken)} — click to break this day down"
                aria-label="${esc(spoken)}"></button>`;
    })
    .join("");

  container.innerHTML = `
    <section class="card heatcal">
      <div class="tm-head">
        <h3>Daily pattern</h3>
        <span class="tm-count">${labels.length} day${labels.length === 1 ? "" : "s"} with data</span>
      </div>
      <div class="heatcal-body">
        <div class="heatcal-daylabels">${DAY_LABELS
          .map((l, i) => `<span${i % 2 ? "" : ' class="on"'}>${i % 2 ? "" : esc(l)}</span>`)
          .join("")}</div>
        <div class="heatcal-scroll">
          <div class="heatcal-months" style="grid-template-columns:repeat(${weeks},var(--heat-cell))">
            ${monthMarks.map((m) => `<span style="grid-column:${m.week + 1}">${esc(m.label)}</span>`).join("")}
          </div>
          <div class="heatcal-grid" style="grid-template-rows:repeat(7,var(--heat-cell));grid-template-columns:repeat(${weeks},var(--heat-cell))">
            ${cells}
          </div>
        </div>
      </div>
      <p class="chart-note">Colour is share of the busiest day in this window (${esc(money(max, currency, 0))}).
        A blank square is a day with no data loaded, not a quiet one.${
          pickable ? " Select a day to see what ran on it." : ""}</p>
    </section>`;

  if (pickable) {
    for (const button of container.querySelectorAll("[data-heat-day]")) {
      button.onclick = () => opts.onPick(button.dataset.heatDay, byDate.get(button.dataset.heatDay));
    }
  }

  const card = container.querySelector(".heatcal");
  const body = container.querySelector(".heatcal-body");
  const dayLabels = container.querySelector(".heatcal-daylabels");

  /**
   * Grow the cell size to use the box we actually have, the same "measure, then lay out to
   * fill it" approach the treemap beside this uses -- rather than a fixed 13px that leaves
   * whatever the row's taller neighbours add as dead space below a small, centred grid.
   *
   * Every dimension in this component already reads from the one `--heat-cell` custom
   * property (month row height, grid row/column sizing, day-label line height), so growing the
   * calendar is only a matter of solving for the cell size that fills the measured box and
   * setting that one property -- no markup to regenerate, unlike the treemap's percentage tiles.
   */
  const layout = () => {
    const h = body.clientHeight;
    const w = body.clientWidth;
    // Hidden tab, or first paint before the flex box has resolved. The observer calls back.
    if (h < 8 || w < 8) return;

    const dayLabelWidth = dayLabels.getBoundingClientRect().width;
    const bodyGap = 8; // var(--s2), the gap between the day-label column and the grid.
    const rowGap = 3; // the 3px gap this component's grid/months rows already use throughout.

    // Height: one month-row (cellSize + 3px margin) plus 7 grid rows with 6 gaps between them,
    // plus the scroll wrapper's own 2px bottom padding -- see .heatcal-months/.heatcal-grid.
    const byHeight = Math.floor((h - rowGap - 2 - 6 * rowGap) / 8);
    // Width: the day-label column plus the gap beside it plus `weeks` columns with their gaps.
    const byWidth = Math.floor(
      (w - dayLabelWidth - bodyGap - (weeks - 1) * rowGap) / weeks
    );

    // Never smaller than the original design (13px stays legible at any size), never so large
    // that a handful of weeks turns into oversized tiles nobody would call a calendar.
    const size = Math.max(13, Math.min(40, byHeight, byWidth));
    card.style.setProperty("--heat-cell", `${size}px`);
  };

  layout();

  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      // The dashboard replaces whole subtrees on re-render. Without this the observer would
      // keep a detached element alive and re-lay-out something nobody can see.
      if (!body.isConnected) {
        ro.disconnect();
        return;
      }
      layout();
    });
    ro.observe(body);
  }
}
