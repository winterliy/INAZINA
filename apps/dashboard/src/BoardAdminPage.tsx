import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Tabs, date, number, plain, type Ctx } from "./common";

type Post = { post_id: number; board: string; title: string; body: string; asset_code: string | null; pinned: number; status: "active" | "hidden" | "deleted"; hidden_reason: string | null; anonymous: number; author_id: string; nickname: string; author_ip: string; view_count: number; created_epoch: number; comment_count: number; open_reports: number };
type Report = { report_id: number; post_id: number | null; comment_id: number | null; target_post: number; reporter_id: string; reason: string; reporter_ip: string; created_epoch: number; comment_body: string | null; comment_author: string | null; comment_ip: string | null; comment_status: string | null };
type Filter = "reported" | "all" | "active" | "hidden" | "deleted";
type Result = { message: string; success: boolean };

const BOARD: Record<string, string> = { notice: "공지", free: "자유", market: "시장 토론" };
const STATUS: Record<string, [string, string]> = { active: ["게시 중", "tag ok"], hidden: ["숨김", "tag warn"], deleted: ["작성자 삭제", "tag"] };
// Addresses are erased one year after writing (privacy policy).
const ip = (value: string | null) => value || "IP 보관 기간 만료";
const ACTIONS: Record<string, string> = { hide: "숨김", restore: "복원", pin: "상단 고정", unpin: "고정 해제", purge: "영구 삭제", resolve: "처리 완료" };

/** HTS board moderation: every post with its real author and IP, and the report queue. */
export function BoardAdminPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [filter, setFilter] = useState<Filter>("reported");
  const [posts, setPosts] = useState<Post[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ status: filter === "reported" ? "all" : filter, reported: filter === "reported" ? "1" : "0", search: query });
    const result = await api<{ posts: Post[]; reports: Report[] }>(`/api/admin/board?${params}`);
    setPosts(result.posts);
    setReports(result.reports);
  }, [filter, query]);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "게시판을 불러오지 못했습니다.", true)); }, [load]);

  function act(kind: "post" | "comment" | "report", id: number, action: string) {
    const label = `${kind === "post" ? "글" : kind === "comment" ? "댓글" : "신고"} #${id}`;
    const warning = action === "purge" ? "\n\n댓글·추천·신고까지 함께 지워지며 되돌릴 수 없습니다." : "";
    if (!window.confirm(`${label}을(를) ${ACTIONS[action]}할까요?${warning}`)) return;
    void run(() => api<Result>(`/api/admin/board/${kind}/${id}`, { method: "POST", body: JSON.stringify({ action }) }),
      "처리하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  const author = (name: string, id: string, anonymous: boolean, address: string) => <><span>{name || "—"}{anonymous && <span className="tag inline">익명 표시</span>}</span><small className="mono muted block">{id} · {ip(address)}</small></>;

  return <>
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
      <Tabs label="게시글 상태" value={filter} onChange={(value) => { setFilter(value); setOpen(null); }} options={[["reported", "신고된 글"], ["all", "전체"], ["active", "게시 중"], ["hidden", "숨김"], ["deleted", "작성자 삭제"]]} />
      <div className="scroll-x tall"><table><thead><tr><th>#</th><th>게시판</th><th>제목</th><th>작성자 · IP</th><th>상태</th><th className="r">댓글</th><th className="r">조회</th><th className="r">신고</th><th>작성</th><th /></tr></thead><tbody>
        {posts.map((post) => <Fragment key={post.post_id}>
          <tr className={open === post.post_id ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => setOpen(open === post.post_id ? null : post.post_id)} onKeyDown={(event) => { if (event.key === "Enter") setOpen(open === post.post_id ? null : post.post_id); }}>
            <td className="mono">#{post.post_id}</td>
            <td>{BOARD[post.board] ?? post.board}{post.asset_code && <span className="tag inline">{post.asset_code}</span>}</td>
            <td className="audit-payload" title={post.title}>{post.pinned ? <span className="tag ok inline-start">고정</span> : null}{post.title}</td>
            <td>{author(post.nickname, post.author_id, post.anonymous === 1, post.author_ip)}</td>
            <td><span className={STATUS[post.status][1]}>{STATUS[post.status][0]}{post.hidden_reason === "reports" ? " · 신고" : ""}</span></td>
            <td className="r">{number(post.comment_count)}</td><td className="r">{number(post.view_count)}</td>
            <td className="r">{post.open_reports ? <span className="tag warn">{number(post.open_reports)}</span> : "—"}</td>
            <td>{date(post.created_epoch)}</td>
            <td className="row-actions" onClick={(event) => event.stopPropagation()}>
              {post.status === "active"
                ? <><button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, post.pinned ? "unpin" : "pin")}>{post.pinned ? "고정 해제" : "고정"}</button><button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, "hide")}>숨김</button></>
                : <button className="soft small" disabled={busy} onClick={() => act("post", post.post_id, "restore")}>복원</button>}
              <button className="soft small danger" disabled={busy} onClick={() => act("post", post.post_id, "purge")}>영구 삭제</button>
            </td>
          </tr>
          {open === post.post_id && <tr className="detail-row"><td colSpan={10}><pre className="board-body-preview">{post.body}</pre></td></tr>}
        </Fragment>)}
      </tbody></table>{!posts.length && <p className="empty">{filter === "reported" ? "신고된 글이 없습니다." : "글이 없습니다."}</p>}</div>
    </Card>
  </>;
}
