import type { IncomingMessage } from "node:http";
import { one } from "./db.ts";
import { HttpError, type Member } from "./http.ts";

const mainApi = process.env.INAZINA_API_URL ?? "http://127.0.0.1:4312";
const cache = new Map<string, { at: number; user: Identity }>();
export type Identity = { userId: string; nickname: string; role: string };

// Who is asking: the session cookie belongs to the main INAZINA login (SSO, client "work"); it is checked with the main API.
// Nothing about the person is trusted from the browser, and a result is reused for 20 seconds at most.
export async function identify(request: IncomingMessage): Promise<Identity> {
  const cookie = request.headers.cookie ?? "";
  if (!cookie) throw new HttpError(401, "login_required");
  const hit = cache.get(cookie);
  if (hit && Date.now() - hit.at < 20_000) return hit.user;
  const response = await fetch(`${mainApi}/api/me`, { headers: { cookie }, signal: AbortSignal.timeout(5000) }).catch(() => undefined);
  if (!response) throw new HttpError(503, "login_unavailable");
  if (response.status === 401) throw new HttpError(401, "login_required");
  if (!response.ok) throw new HttpError(503, "login_unavailable");
  const user = ((await response.json()) as any).user as Identity | undefined;
  if (!user?.userId) throw new HttpError(401, "login_required");
  if (cache.size > 500) cache.clear();
  cache.set(cookie, { at: Date.now(), user });
  return user;
}

// Entry is by allow-list only: the person must already be a work member (invited, approved, or a configured first administrator).
// Being an administrator of the game gives nothing here.
export async function authenticate(request: IncomingMessage): Promise<Member> {
  const user = await identify(request);
  const row = await one<{ id: number; status: string; role: Member["role"]; display_name: string }>("SELECT id,status,role,display_name FROM members WHERE user_ref=?", [user.userId]);
  if (!row) throw new HttpError(403, "not_member");
  if (row.status === "retired") throw new HttpError(403, "retired");
  return { id: row.id, userRef: user.userId, name: row.display_name, role: row.role };
}
