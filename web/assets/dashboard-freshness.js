/** Response metadata and active-context revalidation. Financial bodies stay in memory only. */
const metadata = new WeakMap();
const excluded = /\/(?:auth|jobs|warehouse|health|whoami|ask)(?:\/|$)|\/(?:ingest|progress|status|resume)(?:\/|$)|\/schedules\/refresh$/;

function sourceUrl(url) {
  const parsed = new URL(url, "http://cloudlens.local");
  if (!parsed.pathname.startsWith("/api/") || excluded.test(parsed.pathname)) return null;
  parsed.searchParams.delete("force");
  parsed.searchParams.delete("refresh");
  parsed.searchParams.sort();
  return parsed.pathname + parsed.search;
}

/** Also usable by Reports' direct-fetch/download paths without changing their JSON shape. */
export function readSnapshotHeaders(headers) {
  const get = name => headers?.get?.(`X-CloudLens-${name}`);
  const cache = get("Cache");
  if (!cache) return null;
  return { cache, asOf: get("As-Of") || null, generation: get("Source-Generation") || null,
    stale: get("Stale") === "true", revalidating: get("Revalidating") === "true",
    error: get("Refresh-Error") ? "source_failed" : null };
}

export function recordSnapshot(data, url, headers, method = "GET") {
  const source = method.toUpperCase() === "GET" && sourceUrl(url);
  const state = readSnapshotHeaders(headers);
  if (source && state && data && typeof data === "object") metadata.set(data, { ...state, url: source });
  return data;
}
export const snapshotOf = data => data && typeof data === "object" ? metadata.get(data) : null;

export function snapshotStatusText(state) {
  if (!state.sources) return "";
  const date = state.asOf && new Date(state.asOf);
  const stamp = date && Number.isFinite(date.getTime()) ? `As of ${date.toLocaleString()}` : "Cached snapshot";
  return `${stamp} · ${state.error ? "Refresh failed; previous data shown"
    : state.exhausted ? "Still updating; previous data shown"
    : state.updating ? "Updating; cached data shown" : "Up to date"}`;
}

export function paintSnapshotStatus(host, state, retry) {
  if (!host) return;
  host.hidden = !state.sources;
  host.textContent = snapshotStatusText(state);
  host.dataset.freshness = state.error ? "error" : state.exhausted ? "paused" : state.updating ? "updating" : "fresh";
  if (state.sources && (state.error || state.exhausted)) {
    const button = host.ownerDocument.createElement("button");
    button.type = "button"; button.className = "linkish"; button.textContent = "Retry refresh";
    button.onclick = retry; host.appendChild(button);
  }
}

export function createSnapshotFlow({ request, isCurrent, onData = () => {}, onRefresh = () => {},
  onStatus = () => {}, document: doc = globalThis.document, schedule = setTimeout, cancel = clearTimeout,
  now = Date.now, delay = 1500, maxAttempts = 24, maxElapsed = 300000 }) {
  const views = new Map(), staged = new Map();
  let owner = null, generation = 0, timer = null, controller = null, running = false;
  let attempts = 0, began = 0, probe = false;
  const sources = () => views.get(owner) || new Map();
  const pending = state => state.stale || state.revalidating || state.error;
  const exhausted = () => attempts >= maxAttempts || (attempts > 0 && now() - began >= maxElapsed);
  const active = mine => mine === generation && owner !== null && isCurrent(owner);
  function emit() {
    const rows = [...sources().values()];
    const stamps = rows.map(row => row.displayAsOf || row.asOf).filter(Boolean).sort();
    onStatus({ sources: rows.length, asOf: stamps[0] || null,
      updating: running || rows.some(pending), error: rows.some(row => row.error), exhausted: exhausted() });
  }
  function plan(immediate = false) {
    if (timer != null || running || doc?.hidden || !active(generation) || exhausted()) return;
    if (!probe && ![...sources().values()].some(pending)) return;
    const failed = [...sources().values()].some(row => row.error);
    const wait = immediate ? 0 : failed ? Math.min(30000, delay * 2 ** Math.min(attempts, 5))
      : Math.min(10000, delay * Math.max(1, attempts));
    timer = schedule(poll, wait);
  }
  async function poll() {
    timer = null;
    const mine = generation, context = owner;
    if (!active(mine) || doc?.hidden || running || exhausted()) return;
    if (!attempts) began = now();
    running = true;
    const ac = controller = new AbortController();
    const all = probe; probe = false; attempts++;
    let changed = false;
    try {
      for (const previous of [...sources().values()]) {
        if (!all && !pending(previous)) continue;
        if (now() - began >= maxElapsed) break;
        if (!active(mine) || doc?.hidden || ac.signal.aborted) return;
        try {
          const data = await request(previous.url, { signal: ac.signal, timeout: 45000 });
          if (!active(mine) || ac.signal.aborted || doc?.hidden) return;
          const state = snapshotOf(data) || { url: previous.url, cache: "live", stale: false, revalidating: false };
          if (Date.parse(sources().get(previous.url)?.asOf) > Date.parse(state.asOf)) continue;
          const different = pending(previous) || state.asOf !== previous.asOf || state.generation !== previous.generation
            || (previous.displayAsOf && previous.displayAsOf !== state.asOf);
          sources().set(previous.url, !pending(state) && different
            ? { ...state, displayAsOf: previous.displayAsOf || previous.asOf } : state);
          if (!pending(state) && different) {
            staged.set(previous.url, data);
            onData(previous.url, data, context);
            changed = true;
          }
        } catch {
          if (!active(mine) || ac.signal.aborted) return;
          sources().set(previous.url, { ...(sources().get(previous.url) || previous), stale: true, error: "read_failed" });
        }
        emit();
      }
      if (changed && active(mine)) {
        try {
          await onRefresh(context);
          if (active(mine)) for (const [url, state] of sources()) {
            const settled = { ...state }; delete settled.displayAsOf; sources().set(url, settled);
          }
        }
        catch {
          if (active(mine)) for (const [url, state] of sources()) {
            sources().set(url, { ...state, stale: true, error: "read_failed" });
          }
        }
        finally { if (active(mine)) staged.clear(); }
      }
    } finally {
      if (active(mine)) {
        running = false; controller = null;
        if (![...sources().values()].some(pending)) { attempts = 0; began = now(); }
        emit(); plan();
      }
    }
  }
  function activate(next) {
    if (next === owner) { emit(); return; }
    generation++; controller?.abort(); controller = null; running = false;
    if (timer != null) cancel(timer);
    timer = null; staged.clear(); owner = next; attempts = 0; began = now();
    probe = Boolean(views.get(next)?.size);
    if (!views.has(next)) views.set(next, new Map());
    while (views.size > 40) views.delete(views.keys().next().value);
    emit(); plan();
  }
  function observe(data, context = owner) {
    const state = snapshotOf(data);
    if (!state || context !== owner || !active(generation)) return data;
    const previous = sources().get(state.url);
    if (Date.parse(previous?.asOf) > Date.parse(state.asOf)) return data;
    sources().set(state.url, running && previous?.displayAsOf
      ? { ...state, displayAsOf: previous.displayAsOf } : state);
    while (sources().size > 40) sources().delete(sources().keys().next().value);
    emit(); plan();
    return data;
  }
  function retry() { attempts = 0; began = now(); probe = true; emit(); plan(true); }
  function visibility() {
    if (doc?.hidden) {
      if (timer != null) cancel(timer);
      timer = null;
      if (controller) { probe = true; controller.abort(); }
    } else plan(true);
  }
  doc?.addEventListener?.("visibilitychange", visibility);
  return { activate, observe, retry,
    take(url) {
      if (/[?&](?:force|refresh)=/.test(url)) return null;
      const key = sourceUrl(url), value = staged.get(key);
      staged.delete(key); return value;
    },
    clear() { activate(null); views.clear(); staged.clear(); },
    stop() { activate(null); doc?.removeEventListener?.("visibilitychange", visibility); },
  };
}

/** Bridge a flow to the shell's existing promise cache, including its cached-hit path. */
export function createViewRevalidation({ request, cache, statusHost, onRefresh, isCurrent, ...options }) {
  const cacheKeys = new Map();
  const flow = createSnapshotFlow({ ...options, request, isCurrent, onRefresh,
    onStatus: state => paintSnapshotStatus(statusHost(), state, () => flow.retry()),
    onData: (url, data) => {
      for (const key of cacheKeys.get(url) || []) {
        if (cache?.has(key)) cache.set(key, { value: Promise.resolve(data) });
      }
    },
  });
  return { ...flow,
    read: (url, opts) => {
      const staged = flow.take(url);
      return staged ? Promise.resolve(staged) : request(url, opts);
    },
    async watch(promise, owner, token, current, key) {
      const data = await promise, state = snapshotOf(data);
      if (state && key) {
        const keys = cacheKeys.get(state.url) || new Set();
        for (const previous of keys) if (cache && !cache.has(previous)) keys.delete(previous);
        keys.add(key); cacheKeys.set(state.url, keys);
        while (cacheKeys.size > 100) cacheKeys.delete(cacheKeys.keys().next().value);
      }
      if (current(token)) flow.observe(data, owner);
      return data;
    },
  };
}
