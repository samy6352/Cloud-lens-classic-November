/** Summary navigation names the period and keeps chart selections in the visible controls. */
export function glanceDestination(index, now = new Date()) {
  const month = offset => new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + offset, 1))
    .toISOString().slice(0, 7);
  return [
    { tab: "overview", month: month(0), target: "trendChart" },
    { tab: "overview", month: month(-1), target: "trendChart" },
    null,
    { tab: "overview", month: month(0), target: "monthOverMonth" },
    { tab: "budgets" },
    { tab: "savings" },
  ][index];
}

export function subscriptionDrillIds(row, subscriptions, scope = []) {
  const visible = subscriptions.filter(sub => !scope.length || scope.includes(sub.id));
  // The cost hierarchy groups by name. Duplicate names therefore drill into all matching
  // subscriptions in the current scope, never an arbitrarily chosen first match.
  return visible.filter(sub => row.id ? sub.id === row.id : (sub.name || sub.id) === row.name)
    .map(sub => sub.id);
}

export function revealOverviewTarget(root, target) {
  const node = [...root.querySelectorAll("[id]")].find(element => element.id === target)
    || [...root.querySelectorAll("h3")].find(heading => heading.textContent === target);
  if (!node) return false;
  for (let parent = node.closest("details"); parent; parent = parent.parentElement?.closest("details")) {
    parent.open = true;
  }
  node.tabIndex = -1;
  node.scrollIntoView({ block: "start" });
  node.focus({ preventScroll: true });
  return true;
}
