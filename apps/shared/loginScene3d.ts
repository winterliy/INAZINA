/**
 * The sign-in picture, as an ink point cloud (the look comes from a set of generative / scan / data-art reels):
 *  - ~60k points burst from one hub as forking, swirling ink strands, leave long smeared trails, pull together into the inazina.
 *    wordmark, hold (crisp), tear into a colour-split glitch, and dissolve back; for a few seconds the paper turns to night.
 *  - after the logo dissolves, more scenes, each from one of the references (all of them points): a stem that blooms, a walk through
 *    a scanned tunnel, a branch whose past frames stack up (time-stack), stacked sound-waveform rows, splintered fragments, a fast
 *    colour-cut montage (eclipse / iris / prism), and an event ring with a live panel and a red fault; then back to paper and the logo
 *  - a scrolling point-cloud ground, a ring of event ticks, radial hairlines, film grain + vignette, and a typed caption in a
 *    black highlight box with a mono readout, like the references. The i dots and the period stay accent blue; a few points
 *    flash copper in the glitch.
 *  - there is no audio on a sign-in page, so the "audio-reactive" part is an internal pulse (a steady beat) driving size and ticks.
 * three.js only (Points / LineSegments + small shaders); trails come from fading the previous frame instead of clearing it.
 * Loaded only on the sign-in screen (dynamic import); a still logo is shown where this is not suitable (see AuthPanel).
 */
import {
  AdditiveBlending, NormalBlending, BufferAttribute, BufferGeometry, LinearFilter, LineSegments, Mesh, PerspectiveCamera, Points, Scene, ShaderMaterial, Vector2, Vector3, WebGLRenderer, WebGLRenderTarget,
} from "three";
import { LETTERS, PERIOD } from "./Logo";

const DOTS = [{ x: 117, y: -634, r: 66 }, { x: 1813, y: -634, r: 66 }];
const S = 0.001;                              // one SVG unit in scene units
const CENTER = { x: 1636, up: 350 };          // wordmark centre in SVG units (up = -y)
const WORD_HALF_WIDTH = 1.64;
const FOV = 28;
const CYCLE = 52;                             // seconds per loop, see phaseOf: logo, bloom, walk, time-stack, waveforms, shards, fast cuts, event panel
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
uniform float uTime, uForm, uBurst, uGlitch, uPx, uTrail, uFormRate, uBurstRate, uBeat, uPresence;
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
  pos.xy += normalize(d + 1e-4) * exp(-dot(d, d) / 0.045) * 0.07 * (0.35 + 0.65 * f);
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
  vAlpha = mix(0.3, 0.95, f) * mix(0.55 + 0.45 * aSeed.x, 1.0, f) * uPresence;
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
  vAlpha = (1.0 - aEnd) * 0.4 * (0.5 + 0.5 * aSeed.x) * (1.0 - smoothstep(0.85, 1.0, uForm)) * uPresence;
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
uniform float uTime, uForm, uBurst, uPresence;
varying float vA, vAcc;
void main() {
  float id = aRay.x;
  float len = (0.9 + 2.4 * h11(id)) * uBurst * (1.0 - smoothstep(0.35, 0.9, uForm) * 0.9);
  float ang = id / 140.0 * 6.2831853 + h11(id + 3.0) * 0.5 + aRay.y * len * 0.55 * (0.8 + 0.4 * sin(id)) + 0.2 * sin(uTime * 0.25 + id);
  vec3 pos = vec3(cos(ang), sin(ang), (h11(id + 9.0) - 0.5) * 1.2 * aRay.y) * aRay.y * len;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  vA = (1.0 - aRay.y) * 0.16 * uBurst * (1.0 - smoothstep(0.2, 0.7, uForm)) * uPresence;
  vAcc = 0.0;
}
`;

/** A ring of event ticks around the word; some tick longer and brighter on the beat, like events passing through. */
const RING_VERT = /* glsl */`
${HASH}
attribute vec2 aTick;              // x: which tick, y: 0 inner / 1 outer end
uniform float uTime, uForm, uBeat, uAlarm, uRing;
varying float vA, vAcc;
void main() {
  float id = aTick.x;
  float ang = id / 120.0 * 6.2831853 + uTime * 0.03;
  float slot = floor(uTime * 1.6 + h11(id) * 7.0);
  float lit = step(0.84, h11(id * 3.1 + slot)) * (0.5 + 0.5 * uBeat);
  float rad = 1.0 + aTick.y * (0.05 + 0.3 * lit);
  vec3 pos = vec3(cos(ang) * 2.1 * rad, sin(ang) * 1.05 * rad, -0.3);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  vA = (0.16 + 0.55 * lit) * (0.45 + 0.55 * uForm) * (1.0 - aTick.y * 0.5) * (1.0 + 1.8 * uRing);
  vAcc = max(step(0.93, h11(id * 5.7 + slot)) * lit, uAlarm * step(0.55, h11(id * 1.3 + slot)));
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

/** Scene 2: a stem grows from the ground, forks, and every fork ends in a cloud of pink / violet blossom points. */
const BLOOM_VERT = /* glsl */`
${HASH}
attribute vec4 aB;
uniform float uTime, uBloom, uBloomVis, uPx;
varying vec3 vCol;
varying float vA;
vec3 stemAt(float t) { return vec3(0.07 * sin(t * 5.0 + uTime * 0.4), -0.98 + t * 1.55, 0.0); }
void main() {
  vec3 pos; float a;
  if (aB.w < 0.1) {                                   // the main stem
    pos = stemAt(aB.x * clamp(uBloom * 1.6, 0.0, 1.0)); vCol = vec3(0.35, 0.85, 0.5); a = 0.8;
  } else {
    float k = floor(aB.y * 16.0);
    float tb = 0.22 + 0.6 * h11(k);
    float gb = clamp((uBloom * 1.5 - tb * 0.9 - 0.1) / 0.55, 0.0, 1.0);   // this fork's growth, later forks start later
    float side = (h11(k + 3.0) - 0.5) * 2.8;
    float len = 0.3 + 0.55 * h11(k + 7.0);
    vec3 base = stemAt(tb);
    vec3 tip = base + vec3(sin(side) * len, 0.18 * len, (h11(k + 11.0) - 0.5) * 0.8 * len);
    float u = aB.x * gb;
    vec3 branch = mix(base, tip, u) + vec3(0.0, 0.18 * sin(u * 3.14159) * len, 0.0);
    if (aB.z > 0.45) {                                // blossom cloud around the tip
      vec3 o = (vec3(h11(aB.x * 91.0), h11(aB.x * 57.0 + 1.0), h11(aB.x * 33.0 + 2.0)) - 0.5) * 2.0;
      pos = tip + o * 0.17 * gb * (0.4 + aB.z);
      vCol = mix(vec3(1.0, 0.62, 0.82), vec3(0.62, 0.55, 1.0), aB.z);
      a = 0.85 * smoothstep(0.35, 1.0, gb);
    } else { pos = branch; vCol = vec3(0.42, 0.9, 0.55); a = 0.55 * smoothstep(0.0, 0.2, gb); }
  }
  pos.x += sin(uTime * 0.6 + pos.y * 3.0) * 0.012;
  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (1.2 + 1.6 * aB.z) * clamp(5.2 / -mv.z, 0.55, 1.7);
  vA = a * uBloomVis;
}
`;

/** Scene 3: flying through a scanned tunnel: points on a wobbling tube stream toward the camera, the tube twists tighter over time. */
const WALK_VERT = /* glsl */`
attribute vec4 aW;
uniform float uTime, uWalk, uWalkVis, uPx;
varying vec3 vCol;
varying float vA;
void main() {
  float z = mod(aW.y * 30.0 + uTime * (2.2 + 1.5 * uWalk), 30.0) - 26.0;
  float ang = aW.x * 6.2831853 + z * 0.12 * uWalk + uTime * 0.05;
  float n = sin(ang * 3.0 + z * 0.7) * 0.18 + sin(ang * 7.0 - z * 1.3) * 0.08;
  float r = 1.65 + n + (aW.z - 0.5) * 0.12;
  vec3 pos = vec3(cos(ang) * r * 1.35, sin(ang) * r * 0.95, z);
  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (0.9 + 1.5 * aW.z) * clamp(5.2 / -mv.z, 0.5, 1.8);
  vCol = mix(vec3(0.86, 0.9, 1.0), vec3(0.55, 0.95, 0.62), step(0.82, aW.w));   // a few leafy green patches
  vA = uWalkVis * smoothstep(-26.0, -8.0, z) * (1.0 - smoothstep(1.0, 3.5, z)) * (0.3 + 0.7 * aW.w) * 0.8;
}
`;

/** Scene 4 (the time-stacked branch): the same swaying branches drawn at many moments in the past, each older copy fainter; a copper band. */
const STACK_VERT = /* glsl */`
${HASH}
attribute vec4 aS;                 // x: along the branch, y: how far back in time (0 now), z: which branch, w: random
uniform float uTime, uStack, uPx;
varying vec3 vCol;
varying float vA;
vec2 curve(float u, float k, float t) {
  return vec2(-1.9 + 3.8 * u, (k - 0.5) * 1.0 + 0.45 * sin(u * 2.6 + t * 0.7 + k * 5.0) + 0.14 * sin(u * 7.0 - t * 1.6 + k * 9.0));
}
void main() {
  float k = floor(aS.z * 5.0) / 4.0;
  vec2 p = curve(aS.x, k, uTime - aS.y * 0.95);
  p.y += aS.y * 0.05;
  vec4 mv = modelViewMatrix * vec4(p, (aS.w - 0.5) * 0.4, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (1.1 + 1.2 * aS.w) * clamp(5.2 / -mv.z, 0.55, 1.6);
  float band = step(0.8, fract(aS.x * 7.0 + k * 3.0)) * step(0.5, h11(floor(aS.x * 7.0 + k * 3.0)));
  vCol = mix(vec3(0.09, 0.09, 0.12), vec3(0.86, 0.45, 0.24), band);
  vA = uStack * (1.0 - aS.y) * (aS.y < 0.04 ? 0.85 : 0.4);
}
`;

/** Scene 5 (waveforms): rows of sound-waveform bars receding in perspective, a copper playhead sweeping across. */
const WAVE_VERT = /* glsl */`
${HASH}
attribute vec4 aV;                 // x: which column, y: which row, z, w: random
uniform float uTime, uWave, uPx;
varying vec3 vCol;
varying float vA;
void main() {
  float row = floor(aV.y * 16.0);
  float col = floor(aV.x * 220.0);
  float env = h11(floor(col / 5.0) * 1.7 + floor(uTime * 2.5 + row * 0.7) + row * 11.0);
  float amp = (0.015 + 0.42 * env * env) * (0.4 + 0.6 * abs(sin(col * 1.9 + row * 2.0 + uTime * 9.0)));
  float x = (col / 220.0 - 0.5) * 5.0;
  float y = -0.62 + row * 0.058 + (aV.w * 2.0 - 1.0) * amp;
  vec4 mv = modelViewMatrix * vec4(x, y, 1.0 - row * 0.42, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (1.0 + 0.9 * aV.z) * clamp(5.2 / -mv.z, 0.5, 1.5);
  float head = step(abs(col - floor(fract(uTime * 0.2) * 220.0)), 1.0);
  vCol = mix(vec3(0.09, 0.09, 0.12), vec3(0.86, 0.45, 0.24), head);
  vA = uWave * (0.3 + 0.6 * (1.0 - row / 16.0));
}
`;

/** Scene 6 (splintered): dot-matrix fragments, each tinted, drifting, flickering in and out and slipping sideways. */
const SHARD_VERT = /* glsl */`
${HASH}
attribute vec4 aSh;                // x: which fragment, y/z: place inside it, w: random
uniform float uTime, uShard, uPx;
varying vec3 vCol;
varying float vA;
void main() {
  float k = floor(aSh.x * 14.0);
  vec2 centre = vec2((h11(k) - 0.5) * 3.4, (h11(k + 5.0) - 0.5) * 1.6) + 0.12 * vec2(sin(uTime * 0.4 + k), cos(uTime * 0.3 + k * 1.3));
  float size = 0.35 + 0.5 * h11(k + 9.0);
  float rot = (h11(k + 2.0) - 0.5) * 0.8 + 0.2 * sin(uTime * 0.5 + k);
  vec2 local = (vec2(floor(aSh.y * 24.0) / 24.0, floor(aSh.z * 20.0) / 20.0) - 0.5) * size * vec2(1.0, 0.8);
  local = mat2(cos(rot), sin(rot), -sin(rot), cos(rot)) * local;
  float slip = step(0.9, h11(k + floor(uTime * 6.0))) * 0.3;
  vec4 mv = modelViewMatrix * vec4(centre + local + vec2(slip, 0.0), 0.0, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (1.3 + 1.2 * aSh.w) * clamp(5.2 / -mv.z, 0.55, 1.6);
  float tint = mod(k, 4.0);
  vCol = tint < 1.0 ? vec3(0.3, 0.85, 0.45) : tint < 2.0 ? vec3(0.2, 0.8, 0.95) : tint < 3.0 ? vec3(0.6, 0.45, 1.0) : vec3(0.97, 0.9, 0.78);
  vA = uShard * 0.8 * step(0.25, h11(k * 3.1 + floor(uTime * 3.0))) * (0.6 + 0.4 * aSh.w);
}
`;

/** Scene 7 (fast cuts): a strobing montage of three loud shapes, each a point cloud: an eclipse, an iris, a prism with its spectrum. */
const CUT_VERT = /* glsl */`
${HASH}
attribute vec4 aC;
uniform float uTime, uCut, uCutIdx, uCutT, uPx;
varying vec3 vCol;
varying float vA;
vec3 hsv(float h) { vec3 k = abs(fract(h + vec3(0.0, 0.6667, 0.3333)) * 6.0 - 3.0); return clamp(k - 1.0, 0.0, 1.0); }
void main() {
  vec2 p = vec2(0.0); float a = 1.0; vec3 col = vec3(1.0);
  float ang = aC.x * 6.2831853;
  if (uCutIdx < 0.5) {                               // eclipse: a lit disc with a dark disc sliding over it
    float r = sqrt(aC.y) * 0.8;
    p = vec2(cos(ang), sin(ang)) * r;
    vec2 m = p - vec2(0.34 * (uCutT - 0.4), 0.08);
    a = step(0.74, length(m));
    col = mix(vec3(1.0, 0.82, 0.3), vec3(1.0, 0.3, 0.08), smoothstep(0.3, 0.8, r));
  } else if (uCutIdx < 1.5) {                        // iris: radial fibres around a dark pupil
    float r = sqrt(aC.y) * 0.95;
    p = vec2(cos(ang), sin(ang)) * r;
    a = step(0.24 + 0.05 * sin(uTime * 9.0), r) * (0.55 + 0.45 * sin(ang * 36.0 + aC.z * 6.0));
    col = r > 0.86 ? vec3(0.95, 0.93, 0.9) : mix(vec3(0.95, 0.5, 0.15), vec3(0.2, 0.7, 0.8), smoothstep(0.25, 0.8, r));
  } else {                                           // prism: triangle outline, white beam in, spectrum fanning out
    if (aC.w < 0.35) {
      float e = floor(aC.x * 3.0), f = fract(aC.x * 3.0);
      vec2 v0 = e < 1.0 ? vec2(-0.6, -0.5) : e < 2.0 ? vec2(0.6, -0.5) : vec2(0.0, 0.62);
      vec2 v1 = e < 1.0 ? vec2(0.6, -0.5) : e < 2.0 ? vec2(0.0, 0.62) : vec2(-0.6, -0.5);
      p = mix(v0, v1, f); col = vec3(0.95, 0.97, 1.0);
    } else if (aC.w < 0.55) { p = vec2(-1.9 + aC.y * 1.7, -0.05 + aC.y * 0.1); col = vec3(1.0); }
    else { p = vec2(0.12 + aC.y * 1.9, 0.0 - aC.y * (aC.z - 0.5) * 1.5); col = hsv(aC.z * 0.8); }
  }
  vec4 mv = modelViewMatrix * vec4(p * (0.85 + 0.15 * smoothstep(0.0, 0.25, uCutT)), 0.0, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uPx * (1.2 + 1.5 * aC.z) * clamp(5.2 / -mv.z, 0.55, 1.7);
  vCol = col;
  vA = uCut * a * (0.55 + 0.45 * step(0.5, fract(uTime * 17.0 + aC.w)));       // strobe
}
`;
const GLOW_FRAG = /* glsl */`
precision mediump float;
varying vec3 vCol;
varying float vA;
void main() {
  float d = length(gl_PointCoord - 0.5);
  gl_FragColor = vec4(vCol, smoothstep(0.5, 0.1, d) * vA);
}
`;
const INK_FRAG = /* glsl */`
precision mediump float;
varying float vA, vAcc;
uniform float uDark, uAccent;
uniform highp float uAlarm;                // also in the ring's vertex shader (highp there): the precisions have to match
void main() {
  vec3 ink = mix(vec3(0.04, 0.05, 0.1), vec3(0.9, 0.93, 1.0), uDark);
  vec3 hot = mix(vec3(1.0, 0.45, 0.16), vec3(1.0, 0.18, 0.14), uAlarm);
  gl_FragColor = vec4(mix(ink, hot, vAcc * max(uAccent, uAlarm)), vA);
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

/** The magnifier that follows the pointer: the finished picture is sampled through a glass disc. Magnified in the middle, and the
 *  samples bunch up toward the rim so the edge bends the picture (with a little colour dispersion and a bright rim). */
const LENS_FRAG = /* glsl */`
precision highp float;
varying vec2 vUv;
uniform sampler2D tMap;
uniform vec2 uCenter;
uniform float uAspect, uRadius, uLens, uDark;
vec3 tapAt(vec2 d, float r, float scale) {
  vec2 dir = d / max(r, 1e-5);
  vec2 uv = uCenter + dir * r * scale / vec2(uAspect, 1.0);
  return texture2D(tMap, uv).rgb;
}
void main() {
  vec2 d = (vUv - uCenter) * vec2(uAspect, 1.0);
  float r = length(d);
  float R = uRadius * uLens;
  vec3 col = texture2D(tMap, vUv).rgb;
  if (R > 0.002 && r < R * 1.1) {
    float t = r / R;
    if (t < 1.0) {
      float edge = smoothstep(0.7, 1.0, t);
      float k = 0.5 + 0.5 * pow(t, 3.0);                                // 2x in the middle, back to 1x at the rim
      col = vec3(tapAt(d, r, k * (1.0 - 0.03 * edge)).r, tapAt(d, r, k).g, tapAt(d, r, k * (1.0 + 0.03 * edge)).b);
      float rim = smoothstep(0.955, 1.0, t);
      float spec = pow(max(dot(d / max(r, 1e-5), normalize(vec2(-0.6, 0.8))), 0.0), 5.0);
      col += rim * (mix(vec3(1.0), vec3(0.8, 0.86, 1.0), uDark) * 0.22 + spec * 0.35);
    } else {                                                            // just outside: a ripple of the picture and a soft shadow
      float u = (t - 1.0) / 0.1;
      col = tapAt(d, r, 1.0 - 0.06 * sin(u * 3.14159)).rgb;
      col *= 1.0 - 0.07 * (1.0 - u);
    }
  }
  gl_FragColor = vec4(col, 1.0);
}
`;
const CAPTIONS: Record<string, string> = {
  burst: "every point, in noise.", converge: "pulled into a word.", hold: "inazina.", disperse: "back to noise.", drift: "noise settles.",
  bloom: "something grows.", walk: "a walk, scanned.", stack: "time, stacked.", wave: "every signal at once.", shards: "splintered.", cuts: "fast cuts.", panel: "ticks, counted.", again: "again.",
};
const GRAIN = "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 .55 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>\")";

/** `onSlow` is called once if the first seconds ran under ~26 fps; the caller then drops the scene and shows the still logo. */
export function mountLoginScene(container: HTMLElement, onSlow?: () => void): () => void {
  const renderer = new WebGLRenderer({ antialias: false, alpha: false, powerPreference: "high-performance" });
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
    uFormRate: { value: 0 }, uBurstRate: { value: 0 }, uBeat: { value: 0 }, uPresence: { value: 1 }, uBloom: { value: 0 }, uBloomVis: { value: 0 }, uWalk: { value: 0 }, uWalkVis: { value: 0 }, uStack: { value: 0 }, uWave: { value: 0 }, uShard: { value: 0 }, uCut: { value: 0 }, uCutIdx: { value: 0 }, uCutT: { value: 0 }, uAlarm: { value: 0 }, uRing: { value: 0 }, uDark: { value: 0 }, uAccent: { value: 0 }, uAlphaScale: { value: 1 },
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


  // the two night scenes after the logo: both are just more points, drawn additively so they glow on the dark
  const glowPoints = (count: number, vert: string, attribute: string, additive = true) => {
    const g = new BufferGeometry();
    const data = new Float32Array(count * 4);
    for (let i = 0; i < data.length; i += 1) data[i] = Math.random();
    g.setAttribute("position", new BufferAttribute(new Float32Array(count * 3), 3));
    g.setAttribute(attribute, new BufferAttribute(data, 4));
    const m = new ShaderMaterial({ uniforms, vertexShader: vert, fragmentShader: GLOW_FRAG, transparent: true, depthTest: false, depthWrite: false, blending: additive ? AdditiveBlending : NormalBlending });
    const mesh = new Points(g, m); mesh.frustumCulled = false;
    return { mesh, geometry: g, material: m };
  };
  const bloom = glowPoints(small ? 9000 : 20000, BLOOM_VERT, "aB");
  const walk = glowPoints(small ? 12000 : 26000, WALK_VERT, "aW");
  const stack = glowPoints(small ? 14000 : 30000, STACK_VERT, "aS", false);   // ink on paper, so normal blending
  const wave = glowPoints(small ? 8000 : 14000, WAVE_VERT, "aV", false);
  const shard = glowPoints(small ? 6000 : 12000, SHARD_VERT, "aSh");
  const cut = glowPoints(small ? 8000 : 16000, CUT_VERT, "aC");

  const scene = new Scene();
  scene.add(groundPoints, bloom.mesh, walk.mesh, stack.mesh, wave.mesh, shard.mesh, cut.mesh, ring.mesh, rays.mesh, trails, points, ...ghosts);
  const camera = new PerspectiveCamera(FOV, 1, 0.1, 60);

  const fadeUniforms = { uFade: { value: 1 }, uAspect: { value: 1 }, uDark: uniforms.uDark };
  const fadeGeometry = new BufferGeometry();
  fadeGeometry.setAttribute("position", new BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3));
  const fadeMaterial = new ShaderMaterial({ uniforms: fadeUniforms, vertexShader: FADE_VERT, fragmentShader: FADE_FRAG, transparent: true, depthTest: false, depthWrite: false });
  const fadeMesh = new Mesh(fadeGeometry, fadeMaterial); fadeMesh.frustumCulled = false;
  const fadeScene = new Scene(); fadeScene.add(fadeMesh);

  // the picture (with its trails) lives in a texture; a last pass shows it through the lens that follows the pointer
  const picture = new WebGLRenderTarget(1, 1, { depthBuffer: false, minFilter: LinearFilter, magFilter: LinearFilter });
  const lensUniforms = { tMap: { value: picture.texture }, uCenter: { value: new Vector2(0.5, 0.5) }, uAspect: { value: 1 }, uRadius: { value: 0.17 }, uLens: { value: 0 }, uDark: uniforms.uDark };
  const lensMaterial = new ShaderMaterial({ uniforms: lensUniforms, vertexShader: FADE_VERT, fragmentShader: LENS_FRAG, depthTest: false, depthWrite: false });
  const lensMesh = new Mesh(fadeGeometry, lensMaterial); lensMesh.frustumCulled = false;
  const lensScene = new Scene(); lensScene.add(lensMesh);

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
  // the live panel (bars, sparkline, counter) that fades in with the event ring, like the data-art reel's bottom strip
  const panel = document.createElement("canvas");
  const PANEL = { w: 360, h: 92 };
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  panel.width = PANEL.w * dpr; panel.height = PANEL.h * dpr;
  panel.setAttribute("aria-hidden", "true");
  panel.style.cssText = `position:absolute;right:22px;bottom:18px;width:${PANEL.w}px;height:${PANEL.h}px;max-width:calc(100% - 44px);pointer-events:none;opacity:0`;
  const panelContext = panel.getContext("2d")!;
  const drawPanel = (time: number, night: boolean, alarm: number) => {
    const ctx = panelContext;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, PANEL.w, PANEL.h);
    const ink = night ? "226,232,255" : "18,24,52", hot = "255,60,45";
    const noise = (i: number, k: number) => { const x = Math.sin(i * 12.9898 + k * 78.233) * 43758.5453; return x - Math.floor(x); };
    const slot = Math.floor(time * 5);
    for (let i = 0; i < 30; i += 1) {                          // load bars; the last ones go red in the fault
      const h = 5 + 46 * Math.pow(noise(i, slot), 2.2) * (i > 21 ? 1 + alarm * 0.9 : 1);
      ctx.fillStyle = `rgba(${i > 21 && alarm > 0.3 ? hot : ink},${0.35 + 0.4 * noise(i, 3)})`;
      ctx.fillRect(i * 6, 56 - Math.min(h, 52), 4, Math.min(h, 52));
    }
    ctx.strokeStyle = `rgba(${ink},.55)`; ctx.lineWidth = 1; ctx.beginPath();     // a trace
    for (let x = 0; x <= 150; x += 3) { const y = 80 - 8 - 14 * noise(x * 0.1, slot * 0.2) * (1 + alarm); x === 0 ? ctx.moveTo(190 + x, y) : ctx.lineTo(190 + x, y); }
    ctx.stroke();
    ctx.font = "500 11px ui-monospace,'SF Mono',Menlo,monospace";
    ctx.fillStyle = `rgba(${ink},.72)`; ctx.fillText(`events  ${(1284000 + Math.floor(time * 2400)).toLocaleString("en-US")}`, 190, 12);
    ctx.fillStyle = alarm > 0.3 ? `rgba(${hot},.95)` : `rgba(${ink},.72)`; ctx.fillText(`p95  ${alarm > 0.3 ? "830" : String(100 + Math.floor(noise(slot, 9) * 20))}ms`, 190, 28);
  };
  container.append(grain, vignette, hud, panel);

  let width = 1, height = 1, dist = 6, halfH = 1, halfW = 1;
  const resize = () => {
    width = Math.max(1, container.clientWidth); height = Math.max(1, container.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height; camera.updateProjectionMatrix();
    const t = Math.tan((FOV * Math.PI) / 360);
    dist = Math.max((WORD_HALF_WIDTH / 0.78) / (t * camera.aspect), 0.95 / t);
    halfH = dist * t; halfW = halfH * camera.aspect;
    uniforms.uPx.value = renderer.getPixelRatio() * Math.min(1.5, Math.max(0.7, height / 760));
    fadeUniforms.uAspect.value = camera.aspect; lensUniforms.uAspect.value = camera.aspect;
    const size = renderer.getDrawingBufferSize(new Vector2()); picture.setSize(size.x, size.y);
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

  /** 0 → 1 → 0 across [from, to], easing over `fade` seconds at each end. */
  const span = (t: number, from: number, to: number, fade = 0.5) => smooth((t - from) / fade) * (1 - smooth((t - (to - fade)) / fade));
  const phaseOf = (t: number) => {
    // logo: [burst] 0–1.4 · [converge] 1.4–4.2 · [hold] 4.2–11 (glitch at 4.25, 7.0, 9.6) · [disperse] 11–14 · [drift] 14–16
    // night: [bloom] 16–22.2 · [walk] 22.2–27     paper: [stack] 27–31 · [wave] 31–35
    // night: [shards] 35–39 · [cuts] 39–43 · [panel] 43–48     then [again] 48–52, the cloud collapsing back to the hub for the next burst
    const back = smooth((t - 49) / 2.5);
    const burst = t < 1.4 ? 0.15 + 0.85 * (1 - Math.pow(1 - t / 1.4, 3)) : 1 - 0.85 * back;
    const form = t < 1.4 ? 0 : t < 4.2 ? smooth((t - 1.4) / 2.8) : t < 11 ? 1 : t < 14 ? 1 - smooth((t - 11) / 3) : 0;
    const glitch = (t > 7.0 && t < 7.22) || (t > 9.6 && t < 9.72) || (t > 4.25 && t < 4.4) ? 1 : 0;
    const names: [number, string][] = [[1.4, "burst"], [4.2, "converge"], [11, "hold"], [14, "disperse"], [16, "drift"], [22.2, "bloom"], [27, "walk"], [31, "stack"], [35, "wave"], [39, "shards"], [43, "cuts"], [48, "panel"]];
    const name = names.find(([end]) => t < end)?.[1] ?? "again";
    const dark = Math.max(smooth((t - 11.2) / 0.9) * (1 - smooth((t - 26.6) / 1.0)), smooth((t - 34.6) / 0.8) * (1 - smooth((t - 48.2) / 1.4)));
    const accent = Math.max(glitch, smooth((t - 11) / 1.2) * (1 - smooth((t - 13.5) / 1.5)));
    const presence = t < 14.5 ? 1 : t < 15.7 ? 1 - smooth((t - 14.5) / 1.2) : t < 49 ? 0 : back;      // the logo cloud steps aside
    const cutAt = t - 39, cutIdx = Math.min(2, Math.floor(cutAt / 1.2));
    return {
      burst, form, glitch, name, dark, accent, presence,
      bloomGrowth: smooth((t - 15.8) / 4.2), bloomVis: span(t, 15.6, 22.9),
      walkVis: span(t, 22.2, 27.2), walkTwist: smooth((t - 22.2) / 3.5),
      stackVis: span(t, 27, 31.4), waveVis: span(t, 31, 35.4), shardVis: span(t, 35, 39.4),
      cutVis: cutAt < 3.6 ? span(t, 39, 42.8, 0.2) : 0, cutIdx, cutT: cutAt - cutIdx * 1.2,
      alarm: span(t, 44.4, 46.2, 0.3), panelVis: span(t, 43, 48.2, 0.6),
    };
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
    uniforms.uTrail.value = t < 14 || t > 49 ? 0.32 : 0.5;
    uniforms.uTime.value = time; uniforms.uForm.value = phase.form; uniforms.uBurst.value = phase.burst; uniforms.uGlitch.value = phase.glitch;
    uniforms.uDark.value = phase.dark; uniforms.uAccent.value = phase.accent; uniforms.uPresence.value = phase.presence;
    uniforms.uBloom.value = phase.bloomGrowth; uniforms.uBloomVis.value = phase.bloomVis; uniforms.uWalk.value = phase.walkTwist; uniforms.uWalkVis.value = phase.walkVis;
    uniforms.uStack.value = phase.stackVis; uniforms.uWave.value = phase.waveVis; uniforms.uShard.value = phase.shardVis;
    uniforms.uCut.value = phase.cutVis; uniforms.uCutIdx.value = phase.cutIdx; uniforms.uCutT.value = phase.cutT; uniforms.uAlarm.value = phase.alarm; uniforms.uRing.value = phase.panelVis;
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
    renderer.setRenderTarget(picture);
    renderer.render(fadeScene, camera);
    fadeUniforms.uFade.value = 1 - Math.exp(-delta * (held ? 9 : phase.name === "drift" ? 2.6 : 1.9));
    renderer.render(scene, camera);
    renderer.setRenderTarget(null);
    lensUniforms.uLens.value += ((pointer.active ? 1 : 0) - lensUniforms.uLens.value) * (1 - Math.exp(-delta * 10));
    lensUniforms.uCenter.value.set(pointer.x * 0.5 + 0.5, pointer.y * 0.5 + 0.5);
    renderer.render(lensScene, camera);

    const nightText = phase.dark > 0.5;
    if (lastDark !== (nightText ? 1 : 0)) {
      lastDark = nightText ? 1 : 0;
      hud.style.color = nightText ? "rgba(226,232,255,.7)" : "rgba(18,24,52,.62)";
      caption.style.background = nightText ? "#eef1f8" : "#0b0d1a"; caption.style.color = nightText ? "#0b0d1a" : "#eef1f8";
      vignette.style.background = VIGNETTE[nightText ? "night" : "day"];
      grain.style.mixBlendMode = nightText ? "screen" : "multiply"; grain.style.opacity = nightText ? ".09" : ".14";
    }
    panel.style.opacity = String(phase.panelVis);
    if (phase.name !== captionKey) { captionKey = phase.name; captionAt = now; }
    if (now - hudAt > 60) {
      hudAt = now;
      if (phase.panelVis > 0.01) drawPanel(time, nightText, phase.alarm);
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
    for (const item of [geometry, material, trailGeometry, trailMaterial, rays.geometry, rays.material, ring.geometry, ring.material, bloom.geometry, bloom.material, walk.geometry, walk.material, stack.geometry, stack.material, wave.geometry, wave.material, shard.geometry, shard.material, cut.geometry, cut.material, ground, groundMaterial, fadeGeometry, fadeMaterial, lensMaterial, picture, ...ghostMaterials]) item.dispose();
    renderer.dispose();
    renderer.domElement.remove(); grain.remove(); vignette.remove(); hud.remove(); panel.remove();
  };
}
