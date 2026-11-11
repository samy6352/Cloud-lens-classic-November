/** Official per-subscription values only; never average unrelated subscription scores. */
export function scoreText(entry) {
  return entry?.status === "available" && Number.isFinite(entry.score)
    ? `${entry.score.toFixed(1)} / 100` : "Not available";
}

const pillarIcons = {
  Cost: ["cost", '<path d="M12 3v18m4-14H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H7"/>'],
  Security: ["security", '<path d="m12 3 8 3v5c0 5-8 10-8 10S4 16 4 11V6Z"/><path d="m8 12 3 3 5-6"/>'],
  HighAvailability: ["reliability", '<path d="m12 3 8 5v8l-8 5-8-5V8Z"/><path d="m8 12 3 3 5-6"/>'],
  OperationalExcellence: ["operations", '<path d="M4 5h16M4 12h16M4 19h16"/><circle cx="8" cy="5" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="9" cy="19" r="2"/>'],
  Performance: ["performance", '<path d="M5 19a9 9 0 1 1 14 0M12 12l5-5M4 12h2m12 0h2M12 3v3"/><circle cx="12" cy="12" r="1"/>'],
};

export function scoreDateRange(dates) {
  // Azure score dates are reporting days, not instants to shift into local time.
  const days = dates.map(value => String(value).slice(0, 10))
    .filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value))
    .map(value => new Date(`${value}T00:00:00Z`))
    .filter(value => Number.isFinite(value.getTime())).sort((a, b) => a - b);
  if (!days.length) return "not available";
  const format = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  return format.formatRange(days[0], days.at(-1)).replace(/\s*–\s*/g, "–");
}

export function checkedTime(value) {
  if (!value || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return "unknown";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZoneName: "short" })
    : "unknown";
}

export function scoreMarkup(data, esc, subName, compact = false) {
  const rows = data.subscriptions || [];
  const categories = data.categories || [];
  const values = categories.map(category => {
    const available = rows.map(row => row.scores?.[category.id])
      .filter(entry => entry?.status === "available" && Number.isFinite(entry.score));
    const text = rows.length === 1 ? scoreText(rows[0].scores?.[category.id])
      : available.length ? `${Math.min(...available.map(x => x.score)).toFixed(1)}–${Math.max(...available.map(x => x.score)).toFixed(1)} / 100 range` : "Not available";
    const [tone, icon] = pillarIcons[category.id] || ["reliability", '<circle cx="12" cy="12" r="8"/>'];
    const low = available.length ? Math.min(...available.map(x => x.score)) : 0;
    const high = available.length ? Math.max(...available.map(x => x.score)) : 0;
    const clamp = value => Math.max(0, Math.min(100, value));
    const status = !available.length ? "Unavailable" : available.length < rows.length ? "Partial coverage" : "Available";
    const number = text.split(" / 100")[0];
    return `<div class="advisor-pillar advisor-pillar--${tone}${available.length ? "" : " advisor-pillar--unavailable"}">
      <div class="advisor-pillar-heading"><span class="advisor-pillar-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icon}</svg></span><strong>${esc(category.label)}</strong></div>
      <div class="advisor-pillar-value">${esc(number)}${available.length ? `<span class="advisor-pillar-scale"> / 100${rows.length > 1 ? " range" : ""}</span>` : ""}</div>
      <div class="advisor-pillar-track" aria-hidden="true">${available.length ? `<span class="advisor-pillar-fill" style="width:${clamp(low)}%"></span>${rows.length > 1 ? `<span class="advisor-pillar-range" style="left:${clamp(low)}%;width:${clamp(high) - clamp(low)}%"></span>` : ""}` : ""}</div>
      <div class="advisor-pillar-coverage"><small>${available.length}/${rows.length} subscriptions</small><small>${status}</small></div>
    </div>`;
  }).join("");
  const issues = rows.filter(row => row.status !== "available").map(row =>
    `${subName(row.subscription_id)}: ${row.message || row.status} (missing pillars are not zero).`);
  const dates = rows.flatMap(row => categories.map(c => row.scores?.[c.id]?.date).filter(Boolean)).sort();
  const metadata = `Azure score dates (UTC reporting days): ${dates.length ? `${dates[0]} – ${dates.at(-1)}` : "not available"}. Checked: ${data.fetched_at || "unknown"}. Subscription scope only. Multiple subscriptions show a range, not an aggregated Advisor score.`;
  return `<section class="card advisor-score-panel">
    <div class="card-head"><div><h3>Azure Advisor Score</h3><span class="advisor-score-subtitle">Five pillars · Official Azure scores</span></div>${compact ? '<button type="button" class="ghost" data-advisor-score-tab>View scores →</button>' : ""}</div>
    <div class="advisor-score-pillars">${values}</div>
    ${compact ? `<footer class="advisor-score-meta">
      <span>${esc(rows.length ? `Scores ${scoreDateRange(dates)}` : "No live subscriptions")}</span>
      <span>· Checked ${esc(checkedTime(data.fetched_at))}</span>
      <details><summary>Per subscription · ranges across subscriptions</summary><div>${esc(metadata)}</div></details>
    </footer>` : `<p class="muted">${esc(rows.length ? `Azure score dates: ${dates.length ? `${dates[0]} – ${dates.at(-1)}` : "not available"}.` : "No live subscriptions available.")} Checked: ${esc(data.fetched_at || "unknown")}.</p>`}
    ${issues.length ? `<p class="advisor-score-issue" role="status">${esc(issues.join(" "))}</p>` : ""}
    ${compact ? "" : `<p class="muted">${esc(data.note || "")}</p>
    <div class="table-scroll"><table><thead><tr><th>Subscription</th><th>Official total</th>${categories.map(c => `<th>${esc(c.label)}</th>`).join("")}<th>Status</th></tr></thead><tbody>${rows.map(row =>
    `<tr><td>${esc(subName(row.subscription_id))}</td><td>${esc(scoreText(row.scores?.Advisor))}</td>${categories.map(c => `<td>${esc(scoreText(row.scores?.[c.id]))}<br><small>${esc(row.scores?.[c.id]?.date || "No score date")}</small></td>`).join("")}<td>${esc(row.status)}</td></tr>`).join("")}</tbody></table></div>`}
    </section>`;
}

export async function mountAdvisorScore(ctx, token, root, compact = false) {
  if (!root) return;
  root.innerHTML = '<section class="card"><h3>Azure Advisor Score</h3><p role="status">Loading five pillars from Azure Advisor…</p></section>';
  try {
    const data = await ctx.get(`/api/dashboard/advisor-score${ctx.qs()}`, { timeout: 60000 });
    if (token !== ctx.loadToken || !root.isConnected) return;
    root.innerHTML = scoreMarkup(data, ctx.esc, ctx.subName, compact);
    const link = root.querySelector("[data-advisor-score-tab]");
    if (link) link.onclick = () => ctx.navigate("advisor-score");
  } catch (error) {
    if (token !== ctx.loadToken || !root.isConnected) return;
    root.innerHTML = `<section class="card"><h3>Azure Advisor Score</h3><p role="status">${ctx.esc(error.message || "Unable to read Advisor scores.")}</p><p>Live Azure access is required; export reporting access does not grant Advisor access.</p></section>`;
  }
}

export function createTabs(ctx) {
  return { renderAdvisorScore: async token => {
    ctx.setContent(ctx.$("tabBody"), '<div id="advisorScore"></div>');
    await mountAdvisorScore(ctx, token, ctx.$("advisorScore"));
  } };
}
