// Терминал в браузере: xterm.js <-> WebSocket /ws.
// Идентификатор сессии и позиция в потоке вывода хранятся в sessionStorage,
// поэтому после перезагрузки страницы или обрыва связи psql продолжается с того же места.

(() => {
  "use strict";

  const STORE_SID = "webpsql.session";
  const STORE_LAST = "webpsql.last";
  const RESIZE_DEBOUNCE_MS = 200;
  const MAX_RETRIES = 5;

  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: '"Cascadia Mono", Consolas, Menlo, monospace',
    fontSize: 14,
    lineHeight: 1.15,
    scrollback: 5000,
    theme: {
      background: css("--term"),
      foreground: "#d6dbe2",
      cursor: "#6d9eff",
      selectionBackground: "rgba(109, 158, 255, .35)",
      black: "#1f2630", brightBlack: "#687385",
      blue: "#6d9eff", brightBlue: "#93b6ff",
      cyan: "#5ccfe6", brightCyan: "#8be3f2",
      green: "#4ade80", brightGreen: "#86efac",
      red: "#f87171", brightRed: "#fca5a5",
      yellow: "#fbbf24", brightYellow: "#fcd34d",
      magenta: "#a78bfa", brightMagenta: "#c4b5fd",
      white: "#d6dbe2", brightWhite: "#ffffff",
    },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("terminal"));
  fit.fit();
  term.focus();

  let ws = null;
  let retries = 0;
  let finished = false; // psql завершился — автоматически не переподключаемся
  let pageFresh = true; // экран пуст (страницу только что открыли) — нужен весь вывод сессии

  function pill(id, text, cls) {
    $(id + "-text").textContent = text;
    $(id).className = "status-pill " + (cls || "");
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  // Сведения о копии базы приходят от worker'а чуть позже, чем ответ gateway, — опрашиваем недолго.
  async function showSandbox(sid) {
    pill("st-db", "копия базы…", "busy");
    for (let i = 0; i < 20; i++) {
      try {
        const r = await fetch(`/api/sessions/${sid}`);
        if (r.ok) {
          const s = await r.json();
          if (s.status === "running" && s.sandbox_ms != null) {
            pill("st-db", `своя копия · ${s.sandbox_ms} мс`, "ok");
            return;
          }
          if (s.status === "rejected" || s.status === "exited") break;
        }
      } catch (e) { /* сеть моргнула — попробуем ещё */ }
      await new Promise((r) => setTimeout(r, 300));
    }
    pill("st-db", "копии базы нет", "bad");
  }

  function connect() {
    finished = false;
    const params = new URLSearchParams({ cols: term.cols, rows: term.rows });
    const sid = sessionStorage.getItem(STORE_SID);
    if (sid) {
      params.set("session", sid);
      // после перезагрузки страницы экран пуст — просим весь сохранённый вывод
      params.set("last", pageFresh ? "0-0" : sessionStorage.getItem(STORE_LAST) || "0-0");
    }
    pageFresh = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${location.host}/ws?${params}`);
    ws = socket;
    pill("st-conn", "подключение…", "busy");

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "session":
          retries = 0;
          if (!msg.resumed) {
            if (sid) term.write("\r\n\x1b[33m[прежняя сессия закрыта, открыта новая с чистой базой]\x1b[0m\r\n");
            sessionStorage.setItem(STORE_LAST, "0-0");
          }
          sessionStorage.setItem(STORE_SID, msg.id);
          pill("st-conn", msg.resumed ? "сессия восстановлена" : "подключено", "ok");
          $("term-title").textContent = `psql — bookstore · сессия ${msg.id.slice(0, 8)}`;
          showSandbox(msg.id);
          // размер мог измениться, пока соединения не было
          send({ type: "resize", cols: term.cols, rows: term.rows });
          break;
        case "output":
          term.write(msg.data);
          sessionStorage.setItem(STORE_LAST, msg.id);
          break;
        case "error":
          term.write(`\r\n\x1b[31m${msg.message}\x1b[0m\r\n`);
          break;
        case "exit":
          finished = true;
          sessionStorage.removeItem(STORE_SID);
          sessionStorage.removeItem(STORE_LAST);
          term.write(`\r\n\x1b[90m[psql завершён (код ${msg.code}), копия базы удалена. Нажмите «Начать заново»]\x1b[0m\r\n`);
          pill("st-conn", "сессия завершена", "warn");
          pill("st-db", "копия удалена", "");
          break;
      }
    };

    socket.onclose = () => {
      if (ws !== socket || finished) return;
      if (retries >= MAX_RETRIES) {
        pill("st-conn", "нет связи", "bad");
        return;
      }
      const delay = Math.min(1000 * 2 ** retries, 10000);
      retries += 1;
      pill("st-conn", `повтор через ${Math.round(delay / 1000)} с`, "bad");
      setTimeout(() => { if (ws === socket) connect(); }, delay);
    };
  }

  term.onData((data) => send({ type: "input", data }));

  // Ctrl+C уходит в psql (прервать запрос), поэтому копирование и вставка — как в
  // терминалах Linux: Ctrl+Shift+C / Ctrl+Shift+V (на macOS — Cmd+C / Cmd+V).
  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== "keydown") return true;
    const key = event.key.toLowerCase();
    const combo = (event.ctrlKey && event.shiftKey) || event.metaKey;
    if (combo && key === "c") {
      const selection = term.getSelection();
      if (selection) navigator.clipboard.writeText(selection);
      event.preventDefault();
      return false;
    }
    if (combo && key === "v") {
      // term.paste учитывает bracketed paste: многострочный текст не выполнится по строкам раньше времени
      navigator.clipboard.readText().then((text) => term.paste(text)).catch(() => {});
      event.preventDefault();
      return false;
    }
    return true;
  });

  let resizeTimer = null;
  term.onResize(({ cols, rows }) => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => send({ type: "resize", cols, rows }), RESIZE_DEBOUNCE_MS);
  });
  window.addEventListener("resize", () => fit.fit());

  // Примеры запросов: вставить в psql и выполнить.
  document.querySelectorAll("[data-sql]").forEach((btn) => {
    btn.addEventListener("click", () => {
      send({ type: "input", data: "\x15" + btn.dataset.sql + "\r" }); // Ctrl+U очищает набранную строку
      term.focus();
    });
  });

  $("reconnect").addEventListener("click", () => {
    retries = 0;
    const old = ws;
    ws = null;
    if (old) old.close();
    connect();
    term.focus();
  });

  $("restart").addEventListener("click", () => {
    send({ type: "close" });
    const old = ws;
    ws = null;
    if (old) old.close();
    sessionStorage.removeItem(STORE_SID);
    sessionStorage.removeItem(STORE_LAST);
    term.reset();
    retries = 0;
    connect();
    term.focus();
  });

  connect();
})();
