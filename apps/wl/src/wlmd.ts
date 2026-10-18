// Pure WLMD helpers shared by the viewer and its tests (no DOM access).

export type Files = Record<string, string>;
export type Entry = { id: string; prefix: string; title: string; author: string; date: string; topic: string;
  fields: Record<string, string>; file: string; order: number };

export const PAGES: [string, string, string][] = [
  ["overview", "소개", "README.md"],
  ["status", "진행 현황", ""],
  ["chat", "대화 기록", ""],
  ["rules", "운영 규칙", "RULES.md"],
  ["wlmd", "기록 형식 (WLMD)", "WLMD.md"],
  ["sources", "자료 등급 · 출처", "SOURCES.md"],
  ["review", "검토 기준", "REVIEW.md"],
  ["summary", "통합본", "wide-learning.md"],
  ["apply", "적용 대기열", "apply-queue.md"],
];
export const DOC_ROUTES: Record<string, string> = Object.fromEntries(PAGES.filter(([, , file]) => file).map(([id, , file]) => [file, id]));
export const THREADS = { "threads/cthread-claude.md": "CLAUDE", "threads/gthread-codex.md": "CODEX" } as const;
export const REGISTRIES = ["registry/user.md", "registry/cthread-claude.md", "registry/gthread-codex.md"];

// ---------------------------------------------------------------- utilities
export const escapeHtml = (value: string) => value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

export function inline(text: string): string {
  // Finished fragments (code, links) are parked as placeholders so later passes never touch them.
  const parked: string[] = [];
  const park = (html: string) => `\u0000${parked.push(html) - 1}\u0000`;
  let html = escapeHtml(text).replace(/`([^`]+)`/g, (_match, code: string) => park(`<code>${code}</code>`));
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label: string, href: string) => {
    const raw = href.replace(/&#38;/g, "&");
    const doc = raw.replace(/^(\.\.\/|\.\/)*/, "").replace(/#.*$/, "");
    if (DOC_ROUTES[doc]) return park(`<a href="#/${DOC_ROUTES[doc]}">${label}</a>`);
    if (/^https?:\/\//.test(raw)) return park(`<a href="${escapeHtml(raw)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    return park(`<span class="ref" title="${escapeHtml(raw)}">${label}</span>`);
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  // Record IDs become links: thread entries open the chat, sources open the source list.
  html = html.replace(/\b([CG]-\d{3})\b/g, (id) => park(`<a class="id-link" href="#/chat?focus=${id}">${id}</a>`));
  html = html.replace(/\b(S-\d{3})\b/g, (id) => park(`<a class="id-link" href="#/sources?focus=${id}">${id}</a>`));
  return html.replace(/\u0000(\d+)\u0000/g, (_match, index: string) => parked[Number(index)]);
}

/** A small Markdown subset: headings, paragraphs, lists, tables, quotes, code fences. */
export function markdown(source: string): string {
  const lines = source.replace(/\r/g, "").split("\n");
  const out: string[] = [];
  let index = 0;
  const isTable = (line: string) => /^\s*\|.*\|\s*$/.test(line);
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      const body: string[] = [];
      index++;
      while (index < lines.length && !lines[index].trim().startsWith("```")) body.push(lines[index++]);
      index++;
      out.push(`<pre><code>${escapeHtml(body.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(4, heading[1].length + 1);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index++;
      continue;
    }
    if (isTable(line) && index + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[index + 1])) {
      const cells = (row: string) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      const head = cells(line);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && isTable(lines[index])) rows.push(cells(lines[index++]));
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (index < lines.length && (/^\s*([-*]|\d+\.)\s+/.test(lines[index]) || (/^\s{2,}\S/.test(lines[index]) && items.length))) {
        const current = lines[index++];
        if (/^\s*([-*]|\d+\.)\s+/.test(current) && !/^\s{4,}/.test(current)) items.push(current.replace(/^\s*([-*]|\d+\.)\s+/, ""));
        else items[items.length - 1] += " " + current.trim().replace(/^([-*]|\d+\.)\s+/, "· ");
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join("")}</${tag}>`);
      continue;
    }
    if (line.startsWith(">")) {
      const quote: string[] = [];
      while (index < lines.length && lines[index].startsWith(">")) quote.push(lines[index++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      continue;
    }
    if (/^---+\s*$/.test(line)) { out.push("<hr>"); index++; continue; }
    if (!line.trim()) { index++; continue; }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim() && !/^(#{1,4}\s|```|>|\s*([-*]|\d+\.)\s|---+\s*$)/.test(lines[index]) && !isTable(lines[index])) {
      paragraph.push(lines[index++].trim());
    }
    if (paragraph.length) out.push(`<p>${inline(paragraph.join(" "))}</p>`);
    else index++;
  }
  return out.join("\n");
}

// ---------------------------------------------------------------- WLMD parsing (same rules as tools/wlmd_check.py)
const HEADER = /^### \[([A-Z])-(\d{2,3})\] (.+?)\s*$/;
const FIELD = /^- ([a-z-]+):[ \t]?(.*)$/;
const THREAD_TITLE = /^(CLAUDE|CODEX) · (\d{4}-\d{2}-\d{2}) · (T-\d{2})$/;

export function parse(text: string, file: string): Entry[] {
  const entries: Entry[] = [];
  let current: Entry | null = null;
  let lastKey: string | null = null;
  let fenced = false;
  for (const line of text.replace(/\r/g, "").split("\n")) {
    if (line.trimStart().startsWith("```")) { fenced = !fenced; continue; }
    if (fenced) continue;
    const header = HEADER.exec(line);
    if (header) {
      const title = header[3];
      const thread = THREAD_TITLE.exec(title);
      current = { id: `${header[1]}-${header[2]}`, prefix: header[1], title, author: thread?.[1] ?? "", date: thread?.[2] ?? "",
        topic: thread?.[3] ?? "", fields: {}, file, order: entries.length };
      entries.push(current);
      lastKey = null;
      continue;
    }
    const field = current ? FIELD.exec(line) : null;
    if (current && field) { lastKey = field[1]; current.fields[lastKey] = field[2].trim(); }
    else if (current && lastKey && line.startsWith("  ") && line.trim()) current.fields[lastKey] += " " + line.trim();
    else if (line.startsWith("#")) { current = null; lastKey = null; }
  }
  return entries;
}

export const ids = (value = "") => value.split(";").map((item) => item.trim().split(/\s+/)[0]).filter((item) => item && item.toLowerCase() !== "none");

export function threadEntries(files: Files): Entry[] {
  return Object.keys(THREADS).flatMap((file) => parse(files[file] ?? "", file));
}

export function registry(files: Files): Map<string, Entry> {
  return new Map(REGISTRIES.flatMap((file) => parse(files[file] ?? "", file)).map((entry) => [entry.id, entry]));
}

export function topics(files: Files): Entry[] {
  return parse(files["topics.md"] ?? "", "topics.md");
}

/** Final status of a claim: its author's latest correction in the chain decides. */
export function finalStatus(root: Entry, all: Entry[]): string {
  const byId = new Map(all.map((entry) => [entry.id, entry]));
  const rootOf = (entry: Entry): Entry => {
    let current = entry;
    const seen = new Set<string>();
    while (current.fields.re && byId.has(current.fields.re) && !seen.has(current.id)) {
      seen.add(current.id);
      const next = byId.get(current.fields.re)!;
      if (current.fields.type !== "correction") break;
      current = next;
    }
    return current;
  };
  const corrections = all.filter((entry) => entry.fields.type === "correction" && entry.author === root.author && rootOf(entry).id === root.id);
  const latest = corrections.sort((a, b) => Number(a.id.slice(2)) - Number(b.id.slice(2))).at(-1);
  return (latest ?? root).fields.status ?? "proposed";
}


const STATUS_GROUPS: Record<string, string[]> = { open: ["proposed", "disputed"], disputed: ["disputed"], verified: ["verified"] };

/** Chat filters: by author, and by a claim's current status — a matching claim
 *  brings its whole reply chain (reviews, corrections) with it. */
export function filterThread(entries: Entry[], all: Entry[], author: string, status: string): Entry[] {
  let kept = entries;
  if (STATUS_GROUPS[status]) {
    const byId = new Map(all.map((entry) => [entry.id, entry]));
    const claimOf = (entry: Entry): Entry | undefined => {
      const seen = new Set<string>();
      let current: Entry | undefined = entry;
      while (current && current.fields.type !== "claim" && current.fields.re && !seen.has(current.id)) {
        seen.add(current.id);
        current = byId.get(current.fields.re);
      }
      return current?.fields.type === "claim" ? current : undefined;
    };
    kept = kept.filter((entry) => {
      const claim = claimOf(entry);
      return claim !== undefined && STATUS_GROUPS[status].includes(finalStatus(claim, all));
    });
  }
  return author === "all" ? kept : kept.filter((entry) => entry.author === author);
}
