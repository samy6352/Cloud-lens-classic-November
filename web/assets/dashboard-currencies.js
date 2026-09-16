/** Native billing amounts are never combined across currencies. */
import { sumCostsByCurrency } from "./cost-totals.js";

export function nativeAmount(value, currency, format) {
  if (!Number.isFinite(value)) return "Not available";
  return /^[A-Z]{3}$/.test(currency || "")
    ? format(value, currency) : `${value.toLocaleString()} (currency unknown)`;
}

export function hasSectionSpend(section) {
  return Number.isFinite(section?.cost) && section.cost > 0
    || (section?.cost_by_currency || []).some(bucket => Number.isFinite(bucket.cost) && bucket.cost > 0);
}

export function sectionCostLabel(section, format) {
  const buckets = section?.cost_by_currency || [];
  if (buckets.length > 1) return `${buckets.length} currencies`;
  if (buckets.length === 1) return nativeAmount(buckets[0].cost, buckets[0].currency, format);
  return section?.cost == null ? "" : nativeAmount(section.cost, section.currency, format);
}

export function currencyAmountList(buckets, field, format) {
  return Object.entries(buckets || {}).map(([currency, value]) =>
    `${currency} ${nativeAmount(field ? value?.[field] : value, currency, format)}`
  ).join(" · ");
}

export function nativeRowTotal(rows, field, currency, format) {
  return sumCostsByCurrency(rows.map(row => ({ currency: row.currency, cost: row[field] })), currency)
    .map(total => `${total.currency} ${nativeAmount(total.amount, total.currency, format)}${total.missing ? " (partial)" : ""}`)
    .join(" · ") || "Not available";
}
