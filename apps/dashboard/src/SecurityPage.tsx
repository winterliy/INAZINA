import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, errorText, type Ctx } from "./common";
import type { LoginEvent } from "./LoginHistoryCard";
import "./security.css";

/**
 * 보안: the web logins that failed (recorded by the login server) and the safeguards in force.
 * (The wide-learning viewer on wl.localhost changes its own passwords in its settings.)
 */
export function SecurityPage({ ctx }: { ctx: Ctx }) {
  const { say } = ctx;
  const [failedLogins, setFailedLogins] = useState<LoginEvent[] | null>(null);

  const load = useCallback(async () => {
    const result = await api<{ events: LoginEvent[] }>("/api/admin/login-history");
    setFailedLogins(result.events.filter((event) => !event.success).slice(0, 10));
  }, []);
  useEffect(() => { load().catch((cause) => say(errorText(cause, "보안 정보를 불러오지 못했습니다."), true)); }, [load, say]);

  if (!failedLogins) return <p className="cap">불러오는 중…</p>;
  return <>
    <Card title="최근 웹 로그인 실패" caption="로그인 서버(id)에서 비밀번호를 틀린 기록입니다. 전체 기록은 작업 기록 탭에 있습니다.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>계정</th><th>주소</th><th>위치</th><th>이유</th></tr></thead><tbody>
        {failedLogins.map((event) => <tr key={event.eventId}><td>{date(event.time)}</td><td>{event.nickname ?? event.loginId}</td><td className="mono">{event.ip}</td><td>{event.location}</td>
          <td>{event.reason === "invalid_credentials" ? "비밀번호 불일치" : event.reason === "account_not_found" ? "계정 없음" : event.reason ?? "—"}</td></tr>)}
      </tbody></table>{!failedLogins.length && <p className="empty">최근 로그인 실패가 없습니다.</p>}</div>
    </Card>

    <Card title="적용 중인 보호" caption="코드에 정해진 값이라 이 화면에서 바꾸지는 않습니다.">
      <dl className="security-facts">
        <div><dt>접속 범위</dt><dd>모든 서비스가 이 컴퓨터(127.0.0.1)에서만 열리고, 사람은 *.inazina.localhost 이름으로만 접속합니다.</dd></div>
        <div><dt>로그인</dt><dd>로그인·가입·비밀번호 재설정은 로그인 서버(id.inazina.localhost) 한 곳에서만 합니다. 대시보드·나루에는 자체 로그인이 없습니다.</dd></div>
        <div><dt>4312 · 4313</dt><dd>브라우저로 열어도 별도 로그인 화면은 없고 JSON 응답만 보입니다. 로그인이 필요한 응답은 인증 오류(401)만 돌려줍니다.</dd></div>
        <div><dt>서비스 간 통신</dt><dd>API 서버가 시작할 때 만든 내부 토큰으로만 데이터 서비스를 부릅니다. 토큰은 저장하지 않습니다.</dd></div>
        <div><dt>로그인 유지</dt><dd>브라우저를 닫으면 로그인이 풀리고, 켜 두어도 12시간 뒤 자동으로 로그아웃됩니다.</dd></div>
        <div><dt>Wide-Learning (wl.localhost)</dt><dd>입장 비밀번호와 로그인 시도 확인 코드는 그 화면의 설정에서 바꿉니다.</dd></div>
      </dl>
    </Card>
  </>;
}
