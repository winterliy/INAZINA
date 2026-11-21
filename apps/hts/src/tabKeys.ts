import type { KeyboardEvent } from "react";

/** Arrow/Home/End move between the role="tab" buttons of a tablist and select the focused one. */
export function tabListKeys(event: KeyboardEvent<HTMLElement>): void {
  const keys = ["ArrowRight", "ArrowLeft", "Home", "End"];
  if (!keys.includes(event.key)) return;
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]:not([disabled])'));
  if (!tabs.length) return;
  const at = tabs.indexOf(document.activeElement as HTMLElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
    : (Math.max(at, 0) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  tabs[next].focus();
  tabs[next].click();
}

/** Only http(s) URLs may become links; anything else (javascript:, data:, relative) is plain text. */
export const safeUrl = (value: string | null | undefined): string | null => /^https?:\/\//i.test((value ?? "").trim()) ? (value as string).trim() : null;
