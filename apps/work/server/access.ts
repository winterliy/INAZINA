// Accounts: login, joining with an invite code, applying for approval, and the administrator's side (requests, invites, resets, withdrawals).
import { createHash, randomBytes } from "node:crypto";
import { unlinkSync } from "node:fs";
import { join } from "node:path";
import { checkLoginId, checkPassword, clientIp, createSession, currentTokenHash, dummyHash, endAllSessions, endSession, hashPassword, limit, loginCookie, clearCookie, verifyPassword } from "./auth.ts";
import { one, pool, rows, run } from "./db.ts";
import { bad, HttpError, int, str, type Handler, type Member } from "./http.ts";
import { audit, notify } from "./routes1.ts";
import type { IncomingMessage } from "node:http";

const hash = (code: string) => createHash("sha256").update(code).digest("hex");
const filesDir = process.env.WORK_FILES_DIR ?? join(import.meta.dirname, "../../../database/work-files");
const newRef = () => `m-${randomBytes(8).toString("hex")}`;
const MAX_FAILS = 5;

type OpenCtx = { request: IncomingMessage; ip: string; body: any };
type Cookied = { __cookie: string };
export const openRoutes: [string, RegExp, (ctx: OpenCtx) => Promise<unknown>][] = [
  ["POST", /^\/auth\/login$/, async ({ request, ip, body }) => {
    limit(`login:${ip}`, 20, 10 * 60_000);
    const id = typeof body.id === "string" ? body.id.trim().toLowerCase() : "", password = typeof body.password === "string" ? body.password.slice(0, 200) : "";
    const fail = () => new HttpError(401, "invalid_credentials");
    const row = id ? await one<{ id: number; password_hash: string; status: string; locked: number; failed_logins: number }>("SELECT id,password_hash,status,(locked_until>NOW()) locked,failed_logins FROM members WHERE login_id=?", [id]) : undefined;
    if (!row) {
      const ok = await verifyPassword(password, (await one<{ password_hash: string }>("SELECT password_hash FROM access_requests WHERE user_ref=? AND status='pending' ORDER BY id DESC LIMIT 1", [id]))?.password_hash ?? dummyHash);
      if (ok) throw new HttpError(403, "pending_approval"); // right password, but the application is still waiting
      throw fail();
    }
    if (row.locked) { await verifyPassword(password, dummyHash); throw new HttpError(429, "account_locked"); }
    if (!(await verifyPassword(password, row.password_hash)) || row.status === "retired") {
      await run("UPDATE members SET failed_logins=failed_logins+1, locked_until=IF(failed_logins+1>=?, DATE_ADD(NOW(), INTERVAL 15 MINUTE), locked_until) WHERE id=?", [MAX_FAILS, row.id]);
      throw fail();
    }
    await run("UPDATE members SET failed_logins=0, locked_until=NULL WHERE id=?", [row.id]);
    return { ok: true, __cookie: loginCookie(request, await createSession(row.id)) };
  }],
  // join with an invite code: the account is created at once
  ["POST", /^\/auth\/signup$/, async ({ request, ip, body }) => {
    limit(`signup:${ip}`, 10, 60 * 60_000);
    const id = checkLoginId(body.id), password = checkPassword(body.password, id), name = str(body.name, 80, "name")!.trim();
    const code = str(body.code, 100, "code")!.trim();
    const passwordHash = await hashPassword(password);
    const conn = await pool.getConnection();
    let memberId = 0;
    try {
      await conn.beginTransaction();
      const [found]: any = await conn.query("SELECT id FROM invites WHERE code_hash=? AND used_at IS NULL AND expires_at>NOW() FOR UPDATE", [hash(code)]);
      if (!found[0]) { await conn.rollback(); await new Promise((r) => setTimeout(r, 800)); throw new HttpError(400, "invite_invalid"); }
      const [taken]: any = await conn.query("SELECT 1 FROM members WHERE login_id=? UNION SELECT 1 FROM access_requests WHERE user_ref=? AND status='pending'", [id, id]);
      if (taken.length) { await conn.rollback(); throw new HttpError(409, "id_taken"); }
      await conn.query("UPDATE invites SET used_by_ref=?, used_at=NOW() WHERE id=?", [id, found[0].id]);
      const [created]: any = await conn.query("INSERT INTO members (user_ref,display_name,login_id,password_hash) VALUES (?,?,?,?)", [newRef(), name, id, passwordHash]);
      memberId = created.insertId;
      await conn.commit();
    } catch (error) { await conn.rollback().catch(() => {}); throw error; } finally { conn.release(); }
    return { ok: true, __cookie: loginCookie(request, await createSession(memberId)) };
  }],
  // apply without a code: an administrator decides; the password is kept (hashed) until then
  ["POST", /^\/auth\/apply$/, async ({ ip, body }) => {
    limit(`apply:${ip}`, 5, 60 * 60_000);
    const id = checkLoginId(body.id), password = checkPassword(body.password, id), name = str(body.name, 80, "name")!.trim();
    if (await one("SELECT 1 FROM members WHERE login_id=?", [id])) throw new HttpError(409, "id_taken");
    const last = await one<{ status: string; age: number }>("SELECT status, TIMESTAMPDIFF(HOUR,requested_at,NOW()) age FROM access_requests WHERE user_ref=? ORDER BY id DESC LIMIT 1", [id]);
    if (last?.status === "pending") throw new HttpError(409, "id_taken");
    if (last?.status === "rejected" && last.age < 24) throw new HttpError(429, "try_later");
    const r = await run("INSERT INTO access_requests (user_ref,display_name,password_hash,message) VALUES (?,?,?,?)", [id, name, await hashPassword(password), str(body.message, 300, "message", true)]);
    await notify((await rows<{ id: number }>("SELECT id FROM members WHERE role='admin' AND status='active'")).map((m) => m.id), "access", r.insertId, `가입 신청: ${name.slice(0, 60)}`);
    return { ok: true };
  }],
];

// for signed-in members; these may also be used while a new password is required
export const sessionRoutes: [string, RegExp, (ctx: { request: IncomingMessage; member: Member; body: any }) => Promise<unknown>][] = [
  ["POST", /^\/auth\/logout$/, async ({ request }) => { await endSession(request); return { ok: true, __cookie: clearCookie(request) }; }],
  ["POST", /^\/auth\/password$/, async ({ request, member, body }) => {
    const row = await one<{ login_id: string; password_hash: string }>("SELECT login_id,password_hash FROM members WHERE id=?", [member.id]);
    if (!row || !(await verifyPassword(String(body.current ?? ""), row.password_hash))) throw new HttpError(403, "wrong_password");
    const next = checkPassword(body.next, row.login_id);
    if (next === body.current) throw bad("password_same");
    await run("UPDATE members SET password_hash=?, must_change_password=0 WHERE id=?", [await hashPassword(next), member.id]);
    await endAllSessions(member.id, currentTokenHash(request)); // other devices are signed out
    await audit(member.id, "password.change");
    return { ok: true };
  }],
];

// The drive's file contents are plain files outside MySQL: delete them first, then let the procedure clear the rows and anonymise the member.
export async function withdrawMember(memberId: number): Promise<void> {
  const member = await one<{ user_ref: string }>("SELECT user_ref FROM members WHERE id=? AND status<>'retired'", [memberId]);
  if (!member) throw new HttpError(409, "not_active");
  for (const { storage_key } of await rows<{ storage_key: string }>("SELECT storage_key FROM drive_nodes WHERE owner_id=? AND storage_key IS NOT NULL", [memberId])) {
    if (/^[0-9a-f]{32}$/.test(storage_key)) { try { unlinkSync(join(filesDir, storage_key)); } catch { /* already gone */ } }
  }
  await run("CALL work_withdraw(?)", [member.user_ref]);
}
const lastAdmin = async (memberId: number) => !(await one("SELECT 1 FROM members WHERE role='admin' AND status='active' AND id<>?", [memberId]));

export const selfRoutes: [string, RegExp, Handler][] = [
  ["POST", /^\/me\/withdraw$/, async ({ member, body }) => {
    const row = await one<{ password_hash: string }>("SELECT password_hash FROM members WHERE id=?", [member.id]);
    if (!(await verifyPassword(String(body.password ?? ""), row?.password_hash))) throw new HttpError(403, "wrong_password");
    if (member.role === "admin" && (await lastAdmin(member.id))) throw new HttpError(409, "last_admin");
    await withdrawMember(member.id);
    return { ok: true };
  }],
];

export const adminAccess: [string, RegExp, Handler][] = [
  ["GET", /^\/access\/requests$/, async () => rows("SELECT id,user_ref login_id,display_name,message,status,requested_at FROM access_requests WHERE status='pending' ORDER BY id")],
  ["POST", /^\/access\/requests\/(\d+)\/decide$/, async ({ member, params, body }) => {
    const id = int(params[0], "id"), approve = body.decision === "approved";
    if (!approve && body.decision !== "rejected") throw bad("decision_invalid");
    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();
      const [found]: any = await conn.query("SELECT user_ref,display_name,password_hash FROM access_requests WHERE id=? AND status='pending' FOR UPDATE", [id]);
      const req = found[0];
      if (!req) { await conn.rollback(); throw new HttpError(409, "not_pending"); }
      if (approve) {
        const [taken]: any = await conn.query("SELECT 1 FROM members WHERE login_id=?", [req.user_ref]);
        if (taken.length) { await conn.rollback(); throw new HttpError(409, "id_taken"); }
        await conn.query("INSERT INTO members (user_ref,display_name,login_id,password_hash) VALUES (?,?,?,?)", [newRef(), req.display_name, req.user_ref, req.password_hash]);
      }
      await conn.query("UPDATE access_requests SET status=?, decided_by=?, decided_at=NOW(), password_hash=NULL WHERE id=?", [approve ? "approved" : "rejected", member.id, id]);
      await conn.commit();
      await audit(member.id, `access.${approve ? "approve" : "reject"}`, req.user_ref);
    } catch (error) { await conn.rollback().catch(() => {}); throw error; } finally { conn.release(); }
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
  // forgot password: a temporary one is shown once and must be changed at the next sign-in; all the person's sessions end
  ["POST", /^\/members\/(\d+)\/reset-password$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    if (id === member.id) throw new HttpError(403, "forbidden"); // change your own from the profile
    const target = await one("SELECT 1 FROM members WHERE id=? AND status<>'retired' AND login_id IS NOT NULL", [id]);
    if (!target) throw new HttpError(404, "not_found");
    const temporary = randomBytes(9).toString("base64url");
    await run("UPDATE members SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL WHERE id=?", [await hashPassword(temporary), id]);
    await endAllSessions(id);
    await audit(member.id, "password.reset", String(id));
    return { temporaryPassword: temporary };
  }],
  ["POST", /^\/members\/(\d+)\/withdraw$/, async ({ member, params }) => {
    const id = int(params[0], "id");
    if (id === member.id) throw new HttpError(403, "forbidden"); // use "탈퇴" in your own profile
    await withdrawMember(id);
    await audit(member.id, "member.withdraw", String(id));
    return { ok: true };
  }],
];
