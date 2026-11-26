import assert from "node:assert/strict";
import test from "node:test";
import { decisionSummary, type CaseFiling } from "./courtCases.ts";

const base: CaseFiling = { filingId: 7, kind: "bankruptcy", label: "파산", reason: "갚을 수 없습니다.", plan: "", status: "submitted", createdEpoch: 1_795_000_000, note: "", handledEpoch: null };

test("a case waiting or under review says where it stands and has no decision yet", () => {
  assert.deepEqual(decisionSummary(base), { heading: "진행 상황", text: "접수되어 심리를 기다리고 있습니다.", decided: false });
  assert.equal(decisionSummary({ ...base, status: "reviewing" }).text, "심리가 진행 중입니다.");
  assert.equal(decisionSummary({ ...base, status: "reviewing", note: "서류를 더 확인하는 중입니다." }).text, "서류를 더 확인하는 중입니다.", "the court's note while reviewing is shown");
});

test("an approved case shows the approval and the court's note", () => {
  const approved = decisionSummary({ ...base, status: "approved", note: "신청 요건을 갖추어 인가합니다.", handledEpoch: 1_795_100_000 });
  assert.equal(approved.heading, "인가 결정");
  assert.equal(approved.text, "신청 요건을 갖추어 인가합니다.");
  assert.equal(approved.decided, true);
  assert.equal(decisionSummary({ ...base, status: "approved" }).text, "인가되었습니다. 별도의 안내 사유는 없습니다.");
});

test("a rejected case shows the reason it was rejected", () => {
  const rejected = decisionSummary({ ...base, status: "rejected", note: "변제 능력이 확인됩니다." });
  assert.equal(rejected.heading, "기각 사유");
  assert.equal(rejected.text, "변제 능력이 확인됩니다.");
  assert.equal(rejected.decided, true);
  assert.match(decisionSummary({ ...base, status: "rejected" }).text, /기각/);
});

test("an unknown status falls back to the raw word without inventing a decision", () => {
  assert.deepEqual(decisionSummary({ ...base, status: "odd" }), { heading: "진행 상황", text: "odd", decided: false });
});
