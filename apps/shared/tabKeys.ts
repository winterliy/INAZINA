import type { KeyboardEvent } from "react";

/** Arrow/Home/End move between the role="tab" buttons of a tablist and select the focused one. */
export function tabListKeys(event: KeyboardEvent<HTMLElement>): void {
  if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]:not([disabled])'));
  if (!tabs.length) return;
  const at = tabs.indexOf(document.activeElement as HTMLElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
    : (Math.max(at, 0) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  tabs[next].focus();
  tabs[next].click();
}

/** ids shared by a tab group: tab buttons point at one panel, and the panel is labelled by the selected tab. */
export const tabId = (group: string, value: string) => `${group}-tab-${value}`;
export const tabPanel = (group: string, value: string) => ({ role: "tabpanel", id: `${group}-panel`, "aria-labelledby": tabId(group, value) }) as const;
export const tabButton = (group: string, value: string, selected: boolean) => ({
  role: "tab", id: tabId(group, value), "aria-selected": selected, "aria-controls": `${group}-panel`, tabIndex: selected ? 0 : -1,
}) as const;
