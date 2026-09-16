/** A calendar-month statement and current optimization estimates, deliberately kept separate. */
import { sumCostsByCurrency } from "./cost-totals.js";
const finite = value => typeof value === "number" && Number.isFinite(value);
const rowsOf = value => Array.isArray(value) ? value : value?.items || [];

export function createTabs(ctx) {
  const { $, esc, get, setContent, money, table, wireTables, drawChart, portalLinkMarkup, portalTargets } = ctx;
  let statement = null;
  let outlook = null;
  let statementError = null;
  let outlookError = null;
  let statementLoading = false;
  let outlookLoading = false;
  let breakdown = "subscriptions";
  let generation = 0;
  let controllers = [];
  const active = (token, version = generation) => token === ctx.loadToken && version === generation;
  const amount = (value, currency) => !finite(value) ? "Not available"
    : /^[A-Z]{3}$/.test(currency || "") ? money(value, currency, 2)
    : `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} (currency unknown)`;
  const percent = value => finite(value) ? `${value.toFixed(1)}%` : "Not available";
  const monthName = value => {
    const date = /^\d{4}-\d{2}$/.test(value || "") ? new Date(`${value}-01T00:00:00Z`) : null;
    return date ? date.toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" }) : value || "";
  };
  const dateLabel = value => {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime())
      ? date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "Not available";
  };
  const checkedTime = value => {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime()) ? date.toLocaleString(undefined,
      { dateStyle: "medium", timeStyle: "short" }) : "Not available";
  };
  const list = value => Array.isArray(value) ? value : value ? [value] : [];
  const metric = name => statement?.kpis?.[name];
  const currency = () => statement?.currency || "";
  const basis = () => typeof statement?.spend_basis === "object" ? statement.spend_basis
    : { code: statement?.spend_basis };
  const spendMetric = () => ["billed_cost", "effective_cost", "recorded_cost"].includes(basis().metric)
    ? basis().metric : finite(metric("billed_cost")) ? "billed_cost" : "recorded_cost";
  const warningText = value => {
    if (typeof value === "string") return value;
    const source = /Microsoft\.Capacity/i.test(value?.source || "") ? "Reservations"
      : /Microsoft\.BillingBenefits/i.test(value?.source || "") ? "Savings Plans" : value?.source;
    const reason = value?.reason || value?.message || value?.error || value?.detail
      || (value?.fields?.length ? `Unavailable metadata: ${value.fields.join(", ")}` : "");
    return [source, reason].filter(Boolean).join(": ") || "Some source details are unavailable.";
  };
  const priceNote = () => {
    const pricing = statement?.pricing;
    if (statement?.fx?.complete === false) return "A complete display-currency conversion is unavailable. Review native-currency totals below; no mixed-currency total is inferred.";
    if (pricing?.status === "complete") return "Measured on comparable usage, after amortized commitment costs.";
    if (pricing?.status === "partial") return `${pricing.covered_rows ?? 0} of ${pricing.total_rows ?? "unknown"} usage rows have complete prices. Some savings metrics remain unavailable.`;
    return "List, contracted and effective price evidence is missing. A FOCUS report refresh is needed to prove savings.";
  };

  function requestQuery(live = false, refresh = false) {
    const original = new URLSearchParams(ctx.qs().slice(1));
    const query = live ? new URLSearchParams() : original;
    query.delete("days");
    if (live) {
      if (original.get("scope")) query.set("scope", original.get("scope"));
      if (original.get("currency")) query.set("currency", original.get("currency"));
      if (refresh) query.set("refresh", "true");
    }
    return query.size ? `?${query}` : "";
  }

  function paintAnswers() {
    const cur = currency();
    const noRows = statement?.empty || statement?.coverage?.current?.rows === 0;
    const spend = metric(spendMetric());
    const spendLabel = basis().label || (spendMetric() === "billed_cost" ? "Total billed cost"
      : basis().code === "amortized" ? "Amortized spend" : "Recorded spend");
    const potential = outlook?.potential || {};
    const saved = metric("actual_savings");
    const extra = finite(potential.monthly) ? potential.monthly : potential.monthly_savings;
    const annual = finite(potential.annual) ? potential.annual : potential.annual_savings;
    const partial = potential.partial || outlook?.status === "partial";
    setContent($("billSpent"), `<span class="bill-step">01 / SPEND</span>
      <h3>${esc(spendLabel)}</h3>
      <strong class="bill-value">${esc(!statement ? statementError ? "Unavailable" : "Loading..." : noRows ? "Not loaded" : amount(spend, cur))}</strong>
      <p>Usage at list price: <strong>${esc(amount(metric("list_cost"), cur))}</strong></p>
      ${statement && spendMetric() !== "billed_cost" ? '<span class="bill-answer-note bill-caution">Not a verified invoice total.</span>' : ""}`);
    setContent($("billSaved"), `<span class="bill-step">02 / SAVINGS</span>
      <h3>Savings vs list</h3>
      <strong class="bill-value${!finite(saved) ? " bill-unavailable" : saved < 0 ? " bill-negative" : ""}">${esc(!statement ? statementError ? "Unavailable" : "Loading..." : amount(saved, cur))}</strong>
      <p>Effective discount: <strong>${esc(percent(metric("effective_discount_pct")))}</strong></p>
      <span class="bill-answer-note">${esc(!statement ? statementError ? "Statement unavailable; see notice above." : "Awaiting price evidence."
        : statement.fx?.complete === false ? "FX incomplete; see native totals below."
        : statement.pricing?.status === "complete" ? "Net of amortized commitment costs"
        : statement.pricing?.status === "partial" ? "Partial pricing; see coverage above."
        : "Price evidence missing; see notice above.")}</span>`);
    setContent($("billPotential"), `<span class="bill-step">03 / POTENTIAL</span>
      <h3>Potential savings</h3>
      <strong class="bill-value">${esc(outlookError && !outlook ? "Unavailable" : !outlook ? "Scanning..." : amount(extra, potential.currency || cur))}</strong>
      <p>per month${finite(annual) ? ` · <strong>${esc(amount(annual, potential.currency || cur))}</strong> per year` : ""}</p>
      <span class="bill-answer-note">${esc(partial ? "Partial estimate · not achieved savings"
        : "Current estimate · not achieved savings")}</span>
      ${outlookLoading && outlook ? '<span class="bill-loading-note">Refreshing · previous estimates shown</span>' : ""}`);
    const button = $("billRefresh");
    if (button) {
      button.disabled = statementLoading || outlookLoading;
      button.textContent = button.disabled ? "Refreshing..." : "Refresh Billing";
    }
  }

  function paintPeriod() {
    const period = statement?.period || {};
    const current = statement?.coverage?.current || {};
    setContent($("billPeriod"), `<div class="bill-period-meta"><strong>${esc(period.label || monthName(period.month) || "Last calendar month")}</strong>
      <span class="muted">${period.days ? `${esc(period.days)} calendar days` : "A complete calendar month"}${period.previous_month ? ` · compared with ${esc(monthName(period.previous_month))}` : ""}</span></div>
      <button type="button" class="ghost" id="billRefresh"${statementLoading || outlookLoading ? " disabled" : ""}>Refresh Billing</button>`);
    $("billRefresh").onclick = () => loadAll(ctx.loadToken, true);
    const warning = (statementError ? `${statementError}${statement ? " Last loaded statement shown." : ""}` : "") || (statement?.empty || current.rows === 0
      ? "No billing rows are loaded for this calendar month. This is not evidence of zero spend."
      : statement?.pricing?.status !== "complete" && statement
        ? priceNote()
        : null);
    setContent($("billCoverage"), `${warning ? `<div class="bill-notice">${esc(warning)}
      ${statement && !statementError && statement.fx?.complete !== false ? '<button type="button" class="linkish" data-billing-tab="settings">Review cost data sources</button>' : ""}</div>` : ""}
      <p class="bill-period-note">Calendar-month statement · recommendations use current subscription-wide signals.</p>`);
  }

  function paintFinancials(token) {
    const host = $("billFinancials");
    const chips = $("billDiscounts");
    if (!statement) {
      setContent(chips, "");
      setContent(host, `<p class="muted">${esc(statementError || "Loading last month's statement...")}</p>`);
      return;
    }
    const cur = currency();
    const components = [
      ["Azure discounts", metric("azure_discounts")],
      ["Reserved Instance savings", metric("reservation_savings")],
      ["Savings Plan savings", metric("savings_plan_savings")],
      ["Other commitment savings", metric("other_commitment_savings")],
      ["Other usage savings", metric("non_commitment_savings")],
    ].filter(([, value], index) => index < 3 || (finite(value) && value !== 0));
    const mom = statement.month_over_month || {};
    setContent(chips, components.map(([label, value]) => `<div class="bill-chip"><span>${esc(label)}</span><strong>${esc(amount(value, cur))}</strong></div>`).join(""));
    setContent(host, `<div class="bill-section-title"><div><h3>Savings &amp; spend trends</h3></div></div>
      <div class="grid-2 bill-chart-grid">
        <div class="card chart-card" id="billSavingsChart"></div>
        <div class="card chart-card" id="billMomChart"></div>
      </div>
      <p class="bill-period-note">${esc(finite(mom.change_pct)
        ? `${mom.change_pct > 0 ? "Spend increased" : mom.change_pct < 0 ? "Spend decreased" : "Spend was unchanged"} ${Math.abs(mom.change_pct).toFixed(1)}% against ${monthName(statement.period?.previous_month)}.`
        : "A percentage comparison needs a comparable, non-zero prior month.")}</p>`);
    const known = components.filter(([, value]) => finite(value));
    if (known.length) {
      drawChart($("billSavingsChart"), { type: "hbar", title: "Savings by source",
        labels: known.map(([label]) => label), datasets: [{ label: "Savings vs list", data: known.map(([, value]) => value) }],
        currency: cur || undefined, note: "Price benefits on comparable usage; unused commitment allocations reduce net savings." });
    } else {
      setContent($("billSavingsChart"), `<h3>Savings by source</h3><p class="bill-empty">
        Pricing attribution unavailable. Refresh FOCUS data for list, contracted and effective costs.
        Missing prices are not treated as savings.</p>`);
    }
    const labels = statement.trend?.labels || [statement.period?.previous_month, statement.period?.month];
    const values = statement.trend?.values || [mom.previous, mom.current];
    if (labels.length === values.length && values.length && values.every(finite) && mom.comparable !== false) {
      drawChart($("billMomChart"), { type: "bar", title: "Month-over-month spend",
        labels: labels.map(monthName), datasets: [{ label: basis().label || "Recorded cost", data: values }],
        currency: cur || undefined, note: mom.note || "Calendar months; differences in month length and data coverage can affect this comparison." });
    } else {
      const bases = statement.trend?.bases || [];
      setContent($("billMomChart"), `<h3>Month-over-month spend</h3><div class="bill-month-totals">
        ${labels.map((label, index) => `<div><span>${esc(monthName(label))}</span>
          <strong>${esc(amount(values[index], cur))}</strong>
          <span>${esc(bases[index] === "billed_cost" ? "Billed cost" : "Recorded cost")}</span></div>`).join("")}</div>
        <p class="bill-empty">These totals are not directly comparable: coverage, currency or cost basis is incomplete or differs. No percentage change is inferred.</p>`);
    }
    if (active(token)) paintBreakdown();
  }

  function paintBreakdown() {
    const rows = rowsOf(statement?.breakdowns?.[breakdown]);
    const cur = currency();
    const title = { subscriptions: "By subscription", services: "By service", resources: "By resource" }[breakdown];
    const total = metric(spendMetric());
    const columns = [{ label: breakdown === "resources" ? "Resource" : breakdown === "services" ? "Service" : "Subscription",
      value: row => row.name || row.id || "", cell: row => portalLinkMarkup(row, ctx.portalScopeIds(), row.name || row.id || "Other") }];
    if (breakdown === "resources") columns.push({ label: "Resource group", cell: row => esc(row.resource_group || "—") });
    if (new Set(rows.map(row => row.currency).filter(Boolean)).size > 1 || statement?.fx?.complete === false) {
      columns.push({ label: "Currency", cell: row => esc(row.currency || "Unknown") });
    }
    columns.push({ label: "Cost", num: true, value: row => row.cost,
      cell: row => esc(amount(row.cost, row.currency || cur)),
      total: matchingRows => sumCostsByCurrency(matchingRows, cur).map(item =>
        `${esc(amount(item.amount, item.currency))}${item.missing ? " (partial)" : ""}`
      ).join(" · ") || "No matching costs" });
    columns.push({ label: "Share", num: true, value: row => row.share_pct,
      cell: row => esc(percent(finite(row.share_pct) ? row.share_pct : finite(total) && total !== 0 ? row.cost / total * 100 : null)) });
    setContent($("billBreakdown"), `<div class="bill-section-title"><div><h3>Cost breakdown</h3></div>
      <div class="bill-segments" role="group" aria-label="Billing cost breakdown">
      ${["subscriptions", "services", "resources"].map(value => `<button type="button" class="ghost" data-bill-breakdown="${value}" aria-pressed="${breakdown === value}">${esc(value[0].toUpperCase() + value.slice(1))}</button>`).join("")}
      </div></div>${table(title, rows, columns, "Select a row for details and Azure Portal links.", { pageSize: 8 })}`);
    for (const button of $("billBreakdown").querySelectorAll("[data-bill-breakdown]")) {
      button.onclick = () => { breakdown = button.dataset.billBreakdown; paintBreakdown(); };
    }
    wireTables($("billBreakdown"));
  }

  function paintOutlook() {
    const potential = outlook?.potential || {};
    const opportunities = rowsOf(outlook?.opportunities);
    const warnings = [...new Set([...list(outlook?.warnings), outlookError].filter(Boolean).map(warningText))];
    setContent($("billOpportunities"), `<div class="bill-section-title"><div><h3>Savings opportunities</h3></div>
      <span class="muted">${outlookLoading ? "Reading current Azure signals..." : "Current estimates · not achieved savings"}</span></div>
      ${warnings.length ? `<div class="bill-notice">${warnings.map(warning => `<p>${esc(warning)}</p>`).join("")}</div>` : ""}
      ${potential.overlap_note ? `<p class="bill-period-note">${esc(potential.overlap_note)}</p>` : ""}
      ${outlook?.freshness?.checked_at ? `<p class="bill-period-note">${outlook.cache?.cached ? "Cached estimates" : "Estimates"} checked ${esc(checkedTime(outlook.freshness.checked_at))} · ${esc(outlook.freshness.window_days || 30)}-day source window. ${esc(outlook.scope_note || "")}</p>` : ""}
      <div class="bill-opportunity-list">${opportunities.slice(0, 6).map((item, index) => opportunityCard(item, index)).join("")}</div>
      ${opportunities.length > 6 ? `<details class="bill-more" id="billMoreOpportunities"><summary>${opportunities.length - 6} more opportunities</summary>
        <div class="bill-opportunity-list">${opportunities.slice(6).map((item, index) => opportunityCard(item, index + 6)).join("")}</div></details>` : ""}
      ${!opportunities.length ? `<p class="bill-empty">${esc(outlookLoading ? "The billing statement remains usable while the optimization sources load."
        : outlookError || outlook?.status === "error" ? "Optimization sources could not be fully read. This does not establish that no savings exist."
        : outlook ? "No additional opportunities were identified by the available sources." : "Waiting for optimization sources.")}</p>` : ""}`);
    paintCommitments();
    paintNotes();
  }

  function opportunityCard(item, index) {
    const cur = item.currency || currency();
    const allowedTargets = new Set(["commitments", "savings", "resources", "esu", "governance"]);
    const target = allowedTargets.has(item.related_tab) ? item.related_tab
      : /reservation|savings plan/i.test(item.action || "") ? "commitments" : "savings";
    const links = portalTargets(item, ctx.portalScopeIds());
    const subscription = ctx.allSubscriptions().find(sub => sub.id === item.subscription_id)?.name || item.subscription_id;
    const resource = item.resource_name || item.resource_id?.split("/").pop();
    const location = [resource !== item.subscription_id ? resource : null,
      item.resource_group, item.region, subscription].filter(Boolean).join(" · ");
    return `<article class="bill-opportunity" data-state-key="${esc(item.id || `opportunity-${index}`)}">
      <div><span class="bill-recommendation">${esc(item.action || "Review opportunity")}</span>
        <h4>${esc(item.title || item.resource_id || "Savings opportunity")}</h4>
        ${location ? `<span class="bill-target">${esc(location)}</span>` : ""}
        <p>${esc(item.reason || item.description || "Review the source evidence before acting.")}</p>
        <span class="muted">${esc([item.basis, item.confidence ? `${item.confidence} confidence` : ""].filter(Boolean).join(" · "))}</span>
        ${item.included_in_total === false ? `<p class="bill-caution">${esc(item.exclusion_reason || "Alternative or non-monetary action: not added to the headline total.")}</p>` : ""}
      </div><div class="bill-opportunity-value"><strong>${esc(amount(item.monthly_savings, cur))}</strong><span>estimated / month</span>
        <span>${esc(amount(item.annual_savings, cur))} / year</span>
        <button type="button" class="ghost" data-billing-tab="${esc(target)}">Review recommendation</button>
        ${links[0] ? `<a href="${esc(links[0].url)}" target="_blank" rel="noopener noreferrer">Open ${esc(links[0].label.toLowerCase())} in Azure ↗</a>` : ""}
      </div></article>`;
  }

  function paintCommitments() {
    const inventory = outlook?.commitments || {};
    const items = rowsOf(inventory.items || inventory);
    const issues = items.filter(item => (finite(item.utilization_pct) && item.utilization_pct < 80)
      || (finite(item.days_to_expiry) && item.days_to_expiry <= 90)
      || /underutil|unused|expir|idle/i.test(item.state || ""));
    const unavailable = [...new Set(list(inventory.unavailable).map(warningText))];
    const utilizationPeriod = item => typeof item.utilization_period === "object" && item.utilization_period
      ? `${item.utilization_period.days}-day provider utilization aggregate. ${item.utilization_period.basis || ""}`
      : item.utilization_period || "";
    const card = item => `<article class="bill-commitment">
      <div><strong>${esc(item.name || item.id || "Commitment")}</strong>
        <span>${esc(item.kind || "Commitment")}${item.state ? ` · ${esc(item.state)}` : ""}</span></div>
      <dl><div><dt>Utilization</dt><dd>${esc(percent(item.utilization_pct))}</dd></div>
        <div><dt>Expiry</dt><dd>${esc(dateLabel(item.expires_on))}</dd></div>
        <div><dt>Observed unused cost / month</dt><dd>${esc(amount(item.monthly_unused_cost, item.currency || currency()))}</dd></div></dl>
      <div class="bill-watch-signals">
        ${finite(item.utilization_pct) && item.utilization_pct < 80 ? '<span>Underutilized</span>' : ""}
        ${finite(item.days_to_expiry) && item.days_to_expiry >= 0 && item.days_to_expiry <= 90 ? `<span>Expires in ${esc(item.days_to_expiry)} days</span>` : ""}
        ${finite(item.days_to_expiry) && item.days_to_expiry < 0 ? '<span>Expired</span>' : ""}
      </div>
      ${item.utilization_period || item.source ? `<p class="muted">${esc([utilizationPeriod(item), item.source].filter(Boolean).join(" · "))}</p>` : ""}
      <button type="button" class="linkish" data-billing-tab="commitments">Review commitment</button></article>`;
    setContent($("billCommitments"), `<div class="bill-section-title"><div><h3>Commitment watch</h3></div></div>
      <p class="bill-period-note">Reservations & Savings Plans · utilization below 80% or expiry within 90 days. Unknown values are not inferred from billing dates.</p>
      ${unavailable.length ? `<div class="bill-notice">${unavailable.map(value => `<p>${esc(value)}</p>`).join("")}</div>` : ""}
      ${inventory.note || inventory.coverage?.note ? `<p class="bill-period-note">${esc(inventory.note || inventory.coverage.note)}</p>` : ""}
      ${list(inventory.notices).length ? `<p class="bill-period-note">${esc(list(inventory.notices).join(" "))}</p>` : ""}
      <div class="bill-commitment-list">${issues.slice(0, 6).map(card).join("")}</div>
      ${!issues.length ? `<p class="bill-empty">${esc(outlookLoading ? "Checking readable commitment information..."
        : unavailable.length || inventory.partial || outlookError || !outlook ? "Commitment lifecycle coverage is incomplete; no all-clear is implied."
        : items.length ? "No confirmed issue was found among the readable commitments. Some fields may still be unavailable."
        : "No attributable commitment inventory was returned by the available sources.")}</p>` : ""}
      ${items.length ? `<details class="bill-more" id="billAllCommitments"><summary>All ${items.length} observed commitments</summary>
        <div class="bill-commitment-list">${items.map(card).join("")}</div></details>` : ""}`);
  }

  function paintNotes() {
    const warnings = list(statement?.warnings);
    const definitions = list(statement?.definitions);
    const coverage = statement?.coverage || {};
    const perCurrency = rowsOf(statement?.per_currency);
    setContent($("billNotes"), `
      ${warnings.map(warning => `<p class="bill-caution">${esc(warningText(warning))}</p>`).join("")}
      <p class="bill-period-note">Savings vs list compares list and effective costs for the same priced usage, not operational savings or list price minus an invoice.</p>
      ${statement?.pricing?.note ? `<p class="bill-period-note">${esc(statement.pricing.note)}</p>` : ""}
      ${statement?.pricing?.status === "partial" && finite(statement.pricing.reconciled_totals?.actual_savings)
        ? `<p><strong>Fully priced subset:</strong> ${esc(amount(statement.pricing.reconciled_totals.actual_savings, currency()))} net savings on
          ${esc(statement.pricing.covered_rows)} of ${esc(statement.pricing.total_rows)} comparable usage rows. This is not a whole-month savings total.</p>` : ""}
      ${statement?.fx?.note ? `<p class="bill-period-note">${esc(statement.fx.note)}</p>` : ""}
      ${statement?.fx?.rates?.some(rate => rate.from !== rate.to) ? `<ul>${statement.fx.rates
        .filter(rate => rate.from !== rate.to).map(rate => `<li>${esc(rate.from || "Unknown")} → ${esc(rate.to || "Unknown")}: ${finite(rate.rate) ? esc(rate.rate) : "rate unavailable"}
        ${rate.as_of ? ` · ${esc(dateLabel(rate.as_of))}` : ""}${rate.source ? ` · ${esc(rate.source)}` : ""}</li>`).join("")}</ul>` : ""}
      ${["current", "previous"].map(key => coverage[key] ? `<p class="bill-period-note"><strong>${key === "current" ? "Statement month" : "Previous month"}:</strong>
        ${esc(coverage[key].rows ?? "Unknown")} rows, ${esc(coverage[key].days_present ?? "unknown")} of ${esc(coverage[key].calendar_days ?? "unknown")} calendar days represented.
        Missing days do not imply zero cost.</p>` : "").join("")}
      ${perCurrency.length > 1 ? `<p>Native currencies are shown separately unless a supported display-currency conversion is available.</p>
        <ul>${perCurrency.map(item => `<li>${esc(item.currency)}: ${esc(amount(item.kpis?.[item.spend_basis?.metric || "recorded_cost"] ?? item.recorded_cost ?? item.cost, item.currency))}</li>`).join("")}</ul>` : ""}
      <p class="bill-period-note">Estimates are not achieved savings. Review workload constraints, overlapping alternatives, licensing and commercial terms before acting. This page makes no purchases or resource changes.</p>
      <details class="card bill-methods view-help" id="billMethods" data-state-key="billing-about">
        <summary>About this view</summary>
        <div><p>The statement covers the calendar month shown. Choose a month in the cost-period
          filter to change it; rolling days use the previous calendar month. Subscription, currency
          and cost filters apply to the statement. Recommendations use current subscription-wide signals.</p>
        <p>Billed charges can include upfront commitment purchases, credits and refunds.
          Savings vs list compares list and effective costs for the same priced usage, not
          list price minus a cash invoice.</p>
        <dl class="bill-definitions">${definitions.map(item => `<div><dt>${esc(item.label || item.name || "")}</dt><dd>${esc(item.explanation || item.description || "")}</dd></div>`).join("")}</dl>
        </div>
      </details>`);
  }

  async function loadStatement(token) {
    const version = generation;
    const controller = new AbortController();
    controllers.push(controller);
    statementLoading = true;
    statementError = null;
    paintPeriod();
    paintAnswers();
    try {
      const data = await get(`/api/dashboard/billing${requestQuery()}`, { signal: controller.signal, timeout: 45000 });
      if (!active(token, version) || controller.signal.aborted) return;
      if (!data.period || !data.kpis || !data.breakdowns) throw new Error("The Billing statement response is incomplete.");
      statement = data;
    } catch (error) {
      if (!active(token, version) || controller.signal.aborted) return;
      statementError = error?.message || "Could not load the Billing statement.";
    } finally {
      if (active(token, version) && !controller.signal.aborted) {
        statementLoading = false;
        paintPeriod(); paintAnswers(); paintFinancials(token); paintNotes();
      }
    }
  }

  async function loadOutlook(token, refresh) {
    const version = generation;
    const controller = new AbortController();
    controllers.push(controller);
    outlookLoading = true;
    outlookError = null;
    paintAnswers();
    paintOutlook();
    try {
      const data = await get(`/api/dashboard/billing/opportunities${requestQuery(true, refresh)}`,
        { signal: controller.signal, timeout: 180000 });
      if (!active(token, version) || controller.signal.aborted) return;
      if (!data.potential || !data.commitments) throw new Error("The optimization response is incomplete.");
      outlook = data;
      if (data.status === "error") outlookError = data.error || "The optimization sources are unavailable.";
    } catch (error) {
      if (!active(token, version) || controller.signal.aborted) return;
      outlookError = error?.message || "Could not load current savings opportunities.";
    } finally {
      if (active(token, version) && !controller.signal.aborted) {
        outlookLoading = false;
        paintAnswers(); paintOutlook();
      }
    }
  }

  function loadAll(token, refresh = false) {
    for (const controller of controllers) controller.abort();
    controllers = [];
    generation++;
    return Promise.all([loadStatement(token), loadOutlook(token, refresh)]);
  }

  async function renderBilling(token) {
    setContent($("tabBody"), `<section class="portal-workbench bill-summary" aria-label="Billing summary">
      <div class="bill-period" id="billPeriod"></div><div id="billCoverage"></div>
      <div class="bill-answers"><article class="bill-answer" id="billSpent"></article>
        <article class="bill-answer bill-saved" id="billSaved"></article>
        <article class="bill-answer bill-potential" id="billPotential"></article>
        <div class="bill-answers-more" id="billDiscounts"></div></div></section>
      <section id="billFinancials"></section><section id="billBreakdown"></section>
      <section id="billOpportunities"></section><section id="billCommitments"></section><section id="billNotes"></section>`);
    $("tabBody").onclick = event => {
      const action = event.target.closest("[data-billing-tab]");
      if (action) ctx.selectTab(action.dataset.billingTab);
    };
    paintPeriod(); paintAnswers(); paintFinancials(token); paintOutlook();
    await loadAll(token);
  }

  return {
    renderBilling,
    stop() { generation++; controllers.forEach(controller => controller.abort()); controllers = []; },
    resume(token, tab) {
      if (tab !== "billing") return;
      // Only resume interrupted requests; completed results wait for Refresh Billing.
      if (statementLoading) loadStatement(token);
      if (outlookLoading) loadOutlook(token, false);
    },
  };
}
