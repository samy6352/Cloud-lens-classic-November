// Browser-owned, memory-only hints. Authentication remains entirely in normal fetch/server code.
const PATHS = new Set([
  "/api/dashboard/sections", "/api/dashboard/executive", "/api/dashboard/billing",
  "/api/dashboard/resources", "/api/filters",
]);
const PARAMS = new Set([
  "days", "scope", "currency", "month", "period_month",
  "rg", "service", "region", "charge", "pricing", "env",
]);
const SECRET = /(?:bearer\s|eyJ[\w-]+\.[\w-]+\.[\w-]+|(?:token|password|secret|sig|cookie|session|authorization)\s*[=:])/i;

export function warmingURL(input, origin) {
  if (typeof input !== "string" || input.length > 4096) return null;
  try {
    const url = new URL(input, origin);
    if (url.origin !== origin || !["http:", "https:"].includes(url.protocol)
        || url.username || url.password || url.hash || !PATHS.has(url.pathname)) return null;
    for (const [name, value] of url.searchParams) {
      if (!PARAMS.has(name) || value.length > 1024 || SECRET.test(value)
          || /[\u0000-\u001f\u007f]/.test(value)) return null;
    }
    return url.pathname + url.search;
  } catch {
    return null;
  }
}

function readyGeneration(response) {
  const h = response?.headers;
  if (response?.status !== 200 || h?.get("X-CloudLens-Source") !== "warehouse"
      || h.get("X-CloudLens-Stale") !== "false"
      || h.get("X-CloudLens-Revalidating") !== "false"
      || h.get("X-CloudLens-Source-State")
      || h.get("X-CloudLens-Cache-Error") || h.get("X-CloudLens-Refresh-Error")) return null;
  const value = h.get("X-CloudLens-Source-Generation");
  return /^\d{1,30}$/.test(value || "") ? BigInt(value) : null;
}

export function createCacheWarmer({
  request, origin, now = Date.now, schedule = setTimeout, cancel = clearTimeout,
  page = globalThis.window, document = globalThis.document,
  maxEntries = 24, ttl = 15 * 60 * 1000, budget = 3, delay = 1000,
  timeout = 20000, backoff = 120000, warn = () => console.warn("CloudLens cache warming paused."),
} = {}) {
  if (!Number.isInteger(maxEntries) || maxEntries < 1 || maxEntries > 64
      || !Number.isInteger(budget) || budget < 1 || budget > 3
      || ![ttl, delay, timeout, backoff].every(v => Number.isFinite(v) && v > 0)) {
    throw new Error("Invalid cache warming bounds");
  }
  const recent = new Map(), inflight = new Map();
  let enabled = false, closed = false, generation = null, queue = [], timer = null;
  let controller = null, coolingUntil = 0, failures = 0;

  function prune() {
    for (const [url, used] of recent) if (now() - used >= ttl) recent.delete(url);
    while (recent.size > maxEntries) recent.delete(recent.keys().next().value);
  }
  function stop() {
    enabled = false;
    queue = [];
    generation = null;
    recent.clear();
    inflight.clear();
    if (timer !== null) cancel(timer);
    timer = null;
    controller?.abort();
  }
  function close() {
    closed = true;
    stop();
    page?.removeEventListener("pagehide", close);
  }
  page?.addEventListener("pagehide", close, { once: true });

  function pump() {
    if (!enabled || closed || timer !== null || controller || !queue.length) return;
    timer = schedule(async () => {
      timer = null;
      prune();
      if (!enabled || closed || document?.hidden || now() < coolingUntil) {
        queue = [];
        return;
      }
      let url;
      while (queue.length) {
        const candidate = queue.shift();
        if (recent.has(candidate) && !inflight.has(candidate)) { url = candidate; break; }
      }
      if (!url) return;
      controller = new AbortController();
      try {
        // fresh is an existing cache control, excluded from the server's private key.
        // Awaiting refresh also prevents serial HTTP calls spawning parallel stale refreshes.
        const response = await request(`${url}${url.includes("?") ? "&" : "?"}fresh=true`, {
          signal: controller.signal, timeout, credentials: "same-origin",
        });
        if (!enabled || closed) return;
        const observed = readyGeneration(response);
        if (observed === null) throw new Error("Incomplete warm");
        if (generation === null || observed > generation) generation = observed;
        failures = 0;
      } catch (error) {
        if (!enabled || closed) return;
        if (error?.status === 401 || error?.status === 403) stop();
        else {
          coolingUntil = now() + backoff * 2 ** Math.min(failures++, 4);
          queue = [];
          warn();
        }
      } finally {
        controller = null;
        pump();
      }
    }, delay);
  }

  return {
    configure(value) {
      if (value !== true) stop();
      else if (!closed) enabled = true;
    },
    begin(input, options = {}) {
      if (!enabled || closed || (options.method || "GET").toUpperCase() !== "GET"
          || Object.keys(options).some(key => !["method", "credentials"].includes(key))
          || (options.credentials && options.credentials !== "same-origin")) return null;
      const url = warmingURL(input, origin);
      if (!url || (!inflight.has(url) && inflight.size >= maxEntries)) return null;
      inflight.set(url, (inflight.get(url) || 0) + 1);
      return url;
    },
    end(url) {
      const remaining = (inflight.get(url) || 0) - 1;
      if (remaining > 0) inflight.set(url, remaining);
      else inflight.delete(url);
    },
    observe(url, response) {
      if (!enabled || closed || !url || !inflight.has(url)) return;
      const next = readyGeneration(response);
      if (next === null) return;
      queue = queue.filter(key => key !== url);
      recent.delete(url);
      recent.set(url, now());
      prune();
      if (generation === null) { generation = next; return; }
      if (next <= generation) return;
      generation = next;
      // A new publication never adds another batch behind an active one.
      if (controller || timer !== null || document?.hidden || now() < coolingUntil) return;
      queue = [...recent.keys()].reverse()
        .filter(key => key !== url && !inflight.has(key)).slice(0, budget);
      pump();
    },
    stop,
    close,
  };
}
