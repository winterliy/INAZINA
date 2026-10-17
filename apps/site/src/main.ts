// NACURI official site: "LINK to" documents load from /links/*.txt, and the
// Contact/Report forms post to the API, which shows them in the admin dashboard.

const DOCS: Record<string, string> = {
  help: "📢 HELP - 명령어 도움말",
  privacy: "📢 개인정보 처리방침",
  terms: "📢 서비스 이용약관",
  operations: "📢 서비스 운영규칙",
  internal: "📢 서비스 내부규칙",
};

const FORMS: Record<string, { title: string; who: string }> = {
  contact: { title: "📞 Contact", who: "문의" },
  report: { title: "🚨 Report", who: "신고" },
};

const $ = <T extends Element>(selector: string) => document.querySelector(selector) as T;

function element(tag: string, text?: string, className?: string): HTMLElement {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

/** Turn the simple text format (#, ##, -, "a | b" tables) into safe DOM nodes. */
function renderDocument(source: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  let list: HTMLElement | null = null;
  let table: HTMLTableElement | null = null;
  const close = () => { list = null; table = null; };
  for (const raw of source.replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    if (!line) { close(); continue; }
    if (line.startsWith("# ")) { close(); continue; } // the dialog shows the title
    if (line.startsWith("## ")) { close(); fragment.append(element("h3", line.slice(3))); continue; }
    if (line.startsWith("- ")) {
      table = null;
      if (!list) { list = element("ul"); fragment.append(list); }
      list.append(element("li", line.slice(2)));
      continue;
    }
    if (line.includes(" | ")) {
      list = null;
      const cells = line.split(" | ");
      if (!table) {
        table = document.createElement("table");
        const head = table.createTHead().insertRow();
        for (const cell of cells) head.append(element("th", cell));
        table.createTBody();
        fragment.append(table);
      } else {
        const row = table.tBodies[0].insertRow();
        for (const cell of cells) row.append(element("td", cell));
      }
      continue;
    }
    close();
    fragment.append(element("p", line));
  }
  return fragment;
}

// Announced revisions (written by the dashboard): pending.json says when each
// takes effect, and <name>.next.txt holds its text. The current file stays in
// force until then.
type Pending = Record<string, { effectiveEpoch: number; announcedEpoch: number }>;
const day = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "long", timeZone: "Asia/Seoul" }).format(epoch * 1000);

async function fetchText(path: string): Promise<string> {
  const response = await fetch(path, { cache: "no-cache" });
  if (!response.ok) throw new Error(String(response.status));
  return response.text();
}

async function openDocument(name: string) {
  const dialog = $<HTMLDialogElement>("#doc-dialog");
  const body = $<HTMLDivElement>("#doc-body");
  $("#doc-title").textContent = DOCS[name] ?? "문서";
  body.replaceChildren(element("p", "불러오는 중…"));
  dialog.showModal();
  try {
    const [current, pending] = await Promise.all([
      fetchText(`/links/${name}.txt`),
      fetch("/links/pending.json", { cache: "no-cache" })
        .then((response): Promise<Pending> | Pending => response.ok ? response.json() as Promise<Pending> : {})
        .catch((): Pending => ({})),
    ]);
    const announced = pending[name] && pending[name].effectiveEpoch * 1000 > Date.now() ? pending[name] : null;
    const show = (text: string, revision: boolean) => {
      const parts: Node[] = [];
      if (announced) {
        const notice = element("div", "");
        notice.className = "doc-revision";
        notice.append(element("p", revision
          ? `개정안입니다. ${day(announced.effectiveEpoch)}부터 적용되며, 그 전까지는 현재 문서가 적용됩니다. (공지 ${day(announced.announcedEpoch)})`
          : `📢 개정 예정: ${day(announced.effectiveEpoch)}부터 새 내용이 적용됩니다. (공지 ${day(announced.announcedEpoch)})`));
        const toggle = element("button", revision ? "현재 문서 보기" : "개정안 보기");
        toggle.setAttribute("type", "button");
        toggle.className = "btn linkout";
        toggle.addEventListener("click", () => {
          if (revision) { show(current, false); return; }
          fetchText(`/links/${name}.next.txt`).then((next) => show(next, true))
            .catch(() => body.replaceChildren(element("p", "개정안을 불러오지 못했습니다.")));
        });
        notice.append(toggle);
        parts.push(notice);
      }
      parts.push(renderDocument(text));
      body.replaceChildren(...parts);
    };
    show(current, false);
  } catch {
    body.replaceChildren(element("p", "문서를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."));
  }
}

const ERRORS: Record<string, string> = {
  invalid_title: "제목을 입력해 주세요 (120자 이하).",
  invalid_body: "내용을 입력해 주세요 (4,000자 이하).",
  invalid_discordId: "디스코드 아이디를 입력해 주세요.",
  invalid_email: "올바른 이메일을 입력해 주세요.",
  consent_required: "개인정보 수집·이용에 동의해 주세요.",
  feedback_rate_limited: "잠시 후 다시 보내 주세요. 10분에 5건까지 보낼 수 있어요.",
};

function openForm(kind: string) {
  const dialog = $<HTMLDialogElement>("#form-dialog");
  const form = $<HTMLFormElement>("#feedback-form");
  const spec = FORMS[kind];
  form.reset();
  (form.elements.namedItem("kind") as HTMLInputElement).value = kind;
  $("#form-title").textContent = spec.title;
  $("[data-hint=discord]").textContent = `* ${spec.who} 작성자 본인의 디스코드 아이디를 요구합니다. 미입력 시 처리가 어렵습니다.`;
  setStatus("");
  dialog.showModal();
}

function setStatus(text: string, kind: "" | "ok" | "error" = "") {
  const status = $<HTMLParagraphElement>(".form-status");
  status.textContent = text;
  status.className = `form-status ${kind}`;
}

async function submitForm(event: SubmitEvent) {
  event.preventDefault();
  const form = event.currentTarget as HTMLFormElement;
  if (!form.reportValidity()) return;
  const data = Object.fromEntries(new FormData(form)) as Record<string, string>;
  const button = form.querySelector("button[type=submit]") as HTMLButtonElement;
  button.disabled = true;
  setStatus("보내는 중…");
  try {
    const response = await fetch("/api/site/feedback", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error ?? "send_failed");
    setStatus(`✅ ${FORMS[data.kind].who}이 운영진에게 전달되었습니다. 접수 번호 #${result.feedback.id}`, "ok");
    form.reset();
    (form.elements.namedItem("kind") as HTMLInputElement).value = data.kind;
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    setStatus(ERRORS[code] ?? "보내지 못했습니다. 잠시 후 다시 시도해 주세요.", "error");
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const target = (event.target as Element).closest("[data-doc],[data-form],[data-close]");
  if (!target) return;
  if (target.hasAttribute("data-close")) (target.closest("dialog") as HTMLDialogElement).close();
  else if (target.hasAttribute("data-doc")) void openDocument(target.getAttribute("data-doc")!);
  else void openForm(target.getAttribute("data-form")!);
});

// Clicking the backdrop closes a dialog.
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
}
$<HTMLFormElement>("#feedback-form").addEventListener("submit", (event) => void submitForm(event));
