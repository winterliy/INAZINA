/** The documents' simple text format (## headings, - lists, "a | b" tables, paragraphs) as escaped HTML. A "# " line is the page title. */
export const escapeHtml = (text: string): string => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function documentTitle(source: string): string {
  const line = source.replace(/\r/g, "").split("\n").find((item) => item.startsWith("# "));
  return line ? line.slice(2).replace(/^[^\p{L}\p{N}]+/u, "").trim() : "";
}

export function renderDocumentHtml(source: string): string {
  const out: string[] = [];
  let list = false, table = false;
  const close = () => { if (list) out.push("</ul>"); if (table) out.push("</tbody></table>"); list = false; table = false; };
  for (const raw of source.replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    if (!line) { close(); continue; }
    if (line.startsWith("# ")) { close(); continue; }
    if (line.startsWith("## ")) { close(); out.push(`<h2>${escapeHtml(line.slice(3))}</h2>`); continue; }
    if (line.startsWith("### ")) { close(); out.push(`<h3>${escapeHtml(line.slice(4))}</h3>`); continue; }
    if (line.startsWith("- ")) {
      if (table) close();
      if (!list) { out.push("<ul>"); list = true; }
      out.push(`<li>${escapeHtml(line.slice(2))}</li>`);
      continue;
    }
    if (line.includes(" | ")) {
      if (list) close();
      const cells = line.split(" | ");
      if (!table) { out.push(`<table><thead><tr>${cells.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("")}</tr></thead><tbody>`); table = true; }
      else out.push(`<tr>${cells.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`);
      continue;
    }
    close();
    out.push(`<p>${escapeHtml(line)}</p>`);
  }
  close();
  return out.join("\n");
}
