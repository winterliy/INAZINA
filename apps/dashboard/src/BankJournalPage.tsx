import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, type Ctx } from "./common";

type Trial = { currency_code: string; account_code: string; debit_units: number; credit_units: number; balance_units: number; debit_amount: string; credit_amount: string; balance_amount: string; decimal_places: number };
type Unbalanced = { entry_id: number; issuer: string; event_type: string; reference_key: string; currency_code: string; line_count: number; debit_units: number; credit_units: number; debit_amount: string; credit_amount: string };
type Reconciliation = { reference_key: string; currency_code: string; ledger_amount: number; journal_amount: number | null; difference: number | null; ledger_amount_text: string; journal_amount_text: string | null; difference_text: string | null; status: string };
type Report = { issuer: string; currency: string | null; trialBalance: Trial[]; unbalancedEntries: Unbalanced[]; resultReconciliation: Reconciliation[] };

export function BankJournalPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [issuer, setIssuer] = useState("");
  const [currency, setCurrency] = useState("");
  const [since, setSince] = useState(() => {
    const value = new Date(Date.now() - 30 * 86400_000);
    value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
    return value.toISOString().slice(0, 16);
  });
  const [report, setReport] = useState<Report | null>(null);
  function load(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!issuer.trim()) { say("금융기관 issuer를 입력해주세요.", true); return; }
    const query = new URLSearchParams({ issuer: issuer.trim(), ...(currency.trim() ? { currency: currency.trim().toUpperCase() } : {}),
      ...(since ? { since: String(Math.floor(new Date(since).getTime() / 1000)) } : {}) });
    void run(() => api<Report>(`/api/admin/bank-journal?${query}`), "은행 원장 대사 정보를 불러오지 못했습니다.", setReport);
  }
  return <Card title="은행 이중분개 원장 대사" caption="조회 전용 · 시산 잔액과 불균형은 전체 이력, 손익 원장 대사는 지정 기간입니다. 금액은 각 통화 정밀도를 따르며 법정 감사의견이 아닙니다.">
    <form className="form-grid" onSubmit={load}>
      <Field label="금융기관 issuer"><input className="mono" required maxLength={100} value={issuer} onChange={(event) => setIssuer(event.target.value)} /></Field>
      <Field label="통화 필터" hint="선택"><input className="mono" maxLength={12} value={currency} onChange={(event) => setCurrency(event.target.value)} /></Field>
      <Field label="손익 원장 대사 시작" hint="선택 · 기본 최근 30일, 비우면 전체"><input type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} /></Field>
      <button className="btn primary" disabled={busy || !issuer.trim()}>원장 조회</button>
    </form>
    {report && <>
      <h3 className="sub-head">시산 잔액 · {report.issuer}</h3>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th>계정</th><th className="r">차변</th><th className="r">대변</th><th className="r">잔액</th></tr></thead><tbody>
        {report.trialBalance.map((row, i) => <tr key={`${row.currency_code}-${row.account_code}-${i}`}><td>{row.currency_code}</td><td className="mono">{row.account_code}</td><td className="r mono">{groupAmount(row.debit_amount)} {row.currency_code}</td><td className="r mono">{groupAmount(row.credit_amount)} {row.currency_code}</td><td className="r mono">{groupAmount(row.balance_amount)} {row.currency_code}</td></tr>)}
      </tbody></table>{!report.trialBalance.length && <p className="empty">전기된 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">불균형 분개</h3>
      <div className="scroll-x"><table><thead><tr><th>분개</th><th>유형</th><th>참조</th><th>통화</th><th className="r">차변</th><th className="r">대변</th></tr></thead><tbody>
        {report.unbalancedEntries.map((row) => <tr key={row.entry_id}><td>#{row.entry_id}</td><td>{row.event_type}</td><td className="mono">{row.reference_key}</td><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.debit_amount)}</td><td className="r mono">{groupAmount(row.credit_amount)}</td></tr>)}
      </tbody></table>{!report.unbalancedEntries.length && <p className="empty">불균형 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">기존 결과 원장 대사</h3>
      <div className="scroll-x"><table><thead><tr><th>참조</th><th>통화</th><th className="r">기존 원장</th><th className="r">분개</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.resultReconciliation.map((row, i) => <tr key={`${row.reference_key}-${row.currency_code}-${i}`}><td className="mono">{row.reference_key}</td><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.ledger_amount_text)}</td><td className="r mono">{row.journal_amount_text ? groupAmount(row.journal_amount_text) : "—"}</td><td className="r mono">{row.difference_text ? groupAmount(row.difference_text) : "—"}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.resultReconciliation.length && <p className="empty">대사할 결과 원장이 없습니다.</p>}</div>
    </>}
  </Card>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}
