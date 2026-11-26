import { escapeHtml } from "./docHtml";

/** One announced change to a document, as the server writes it to public/links/notices.json (nothing about who saved it). */
export type Notice = {
  id: number;
  document: string;
  title: string;
  status: "open" | "applied" | "cancelled" | string;
  announcedEpoch: number;
  effectiveEpoch: number;
  unfavourable: boolean;
  changes: { section: string; before: string[]; after: string[] }[];
};

const day = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "long", timeZone: "Asia/Seoul" }).format(epoch * 1000);
const STATE: Record<string, string> = { open: "시행 예정", applied: "시행 중", cancelled: "취소됨" };
const CONSENT = "이용자에게 불리하거나 중요한 변경입니다. Discord DM으로 보내 드린 개정안의 '동의합니다' 버튼을 눌러야 시행일 이후에도 서비스를 계속 이용할 수 있습니다.";
const PLAIN = "일반 변경입니다. 시행일 이후에도 계속 이용하면 동의한 것으로 봅니다.";
const list = (lines: string[]) => lines.length ? `<ul>${lines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>` : `<p class="none">(없음)</p>`;

function renderNotice(notice: Notice): string {
  const state = STATE[notice.status] ?? notice.status;
  const consent = notice.status === "cancelled" ? "" : `<p class="notice-consent${notice.unfavourable ? " must" : ""}">${notice.unfavourable ? CONSENT : PLAIN}</p>`;
  const compare = notice.changes.length
    ? notice.changes.map((change) => `<h3>${escapeHtml(change.section)}</h3><table class="compare"><thead><tr><th>변경 전</th><th>변경 후</th></tr></thead><tbody><tr><td>${list(change.before)}</td><td>${list(change.after)}</td></tr></tbody></table>`).join("")
    : `<p class="none">문구는 바뀌지 않았고 날짜와 개정 안내만 바뀌었습니다.</p>`;
  return `<article class="notice" id="notice-${notice.id}">
<header><span class="badge ${escapeHtml(notice.status)}">${escapeHtml(state)}</span><h2>${escapeHtml(notice.title)} 개정 안내</h2>
<p class="meta">공지 ${day(notice.announcedEpoch)} · 시행 ${day(notice.effectiveEpoch)} · <a href="../${escapeHtml(notice.document)}/">전문 보기</a></p></header>
${consent}
${notice.status === "cancelled" ? "<p>예고했던 개정이 취소되어 지금 적용 중인 문서가 계속 적용됩니다.</p>" : compare}
</article>`;
}

export function renderNotices(notices: Notice[]): string {
  return notices.length ? notices.map(renderNotice).join("\n") : `<p class="none">아직 공지가 없습니다.</p>`;
}

/** A line on the home page while a change is announced and not yet in force (the newest one). */
export function renderNoticeBar(notices: Notice[], href: string): string {
  const open = notices.filter((notice) => notice.status === "open").sort((a, b) => b.announcedEpoch - a.announcedEpoch)[0];
  if (!open) return "";
  return `<aside class="announce-bar"><p>📢 <b>${escapeHtml(open.title)}</b> 개정 예정 — ${day(open.effectiveEpoch)} 시행</p><a href="${escapeHtml(href)}">변경 내용 보기</a></aside>`;
}

/** An Atom feed of the notices (one entry each, linking to its place on the notice page). */
export function renderFeed(notices: Notice[], site: string, updated: number): string {
  const iso = (epoch: number) => new Date(epoch * 1000).toISOString();
  const entries = notices.map((notice) => `  <entry>
    <title>${escapeHtml(notice.title)} 개정 안내 (${escapeHtml(STATE[notice.status] ?? notice.status)})</title>
    <link href="${escapeHtml(site)}notices/#notice-${notice.id}"/>
    <id>${escapeHtml(site)}notices/#notice-${notice.id}</id>
    <updated>${iso(notice.announcedEpoch)}</updated>
    <summary>시행 ${day(notice.effectiveEpoch)}${notice.unfavourable ? " · 이용자에게 불리하거나 중요한 변경" : ""}</summary>
  </entry>`).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n  <title>INAZINA 공지</title>\n  <link href="${escapeHtml(site)}notices/"/>\n  <id>${escapeHtml(site)}notices/</id>\n  <updated>${iso(updated)}</updated>\n${entries}\n</feed>\n`;
}
