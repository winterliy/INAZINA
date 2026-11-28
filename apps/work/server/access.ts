// Who may enter: allow-list (members) + invite codes + applications an administrator approves. Open to any signed-in INAZINA
// account, because these are how a stranger asks to come in; everything else needs a member.
import { createHash, randomBytes } from "node:crypto";
import { one, pool, rows, run } from "./db.ts";
import { bad, HttpError, int, str, type Handler, type Member } from "./http.ts";
import type { Identity } from "./auth.ts";
import { audit, notify } from "./routes1.ts";

const hash = (code: string) => createHash("sha256").update(code).digest("hex");
// WORK_BOOTSTRAP_ADMINS (comma separated account ids) are admitted as work administrators on their first visit; nobody else is.
const bootstrap = new Set((process.env.WORK_BOOTSTRAP_ADMINS ?? "").split(",").map((x) => x.trim()).filter(Boolean));
export const bootstrapCount = bootstrap.size;

async function admit(user: Identity, role: Member["role"]): Promise<void> {
  await run("INSERT INTO members (user_ref, display_name, role) VALUES (?,?,?) ON DUPLICATE KEY UPDATE user_ref=user_ref", [user.userId, user.nickname.slice(0, 80), role]);
}

type OpenHandler = (ctx: { user: Identity; query: URLSearchParams; body: any }) => Promise<unknown>;
export const openRoutes: [string, RegExp, OpenHandler][] = [
  // where am I: member / pending / rejected / none. A configured first administrator becomes a member right here.
  ["GET", /^\/access$/, async ({ user }) => {
    let member = await one<{ status: string }>("SELECT status FROM members WHERE user_ref=?", [user.userId]);
    if (!member && bootstrap.has(user.userId)) { await admit(user, "admin"); member = { status: "active" }; }
    if (member) return { state: member.status === "retired" ? "retired" : "member" };
    const req = await one<{ status: string; requested_at: string }>("SELECT status,requested_at FROM access_requests WHERE user_ref=? ORDER BY id DESC LIMIT 1", [user.userId]);
    return { state: req?.status ?? "none", requestedAt: req?.requested_at ?? null };
  }],
  // join with an invite code (admitted at once) or apply for approval (waits for an administrator)
  ["POST", /^\/access\/apply$/, async ({ user, body }) => {
    if (await one("SELECT 1 FROM members WHERE user_ref=?", [user.userId])) throw new HttpError(409, "already_member");
    const code = typeof body.code === "string" ? body.code.trim() : "";
    if (code) {
      const conn = await pool.getConnection();
      try {
        await conn.beginTransaction();
        const [found]: any = await conn.query("SELECT id FROM invites WHERE code_hash=? AND used_at IS NULL AND expires_at>NOW() FOR UPDATE", [hash(code)]);
        if (!found[0]) { await conn.rollback(); await new Promise((r) => setTimeout(r, 800)); throw new HttpError(400, "invite_invalid"); } // slow wrong guesses
        await conn.query("UPDATE invites SET used_by_ref=?, used_at=NOW() WHERE id=?", [user.userId, found[0].id]);
        await conn.query("INSERT INTO members (user_ref, display_name, role) VALUES (?,?, 'member') ON DUPLICATE KEY UPDATE user_ref=user_ref", [user.userId, user.nickname.slice(0, 80)]);
        await conn.commit();
      } catch (error) { await conn.rollback().catch(() => {}); throw error; } finally { conn.release(); }
      return { state: "member" };
    }
    const last = await one<{ status: string; age: number }>("SELECT status, TIMESTAMPDIFF(HOUR,requested_at,NOW()) age FROM access_requests WHERE user_ref=? ORDER BY id DESC LIMIT 1", [user.userId]);
    if (last?.status === "pending") throw new HttpError(409, "already_pending");
    if (last?.status === "rejected" && last.age < 24) throw new HttpError(429, "try_later"); // a rejected account may ask again after a day
    const r = await run("INSERT INTO access_requests (user_ref,display_name,message) VALUES (?,?,?)", [user.userId, user.nickname.slice(0, 80), str(body.message, 300, "message", true)]);
    await notify((await rows<{ id: number }>("SELECT id FROM members WHERE role='admin' AND status='active'")).map((m) => m.id), "access", r.insertId, `가입 신청: ${user.nickname.slice(0, 60)}`);
    return { state: "pending" };
  }],
];

// administrator-only (the caller is a member with role admin)
export const adminAccess: [string, RegExp, Handler][] = [
  ["GET", /^\/access\/requests$/, async () => rows("SELECT id,user_ref,display_name,message,status,requested_at FROM access_requests WHERE status='pending' ORDER BY id")],
  ["POST", /^\/access\/requests\/(\d+)\/decide$/, async ({ member, params, body }) => {
    const id = int(params[0], "id"), approve = body.decision === "approved";
    if (!approve && body.decision !== "rejected") throw bad("decision_invalid");
    const req = await one<{ user_ref: string; display_name: string }>("SELECT user_ref,display_name FROM access_requests WHERE id=? AND status='pending'", [id]);
    if (!req) throw new HttpError(409, "not_pending");
    await run("UPDATE access_requests SET status=?, decided_by=?, decided_at=NOW() WHERE id=?", [approve ? "approved" : "rejected", member.id, id]);
    if (approve) await admit({ userId: req.user_ref, nickname: req.display_name, role: "" }, "member");
    await audit(member.id, `access.${approve ? "approve" : "reject"}`, req.user_ref);
    return { ok: true };
  }],
  ["GET", /^\/invites$/, async () => rows("SELECT i.id,i.note,i.expires_at,i.used_at,i.used_by_ref,x.display_name created_by FROM invites i JOIN members x ON x.id=i.created_by WHERE i.created_at>DATE_SUB(NOW(), INTERVAL 30 DAY) ORDER BY i.id DESC LIMIT 50")],
  // the code is shown once and only its hash is stored; it works once and expires (1-14 days, default 7)
  ["POST", /^\/invites$/, async ({ member, body }) => {
    const days = Math.min(14, Math.max(1, Number(body.days ?? 7) | 0));
    const code = randomBytes(12).toString("base64url");
    const r = await run("INSERT INTO invites (code_hash,created_by,note,expires_at) VALUES (?,?,?,DATE_ADD(NOW(), INTERVAL ? DAY))", [hash(code), member.id, str(body.note, 120, "note", true), days]);
    await audit(member.id, "invite.create", String(r.insertId));
    return { id: r.insertId, code, days };
  }],
  ["DELETE", /^\/invites\/(\d+)$/, async ({ params }) => { await run("DELETE FROM invites WHERE id=? AND used_at IS NULL", [int(params[0], "id")]); return { ok: true }; }],
];
