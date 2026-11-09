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
  permission: "The microphone is needed. Allow it in the browser, then press Start again.",
  connecting: "Connecting.",
  "no-speech": "No words were recognised. Please try again.",
  reconnect: "The connection dropped. Press Start again to rejoin. An attempt that never connected is not counted.",
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

// The particle field has four shapes; the setup screens share the form's galaxy.
const FIELD_SHAPE = { choose: "choose", debate: "form", partner: "form" };
const VIEWS = ["home", "choose", "form", "debate", "partner", "room", "report"];

function show(name) {
  for (const id of VIEWS) {
    if ($("view-" + id)) $("view-" + id).hidden = id !== name;
  }
  const mode = currentMode();
  const kickers = {
    home: "01 / Practice",
    choose: "02 / Choose a mode",
    form: "02 / The form",
    debate: "02 / The motion",
    partner: "02 / Your topic",
    room: { upsc: "03 / The panel", debate: "03 / The debate", partner: "03 / Study partner" }[mode],
    report: "04 / Report",
  };
  $("section-kicker").textContent = kickers[name] || "";
  window.scrollTo(0, 0);
  const heading = $("view-" + name).querySelector("h1");
  if (heading && name !== "home") {
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }
  if (window.Presence) Presence.show(FIELD_SHAPE[name] || name);
}

// The sitting the candidate chose (Nov 7): { mode: "upsc" | "debate" | "partner", ... }.
// Without a choice the server's own mode stands, as before the mode picker.
let sitting = null;

function currentMode() {
  if (sitting) return sitting.mode;
  return cfg && cfg.mode !== "upsc" ? "debate" : "upsc";
}

function hasPicker() {
  return !!(cfg && Array.isArray(cfg.modes) && cfg.modes.length > 1);
}

const MODE_BADGE = { upsc: "UPSC interview", debate: "Debate", partner: "Study partner" };
const SETUP_VIEW = { upsc: "form", debate: "debate", partner: "partner" };

function chooseMode(mode) {
  sitting = { mode };
  document.body.dataset.mode = mode;
  $("mode-badge").textContent = MODE_BADGE[mode] || "";
  const slot = $("access-slot");
  const setup = $(mode === "upsc" ? "form" : mode + "-form");
  if (slot && setup) setup.insertBefore(slot, setup.querySelector(".actions.enter"));
  if (mode === "debate") fillMotions();
  show(SETUP_VIEW[mode]);
}

function fillMotions() {
  const select = $("motion-select");
  if (!select || select.options.length) return;
  for (const m of (cfg && cfg.motions) || []) {
    const option = document.createElement("option");
    option.value = String(m.number);
    option.textContent = `${m.number}. ${m.text}`;
    select.append(option);
  }
}

// The room's header line for the chosen sitting: the motion and side, or the topic.
function dressRoom() {
  const mode = currentMode();
  const panel = $("debate-panel");
  if (mode === "upsc") {
    panel.hidden = true;
    $("room-title").textContent = "The panel";
    return;
  }
  panel.hidden = false;
  if (mode === "partner") {
    panel.querySelector(".kicker").textContent = "Topic";
    $("motion").textContent = sitting.partner.topic;
    $("room-title").textContent = "Study partner";
    return;
  }
  panel.querySelector(".kicker").textContent = "Motion";
  $("room-title").textContent = "The debate";
  if (sitting && sitting.debate) {
    const m = ((cfg && cfg.motions) || []).find((x) => String(x.number) === sitting.debate.motion);
    const side = sitting.debate.side === "for" ? "for" : "against";
    $("motion").textContent = (m ? m.text : "") + " You speak " + side + " it.";
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
  if (window.Presence) Presence.who(who || "");
}

function fmt(total) {
  const s = Math.max(0, total | 0);
  const m = Math.floor(s / 60);
  return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

function minutesFor(mode) {
  const byMode = (cfg && cfg.minutes_by_mode) || {};
  return byMode[mode] || (cfg && cfg.max_minutes) || 0;
}

function armClock() {
  if (!cfg) return;
  $("cap").textContent = fmt(Math.round(minutesFor(currentMode()) * 60));
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
  else if (/[\u0B80-\u0BFF]/.test(text)) div.lang = "ta";
  // The segment id ("form_hr") is the board's internal plan name: kept on the row for
  // debugging, never shown to the candidate.
  if (segment) div.dataset.segment = String(segment);
  if (who) {
    const w = document.createElement("span");
    w.className = "who";
    w.textContent = who;
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
    document.querySelector(`[data-seat="${i + 1}"]`).hidden = false;
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
    // The panel shows its five seats from the start; debate and the study partner fill a seat
    // when its speaker first talks, so no empty "—" seats are left.
    seat.hidden = currentMode() !== "upsc";
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
  // A debate has two seats and the study partner one: no empty "—" seats.
  document.querySelectorAll(".seat").forEach((seat, i) => { seat.hidden = i >= names.length; });
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
      if (window.Presence && Presence.state) Presence.state(m.state);
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

// Upgrade #3: a second attempt at the weakest question. The member asks it again (Timbre, in their
// voice), the candidate answers aloud (Prisma hears it live) or types, and the page compares the
// two answers on plain, observable parts. No score. Everything is built with textContent.
let replayRun = null;

function replayLanguage() {
  const form = $("form");
  return (form && form.elements.interview_language && form.elements.interview_language.value) || "hinglish";
}

function replayButton(label, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ghost";
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

async function showComparison(box, before, after, typed = false) {
  const out = document.createElement("section");
  out.className = "replay-compare";
  out.setAttribute("aria-live", "polite");
  const h = document.createElement("h4");
  h.textContent = "Your second answer, compared with the first";
  out.append(h);
  const heard = document.createElement("p");
  const lead = typed ? "Your second answer" : "What the board heard";
  heard.textContent = after ? `${lead}: “${after}”` : "No second answer was heard.";
  out.append(heard);
  box.querySelectorAll(".replay-compare").forEach((el) => el.remove());
  box.append(out);
  let c;
  try {
    const res = await fetch("/api/replay/compare", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ before, after }), credentials: "same-origin",
    });
    c = await res.json();
  } catch {
    addLine(out, "The comparison could not load. Your answer above is what the board heard.");
    return;
  }
  addLine(out, String(c.summary || ""));
  const list = document.createElement("ul");
  list.className = "compare-rows";
  for (const row of c.rows || []) {
    const li = document.createElement("li");
    const mark = (v) => (v ? "yes" : "not heard");
    li.textContent = `${row.label}: first answer ${mark(row.before)}, second answer ${mark(row.after)}`;
    list.append(li);
  }
  out.append(list);
  addLine(out, `Length: ${c.before_words} words, then ${c.after_words} words.`);
  if (c.note) addLine(out, String(c.note));
}

async function showLikely(box, attempt, weakest, heard) {
  if (!heard) return;
  let pairs = [];
  try {
    const res = await fetch(`/api/replay/${encodeURIComponent(attempt.attempt)}/repair`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ heard, question: weakest.question }), credentials: "same-origin",
    });
    pairs = (await res.json()).likely || [];
  } catch {
    return;
  }
  if (!pairs.length) return;
  const out = box.querySelector(".replay-compare");
  if (!out) return;
  const p = document.createElement("p");
  p.textContent = "Likely misheard: " + pairs.map((x) => `“${String(x.heard)}” was probably “${String(x.likely)}”`).join("; ")
    + ". The board reads your meaning, but says this openly rather than changing your words.";
  out.insertBefore(p, out.children[2] || null);
}

function stopReplayCapture() {
  const run = replayRun;
  if (!run) return null;
  replayRun = null;
  run.processor.disconnect();
  run.source.disconnect();
  run.stream.getTracks().forEach((t) => t.stop());
  run.ctx.close();
  clearTimeout(run.timer);
  return run;
}

async function answerAloud(box, weakest, attempt, controls) {
  if (replayRun) return;
  const status = document.createElement("p");
  status.setAttribute("aria-live", "polite");
  box.append(status);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
  } catch {
    status.textContent = "The microphone was not allowed. You can type your answer instead.";
    return;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/replay/${encodeURIComponent(attempt.attempt)}`);
  ws.binaryType = "arraybuffer";
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  if (ctx.state === "suspended") await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (e) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    const pcm = downsampleTo16k([new Float32Array(e.inputBuffer.getChannelData(0))], ctx.sampleRate);
    ws.send(pcm.buffer);
  };
  source.connect(processor);
  processor.connect(ctx.destination);
  const done = replayButton("I have finished my answer", () => finish());
  replayRun = { stream, ctx, source, processor, timer: setTimeout(() => finish(), 85000) };
  controls.hidden = true;
  box.append(done);
  done.focus();
  status.textContent = "Listening. Answer aloud, then press the button.";
  ws.onmessage = (event) => {
    let reply = {};
    try { reply = JSON.parse(event.data); } catch { reply = {}; }
    done.remove();
    controls.hidden = false;
    if (reply.error) {
      status.textContent = "The speech service did not answer. You can type your answer instead.";
      return;
    }
    status.textContent = reply.demo ? "Demo: no speech service, so a sample second answer is shown." : "";
    const heardText = String(reply.heard || "");
    showComparison(box, weakest.answer || "", heardText).then(() => showLikely(box, attempt, weakest, heardText));
  };
  ws.onerror = () => { status.textContent = "The connection dropped. You can type your answer instead."; };
  function finish() {
    if (!stopReplayCapture()) return;
    status.textContent = "Checking what the board heard…";
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "done" }));
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
  btn.onclick = async () => {
    box.hidden = false;
    box.replaceChildren();
    addLine(box, (weakest.member ? weakest.member + " asks again: " : "") + weakest.question);
    outlineList(box, weakest.outline);
    const controls = document.createElement("div");
    controls.className = "actions";
    box.append(controls);
    let attempt = null;
    try {
      const res = await fetch("/api/replay/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segment: weakest.segment, language: replayLanguage() }),
        credentials: "same-origin",
      });
      attempt = await res.json();
    } catch {
      attempt = null;
    }
    if (attempt && attempt.limit) addLine(box, String(attempt.limit.message || ""));
    const spoken = attempt && attempt.attempt;
    if (spoken && !attempt.demo) {
      controls.append(replayButton("Hear the question", async (e) => {
        e.target.disabled = true;
        try {
          const res = await fetch(`/api/replay/${encodeURIComponent(attempt.attempt)}/question`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: weakest.question }), credentials: "same-origin",
          });
          if (res.status === 200) {
            const url = URL.createObjectURL(await res.blob());
            const audio = new Audio(url);
            audio.onended = () => URL.revokeObjectURL(url);
            await audio.play();
          } else {
            addLine(box, "The question could not be spoken just now. It is written above.");
          }
        } catch {
          addLine(box, "The question could not be spoken just now. It is written above.");
        }
      }));
    }
    if (spoken) {
      controls.append(replayButton("Answer aloud", () => answerAloud(box, weakest, attempt, controls)));
    }
    controls.append(replayButton("Type my answer instead", () => {
      if (box.querySelector("textarea")) return;
      const label = document.createElement("label");
      label.className = "field";
      label.textContent = "Your second answer";
      const area = document.createElement("textarea");
      area.maxLength = 4000;
      label.append(area);
      box.append(label);
      box.append(replayButton("Compare with my first answer", () => showComparison(box, weakest.answer || "", area.value.trim(), true)));
      area.focus();
    }));
    if (attempt && attempt.demo) addLine(box, "Demo mode: the question is not spoken, and a sample answer stands in for your voice.");
  };
}

// How the board heard each answer: the transcript as recognised, the listener's note, any request
// and what the board did next. Built by code from the session, never by the judge.
const HEARD_STATUS = {
  answer: "",
  request: "A request, not an answer",
  unclear: "Heard as unclear",
  silence: "No speech was heard",
  not_analysed: "Not analysed (the listener did not run)",
  honest_unknown: "You said you were not sure. The board noted it, and that is fine",
};

function renderHeard(parent, m, title = "How the board heard you") {
  const rows = Array.isArray(m.heard) ? m.heard.filter((r) => r && typeof r === "object") : [];
  if (!rows.length) return;
  const block = document.createElement("section");
  block.className = "member-block heard-block";
  const h = document.createElement("h3");
  h.textContent = title;
  block.append(h);
  if (m.heard_note) addLine(block, String(m.heard_note));
  rows.forEach((row, i) => {
    const item = document.createElement("details");
    item.className = "heard-row";
    const summary = document.createElement("summary");
    const status = HEARD_STATUS[row.status] || "";
    summary.textContent = `Answer ${i + 1}` + (row.member ? ` · ${row.member}` : "") + (status ? ` · ${status}` : "");
    item.append(summary);
    const field = (label, value) => {
      if (!value) return;
      const p = document.createElement("p");
      const b = document.createElement("strong");
      b.textContent = label + " ";
      p.append(b, document.createTextNode(String(value)));
      item.append(p);
    };
    field("Question:", row.question);
    field("Heard:", row.said ? `“${row.said}”` : "");
    if (Array.isArray(row.likely) && row.likely.length) {
      item.open = true; // a likely mishearing is the reason to look: show it without a click
      field("Likely misheard:", row.likely.map((p) => `“${String(p.heard)}” was probably “${String(p.likely)}”`).join("; "));
    }
    field("Understood:", row.understood);
    if (row.request) field("Request:", `${row.request} (found by ${row.request_source || "the board"})`);
    field("Next:", row.next_step ? `the board ${row.next_step}` : "");
    block.append(item);
  });
  parent.append(block);
}

// The study partner's summary (Nov 7): the speaker's own points, each with its reason, example,
// the counterpoint raised and how they answered. No scores. Text only, built with textContent.
const PARTNER_FIELDS = [
  ["reason", "Your reason"],
  ["example", "Your example"],
  ["counterpoint", "A counterpoint raised"],
  ["answer", "How you answered it"],
  ["open", "Still open"],
];

function renderPartner(p, grouped) {
  const intro = document.createElement("section");
  intro.className = "member-block";
  const h = document.createElement("h3");
  h.textContent = p.topic ? String(p.topic) : "Your topic";
  intro.append(h);
  const notes = (Array.isArray(p.notes) ? p.notes : [p.notes || p.note]).filter(Boolean).map(String);
  addLine(intro, "Your points, in your own words, with what made them stronger. Nothing here is a score.");
  for (const note of notes) addLine(intro, note);
  grouped.append(intro);
  const points = Array.isArray(p.points) ? p.points.filter((x) => x && typeof x === "object") : [];
  points.forEach((point, i) => {
    const block = document.createElement("section");
    block.className = "member-block partner-point";
    block.dataset.member = String(i % 5);
    const title = document.createElement("h3");
    title.textContent = `${i + 1}. ${String(point.point || "Your point")}`;
    block.append(title);
    for (const [key, label] of PARTNER_FIELDS) {
      if (!point[key]) continue;
      const row = document.createElement("p");
      const b = document.createElement("strong");
      b.textContent = label + ": ";
      row.append(b, document.createTextNode(String(point[key])));
      block.append(row);
    }
    grouped.append(block);
  });
  const steps = Array.isArray(p.next_steps) ? p.next_steps : [];
  if (steps.length) {
    const block = document.createElement("section");
    block.className = "member-block";
    const title = document.createElement("h3");
    title.textContent = "Next time, try";
    block.append(title);
    outlineList(block, steps);
    grouped.append(block);
  }
}

function renderReport(m) {
  const reportTitle = $("view-report").querySelector("h1");
  if (reportTitle) reportTitle.textContent = m.partner ? "Your points, made stronger" : "Report";
  if (m.partner && typeof m.partner === "object") {
    const grouped = $("report-grouped");
    grouped.replaceChildren();
    grouped.hidden = false;
    $("report").hidden = true;
    $("report").textContent = m.text || "";
    renderPartner(m.partner, grouped);
    renderHeard(grouped, { heard: m.partner.heard || m.heard, heard_note: m.heard_note },
      "How your partner heard you");
    $("report-demo").hidden = !(cfg && (cfg.fake_llm || cfg.fake_speech));
    $("report-path").textContent = "";
    $("replay").hidden = true;
    $("replay-box").hidden = true;
    show("report");
    return;
  }
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
    const scores = Array.isArray(m.scores)
      ? m.scores.filter((item) => item && typeof item === "object")
      : [];
    if (scores.length) {
      const assessment = document.createElement("section");
      assessment.className = "member-block";
      const heading = document.createElement("h3");
      heading.textContent = "How your answers came across";
      assessment.append(heading);
      addLine(assessment, "Practice feedback, not an official UPSC mark.");
      for (const item of scores) {
        const row = document.createElement("div");
        row.className = "assessment-row";
        const label = String(item.dimension || "").replaceAll("_", " ");
        const score = item.score === "insufficient_evidence"
          ? "Not enough evidence"
          : (Number.isInteger(item.score) ? `${item.score} / 5` : "Not scored");
        const title = document.createElement("h4");
        title.textContent = `${label}${item.practice_indicator ? " · practice indicator" : ""} — ${score}`;
        row.append(title);
        if (item.rationale) addLine(row, String(item.rationale));
        if (Array.isArray(item.evidence)) {
          for (const quote of item.evidence) {
            const evidence = document.createElement("blockquote");
            evidence.className = "evidence-quote";
            evidence.textContent = `“${String(quote)}”`;
            row.append(evidence);
          }
        }
        assessment.append(row);
      }
      grouped.append(assessment);
    }
    for (const [i, member] of members.entries()) {
      const block = document.createElement("section");
      block.className = "member-block";
      block.dataset.member = String(i % 5); // presence.js lights this member's cluster on hover
      const h = document.createElement("h3");
      h.textContent = member.name ? String(member.name) : "Member";
      block.append(h);
      if (member.notes) addLine(block, String(member.notes));
      if (Array.isArray(member.questions) && member.questions.length) {
        const label = document.createElement("h4");
        label.textContent = "Questions in this segment";
        block.append(label);
        outlineList(block, member.questions);
      }
      if (member.weakest_question) addLine(block, "Weakest question. " + String(member.weakest_question));
      outlineList(block, member.outline);
      if (Array.isArray(member.evidence) && member.evidence.length) {
        for (const quote of member.evidence) {
          const evidence = document.createElement("blockquote");
          evidence.className = "evidence-quote";
          evidence.textContent = `“${String(quote)}”`;
          block.append(evidence);
        }
      }
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
    renderHeard(grouped, m);
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

// Every quoted phrase here must be recognised by the board's rules, in the language it is shown
// for: tests/test_request_hints.py checks each one, so a hint never promises a phrase that fails.
const TOPIC_HINTS = {
  hinglish: 'To change topics, say: “Kisi aur topic pe baat karte hain.” To hear a question again, say: “Phir se boliye.” To think before answering, say: “Ek minute sochne dijiye.”',
  english: 'To change topics, say: “Could we move to another topic?” To hear a question again, say: “Could you repeat the question?” To think before answering, say: “May I take a moment?”',
  hindi: 'विषय बदलने के लिए कहें: “कृपया विषय बदलिए।” प्रश्न दोबारा सुनने के लिए कहें: “फिर से बोलिए।” सोचने का समय चाहिए तो कहें: “एक मिनट सोचने दीजिए।”',
  tanglish: 'To change topics, say: “வேறு தலைப்புக்கு போகலாமா?” (Can we move to another topic?) To hear a question again, say: “மீண்டும் சொல்லுங்கள்.” (Please say it again.) To think before answering, say: “ஒரு நிமிடம் யோசிக்கிறேன்.” (Let me think for a minute.)',
};

function updateTopicHint() {
  const language = $("form").elements.interview_language.value;
  $("topic-hint").textContent = TOPIC_HINTS[language] || TOPIC_HINTS.hinglish;
}

// Upgrade #2: the microphone and language check. The candidate reads one line; the server sends
// it to Prisma with the locale the sitting will use and says what was heard. Text only, built
// with textContent. The microphone is released as soon as the reading ends.
const MIC_LINES = {
  hinglish: "Mere zile mein saaf paani sabse badi chunauti hai.",
  english: "Clean drinking water is the biggest challenge in my district.",
  hindi: "मेरे ज़िले में साफ़ पानी सबसे बड़ी चुनौती है।",
  tanglish: "என் மாவட்டத்தில் குடிநீர் தான் மிகப்பெரிய சவால்.",
};
const MIC_LANG = { hinglish: "hi-Latn", english: "en", hindi: "hi", tanglish: "ta" };
const MIC_MAX_S = 8;
const MIC_VERDICT = {
  clear: "Heard clearly",
  partly: "Heard in part",
  unclear: "Not understood",
  silent: "No speech heard",
  error: "Check not available",
};
let micCheck = null;

function updateMicLine() {
  const language = $("form").elements.interview_language.value;
  const line = $("mic-line");
  if (!line) return;
  line.textContent = MIC_LINES[language] || MIC_LINES.hinglish;
  line.lang = MIC_LANG[language] || "en";
}

function downsampleTo16k(chunks, rate) {
  let length = 0;
  for (const c of chunks) length += c.length;
  const input = new Float32Array(length);
  let offset = 0;
  for (const c of chunks) { input.set(c, offset); offset += c.length; }
  const ratio = rate / 16000;
  const out = new Int16Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const s = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function showMicResult(r) {
  const box = $("mic-result");
  box.replaceChildren();
  if (r.limit) { addLine(box, String(r.limit.message || "")); return; }
  const title = document.createElement("p");
  const strong = document.createElement("strong");
  strong.textContent = (MIC_VERDICT[r.verdict] || "Result") + (r.demo ? " (demo: no speech service)" : "");
  title.append(strong);
  box.append(title);
  if (r.heard) {
    const heard = document.createElement("p");
    heard.lang = MIC_LANG[r.language] || "en";
    heard.textContent = `The board heard: “${String(r.heard)}”`;
    box.append(heard);
  }
  if (Array.isArray(r.missing) && r.missing.length && r.verdict !== "silent" && r.verdict !== "error") {
    addLine(box, "Words it did not catch: " + r.missing.map(String).join(", "));
  }
  if (r.script_note) addLine(box, String(r.script_note));
  if (r.advice) addLine(box, String(r.advice));
}

async function finishMicCheck() {
  const run = micCheck;
  if (!run) return;
  micCheck = null;
  clearTimeout(run.timer);
  run.processor.disconnect();
  run.source.disconnect();
  run.stream.getTracks().forEach((t) => t.stop());
  const rate = run.ctx.sampleRate;
  await run.ctx.close();
  $("mic-done").hidden = true;
  $("mic-btn").disabled = false;
  addLine($("mic-result"), "Checking…");
  const pcm = downsampleTo16k(run.chunks, rate);
  const language = $("form").elements.interview_language.value;
  try {
    const res = await fetch(`/api/mic-check?language=${encodeURIComponent(language)}`, {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: pcm.buffer,
      credentials: "same-origin",
    });
    showMicResult(await res.json());
  } catch {
    showMicResult({ verdict: "error", advice: "The check could not reach the server. You can still begin." });
  }
}

async function startMicCheck() {
  if (micCheck) return;
  const box = $("mic-result");
  box.replaceChildren();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
  } catch {
    showMicResult({ verdict: "silent", advice: "The microphone was not allowed for this page. Allow it in the browser's address bar, then try again." });
    return;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  if (ctx.state === "suspended") await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  processor.onaudioprocess = (e) => { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
  source.connect(processor);
  processor.connect(ctx.destination);
  micCheck = { stream, ctx, source, processor, chunks, timer: setTimeout(finishMicCheck, MIC_MAX_S * 1000) };
  $("mic-btn").disabled = true;
  $("mic-done").hidden = false;
  addLine(box, `Listening. Read the line above aloud (up to ${MIC_MAX_S} seconds).`);
  $("mic-done").focus();
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
  show(sitting ? SETUP_VIEW[sitting.mode] : (cfg && cfg.mode === "upsc" ? "form" : "room"));
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
  if (cfg && cfg.mode !== "upsc" && !hasPicker()) {
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
  if (sitting && sitting.mode === "debate") bodyData = { mode: "debate", debate: sitting.debate, page_id: PAGE_ID };
  if (sitting && sitting.mode === "partner") bodyData = { mode: "partner", partner: sitting.partner, page_id: PAGE_ID };
  if (currentMode() === "upsc") {
    const locked = $("form").elements.home_state.disabled;
    if (!locked) {
      const fresh = readForm();
      if (!fresh) { show("form"); return; }
      pendingForm = fresh;
    }
    if (!pendingForm) { show("form"); return; }
    bodyData = { ...pendingForm, page_id: PAGE_ID, ...(sitting ? { mode: "upsc" } : {}) };
  }
  const code = codeValue();
  if (code) {
    connecting = true;
    const redeemed = await redeemCode(code);
    connecting = false;
    if (!redeemed) return;
  }
  if (refusedLocally()) return;
  dressRoom();
  $("start").disabled = true;
  $("start").hidden = true; // you just speak during a sitting; no button to press
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
  $("start").textContent = "Start again"; // a new sitting, not a talk button
  $("start").hidden = false;
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
  // The confirm() dialog freezes the page, and with it the 1 s pinger. Pipecat treats a page
  // with no ping for 3 s as gone and queues its messages until a reconnect that never comes,
  // so Stop was lost (Nov 9, live). A ping first, on the same ordered channel, marks the page
  // alive before the stop arrives.
  dc.send("ping: " + Date.now());
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
  document.body.dataset.mode = upsc ? "upsc" : "debate";
  if ($("stat-minutes") && cfg.max_minutes) $("stat-minutes").textContent = String(cfg.max_minutes);
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
  if (hasPicker()) {
    document.querySelectorAll(".mode-card").forEach((card) => {
      card.hidden = !cfg.modes.includes(card.dataset.mode);
    });
    const count = ["", "One", "Two", "Three"][cfg.modes.length] || String(cfg.modes.length);
    $("choose-title").textContent = `${count} ways to practise, out loud.`;
    $("mode-badge").textContent = "Practice";
    document.body.dataset.mode = "upsc";
    $("debate-panel").hidden = true;
  } else if (!upsc) {
    $("home-title").textContent = "A motion, spoken.";
    $("home-lede").textContent = "You argue one side. The board takes the other, out loud.";
    $("home-honest").textContent = "This is a practice debate, not an official proceeding. The motion was chosen when the app started.";
    $("steps-upsc").hidden = true;
    $("steps-debate").hidden = false;
    $("motion").textContent = "#" + cfg.motion_number + ": " + cfg.motion + " You speak " + cfg.side + " it.";
    $("room-title").textContent = "The motion";
    if ($("likely-slot")) $("likely-slot").hidden = true;
    if ($("mic-slot")) $("mic-slot").hidden = true;
  }
}

$("begin").addEventListener("click", () => {
  if (!cfg) return;
  if (hasPicker()) {
    show("choose");
    return;
  }
  if (cfg.mode !== "upsc") { // debate has no form: this button starts the debate, as before
    $("start").textContent = "Start speaking";
    $("start").hidden = false;
  }
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

document.querySelectorAll(".mode-card").forEach((card) => {
  card.addEventListener("click", () => chooseMode(card.dataset.mode));
});

$("debate-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = $("debate-form");
  if (!f.reportValidity()) return;
  sitting = {
    mode: "debate",
    debate: {
      motion: f.elements.motion.value,
      side: f.elements.side.value,
      language: f.elements.language.value,
      opponent: f.elements.opponent.value,
      length: f.elements.length.value,
    },
  };
  start();
});

$("partner-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = $("partner-form");
  if (!f.reportValidity()) return;
  sitting = {
    mode: "partner",
    partner: { topic: f.elements.topic.value.trim(), language: f.elements.language.value },
  };
  start();
});

document.querySelectorAll(".topic-ideas .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $("partner-topic").value = chip.dataset.topic;
    $("partner-topic").focus();
  });
});
// A spoken "end" is confirmed aloud (Ruling 52); a tap is confirmed here, so a slip of the finger
// does not end a practice interview the candidate cannot get back.
$("stop").addEventListener("click", () => {
  if (window.confirm("End the interview now? The board will stop and prepare your report.")) requestStop();
});
$("print").addEventListener("click", () => { window.print(); });
if ($("likely-btn")) $("likely-btn").addEventListener("click", () => { loadLikely(); });

$("form").elements.interview_language.addEventListener("change", updateTopicHint);
updateTopicHint();
if ($("mic-btn")) {
  $("form").elements.interview_language.addEventListener("change", updateMicLine);
  updateMicLine();
  $("mic-btn").addEventListener("click", () => { startMicCheck(); });
  $("mic-done").addEventListener("click", () => { finishMicCheck(); });
}

init().catch((e) => {
  const msg = "Could not load the config: " + e.message;
  $("boot").textContent = msg;
});
