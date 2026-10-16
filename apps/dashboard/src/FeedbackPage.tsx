import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Tabs, date, plain, type Ctx } from "./common";

type Feedback = { feedback_id: number; kind: "contact" | "report"; title: string; body: string; discord_id: string; email: string;
  status: "new" | "read" | "done"; created_epoch: number; handled_by: string | null; handled_epoch: number | null };
type Filter = "new" | "read" | "done" | "all";
type Result = { message: string; success: boolean };

const KIND = { contact: "📞 문의", report: "🚨 신고" };
const STATUS = { new: "새 알림", read: "확인함", done: "처리 완료" };

/** Contact/Report submissions from the official site (127.0.0.1:4314). */
export function FeedbackPage({ ctx, onChange }: { ctx: Ctx; onChange: () => void }) {
  const { busy, run, say, query } = ctx;
  const [items, setItems] = useState<Feedback[]>([]);
  const [filter, setFilter] = useState<Filter>("new");
  const [open, setOpen] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    const result = await api<{ items: Feedback[] }>(`/api/admin/feedback${filter === "all" ? "" : `?status=${filter}`}`);
    setItems(result.items);
    setSelected(new Set());
  }, [filter]);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "문의·신고를 불러오지 못했습니다.", true)); }, [load]);

  const done = async (result: Result) => { say(plain(result.message), !result.success); await load(); onChange(); };

  function mark(item: Feedback, status: Feedback["status"]) {
    void run(() => api<Result>(`/api/admin/feedback/${item.feedback_id}`, { method: "POST", body: JSON.stringify({ status }) }),
      "상태를 바꾸지 못했습니다.", done);
  }

  function remove(body: { ids: number[] } | { all: true }, question: string) {
    if (!window.confirm(question)) return;
    void run(() => api<Result>("/api/admin/feedback/delete", { method: "POST", body: JSON.stringify(body) }), "삭제하지 못했습니다.", done);
  }

  function expand(item: Feedback) {
    setOpen(open === item.feedback_id ? null : item.feedback_id);
    if (item.status === "new") mark(item, "read");
  }

  function toggle(id: number) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  }

  const term = query.trim().toLocaleLowerCase();
  const visible = items.filter((item) => !term || `${item.title} ${item.body} ${item.discord_id} ${item.email}`.toLocaleLowerCase().includes(term));
  // Only read submissions can be deleted; a new one must be opened first.
  const deletable = visible.filter((item) => item.status !== "new");
  const allChecked = deletable.length > 0 && deletable.every((item) => selected.has(item.feedback_id));

  return <Card title="공식 사이트 문의·신고" caption="공식 사이트(127.0.0.1:4314)의 Contact·Report 양식으로 들어온 내용입니다. 새 알림은 왼쪽 메뉴에 숫자로 표시되고 1분마다 갱신됩니다. 행을 열면 '확인함'으로 바뀌고, 확인한 내용만 삭제할 수 있습니다."
    actions={<>
      <button className="soft small danger" type="button" disabled={busy || !selected.size}
        onClick={() => remove({ ids: [...selected] }, `선택한 ${selected.size}건을 삭제할까요? 되돌릴 수 없습니다.`)}>선택 삭제 ({selected.size})</button>
      <button className="soft small danger" type="button" disabled={busy}
        onClick={() => remove({ all: true }, "읽은 문의·신고를 모두 삭제할까요? 새 알림은 남고, 삭제한 내용은 되돌릴 수 없습니다.")}>읽은 항목 전체 삭제</button>
    </>}>
    <Tabs label="상태" value={filter} onChange={setFilter} options={[["new", "새 알림"], ["read", "확인함"], ["done", "처리 완료"], ["all", "전체"]]} />
    <div className="scroll-x tall"><table><thead><tr>
      <th><input type="checkbox" aria-label="읽은 항목 모두 선택" checked={allChecked} disabled={!deletable.length}
        onChange={() => setSelected(allChecked ? new Set() : new Set(deletable.map((item) => item.feedback_id)))} /></th>
      <th>#</th><th>종류</th><th>제목</th><th>디스코드 아이디</th><th>이메일</th><th>접수</th><th>상태</th><th />
    </tr></thead><tbody>
      {visible.map((item) => <Fragment key={item.feedback_id}>
        <tr className={open === item.feedback_id ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => expand(item)} onKeyDown={(event) => { if (event.key === "Enter") expand(item); }}>
          <td onClick={(event) => event.stopPropagation()}><input type="checkbox" aria-label={`#${item.feedback_id} 선택`} disabled={item.status === "new"}
            title={item.status === "new" ? "먼저 열어 확인해야 삭제할 수 있습니다" : undefined}
            checked={selected.has(item.feedback_id)} onChange={() => toggle(item.feedback_id)} /></td>
          <td className="mono">#{item.feedback_id}</td><td>{KIND[item.kind]}</td><td><b>{item.title}</b></td>
          <td className="mono">{item.discord_id}</td><td className="mono">{item.email}</td><td>{date(item.created_epoch)}</td>
          <td><span className={item.status === "new" ? "tag warn" : item.status === "done" ? "tag ok" : "tag"}>{STATUS[item.status]}</span></td>
          <td className="row-actions" onClick={(event) => event.stopPropagation()}>
            {item.status !== "done"
              ? <button className="soft small" disabled={busy} onClick={() => mark(item, "done")}>처리 완료</button>
              : <button className="soft small" disabled={busy} onClick={() => mark(item, "read")}>다시 열기</button>}
            <button className="soft small danger" disabled={busy || item.status === "new"} title={item.status === "new" ? "먼저 열어 확인해야 삭제할 수 있습니다" : "삭제"}
              onClick={() => remove({ ids: [item.feedback_id] }, `#${item.feedback_id} "${item.title}"을(를) 삭제할까요? 되돌릴 수 없습니다.`)}>삭제</button>
          </td>
        </tr>
        {open === item.feedback_id && <tr><td colSpan={9}><pre className="feedback-body">{item.body}</pre>
          {item.handled_by && <p className="hint">마지막 처리: {item.handled_by} · {item.handled_epoch ? date(item.handled_epoch) : ""}</p>}</td></tr>}
      </Fragment>)}
    </tbody></table>{!visible.length && <p className="empty">{filter === "new" ? "새 문의·신고가 없습니다." : "내역이 없습니다."}</p>}</div>
  </Card>;
}
