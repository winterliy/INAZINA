"""The single writer for ``admin_audit`` rows."""
import json
import time


def record_admin_audit(conn, actor, action, payload, recorded_epoch=None):
    """Append one audit row. ``payload`` is stored as given when it is already JSON text."""
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, default=str)
    conn.execute(
        "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
        (str(actor), action, payload, time.time() if recorded_epoch is None else recorded_epoch),
    )
