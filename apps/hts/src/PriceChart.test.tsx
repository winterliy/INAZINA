import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { DEFAULT_INDICATORS, INDICATOR_CATALOG, PriceChart, type Candle, type IndicatorData } from "./PriceChart";

// The chart renders in a browser; these stand-ins are all it touches while rendering.
(globalThis as { ResizeObserver?: unknown }).ResizeObserver = class { observe() {} disconnect() {} };

const candles: Candle[] = Array.from({ length: 60 }, (_, index) => {
  const open = 100 + Math.sin(index / 5) * 10, close = open + Math.cos(index) * 2;
  return { time: 1_700_000_000 + index * 600, open, high: Math.max(open, close) + 1, low: Math.min(open, close) - 1, close, ticks: 3, volume: index % 7 };
});
const points = candles.map((item) => ({ time: item.time, price: item.close }));
const everything = { upper: INDICATOR_CATALOG.upper.map((item) => item.key), lower: INDICATOR_CATALOG.lower.map((item) => item.key) };
const series = (label: string, kind: "line" | "bar" | "marker" | "profile" = "line") => ({ label, kind, levels: [0], color: "#888888", values: candles.map((item, index) => index < 5 ? null : item.close) });
const data: IndicatorData = {
  upper: Object.fromEntries(everything.upper.map((key) => [key, [series(key, key === "volume_profile" ? "profile" : key === "sar" ? "marker" : "line")]])),
  lower: Object.fromEntries(everything.lower.filter((key) => key !== "volume").map((key) => [key, [series(key), series(`${key}-hist`, "bar")]])),
};

function render(choice = DEFAULT_INDICATORS, given: IndicatorData = data, mode: "candle" | "line" = "candle", rows = candles) {
  return renderToString(createElement(PriceChart, {
    points, candles: rows, candleInterval: 600, mode, fallbackPrice: 1, drawings: [], onDrawingsChange() {}, tool: "select", onToolChange() {},
    resetKey: "test", controls: null, label: "테스트", indicators: choice, data: given, catalog: INDICATOR_CATALOG, onIndicatorsChange() {},
  }));
}

test("the chart renders with the default and with every Discord /그래프 indicator", () => {
  for (const mode of ["candle", "line"] as const) {
    assert.match(render(DEFAULT_INDICATORS, data, mode), /<svg/);
    assert.match(render(everything, data, mode), /<svg/);
  }
});

test("chosen indicators without server data, and an empty market, still render", () => {
  assert.match(render(everything, { upper: {}, lower: {} }), /지표 데이터를 받는 중/);
  assert.match(render(everything, data, "candle", []), /<svg/);
});
