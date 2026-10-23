import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Tabs, date, type Ctx } from "./common";

type LoginEvent = { eventId: number; userId: string; nickname: string | null; loginId: string; success: boolean; reason: string | null;
  ip: string; location: string; app: string | null; device: string | null; userAgent: string | null; time: number };
type Filter = "all" | "failed";

const REASON: Record<string, string> = { invalid_credentials: "비밀번호 불일치", account_not_found: "계정 없음" };

/** Every login to an administrator account (success and failure): time, IP, location, app and device. */
export function LoginHistoryCard({ ctx }: { ctx: Ctx }) {
  const { say, query } = ctx;
  const [events, setEvents] = useState<LoginEvent[]>([]);
  const [retention, setRetention] = useState(365);
  const [filter, setFilter] = useState<Filter>("all");

  const load = useCallback(async () => {
    const result = await api<{ events: LoginEvent[]; retentionDays: number }>("/api/admin/login-history");
    setEvents(result.events);
    setRetention(result.retentionDays);
  }, []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "로그인 기록을 불러오지 못했습니다.", true)); }, [load]);

  const term = query.trim().toLocaleLowerCase();
  const failed = events.filter((item) => !item.success).length;
  const visible = events.filter((item) => (filter === "all" || !item.success)
    && (!term || `${item.userId} ${item.nickname ?? ""} ${item.loginId} ${item.ip} ${item.location} ${item.device ?? ""}`.toLocaleLowerCase().includes(term)));

  return <Card title="관리자 로그인 기록" caption={`관리자 계정으로 들어온 로그인 시도(성공·실패)를 최근 200건까지 보여 줍니다. 기록은 ${retention}일 뒤 자동 삭제됩니다.`}
    actions={<button className="soft small" type="button" onClick={() => void load()}>새로고침</button>}>
    <Tabs label="로그인 기록 필터" value={filter} onChange={setFilter} options={[["all", "전체", events.length], ["failed", "실패만", failed]]} />
    <div className="scroll-x tall"><table><thead><tr><th>시각</th><th>관리자</th><th>결과</th><th>IP</th><th>위치</th><th>앱</th><th>기기</th></tr></thead><tbody>
      {visible.map((item) => <tr key={item.eventId}>
        <td>{date(item.time)}</td>
        <td>{item.nickname ?? item.userId}<small className="mono"> {item.loginId !== item.userId ? `${item.loginId} · ` : ""}{item.userId}</small></td>
        <td>{item.success ? <span className="tag ok">성공</span> : <span className="tag danger">실패 · {REASON[item.reason ?? ""] ?? item.reason ?? "실패"}</span>}</td>
        <td className="mono">{item.ip}</td>
        <td>{item.location}</td>
        <td>{item.app ?? "—"}</td>
        <td title={item.userAgent ?? undefined}>{item.device ?? "—"}</td>
      </tr>)}
    </tbody></table>{!visible.length && <p className="empty">{events.length ? "조건에 맞는 기록이 없습니다." : "아직 관리자 로그인 기록이 없습니다. 기능이 추가된 뒤의 로그인부터 기록됩니다."}</p>}</div>
    <p className="hint">위치(나라 · 지역 · 도시)는 IP 주소로 추정한 값입니다. IP Geolocation by <a href="https://db-ip.com" target="_blank" rel="noreferrer">DB-IP</a> (CC BY 4.0) · 매달 자동 갱신</p>
  </Card>;
}
