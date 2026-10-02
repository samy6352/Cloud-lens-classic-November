/** Real Cost data view, with every backend request fulfilled in a private context. */
export async function createCostDataFixture(page, { configured = false, scope = [] } = {}) {
  const origin = new URL(page.url()).origin;
  const source = await page.request.get(`${origin}/assets/dashboard-reports.js`);
  const css = await page.request.get(`${origin}/assets/app.css`);
  if (!source.ok() || !css.ok()) throw new Error("Local Cost data assets are unavailable.");
  const context = await page.context().browser().newContext({ viewport: { width: 1280, height: 900 } });
  const test = await context.newPage();
  test.setDefaultTimeout(8000);
  const reads = [], previews = [], writes = [], errors = [], unexpected = [];
  const state = {
    can_review: true, auto_enabled: true, setup_needed: !configured, checked_at: "2026-09-15T10:00:00Z",
    subscriptions: ["Alpha", "Beta"].map((name, index) => ({
      id: `sub-${index}`, name, configured, status: configured ? "ready" : "idle",
      config: configured ? { cadence: "Daily", enabled: true } : {},
      tasks: [], coverage: { requested_days: 90, authoritative_days: configured ? 90 : 0 },
      history: { from: "2026-06-18", to: "2026-09-15" },
      last_success: configured ? "2026-09-15T09:00:00Z" : null,
      through: configured ? "2026-09-15" : null,
    })),
    cadences: { Daily: "Daily correction checks", Weekly: "Weekly correction checks",
      Monthly: "Previous completed calendar month plus corrections" },
  };
  const fixture = { context, test, state, reads, previews, writes, errors, unexpected,
    blocked: false, failApproval: false, inventory: [], headerScope: scope };
  test.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    if (pathname === "/reports.js") return route.fulfill({ body: await source.body(), contentType: "text/javascript" });
    if (pathname === "/app.css") return route.fulfill({ body: await css.body(), contentType: "text/css" });
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<!doctype html><html><head><link rel="stylesheet" href="/app.css"></head><body><main id="tabBody" style="max-width:1100px;margin:20px auto"></main></body></html>' });
    if (request.method() === "GET") {
      reads.push({ path: pathname, scope: url.searchParams.get("scope")?.split(",") || [] });
      if (pathname === "/api/cost-data/subscriptions")
        return route.fulfill({ json: { subscriptions: state.subscriptions.map(({ id, name }) => ({ id, name })) } });
      if (pathname === "/api/cost-data")
        return route.fulfill({ json: { ...state, subscriptions: state.subscriptions.filter(s =>
          url.searchParams.get("scope")?.split(",").includes(s.id)) } });
      if (pathname === "/api/schedules") return route.fulfill({ json: { schedules: fixture.inventory } });
    } else if (request.method() === "POST" && pathname === "/api/cost-data/preview") {
      const body = request.postDataJSON();
      previews.push(body);
      return route.fulfill({ json: {
        can_start: !fixture.blocked, preview_token: `review-${previews.length}`, from: body.start, to: body.end,
        explanation: state.cadences[body.cadence], identity: "Persistent workload identity",
        notes: ["External and legacy exports are untouched. No broad roles are granted.",
          "Existing data is preserved; retained reports incur storage charges."],
        subscriptions: state.subscriptions.filter(s => body.scope.includes(s.id)).map(s => ({
          subscription: s.id, name: s.name, action: "Configure deployment-owned FOCUS export",
          export: { name: `owned-${s.id}`, etag: "reviewed-v1" },
          destination: { account_id: "deployment-storage", container: "focus", folder: s.id },
          blockers: fixture.blocked ? ["Workload Blob read access is missing."] : [],
        })),
      } });
    } else if (request.method() === "POST" && pathname === "/api/cost-data/setup") {
      const body = request.postDataJSON();
      writes.push(body);
      if (fixture.failApproval) {
        fixture.failApproval = false;
        return route.fulfill({ status: 503, json: { detail: "Temporary approval failure" } });
      }
      for (const sub of state.subscriptions.filter(s => body.scope.includes(s.id))) {
        sub.status = "queued"; sub.job_id = "durable-fixture-job";
      }
      return route.fulfill({ json: { job_id: "durable-fixture-job", status: "queued" } });
    }
    unexpected.push(`${request.method()} ${pathname}`);
    return route.fulfill({ status: 503, json: { detail: `Unexpected fixture request: ${pathname}` } });
  });
  try {
    await test.goto(origin);
    await test.evaluate(async scope => {
      const { createTabs } = await import("/reports.js");
      window.events = [];
      window.addEventListener("cloudlens:focus-synced", event => window.events.push(event.detail));
      window.ctx = {
        $: id => document.getElementById(id), loadToken: 1, scopeIds: () => scope, qs: () => "",
        allSubscriptions: () => [], key: name => name, readStore: (_, fallback) => fallback,
        esc: value => String(value ?? "").replace(/[&<>"']/g, c =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]),
        get: async (url, options) => {
          const response = await fetch(url, { signal: options?.signal });
          if (!response.ok) throw new Error("Fixture read failed");
          return response.json();
        },
        setContent: (host, markup) => { host.innerHTML = markup; }, toast: () => {},
        whoAmI: async () => ({ user: { admin: true } }),
      };
      window.reports = createTabs(window.ctx);
      await window.reports.renderSettings(1);
    }, scope);
    return fixture;
  } catch (error) {
    await context.close();
    throw error;
  }
}
