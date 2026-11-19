/**
 * The sign-in picture in 3D: the inazina. wordmark as a glossy solid, rising letter by letter, the two blue i dots
 * dropping in and bouncing, the period sending out rings, blue sparks drifting up, a soft bloom on everything blue.
 * three.js for the scene, postprocessing for the bloom. Loaded only on the sign-in screen (dynamic import) and
 * replaced by the plain SVG (scene.svg) when WebGL is missing or the person asked for less motion.
 */
import {
  AdditiveBlending, AmbientLight, DirectionalLight, DoubleSide, ExtrudeGeometry, Group,
  HalfFloatType, InstancedMesh, Matrix4, Mesh, MeshBasicMaterial, MeshPhysicalMaterial, MeshStandardMaterial, PerspectiveCamera, TorusGeometry, Vector3,
  PMREMGenerator, RingGeometry, Scene, SphereGeometry, WebGLRenderer,
} from "three";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { BloomEffect, EffectComposer, EffectPass, RenderPass, VignetteEffect } from "postprocessing";
import { BatchedRenderer, Bezier, Vector4, ConstantColor, ConstantValue, IntervalValue, ParticleSystem, PiecewiseBezier, RenderMode, SizeOverLife, SphereEmitter } from "three.quarks";
import { LETTERS, PERIOD } from "./Logo";

const S = 0.001;                       // one SVG unit in scene units
const BLUE = 0x1f3fff;
const DOTS = [{ x: 117, y: -634, r: 66 }, { x: 1813, y: -634, r: 66 }];
const PERIOD_CENTER = { x: 3116, y: -64, r: 74 };
const WORD_WIDTH = 3273 * S;
const INTRO = { letter: 0.13, letterTime: 1.0, dotStart: 1.0, dotStagger: 0.16, dotTime: 1.4 };

const easeOutBack = (t: number) => { const c = 1.7; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); };
const clamp01 = (t: number) => Math.min(1, Math.max(0, t));
/** 0 → 1 with two decaying bounces. */
function bounce(t: number) {
  t = clamp01(t);
  if (t < 0.55) return (t / 0.55) ** 2;
  if (t < 0.82) { const u = (t - 0.55) / 0.27; return 1 - 0.16 * Math.sin(u * Math.PI); }
  const u = (t - 0.82) / 0.18; return 1 - 0.05 * Math.sin(u * Math.PI);
}

export function mountLoginScene(container: HTMLElement): () => void {
  const renderer = new WebGLRenderer({ antialias: false, alpha: true, powerPreference: "high-performance" });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.domElement.className = "auth-canvas";
  renderer.domElement.setAttribute("aria-hidden", "true");

  const scene = new Scene();
  const pmrem = new PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.1;   // the room only adds soft edge highlights; the letters stay deep black
  scene.add(new AmbientLight(0xffffff, 0.1));
  const key = new DirectionalLight(0xffffff, 0.9); key.position.set(-2, 3, 4); scene.add(key);
  const rim = new DirectionalLight(0x9db0ff, 1.1); rim.position.set(3, -1, -2); scene.add(rim);

  const camera = new PerspectiveCamera(28, 1, 0.1, 50);

  // ---- the wordmark ------------------------------------------------------------------------------------------------
  const stage = new Group();                          // pointer parallax + idle float
  const word = new Group();                           // SVG space, y down
  word.scale.set(S, -S, S);
  word.position.set(-1630 * S, -350 * S, 0);
  stage.add(word);
  scene.add(stage);

  const loader = new SVGLoader();
  const ink = (): MeshPhysicalMaterial => new MeshPhysicalMaterial({
    color: 0x050507, roughness: 0.55, metalness: 0, clearcoat: 0.15, clearcoatRoughness: 0.35, envMapIntensity: 0.12, transparent: true, side: DoubleSide });
  const letters: { mesh: Mesh; delay: number }[] = [];
  const disposables: { dispose(): void }[] = [];
  LETTERS.forEach((d, index) => {
    const data = loader.parse(`<svg xmlns="http://www.w3.org/2000/svg"><path d="${d}"/></svg>`);
    const shapes = data.paths.flatMap((path) => path.toShapes());
    const geometry = new ExtrudeGeometry(shapes, { depth: 150, bevelEnabled: true, bevelThickness: 18, bevelSize: 14, bevelSegments: 4, curveSegments: 14 });
    const mesh = new Mesh(geometry, ink());
    mesh.position.y = 900; mesh.visible = false;
    word.add(mesh);
    letters.push({ mesh, delay: 0.15 + index * INTRO.letter });
    disposables.push(geometry, mesh.material as MeshPhysicalMaterial);
  });

  const glow = () => new MeshStandardMaterial({ color: BLUE, emissive: BLUE, emissiveIntensity: 2.4, roughness: 0.35, metalness: 0, envMapIntensity: 0.4, transparent: true });
  const dotGeometry = new SphereGeometry(1, 40, 24);
  disposables.push(dotGeometry);
  const dots = DOTS.map((dot, index) => {
    const mesh = new Mesh(dotGeometry, glow());
    mesh.scale.setScalar(dot.r); mesh.position.set(dot.x, dot.y - 1700, 75); mesh.visible = false;
    word.add(mesh); disposables.push(mesh.material as MeshStandardMaterial);
    return { mesh, rest: dot.y, delay: INTRO.dotStart + index * INTRO.dotStagger };
  });
  const period = new Mesh(dotGeometry, glow());
  period.scale.setScalar(0.001); period.position.set(PERIOD_CENTER.x, PERIOD_CENTER.y, 75); period.visible = false;
  word.add(period); disposables.push(period.material as MeshStandardMaterial);
  void PERIOD;

  const ringGeometry = new RingGeometry(0.94, 1, 96);
  disposables.push(ringGeometry);
  const rings = [0, 1, 2].map((index) => {
    const material = new MeshBasicMaterial({ color: BLUE, transparent: true, opacity: 0, side: DoubleSide, depthWrite: false });
    const mesh = new Mesh(ringGeometry, material);
    mesh.position.set(PERIOD_CENTER.x, PERIOD_CENTER.y, 60); word.add(mesh); disposables.push(material);
    return { mesh, material, offset: index * 1.4 };
  });

  // ---- floor reflection: the same wordmark mirrored under the baseline, faint ------------------------------------------
  const mirror = new Group();
  mirror.scale.set(1, -1, 1);                       // flip over the baseline (svg y = 0)
  mirror.position.y = 120;
  word.add(mirror);
  const mirrored: Mesh[] = [];
  letters.forEach(({ mesh }) => {
    const copy = new Mesh(mesh.geometry, new MeshBasicMaterial({ color: 0x0c0c12, transparent: true, opacity: 0, depthWrite: false }));
    copy.position.copy(mesh.position); copy.visible = false;
    mirror.add(copy); mirrored.push(copy); disposables.push(copy.material as MeshBasicMaterial);
  });

  // ---- an orbiting ring around the period ------------------------------------------------------------------------------
  const orbitGeometry = new TorusGeometry(1, 0.045, 12, 120);
  disposables.push(orbitGeometry);
  const orbits = [0, 1].map((index) => {
    const material = new MeshBasicMaterial({ color: BLUE, transparent: true, opacity: 0.0, blending: AdditiveBlending, depthWrite: false });
    const mesh = new Mesh(orbitGeometry, material);
    mesh.scale.setScalar(PERIOD_CENTER.r * (2.1 + index * 0.9));
    mesh.position.set(PERIOD_CENTER.x, PERIOD_CENTER.y, 75);
    word.add(mesh); disposables.push(material);
    return { mesh, material, speed: 0.5 + index * 0.35, tilt: 1.0 + index * 0.55 };
  });

  // ---- big soft blue spheres at different depths (they go out of focus and read as bokeh) -------------------------------
  const bokehGeometry = new SphereGeometry(1, 32, 20);
  disposables.push(bokehGeometry);
  const bokeh = Array.from({ length: 9 }, (_, index) => {
    const material = new MeshBasicMaterial({ color: index % 3 === 0 ? 0x7c8dff : BLUE, transparent: true, opacity: 0.28 + (index % 3) * 0.08, blending: AdditiveBlending, depthWrite: false });
    const mesh = new Mesh(bokehGeometry, material);
    const angle = (index / 9) * Math.PI * 2 + 0.6;
    const radius = 1.4 + (index % 4) * 0.35;
    mesh.position.set(Math.cos(angle) * radius, Math.sin(angle * 1.3) * 0.7, -1.2 + (index % 5) * 0.75);
    mesh.scale.setScalar(0.05 + (index % 4) * 0.035);
    scene.add(mesh); disposables.push(material);
    return { mesh, base: mesh.position.clone(), phase: index * 1.7 };
  });

  // ---- particle bursts (three.quarks): the dots and the period spark when they land ------------------------------------
  const batch = new BatchedRenderer();
  scene.add(batch);
  const burstMaterial = new MeshBasicMaterial({ color: 0x3b5bff, transparent: true, blending: AdditiveBlending, depthWrite: false });
  disposables.push(burstMaterial);
  const makeBurst = () => {
    const system = new ParticleSystem({
      duration: 1.2, looping: false, autoDestroy: false,
      startLife: new IntervalValue(0.7, 1.4), startSpeed: new IntervalValue(0.35, 1.1), startSize: new IntervalValue(0.008, 0.022),
      startColor: new ConstantColor(new Vector4(0.12, 0.25, 1, 0.8)), worldSpace: true,
      emissionOverTime: new ConstantValue(0), emissionBursts: [{ time: 0, count: new ConstantValue(46), cycle: 1, interval: 0.01, probability: 1 }],
      shape: new SphereEmitter({ radius: 0.04, thickness: 1, arc: Math.PI * 2 }),
      material: burstMaterial, renderMode: RenderMode.BillBoard, renderOrder: 3,
    });
    system.addBehavior(new SizeOverLife(new PiecewiseBezier([[new Bezier(1, 0.85, 0.4, 0), 0]])));
    scene.add(system.emitter); batch.addSystem(system);
    return system;
  };
  const bursts = { a: makeBurst(), b: makeBurst(), c: makeBurst() };
  const burstFired = { a: false, b: false, c: false };
  const fire = (system: ParticleSystem, mesh: Mesh) => {
    const position = new Vector3(); mesh.getWorldPosition(position);
    system.emitter.position.copy(position); system.restart();
  };

  // ---- sparks ------------------------------------------------------------------------------------------------------
  const SPARKS = 110;
  const sparkMaterial = new MeshBasicMaterial({ color: BLUE, transparent: true, opacity: 0.85 });
  const sparkGeometry = new SphereGeometry(1, 10, 8);
  disposables.push(sparkMaterial, sparkGeometry);
  const sparks = new InstancedMesh(sparkGeometry, sparkMaterial, SPARKS);
  const seeds = Array.from({ length: SPARKS }, () => ({ x: (Math.random() - 0.5) * 4.6, z: -0.6 + Math.random() * 1.4, speed: 0.06 + Math.random() * 0.12,
    phase: Math.random(), size: 0.006 + Math.random() * 0.012, sway: Math.random() * 6 }));
  scene.add(sparks);
  const matrix = new Matrix4();

  // ---- composer ----------------------------------------------------------------------------------------------------
  const composer = new EffectComposer(renderer, { frameBufferType: HalfFloatType, multisampling: 4, alpha: true });
  composer.addPass(new RenderPass(scene, camera));
  composer.addPass(new EffectPass(camera, new BloomEffect({ intensity: 0.8, luminanceThreshold: 0.45, luminanceSmoothing: 0.3, mipmapBlur: true, radius: 0.72 }), new VignetteEffect({ offset: 0.35, darkness: 0.35 })));

  container.appendChild(renderer.domElement);

  let baseDistance = 7, lastPeriodBurst = 0;
  const resize = () => {
    const width = Math.max(1, container.clientWidth), height = Math.max(1, container.clientHeight);
    renderer.setSize(width, height, false);
    composer.setSize(width, height, false);
    camera.aspect = width / height;
    const visibleWidth = WORD_WIDTH * 1.32;
    const distance = visibleWidth / (2 * Math.tan((camera.fov * Math.PI) / 360) * Math.min(camera.aspect, 1.6));
    baseDistance = distance;
    camera.updateProjectionMatrix();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  const onPointer = (event: PointerEvent) => {
    const rect = container.getBoundingClientRect();
    pointer.tx = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
    pointer.ty = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
  };
  window.addEventListener("pointermove", onPointer, { passive: true });

  let running = true, visible = true, frame = 0, last = performance.now(), time = 0;
  const render = (now: number) => {
    frame = requestAnimationFrame(render);
    if (!running || !visible) { last = now; return; }
    const delta = Math.min(1, (now - last) / 1000); last = now; time += delta;   // wall clock: a slow machine still gets the whole intro

    letters.forEach(({ mesh, delay }, index) => {
      const p = clamp01((time - delay) / INTRO.letterTime);
      mesh.visible = p > 0;
      mesh.position.y = 900 * (1 - easeOutBack(p));
      mesh.rotation.x = -1.15 * (1 - easeOutBack(p));       // each letter tips up from the baseline as it rises
      (mesh.material as MeshPhysicalMaterial).opacity = clamp01(p * 2.2);
      const copy = mirrored[index];
      copy.visible = p > 0; copy.position.copy(mesh.position); copy.rotation.copy(mesh.rotation);
      (copy.material as MeshBasicMaterial).opacity = 0.055 * clamp01(p * 1.5);
    });
    dots.forEach(({ mesh, rest, delay }, index) => {
      const p = clamp01((time - delay) / INTRO.dotTime);
      mesh.visible = p > 0;
      mesh.position.y = rest - 1700 * (1 - bounce(p));
      (mesh.material as MeshStandardMaterial).opacity = clamp01(p * 3);
      const key = index === 0 ? "a" : "b";
      if (!burstFired[key] && p >= 0.56) { burstFired[key] = true; fire(bursts[key], mesh); }
    });
    const pp = clamp01((time - 1.45) / 1.1);
    period.visible = pp > 0;
    period.scale.setScalar(Math.max(0.001, PERIOD_CENTER.r * easeOutBack(pp)));
    const pulse = 1 + 0.05 * Math.sin(time * 2.2);
    if (pp >= 1) period.scale.setScalar(PERIOD_CENTER.r * pulse);
    for (const ring of rings) {
      const t = ((time - 2.2 - ring.offset) % 4.2 + 4.2) % 4.2 / 4.2;
      const active = time > 2.2 + ring.offset;
      const scale = PERIOD_CENTER.r * (1 + t * 12);
      ring.mesh.scale.setScalar(scale);
      ring.material.opacity = active ? 0.5 * (1 - t) ** 1.6 : 0;
    }

    if (!burstFired.c && pp >= 0.55) { burstFired.c = true; lastPeriodBurst = time; fire(bursts.c, period); }
    else if (burstFired.c && time - lastPeriodBurst > 6.5) { lastPeriodBurst = time; fire(bursts.c, period); }
    const orbitIn = clamp01((time - 2.4) / 1.2);
    for (const orbit of orbits) {
      orbit.mesh.rotation.set(orbit.tilt, time * orbit.speed, time * 0.2);
      orbit.material.opacity = 0.42 * orbitIn;
    }
    for (const item of bokeh) {
      item.mesh.position.set(item.base.x + Math.sin(time * 0.25 + item.phase) * 0.12, item.base.y + Math.cos(time * 0.3 + item.phase) * 0.09, item.base.z);
      (item.mesh.material as MeshBasicMaterial).opacity = Math.min(0.4, clamp01((time - 0.4) / 2) * 0.4);
    }
    // camera: sweeps in from the side and a little closer, settles in front of the wordmark
    const sweep = clamp01(time / 3.4), eased = 1 - Math.pow(1 - sweep, 3);
    const angle = (1 - eased) * 0.62, distance = baseDistance * (1 + (1 - eased) * 0.5);
    camera.position.set(Math.sin(angle) * distance, 0.05 + (1 - eased) * 0.3, Math.cos(angle) * distance);
    camera.lookAt(0, 0, 0);
    batch.update(delta);

    pointer.x += (pointer.tx - pointer.x) * 0.05; pointer.y += (pointer.ty - pointer.y) * 0.05;
    stage.rotation.y = pointer.x * 0.16 + Math.sin(time * 0.35) * 0.05;
    stage.rotation.x = pointer.y * 0.08;
    stage.position.y = Math.sin(time * 0.8) * 0.018;

    for (let index = 0; index < SPARKS; index += 1) {
      const seed = seeds[index];
      const progress = ((time * seed.speed + seed.phase) % 1);
      const x = seed.x + Math.sin(time * 0.4 + seed.sway) * 0.08;
      const y = -1.1 + progress * 2.3;
      const fade = Math.sin(progress * Math.PI);
      matrix.makeScale(seed.size * fade, seed.size * fade, seed.size * fade).setPosition(x, y, seed.z);
      sparks.setMatrixAt(index, matrix);
    }
    sparks.instanceMatrix.needsUpdate = true;
    composer.render(delta);
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
    for (const item of disposables) item.dispose();
    sparks.dispose(); batch.dispose?.();
    composer.dispose(); pmrem.dispose(); renderer.dispose();
    renderer.domElement.remove();
  };
}
