import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Field, Tabs, date, number, plain, type Ctx } from "./common";

type Category = { category_id: number; name: string; sort_order: number };
type BoardInfo = { board_id: string; name: string; description: string; admin_only: number; allow_asset: number; sort_order: number; post_count: number; categories: Category[];
  secret: number; news: number; members?: { user_id: string; nickname: string }[]; suspended: number; suspended_reason: string; suspended_until: number | null; suspension: string | null };
// news_status: 뉴스 that moves the market (scheduled → live → ended, or failed).
type Post = { news_status: "scheduled" | "live" | "ended" | "failed" | null; news_publish_epoch: number | null; post_id: number; board: string; board_name: string; category_id: number | null; category_name: string | null; notice: number; author_admin: number; title: string; body: string; asset_code: string | null; pinned: number; status: "active" | "hidden" | "deleted"; hidden_reason: string | null; anonymous: number; author_id: string; nickname: string; author_ip: string; view_count: number; created_epoch: number; comment_count: number; open_reports: number };
type Report = { report_id: number; post_id: number | null; comment_id: number | null; target_post: number; reporter_id: string; reason: string; reporter_ip: string; created_epoch: number; comment_body: string | null; comment_author: string | null; comment_ip: string | null; comment_status: string | null };
type Filter = "reported" | "all" | "active" | "hidden";
type Result = { message: string; success: boolean };

const STATUS: Record<string, [string, string]> = { active: ["게시 중", "tag ok"], hidden: ["숨김", "tag warn"], deleted: ["작성자 삭제", "tag"] };
// Addresses are erased one year after writing (privacy policy).
const ip = (value: string | null) => value || "IP 보관 기간 만료";
const ACTIONS: Record<string, string> = { hide: "숨김", restore: "복원", pin: "상단 고정", unpin: "고정 해제", notice: "공지로 설정", unnotice: "공지 해제", purge: "영구 삭제", resolve: "처리 완료" };

/** HTS board moderation: every post with its real author and IP, and the report queue. */
export function BoardAdminPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [filter, setFilter] = useState<Filter>("reported");
  const [posts, setPosts] = useState<Post[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [boards, setBoards] = useState<BoardInfo[]>([]);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ status: filter === "reported" ? "all" : filter, reported: filter === "reported" ? "1" : "0", search: query });
    const result = await api<{ posts: Post[]; reports: Report[] }>(`/api/admin/board?${params}`);
    setPosts(result.posts);
    setReports(result.reports);
    setBoards((await api<{ boards: BoardInfo[] }>("/api/board/boards")).boards);
  }, [filter, query]);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "게시판을 불러오지 못했습니다.", true)); }, [load]);

  function act(kind: "post" | "comment" | "report", id: number, action: string) {
    const label = `${kind === "post" ? "글" : kind === "comment" ? "댓글" : "신고"} #${id}`;
    const warning = action === "purge" ? "\n\n댓글·좋아요·싫어요·신고까지 함께 지워지며 되돌릴 수 없습니다." : "";
    if (!window.confirm(`${label}을(를) ${ACTIONS[action]}할까요?${warning}`)) return;
    void run(() => api<Result>(`/api/admin/board/${kind}/${id}`, { method: "POST", body: JSON.stringify({ action }) }),
      "처리하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  // Board structure and moving posts: { action, ...fields } to /api/admin/board/{kind}/{id}.
  function structure(kind: "board" | "category" | "post", id: string | number, body: Record<string, unknown>, question?: string) {
    if (question && !window.confirm(question)) return;
    void run(() => api<Result>(`/api/admin/board/${kind}/${encodeURIComponent(String(id))}`, { method: "POST", body: JSON.stringify(body) }),
      "처리하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  const author = (name: string, id: string, anonymous: boolean, address: string, isAdmin = false) => <><span>{name || "—"}{isAdmin && <span className="tag ok inline">관리자</span>}{anonymous && <span className="tag inline">익명 표시</span>}</span><small className="mono muted block">{id} · {ip(address)}</small></>;

  return <>
    <TrendingAdmin ctx={ctx} />
    <Card title="신고 대기" caption={`처리하지 않은 신고 ${number(reports.length)}건 · 서로 다른 3명이 신고한 글·댓글은 자동으로 숨겨집니다. 복원하면 그 대상의 신고가 함께 처리 완료됩니다.`}>
      <div className="scroll-x"><table><thead><tr><th>신고</th><th>대상</th><th>사유</th><th>신고자 · IP</th><th>시각</th><th /></tr></thead><tbody>
        {reports.map((item) => <tr key={item.report_id}>
          <td className="mono">#{item.report_id}</td>
          <td>{item.comment_id
            ? <><span>글 #{item.target_post}의 댓글 #{item.comment_id}{item.comment_status !== "active" && <span className="tag warn inline">{STATUS[item.comment_status ?? ""]?.[0] ?? item.comment_status}</span>}</span><small className="muted block audit-payload">{item.comment_body}</small><small className="mono muted block">{item.comment_author} · {ip(item.comment_ip)}</small></>
            : <>글 #{item.post_id}</>}</td>
          <td className="audit-payload" title={item.reason}>{item.reason}</td>
          <td><span className="mono">{item.reporter_id}</span><small className="mono muted block">{ip(item.reporter_ip)}</small></td>
          <td>{date(item.created_epoch)}</td>
          <td className="row-actions">
            {item.comment_id && (item.comment_status === "active"
              ? <button className="soft small" disabled={busy} onClick={() => act("comment", item.comment_id!, "hide")}>댓글 숨김</button>
              : <button className="soft small" disabled={busy} onClick={() => act("comment", item.comment_id!, "restore")}>댓글 복원</button>)}
            {item.comment_id && <button className="soft small danger" disabled={busy} onClick={() => act("comment", item.comment_id!, "purge")}>댓글 영구 삭제</button>}
            <button className="btn primary small" disabled={busy} onClick={() => act("report", item.report_id, "resolve")}>처리 완료</button>
          </td>
        </tr>)}
      </tbody></table>{!reports.length && <p className="empty">처리할 신고가 없습니다.</p>}</div>
    </Card>

    <Card title="게시글" caption="최근 200건 · 익명 글도 실제 작성자와 접속 IP가 보입니다. IP는 작성 1년 뒤 자동 파기됩니다. 행을 누르면 본문을 펼칩니다. 상단 검색은 제목·내용·작성자 ID를 찾습니다.">
      <Tabs label="게시글 상태" value={filter} onChange={(value) => { setFilter(value); setOpen(null); }} options={[["reported", "신고된 글"], ["all", "전체"], ["active", "게시 중"], ["hidden", "숨김"]]} />
      <div className="scroll-x tall"><table><thead><tr><th>#</th><th>게시판</th><th>제목</th><th>작성자 · IP</th><th>상태</th><th className="r">댓글</th><th className="r">조회</th><th className="r">신고</th><th>작성</th><th /></tr></thead><tbody>
        {posts.map((post) => <Fragment key={post.post_id}>
          <tr className={open === post.post_id ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => setOpen(open === post.post_id ? null : post.post_id)} onKeyDown={(event) => { if (event.key === "Enter") setOpen(open === post.post_id ? null : post.post_id); }}>
            <td className="mono">#{post.post_id}</td>
            <td onClick={(event) => event.stopPropagation()}>
              <select className="mini-select" aria-label={`#${post.post_id} 게시판`} value={post.board} disabled={busy} onChange={(event) => structure("post", post.post_id, { action: "move", board: event.target.value }, `글 #${post.post_id}을(를) ${boards.find((item) => item.board_id === event.target.value)?.name} 게시판으로 옮길까요? 카테고리는 해제됩니다.`)}>{boards.map((item) => <option key={item.board_id} value={item.board_id}>{item.name}</option>)}</select>
              {(boards.find((item) => item.board_id === post.board)?.categories.length ?? 0) > 0 && <select className="mini-select block" aria-label={`#${post.post_id} 카테고리`} value={post.category_id ?? ""} disabled={busy} onChange={(event) => structure("post", post.post_id, { action: "category", categoryId: event.target.value ? Number(event.target.value) : null })}><option value="">카테고리 없음</option>{boards.find((item) => item.board_id === post.board)!.categories.map((item) => <option key={item.category_id} value={item.category_id}>{item.name}</option>)}</select>}
              {post.asset_code && <span className="tag inline">{post.asset_code}</span>}</td>
            <td className="audit-payload" title={post.title}>{post.news_status ? <span className={post.news_status === "failed" ? "tag warn inline-start" : "tag inline-start"} title="시장에 영향을 주는 뉴스입니다. 숨기거나 지우면 영향이 멈춥니다.">📰 {{ scheduled: `예약 ${post.news_publish_epoch ? date(post.news_publish_epoch) : ""}`, live: "뉴스", ended: "영향 종료", failed: "게시 실패" }[post.news_status]}</span> : null}{post.notice ? <span className="tag warn inline-start">공지</span> : null}{post.pinned ? <span className="tag ok inline-start">고정</span> : null}{post.title}</td>
            <td>{author(post.nickname, post.author_id, post.anonymous === 1, post.author_ip, post.author_admin === 1)}</td>
            <td><span className={STATUS[post.status][1]}>{STATUS[post.status][0]}{post.hidden_reason === "reports" ? " · 신고" : ""}</span></td>
            <td className="r">{number(post.comment_count)}</td><td className="r">{number(post.view_count)}</td>
            <td className="r">{post.open_reports ? <span className="tag warn">{number(post.open_reports)}</span> : "—"}</td>
            <td>{date(post.created_epoch)}</td>
            <td className="row-actions" onClick={(event) => event.stopPropagation()}>
              {post.status === "active"
                ? <><button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, post.pinned ? "unpin" : "pin")}>{post.pinned ? "고정 해제" : "고정"}</button><button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, post.notice ? "unnotice" : "notice")}>{post.notice ? "공지 해제" : "공지"}</button><button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, "hide")}>숨김</button></>
                : <button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, "restore")}>복원</button>}
              <button className="soft small danger" disabled={busy} onClick={() => act("post", post.post_id, "purge")}>영구 삭제</button>
            </td>
          </tr>
          {open === post.post_id && <tr className="detail-row"><td colSpan={10}><pre className="board-body-preview">{post.body}</pre></td></tr>}
        </Fragment>)}
      </tbody></table>{!posts.length && <p className="empty">{filter === "reported" ? "신고된 글이 없습니다." : "글이 없습니다."}</p>}</div>
    </Card>
    <BoardStructure busy={busy} boards={boards} onSubmit={structure} />
  </>;
}


/** Boards and their categories: create, rename, reorder, delete. */
function BoardStructure({ busy, boards, onSubmit }: { busy: boolean; boards: BoardInfo[]; onSubmit: (kind: "board" | "category", id: string | number, body: Record<string, unknown>, question?: string) => void }) {
  const blank = { boardId: "", name: "", description: "", adminOnly: false, allowAsset: false, secret: false, news: false, sortOrder: "0" };
  const [draft, setDraft] = useState(blank);
  const [edits, setEdits] = useState<Record<string, typeof blank>>({});
  const [newCategory, setNewCategory] = useState<Record<string, string>>({});
  const [categoryNames, setCategoryNames] = useState<Record<number, string>>({});
  const [moveTo, setMoveTo] = useState<Record<string, string>>({});
  const [newMember, setNewMember] = useState<Record<string, string>>({});
  const [suspend, setSuspend] = useState<Record<string, { reason: string; until: string }>>({});
  const valuesOf = (item: BoardInfo) => edits[item.board_id] ?? { boardId: item.board_id, name: item.name, description: item.description, adminOnly: item.admin_only === 1, allowAsset: item.allow_asset === 1, secret: item.secret === 1, news: item.news === 1, sortOrder: String(item.sort_order) };
  const edit = (item: BoardInfo, change: Partial<typeof blank>) => setEdits({ ...edits, [item.board_id]: { ...valuesOf(item), ...change } });
  const send = (values: typeof blank) => ({ name: values.name, description: values.description, adminOnly: values.adminOnly, allowAsset: values.allowAsset, secret: values.secret, news: values.news, sortOrder: Number(values.sortOrder) || 0 });

  return <Card title="게시판 · 카테고리" caption="게시판을 만들고 이름·설명·순서·쓰기 권한을 바꿉니다. 비밀게시판은 허용 목록의 Discord 아이디와 관리자만 보고 씁니다. 정지한 게시판은 읽기·신고만 됩니다(관리자는 계속 씀). 글이 있는 게시판을 지우려면 글을 옮길 게시판을 고르세요.">
    <div className="scroll-x"><table><thead><tr><th>ID</th><th>이름</th><th>설명</th><th className="r">순서</th><th>쓰기</th><th>종목 코드</th><th>비밀</th><th>뉴스</th><th className="r">글</th><th>카테고리</th><th>정지</th><th /></tr></thead><tbody>
      {boards.map((item) => {
        const values = valuesOf(item);
        const others = boards.filter((other) => other.board_id !== item.board_id);
        return <tr key={item.board_id}>
          <td className="mono">{item.board_id}</td>
          <td><input className="mini-input" aria-label={`${item.board_id} 이름`} value={values.name} maxLength={30} onChange={(event) => edit(item, { name: event.target.value })} /></td>
          <td><input className="mini-input wide" aria-label={`${item.board_id} 설명`} value={values.description} maxLength={200} onChange={(event) => edit(item, { description: event.target.value })} /></td>
          <td className="r"><input className="mini-input narrow" type="number" aria-label={`${item.board_id} 순서`} value={values.sortOrder} onChange={(event) => edit(item, { sortOrder: event.target.value })} /></td>
          <td><label className="check"><input type="checkbox" checked={values.adminOnly} onChange={(event) => edit(item, { adminOnly: event.target.checked })} />관리자만</label></td>
          <td><label className="check"><input type="checkbox" checked={values.allowAsset} onChange={(event) => edit(item, { allowAsset: event.target.checked })} />허용</label></td>
          <td><label className="check"><input type="checkbox" checked={values.secret} onChange={(event) => edit(item, { secret: event.target.checked })} />비밀</label>
            {item.secret === 1 && <div className="chips">{(item.members ?? []).map((member) => <span key={member.user_id} className="category-chip"><span className="tag" title={member.user_id}>{member.nickname || member.user_id} <small className="mono muted">{member.user_id}</small></span>
              <button className="soft small danger" type="button" disabled={busy} title="허용 목록에서 빼기" onClick={() => onSubmit("board", item.board_id, { action: "member_remove", userId: member.user_id }, `${member.nickname || member.user_id}을(를) ${item.name} 허용 목록에서 뺄까요?`)}>✕</button></span>)}
              <span className="category-chip"><input className="mini-input mono" aria-label={`${item.name}에 허용할 Discord 아이디`} placeholder="Discord 아이디" value={newMember[item.board_id] ?? ""} onChange={(event) => setNewMember({ ...newMember, [item.board_id]: event.target.value.trim() })} />
                <button className="soft small" type="button" disabled={busy || !(newMember[item.board_id] ?? "")} onClick={() => { onSubmit("board", item.board_id, { action: "member_add", userId: newMember[item.board_id] }); setNewMember({ ...newMember, [item.board_id]: "" }); }}>허용</button></span>
              {!(item.members ?? []).length && <small className="muted">아직 허용된 사람이 없습니다(관리자만 봄).</small>}</div>}</td>
          <td><label className="check" title="관리자 글에 시장 영향(호재·악재)을 넣을 수 있는 게시판"><input type="checkbox" checked={values.news} onChange={(event) => edit(item, { news: event.target.checked })} />뉴스</label></td>
          <td className="r">{number(item.post_count)}</td>
          <td><div className="chips">{item.categories.map((category) => <span key={category.category_id} className="category-chip">
            <input className="mini-input" aria-label={`카테고리 ${category.name}`} value={categoryNames[category.category_id] ?? category.name} maxLength={20} onChange={(event) => setCategoryNames({ ...categoryNames, [category.category_id]: event.target.value })} />
            {(categoryNames[category.category_id] ?? category.name) !== category.name && <button className="soft small" type="button" disabled={busy} onClick={() => onSubmit("category", category.category_id, { action: "edit", name: categoryNames[category.category_id], sortOrder: category.sort_order })}>저장</button>}
            <button className="soft small danger" type="button" disabled={busy} title="카테고리 삭제" onClick={() => onSubmit("category", category.category_id, { action: "delete" }, `카테고리 "${category.name}"을(를) 지울까요? 그 글은 카테고리 없이 남습니다.`)}>✕</button>
          </span>)}
            <span className="category-chip"><input className="mini-input" aria-label={`${item.name} 새 카테고리`} placeholder="새 카테고리" maxLength={20} value={newCategory[item.board_id] ?? ""} onChange={(event) => setNewCategory({ ...newCategory, [item.board_id]: event.target.value })} />
              <button className="soft small" type="button" disabled={busy || !(newCategory[item.board_id] ?? "").trim()} onClick={() => { onSubmit("category", "new", { action: "create", boardId: item.board_id, name: newCategory[item.board_id], sortOrder: item.categories.length }); setNewCategory({ ...newCategory, [item.board_id]: "" }); }}>추가</button></span>
          </div></td>
          <td>{item.suspension
            ? <><span className="tag warn" title={item.suspension}>정지 중{item.suspended_until ? ` · ${date(item.suspended_until)}까지` : ""}</span>{item.suspended_reason && <small className="muted block">{item.suspended_reason}</small>}
                <button className="soft small" type="button" disabled={busy} onClick={() => onSubmit("board", item.board_id, { action: "resume" }, `${item.name} 게시판의 정지를 풀까요?`)}>정지 해제</button></>
            : suspend[item.board_id]
              ? <div className="suspend-form"><input className="mini-input wide" placeholder="사유 (선택)" maxLength={200} value={suspend[item.board_id].reason} onChange={(event) => setSuspend({ ...suspend, [item.board_id]: { ...suspend[item.board_id], reason: event.target.value } })} />
                  <input className="mini-input" type="datetime-local" aria-label="해제 예정 (선택)" title="비우면 직접 풀 때까지" value={suspend[item.board_id].until} onChange={(event) => setSuspend({ ...suspend, [item.board_id]: { ...suspend[item.board_id], until: event.target.value } })} />
                  <button className="btn primary small" type="button" disabled={busy} onClick={() => { const value = suspend[item.board_id]; onSubmit("board", item.board_id, { action: "suspend", reason: value.reason, until: value.until ? Math.floor(new Date(value.until).getTime() / 1000) : null }, `${item.name} 게시판을 정지할까요? 읽기·신고만 되고 관리자 외에는 글·댓글·반응·투표를 할 수 없습니다.`); const next = { ...suspend }; delete next[item.board_id]; setSuspend(next); }}>정지</button>
                  <button className="soft small" type="button" onClick={() => { const next = { ...suspend }; delete next[item.board_id]; setSuspend(next); }}>취소</button></div>
              : <button className="soft small" type="button" disabled={busy} onClick={() => setSuspend({ ...suspend, [item.board_id]: { reason: "", until: "" } })}>정지…</button>}</td>
          <td className="row-actions">
            <button className="btn primary small" type="button" disabled={busy || !edits[item.board_id]} onClick={() => { onSubmit("board", item.board_id, { action: "edit", ...send(values) }); const next = { ...edits }; delete next[item.board_id]; setEdits(next); }}>저장</button>
            {item.post_count > 0 && <select className="mini-select" aria-label={`${item.name} 글을 옮길 게시판`} value={moveTo[item.board_id] ?? ""} onChange={(event) => setMoveTo({ ...moveTo, [item.board_id]: event.target.value })}><option value="">글 옮길 곳</option>{others.map((other) => <option key={other.board_id} value={other.board_id}>{other.name}</option>)}</select>}
            <button className="soft small danger" type="button" disabled={busy || boards.length <= 1 || (item.post_count > 0 && !moveTo[item.board_id])}
              title={item.post_count > 0 && !moveTo[item.board_id] ? "글을 옮길 게시판을 먼저 고르세요" : "게시판 삭제"}
              onClick={() => onSubmit("board", item.board_id, { action: "delete", moveTo: moveTo[item.board_id] }, `${item.name} 게시판을 지울까요?${item.post_count ? ` 글 ${item.post_count}개는 ${boards.find((other) => other.board_id === moveTo[item.board_id])?.name}(으)로 옮겨지고 카테고리는 해제됩니다.` : ""}`)}>삭제</button>
          </td>
        </tr>;
      })}
    </tbody></table></div>
    <h3 className="sub-head">새 게시판</h3>
    <form className="board-create-form" onSubmit={(event) => { event.preventDefault(); onSubmit("board", "new", { action: "create", boardId: draft.boardId, ...send(draft) }); setDraft(blank); }}>
      <div className="board-create-fields">
        <Field label="게시판 ID" hint="영문 소문자·숫자·_·- (바꿀 수 없음)"><input className="mono" value={draft.boardId} maxLength={32} required onChange={(event) => setDraft({ ...draft, boardId: event.target.value.toLowerCase() })} /></Field>
        <Field label="이름"><input value={draft.name} maxLength={30} required onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></Field>
        <Field label="설명" hint="선택"><input value={draft.description} maxLength={200} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></Field>
        <Field label="순서" hint="작을수록 앞"><input type="number" value={draft.sortOrder} onChange={(event) => setDraft({ ...draft, sortOrder: event.target.value })} /></Field>
      </div>
      <fieldset className="board-create-options">
        <legend>게시판 설정</legend>
        <label className="board-create-option"><input type="checkbox" checked={draft.adminOnly} onChange={(event) => setDraft({ ...draft, adminOnly: event.target.checked })} /><span>관리자만 쓰기</span></label>
        <label className="board-create-option"><input type="checkbox" checked={draft.allowAsset} onChange={(event) => setDraft({ ...draft, allowAsset: event.target.checked })} /><span>종목 코드 허용</span></label>
        <label className="board-create-option"><input type="checkbox" checked={draft.news} onChange={(event) => setDraft({ ...draft, news: event.target.checked })} /><span><b>뉴스 게시판</b><small>관리자 글에 시장 영향 설정을 사용할 수 있습니다.</small></span></label>
        <label className="board-create-option"><input type="checkbox" checked={draft.secret} onChange={(event) => setDraft({ ...draft, secret: event.target.checked })} /><span><b>비밀게시판</b><small>게시판을 만든 뒤 허용할 Discord 아이디를 추가합니다.</small></span></label>
      </fieldset>
      <div className="board-create-actions"><button className="btn primary" disabled={busy || !draft.boardId || !draft.name.trim()}>게시판 만들기</button></div>
    </form>
  </Card>;
}

type Trending = { terms: { term: string; searches: number; last_epoch: number; blocked: number }[]; trending: { term: string; rank: number }[]; blocked: { term: string; blocked_epoch: number }[];
  pinned: { term: string; rank: number; until_epoch: number | null }[] };

/** 실시간 검색어 관리: see what people search, keep a term off the public list, or clear its count. */
function TrendingAdmin({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Trending | null>(null);
  const [term, setTerm] = useState("");
  const [rank, setRank] = useState("1");
  const [hours, setHours] = useState("");
  const [boost, setBoost] = useState("10");
  const load = useCallback(async () => setData(await api<Trending>("/api/admin/board?view=search")), []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "검색어를 불러오지 못했습니다.", true)); }, [load]);
  const act = (target: string, action: "block" | "unblock" | "reset" | "pin" | "unpin" | "boost", confirmText?: string, extra: Record<string, unknown> = {}) => {
    if (confirmText && !window.confirm(confirmText)) return;
    void run(() => api<{ message: string; success: boolean }>(`/api/admin/board/search/${encodeURIComponent(target)}`, { method: "POST", body: JSON.stringify({ action, ...extra }) }),
      "검색어를 처리하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  };
  const live = new Map((data?.trending ?? []).map((item) => [item.term, item.rank]));
  return <Card title="실시간 검색어" caption="광장 오른쪽 '실시간 검색어' 순위(최근 24시간)를 관리합니다. 고정한 검색어는 그 순위에 보이고 나머지는 실제 검색 순으로 채워집니다. 검색 수 추가는 지금 검색된 것으로 집계합니다. 숨긴 검색어는 순위에서 빠지지만 검색은 그대로 됩니다. 방문자에게는 고정·추가 여부가 보이지 않습니다."
    actions={<button className="soft small" type="button" onClick={() => void load()}>새로고침</button>}>
    <form className="form-grid" onSubmit={(event) => event.preventDefault()}>
      <Field label="검색어" hint="아직 순위에 없는 검색어도 넣을 수 있습니다"><input value={term} maxLength={50} onChange={(event) => setTerm(event.target.value)} placeholder="예: 신규 이벤트" /></Field>
      <Field label="고정 순위"><select value={rank} onChange={(event) => setRank(event.target.value)}>{Array.from({ length: 10 }, (_, index) => <option key={index} value={index + 1}>{index + 1}위</option>)}</select></Field>
      <Field label="고정 시간" hint="비우면 직접 해제할 때까지"><input inputMode="decimal" value={hours} onChange={(event) => setHours(event.target.value.replace(/[^\d.]/g, ""))} placeholder="시간" /></Field>
      <Field label="추가할 검색 수" hint="최대 1,000회"><input inputMode="numeric" value={boost} onChange={(event) => setBoost(event.target.value.replace(/\D/g, ""))} /></Field>
      <div className="actions span-wide">
        <button className="btn primary" type="button" disabled={busy || !term.trim()} onClick={() => act(term.trim(), "pin", undefined, { rank: Number(rank), hours: hours || null })}>{rank}위에 고정</button>
        <button className="soft" type="button" disabled={busy || !term.trim() || !Number(boost)} onClick={() => act(term.trim(), "boost", undefined, { count: Number(boost) })}>검색 수 +{boost || 0}</button>
        <button className="soft danger" type="button" disabled={busy || !term.trim()} onClick={() => { act(term.trim(), "block"); setTerm(""); }}>숨기기</button>
      </div>
    </form>
    {(data?.pinned.length ?? 0) > 0 && <>
      <h3 className="sub-head">순위 고정 {data!.pinned.length}개</h3>
      <div className="command-list">{data!.pinned.map((item) => <button key={item.term} type="button" className="command-chip active" disabled={busy} title="눌러서 고정 해제" onClick={() => act(item.term, "unpin")}><b>{item.rank}위 · {item.term}</b><small>{item.until_epoch ? `${date(item.until_epoch)}까지` : "해제할 때까지"} · 눌러서 해제</small></button>)}</div>
    </>}
    <div className="scroll-x"><table><thead><tr><th>순위</th><th>검색어</th><th className="r">24시간 검색</th><th>마지막 검색</th><th>상태</th><th /></tr></thead><tbody>
      {(data?.terms ?? []).map((item) => <tr key={item.term}>
        <td>{live.has(item.term) ? `${live.get(item.term)}위` : "—"}</td>
        <td><button type="button" className="link" onClick={() => setTerm(item.term)} title="위 입력칸에 넣기">{item.term}</button></td>
        <td className="r mono">{number(item.searches)}</td>
        <td>{date(item.last_epoch)}</td>
        <td>{item.blocked ? <span className="tag warn">숨김</span> : live.has(item.term) ? <span className="tag ok">노출</span> : <span className="tag">순위 밖</span>}</td>
        <td className="row-actions">{item.blocked
          ? <button className="soft small" type="button" disabled={busy} onClick={() => act(item.term, "unblock")}>숨김 해제</button>
          : <button className="soft small" type="button" disabled={busy} onClick={() => act(item.term, "block")}>숨기기</button>}
          <button className="soft small danger" type="button" disabled={busy} onClick={() => act(item.term, "reset", `'${item.term}'의 검색 집계를 지울까요? 순위에서 바로 빠집니다.`)}>초기화</button></td>
      </tr>)}
    </tbody></table>{data && !data.terms.length && <p className="empty">최근 24시간 동안 검색 기록이 없습니다.</p>}</div>
    {(data?.blocked.length ?? 0) > 0 && <>
      <h3 className="sub-head">숨긴 검색어 {data!.blocked.length}개</h3>
      <div className="command-list">{data!.blocked.map((item) => <button key={item.term} type="button" className="command-chip" disabled={busy} title="눌러서 숨김 해제" onClick={() => act(item.term, "unblock")}><b>{item.term}</b><small>{date(item.blocked_epoch)} · 눌러서 해제</small></button>)}</div>
    </>}
  </Card>;
}
