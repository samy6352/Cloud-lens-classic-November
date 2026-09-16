/** One bounded, abortable polling loop. Refresh is sent once, never on continuation polls. */
export function createSavingsScan({ request, isCurrent, onData, onError, delay = 1200,
  maxPolls = 250, schedule = setTimeout, cancel = clearTimeout }) {
  let controller = null;
  let timer = null;
  let generation = 0;
  function stop() {
    generation++;
    if (timer != null) cancel(timer);
    timer = null;
    controller?.abort();
    controller = null;
  }
  async function start(token, force = false) {
    stop();
    const mine = generation;
    controller = new AbortController();
    const signal = controller.signal;
    let polls = 0;
    async function poll(refresh) {
      if (mine !== generation || !isCurrent(token)) return;
      try {
        const data = await request({ refresh, signal });
        if (mine !== generation || !isCurrent(token)) return;
        onData(data, token);
        if (data.scan?.status === "running" || data.freshness?.refreshing) {
          if (++polls >= maxPolls) {
            onError(new Error("The scan is still running. Refresh scan to check again."), token);
            return;
          }
          timer = schedule(() => poll(false), delay);
        }
      } catch (err) {
        if (mine !== generation || signal.aborted || !isCurrent(token)) return;
        onError(err, token);
      }
    }
    return poll(force);
  }
  return { start, stop };
}

/** Local presentation only: neither this clock nor its callback starts a scan. */
export function createSavingsClock({ isCurrent, onTick, interval = 1000,
  schedule = setTimeout, cancel = clearTimeout }) {
  let timer = null;
  let generation = 0;
  function stop() {
    generation++;
    if (timer != null) cancel(timer);
    timer = null;
  }
  function start(token) {
    stop();
    const mine = generation;
    const tick = () => {
      if (mine !== generation) return;
      timer = null;
      if (!isCurrent(token)) return;
      onTick(token);
      if (mine === generation && isCurrent(token)) timer = schedule(tick, interval);
    };
    tick();
  }
  return { start, stop };
}

/** Derive status from the latest scan metadata, not a cached payload's unavailable list. */
export function savingsPresentation(data = {}, error = null, now = Date.now()) {
  const freshness = data.freshness || {};
  const progress = data.scan || { status: "running", completed: [], total: 4 };
  const sourceErrors = Object.entries(progress.errors || {});
  const sourceNames = sourceErrors.map(([name]) => name).join(", ");
  const warning = sourceErrors.map(([name, reason]) => `${name}: ${reason || "source failed"}`).join("; ");
  const failed = error || (progress.status === "error" ? warning || "One or more sources failed." : "");
  const generated = freshness.generated_at;
  const stamp = generated ? new Date(generated) : null;
  const validStamp = stamp && Number.isFinite(stamp.getTime());
  const age = validStamp ? Math.max(0, Math.floor((now - stamp.getTime()) / 1000))
    : freshness.age_seconds;
  const ageText = age == null ? "" : age < 60 ? `${Math.floor(age)}s ago`
    : age < 3600 ? `${Math.floor(age / 60)}m ago` : `${Math.floor(age / 3600)}h ago`;
  const maxAge = freshness.max_age_seconds;
  const stale = Boolean(freshness.stale)
    || (Number.isFinite(maxAge) && age != null && age >= maxAge);
  const running = !failed && (progress.status === "running" || Boolean(freshness.refreshing));
  let state = failed ? `Refresh failed${(data.opportunities || []).length ? " — previous results kept" : ""}`
    : running ? `${freshness.cached && stale ? "Stale cached results — refreshing"
      : freshness.cached ? "Refreshing cached results" : "Scanning"} · ${(progress.completed || []).length}/${progress.total || 4} sources complete`
    : warning ? `Scan complete with warnings — unavailable: ${sourceNames}${stale ? " · stale results" : ""}`
    : stale ? "Stale results — refresh needed"
    : freshness.cached ? "Cached results" : "Scan complete";
  if (running && warning) state += ` · warnings: ${sourceNames}`;
  return { generated, stamp, validStamp, age, ageText, stale, running, state, failed, warning,
    provisional: !freshness.cached && (progress.status === "running" || Boolean(freshness.refreshing)) };
}
