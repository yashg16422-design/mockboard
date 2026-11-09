// Drives one Mock Board sitting in a real browser and records it for the demo video.
//
//   node driver/record.mjs runs/<name>.json
//
// The run file names the server, the form and a list of steps. Candidate clips are played into
// the page as its microphone (see inject.js). Output, in runs/<name>/:
//   frames/      JPEG screen frames (Chrome's screencast) and frames.json with their times
//   audio.webm   what a viewer hears: board voices, candidate clips, page audio
//   events.json  every server event and driver step, timestamped (epoch ms)
//   screen.mp4   the frames as a constant 30 fps video (made with ffmpeg at the end)
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { framesToMp4 } from "./frames2mp4.mjs";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const runFile = process.argv[2];
if (!runFile) throw new Error("usage: node driver/record.mjs runs/<name>.json");
const run = JSON.parse(fs.readFileSync(runFile, "utf8"));
const runDir = path.join(path.dirname(runFile), path.basename(runFile, ".json"));
const clipDir = path.resolve(path.dirname(runFile), run.clips || "clips");
fs.rmSync(runDir, { recursive: true, force: true });
fs.mkdirSync(path.join(runDir, "frames"), { recursive: true });

const [W, H] = run.viewport || [1920, 1080];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const steps = [];
const mark = (kind, data = {}) => {
  const entry = { t: Date.now(), kind, ...data };
  steps.push(entry);
  console.log(new Date(entry.t).toISOString().slice(11, 23), kind, JSON.stringify(data));
};

const browser = await chromium.launch({
  headless: false, // a real GPU for the WebGL field; the window opens on screen while recording
  args: ["--autoplay-policy=no-user-gesture-required", `--window-size=${W},${H + 120}`],
});
const context = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
const page = await context.newPage();
page.on("console", (m) => { if (m.type() === "error") mark("console_error", { text: m.text() }); });
page.on("pageerror", (e) => mark("page_error", { text: String(e) }));

const audioChunks = [];
await page.exposeFunction("__demoSaveChunk", (b64) => { audioChunks.push(Buffer.from(b64, "base64")); });
await page.addInitScript({ path: path.join(here, "inject.js") });
await page.route("**/__demo/clips/*", (route) => {
  const name = decodeURIComponent(route.request().url().split("/__demo/clips/")[1]);
  const file = path.join(clipDir, path.basename(name));
  route.fulfill({ status: 200, contentType: "audio/wav", body: fs.readFileSync(file) });
});

// Screen frames from Chrome's screencast: sharp, and timestamped by the browser.
const cdp = await context.newCDPSession(page);
const frames = [];
let frameNo = 0;
cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
  const n = frameNo++;
  const file = `f${String(n).padStart(6, "0")}.jpg`;
  frames.push({ file, t: metadata.timestamp * 1000 });
  fs.writeFile(path.join(runDir, "frames", file), Buffer.from(data, "base64"), () => {});
  cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
});

const events = () => page.evaluate(() => window.__demo.log.filter((e) => e.kind === "event"));
async function waitForEvent(match, sinceT, timeoutS = 90) {
  const end = Date.now() + timeoutS * 1000;
  while (Date.now() < end) {
    const hit = (await events()).find((e) => e.t > sinceT && match(e.data));
    if (hit) return hit;
    await sleep(150);
  }
  throw new Error(`timed out waiting for ${match}`);
}
// The candidate's turn: the server says "listening" after the board's last line.
const listening = (d) => d.event === "state" && d.state === "listening";
const boardSpoke = (d) => (d.event === "caption" && d.side === "board") || (d.event === "state" && d.state === "speaking");

async function doStep(step) {
  mark("step", step);
  switch (step.do) {
    case "goto":
      await page.goto(run.url, { waitUntil: "load" });
      break;
    case "click":
      if (step.selector) await page.click(step.selector);
      else await page.getByRole("button", { name: step.text, exact: false }).first().click();
      break;
    case "type": // human-speed typing into a field, for the camera
      await page.click(step.selector);
      await page.keyboard.type(step.value, { delay: step.delayMs ?? 45 });
      break;
    case "select":
      await page.selectOption(step.selector, step.value);
      break;
    case "wait":
      await sleep(step.ms);
      break;
    case "waitFor":
      await page.waitForSelector(step.selector, { state: step.state || "visible", timeout: (step.timeoutS || 60) * 1000 });
      break;
    case "say": {
      if (step.when === "listening") {
        // The board must speak after the candidate's last clip, then hand the turn back.
        const spoke = await waitForEvent(boardSpoke, lastSayEnd, step.timeoutS || 120);
        await waitForEvent(listening, spoke.t, step.timeoutS || 120);
        await sleep(step.gapMs ?? 800); // a person takes a breath before answering
      }
      await page.evaluate(([url, label]) => window.__demo.say(url, label), [`/__demo/clips/${step.clip}`, step.clip]);
      lastSayEnd = Date.now();
      break;
    }
    case "waitEvent":
      await waitForEvent((d) => d.event === step.event, lastStepT, step.timeoutS || 120);
      break;
    case "scrollTo": // a smooth scroll, for the camera
      await page.evaluate(async ([sel, ms, offset]) => {
        const el = sel === "top" ? null : document.querySelector(sel);
        const target = el ? el.getBoundingClientRect().top + scrollY - offset : 0;
        const start = scrollY, t0 = performance.now();
        await new Promise((done) => {
          const tick = (now) => {
            const k = Math.min(1, (now - t0) / ms);
            const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
            scrollTo(0, start + (target - start) * e);
            k < 1 ? requestAnimationFrame(tick) : done();
          };
          requestAnimationFrame(tick);
        });
      }, [step.selector, step.ms ?? 1600, step.offset ?? 96]);
      break;
    case "eval":
      await page.evaluate(step.js);
      break;
    default:
      throw new Error(`unknown step ${step.do}`);
  }
  lastStepT = Date.now();
}

let lastSayEnd = 0;
let lastStepT = 0;
let failed = null;
await page.goto(run.url, { waitUntil: "load" });
await sleep(800);
await cdp.send("Page.startScreencast", { format: "jpeg", quality: run.jpegQuality ?? 90, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
await page.evaluate(() => window.__demo.startRecording());
mark("recording_started");
try {
  for (const step of run.steps) await doStep(step);
  await sleep(run.tailMs ?? 1500);
} catch (err) {
  failed = String(err);
  mark("failed", { error: failed });
}
await page.evaluate(() => window.__demo.stopRecording());
await cdp.send("Page.stopScreencast");
await sleep(600);
const log = await page.evaluate(() => window.__demo.log);
await browser.close();

fs.writeFileSync(path.join(runDir, "audio.webm"), Buffer.concat(audioChunks));
fs.writeFileSync(path.join(runDir, "frames.json"), JSON.stringify(frames));
const recStart = log.find((e) => e.kind === "rec_start")?.t ?? frames[0]?.t;
fs.writeFileSync(path.join(runDir, "events.json"), JSON.stringify({ run: path.basename(runFile), recStart, firstFrame: frames[0]?.t, steps, log, failed }, null, 1));

const video = framesToMp4(runDir, [W, H]);
fs.writeFileSync(path.join(runDir, "events.json"), JSON.stringify({ run: path.basename(runFile), recStart, firstFrame: video.first, steps, log, failed }, null, 1));
console.log(`\n${failed ? "FAILED: " + failed : "done"} · ${frames.length} frames · audio ${(Buffer.concat(audioChunks).length / 1024).toFixed(0)} KB · ${runDir}`);
process.exit(failed ? 1 : 0);
