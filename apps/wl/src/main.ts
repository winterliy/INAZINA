// Wide-Learning viewer: renders docs/wide-learning read-only, and shows the
// CThread (CLAUDE) ↔ GThread (CODEX) records as a chat. Thread text can contain
// web-sourced sentences, so every string is HTML-escaped before rendering.

import { PAGES, escapeHtml, filterThread, finalStatus, ids, inline, markdown, registry as registryOf, threadEntries as entriesOf,
  topics as topicsOf, type Entry, type Files } from "./wlmd.ts";
import { DEFAULTS, THEMES, ZOOMS, apply, load, save, type Settings } from "./settings.ts";

let files: Files = {};
let check = { claude: "", codex: "" };
let topicFilter = "all";
let settings: Settings = load(PAGES.map(([id]) => id));
apply(settings);

const threadEntries = () => entriesOf(files);
const registry = () => registryOf(files);
const topics = () => topicsOf(files);

// ---------------------------------------------------------------- views
const view = document.getElementById("view")!;

function renderNav(active: string) {
  const all = threadEntries();
  document.getElementById("nav")!.innerHTML = PAGES.map(([id, label], i) =>
    `<a href="#/${id}" class="tab${id === active ? " active" : ""}"${id === active ? ' aria-current="page"' : ""}><span class="tab-no">${i + 1}</span>${escapeHtml(label)}${id === "chat" && all.length ? `<span class="count">${all.length}</span>` : ""}</a>`).join("");
  const file = PAGES.find(([id]) => id === active)?.[2];
  document.getElementById("where")!.textContent = file ? `${active} · ${file}` : active;
  document.querySelector(".tabs .active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

/** A block progress bar, e.g. ███░░░ — the TUI design system's "progress" primitive. */
const bar = (part: number, total: number, width = 12) => {
  const filled = total ? Math.round((part / total) * width) : 0;
  return `<span class="bar" aria-label="${part}/${total}">${"█".repeat(filled)}<span>${"░".repeat(width - filled)}</span></span>`;
};

function docPage(file: string, intro = "") {
  return `<article class="doc">${intro}${markdown(files[file] || "_(파일이 비어 있습니다)_")}<p class="file-path">docs/wide-learning/${escapeHtml(file)}</p></article>`;
}

function statusPage(): string {
  const all = threadEntries();
  const claims = all.filter((entry) => entry.fields.type === "claim");
  const reviews = all.filter((entry) => entry.fields.type === "review");
  const counts = (author: string) => all.filter((entry) => entry.author === author).length;
  const statusCount = (status: string) => claims.filter((claim) => finalStatus(claim, all) === status).length;
  const topicCards = topics().map((topic) => {
    const mine = claims.filter((claim) => claim.topic === topic.id);
    const breakdown = ["proposed", "verified", "simulator-assumption", "disputed", "rejected"]
      .map((status) => [status, mine.filter((claim) => finalStatus(claim, all) === status).length] as const).filter(([, n]) => n);
    return `<article class="topic ${escapeHtml(topic.fields.status ?? "")}">
      <header><span class="tid">${escapeHtml(topic.id)}</span><span class="chip status-${escapeHtml(topic.fields.status ?? "")}">${escapeHtml(topic.fields.status ?? "")}</span></header>
      <h3>${inline(topic.title)}</h3>
      <p>${inline(topic.fields.question ?? "")}</p>
      <p class="progress">${bar(mine.filter((claim) => finalStatus(claim, all) === "verified").length, mine.length)} <span class="muted">확정 ${mine.filter((claim) => finalStatus(claim, all) === "verified").length}/${mine.length}</span></p>
      <footer><span>Laplace ${escapeHtml(topic.fields.laplace ?? "-")}장</span>
        <span>${breakdown.length ? breakdown.map(([status, n]) => `<span class="chip status-${status}">${status} ${n}</span>`).join(" ") : "주장 없음"}</span>
        ${mine.length ? `<a href="#/chat?topic=${escapeHtml(topic.id)}">대화 보기 →</a>` : ""}</footer>
    </article>`;
  }).join("");
  return `<article class="doc wide">
    <h1>진행 현황</h1>
    <div class="stats">
      <div><small>CThread · CLAUDE</small><b>${counts("CLAUDE")}</b><span>기록</span></div>
      <div><small>GThread · CODEX</small><b>${counts("CODEX")}</b><span>기록</span></div>
      <div><small>주장 (claim)</small><b>${claims.length}</b><span>검토 ${reviews.length}</span></div>
      <div><small>확정 (verified)</small><b>${statusCount("verified")}</b><span>가정 ${statusCount("simulator-assumption")}</span></div>
      <div><small>쟁점 (disputed)</small><b>${statusCount("disputed")}</b><span>사용자 판단 대기</span></div>
    </div>
    <h2>주제</h2>
    <p class="muted">approved 주제만 진행합니다. draft는 사용자가 승인해야 시작합니다.</p>
    <div class="topics">${topicCards}</div>
    <h2>검사기 결과 · 할 일</h2>
    <p class="muted"><code>python3 tools/wlmd_check.py --todo CLAUDE|CODEX</code> 출력입니다. 파일이 바뀌면 바로 다시 실행됩니다.</p>
    <div class="checks"><section><h3>CLAUDE</h3><pre>${escapeHtml(check.claude || "실행 중…")}</pre></section>
      <section><h3>CODEX</h3><pre>${escapeHtml(check.codex || "실행 중…")}</pre></section></div>
  </article>`;
}

const TYPE_LABEL: Record<string, string> = { claim: "주장", question: "질문", review: "검토", correction: "정정·확정", summary: "요약" };
const VERDICT_LABEL: Record<string, string> = { agree: "동의", partial: "부분 동의", "needs-source": "출처 필요", dispute: "반박" };

function bubble(entry: Entry, all: Entry[], sources: Map<string, Entry>, focus: string): string {
  const f = entry.fields;
  const side = entry.author === "CLAUDE" ? "left" : "right";
  const sourceChips = ids(f.sources).map((id) => {
    const source = sources.get(id);
    const tip = source ? `${source.title} · ${source.fields.publisher ?? ""} · ${source.fields.tier ?? ""}` : "등록되지 않은 출처";
    const url = source?.fields.url;
    return url && /^https?:\/\//.test(url)
      ? `<a class="source tier-${escapeHtml(source?.fields.tier ?? "")}" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(tip)}">${escapeHtml(id)}</a>`
      : `<span class="source missing" title="${escapeHtml(tip)}">${escapeHtml(id)}</span>`;
  }).join("");
  const status = f.type === "claim" ? finalStatus(entry, all) : f.status;
  const meta = [
    f.tier && `<span class="chip tier-${escapeHtml(f.tier)}">${escapeHtml(f.tier)}</span>`,
    f.confidence && `<span class="chip">신뢰도 ${escapeHtml(f.confidence)}</span>`,
    status && `<span class="chip status-${escapeHtml(status)}">${escapeHtml(status)}${f.type === "claim" && status !== f.status ? " (현재)" : ""}</span>`,
    f.laplace && `<span class="chip">Laplace ${escapeHtml(f.laplace)}</span>`,
  ].filter(Boolean).join("");
  return `<div class="msg ${side}${entry.id === focus ? " focus" : ""}" id="${escapeHtml(entry.id)}">
    <div class="avatar ${side}" aria-hidden="true">${entry.author === "CLAUDE" ? "C" : "G"}</div>
    <div class="bubble">
      <header><b>${escapeHtml(entry.author || "?")}</b><span class="mid">${escapeHtml(entry.id)}</span>
        <span class="type type-${escapeHtml(f.type ?? "")}">${escapeHtml(TYPE_LABEL[f.type ?? ""] ?? f.type ?? "")}</span>
        <time>${escapeHtml(entry.date)}</time><span class="tid">${escapeHtml(entry.topic)}</span></header>
      ${f.re ? `<p class="re">↪ <a class="id-link" href="#/chat?focus=${escapeHtml(f.re)}">${escapeHtml(f.re)}</a>에 대한 ${escapeHtml(TYPE_LABEL[f.type ?? ""] ?? "")}${f.verdict ? ` · <span class="verdict v-${escapeHtml(f.verdict)}">${escapeHtml(VERDICT_LABEL[f.verdict] ?? f.verdict)}</span>` : ""}</p>` : ""}
      ${f.claim ? `<p class="claim">${inline(f.claim)}</p>` : ""}
      ${fold(`${f.detail ? `<p class="detail">${inline(f.detail)}</p>` : ""}${f.note ? `<p class="note">📝 ${inline(f.note)}</p>` : ""}`)}
      ${f.question ? `<p class="claim">${inline(f.question)}</p>` : ""}
      ${f.locator ? `<p class="small">위치: ${inline(f.locator)}</p>` : ""}
      ${f.checked ? `<p class="small">직접 확인한 출처: ${f.checked.trim().toLowerCase() === "none" ? "<em>없음 (판정 근거 아님)</em>" : ids(f.checked).map(escapeHtml).join(", ")}</p>` : ""}
      ${f.answers ? `<p class="small">답한 검토: ${ids(f.answers).map((id) => `<a class="id-link" href="#/chat?focus=${escapeHtml(id)}">${escapeHtml(id)}</a>`).join(", ")}</p>` : ""}
      ${sourceChips || meta ? `<footer>${sourceChips}${meta}</footer>` : ""}
    </div>
  </div>`;
}

/** With "긴 내용 접기" on, detail and note sit under a toggle. */
const fold = (html: string) => !html || !settings.collapse ? html : `<details class="fold"><summary>자세히</summary>${html}</details>`;

const AUTHOR_LABEL: Record<string, string> = { CLAUDE: "CLAUDE만", CODEX: "CODEX만" };
const STATUS_LABEL: Record<string, string> = { open: "미해결(proposed·disputed)", disputed: "쟁점(disputed)", verified: "확정(verified)" };

function chatPage(params: URLSearchParams): string {
  const all = threadEntries();
  const sources = registry();
  const focus = params.get("focus") ?? "";
  topicFilter = params.get("topic") ?? topicFilter;
  const topicIds = [...new Set(all.map((entry) => entry.topic).filter(Boolean))].sort();
  const inTopic = all.filter((entry) => topicFilter === "all" || entry.topic === topicFilter);
  const shown = [...new Set([...filterThread(inTopic, all, settings.author, settings.status), ...all.filter((entry) => entry.id === focus)])]
    .sort((a, b) => a.date.localeCompare(b.date) || a.order - b.order || a.prefix.localeCompare(b.prefix));
  if (settings.order === "desc") shown.reverse();
  const active = [AUTHOR_LABEL[settings.author], STATUS_LABEL[settings.status], settings.order === "desc" ? "최신 순" : ""].filter(Boolean);
  let lastDate = "";
  const messages = shown.map((entry) => {
    const divider = entry.date !== lastDate ? `<div class="day"><span>${escapeHtml(entry.date || "날짜 없음")}</span></div>` : "";
    lastDate = entry.date;
    return divider + bubble(entry, all, sources, focus);
  }).join("");
  return `<section class="chat">
    <header class="chat-head">
      <div><h1>대화 기록</h1><p class="muted">CThread(CLAUDE)와 GThread(CODEX)의 WLMD 기록을 날짜순으로 합쳐 보여 줍니다. 두 파일은 각 CLI만 덧붙여 쓰며, 이 화면은 읽기만 합니다.</p></div>
      <div class="chat-tools">
      <form id="jump" class="jump"><input name="id" placeholder="ID로 이동 (예: C-012)" aria-label="기록 ID로 이동" autocomplete="off"></form>
      <label>주제 <select id="topic-filter"><option value="all">전체</option>${topicIds.map((id) => `<option value="${escapeHtml(id)}"${id === topicFilter ? " selected" : ""}>${escapeHtml(id)}</option>`).join("")}</select></label>
      </div>
    </header>
    ${active.length ? `<p class="filters">필터: ${active.map((text) => `<span class="chip">${escapeHtml(text)}</span>`).join(" ")} · ${shown.length}/${inTopic.length}개 표시 <button type="button" class="link" data-open-settings>설정에서 바꾸기</button></p>` : ""}
    <div class="legend"><span class="dot left"></span>CLAUDE · CThread <span class="dot right"></span>CODEX · GThread
      <span class="chip status-verified">verified</span><span class="chip status-proposed">proposed</span><span class="chip status-disputed">disputed</span></div>
    <div class="messages">${messages || `<div class="empty"><b>${all.length ? "조건에 맞는 기록이 없습니다." : "아직 대화가 없습니다."}</b><p>두 스레드가 <code>threads/cthread-claude.md</code>와 <code>threads/gthread-codex.md</code>에 기록을 남기면 여기에 바로 나타납니다.</p></div>`}</div>
  </section>`;
}

function sourcesPage(): string {
  const rows = [...registry().values()].map((source) => {
    const url = source.fields.url ?? "";
    return `<tr id="${escapeHtml(source.id)}"><td class="mono">${escapeHtml(source.id)}</td><td>${inline(source.title)}</td><td><span class="chip tier-${escapeHtml(source.fields.tier ?? "")}">${escapeHtml(source.fields.tier ?? "")}</span></td>
      <td>${escapeHtml(source.fields.publisher ?? "")}</td><td>${escapeHtml(source.fields["added-by"] ?? "")}</td>
      <td>${/^https?:\/\//.test(url) ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">열기</a>` : ""}</td></tr>`;
  }).join("");
  return docPage("SOURCES.md", "") + `<article class="doc"><h2>등록된 출처</h2><p class="muted">registry/user.md · cthread-claude.md · gthread-codex.md 를 합친 목록입니다.</p>
    <div class="table-wrap"><table><thead><tr><th>ID</th><th>제목</th><th>등급</th><th>발행</th><th>등록</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></article>`;
}

function render() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const page = PAGES.some(([id]) => id === path) ? path : settings.home;
  const params = new URLSearchParams(query);
  renderNav(page);
  const file = PAGES.find(([id]) => id === page)![2];
  const keep = view.scrollTop;
  view.innerHTML = page === "status" ? statusPage() : page === "chat" ? chatPage(params) : page === "sources" ? sourcesPage() : docPage(file);
  const filter = document.getElementById("topic-filter") as HTMLSelectElement | null;
  filter?.addEventListener("change", () => { topicFilter = filter.value; location.hash = `#/chat?topic=${filter.value}`; });
  const jump = document.getElementById("jump") as HTMLFormElement | null;
  jump?.addEventListener("submit", (event) => {
    event.preventDefault();
    const id = String(new FormData(jump).get("id") ?? "").trim().toUpperCase();
    const input = jump.elements.namedItem("id") as HTMLInputElement;
    if (id && threadEntries().some((entry) => entry.id === id)) location.hash = `#/chat?focus=${encodeURIComponent(id)}`;
    else { input.setCustomValidity(id ? `${id} 기록이 없습니다` : "ID를 입력하세요"); input.reportValidity(); input.setCustomValidity(""); }
  });
  const focus = params.get("focus");
  const target = focus ? document.getElementById(focus) : null;
  if (target) { target.classList.add("focus"); target.scrollIntoView({ block: "center" }); }
  else if (page === "chat") view.scrollTop = settings.order === "desc" ? 0 : view.scrollHeight;
  else view.scrollTop = keep;
}

// ---------------------------------------------------------------- live data
let signature = "";
const TITLE = document.title;
let unseen = 0;
document.addEventListener("visibilitychange", () => { if (!document.hidden) { unseen = 0; document.title = TITLE; } });
let loading = false;
async function refresh(force = false) {
  if (loading) return;
  loading = true;
  const button = document.getElementById("reload") as HTMLButtonElement;
  button.disabled = true;
  try {
    const [data, checks] = await Promise.all([
      fetch("/wl-api/files", { cache: "no-store" }).then((response) => response.json() as Promise<{ files: Files }>),
      fetch("/wl-api/check", { cache: "no-store" }).then((response) => response.json() as Promise<{ claude: string; codex: string }>),
    ]);
    const next = JSON.stringify([data.files, checks]);
    const stamp = () => { document.getElementById("sync")!.textContent = `${threadEntries().length} records · 확인 ${new Date().toLocaleTimeString("ko-KR")}${settings.autoRefresh ? " · live" : ""}`; };
    if (next === signature && !force) { stamp(); return; }
    signature = next;
    const before = new Set(threadEntries().map((entry) => entry.id));
    const first = !Object.keys(files).length;
    files = data.files;
    if (!first && settings.notify && document.hidden) {
      unseen += threadEntries().filter((entry) => !before.has(entry.id)).length;
      document.title = unseen ? `(${unseen}) ${TITLE}` : TITLE;
    }
    check = checks;
    render();
    stamp();
  } catch {
    document.getElementById("sync")!.textContent = "✕ 파일을 읽지 못했습니다 — 개발 서버 확인";
  } finally {
    loading = false;
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- keys
// 1–9 switch tabs, r refreshes, "," opens settings (ignored while typing).
document.addEventListener("keydown", (event) => {
  const target = event.target as HTMLElement;
  if (event.metaKey || event.ctrlKey || event.altKey || target.closest("input, select, textarea, dialog")) return;
  const page = PAGES[Number(event.key) - 1];
  if (page) location.hash = `#/${page[0]}`;
  else if (event.key === "r") void refresh(true);
  else if (event.key === ",") openSettings();
});

// ---------------------------------------------------------------- settings
const dialog = document.getElementById("settings-dialog") as HTMLDialogElement;

function option<K extends keyof Settings>(key: K, label: string, choices: [Settings[K], string][]): string {
  return `<fieldset><legend>${escapeHtml(label)}</legend><div class="seg">${choices.map(([value, text]) =>
    `<label><input type="radio" name="${key}" value="${escapeHtml(String(value))}"${settings[key] === value ? " checked" : ""}><span>${escapeHtml(text)}</span></label>`).join("")}</div></fieldset>`;
}

function renderSettings() {
  const swatch = (id: string, name: string, colors: string[], bg: string, fg: string) =>
    `<button type="button" class="theme-card${settings.theme === id ? " on" : ""}" data-theme="${id}" style="background:${bg};color:${fg}" aria-pressed="${settings.theme === id}">
      <span class="theme-dots">${colors.map((color) => `<i style="background:${color}"></i>`).join("")}</span><b>${escapeHtml(name)}</b></button>`;
  document.getElementById("theme-list")!.innerHTML =
    swatch("system", "기본 (시스템)", ["#c8623b", "#2f5d8a", "#2e7d4f", "#9a6a00"], "linear-gradient(135deg,#f6f5f1 50%,#161714 50%)", "#77756e")
    + THEMES.map((t) => swatch(t.id, t.name, [1, 2, 3, 4, 5, 6].map((i) => t.ansi[i]), t.bg, t.fg)).join("");
  document.getElementById("setting-rows")!.innerHTML = [
    option("chat", "대화 보기", [["bubble", "말풍선"], ["log", "터미널 로그"]]),
    option("font", "본문 글꼴", [["sans", "Noto Sans KR"], ["mono", "JetBrains Mono"]]),
    option("zoom", "글자 크기", ZOOMS.map((zoom) => [zoom, `${zoom}%`])),
    option("autoRefresh", "자동 새로고침", [[true, "켜기"], [false, "끄기 (버튼만)"]]),
    option("home", "시작 페이지", PAGES.map(([id, label]) => [id, label])),
    `<h3 class="rows-head">대화 기록</h3>`,
    option("author", "작성자", [["all", "둘 다"], ["CLAUDE", "CLAUDE만"], ["CODEX", "CODEX만"]]),
    option("status", "상태", [["all", "전체"], ["open", "미해결"], ["disputed", "쟁점만"], ["verified", "확정만"]]),
    option("order", "날짜 순서", [["asc", "오래된 순"], ["desc", "최신 순"]]),
    option("collapse", "긴 내용 접기", [[false, "펼치기"], [true, "접기"]]),
    option("notify", "새 기록 알림 (탭 제목)", [[true, "켜기"], [false, "끄기"]]),
  ].join("");
}

function update(next: Partial<Settings>) {
  settings = { ...settings, ...next };
  save(settings);
  apply(settings);
  renderSettings();
  if (Object.keys(files).length) render();
}

dialog.addEventListener("click", (event) => {
  const card = (event.target as Element).closest<HTMLElement>(".theme-card");
  if (card) update({ theme: card.dataset.theme! });
  else if (event.target === dialog) dialog.close();
});
dialog.addEventListener("change", (event) => {
  const input = event.target as HTMLInputElement;
  const key = input.name as keyof Settings;
  const raw = input.value;
  update({ [key]: typeof DEFAULTS[key] === "boolean" ? raw === "true" : typeof DEFAULTS[key] === "number" ? Number(raw) : raw });
});
document.getElementById("settings-reset")!.addEventListener("click", () => update({ ...DEFAULTS }));
function openSettings() { renderSettings(); dialog.showModal(); }
document.getElementById("settings")!.addEventListener("click", openSettings);
view.addEventListener("click", (event) => { if ((event.target as Element).closest("[data-open-settings]")) openSettings(); });

window.addEventListener("hashchange", render);
document.getElementById("reload")!.addEventListener("click", () => void refresh(true));
// The dev server announces saved files at once; a slow poll is the fallback.
import.meta.hot?.on("wl:changed", () => { if (settings.autoRefresh) void refresh(); });
void refresh();
window.setInterval(() => { if (settings.autoRefresh) void refresh(); }, 30_000);
