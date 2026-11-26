import sceneSvg from "../../shared/scene.svg?raw";
import { STILL_KEY, forcedMotion, stillLogoOnly } from "../../shared/sceneGuard";

// The sign-in screen's motion picture on a page of its own: it fills the window, the pointer's lens is off, and F (or the button) goes full screen.
// The sign-in screen's safeguard applies: a phone, weak machine, software rendering or reduced-motion setting gets the still logo and the 3D code
// is not even loaded; a machine that turns out too slow falls back to the logo and is remembered (the same key as the sign-in screen).
const scene = document.getElementById("scene") as HTMLElement;
const controls = document.getElementById("controls") as HTMLElement;

const still = () => {
  scene.querySelector("canvas")?.remove();
  scene.classList.add("is-still");
  scene.innerHTML = `<div class="still-logo">${sceneSvg}</div>`;
};
if (stillLogoOnly()) still();
else {
  let dispose: (() => void) | undefined;
  const settle = () => {
    dispose?.(); dispose = undefined; still();
    try { localStorage.setItem(STILL_KEY, "still"); } catch { /* not remembered */ }
  };
  void import("../../shared/loginScene3d").then((module) => {
    try { dispose = module.mountLoginScene(scene, forcedMotion() ? undefined : settle, { lens: false }); } catch { still(); }
  }).catch(still);
}

const toggle = () => {
  try {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    else void document.documentElement.requestFullscreen().catch(() => undefined);   // refused (no user gesture, embedded frame): the page still fills the window
  } catch { /* a browser without the Fullscreen API: the page still fills the window */ }
};
document.getElementById("full")?.addEventListener("click", toggle);
window.addEventListener("keydown", (event) => { if (event.key === "f" || event.key === "F") toggle(); });

let hide = 0;
const wake = () => { controls.classList.add("show"); window.clearTimeout(hide); hide = window.setTimeout(() => controls.classList.remove("show"), 2500); };
window.addEventListener("pointermove", wake);
wake();
