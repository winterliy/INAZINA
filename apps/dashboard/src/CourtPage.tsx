import { tabPanel } from "./tabKeys";
import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Tabs, date, plain, type Ctx, confirmAction } from "./common";

type Status = "submitted" | "reviewing" | "approved" | "rejected";
type Filing = { filingId: number; kind: "rehabilitation" | "bankruptcy"; label: string; reason: string; plan: string; status: Status;
  createdEpoch: number; note: string; handledEpoch: number | null; userId: string; nickname: string; handledBy: string | null;
  snapshot: Record<string, unknown> };
type Filter = Status | "all";
type Result = { message: string; success: boolean };

const STATUS: Record<Status, string> = { submitted: "접수", reviewing: "심리 중", approved: "인가", rejected: "기각" };
const TAG: Record<Status, string> = { submitted: "tag warn", reviewing: "tag", approved: "tag ok", rejected: "tag danger" };
const SNAPSHOT: [string, string][] = [["cash", "현금"], ["account_total", "통장"], ["stock_total", "주식"], ["bond_total", "채권"],
  ["order_reserved_total", "주문 예치금"], ["grand_total", "총 자산"], ["loan_total", "대출 잔액"], ["net_worth", "순자산"]];
type Policy = Record<string, number> & { nationality: string; currency: string; custom: boolean };
// [key, label, sub-label, unit]
const POLICY_FIELDS: [string, string, string, string][] = [["exempt_amount", "파산 면제재산", "본국 통화", ""], ["bankruptcy_score", "파산 후 신용점수 상한", "0~1000", "점"],
  ["bankruptcy_ban_days", "파산 후 대출 금지", "", "일"], ["rehab_score", "회생 인가 시 신용점수 상한", "", "점"], ["rehab_weeks", "회생 기본 기간", "", "주"],
  ["rehab_min_weeks", "회생 최소 기간", "", "주"], ["rehab_max_weeks", "회생 최대 기간", "", "주"], ["rehab_repay_percent", "최소 변제율", "", "%"],
  ["rehab_max_missed", "허용 미납 횟수", "", "회"], ["rehab_ban_days_after", "회생 완료 후 대출 금지", "", "일"], ["rediscard_days", "재면책 금지", "", "일"]];
const digits = (value: string) => value.replace(/[^0-9.]/g, "");
const grouped = (value: string) => { const [whole, fraction] = digits(value).split("."); return whole ? Number(whole).toLocaleString("ko-KR") + (fraction !== undefined ? `.${fraction}` : "") : ""; };
const caseNo = (item: Filing) => `${new Date(item.createdEpoch * 1000).getFullYear()}${item.kind === "rehabilitation" ? "개회" : "하단"}${item.filingId}`;
const amount = (value: unknown) => value === undefined || value === null || value === "" ? "—" : Number(value).toLocaleString("ko-KR", { maximumFractionDigits: 2 });

/** 회생·파산 심리: filings from 나루's 회생 · 파산 tab; a decision is DMed to the applicant. */
export function CourtPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [filter, setFilter] = useState<Filter>("submitted");
  const [items, setItems] = useState<Filing[]>([]);
  const [counts, setCounts] = useState<Record<Status, number>>({ submitted: 0, reviewing: 0, approved: 0, rejected: 0 });
  const [open, setOpen] = useState<number | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [weeks, setWeeks] = useState<Record<number, string>>({});
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const loadPolicies = useCallback(async () => setPolicies((await api<{ items: Policy[] }>("/api/admin/court/policies")).items), []);
  useEffect(() => { loadPolicies().catch(() => undefined); }, [loadPolicies]);
  const changed = Object.entries(edits).map(([nationality, fields]) => [nationality, Object.fromEntries(Object.entries(fields).filter(([, v]) => digits(v) !== "").map(([k, v]) => [k, Number(digits(v))]))] as const)
    .filter(([, values]) => Object.keys(values).length);
  function savePolicies() {
    void run(async () => {
      const messages: string[] = [];
      for (const [nationality, values] of changed) messages.push((await api<Result>(`/api/admin/court/policies/${encodeURIComponent(nationality)}`, { method: "POST", body: JSON.stringify({ values }) })).message);
      return { success: true, message: messages.join(" ") } as Result;
    }, "기준을 저장하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); setEdits({}); await loadPolicies(); });
  }

  const load = useCallback(async () => {
    const data = await api<{ items: Filing[]; counts: Record<Status, number> }>(`/api/admin/court${filter === "all" ? "" : `?status=${filter}`}`);
    setItems(data.items);
    setCounts(data.counts);
  }, [filter]);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "신청을 불러오지 못했습니다.", true)); }, [load]);

  async function decide(item: Filing, status: Status) {
    const note = (notes[item.filingId] ?? "").trim();
    if (status === "rejected" && !note) { say("기각 사유를 적어주세요.", true); return; }
    const effect = status !== "approved" ? "" : item.kind === "bankruptcy"
      ? " 면제재산을 뺀 주식·채권·통장을 처분해 대출에 배당하고, 남은 대출을 면책하며, 신용점수를 내리고 대출을 막습니다."
      : " 변제계획을 시작합니다. 변제 중에는 이자가 멈추고 매주 변제금이 자동으로 빠져나갑니다.";
    if (status !== "reviewing" && !await confirmAction(`${caseNo(item)} ${item.label} 신청을 '${STATUS[status]}'(으)로 결정할까요?${effect} 되돌릴 수 없고, 신청자에게 DM으로 알립니다.`)) return;
    const body = { status, note, ...(status === "approved" && item.kind === "rehabilitation" && weeks[item.filingId] ? { weeks: Number(weeks[item.filingId]) } : {}) };
    void run(() => api<Result>(`/api/admin/court/${item.filingId}`, { method: "POST", body: JSON.stringify(body) }),
      "결정을 저장하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  const term = query.trim().toLocaleLowerCase();
  const visible = items.filter((item) => !term || `${caseNo(item)} ${item.nickname} ${item.userId} ${item.reason}`.toLocaleLowerCase().includes(term));
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0);

  return <div className="court-ref">
    <span className="eyebrow">Court · 회생 · 파산</span>
    <div className="tile-row">
      <section className="tile tile-dark"><p className="tile-label">새 신청 · 접수</p><p className="tile-value">{counts.submitted}</p></section>
      <section className="tile"><p className="tile-label">심리 중</p><p className="tile-value">{counts.reviewing}</p></section>
      <section className="tile"><p className="tile-label">인가</p><p className="tile-value">{counts.approved}</p></section>
      <section className="tile"><p className="tile-label">기각</p><p className="tile-value">{counts.rejected}</p></section>
    </div>
    <Card title="회생 · 파산 심리" caption="나루 '회생 · 파산' 탭으로 들어온 신청입니다. 행을 열면 신청 이유·변제계획과 제출 당시 자산을 볼 수 있습니다. 결정하면 신청자에게 Discord DM으로 알리며, 인가·기각은 되돌릴 수 없습니다. 파산 인가는 자산 처분·배당·면책을, 회생 인가는 주간 자동 변제를 바로 실행합니다. 수치는 아래 국가별 기준을 따릅니다.">
      <Tabs group="court" label="상태" value={filter} onChange={setFilter} options={[["submitted", "접수", counts.submitted], ["reviewing", "심리 중", counts.reviewing], ["approved", "인가", counts.approved], ["rejected", "기각", counts.rejected], ["all", "전체", total]]} />
      <div className="scroll-x tall" {...tabPanel("court", filter)}><table><thead><tr><th>사건번호</th><th>종류</th><th>신청인</th><th>접수</th><th>상태</th><th>처리</th></tr></thead><tbody>
        {visible.map((item) => <Fragment key={item.filingId}>
          <tr className={open === item.filingId ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => setOpen(open === item.filingId ? null : item.filingId)} onKeyDown={(event) => { if (event.key === "Enter") setOpen(open === item.filingId ? null : item.filingId); }}>
            <td className="mono">{caseNo(item)}</td><td>{item.label}</td><td><b>{item.nickname}</b> <span className="mono hint">{item.userId}</span></td>
            <td>{date(item.createdEpoch)}</td><td><span className={TAG[item.status]}>{STATUS[item.status]}</span></td>
            <td>{item.handledBy ? `${item.handledBy} · ${item.handledEpoch ? date(item.handledEpoch) : ""}` : "—"}</td>
          </tr>
          {open === item.filingId && <tr><td colSpan={6}><div className="court-detail">
            <div><h3>신청 이유</h3><pre className="feedback-body">{item.reason}</pre>
              {item.plan && <><h3>변제계획</h3><pre className="feedback-body">{item.plan}</pre></>}
              {item.note && <><h3>처리 메모</h3><pre className="feedback-body">{item.note}</pre></>}</div>
            <div><h3>제출 당시 자산</h3><dl className="court-snapshot">{SNAPSHOT.map(([key, label]) => <div key={key}><dt>{label}</dt><dd className="mono">{amount(item.snapshot[key])}</dd></div>)}</dl></div>
            {(item.status === "submitted" || item.status === "reviewing") && <div className="court-decide">
              {item.kind === "rehabilitation" && <label>변제 기간(주, 비우면 국가 기본값)<input type="number" min={1} value={weeks[item.filingId] ?? ""} onChange={(event) => setWeeks({ ...weeks, [item.filingId]: event.target.value })} /></label>}
              <label>신청자에게 보낼 안내·사유 (기각 시 필수)<textarea rows={3} maxLength={1000} value={notes[item.filingId] ?? ""} onChange={(event) => setNotes({ ...notes, [item.filingId]: event.target.value })} /></label>
              <div className="actions">
                {item.status === "submitted" && <button className="soft small" disabled={busy} onClick={() => decide(item, "reviewing")}>심리 개시</button>}
                <button className="small" disabled={busy} onClick={() => decide(item, "approved")}>인가</button>
                <button className="soft small danger" disabled={busy} onClick={() => decide(item, "rejected")}>기각</button>
              </div>
            </div>}
          </div></td></tr>}
        </Fragment>)}
      </tbody></table>{!visible.length && <p className="empty">{filter === "submitted" ? "새로 접수된 신청이 없습니다." : "내역이 없습니다."}</p>}</div>
    </Card>
    <Card title="국가별 회생 · 파산 기준" caption="인가할 때 쓰는 수치입니다. 따로 저장하지 않은 국가는 기본값을 씁니다. 바꾼 값은 주황색으로 표시되고, 변경 저장을 누르면 한 번에 반영됩니다.">
      <div className="scroll-x"><table className="policy-table"><thead><tr><th>항목</th>{policies.map((p) => {
        const dirty = changed.some(([nationality]) => nationality === p.nationality);
        return <th key={p.nationality} className="num"><span className="cur">{p.nationality} {p.currency}</span><span className={dirty || p.custom ? "policy-badge custom" : "policy-badge"}>{dirty ? "변경 중" : p.custom ? "국가 설정" : "기본값"}</span></th>;
      })}</tr></thead><tbody>
        {POLICY_FIELDS.map(([key, label, sub, unit]) => <tr key={key}><td className="label">{label}{sub && <small>{sub}</small>}</td>{policies.map((p) => {
          const value = edits[p.nationality]?.[key] ?? "";
          return <td key={p.nationality} className="num">
            <input className={digits(value) ? "ref-input dirty" : "ref-input"} inputMode="decimal" aria-label={`${p.nationality} ${label}`} placeholder={Number(p[key]).toLocaleString("ko-KR")} value={value}
              onChange={(event) => setEdits({ ...edits, [p.nationality]: { ...(edits[p.nationality] ?? {}), [key]: grouped(event.target.value) } })} />
            <span className="ref-unit">{unit || p.currency}</span></td>;
        })}</tr>)}
      </tbody></table></div>
      <div className="policy-actions">
        <button className="soft small" disabled={busy || !changed.length} onClick={() => setEdits({})}>되돌리기</button>
        <button className="small" disabled={busy || !changed.length} onClick={savePolicies}>변경 저장</button>
      </div>
    </Card>
  </div>;
}
