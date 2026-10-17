import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, number, plain, type Ctx } from "./common";

type Summary = { id: string; title: string; shown: string; legal: boolean; path: string; size: number; updatedEpoch: number | null; pendingEffectiveEpoch: number | null };
type Pending = { content: string; effectiveEpoch: number; announcedEpoch: number; important: boolean };
// content: the version in force; base: what editing starts from (the announced revision when there is one).
type Document = { id: string; title: string; shown: string; legal: boolean; path: string; content: string; pending: Pending | null; base: string; sha256: string;
  history: { name: string; size: number }[]; noticeDays: number; importantNoticeDays: number };
type Preview = { diff: string[]; added: number; removed: number; changed: boolean; notes: string[]; warnings: string[]; blocking: boolean; effectiveEpoch: number | null };

/**
 * Terms, privacy policy, rules, help and the Discord registration notice.
 * Edit → "변경 내용 확인" (diff and effects) → "확정". Legal documents are announced at
 * once and take effect after their notice period (7 days, 30 for important changes);
 * help and the registration notice change at once. A save is refused when the file
 * changed meanwhile; replaced versions are kept on the server.
 */
export function DocumentsPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [list, setList] = useState<Summary[]>([]);
  const [selected, setSelected] = useState("privacy");
  const [document, setDocument] = useState<Document | null>(null);
  const [draft, setDraft] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [important, setImportant] = useState(false);

  const loadList = useCallback(async () => setList((await api<{ documents: Summary[] }>("/api/admin/documents")).documents), []);
  const open = useCallback(async (id: string) => {
    const result = await api<Document>(`/api/admin/documents/${id}`);
    setDocument(result);
    setDraft(result.base);
    setPreview(null);
    setImportant(result.pending?.important ?? false);
  }, []);
  useEffect(() => { loadList().catch((cause) => say(cause instanceof Error ? cause.message : "문서를 불러오지 못했습니다.", true)); }, [loadList]);
  useEffect(() => { open(selected).catch((cause) => say(cause instanceof Error ? cause.message : "문서를 불러오지 못했습니다.", true)); }, [selected, open]);

  const dirty = document !== null && draft !== document.base;
  function choose(id: string) {
    if (dirty && !window.confirm("저장하지 않은 수정이 있습니다. 버리고 다른 문서를 열까요?")) return;
    setSelected(id);
  }
  function check() {
    void run(() => api<Preview>(`/api/admin/documents/${selected}/preview`, { method: "POST", body: JSON.stringify({ content: draft, important }) }),
      "변경 내용을 확인하지 못했습니다.", setPreview);
  }
  function save() {
    if (!document || !preview) return;
    const effects = [...preview.notes, ...preview.warnings].map((line) => `· ${line}`).join("\n");
    const question = document.legal ? "개정안을 공지할까요?" : "저장할까요? 바로 적용됩니다.";
    if (!window.confirm(`${document.title} ${question} (+${preview.added} / -${preview.removed}줄)${effects ? `\n\n${effects}` : ""}`)) return;
    void run(() => api<{ success: boolean; message: string; document: Document }>(`/api/admin/documents/${selected}`, {
      method: "POST", body: JSON.stringify({ content: draft, baseSha256: document.sha256, confirmed: true, important }),
    }), "저장하지 못했습니다.", async (result) => {
      say(plain(result.message), !result.success);
      setDocument(result.document); setDraft(result.document.base); setPreview(null);
      await loadList();
    });
  }
  function withdraw() {
    if (!document?.pending || !window.confirm("공지한 개정안을 거둘까요? 지금 문서가 계속 적용되고, 사이트의 개정 예정 안내도 사라집니다.")) return;
    void run(() => api<{ success: boolean; message: string; document: Document }>(`/api/admin/documents/${selected}/cancel`, { method: "POST", body: "{}" }),
      "개정안을 거두지 못했습니다.", async (result) => { say(plain(result.message), !result.success); setDocument(result.document); setDraft(result.document.base); setPreview(null); await loadList(); });
  }

  return <>
    <Card title="문서 · 약관" caption="약관·처리방침·운영규칙·내부규칙은 확정하면 바로 개정 예정으로 공지되고, 공지 기간(7일, 이용자에게 불리하거나 중요한 변경은 30일)이 지난 날 0시(한국시간)에 적용됩니다. 그때까지는 지금 문서가 적용됩니다. 도움말과 Discord 등록 안내문은 확정하면 바로 바뀝니다.">
      <div className="doc-tabs" role="tablist" aria-label="문서">{list.map((item) => <button key={item.id} type="button" role="tab" aria-selected={selected === item.id} className={selected === item.id ? "doc-tab active" : "doc-tab"} onClick={() => choose(item.id)}>
        <b>{item.title}</b><small>{item.shown}{item.legal ? " · 공지 후 적용" : " · 즉시 적용"}</small>{item.pendingEffectiveEpoch ? <small className="doc-pending-tag">개정 예정 · {date(item.pendingEffectiveEpoch)} 적용</small> : null}<small className="mono">{number(item.size)}B · {item.updatedEpoch ? date(item.updatedEpoch) : "—"}</small>
      </button>)}</div>
    </Card>
    {document && <Card title={document.title} caption={<><span className="mono">{document.path}</span> · {document.shown}</>}
      actions={<><button className="soft" type="button" disabled={busy || !dirty} onClick={() => { setDraft(document.base); setPreview(null); }}>되돌리기</button>
        <button className="soft" type="button" disabled={busy} onClick={() => { if (!dirty || window.confirm("수정한 내용을 버리고 다시 불러올까요?")) void run(() => open(selected), "다시 불러오지 못했습니다."); }}>다시 불러오기</button>
        <button className="btn primary small" type="button" disabled={busy || !dirty} onClick={check}>변경 내용 확인</button></>}>
      {document.pending && <div className="doc-pending">
        <p>📢 개정안 공지 중 — <b>{date(document.pending.effectiveEpoch)}</b>에 적용됩니다 (공지 {date(document.pending.announcedEpoch)}, {document.pending.important ? `중요한 변경 ${document.importantNoticeDays}일` : `${document.noticeDays}일`}). 아래 편집기는 개정안을 보여 줍니다. 다시 확정하면 개정안을 바꾸고 적용일을 새로 계산합니다.</p>
        <button className="soft small danger" type="button" disabled={busy} onClick={withdraw}>개정안 거두기</button>
      </div>}
      <p className="hint">형식: <code># 제목</code> · <code>## 소제목</code> · <code>- 항목</code> · <code>왼쪽 | 오른쪽</code>(표, 첫 줄은 머리글) · 빈 줄은 문단 나눔.{document.legal && " '시행일:' 줄은 적용일로 자동으로 채워지니 직접 고치지 않아도 됩니다."}</p>
      <textarea className="doc-editor" aria-label={`${document.title} 내용`} value={draft} spellCheck={false} onChange={(event) => { setDraft(event.target.value); setPreview(null); }} />
      <p className="hint">{number(draft.length)}자{dirty ? " · 저장하지 않은 수정 있음" : ""}</p>
      {document.legal && <label className="check"><input type="checkbox" checked={important} onChange={(event) => { setImportant(event.target.checked); setPreview(null); }} />이용자에게 불리하거나 중요한 변경 (공지 {document.importantNoticeDays}일 뒤 적용, 아니면 {document.noticeDays}일)</label>}
      {preview && <div className="doc-preview">
        <h3 className="sub-head">변경 내용 {preview.changed ? `(+${preview.added} / -${preview.removed}줄)` : "— 바뀐 내용 없음"}</h3>
        {preview.notes.map((note) => <p key={note} className="doc-note">ℹ️ {note}</p>)}
        {preview.warnings.map((warning) => <p key={warning} className="doc-warning">⚠️ {warning}</p>)}
        {preview.changed && <pre className="doc-diff">{preview.diff.map((line, index) => <span key={index} className={line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : ""}>{line}{"\n"}</span>)}</pre>}
        <div className="actions"><button className="soft" type="button" onClick={() => setPreview(null)}>계속 수정</button>
          <button className="btn primary" type="button" disabled={busy || !preview.changed || preview.blocking} onClick={save}>{document.legal ? "확정 · 개정 공지" : "확정 저장"}</button></div>
      </div>}
      {document.history.length > 0 && <details className="help"><summary>보관된 이전 판 {document.history.length}개</summary><ul className="doc-history">{document.history.map((item) => <li key={item.name} className="mono">{item.name} · {number(item.size)}B</li>)}</ul><p className="hint">서버의 database/document_history/{document.id}/ 폴더에 있습니다.</p></details>}
    </Card>}
  </>;
}
