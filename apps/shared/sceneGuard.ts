// Who gets the motion picture: shared by the sign-in screen and the /display/ page of the public site.
/** `?scene=3d` forces the motion on (testing): no hardware checks and no slow-run fallback. */
export const forcedMotion = () => { try { return new URLSearchParams(location.search).get("scene") === "3d"; } catch { return false; } };
export const STILL_KEY = "inazina.scene";
/** The motion picture is for capable desktops only; phones, weak machines and software rendering get the still logo.
 *  A machine that turned out too slow is remembered (see loginScene3d's onSlow). */
export function stillLogoOnly(): boolean {
  if (forcedMotion()) return false;
  try {
    if (localStorage.getItem(STILL_KEY) === "still") return true;
  } catch { /* storage blocked: decide from the hardware alone */ }
  const nav = navigator as Navigator & { deviceMemory?: number };
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return true;
  if (window.matchMedia?.("(pointer: coarse)").matches || window.innerWidth < 900 || /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent)) return true;
  if ((nav.hardwareConcurrency ?? 8) <= 2 || (nav.deviceMemory ?? 8) <= 2) return true;
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    const info = gl?.getExtension("WEBGL_debug_renderer_info");
    const renderer = gl && info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : "";
    if (!gl || /swiftshader|llvmpipe|software|basic render/i.test(renderer)) return true;
  } catch { return true; }
  return false;
}
