import "./fonts";
import { renderDocumentHtml } from "./docHtml";

// A revision announced in the dashboard is listed in links/pending.json (and its text is links/<name>.next.txt). The current
// text stays in force until then, so the page shows it and offers the announced revision.
type Pending = Record<string, { effectiveEpoch: number; announcedEpoch: number }>;
const BASE = import.meta.env.BASE_URL;
const day = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "long", timeZone: "Asia/Seoul" }).format(epoch * 1000);

async function announce(name: string, body: HTMLElement) {
  let pending: Pending = {};
  try { const response = await fetch(`${BASE}links/pending.json`, { cache: "no-cache" }); if (response.ok) pending = await response.json() as Pending; } catch { return; }
  const entry = pending[name];
  if (!entry || entry.effectiveEpoch * 1000 <= Date.now()) return;
  const current = body.innerHTML;
  const notice = document.createElement("div");
  notice.className = "doc-revision";
  const text = document.createElement("p");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "btn linkout";
  const showCurrent = () => { body.innerHTML = current; text.textContent = `📢 개정 예정: ${day(entry.effectiveEpoch)}부터 새 내용이 적용됩니다. (공지 ${day(entry.announcedEpoch)})`; toggle.textContent = "개정안 보기"; toggle.onclick = showRevision; };
  const showRevision = async () => {
    const response = await fetch(`${BASE}links/${name}.next.txt`, { cache: "no-cache" });
    if (!response.ok) { text.textContent = "개정안을 불러오지 못했습니다."; return; }
    body.innerHTML = renderDocumentHtml(await response.text());
    text.textContent = `개정안입니다. ${day(entry.effectiveEpoch)}부터 적용되며, 그 전까지는 현재 문서가 적용됩니다.`;
    toggle.textContent = "현재 문서 보기"; toggle.onclick = showCurrent;
  };
  showCurrent();
  notice.append(text, toggle);
  body.before(notice);
}

const name = document.body.dataset.doc;
const body = document.querySelector<HTMLElement>("#doc-body");
if (name && body) void announce(name, body);
