"""Read-only data for the 통제 · 복구 admin screen: the control-evidence matrix with each piece of evidence checked against the
test files, the dated drill/access-review records, and the stress scenarios. Nothing here writes."""
import ast
import json
import os
import re
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "controls_laplace.md"
RECORDS = ROOT / "docs" / "m8_records" / "drills.json"
BACKUPS = ROOT / "database" / "backups"
ACCESS_REVIEW_MAX_DAYS = 35      # monthly review plus a few days of slack (docs/m8_access_review_procedure.md)


def backup_status(directory=BACKUPS, now=None, max_age_hours=None):
    """Read-only freshness check of the newest pg_dump file. It never creates or deletes a backup."""
    limit = float(max_age_hours if max_age_hours is not None else os.environ.get("INAZINA_BACKUP_MAX_AGE_HOURS") or 48)
    files = sorted(Path(directory).glob("inazina-pg-*.dump"), key=lambda path: path.stat().st_mtime) if Path(directory).is_dir() else []
    files = [path for path in files if path.stat().st_size > 0]
    if not files:
        return {"status": "missing", "count": 0, "maxAgeHours": limit, "latest": None, "ageHours": None}
    age = ((now if now is not None else time.time()) - files[-1].stat().st_mtime) / 3600
    return {"status": "stale" if age > limit else "ok", "count": len(files), "maxAgeHours": limit,
            "latest": files[-1].name, "ageHours": round(age, 1)}


def access_review_status(reviews, today=None):
    """The newest dated access review; overdue when older than ACCESS_REVIEW_MAX_DAYS."""
    dates = sorted(date.fromisoformat(item["date"]) for item in reviews)
    if not dates:
        return {"status": "missing", "lastDate": None, "ageDays": None, "maxDays": ACCESS_REVIEW_MAX_DAYS}
    age = ((today or date.today()) - dates[-1]).days
    return {"status": "overdue" if age > ACCESS_REVIEW_MAX_DAYS else "ok", "lastDate": dates[-1].isoformat(), "ageDays": age, "maxDays": ACCESS_REVIEW_MAX_DAYS}


def _defined(path):
    try:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def controls():
    rows, cache = [], {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not re.fullmatch(r"C-\d{2}", cells[0]):
            continue
        evidence = []
        for reference in (item.strip() for item in cells[3].split(",") if item.strip()):
            path, _, name = reference.partition("::")
            if path not in cache:
                cache[path] = _defined(path)
            evidence.append({"path": path, "test": name, "found": name in cache[path]})
        rows.append({"id": cells[0], "control": cells[1], "location": cells[2], "evidence": evidence,
                     "status": "verified" if evidence and all(item["found"] for item in evidence) else "broken"})
    return rows


KST_OFFSET = 9 * 3600
OFF_HOURS = range(2, 6)            # 02:00-05:59 KST counts as an unusual hour for an administrator write
LOGIN_FAILURE_WINDOW = 3600
LOGIN_FAILURE_LIMIT = 5            # failed admin logins within the window
NEW_ADMIN_DAYS = 7
ROTATION_MAX_DAYS = 180
READ_SUFFIXES = (".read", ".list", ".preview", ".view")


def _is_write(action):
    """Heuristic: an audit action is a write unless its name ends like a read (``*.read``, ``*.list``, ``*.preview``, ``*.view``)."""
    return not str(action).endswith(READ_SUFFIXES)


def anomalies(conn, now=None):
    """Read-only alerts computed from admin_login_events and admin_audit: failed-login spike, administrators first seen recently,
    administrator writes at unusual hours (KST). ``system:`` actors are automation and are left out. Returns a list of rows."""
    now = time.time() if now is None else now
    rows = []
    try:
        failures = conn.execute("SELECT count(*) FROM admin_login_events WHERE success=0 AND created_epoch>=?", (now - LOGIN_FAILURE_WINDOW,)).fetchone()[0]
        rows.append({"key": "login_failures", "label": "관리자 로그인 실패 급증", "status": "warn" if failures >= LOGIN_FAILURE_LIMIT else "ok",
                     "detail": f"최근 {LOGIN_FAILURE_WINDOW // 3600}시간 실패 {failures}건 (기준 {LOGIN_FAILURE_LIMIT}건)"})
        recent = conn.execute(
            "SELECT e.user_id, min(e.created_epoch) first_seen FROM admin_login_events e JOIN users u ON u.user_id=e.user_id "
            "WHERE e.success=1 AND u.role='admin' GROUP BY e.user_id HAVING min(e.created_epoch)>=?", (now - NEW_ADMIN_DAYS * 86400,)).fetchall()
        rows.append({"key": "new_admin", "label": "새 관리자 접속", "status": "warn" if recent else "ok",
                     "detail": (f"최근 {NEW_ADMIN_DAYS}일 안에 처음 접속한 관리자 {len(recent)}명 (끝 4자리 " + ", ".join(str(r[0])[-4:] for r in recent) + ")") if recent
                     else f"최근 {NEW_ADMIN_DAYS}일 안에 처음 접속한 관리자 없음"})
    except Exception:
        rows.append({"key": "login_events", "label": "관리자 로그인 기록", "status": "unknown", "detail": "로그인 기록 표를 읽지 못했습니다."})
    try:
        audit = conn.execute("SELECT actor_id, action, recorded_epoch FROM admin_audit WHERE recorded_epoch>=?", (now - 7 * 86400,)).fetchall()
        night = [row for row in audit if _is_write(row[1]) and not str(row[0]).startswith("system:")
                 and int(((float(row[2]) + KST_OFFSET) % 86400) // 3600) in OFF_HOURS]
        top = {}
        for row in night:
            top[row[1]] = top.get(row[1], 0) + 1
        rows.append({"key": "off_hours_writes", "label": "비정상 시간대 관리자 쓰기", "status": "warn" if night else "ok",
                     "detail": (f"최근 7일 02~06시(KST) 쓰기 {len(night)}건: " + ", ".join(f"{a} {n}" for a, n in sorted(top.items(), key=lambda x: -x[1])[:4])) if night
                     else "최근 7일 02~06시(KST) 관리자 쓰기 없음"})
    except Exception:
        rows.append({"key": "off_hours_writes", "label": "비정상 시간대 관리자 쓰기", "status": "unknown", "detail": "감사 기록을 읽지 못했습니다."})
    return rows


def rotation_status(rotations, today=None):
    """Per secret name: last recorded rotation date (names and dates only, never values) and whether it is overdue."""
    today = today or date.today()
    out = []
    for item in rotations:
        last = item.get("last_rotated")
        age = (today - date.fromisoformat(last)).days if last else None
        out.append({"name": item["name"], "lastRotated": last, "ageDays": age, "maxDays": ROTATION_MAX_DAYS,
                    "status": "missing" if last is None else "overdue" if age > ROTATION_MAX_DAYS else "ok"})
    return out


def overview(conn=None):
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    rows = controls()
    return {"controls": rows,
            "summary": {"total": len(rows), "verified": sum(row["status"] == "verified" for row in rows)},
            "restoreDrills": records.get("restore_drills", []), "accessReviews": records.get("access_reviews", []),
            "backup": backup_status(), "accessReview": access_review_status(records.get("access_reviews", [])),
            "rotations": rotation_status(records.get("secret_rotations", [])),
            "anomalies": anomalies(conn) if conn is not None else [],
            "stress": records.get("stress", {})}
