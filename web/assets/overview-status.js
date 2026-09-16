/** Keep unavailable budget evidence separate from confirmed absence. */
export function budgetPresentation(overview) {
  const budget = overview.budget;
  const hasReading = Number.isFinite(budget?.percent_used);
  const status = overview.budget_status;
  if (hasReading) {
    const complete = status === "ok";
    const name = budget.over > 1 ? `${budget.over} budgets over` : budget.name;
    return {
      value: `${budget.percent_used}%`,
      sub: complete ? name : `${name || "Available budget"} · incomplete coverage`,
      tone: budget.status === "over" || budget.status === "near" || !complete ? "warn" : "",
      percent: budget.percent_used,
    };
  }
  if (status === "none" && !budget) {
    return { value: "none set", sub: "confirmed in selected scope", tone: "" };
  }
  return {
    value: status === "unknown" ? "Unknown" : "Unavailable",
    sub: overview.budget_note || "Budget coverage could not be confirmed",
    tone: "warn",
  };
}
