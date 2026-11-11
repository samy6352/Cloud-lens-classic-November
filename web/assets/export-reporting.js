"use strict";
(() => {
  const el = (id) => document.getElementById(id);
  let identity = "";
  let managed = [];
  let generation = 0;
  async function api(path, body) {
    const response = await fetch("/api/export-reporting" + path, {
      cache: "no-store",
      ...(body === undefined ? {} : {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || "Request failed.");
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function option(select, value, text) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = text;
    select.append(node);
  }
  function clearReport() {
    generation++;
    el("report").hidden = true;
    el("charges").replaceChildren();
  }
  function showGrants() {
    const item = managed.find((d) => d.dataset_id === el("managed").value);
    el("grants").textContent = item ? JSON.stringify(item.grants, null, 2) : "";
  }
  async function load() {
    clearReport();
    el("datasets").replaceChildren();
    option(el("datasets"), "", "Select a report");
    const catalog = await api("");
    identity = catalog.principal;
    el("identity").textContent = "Your verified identity: " + identity;
    for (const item of catalog.datasets.filter((d) => d.reporting_available)) {
      option(el("datasets"), item.dataset_id,
        `${item.label} — Export-backed — ${item.subscription_id} — imported ${item.last_import}`);
    }
    el("status").textContent = catalog.datasets.length ? "" : "No explicitly approved reports yet.";
    try {
      const config = await api("/admin");
      managed = config.datasets;
      el("admin").hidden = false;
      el("sources").replaceChildren();
      el("managed").replaceChildren();
      for (const s of config.sources) option(el("sources"), s.key, s.label + " — " + s.subscription_id);
      for (const d of managed) option(el("managed"), d.dataset_id, d.subscription_id + " — " + d.last_import + " — " + d.dataset_id);
      showGrants();
    } catch (error) {
      el("admin").hidden = true;
      // Readers do not have administrator access; report access stays independent.
      if (error.status !== 403) throw error;
    }
  }
  async function run(action) {
    try { await action(); } catch (error) { el("status").textContent = error.message; }
  }
  el("datasets").addEventListener("change", () => run(async () => {
    clearReport();
    const version = generation;
    const dataset = el("datasets").value;
    if (!dataset) return;
    const data = await api("/" + encodeURIComponent(dataset) + "/billing");
    if (version !== generation) return;
    el("label").textContent = data.label + " — Export-backed";
    el("freshness").textContent = `Imported ${data.last_import}; source submitted ${data.source_submitted}. ${data.detail}`;
    el("coverage").textContent = `Manifest coverage: ${data.coverage_start} to ${data.coverage_end}; ${data.rows} charge rows.`;
    el("totals").textContent = data.totals.map((t) =>
      `${t.currency}: billed ${t.billed_cost}; effective ${t.effective_cost}`).join(" | ");
    for (const row of data.charges) {
      const tr = document.createElement("tr");
      for (const key of ["day", "service", "currency", "billed_cost", "effective_cost", "rows"]) {
        const td = document.createElement("td");
        td.textContent = row[key];
        tr.append(td);
      }
      el("charges").append(tr);
    }
    el("report").hidden = false;
  }));
  el("managed").addEventListener("change", showGrants);
  el("self").addEventListener("click", () => { el("principal").value = identity; });
  el("import").addEventListener("click", () => run(async () => {
    el("import").disabled = true;
    el("status").textContent = "Validating and importing the pinned FOCUS run…";
    try {
      const data = await api("/import", { source: el("sources").value });
      await load();
      el("managed").value = data.dataset_id;
      showGrants();
      el("status").textContent = data.detail + " Dataset: " + data.dataset_id;
    } finally { el("import").disabled = false; }
  }));
  for (const action of ["grant", "revoke"]) {
    el(action).addEventListener("click", () => run(async () => {
      await api("/" + encodeURIComponent(el("managed").value) + "/grant",
        { principal: el("principal").value.trim(), allow: action === "grant" });
      await load();
      el("status").textContent = action === "grant" ? "Read access approved." : "Read access revoked.";
    }));
  }
  // Never retain an export payload in local storage or a client-side report cache.
  window.addEventListener("pageshow", () => run(load));
  window.addEventListener("pagehide", clearReport);
})();
