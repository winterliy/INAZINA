/** In-app navigation without reloading: the HTS keeps its session, market data and wallet. */
export const NAVIGATE = "inazina:navigate";

export function navigate(path: string, replace = false) {
  if (replace) history.replaceState(null, "", path); else history.pushState(null, "", path);
  window.dispatchEvent(new Event(NAVIGATE));
  window.scrollTo({ top: 0 });
}

// Links inside the HTS move without a reload; other apps, the API, downloads and new tabs keep the browser default.
const IN_APP = /^\/(?!api\/)/;
export function interceptLinks(event: MouseEvent) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = (event.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
  if (!link || link.target || link.hasAttribute("download") || link.origin !== location.origin || !IN_APP.test(link.pathname)) return;
  event.preventDefault();
  if (link.pathname + link.search !== location.pathname + location.search) navigate(link.pathname + link.search + link.hash);
}
