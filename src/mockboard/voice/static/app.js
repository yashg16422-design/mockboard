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

const WAITING = "Connected. Waiting for the board.";

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
let lastAnswers = null;
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
  if (window.Presence) Presence.show(name);
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
  if (window.Presence) Presence.who(who || "");
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
    if (n === i) el.setAttribute("aria-current", "true");
    else el.removeAttribute("aria-current");
  });
  $("speaking").textContent = "Speaking: " + name;
  setVoice("board");
}

function resetSeats() {
  for (let i = 0; i < seatNames.length; i += 1) {
    seatNames[i] = null;
    const seat = document.querySelector(`[data-seat="${i + 1}"]`);
    seat.classList.remove("is-active");
    seat.removeAttribute("aria-current");
    seat.querySelector(".seat-name").textContent = "—";
  }
}

function clearSpeaker() {
  document.querySelectorAll(".seat").forEach((el) => {
    el.classList.remove("is-active");
    el.removeAttribute("aria-current");
  });
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

function showAnswers(m) {
  if (typeof m.used !== "number" || typeof m.limit !== "number") return; // capped sessions only
  lastAnswers = m;
  const count = $("answer-count");
  count.hidden = false;
  count.textContent = "Answer " + m.used + " of " + m.limit;
}

function resetAnswers() {
  lastAnswers = null;
  $("answer-count").hidden = true;
  $("answer-count").textContent = "";
}

function onClosing() {
  $("stop").disabled = true; // a close cannot be interrupted
  if (lastAnswers && lastAnswers.used >= lastAnswers.limit) {
    caption("note", "", "This interview has reached its answer limit. The board will close here. Thank you for trying Mock Board.");
  }
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
      if (!clockStarted) startClock();
      if (window.Presence) Presence.pulse();
      if (m.side === "board") {
        markSpeaker(m.speaker);
        caption("board", m.speaker, m.text, "", m.segment);
        // The opener sends captions without a speaking state. Replace only the
        // line this page wrote, so a later state event still wins.
        const status = $("status").textContent;
        if (status === WAITING || status === "Connecting.") {
          $("status").textContent = STATES.speaking;
        }
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
    case "answers":
      showAnswers(m);
      break;
    case "closing":
      onClosing();
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
      sessionOver();
      break;
    case "report":
      reportReady = true;
      renderReport(m);
      $("status").textContent = "The session is over. The report is ready.";
      sessionOver();
      break;
    default:
      break;
  }
}

function addLine(parent, text) {
  const p = document.createElement("p");
  p.textContent = text;
  parent.append(p);
}

function outlineList(parent, outline) {
  if (Array.isArray(outline)) {
    const ol = document.createElement("ol");
    for (const step of outline) {
      const li = document.createElement("li");
      li.textContent = String(step);
      ol.append(li);
    }
    parent.append(ol);
    return;
  }
  if (outline) addLine(parent, String(outline));
}

async function loadLikely() {
  const fresh = readForm();
  if (!fresh) return;
  const btn = $("likely-btn");
  const note = $("likely-note");
  const list = $("likely-list");
  btn.disabled = true;
  note.hidden = false;
  note.textContent = "Preparing the list.";
  try {
    const res = await fetch("/api/likely-questions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(fresh),
    });
    const data = await res.json();
    if (!res.ok) {
      note.textContent = "The list could not be prepared from this form. Please check the required fields.";
      return;
    }
    list.replaceChildren();
    for (const q of data.questions || []) {
      const item = document.createElement("li");
      const who = q.member ? String(q.member) + ". " : "";
      item.textContent = who + String(q.text || "");
      if (q.hint) {
        const hint = document.createElement("p");
        hint.className = "quiet";
        hint.textContent = "How to approach it: " + String(q.hint);
        item.append(hint);
      }
      list.append(item);
    }
    list.hidden = !(data.questions && data.questions.length);
    if (data.limit && data.limit.message) {
      note.textContent = String(data.limit.message);
    } else {
      note.textContent = data.note ? String(data.note) : "";
    }
    note.hidden = !note.textContent;
  } catch (e) {
    note.textContent = "The list could not be prepared just now. You can still enter the room.";
    note.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

function renderReplay(m) {
  const btn = $("replay");
  const box = $("replay-box");
  if (!btn || !box) return;
  const weakest = m.weakest && m.weakest.question ? m.weakest : null;
  if (!weakest) {
    btn.hidden = true;
    box.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.onclick = () => {
    box.hidden = false;
    box.replaceChildren();
    addLine(box, weakest.question);
    outlineList(box, weakest.outline);
    addLine(box, "Text only. Say your answer out loud, then compare it with what you said in the sitting.");
  };
}

function renderReport(m) {
  // Plain text until the server sends members. Nothing here is invented.
  const members = Array.isArray(m.members)
    ? m.members.filter((item) => item && typeof item === "object")
    : [];
  const grouped = $("report-grouped");
  const plain = $("report");
  grouped.replaceChildren();
  plain.textContent = m.text || "";
  if (!members.length) {
    grouped.hidden = true;
    plain.hidden = false;
  } else {
    grouped.hidden = false;
    plain.hidden = true;
    for (const member of members) {
      const block = document.createElement("section");
      block.className = "member-block";
      const h = document.createElement("h3");
      h.textContent = member.name ? String(member.name) : "Member";
      block.append(h);
      if (member.notes) addLine(block, String(member.notes));
      if (member.weakest_question) addLine(block, "Weakest question. " + String(member.weakest_question));
      outlineList(block, member.outline);
      grouped.append(block);
    }
    if (Array.isArray(m.practice_plan) && m.practice_plan.length) {
      const block = document.createElement("section");
      block.className = "member-block";
      const h = document.createElement("h3");
      h.textContent = "Practice plan";
      block.append(h);
      outlineList(block, m.practice_plan);
      grouped.append(block);
    }
  }
  $("report-demo").hidden = !(cfg && (cfg.fake_llm || cfg.fake_speech));
  // A local run only: on a server this path is neither on the reader's computer nor theirs.
  const local = access && !access.enabled;
  $("report-path").textContent = m.path && local ? "Saved on this computer at " + m.path : "";
  renderReplay(m);
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
      if (window.Presence && $("meter").dataset.who !== "board") Presence.level(level);
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

let access = null;

function codeValue() {
  const el = $("access-code");
  return el ? el.value.trim() : "";
}

function copyFree(limit, answers) {
  return "Thank you for trying Mock Board. Every spoken interview uses paid speech and AI services, "
    + "and this is a student project, so test access is limited to " + limit + " interviews of "
    + answers + " answers each. You have used yours. If you are reviewing this project, please "
    + "enter your access code.";
}

function copyShared() {
  return "Thank you for trying Mock Board. Every spoken interview uses paid speech and AI services, "
    + "and many interviews have already come from this network, which people often share. "
    + "If you are reviewing this project, please enter your access code.";
}

function istTime(stamp) {
  const when = new Date(stamp);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata", day: "numeric", month: "long",
    hour: "numeric", minute: "2-digit", hour12: true,
  }) + " IST";
}

function copyDaily() {
  const when = access && access.resets_at ? istTime(access.resets_at) : "";
  const opens = when ? "They open again at " + when + "." : "They open again tomorrow.";
  return "Today's test interviews are all taken. " + opens + " Thank you for your patience.";
}

function limitCopy(reason, limit, answers) {
  if (reason === "daily_full") return copyDaily();
  if (reason === "ip_full") return copyShared();
  return copyFree(limit, answers);
}

function showAccessNote(text) {
  $("access-slot").hidden = false;
  $("access-note").textContent = text;
  $("access-code-label").hidden = false;
  $("problem").textContent = "";
  show(cfg && cfg.mode === "upsc" ? "form" : "room");
}

function showLimitReached(body) {
  const answers = access && access.answers_per_interview ? access.answers_per_interview : 10;
  const limit = (body && body.limit) || (access && access.limit) || 5;
  showAccessNote(limitCopy(body && body.reason, limit, answers));
}

function showCodeProblem(reason) {
  showAccessNote(reason === "locked" ? "Please try again in a few minutes."
    : "That access code was not recognised. Nothing was used.");
}

function refusalReason() {
  if (!access || !access.enabled || access.reviewer) return null;
  return access.reason || null;
}

function refusedLocally() {
  const reason = refusalReason();
  if (!reason) return false;
  showLimitReached({ reason, limit: access.limit });
  return true;
}

function renderAccess() {
  const slot = $("access-slot");
  if (!access || !access.enabled) {
    slot.hidden = true;
    return;
  }
  slot.hidden = false;
  if (cfg && cfg.mode !== "upsc") {
    const room = $("view-room");
    room.insertBefore(slot, room.querySelector(".actions"));
  }
  $("access-code-label").hidden = !!access.reviewer;
  if (access.reviewer) {
    $("access-note").textContent = "Reviewer access. No interview limit on this browser.";
    return;
  }
  if (access.reason) {
    $("access-note").textContent = limitCopy(access.reason, access.limit, access.answers_per_interview);
    return;
  }
  $("access-note").textContent = "This is a student project. Every spoken interview uses paid speech "
    + "and AI services, so test access is limited to " + access.limit + " interviews of "
    + access.answers_per_interview + " answers each. You have " + access.remaining + " left. "
    + access.daily_remaining + " test interviews remain for everyone today. "
    + "Repeats and clarifying questions do not count, up to 4.";
}

async function loadAccess() {
  try {
    access = await (await fetch("/api/access")).json();
  } catch (e) {
    access = null;
  }
  renderAccess();
  let duration = "A session stops at " + cfg.max_minutes + " minutes.";
  if (access && access.enabled && !access.reviewer && typeof access.remaining === "number") {
    duration += " Test access: " + access.remaining + " of " + access.limit + " interviews left.";
  }
  $("duration").textContent = duration;
}

// A wrong code must never spend an interview, so it is checked before the room opens.
async function redeemCode(code) {
  try {
    const res = await fetch("/api/access", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    access = await res.json();
  } catch (e) {
    $("problem").textContent = "The access code could not be checked. Please try again.";
    return false;
  }
  renderAccess();
  if (access.code !== "ok") {
    showCodeProblem(access.code);
    return false;
  }
  $("access-code").value = "";
  return true;
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
  const code = codeValue();
  if (code) {
    connecting = true;
    const redeemed = await redeemCode(code);
    connecting = false;
    if (!redeemed) return;
  }
  if (refusedLocally()) return;
  $("start").disabled = true;
  connecting = true;
  resetSeats();
  resetClock();
  resetAnswers();
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
      if (state === "disconnected") {
        setNote("reconnect");
        loadAccess();
      }
      if (state === "failed" || state === "closed") connectionLost();
    };
    dc = pc.createDataChannel("messages");
    dc.onopen = () => {
      startClock();
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
      if (res.status === 429 && body.reason) {
        const limit = new Error("limit");
        limit.detail = body;
        throw limit;
      }
      if (res.status === 403 && body.error === "code") {
        const refused = new Error("code");
        refused.codeReason = body.reason;
        throw refused;
      }
      if (res.status === 429 && body.error === "busy") {
        throw new Error("Please try again in a few minutes.");
      }
      if (body.problems) throw new Error("The form needs a small change: " + body.problems.join("; "));
      throw new Error("The room did not open (" + res.status + ").");
    }
    const answer = await res.json();
    // Set this before the answer is applied. Captions and state can arrive
    // during setRemoteDescription, and a write afterwards would replace them.
    $("status").textContent = WAITING;
    setNote("");
    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    $("stop").disabled = false;
    connecting = false;
    $("form").querySelectorAll("input:not(#access-code), select, textarea").forEach((el) => {
      el.disabled = true;
    });
  } catch (err) {
    const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError"
      || err.name === "NotFoundError");
    stop();
    if (err && err.detail && err.detail.reason) {
      showLimitReached(err.detail);
      return;
    }
    if (err && err.codeReason) {
      showCodeProblem(err.codeReason);
      return;
    }
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

// Sessions the server ends arrive here too: `ended` or `report` closes the microphone.
function sessionOver() {
  if (stopWaiter) {
    stopWaiter();
    return;
  }
  stop();
  loadAccess();
}

async function connectionLost() {
  stop();
  setNote("reconnect");
  $("status").textContent = "The connection dropped.";
  await loadAccess();
  if (refusalReason()) {
    setNote("");
    refusedLocally();
  }
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
  loadAccess();
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
  armClock();
  await loadAccess();
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
    if ($("likely-slot")) $("likely-slot").hidden = true;
  }
}

$("begin").addEventListener("click", () => {
  if (!cfg) return;
  show(cfg.mode === "upsc" ? "form" : "room");
});

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  // After a session the form is locked and reads as empty; `start` keeps the form it has.
  if (!$("form").elements.home_state.disabled) pendingForm = readForm();
  if (!pendingForm) return;
  start();
});

$("start").addEventListener("click", start);
$("stop").addEventListener("click", () => { requestStop(); });
$("print").addEventListener("click", () => { window.print(); });
if ($("likely-btn")) $("likely-btn").addEventListener("click", () => { loadLikely(); });

init().catch((e) => {
  const msg = "Could not load the config: " + e.message;
  $("boot").textContent = msg;
});
