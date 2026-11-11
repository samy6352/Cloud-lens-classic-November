/** View IDs remain stable for cross-links; only their navigation hierarchy changes. */
export const PRIMARY_SECTIONS = [
  { id: "overview", label: "Overview", icon: "overview", members: ["overview"] },
  { id: "billing", label: "Billing", icon: "billing", members: ["billing", "uptime", "tags", "history", "budgets"] },
  { id: "resources", label: "All resources", icon: "resources", members: ["resources"] },
  { id: "savings", label: "Savings", icon: "savings", members: ["savings", "commitments"] },
  { id: "monitor", label: "Monitor", icon: "performance", members: ["changes", "performance", "anomalies", "azure-updates"] },
  { id: "governance", label: "Governance", icon: "security", members: ["governance", "advisor-score", "esu", "health", "audit"] },
  { id: "reports", label: "Reports", icon: "reports", members: ["report", "settings"] },
];

const LABELS = {
  billing: "Billing summary", tags: "Applications & Tags", savings: "Savings opportunities",
  commitments: "Reservations & Savings Plans", governance: "Compliance",
};

const RAIL_GROUPS = [
  { id: "spend", label: "Spend", icon: "overview", sections: ["overview", "billing", "resources"] },
  { id: "monitor", label: "Monitor", icon: "performance", sections: ["monitor"] },
  { id: "optimize", label: "Optimize", icon: "savings", sections: ["savings"] },
  { id: "govern", label: "Govern", icon: "security", sections: ["governance"] },
  { id: "reports", label: "Reports", icon: "reports", sections: ["reports"] },
];

export function groupedNavigation(views) {
  return RAIL_GROUPS.map(group => ({
    ...group, views: group.sections.flatMap(id => sectionViews(id, views)),
  })).filter(group => group.views.length);
}

export function railGroupFor(viewId, views) {
  const section = sectionFor(viewId, views);
  return RAIL_GROUPS.find(group => group.sections.includes(section?.id))?.id || null;
}

export function sectionFor(viewId, views) {
  const view = views.find(item => item.id === viewId);
  if (!view) return null;
  return PRIMARY_SECTIONS.find(section => section.members.includes(viewId))
    || (view.area ? PRIMARY_SECTIONS[0] : null);
}

export function sectionViews(sectionId, views) {
  const section = PRIMARY_SECTIONS.find(item => item.id === sectionId);
  return (section?.members || []).flatMap(id => {
    const view = views.find(item => item.id === id);
    return view ? [{ ...view, label: LABELS[id] || view.label }] : [];
  });
}

export function sectionDestination(sectionId, views, lastView) {
  const section = PRIMARY_SECTIONS.find(item => item.id === sectionId);
  if (!section) return null;
  if (sectionFor(lastView, views)?.id === sectionId) return lastView;
  return sectionViews(sectionId, views)[0]?.id || null;
}

export function keyboardDestination(ids, focused, key) {
  if (!ids.length) return null;
  const index = Math.max(0, ids.indexOf(focused));
  if (key === "Home") return ids[0];
  if (key === "End") return ids.at(-1);
  if (key === "ArrowRight" || key === "ArrowDown") return ids[(index + 1) % ids.length];
  if (key === "ArrowLeft" || key === "ArrowUp") return ids[(index + ids.length - 1) % ids.length];
  return null;
}
