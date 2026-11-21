// Entry screen: a terminal window showing a turning 3D model drawn in ASCII
// (Three.js AsciiEffect, as in the "3D ASCII Model Viewer" Figma Make file),
// with a model list, a control panel and "enter to start", in the current theme's colours.
// Three.js is loaded on demand, and everything is disposed on entering.


// A model is either a .glb file or a procedural scene from entry-scenes.ts.
type Model = { id: string; name: string; credit: string; file?: string; scene?: keyof typeof import("./entry-scenes") };

const MODELS: Model[] = [
  { id: "avocado", name: "Avocado", file: "/models/avocado.glb", credit: "Avocado — Microsoft, CC0 1.0" },
  { id: "teacup", name: "Teacup", file: "/models/teacup.glb", credit: "Teacup — Polyhaven.com and Eric Chadwick, CC0 1.0" },
  { id: "fish", name: "Fish", file: "/models/fish.glb", credit: "Barramundi Fish — Microsoft, CC0 1.0" },
  { id: "waterbottle", name: "Bottle", file: "/models/waterbottle.glb", credit: "Water Bottle — Microsoft, CC0 1.0" },
  { id: "chair", name: "Chair", file: "/models/chair.glb", credit: "Chair Damask Purplegold — Eric Chadwick / Wayfair, CC BY 4.0" },
  { id: "barnlamp", name: "Barn Lamp", file: "/models/barnlamp.glb", credit: "Anisotropy Barn Lamp — Eric Chadwick / Wayfair, LLC, CC BY 4.0" },
  { id: "fridge", name: "Fridge", file: "/models/fridge.glb", credit: "Commercial Refrigerator — Eric Chadwick / Wayfair, CC BY 4.0" },
  { id: "sheenchair", name: "Armchair", file: "/models/sheenchair.glb", credit: "Sheen Chair — Eric Chadwick / Wayfair, CC0 1.0" },
  { id: "pouf", name: "Pouf", file: "/models/pouf.glb", credit: "Specular Silk Pouf — Eric Chadwick / Wayfair, CC BY 4.0" },
  { id: "olives", name: "Olives", file: "/models/olives.glb", credit: "Iridescent Dish with Olives — Wayfair, CC BY 4.0" },
  { id: "tree", name: "Xmas Tree", scene: "christmasTree", credit: "Xmas Tree — after dwcminh.ascii \"ASCII Christmas Tree\"" },
  { id: "bouquet", name: "Bouquet", scene: "roseBouquet", credit: "Bouquet — after dwcminh.ascii rose bouquet" },
  { id: "heart", name: "Heart", scene: "heart", credit: "Heart — after jin_22ee ASCII reels" },
  { id: "ocean", name: "Ocean", scene: "ocean", credit: "Ocean — after jin_22ee ASCII reels" },
  { id: "blackhole", name: "Black Hole", scene: "blackHole", credit: "Black Hole — after jin_22ee ASCII reels" },
  { id: "cake", name: "Cake", scene: "cake", credit: "Cake — after jin_22ee ASCII reels" },
  { id: "rose", name: "Rose", scene: "rose", credit: "Rose — after jin_22ee ASCII reels" },
  { id: "ring", name: "Ring", scene: "ring", credit: "Ring — after jin_22ee ASCII reels" },
  { id: "snowman", name: "Snowman", scene: "snowman", credit: "Snowman — after jin_22ee ASCII reels" },
];
// The model list shows a window of this many names. ▲ ▼ move it a page; the mouse wheel
// moves it one line at a time, with no easing, like scrolling a terminal.
const MODELS_VISIBLE = 8;
const PRESETS: [string, string][] = [[".:-=+*#%@", " .:-=+*#%@"], [".-+*#", " .-+*#"]];
// The reference's defaults; colours follow the theme (see entryColors).
const DEFAULTS = { model: "avocado", characters: " .:-=+*#%@", resolution: 0.22, scale: 1 };

// The entry always takes the current theme's own background and text colours (--bg / --ink),
// the default "system" theme included.
// A terminal theme puts --t-bg / --t-fg on <html> itself; the default "system" theme only has
// --bg / --ink from the stylesheet, which in dev arrives a moment after the script (Safari then
// reads them as ""), so that case waits for it.
function entryColors() {
  const css = getComputedStyle(document.documentElement);
  const read = (theme: string, sheet: string) => css.getPropertyValue(theme).trim() || css.getPropertyValue(sheet).trim();
  return { fg: read("--t-fg", "--ink"), bg: read("--t-bg", "--bg") };
}
async function stylesheetReady() {
  for (let frame = 0; frame < 120 && !getComputedStyle(document.documentElement).getPropertyValue("--bg").trim(); frame++)
    await new Promise(requestAnimationFrame);
}

const html = String.raw;
const escapeHtml = (text: string) => text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
type LogEntry = { at: string; ip: string; location: string; length?: number; pass: boolean };

export async function showEntry(onEnter: () => void): Promise<void> {
  if (document.body.classList.contains("entering")) return;
  await stylesheetReady();
  const colors = entryColors();
  document.body.classList.add("entering");
  const root = document.createElement("div");
  root.className = "entry";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", "Wide-Learning 시작 화면");
  root.innerHTML = html`
    <div class="term entry-term">
      <header class="term-bar"><span class="term-title"><b>wl@laplace</b> — main<i class="cursor" aria-hidden="true"></i></span></header>
      <div class="entry-stage" style="--entry-fg:${colors.fg};--entry-bg:${colors.bg}">
        <div class="entry-canvas" aria-hidden="true"></div>
        <nav class="entry-models" aria-label="모델">
          <div class="entry-model-page"></div>
          <hr>
          <div class="entry-model-arrows"><button type="button" data-page="-1" aria-label="이전 모델">▲</button><button type="button" data-page="1" aria-label="다음 모델">▼</button></div>
        </nav>
        <aside class="entry-panel" aria-label="ASCII 설정">
          <section><h2>Presets</h2>${PRESETS.map(([label, chars]) =>
            `<button type="button" class="entry-btn" data-chars="${chars}">${label}</button>`).join("")}</section>
          <section><h2>Resolution</h2><input type="range" min="0.05" max="0.5" step="0.01" data-set="resolution"><output data-out="resolution"></output></section>
          <section><h2>Scale</h2><input type="range" min="0.1" max="3" step="0.1" data-set="scale"><output data-out="scale"></output></section>
          <button type="button" class="entry-btn" data-action="reset">Reset</button>
          <button type="button" class="entry-link" data-action="credits">Credits</button>
        </aside>
        <button type="button" class="entry-start" data-action="enter">enter to start<i class="cursor" aria-hidden="true"></i></button>
        <p class="entry-status" role="status"></p>
        <div class="entry-prompt" hidden>
          <form class="entry-prompt-box" role="dialog" aria-label="비밀번호">
            <label><span class="entry-label">비밀번호</span>
              <input class="entry-secret" type="password" autocomplete="off" spellcheck="false" aria-label="비밀번호" maxlength="128"></label>
          </form>
        </div>
        <div class="entry-log" hidden>
          <section class="entry-log-box" role="dialog" aria-label="로그인 기록">
            <header><b>login attempts</b><span>esc 닫기</span></header>
            <div class="entry-log-body"></div>
          </section>
        </div>
      </div>
    </div>
    <dialog class="entry-credits"><h2>Credits</h2>${MODELS.map((model) => `<p>${model.credit}</p>`).join("")}
      <p>Other models from the Khronos glTF Sample Assets (textures removed).</p><form method="dialog"><button class="entry-btn">닫기</button></form></dialog>`;
  document.body.prepend(root);

  const $ = <T extends Element>(selector: string) => root.querySelector(selector) as T;
  const state = { ...DEFAULTS };
  const lastStart = Math.max(0, MODELS.length - MODELS_VISIBLE);
  let start = 0;
  const renderModels = () => {
    $<HTMLDivElement>(".entry-model-page").innerHTML = MODELS.slice(start, start + MODELS_VISIBLE).map((model) =>
      `<button type="button" data-model="${model.id}" aria-current="${model.id === state.model}">${model.name}</button>`).join("");
    $<HTMLButtonElement>("[data-page='-1']").disabled = start === 0;
    $<HTMLButtonElement>("[data-page='1']").disabled = start >= lastStart;
    $<HTMLDivElement>(".entry-model-arrows").hidden = lastStart === 0;
  };
  const scrollModels = (lines: number) => {
    const next = Math.max(0, Math.min(lastStart, start + lines));
    if (next !== start) { start = next; renderModels(); }
  };
  renderModels();
  root.querySelectorAll<HTMLButtonElement>("[data-page]").forEach((button) =>
    button.addEventListener("click", () => scrollModels(Number(button.dataset.page) * MODELS_VISIBLE)));
  // One wheel notch (or ~40px of trackpad travel) is one line.
  let wheel = 0;
  $<HTMLElement>(".entry-models").addEventListener("wheel", (event) => {
    event.preventDefault();
    wheel += event.deltaMode === 1 ? event.deltaY * 40 : event.deltaY;
    const lines = Math.trunc(wheel / 40);
    if (lines) { wheel -= lines * 40; scrollModels(Math.sign(lines)); }
  }, { passive: false });
  const status = $<HTMLParagraphElement>(".entry-status");
  let cleanup = () => {};
  // Follow a theme change while the entry is open: new stage colours, and the scene redraws.
  const stage = $<HTMLDivElement>(".entry-stage");
  const themeWatch = new MutationObserver(() => {
    Object.assign(colors, entryColors());
    stage.style.setProperty("--entry-fg", colors.fg);
    stage.style.setProperty("--entry-bg", colors.bg);
    root.dispatchEvent(new Event("entry-theme"));
  });
  themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["style", "data-theme"] });

  const enter = () => {
    cleanup();
    themeWatch.disconnect();
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("keyup", onKeyUp, true);
    window.removeEventListener("blur", onBlur);
    root.classList.add("leaving");
    document.body.classList.remove("entering");
    setTimeout(() => root.remove(), 260);
    onEnter();
  };
  // Enter asks for the password in a small TUI box. Right: into the terminal.
  // Wrong (or Esc, or a click outside): the box just closes, nothing else is said.
  const prompt = $<HTMLDivElement>(".entry-prompt");
  const secret = $<HTMLInputElement>(".entry-secret");
  const closePrompt = () => {
    prompt.hidden = true;
    secret.value = "";
    $<HTMLButtonElement>(".entry-start").focus({ preventScroll: true });
  };
  const openPrompt = () => {
    prompt.hidden = false;
    secret.value = "";
    secret.focus({ preventScroll: true });
  };
  secret.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); closePrompt(); } });
  prompt.addEventListener("mousedown", (event) => { if (event.target === prompt) closePrompt(); else event.preventDefault(); });
  $<HTMLFormElement>(".entry-prompt-box").addEventListener("submit", (event) => {
    event.preventDefault();
    const typed = secret.value;
    // The dev server judges and records the attempt itself (time, address, location, verdict).
    void fetch("/wl-api/entry-attempt", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify({ typed }) })
      .then((reply) => reply.json() as Promise<{ pass?: boolean }>).then(({ pass }) => (pass ? enter() : closePrompt())).catch(closePrompt);
  });
  // While the entry is open, the terminal's own shortcuts wait.
  // Hidden login log: with the password box closed, type the log code (set in the settings tab; 0154 until
  // changed), then press Shift, Space, Esc and Enter together (within 5 s). While armed, Space and Enter don't open the password box.
  const logView = $<HTMLDivElement>(".entry-log");
  const COMBO = ["Shift", " ", "Escape", "Enter"];
  const held = new Set<string>();
  let code = "", codeAt = 0;
  const armed = () => code.length >= 4 && performance.now() - codeAt < 5000;
  const closeLog = () => { logView.hidden = true; $<HTMLButtonElement>(".entry-start").focus({ preventScroll: true }); };
  const openLog = async (opened: string) => {
    const body = $<HTMLDivElement>(".entry-log-body");
    try {
      // The server checks the digits typed last against the log code; a wrong one gets nothing.
      const reply = await fetch("/wl-api/entry-log", { method: "POST", cache: "no-store", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify({ code: opened }) });
      if (!reply.ok) return;  // a wrong code does nothing at all, so the log view stays unannounced
      const { entries } = await reply.json() as { entries: LogEntry[] };
      logView.hidden = false;
      body.innerHTML = entries.length ? html`<table>
        <thead><tr><th>일시</th><th>IP</th><th>위치</th><th>입력</th><th>결과</th></tr></thead>
        <tbody>${entries.map((entry) => `<tr class="${entry.pass ? "pass" : "fail"}"><td>${escapeHtml(new Date(entry.at).toLocaleString("ko-KR", { hour12: false }))}</td>`
          + `<td>${escapeHtml(entry.ip)}</td><td>${escapeHtml(entry.location)}</td>`
          + `<td>${typeof entry.length === "number" ? `${"●".repeat(Math.min(entry.length, 16))} (${entry.length}자)` : "<i>(기록 안 함)</i>"}</td><td>${entry.pass ? "Pass" : "Fail"}</td></tr>`).join("")}</tbody>
      </table>` : "기록이 없습니다.";
    } catch { body.textContent = "기록을 불러오지 못했습니다."; }
  };
  const onKey = (event: KeyboardEvent) => {
    if ((event.target as HTMLElement).closest("input, dialog")) return;
    held.add(event.key);
    if (!logView.hidden) {
      if (event.key === "Escape") closeLog();
    } else if (armed() && COMBO.includes(event.key)) {
      event.preventDefault();
      if (COMBO.every((key) => held.has(key))) { const typed = code; code = ""; void openLog(typed); }
    } else if (/^\d$/.test(event.key)) {
      code = (code + event.key).slice(-8);
      codeAt = performance.now();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      code = "";
      openPrompt();
    } else if (event.key !== "Shift") code = "";
    event.stopPropagation();
  };
  const onKeyUp = (event: KeyboardEvent) => held.delete(event.key);
  const onBlur = () => held.clear();
  window.addEventListener("keyup", onKeyUp, true);
  window.addEventListener("blur", onBlur);
  window.addEventListener("keydown", onKey, true);
  $<HTMLButtonElement>("[data-action=enter]").addEventListener("click", openPrompt);
  $<HTMLButtonElement>("[data-action=credits]").addEventListener("click", () => $<HTMLDialogElement>(".entry-credits").showModal());

  // One retry covers a module or model request that failed on the way (e.g. while the
  // dev server re-bundled); after that, say why and keep the entry usable without 3D.
  const say = (text: string) => { status.textContent = text; };
  for (let attempt = 1; ; attempt++) {
    try {
      $<HTMLDivElement>(".entry-canvas").replaceChildren();
      cleanup = await startScene($<HTMLDivElement>(".entry-canvas"), state, say, root, colors);
      break;
    } catch (error) {
      console.error("[wl entry] 3D scene failed", error);
      if (attempt < 2 && root.isConnected) { await new Promise((done) => setTimeout(done, 800)); continue; }
      say(`3D 화면을 띄우지 못했습니다 (${error instanceof Error ? error.message : String(error)}). Enter로 계속할 수 있습니다.`);
      break;
    }
  }
  $<HTMLButtonElement>(".entry-start").focus({ preventScroll: true });
}

async function startScene(host: HTMLDivElement, state: typeof DEFAULTS, say: (text: string) => void, root: HTMLElement, colors: ReturnType<typeof entryColors>) {
  const THREE = await import("three");
  const { colorAscii } = await import("./entry-ascii-color");
  const [{ AsciiEffect }, { OrbitControls }, { GLTFLoader }, { RoomEnvironment }] = await Promise.all([
    import("three/addons/effects/AsciiEffect.js"),
    import("three/addons/controls/OrbitControls.js"),
    import("three/addons/loaders/GLTFLoader.js"),
    import("three/addons/environments/RoomEnvironment.js"),
  ]);

  const renderer = new THREE.WebGLRenderer();
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 100);
  camera.position.set(0, 0, 3);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.add(new THREE.AmbientLight(0xffffff, 0.5));
  const sun = new THREE.DirectionalLight(0xffffff, 1);
  sun.position.set(10, 10, 5);
  scene.add(sun);
  const fill = new THREE.PointLight(0xffffff, 0.5);
  fill.position.set(-10, -10, -5);
  scene.add(fill);

  const holder = new THREE.Group();  // user scale applies here
  scene.add(holder);
  const loader = new GLTFLoader();
  const cache = new Map<string, { object: import("three").Object3D; clips: import("three").AnimationClip[]; update?: (t: number) => void }>();
  let mixer: import("three").AnimationMixer | null = null;
  let animate: ((t: number) => void) | null = null;  // procedural scenes
  let shownAt = 0;
  let colorful = false;  // procedural scenes are drawn in their own colours; .glb models stay one colour
  let current: import("three").Object3D | null = null;
  type Effect = { domElement: HTMLElement; setSize(w: number, h: number): void; render(s: import("three").Scene, c: import("three").Camera): void; dispose?(): void };
  let effect: Effect | null = null;
  let controls: InstanceType<typeof OrbitControls> | null = null;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Every model is centred and fitted to the same size, so no per-model scale table is needed.
  const fit = (object: import("three").Object3D) => {
    const box = new THREE.Box3().setFromObject(object);
    const size = box.getSize(new THREE.Vector3()).length() || 1;
    const center = box.getCenter(new THREE.Vector3());
    const wrapper = new THREE.Group();
    // Some light-weighted models dropped their normals; shading needs them back.
    object.traverse((child) => {
      const mesh = child as import("three").Mesh;
      if (mesh.isMesh && !mesh.geometry.attributes.normal) mesh.geometry.computeVertexNormals();
    });
    object.position.sub(center);
    wrapper.add(object);
    wrapper.scale.setScalar(1.9 / size);
    return wrapper;
  };

  async function show(id: string) {
    const model = MODELS.find((item) => item.id === id) ?? MODELS[0];
    root.querySelectorAll<HTMLButtonElement>("[data-model]").forEach((button) =>
      button.setAttribute("aria-current", String(button.dataset.model === model.id)));
    say(`${model.name} 불러오는 중…`);
    let entry = cache.get(model.id);
    if (!entry) {
      if (model.scene) {
        const built = (await import("./entry-scenes"))[model.scene](THREE);
        const object = fit(built.object);  // fitted at full size, without the sparkles around it
        if (built.ambient) { built.ambient.position.copy(built.object.position); object.add(built.ambient); }
        entry = { object, clips: [], update: built.update };
      } else {
        const gltf = await loader.loadAsync(model.file!);
        entry = { object: fit(gltf.scene), clips: gltf.animations };
      }
      cache.set(model.id, entry);
    }
    if (current) holder.remove(current);
    current = entry.object;
    holder.add(current);
    mixer = null;
    animate = entry.update ?? null;
    isScene = Boolean(model.scene);
    if (colorful !== wantColor()) { colorful = wantColor(); rebuild(); }
    shownAt = performance.now();  // a scene replays from the start each time it is picked
    animate?.(reduced ? 60 : 0);
    if (entry.clips.length) {  // the fox walks
      mixer = new THREE.AnimationMixer(current);
      const clip = entry.clips.find((item) => /walk/i.test(item.name)) ?? entry.clips[0];
      mixer.clipAction(clip).play();
    }
    say("");
  }

  // AsciiEffect takes its options at construction, so a setting change rebuilds it
  // (the reference re-mounts its AsciiRenderer the same way).
  // Procedural scenes are drawn in colour, except on Deep Dark, which is black and white only.
  let isScene = false;
  const wantColor = () => isScene && document.documentElement.dataset.theme !== "deep-dark";
  const onTheme = () => { colorful = wantColor(); rebuild(); };
  root.addEventListener("entry-theme", onTheme);

  function rebuild() {
    const target = controls?.target.clone();
    controls?.dispose();
    effect?.domElement.remove();
    effect?.dispose?.();
    // Colour scenes draw on a canvas (fast); the one-colour models keep AsciiEffect's text grid.
    effect = colorful ? colorAscii(THREE, renderer, state.characters, state.resolution)
      : new AsciiEffect(renderer, state.characters, { invert: true, resolution: state.resolution });
    effect.domElement.style.color = colors.fg;
    effect.domElement.style.backgroundColor = colors.bg;
    effect.domElement.classList.add("entry-ascii");
    host.append(effect.domElement);
    resize();
    controls = new OrbitControls(camera, effect.domElement);
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 2;
    controls.enableDamping = true;
    if (target) controls.target.copy(target);
  }

  function resize() {
    const { width, height } = host.getBoundingClientRect();
    camera.aspect = width / Math.max(1, height);
    camera.updateProjectionMatrix();
    effect?.setSize(Math.max(1, Math.floor(width)), Math.max(1, Math.floor(height)));
  }

  function sync() {
    root.querySelector<HTMLInputElement>("[data-set=resolution]")!.value = String(state.resolution);
    root.querySelector<HTMLInputElement>("[data-set=scale]")!.value = String(state.scale);
    root.querySelector("[data-out=resolution]")!.textContent = state.resolution.toFixed(3);
    root.querySelector("[data-out=scale]")!.textContent = state.scale.toFixed(2);
    root.querySelectorAll<HTMLButtonElement>("[data-chars]").forEach((button) =>
      button.setAttribute("aria-pressed", String(button.dataset.chars === state.characters)));
    holder.scale.setScalar(state.scale);
  }

  let pending = 0;
  const onPanel = (event: Event) => {
    const element = event.target as HTMLElement;
    const button = element.closest("button");
    if (event.type === "click" && button?.dataset.model) {
      state.model = button.dataset.model;
      void show(state.model).catch(() => say("모델을 불러오지 못했습니다."));
    }
    else if (event.type === "click" && button?.dataset.chars) { state.characters = button.dataset.chars; rebuild(); }
    else if (event.type === "click" && button?.dataset.action === "reset") { Object.assign(state, DEFAULTS, { model: state.model }); rebuild(); }
    else if (element instanceof HTMLInputElement && element.dataset.set === "scale") state.scale = Number(element.value);
    // Applied while dragging, like scale; rebuilding the ASCII grid at most once a frame.
    else if (element instanceof HTMLInputElement && element.dataset.set === "resolution" && Number(element.value) !== state.resolution) {
      state.resolution = Number(element.value);
      if (!pending) pending = requestAnimationFrame(() => { pending = 0; rebuild(); });
    }
    sync();
  };
  root.addEventListener("click", onPanel);
  root.addEventListener("input", onPanel);
  root.addEventListener("change", onPanel);

  const observer = new ResizeObserver(resize);
  observer.observe(host);
  const clock = new THREE.Clock();
  let frame = 0;
  const loop = () => {
    frame = requestAnimationFrame(loop);
    const delta = clock.getDelta();
    mixer?.update(delta);
    if (animate && !reduced) animate((performance.now() - shownAt) / 1000);
    controls?.update(delta);
    effect?.render(scene, camera);
  };

  const dispose = () => {
    cancelAnimationFrame(frame);
    cancelAnimationFrame(pending);
    observer.disconnect();
    root.removeEventListener("click", onPanel);
    root.removeEventListener("input", onPanel);
    root.removeEventListener("change", onPanel);
    root.removeEventListener("entry-theme", onTheme);
    controls?.dispose();
    effect?.domElement.remove();
    effect?.dispose?.();
    scene.environment?.dispose();
    pmrem.dispose();
    scene.traverse((object) => {
      const mesh = object as import("three").Mesh;
      mesh.geometry?.dispose();
      const materials = Array.isArray(mesh.material) ? mesh.material : mesh.material ? [mesh.material] : [];
      materials.forEach((material) => {
        const textured = material as unknown as Record<string, { dispose?: () => void } | null | undefined>;
        for (const slot of ["map", "normalMap", "roughnessMap", "metalnessMap", "emissiveMap", "aoMap"]) textured[slot]?.dispose?.();
        material.dispose();
      });
    });
    renderer.dispose();
    renderer.domElement.remove();
    renderer.forceContextLoss();
  };

  try {
    rebuild();
    sync();
    await show(state.model);
  } catch (error) {
    dispose();  // a failed start leaves nothing behind for the retry
    throw error;
  }
  loop();
  return dispose;
}
