import { useState } from "react";
import { del, get, post, api } from "./api.ts";
import { Attempt, Empty, Err, Field, useLoad, when } from "./ui.tsx";
import { MemberPick, type Me } from "./pages1.tsx";

export function Drive() {
  const [stack, setStack] = useState<{ id: number; name: string }[]>([]);
  const [trash, setTrash] = useState(false);
  const parent = stack.at(-1)?.id;
  const list = useLoad<any[]>(() => get(`/drive?${trash ? "trash=1" : parent ? `parent=${parent}` : ""}`), [parent, trash]);
  const [folder, setFolder] = useState(""); const [error, setError] = useState("");
  const [share, setShare] = useState<{ id: number; ids: number[] }>();
  const upload = (file: File) => {
    if (file.size > 15 * 1024 * 1024) { setError("파일이 너무 큽니다(최대 15MB)."); return; }
    const reader = new FileReader();
    reader.onload = () => post("/drive/files", { parentId: parent, name: file.name, dataBase64: String(reader.result).split(",")[1] }).then(() => { setError(""); list.reload(); }).catch(() => setError("올리지 못했습니다(실행 파일 형식은 올릴 수 없습니다)."));
    reader.readAsDataURL(file);
  };
  const download = async (id: number, name: string) => {
    const r = await fetch(`/api/work/drive/${id}/download`, { credentials: "same-origin" });
    if (!r.ok) { setError("내려받지 못했습니다."); return; }
    const url = URL.createObjectURL(await r.blob()); const a = document.createElement("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
  };
  return <>
    <h2>드라이브</h2><Err text={error || list.error} />
    <div className="row" style={{ marginBottom: 10 }}><button className={trash ? "ghost" : ""} onClick={() => { setTrash(false); setStack([]); }}>내 드라이브</button><button className={trash ? "" : "ghost"} onClick={() => setTrash(true)}>휴지통</button>
      {!trash && <><input type="file" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} /><input placeholder="새 폴더 이름" value={folder} onChange={(e) => setFolder(e.target.value)} /><Attempt className="ghost" action={async () => { await post("/drive/folders", { parentId: parent, name: folder }); setFolder(""); list.reload(); }}>폴더 만들기</Attempt></>}</div>
    {!trash && <p className="muted"><a href="#" onClick={(e) => { e.preventDefault(); setStack([]); }}>처음</a>{stack.map((s, i) => <span key={s.id}> / <a href="#" onClick={(e) => { e.preventDefault(); setStack(stack.slice(0, i + 1)); }}>{s.name}</a></span>)}</p>}
    <div className="card"><table><tbody>{(list.data ?? []).map((n) => <tr key={n.id}>
      <td>{n.kind === "folder" ? <a href="#" onClick={(e) => { e.preventDefault(); setStack([...stack, { id: n.id, name: n.name }]); }}>📁 {n.name}</a> : <a href="#" onClick={(e) => { e.preventDefault(); void download(n.id, n.name); }}>{n.name}</a>}</td>
      <td className="muted">{n.kind === "file" ? `${Math.ceil(n.size_bytes / 1024)} KB` : ""}</td><td className="muted">{when(n.created_at)}</td>
      <td>{trash ? <button className="danger" onClick={() => del(`/drive/${n.id}?purge=1`).then(list.reload)}>완전 삭제</button> : <><button className="ghost" onClick={() => setShare({ id: n.id, ids: [] })}>공유</button> <button className="ghost" onClick={() => del(`/drive/${n.id}`).then(list.reload)}>삭제</button></>}</td></tr>)}</tbody></table>{(list.data ?? []).length === 0 && <Empty />}</div>
    {share && <div className="card"><h3>공유 대상(선택한 사람에게 보기 권한, 비우면 공유 해제)</h3><MemberPick value={share.ids} onChange={(ids) => setShare({ ...share, ids })} />
      <div className="row"><Attempt action={async () => { await post(`/drive/${share.id}/share`, { memberIds: share.ids, permission: "view" }); setShare(undefined); }}>적용</Attempt><button className="ghost" onClick={() => setShare(undefined)}>닫기</button></div></div>}
  </>;
}

export function Docs() {
  const list = useLoad<any[]>(() => get("/docs"));
  const [id, setId] = useState<number>();
  const doc = useLoad<any>(() => (id ? get(`/docs/${id}`) : Promise.resolve(undefined)), [id]);
  const [title, setTitle] = useState(""); const [draft, setDraft] = useState<string>(); const [comment, setComment] = useState(""); const [shareIds, setShareIds] = useState<number[]>([]); const [shareMode, setShareMode] = useState("view");
  const content = draft ?? doc.data?.content ?? "";
  return <>
    <h2>문서</h2><Err text={list.error || doc.error} />
    <div className="split"><div><div className="card list">{(list.data ?? []).map((d) => <div key={d.id} className={id === d.id ? "on" : ""} onClick={() => { setId(d.id); setDraft(undefined); }}><span>{d.title}<br /><span className="muted">{d.owner}</span></span><span className="muted">v{d.version}</span></div>)}{(list.data ?? []).length === 0 && <Empty />}</div>
      <div className="card"><input placeholder="새 문서 제목" value={title} onChange={(e) => setTitle(e.target.value)} /> <Attempt action={async () => { const r = await post("/docs", { title }); setTitle(""); list.reload(); setId(r.id); }}>만들기</Attempt></div></div>
    <div className="card">{!doc.data ? <Empty text="문서를 선택하세요." /> : <>
      <h3>{doc.data.title} <span className="muted">v{doc.data.version} · {doc.data.permission === "edit" ? "편집 가능" : doc.data.permission === "comment" ? "댓글만" : "보기만"}</span></h3>
      <textarea style={{ minHeight: 260 }} value={content} readOnly={doc.data.permission !== "edit"} onChange={(e) => setDraft(e.target.value)} />
      {doc.data.permission === "edit" && <div className="row"><Attempt action={async () => { await api("PUT", `/docs/${id}`, { baseVersion: doc.data.version, content }); setDraft(undefined); doc.reload(); list.reload(); }}>저장</Attempt>
        <span className="muted">저장하면 새 버전이 기록됩니다. 다른 사람이 먼저 저장했으면 알려 드립니다.</span></div>}
      <h3>댓글</h3>{doc.data.comments.map((c: any) => <div key={c.id} className="msg"><b>{c.author}</b>{c.body} <span className="muted">{when(c.created_at)}</span></div>)}
      {doc.data.permission !== "view" && <div className="row"><input style={{ flex: 1 }} value={comment} onChange={(e) => setComment(e.target.value)} /><Attempt className="ghost" action={async () => { await post(`/docs/${id}/comments`, { body: comment }); setComment(""); doc.reload(); }}>댓글</Attempt></div>}
      {doc.data.owner_id !== undefined && doc.data.permission === "edit" && <><h3>공유(이 목록으로 덮어씁니다)</h3><MemberPick value={shareIds} onChange={setShareIds} />
        <select value={shareMode} onChange={(e) => setShareMode(e.target.value)}><option value="view">보기</option><option value="comment">댓글</option><option value="edit">편집</option></select>
        <Attempt className="ghost" action={async () => { await post(`/docs/${id}/share`, { members: shareIds.map((memberId) => ({ memberId, permission: shareMode })) }); doc.reload(); }}>공유 적용</Attempt></>}</>}</div></div>
  </>;
}

export function Approvals({ me }: { me: Me }) {
  const [box, setBox] = useState("todo");
  const list = useLoad<any[]>(() => get(`/approvals?box=${box}`), [box]);
  const [id, setId] = useState<number>();
  const detail = useLoad<any>(() => (id ? get(`/approvals/${id}`) : Promise.resolve(undefined)), [id]);
  const forms = useLoad<any[]>(() => get("/approvals/forms"));
  const [writing, setWriting] = useState(false);
  const [f, setF] = useState({ formId: "", title: "", body: "" }); const [approvers, setApprovers] = useState<number[]>([]); const [ccs, setCcs] = useState<number[]>([]); const [comment, setComment] = useState("");
  const names: Record<string, string> = { draft: "임시", pending: "진행", approved: "승인", rejected: "반려", withdrawn: "회수" };
  const myTurn = detail.data?.status === "pending" && (() => { const w = detail.data.steps.filter((s: any) => s.decision === "waiting" && s.kind !== "cc"); return w.some((s: any) => s.seq === w[0]?.seq && s.approver_id === me.member.id); })();
  return <>
    <h2>전자결재</h2>
    <div className="row" style={{ marginBottom: 10 }}>{[["todo", "결재할 문서"], ["drafted", "내가 올린 문서"], ["done", "처리한 문서"], ["cc", "참조"]].map(([b, l]) => <button key={b} className={box === b ? "" : "ghost"} onClick={() => { setBox(b); setId(undefined); }}>{l}</button>)}<button onClick={() => setWriting(!writing)}>{writing ? "닫기" : "기안 작성"}</button></div>
    {writing && <div className="card"><Field label="양식"><select value={f.formId} onChange={(e) => { const form = forms.data?.find((x) => String(x.id) === e.target.value); setF({ ...f, formId: e.target.value, body: form?.template ?? f.body }); }}><option value="">선택</option>{(forms.data ?? []).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></Field>
      <Field label="제목"><input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field><Field label="내용"><textarea value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} /></Field>
      <Field label="결재선(여러 명을 고르면 목록의 위에서 아래 순서로 차례대로 결재)"><MemberPick value={approvers} onChange={setApprovers} exclude={me.member.id} /></Field><Field label="참조"><MemberPick value={ccs} onChange={setCcs} /></Field>
      <Attempt action={async () => { await post("/approvals", { formId: Number(f.formId), title: f.title, body: f.body, steps: [...approvers.map((a, i) => ({ approverId: a, seq: i + 1 })), ...ccs.map((a) => ({ approverId: a, kind: "cc", seq: 99 }))] }); setWriting(false); setBox("drafted"); list.reload(); }}>상신</Attempt></div>}
    <Err text={list.error} />
    <div className="split"><div className="card list">{(list.data ?? []).length === 0 ? <Empty /> : list.data!.map((a) => <div key={a.id} className={id === a.id ? "on" : ""} onClick={() => setId(a.id)}><span>{a.title}<br /><span className="muted">{a.drafter}</span></span><span className="badge">{names[a.status]}</span></div>)}</div>
      <div className="card">{!detail.data ? <Empty text="문서를 선택하세요." /> : <><h3>{detail.data.title} <span className="badge">{names[detail.data.status]}</span></h3><p className="muted">기안자 {detail.data.drafter} · {when(detail.data.submitted_at)}</p><pre style={{ whiteSpace: "pre-wrap", font: "inherit" }}>{detail.data.body}</pre>
        <table><tbody>{detail.data.steps.map((s: any) => <tr key={s.id}><td>{s.kind === "cc" ? "참조" : `${s.seq}차`}</td><td>{s.approver}</td><td>{{ waiting: "대기", approved: "승인", rejected: "반려" }[s.decision as string]}</td><td className="muted">{s.comment}</td></tr>)}</tbody></table>
        {myTurn && <><Field label="의견(반려 시 필수)"><input value={comment} onChange={(e) => setComment(e.target.value)} /></Field><div className="row"><Attempt action={async () => { await post(`/approvals/${id}/decide`, { decision: "approved", comment }); setComment(""); detail.reload(); list.reload(); }}>승인</Attempt><Attempt className="danger" action={async () => { await post(`/approvals/${id}/decide`, { decision: "rejected", comment }); setComment(""); detail.reload(); list.reload(); }}>반려</Attempt></div></>}
        {detail.data.drafter_id === me.member.id && ["draft", "pending"].includes(detail.data.status) && <Attempt className="ghost" action={async () => { await post(`/approvals/${id}/withdraw`); detail.reload(); list.reload(); }}>회수</Attempt>}</>}</div></div>
  </>;
}

export function Boards({ me }: { me: Me }) {
  const boards = useLoad<any[]>(() => get("/boards"));
  const [bid, setBid] = useState<number>();
  const board = boards.data?.find((b) => b.id === (bid ?? boards.data?.[0]?.id));
  const posts = useLoad<any[]>(() => (board ? get(`/boards/${board.id}/posts`) : Promise.resolve([])), [board?.id]);
  const [pid, setPid] = useState<number>();
  const post1 = useLoad<any>(() => (pid ? get(`/posts/${pid}`) : Promise.resolve(undefined)), [pid]);
  const [f, setF] = useState({ title: "", body: "", pinned: false, mustRead: false }); const [comment, setComment] = useState(""); const [writing, setWriting] = useState(false);
  const canWrite = board && (!board.notice_only || me.member.role === "admin");
  return <>
    <h2>게시판/공지</h2><Err text={boards.error || posts.error} />
    <div className="row" style={{ marginBottom: 10 }}>{(boards.data ?? []).map((b) => <button key={b.id} className={b.id === board?.id ? "" : "ghost"} onClick={() => { setBid(b.id); setPid(undefined); }}>{b.name}</button>)}{canWrite && <button onClick={() => setWriting(!writing)}>글쓰기</button>}</div>
    {writing && <div className="card"><Field label="제목"><input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field><Field label="내용"><textarea value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} /></Field>
      {board.notice_only ? <div className="row"><label><input type="checkbox" checked={f.pinned} onChange={(e) => setF({ ...f, pinned: e.target.checked })} /> 상단 고정</label><label><input type="checkbox" checked={f.mustRead} onChange={(e) => setF({ ...f, mustRead: e.target.checked })} /> 필독</label></div> : null}
      <Attempt action={async () => { await post(`/boards/${board.id}/posts`, f); setWriting(false); setF({ title: "", body: "", pinned: false, mustRead: false }); posts.reload(); }}>등록</Attempt></div>}
    <div className="split"><div className="card list">{(posts.data ?? []).length === 0 ? <Empty /> : posts.data!.map((p) => <div key={p.id} className={pid === p.id ? "on" : ""} onClick={() => setPid(p.id)}><span>{p.pinned ? "📌 " : ""}{p.must_read && !p.read_by_me ? <span className="badge new">필독</span> : null} {p.title}<br /><span className="muted">{p.author} · 댓글 {p.comments}</span></span><span className="muted">{when(p.created_at)}</span></div>)}</div>
      <div className="card">{!post1.data ? <Empty text="글을 선택하세요." /> : <><h3>{post1.data.title}</h3><p className="muted">{post1.data.author} · {when(post1.data.created_at)}</p><pre style={{ whiteSpace: "pre-wrap", font: "inherit" }}>{post1.data.body}</pre>
        <h3>댓글</h3>{post1.data.comments.map((c: any) => <div key={c.id} className="msg"><b>{c.author}</b>{c.body}</div>)}<div className="row"><input style={{ flex: 1 }} value={comment} onChange={(e) => setComment(e.target.value)} /><Attempt className="ghost" action={async () => { await post(`/posts/${pid}/comments`, { body: comment }); setComment(""); post1.reload(); posts.reload(); }}>댓글</Attempt></div>
        {(post1.data.author_id === me.member.id || me.member.role === "admin") && <Attempt className="ghost" action={async () => { await del(`/posts/${pid}`); setPid(undefined); posts.reload(); }}>삭제</Attempt>}</>}</div></div>
  </>;
}
