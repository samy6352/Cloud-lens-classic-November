/**
 * Billing-only estimates never establish workload availability or shutdown eligibility.
 *
 * Two independent judgements have to agree before a row is offered for review:
 *
 *   1. The server identifies a VM compute meter whose billed hours can be reduced.
 *   2. The row is the resource itself, not something billed alongside it. A licence meter, a VM
 *      extension and an unparseable id all arrive with the same service name as the thing they
 *      belong to, and counting them would inflate the estimate with money that stopping the
 *      resource does not release.
 *
 * The result is `needs_review`, never an instruction: billing data cannot prove a workload is
 * safe to stop, so the saving is a ceiling for a human to check, not a recommendation.
 */

const VM = /^\/subscriptions\/[^/]+\/resourceGroups\/[^/]+\/providers\/Microsoft\.Compute\/virtualMachines\/[^/]+$/i;

export function scheduleAssessment(row) {
  if (!row || row.schedulable !== true || row.schedule_status === "not_assessed") return "not_assessed";
  return VM.test(row.id || "") ? "needs_review" : "not_assessed";
}

export function illustrativeSaving(row, weeklyHours) {
  if (scheduleAssessment(row) !== "needs_review" || !Number.isFinite(row.cost) || row.cost <= 0
    || !Number.isFinite(row.uptime_pct) || row.uptime_pct <= 0 || row.uptime_pct > 100
    || !Number.isFinite(weeklyHours) || weeklyHours < 0 || weeklyHours > 168) return 0;
  const billedWeek = row.uptime_pct / 100 * 168;
  return billedWeek > weeklyHours ? row.cost * (1 - weeklyHours / billedWeek) : 0;
}
