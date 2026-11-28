// work's own login: id + password (scrypt) and database sessions. Nothing is shared with the game's login.
import { createHash, randomBytes, scrypt, timingSafeEqual } from "node:crypto";
import type { IncomingMessage } from "node:http";
import { one, run } from "./db.ts";
import { bad, HttpError, type Member } from "./http.ts";

const N = 16384, R = 8, P = 1, KEYLEN = 64;
const derive = (password: string, salt: Buffer, n = N, r = R, p = P) => new Promise<Buffer>((done, fail) =>
  scrypt(password, salt, KEYLEN, { N: n, r, p, maxmem: 64 * 1024 * 1024 }, (error, key) => (error ? fail(error) : done(key))));

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  return `scrypt$${N}$${R}$${P}$${salt.toString("base64")}$${(await derive(password, salt)).toString("base64")}`;
}
export async function verifyPassword(password: string, stored: string | null | undefined): Promise<boolean> {
  const parts = (stored ?? "").split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return false;
  const key = Buffer.from(parts[5], "base64");
  const actual = await derive(password, Buffer.from(parts[4], "base64"), Number(parts[1]), Number(parts[2]), Number(parts[3]));
  return actual.length === key.length && timingSafeEqual(actual, key);
}
// Verified against when the id does not exist, so a missing id costs the same time as a wrong password.
export const dummyHash = await hashPassword(randomBytes(12).toString("hex"));

export function checkLoginId(value: unknown): string {
  if (typeof value !== "string" || !/^[a-z0-9][a-z0-9_.-]{2,31}$/.test(value)) throw bad("id_invalid");
  return value;
}
export function checkPassword(value: unknown, loginId: string): string {
  if (typeof value !== "string" || value.length < 10 || value.length > 200) throw bad("password_weak");
  if (value.toLowerCase().includes(loginId.toLowerCase())) throw bad("password_weak");
  if (/^(.)\1+$/.test(value)) throw bad("password_weak");
  return value;
}

export const COOKIE = "work_session";
const tokenHash = (token: string) => createHash("sha256").update(token).digest("hex");
const IDLE_HOURS = 12, MAX_DAYS = 7;

export async function createSession(memberId: number): Promise<string> {
  const token = randomBytes(32).toString("base64url");
  await run("INSERT INTO sessions (token_hash,member_id,expires_at) VALUES (?,?,DATE_ADD(NOW(), INTERVAL ? HOUR))", [tokenHash(token), memberId, IDLE_HOURS]);
  await run("DELETE FROM sessions WHERE expires_at<NOW()");
  return token;
}
export async function endSession(request: IncomingMessage): Promise<void> {
  const token = cookieOf(request);
  if (token) await run("DELETE FROM sessions WHERE token_hash=?", [tokenHash(token)]);
}
export const endAllSessions = (memberId: number, except?: string) => run("DELETE FROM sessions WHERE member_id=? AND token_hash<>?", [memberId, except ?? ""]);
export const currentTokenHash = (request: IncomingMessage) => { const t = cookieOf(request); return t ? tokenHash(t) : ""; };

export function sessionCookie(request: IncomingMessage, token: string, maxAgeSeconds: number): string {
  const secure = request.headers["x-forwarded-proto"] === "https" ? "; Secure" : ""; // Caddy terminates TLS and says so
  return `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAgeSeconds}${secure}`;
}
export const loginCookie = (request: IncomingMessage, token: string) => sessionCookie(request, token, IDLE_HOURS * 3600);
export const clearCookie = (request: IncomingMessage) => sessionCookie(request, "", 0);

function cookieOf(request: IncomingMessage): string | undefined {
  const entry = (request.headers.cookie ?? "").split(";").map((part) => part.trim()).find((part) => part.startsWith(`${COOKIE}=`));
  const value = entry?.slice(COOKIE.length + 1);
  return value && /^[A-Za-z0-9_-]{20,100}$/.test(value) ? value : undefined;
}

// Who is asking, from the session cookie alone. Idle sessions end after 12 hours, any session after 7 days.
export async function authenticate(request: IncomingMessage): Promise<Member> {
  const token = cookieOf(request);
  if (!token) throw new HttpError(401, "login_required");
  const row = await one<{ id: number; user_ref: string; display_name: string; role: Member["role"]; status: string; must: number; stale: number }>(
    `SELECT m.id,m.user_ref,m.display_name,m.role,m.status,m.must_change_password must,TIMESTAMPDIFF(MINUTE,s.last_seen,NOW()) stale
     FROM sessions s JOIN members m ON m.id=s.member_id WHERE s.token_hash=? AND s.expires_at>NOW()`, [tokenHash(token)]);
  if (!row || row.status === "retired") throw new HttpError(401, "login_required");
  if (row.stale >= 5) await run("UPDATE sessions SET last_seen=NOW(), expires_at=LEAST(DATE_ADD(created_at, INTERVAL ? DAY), DATE_ADD(NOW(), INTERVAL ? HOUR)) WHERE token_hash=?", [MAX_DAYS, IDLE_HOURS, tokenHash(token)]);
  return { id: row.id, userRef: row.user_ref, name: row.display_name, role: row.role, mustChange: !!row.must };
}

// Attempts per address, in memory (the API listens on loopback only; the address comes from the header Caddy sets).
const attempts = new Map<string, number[]>();
export function clientIp(request: IncomingMessage): string {
  const forwarded = String(request.headers["x-forwarded-for"] ?? "").split(",").map((x) => x.trim()).filter(Boolean);
  return forwarded.at(-1) ?? request.socket.remoteAddress ?? "?";
}
export function limit(key: string, max: number, windowMs: number): void {
  const now = Date.now();
  const recent = (attempts.get(key) ?? []).filter((t) => now - t < windowMs);
  if (recent.length >= max) throw new HttpError(429, "too_many_attempts");
  recent.push(now);
  attempts.set(key, recent);
  if (attempts.size > 5000) attempts.clear();
}
