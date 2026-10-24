import { useState } from "react";
import type { FormEvent } from "react";
import type { Result } from "../commandKit";
import { won, type ServiceProps } from "./ServiceKit";

const QUICK = [10_000, 50_000, 100_000, 1_000_000];

/** 계좌이체 as an internet-banking screen: enter → confirm → receipt. Runs the same 송금 as Discord. */
export function TransferPage({ kit, cash, homeCurrency, homeDigits, accounts }: ServiceProps) {
  const [step, setStep] = useState<"input" | "confirm" | "done">("input");
  const [source, setSource] = useState("");
  const [recipient, setRecipient] = useState("");
  const [amount, setAmount] = useState("");
  const [memo, setMemo] = useState("");
  const [result, setResult] = useState<Result | null>(null);

  const active = accounts.filter((account) => account.resolution_status === "active");
  const from = active.find((account) => account.number === source);
  const balance = from ? from.balance : cash;
  const currency = from?.currency_code ?? homeCurrency;
  const digits = from?.decimal_places ?? homeDigits;
  const value = Number(amount) || 0;
  const own = active.find((account) => account.number === recipient.trim());

  function next(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (value <= 0) return;
    setStep("confirm");
  }
  async function send() {
    const outcome = await kit.run("송금", { recipient: recipient.trim(), amount, source }, false);
    if (!outcome) return;
    setResult(outcome);
    setStep("done");
  }
  function reset() { setStep("input"); setRecipient(""); setAmount(""); setMemo(""); setResult(null); }

  return <div className="svc-transfer">
    <ol className="svc-steps" aria-label="이체 단계">{["정보 입력", "내용 확인", "이체 완료"].map((label, index) => <li key={label} aria-current={["input", "confirm", "done"][index] === step ? "step" : undefined}><i>{index + 1}</i>{label}</li>)}</ol>
    {step === "input" && <form className="svc-transfer-grid" onSubmit={next}>
      <section className="svc-card">
        <header><h3>출금 정보</h3></header>
        <label className="svc-field">출금 계좌<select value={source} onChange={(event) => setSource(event.target.value)}>
          <option value="">내 지갑 · {won(cash, homeDigits)} {homeCurrency}</option>
          {active.map((account) => <option key={account.number} value={account.number}>{account.product_name} {account.number} · {won(account.balance, account.decimal_places)} {account.currency_code}</option>)}
        </select></label>
        <div className="svc-balance"><span>출금 가능 금액</span><b>{won(balance, digits)} <small>{currency}</small></b></div>
      </section>
      <section className="svc-card">
        <header><h3>입금 정보</h3></header>
        <label className="svc-field">받는 분 (아이디 또는 계좌번호)<input required value={recipient} onChange={(event) => setRecipient(event.target.value)} placeholder="예: 1234567890 또는 사용자 ID" /></label>
        {active.length > 0 && <div className="svc-chips" aria-label="내 계좌로 보내기"><small>내 계좌</small>{active.filter((account) => account.number !== source).map((account) => <button type="button" key={account.number} className={recipient === account.number ? "active" : ""} onClick={() => setRecipient(account.number)}>{account.product_name} · {account.currency_code}</button>)}</div>}
        {own && own.currency_code !== currency && <p className="svc-hint">외환통장으로 보내면 현재 환율로 {own.currency_code} 환전되어 입금됩니다.</p>}
        <label className="svc-field">이체 금액<div className="svc-amount"><input required inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value.replace(/[^\d.]/g, ""))} placeholder="0" /><span>{currency}</span></div></label>
        <div className="svc-chips">{QUICK.map((step) => <button type="button" key={step} onClick={() => setAmount(String(value + step))}>+{won(step)}</button>)}<button type="button" onClick={() => setAmount(String(balance))}>전액</button><button type="button" onClick={() => setAmount("")}>정정</button></div>
        {value > balance && <p className="svc-hint warn">출금 가능 금액보다 많습니다.</p>}
        <label className="svc-field">받는 분 통장 표시 (선택)<input maxLength={20} value={memo} onChange={(event) => setMemo(event.target.value)} placeholder="화면 기록용 메모" /></label>
      </section>
      <div className="svc-actions"><button className="svc-submit" disabled={!recipient.trim() || value <= 0}>다음</button></div>
    </form>}
    {step === "confirm" && <section className="svc-card svc-confirm">
      <header><h3>이체 내용을 확인하세요</h3><p>이체 후에는 취소할 수 없습니다.</p></header>
      <dl>
        <div><dt>출금 계좌</dt><dd>{from ? `${from.product_name} ${from.number}` : "내 지갑"}</dd></div>
        <div><dt>받는 분</dt><dd>{own ? `내 ${own.product_name} ${own.number}` : recipient}</dd></div>
        <div><dt>이체 금액</dt><dd className="svc-big">{won(value, digits)} {currency}</dd></div>
        {memo && <div><dt>메모</dt><dd>{memo}</dd></div>}
        <div><dt>이체 후 잔액</dt><dd>{won(balance - value, digits)} {currency}</dd></div>
      </dl>
      <div className="svc-actions"><button type="button" onClick={() => setStep("input")}>수정</button><button type="button" className="svc-submit" disabled={kit.busy} onClick={() => void send()}>{kit.busy ? "이체 중…" : "이체하기"}</button></div>
    </section>}
    {step === "done" && result && <section className={result.success ? "svc-card svc-receipt ok" : "svc-card svc-receipt failed"}>
      <span className="svc-receipt-icon" aria-hidden="true">{result.success ? "✓" : "!"}</span>
      <h3>{result.success ? "이체가 완료되었습니다" : "이체하지 못했습니다"}</h3>
      <p>{result.message}</p>
      <dl><div><dt>받는 분</dt><dd>{recipient}</dd></div><div><dt>금액</dt><dd>{won(value, digits)} {currency}</dd></div><div><dt>처리 시각</dt><dd>{new Date(result.time).toLocaleString("ko-KR")}</dd></div></dl>
      <div className="svc-actions"><button type="button" onClick={reset}>추가 이체</button><a className="svc-link" href="/">HTS로</a></div>
    </section>}
  </div>;
}
