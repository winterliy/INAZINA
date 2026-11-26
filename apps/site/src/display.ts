import { mountLoginScene } from "../../shared/loginScene3d";

// The sign-in screen's motion picture on a page of its own: it fills the window, the pointer's lens is off, and F (or the button) goes full screen.
const scene = document.getElementById("scene") as HTMLElement;
const controls = document.getElementById("controls") as HTMLElement;
mountLoginScene(scene, undefined, { lens: false });

const toggle = () => {
  try {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void document.documentElement.requestFullscreen();
  } catch { /* a browser without the Fullscreen API: the page still fills the window */ }
};
document.getElementById("full")?.addEventListener("click", toggle);
window.addEventListener("keydown", (event) => { if (event.key === "f" || event.key === "F") toggle(); });

let hide = 0;
const wake = () => { controls.classList.add("show"); window.clearTimeout(hide); hide = window.setTimeout(() => controls.classList.remove("show"), 2500); };
window.addEventListener("pointermove", wake);
wake();
