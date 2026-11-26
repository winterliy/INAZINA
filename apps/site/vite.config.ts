import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import { documentTitle, escapeHtml, renderDocumentHtml } from "./src/docHtml";
import { renderFeed, renderNoticeBar, renderNotices, type Notice } from "./src/noticesHtml";

// INAZINA official site: a static, display-only site (published on GitHub Pages, see .github/workflows/pages.yml).
//  - The home page and one page per document (help, privacy, terms, operations, internal). The documents' text is written into
//    their pages at build time from public/links/*.txt, so crawlers and link checkers read it without running scripts.
//  - VITE_BASE is the sub-path the site is served from (e.g. /INAZINA/); VITE_SITE_URL is the public address (used for canonical
//    links, the link-preview image, robots.txt and sitemap.xml; without it those are left out).
const root = fileURLToPath(new URL(".", import.meta.url));
const DOCS = ["help", "privacy", "terms", "operations", "internal"];
// Site-ownership proofs for search engines (site-verification.json): meta tags added to the home page.
const proofs: Record<string, string> = (JSON.parse(readFileSync(`${root}site-verification.json`, "utf8")).tags ?? {}) as Record<string, string>;
const proofTags = Object.entries(proofs).map(([name, content]) => `<meta name="${escapeHtml(name)}" content="${escapeHtml(content)}">`).join("\n  ");
// The announced changes (public/links/notices.json, written by the console): the notice page, the home page's announcement bar and the feed.
function readNotices(): { updated: number; notices: Notice[] } {
  try {
    const data = JSON.parse(readFileSync(`${root}public/links/notices.json`, "utf8"));
    return { updated: Number(data.updated) || 0, notices: Array.isArray(data.notices) ? data.notices : [] };
  } catch { return { updated: 0, notices: [] }; }
}
const SITE = process.env.VITE_SITE_URL ? process.env.VITE_SITE_URL.replace(/\/?$/, "/") : "";

function site(): Plugin {
  return {
    name: "inazina-site",
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        let out = html;
        const feedData = readNotices();
        out = out.replace("<!--NOTICES-->", () => renderNotices(feedData.notices)).replace("<!--NOTICE_BAR-->", () => renderNoticeBar(feedData.notices, "notices/"));
        const name = /data-doc="([a-z]+)"/.exec(html)?.[1];
        if (name && DOCS.includes(name)) {
          const text = readFileSync(`${root}public/links/${name}.txt`, "utf8");
          const lead = text.split("\n").map((line) => line.trim()).find((line) => line && !/^(#|시행일|개정|이전 개정)/.test(line)) ?? "";
          // Function replacers: the documents contain "$" (e.g. `$` commands), which String.replace would read as a pattern.
          const title = escapeHtml(documentTitle(text)), description = escapeHtml(lead.slice(0, 150)), body = renderDocumentHtml(text);
          out = out.replaceAll("__TITLE__", () => title).replace("__DESCRIPTION__", () => description).replace("<!--DOC_BODY-->", () => body);
        }
        out = out.replace("<!--VERIFICATION-->", () => proofTags);
        if (SITE) return out.replaceAll("__SITE__", SITE).replaceAll("https://www.inazina.localhost/", SITE);
        return out.replace(/<link[^>]*data-site-only[^>]*>\s*/g, "").replace(/<script[^>]*data-site-only[^>]*>[\s\S]*?<\/script>\s*/g, "");
      },
    },
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "robots.txt", source: `User-agent: *\nAllow: /\n${SITE ? `\nSitemap: ${SITE}sitemap.xml\n` : ""}` });
      if (!SITE) return;
      const day = new Date().toISOString().slice(0, 10);
      const urls = [SITE, `${SITE}notices/`, ...DOCS.map((name) => `${SITE}${name}/`)];
      this.emitFile({ type: "asset", fileName: "feed.xml", source: renderFeed(readNotices().notices, SITE, readNotices().updated || Math.floor(Date.now() / 1000)) });
      this.emitFile({ type: "asset", fileName: "sitemap.xml", source: `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls.map((url) => `  <url><loc>${url}</loc><lastmod>${day}</lastmod></url>`).join("\n")}\n</urlset>\n` });
    },
  };
}

export default defineConfig({
  base: process.env.VITE_BASE || "/",
  plugins: [site()],
  build: { rollupOptions: { input: { main: `${root}index.html`, notices: `${root}notices/index.html`, display: `${root}display/index.html`, ...Object.fromEntries(DOCS.map((name) => [name, `${root}${name}/index.html`])) } } },
  // Only for looking at the site locally (npm run dev --workspace @inazina/site); start_all no longer runs it.
  server: { host: "127.0.0.1", port: 4314, strictPort: true },
  preview: { host: "127.0.0.1", port: 4314, strictPort: true },
});
