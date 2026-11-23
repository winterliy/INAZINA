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


def overview():
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    rows = controls()
    return {"controls": rows,
            "summary": {"total": len(rows), "verified": sum(row["status"] == "verified" for row in rows)},
            "restoreDrills": records.get("restore_drills", []), "accessReviews": records.get("access_reviews", []),
            "backup": backup_status(), "accessReview": access_review_status(records.get("access_reviews", [])),
            "stress": records.get("stress", {})}
