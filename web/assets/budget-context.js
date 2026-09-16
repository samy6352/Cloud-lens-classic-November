/** Budget scope is matched from Azure's filter clauses, never from its display name. */
export function matchingTagBudgets(budgets, selection, mode = "all") {
  if (!selection.length) return [];
  const sameKey = (clause, tag) => clause[0].toLowerCase() === tag.key.toLowerCase();
  const overlaps = (clause, tag) => !tag.values?.length || !clause[1].length
    || tag.values.some(value => clause[1].includes(value));
  return (budgets || []).flatMap(budget => {
    if (!budget || budget.filter_exact === false || budget.filtered_exactly === false) return [];
    const clauses = budget.tag_filter;
    if (!Array.isArray(clauses) || !clauses.length || !clauses.every(pair =>
      Array.isArray(pair) && typeof pair[0] === "string" && Array.isArray(pair[1])
      && pair[1].every(value => typeof value === "string"))) return [];
    if (!selection.some(tag => clauses.some(clause => sameKey(clause, tag) && overlaps(clause, tag)))) return [];
    const compatible = tag => clauses.filter(clause => sameKey(clause, tag))
      .every(clause => overlaps(clause, tag));
    if (!(mode === "any" ? selection.some(compatible) : selection.every(compatible))) return [];
    return [{
      budget,
      extra: clauses.filter(clause => !selection.some(tag => sameKey(clause, tag)))
        .map(([key, values]) => ({ key, values })),
      partial: selection.some(tag => tag.values?.length && clauses.some(clause =>
        sameKey(clause, tag) && clause[1].some(value => !tag.values.includes(value)))),
    }];
  }).sort((a, b) => (Number(b.budget.percent_used) || 0) - (Number(a.budget.percent_used) || 0));
}

export function budgetReadIssue(data) {
  if (!data || !Array.isArray(data.budgets)) return "The budget service did not return a readable list.";
  const failed = data.failed_subscriptions || data.unavailable_subscriptions;
  if (data.error || data.partial || data.partial_failure || data.errors?.length
      || (Array.isArray(failed) ? failed.length : Number(failed) > 0)) {
    return typeof data.error === "string" && data.error
      ? data.error : "Some subscriptions could not be read. Budget counts and results may be incomplete.";
  }
  return "";
}
