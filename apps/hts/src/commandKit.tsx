import { formatMoney, minorMoney, type Money } from "../../shared/money";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../../shared/api";

export type Field = { key: string; label: string; kind: string; optional: boolean; hint: string; source: string | null };
export type Command = { name: string; group: string; description: string; guide: string; fields: Field[]; job: boolean };
export type Option = { value: string; label: string; detail?: string };
export type Lookups = {
  savingProducts: { code: string; name: string; interest_rate: number; deposit_amount: Money; maturity_days: number; quote_currency: string; product_kind: string }[];
  loanProducts: { code: string; name: string; interest_rate: number; loan_limit: Money; maturity_days: number; min_credit_score: number }[];
  accounts: { account_number: string; product_code: string; account_type: string; quote_currency: string; balance: Money | null; balance_units: Money | null }[];
  loans: { loan_id: number; product_code: string; outstanding_balance: Money; quote_currency: string }[];
  futures: { code: string; name: string }[];
  countries: string[];
};
export type Result = { id: number; command: string; message: string; data: unknown; success: boolean; time: number };
export type Instrument = { code: string; name: string; type: string; price: Money; quote_currency?: string };
export type Portfolio = { stocks: { code: string; name: string; qty: number }[] };
export type Order = { order_id: number; asset_code: string; side: string; limit_price: Money; remaining_quantity: number; status: string };

export const READ_ONLY = new Set(["내주문", "호가", "옵션체인", "내복권", "복권결과", "복권규칙", "법정최고이자", "재무"]);
export const OPEN = new Set(["open", "partial", "scheduled"]);

// Korean names for the columns that come back from the game's managers.
const LABELS: Record<string, string> = {
  user_id: "사용자 ID", sender_id: "보낸 사용자 ID", recipient_id: "받는 사용자 ID",
  order_id: "주문", asset_code: "종목", side: "구분", limit_price: "지정가", quantity: "수량", remaining_quantity: "잔량", filled_quantity: "체결",
  status: "상태", created_epoch: "시각", updated_epoch: "변경", quote_currency: "통화", reserved_cash: "예치금", bids: "매수 호가", asks: "매도 호가",
  order_count: "주문 수", round_id: "회차", close_epoch: "판매 마감", draw_epoch: "추첨", ticket_price: "가격", max_tickets: "1인 최대", prize_percent: "상금 비율 %",
  treasury_percent: "국고 비율 %", numbers: "번호", winning_numbers: "당첨 번호", bonus_number: "보너스", rank: "등수", gross: "당첨금", tax: "세금", net: "실수령",
  ticket_id: "복권", purchased_epoch: "구매 시각", contract_code: "계약", underlying_code: "기초자산", option_type: "유형", strike_price: "행사가",
  expiration_epoch: "만기", implied_volatility: "변동성", outstanding_contracts: "미결제", issued_contracts: "발행", writer_id: "작성자",
  code: "코드", name: "이름", currency: "통화", period: "기간", balances: "재무상태", income: "손익", ratios: "비율", cash: "현금", places: "소수 자릿수",
  opened_epoch: "시작", price: "가격", total: "합계", commitment: "추첨 검증값", carryover: "이월금", pool: "상금 풀",
};
export const VALUES: Record<string, string> = { buy: "매수", sell: "매도", open: "미체결", partial: "부분 체결", scheduled: "예약", filled: "체결", cancelled: "취소", expired: "만료", selling: "판매 중", settled: "추첨 완료", drawn: "추첨됨", call: "콜", put: "풋", active: "진행 중" };
export const number = formatMoney;
/** A bank account's balance; foreign-currency savings are kept in millionths. */
export const accountBalance = (account: Lookups["accounts"][number]) => account.account_type === "currency_saving" ? minorMoney(account.balance_units ?? 0, 6) : account.balance ?? 0;
/** Every account is a savings account; it reads as "외환통장" only to an owner whose currency differs. */
export const accountLabel = (currency: string | null | undefined, homeCurrency: string, home = "통장") => currency && currency !== homeCurrency ? "외환통장" : home;
export const plain = (text: string) => text.replace(/<t:(\d+)(?::[a-zA-Z])?>/g, (_m, epoch: string) => new Date(Number(epoch) * 1000).toLocaleString("ko-KR")).replace(/\*\*/g, "").replace(/`/g, "");

function cell(key: string, value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return <span className="muted">—</span>;
  if (typeof value === "boolean") return value ? "예" : "아니요";
  if (typeof value === "number") return key.endsWith("_epoch") && value > 1e9 ? new Date(value * 1000).toLocaleString("ko-KR") : number(value);
  if (Array.isArray(value) && value.every((item) => typeof item !== "object")) return <span className="chips">{value.map((item, index) => <span key={index} className="chip">{String(item)}</span>)}</span>;
  if (typeof value === "string") {
    if (/(?:amount|cash|balance|price|premium|notional|value|total|principal|fee|tax|profit|capital|revenue|limit|cost|reserve)(?:_|$)/i.test(key) && /^[+-]?\d+(?:\.\d+)?$/.test(value)) return number(value);
    if (value.startsWith("[") && value.endsWith("]")) { try { return cell(key, JSON.parse(value)); } catch { /* plain text */ } }
    return VALUES[value] ?? value;
  }
  return <DataView data={value} />;
}

/** Records as tables and objects as label/value grids, recursively. */
export function DataView({ data }: { data: unknown }) {
  if (Array.isArray(data)) {
    if (!data.length) return <p className="result-empty">결과가 없습니다.</p>;
    if (data.every((row) => row && typeof row === "object" && !Array.isArray(row))) {
      const columns = Array.from(new Set(data.flatMap((row) => Object.keys(row as object)))).filter((key) => !["password"].includes(key)).slice(0, 9);
      return <div className="result-table"><table><thead><tr>{columns.map((key) => <th key={key}>{LABELS[key] ?? key}</th>)}</tr></thead>
        <tbody>{data.map((row, index) => <tr key={index}>{columns.map((key) => <td key={key} className={key === "side" ? ((row as Record<string, unknown>)[key] === "buy" ? "buy-text" : "sell-text") : undefined}>{cell(key, (row as Record<string, unknown>)[key])}</td>)}</tr>)}</tbody></table></div>;
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
export function Picker({ value, onChange, options, placeholder, required }: { value: string; onChange: (value: string) => void; options: Option[]; placeholder?: string; required: boolean }) {
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

/** Command catalog, lookups and execution for the command pages; requests match the Discord commands. */
export function useCommands({ instruments, portfolio, orders, homeCurrency, onDone }: { instruments: Instrument[]; portfolio: Portfolio | null; orders: Order[]; homeCurrency: string; onDone: () => void }) {
  const [commands, setCommands] = useState<Command[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [lookups, setLookups] = useState<Lookups | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => api<{ commands: Command[]; lookups: Lookups }>("/api/hts/commands").then((result) => {
    setCommands(result.commands);
    setLookups(result.lookups);
    setLoaded(true);
  }).catch((cause) => { setLoaded(true); setError(cause instanceof Error ? cause.message : "명령어 목록을 불러오지 못했습니다."); }), []);
  useEffect(() => { void load(); }, [load]);

  const sources = useMemo<Record<string, Option[]>>(() => {
    const instrumentOptions = instruments.map((item) => ({ value: item.code, label: item.name, detail: `${number(item.price)} ${item.quote_currency || homeCurrency}` }));
    return {
      instruments: instrumentOptions,
      stocks: instrumentOptions.filter((_, index) => instruments[index].type === "stock"),
      holdings: (portfolio?.stocks ?? []).filter((item) => item.qty).map((item) => ({ value: item.code, label: item.name, detail: `보유 ${number(item.qty)}` })),
      orders: orders.filter((order) => OPEN.has(order.status)).map((order) => ({ value: String(order.order_id), label: `#${order.order_id} ${order.asset_code} ${order.side === "buy" ? "매수" : "매도"}`, detail: `${number(order.limit_price)} × 잔량 ${number(order.remaining_quantity)}` })),
      accounts: (lookups?.accounts ?? []).map((account) => ({ value: account.account_number, label: `${account.account_type === "currency_saving" ? "외환통장" : "통장"} ${account.product_code}`, detail: `${number(accountBalance(account))} ${account.quote_currency}` })),
      saving_products: (lookups?.savingProducts ?? []).map((item) => ({ value: item.code, label: `${item.product_kind === "currency_saving" ? "외환통장" : "저축"} · ${item.name}`, detail: item.product_kind === "currency_saving" ? `${item.quote_currency} · 연 ${item.interest_rate}%` : `연 ${item.interest_rate}% · 최소 ${number(item.deposit_amount)} · ${item.maturity_days}일` })),
      loan_products: (lookups?.loanProducts ?? []).map((item) => ({ value: item.code, label: item.name, detail: `연 ${item.interest_rate}% · 한도 ${number(item.loan_limit)} · 신용 ${item.min_credit_score}+` })),
      loans: (lookups?.loans ?? []).map((loan) => ({ value: String(loan.loan_id), label: `대출 #${loan.loan_id} · ${loan.product_code}`, detail: `잔액 ${number(loan.outstanding_balance)} ${loan.quote_currency}` })),
      futures: (lookups?.futures ?? []).map((item) => ({ value: item.code, label: item.name })),
      countries: (lookups?.countries ?? []).map((country) => ({ value: country, label: country })),
    };
  }, [instruments, portfolio, orders, lookups, homeCurrency]);

  /** Runs a command; resolves to its result (null when the confirmation was declined). */
  async function execute(target: Command, input: Record<string, string>, ask = true): Promise<Result | null> {
    const summary = target.fields.filter((field) => input[field.key] && field.kind !== "password").map((field) => `${field.label}: ${input[field.key]}`).join("\n");
    if (ask && !READ_ONLY.has(target.name) && !window.confirm(`${target.description}\n${summary ? `\n${summary}\n` : ""}\n진행할까요?`)) return null;
    setBusy(true);
    setError("");
    let entry: Result;
    try {
      const result = await api<{ success: boolean; message: string; data: unknown }>("/api/hts/commands", { method: "POST", body: JSON.stringify({ command: target.name, values: input }) });
      entry = { id: Date.now(), command: target.name, message: plain(result.message).replace(/^[✅❌⏳⚠️💀🎉]\s*/u, ""), data: result.data, success: result.success, time: Date.now() };
      if (!READ_ONLY.has(target.name)) { onDone(); void load(); }
    } catch (cause) {
      entry = { id: Date.now(), command: target.name, message: cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.", data: null, success: false, time: Date.now() };
    } finally {
      setBusy(false);
    }
    return entry;
  }
  /** Runs a command by name. */
  const run = (name: string, input: Record<string, string>, ask = true) => {
    const target = commands.find((item) => item.name === name);
    return target ? execute(target, input, ask) : Promise.resolve(null);
  };

  return { commands, loaded, error, busy, sources, lookups, execute, run };
}

/** Inputs for one action's fields: option buttons, pick lists or text, as the action defines them. */
export function ActionFields({ command, values, onChange, sources, only, labels }: { command: Command; values: Record<string, string>; onChange: (next: Record<string, string>) => void; sources: Record<string, Option[]>; only?: string[]; labels?: Record<string, string> }) {
  const fields = command.fields.filter((field) => !only || only.includes(field.key));
  const input = (item: Field) => {
    const value = values[item.key] ?? "";
    const set = (next: string) => onChange({ ...values, [item.key]: next });
    if (item.kind.startsWith("select:")) {
      return <div className="cmd-choice" role="radiogroup" aria-label={item.label}>{item.kind.slice(7).split(",").map((option) => <button type="button" role="radio" aria-checked={value === option} key={option} className={value === option ? "active" : ""} onClick={() => set(option)}>{VALUES[option] ?? option}</button>)}</div>;
    }
    if (item.source === "orders" || item.source === "countries" || (item.source === "loans" && item.kind === "number")) {
      const options = sources[item.source];
      return <select value={value} required={!item.optional} onChange={(event) => set(event.target.value)}><option value="">{options.length ? (item.optional ? "선택 안 함" : "선택하세요") : "선택할 항목이 없습니다"}</option>{options.map((option) => <option key={option.value} value={option.value}>{option.label}{option.detail ? ` · ${option.detail}` : ""}</option>)}</select>;
    }
    if (item.source && sources[item.source]) return <Picker value={value} onChange={set} options={sources[item.source]} required={!item.optional} />;
    return <input type={item.kind === "password" ? "password" : "text"} inputMode={item.kind === "number" ? "decimal" : undefined} autoComplete={item.kind === "password" ? "off" : undefined} required={!item.optional} value={value} onChange={(event) => set(event.target.value)} />;
  };
  return <div className="cmd-fields">{fields.map((item) => <label key={item.key} className={item.kind.startsWith("select:") ? "wide" : undefined}>
    <span>{labels?.[item.key] ?? item.label}{item.optional && <em>선택</em>}</span>{input(item)}{item.hint && <small>{item.hint}</small>}
  </label>)}</div>;
}

/** Empty values for an action, with option fields set to their first choice. */
export const initialValues = (command: Command | undefined) => Object.fromEntries((command?.fields ?? []).map((field) => [field.key, field.kind.startsWith("select:") ? field.kind.slice(7).split(",")[0] : ""]));
