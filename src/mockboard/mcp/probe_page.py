"""The probe's view: one self-contained HTML page (no library, no outside host).

It speaks MCP Apps' JSON-RPC 2.0 over `window.parent.postMessage` (spec 2026-01-26): `ui/initialize`
with `appInfo`, then `ui/notifications/initialized`. It then tests, one by one: the microphone, a
`fetch` and a WebSocket to the probe's own localhost endpoint, and one `ui/message` carrying the
readout. It reports to the server with the app-only tool `probe_report`. `__PORT__` and
`__VERSION__` are filled in by `probe.py`. Nothing is recorded: the microphone stream is stopped
at once and no audio leaves the page.
"""

from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock Board probe</title>
<style>
  :root { color-scheme: light dark; --bg: #0f1115; --fg: #e8eaed; --dim: #9aa0a6;
          --ok: #5fd4a0; --bad: #ff7b72; --wait: #e3b341; }
  @media (prefers-color-scheme: light) {
    :root { --bg: #ffffff; --fg: #1f2328; --dim: #57606a; --ok: #1a7f37; --bad: #cf222e;
            --wait: #9a6700; }
  }
  body { margin: 0; padding: 14px 16px; background: var(--bg); color: var(--fg);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  h1 { font-size: 15px; margin: 0 0 8px; }
  ul { list-style: none; padding: 0; margin: 0 0 10px; }
  li { display: flex; gap: 8px; padding: 2px 0; }
  li b { min-width: 9.5em; font-weight: 600; }
  .ok { color: var(--ok); } .bad { color: var(--bad); } .wait { color: var(--wait); }
  pre { white-space: pre-wrap; word-break: break-word; font: 12px/1.4 ui-monospace, Menlo,
        monospace; padding: 8px; border: 1px solid var(--dim); border-radius: 6px; margin: 6px 0; }
  button { font: inherit; padding: 4px 10px; margin-right: 6px; cursor: pointer; }
  p.note { color: var(--dim); margin: 6px 0 0; font-size: 12px; }
</style>
</head>
<body>
<h1>Mock Board probe (free, no speech is recorded)</h1>
<ul id="rows"></ul>
<div>
  <button id="mic" type="button">Try the microphone again</button>
  <button id="copy" type="button">Copy readout</button>
</div>
<pre id="out">Running...</pre>
<p class="note">Paste the readout above back to the Mock Board session.</p>
<script>
(() => {
  "use strict";
  const PORT = "__PORT__";
  const VERSION = "__VERSION__";
  const R = {
    "view rendered": "yes",
    "ui handshake": "waiting",
    "host": "unknown",
    "host says mic": "unknown",
    "mic (getUserMedia)": "not tried",
    "localhost fetch": "not tried",
    "localhost websocket": "not tried",
    "ui/message to chat": "not tried",
    "host capabilities": "unknown",
  };
  const INFO = new Set(["host", "host capabilities"]);  // facts, not pass or fail
  let nextId = 1;
  const pending = new Map();

  function show() {
    const rows = document.getElementById("rows");
    rows.textContent = "";
    for (const [k, v] of Object.entries(R)) {
      const li = document.createElement("li");
      const b = document.createElement("b");
      b.textContent = k;
      const span = document.createElement("span");
      span.textContent = v;
      span.className = INFO.has(k) ? "" : /^(yes|ok|granted|sent)/.test(v) ? "ok"
        : /^(waiting|not tried|unknown|running)/.test(v) ? "wait" : "bad";
      li.append(b, span);
      rows.append(li);
    }
    document.getElementById("out").textContent = readout();
  }

  function readout() {
    const lines = ["MOCK BOARD PROBE " + VERSION];
    for (const [k, v] of Object.entries(R)) lines.push(k + ": " + v);
    return lines.join("\n");
  }

  function send(msg) { window.parent.postMessage(msg, "*"); }

  function request(method, params, timeoutMs) {
    const id = "probe-" + (nextId++);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("no answer in " + Math.round(timeoutMs / 1000) + " s"));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      send({ jsonrpc: "2.0", id, method, params });
    });
  }

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") return;
    if (msg.id !== undefined && pending.has(msg.id) && !msg.method) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.error) p.reject(new Error("error " + msg.error.code + ": " + msg.error.message));
      else p.resolve(msg.result || {});
      return;
    }
    if (msg.method && msg.id !== undefined) {  // a host request we do not handle: answer politely
      if (msg.method === "ui/resource-teardown") send({ jsonrpc: "2.0", id: msg.id, result: {} });
      else send({ jsonrpc: "2.0", id: msg.id,
                  error: { code: -32601, message: "not handled by the probe" } });
    }
  });

  function errText(e) { return (e && (e.name ? e.name + ": " : "") + (e.message || e)) || "?"; }

  async function tryMic() {
    R["mic (getUserMedia)"] = "running";
    show();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      R["mic (getUserMedia)"] = "unavailable (no mediaDevices in this frame)";
      show();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());  // nothing is recorded
      R["mic (getUserMedia)"] = "granted";
    } catch (e) {
      R["mic (getUserMedia)"] = "denied (" + errText(e) + ")";
    }
    show();
  }

  async function tryFetch() {
    try {
      const res = await fetch("http://127.0.0.1:" + PORT + "/ping", { cache: "no-store" });
      const text = await res.text();
      R["localhost fetch"] = res.ok && text.trim() === "pong" ? "ok (http://127.0.0.1:" + PORT + ")"
        : "unexpected answer (" + res.status + ")";
    } catch (e) {
      R["localhost fetch"] = "blocked (" + errText(e) + ")";
    }
    show();
  }

  function tryWebSocket() {
    return new Promise((resolve) => {
      let done = false;
      const finish = (text) => {
        if (done) return;
        done = true;
        R["localhost websocket"] = text;
        show();
        resolve();
      };
      let ws;
      try {
        ws = new WebSocket("ws://127.0.0.1:" + PORT + "/ws");
      } catch (e) {
        finish("blocked (" + errText(e) + ")");
        return;
      }
      const timer = setTimeout(() => {
        try { ws.close(); } catch (_) {}
        finish("blocked (no answer in 5 s)");
      }, 5000);
      ws.onopen = () => ws.send("ping");
      ws.onmessage = (ev) => {
        clearTimeout(timer);
        finish(ev.data === "pong" ? "ok (ws://127.0.0.1:" + PORT + ")" : "unexpected answer");
        try { ws.close(); } catch (_) {}
      };
      ws.onerror = () => {
        clearTimeout(timer);
        finish("blocked (connection refused or not allowed)");
      };
    });
  }

  async function handshake() {
    try {
      const res = await request("ui/initialize", {
        protocolVersion: "2026-01-26",
        appInfo: { name: "mockboard-probe", version: VERSION },
        appCapabilities: {},
      }, 6000);
      R["ui handshake"] = "ok (protocol " + (res.protocolVersion || "?") + ")";
      const hi = res.hostInfo || {};
      const hc = res.hostContext || {};
      R["host"] = (hi.name || "?") + " " + (hi.version || "") +
        (hc.platform ? " / " + hc.platform : "") + (hc.userAgent ? " / " + hc.userAgent : "");
      const caps = res.hostCapabilities || {};
      const perms = (caps.sandbox && caps.sandbox.permissions) || {};
      R["host says mic"] = "microphone" in perms ? "granted" : "not listed";
      const csp = (caps.sandbox && caps.sandbox.csp && caps.sandbox.csp.connectDomains) || [];
      R["host capabilities"] = ["message", "openLinks", "updateModelContext", "serverTools"]
        .map((k) => k + "=" + (k in caps ? "yes" : "no")).join(" ") +
        " connectDomains=" + (csp.length ? csp.join(",") : "none");
      send({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
      return true;
    } catch (e) {
      R["ui handshake"] = "failed (" + errText(e) + ")";
      return false;
    }
  }

  async function postToChat() {
    R["ui/message to chat"] = "sending (if this readout appears in the chat, it worked)";
    show();
    try {
      const res = await request("ui/message", {
        role: "user",
        content: [{ type: "text",
                    text: "Probe readout (sent by the Mock Board view):\n" + readout() }],
      }, 15000);
      R["ui/message to chat"] = res && res.isError ? "rejected by the host"
        : "sent (check it appears in the chat)";
    } catch (e) {
      R["ui/message to chat"] = "failed (" + errText(e) + ")";
    }
    show();
  }

  async function report() {
    try {
      await request("tools/call",
                    { name: "probe_report", arguments: { readout: readout() } }, 8000);
    } catch (_) { /* probe_result says when no report came */ }
  }

  document.getElementById("mic").addEventListener("click", async () => {
    await tryMic();
    report();
  });
  document.getElementById("copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(readout()); } catch (_) {
      const range = document.createRange();
      range.selectNodeContents(document.getElementById("out"));
      const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }
  });

  (async () => {
    show();
    const ok = await handshake();
    show();
    await tryMic();
    await tryFetch();
    await tryWebSocket();
    if (ok) await postToChat();
    else R["ui/message to chat"] = "skipped (no handshake)";
    show();
    await report();
  })();
})();
</script>
</body>
</html>
"""
