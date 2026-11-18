import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, date, errorText, type Ctx } from "./common";
import type { LoginEvent } from "./LoginHistoryCard";
import "./security.css";

type Service = { id: "api" | "python"; label: string; port: number; isDefault: boolean; updatedEpoch: number | null; updatedBy: string | null; locked: string[] };
type Attempt = { at: number; service: string; ip: string; ok: boolean };
type Overview = { services: Service[]; attempts: Attempt[]; last24h: { total: number; failed: number };
  policy: { minLength: number; maxLength: number; lockAttempts: number; lockSeconds: number; storage: string } };

const SERVICE_NAME: Record<string, string> = { api: "API 서버 (4312)", python: "데이터 서비스 (4313)" };

/**
 * 보안: the browser-access passwords of the two private service ports, what has tried them, and the
 * safeguards in force. (The wide-learning viewer on 4315 changes its own passwords in its settings.)
 */
export function SecurityPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [failedLogins, setFailedLogins] = useState<LoginEvent[]>([]);
  const [form, setForm] = useState({ service: "api", current: "", password: "", repeat: "" });

  const load = useCallback(async () => {
    setData(await api<Overview>("/api/admin/security"));
    api<{ events: LoginEvent[] }>("/api/admin/login-history").then((result) => setFailedLogins(result.events.filter((event) => !event.success).slice(0, 10))).catch(() => setFailedLogins([]));
  }, []);
  useEffect(() => { load().catch((cause) => say(errorText(cause, "보안 정보를 불러오지 못했습니다."), true)); }, [load, say]);

  function change(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (form.password !== form.repeat) { say("새 비밀번호가 서로 다릅니다.", true); return; }
    if (!window.confirm(`${SERVICE_NAME[form.service]}의 접속 비밀번호를 바꿀까요? 바로 적용되고, 예전 비밀번호는 쓸 수 없습니다.`)) return;
    void run(() => api<{ message: string }>("/api/admin/security", { method: "POST", body: JSON.stringify({ action: "browser_password", service: form.service, current: form.current, password: form.password }) }),
      "비밀번호를 바꾸지 못했습니다.", (result) => { say(result.message); setForm({ ...form, current: "", password: "", repeat: "" }); void load(); });
  }

  if (!data) return <p className="cap">불러오는 중…</p>;
  const defaults = data.services.filter((service) => service.isDefault);
  const locked = data.services.flatMap((service) => service.locked.map((ip) => ({ service: service.id, ip })));
  return <>
    <section className="tile-row security-tiles">
      <section className={defaults.length ? "tile warn" : "tile"}><p className="tile-label">기본 비밀번호</p><p className="tile-value">{defaults.length ? `${defaults.length}곳 사용 중` : "없음"}</p>
        <p className="cap">{defaults.length ? defaults.map((service) => service.label).join(" · ") : "모두 직접 정한 비밀번호입니다"}</p></section>
      <section className="tile"><p className="tile-label">최근 24시간 접속 시도</p><p className="tile-value">{data.last24h.total}건</p><p className="cap">실패 {data.last24h.failed}건</p></section>
      <section className={locked.length ? "tile warn" : "tile"}><p className="tile-label">잠긴 주소</p><p className="tile-value">{locked.length}곳</p>
        <p className="cap">{data.policy.lockAttempts}번 틀리면 {data.policy.lockSeconds / 60}분 잠깁니다</p></section>
    </section>

    <Card title="접속 비밀번호" caption="브라우저로 4312·4313 주소를 열 때 묻는 비밀번호입니다. 서비스마다 따로 정하고, 바꾸면 재시작 없이 바로 적용됩니다.">
      <div className="scroll-x"><table><thead><tr><th>서비스</th><th>상태</th><th>마지막 변경</th><th>변경한 사람</th></tr></thead><tbody>
        {data.services.map((service) => <tr key={service.id}><td>{service.label} <span className="mono">:{service.port}</span></td>
          <td><span className={service.isDefault ? "state warn" : "state ok"}>{service.isDefault ? "기본값" : "직접 설정"}</span></td>
          <td>{service.updatedEpoch ? date(service.updatedEpoch) : "—"}</td><td className="mono">{service.updatedBy ?? "—"}</td></tr>)}
      </tbody></table></div>
      <form className="form-grid" onSubmit={change}>
        <Field label="서비스"><select value={form.service} onChange={(event) => setForm({ ...form, service: event.target.value })}>{data.services.map((service) => <option key={service.id} value={service.id}>{SERVICE_NAME[service.id]}</option>)}</select></Field>
        <Field label="현재 비밀번호"><input type="password" autoComplete="off" value={form.current} onChange={(event) => setForm({ ...form, current: event.target.value })} required /></Field>
        <Field label="새 비밀번호" hint={`${data.policy.minLength}~${data.policy.maxLength}자`}><input type="password" autoComplete="new-password" minLength={data.policy.minLength} maxLength={data.policy.maxLength} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} required /></Field>
        <Field label="새 비밀번호 확인"><input type="password" autoComplete="new-password" value={form.repeat} onChange={(event) => setForm({ ...form, repeat: event.target.value })} required /></Field>
        <button className="btn primary" disabled={busy || !form.current || !form.password}>비밀번호 변경</button>
      </form>
    </Card>

    <Card title="접속 시도 기록" caption="4312·4313에 비밀번호를 입력한 기록입니다. 입력한 내용은 저장하지 않고 시각, 서비스, 주소, 결과만 남깁니다. 최근 100건.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>서비스</th><th>주소</th><th>결과</th></tr></thead><tbody>
        {data.attempts.map((row, index) => <tr key={`${row.at}-${index}`}><td>{date(row.at)}</td><td>{SERVICE_NAME[row.service] ?? row.service}</td><td className="mono">{row.ip}</td>
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
        <div><dt>비밀번호 저장</dt><dd>{data.policy.storage}. 입력한 비밀번호는 기록하지 않습니다.</dd></div>
        <div><dt>시도 제한</dt><dd>같은 주소에서 {data.policy.lockAttempts}번 틀리면 {data.policy.lockSeconds / 60}분 동안 막습니다.</dd></div>
        <div><dt>서비스 간 통신</dt><dd>API 서버가 시작할 때 만든 내부 토큰으로만 데이터 서비스를 부릅니다. 토큰은 저장하지 않습니다.</dd></div>
        <div><dt>로그인 유지</dt><dd>12시간 뒤 자동으로 로그아웃됩니다.</dd></div>
        <div><dt>Wide-Learning (4315)</dt><dd>입장 비밀번호와 로그인 시도 확인 코드는 그 화면의 설정에서 바꿉니다.</dd></div>
      </dl>
    </Card>
  </>;
}
