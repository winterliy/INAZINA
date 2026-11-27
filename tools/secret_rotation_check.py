#!/usr/bin/env python3
"""Rotation check for the internal token and other secrets. Prints names, lengths and dates, NEVER values.

    python tools/secret_rotation_check.py                 # check
    python tools/secret_rotation_check.py --record INAZINA_INTERNAL_TOKEN   # after rotating: record today's date in docs/m8_records/drills.json

Checks: the token's length (from the environment; at least 32 characters), credential patterns left in logs/*.log and in tracked files
(functions/secret_patterns.py), and the last recorded rotation date per secret (older than 180 days is overdue). Exit 1 when any check fails.
Procedure: docs/m8_secret_rotation.md.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RECORDS = ROOT / "docs" / "m8_records" / "drills.json"
MIN_TOKEN_LENGTH = 32


def scan_text(text):
    from functions.secret_patterns import SECRET_PATTERNS
    return sorted(label for label, pattern in SECRET_PATTERNS.items() if re.search(pattern, text))


def run(environ=os.environ, root=ROOT, today=None):
    problems, report = [], {}
    token = environ.get("INAZINA_INTERNAL_TOKEN", "")
    report["token"] = {"set": bool(token), "length": len(token)}
    if not token:
        problems.append("INAZINA_INTERNAL_TOKEN is not set in this environment")
    elif len(token) < MIN_TOKEN_LENGTH:
        problems.append(f"INAZINA_INTERNAL_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters")
    found = {}
    for path in sorted((root / "logs").glob("*.log")) if (root / "logs").is_dir() else []:
        labels = scan_text(path.read_text(encoding="utf-8", errors="ignore"))
        if labels:
            found[str(path.relative_to(root))] = labels
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True)
    if tracked.returncode == 0:
        for name in tracked.stdout.split("\0"):
            file = root / name
            if name and file.suffix in {".py", ".ts", ".tsx", ".json", ".md", ".sh", ".yml", ".toml", ".env", ".example"} and file.is_file() \
                    and name != "functions/secret_patterns.py" and not name.startswith("tests/"):
                labels = scan_text(file.read_text(encoding="utf-8", errors="ignore"))
                if labels:
                    found[name] = labels
    report["patterns_found_in"] = found
    if found:
        problems.append("credential patterns in: " + ", ".join(found))
    from web_api.controls_overview import rotation_status
    records = json.loads((root / "docs" / "m8_records" / "drills.json").read_text(encoding="utf-8")) if (root / "docs" / "m8_records" / "drills.json").exists() else {}
    report["rotations"] = rotation_status(records.get("secret_rotations", []), today)
    problems += [f"{row['name']}: rotation {row['status']}" for row in report["rotations"] if row["status"] != "ok"]
    return report, problems


def record(name, today=None):
    data = json.loads(RECORDS.read_text(encoding="utf-8"))
    for item in data.setdefault("secret_rotations", []):
        if item["name"] == name:
            item["last_rotated"] = (today or date.today()).isoformat()
            break
    else:
        data["secret_rotations"].append({"name": name, "last_rotated": (today or date.today()).isoformat()})
    RECORDS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--record":
        record(sys.argv[2])
    report, problems = run()
    print(json.dumps({"report": report, "problems": problems}, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
