// Entry screen: a terminal window showing a turning 3D model drawn in ASCII
// (Three.js AsciiEffect, as in the "3D ASCII Model Viewer" Figma Make file),
// with a model list, a control panel and "enter to start". Colours are always inverted.
// Three.js is loaded on demand, and everything is disposed on entering.

type Model = { id: string; name: string; file: string; credit: string };

const MODELS: Model[] = [
  { id: "suzanne", name: "Suzanne", file: "/models/suzanne.glb", credit: "Suzanne — Blender Foundation, CC0 1.0" },
  { id: "fox", name: "Fox", file: "/models/fox.glb", credit: "Fox — tomkranis; glTF by @AsoboStudio and @scurest, CC BY 4.0" },
  { id: "lantern", name: "Lantern", file: "/models/lantern.glb", credit: "Lantern — Microsoft, CC0 1.0" },
  { id: "avocado", name: "Avocado", file: "/models/avocado.glb", credit: "Avocado — Microsoft, CC0 1.0" },
  { id: "boombox", name: "Boom Box", file: "/models/boombox.glb", credit: "Boom Box — Microsoft, CC0 1.0" },
  { id: "teacup", name: "Teacup", file: "/models/teacup.glb", credit: "Teacup — Polyhaven.com and Eric Chadwick, CC0 1.0" },
  { id: "fish", name: "Fish", file: "/models/fish.glb", credit: "Barramundi Fish — Microsoft, CC0 1.0" },
  { id: "waterbottle", name: "Bottle", file: "/models/waterbottle.glb", credit: "Water Bottle — Microsoft, CC0 1.0" },
  { id: "corset", name: "Corset", file: "/models/corset.glb", credit: "Corset — Microsoft / UX3D, CC0 1.0" },
  { id: "shoe", name: "Shoe", file: "/models/shoe.glb", credit: "Materials Variants Shoe — Shopify, CC BY 4.0" },
  { id: "chair", name: "Chair", file: "/models/chair.glb", credit: "Chair Damask Purplegold — Eric Chadwick / Wayfair, CC BY 4.0" },
  { id: "sofa", name: "Sofa", file: "/models/sofa.glb", credit: "Glam Velvet Sofa — Eric Chadwick / Wayfair, LLC, CC BY 4.0" },
  { id: "barnlamp", name: "Barn Lamp", file: "/models/barnlamp.glb", credit: "Anisotropy Barn Lamp — Eric Chadwick / Wayfair, LLC, CC BY 4.0" },
  { id: "dragon", name: "Dragon", file: "/models/dragon.glb", credit: "Dragon — Stanford 3D Scanning Repository (glTF: Khronos)" },
  { id: "helmet", name: "Helmet", file: "/models/helmet.glb", credit: "Damaged Helmet — theblueturtle_, CC BY-NC 4.0" },
  { id: "brainstem", name: "BrainStem", file: "/models/brainstem.glb", credit: "BrainStem — Smith Micro Software (Poser)" },
  { id: "cesiumman", name: "Walker", file: "/models/cesiumman.glb", credit: "Cesium Man — Cesium, CC BY 4.0" },
  { id: "truck", name: "Milk Truck", file: "/models/truck.glb", credit: "Cesium Milk Truck — Cesium, CC BY 4.0" },
  { id: "toycar", name: "Toy Car", file: "/models/toycar.glb", credit: "Toy Car — Guido Odendahl, CC0 1.0" },
  { id: "camera", name: "Camera", file: "/models/camera.glb", credit: "Antique Camera — UX3D, CC0 1.0" },
  { id: "fridge", name: "Fridge", file: "/models/fridge.glb", credit: "Commercial Refrigerator — Eric Chadwick / Wayfair, CC BY 4.0" },
  { id: "sheenchair", name: "Armchair", file: "/models/sheenchair.glb", credit: "Sheen Chair — Eric Chadwick / Wayfair, CC0 1.0" },
  { id: "pouf", name: "Pouf", file: "/models/pouf.glb", credit: "Specular Silk Pouf — Eric Chadwick / Wayfair, CC BY 4.0" },
  { id: "olives", name: "Olives", file: "/models/olives.glb", credit: "Iridescent Dish with Olives — Wayfair, CC BY 4.0" },
];
// The model list shows a window of this many names. ▲ ▼ move it a page; the mouse wheel
// moves it one line at a time, with no easing, like scrolling a terminal.
const MODELS_VISIBLE = 8;
const PRESETS: [string, string][] = [[".:-=+*#%@", " .:-=+*#%@"], [".-+*#", " .-+*#"]];
// The reference's colours and defaults; colours are always inverted.
const DEFAULTS = { model: "suzanne", characters: " .:-=+*#%@", resolution: 0.22, scale: 1 };
const INVERT = true;
// SHA-256 of the entry password, so the password itself is not in the source.
// This only gates the page in the browser; it is not access control.
const PASSWORD_SHA256 = "04b2ad6979a436129b5f30cd00a3804bd87fc8bef73b0bb38640bf49a8479c64";

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
const FG = "#ffffff", BG = "#007BE5";

const html = String.raw;

export async function showEntry(onEnter: () => void): Promise<void> {
  if (document.body.classList.contains("entering")) return;
  document.body.classList.add("entering");
  const root = document.createElement("div");
  root.className = "entry";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", "Wide-Learning 시작 화면");
  root.innerHTML = html`
    <div class="term entry-term">
      <header class="term-bar"><span class="term-title"><b>wl@laplace</b> — ascii<i class="cursor" aria-hidden="true"></i></span></header>
      <div class="entry-stage" style="--entry-fg:${FG};--entry-bg:${BG}">
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
            <label><span class="entry-mask"></span><i class="cursor" aria-hidden="true"></i>
              <input class="entry-secret" type="password" autocomplete="off" spellcheck="false" aria-label="비밀번호"></label>
          </form>
        </div>
      </div>
    </div>
    <dialog class="entry-credits"><h2>Credits</h2>${MODELS.map((model) => `<p>${model.credit}</p>`).join("")}
      <p>Models from the Khronos glTF Sample Assets (textures removed).</p><form method="dialog"><button class="entry-btn">닫기</button></form></dialog>`;
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

  const enter = () => {
    cleanup();
    window.removeEventListener("keydown", onKey, true);
    root.classList.add("leaving");
    document.body.classList.remove("entering");
    setTimeout(() => root.remove(), 260);
    onEnter();
  };
  // Enter asks for the password in a small TUI box. Right: into the terminal.
  // Wrong (or Esc, or a click outside): the box just closes, nothing else is said.
  const prompt = $<HTMLDivElement>(".entry-prompt");
  const secret = $<HTMLInputElement>(".entry-secret");
  const mask = $<HTMLSpanElement>(".entry-mask");
  const closePrompt = () => {
    prompt.hidden = true;
    secret.value = "";
    mask.textContent = "";
    $<HTMLButtonElement>(".entry-start").focus({ preventScroll: true });
  };
  const openPrompt = () => {
    prompt.hidden = false;
    secret.value = "";
    mask.textContent = "";
    secret.focus({ preventScroll: true });
  };
  secret.addEventListener("input", () => { mask.textContent = "*".repeat(secret.value.length); });
  secret.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); closePrompt(); } });
  secret.addEventListener("blur", () => { if (!prompt.hidden) requestAnimationFrame(() => secret.focus({ preventScroll: true })); });
  prompt.addEventListener("mousedown", (event) => { if (event.target === prompt) closePrompt(); else event.preventDefault(); });
  $<HTMLFormElement>(".entry-prompt-box").addEventListener("submit", (event) => {
    event.preventDefault();
    const typed = secret.value;
    void sha256(typed).then((hash) => (hash === PASSWORD_SHA256 ? enter() : closePrompt())).catch(closePrompt);
  });
  // While the entry is open, the terminal's own shortcuts wait.
  const onKey = (event: KeyboardEvent) => {
    if ((event.target as HTMLElement).closest("input, dialog")) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openPrompt();
    }
    event.stopPropagation();
  };
  window.addEventListener("keydown", onKey, true);
  $<HTMLButtonElement>("[data-action=enter]").addEventListener("click", openPrompt);
  $<HTMLButtonElement>("[data-action=credits]").addEventListener("click", () => $<HTMLDialogElement>(".entry-credits").showModal());

  // One retry covers a module or model request that failed on the way (e.g. while the
  // dev server re-bundled); after that, say why and keep the entry usable without 3D.
  const say = (text: string) => { status.textContent = text; };
  for (let attempt = 1; ; attempt++) {
    try {
      $<HTMLDivElement>(".entry-canvas").replaceChildren();
      cleanup = await startScene($<HTMLDivElement>(".entry-canvas"), state, say, root);
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

async function startScene(host: HTMLDivElement, state: typeof DEFAULTS, say: (text: string) => void, root: HTMLElement) {
  const THREE = await import("three");
  const [{ AsciiEffect }, { OrbitControls }, { GLTFLoader }, { RoomEnvironment }] = await Promise.all([
    import("three/addons/effects/AsciiEffect.js"),
    import("three/addons/controls/OrbitControls.js"),
    import("three/addons/loaders/GLTFLoader.js"),
    import("three/addons/environments/RoomEnvironment.js"),
  ]);

  const renderer = new THREE.WebGLRenderer({ preserveDrawingBuffer: true });
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
  const cache = new Map<string, { object: import("three").Object3D; clips: import("three").AnimationClip[] }>();
  let mixer: import("three").AnimationMixer | null = null;
  let current: import("three").Object3D | null = null;
  let effect: InstanceType<typeof AsciiEffect> | null = null;
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
      const gltf = await loader.loadAsync(model.file);
      entry = { object: fit(gltf.scene), clips: gltf.animations };
      cache.set(model.id, entry);
    }
    if (current) holder.remove(current);
    current = entry.object;
    holder.add(current);
    mixer = null;
    if (entry.clips.length) {  // the fox walks
      mixer = new THREE.AnimationMixer(current);
      const clip = entry.clips.find((item) => /walk/i.test(item.name)) ?? entry.clips[0];
      mixer.clipAction(clip).play();
    }
    say("");
  }

  // AsciiEffect takes its options at construction, so a setting change rebuilds it
  // (the reference re-mounts its AsciiRenderer the same way).
  function rebuild() {
    const target = controls?.target.clone();
    controls?.dispose();
    effect?.domElement.remove();
    effect = new AsciiEffect(renderer, state.characters, { invert: INVERT, resolution: state.resolution });
    effect.domElement.style.color = FG;
    effect.domElement.style.backgroundColor = BG;
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
    controls?.dispose();
    effect?.domElement.remove();
    scene.environment?.dispose();
    pmrem.dispose();
    scene.traverse((object) => {
      const mesh = object as import("three").Mesh;
      mesh.geometry?.dispose();
      const materials = Array.isArray(mesh.material) ? mesh.material : mesh.material ? [mesh.material] : [];
      materials.forEach((material) => material.dispose());
    });
    renderer.dispose();
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
