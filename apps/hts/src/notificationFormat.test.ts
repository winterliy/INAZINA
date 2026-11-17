import test from "node:test";
import assert from "node:assert/strict";
import { ago, daysLeft, formatAmount } from "./notificationFormat";

test("notification ages read naturally", () => {
  const now = 1_800_000_000;
  assert.equal(ago(now - 20, now), "방금");
  assert.equal(ago(now - 12 * 60, now), "12분 전");
  assert.equal(ago(now - 3 * 3600, now), "3시간 전");
  assert.equal(ago(now - 30 * 3600, now), "어제");
  assert.match(ago(now - 5 * 86400, now), /\d{4}/);
});

test("payout amounts keep their decimals and gain thousands separators", () => {
  assert.equal(formatAmount("1250000"), "1,250,000");
  assert.equal(formatAmount("833.33"), "833.33");
  assert.equal(formatAmount("37500"), "37,500");
});

test("days left never go negative and round up", () => {
  const now = 1_800_000_000;
  assert.equal(daysLeft(now + 29.2 * 86400, now), 30);
  assert.equal(daysLeft(now - 100, now), 0);
});
