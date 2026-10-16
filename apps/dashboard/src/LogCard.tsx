import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../../shared/api";
import { Card, plain, type Ctx } from "./common";

type Result = { message: string; success: boolean };

/**
 * A log table with checkbox selection, "선택 삭제" and "전체 삭제".
 *
 * `log` names the server-side log (events, flows, audit, jobs). Rows that
 * `deletable` rejects (a running event, a pending bot job) cannot be checked,
 * and the server skips them as well.
 */
export function LogCard<T>({ ctx, log, title, caption, rows, idOf, deletable = () => true, head, row, empty, keepNote, onChanged }: {
  ctx: Ctx; log: "events" | "flows" | "audit" | "jobs"; title: string; caption: ReactNode;
  rows: T[]; idOf: (item: T) => number; deletable?: (item: T) => boolean;
  head: ReactNode; row: (item: T) => ReactNode; empty: string; keepNote?: string; onChanged: () => void;
}) {
  const { busy, run, say } = ctx;
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Drop selections whose rows disappeared after a reload.
  useEffect(() => setSelected((current) => new Set([...current].filter((id) => rows.some((item) => idOf(item) === id)))), [rows]);

  const candidates = rows.filter(deletable);
  const allChecked = candidates.length > 0 && candidates.every((item) => selected.has(idOf(item)));
  function toggle(id: number) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  }
  function remove(body: { ids: number[] } | { all: true }, question: string) {
    if (!window.confirm(question)) return;
    void run(() => api<Result>(`/api/admin/logs/${log}/delete`, { method: "POST", body: JSON.stringify(body) }), "기록을 삭제하지 못했습니다.",
      (result) => { say(plain(result.message), !result.success); setSelected(new Set()); onChanged(); });
  }

  return <Card title={title} caption={caption} actions={<>
    <button className="soft small danger" type="button" disabled={busy || !selected.size}
      onClick={() => remove({ ids: [...selected] }, `선택한 ${selected.size}건을 삭제할까요? 되돌릴 수 없습니다.`)}>선택 삭제 ({selected.size})</button>
    <button className="soft small danger" type="button" disabled={busy || !rows.length}
      onClick={() => remove({ all: true }, `${title} 기록을 모두 삭제할까요?${keepNote ? ` ${keepNote}` : ""} 되돌릴 수 없습니다.`)}>전체 삭제</button>
  </>}>
    <div className="scroll-x tall"><table><thead><tr>
      <th><input type="checkbox" aria-label="삭제할 수 있는 항목 모두 선택" checked={allChecked} disabled={!candidates.length}
        onChange={() => setSelected(allChecked ? new Set() : new Set(candidates.map(idOf)))} /></th>{head}
    </tr></thead><tbody>{rows.map((item) => <tr key={idOf(item)}>
      <td><input type="checkbox" aria-label={`#${idOf(item)} 선택`} disabled={!deletable(item)} title={deletable(item) ? undefined : keepNote}
        checked={selected.has(idOf(item))} onChange={() => toggle(idOf(item))} /></td>{row(item)}
    </tr>)}</tbody></table>{!rows.length && <p className="empty">{empty}</p>}</div>
  </Card>;
}
