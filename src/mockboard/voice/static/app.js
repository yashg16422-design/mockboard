// SmallWebRTC (pipecat-ai 1.5.0) client.
// One audio transceiver at index 0, a data channel whose "ping" messages keep the
// server's connection check alive, and one non-trickle offer POSTed to /api/offer.
// The server sends {"type":"mockboard", "event": ...} on that channel.
// page_id is one id per page load. A new offer with the same page_id closes this
// page's previous connection (defect D). The form has no name, roll number, phone,
// email or college field, and this script never adds one.

const $ = (id) => document.getElementById(id);
const PAGE_ID = (crypto.randomUUID && crypto.randomUUID()) || String(Math.random()).slice(2);

const STATES = {
  starting: "The board is opening.",
  listening: "Your turn. Speak, then pause.",
  user: "Listening to you.",
  closing: "You paused. Checking you are done.",
  thinking: "The board is thinking.",
  speaking: "The board is speaking.",
  finished: "The session is over. Preparing the report.",
};

const ROOM_NOTE = {
  permission: "The microphone is needed. Allow it in the browser, then press Start speaking again.",
  connecting: "Connecting.",
  "no-speech": "No words were recognised. Please try again.",
  reconnect: "The connection dropped. Press Start speaking to enter again. An attempt that never connected is not counted.",
};

let pc = null;
let mic = null;
let pinger = null;
let meterCtx = null;
let cfg = null;
let connecting = false;
let dc = null;
let stopWaiter = null;
let expectClose = false;
let reportReady = false;
let pendingForm = null;
let clockId = null;
let clockStarted = 0;
const seatNames = [null, null, null, null, null];

function show(name) {
  for (const id of ["home", "form", "room", "report"]) {
    $("view-" + id).hidden = id !== name;
  }
  const kickers = {
    home: "01 / Practice",
    form: "02 / The form",
    room: cfg && cfg.mode !== "upsc" ? "02 / The motion" : "03 / The panel",
    report: "04 / Report",
  };
  $("section-kicker").textContent = kickers[name] || "";
  const heading = $("view-" + name).querySelector("h1");
  if (heading && name !== "home") {
    heading.tabIndex = -1;
    heading.focus();
  }
}

function setNote(key) {
  const note = $("room-note");
  const text = ROOM_NOTE[key] || "";
  note.hidden = !text;
  note.textContent = text;
}

function setMic(text) {
  $("mic-state").textContent = text;
}

function setVoice(who) {
  const meter = $("meter");
  meter.dataset.who = who || "";
  if (who === "board") $("level").style.width = "";
}

function fmt(total) {
  const s = Math.max(0, total | 0);
  const m = Math.floor(s / 60);
  return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

function armClock() {
  if (!cfg) return;
  $("cap").textContent = fmt(Math.round((cfg.max_minutes || 0) * 60));
}

function resetClock() {
  stopClock();
  clockStarted = 0;
  $("elapsed").textContent = "00:00";
}

function startClock() {
  if (clockStarted) return;
  clockStarted = Date.now();
  const tick = () => {
    $("elapsed").textContent = fmt(Math.floor((Date.now() - clockStarted) / 1000));
  };
  tick();
  clockId = setInterval(tick, 1000);
}

function stopClock() {
  if (clockId) clearInterval(clockId);
  clockId = null;
}

function caption(side, who, text, extra, segment) {
  const div = document.createElement("div");
  div.className = `cap ${side} ${extra || ""}`.trim();
  if (/[\u0900-\u097F]/.test(text)) div.lang = "hi";
  if (who) {
    const w = document.createElement("span");
    w.className = "who";
    w.textContent = who;
    if (segment) {
      const s = document.createElement("span");
      s.className = "segment";
      s.textContent = " · " + segment;
      w.append(s);
    }
    div.append(w);
  }
  div.append(document.createTextNode(text));
  const box = $("captions");
  box.append(div);
  box.scrollTop = box.scrollHeight;
}

function markSpeaker(name) {
  if (!name) return;
  let i = seatNames.indexOf(name);
  if (i < 0) {
    i = seatNames.indexOf(null);
    if (i < 0) {
      $("speaking").textContent = "Speaking: " + name;
      return;
    }
    seatNames[i] = name;
    document.querySelector(`[data-seat="${i + 1}"] .seat-name`).textContent = name;
  }
  document.querySelectorAll(".seat").forEach((el, n) => {
    el.classList.toggle("is-active", n === i);
  });
  $("speaking").textContent = "Speaking: " + name;
  setVoice("board");
}

function resetSeats() {
  for (let i = 0; i < seatNames.length; i += 1) {
    seatNames[i] = null;
    const seat = document.querySelector(`[data-seat="${i + 1}"]`);
    seat.classList.remove("is-active");
    seat.querySelector(".seat-name").textContent = "—";
  }
}

function clearSpeaker() {
  document.querySelectorAll(".seat").forEach((el) => el.classList.remove("is-active"));
  $("speaking").textContent = "";
  if ($("meter").dataset.who === "board") setVoice("");
}

function placeMembers(names) {
  if (!Array.isArray(names)) return;
  names.slice(0, 5).forEach((name, i) => {
    if (!name) return;
    seatNames[i] = String(name);
    document.querySelector(`[data-seat="${i + 1}"] .seat-name`).textContent = seatNames[i];
  });
}

function onMessage(raw) {
  let m;
  try { m = JSON.parse(raw); } catch { return; }
  if (!m || m.type !== "mockboard") return;
  switch (m.event) {
    case "session":
      startClock();
      placeMembers(m.members);
      break;
    case "state":
      $("status").textContent = STATES[m.state] || m.state;
      if (m.state === "speaking" || m.state === "starting") setVoice("board");
      else clearSpeaker();
      if (m.state === "listening" || m.state === "user") setNote("");
      break;
    case "caption":
      if (m.side === "board") {
        markSpeaker(m.speaker);
        caption("board", m.speaker, m.text, "", m.segment);
      } else {
        setVoice("you");
        caption("you", m.late ? "You, late transcript" : "You", m.text, "", m.segment);
      }
      break;
    case "line_end":
      if (m.interrupted) caption("note", "", "The board was interrupted.");
      break;
    case "turn_reopened":
      caption("note", "", "You went on, so the board is still listening.");
      break;
    case "no_speech":
      setNote("no-speech");
      caption("note", "", "No words were recognised. Please try again.");
      break;
    case "tts_failed":
      caption("note", "", "The voice failed for one line. It is shown as text.");
      break;
    case "time_up":
      caption("note", "", "Time is up. The board is wrapping up.");
      break;
    case "preparing_report":
      $("status").textContent = "Preparing the report.";
      break;
    case "error":
      $("problem").textContent = m.text || "";
      break;
    case "stopping":
      $("status").textContent = "The board is preparing your report.";
      break;
    case "ended":
      $("status").textContent = m.text || "The session is over.";
      if (stopWaiter) stopWaiter();
      break;
    case "report":
      reportReady = true;
      renderReport(m);
      $("status").textContent = "The session is over. The report is ready.";
      if (stopWaiter) stopWaiter();
      break;
    default:
      break;
  }
}

function renderReport(m) {
  // Grouped layout arrives in the report commit. Until then, show the text the server sent.
  $("report").textContent = m.text || "";
  $("report-path").textContent = m.path ? "Saved on this computer at " + m.path : "";
  show("report");
}

function startMeter(stream) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  meterCtx = new Ctx();
  if (meterCtx.state === "suspended") meterCtx.resume();
  const analyser = meterCtx.createAnalyser();
  analyser.fftSize = 512;
  meterCtx.createMediaStreamSource(stream).connect(analyser);
  const data = new Uint8Array(analyser.fftSize);
  (function tick() {
    if (!meterCtx) return;
    if ($("meter").dataset.who !== "board") {
      analyser.getByteTimeDomainData(data);
      let peak = 0;
      for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
      const level = Math.min(100, (peak / 128) * 160);
      $("level").style.width = level + "%";
      if (level > 8) setVoice("you");
      else if ($("meter").dataset.who === "you") setVoice("");
    }
    requestAnimationFrame(tick);
  })();
}

function waitForIceGathering(conn, timeoutMs = 2000) {
  if (conn.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      conn.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => { if (conn.iceGatheringState === "complete") done(); };
    conn.addEventListener("icegatheringstatechange", check);
    setTimeout(done, timeoutMs);
  });
}

function readForm() {
  const f = $("form");
  if (!f.reportValidity()) return null;
  const hobbies = ["hobby-1", "hobby-2", "hobby-3"]
    .map((id) => $(id).value.trim())
    .filter(Boolean);
  const raw = new FormData(f);
  const out = {};
  for (const key of ["home_state", "district", "degree_subject", "optional_subject",
                     "work_experience", "interview_language"]) {
    const value = String(raw.get(key) || "").trim();
    if (value) out[key] = value;
  }
  if (hobbies.length) out.hobbies = hobbies;
  return { form: out };
}

async function start() {
  if (connecting || pc) return;
  $("problem").textContent = "";
  expectClose = false;
  reportReady = false;
  let bodyData = { page_id: PAGE_ID };
  if (cfg.mode === "upsc") {
    const locked = $("form").elements.home_state.disabled;
    if (!locked) {
      const fresh = readForm();
      if (!fresh) { show("form"); return; }
      pendingForm = fresh;
    }
    if (!pendingForm) { show("form"); return; }
    bodyData = { ...pendingForm, page_id: PAGE_ID };
  }
  $("start").disabled = true;
  connecting = true;
  resetSeats();
  resetClock();
  armClock();
  $("captions").replaceChildren();
  show("room");
  setNote("connecting");
  $("status").textContent = "Connecting.";
  try {
    setMic("Asking for the microphone");
    mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    setMic("Microphone open");
    startMeter(mic);
    pc = new RTCPeerConnection();
    pc.addTransceiver(mic.getAudioTracks()[0], { direction: "sendrecv" });
    pc.ontrack = (e) => {
      $("bot").srcObject = e.streams[0] || new MediaStream([e.track]);
    };
    pc.onconnectionstatechange = () => {
      if (!pc || expectClose) return;
      const state = pc.connectionState;
      if (state === "connected") setNote("");
      if (state === "disconnected") setNote("reconnect");
      if (state === "failed") {
        stop();
        setNote("reconnect");
        $("status").textContent = "The connection dropped.";
      }
    };
    dc = pc.createDataChannel("messages");
    dc.onopen = () => {
      pinger = setInterval(() => {
        if (dc && dc.readyState === "open") dc.send("ping: " + Date.now());
      }, 1000);
    };
    dc.onmessage = (e) => onMessage(e.data);
    await pc.setLocalDescription(await pc.createOffer());
    await waitForIceGathering(pc);
    const res = await fetch("/api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
        request_data: bodyData,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      if (body.problems) throw new Error("The form needs a small change: " + body.problems.join("; "));
      throw new Error("The room did not open (" + res.status + ").");
    }
    const answer = await res.json();
    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    $("stop").disabled = false;
    connecting = false;
    $("status").textContent = "Connected. Waiting for the board.";
    setNote("");
    $("form").querySelectorAll("input, select, textarea").forEach((el) => { el.disabled = true; });
  } catch (err) {
    const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError"
      || err.name === "NotFoundError");
    stop();
    if (denied) {
      setNote("permission");
      setMic("Microphone not available");
      $("status").textContent = "The microphone is needed.";
      $("problem").textContent = "";
    } else {
      setNote("");
      $("problem").textContent = err.message || String(err);
      $("status").textContent = "The room did not open.";
    }
  }
}

function stop() {
  expectClose = true;
  connecting = false;
  if (pinger) clearInterval(pinger);
  pinger = null;
  if (pc) pc.close();
  pc = null;
  if (mic) mic.getTracks().forEach((t) => t.stop());
  mic = null;
  if (meterCtx) meterCtx.close();
  meterCtx = null;
  setVoice("");
  $("level").style.width = "0";
  setMic("Microphone closed");
  $("start").disabled = false;
  $("stop").disabled = true;
  clearSpeaker();
  if (reportReady) return;
  if (/session is over|no report/i.test($("status").textContent)) return;
  if ($("room-note").textContent) return;
  $("status").textContent = "Stopped.";
}

async function requestStop() {
  $("stop").disabled = true;
  if (!dc || dc.readyState !== "open") { stop(); return; }
  if (mic) mic.getAudioTracks().forEach((t) => { t.enabled = false; });
  setMic("Microphone paused");
  $("status").textContent = "The board is preparing your report.";
  dc.send(JSON.stringify({ type: "mockboard", event: "stop" }));
  await new Promise((resolve) => {
    stopWaiter = resolve;
    setTimeout(resolve, 180000);
  });
  stopWaiter = null;
  stop();
}

async function init() {
  cfg = await (await fetch("/api/config")).json();
  const upsc = cfg.mode === "upsc";
  $("mode-badge").textContent = upsc ? "UPSC interview" : "Debate";
  const fake = [cfg.fake_llm && "fake model", cfg.fake_speech && "fake speech"].filter(Boolean);
  if (fake.length) {
    $("fake-badge").hidden = false;
    $("fake-badge").textContent = "Demo mode";
    $("fake-badge").title = fake.join(", ");
  }
  $("duration").textContent = "A session stops at " + cfg.max_minutes + " minutes.";
  armClock();
  $("recording").hidden = !cfg.record_audio;
  $("debate-panel").hidden = upsc;
  if (!upsc) {
    $("home-title").textContent = "A motion, spoken.";
    $("home-lede").textContent = "You argue one side. The board takes the other, out loud.";
    $("home-honest").textContent = "This is a practice debate, not an official proceeding. The motion was chosen when the app started.";
    $("steps-upsc").hidden = true;
    $("steps-debate").hidden = false;
    $("motion").textContent = "#" + cfg.motion_number + ": " + cfg.motion + " You speak " + cfg.side + " it.";
    $("room-title").textContent = "The motion";
  }
}

$("begin").addEventListener("click", () => {
  if (!cfg) return;
  show(cfg.mode === "upsc" ? "form" : "room");
});

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  pendingForm = readForm();
  if (!pendingForm) return;
  start();
});

$("start").addEventListener("click", start);
$("stop").addEventListener("click", () => { requestStop(); });

init().catch((e) => {
  const msg = "Could not load the config: " + e.message;
  $("boot").textContent = msg;
});
