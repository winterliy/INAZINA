import { useState } from "react";
import type { FormEvent } from "react";
import { accountBalance, accountLabel, type Result } from "../commandKit";
import { Outcome, won, type ServiceProps } from "./ServiceKit";

type Tab = "accounts" | "savings" | "loans";
type Open = { kind: "join" | "close" | "borrow" | "repay"; key: string } | null;

/** 은행: 내 통장, 예금·적금 가입, 대출 — product cards instead of command forms. */
export function BankPage({ kit, homeCurrency, creditScore }: ServiceProps) {
  const [tab, setTab] = useState<Tab>("accounts");
  const [open, setOpen] = useState<Open>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [result, setResult] = useState<Result | null>(null);
  const lookups = kit.lookups;
  const accounts = lookups?.accounts ?? [];
  const loans = lookups?.loans ?? [];

  function toggle(next: Open) {
    setOpen(open && next && open.kind === next.kind && open.key === next.key ? null : next);
    setForm({});
    setResult(null);
  }
  function submit(event: FormEvent<HTMLFormElement>, name: string, values: Record<string, string>) {
    event.preventDefault();
    void kit.run(name, values).then((next) => { if (next) setResult(next); if (next?.success) setForm({}); });
  }
  const field = (key: string, label: string, props: Record<string, unknown> = {}) => <label className="svc-field">{label}<input value={form[key] ?? ""} onChange={(event) => setForm({ ...form, [key]: event.target.value })} {...props} /></label>;

  return <div className="svc-bank">
    <div className="svc-tabs" role="tablist" aria-label="은행 메뉴">
      {([["accounts", `내 통장 ${accounts.length}`], ["savings", "예금 · 적금"], ["loans", `대출${loans.length ? ` ${loans.length}` : ""}`]] as const).map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => { setTab(id); toggle(null); }}>{label}</button>)}
    </div>

    {tab === "accounts" && <section className="svc-list">
      {accounts.map((account) => <article key={account.account_number} className="svc-row-card">
        <div><small>{accountLabel(account.quote_currency, homeCurrency, "저축")} · {account.product_code}</small><b className="mono">{account.account_number}</b></div>
        <strong>{won(accountBalance(account), 2)} <small>{account.quote_currency}</small></strong>
        <div className="svc-row-actions"><a href={`/transfer`}>이체</a><button type="button" onClick={() => toggle({ kind: "close", key: account.account_number })}>해지</button></div>
        {open?.kind === "close" && open.key === account.account_number && <form className="svc-inline" onSubmit={(event) => submit(event, "계좌해지", { account: account.account_number, password: form.password ?? "", destination: form.destination ?? "" })}>
          {field("password", "계좌 비밀번호", { type: "password", required: true, autoComplete: "off" })}
          <label className="svc-field">잔액 받을 곳<select value={form.destination ?? ""} onChange={(event) => setForm({ ...form, destination: event.target.value })}><option value="">내 지갑</option>{accounts.filter((other) => other.account_number !== account.account_number).map((other) => <option key={other.account_number} value={other.account_number}>{other.product_code} {other.account_number}</option>)}</select></label>
          <p className="svc-hint">만기 전 해지는 상품에 따라 중도해지 이율이나 수수료가 적용됩니다.</p>
          <button className="svc-submit danger" disabled={kit.busy}>해지하기</button>
          <Outcome result={result} />
        </form>}
      </article>)}
      {!accounts.length && <div className="svc-empty"><b>아직 통장이 없습니다.</b><button type="button" onClick={() => setTab("savings")}>예금 · 적금 둘러보기</button></div>}
    </section>}

    {tab === "savings" && <section className="svc-products">
      {(lookups?.savingProducts ?? []).map((product) => <article key={product.code} className={open?.key === product.code ? "svc-product open" : "svc-product"}>
        <small>{accountLabel(product.quote_currency, homeCurrency, "저축 예금")}{product.quote_currency !== homeCurrency ? ` · ${product.quote_currency}` : ""}</small>
        <h3>{product.name}</h3>
        <p className="svc-rate">연 <b>{product.interest_rate}</b>%</p>
        <ul><li>최소 예치금 {won(product.deposit_amount)} {product.quote_currency}</li>{product.maturity_days > 0 && <li>만기 {product.maturity_days}일</li>}</ul>
        <button type="button" className="svc-submit" onClick={() => toggle({ kind: "join", key: product.code })}>{open?.key === product.code ? "닫기" : "가입하기"}</button>
        {open?.kind === "join" && open.key === product.code && <form className="svc-inline" onSubmit={(event) => submit(event, "계좌개설", { product: product.code, password: form.password ?? "", deposit: form.deposit ?? "" })}>
          {field("password", "계좌 비밀번호 (4~16자)", { type: "password", required: true, minLength: 4, maxLength: 16, autoComplete: "new-password" })}
          {field("deposit", `처음 넣을 돈 (${product.quote_currency})`, { inputMode: "decimal", placeholder: `최소 ${won(product.deposit_amount)}` })}
          <button className="svc-submit" disabled={kit.busy}>가입 완료</button>
          <Outcome result={result} />
        </form>}
      </article>)}
      {!lookups?.savingProducts.length && <p className="svc-empty">가입할 수 있는 상품이 없습니다.</p>}
    </section>}

    {tab === "loans" && <>
      {loans.length > 0 && <section className="svc-list"><h3 className="svc-section-title">이용 중인 대출</h3>{loans.map((loan) => <article key={loan.loan_id} className="svc-row-card">
        <div><small>대출 #{loan.loan_id} · {loan.product_code}</small><b>남은 원금</b></div>
        <strong>{won(loan.outstanding_balance, 2)} <small>{loan.quote_currency}</small></strong>
        <div className="svc-row-actions"><button type="button" onClick={() => toggle({ kind: "repay", key: String(loan.loan_id) })}>상환</button><button type="button" disabled={kit.busy} onClick={() => void kit.run("대출연장", { loan: String(loan.loan_id) }).then((next) => { setOpen({ kind: "repay", key: String(loan.loan_id) }); setResult(next); })}>만기 연장</button></div>
        {open?.kind === "repay" && open.key === String(loan.loan_id) && <form className="svc-inline" onSubmit={(event) => submit(event, "대출상환", { product: loan.product_code, amount: form.amount ?? "", loan: String(loan.loan_id) })}>
          {field("amount", `상환액 (${loan.quote_currency})`, { required: true, inputMode: "decimal" })}
          <div className="svc-chips"><button type="button" onClick={() => setForm({ ...form, amount: String(loan.outstanding_balance) })}>전액 상환</button></div>
          <button className="svc-submit" disabled={kit.busy}>상환하기</button>
          <Outcome result={result} />
        </form>}
      </article>)}</section>}
      <h3 className="svc-section-title">대출 상품 <small>내 신용점수 {creditScore}점</small></h3>
      <section className="svc-products">{(lookups?.loanProducts ?? []).map((product) => {
        const eligible = creditScore >= product.min_credit_score;
        return <article key={product.code} className={open?.key === product.code ? "svc-product open" : "svc-product"}>
          <small>{eligible ? "신청 가능" : `신용 ${product.min_credit_score}점 이상`}</small>
          <h3>{product.name}</h3>
          <p className="svc-rate">연 <b>{product.interest_rate}</b>%</p>
          <ul><li>한도 {won(product.loan_limit)} {homeCurrency}</li><li>만기 {product.maturity_days}일</li></ul>
          <button type="button" className="svc-submit" disabled={!eligible} onClick={() => toggle({ kind: "borrow", key: product.code })}>{open?.key === product.code ? "닫기" : "대출 신청"}</button>
          {open?.kind === "borrow" && open.key === product.code && <form className="svc-inline" onSubmit={(event) => submit(event, "대출신청", { product: product.code, amount: form.amount ?? "" })}>
            {field("amount", "빌릴 금액", { required: true, inputMode: "decimal", placeholder: `최대 ${won(product.loan_limit)}` })}
            <p className="svc-hint">소득 대비 상환 부담(DTI·DSR)을 함께 심사합니다.</p>
            <button className="svc-submit" disabled={kit.busy}>신청하기</button>
            <Outcome result={result} />
          </form>}
        </article>;
      })}</section>
    </>}
  </div>;
}
