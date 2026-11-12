// Sessions table: GET /api/sessions every 3 seconds.
(() => {
  "use strict";

  const STATUS = { pending: "waiting for worker", running: "running", exited: "finished", rejected: "rejected", lost: "lost" };
  const mine = sessionStorage.getItem("webpsql.session");
  const rows = document.getElementById("rows");
  const pill = document.getElementById("st-count");
  const pillText = document.getElementById("st-count-text");

  const esc = (s) => String(s ?? "—").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const time = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-GB") : "—");

  function render(list) {
    const running = list.filter((s) => s.status === "running").length;
    pillText.textContent = `psql running: ${running}`;
    pill.className = "status-pill " + (running ? "ok" : "");
    if (!list.length) {
      rows.innerHTML = '<tr><td colspan="8" class="empty">No sessions — open the <a href="/">terminal</a>.</td></tr>';
      return;
    }
    rows.innerHTML = list.map((s) => `
      <tr class="${s.id === mine ? "mine" : ""}" title="${s.id === mine ? "this tab's session" : ""}">
        <td class="mono">${esc(s.id.slice(0, 8))}</td>
        <td><span class="badge ${esc(s.status)}">${esc(STATUS[s.status] || s.status)}</span></td>
        <td class="mono">${esc(s.worker)}${s.pid ? ` <span class="muted">pid ${s.pid}</span>` : ""}</td>
        <td class="mono">${esc(s.database)}</td>
        <td>${s.sandbox_ms != null ? `${s.sandbox_ms} ms` : "—"}</td>
        <td>${s.connections}</td>
        <td class="mono">${s.cols && s.rows ? `${s.cols}×${s.rows}` : "—"}</td>
        <td>${time(s.created_at)}</td>
      </tr>`).join("");
  }

  async function refresh() {
    try {
      const r = await fetch("/api/sessions");
      if (!r.ok) throw new Error(r.status);
      render(await r.json());
    } catch (e) {
      pillText.textContent = "no connection";
      pill.className = "status-pill bad";
    }
  }

  refresh();
  setInterval(refresh, 3000);
})();
