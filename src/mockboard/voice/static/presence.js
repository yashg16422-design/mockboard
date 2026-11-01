// A point field. On the home page it follows the pointer. In the room it
// follows whoever is speaking, and each caption sends a pulse through it.
// prefers-reduced-motion draws one still frame and then stops.

(function () {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const homeCanvas = document.getElementById("home-field");
  const roomCanvas = document.getElementById("room-field");
  const homeNote = document.getElementById("home-field-note");
  const roomNote = document.getElementById("room-field-note");
  const homeBox = document.getElementById("home-presence");
  const roomBox = document.getElementById("room-presence");
  if (!homeCanvas || !roomCanvas) return;

  const NOTES = {
    home: "Move across the field. It follows your pointer.",
    idle: "Quiet. It is waiting for a voice.",
    board: "The board is speaking, so the field moves with it.",
    you: "It hears you.",
  };

  const mint = [125, 232, 198];
  const violet = [138, 124, 255];
  const peach = [227, 154, 106];

  function mix(a, b, t) {
    return [
      a[0] + (b[0] - a[0]) * t,
      a[1] + (b[1] - a[1]) * t,
      a[2] + (b[2] - a[2]) * t,
    ];
  }

  function rgba(rgb, alpha) {
    return "rgba(" + (rgb[0] | 0) + "," + (rgb[1] | 0) + "," + (rgb[2] | 0) + "," + alpha + ")";
  }

  const palette = [];
  for (let i = 0; i < 24; i += 1) {
    const t = i / 23;
    const ring = t < 0.5 ? mix(mint, violet, t * 2) : mix(violet, mint, (t - 0.5) * 2);
    palette.push(rgba(ring, 1));
  }
  const peachColor = rgba(peach, 1);

  function makePoints(n) {
    const points = [];
    for (let i = 0; i < n; i += 1) {
      points.push({
        ring: Math.random() * Math.PI * 2,
        tube: Math.random() * Math.PI * 2,
        tubeR: 0.45 + Math.random() * 1.05,
        tw: Math.random(),
        hue: (Math.random() * 24) | 0,
      });
    }
    return points;
  }

  const count = Math.min(window.innerWidth || 1200, 1400) < 800 ? 1400 : 2800;
  const points = makePoints(reduced ? Math.min(count, 700) : count);
  const comet = [];
  for (let i = 0; i < 26; i += 1) comet.push({ x: 0.5, y: 0.52 });

  let active = "home";
  let who = "";
  let level = 0;
  let pulse = 0;
  let running = false;
  let frame = 0;
  const pointer = {
    home: { x: 0.5, y: 0.46, inside: false },
    room: { x: 0.5, y: 0.5, inside: false },
  };

  function canvasFor(name) {
    return name === "room" ? roomCanvas : homeCanvas;
  }

  function boxFor(name) {
    return name === "room" ? roomBox : homeBox;
  }

  function resize(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (rect.width < 2 || rect.height < 2) return false;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    return true;
  }

  function paint(canvas, name, time) {
    if (!resize(canvas)) return false;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    const cx = w * 0.5;
    const cy = h * (name === "home" ? 0.48 : 0.5);
    const scale = Math.min(w, h);
    const energy = who === "you" ? level : who === "board" ? 0.65 : 0;
    const R = scale * (0.34 + energy * 0.05 + pulse * 0.05);
    const tube = scale * (0.11 + (who === "board" ? 0.03 : 0) + pulse * 0.02);
    const spin = reduced ? 0.6 : time * (who === "board" ? 0.62 : 0.2);
    const tilt = 1.05;
    const cosTilt = Math.cos(tilt);
    const sinTilt = Math.sin(tilt);
    const aim = pointer[name];

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#050505";
    ctx.fillRect(0, 0, w, h);

    const glow = ctx.createRadialGradient(cx, cy, scale * 0.02, cx, cy, scale * 0.48);
    const glowMint = who === "you" ? 0.05 : 0.14 + pulse * 0.12;
    const glowViolet = who === "you" ? 0.04 : 0.07;
    glow.addColorStop(0, "rgba(125,232,198," + glowMint + ")");
    glow.addColorStop(0.42, "rgba(138,124,255," + glowViolet + ")");
    glow.addColorStop(1, "rgba(5,5,5,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);

    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < points.length; i += 1) {
      const p = points[i];
      const around = p.ring + spin + p.tw * 0.15;
      const aroundTube = p.tube + spin * (who === "board" ? 2.1 : 1.3);
      const rr = tube * p.tubeR;
      const ringX = R + rr * Math.cos(aroundTube);
      const x3 = ringX * Math.cos(around);
      const y3 = rr * Math.sin(aroundTube);
      const z3 = ringX * Math.sin(around);
      const y2 = y3 * cosTilt - z3 * sinTilt;
      const z2 = y3 * sinTilt + z3 * cosTilt;
      let sx = cx + x3;
      let sy = cy + y2;
      const depth = Math.max(0, Math.min(1, z2 / (R + tube + 1) * 0.5 + 0.5));
      const hue = ((around % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
      const hueIndex = (hue / (Math.PI * 2) * 23) | 0;
      if (aim.inside) {
        const dx = aim.x * w - sx;
        const dy = aim.y * h - sy;
        const dist = Math.hypot(dx, dy) || 1;
        const reach = scale * (name === "home" ? 0.34 : 0.22);
        if (dist < reach) {
          const pull = (1 - dist / reach) * (name === "home" ? 28 : 14 + energy * 26);
          sx += (dx / dist) * pull;
          sy += (dy / dist) * pull;
        }
      }
      if (who === "you" && name === "room") {
        sx += Math.sin(p.tube * 9 + time * 7) * level * scale * 0.012;
        sy += Math.cos(p.ring * 8 + time * 7) * level * scale * 0.012;
      }
      const alpha = (0.22 + depth * 0.78) * (0.4 + p.tw * 0.6) * (who === "board" && name === "room" ? 1 : 0.82);
      const size = (1.15 + depth * 2.3) * (who === "board" && name === "room" ? 1.2 : 1);
      ctx.globalAlpha = Math.max(0, Math.min(1, alpha));
      ctx.fillStyle = who === "you" && name === "room" && p.tw > 0.72 ? peachColor : palette[hueIndex];
      ctx.fillRect(sx, sy, size, size);
    }

    let leadX = aim.x;
    let leadY = aim.y;
    if (name === "room" && who === "board") {
      leadX = 0.5 + Math.cos(time * 0.8) * 0.08;
      leadY = 0.5 + Math.sin(time * 0.6) * 0.06;
    }
    for (let i = 0; i < comet.length; i += 1) {
      const prev = i === 0 ? { x: leadX, y: leadY } : comet[i - 1];
      const follow = i === 0 ? 0.18 : 0.35;
      comet[i].x += (prev.x - comet[i].x) * follow;
      comet[i].y += (prev.y - comet[i].y) * follow;
      const c = comet[i];
      ctx.globalAlpha = 0.15 + (1 - i / comet.length) * 0.45;
      ctx.fillStyle = i < 4 ? peachColor : palette[(i * 3) % 24];
      const s = (name === "room" && who === "you" ? 3.2 + level * 4 : 2.4) * (1 - i / comet.length);
      ctx.fillRect(c.x * w, c.y * h, s, s);
    }

    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    return true;
  }

  function loop(now) {
    frame = requestAnimationFrame(loop);
    const time = (now || 0) / 1000;
    if (document.hidden) return;
    paint(canvasFor(active), active, time);
    if (pulse > 0) pulse *= 0.92;
    if (level > 0 && who !== "you") level *= 0.9;
  }

  function start() {
    if (reduced) {
      let tries = 0;
      const draw = () => {
        tries += 1;
        if (active && !paint(canvasFor(active), active, 0.8) && tries < 30) {
          requestAnimationFrame(draw);
        }
      };
      requestAnimationFrame(draw);
      return;
    }
    if (!running) {
      running = true;
      frame = requestAnimationFrame(loop);
    }
  }

  function stop() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    running = false;
  }

  function bind(box, name) {
    const aim = pointer[name];
    const move = (clientX, clientY) => {
      const rect = box.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      aim.x = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      aim.y = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height));
      aim.inside = true;
    };
    box.addEventListener("pointermove", (e) => move(e.clientX, e.clientY));
    box.addEventListener("pointerleave", () => { aim.inside = false; });
    box.addEventListener("pointerdown", (e) => move(e.clientX, e.clientY));
    box.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 0.08 : 0.03;
      if (e.key === "ArrowLeft") aim.x = Math.max(0, aim.x - step);
      else if (e.key === "ArrowRight") aim.x = Math.min(1, aim.x + step);
      else if (e.key === "ArrowUp") aim.y = Math.max(0, aim.y - step);
      else if (e.key === "ArrowDown") aim.y = Math.min(1, aim.y + step);
      else return;
      aim.inside = true;
      e.preventDefault();
    });
  }

  bind(homeBox, "home");
  bind(roomBox, "room");

  function noteFor() {
    if (active === "home") return NOTES.home;
    if (who === "board") return NOTES.board;
    if (who === "you") return NOTES.you;
    return NOTES.idle;
  }

  function applyNote() {
    const text = noteFor();
    if (active === "home" && homeNote) {
      homeNote.textContent = reduced
        ? "Motion is reduced in this browser. The field stays still."
        : text;
    }
    if (active === "room" && roomNote) roomNote.textContent = text;
    const box = boxFor(active);
    if (box) box.dataset.who = active === "room" ? who : "";
  }

  window.Presence = {
    show(name) {
      active = name === "room" ? "room" : name === "home" ? "home" : "";
      if (!active) {
        stop();
        return;
      }
      applyNote();
      start();
    },
    who(next) {
      who = next || "";
      applyNote();
      if (reduced && active) paint(canvasFor(active), active, 0.8);
    },
    level(next) {
      const n = Math.max(0, Math.min(100, Number(next) || 0)) / 100;
      if (n > level) level = n;
    },
    pulse() {
      pulse = 1;
      if (reduced && active) paint(canvasFor(active), active, 0.8);
    },
  };

  window.addEventListener("resize", () => {
    if (active) paint(canvasFor(active), active, reduced ? 0.8 : performance.now() / 1000);
  });

  Presence.show("home");
})();
