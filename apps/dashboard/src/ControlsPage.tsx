import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, errorText, type Ctx } from "./common";

type Evidence = { path: string; test: string; found: boolean };
type Control = { id: string; control: string; location: string; evidence: Evidence[]; status: "verified" | "broken" };
type Drill = { date: string; source: string; command: string; ok: boolean; cleanup_ok: boolean; elapsed_seconds?: number; tables?: number; error?: string; note?: string };
type Review = { date: string; scope: string; admin_count: number; audit_rows: number; audit_range: string; findings: string[] };
type Backup = { status: "ok" | "stale" | "missing"; count: number; maxAgeHours: number; latest: string | null; ageHours: number | null };
type ReviewState = { status: "ok" | "overdue" | "missing"; lastDate: string | null; ageDays: number | null; maxDays: number };
type Row = { key: string; label: string; status: "ok" | "warn" | "unknown"; detail: string };
type Rotation = { name: string; lastRotated: string | null; ageDays: number | null; maxDays: number; status: "ok" | "overdue" | "missing" };
type ApprovalItem = { requestId: number; action: string; summary: string; requester: string; createdEpoch: number; approver: string | null; status: "pending" | "approved" | "rejected" | "used" | "expired" };
type Approvals = { enabled: boolean; administrators: number; highRisk: string[]; requests: ApprovalItem[] };
type Overview = { rotations: Rotation[]; anomalies: Row[]; backup: Backup; accessReview: ReviewState; controls: Control[]; summary: { total: number; verified: number }; restoreDrills: Drill[]; accessReviews: Review[]; stress: { scenarios?: string[]; last_run?: string; result?: string; test_file?: string } };
const SCENARIO = { rate_spike: "금리 급등", deposit_run: "예금 인출", correlated_defaults: "상관 부도", collateral_decline: "담보 하락", fx_rate_jump: "환율 급변", settlement_failures: "결제 실패" } as Record<string, string>;

/** 통제 · 복구: control-evidence status, dated restore drills and access reviews, stress scenarios. Read only. */
export function ControlsPage({ ctx }: { ctx: Ctx }) {
  const { say } = ctx;
  const { run } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [approvals, setApprovals] = useState<Approvals | null>(null);
  useEffect(() => {
    api<Overview>("/api/admin/controls").then(setData).catch((cause) => say(errorText(cause, "통제 정보를 불러오지 못했습니다."), true));
    api<Approvals>("/api/admin/approvals").then(setApprovals).catch(() => undefined);
  }, []);
  const decide = (item: ApprovalItem, action: "approve" | "reject") => {
    if (!window.confirm(`요청 ${item.requestId}번(${item.action}, 요청자 끝 4자리 ${item.requester.slice(-4)})을 ${action === "approve" ? "승인" : "거부"}할까요? 기록은 지울 수 없습니다.`)) return;
    void run(() => api<Approvals>("/api/admin/approvals", { method: "POST", body: JSON.stringify({ action, requestId: item.requestId }) }), "처리하지 못했습니다.", setApprovals);
  };
  const toggleSwitch = (enabled: boolean) => {
    if (!window.confirm(enabled ? "고위험 관리자 쓰기에 2인 승인을 요구합니다. 관리자가 2명 이상이어야 켤 수 있습니다. 켤까요?" : "2인 승인을 끄려면 다른 관리자의 승인이 필요합니다. 요청을 보낼까요?")) return;
    void run(() => api<Approvals>("/api/admin/approvals", { method: "POST", body: JSON.stringify({ action: "set_switch", enabled }) }), "처리하지 못했습니다.", setApprovals);
  };
  if (!data) return <p className="cap">불러오는 중…</p>;
  const { backup, accessReview } = data;
  return <>
    <Card title="점검 경고" caption="조회 전용입니다. 백업을 만들거나 검토를 대신하지 않고 상태만 알려 줍니다.">
      <div className="scroll-x"><table><thead><tr><th>항목</th><th>상태</th><th>내용</th></tr></thead><tbody>
        <tr><td>백업 최신성</td><td className={backup.status === "ok" ? "" : "neg"}>{{ ok: "정상", stale: "오래됨", missing: "없음" }[backup.status]}</td>
          <td className="cap">{backup.latest ? `${backup.latest} · ${backup.ageHours}시간 전 (기준 ${backup.maxAgeHours}시간) · ${backup.count}개 보관` : "백업 파일이 없습니다."}</td></tr>
        <tr><td>관리자 접근 검토</td><td className={accessReview.status === "ok" ? "" : "neg"}>{{ ok: "정상", overdue: "기한 지남", missing: "없음" }[accessReview.status]}</td>
          <td className="cap">{accessReview.lastDate ? `마지막 검토 ${accessReview.lastDate} · ${accessReview.ageDays}일 전 (기준 ${accessReview.maxDays}일)` : "검토 기록이 없습니다."}</td></tr>
        {data.rotations.map((row) => <tr key={row.name}><td>비밀 교체 ({row.name})</td><td className={row.status === "ok" ? "" : "neg"}>{{ ok: "정상", overdue: "교체 기한", missing: "기록 없음" }[row.status]}</td>
          <td className="cap">{row.lastRotated ? `마지막 교체 ${row.lastRotated} · ${row.ageDays}일 전 (기준 ${row.maxDays}일)` : "교체 기록이 없습니다."}</td></tr>)}
        {data.anomalies.map((row) => <tr key={row.key}><td>{row.label}</td><td className={row.status === "warn" ? "neg" : ""}>{{ ok: "정상", warn: "경고", unknown: "확인 못 함" }[row.status]}</td><td className="cap">{row.detail}</td></tr>)}
      </tbody></table></div>
    </Card>
    {approvals && <Card title="2인 승인" caption={`고위험 관리자 쓰기에 요청자와 다른 관리자의 승인을 요구합니다(기본 꺼짐). 관리자 ${approvals.administrators}명. 고위험 동작: ${approvals.highRisk.join(", ")}`}
      actions={<button type="button" disabled={ctx.busy} onClick={() => toggleSwitch(!approvals.enabled)}>{approvals.enabled ? "끄기 요청" : "켜기"}</button>}>
      <p className="cap">현재 {approvals.enabled ? "켜짐" : "꺼짐"}.</p>
      <div className="scroll-x"><table><thead><tr><th>번호</th><th>동작</th><th>요청자</th><th>상태</th><th>승인자</th><th></th></tr></thead><tbody>
        {approvals.requests.map((item) => <tr key={item.requestId}>
          <td>{item.requestId}</td><td className="cap">{item.summary}</td><td>…{item.requester.slice(-4)}</td>
          <td>{{ pending: "대기", approved: "승인됨", rejected: "거부됨", used: "사용됨", expired: "만료" }[item.status]}</td><td>{item.approver ? `…${item.approver.slice(-4)}` : "—"}</td>
          <td>{item.status === "pending" && <><button type="button" disabled={ctx.busy} onClick={() => decide(item, "approve")}>승인</button> <button type="button" disabled={ctx.busy} onClick={() => decide(item, "reject")}>거부</button></>}</td>
        </tr>)}
      </tbody></table></div>
    </Card>}
    <Card title="통제 증빙표" caption={`${data.summary.verified} / ${data.summary.total}개 통제의 증빙 시험이 실제로 존재합니다. 존재만 확인하며 통과 여부는 시험 실행으로 확인합니다.`}>
      <div className="scroll-x"><table><thead><tr><th>ID</th><th>상태</th><th>증빙</th><th>통제</th><th>구현 위치</th></tr></thead><tbody>
        {data.controls.map((row) => <tr key={row.id}>
          <td>{row.id}</td>
          <td className={row.status === "verified" ? "" : "neg"}>{row.status === "verified" ? "확인됨" : "증빙 없음"}</td>
          <td className="cap">{row.evidence.length}건</td>
          <td>{row.control}</td><td className="cap">{row.location}</td>
        </tr>)}
      </tbody></table></div>
    </Card>
    <Card title="복구 훈련 기록" caption="운영 DB의 읽기 전용 복사본으로 실행한 결과입니다. 기록은 추가만 합니다.">
      <div className="scroll-x"><table><thead><tr><th>날짜</th><th>명령</th><th>결과</th><th>정리</th><th>소요(초)</th><th>비고</th></tr></thead><tbody>
        {data.restoreDrills.map((row, index) => <tr key={index}>
          <td>{row.date}</td><td className="cap">{row.command}</td><td className={row.ok ? "" : "neg"}>{row.ok ? "성공" : "실패"}</td>
          <td>{row.cleanup_ok ? "완료" : "확인 필요"}</td><td>{row.elapsed_seconds ?? "—"}</td><td className="cap">{row.error ? `${row.error}. ` : ""}{row.note}</td>
        </tr>)}
      </tbody></table></div>
    </Card>
    <Card title="관리자 접근 검토" caption="관리자 목록과 감사 기록 요약입니다.">
      <div className="scroll-x"><table><thead><tr><th>날짜</th><th>범위</th><th>관리자 수</th><th>감사 기록</th><th>발견 사항</th></tr></thead><tbody>
        {data.accessReviews.map((row, index) => <tr key={index}>
          <td>{row.date}</td><td className="cap">{row.scope}</td><td>{row.admin_count}</td><td>{row.audit_rows.toLocaleString("ko-KR")} ({row.audit_range})</td><td className="cap">{row.findings.join(" ")}</td>
        </tr>)}
      </tbody></table></div>
    </Card>
    <Card title="스트레스 시험" caption={`${data.stress.test_file ?? ""} · 마지막 실행 ${data.stress.last_run ?? "—"} · ${data.stress.result ?? ""}`}>
      <div className="scroll-x"><table><tbody><tr>{(data.stress.scenarios ?? []).map((name) => <td key={name}>{SCENARIO[name] ?? name}</td>)}</tr></tbody></table></div>
    </Card>
  </>;
}
