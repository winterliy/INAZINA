import test from "node:test";
import assert from "node:assert/strict";
import { allocationSegments, performanceRankings } from "./PortfolioPage";

test("allocation segments sum to 100% for valued holdings", () => {
  const segments = allocationSegments([{ code: "A", name: "A", qty: 1, value: "25" }, { code: "B", name: "B", qty: 1, value: "75" }]);
  assert.equal(segments.length, 2);
  assert.equal(Math.round(segments.reduce((sum, item) => sum + item.percent, 0)), 100);
});

test("performance rankings ignore holdings without a market change and sort descending", () => {
  const holdings = [{ code: "A", name: "A", qty: 1, value: "10" }, { code: "B", name: "B", qty: 1, value: "20" }, { code: "C", name: "C", qty: 1, value: "30" }];
  const rankings = performanceRankings(holdings, [{ code: "A", change24h: -2 }, { code: "B", change24h: 4 }, { code: "C" }]);
  assert.deepEqual(rankings.map((item) => item.code), ["B", "A"]);
});
