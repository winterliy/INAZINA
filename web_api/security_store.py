"""Browser-access passwords for the two private service ports, and the attempt log.

Opening the API (4312) or the data service (4313) in a browser asks for a password. Each service has its own,
changed from the dashboard's 보안 tab. The file below is the single source: the Python service and the Node API
both read it on every attempt, so a change applies at once without a restart.

New passwords are stored salted (PBKDF2-SHA256). The old unsalted SHA-256 hex of the environment variable or the
built-in default still verifies, so nothing breaks before the first change.
"""
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = Path(os.environ.get("INAZINA_SECURITY_PATH", ROOT / "database" / "security.json"))
ATTEMPTS = Path(os.environ.get("INAZINA_BROWSER_ACCESS_LOG", ROOT / "database" / "browser-access.log.jsonl"))
DEFAULT_SHA256 = "04b2ad6979a436129b5f30cd00a3804bd87fc8bef73b0bb38640bf49a8479c64"
SERVICES = {"api": ("API 서버", 4312), "python": ("데이터 서비스", 4313)}
ITERATIONS = 210_000
MIN_LENGTH, MAX_LENGTH = 8, 64
LOG_KEEP = 2000
LOCK_ATTEMPTS, LOCK_SECONDS = 10, 600


def _read():
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=STORE.parent, prefix=".security-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, STORE)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def _matches(stored, password):
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt, digest = stored.split("$")
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)).hex()
        except ValueError:
            return False
        return hmac.compare_digest(actual, digest)
    return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), stored)


def _stored_hash(service):
    entry = _read().get(service)
    if isinstance(entry, dict) and isinstance(entry.get("hash"), str):
        return entry["hash"]
    return os.environ.get("INAZINA_BROWSER_PASSWORD_SHA256") or DEFAULT_SHA256


def verify(service, password):
    return service in SERVICES and _matches(_stored_hash(service), str(password))


def set_password(service, current, new, actor):
    if service not in SERVICES:
        raise ValueError("서비스를 확인해주세요.")
    if not verify(service, current):
        raise ValueError("현재 비밀번호가 맞지 않습니다.")
    if new == current:
        raise ValueError("현재와 다른 비밀번호를 입력해주세요.")
    reset_password(service, new, actor)


def reset_password(service, new, actor="local-reset"):
    """Set a password without knowing the current one (the local reset tool); same rules for the new value."""
    if service not in SERVICES:
        raise ValueError("서비스를 확인해주세요.")
    if not isinstance(new, str) or not MIN_LENGTH <= len(new) <= MAX_LENGTH:
        raise ValueError(f"새 비밀번호는 {MIN_LENGTH}~{MAX_LENGTH}자로 입력해주세요.")
    data = _read()
    data[service] = {"hash": hash_password(new), "updatedEpoch": int(time.time()), "updatedBy": str(actor)}
    _write(data)


def log_attempt(service, address, ok):
    """One JSON line per attempt: when, which service, from where, and whether it passed. Never the password."""
    try:
        ATTEMPTS.parent.mkdir(parents=True, exist_ok=True)
        with ATTEMPTS.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": int(time.time()), "service": service, "ip": str(address)[:64], "ok": bool(ok)}) + "\n")
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
    data = _read()
    now = time.time()
    attempts = recent_attempts(200)
    failures = {}
    for row in attempts:
        if not row.get("ok") and now - row.get("at", 0) <= LOCK_SECONDS:
            key = (row.get("service"), row.get("ip"))
            failures[key] = failures.get(key, 0) + 1
    services = []
    for service, (label, port) in SERVICES.items():
        entry = data.get(service) if isinstance(data.get(service), dict) else {}
        stored = _stored_hash(service)
        services.append({
            "id": service, "label": label, "port": port,
            "isDefault": not entry.get("hash") and stored == DEFAULT_SHA256,
            "updatedEpoch": entry.get("updatedEpoch"), "updatedBy": entry.get("updatedBy"),
            "locked": sorted({ip for (svc, ip), count in failures.items() if svc == service and count >= LOCK_ATTEMPTS}),
        })
    day = [row for row in attempts if now - row.get("at", 0) <= 86400]
    return {
        "services": services, "attempts": attempts[:100],
        "last24h": {"total": len(day), "failed": sum(1 for row in day if not row.get("ok"))},
        "policy": {"minLength": MIN_LENGTH, "maxLength": MAX_LENGTH, "lockAttempts": LOCK_ATTEMPTS, "lockSeconds": LOCK_SECONDS,
                   "storage": f"PBKDF2-SHA256 {ITERATIONS:,}회 · 솔트 포함"},
    }
