import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, type ApiUser } from "../../shared/api";

type BoardId = "notice" | "free" | "market";
type Summary = { post_id: number; board: BoardId; title: string; asset_code: string | null; pinned: number; anonymous: number; view_count: number; created_epoch: number; updated_epoch: number; author: string; comment_count: number; like_count: number; mine: boolean };
type Comment = { comment_id: number; body: string; created_epoch: number; author: string; mine: boolean };
type Post = Summary & { body: string; liked: boolean; comments: Comment[] };
type Page = { posts: Summary[]; total: number; page: number; pageSize: number; writeOpensEpoch?: number };
type Draft = { postId: number | null; board: BoardId; title: string; body: string; assetCode: string; anonymous: boolean };

const BOARDS: [BoardId | "", string][] = [["", "전체"], ["notice", "공지"], ["free", "자유"], ["market", "시장 토론"]];
const NAMES: Record<BoardId, string> = { notice: "공지", free: "자유", market: "시장 토론" };
const when = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(epoch * 1000);
const count = (value: number) => new Intl.NumberFormat("ko-KR").format(value);
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.";

/**
 * HTS board. Reading needs no login; writing, comments, likes and reports do.
 * `user` is null for a visitor, and `onLogin` takes them to the login screen.
 */
export function BoardPanel({ user, initialAsset = "", onClose, onLogin }: { user: ApiUser | null; initialAsset?: string; onClose: () => void; onLogin: () => void }) {
  const [board, setBoard] = useState<BoardId | "">(initialAsset ? "market" : "");
  const [asset, setAsset] = useState(initialAsset);
  const [search, setSearch] = useState("");
  const [term, setTerm] = useState("");
  const [page, setPage] = useState(1);
  const [list, setList] = useState<Page | null>(null);
  const [post, setPost] = useState<Post | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [comment, setComment] = useState("");
  const [commentAnonymous, setCommentAnonymous] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page) });
    if (board) query.set("board", board);
    if (asset) query.set("asset", asset);
    if (term) query.set("search", term);
    setList(await api<Page>(`/api/board/posts?${query}`));
  }, [board, asset, term, page]);
  useEffect(() => { loadList().catch((cause) => setError(errorText(cause))); }, [loadList]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function open(postId: number) {
    setError("");
    try { setPost((await api<{ post: Post }>(`/api/board/posts/${postId}`)).post); setDraft(null); }
    catch (cause) { setError(errorText(cause)); }
  }

  // Runs a write; a visitor is sent to the login screen instead.
  async function write<T>(path: string, body: Record<string, unknown>, done?: (result: T) => void | Promise<void>) {
    if (!user) { onLogin(); return; }
    setBusy(true); setError("");
    try {
      const result = await api<T>(path, { method: "POST", body: JSON.stringify(body) });
      await done?.(result);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  }

  function submitDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft) return;
    const body = { board: draft.board, title: draft.title, body: draft.body, assetCode: draft.board === "market" ? draft.assetCode.trim() : "", anonymous: draft.anonymous };
    if (draft.postId) void write(`/api/board/posts/${draft.postId}/edit`, body, async () => { await open(draft.postId!); await loadList(); });
    else void write<{ postId: number }>("/api/board/posts", body, async (result) => { setPage(1); await loadList(); await open(result.postId); });
  }

  function submitComment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!post) return;
    void write(`/api/board/posts/${post.post_id}/comments`, { body: comment, anonymous: commentAnonymous }, async () => { setComment(""); await open(post.post_id); await loadList(); });
  }

  function reportTarget(target: { postId: number } | { commentId: number }) {
    if (!user) { onLogin(); return; }
    const reason = window.prompt("신고 사유를 적어 주세요. (300자 이내)");
    if (!reason?.trim()) return;
    void write("/api/board/reports", { ...target, reason }, () => window.alert("신고를 접수했습니다. 3명 이상이 신고하면 관리자 확인 전까지 숨겨집니다."));
  }

  function startDraft(existing?: Post) {
    if (!user) { onLogin(); return; }
    setPost(null);
    setDraft(existing
      ? { postId: existing.post_id, board: existing.board, title: existing.title, body: existing.body, assetCode: existing.asset_code ?? "", anonymous: existing.anonymous === 1 }
      : { postId: null, board: board || "free", title: "", body: "", assetCode: asset, anonymous: false });
  }

  const pages = list ? Math.max(1, Math.ceil(list.total / list.pageSize)) : 1;
  // Posts, comments and reports record the writer's IP, announced to start on this date.
  const opens = list?.writeOpensEpoch ?? 0;
  const closed = Date.now() / 1000 < opens;
  const opensText = opens ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "long" }).format(opens * 1000) : "";
  const writable = (Object.keys(NAMES) as BoardId[]).filter((id) => id !== "notice" || user?.role === "admin");

  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="board-modal" role="dialog" aria-modal="true" aria-labelledby="board-title">
      <div className="board-head">
        <div><p className="eyebrow">BOARD{user ? "" : " · 읽기 전용"}</p><h2 id="board-title">게시판</h2></div>
        <div className="board-head-actions">
          <button type="button" className="board-primary" disabled={closed} title={closed ? `${opensText}부터 쓸 수 있습니다` : undefined} onClick={() => startDraft()}>{closed ? `${opensText}부터 글쓰기` : user ? "글쓰기" : "로그인하고 글쓰기"}</button>
          <button type="button" onClick={onClose}>닫기</button>
        </div>
      </div>
      {closed && <p className="board-notice">게시판은 지금 읽기만 할 수 있습니다. 글쓰기·댓글·신고는 개인정보 처리방침 개정에 따라 <b>{opensText}</b>부터 열립니다. 작성·신고 시 접속 IP가 1년간 보관됩니다.</p>}
      {error && <p className="board-error" role="alert">{error}</p>}
      <div className="board-body">
        <aside className="board-list">
          <div className="board-tabs" role="tablist" aria-label="게시판">{BOARDS.map(([id, label]) => <button key={id || "all"} type="button" role="tab" aria-selected={board === id} className={board === id ? "active" : ""} onClick={() => { setBoard(id); setPage(1); if (id !== "market") setAsset(""); }}>{label}</button>)}</div>
          <form className="board-search" onSubmit={(event) => { event.preventDefault(); setTerm(search.trim()); setPage(1); }}>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="제목·내용 검색" aria-label="게시글 검색" />
            <button type="submit">검색</button>
          </form>
          {asset && <p className="board-filter">종목 <b>{asset}</b> 토론만 보는 중 <button type="button" onClick={() => { setAsset(""); setPage(1); }}>해제</button></p>}
          <ul>{list?.posts.map((item) => <li key={item.post_id}><button type="button" className={post?.post_id === item.post_id ? "board-item active" : "board-item"} onClick={() => void open(item.post_id)}>
            <span className="board-item-title">{item.pinned ? <em className="board-pin">고정</em> : null}{!board && <em className="board-kind">{NAMES[item.board]}</em>}{item.asset_code && <em className="board-kind">{item.asset_code}</em>}{item.title}{item.comment_count > 0 && <small> [{item.comment_count}]</small>}</span>
            <span className="board-item-meta">{item.author} · {when(item.created_epoch)} · 조회 {count(item.view_count)} · 추천 {count(item.like_count)}</span>
          </button></li>)}</ul>
          {list && !list.posts.length && <p className="board-empty">{term ? "검색 결과가 없습니다." : "아직 글이 없습니다."}</p>}
          {pages > 1 && <div className="board-pages"><button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>이전</button><span>{page} / {pages}</span><button type="button" disabled={page >= pages} onClick={() => setPage(page + 1)}>다음</button></div>}
        </aside>

        <div className="board-main">
          {draft && <form className="board-form" onSubmit={submitDraft}>
            <h3>{draft.postId ? "글 수정" : "새 글"}</h3>
            <div className="board-form-row">
              <label>게시판<select value={draft.board} disabled={!!draft.postId} onChange={(event) => setDraft({ ...draft, board: event.target.value as BoardId })}>{writable.map((id) => <option key={id} value={id}>{NAMES[id]}</option>)}</select></label>
              {draft.board === "market" && <label>종목 코드 (선택)<input value={draft.assetCode} onChange={(event) => setDraft({ ...draft, assetCode: event.target.value.toUpperCase() })} placeholder="예: SELO" /></label>}
            </div>
            <label>제목<input value={draft.title} maxLength={100} required onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
            <label>내용<textarea value={draft.body} maxLength={10000} required rows={12} onChange={(event) => setDraft({ ...draft, body: event.target.value })} /></label>
            <label className="board-check"><input type="checkbox" checked={draft.anonymous} onChange={(event) => setDraft({ ...draft, anonymous: event.target.checked })} />익명으로 표시 <small>작성자 정보와 접속 IP는 관리 목적으로 기록됩니다.</small></label>
            <div className="board-form-actions"><button type="button" onClick={() => setDraft(null)}>취소</button><button className="board-primary" disabled={busy}>{busy ? "저장 중…" : draft.postId ? "수정" : "등록"}</button></div>
          </form>}

          {!draft && post && <article className="board-post">
            <p className="board-post-kind">{NAMES[post.board]}{post.asset_code ? ` · ${post.asset_code}` : ""}{post.pinned ? " · 고정" : ""}</p>
            <h3>{post.title}</h3>
            <p className="board-post-meta">{post.author} · {when(post.created_epoch)}{post.updated_epoch > post.created_epoch ? ` (수정 ${when(post.updated_epoch)})` : ""} · 조회 {count(post.view_count)}</p>
            <div className="board-post-body">{post.body}</div>
            <div className="board-post-actions">
              <button type="button" className={post.liked ? "board-like active" : "board-like"} disabled={busy} onClick={() => void write(`/api/board/posts/${post.post_id}/like`, {}, async () => { await open(post.post_id); await loadList(); })}>추천 {count(post.like_count)}</button>
              {post.mine ? <>
                <button type="button" onClick={() => startDraft(post)}>수정</button>
                <button type="button" className="danger" disabled={busy} onClick={() => { if (window.confirm("이 글을 삭제할까요?")) void write(`/api/board/posts/${post.post_id}/delete`, {}, async () => { setPost(null); await loadList(); }); }}>삭제</button>
              </> : <button type="button" disabled={closed} onClick={() => reportTarget({ postId: post.post_id })}>신고</button>}
            </div>
            <section className="board-comments" aria-label="댓글">
              <h4>댓글 {post.comments.length}</h4>
              <ul>{post.comments.map((item) => <li key={item.comment_id}>
                <p className="board-comment-meta"><b>{item.author}</b> · {when(item.created_epoch)}
                  {item.mine
                    ? <button type="button" className="danger" onClick={() => { if (window.confirm("댓글을 삭제할까요?")) void write(`/api/board/comments/${item.comment_id}/delete`, {}, async () => { await open(post.post_id); await loadList(); }); }}>삭제</button>
                    : <button type="button" disabled={closed} onClick={() => reportTarget({ commentId: item.comment_id })}>신고</button>}</p>
                <p className="board-comment-body">{item.body}</p>
              </li>)}</ul>
              {closed ? <p className="board-empty">댓글은 {opensText}부터 쓸 수 있습니다.</p> : user ? <form className="board-comment-form" onSubmit={submitComment}>
                <textarea value={comment} maxLength={1000} required rows={3} placeholder="댓글을 입력하세요" onChange={(event) => setComment(event.target.value)} />
                <div><label className="board-check"><input type="checkbox" checked={commentAnonymous} onChange={(event) => setCommentAnonymous(event.target.checked)} />익명</label><button className="board-primary" disabled={busy || !comment.trim()}>댓글 등록</button></div>
              </form> : <button type="button" className="board-login" onClick={onLogin}>로그인하고 댓글 쓰기</button>}
            </section>
          </article>}

          {!draft && !post && <p className="board-empty board-placeholder">왼쪽 목록에서 글을 고르세요.</p>}
        </div>
      </div>
    </section>
  </div>;
}
