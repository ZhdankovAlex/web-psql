// Таблица сессий: GET /api/sessions раз в 3 секунды.
(() => {
  "use strict";

  const STATUS = { pending: "ждёт worker", running: "работает", exited: "завершена", rejected: "отказ", lost: "потеряна" };
  const mine = sessionStorage.getItem("webpsql.session");
  const rows = document.getElementById("rows");
  const pill = document.getElementById("st-count");
  const pillText = document.getElementById("st-count-text");

  const esc = (s) => String(s ?? "—").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const time = (iso) => (iso ? new Date(iso).toLocaleTimeString("ru-RU") : "—");

  function render(list) {
    const running = list.filter((s) => s.status === "running").length;
    pillText.textContent = `запущено psql: ${running}`;
    pill.className = "status-pill " + (running ? "ok" : "");
    if (!list.length) {
      rows.innerHTML = '<tr><td colspan="8" class="empty">Сессий нет — откройте <a href="/">терминал</a>.</td></tr>';
      return;
    }
    rows.innerHTML = list.map((s) => `
      <tr class="${s.id === mine ? "mine" : ""}" title="${s.id === mine ? "сессия этой вкладки" : ""}">
        <td class="mono">${esc(s.id.slice(0, 8))}</td>
        <td><span class="badge ${esc(s.status)}">${esc(STATUS[s.status] || s.status)}</span></td>
        <td class="mono">${esc(s.worker)}${s.pid ? ` <span class="muted">pid ${s.pid}</span>` : ""}</td>
        <td class="mono">${esc(s.database)}</td>
        <td>${s.sandbox_ms != null ? `${s.sandbox_ms} мс` : "—"}</td>
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
      pillText.textContent = "нет связи";
      pill.className = "status-pill bad";
    }
  }

  refresh();
  setInterval(refresh, 3000);
})();
