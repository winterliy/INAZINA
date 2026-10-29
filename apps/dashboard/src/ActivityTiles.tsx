import { date } from "./common";

const DAY = 86400;
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

/** 작업 기록: the last seven days by hour, darker where more admin work happened. */
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
  return <section className="tile heatmap-tile" aria-label="최근 7일 시간대별 관리자 작업">
    <p className="tile-label">작업 히트맵 · 최근 7일 (불러온 {entries.length}건 기준)</p>
    <div className="heatmap">
      {counts.map((hours, day) => <div key={day} className="heatmap-row">
        <span>{WEEKDAYS[new Date((first + day * DAY) * 1000).getDay()]}</span>
        {hours.map((count, hour) => <i key={hour} title={`${hour}시 · ${count}건`} style={{ opacity: count ? 0.25 + 0.75 * count / max : 1 }} className={count ? "on" : undefined} />)}
      </div>)}
      <div className="heatmap-row heatmap-hours"><span />{[0, 6, 12, 18].map((hour) => <small key={hour} style={{ gridColumn: `${hour + 2} / span 6` }}>{hour}시</small>)}</div>
    </div>
  </section>;
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
