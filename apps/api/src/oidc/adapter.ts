// node-oidc-provider storage adapter: every operation is one call to the data service's /internal/oidc/store/<op>
// (web_api/oidc_store.py). Argument names follow that module.
import type { Adapter, AdapterPayload } from "oidc-provider";

export type StoreCall = (operation: string, body: Record<string, unknown>) => Promise<{ result?: unknown } | undefined>;

export function createAdapterClass(call: StoreCall): new (model: string) => Adapter {
  return class StoreAdapter implements Adapter {
    constructor(private readonly model: string) {}

    async upsert(id: string, payload: AdapterPayload, expiresIn: number): Promise<void> {
      await call("upsert", {
        model: this.model, id, payload, expires_at: Math.floor(Date.now() / 1000) + expiresIn,
        ...(payload.grantId ? { grant_id: payload.grantId } : {}),
        ...(payload.uid ? { uid: payload.uid } : {}),
        ...(payload.userCode ? { user_code: payload.userCode } : {}),
      });
    }
    private async read(operation: string, key: Record<string, string>): Promise<AdapterPayload | undefined> {
      const response = await call(operation, { model: this.model, ...key });
      return (response?.result as AdapterPayload | null | undefined) ?? undefined;
    }
    find(id: string) { return this.read("find", { id }); }
    findByUid(uid: string) { return this.read("find_by_uid", { uid }); }
    findByUserCode(userCode: string) { return this.read("find_by_user_code", { user_code: userCode }); }
    async consume(id: string): Promise<void> { await call("consume", { model: this.model, id }); }
    async destroy(id: string): Promise<void> { await call("destroy", { model: this.model, id }); }
    async revokeByGrantId(grantId: string): Promise<void> { await call("revoke_by_grant_id", { grant_id: grantId }); }
  };
}
