/**
 * The sign-in picture, as an ink point cloud. ~45k points start as radial ink strands bursting from one hub (swirled, depth-
 * scattered, left behind as faint trails), pull together into the inazina. wordmark, hold there, tear into a brief glitch,
 * and dissolve back into strands. The i dots and the period stay accent blue. A small mono readout (point count, phase,
 * pointer) sits in the corner, like the data-art references this was made from.
 * three.js only (Points + two tiny shaders); trails come from fading the previous frame instead of clearing it.
 * Loaded only on the sign-in screen (dynamic import); the plain SVG (scene.svg) shows when WebGL is missing or motion is reduced.
 */
import {
  BufferAttribute, BufferGeometry, LineSegments, Mesh, PerspectiveCamera, Points, Scene, ShaderMaterial, WebGLRenderer,
} from "three";
import { LETTERS, PERIOD } from "./Logo";

const DOTS = [{ x: 117, y: -634, r: 66 }, { x: 1813, y: -634, r: 66 }];
const S = 0.001;                              // one SVG unit in scene units
const CENTER = { x: 1636, up: 350 };          // wordmark centre in SVG units (up = -y)
const WORD_HALF_WIDTH = 1.64;
const FOV = 28;
const CYCLE = 16;                             // seconds per loop

const clamp01 = (t: number) => Math.min(1, Math.max(0, t));
const smooth = (t: number) => { t = clamp01(t); return t * t * (3 - 2 * t); };

/** Where the wordmark's pixels are: [black letters, blue dots + period], as SVG-space sample points. */
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

const POINT_VERT = /* glsl */`
${HASH}
attribute vec3 aTarget;
attribute vec4 aSeed;
attribute float aBlue;
uniform float uTime, uForm, uBurst, uGlitch, uPx;
uniform vec2 uPointer;
varying float vBlue, vAlpha;

void main() {
  float strand = floor(aSeed.w * 90.0);
  float baseAngle = strand / 90.0 * 6.2831853 + h11(strand) * 0.4;
  float r = pow(aSeed.y, 0.62) * 2.6 * uBurst;
  float swirl = r * (0.75 + 0.35 * sin(strand * 1.7)) + 0.35 * sin(uTime * 0.23 + strand * 0.9);
  float angle = baseAngle + swirl;
  vec3 scatter = vec3(cos(angle), sin(angle), 0.0) * r;
  scatter.z = (aSeed.z - 0.5) * 2.4 * uBurst;
  // slow stochastic drift, the "chaos" half
  scatter.xy += 0.06 * vec2(sin(uTime * 0.7 + aSeed.x * 40.0), cos(uTime * 0.6 + aSeed.y * 40.0)) * (0.4 + r);
  scatter.z += 0.1 * sin(uTime * 0.5 + aSeed.x * 20.0);

  float stagger = fract(aSeed.z * 7.13) * 0.55 + clamp((aTarget.x + 1.7) / 3.4, 0.0, 1.0) * 0.25;
  float f = smoothstep(0.0, 1.0, clamp((uForm * 1.55 - stagger) / 0.55, 0.0, 1.0));
  vec3 target = aTarget;
  target.z += (aSeed.z - 0.5) * 0.18;
  target.xy += 0.0016 * vec2(sin(uTime * 1.3 + aSeed.x * 60.0), cos(uTime * 1.1 + aSeed.y * 60.0));
  vec3 pos = mix(scatter, target, f);

  // glitch: horizontal bands slide sideways, a few at a time
  float band = floor((aTarget.y + 1.0) * 16.0);
  float tick = floor(uTime * 14.0);
  float hit = step(0.62, h11(band * 7.0 + tick));
  pos.x += (h11(band * 3.0 + tick) - 0.5) * 0.7 * uGlitch * hit * f;

  // pointer pushes points aside, the formed word springs back
  vec2 d = pos.xy - uPointer;
  float push = exp(-dot(d, d) / 0.045) * 0.3 * (0.35 + 0.65 * f);
  pos.xy += normalize(d + 1e-4) * push;

  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * mix(1.15 + 1.9 * aSeed.y, 1.0 + 0.9 * aSeed.y, f) * clamp(5.2 / -mv.z, 0.55, 1.7);
  vBlue = aBlue;
  vAlpha = mix(0.3, 0.95, f) * mix(0.55 + 0.45 * aSeed.x, 1.0, f);
}
`;

const POINT_FRAG = /* glsl */`
precision mediump float;
varying float vBlue, vAlpha;
void main() {
  float d = length(gl_PointCoord - 0.5);
  float a = smoothstep(0.5, 0.18, d) * vAlpha;
  vec3 ink = mix(vec3(0.035, 0.04, 0.085), vec3(0.12, 0.25, 1.0), vBlue);
  gl_FragColor = vec4(ink, a);
}
`;

const RAY_VERT = /* glsl */`
${HASH}
attribute vec2 aRay;               // x: which ray, y: 0 at the hub / 1 at the tip
uniform float uTime, uForm, uBurst;
varying float vA;
void main() {
  float id = aRay.x;
  float len = (0.9 + 2.4 * h11(id)) * uBurst * (1.0 - smoothstep(0.35, 0.9, uForm) * 0.9);
  float ang = id / 140.0 * 6.2831853 + h11(id + 3.0) * 0.5 + aRay.y * len * 0.55 * (0.8 + 0.4 * sin(id)) + 0.2 * sin(uTime * 0.25 + id);
  vec3 pos = vec3(cos(ang), sin(ang), (h11(id + 9.0) - 0.5) * 1.2 * aRay.y) * aRay.y * len;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  vA = (1.0 - aRay.y) * 0.16 * uBurst * (1.0 - smoothstep(0.2, 0.7, uForm));
}
`;
const RAY_FRAG = /* glsl */`
precision mediump float;
varying float vA;
void main() { gl_FragColor = vec4(0.04, 0.05, 0.1, vA); }
`;

/** Fullscreen paper gradient (same as the CSS backdrop). Drawn each frame at low alpha, so the last frames linger as trails. */
const FADE_VERT = /* glsl */`
varying vec2 vUv;
void main() { vUv = position.xy * 0.5 + 0.5; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;
const FADE_FRAG = /* glsl */`
precision mediump float;
varying vec2 vUv;
uniform float uFade, uAspect;
void main() {
  vec2 dir = vec2(0.4226, -0.9063);
  float t = 0.5 + dot((vUv - 0.5) * vec2(uAspect, 1.0), dir) / (uAspect * 0.4226 + 0.9063);
  vec3 a = vec3(0.933, 0.945, 0.973), b = vec3(0.867, 0.890, 0.949), c = vec3(0.788, 0.827, 0.925);
  vec3 col = t < 0.55 ? mix(a, b, t / 0.55) : mix(b, c, (t - 0.55) / 0.45);
  gl_FragColor = vec4(col, uFade);
}
`;

export function mountLoginScene(container: HTMLElement): () => void {
  const renderer = new WebGLRenderer({ antialias: false, alpha: false, preserveDrawingBuffer: true, powerPreference: "high-performance" });
  renderer.autoClear = false;
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.domElement.className = "auth-canvas";
  renderer.domElement.setAttribute("aria-hidden", "true");

  const small = container.clientWidth < 700;
  const BLACK = small ? 30000 : 58000, BLUE = small ? 1800 : 3400, COUNT = BLACK + BLUE;
  const sample = sampleWordmark(BLACK, BLUE);

  const target = new Float32Array(COUNT * 3), seed = new Float32Array(COUNT * 4), blue = new Float32Array(COUNT);
  for (let i = 0; i < COUNT; i += 1) {
    const isBlue = i >= BLACK, j = isBlue ? i - BLACK : i;
    const source = isBlue ? sample.blue : sample.black;
    target[i * 3] = source[j * 2]; target[i * 3 + 1] = source[j * 2 + 1]; target[i * 3 + 2] = 0;
    for (let k = 0; k < 4; k += 1) seed[i * 4 + k] = Math.random();
    blue[i] = isBlue ? 1 : 0;
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(new Float32Array(COUNT * 3), 3));   // unused, three wants it
  geometry.setAttribute("aTarget", new BufferAttribute(target, 3));
  geometry.setAttribute("aSeed", new BufferAttribute(seed, 4));
  geometry.setAttribute("aBlue", new BufferAttribute(blue, 1));
  const uniforms = { uTime: { value: 0 }, uForm: { value: 0 }, uBurst: { value: 0.15 }, uGlitch: { value: 0 }, uPx: { value: 1 }, uPointer: { value: { x: 9, y: 9 } } };
  const material = new ShaderMaterial({ uniforms, vertexShader: POINT_VERT, fragmentShader: POINT_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const points = new Points(geometry, material);
  points.frustumCulled = false;

  const RAYS = 140;
  const rayData = new Float32Array(RAYS * 4);
  for (let i = 0; i < RAYS; i += 1) { rayData.set([i, 0, i, 1], i * 4); }
  const rayGeometry = new BufferGeometry();
  rayGeometry.setAttribute("position", new BufferAttribute(new Float32Array(RAYS * 2 * 3), 3));
  rayGeometry.setAttribute("aRay", new BufferAttribute(rayData, 2));
  const rayMaterial = new ShaderMaterial({ uniforms, vertexShader: RAY_VERT, fragmentShader: RAY_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const rays = new LineSegments(rayGeometry, rayMaterial);
  rays.frustumCulled = false;

  const scene = new Scene();
  scene.add(rays, points);
  const camera = new PerspectiveCamera(FOV, 1, 0.1, 60);

  const fadeUniforms = { uFade: { value: 1 }, uAspect: { value: 1 } };
  const fadeGeometry = new BufferGeometry();
  fadeGeometry.setAttribute("position", new BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3));
  const fadeMaterial = new ShaderMaterial({ uniforms: fadeUniforms, vertexShader: FADE_VERT, fragmentShader: FADE_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const fadeMesh = new Mesh(fadeGeometry, fadeMaterial); fadeMesh.frustumCulled = false;
  const fadeScene = new Scene(); fadeScene.add(fadeMesh);

  // ---- corner readout --------------------------------------------------------------------------------------------------
  const hud = document.createElement("div");
  hud.setAttribute("aria-hidden", "true");
  hud.style.cssText = "position:absolute;left:22px;bottom:18px;z-index:1;pointer-events:none;font:500 11px/1.7 ui-monospace,'SF Mono',Menlo,monospace;letter-spacing:.02em;color:rgba(18,24,52,.62);white-space:pre";
  const tag = document.createElement("span");
  tag.style.cssText = "display:inline-block;padding:0 6px;margin-bottom:3px;background:#0b0d1a;color:#eef1f8";
  const readout = document.createElement("div");
  hud.append(tag, readout);
  container.appendChild(hud);

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
  container.appendChild(renderer.domElement);

  const pointer = { x: 0, y: 0, tx: 0, ty: 0, wx: 9, wy: 9, active: false };
  const onPointer = (event: PointerEvent) => {
    const rect = container.getBoundingClientRect();
    const nx = ((event.clientX - rect.left) / rect.width) * 2 - 1, ny = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
    pointer.active = Math.abs(nx) <= 1 && Math.abs(ny) <= 1;
    pointer.tx = Math.max(-1, Math.min(1, nx)); pointer.ty = Math.max(-1, Math.min(1, ny));
  };
  window.addEventListener("pointermove", onPointer);

  const phaseOf = (t: number) => {
    // [scatter burst] 0–1.4 · [converge] 1.4–4.2 · [hold] 4.2–11 (glitch ticks at 7.0 and 9.6) · [disperse] 11–14 · [drift] 14–16
    const burst = t < 1.4 ? 0.15 + 0.85 * (1 - Math.pow(1 - t / 1.4, 3)) : 1;
    const form = t < 1.4 ? 0 : t < 4.2 ? smooth((t - 1.4) / 2.8) : t < 11 ? 1 : t < 14 ? 1 - smooth((t - 11) / 3) : 0;
    const glitch = (t > 7.0 && t < 7.22) || (t > 9.6 && t < 9.72) || (t > 4.25 && t < 4.4) ? 1 : 0;
    const label = t < 1.4 ? "burst" : t < 4.2 ? "converge" : t < 11 ? (glitch ? "glitch" : "hold") : t < 14 ? "disperse" : "drift";
    return { burst, form, glitch, label };
  };

  let running = true, visible = true, frame = 0, last = performance.now(), time = 0, hudAt = 0;
  const render = (now: number) => {
    frame = requestAnimationFrame(render);
    const delta = Math.min(1, (now - last) / 1000); last = now;
    if (!running || !visible) return;
    time += delta;
    const t = time % CYCLE;
    const phase = phaseOf(t);
    uniforms.uTime.value = time; uniforms.uForm.value = phase.form; uniforms.uBurst.value = phase.burst; uniforms.uGlitch.value = phase.glitch;

    pointer.x += (pointer.tx - pointer.x) * 0.06; pointer.y += (pointer.ty - pointer.y) * 0.06;
    pointer.wx = pointer.active ? pointer.x * halfW : 9; pointer.wy = pointer.active ? pointer.y * halfH : 9;
    uniforms.uPointer.value = { x: pointer.wx, y: pointer.wy };

    const yaw = 0.2 * Math.sin(time * 0.16) + pointer.x * 0.12 - 0.08 * (1 - phase.form), pitch = 0.05 * Math.sin(time * 0.21) + pointer.y * 0.05;
    camera.position.set(Math.sin(yaw) * dist, Math.sin(pitch) * dist, Math.cos(yaw) * Math.cos(pitch) * dist);
    camera.lookAt(0, 0, 0);

    renderer.render(fadeScene, camera);          // fade the last frame toward the paper colour: that is the trail
    fadeUniforms.uFade.value = 1 - Math.exp(-delta * (phase.form > 0.98 ? 9 : 4.2));
    renderer.render(scene, camera);

    if (now - hudAt > 120) {
      hudAt = now;
      tag.textContent = phase.label;
      readout.textContent = `\ninazina. / point field\nn ${COUNT.toLocaleString("en-US")}   t ${t.toFixed(1).padStart(4, "0")}s\nx ${(pointer.active ? pointer.wx : 0).toFixed(2)}   y ${(pointer.active ? pointer.wy : 0).toFixed(2)}`;
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
    geometry.dispose(); material.dispose(); rayGeometry.dispose(); rayMaterial.dispose(); fadeGeometry.dispose(); fadeMaterial.dispose();
    renderer.dispose();
    renderer.domElement.remove(); hud.remove();
  };
}
