import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "../../shared/api";

type Field = { key: string; label: string; kind: string; optional: boolean; hint: string; source: string | null };
type Command = { name: string; group: string; description: string; guide: string; fields: Field[]; job: boolean };
type Option = { value: string; label: string; detail?: string };
type Lookups = {
  savingProducts: { code: string; name: string; interest_rate: number; deposit_amount: number; maturity_days: number; quote_currency: string; product_kind: string }[];
  loanProducts: { code: string; name: string; interest_rate: number; loan_limit: number; maturity_days: number; min_credit_score: number }[];
  accounts: { account_number: string; product_code: string; account_type: string; quote_currency: string; balance: number | null; balance_units: number | null }[];
  loans: { loan_id: number; product_code: string; outstanding_balance: number; quote_currency: string }[];
  futures: { code: string; name: string }[];
  countries: string[];
};
type Result = { id: number; command: string; message: string; data: unknown; success: boolean; time: number };
type Instrument = { code: string; name: string; type: string; price: number; quote_currency?: string };
type Portfolio = { stocks: { code: string; name: string; qty: number }[] };
type Order = { order_id: number; asset_code: string; side: string; limit_price: number; remaining_quantity: number; status: string };

const GROUP_ICON: Record<string, string> = { 거래: "📈", 돈: "💸", 은행: "🏦", 파생: "🧮", 복권: "🎟️", 정보: "📊", 요청: "📨" };
const READ_ONLY = new Set(["내주문", "호가", "옵션체인", "내복권", "복권결과", "복권규칙", "법정최고이자", "재무"]);
const OPEN = new Set(["open", "partial", "scheduled"]);

// Korean names for the columns that come back from the game's managers.
const LABELS: Record<string, string> = {
  order_id: "주문", asset_code: "종목", side: "구분", limit_price: "지정가", quantity: "수량", remaining_quantity: "잔량", filled_quantity: "체결",
  status: "상태", created_epoch: "시각", updated_epoch: "변경", quote_currency: "통화", reserved_cash: "예치금", bids: "매수 호가", asks: "매도 호가",
  order_count: "주문 수", round_id: "회차", close_epoch: "판매 마감", draw_epoch: "추첨", ticket_price: "가격", max_tickets: "1인 최대", prize_percent: "상금 비율 %",
  treasury_percent: "국고 비율 %", numbers: "번호", winning_numbers: "당첨 번호", bonus_number: "보너스", rank: "등수", gross: "당첨금", tax: "세금", net: "실수령",
  ticket_id: "복권", purchased_epoch: "구매 시각", contract_code: "계약", underlying_code: "기초자산", option_type: "유형", strike_price: "행사가",
  expiration_epoch: "만기", implied_volatility: "변동성", outstanding_contracts: "미결제", issued_contracts: "발행", writer_id: "작성자",
  code: "코드", name: "이름", currency: "통화", period: "기간", balances: "재무상태", income: "손익", ratios: "비율", cash: "현금", places: "소수 자릿수",
  opened_epoch: "시작", price: "가격", total: "합계", commitment: "추첨 검증값", carryover: "이월금", pool: "상금 풀",
};
const VALUES: Record<string, string> = { buy: "매수", sell: "매도", open: "미체결", partial: "부분 체결", scheduled: "예약", filled: "체결", cancelled: "취소", expired: "만료", selling: "판매 중", settled: "추첨 완료", drawn: "추첨됨", call: "콜", put: "풋", active: "진행 중" };
const number = (value: number) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 6 }).format(value);
const plain = (text: string) => text.replace(/<t:(\d+)(?::[a-zA-Z])?>/g, (_m, epoch: string) => new Date(Number(epoch) * 1000).toLocaleString("ko-KR")).replace(/\*\*/g, "").replace(/`/g, "");

function cell(key: string, value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return <span className="muted">—</span>;
  if (typeof value === "boolean") return value ? "예" : "아니요";
  if (typeof value === "number") return key.endsWith("_epoch") && value > 1e9 ? new Date(value * 1000).toLocaleString("ko-KR") : number(value);
  if (Array.isArray(value) && value.every((item) => typeof item !== "object")) return <span className="chips">{value.map((item, index) => <span key={index} className="chip">{String(item)}</span>)}</span>;
  if (typeof value === "string") {
    if (value.startsWith("[") && value.endsWith("]")) { try { return cell(key, JSON.parse(value)); } catch { /* plain text */ } }
    return VALUES[value] ?? value;
  }
  return <DataView data={value} />;
}

/** Records as tables and objects as label/value grids, recursively. */
function DataView({ data, actions }: { data: unknown; actions?: (row: Record<string, unknown>) => ReactNode }) {
  if (Array.isArray(data)) {
    if (!data.length) return <p className="result-empty">결과가 없습니다.</p>;
    if (data.every((row) => row && typeof row === "object" && !Array.isArray(row))) {
      const columns = Array.from(new Set(data.flatMap((row) => Object.keys(row as object)))).filter((key) => !["user_id", "password"].includes(key)).slice(0, 9);
      return <div className="result-table"><table><thead><tr>{columns.map((key) => <th key={key}>{LABELS[key] ?? key}</th>)}{actions && <th />}</tr></thead>
        <tbody>{data.map((row, index) => <tr key={index}>{columns.map((key) => <td key={key} className={key === "side" ? ((row as Record<string, unknown>)[key] === "buy" ? "buy-text" : "sell-text") : undefined}>{cell(key, (row as Record<string, unknown>)[key])}</td>)}{actions && <td>{actions(row as Record<string, unknown>)}</td>}</tr>)}</tbody></table></div>;
    }
    return <>{cell("", data)}</>;
  }
  if (data && typeof data === "object") {
    const entries = Object.entries(data as Record<string, unknown>);
    const simple = entries.filter(([, value]) => value === null || typeof value !== "object" || (Array.isArray(value) && value.every((item) => typeof item !== "object")));
    const nested = entries.filter((entry) => !simple.includes(entry));
    return <div className="result-object">
      {simple.length > 0 && <dl className="result-grid">{simple.map(([key, value]) => <div key={key}><dt>{LABELS[key] ?? key}</dt><dd>{cell(key, value)}</dd></div>)}</dl>}
      {nested.map(([key, value]) => <section key={key} className="result-section"><h4>{LABELS[key] ?? key}</h4><DataView data={value} /></section>)}
    </div>;
  }
  return <>{cell("", data)}</>;
}

/** Searchable list with free text: suggestions never block typing an ID by hand. */
function Picker({ value, onChange, options, placeholder, required }: { value: string; onChange: (value: string) => void; options: Option[]; placeholder?: string; required: boolean }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const term = value.trim().toLocaleLowerCase();
  const matches = options.filter((option) => !term || `${option.value} ${option.label} ${option.detail ?? ""}`.toLocaleLowerCase().includes(term)).slice(0, 40);
  const chosen = options.find((option) => option.value === value);
  useEffect(() => {
    const close = (event: MouseEvent) => { if (!wrap.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  return <div className="picker" ref={wrap}>
    <input value={value} required={required} placeholder={placeholder ?? (options.length ? "검색하거나 직접 입력" : "직접 입력")} onFocus={() => setOpen(true)} onChange={(event) => { onChange(event.target.value); setOpen(true); }} onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }} />
    {chosen && !open && <span className="picker-chosen">{chosen.label}{chosen.detail ? ` · ${chosen.detail}` : ""}</span>}
    {open && matches.length > 0 && <div className="picker-list" role="listbox">{matches.map((option) => <button type="button" role="option" aria-selected={option.value === value} key={option.value} onMouseDown={(event) => event.preventDefault()} onClick={() => { onChange(option.value); setOpen(false); }}><b>{option.label}</b><span>{option.value}{option.detail ? ` · ${option.detail}` : ""}</span></button>)}</div>}
  </div>;
}

/** Every Discord user command as a form, with readable results. */
export function CommandPanel({ initial, instruments, portfolio, orders, homeCurrency, onClose, onDone }: { initial: string; instruments: Instrument[]; portfolio: Portfolio | null; orders: Order[]; homeCurrency: string; onClose: () => void; onDone: () => void }) {
  const [commands, setCommands] = useState<Command[]>([]);
  const [lookups, setLookups] = useState<Lookups | null>(null);
  const [name, setName] = useState(initial);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Result[]>([]);
  const [error, setError] = useState("");

  const load = () => api<{ commands: Command[]; lookups: Lookups }>("/api/hts/commands").then((result) => {
    setCommands(result.commands);
    setLookups(result.lookups);
    setName((current) => current || result.commands[0]?.name || "");
  }).catch((cause) => setError(cause instanceof Error ? cause.message : "명령어 목록을 불러오지 못했습니다."));

  useEffect(() => {
    void load();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const command = commands.find((item) => item.name === name);
  useEffect(() => {
    setValues(Object.fromEntries((command?.fields ?? []).map((field) => [field.key, field.kind.startsWith("select:") ? field.kind.slice(7).split(",")[0] : ""])));
    setError("");
  }, [command]);
  const groups = useMemo(() => Array.from(new Set(commands.map((item) => item.group))), [commands]);

  const sources = useMemo<Record<string, Option[]>>(() => {
    const instrumentOptions = instruments.map((item) => ({ value: item.code, label: item.name, detail: `${number(item.price)} ${item.quote_currency || "NWX"}` }));
    return {
      instruments: instrumentOptions,
      stocks: instruments.filter((item) => item.type === "stock").map((item) => ({ value: item.code, label: item.name, detail: `${number(item.price)} ${item.quote_currency || "NWX"}` })),
      holdings: (portfolio?.stocks ?? []).filter((item) => item.qty).map((item) => ({ value: item.code, label: item.name, detail: `보유 ${number(item.qty)}` })),
      orders: orders.filter((order) => OPEN.has(order.status)).map((order) => ({ value: String(order.order_id), label: `#${order.order_id} ${order.asset_code} ${order.side === "buy" ? "매수" : "매도"}`, detail: `${number(order.limit_price)} × 잔량 ${number(order.remaining_quantity)}` })),
      accounts: (lookups?.accounts ?? []).map((account) => ({ value: account.account_number, label: `${account.account_type === "currency_saving" ? "외환통장" : "통장"} ${account.product_code}`, detail: account.account_type === "currency_saving" ? `${number((account.balance_units ?? 0) / 1_000_000)} ${account.quote_currency}` : `${number(account.balance ?? 0)} ${account.quote_currency}` })),
      saving_products: (lookups?.savingProducts ?? []).map((item) => ({ value: item.code, label: `${item.product_kind === "currency_saving" ? "외환통장" : "저축"} · ${item.name}`, detail: item.product_kind === "currency_saving" ? `${item.quote_currency} · 연 ${item.interest_rate}%` : `연 ${item.interest_rate}% · 최소 ${number(item.deposit_amount)} · ${item.maturity_days}일` })),
      loan_products: (lookups?.loanProducts ?? []).map((item) => ({ value: item.code, label: item.name, detail: `연 ${item.interest_rate}% · 한도 ${number(item.loan_limit)} · 신용 ${item.min_credit_score}+` })),
      loans: (lookups?.loans ?? []).map((loan) => ({ value: String(loan.loan_id), label: `대출 #${loan.loan_id} · ${loan.product_code}`, detail: `잔액 ${number(loan.outstanding_balance)} ${loan.quote_currency}` })),
      futures: (lookups?.futures ?? []).map((item) => ({ value: item.code, label: item.name })),
      countries: (lookups?.countries ?? []).map((country) => ({ value: country, label: country })),
    };
  }, [instruments, portfolio, orders, lookups]);

  async function execute(target: Command, input: Record<string, string>, ask = true) {
    const summary = target.fields.filter((field) => input[field.key] && field.kind !== "password").map((field) => `${field.label}: ${input[field.key]}`).join("\n");
    if (ask && !READ_ONLY.has(target.name) && !window.confirm(`/${target.name}을(를) 실행할까요?${summary ? `\n\n${summary}` : ""}`)) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ success: boolean; message: string; data: unknown }>("/api/hts/commands", { method: "POST", body: JSON.stringify({ command: target.name, values: input }) });
      setResults((items) => [{ id: Date.now(), command: target.name, message: result.message, data: result.data, success: result.success, time: Date.now() }, ...items].slice(0, 12));
      if (!READ_ONLY.has(target.name)) { onDone(); void load(); }
    } catch (cause) {
      setResults((items) => [{ id: Date.now(), command: target.name, message: cause instanceof Error ? cause.message : "명령어를 실행하지 못했습니다.", data: null, success: false, time: Date.now() }, ...items].slice(0, 12));
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!command) return;
    void execute(command, values).then(() => setValues((current) => Object.fromEntries(Object.entries(current).map(([key, value]) => [key, command.fields.find((field) => field.key === key)?.kind === "password" ? "" : value]))));
  }

  const cancelCommand = commands.find((item) => item.name === "주문취소");
  const field = (item: Field) => {
    const value = values[item.key] ?? "";
    const set = (next: string) => setValues({ ...values, [item.key]: next });
    if (item.kind.startsWith("select:")) {
      return <div className="choice" role="radiogroup" aria-label={item.label}>{item.kind.slice(7).split(",").map((option) => <button type="button" role="radio" aria-checked={value === option} key={option} className={value === option ? "active" : ""} onClick={() => set(option)}>{VALUES[option] ?? option}<small>{option}</small></button>)}</div>;
    }
    if (item.source === "orders" || item.source === "countries" || (item.source === "loans" && item.kind === "number")) {
      const options = sources[item.source];
      return <select value={value} required={!item.optional} onChange={(event) => set(event.target.value)}><option value="">{options.length ? (item.optional ? "선택 안 함" : "선택하세요") : "선택할 항목이 없습니다"}</option>{options.map((option) => <option key={option.value} value={option.value}>{option.label}{option.detail ? ` · ${option.detail}` : ""}</option>)}</select>;
    }
    if (item.source && sources[item.source]) return <Picker value={value} onChange={set} options={sources[item.source]} required={!item.optional} />;
    return <input type={item.kind === "password" ? "password" : "text"} inputMode={item.kind === "number" ? "decimal" : undefined} autoComplete={item.kind === "password" ? "off" : undefined} required={!item.optional} value={value} onChange={(event) => set(event.target.value)} />;
  };

  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="command-modal" role="dialog" aria-modal="true" aria-labelledby="command-title">
      <div className="command-head"><div><p className="eyebrow">COMMANDS · 금액 기준 {homeCurrency}</p><h2 id="command-title">명령어</h2></div><button type="button" onClick={onClose}>닫기</button></div>
      <div className="command-body">
        <nav className="command-list" aria-label="명령어 목록">{groups.map((group) => <div key={group} className="command-group"><span>{GROUP_ICON[group]} {group}</span>{commands.filter((item) => item.group === group).map((item) => <button key={item.name} type="button" className={item.name === name ? "active" : ""} onClick={() => setName(item.name)}><b>{item.name}</b><small>{item.description}</small></button>)}</div>)}</nav>
        <div className="command-main">
          {command && <form onSubmit={submit} className="command-form">
            <div className="command-title"><span className="command-icon" aria-hidden="true">{GROUP_ICON[command.group]}</span><div><h3>{command.name}{command.guide && <span className="info-tip"><button type="button" className="info-icon" aria-label="설명 보기">i</button><span className="info-bubble" role="tooltip">{command.guide}</span></span>}</h3><p>{command.description}</p></div>{command.job && <span className="tag">Discord 봇 처리</span>}</div>
            {command.fields.length > 0 && <div className="command-fields">{command.fields.map((item) => <label key={item.key} className={item.kind.startsWith("select:") ? "wide" : undefined}><span className="field-label">{item.label}{item.optional && <em>선택</em>}</span>{field(item)}{item.hint && <small>{item.hint}</small>}</label>)}</div>}
            <div className="command-actions"><button className="command-run" disabled={busy}>{busy ? "실행 중…" : READ_ONLY.has(command.name) ? "조회" : "실행"}</button>{!command.fields.length && <span className="hint">입력할 값이 없어요.</span>}</div>
          </form>}
          {error && <p className="account-error" role="alert">{error}</p>}
          <div className="command-results" aria-live="polite">{results.map((item) => <article key={item.id} className={item.success ? "result-card ok" : "result-card failed"}>
            <header><span className="result-icon" aria-hidden="true">{item.success ? (item.message.startsWith("⏳") ? "⏳" : "✓") : "!"}</span><b>{item.command}</b><time>{new Date(item.time).toLocaleTimeString("ko-KR")}</time></header>
            {item.data !== null && item.data !== undefined
              ? <DataView data={item.data} actions={item.command === "내주문" && cancelCommand ? (row) => OPEN.has(String(row.status)) ? <button type="button" className="cancel-button" disabled={busy} onClick={() => void execute(cancelCommand, { order: String(row.order_id) })}>취소</button> : null : undefined} />
              : <p className="result-message">{plain(item.message).replace(/^[✅❌⏳⚠️💀🎉]\s*/u, "")}</p>}
          </article>)}{!results.length && <p className="result-empty">실행 결과가 여기에 표시됩니다.</p>}</div>
        </div>
      </div>
    </section>
  </div>;
}
