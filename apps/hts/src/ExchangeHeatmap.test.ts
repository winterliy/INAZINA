import assert from "node:assert/strict";
import { test } from "node:test";
import { exchangeStocks, summarizeExchanges } from "./ExchangeHeatmap.tsx";

test("summarizes tradeable instruments by exchange and ignores missing changes", () => {
  assert.deepEqual(summarizeExchanges([
    { exchange_id: "INZ", change24h: 4 },
    { exchange_id: "INZ", change24h: -2 },
    { exchange_id: "BOND", change24h: null },
    { exchange_id: "CURRENCY", change24h: 20 },
    { exchange_id: null, change24h: 10 },
  ]), [
    { exchangeId: "INZ", count: 2, averageChange: 1 },
    { exchangeId: "BOND", count: 1, averageChange: 0 },
  ]);
});

test("selects only stocks from the requested exchange", () => {
  assert.deepEqual(exchangeStocks([
    { code: "A", name: "A", type: "stock", exchange_id: "NEX", volume24h: 10, change24h: 1 },
    { code: "B", name: "B", type: "bond", exchange_id: "NEX", volume24h: 20, change24h: 2 },
    { code: "C", name: "C", type: "stock", exchange_id: "OTHER", volume24h: 30, change24h: 3 },
  ], "NEX").map(({ code }) => code), ["A"]);
});
