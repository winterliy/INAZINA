import assert from "node:assert/strict";
import { test } from "node:test";
import { axisWidth, panelHeight, priceTicks, timeTicks } from "./chartScale";

const kst = (text: string) => Date.parse(`${text}+09:00`) / 1000;

test("price ticks are round steps that fit the height", () => {
  assert.deepEqual(priceTicks(9_430, 10_270, 6), { step: 200, ticks: [9_600, 9_800, 10_000, 10_200], decimals: 0 });
  const tall = priceTicks(9_430, 10_270, 12);
  assert.equal(tall.step, 100);
  assert.equal(tall.ticks.length, 8);
  const small = priceTicks(1.02, 1.18, 4);
  assert.equal(small.step, 0.05);
  assert.equal(small.decimals, 2);
  assert.deepEqual(small.ticks, [1.05, 1.1, 1.15]);
  assert.equal(priceTicks(0, 25, 10).step, 2.5);
});

test("time ticks follow the width and land on KST clock and calendar boundaries", () => {
  const hour = timeTicks(kst("2026-11-02T09:07:00"), kst("2026-11-02T10:07:00"), 6);
  assert.equal(hour.step, 600);
  assert.deepEqual(hour.ticks.map((tick) => tick.label), ["11/2 09:10", "09:20", "09:30", "09:40", "09:50", "10:00"]);
  const week = timeTicks(kst("2026-11-02T13:00:00"), kst("2026-11-09T13:00:00"), 8);
  assert.equal(week.step, 86_400);
  assert.equal(week.ticks[0].label, "11/3");
  assert.equal(week.ticks[0].time, kst("2026-11-03T00:00:00"));
  const year = timeTicks(kst("2026-01-15T00:00:00"), kst("2026-12-20T00:00:00"), 6);
  assert.deepEqual(year.ticks.map((tick) => tick.label), ["2026.3", "5월", "7월", "9월", "11월"]);
});

test("a narrower chart shows fewer time labels", () => {
  const start = kst("2026-11-02T09:00:00"), end = kst("2026-11-02T15:30:00");
  assert.ok(timeTicks(start, end, 3).ticks.length < timeTicks(start, end, 10).ticks.length);
});

test("the axis and lower panels size themselves", () => {
  assert.equal(axisWidth(["9,800"]), 60);
  assert.ok(axisWidth(["123,456,789"]) > axisWidth(["9,800"]));
  assert.equal(panelHeight(600, 0), 0);
  assert.ok(panelHeight(900, 1) > panelHeight(400, 1));
  assert.ok(panelHeight(500, 4) * 4 <= 250);
});
