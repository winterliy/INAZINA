import { useState } from "react";
import { date } from "./common";
import "./heatmap-iso.css";

const DAY = 86400;
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

/** 작업 기록: the last seven days by hour, darker where more admin work happened. One click lifts every cell into a column
 *  as tall as its activity (an isometric landscape); a second click lays them back down. */
export function AuditHeatmap({ entries }: { entries: { time: number }[] }) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const first = today.getTime() / 1000 - 6 * DAY;
  const counts = Array.from({ length: 7 }, () => Array<number>(24).fill(0));
  for (const entry of entries) {
    const day = Math.floor((entry.time - first) / DAY);
    if (day >= 0 && day < 7) counts[day][new Date(entry.time * 1000).getHours()] += 1;
  }
  const max = Math.max(1, ...counts.flat());
  const [iso, setIso] = useState(false);
  return <section className="tile heatmap-tile" aria-label="최근 7일 시간대별 관리자 작업">
    <div className="heatmap-head">
      <p className="tile-label">작업 히트맵 · 최근 7일 (불러온 {entries.length}건 기준)</p>
      <button type="button" className="outline heatmap-toggle" aria-pressed={iso} onClick={() => setIso((value) => !value)}>{iso ? "2D로 보기" : "3D로 보기"}</button>
    </div>
    {iso
      ? <IsoHeatmap counts={counts} max={max} first={first} />
      : <div className="heatmap-stage"><div className="heatmap">
        {counts.map((hours, day) => <div key={day} className="heatmap-row">
          <span>{WEEKDAYS[new Date((first + day * DAY) * 1000).getDay()]}</span>
          {hours.map((count, hour) => <i key={hour} title={`${hour}시 · ${count}건`} style={{ opacity: count ? 0.25 + 0.75 * count / max : 1 }} className={count ? "on" : undefined} />)}
        </div>)}
        <div className="heatmap-row heatmap-hours"><span />{[0, 6, 12, 18].map((hour) => <small key={hour} style={{ gridColumn: `${hour + 2} / span 6` }}>{hour}시</small>)}</div>
      </div></div>}
  </section>;
}

// Isometric columns drawn as flat SVG polygons, back to front, so every browser paints them the same
// (CSS 3D transforms render differently in Safari). Days run to the lower left, hours to the lower right.
const TW = 15, TH = 8.5, TALL = 64;
const px = (hour: number, day: number) => (hour - day) * TW;
const py = (hour: number, day: number) => (hour + day) * TH;

/** An opaque face: the chip colour, the accent over it, then a dark wash for the side it turns away from the light. */
function Face({ points, a, shade }: { points: string; a: number; shade: number }) {
  return <>
    <polygon className="iso-top" points={points} />
    <polygon className="iso-top-on" points={points} fillOpacity={a} />
    <polygon className="iso-shade" points={points} fillOpacity={shade} />
  </>;
}

function IsoHeatmap({ counts, max, first }: { counts: number[][]; max: number; first: number }) {
  const cells: { day: number; hour: number; count: number }[] = [];
  for (let sum = 0; sum <= 23 + 6; sum++) for (let day = 0; day < 7; day++) { const hour = sum - day; if (hour >= 0 && hour < 24) cells.push({ day, hour, count: counts[day][hour] }); }
  const left = px(-1, 6) - TW * 2, right = px(23, 0) + TW + 6, top = -TALL - 6, bottom = py(23, 8) + TH * 2 + 14;
  return <div className="heatmap-iso">
    <svg viewBox={`${left} ${top} ${right - left} ${bottom - top}`} role="img" aria-label="시간대별 관리자 작업 3D 보기">
      {/* drawn first, so the columns stand in front of the labels */}
      {counts.map((_, day) => <text key={day} className="iso-label" x={px(-1, day) - 4} y={py(-1, day) + TH + 4} textAnchor="end">{WEEKDAYS[new Date((first + day * DAY) * 1000).getDay()]}</text>)}
      {[0, 6, 12, 18].map((hour) => <text key={hour} className="iso-label" x={px(hour, 8) + TW} y={py(hour, 8) + TH}>{hour}시</text>)}
      {cells.map(({ day, hour, count }) => {
        const x = px(hour, day), y = py(hour, day), rise = count ? Math.max(6, Math.round(count / max * TALL)) : 0, a = count ? 0.3 + 0.7 * count / max : 0;
        const topFace = `${x},${y - rise} ${x + TW},${y + TH - rise} ${x},${y + TH * 2 - rise} ${x - TW},${y + TH - rise}`;
        return <g key={`${day}-${hour}`}>
          <title>{`${hour}시 · ${count}건`}</title>
          {rise > 0 && <>
            <Face points={`${x - TW},${y + TH - rise} ${x},${y + TH * 2 - rise} ${x},${y + TH * 2} ${x - TW},${y + TH}`} a={a} shade={0.22} />
            <Face points={`${x},${y + TH * 2 - rise} ${x + TW},${y + TH - rise} ${x + TW},${y + TH} ${x},${y + TH * 2}`} a={a} shade={0.4} />
          </>}
          <polygon className="iso-top" points={topFace} />
          {count > 0 && <polygon className="iso-top-on" points={topFace} fillOpacity={a} />}
        </g>;
      })}
    </svg>
  </div>;
}

type MarketEvent = { event_id: number; asset_code: string | null; event_type: string; starts_epoch: number; ends_epoch: number; active: boolean };

/** 시장 사건: counts and a small timeline of the latest events, with a line at now. */
export function EventTimeline({ events }: { events: MarketEvent[] }) {
  const recent = events.slice(0, 12);
  const now = Date.now() / 1000;
  const start = Math.min(now, ...recent.map((item) => item.starts_epoch));
  const end = Math.max(now, ...recent.map((item) => item.ends_epoch));
  const at = (epoch: number) => (epoch - start) / Math.max(1, end - start) * 100;
  return <div className="tile-row event-tiles">
    <section className="tile tile-dark"><p className="tile-label">진행 중 사건</p><p className="tile-value">{events.filter((item) => item.active).length}</p></section>
    <section className="tile"><p className="tile-label">호재</p><p className="tile-value up">{events.filter((item) => item.event_type === "good").length}</p></section>
    <section className="tile"><p className="tile-label">악재</p><p className="tile-value down">{events.filter((item) => item.event_type !== "good").length}</p></section>
    {recent.length > 0 && <section className="tile timeline-tile" aria-label="최근 사건 타임라인">
      <p className="tile-label">최근 사건 타임라인 · {date(start)} ~ {date(end)}</p>
      <div className="timeline">
        {recent.map((item) => <div key={item.event_id} className="timeline-row" title={`#${item.event_id} ${item.asset_code || "시장 전체"} · ${date(item.starts_epoch)} ~ ${date(item.ends_epoch)}`}>
          <span>#{item.event_id} {item.asset_code || "시장 전체"}</span>
          <div className="timeline-track"><i className={item.event_type === "good" ? "up" : "down"} style={{ left: `${at(item.starts_epoch)}%`, width: `${Math.max(0.5, at(item.ends_epoch) - at(item.starts_epoch))}%` }} /></div>
        </div>)}
        <b className="timeline-now" style={{ left: `calc(108px + (100% - 108px) * ${at(now) / 100})` }} aria-label="지금" />
      </div>
    </section>}
  </div>;
}
