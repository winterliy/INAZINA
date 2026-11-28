import { createServer } from "node:http";
import { authenticate } from "./auth.ts";
import { migrate, pool, one } from "./db.ts";
import { bad, HttpError, readBody, send } from "./http.ts";
import { routes1, type Route } from "./routes1.ts";
import { routes2 } from "./routes2.ts";

const port = Number(process.env.WORK_API_PORT ?? 4317);
const domain = process.env.INAZINA_DOMAIN || "inazina.localhost";
const origin = `https://work.${domain}`;
const routes: Route[] = [...routes1, ...routes2];

if (!process.env.WORK_DB_PASSWORD) { console.error("WORK_DB_PASSWORD is not set (see .env.example)"); process.exit(1); }
await migrate();
if (!(await one("SELECT 1 FROM boards LIMIT 1"))) {
  await pool.query("INSERT INTO boards (name,notice_only) VALUES ('공지사항',1),('자유게시판',0)");
  await pool.query("INSERT INTO approval_forms (name,template) VALUES ('일반 기안','목적:\n내용:\n'),('지출 결의','항목:\n금액:\n사유:\n'),('휴가 신청','기간:\n사유:\n')");
  await pool.query("INSERT INTO calendars (owner_id,name,kind) VALUES (NULL,'전체 일정','shared'),(NULL,'회의실 A','resource')");
}

createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", "http://work");
    if (url.pathname === "/api/work/health") { await one("SELECT 1"); return send(response, 200, { ok: true }); }
    if (!url.pathname.startsWith("/api/work/")) return send(response, 404, { error: "not_found" });
    const path = url.pathname.slice("/api/work".length);
    const method = request.method ?? "GET";
    if (method !== "GET") {
      // The browser app sends this header and its own origin; a form on another site can do neither.
      if (request.headers["x-work-request"] !== "1") throw new HttpError(403, "csrf");
      const from = request.headers.origin;
      if (from && from !== origin && !from.startsWith("http://127.0.0.1")) throw new HttpError(403, "origin");
    }
    const found = routes.find(([m, re]) => m === method && re.test(path));
    if (!found) throw new HttpError(404, "not_found");
    const member = await authenticate(request);
    const isUpload = path === "/drive/files";
    const body = method === "GET" || method === "DELETE" ? {} : await readBody(request, isUpload ? 24 * 1024 * 1024 : 1024 * 1024);
    if (body === null || typeof body !== "object") throw bad("invalid_body");
    const result: any = await found[2]({ member, params: path.match(found[1])!.slice(1), query: url.searchParams, body });
    if (result?.__file) {
      response.writeHead(200, { "content-type": "application/octet-stream", "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(result.name)}`, "x-content-type-options": "nosniff", "cache-control": "no-store" });
      return response.end(result.data);
    }
    send(response, 200, result ?? { ok: true });
  } catch (error) {
    if (error instanceof HttpError) return send(response, error.status, { error: error.message });
    console.error("work API error:", error);
    send(response, 500, { error: "server_error" });
  }
}).listen(port, "127.0.0.1", () => console.log(`INAZINA Work API listening at http://127.0.0.1:${port}`));

process.on("SIGTERM", () => { void pool.end().finally(() => process.exit(0)); });
