// Screencast frames (variable rate, timestamped) to a constant 30 fps H.264 video.
//   node driver/frames2mp4.mjs runs/<name>
// Frames are sorted by their browser timestamp (they can arrive out of order) and each one is
// held until the next. The video starts at the first frame; events.json says when that was.
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

export function framesToMp4(runDir, [W, H] = [1920, 1080]) {
  const frames = JSON.parse(fs.readFileSync(path.join(runDir, "frames.json"), "utf8"))
    .sort((a, b) => a.t - b.t);
  const lines = ["ffconcat version 1.0"];
  frames.forEach((f, i) => {
    const next = frames[i + 1]?.t ?? f.t + 33;
    lines.push(`file 'frames/${f.file}'`, `duration ${Math.max(0.001, (next - f.t) / 1000).toFixed(4)}`);
  });
  lines.push(`file 'frames/${frames.at(-1).file}'`);
  fs.writeFileSync(path.join(runDir, "frames.txt"), lines.join("\n") + "\n");
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "frames.txt",
    "-vf", `fps=30,scale=${W}:${H}:flags=lanczos,format=yuv420p`, "-c:v", "libx264", "-preset", "slow",
    "-crf", "16", "-movflags", "+faststart", "screen.mp4"], { cwd: runDir });
  return { first: frames[0].t, last: frames.at(-1).t, count: frames.length };
}

if (process.argv[1] && process.argv[1].endsWith("frames2mp4.mjs")) {
  const out = framesToMp4(process.argv[2]);
  console.log(`screen.mp4 · ${out.count} frames · ${((out.last - out.first) / 1000).toFixed(1)} s`);
}
