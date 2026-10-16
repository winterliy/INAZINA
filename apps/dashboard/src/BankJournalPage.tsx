import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, type Ctx } from "./common";

type Trial = { currency_code: string; account_code: string; debit_units: number; credit_units: number; balance_units: number };
type Unbalanced = { entry_id: number; issuer: string; event_type: string; reference_key: string; currency_code: string; line_count: number; debit_units: number; credit_units: number };
type Reconciliation = { reference_key: string; currency_code: string; ledger_amount: number; journal_amount: number | null; difference: number | null; status: string };
type Report = { issuer: string; currency: string | null; trialBalance: Trial[]; unbalancedEntries: Unbalanced[]; resultReconciliation: Reconciliation[] };

export function BankJournalPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [issuer, setIssuer] = useState("");
  const [currency, setCurrency] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  function load(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!issuer.trim()) { say("금융기관 issuer를 입력해주세요.", true); return; }
    const query = new URLSearchParams({ issuer: issuer.trim(), ...(currency.trim() ? { currency: currency.trim().toUpperCase() } : {}) });
    void run(() => api<Report>(`/api/admin/bank-journal?${query}`), "은행 원장 대사 정보를 불러오지 못했습니다.", setReport);
  }
  return <Card title="은행 이중분개 원장 대사" caption="조회 전용 · 금액은 각 통화의 최소 단위 정수로 표시합니다. 불일치는 회계 차이 조사 신호이며 법정 감사의견이 아닙니다.">
    <form className="form-grid" onSubmit={load}>
      <Field label="금융기관 issuer"><input className="mono" required maxLength={100} value={issuer} onChange={(event) => setIssuer(event.target.value)} /></Field>
      <Field label="통화 필터" hint="선택"><input className="mono" maxLength={12} value={currency} onChange={(event) => setCurrency(event.target.value)} /></Field>
      <button className="btn primary" disabled={busy || !issuer.trim()}>원장 조회</button>
    </form>
    {report && <>
      <h3 className="sub-head">시산 잔액 · {report.issuer}</h3>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th>계정</th><th className="r">차변 최소단위</th><th className="r">대변 최소단위</th><th className="r">잔액 최소단위</th></tr></thead><tbody>
        {report.trialBalance.map((row, i) => <tr key={`${row.currency_code}-${row.account_code}-${i}`}><td>{row.currency_code}</td><td className="mono">{row.account_code}</td><td className="r mono">{row.debit_units}</td><td className="r mono">{row.credit_units}</td><td className="r mono">{row.balance_units}</td></tr>)}
      </tbody></table>{!report.trialBalance.length && <p className="empty">전기된 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">불균형 분개</h3>
      <div className="scroll-x"><table><thead><tr><th>분개</th><th>유형</th><th>참조</th><th>통화</th><th className="r">차변</th><th className="r">대변</th></tr></thead><tbody>
        {report.unbalancedEntries.map((row) => <tr key={row.entry_id}><td>#{row.entry_id}</td><td>{row.event_type}</td><td className="mono">{row.reference_key}</td><td>{row.currency_code}</td><td className="r mono">{row.debit_units}</td><td className="r mono">{row.credit_units}</td></tr>)}
      </tbody></table>{!report.unbalancedEntries.length && <p className="empty">불균형 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">기존 결과 원장 대사</h3>
      <div className="scroll-x"><table><thead><tr><th>참조</th><th>통화</th><th className="r">기존 원장</th><th className="r">분개</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.resultReconciliation.map((row, i) => <tr key={`${row.reference_key}-${row.currency_code}-${i}`}><td className="mono">{row.reference_key}</td><td>{row.currency_code}</td><td className="r mono">{row.ledger_amount}</td><td className="r mono">{row.journal_amount ?? "—"}</td><td className="r mono">{row.difference ?? "—"}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.resultReconciliation.length && <p className="empty">대사할 결과 원장이 없습니다.</p>}</div>
    </>}
  </Card>;
}
