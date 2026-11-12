// Terminal in the browser: xterm.js <-> WebSocket /ws.
// The session id and the position in the output stream are kept in sessionStorage,
// so after a page reload or a dropped connection psql carries on where it was.

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
  let finished = false; // psql has exited — do not reconnect automatically
  let pageFresh = true; // the screen is empty (page just opened) — we need the whole session output

  function pill(id, text, cls) {
    $(id + "-text").textContent = text;
    $(id).className = "status-pill " + (cls || "");
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  // The worker reports the database copy a little later than the gateway answers, so poll briefly.
  async function showSandbox(sid) {
    pill("st-db", "database copy…", "busy");
    for (let i = 0; i < 20; i++) {
      try {
        const r = await fetch(`/api/sessions/${sid}`);
        if (r.ok) {
          const s = await r.json();
          if (s.status === "running" && s.sandbox_ms != null) {
            pill("st-db", `own copy · ${s.sandbox_ms} ms`, "ok");
            return;
          }
          if (s.status === "rejected" || s.status === "exited") break;
        }
      } catch (e) { /* network hiccup — try again */ }
      await new Promise((r) => setTimeout(r, 300));
    }
    pill("st-db", "no database copy", "bad");
  }

  function connect() {
    finished = false;
    const params = new URLSearchParams({ cols: term.cols, rows: term.rows });
    const sid = sessionStorage.getItem(STORE_SID);
    if (sid) {
      params.set("session", sid);
      // after a page reload the screen is empty — ask for all saved output
      params.set("last", pageFresh ? "0-0" : sessionStorage.getItem(STORE_LAST) || "0-0");
    }
    pageFresh = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${location.host}/ws?${params}`);
    ws = socket;
    pill("st-conn", "connecting…", "busy");

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "session":
          retries = 0;
          if (!msg.resumed) {
            if (sid) term.write("\r\n\x1b[33m[the previous session has ended, a new one with a clean database is open]\x1b[0m\r\n");
            sessionStorage.setItem(STORE_LAST, "0-0");
          }
          sessionStorage.setItem(STORE_SID, msg.id);
          pill("st-conn", msg.resumed ? "session restored" : "connected", "ok");
          $("term-title").textContent = `psql — bookstore · session ${msg.id.slice(0, 8)}`;
          showSandbox(msg.id);
          // the window size may have changed while we were disconnected
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
          term.write(`\r\n\x1b[90m[psql exited (code ${msg.code}), the database copy is deleted. Press “Start over”]\x1b[0m\r\n`);
          pill("st-conn", "session ended", "warn");
          pill("st-db", "copy deleted", "");
          break;
      }
    };

    socket.onclose = () => {
      if (ws !== socket || finished) return;
      if (retries >= MAX_RETRIES) {
        pill("st-conn", "no connection", "bad");
        return;
      }
      const delay = Math.min(1000 * 2 ** retries, 10000);
      retries += 1;
      pill("st-conn", `retrying in ${Math.round(delay / 1000)} s`, "bad");
      setTimeout(() => { if (ws === socket) connect(); }, delay);
    };
  }

  term.onData((data) => send({ type: "input", data }));

  // Ctrl+C goes to psql (cancels the query), so copy and paste work like in Linux
  // terminals: Ctrl+Shift+C / Ctrl+Shift+V (Cmd+C / Cmd+V on macOS).
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
      // term.paste uses bracketed paste: multi-line text is not run line by line too early
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

  // Example queries: type into psql and run.
  document.querySelectorAll("[data-sql]").forEach((btn) => {
    btn.addEventListener("click", () => {
      send({ type: "input", data: "\x15" + btn.dataset.sql + "\r" }); // Ctrl+U clears what was typed
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
