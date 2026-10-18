import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, type ApiUser } from "../../shared/api";
import { DateTimePicker } from "./DateTimePicker";

type Category = { category_id: number; name: string };
// suspension: the notice while an administrator has stopped writing on the board.
type BoardInfo = { board_id: string; name: string; description: string; admin_only: number; allow_asset: number; secret: number; news: number; suspension: string | null; categories: Category[] };
// News moves the market from publication: administrators see its impact, readers only that it is news.
type News = { status: "scheduled" | "live" | "ended" | "failed"; publish_epoch: number; event_type?: "good" | "bad"; severity?: number; asset_code?: string | null; duration_seconds?: number; target_price?: number | null; error?: string | null };
type NewsDraft = { eventType: "good" | "bad"; severity: string; minutes: string; targetPrice: string; publish: number | null };
type Summary = { post_id: number; board: string; board_name: string; category_id: number | null; category_name: string | null; notice: number; author_admin: number; title: string; asset_code: string | null; pinned: number; anonymous: number; view_count: number; created_epoch: number; updated_epoch: number; author: string; comment_count: number; like_count: number; dislike_count: number; mine: boolean; has_attachments: number; has_poll: number; news_status: News["status"] | null; news_publish_epoch: number | null };
type Reaction = 1 | -1 | 0;
type Attachment = { attachment_id: number; kind: "file" | "link"; name: string; url: string | null; size: number | null };
type PollOption = { option_id: number; label: string; votes: number; voters?: string[] };
type Poll = { poll_id: number; question: string; anonymous: number; multiple: number; closes_epoch: number | null; closed_epoch: number | null; open: boolean; options: PollOption[]; my_votes: number[]; voters: number };
type Upload = { attachmentId: number; name: string; size: number };
type PollDraft = { question: string; options: string[]; anonymous: boolean; multiple: boolean; closes: number | null };
// parent_id null: a top-level comment. removed: a comment hidden by moderation, kept as the anchor of its replies.
type Comment = { comment_id: number; parent_id: number | null; removed: boolean; body: string; created_epoch: number; author: string; author_admin: number; mine: boolean; like_count: number; dislike_count: number; my_reaction: Reaction };
type Post = Summary & { body: string; my_reaction: Reaction; comments: Comment[]; attachments: Attachment[]; poll: Poll | null; news: News | null };
type Page = { posts: Summary[]; total: number; page: number; pageSize: number; writeOpensEpoch?: number };
type Draft = { postId: number | null; board: string; categoryId: number | null; title: string; body: string; assetCode: string; anonymous: boolean; pinned: boolean; notice: boolean;
  // Files uploaded for this draft, links, attachments to drop (editing) and a poll (new posts).
  uploads: Upload[]; links: { title: string; url: string }[]; existing: Attachment[]; remove: number[]; poll: PollDraft | null; news: NewsDraft | null };
const NEWS_STATUS: Record<News["status"], string> = { scheduled: "예약", live: "게시됨", ended: "영향 종료", failed: "게시 실패" };
const when = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(epoch * 1000);
const count = (value: number) => new Intl.NumberFormat("ko-KR").format(value);
const bytes = (value: number | null) => !value ? "" : value < 1024 * 1024 ? `${Math.ceil(value / 1024)}KB` : `${(value / 1024 / 1024).toFixed(1)}MB`;
const POLL_LONGEST_SECONDS = 90 * 86400;
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.";

/**
 * HTS board. Reading needs no login; writing, comments, replies, reactions and reports do.
 * `user` is null for a visitor, and `onLogin` takes them to the login screen.
 */
export function BoardPanel({ user, initialAsset = "", onClose, onLogin }: { user: ApiUser | null; initialAsset?: string; onClose: () => void; onLogin: () => void }) {
  const admin = user?.role === "admin";
  const [boards, setBoards] = useState<BoardInfo[]>([]);
  const [board, setBoard] = useState("");
  const [category, setCategory] = useState<number | null>(null);
  const [asset, setAsset] = useState(initialAsset);
  const [search, setSearch] = useState("");
  const [term, setTerm] = useState("");
  const [page, setPage] = useState(1);
  const [list, setList] = useState<Page | null>(null);
  const [post, setPost] = useState<Post | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [comment, setComment] = useState("");
  const [commentAnonymous, setCommentAnonymous] = useState(false);
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [reply, setReply] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ boards: BoardInfo[] }>("/api/board/boards").then((result) => {
      setBoards(result.boards);
      // "종목 토론" opens the first board that takes instrument codes.
      if (initialAsset) setBoard(result.boards.find((item) => item.allow_asset)?.board_id ?? "");
    }).catch((cause) => setError(errorText(cause)));
  }, [initialAsset]);
  const boardInfo = (id: string) => boards.find((item) => item.board_id === id);
  const current = boardInfo(board);

  const loadList = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page) });
    if (board) query.set("board", board);
    if (category) query.set("category", String(category));
    if (asset) query.set("asset", asset);
    if (term) query.set("search", term);
    setList(await api<Page>(`/api/board/posts?${query}`));
  }, [board, category, asset, term, page]);
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
    const poll = draft.poll && !draft.postId ? {
      question: draft.poll.question, options: draft.poll.options.map((option) => option.trim()).filter(Boolean),
      anonymous: draft.poll.anonymous, multiple: draft.poll.multiple,
      closesEpoch: draft.poll.closes } : undefined;
    const body = { board: draft.board, categoryId: draft.categoryId, title: draft.title, body: draft.body,
      assetCode: boardInfo(draft.board)?.allow_asset ? draft.assetCode.trim() : "", anonymous: draft.anonymous, pinned: draft.pinned, notice: draft.notice,
      attachments: draft.uploads.map((item) => item.attachmentId), links: draft.links.filter((link) => link.url.trim()), removeAttachments: draft.remove, ...(poll ? { poll } : {}),
      ...(draft.news && !draft.postId ? { news: { eventType: draft.news.eventType, severity: Math.round(Number(draft.news.severity)), durationSeconds: Math.round(Number(draft.news.minutes) * 60),
        targetPrice: draft.news.targetPrice.trim() ? Math.round(Number(draft.news.targetPrice)) : null, publishEpoch: draft.news.publish } } : {}) };
    if (draft.postId) void write(`/api/board/posts/${draft.postId}/edit`, body, async () => { await open(draft.postId!); await loadList(); });
    else void write<{ postId: number }>("/api/board/posts", body, async (result) => { setPage(1); await loadList(); await open(result.postId); });
  }

  function submitComment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!post) return;
    void write(`/api/board/posts/${post.post_id}/comments`, { body: comment, anonymous: commentAnonymous }, async () => { setComment(""); await open(post.post_id); await loadList(); });
  }

  function submitReply(event: FormEvent<HTMLFormElement>, parentId: number) {
    event.preventDefault();
    if (!post) return;
    void write(`/api/board/posts/${post.post_id}/comments`, { body: reply, anonymous: commentAnonymous, parentId }, async () => { setReply(""); setReplyTo(null); await open(post.post_id); await loadList(); });
  }

  // 좋아요/싫어요: the same button again cancels, the other one switches.
  function react(path: string, value: 1 | -1) {
    if (!post) return;
    void write(path, { value }, async () => { await open(post.post_id); await loadList(); });
  }

  function reactions(likes: number, dislikes: number, mine: Reaction, path: string, small = false) {
    return <span className={small ? "board-reactions small" : "board-reactions"}>
      <button type="button" className={mine === 1 ? "board-react like active" : "board-react like"} disabled={busy} aria-pressed={mine === 1} aria-label={`좋아요 ${likes}`} onClick={() => react(path, 1)}>👍 {count(likes)}</button>
      <button type="button" className={mine === -1 ? "board-react dislike active" : "board-react dislike"} disabled={busy} aria-pressed={mine === -1} aria-label={`싫어요 ${dislikes}`} onClick={() => react(path, -1)}>👎 {count(dislikes)}</button>
    </span>;
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
    const first = writable[0]?.board_id ?? "";
    const target = board && writable.some((item) => item.board_id === board) ? board : first;
    const empty = { uploads: [], links: [], existing: [], remove: [], poll: null, news: null };
    setDraft(existing
      ? { ...empty, postId: existing.post_id, board: existing.board, categoryId: existing.category_id, title: existing.title, body: existing.body, assetCode: existing.asset_code ?? "", anonymous: existing.anonymous === 1, pinned: existing.pinned === 1, notice: existing.notice === 1, existing: existing.attachments }
      : { ...empty, postId: null, board: target, categoryId: target === board ? category : null, title: "", body: "", assetCode: asset, anonymous: false, pinned: false, notice: false });
  }

  // Each file goes up on its own (10MB at most); the post then refers to the returned ids.
  async function uploadFiles(files: FileList | null) {
    if (!files?.length || !draft) return;
    setBusy(true); setError("");
    const added: Upload[] = [];
    try {
      for (const file of Array.from(files)) {
        added.push(await api<Upload>("/api/board/files", { method: "POST", body: file, headers: { "x-file-name": encodeURIComponent(file.name) } }));
      }
    } catch (cause) { setError(errorText(cause)); }
    finally {
      setDraft((current) => current && { ...current, uploads: [...current.uploads, ...added] });
      setBusy(false);
    }
  }

  function votePoll(poll: Poll, optionIds: number[]) {
    if (!post) return;
    void write(`/api/board/polls/${poll.poll_id}/vote`, { optionIds }, async () => { await open(post.post_id); });
  }

  // Administrator actions on a post: 고정, 공지, moving board, category.
  function moderate(body: Record<string, unknown>) {
    if (!post) return;
    void write<{ success: boolean; message: string }>(`/api/admin/board/post/${post.post_id}`, body, async (result) => {
      if (!result.success) setError(result.message.replace(/^❌\s*/, ""));
      await open(post.post_id); await loadList();
    });
  }

  const authorView = (name: string, isAdmin: number) => <>{name}{isAdmin ? <em className="board-admin">관리자</em> : null}</>;

  // One comment or reply. Replies to a reply join the same thread (one level).
  function commentView(item: Comment) {
    if (item.removed) return <p className="board-comment-body board-removed">{item.body}</p>;
    const thread = item.parent_id ?? item.comment_id;
    return <>
      <p className="board-comment-meta"><b>{authorView(item.author, item.author_admin)}</b> · {when(item.created_epoch)}
        {item.mine
          ? <button type="button" className="danger" onClick={() => { if (window.confirm("댓글을 삭제할까요?")) void write(`/api/board/comments/${item.comment_id}/delete`, {}, async () => { await open(post!.post_id); await loadList(); }); }}>삭제</button>
          : <button type="button" disabled={closed} onClick={() => reportTarget({ commentId: item.comment_id })}>신고</button>}</p>
      <p className="board-comment-body">{item.body}</p>
      <div className="board-comment-actions">
        {reactions(item.like_count, item.dislike_count, item.my_reaction, `/api/board/comments/${item.comment_id}/react`, true)}
        <button type="button" className="board-reply-button" disabled={closed} title={closed ? `${opensText}부터 쓸 수 있습니다` : undefined} onClick={() => { if (!user) { onLogin(); return; } setReplyTo(thread); setReply(""); }}>답글</button>
      </div>
    </>;
  }

  const pages = list ? Math.max(1, Math.ceil(list.total / list.pageSize)) : 1;
  // Posts, comments and reports record the writer's IP, announced to start on this date.
  const opens = list?.writeOpensEpoch ?? 0;
  const closed = Date.now() / 1000 < opens;
  const opensText = opens ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "long" }).format(opens * 1000) : "";
  const writable = boards.filter((item) => admin || (!item.admin_only && !item.suspension));

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
          <div className="board-tabs" role="tablist" aria-label="게시판">{[{ board_id: "", name: "전체" }, ...boards].map((item) => <button key={item.board_id || "all"} type="button" role="tab" aria-selected={board === item.board_id} className={board === item.board_id ? "active" : ""} onClick={() => { setBoard(item.board_id); setCategory(null); setPage(1); if (!boardInfo(item.board_id)?.allow_asset) setAsset(""); }}>{boardInfo(item.board_id)?.secret ? "🔒 " : ""}{item.name}</button>)}</div>
          {current?.description && <p className="board-description">{current.description}</p>}
          {current?.secret ? <p className="board-description">🔒 허용된 사람만 보는 비밀게시판입니다.</p> : null}
          {current?.suspension && <p className="board-notice">⛔ {current.suspension} 지금은 읽기만 할 수 있습니다.</p>}
          {current && current.categories.length > 0 && <div className="board-categories" aria-label="카테고리">{[{ category_id: 0, name: "전체" }, ...current.categories].map((item) => <button key={item.category_id} type="button" aria-pressed={(category ?? 0) === item.category_id} className={(category ?? 0) === item.category_id ? "active" : ""} onClick={() => { setCategory(item.category_id || null); setPage(1); }}>{item.name}</button>)}</div>}
          <form className="board-search" onSubmit={(event) => { event.preventDefault(); setTerm(search.trim()); setPage(1); }}>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="제목·내용 검색" aria-label="게시글 검색" />
            <button type="submit">검색</button>
          </form>
          {asset && <p className="board-filter">종목 <b>{asset}</b> 토론만 보는 중 <button type="button" onClick={() => { setAsset(""); setPage(1); }}>해제</button></p>}
          <ul>{list?.posts.map((item) => <li key={item.post_id}><button type="button" className={post?.post_id === item.post_id ? "board-item active" : "board-item"} onClick={() => void open(item.post_id)}>
            <span className="board-item-title">{item.notice ? <em className="board-notice-label">[공지]</em> : item.pinned && item.board === board ? <em className="board-pin">고정</em> : null}{(!board || item.board !== board) && <em className="board-kind">{item.board_name}</em>}{item.category_name && <em className="board-kind">{item.category_name}</em>}{item.asset_code && <em className="board-kind">{item.asset_code}</em>}{item.news_status ? <em className="board-news-label">📰{item.news_status === "scheduled" && item.news_publish_epoch ? ` 예약 ${when(item.news_publish_epoch)}` : item.news_status === "failed" ? " 게시 실패" : ""}</em> : null}{item.title}{item.has_attachments ? <span className="board-icon" title="첨부 있음"> 📎</span> : null}{item.has_poll ? <span className="board-icon" title="설문 있음"> 📊</span> : null}{item.comment_count > 0 && <small> [{item.comment_count}]</small>}</span>
            <span className="board-item-meta">{authorView(item.author, item.author_admin)} · {when(item.created_epoch)} · 조회 {count(item.view_count)} · 👍 {count(item.like_count)} · 👎 {count(item.dislike_count)}</span>
          </button></li>)}</ul>
          {list && !list.posts.length && <p className="board-empty">{term ? "검색 결과가 없습니다." : "아직 글이 없습니다."}</p>}
          {pages > 1 && <div className="board-pages"><button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>이전</button><span>{page} / {pages}</span><button type="button" disabled={page >= pages} onClick={() => setPage(page + 1)}>다음</button></div>}
        </aside>

        <div className="board-main">
          {draft && <form className="board-form" onSubmit={submitDraft}>
            <h3>{draft.postId ? "글 수정" : "새 글"}</h3>
            <div className="board-form-row">
              <label>게시판<select value={draft.board} disabled={!!draft.postId} onChange={(event) => setDraft({ ...draft, board: event.target.value, categoryId: null })}>{writable.map((item) => <option key={item.board_id} value={item.board_id}>{item.name}</option>)}</select></label>
              {(boardInfo(draft.board)?.categories.length ?? 0) > 0 && <label>카테고리 (선택)<select value={draft.categoryId ?? ""} onChange={(event) => setDraft({ ...draft, categoryId: event.target.value ? Number(event.target.value) : null })}><option value="">없음</option>{boardInfo(draft.board)!.categories.map((item) => <option key={item.category_id} value={item.category_id}>{item.name}</option>)}</select></label>}
              {!!boardInfo(draft.board)?.allow_asset && <label>종목 코드 (선택)<input value={draft.assetCode} onChange={(event) => setDraft({ ...draft, assetCode: event.target.value.toUpperCase() })} placeholder="예: SELO" /></label>}
            </div>
            <label>제목<input value={draft.title} maxLength={100} required onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
            <label>내용<textarea value={draft.body} maxLength={10000} required rows={12} onChange={(event) => setDraft({ ...draft, body: event.target.value })} /></label>
            <label className="board-check"><input type="checkbox" checked={draft.anonymous} onChange={(event) => setDraft({ ...draft, anonymous: event.target.checked })} />익명으로 표시 <small>작성자 정보와 접속 IP는 관리 목적으로 기록됩니다.{admin ? " 관리자가 쓴 글에는 익명이어도 관리자 표시가 붙습니다." : ""}</small></label>
            <fieldset className="board-extras">
              <legend>첨부 · 설문</legend>
              {draft.existing.length > 0 && <ul className="board-attach-list">{draft.existing.map((item) => <li key={item.attachment_id}><label className="board-check"><input type="checkbox" checked={draft.remove.includes(item.attachment_id)} onChange={(event) => setDraft({ ...draft, remove: event.target.checked ? [...draft.remove, item.attachment_id] : draft.remove.filter((id) => id !== item.attachment_id) })} />지우기</label><span>{item.kind === "file" ? "📎" : "🔗"} {item.name}</span></li>)}</ul>}
              {draft.uploads.length > 0 && <ul className="board-attach-list">{draft.uploads.map((item) => <li key={item.attachmentId}><span>📎 {item.name} <small>{bytes(item.size)}</small></span><button type="button" onClick={() => setDraft({ ...draft, uploads: draft.uploads.filter((upload) => upload.attachmentId !== item.attachmentId) })}>빼기</button></li>)}</ul>}
              <label className="board-file">파일 추가 <small>10MB 이하 · 글당 5개 · pdf, 이미지, 문서, zip 등</small><input type="file" multiple disabled={busy} onChange={(event) => { void uploadFiles(event.target.files); event.target.value = ""; }} /></label>
              {draft.links.map((link, index) => <div className="board-link-row" key={index}>
                <input placeholder="링크 이름 (선택)" maxLength={100} value={link.title} onChange={(event) => setDraft({ ...draft, links: draft.links.map((item, at) => at === index ? { ...item, title: event.target.value } : item) })} />
                <input placeholder="https://" maxLength={500} value={link.url} onChange={(event) => setDraft({ ...draft, links: draft.links.map((item, at) => at === index ? { ...item, url: event.target.value } : item) })} />
                <button type="button" onClick={() => setDraft({ ...draft, links: draft.links.filter((_, at) => at !== index) })}>빼기</button>
              </div>)}
              <button type="button" className="board-add" onClick={() => setDraft({ ...draft, links: [...draft.links, { title: "", url: "" }] })}>+ 링크</button>
              {!draft.postId && (draft.poll ? <div className="board-poll-draft">
                <label>설문 질문<input maxLength={200} required value={draft.poll.question} onChange={(event) => setDraft({ ...draft, poll: { ...draft.poll!, question: event.target.value } })} /></label>
                {draft.poll.options.map((option, index) => <div className="board-link-row" key={index}><input placeholder={`선택지 ${index + 1}`} maxLength={50} value={option} onChange={(event) => setDraft({ ...draft, poll: { ...draft.poll!, options: draft.poll!.options.map((item, at) => at === index ? event.target.value : item) } })} />{draft.poll!.options.length > 2 && <button type="button" onClick={() => setDraft({ ...draft, poll: { ...draft.poll!, options: draft.poll!.options.filter((_, at) => at !== index) } })}>빼기</button>}</div>)}
                {draft.poll.options.length < 10 && <button type="button" className="board-add" onClick={() => setDraft({ ...draft, poll: { ...draft.poll!, options: [...draft.poll!.options, ""] } })}>+ 선택지</button>}
                <div className="board-admin-options">
                  <label className="board-check"><input type="checkbox" checked={draft.poll.anonymous} onChange={(event) => setDraft({ ...draft, poll: { ...draft.poll!, anonymous: event.target.checked } })} />익명 투표 (누가 골랐는지 숨김)</label>
                  <label className="board-check"><input type="checkbox" checked={draft.poll.multiple} onChange={(event) => setDraft({ ...draft, poll: { ...draft.poll!, multiple: event.target.checked } })} />여러 개 선택</label>
                  <div className="board-check">예약 마감 <DateTimePicker value={draft.poll.closes} min={Math.ceil(Date.now() / 1000) + 300} max={Math.floor(Date.now() / 1000) + POLL_LONGEST_SECONDS} placeholder="마감 없음 (직접 마감)" onChange={(closes) => setDraft({ ...draft, poll: { ...draft.poll!, closes } })} /><small>90일 안에서 고르세요. 비우면 직접 마감할 때까지 열려 있습니다.</small></div>
                </div>
                <button type="button" className="board-add" onClick={() => setDraft({ ...draft, poll: null })}>설문 빼기</button>
              </div> : <button type="button" className="board-add" onClick={() => setDraft({ ...draft, poll: { question: "", options: ["", ""], anonymous: true, multiple: false, closes: null } })}>+ 설문/투표</button>)}
              {draft.postId && <small className="board-hint">설문은 글을 쓸 때만 만들 수 있습니다.</small>}
            </fieldset>
            {admin && !draft.postId && !!boardInfo(draft.board)?.news && (draft.news ? <fieldset className="board-extras board-news-draft">
              <legend>📰 시장 영향 <small>게시하는 순간 시세에 반영</small></legend>
              <div className="board-form-row">
                <label>방향<select value={draft.news.eventType} onChange={(event) => setDraft({ ...draft, news: { ...draft.news!, eventType: event.target.value as "good" | "bad" } })}><option value="good">호재</option><option value="bad">악재</option></select></label>
                <label>강도 (%)<input type="number" min={0} max={100} required value={draft.news.severity} onChange={(event) => setDraft({ ...draft, news: { ...draft.news!, severity: event.target.value } })} /></label>
                <label>영향 시간 (분)<input type="number" min={1} max={10080} required value={draft.news.minutes} onChange={(event) => setDraft({ ...draft, news: { ...draft.news!, minutes: event.target.value } })} /></label>
                <label>목표가 (선택)<input type="number" min={1} disabled={!draft.assetCode.trim()} placeholder={draft.assetCode.trim() ? "비우면 강도만큼" : "종목 코드를 먼저"} value={draft.news.targetPrice} onChange={(event) => setDraft({ ...draft, news: { ...draft.news!, targetPrice: event.target.value } })} /></label>
              </div>
              <small className="board-hint">대상: {draft.assetCode.trim() ? `종목 ${draft.assetCode.trim()}` : "시장 전체 (위 종목 코드를 비우면)"} · 강도는 움직이는 동안의 변동성이자 목표가가 없을 때의 목표 변동률입니다.</small>
              <div className="board-check">예약 게시 <DateTimePicker value={draft.news.publish} min={Math.ceil(Date.now() / 1000) + 60} max={Math.floor(Date.now() / 1000) + 90 * 86400} placeholder="지금 바로 게시" onChange={(publish) => setDraft({ ...draft, news: { ...draft.news!, publish } })} /><small>예약하면 그 시각까지 관리자만 보고, 게시와 동시에 시세 영향이 시작됩니다.</small></div>
              <button type="button" className="board-add" onClick={() => setDraft({ ...draft, news: null })}>시장 영향 빼기</button>
            </fieldset> : <button type="button" className="board-add" onClick={() => setDraft({ ...draft, news: { eventType: "good", severity: "10", minutes: "60", targetPrice: "", publish: null } })}>📰 시장 영향 넣기</button>)}
            {admin && !draft.postId && <div className="board-admin-options"><label className="board-check"><input type="checkbox" checked={draft.pinned} onChange={(event) => setDraft({ ...draft, pinned: event.target.checked })} />이 게시판 맨 위에 고정</label><label className="board-check"><input type="checkbox" checked={draft.notice} onChange={(event) => setDraft({ ...draft, notice: event.target.checked })} />공지로 설정 (이 게시판 맨 위에 [공지] 표시)</label></div>}
            <div className="board-form-actions"><button type="button" onClick={() => setDraft(null)}>취소</button><button className="board-primary" disabled={busy}>{busy ? "저장 중…" : draft.postId ? "수정" : "등록"}</button></div>
          </form>}

          {!draft && post && <article className="board-post">
            <p className="board-post-kind">{post.notice ? <b className="board-notice-label">[공지] </b> : null}{post.news ? <b className="board-news-label">📰 뉴스 </b> : null}{post.board_name}{post.category_name ? ` · ${post.category_name}` : ""}{post.asset_code ? ` · ${post.asset_code}` : ""}{post.pinned ? " · 고정" : ""}</p>
            <h3>{post.title}</h3>
            <p className="board-post-meta">{authorView(post.author, post.author_admin)} · {when(post.created_epoch)}{post.updated_epoch > post.created_epoch ? ` (수정 ${when(post.updated_epoch)})` : ""} · 조회 {count(post.view_count)}</p>
            {admin && post.news?.event_type && <p className="board-news-impact">
              시장 영향 · {post.news.event_type === "good" ? "호재" : "악재"} {post.news.severity}% · {post.news.asset_code || "시장 전체"} · {Math.round((post.news.duration_seconds ?? 0) / 60)}분{post.news.target_price ? ` · 목표가 ${count(post.news.target_price)}` : ""}
              {" · "}{NEWS_STATUS[post.news.status]}{post.news.status === "scheduled" ? ` (${when(post.news.publish_epoch)} 게시)` : ""}{post.news.error ? ` — ${post.news.error}` : ""}
              <small>뉴스를 지우거나 숨기면 이후 영향이 멈춥니다(이미 움직인 가격은 그대로).</small></p>}
            <div className="board-post-body">{post.body}</div>
            {post.attachments.length > 0 && <ul className="board-attachments" aria-label="첨부">{post.attachments.map((item) => <li key={item.attachment_id}>{item.kind === "file"
              ? <a href={`/api/board/files/${item.attachment_id}`} download>📎 {item.name} <small>{bytes(item.size)}</small></a>
              : <a href={item.url ?? "#"} target="_blank" rel="noopener noreferrer nofollow">🔗 {item.name}</a>}</li>)}</ul>}
            {post.poll && (() => {
              const poll = post.poll;
              const total = poll.options.reduce((sum, option) => sum + option.votes, 0);
              const voted = poll.my_votes.length > 0;
              return <section className="board-poll" aria-label="설문">
                <h4>📊 {poll.question}</h4>
                <p className="board-poll-meta">{poll.anonymous ? "익명 투표" : "기명 투표"} · {poll.multiple ? "여러 개 선택" : "하나 선택"} · 참여 {count(poll.voters)}명 · {poll.open ? (poll.closes_epoch ? `${when(poll.closes_epoch)} 마감 예정` : "진행 중") : "마감됨"}</p>
                <form key={poll.my_votes.join(",") || "none"} onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); votePoll(poll, data.getAll("option").map(Number)); }}>
                  <ul>{poll.options.map((option) => {
                    const share = total ? Math.round(option.votes / total * 100) : 0;
                    return <li key={option.option_id}>
                      <label className="board-poll-option">{poll.open && <input type={poll.multiple ? "checkbox" : "radio"} name="option" value={option.option_id} defaultChecked={poll.my_votes.includes(option.option_id)} />}
                        <span className="board-poll-label">{option.label}{poll.my_votes.includes(option.option_id) && <em> ✓ 내 선택</em>}</span>
                        <span className="board-poll-count">{count(option.votes)}표 · {share}%</span>
                        <span className="board-poll-bar" aria-hidden="true"><span style={{ width: `${share}%` }} /></span></label>
                      {option.voters && option.voters.length > 0 && <small className="board-poll-voters">{option.voters.join(", ")}</small>}
                    </li>;
                  })}</ul>
                  {poll.open && <div className="board-poll-actions">
                    {user ? <button className="board-primary" disabled={busy}>{voted ? "다시 투표" : "투표"}</button> : <button type="button" onClick={onLogin}>로그인하고 투표</button>}
                    {voted && <button type="button" disabled={busy} onClick={() => votePoll(poll, [])}>투표 취소</button>}
                  </div>}
                </form>
                {(admin || post.mine) && <div className="board-poll-actions">{poll.open
                  ? <button type="button" disabled={busy} onClick={() => { if (window.confirm("설문을 지금 마감할까요?")) void write(`/api/board/polls/${poll.poll_id}/close`, {}, async () => { await open(post.post_id); }); }}>지금 마감</button>
                  : <button type="button" disabled={busy} onClick={() => void write(`/api/board/polls/${poll.poll_id}/reopen`, {}, async () => { await open(post.post_id); })}>다시 열기</button>}</div>}
              </section>;
            })()}
            <div className="board-post-actions">
              {reactions(post.like_count, post.dislike_count, post.my_reaction, `/api/board/posts/${post.post_id}/react`)}
              {post.mine ? <>
                <button type="button" onClick={() => startDraft(post)}>수정</button>
                <button type="button" className="danger" disabled={busy} onClick={() => { if (window.confirm("이 글을 삭제할까요?")) void write(`/api/board/posts/${post.post_id}/delete`, {}, async () => { setPost(null); await loadList(); }); }}>삭제</button>
              </> : <button type="button" disabled={closed} onClick={() => reportTarget({ postId: post.post_id })}>신고</button>}
            </div>
            {admin && <div className="board-admin-bar" aria-label="관리자 설정">
              <span className="board-admin-label">관리자</span>
              <button type="button" disabled={busy} className={post.pinned ? "active" : ""} onClick={() => moderate({ action: post.pinned ? "unpin" : "pin" })}>{post.pinned ? "고정 해제" : "고정"}</button>
              <button type="button" disabled={busy} className={post.notice ? "active" : ""} onClick={() => moderate({ action: post.notice ? "unnotice" : "notice" })}>{post.notice ? "공지 해제" : "공지로 설정"}</button>
              <label>게시판 이동<select value={post.board} disabled={busy} onChange={(event) => { if (window.confirm(`이 글을 ${boardInfo(event.target.value)?.name ?? event.target.value} 게시판으로 옮길까요? 카테고리는 해제됩니다.`)) moderate({ action: "move", board: event.target.value }); }}>{boards.map((item) => <option key={item.board_id} value={item.board_id}>{item.name}</option>)}</select></label>
              {(boardInfo(post.board)?.categories.length ?? 0) > 0 && <label>카테고리<select value={post.category_id ?? ""} disabled={busy} onChange={(event) => moderate({ action: "category", categoryId: event.target.value ? Number(event.target.value) : null })}><option value="">없음</option>{boardInfo(post.board)!.categories.map((item) => <option key={item.category_id} value={item.category_id}>{item.name}</option>)}</select></label>}
            </div>}
            <section className="board-comments" aria-label="댓글">
              <h4>댓글 {post.comments.filter((item) => !item.removed).length}</h4>
              <ul>{post.comments.filter((item) => !item.parent_id).map((item) => <li key={item.comment_id}>
                {commentView(item)}
                {(post.comments.some((child) => child.parent_id === item.comment_id) || replyTo === item.comment_id) && <ul className="board-replies">
                  {post.comments.filter((child) => child.parent_id === item.comment_id).map((child) => <li key={child.comment_id}>{commentView(child)}</li>)}
                  {replyTo === item.comment_id && <li><form className="board-comment-form" onSubmit={(event) => submitReply(event, item.comment_id)}>
                    <textarea value={reply} maxLength={1000} required rows={2} autoFocus placeholder="답글을 입력하세요" onChange={(event) => setReply(event.target.value)} />
                    <div><label className="board-check"><input type="checkbox" checked={commentAnonymous} onChange={(event) => setCommentAnonymous(event.target.checked)} />익명</label><span><button type="button" onClick={() => { setReplyTo(null); setReply(""); }}>취소</button> <button className="board-primary" disabled={busy || !reply.trim()}>답글 등록</button></span></div>
                  </form></li>}
                </ul>}
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
