// Top bar structure: four groups over the page ids App.tsx routes. A page id missing from the
// page list (not built yet) is skipped, so the groups stay valid while pages come and go.
export type NavGroup = { id: string; label: string; pages: string[]; external?: { label: string; hash: string } };

export const NAV_GROUPS: NavGroup[] = [
  { id: "ops", label: "운영", pages: ["overview", "audit", "documents", "commands", "database"] },
  { id: "market", label: "시장 · 회계", pages: ["instruments", "finance", "exchanges", "events", "liquidity", "accounting", "close", "credit"] },
  { id: "corp", label: "기업", pages: ["companies", "organizations"], external: { label: "조직도", hash: "#ownership" } },
  { id: "users", label: "사용자", pages: ["users", "naturalization", "court", "feedback", "board"] },
];

/** Addresses that moved: the old hash keeps opening the right page. */
export const LEGACY_PAGE: Record<string, string> = { compliance: "credit" };

export const groupOf = (page: string): NavGroup | undefined => NAV_GROUPS.find((group) => group.pages.includes(page));
