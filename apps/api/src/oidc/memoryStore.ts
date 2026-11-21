// An in-memory StoreCall with the same behavior as web_api/oidc_store.py, for tests of the provider and the relying party.
import type { StoreCall } from "./adapter.js";

type Row = { payload: Record<string, unknown>; expiresAt: number; grantId?: string; uid?: string; userCode?: string };

export function memoryStore(): StoreCall {
  const rows = new Map<string, Row>();
  const key = (model: unknown, id: unknown) => `${model}\u0000${id}`;
  const live = (row?: Row) => (row && row.expiresAt > Math.floor(Date.now() / 1000) ? row : undefined);
  const match = (model: unknown, field: "uid" | "userCode", value: unknown) =>
    [...rows.entries()].find(([name, row]) => name.startsWith(`${model}\u0000`) && row[field] === value && live(row))?.[1];
  return async (operation, body) => {
    switch (operation) {
      case "upsert":
        rows.set(key(body.model, body.id), { payload: body.payload as Record<string, unknown>, expiresAt: body.expires_at as number,
          grantId: body.grant_id as string | undefined, uid: body.uid as string | undefined, userCode: body.user_code as string | undefined });
        return { result: undefined };
      case "find": return { result: live(rows.get(key(body.model, body.id)))?.payload ?? null };
      case "find_by_uid": return { result: match(body.model, "uid", body.uid)?.payload ?? null };
      case "find_by_user_code": return { result: match(body.model, "userCode", body.user_code)?.payload ?? null };
      case "consume": {
        const row = live(rows.get(key(body.model, body.id)));
        if (row && row.payload.consumed === undefined) row.payload.consumed = Math.floor(Date.now() / 1000);
        return { result: undefined };
      }
      case "destroy": rows.delete(key(body.model, body.id)); return { result: undefined };
      case "revoke_by_grant_id":
        for (const [name, row] of rows) if (row.grantId === body.grant_id) rows.delete(name);
        return { result: undefined };
      default: throw new Error(`unknown store operation ${operation}`);
    }
  };
}
