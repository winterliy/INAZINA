"""The attempt log of the browser-access pages on the API (4312) and data-service (4313) ports.

Those pages sign in with an administrator's dashboard id and password (or the emergency passcode); this
module only keeps the record both servers share: when, which service, from where, which id was typed, and
whether it was let in. The dashboard's 보안 tab reads it. Passwords are never written here.
"""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATTEMPTS = Path(os.environ.get("INAZINA_BROWSER_ACCESS_LOG", ROOT / "database" / "browser-access.log.jsonl"))
SERVICES = {"api": ("API 서버", 4312), "python": ("데이터 서비스", 4313)}
LOG_KEEP = 2000
LOCK_ATTEMPTS, LOCK_SECONDS = 10, 600


def log_attempt(service, address, ok, login_id="", via="login"):
    """One JSON line per attempt. `via` is "login" (an administrator id and password) or "emergency"."""
    try:
        ATTEMPTS.parent.mkdir(parents=True, exist_ok=True)
        with ATTEMPTS.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": int(time.time()), "service": service, "ip": str(address)[:64], "ok": bool(ok),
                                     "loginId": str(login_id)[:80], "via": via}, ensure_ascii=False) + "\n")
        if ATTEMPTS.stat().st_size > 400_000:
            lines = ATTEMPTS.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]
            ATTEMPTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def recent_attempts(limit=100):
    try:
        lines = ATTEMPTS.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def overview():
    now = time.time()
    attempts = recent_attempts(200)
    failures = {}
    for row in attempts:
        if not row.get("ok") and now - row.get("at", 0) <= LOCK_SECONDS:
            key = (row.get("service"), row.get("ip"))
            failures[key] = failures.get(key, 0) + 1
    locked = [{"service": service, "ip": ip} for (service, ip), count in sorted(failures.items(), key=str) if count >= LOCK_ATTEMPTS]
    day = [row for row in attempts if now - row.get("at", 0) <= 86400]
    return {
        "attempts": attempts[:100],
        "last24h": {"total": len(day), "failed": sum(1 for row in day if not row.get("ok")), "emergency": sum(1 for row in day if row.get("via") == "emergency" and row.get("ok"))},
        "locked": locked,
        "services": [{"id": key, "label": label, "port": port} for key, (label, port) in SERVICES.items()],
        "policy": {"lockAttempts": LOCK_ATTEMPTS, "lockSeconds": LOCK_SECONDS},
    }
