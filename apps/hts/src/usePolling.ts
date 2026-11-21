import { useEffect, useRef } from "react";

/**
 * Calls `fn` now and then every `ms` milliseconds while the tab is visible.
 * Pauses while document.hidden, runs again at once when the tab returns, and never starts a call while
 * the previous one is still running. Restarts when `ms`, `enabled` or `deps` change.
 */
export function usePolling(fn: () => Promise<unknown> | void, ms: number, enabled = true, deps: readonly unknown[] = []): void {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let inFlight = false;
    let stopped = false;
    let timer: number | undefined;
    const run = async () => {
      if (inFlight || stopped || document.hidden) return;
      inFlight = true;
      try { await latest.current(); } catch { /* the caller reports its own errors */ } finally { inFlight = false; }
    };
    const stop = () => { if (timer !== undefined) { window.clearInterval(timer); timer = undefined; } };
    const start = () => { stop(); timer = window.setInterval(() => void run(), ms); };
    const onVisibility = () => { if (document.hidden) stop(); else { void run(); start(); } };
    void run();
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stopped = true; stop(); document.removeEventListener("visibilitychange", onVisibility); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled, ...deps]);
}
