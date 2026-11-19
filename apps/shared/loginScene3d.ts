/**
 * The sign-in picture, as an ink point cloud (the look comes from a set of generative / scan / data-art reels):
 *  - ~60k points burst from one hub as forking, swirling ink strands, leave long smeared trails, pull together into the inazina.
 *    wordmark, hold (crisp), tear into a colour-split glitch, and dissolve back; for a few seconds the paper turns to night.
 *  - a scrolling point-cloud ground, a ring of event ticks, radial hairlines, film grain + vignette, and a typed caption in a
 *    black highlight box with a mono readout, like the references. The i dots and the period stay accent blue; a few points
 *    flash copper in the glitch.
 *  - there is no audio on a sign-in page, so the "audio-reactive" part is an internal pulse (a steady beat) driving size and ticks.
 * three.js only (Points / LineSegments + small shaders); trails come from fading the previous frame instead of clearing it.
 * Loaded only on the sign-in screen (dynamic import); a still logo is shown where this is not suitable (see AuthPanel).
 */
import {
  BufferAttribute, BufferGeometry, LineSegments, Mesh, PerspectiveCamera, Points, Scene, ShaderMaterial, Vector2, Vector3, WebGLRenderer,
} from "three";
import { LETTERS, PERIOD } from "./Logo";

const DOTS = [{ x: 117, y: -634, r: 66 }, { x: 1813, y: -634, r: 66 }];
const S = 0.001;                              // one SVG unit in scene units
const CENTER = { x: 1636, up: 350 };          // wordmark centre in SVG units (up = -y)
const WORD_HALF_WIDTH = 1.64;
const FOV = 28;
const CYCLE = 16;                             // seconds per loop
const BEAT = 0.6;                             // seconds per pulse

const clamp01 = (t: number) => Math.min(1, Math.max(0, t));
const smooth = (t: number) => { t = clamp01(t); return t * t * (3 - 2 * t); };

/** Where the wordmark's pixels are: [black letters, blue dots + period], as scene-space sample points. */
function sampleWordmark(blackCount: number, blueCount: number) {
  const W = 1660, H = 400, k = 0.5;
  const read = (draw: (ctx: CanvasRenderingContext2D) => void) => {
    const canvas = document.createElement("canvas"); canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
    ctx.setTransform(k, 0, 0, k, 30, 380);
    draw(ctx);
    const data = ctx.getImageData(0, 0, W, H).data;
    const hits: number[] = [];
    for (let i = 3; i < data.length; i += 4) if (data[i] > 128) hits.push((i - 3) / 4);
    return hits;
  };
  const black = read((ctx) => { ctx.fillStyle = "#000"; for (const d of LETTERS) ctx.fill(new Path2D(d)); });
  const blue = read((ctx) => {
    ctx.fillStyle = "#000"; ctx.fill(new Path2D(PERIOD));
    for (const dot of DOTS) { ctx.beginPath(); ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2); ctx.fill(); }
  });
  const pick = (hits: number[], count: number) => {
    const out = new Float32Array(count * 2);
    for (let i = 0; i < count; i += 1) {
      const hit = hits[(Math.random() * hits.length) | 0];
      const px = (hit % W) + Math.random(), py = Math.floor(hit / W) + Math.random();
      out[i * 2] = ((px - 30) / k - CENTER.x) * S;
      out[i * 2 + 1] = (-((py - 380) / k) - CENTER.up) * S;
    }
    return out;
  };
  return { black: pick(black, blackCount), blue: pick(blue, blueCount) };
}

const HASH = /* glsl */`
float h11(float p) { p = fract(p * .1031); p *= p + 33.33; p *= p + p; return fract(p); }
`;

/** Where point i is at a given moment (time, how formed, how far the burst has spread). Shared by the heads and their trails. */
const PLACE = /* glsl */`
${HASH}
attribute vec3 aTarget;
attribute vec4 aSeed;
attribute float aBlue;                      // 0 ink, 1 accent blue, 2 ink that flashes copper
uniform float uTime, uForm, uBurst, uGlitch, uPx, uTrail, uFormRate, uBurstRate, uBeat;
uniform vec2 uPointer, uOffset;
float gF;                                   // this point's own 0..1 "settled into the word" at the moment asked about

vec3 place(float time, float form, float burst) {
  float strand = floor(aSeed.w * 90.0);
  float baseAngle = strand / 90.0 * 6.2831853 + h11(strand) * 0.4;
  float r = pow(aSeed.y, 0.62) * 2.6 * burst;
  float swirl = r * (0.75 + 0.35 * sin(strand * 1.7)) + 0.35 * sin(time * 0.23 + strand * 0.9);
  // every strand forks once it is long enough: half of its points bend one way, half the other
  float fork = step(0.5, aSeed.x) * 2.0 - 1.0;
  float angle = baseAngle + swirl + fork * max(0.0, r - (0.45 + 0.9 * h11(strand + 5.0))) * 0.8;
  vec3 scatter = vec3(cos(angle), sin(angle), 0.0) * r;
  scatter.z = (aSeed.z - 0.5) * 2.4 * burst;
  // slow stochastic drift, the "chaos" half
  scatter.xy += 0.06 * vec2(sin(time * 0.7 + aSeed.x * 40.0), cos(time * 0.6 + aSeed.y * 40.0)) * (0.4 + r);
  scatter.z += 0.1 * sin(time * 0.5 + aSeed.x * 20.0);

  float stagger = fract(aSeed.z * 7.13) * 0.55 + clamp((aTarget.x + 1.7) / 3.4, 0.0, 1.0) * 0.25;
  float f = smoothstep(0.0, 1.0, clamp((form * 1.55 - stagger) / 0.55, 0.0, 1.0));
  gF = f;
  vec3 target = aTarget;
  target.z += (aSeed.z - 0.5) * 0.18;
  target.xy += 0.0016 * vec2(sin(time * 1.3 + aSeed.x * 60.0), cos(time * 1.1 + aSeed.y * 60.0));
  vec3 pos = mix(scatter, target, f);

  // glitch: horizontal bands slide sideways, a few at a time
  float band = floor((aTarget.y + 1.0) * 16.0);
  float tick = floor(uTime * 14.0);
  float hit = step(0.62, h11(band * 7.0 + tick));
  pos.x += (h11(band * 3.0 + tick) - 0.5) * 0.7 * uGlitch * hit * f;

  // pointer pushes points aside, the formed word springs back
  vec2 d = pos.xy - uPointer;
  pos.xy += normalize(d + 1e-4) * exp(-dot(d, d) / 0.045) * 0.3 * (0.35 + 0.65 * f);
  pos.xy += uOffset;
  return pos;
}
`;

const POINT_VERT = /* glsl */`
${PLACE}
varying float vBlue, vAlpha;

void main() {
  vec4 mv = modelViewMatrix * vec4(place(uTime, uForm, uBurst), 1.0);
  float f = gF;
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * mix(1.15 + 1.9 * aSeed.y, 1.0 + 0.9 * aSeed.y, f) * (1.0 + 0.4 * uBeat * (1.0 - f)) * clamp(5.2 / -mv.z, 0.55, 1.7);
  vBlue = aBlue;
  vAlpha = mix(0.3, 0.95, f) * mix(0.55 + 0.45 * aSeed.x, 1.0, f);
}
`;

/** Each point also draws a line back along where it just was (a fixed slice of time earlier, so fast movers stretch long and
 *  still ones stay dots). The slice is zero once the word is held, so the finished logo stays crisp. */
const TRAIL_VERT = /* glsl */`
${PLACE}
attribute float aEnd;                       // 0 at the head, 1 at the tail
varying float vBlue, vAlpha;

void main() {
  float back = aEnd * uTrail;
  vec3 pos = place(uTime - back, clamp(uForm - uFormRate * back, 0.0, 1.0), max(0.15, uBurst - uBurstRate * back));
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  vBlue = aBlue;
  vAlpha = (1.0 - aEnd) * 0.4 * (0.5 + 0.5 * aSeed.x) * (1.0 - smoothstep(0.85, 1.0, uForm));
}
`;

const POINT_FRAG = /* glsl */`
precision mediump float;
varying float vBlue, vAlpha;
uniform float uDark, uAccent, uAlphaScale, uTintMix;
uniform vec3 uTint;
void main() {
  float d = length(gl_PointCoord - 0.5);
  float a = smoothstep(0.5, 0.18, d) * vAlpha * uAlphaScale;
  vec3 ink = mix(vec3(0.035, 0.04, 0.085), vec3(0.9, 0.93, 1.0), uDark);
  vec3 blue = mix(vec3(0.12, 0.25, 1.0), vec3(0.3, 0.5, 1.0), uDark);
  vec3 col = vBlue > 1.5 ? mix(ink, vec3(1.0, 0.45, 0.16), uAccent) : mix(ink, blue, vBlue);
  gl_FragColor = vec4(mix(col, uTint, uTintMix), a);
}
`;

/** Thin radial ink lines out of the hub. */
const RAY_VERT = /* glsl */`
${HASH}
attribute vec2 aRay;               // x: which ray, y: 0 at the hub / 1 at the tip
uniform float uTime, uForm, uBurst;
varying float vA, vAcc;
void main() {
  float id = aRay.x;
  float len = (0.9 + 2.4 * h11(id)) * uBurst * (1.0 - smoothstep(0.35, 0.9, uForm) * 0.9);
  float ang = id / 140.0 * 6.2831853 + h11(id + 3.0) * 0.5 + aRay.y * len * 0.55 * (0.8 + 0.4 * sin(id)) + 0.2 * sin(uTime * 0.25 + id);
  vec3 pos = vec3(cos(ang), sin(ang), (h11(id + 9.0) - 0.5) * 1.2 * aRay.y) * aRay.y * len;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  vA = (1.0 - aRay.y) * 0.16 * uBurst * (1.0 - smoothstep(0.2, 0.7, uForm));
  vAcc = 0.0;
}
`;

/** A ring of event ticks around the word; some tick longer and brighter on the beat, like events passing through. */
const RING_VERT = /* glsl */`
${HASH}
attribute vec2 aTick;              // x: which tick, y: 0 inner / 1 outer end
uniform float uTime, uForm, uBeat;
varying float vA, vAcc;
void main() {
  float id = aTick.x;
  float ang = id / 120.0 * 6.2831853 + uTime * 0.03;
  float slot = floor(uTime * 1.6 + h11(id) * 7.0);
  float lit = step(0.84, h11(id * 3.1 + slot)) * (0.5 + 0.5 * uBeat);
  float rad = 1.0 + aTick.y * (0.05 + 0.3 * lit);
  vec3 pos = vec3(cos(ang) * 2.1 * rad, sin(ang) * 1.05 * rad, -0.3);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  vA = (0.16 + 0.55 * lit) * (0.45 + 0.55 * uForm) * (1.0 - aTick.y * 0.5);
  vAcc = step(0.93, h11(id * 5.7 + slot)) * lit;
}
`;

/** A scrolling point-cloud ground (the scanned-terrain look). */
const GROUND_VERT = /* glsl */`
attribute vec3 aGrid;              // x, z-start, random
uniform float uTime, uForm, uPx;
varying float vA, vAcc;
void main() {
  float z = mod(aGrid.y + uTime * 0.35, 12.0) - 9.0;
  float x = aGrid.x;
  float h = 0.12 * sin(x * 1.3 + z * 0.9 + uTime * 0.4) + 0.08 * sin(x * 2.7 - z * 1.7);
  vec4 mv = modelViewMatrix * vec4(x, -0.98 + h, z, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (0.9 + 1.4 * aGrid.z) * clamp(5.2 / -mv.z, 0.5, 1.6);
  vA = smoothstep(-9.0, -4.0, z) * (1.0 - smoothstep(0.8, 3.0, z)) * (1.0 - smoothstep(3.0, 5.5, abs(x))) * 0.5 * (1.0 - 0.75 * uForm);
  vAcc = 0.0;
}
`;
const INK_FRAG = /* glsl */`
precision mediump float;
varying float vA, vAcc;
uniform float uDark, uAccent;
void main() {
  vec3 ink = mix(vec3(0.04, 0.05, 0.1), vec3(0.9, 0.93, 1.0), uDark);
  gl_FragColor = vec4(mix(ink, vec3(1.0, 0.45, 0.16), vAcc * uAccent), vA);
}
`;
const GROUND_FRAG = /* glsl */`
precision mediump float;
varying float vA, vAcc;
uniform float uDark;
void main() {
  if (length(gl_PointCoord - 0.5) > 0.5) discard;
  vec3 ink = mix(vec3(0.04, 0.05, 0.1), vec3(0.9, 0.93, 1.0), uDark);
  gl_FragColor = vec4(ink, vA);
}
`;

/** Fullscreen backdrop (the CSS paper gradient, or night). Drawn each frame at low alpha, so the last frames linger as trails. */
const FADE_VERT = /* glsl */`
varying vec2 vUv;
void main() { vUv = position.xy * 0.5 + 0.5; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;
const FADE_FRAG = /* glsl */`
precision mediump float;
varying vec2 vUv;
uniform float uFade, uAspect, uDark;
void main() {
  vec2 dir = vec2(0.4226, -0.9063);
  float t = 0.5 + dot((vUv - 0.5) * vec2(uAspect, 1.0), dir) / (uAspect * 0.4226 + 0.9063);
  vec3 a = vec3(0.933, 0.945, 0.973), b = vec3(0.867, 0.890, 0.949), c = vec3(0.788, 0.827, 0.925);
  vec3 paper = t < 0.55 ? mix(a, b, t / 0.55) : mix(b, c, (t - 0.55) / 0.45);
  vec3 night = mix(vec3(0.075, 0.09, 0.17), vec3(0.03, 0.035, 0.07), clamp(t * 1.1, 0.0, 1.0));
  gl_FragColor = vec4(mix(paper, night, uDark), uFade);
}
`;

const VIGNETTE = { day: "radial-gradient(ellipse at 50% 46%, transparent 52%, rgba(8,12,36,.12) 100%)", night: "radial-gradient(ellipse at 50% 46%, transparent 52%, rgba(0,0,0,.35) 100%)" };
const CAPTIONS: Record<string, string> = {
  burst: "every point, in noise.", converge: "pulled into a word.", hold: "inazina.", disperse: "back to noise.", drift: "again.",
};
const GRAIN = "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 .55 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>\")";

/** `onSlow` is called once if the first seconds ran under ~26 fps; the caller then drops the scene and shows the still logo. */
export function mountLoginScene(container: HTMLElement, onSlow?: () => void): () => void {
  const renderer = new WebGLRenderer({ antialias: false, alpha: false, preserveDrawingBuffer: true, powerPreference: "high-performance" });
  renderer.autoClear = false;
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.domElement.className = "auth-canvas";
  renderer.domElement.setAttribute("aria-hidden", "true");
  renderer.domElement.style.filter = "saturate(.9) contrast(1.04)";       // a light film grade

  const small = container.clientWidth < 700;
  const BLACK = small ? 30000 : 58000, BLUE = small ? 1800 : 3400, COUNT = BLACK + BLUE;
  const sample = sampleWordmark(BLACK, BLUE);

  const target = new Float32Array(COUNT * 3), seed = new Float32Array(COUNT * 4), kind = new Float32Array(COUNT);
  for (let i = 0; i < COUNT; i += 1) {
    const isBlue = i >= BLACK, j = isBlue ? i - BLACK : i;
    const source = isBlue ? sample.blue : sample.black;
    target[i * 3] = source[j * 2]; target[i * 3 + 1] = source[j * 2 + 1]; target[i * 3 + 2] = 0;
    for (let k = 0; k < 4; k += 1) seed[i * 4 + k] = Math.random();
    kind[i] = isBlue ? 1 : Math.random() < 0.04 ? 2 : 0;
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(new Float32Array(COUNT * 3), 3));   // unused, three wants it
  geometry.setAttribute("aTarget", new BufferAttribute(target, 3));
  geometry.setAttribute("aSeed", new BufferAttribute(seed, 4));
  geometry.setAttribute("aBlue", new BufferAttribute(kind, 1));
  const uniforms = {
    uTime: { value: 0 }, uForm: { value: 0 }, uBurst: { value: 0.15 }, uGlitch: { value: 0 }, uPx: { value: 1 }, uTrail: { value: 0 },
    uFormRate: { value: 0 }, uBurstRate: { value: 0 }, uBeat: { value: 0 }, uDark: { value: 0 }, uAccent: { value: 0 }, uAlphaScale: { value: 1 },
    uPointer: { value: new Vector2(9, 9) }, uOffset: { value: new Vector2(0, 0) }, uTint: { value: new Vector3(0, 0, 0) }, uTintMix: { value: 0 },
  };
  const pointMaterial = (extra: Partial<typeof uniforms> = {}) => new ShaderMaterial({
    uniforms: { ...uniforms, ...extra }, vertexShader: POINT_VERT, fragmentShader: POINT_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const material = pointMaterial();
  const points = new Points(geometry, material);
  points.frustumCulled = false;
  // colour-split ghosts of the word, only drawn while it glitches
  const ghostMaterials = [
    pointMaterial({ uOffset: { value: new Vector2(0.022, 0) }, uTint: { value: new Vector3(1, 0.12, 0.28) }, uTintMix: { value: 1 } }),
    pointMaterial({ uOffset: { value: new Vector2(-0.022, 0) }, uTint: { value: new Vector3(0, 0.75, 1) }, uTintMix: { value: 1 } }),
  ];
  const ghosts = ghostMaterials.map((ghostMaterial) => { const g = new Points(geometry, ghostMaterial); g.frustumCulled = false; g.visible = false; return g; });

  // trails: the same points again as two-vertex lines (head, tail) sharing the point attributes
  const trailGeometry = new BufferGeometry();
  const doubled = (source: Float32Array, size: number) => { const out = new Float32Array(source.length * 2); for (let i = 0; i < COUNT; i += 1) { const part = source.subarray(i * size, i * size + size); out.set(part, i * size * 2); out.set(part, i * size * 2 + size); } return out; };
  trailGeometry.setAttribute("position", new BufferAttribute(new Float32Array(COUNT * 6), 3));
  trailGeometry.setAttribute("aTarget", new BufferAttribute(doubled(target, 3), 3));
  trailGeometry.setAttribute("aSeed", new BufferAttribute(doubled(seed, 4), 4));
  trailGeometry.setAttribute("aBlue", new BufferAttribute(doubled(kind, 1), 1));
  trailGeometry.setAttribute("aEnd", new BufferAttribute(Float32Array.from({ length: COUNT * 2 }, (_, i) => i & 1), 1));
  const trailMaterial = new ShaderMaterial({ uniforms, vertexShader: TRAIL_VERT, fragmentShader: POINT_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const trails = new LineSegments(trailGeometry, trailMaterial);
  trails.frustumCulled = false;

  const lineMesh = (vert: string, attribute: string, pairs: number) => {
    const g = new BufferGeometry();
    const values = new Float32Array(pairs * 4);
    for (let i = 0; i < pairs; i += 1) values.set([i, 0, i, 1], i * 4);
    g.setAttribute("position", new BufferAttribute(new Float32Array(pairs * 6), 3));
    g.setAttribute(attribute, new BufferAttribute(values, 2));
    const m = new ShaderMaterial({ uniforms, vertexShader: vert, fragmentShader: INK_FRAG, transparent: true, depthTest: false, depthWrite: false });
    const mesh = new LineSegments(g, m); mesh.frustumCulled = false;
    return { mesh, geometry: g, material: m };
  };
  const rays = lineMesh(RAY_VERT, "aRay", 140);
  const ring = lineMesh(RING_VERT, "aTick", 120);

  const GROUND = small ? 7000 : 15000;
  const ground = new BufferGeometry();
  const grid = new Float32Array(GROUND * 3);
  for (let i = 0; i < GROUND; i += 1) { grid[i * 3] = (Math.random() - 0.5) * 12; grid[i * 3 + 1] = Math.random() * 12; grid[i * 3 + 2] = Math.random(); }
  ground.setAttribute("position", new BufferAttribute(new Float32Array(GROUND * 3), 3));
  ground.setAttribute("aGrid", new BufferAttribute(grid, 3));
  const groundMaterial = new ShaderMaterial({ uniforms, vertexShader: GROUND_VERT, fragmentShader: GROUND_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const groundPoints = new Points(ground, groundMaterial);
  groundPoints.frustumCulled = false;

  const scene = new Scene();
  scene.add(groundPoints, ring.mesh, rays.mesh, trails, points, ...ghosts);
  const camera = new PerspectiveCamera(FOV, 1, 0.1, 60);

  const fadeUniforms = { uFade: { value: 1 }, uAspect: { value: 1 }, uDark: uniforms.uDark };
  const fadeGeometry = new BufferGeometry();
  fadeGeometry.setAttribute("position", new BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3));
  const fadeMaterial = new ShaderMaterial({ uniforms: fadeUniforms, vertexShader: FADE_VERT, fragmentShader: FADE_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const fadeMesh = new Mesh(fadeGeometry, fadeMaterial); fadeMesh.frustumCulled = false;
  const fadeScene = new Scene(); fadeScene.add(fadeMesh);

  // ---- film grain, vignette, caption and readout (DOM over the canvas) ---------------------------------------------------
  const overlay = (css: string) => { const el = document.createElement("div"); el.setAttribute("aria-hidden", "true"); el.style.cssText = `position:absolute;inset:0;pointer-events:none;${css}`; return el; };
  const grain = overlay(`background-image:${GRAIN};background-size:160px 160px;opacity:.14;mix-blend-mode:multiply`);
  const grainAnimation = grain.animate([{ backgroundPosition: "0 0" }, { backgroundPosition: "-80px 48px" }, { backgroundPosition: "40px -64px" }, { backgroundPosition: "0 0" }], { duration: 480, iterations: Infinity, easing: "steps(4)" });
  const vignette = overlay(`background:${VIGNETTE.day}`);
  const hud = overlay("inset:auto auto 18px 22px;font:500 11px/1.7 ui-monospace,'SF Mono',Menlo,monospace;letter-spacing:.02em;white-space:pre;z-index:1");
  const caption = document.createElement("span");
  caption.style.cssText = "display:inline-block;padding:1px 9px 2px;margin-bottom:6px;font:600 15px/1.45 var(--font-system,'DM Sans','Noto Sans KR',system-ui,sans-serif);letter-spacing:-.01em;white-space:pre;min-width:22px;min-height:24px";
  const readout = document.createElement("div");
  hud.append(caption, readout);
  container.append(grain, vignette, hud);

  let width = 1, height = 1, dist = 6, halfH = 1, halfW = 1;
  const resize = () => {
    width = Math.max(1, container.clientWidth); height = Math.max(1, container.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height; camera.updateProjectionMatrix();
    const t = Math.tan((FOV * Math.PI) / 360);
    dist = Math.max((WORD_HALF_WIDTH / 0.78) / (t * camera.aspect), 0.95 / t);
    halfH = dist * t; halfW = halfH * camera.aspect;
    uniforms.uPx.value = renderer.getPixelRatio() * Math.min(1.5, Math.max(0.7, height / 760));
    fadeUniforms.uAspect.value = camera.aspect;
    fadeUniforms.uFade.value = 1;                         // repaint the backdrop once so no stale pixels survive a resize
  };
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();
  container.insertBefore(renderer.domElement, grain);

  const pointer = { x: 0, y: 0, tx: 0, ty: 0, active: false };
  const onPointer = (event: PointerEvent) => {
    const rect = container.getBoundingClientRect();
    const nx = ((event.clientX - rect.left) / rect.width) * 2 - 1, ny = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
    pointer.active = Math.abs(nx) <= 1 && Math.abs(ny) <= 1;
    pointer.tx = Math.max(-1, Math.min(1, nx)); pointer.ty = Math.max(-1, Math.min(1, ny));
  };
  window.addEventListener("pointermove", onPointer);

  const phaseOf = (t: number) => {
    // [burst] 0–1.4 · [converge] 1.4–4.2 · [hold] 4.2–11 (glitch ticks at 4.25, 7.0, 9.6) · [disperse] 11–14 · [drift] 14–16
    const burst = t < 1.4 ? 0.15 + 0.85 * (1 - Math.pow(1 - t / 1.4, 3)) : 1;
    const form = t < 1.4 ? 0 : t < 4.2 ? smooth((t - 1.4) / 2.8) : t < 11 ? 1 : t < 14 ? 1 - smooth((t - 11) / 3) : 0;
    const glitch = (t > 7.0 && t < 7.22) || (t > 9.6 && t < 9.72) || (t > 4.25 && t < 4.4) ? 1 : 0;
    const name = t < 1.4 ? "burst" : t < 4.2 ? "converge" : t < 11 ? "hold" : t < 14 ? "disperse" : "drift";
    const dark = smooth((t - 11.2) / 0.9) * (1 - smooth((t - 15.0) / 0.8));         // a few seconds of night while it dissolves
    const accent = Math.max(glitch, smooth((t - 11) / 1.2) * (1 - smooth((t - 13.5) / 1.5)));
    return { burst, form, glitch, name, dark, accent };
  };

  let measured = 0, measuredFrames = 0, prevForm = 0, prevBurst = 0.15, running = true, visible = true, frame = 0, last = performance.now(), time = 0, hudAt = 0;
  let captionKey = "", captionAt = 0, lastDark = -1;                              // -1 = styles not applied yet
  const render = (now: number) => {
    frame = requestAnimationFrame(render);
    const delta = Math.min(1, (now - last) / 1000); last = now;
    if (!running || !visible) return;
    time += delta;
    if (onSlow && time > 0.6 && measured < 2.4) {                // first 0.6 s is shader compile and layout; judge the next 2.4 s
      measured += delta; measuredFrames += 1;
      if (measured >= 2.4 && measuredFrames / measured < 26) { cancelAnimationFrame(frame); onSlow(); return; }
    }
    const t = time % CYCLE;
    const phase = phaseOf(t);
    if (delta > 0) {                                           // how fast the cloud is changing, to size the trails
      const k = 1 - Math.exp(-delta * 8);
      uniforms.uFormRate.value += ((phase.form - prevForm) / delta - uniforms.uFormRate.value) * k;
      uniforms.uBurstRate.value += ((phase.burst - prevBurst) / delta - uniforms.uBurstRate.value) * k;
    }
    prevForm = phase.form; prevBurst = phase.burst;
    const held = phase.form > 0.98;
    uniforms.uTrail.value = t < 14 ? 0.32 : 0.5;
    uniforms.uTime.value = time; uniforms.uForm.value = phase.form; uniforms.uBurst.value = phase.burst; uniforms.uGlitch.value = phase.glitch;
    uniforms.uDark.value = phase.dark; uniforms.uAccent.value = phase.accent;
    uniforms.uBeat.value = Math.pow(0.5 + 0.5 * Math.cos((time * Math.PI * 2) / BEAT), 6);
    uniforms.uAlphaScale.value = held ? 1 : 0.55;              // the long smear stacks many frames, so each one is fainter
    for (const ghost of ghosts) ghost.visible = phase.glitch > 0;

    pointer.x += (pointer.tx - pointer.x) * 0.06; pointer.y += (pointer.ty - pointer.y) * 0.06;
    uniforms.uPointer.value.set(pointer.active ? pointer.x * halfW : 9, pointer.active ? pointer.y * halfH : 9);

    const yaw = 0.2 * Math.sin(time * 0.16) + pointer.x * 0.12 - 0.08 * (1 - phase.form);
    const pitch = 0.09 + 0.05 * Math.sin(time * 0.21) + pointer.y * 0.05;
    camera.position.set(Math.sin(yaw) * dist, Math.sin(pitch) * dist, Math.cos(yaw) * Math.cos(pitch) * dist);
    camera.lookAt(0, 0, 0);

    // long smear while moving (many past frames stay), crisp once the word is held
    renderer.render(fadeScene, camera);
    fadeUniforms.uFade.value = 1 - Math.exp(-delta * (held ? 9 : phase.name === "drift" ? 2.6 : 1.9));
    renderer.render(scene, camera);

    const nightText = phase.dark > 0.5;
    if (lastDark !== (nightText ? 1 : 0)) {
      lastDark = nightText ? 1 : 0;
      hud.style.color = nightText ? "rgba(226,232,255,.7)" : "rgba(18,24,52,.62)";
      caption.style.background = nightText ? "#eef1f8" : "#0b0d1a"; caption.style.color = nightText ? "#0b0d1a" : "#eef1f8";
      vignette.style.background = VIGNETTE[nightText ? "night" : "day"];
      grain.style.mixBlendMode = nightText ? "screen" : "multiply"; grain.style.opacity = nightText ? ".09" : ".14";
    }
    if (phase.name !== captionKey) { captionKey = phase.name; captionAt = now; }
    if (now - hudAt > 60) {
      hudAt = now;
      const full = CAPTIONS[captionKey] ?? "";
      const typed = Math.min(full.length, Math.floor((now - captionAt) / 38));
      caption.textContent = full.slice(0, typed) + (typed < full.length || Math.floor(now / 500) % 2 === 0 ? "▍" : " ");
      const px = pointer.active ? pointer.x * halfW : 0, py = pointer.active ? pointer.y * halfH : 0;
      readout.textContent = `inazina. / point field\nn ${COUNT.toLocaleString("en-US")}   t ${t.toFixed(1).padStart(4, "0")}s   ${phase.glitch ? "glitch" : phase.name}\nx ${px.toFixed(2)}   y ${py.toFixed(2)}`;
    }
  };
  frame = requestAnimationFrame(render);

  const onVisibility = () => { running = !document.hidden; };
  document.addEventListener("visibilitychange", onVisibility);
  const intersection = new IntersectionObserver((entries) => { visible = entries.some((entry) => entry.isIntersecting); });
  intersection.observe(container);

  return () => {
    cancelAnimationFrame(frame);
    observer.disconnect(); intersection.disconnect();
    window.removeEventListener("pointermove", onPointer);
    document.removeEventListener("visibilitychange", onVisibility);
    grainAnimation.cancel();
    for (const item of [geometry, material, trailGeometry, trailMaterial, rays.geometry, rays.material, ring.geometry, ring.material, ground, groundMaterial, fadeGeometry, fadeMaterial, ...ghostMaterials]) item.dispose();
    renderer.dispose();
    renderer.domElement.remove(); grain.remove(); vignette.remove(); hud.remove();
  };
}
