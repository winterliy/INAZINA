import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { BankJournalPage } from "./BankJournalPage";
import { Card, Field, number, plain, show, type Ctx } from "./common";

type Bank = { id: string; issuer: string; nationality: string; status: string; equityCapital: number; retainedEarnings: number; products: number;
  depositAccounts: number; deposits: number; loans: number; loanBalance: number; account: string | null; cash: number | null };
type Detail = Bank & { settings: Record<string, number | string>; productsList: { code: string; name: string; kind: string; interest_rate: number; status: string }[] };
type Result = { message: string; success: boolean };

const STATUS: Record<string, string> = { active: "영업", restricted: "영업 제한", failed: "파산" };
const SETTINGS: [string, string, string][] = [
  ["funding_spread", "조달 가산율 %", "-100~100"], ["operating_cost_spread", "운영비 가산율 %", "0~100"], ["liquidity_spread", "유동성 가산율 %", "-100~100"],
  ["target_margin", "목표 마진 %", "-100~100"], ["competition_adjustment", "경쟁 조정 %", "-100~100"], ["target_capital_ratio", "목표 자본비율", "0~1"],
  ["target_loan_deposit_ratio", "목표 예대율", "0보다 큼"], ["reserve_ratio", "지급준비율", "0~1"], ["deposit_insurance_limit", "예금보호 한도", "0 이상"],
];

/** Banks managed like companies: each has a unique ID, a list row, and its own page with settings, products and books. */
export function BanksPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [banks, setBanks] = useState<Bank[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState({ id: "", issuer: "", nationality: "", equityCapital: "" });

  const load = useCallback(async () => setBanks((await api<{ banks: Bank[] }>("/api/admin/banks")).banks), []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "은행 목록을 불러오지 못했습니다.", true)); }, [load]);
  const loadDetail = useCallback(async (id: string) => {
    const result = await api<Detail>(`/api/admin/banks/${encodeURIComponent(id)}`);
    setDetail(result);
    setSettings(Object.fromEntries(Object.entries(result.settings).map(([key, value]) => [key, String(value)])));
  }, []);
  useEffect(() => {
    setDetail(null);
    window.scrollTo({ top: 0 });
    if (open) loadDetail(open).catch((cause) => say(cause instanceof Error ? cause.message : "은행 정보를 불러오지 못했습니다.", true));
  }, [open, loadDetail]);

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!window.confirm(`${draft.nationality} 국적의 ${draft.issuer} 은행을 만들까요?`)) return;
    void run(() => api<Result>("/api/admin/banks", { method: "POST", body: JSON.stringify(draft) }), "은행을 만들지 못했습니다.",
      async (result) => { say(plain(result.message), !result.success); setDraft({ id: "", issuer: "", nationality: "", equityCapital: "" }); await load(); });
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    const changes = Object.fromEntries(Object.entries(settings).filter(([key, value]) => value !== String(detail.settings[key])));
    if (!Object.keys(changes).length) { say("변경된 설정이 없습니다.", true); return; }
    if (!window.confirm(`${detail.issuer} (${detail.id}) 설정을 바꿀까요?\n\n${Object.entries(changes).map(([key, value]) => `${key}: ${detail.settings[key]} → ${value}`).join("\n")}`)) return;
    void run(() => api<Result>(`/api/admin/banks/${encodeURIComponent(detail.id)}`, { method: "POST", body: JSON.stringify(changes) }), "은행 설정을 저장하지 못했습니다.",
      async (result) => { say(plain(result.message), !result.success); await Promise.all([loadDetail(detail.id), load()]); });
  }

  if (open) return <>
    <div className="subpage-bar">
      <button className="soft" type="button" onClick={() => setOpen(null)}>← 은행 목록으로 돌아가기</button>
      <p><b>{detail?.issuer ?? "…"}</b> <span className="mono">은행 ID {open}{detail ? ` · ${detail.nationality}` : ""}</span></p>
    </div>
    {detail && <>
      <div className="tile-row">
        <section className="tile tile-dark"><p className="tile-label">자기자본</p><p className="tile-value">{number(detail.equityCapital)}</p></section>
        <section className="tile"><p className="tile-label">예금 · {number(detail.depositAccounts)}계좌</p><p className="tile-value">{number(detail.deposits)}</p></section>
        <section className="tile"><p className="tile-label">대출 잔액 · {number(detail.loans)}건</p><p className="tile-value">{number(detail.loanBalance)}</p></section>
        <section className="tile"><p className="tile-label">본사 계좌</p><p className="tile-value">{detail.cash === null ? "—" : number(detail.cash)}</p><p className="hint mono">{show(detail.account)}</p></section>
      </div>
      <Card title="은행 설정" caption="금리 산정 가산율·자본비율·예금보호 한도와 영업 상태입니다. 바꾼 칸만 저장하고 작업 기록에 남깁니다."
        actions={<button className="soft" type="button" onClick={() => setOpen(null)}>닫기</button>}>
        <form className="form-grid" onSubmit={save}>
          {SETTINGS.map(([key, label, hint]) => <Field key={key} label={label} hint={hint}><input className="mono" inputMode="decimal" value={settings[key] ?? ""} onChange={(event) => setSettings({ ...settings, [key]: event.target.value })} /></Field>)}
          <Field label="영업 상태"><select value={settings.status ?? "active"} onChange={(event) => setSettings({ ...settings, status: event.target.value })}>{Object.entries(STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
          <button className="btn primary" disabled={busy}>저장</button>
        </form>
      </Card>
      <Card title="발행 상품" caption={`${number(detail.productsList.length)}개 · 저축·대출 상품의 발행 은행 ID가 이 은행입니다. 상품 생성·수정은 금융 상품 탭에서 합니다.`}>
        <div className="scroll-x"><table><thead><tr><th>코드</th><th>이름</th><th>종류</th><th className="r">금리</th><th>상태</th></tr></thead><tbody>
          {detail.productsList.map((item) => <tr key={`${item.kind}-${item.code}`}><td className="mono">{item.code}</td><td>{item.name}</td><td>{item.kind === "loan" ? "대출" : "저축"}</td><td className="r mono">{item.interest_rate}%</td><td><span className={item.status === "active" ? "tag ok" : "tag warn"}>{item.status}</span></td></tr>)}
        </tbody></table>{!detail.productsList.length && <p className="empty">이 은행이 발행한 상품이 없습니다.</p>}</div>
      </Card>
      <BankJournalPage key={detail.id} ctx={ctx} bank={detail.id} />
    </>}
  </>;

  const term = query.trim().toLocaleLowerCase();
  const visible = banks.filter((bank) => !term || `${bank.id} ${bank.issuer} ${bank.nationality}`.toLocaleLowerCase().includes(term));
  return <>
    <div className="tile-row">
      <section className="tile tile-dark"><p className="tile-label">은행</p><p className="tile-value">{number(banks.length)}</p></section>
      <section className="tile"><p className="tile-label">총 예금</p><p className="tile-value">{number(banks.reduce((sum, bank) => sum + bank.deposits, 0))}</p></section>
      <section className="tile"><p className="tile-label">총 대출 잔액</p><p className="tile-value">{number(banks.reduce((sum, bank) => sum + bank.loanBalance, 0))}</p></section>
      <section className="tile"><p className="tile-label">영업 제한 · 파산</p><p className="tile-value">{number(banks.filter((bank) => bank.status !== "active").length)}</p></section>
    </div>
    <Card title="은행" caption="행을 누르면 그 은행의 관리 페이지가 열려 설정·발행 상품·원장 대사를 봅니다. 은행 ID는 만들 때 정해지고 바뀌지 않습니다.">
      <div className="scroll-x tall"><table><thead><tr><th>은행 ID</th><th>은행</th><th>국가</th><th>상태</th><th className="r">자기자본</th><th className="r">예금</th><th className="r">대출 잔액</th><th className="r">상품</th><th>본사 계좌</th></tr></thead><tbody>
        {visible.map((bank) => <tr key={bank.id} className="clickable" tabIndex={0} onClick={() => setOpen(bank.id)} onKeyDown={(event) => { if (event.key === "Enter") setOpen(bank.id); }}>
          <td className="mono">{bank.id}</td><td><b>{bank.issuer}</b></td><td>{bank.nationality}</td>
          <td><span className={bank.status === "active" ? "tag ok" : "tag warn"}>{STATUS[bank.status] ?? bank.status}</span></td>
          <td className="r mono">{number(bank.equityCapital)}</td><td className="r mono">{number(bank.deposits)}</td><td className="r mono">{number(bank.loanBalance)}</td>
          <td className="r">{number(bank.products)}</td><td className="mono">{show(bank.account)}</td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">은행이 없습니다.</p>}</div>
    </Card>
    <Card title="은행 만들기" caption="은행 ID를 직접 정하거나, 비워 두면 무작위로 붙입니다(BNK + 6자리). 저축·대출 상품과 원장 조회 등 은행이 필요한 곳에는 모두 이 은행 ID를 씁니다.">
      <form className="form-grid" onSubmit={create}>
        <Field label="은행 ID" hint="선택 · 비우면 BNK + 6자리 무작위"><input className="mono" maxLength={20} value={draft.id} onChange={(event) => setDraft({ ...draft, id: event.target.value.toUpperCase() })} /></Field>
        <Field label="은행 이름"><input required maxLength={60} value={draft.issuer} onChange={(event) => setDraft({ ...draft, issuer: event.target.value })} /></Field>
        <Field label="국가"><input required value={draft.nationality} onChange={(event) => setDraft({ ...draft, nationality: event.target.value })} /></Field>
        <Field label="자기자본" hint="선택 · 기본 1,000,000"><input className="mono" inputMode="decimal" value={draft.equityCapital} onChange={(event) => setDraft({ ...draft, equityCapital: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy}>은행 만들기</button>
      </form>
    </Card>
    <BankJournalPage ctx={ctx} />
  </>;
}
