import { consentGuidance, stageLabel, type Announcement } from "./announcements";

const day = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "long", timeZone: "Asia/Seoul" }).format(epoch * 1000);
const lines = (items: string[]) => items.length ? <ul>{items.map((line, index) => <li key={index}>{line}</li>)}</ul> : <p className="announce-none">(없음)</p>;

/** One announced change: its state and dates, whether agreement is needed, and the before/after comparison of the changed clauses. */
export function AnnouncementCard({ item, children }: { item: Announcement; children?: React.ReactNode }) {
  const guidance = consentGuidance(item);
  return <article className="announce-card">
    <header>
      <span className={`announce-badge ${item.stage}`}>{stageLabel(item.stage)}</span>
      <h3>{item.title} 개정 안내</h3>
      <p className="announce-meta">공지 {day(item.announcedEpoch)} · 시행 {day(item.effectiveEpoch)}</p>
    </header>
    {guidance && <p className={`announce-guidance${item.unfavourable ? " must" : ""}`}>{guidance}</p>}
    {item.stage === "cancelled" ? <p>예고했던 개정이 취소되어 지금 적용 중인 문서가 계속 적용됩니다.</p>
      : item.changes.length ? item.changes.map((change) => <section key={change.section} className="announce-change">
        <h4>{change.section}</h4>
        <div className="announce-compare">
          <div><b>변경 전</b>{lines(change.before)}</div>
          <div><b>변경 후</b>{lines(change.after)}</div>
        </div>
      </section>) : <p className="announce-none">문구는 바뀌지 않았고 날짜와 개정 안내만 바뀌었습니다.</p>}
    {children}
  </article>;
}
