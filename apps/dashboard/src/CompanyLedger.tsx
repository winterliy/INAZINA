import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Field, Tabs, date, errorText, number, plain, type Ctx } from "./common";

type Account = { code: string; name: string; category: string; protected: boolean };
type Line = { account: string; amount: number };
type Entry = { id: number; epoch: number; description: string; flow: string; manual: boolean; reversalOf: number | null; reversedBy: number | null; lines: Line[] };
type Profile = { enabled: boolean; revenue: number; cogs: number; sga: number; depreciation: number; tax_percent: string; start_period: string };
type Journal = { company: string; period: string; currency: string; places: number; accounts: Account[]; entries: Entry[]; profile: Profile | null; closed: string[] };
type Row = { account: string; side: "debit" | "credit"; amount: string };
type Tab = "entry" | "history" | "operating" | "close";

const CATEGORY: Record<string, string> = { current_asset: "유동자산", noncurrent_asset: "비유동자산", current_liability: "유동부채", noncurrent_liability: "비유동부채", equity: "자본", revenue: "수익", expense: "비용" };
const FLOW: [string, string][] = [["none", "현금 없음 (none)"], ["operating", "영업활동 (operating)"], ["investing", "투자활동 (investing)"], ["financing", "재무활동 (financing)"]];
const FLOW_NAME: Record<string, string> = { none: "—", opening: "기초", operating: "영업", investing: "투자", financing: "재무", adjustment: "조정" };
const AMOUNTS = [["revenue", "월 매출액"], ["cogs", "월 매출원가"], ["sga", "월 판매비와관리비"], ["depreciation", "월 감가상각비"]] as const;
const PROFILE_FIELDS = [...AMOUNTS, ["tax_percent", "법인세율 %"]] as const;
const blank = (): Row[] => [{ account: "", side: "debit", amount: "" }, { account: "", side: "credit", amount: "" }];
const thisMonth = () => new Date(Date.now() + 9 * 3600_000).toISOString().slice(0, 7);
const lastMonth = () => { const now = new Date(Date.now() + 9 * 3600_000); now.setUTCDate(1); now.setUTCMonth(now.getUTCMonth() - 1); return now.toISOString().slice(0, 7); };

/**
 * Edits a company's statements the way an accountant does: balanced adjusting entries,
 * reversals of earlier administrator entries, the monthly operating profile and month close.
 * Automatic and settlement-linked accounts stay with their source commands.
 */
export function CompanyLedger({ code, ctx, onChanged }: { code: string; ctx: Ctx; onChanged: () => void }) {
  const { busy, run, say } = ctx;
  const [tab, setTab] = useState<Tab>("entry");
  const [period, setPeriod] = useState(thisMonth);
  const [journal, setJournal] = useState<Journal | null>(null);
  const [description, setDescription] = useState("");
  const [flow, setFlow] = useState("none");
  const [rows, setRows] = useState<Row[]>(blank);
  const [profile, setProfile] = useState<Record<string, string>>({});
  const [enabled, setEnabled] = useState(false);
  const [closing, setClosing] = useState(lastMonth);

  const load = useCallback(async (target: string) => {
    const result = await api<Journal>(`/api/admin/companies/${encodeURIComponent(code)}/journal?period=${encodeURIComponent(target)}`);
    setJournal(result);
    const scale = 10 ** result.places;
    setEnabled(result.profile?.enabled ?? false);
    setProfile({ ...Object.fromEntries(AMOUNTS.map(([key]) => [key, String((result.profile?.[key] ?? 0) / scale)])), tax_percent: result.profile?.tax_percent ?? "0" });
  }, [code]);
  useEffect(() => { load(period).catch((cause) => say(errorText(cause, "원장을 불러오지 못했습니다."), true)); }, [load, period]);

  const accounts = journal?.accounts ?? [];
  const name = (account: string) => accounts.find((item) => item.code === account)?.name ?? account;
  const money = (units: number) => `${number(units / 10 ** (journal?.places ?? 0))}`;
  const sum = (side: Row["side"]) => rows.reduce((total, row) => total + (row.side === side ? Number(row.amount) || 0 : 0), 0);
  const debit = sum("debit"), credit = sum("credit");
  const balanced = debit > 0 && Math.abs(debit - credit) < 1e-9;
  const touchesCash = rows.some((row) => row.account === "cash");

  function act(body: Record<string, unknown>, confirmText: string, done?: () => void) {
    if (!window.confirm(confirmText)) return;
    void run(() => api<{ success: boolean; message: string }>(`/api/admin/companies/${encodeURIComponent(code)}/journal`, { method: "POST", body: JSON.stringify(body) }),
      "재무 기록을 저장하지 못했습니다.", async (result) => {
        say(plain(result.message), !result.success);
        if (!result.success) return;
        done?.();
        await load(period);
        onChanged();
      });
  }

  function post(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const lines = rows.filter((row) => row.account && Number(row.amount)).map((row) => ({ account: row.account, amount: row.side === "debit" ? row.amount.trim() : `-${row.amount.trim()}` }));
    const summary = lines.map((line) => `${line.amount.startsWith("-") ? "대변" : "차변"} ${name(line.account)} ${line.amount.replace(/^-/, "")}`).join("\n");
    act({ action: "entry", description, flow, lines }, `${code}에 수정 분개를 기록할까요?\n\n${description}\n${summary}`, () => { setRows(blank()); setDescription(""); setFlow("none"); });
  }

  function reverse(entry: Entry) {
    const reason = window.prompt(`분개 #${entry.id} (${entry.description})을 정정합니다. 원래 분개의 반대 분개가 이번 달에 기록됩니다.\n\n정정 사유:`);
    if (!reason?.trim()) return;
    act({ action: "reverse", entryId: entry.id, description: reason.trim() }, `분개 #${entry.id}을 정정할까요?`);
  }

  function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const config = { enabled, ...Object.fromEntries(PROFILE_FIELDS.map(([key]) => [key, profile[key] || "0"])) };
    act({ action: "configure", config }, `${code} 월간 영업 설정을 저장할까요? 이번 달부터 적용되고 다음 달에 자동 인식됩니다.`);
  }

  const setRow = (index: number, next: Partial<Row>) => setRows(rows.map((row, position) => position === index ? { ...row, ...next } : row));
  const options = <>{Object.entries(CATEGORY).map(([category, label]) => <optgroup key={category} label={label}>{accounts.filter((item) => item.category === category).map((item) => <option key={item.code} value={item.code} disabled={item.protected}>{item.name} ({item.code}){item.protected ? " · 자동 연동" : ""}</option>)}</optgroup>)}</>;

  return <section className="ledger">
    <div className="ledger-head">
      <h3 className="sub-head">재무제표 수정</h3>
      <label className="ledger-period">기간<input type="month" value={period} max={thisMonth()} onChange={(event) => event.target.value && setPeriod(event.target.value)} /></label>
    </div>
    <p className="hint">재무제표는 원장에서 계산됩니다. 숫자를 직접 덮어쓰지 않고 차변·대변이 맞는 수정 분개로 고치며, 잘못 넣은 관리자 분개는 반대 분개로 정정합니다. 결산한 달은 바꿀 수 없으니 다음 달에 정정하세요. 금액 단위 {journal?.currency ?? "—"}.</p>
    <Tabs label="재무 수정" value={tab} onChange={setTab} options={[["entry", "수정 분개"], ["history", "분개 내역", journal?.entries.length], ["operating", "월간 영업 설정"], ["close", "결산", journal?.closed.length]]} />

    {tab === "entry" && <form className="ledger-form" onSubmit={post}>
      <div className="form-grid">
        <Field label="거래 설명" wide><input required maxLength={300} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="예: 3분기 외상매출 인식" /></Field>
        <Field label="현금흐름 구분" hint={touchesCash ? "법인 현금이 움직이면 영업·투자·재무 중 하나가 필요합니다" : undefined}><select value={flow} onChange={(event) => setFlow(event.target.value)}>{FLOW.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
      </div>
      <div className="scroll-x"><table className="ledger-lines"><thead><tr><th>계정</th><th>차변 / 대변</th><th className="r">금액</th><th /></tr></thead><tbody>
        {rows.map((row, index) => <tr key={index}>
          <td><select aria-label={`${index + 1}행 계정`} value={row.account} onChange={(event) => setRow(index, { account: event.target.value })}><option value="">계정 선택</option>{options}</select></td>
          <td><div className="segment-tabs" role="radiogroup" aria-label={`${index + 1}행 차대`}>{(["debit", "credit"] as const).map((side) => <button key={side} type="button" role="radio" aria-checked={row.side === side} className={row.side === side ? "active" : ""} onClick={() => setRow(index, { side })}>{side === "debit" ? "차변" : "대변"}</button>)}</div></td>
          <td className="r"><input aria-label={`${index + 1}행 금액`} className="mono r" inputMode="decimal" value={row.amount} onChange={(event) => setRow(index, { amount: event.target.value.replace(/[^\d.]/g, "") })} placeholder="0" /></td>
          <td><button type="button" className="soft small" disabled={rows.length <= 2} onClick={() => setRows(rows.filter((_, position) => position !== index))} aria-label={`${index + 1}행 삭제`}>삭제</button></td>
        </tr>)}
      </tbody><tfoot><tr><td><button type="button" className="soft small" disabled={rows.length >= 30} onClick={() => setRows([...rows, { account: "", side: credit > debit ? "debit" : "credit", amount: "" }])}>행 추가</button></td><td className={balanced ? "ledger-check ok" : "ledger-check"}>{balanced ? "차대 일치" : `차이 ${number(Math.abs(debit - credit))}`}</td><td className="r mono">차변 {number(debit)} · 대변 {number(credit)}</td><td /></tr></tfoot></table></div>
      <button className="btn primary" disabled={busy || !balanced || !description.trim()}>분개 기록</button>
    </form>}

    {tab === "history" && <div className="scroll-x tall"><table><thead><tr><th>#</th><th>시각</th><th>설명</th><th>분개</th><th>현금흐름</th><th /></tr></thead><tbody>
      {(journal?.entries ?? []).map((entry) => <tr key={entry.id} className={entry.reversedBy ? "muted" : undefined}>
        <td className="mono">#{entry.id}</td><td>{date(entry.epoch)}</td>
        <td>{entry.description}{!entry.manual && <span className="tag inline">자동</span>}{entry.reversalOf && <span className="tag inline">#{entry.reversalOf} 정정</span>}{entry.reversedBy && <span className="tag warn inline">#{entry.reversedBy}로 정정됨</span>}</td>
        <td><ul className="ledger-entry">{entry.lines.map((line, index) => <li key={index}><span>{line.amount > 0 ? "차" : "대"}</span>{name(line.account)}<b className="mono">{money(Math.abs(line.amount))}</b></li>)}</ul></td>
        <td>{FLOW_NAME[entry.flow] ?? entry.flow}</td>
        <td>{entry.manual && !entry.reversedBy && !entry.reversalOf && <button type="button" className="soft small" disabled={busy || journal?.closed.includes(journal.period)} onClick={() => reverse(entry)}>정정</button>}</td>
      </tr>)}
    </tbody></table>{journal && !journal.entries.length && <p className="empty">{journal.period}에 기록된 분개가 없습니다.</p>}</div>}

    {tab === "operating" && <form className="form-grid" onSubmit={saveProfile}>
      <p className="hint span-wide">매달 외상매출·비용을 자동으로 인식합니다. 현금 수금·지급은 수정 분개로 처리합니다.{journal?.profile ? ` 현재 ${journal.profile.enabled ? "활성" : "중지"} · ${journal.profile.start_period}부터 적용.` : " 아직 설정이 없습니다."}</p>
      <Field label="자동 영업 인식"><select value={enabled ? "on" : "off"} onChange={(event) => setEnabled(event.target.value === "on")}><option value="on">켜기</option><option value="off">끄기</option></select></Field>
      {PROFILE_FIELDS.map(([key, label]) => <Field key={key} label={label}><input className="mono" inputMode="decimal" value={profile[key] ?? ""} onChange={(event) => setProfile({ ...profile, [key]: event.target.value.replace(/[^\d.]/g, "") })} /></Field>)}
      <button className="btn primary" disabled={busy}>영업 설정 저장</button>
    </form>}

    {tab === "close" && <form className="form-grid" onSubmit={(event) => { event.preventDefault(); act({ action: "close", period: closing }, `${code}의 ${closing}을 결산할까요? 결산한 달은 더 이상 수정할 수 없습니다.`); }}>
      <Field label="마감할 월" hint="끝난 달만 결산할 수 있습니다"><input type="month" required value={closing} max={lastMonth()} onChange={(event) => setClosing(event.target.value)} /></Field>
      <button className="btn primary" disabled={busy}>결산</button>
      <p className="hint span-wide">결산한 달: {journal?.closed.length ? journal.closed.join(", ") : "없음"}</p>
    </form>}
  </section>;
}
