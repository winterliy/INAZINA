"""Storage behind the OIDC identity provider (the Node API's node-oidc-provider adapter).

One row per provider model object (Session, Grant, AuthorizationCode, ...), keyed by (model, id). Rows expire by
`expires_at` (epoch seconds); expired rows are never returned. Payloads are the provider's own JSON; this module
never interprets them, except that `consume` adds a `consumed` epoch to the payload as the provider expects.
No passwords or credentials are kept here.
"""
import json
import time


def ensure_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS oidc_store (
        model TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, expires_at INTEGER NOT NULL,
        grant_id TEXT, uid TEXT, user_code TEXT, PRIMARY KEY (model, id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_oidc_store_grant ON oidc_store(grant_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_oidc_store_uid ON oidc_store(uid)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_oidc_store_user_code ON oidc_store(user_code)")
    connection.execute("DELETE FROM oidc_store WHERE expires_at<=?", (int(time.time()),))


def upsert(connection, model, id, payload, expires_at, grant_id=None, uid=None, user_code=None):
    connection.execute(
        "INSERT INTO oidc_store(model,id,payload,expires_at,grant_id,uid,user_code) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(model,id) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at, "
        "grant_id=excluded.grant_id, uid=excluded.uid, user_code=excluded.user_code",
        (model, id, json.dumps(payload), int(expires_at), grant_id, uid, user_code))


def _one(connection, where, params):
    row = connection.execute(f"SELECT payload FROM oidc_store WHERE {where} AND expires_at>?",
                             (*params, int(time.time()))).fetchone()
    return json.loads(row[0]) if row else None


def find(connection, model, id):
    return _one(connection, "model=? AND id=?", (model, id))


def find_by_uid(connection, model, uid):
    return _one(connection, "model=? AND uid=?", (model, uid))


def find_by_user_code(connection, model, user_code):
    return _one(connection, "model=? AND user_code=?", (model, user_code))


def consume(connection, model, id):
    """Mark consumed once; a second call keeps the first timestamp."""
    payload = find(connection, model, id)
    if payload is None or "consumed" in payload:
        return
    payload["consumed"] = int(time.time())
    connection.execute("UPDATE oidc_store SET payload=? WHERE model=? AND id=?", (json.dumps(payload), model, id))


def destroy(connection, model, id):
    connection.execute("DELETE FROM oidc_store WHERE model=? AND id=?", (model, id))


def revoke_by_grant_id(connection, grant_id):
    connection.execute("DELETE FROM oidc_store WHERE grant_id=?", (grant_id,))
