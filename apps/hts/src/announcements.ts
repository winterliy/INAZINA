/** An announced change to the terms or the privacy policy, as 나루 gets it (GET /api/me/announcements). */
export type Announcement = {
  id: number;
  document: string;
  title: string;
  /** announced (not yet in force), applied (in force since the effective date), cancelled. */
  stage: "announced" | "applied" | "cancelled" | string;
  status: string;
  effectiveEpoch: number;
  announcedEpoch: number;
  unfavourable: boolean;
  changes: { section: string; before: string[]; after: string[] }[];
  text: { title: string; body: string };
  campaignId: number | null;
  /** For an unfavourable change: whether this member has already agreed (null when not asked). */
  consented: boolean | null;
};

const LABEL: Record<string, string> = { announced: "시행 예정", applied: "시행됨", cancelled: "취소됨" };
export const stageLabel = (stage: string): string => LABEL[stage] ?? stage;

/** The button "지금 동의하기" belongs to an announced unfavourable change this member has not agreed to yet. */
export const needsConsentNow = (item: Announcement): boolean =>
  item.unfavourable && item.stage === "announced" && item.campaignId !== null && item.consented === false;

export function consentGuidance(item: Announcement): string {
  if (item.stage === "cancelled") return "";
  return item.unfavourable
    ? "이용자에게 불리하거나 중요한 변경입니다. Discord DM으로 보내 드린 개정안의 '동의합니다' 버튼(또는 아래 '지금 동의하기')을 눌러야 시행일 이후에도 서비스를 계속 이용할 수 있습니다."
    : "일반 변경입니다. 시행일 이후에도 계속 이용하면 동의한 것으로 봅니다.";
}
