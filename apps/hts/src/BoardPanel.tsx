import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, type ApiUser } from "../../shared/api";
import { DateTimePicker } from "./DateTimePicker";
import { PlazaLogo } from "./PlazaLogo";
import { BoardAccount, loadBoardDefaults, type BoardDefaults } from "./BoardAccount";

type Category = { category_id: number; name: string };
// suspension: the notice while an administrator has stopped writing on the board.
type BoardInfo = { board_id: string; name: string; description: string; admin_only: number; allow_asset: number; secret: number; news: number; sort_order: number; post_count?: number; suspension: string | null; categories: Category[] };
type BoardDraft = { boardId: string; name: string; description: string; adminOnly: boolean; allowAsset: boolean; secret: boolean; news: boolean; sortOrder: string };
// News moves the market from publication: administrators see its impact, readers only that it is news.
type News = { status: "scheduled" | "live" | "ended" | "failed"; publish_epoch: number; event_type?: "good" | "bad"; severity?: number; asset_code?: string | null; duration_seconds?: number; target_price?: number | null; error?: string | null };
type NewsDraft = { eventType: "good" | "bad"; severity: string; minutes: string; targetPrice: string; publish: number | null };
type Summary = { post_id: number; board: string; board_name: string; category_id: number | null; category_name: string | null; notice: number; author_admin: number; title: string; asset_code: string | null; pinned: number; anonymous: number; secret: number; view_count: number; created_epoch: number; updated_epoch: number; author: string; comment_count: number; like_count: number; dislike_count: number; mine: boolean; has_attachments: number; has_poll: number; news_status: News["status"] | null; news_publish_epoch: number | null };
type Reaction = 1 | -1 | 0;
type Attachment = { attachment_id: number; kind: "file" | "link"; name: string; url: string | null; size: number | null };
type PollOption = { option_id: number; label: string; votes: number; voters?: string[] };
type Poll = { poll_id: number; question: string; anonymous: number; multiple: number; closes_epoch: number | null; closed_epoch: number | null; open: boolean; options: PollOption[]; my_votes: number[]; voters: number };
type Upload = { attachmentId: number; name: string; size: number };
type PollDraft = { question: string; options: string[]; anonymous: boolean; multiple: boolean; closes: number | null };
// parent_id null: a top-level comment. removed: a comment hidden by moderation, kept as the anchor of its replies.
type Comment = { comment_id: number; parent_id: number | null; removed: boolean; body: string; created_epoch: number; author: string; author_admin: number; mine: boolean; like_count: number; dislike_count: number; my_reaction: Reaction; locked?: boolean };
type Post = Summary & { locked?: boolean; body: string; my_reaction: Reaction; comments: Comment[]; attachments: Attachment[]; poll: Poll | null; news: News | null };
type Page = { posts: Summary[]; total: number; page: number; pageSize: number; writeOpensEpoch?: number; trending?: { term: string; rank: number; change: number | null }[] };
type Draft = { postId: number | null; board: string; categoryId: number | null; title: string; body: string; assetCode: string; anonymous: boolean; secret: boolean; pinned: boolean; notice: boolean;
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
export function BoardPanel({ user, initialAsset = "", onLogin, onLogout }: { user: ApiUser | null; initialAsset?: string; onLogin: () => void; onLogout: () => void }) {
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
  // 계정설정's writing defaults: new posts and comments start from them.
  const [defaults, setDefaults] = useState<BoardDefaults>(loadBoardDefaults);
  const [accountOpen, setAccountOpen] = useState(false);
  const [commentAnonymous, setCommentAnonymous] = useState(defaults.commentAnonymous);
  const [commentSecret, setCommentSecret] = useState(defaults.commentSecret);
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [reply, setReply] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [manageBoards, setManageBoards] = useState(false);
  const [adminPostsBoard, setAdminPostsBoard] = useState("");
  const [boardDraft, setBoardDraft] = useState<BoardDraft | null>(null);
  const [editingBoardId, setEditingBoardId] = useState<string | null>(null);
  const [deleteTargets, setDeleteTargets] = useState<Record<string, string>>({});

  useEffect(() => {
    api<{ boards: BoardInfo[] }>("/api/board/boards").then((result) => {
      setBoards(result.boards);
      // "종목 토론" opens the first board that takes instrument codes.
      if (initialAsset) setBoard(result.boards.find((item) => item.allow_asset)?.board_id ?? "");
    }).catch((cause) => setError(errorText(cause)));
  }, [initialAsset]);
  const boardInfo = (id: string) => boards.find((item) => item.board_id === id);
  const current = boardInfo(board);

  function editBoard(item?: BoardInfo) {
    setEditingBoardId(item?.board_id ?? null);
    setBoardDraft(item ? { boardId: item.board_id, name: item.name, description: item.description,
      adminOnly: !!item.admin_only, allowAsset: !!item.allow_asset, secret: !!item.secret, news: !!item.news,
      sortOrder: String(item.sort_order ?? 0) }
      : { boardId: "", name: "", description: "", adminOnly: false, allowAsset: false, secret: false, news: false, sortOrder: String(boards.length) });
  }

  async function reloadBoards() {
    const result = await api<{ boards: BoardInfo[] }>("/api/board/boards");
    setBoards(result.boards);
    return result.boards;
  }

  function saveBoard(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!boardDraft) return;
    const creating = editingBoardId === null;
    const path = creating ? "/api/admin/board/board/new" : `/api/admin/board/board/${encodeURIComponent(boardDraft.boardId)}`;
    const payload = { action: creating ? "create" : "edit", ...boardDraft, adminOnly: boardDraft.adminOnly,
      allowAsset: boardDraft.allowAsset, sortOrder: Number(boardDraft.sortOrder) };
    void write(path, payload, async () => { await reloadBoards(); setBoardDraft(null); setEditingBoardId(null); setError(""); });
  }

  function removeBoard(item: BoardInfo) {
    const moveTo = deleteTargets[item.board_id] || boards.find((other) => other.board_id !== item.board_id)?.board_id || "";
    if (!window.confirm(item.post_count
      ? `“${item.name}” 게시판을 삭제하고 게시글 ${item.post_count}개를 “${boardInfo(moveTo)?.name ?? moveTo}”으로 옮길까요?`
      : `“${item.name}” 게시판을 삭제할까요?`)) return;
    void write(`/api/admin/board/board/${encodeURIComponent(item.board_id)}`,
      { action: "delete", moveTo }, async () => {
        const updated = await reloadBoards();
        if (board === item.board_id) { setBoard(""); setCategory(null); setPost(null); }
        if (editingBoardId === item.board_id) { setBoardDraft(null); setEditingBoardId(null); }
        setDeleteTargets((previous) => { const next = { ...previous }; delete next[item.board_id]; return next; });
        if (!updated.length) setBoard("");
        setError("");
      });
  }

  // Bumped to reload the list after writing, even when the board and page stay the same.
  const [reloadKey, setReloadKey] = useState(0);
  const loadList = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page) });
    if (board) query.set("board", board);
    if (category) query.set("category", String(category));
    if (asset) query.set("asset", asset);
    if (term) query.set("search", term);
    setList(await api<Page>(`/api/board/posts?${query}`));
  }, [board, category, asset, term, page, reloadKey]);
  useEffect(() => { loadList().catch((cause) => setError(errorText(cause))); }, [loadList]);
  // The open post or draft sits above the list, as on gallery boards: bring it into view.
  const mainRef = useRef<HTMLDivElement>(null);
  const shown = draft ? `draft:${draft.postId ?? "new"}` : post ? `post:${post.post_id}` : "";
  useEffect(() => { if (shown) mainRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [shown]);

  // Clicking the open post again closes it, as on gallery boards.
  function toggle(postId: number) {
    if (post?.post_id === postId && !draft) setPost(null); else void open(postId);
  }
  // A post belongs to the list it was opened from: another board or 말머리 closes it.
  useEffect(() => { setPost(null); }, [board, category, adminPostsBoard]);
  // A post just written opens once its board's list has loaded (after the switch above closed the old one).
  const [pendingOpen, setPendingOpen] = useState<number | null>(null);
  useEffect(() => { if (pendingOpen !== null && list) { setPendingOpen(null); void open(pendingOpen); } }, [list]);

  async function open(postId: number) {
    setError("");
    setAccountOpen(false);
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
      assetCode: boardInfo(draft.board)?.allow_asset ? draft.assetCode.trim() : "", anonymous: draft.anonymous, secret: draft.secret, pinned: draft.pinned, notice: draft.notice,
      attachments: draft.uploads.map((item) => item.attachmentId), links: draft.links.filter((link) => link.url.trim()), removeAttachments: draft.remove, ...(poll ? { poll } : {}),
      ...(draft.news && !draft.postId ? { news: { eventType: draft.news.eventType, severity: Math.round(Number(draft.news.severity)), durationSeconds: Math.round(Number(draft.news.minutes) * 60),
        targetPrice: draft.news.targetPrice.trim() ? Math.round(Number(draft.news.targetPrice)) : null, publishEpoch: draft.news.publish } } : {}) };
    if (draft.postId) void write(`/api/board/posts/${draft.postId}/edit`, body, async () => { await open(draft.postId!); setReloadKey((key) => key + 1); });
    // A new post shows at the top of the board it went to (which may not be the one on screen).
    else void write<{ postId: number }>("/api/board/posts", body, async (result) => {
      setDraft(null); setBoard(draft.board); setCategory(null); setAsset(""); setSearch(""); setTerm(""); setAdminPostsBoard(""); setPage(1);
      setPendingOpen(result.postId); setReloadKey((key) => key + 1);
    });
  }

  function submitComment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!post) return;
    void write(`/api/board/posts/${post.post_id}/comments`, { body: comment, anonymous: commentAnonymous, secret: commentSecret }, async () => { setComment(""); await open(post.post_id); await loadList(); });
  }

  function submitReply(event: FormEvent<HTMLFormElement>, parentId: number) {
    event.preventDefault();
    if (!post) return;
    void write(`/api/board/posts/${post.post_id}/comments`, { body: reply, anonymous: commentAnonymous, secret: commentSecret, parentId }, async () => { setReply(""); setReplyTo(null); await open(post.post_id); await loadList(); });
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
    setAccountOpen(false);
    const first = writable[0]?.board_id ?? "";
    const target = board && writable.some((item) => item.board_id === board) ? board : first;
    const empty = { uploads: [], links: [], existing: [], remove: [], poll: null, news: null };
    setDraft(existing
      ? { ...empty, postId: existing.post_id, board: existing.board, categoryId: existing.category_id, title: existing.title, body: existing.body, assetCode: existing.asset_code ?? "", anonymous: existing.anonymous === 1, secret: existing.secret === 1, pinned: existing.pinned === 1, notice: existing.notice === 1, existing: existing.attachments }
      : { ...empty, postId: null, board: target, categoryId: target === board ? category : null, title: "", body: "", assetCode: asset, anonymous: defaults.anonymous, secret: defaults.secret, pinned: false, notice: false });
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
      <p className={item.locked ? "board-comment-body board-locked" : "board-comment-body"}>{item.locked ? `🔒 ${item.body}` : item.body}</p>
      <div className="board-comment-actions">
        {reactions(item.like_count, item.dislike_count, item.my_reaction, `/api/board/comments/${item.comment_id}/react`, true)}
        <button type="button" className="board-reply-button" disabled={closed} title={closed ? `${opensText}부터 쓸 수 있습니다` : undefined} onClick={() => { if (!user) { onLogin(); return; } setReplyTo(thread); setReply(""); }}>답글</button>
      </div>
    </>;
  }

  const pages = list ? Math.max(1, Math.ceil(list.total / list.pageSize)) : 1;
  const rankPosts = [...(list?.posts ?? [])].filter((item) => item.like_count > 0).sort((a, b) => b.like_count - a.like_count || b.view_count - a.view_count).slice(0, 8);
  const popularPosts = rankPosts.slice(0, 3);
  // Posts, comments and reports record the writer's IP, announced to start on this date.
  const opens = list?.writeOpensEpoch ?? 0;
  const closed = Date.now() / 1000 < opens;
  const opensText = opens ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "long" }).format(opens * 1000) : "";
  const writable = boards.filter((item) => admin || (!item.admin_only && !item.suspension));

  const writeButton = <button type="button" className="board-primary plaza-write" disabled={closed} title={closed ? `${opensText}부터 쓸 수 있습니다` : undefined} onClick={() => startDraft()}>{closed ? `${opensText}부터 글쓰기` : user ? "글쓰기" : "로그인하고 글쓰기"}</button>;

  return <main className="board-page">
    <section className="board-modal" aria-labelledby="board-title">
      <header className="plaza-top">
        <a className="plaza-logo" href="/board" aria-label="광장 · INAZINA 커뮤니티 첫 화면" onClick={(event) => { event.preventDefault(); setBoard(""); setCategory(null); setAsset(""); setSearch(""); setTerm(""); setPage(1); setAdminPostsBoard(""); setDraft(null); setPost(null); history.replaceState(null, "", "/board"); }}><PlazaLogo /><b id="board-title">광장</b><small>INAZINA 커뮤니티</small></a>
          <form className="board-search plaza-search" onSubmit={(event) => { event.preventDefault(); setTerm(search.trim()); setPage(1); }}>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="제목·내용 검색" aria-label="게시글 검색" />
            <button type="submit">검색</button>
          </form>
        <nav className="plaza-util" aria-label="계정">
          {user ? <><span className="plaza-user">{user.nickname}</span><button type="button" aria-expanded={accountOpen} onClick={() => { setAccountOpen((open) => !open); setPost(null); setDraft(null); }}>계정설정</button></> : <button type="button" onClick={onLogin}>로그인</button>}
          {user && <button type="button" onClick={onLogout}>로그아웃</button>}
          <a href="/">HTS</a>
        </nav>
      </header>
      {closed && <p className="board-notice">게시판은 지금 읽기만 할 수 있습니다. 글쓰기·댓글·신고는 개인정보 처리방침 개정에 따라 <b>{opensText}</b>부터 열립니다. 작성·신고 시 접속 IP가 1년간 보관됩니다.</p>}
      {error && <p className="board-error" role="alert">{error}</p>}
      {admin && manageBoards && <section className="board-manager" aria-label="게시판 종류 관리">
        <div className="board-manager-head"><div><h3>게시판 종류 관리</h3><p>게시판의 이름·설명·공개 및 글쓰기 설정을 관리합니다.</p></div><button type="button" disabled={busy} onClick={() => editBoard()}>게시판 추가</button></div>
        {boardDraft && <form className="board-manager-form" onSubmit={saveBoard}>
          <h4>{editingBoardId ? "게시판 수정" : "새 게시판"}</h4>
          <div className="board-manager-fields">
            <label>게시판 ID<input value={boardDraft.boardId} disabled={editingBoardId !== null} required maxLength={32} pattern="[a-z0-9][a-z0-9_-]{0,31}" placeholder="예: lounge" onChange={(event) => setBoardDraft({ ...boardDraft, boardId: event.target.value })} /></label>
            <label>이름<input value={boardDraft.name} required maxLength={30} onChange={(event) => setBoardDraft({ ...boardDraft, name: event.target.value })} /></label>
            <label>정렬 순서<input type="number" min={-1000} max={1000} value={boardDraft.sortOrder} onChange={(event) => setBoardDraft({ ...boardDraft, sortOrder: event.target.value })} /></label>
            <label className="board-manager-description">설명<input value={boardDraft.description} maxLength={200} onChange={(event) => setBoardDraft({ ...boardDraft, description: event.target.value })} /></label>
          </div>
          <div className="board-manager-options">
            <label><input type="checkbox" checked={boardDraft.adminOnly} onChange={(event) => setBoardDraft({ ...boardDraft, adminOnly: event.target.checked })} />관리자만 글쓰기</label>
            <label><input type="checkbox" checked={boardDraft.allowAsset} onChange={(event) => setBoardDraft({ ...boardDraft, allowAsset: event.target.checked })} />종목 코드 연결</label>
            <label><input type="checkbox" checked={boardDraft.secret} onChange={(event) => setBoardDraft({ ...boardDraft, secret: event.target.checked })} />비밀 게시판</label>
            <label><input type="checkbox" checked={boardDraft.news} onChange={(event) => setBoardDraft({ ...boardDraft, news: event.target.checked })} />시장 영향 뉴스 허용</label>
          </div>
          <div className="board-form-actions"><button type="button" disabled={busy} onClick={() => { setBoardDraft(null); setEditingBoardId(null); }}>취소</button><button type="submit" className="board-primary" disabled={busy}>{busy ? "저장 중…" : "저장"}</button></div>
        </form>}
        <ul className="board-manager-list">{boards.map((item) => <li key={item.board_id}>
          <div><button type="button" className="board-manager-name" onClick={() => { setBoard(item.board_id); setCategory(null); setAsset(""); setSearch(""); setTerm(""); setPage(1); setPost(null); setDraft(null); setAdminPostsBoard(item.board_id); setManageBoards(false); setBoardDraft(null); setEditingBoardId(null); }}>{item.name}</button><code>{item.board_id}</code><small>{item.post_count ?? 0}개 글{item.secret ? " · 비밀" : ""}{item.admin_only ? " · 관리자 전용" : ""}</small></div>
          <div className="board-manager-actions"><button type="button" disabled={busy} onClick={() => editBoard(item)}>수정</button>
            {boards.length > 1 && <><label className="sr-only" htmlFor={`board-delete-target-${item.board_id}`}>삭제 시 게시글을 옮길 게시판</label><select id={`board-delete-target-${item.board_id}`} value={deleteTargets[item.board_id] || boards.find((other) => other.board_id !== item.board_id)?.board_id || ""} onChange={(event) => setDeleteTargets({ ...deleteTargets, [item.board_id]: event.target.value })}>{boards.filter((other) => other.board_id !== item.board_id).map((other) => <option key={other.board_id} value={other.board_id}>{other.name}으로 이동</option>)}</select><button type="button" className="danger" disabled={busy} onClick={() => removeBoard(item)}>삭제</button></>}
          </div>
        </li>)}</ul>
      </section>}
      <nav className="plaza-boards plaza-gnb" aria-label="게시판 목록">
          <div className="board-tabs" role="tablist" aria-label="게시판">{[{ board_id: "", name: "전체" }, ...boards].map((item) => <button key={item.board_id || "all"} type="button" role="tab" aria-selected={board === item.board_id} className={board === item.board_id ? "active" : ""} onClick={() => { setBoard(item.board_id); setAdminPostsBoard(""); setCategory(null); setPage(1); if (!boardInfo(item.board_id)?.allow_asset) setAsset(""); }}>{boardInfo(item.board_id)?.secret ? "🔒 " : ""}{item.name}</button>)}</div>
      </nav>
      <div className="board-body plaza-body">
        <div className="plaza-center">
          {user && accountOpen && <BoardAccount user={user} onClose={() => setAccountOpen(false)} onChange={(next) => { setDefaults(next); setCommentAnonymous(next.commentAnonymous); setCommentSecret(next.commentSecret); }} />}
          {(draft || post) && <div className="board-main" ref={mainRef}>
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
            <label className="board-check"><input type="checkbox" checked={draft.secret} onChange={(event) => setDraft({ ...draft, secret: event.target.checked })} />🔒 비밀글 <small>본문은 나와 관리자만 볼 수 있고, 제목은 목록에 보입니다.</small></label>
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
            <button type="button" className="board-post-close" aria-label="글 닫기" onClick={() => setPost(null)}>닫기 ✕</button>
            <p className="board-post-kind">{post.notice ? <b className="board-notice-label">[공지] </b> : null}{post.news ? <b className="board-news-label">📰 뉴스 </b> : null}{post.board_name}{post.category_name ? ` · ${post.category_name}` : ""}{post.asset_code ? ` · ${post.asset_code}` : ""}{post.pinned ? " · 고정" : ""}</p>
            <h3>{post.title}</h3>
            <p className="board-post-meta">{authorView(post.author, post.author_admin)} · {when(post.created_epoch)}{post.updated_epoch > post.created_epoch ? ` (수정 ${when(post.updated_epoch)})` : ""} · 조회 {count(post.view_count)}</p>
            {admin && post.news?.event_type && <p className="board-news-impact">
              시장 영향 · {post.news.event_type === "good" ? "호재" : "악재"} {post.news.severity}% · {post.news.asset_code || "시장 전체"} · {Math.round((post.news.duration_seconds ?? 0) / 60)}분{post.news.target_price ? ` · 목표가 ${count(post.news.target_price)}` : ""}
              {" · "}{NEWS_STATUS[post.news.status]}{post.news.status === "scheduled" ? ` (${when(post.news.publish_epoch)} 게시)` : ""}{post.news.error ? ` — ${post.news.error}` : ""}
              <small>뉴스를 지우거나 숨기면 이후 영향이 멈춥니다(이미 움직인 가격은 그대로).</small></p>}
            <div className={post.locked ? "board-post-body board-locked" : "board-post-body"}>{post.locked ? <>🔒 {post.body}</> : post.body}</div>
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
                    <div><label className="board-check"><input type="checkbox" checked={commentAnonymous} onChange={(event) => setCommentAnonymous(event.target.checked)} />익명</label><label className="board-check"><input type="checkbox" checked={commentSecret} onChange={(event) => setCommentSecret(event.target.checked)} />🔒 비밀</label><span><button type="button" onClick={() => { setReplyTo(null); setReply(""); }}>취소</button> <button className="board-primary" disabled={busy || !reply.trim()}>답글 등록</button></span></div>
                  </form></li>}
                </ul>}
              </li>)}</ul>
              {closed ? <p className="board-empty">댓글은 {opensText}부터 쓸 수 있습니다.</p> : user ? <form className="board-comment-form" onSubmit={submitComment}>
                <textarea value={comment} maxLength={1000} required rows={3} placeholder="댓글을 입력하세요" onChange={(event) => setComment(event.target.value)} />
                <div><label className="board-check"><input type="checkbox" checked={commentAnonymous} onChange={(event) => setCommentAnonymous(event.target.checked)} />익명</label><label className="board-check"><input type="checkbox" checked={commentSecret} onChange={(event) => setCommentSecret(event.target.checked)} />🔒 비밀</label><button className="board-primary" disabled={busy || !comment.trim()}>댓글 등록</button></div>
              </form> : <button type="button" className="board-login" onClick={onLogin}>로그인하고 댓글 쓰기</button>}
            </section>
          </article>}

          </div>}
          <section className="board-list plaza-gallery" aria-label="글 목록">
            <div className="plaza-gall-head">
              <div><h2>{adminPostsBoard ? `게시물 관리 · ${boardInfo(adminPostsBoard)?.name ?? adminPostsBoard}` : `${current?.name ?? "전체"} 갤러리`}</h2>{current?.description && <p>{current.description}</p>}</div>
              <div className="board-head-actions">
          {adminPostsBoard && <button type="button" onClick={() => { setAdminPostsBoard(""); setBoard(""); setCategory(null); setPage(1); setPost(null); }}>게시판 목록</button>}
          {admin && <button type="button" disabled={busy} aria-expanded={manageBoards} onClick={() => { setManageBoards((value) => !value); setBoardDraft(null); setEditingBoardId(null); }}>{manageBoards ? "게시판 관리 닫기" : "게시판 관리"}</button>}
                {writeButton}
              </div>
            </div>
          
          {current?.secret ? <p className="board-description">🔒 허용된 사람만 보는 비밀게시판입니다.</p> : null}
          {current?.suspension && <p className="board-notice">⛔ {current.suspension} 지금은 읽기만 할 수 있습니다.</p>}
          {current && current.categories.length > 0 && <div className="board-categories" aria-label="카테고리">{[{ category_id: 0, name: "전체" }, ...current.categories].map((item) => <button key={item.category_id} type="button" aria-pressed={(category ?? 0) === item.category_id} className={(category ?? 0) === item.category_id ? "active" : ""} onClick={() => { setCategory(item.category_id || null); setPage(1); }}>{item.name}</button>)}</div>}
          {asset && <p className="board-filter">종목 <b>{asset}</b> 토론만 보는 중 <button type="button" onClick={() => { setAsset(""); setPage(1); }}>해제</button></p>}
          <div className="board-table-scroll"><table className="board-post-table"><thead><tr><th>번호</th><th>말머리</th><th>제목</th><th>글쓴이</th><th>작성일</th><th>조회</th><th>추천</th></tr></thead><tbody>{list?.posts.map((item) => <tr key={item.post_id} className={post?.post_id === item.post_id ? "selected" : ""} onClick={() => toggle(item.post_id)}>
            <td className="board-number">{item.notice ? "공지" : item.post_id}</td>
            <td className="board-category-cell">{item.category_name ?? item.asset_code ?? (item.news_status ? "뉴스" : "일반")}</td>
            <td><button type="button" className="board-title-button" onClick={(event) => { event.stopPropagation(); toggle(item.post_id); }}>
              {item.notice ? <em className="board-notice-label">[공지]</em> : item.pinned && item.board === board ? <em className="board-pin">고정</em> : null}{popularPosts.some((popular) => popular.post_id === item.post_id) && <em className="board-popular-label">개념글</em>}{(!board || item.board !== board) && <em className="board-kind">{item.board_name}</em>}{item.asset_code && <em className="board-kind">{item.asset_code}</em>}{item.news_status ? <em className="board-news-label">뉴스{item.news_status === "scheduled" && item.news_publish_epoch ? ` · 예약 ${when(item.news_publish_epoch)}` : item.news_status === "failed" ? " · 게시 실패" : ""}</em> : null}{item.secret ? <span className="board-lock" title="비밀글">🔒 </span> : null}<span className="board-title-text">{item.title}</span>{item.comment_count > 0 && <small className="board-comment-count">[{item.comment_count}]</small>}{item.has_attachments ? <span className="board-icon" title="첨부 있음"> 📎</span> : null}{item.has_poll ? <span className="board-icon" title="설문 있음"> 📊</span> : null}
            </button></td>
            <td className="board-author-cell">{authorView(item.author, item.author_admin)}</td><td className="board-date-cell">{when(item.created_epoch)}</td><td>{count(item.view_count)}</td><td>{count(item.like_count)}</td>
          </tr>)}</tbody></table></div>
          {list && !list.posts.length && <p className="board-empty">{term ? "검색 결과가 없습니다." : "아직 글이 없습니다."}</p>}
            <div className="plaza-gall-foot">
          {pages > 1 && <nav className="board-pages" aria-label="페이지">{(() => {
            const first = Math.floor((page - 1) / 10) * 10 + 1, last = Math.min(pages, first + 9);
            return <>{first > 1 && <button type="button" onClick={() => setPage(first - 1)}>이전</button>}
              {Array.from({ length: last - first + 1 }, (_, index) => first + index).map((number) => <button key={number} type="button" aria-current={number === page ? "page" : undefined} onClick={() => setPage(number)}>{number}</button>)}
              {last < pages && <button type="button" onClick={() => setPage(last + 1)}>다음</button>}</>;
          })()}</nav>}
              {writeButton}
            </div>
          </section>
        </div>
        <aside className="plaza-side" aria-label="실시간 인기글">
          <section className="plaza-me">{user ? <><b>{user.nickname}</b><small>글쓰기와 댓글을 쓸 수 있어요</small><button type="button" onClick={() => { setAccountOpen(true); setPost(null); setDraft(null); window.scrollTo({ top: 0, behavior: "smooth" }); }}>계정설정</button></> : <><b>광장에 오신 것을 환영해요</b><small>로그인하면 글과 댓글을 쓸 수 있어요.</small><button type="button" className="board-primary" onClick={onLogin}>로그인</button></>}</section>
          <section className="plaza-rank plaza-trend"><h3>실시간 검색어</h3>{list?.trending?.length ? <ol>{list.trending.map((item) => <li key={item.term}><button type="button" onClick={() => { setSearch(item.term); setTerm(item.term); setPage(1); setPost(null); }}><i>{item.rank}</i><span>{item.term}</span><small className={item.change === null ? "new" : item.change > 0 ? "up" : item.change < 0 ? "down" : ""}>{item.change === null ? "NEW" : item.change > 0 ? `▲${item.change}` : item.change < 0 ? `▼${-item.change}` : "–"}</small></button></li>)}</ol> : <p className="board-empty">아직 검색 기록이 없습니다.</p>}</section>
          <section className="plaza-rank"><h3>실시간 인기글</h3>{rankPosts.length ? <ol>{rankPosts.map((item, index) => <li key={item.post_id}><button type="button" onClick={() => void open(item.post_id)}><i>{index + 1}</i><span>{item.title}</span>{item.comment_count > 0 && <small>[{item.comment_count}]</small>}</button></li>)}</ol> : <p className="board-empty">아직 추천받은 글이 없습니다.</p>}</section>
        </aside>
      </div>
    </section>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </main>;
}
