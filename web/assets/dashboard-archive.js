/** Archive status and durable-job controls; no reverse dependency on the dashboard shell. */
export function createArchiveStatus({ $, get, whoAmI, esc, toast }) {
  async function jobs(host, version) {
    if (host.archiveVersion !== version || !host.isConnected) return;
    clearTimeout(host.archiveTimer);
    const panel = host.querySelector("[data-archive-jobs]");
    if (!panel || host.archivePolling === version) return;
    host.archivePolling = version;
    try {
      const [a, me] = await Promise.all([get("/api/archive?jobs_only=true"), whoAmI()]);
      if (!panel.isConnected || host.archiveVersion !== version) return;
      const records = a.jobs || [];
      panel.innerHTML = records.slice(0, 10).map((j) => {
        const p = j.progress || {};
        return `<div>Archive ${esc(j.status)} · ${esc(p.phase || "queued")} ·
          ${Number(p.rows || 0).toLocaleString()} rows ·
          ${Number(p.uploaded_bytes || 0).toLocaleString()} bytes uploaded
          ${j.detail ? `<span class="${j.status === "failed" ? "bad" : "muted"}">${esc(j.detail)}</span>` : ""}
          ${j.status === "ready" ? ` · ${esc(j.result?.name || "")}` : ""}
          ${j.can_resume && me?.user?.admin && !a.read_only ? `<button data-archive-resume="${esc(j.job_id)}">Resume archive</button>` : ""}
          ${j.can_cancel && me?.user?.admin && !a.read_only ? `<button data-archive-cancel="${esc(j.job_id)}">Cancel archive</button>` : ""}</div>`;
      }).join("");
      const submit = async (button, url) => {
        if (button.disabled || !button.isConnected || host.archiveVersion !== version) return;
        button.disabled = true;
        try {
          const response = await fetch(url, { method: "POST" });
          const data = await response.json();
          if (!response.ok) throw new Error(data.detail || "Could not update archive job.");
          if (url.startsWith("/api/archive?")) toast(`Archive queued (${data.job_id}).`);
          await jobs(host, version);
        } catch (error) {
          toast(error.message, "err");
          if (button.isConnected) button.disabled = false;
        }
      };
      if (a.enabled && me?.user?.admin && !a.read_only) {
        panel.insertAdjacentHTML("beforeend", `<button id="archiveNow">Archive whole estate today</button>
          <span class="muted">Runs in the background, independently of the header selection.</span>`);
        const button = $("archiveNow");
        const approval = crypto.randomUUID();
        button.onclick = () => submit(button, `/api/archive?idempotency_key=${encodeURIComponent(approval)}`);
      }
      panel.querySelectorAll("[data-archive-resume],[data-archive-cancel]").forEach(button => {
        const resume = button.dataset.archiveResume;
        const url = resume ? `/api/jobs/${encodeURIComponent(resume)}/resume`
          : `/api/archive/jobs/${encodeURIComponent(button.dataset.archiveCancel)}/cancel`;
        button.onclick = () => submit(button, url);
      });
      if (records.some(j => !["ready", "partial", "failed", "cancelled"].includes(j.status))) {
        host.archiveTimer = setTimeout(() => { void jobs(host, version); }, 5000);
      }
    } catch (error) {
      if (panel.isConnected && host.archiveVersion === version) {
        panel.innerHTML = `<span class="muted">Could not read archive jobs: ${esc(error.message)}; retrying.</span>`;
        host.archiveTimer = setTimeout(() => { void jobs(host, version); }, 10000);
      }
    } finally {
      if (host.archivePolling === version) host.archivePolling = null;
    }
  }

  async function render() {
    const host = $("archiveNote");
    if (!host) return;
    clearTimeout(host.archiveTimer);
    const version = host.archiveVersion = (host.archiveVersion || 0) + 1;
    host.innerHTML = `<div data-archive-storage>Checking the archive…</div><div data-archive-jobs></div>`;
    void jobs(host, version);
    const storage = host.querySelector("[data-archive-storage]");
    try {
      const a = await get("/api/archive?limit=6", { timeout: 30000 });
      if (!storage.isConnected) return;
      if (!a.enabled) {
        storage.innerHTML = `<span class="muted">No archive configured.</span>`;
      } else if (!a.reachable) {
        storage.innerHTML = `<span class="bad">Archive unreachable</span>
          <span class="muted">${esc(a.error || "")}</span>`;
      } else {
        const names = Object.keys(a.latest || {});
        const last = names.length ? names.map(k =>
          `${esc(k)} · ${esc((a.latest[k].name || "").split("/").pop())}`).join("<br>")
          : "nothing archived yet";
        storage.innerHTML = `<span class="ok">Archiving to ${esc(a.account)}/${esc(a.container)}</span>
          <span class="muted">One file per dataset per day. Latest:<br>${last}</span>`;
      }
    } catch (error) {
      if (storage.isConnected) storage.innerHTML =
        `<span class="muted">Could not read archive storage status: ${esc(error.message)}</span>`;
    }
  }
  return { render };
}
