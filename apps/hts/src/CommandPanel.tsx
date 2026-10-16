import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";

type Field = { key: string; label: string; kind: string; optional: boolean; hint: string };
type Command = { name: string; group: string; description: string; fields: Field[]; job: boolean };
type Result = { command: string; message: string; success: boolean; time: number };
type Instrument = { code: string; name: string };
type Portfolio = { stocks: { code: string; name: string }[]; accounts?: Record<string, unknown>[] };
type Order = { order_id: number; asset_code: string; side: string; status: string };

// Suggestions shown under fields that take codes, accounts or orders.
const SUGGEST: Record<string, string> = {
  asset: "hts-instruments", underlying: "hts-instruments", future: "hts-instruments", code: "hts-instruments", product: "hts-instruments",
  order: "hts-orders", account: "hts-accounts", source: "hts-accounts", destination: "hts-accounts",
};
const plain = (text: string) => text.replace(/<t:(\d+)(?::[a-zA-Z])?>/g, (_m, epoch: string) => new Date(Number(epoch) * 1000).toLocaleString("ko-KR")).replace(/\*\*/g, "").replace(/`/g, "");

/** Every Discord user command as a form, run against the same game logic. */
export function CommandPanel({ initial, instruments, portfolio, orders, onClose, onDone }: { initial: string; instruments: Instrument[]; portfolio: Portfolio | null; orders: Order[]; onClose: () => void; onDone: () => void }) {
  const [commands, setCommands] = useState<Command[]>([]);
  const [name, setName] = useState(initial);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Result[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ commands: Command[] }>("/api/hts/commands").then((result) => {
      setCommands(result.commands);
      setName((current) => current || result.commands[0]?.name || "");
    }).catch((cause) => setError(cause instanceof Error ? cause.message : "명령어 목록을 불러오지 못했습니다."));
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

  async function run(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!command) return;
    const summary = command.fields.filter((field) => values[field.key] && field.kind !== "password").map((field) => `${field.label}: ${values[field.key]}`).join("\n");
    if (!["내주문", "호가", "옵션체인", "내복권", "복권결과", "복권규칙", "법정최고이자", "재무"].includes(command.name)
        && !window.confirm(`/${command.name}을(를) 실행할까요?${summary ? `\n\n${summary}` : ""}`)) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ success: boolean; message: string }>("/api/hts/commands", { method: "POST", body: JSON.stringify({ command: command.name, values }) });
      setResults((items) => [{ command: command.name, message: result.message, success: result.success, time: Date.now() }, ...items].slice(0, 15));
      setValues((current) => Object.fromEntries(Object.entries(current).map(([key, value]) => [key, command.fields.find((field) => field.key === key)?.kind === "password" ? "" : value])));
      onDone();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "명령어를 실행하지 못했습니다.";
      setResults((items) => [{ command: command.name, message, success: false, time: Date.now() }, ...items].slice(0, 15));
    } finally {
      setBusy(false);
    }
  }

  const accounts = (portfolio?.accounts ?? []).map((account) => String(account.account_number ?? "")).filter(Boolean);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="command-modal" role="dialog" aria-modal="true" aria-labelledby="command-title">
      <div className="command-head"><div><p className="eyebrow">COMMANDS</p><h2 id="command-title">명령어</h2></div><button type="button" onClick={onClose}>닫기</button></div>
      <div className="command-body">
        <nav className="command-list" aria-label="명령어 목록">{groups.map((group) => <div key={group} className="command-group"><span>{group}</span>{commands.filter((item) => item.group === group).map((item) => <button key={item.name} type="button" className={item.name === name ? "active" : ""} onClick={() => setName(item.name)}>/{item.name}</button>)}</div>)}</nav>
        <div className="command-main">
          {command && <form onSubmit={(event) => void run(event)}>
            <h3>/{command.name}</h3>
            <p className="command-description">{command.description}{command.job && " · Discord 봇이 DM으로 처리합니다."}</p>
            <div className="command-fields">{command.fields.map((field) => <label key={field.key}>{field.label}{(field.optional || field.hint) && <small>{[field.optional ? "선택" : "", field.hint].filter(Boolean).join(" · ")}</small>}
              {field.kind.startsWith("select:")
                ? <select value={values[field.key] ?? ""} onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}>{field.kind.slice(7).split(",").map((option) => <option key={option} value={option}>{option}</option>)}</select>
                : <input type={field.kind === "password" ? "password" : "text"} inputMode={field.kind === "number" ? "decimal" : undefined} autoComplete={field.kind === "password" ? "off" : undefined} list={SUGGEST[field.key]} required={!field.optional} value={values[field.key] ?? ""} onChange={(event) => setValues({ ...values, [field.key]: event.target.value })} />}
            </label>)}</div>
            {!command.fields.length && <p className="command-description">입력할 값이 없습니다. 실행을 누르세요.</p>}
            <button className="command-run" disabled={busy}>{busy ? "실행 중…" : "실행"}</button>
          </form>}
          {error && <p className="account-error" role="alert">{error}</p>}
          <div className="command-results">{results.map((item) => <div key={item.time} className={item.success ? "command-result" : "command-result failed"}><div><b>/{item.command}</b><span>{new Date(item.time).toLocaleTimeString("ko-KR")}</span></div><pre>{plain(item.message)}</pre></div>)}</div>
        </div>
      </div>
      <datalist id="hts-instruments">{[...instruments, ...(portfolio?.stocks ?? [])].map((item, index) => <option key={`${item.code}-${index}`} value={item.code}>{item.name}</option>)}</datalist>
      <datalist id="hts-orders">{orders.filter((order) => ["open", "partial", "scheduled"].includes(order.status)).map((order) => <option key={order.order_id} value={String(order.order_id)}>{order.asset_code} {order.side === "buy" ? "매수" : "매도"}</option>)}</datalist>
      <datalist id="hts-accounts">{accounts.map((account) => <option key={account} value={account} />)}</datalist>
    </section>
  </div>;
}
