"""M8 records: the dated drill/access-review log is well formed and the threat model keeps its honesty markers."""
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS = json.loads((ROOT / "docs" / "m8_records" / "drills.json").read_text(encoding="utf-8"))


def test_restore_drill_records_are_dated_and_report_cleanup():
    assert RECORDS["restore_drills"]
    for drill in RECORDS["restore_drills"]:
        date.fromisoformat(drill["date"])
        assert isinstance(drill["ok"], bool) and isinstance(drill["cleanup_ok"], bool)
        assert drill["command"].startswith("tools/backup_restore_drill.py")
        if not drill["ok"]:
            assert drill.get("error"), "a failed drill must say why"


def test_access_review_lists_admins_and_masks_ids():
    for review in RECORDS["access_reviews"]:
        date.fromisoformat(review["date"])
        assert review["admin_count"] == len(review["admins"])
        assert all(re.fullmatch(r"\d{4}", admin["user_id_tail"]) for admin in review["admins"])


def test_threat_model_rows_have_controls_or_are_marked_missing():
    text = (ROOT / "docs" / "threat_model_laplace.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if re.match(r"\| T-\d{2} \|", line)]
    assert len(rows) >= 10
    matrix = (ROOT / "docs" / "controls_laplace.md").read_text(encoding="utf-8")
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) == 5, row
        controls = re.findall(r"C-\d{2}", row)
        assert all(f"| {c} |" in matrix for c in controls), f"unknown control in {cells[0]}"
        assert controls or "미비" in row or cells[3], f"{cells[0]} has neither a control nor a 미비 mark"


def test_controls_screen_data_marks_every_control_verified():
    from web_api import controls_overview
    data = controls_overview.overview()
    assert data["summary"]["total"] >= 10
    assert [row["id"] for row in data["controls"] if row["status"] != "verified"] == []
    assert data["restoreDrills"] and data["accessReviews"] and len(data["stress"]["scenarios"]) == 6
