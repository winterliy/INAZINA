"""Two-person approval for high-risk administrator writes (M8). OFF by default.

When the switch ``admin_dual_approval`` is on, a request that matches ``risk_action`` is not executed on first submit. The gate records a
request (409 ``approval_required`` with its id), a *different* administrator approves or rejects it, and the requester sends the same request
again with ``approvalId``. The gate then checks that the request is for exactly this path and payload (hash), was approved by someone other
than the requester who is still an administrator, has not expired and has not been used; the approval is consumed only when the write
succeeds. Requests, decisions, uses and role changes are append-only tables; every step also writes an ``admin_audit`` row (action key and
payload hash only, never the values).

Turning the switch on needs at least two active administrators; turning it off while it is on is itself a high-risk action.
"""
import hashlib
import json
import threading
import time

from functions.admin_access import BOOTSTRAP_ADMIN_IDS
from functions.admin_audit import record_admin_audit

SETTING = "admin_dual_approval"
TTL_SECONDS = 24 * 3600
# Prudential parameters whose change switches behaviour that moves money (managers/alm_manager.py).
RISK_PARAMETER_KEYS = ("funding_auto_maturity_flag", "ftp_apply_enabled")
FUNDING_ACTIONS = ("repay", "process_due", "draw", "set_limit")
# Every action key the gate can return (the static test checks that server.py routes them through the gate).
HIGH_RISK_ACTIONS = (
    "db.update", "db.delete", "accounting_close.write", "user.role", "user.cash",
    "prudential_parameter.set", "funding.repay", "funding.process_due", "funding.draw", "funding.set_limit",
    "ftp_rate.apply", "approval.switch_off",
)


_CURRENT = threading.local()


def current_approver():
    """Who approved the write being executed on this thread (set by ``gate``), or None."""
    return getattr(_CURRENT, "approver", None)


def risk_action(path, payload):
    """The high-risk action key for this admin POST, or None. Pure: looks only at the path and the payload."""
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if path.startswith("/internal/admin/db/tables/") and "/rows/" in path:
        return "db.delete" if path.endswith("/delete") else "db.update"
    if path == "/internal/admin/accounting-close":
        return "accounting_close.write"
    if path.startswith("/internal/admin/users/") and path.count("/") == 4:
        if "role" in payload:
            return "user.role"
        if "cash" in payload:
            return "user.cash"
        return None
    if path == "/internal/admin/compliance":
        if action == "prudential_parameter_set" and payload.get("key") in RISK_PARAMETER_KEYS:
            return "prudential_parameter.set"
        if action == "ftp_rate_apply":
            return "ftp_rate.apply"
        return None
    if path == "/internal/admin/funding" and action in FUNDING_ACTIONS:
        return f"funding.{action}"
    if path == "/internal/admin/approvals" and action == "set_switch" and payload.get("enabled") is False:
        return "approval.switch_off"
    return None


def payload_hash(path, payload):
    body = {key: value for key, value in payload.items() if key != "approvalId"}
    text = json.dumps({"path": path, "payload": body}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def enabled(conn):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING,)).fetchone()
    return bool(row) and str(row[0]) == "1"


def active_admins(conn):
    """Active accounts that can act as administrators: the role says admin, or the account is a bootstrap administrator."""
    rows = conn.execute("SELECT user_id,role FROM users WHERE status='active'").fetchall()
    return {str(row[0]) for row in rows if row[1] == 'admin' or str(row[0]) in BOOTSTRAP_ADMIN_IDS}


def _is_admin(conn, user_id):
    return str(user_id) in active_admins(conn)


def gate(conn, actor, path, payload, now=None):
    """``("ok", None)`` when the write may proceed without a second person, ``("ok", id)`` with the approval id to consume when it may
    proceed with one, ``("needs", id)`` after recording a new request when an approval is required and not yet given (the caller must
    commit and answer 409). A bad approval raises ValueError.
    ``approvalId`` is removed from the payload either way, so the strict payload checks of the handlers never see it."""
    _CURRENT.approver = None
    approval_id = payload.pop("approvalId", None) if isinstance(payload, dict) else None
    key = risk_action(path, payload)
    if key is None or not enabled(conn):
        return ("ok", None)
    now = time.time() if now is None else now
    digest = payload_hash(path, payload)
    if approval_id is None:
        cursor = conn.execute(
            "INSERT INTO admin_approval_requests(action_key,payload_hash,summary,requester_id,reason,created_epoch,expires_epoch) "
            "VALUES (?,?,?,?,?,?,?)", (key, digest, f"{key} {path}", str(actor), "", now, now + TTL_SECONDS))
        request_id = int(cursor.lastrowid)
        record_admin_audit(conn, actor, "approval.request", {"request_id": request_id, "action": key, "payload_hash": digest})
        return ("needs", request_id)
    try:
        request_id = int(approval_id)
    except (TypeError, ValueError):
        raise ValueError("invalid_approval_id") from None
    request = conn.execute("SELECT * FROM admin_approval_requests WHERE request_id=?", (request_id,)).fetchone()
    if not request or request["requester_id"] != str(actor) or request["payload_hash"] != digest or request["action_key"] != key:
        raise ValueError("approval_does_not_match_request")
    if now > float(request["expires_epoch"]):
        raise ValueError("approval_expired")
    decision = conn.execute("SELECT * FROM admin_approval_decisions WHERE request_id=?", (request_id,)).fetchone()
    if not decision or decision["decision"] != "approve":
        raise ValueError("approval_not_granted")
    if decision["approver_id"] == str(actor) or not _is_admin(conn, decision["approver_id"]):
        raise ValueError("approver_not_valid")
    if conn.execute("SELECT 1 FROM admin_approval_uses WHERE request_id=?", (request_id,)).fetchone():
        raise ValueError("approval_already_used")
    _CURRENT.approver = decision["approver_id"]
    return ("ok", request_id)


def consume(conn, request_id, actor, now=None):
    now = time.time() if now is None else now
    conn.execute("INSERT INTO admin_approval_uses(request_id,used_by,used_epoch) VALUES (?,?,?)", (request_id, str(actor), now))
    record_admin_audit(conn, actor, "approval.use", {"request_id": request_id})


def decide(conn, approver, request_id, decision, note="", now=None):
    now = time.time() if now is None else now
    if decision not in ("approve", "reject"):
        raise ValueError("invalid_approval_decision")
    request = conn.execute("SELECT * FROM admin_approval_requests WHERE request_id=?", (int(request_id),)).fetchone()
    if not request:
        raise ValueError("approval_request_not_found")
    if request["requester_id"] == str(approver):
        raise ValueError("requester_cannot_decide_own_request")
    if not _is_admin(conn, approver):
        raise ValueError("approver_must_be_administrator")
    if now > float(request["expires_epoch"]):
        raise ValueError("approval_expired")
    if conn.execute("SELECT 1 FROM admin_approval_decisions WHERE request_id=?", (int(request_id),)).fetchone():
        raise ValueError("approval_already_decided")
    conn.execute("INSERT INTO admin_approval_decisions(request_id,approver_id,decision,note,decided_epoch) VALUES (?,?,?,?,?)",
                 (int(request_id), str(approver), decision, str(note or "")[:500], now))
    record_admin_audit(conn, approver, f"approval.{decision}", {"request_id": int(request_id), "action": request["action_key"]})


def set_switch(conn, actor, turn_on):
    """Turn the requirement on (refused unless two active administrators exist) or off (the caller has already passed the gate)."""
    if turn_on and len(active_admins(conn)) < 2:
        raise ValueError("dual_approval_needs_two_administrators")
    conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (SETTING, "1" if turn_on else "0"))
    record_admin_audit(conn, actor, "approval.switch", {"enabled": bool(turn_on)})


def overview(conn, now=None):
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT r.request_id,r.action_key,r.summary,r.requester_id,r.created_epoch,r.expires_epoch,d.approver_id,d.decision,u.used_epoch "
        "FROM admin_approval_requests r LEFT JOIN admin_approval_decisions d ON d.request_id=r.request_id "
        "LEFT JOIN admin_approval_uses u ON u.request_id=r.request_id ORDER BY r.request_id DESC LIMIT 50").fetchall()
    items = []
    for row in rows:
        status = ("used" if row["used_epoch"] else "rejected" if row["decision"] == "reject" else "approved" if row["decision"]
                  else "expired" if now > float(row["expires_epoch"]) else "pending")
        items.append({"requestId": row["request_id"], "action": row["action_key"], "summary": row["summary"], "requester": row["requester_id"],
                      "createdEpoch": float(row["created_epoch"]), "approver": row["approver_id"], "status": status})
    return {"enabled": enabled(conn), "administrators": len(active_admins(conn)), "highRisk": list(HIGH_RISK_ACTIONS), "requests": items}


def record_role_change(conn, actor, target, old_role, new_role, reason=None, approver=None, now=None):
    """Append-only record of an administrator grant or revoke (role to or from ``admin``)."""
    conn.execute("INSERT INTO admin_role_changes(target_id,old_role,new_role,actor_id,reason,approver_id,recorded_epoch) VALUES (?,?,?,?,?,?,?)",
                 (str(target), str(old_role), str(new_role), str(actor), (str(reason).strip()[:500] or None) if reason else None,
                  str(approver) if approver else None, time.time() if now is None else now))
    record_admin_audit(conn, actor, "admin_role.change", {"target": str(target), "old": str(old_role), "new": str(new_role)})
