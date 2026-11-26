/** A filing as 나루 gets it (GET /api/court/filings): the court's note is the reason for an approval or a rejection. */
export type CaseFiling = {
  filingId: number;
  kind: "rehabilitation" | "bankruptcy";
  label: string;
  reason: string;
  plan: string;
  status: string;
  createdEpoch: number;
  note: string;
  handledEpoch: number | null;
};

/** What the opened case says about its outcome: a heading, the text (the court's own note when there is one) and whether it is decided. */
export function decisionSummary(filing: CaseFiling): { heading: string; text: string; decided: boolean } {
  const note = filing.note.trim();
  switch (filing.status) {
    case "submitted": return { heading: "진행 상황", text: note || "접수되어 심리를 기다리고 있습니다.", decided: false };
    case "reviewing": return { heading: "진행 상황", text: note || "심리가 진행 중입니다.", decided: false };
    case "approved": return { heading: "인가 결정", text: note || "인가되었습니다. 별도의 안내 사유는 없습니다.", decided: true };
    case "rejected": return { heading: "기각 사유", text: note || "기각되었습니다. 별도의 사유는 기재되지 않았습니다.", decided: true };
    default: return { heading: "진행 상황", text: filing.status, decided: false };
  }
}
