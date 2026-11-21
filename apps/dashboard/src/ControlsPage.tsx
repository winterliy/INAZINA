import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, errorText, type Ctx } from "./common";

type Evidence = { path: string; test: string; found: boolean };
type Control = { id: string; control: string; location: string; evidence: Evidence[]; status: "verified" | "broken" };
type Drill = { date: string; source: string; command: string; ok: boolean; cleanup_ok: boolean; elapsed_seconds?: number; tables?: number; error?: string; note?: string };
type Review = { date: string; scope: string; admin_count: number; audit_rows: number; audit_range: string; findings: string[] };
type Overview = { controls: Control[]; summary: { total: number; verified: number }; restoreDrills: Drill[]; accessReviews: Review[]; stress: { scenarios?: string[]; last_run?: string; result?: string; test_file?: string } };
const SCENARIO = { rate_spike: "금리 급등", deposit_run: "예금 인출", correlated_defaults: "상관 부도", collateral_decline: "담보 하락", fx_rate_jump: "환율 급변", settlement_failures: "결제 실패" } as Record<string, string>;

/** 통제 · 복구: control-evidence status, dated restore drills and access reviews, stress scenarios. Read only. */
export function ControlsPage({ ctx }: { ctx: Ctx }) {
  const { say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  useEffect(() => { api<Overview>("/api/admin/controls").then(setData).catch((cause) => say(errorText(cause, "통제 정보를 불러오지 못했습니다."), true)); }, []);
  if (!data) return <p className="cap">불러오는 중…</p>;
  return <>
    <Card title="통제 증빙표" caption={`${data.summary.verified} / ${data.summary.total}개 통제의 증빙 시험이 실제로 존재합니다. 존재만 확인하며 통과 여부는 시험 실행으로 확인합니다.`}>
      <div className="scroll-x"><table><thead><tr><th>ID</th><th>통제</th><th>구현 위치</th><th>증빙</th><th>상태</th></tr></thead><tbody>
        {data.controls.map((row) => <tr key={row.id}>
          <td>{row.id}</td><td>{row.control}</td><td className="cap">{row.location}</td>
          <td className="cap">{row.evidence.length}건</td>
          <td className={row.status === "verified" ? "" : "neg"}>{row.status === "verified" ? "확인됨" : "증빙 없음"}</td>
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
