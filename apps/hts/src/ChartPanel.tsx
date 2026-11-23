import { ErrorBoundary } from "./ErrorBoundary";
import { ExchangeHeatmap } from "./ExchangeHeatmap";
import { PriceChart, type Candle, type Drawing, type IndicatorCatalog, type IndicatorChoice, type IndicatorData, type Point, type Tool } from "./PriceChart";
import { intervals, rangeNames, ranges } from "./appShared";
import type { Instrument } from "./types";

type Props = {
  instruments: Instrument[];
  selected: Instrument | null;
  selectedCode: string;
  points: Point[];
  candles: Candle[];
  candleInterval: number;
  chartMode: "candle" | "line";
  onChartMode: (mode: "candle" | "line") => void;
  range: string;
  onRange: (range: string) => void;
  interval: number | null;
  onInterval: (seconds: number | null) => void;
  drawings: Drawing[];
  onDrawingsChange: (drawings: Drawing[]) => void;
  tool: Tool;
  onToolChange: (tool: Tool) => void;
  indicators: IndicatorChoice;
  indicatorData: IndicatorData;
  indicatorCatalog: IndicatorCatalog;
  onIndicatorsChange: (next: IndicatorChoice) => void;
  onRefresh: () => void;
  heatmapExchange: string | undefined;
  onExchangeChange: (exchangeId: string) => void;
  onChoose: (code: string) => void;
};

/** Price chart with its range, mode and interval controls, and the exchange heatmap below it. */
export function ChartPanel({ instruments, selected, selectedCode, points, candles, candleInterval, chartMode, onChartMode, range, onRange, interval, onInterval, drawings, onDrawingsChange, tool, onToolChange, indicators, indicatorData, indicatorCatalog, onIndicatorsChange, onRefresh, heatmapExchange, onExchangeChange, onChoose }: Props) {
  return <>
    <ErrorBoundary label="차트" resetKey={`${selectedCode}-${range}-${interval}-${chartMode}`}>
    <PriceChart points={points} candles={candles} candleInterval={candleInterval} mode={chartMode} fallbackPrice={Number(selected?.price ?? 1)}
      drawings={drawings} onDrawingsChange={onDrawingsChange} tool={tool} onToolChange={onToolChange} resetKey={`${selectedCode}-${range}-${interval}-${chartMode}`} label={selected?.name || "가격"}
      indicators={indicators} data={indicatorData} catalog={indicatorCatalog} onIndicatorsChange={onIndicatorsChange}
      controls={<>{ranges.map((item) => <button key={item} className={range === item ? "selected" : ""} onClick={() => onRange(item)}>{rangeNames[item]}</button>)}
        <span className="divider" aria-hidden="true" />
        <button className={chartMode === "candle" ? "selected" : ""} onClick={() => onChartMode("candle")}>캔들</button>
        <button className={chartMode === "line" ? "selected" : ""} onClick={() => onChartMode("line")}>선</button>
        {chartMode === "candle" && <select className="interval-select" aria-label="봉 주기" value={interval ?? ""} onChange={(event) => onInterval(event.target.value ? Number(event.target.value) : null)}><option value="">자동 ({intervals.find(([seconds]) => seconds === candleInterval)?.[1] ?? "봉"})</option>{intervals.map(([seconds, name]) => <option key={seconds} value={seconds}>{name}</option>)}</select>}<button className="chart-refresh" onClick={onRefresh}>새로고침</button></>} />
    </ErrorBoundary>
    <ExchangeHeatmap instruments={instruments} exchangeId={heatmapExchange} onExchangeChange={onExchangeChange} onSelect={onChoose} />
</>;
}
