// One particle cloud behind the whole page, drawn with plain WebGL (no library, no
// third-party script). It changes shape with the page:
//   home   -> a breathing orb that turns slowly
//   form   -> the orb opens into a spiral galaxy
//   room   -> it gathers into a brain that answers the voice:
//             the board speaking = violet, with a band of light sweeping through it;
//             you speaking = mint, rippling with your microphone level;
//             the board thinking = it turns faster and sparks
//   report -> five lit clusters, one per member, joined by threads
// It follows the pointer (pointermove) everywhere. prefers-reduced-motion keeps it still:
// no morphing, no ripples, no sparks, a very slow turn.
// API used by app.js: Presence.show(view), .who("board"|"you"|""), .level(0-100), .pulse(),
// .state(serverState). Nothing here touches the audio path.

(function () {
  const canvas = document.getElementById("field");
  if (!canvas) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const gl = canvas.getContext("webgl", { alpha: true, antialias: false, premultipliedAlpha: true, powerPreference: "high-performance" });
  const noop = () => {};
  if (!gl) {
    window.Presence = { show: noop, who: noop, level: noop, pulse: noop, state: noop };
    return;
  }

  const small = () => window.innerWidth < 900;
  const N = small() ? 14000 : 30000;

  /* ---------- seeded random, so the shapes are the same on every load ---------- */
  let seed = 7;
  const rnd = () => {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const gauss = () => {
    let u = 0, v = 0;
    while (!u) u = rnd();
    while (!v) v = rnd();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  };
  const sphereDir = () => {
    const z = rnd() * 2 - 1, a = rnd() * Math.PI * 2, r = Math.sqrt(1 - z * z);
    return [r * Math.cos(a), z, r * Math.sin(a)];
  };
  const sn = (x, y, z) => Math.sin(x * 1.7 + Math.sin(y * 2.3)) * Math.cos(y * 1.9 + Math.sin(z * 1.3)) * Math.sin(z * 2.1 + Math.sin(x * 1.1));

  /* ---------- the four shapes ---------- */
  function orb() {
    const out = new Float32Array(N * 3), R = 0.92, r = 0.62;
    for (let i = 0; i < N; i += 1) {
      let u, v;
      do { u = rnd() * Math.PI * 2; v = rnd() * Math.PI * 2; } while (rnd() > (R + r * Math.cos(v)) / (R + r));
      const nx = Math.cos(v) * Math.cos(u), ny = Math.cos(v) * Math.sin(u), nz = Math.sin(v);
      const cx = R * Math.cos(u), cy = R * Math.sin(u);
      const d = r * (1 + 0.22 * sn(nx * 2.2 + cx, ny * 2.2 + cy, nz * 2.2)) + gauss() * 0.018;
      out[i * 3] = cx + nx * d; out[i * 3 + 1] = cy + ny * d; out[i * 3 + 2] = nz * d;
    }
    return out;
  }

  function galaxy() {
    const out = new Float32Array(N * 3);
    for (let i = 0; i < N; i += 1) {
      let x, y, z;
      if (rnd() < 0.16) {
        x = gauss() * 0.2; y = gauss() * 0.08; z = gauss() * 0.2;
      } else {
        const arm = rnd() < 0.5 ? 0 : Math.PI, t = Math.pow(rnd(), 0.85), rad = 0.25 + t * 2.15;
        const ang = arm + rad * 2.35 + gauss() * (0.16 + 0.12 * t);
        const j = 0.04 + 0.07 * rad;
        x = Math.cos(ang) * rad + gauss() * j; z = Math.sin(ang) * rad + gauss() * j; y = gauss() * (0.05 + 0.03 * (1 - t));
      }
      out[i * 3] = x; out[i * 3 + 1] = y; out[i * 3 + 2] = z;
    }
    return out;
  }

  function brain() {
    const out = new Float32Array(N * 3);
    const ridge = (x, y, z) => {
      const a = Math.sin(x * 7.5 + Math.sin(z * 4.2) * 1.6 + y * 2.0);
      const b = Math.sin(z * 8.5 + Math.sin(y * 5.1) * 1.4 - x * 1.5);
      const c = Math.sin(y * 9.0 + Math.sin(x * 3.7) * 1.8 + z * 2.2);
      return 1 - Math.abs(a * 0.45 + b * 0.35 + c * 0.3);
    };
    let i = 0;
    while (i < N) {
      const k = rnd();
      let x, y, z;
      if (k < 0.89) { // cerebrum: two hemispheres, points kept mostly on the folds
        const s = rnd() < 0.5 ? -1 : 1;
        const [ux, uy, uz] = sphereDir();
        const medial = ux * s < 0;
        const rx = medial ? 0.1 : 0.6, ry = uy < 0 ? 0.42 + 0.12 * Math.max(0, uz) : 0.66, rz = uz > 0 ? 0.95 : 1.05;
        const rg = ridge(ux * 1.2 + s * 0.3, uy * 1.2, uz * 1.2);
        if (!medial && rnd() > 0.18 + 0.82 * Math.pow(rg, 2.2)) continue;
        const fold = 1 + 0.055 * rg;
        const fill = rnd() < 0.06 ? 0.7 + rnd() * 0.25 : 1;
        x = s * 0.055 + ux * rx * fold * fill;
        y = 0.16 + uy * ry * fold * fill;
        z = uz * rz * fold * fill;
        if (uy < 0 && uz > -0.4 && uz < 0.55 && !medial) { y -= 0.1 * -uy; x += s * 0.04 * -uy; }
        if (uz < -0.5) y += 0.05 * (uz + 0.5);
      } else if (k < 0.97) { // cerebellum
        const s = rnd() < 0.5 ? -1 : 1;
        const [ux, uy, uz] = sphereDir();
        if (rnd() > 0.3 + 0.7 * Math.abs(Math.sin(uy * 14))) continue;
        x = s * 0.2 + ux * 0.3; y = -0.36 + uy * 0.17; z = -0.6 + uz * 0.26;
      } else { // brainstem
        const t = rnd(), a = rnd() * Math.PI * 2, rr = 0.075 * (1 - t * 0.3);
        x = Math.cos(a) * rr; z = -0.22 + t * 0.06 + Math.sin(a) * rr; y = -0.22 - t * 0.5;
      }
      out[i * 3] = x + gauss() * 0.006; out[i * 3 + 1] = y + gauss() * 0.006; out[i * 3 + 2] = z + gauss() * 0.006;
      i += 1;
    }
    return out;
  }

  const groups = new Float32Array(N).fill(-1);
  function constellation() {
    const out = new Float32Array(N * 3);
    const cs = [-1.9, -0.95, 0, 0.95, 1.9].map((x, i) => [x, 0.18 - 0.1 * x * x, (i % 2) * 0.15]);
    for (let i = 0; i < N; i += 1) {
      const k = rnd();
      let x, y, z;
      if (k < 0.8) {
        const g = Math.floor(rnd() * 5), c = cs[g], sz = g === 0 ? 0.27 : 0.2;
        const [ux, uy, uz] = sphereDir(), rr = sz * Math.pow(rnd(), 0.5);
        x = c[0] + ux * rr; y = c[1] + uy * rr; z = c[2] + uz * rr; groups[i] = g;
      } else if (k < 0.93) {
        const g = Math.floor(rnd() * 4), a = cs[g], b = cs[g + 1], t = rnd();
        const sag = Math.sin(t * Math.PI) * -0.08;
        x = a[0] + (b[0] - a[0]) * t + gauss() * 0.012;
        y = a[1] + (b[1] - a[1]) * t + sag + gauss() * 0.012;
        z = a[2] + (b[2] - a[2]) * t;
      } else {
        x = gauss() * 1.6; y = gauss() * 0.5; z = gauss() * 0.6;
      }
      out[i * 3] = x; out[i * 3 + 1] = y; out[i * 3 + 2] = z;
    }
    return out;
  }

  const SHAPES = { home: orb(), form: galaxy(), room: brain(), report: constellation() };
  SHAPES.choose = SHAPES.home; // the mode chooser: the same orb, smaller and out of the text

  // Where the cloud sits on each page. Wide screens first, then phones.
  function layoutFor(view) {
    const s = small();
    switch (view) {
      case "form": return { pos: s ? [0, 0.95, 0] : [-1.05, -0.32, 0], scale: s ? 0.4 : 0.78, tilt: 0.42, spin: 0.07, wobble: 0, bright: 0.95 };
      case "room": return { pos: s ? [0, -0.42, 0] : [0, 0.02, 0], scale: s ? 0.4 : 0.98, tilt: 0.12, spin: 0.11, wobble: 0, bright: s ? 0.6 : 1 };
      case "choose": return { pos: s ? [0.55, -1.25, 0] : [1.5, 0.3, 0], scale: s ? 0.28 : 0.5, tilt: 0.35, spin: 0.16, wobble: 0.35, bright: 0.6 };
      case "report": return { pos: s ? [0, 1.25, 0] : [0, 0.98, 0], scale: s ? 0.36 : 0.82, tilt: 0, spin: 0, wobble: 0, bright: 0.72 };
      default: return { pos: s ? [0.3, -0.95, 0] : [1.08, 0.12, 0], scale: s ? 0.34 : 0.72, tilt: 0.35, spin: 0.16, wobble: 0.35, bright: 1 };
    }
  }

  /* ---------- buffers ---------- */
  const posA = new Float32Array(SHAPES.home), posB = new Float32Array(SHAPES.home);
  const seeds = new Float32Array(N), sizes = new Float32Array(N), scat = new Float32Array(N * 3);
  for (let i = 0; i < N; i += 1) {
    seeds[i] = rnd();
    sizes[i] = rnd() < 0.015 ? 2.6 + rnd() * 1.6 : 0.55 + rnd() * 1.05;
    scat[i * 3] = gauss(); scat[i * 3 + 1] = gauss(); scat[i * 3 + 2] = gauss();
  }

  const VS = `
    attribute vec3 aPosA; attribute vec3 aPosB; attribute float aSeed; attribute float aSize; attribute vec3 aScat; attribute float aGroup;
    uniform mat4 uMV; uniform mat4 uP;
    uniform float uTime, uMix, uScatter, uPixel, uSize, uAspect, uEnergy, uWave, uSpark, uHue, uRipple, uFocus, uBright, uPulse;
    uniform vec2 uMouse;
    varying vec3 vColor;
    float h(float n) { return fract(sin(n) * 43758.5453); }
    void main() {
      float m = clamp(uMix * 1.5 - aSeed * 0.5, 0.0, 1.0);
      m = m * m * (3.0 - 2.0 * m);
      vec3 p = mix(aPosA, aPosB, m) + aScat * sin(m * 3.14159) * uScatter;
      vec3 n = normalize(p + vec3(0.0001));
      p += n * (sin(uTime * 0.8 + aSeed * 6.283) * 0.01 + sin(uTime * 0.35 + p.y * 2.0) * 0.014);
      p += n * sin(length(p) * 13.0 - uTime * 7.0) * 0.03 * uRipple;
      vec4 mv = uMV * vec4(p, 1.0);
      vec4 clip = uP * mv;
      vec2 d = clip.xy / clip.w - uMouse;
      d.x *= uAspect;
      float dl = length(d);
      float near = smoothstep(0.32, 0.0, dl);
      vec2 dir = dl > 0.0001 ? d / dl : vec2(0.0);
      dir.x /= uAspect;
      clip.xy += dir * near * 0.07 * clip.w;
      gl_Position = clip;

      float f = clamp(0.5 - p.y * 0.34 + p.x * 0.14 + (aSeed - 0.5) * 0.55 + uHue * 0.32 + (1.0 - smoothstep(0.0, 0.55, length(p))) * 0.55, 0.0, 1.0);
      vec3 mint = vec3(0.37, 0.95, 0.76), teal = vec3(0.25, 0.74, 0.86), violet = vec3(0.5, 0.37, 1.0);
      vec3 c = f < 0.5 ? mix(mint, teal, f * 2.0) : mix(teal, violet, (f - 0.5) * 2.0);
      float band = exp(-pow((p.z - uWave) * 3.2, 2.0)) * uEnergy;
      float spark = step(0.992, h(aSeed * 733.0 + floor(uTime * 9.0))) * uSpark;
      float b = 0.62 + band * 1.35 + near * 1.1 + uPulse * 0.35 + (aSeed > 0.985 ? 0.8 : 0.0);
      float foc = 1.0;
      if (uFocus > -0.5) foc = abs(aGroup - uFocus) < 0.5 ? 2.1 : 0.45;
      vColor = (c * b + vec3(1.0) * (spark * 1.6 + near * 0.25 + band * 0.2)) * foc * uBright;
      gl_PointSize = uSize * aSize * uPixel * (1.0 + band * 0.7 + spark * 1.6 + near * 0.5) / -mv.z;
    }`;
  const FS = `
    precision mediump float;
    varying vec3 vColor;
    void main() {
      float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
      gl_FragColor = vec4(vColor * a * 0.9, a * 0.9);
    }`;
  const STAR_VS = `
    attribute vec3 aPos; uniform mat4 uMV; uniform mat4 uP; uniform float uPixel;
    void main() { vec4 mv = uMV * vec4(aPos, 1.0); gl_Position = uP * mv; gl_PointSize = 2.2 * uPixel; }`;
  const STAR_FS = `
    precision mediump float;
    void main() { float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5)) * 0.5; gl_FragColor = vec4(vec3(0.62, 0.91, 0.85) * a, a); }`;

  function program(vs, fs) {
    const make = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader");
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, make(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, make(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || "link");
    return p;
  }

  let prog, starProg;
  try {
    prog = program(VS, FS);
    starProg = program(STAR_VS, STAR_FS);
  } catch (err) {
    window.Presence = { show: noop, who: noop, level: noop, pulse: noop, state: noop };
    return;
  }

  function buffer(data) {
    const b = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
    return b;
  }
  const buf = {
    aPosA: [buffer(posA), 3], aPosB: [buffer(posB), 3], aSeed: [buffer(seeds), 1],
    aSize: [buffer(sizes), 1], aScat: [buffer(scat), 3], aGroup: [buffer(groups), 1],
  };
  const loc = {};
  for (const name of Object.keys(buf)) loc[name] = gl.getAttribLocation(prog, name);
  const U = {};
  for (const name of ["uMV", "uP", "uTime", "uMix", "uScatter", "uPixel", "uSize", "uAspect", "uEnergy", "uWave",
    "uSpark", "uHue", "uRipple", "uFocus", "uBright", "uPulse", "uMouse"]) U[name] = gl.getUniformLocation(prog, name);

  const STARS = 2400, starPos = new Float32Array(STARS * 3);
  for (let i = 0; i < STARS; i += 1) {
    const [x, y, z] = sphereDir(), r = 9 + rnd() * 14;
    starPos[i * 3] = x * r; starPos[i * 3 + 1] = y * r; starPos[i * 3 + 2] = z * r - 6;
  }
  const starBuf = buffer(starPos);
  const starLoc = gl.getAttribLocation(starProg, "aPos");
  const SU = { uMV: gl.getUniformLocation(starProg, "uMV"), uP: gl.getUniformLocation(starProg, "uP"), uPixel: gl.getUniformLocation(starProg, "uPixel") };

  /* ---------- small matrix helpers (column-major) ---------- */
  const mul = (a, b) => {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c += 1) {
      for (let r = 0; r < 4; r += 1) {
        o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
      }
    }
    return o;
  };
  const ident = () => { const m = new Float32Array(16); m[0] = m[5] = m[10] = m[15] = 1; return m; };
  const translate = (x, y, z) => { const m = ident(); m[12] = x; m[13] = y; m[14] = z; return m; };
  const scaleM = (s) => { const m = ident(); m[0] = m[5] = m[10] = s; return m; };
  const rotX = (a) => { const m = ident(), c = Math.cos(a), s = Math.sin(a); m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m; };
  const rotY = (a) => { const m = ident(), c = Math.cos(a), s = Math.sin(a); m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m; };
  const rotZ = (a) => { const m = ident(), c = Math.cos(a), s = Math.sin(a); m[0] = c; m[1] = s; m[4] = -s; m[5] = c; return m; };
  const perspective = (fovDeg, aspect, near, far) => {
    const f = 1 / Math.tan((fovDeg * Math.PI) / 360), m = new Float32Array(16);
    m[0] = f / aspect; m[5] = f; m[10] = (far + near) / (near - far); m[11] = -1; m[14] = (2 * far * near) / (near - far);
    return m;
  };
  const VIEW = translate(0, 0, -5.4);

  /* ---------- state ---------- */
  let view = "home";
  let L = layoutFor(view);
  const cur = { x: L.pos[0], y: L.pos[1], z: L.pos[2], s: L.scale, tilt: L.tilt, ix: 0, rz: 0, bright: L.bright };
  let rotYAngle = 0;
  let mixStart = -10;
  let scatter = 0.45;
  let who = "";
  let serverState = "";
  let micLevel = 0;
  let pulse = 0;
  let energy = 0, hue = 0, spark = 0, ripple = 0;
  let focus = -1;
  const mouse = { x: 9, y: 9, tx: 9, ty: 9 };
  let pixel = 1, aspect = 1, proj = perspective(38, 1, 0.1, 100);

  function mixNow() {
    if (reduced) return 1;
    return Math.min(1, (performance.now() / 1000 - mixStart) / 2.1);
  }

  // Freeze the blend the eye sees into A, so a new morph starts from there.
  function snapshot() {
    const t = mixNow();
    for (let i = 0; i < N; i += 1) {
      let m = Math.min(1, Math.max(0, t * 1.5 - seeds[i] * 0.5));
      m = m * m * (3 - 2 * m);
      const s = reduced ? 0 : Math.sin(m * Math.PI) * scatter;
      for (let k = 0; k < 3; k += 1) {
        const j = i * 3 + k;
        posA[j] = posA[j] + (posB[j] - posA[j]) * m + scat[j] * s;
      }
    }
  }

  function morphTo(name) {
    snapshot();
    posB.set(SHAPES[name]);
    gl.bindBuffer(gl.ARRAY_BUFFER, buf.aPosA[0]); gl.bufferSubData(gl.ARRAY_BUFFER, 0, posA);
    gl.bindBuffer(gl.ARRAY_BUFFER, buf.aPosB[0]); gl.bufferSubData(gl.ARRAY_BUFFER, 0, posB);
    scatter = name === "form" ? 0.75 : 0.45;
    mixStart = reduced ? -10 : performance.now() / 1000;
  }

  function resize() {
    pixel = Math.min(window.devicePixelRatio || 1, 1.75);
    const w = Math.round(window.innerWidth * pixel), h = Math.round(window.innerHeight * pixel);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    aspect = window.innerWidth / Math.max(1, window.innerHeight);
    proj = perspective(38, aspect, 0.1, 100);
    gl.viewport(0, 0, w, h);
    L = layoutFor(view);
  }

  window.addEventListener("resize", resize);
  window.addEventListener("pointermove", (e) => {
    mouse.tx = (e.clientX / window.innerWidth) * 2 - 1;
    mouse.ty = -(e.clientY / window.innerHeight) * 2 + 1;
  }, { passive: true });
  document.addEventListener("pointerleave", () => { mouse.tx = 9; mouse.ty = 9; });

  // Report: hovering a member's block lights that member's cluster.
  document.addEventListener("pointerover", (e) => {
    if (view !== "report") return;
    const block = e.target.closest && e.target.closest("#report-grouped [data-member]");
    focus = block ? Number(block.dataset.member) || 0 : -1;
  });

  let lost = false;
  canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); lost = true; });

  const lerp = (a, b, t) => a + (b - a) * t;
  let last = performance.now();

  function frame(nowMs) {
    requestAnimationFrame(frame);
    if (lost || document.hidden) { last = nowMs; return; }
    const dt = Math.min(0.05, (nowMs - last) / 1000);
    last = nowMs;
    const t = nowMs / 1000;
    const k = 1 - Math.pow(0.0025, dt);

    mouse.x = mouse.tx > 5 ? mouse.tx : lerp(mouse.x > 5 ? mouse.tx : mouse.x, mouse.tx, 0.12);
    mouse.y = mouse.ty > 5 ? mouse.ty : lerp(mouse.y > 5 ? mouse.ty : mouse.y, mouse.ty, 0.12);

    // Scrolling away on the long pages lifts and dims the cloud, so text stays readable.
    const scrolled = view === "report" || view === "home" ? Math.min(1, window.scrollY / window.innerHeight) : 0;
    cur.x = lerp(cur.x, L.pos[0], k);
    cur.y = lerp(cur.y, L.pos[1] + scrolled * 2.2, k);
    cur.z = lerp(cur.z, L.pos[2], k);
    cur.s = lerp(cur.s, L.scale, k);
    cur.tilt = lerp(cur.tilt, L.tilt + (mouse.y < 5 ? mouse.y * 0.06 : 0), k);
    cur.bright = lerp(cur.bright, L.bright * (1 - scrolled * 0.85), k);
    const thinking = serverState === "thinking";
    const sp = reduced ? L.spin * 0.15 : L.spin * (thinking ? 2.6 : 1);
    if (L.spin > 0) rotYAngle += sp * dt;
    else rotYAngle = lerp(rotYAngle, Math.round(rotYAngle / (Math.PI * 2)) * Math.PI * 2 + (reduced ? 0 : Math.sin(t * 0.3) * 0.18), k);
    cur.rz = lerp(cur.rz, reduced ? 0 : Math.sin(t * 0.21) * L.wobble, k);
    cur.ix = lerp(cur.ix, view === "home" && !reduced ? Math.sin(t * 0.17) * 0.4 : 0, k);

    // Voice: the board's energy is a gentle synthetic rhythm while it speaks (the page does not
    // tap the board's audio); yours follows the real microphone level from app.js.
    const inRoom = view === "room";
    micLevel *= Math.pow(0.04, dt);
    pulse *= Math.pow(0.08, dt);
    let target = 0;
    if (inRoom && who === "board") {
      const sy = 0.5 + 0.5 * Math.sin(t * 9) * Math.sin(t * 3.7 + 1);
      target = 0.55 + 0.45 * sy;
    } else if (inRoom && who === "you") {
      target = 0.3 + 0.7 * micLevel;
    }
    energy = lerp(energy, target, 0.18);
    hue = lerp(hue, inRoom ? (who === "board" ? 1 : who === "you" ? -1 : 0.2) : 0, 0.04);
    spark = lerp(spark, inRoom && thinking ? 1 : inRoom && who === "board" ? 0.25 : 0, 0.06);
    ripple = lerp(ripple, inRoom && who === "you" ? energy : 0, 0.12);

    const model = mul(translate(cur.x, cur.y, cur.z), mul(scaleM(cur.s), mul(rotX(cur.tilt + cur.ix), mul(rotY(rotYAngle + (inRoom ? -0.9 : 0)), rotZ(cur.rz)))));
    const mv = mul(VIEW, model);

    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    gl.disable(gl.DEPTH_TEST);

    // far stars
    gl.useProgram(starProg);
    gl.uniformMatrix4fv(SU.uMV, false, mul(VIEW, rotY(t * 0.006)));
    gl.uniformMatrix4fv(SU.uP, false, proj);
    gl.uniform1f(SU.uPixel, pixel);
    for (const name of Object.keys(buf)) if (loc[name] >= 0) gl.disableVertexAttribArray(loc[name]);
    gl.bindBuffer(gl.ARRAY_BUFFER, starBuf);
    gl.enableVertexAttribArray(starLoc);
    gl.vertexAttribPointer(starLoc, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.POINTS, 0, STARS);
    gl.disableVertexAttribArray(starLoc);

    // the cloud
    gl.useProgram(prog);
    for (const name of Object.keys(buf)) {
      if (loc[name] < 0) continue;
      gl.bindBuffer(gl.ARRAY_BUFFER, buf[name][0]);
      gl.enableVertexAttribArray(loc[name]);
      gl.vertexAttribPointer(loc[name], buf[name][1], gl.FLOAT, false, 0, 0);
    }
    gl.uniformMatrix4fv(U.uMV, false, mv);
    gl.uniformMatrix4fv(U.uP, false, proj);
    gl.uniform1f(U.uTime, reduced ? 0 : t);
    gl.uniform1f(U.uMix, mixNow());
    gl.uniform1f(U.uScatter, reduced ? 0 : scatter);
    gl.uniform1f(U.uPixel, pixel);
    gl.uniform1f(U.uSize, small() ? 14 : 19);
    gl.uniform1f(U.uAspect, aspect);
    gl.uniform1f(U.uEnergy, who === "board" ? energy : energy * 0.25);
    gl.uniform1f(U.uWave, Math.sin(t * 1.6) * 1.1);
    gl.uniform1f(U.uSpark, reduced ? 0 : spark);
    gl.uniform1f(U.uHue, hue);
    gl.uniform1f(U.uRipple, reduced ? 0 : ripple);
    gl.uniform1f(U.uFocus, view === "report" ? focus : -1);
    gl.uniform1f(U.uBright, cur.bright);
    gl.uniform1f(U.uPulse, pulse);
    gl.uniform2f(U.uMouse, mouse.x, mouse.y);
    gl.drawArrays(gl.POINTS, 0, N);
  }

  window.Presence = {
    show(name) {
      const next = SHAPES[name] ? name : "home";
      document.body.dataset.view = next;
      if (next === view) return;
      view = next;
      L = layoutFor(view);
      focus = -1;
      morphTo(view);
    },
    who(next) { who = next || ""; },
    level(next) {
      const n = Math.max(0, Math.min(100, Number(next) || 0)) / 100;
      if (n > micLevel) micLevel = n;
    },
    pulse() { pulse = 1; },
    state(next) { serverState = next || ""; },
  };

  resize();
  document.body.dataset.view = view;
  requestAnimationFrame(frame);
})();
