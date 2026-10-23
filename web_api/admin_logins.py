"""Login history for administrator accounts: when, from where (IP), which app and device.

Only logins to an administrator account are recorded, successful or not, so a
failed attempt on an admin ID is visible too. Rows older than the retention
period are deleted on every write.
"""
import ipaddress
import time

RETENTION_SECONDS = 365 * 86400
APPS = {"dashboard": "대시보드", "hts": "HTS"}


def ensure_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS admin_login_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        login_id TEXT NOT NULL,
        success INTEGER NOT NULL CHECK(success IN (0,1)),
        reason TEXT,
        ip TEXT NOT NULL,
        location TEXT NOT NULL,
        app TEXT,
        device TEXT,
        user_agent TEXT,
        created_epoch INTEGER NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_admin_login_events_user "
                       "ON admin_login_events(user_id, event_id)")


def location(ip):
    """Where the login came from, as far as the address alone can tell."""
    try:
        address = ipaddress.ip_address(str(ip).removeprefix("::ffff:"))
    except ValueError:
        return "알 수 없음"
    if address.is_loopback:
        return "이 컴퓨터 (로컬)"
    if address.is_private or address.is_link_local:
        return "내부 네트워크"
    return "외부 인터넷"


def device(user_agent):
    """Operating system and browser from the User-Agent header, e.g. 'macOS · Chrome'."""
    agent = str(user_agent or "")
    system = next((name for marker, name in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
        ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"),
    ) if marker in agent), "")
    browser = next((name for marker, name in (
        ("Whale/", "Whale"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser/", "Samsung Internet"),
        ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"),
    ) if marker in agent), "")
    return " · ".join(part for part in (system, browser) if part) or "알 수 없음"


def admin_account(connection, login_id, bootstrap_ids=()):
    """The administrator a login ID names (Discord ID or HTS ID), or None for anyone else."""
    login_id = str(login_id or "").strip()
    if not login_id:
        return None
    row = connection.execute(
        "SELECT u.user_id,u.role FROM users u WHERE u.user_id=? "
        "UNION ALL SELECT u.user_id,u.role FROM web_auth_credentials w JOIN users u ON u.user_id=w.user_id "
        "WHERE w.hts_id=? LIMIT 1", (login_id, login_id.lower()),
    ).fetchone()
    if not row:
        return None
    user_id, role = str(row[0]), str(row[1] or "").strip().lower()
    return user_id if role == "admin" or user_id in bootstrap_ids else None


def record(connection, login_id, success, meta, *, bootstrap_ids=(), reason=None, now=None):
    """Store one admin login attempt. Returns the event id, or None if it was not an admin login."""
    ensure_table(connection)
    user_id = admin_account(connection, login_id, bootstrap_ids)
    if not user_id:
        return None
    now = int(time.time() if now is None else now)
    meta = meta if isinstance(meta, dict) else {}
    ip = str(meta.get("clientIp") or "unknown")[:64]
    app = str(meta.get("app") or "")
    user_agent = str(meta.get("userAgent") or "")[:300]
    cursor = connection.execute(
        "INSERT INTO admin_login_events(user_id,login_id,success,reason,ip,location,app,device,user_agent,created_epoch) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, str(login_id).strip()[:80], 1 if success else 0, reason, ip, location(ip),
         app if app in APPS else None, device(user_agent), user_agent or None, now),
    )
    connection.execute("DELETE FROM admin_login_events WHERE created_epoch<?", (now - RETENTION_SECONDS,))
    return cursor.lastrowid


def history(connection, user_id=None, limit=200):
    ensure_table(connection)
    rows = connection.execute(
        "SELECT e.*,u.nickname FROM admin_login_events e LEFT JOIN users u ON u.user_id=e.user_id "
        "WHERE (? IS NULL OR e.user_id=?) ORDER BY e.event_id DESC LIMIT ?",
        (user_id, user_id, max(1, min(int(limit), 500))),
    ).fetchall()
    return [{
        "eventId": row["event_id"], "userId": row["user_id"], "nickname": row["nickname"],
        "loginId": row["login_id"], "success": bool(row["success"]), "reason": row["reason"],
        "ip": row["ip"], "location": row["location"], "app": APPS.get(row["app"] or "", None),
        "device": row["device"], "userAgent": row["user_agent"], "time": row["created_epoch"],
    } for row in rows]
