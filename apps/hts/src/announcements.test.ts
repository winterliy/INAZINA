import assert from "node:assert/strict";
import test from "node:test";
import { consentGuidance, needsConsentNow, stageLabel, type Announcement } from "./announcements.ts";

const item: Announcement = {
  id: 1, document: "terms", title: "서비스 이용약관", stage: "announced", status: "open", effectiveEpoch: 1797600000, announcedEpoch: 1795000000,
  unfavourable: true, changes: [], text: { title: "t", body: "b" }, campaignId: 7, consented: false,
};

test("the consent button is offered only while an unfavourable change is announced and this member has not agreed", () => {
  assert.equal(needsConsentNow(item), true);
  assert.equal(needsConsentNow({ ...item, consented: true }), false, "already agreed");
  assert.equal(needsConsentNow({ ...item, unfavourable: false, campaignId: null, consented: null }), false, "a plain change asks for nothing");
  assert.equal(needsConsentNow({ ...item, stage: "applied" }), false, "once in force the whole screen is held instead");
  assert.equal(needsConsentNow({ ...item, campaignId: null }), false);
});

test("the labels name the state in Korean", () => {
  assert.equal(stageLabel("announced"), "시행 예정");
  assert.equal(stageLabel("applied"), "시행됨");
  assert.equal(stageLabel("cancelled"), "취소됨");
  assert.equal(stageLabel("odd"), "odd");
});

test("only an unfavourable change reminds that agreeing by DM is needed to keep using the service", () => {
  assert.match(consentGuidance(item), /동의/);
  assert.match(consentGuidance(item), /DM/);
  assert.match(consentGuidance({ ...item, unfavourable: false }), /계속 이용하면 동의한 것으로 봅니다/);
  assert.equal(consentGuidance({ ...item, stage: "cancelled" }), "");
});
