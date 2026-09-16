/** Separate navigation feedback, module, API and commit timings in the browser Performance panel. */
export function beginNavigation(tab, cached = false) {
  const started = performance.now();
  const record = { tab, cached, started, api_ms: 0, render_ms: 0, module_ms: 0 };
  performance.mark(`cloudlens:${tab}:start`);
  return record;
}

export function measurePhase(record, phase, started) {
  if (record) record[`${phase}_ms`] += performance.now() - started;
}

export function navigationFeedback(record) {
  record.feedback_ms = performance.now() - record.started;
}

export function finishNavigation(record) {
  record.total_ms = performance.now() - record.started;
  record.kind = record.cached ? "cached" : "initial";
  performance.measure(`cloudlens:${record.tab}:${record.kind}`, {
    start: record.started, end: performance.now(), detail: { ...record },
  });
  window.dispatchEvent(new CustomEvent("cl:dashboard-performance", { detail: { ...record } }));
  return record;
}

export function reportReadiness(ready, error = null) {
  document.documentElement.dataset.dashboardReady = ready ? "true" : "error";
  // The boot skeleton holds space for chrome that has not been built yet. Once a board has
  // rendered -- or failed to -- that space is occupied by the real thing, and keeping the
  // reservation would leave an empty strip below the summary for the rest of the session.
  document.documentElement.classList.remove("cl-booting");
  window.dispatchEvent(new CustomEvent(ready ? "cl:dashboard-ready" : "cl:dashboard-error", {
    detail: ready ? null : { error: error || "The dashboard could not render this view." },
  }));
}
