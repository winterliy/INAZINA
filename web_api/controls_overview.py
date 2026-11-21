"""Read-only data for the 통제 · 복구 admin screen: the control-evidence matrix with each piece of evidence checked against the
test files, the dated drill/access-review records, and the stress scenarios. Nothing here writes."""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "controls_laplace.md"
RECORDS = ROOT / "docs" / "m8_records" / "drills.json"


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
            "stress": records.get("stress", {})}
