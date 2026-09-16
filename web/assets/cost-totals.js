/** Sum like currencies only; retain missing-cost evidence alongside each total. */
export function sumCostsByCurrency(rows, fallbackCurrency = "") {
  const groups = new Map();
  for (const row of rows) {
    const currency = String(row.currency || fallbackCurrency).trim().toUpperCase() || "UNKNOWN";
    const group = groups.get(currency) || { currency, amount: 0, recorded: 0, missing: 0 };
    if (Number.isFinite(row.cost)) {
      group.amount += row.cost;
      group.recorded++;
    } else {
      group.missing++;
    }
    groups.set(currency, group);
  }
  return [...groups.values()].map(group => ({
    ...group, amount: group.recorded && group.currency !== "UNKNOWN" ? group.amount : null,
  }))
    .sort((left, right) => left.currency.localeCompare(right.currency));
}
