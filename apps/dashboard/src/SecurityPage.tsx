import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, errorText, type Ctx } from "./common";
import type { LoginEvent } from "./LoginHistoryCard";
import "./security.css";

type Attempt = { at: number; service: string; ip: string; ok: boolean; loginId?: string; via?: "login" | "emergency" };
type Overview = { attempts: Attempt[]; last24h: { total: number; failed: number; emergency: number }; locked: { service: string; ip: string }[];
  policy: { lockAttempts: number; lockSeconds: number } };

const SERVICE_NAME: Record<string, string> = { api: "API 서버 (4312)", python: "데이터 서비스 (4313)" };

/**
 * 보안: who has tried the browser pages of the two private service ports (4312, 4313), the web logins that
 * failed, and the safeguards in force. Those pages sign in with an administrator's dashboard id and password.
 * (The wide-learning viewer on 4315 changes its own passwords in its settings.)
 */
export function SecurityPage({ ctx }: { ctx: Ctx }) {
  const { say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [failedLogins, setFailedLogins] = useState<LoginEvent[]>([]);

  const load = useCallback(async () => {
    setData(await api<Overview>("/api/admin/security"));
    api<{ events: LoginEvent[] }>("/api/admin/login-history").then((result) => setFailedLogins(result.events.filter((event) => !event.success).slice(0, 10))).catch(() => setFailedLogins([]));
  }, []);
  useEffect(() => { load().catch((cause) => say(errorText(cause, "보안 정보를 불러오지 못했습니다."), true)); }, [load, say]);

  if (!data) return <p className="cap">불러오는 중…</p>;
  return <>
    <section className="tile-row security-tiles">
      <section className="tile"><p className="tile-label">최근 24시간 접속 시도</p><p className="tile-value">{data.last24h.total}건</p><p className="cap">실패 {data.last24h.failed}건</p></section>
      <section className={data.locked.length ? "tile warn" : "tile"}><p className="tile-label">잠긴 주소</p><p className="tile-value">{data.locked.length}곳</p>
        <p className="cap">{data.policy.lockAttempts}번 틀리면 {data.policy.lockSeconds / 60}분 잠깁니다</p></section>
      <section className={data.last24h.emergency ? "tile warn" : "tile"}><p className="tile-label">비상 경로 사용</p><p className="tile-value">{data.last24h.emergency}회</p><p className="cap">최근 24시간 · 아이디 없이 연 기록</p></section>
    </section>

    <Card title="접속 시도 기록" caption="4312·4313의 브라우저 페이지에 로그인한 기록입니다. 아이디, 주소, 결과만 남기고 비밀번호는 저장하지 않습니다. 최근 100건.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>서비스</th><th>아이디</th><th>주소</th><th>방식</th><th>결과</th></tr></thead><tbody>
        {data.attempts.map((row, index) => <tr key={`${row.at}-${index}`}><td>{date(row.at)}</td><td>{SERVICE_NAME[row.service] ?? row.service}</td><td className="mono">{row.loginId || "—"}</td><td className="mono">{row.ip}</td>
          <td>{row.via === "emergency" ? "비상 경로" : "관리자 로그인"}</td>
          <td><span className={row.ok ? "state ok" : "state warn"}>{row.ok ? "성공" : "실패"}</span></td></tr>)}
      </tbody></table>{!data.attempts.length && <p className="empty">아직 접속 시도가 없습니다.</p>}</div>
    </Card>

    <Card title="최근 웹 로그인 실패" caption="대시보드와 나루 로그인에서 비밀번호를 틀린 기록입니다. 전체 기록은 작업 기록 탭에 있습니다.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>계정</th><th>주소</th><th>위치</th><th>이유</th></tr></thead><tbody>
        {failedLogins.map((event) => <tr key={event.eventId}><td>{date(event.time)}</td><td>{event.nickname ?? event.loginId}</td><td className="mono">{event.ip}</td><td>{event.location}</td>
          <td>{event.reason === "invalid_credentials" ? "비밀번호 불일치" : event.reason === "account_not_found" ? "계정 없음" : event.reason ?? "—"}</td></tr>)}
      </tbody></table>{!failedLogins.length && <p className="empty">최근 로그인 실패가 없습니다.</p>}</div>
    </Card>

    <Card title="적용 중인 보호" caption="코드에 정해진 값이라 이 화면에서 바꾸지는 않습니다.">
      <dl className="security-facts">
        <div><dt>접속 범위</dt><dd>모든 서비스가 이 컴퓨터(127.0.0.1)에서만 열립니다.</dd></div>
        <div><dt>4312 · 4313 로그인</dt><dd>대시보드·나루와 같은 아이디와 비밀번호로 로그인하고, 관리자 계정만 통과합니다.</dd></div>
        <div><dt>비상 경로</dt><dd>데이터베이스나 데이터 서비스가 멈췄을 때를 위해, 아이디 없이 정해진 비상 비밀번호로도 두 페이지를 열 수 있습니다. 사용하면 위 기록에 "비상 경로"로 남습니다.</dd></div>
        <div><dt>시도 제한</dt><dd>같은 주소에서 {data.policy.lockAttempts}번 틀리면 {data.policy.lockSeconds / 60}분 동안 막습니다.</dd></div>
        <div><dt>서비스 간 통신</dt><dd>API 서버가 시작할 때 만든 내부 토큰으로만 데이터 서비스를 부릅니다. 토큰은 저장하지 않습니다.</dd></div>
        <div><dt>로그인 유지</dt><dd>12시간 뒤 자동으로 로그아웃됩니다.</dd></div>
        <div><dt>Wide-Learning (4315)</dt><dd>입장 비밀번호와 로그인 시도 확인 코드는 그 화면의 설정에서 바꿉니다.</dd></div>
      </dl>
    </Card>
  </>;
}
