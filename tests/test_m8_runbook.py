"""M8 production-migration runbook: every tool it names exists, the preview tool refuses the live database, and the runbook keeps its sections."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs" / "m8_prod_migration_runbook.md").read_text(encoding="utf-8")


def test_runbook_names_only_existing_tools_and_has_every_section():
    for tool in set(re.findall(r"tools/[a-z_]+\.py", RUNBOOK)):
        assert (ROOT / tool).is_file(), tool
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 리허설 기록"):
        assert heading in RUNBOOK, heading
    assert "운영 DB에는 쓰지 않았다" in RUNBOOK


def test_schema_preview_refuses_the_live_database():
    run = subprocess.run([sys.executable, str(ROOT / "tools" / "pg_schema_preview.py")], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "INAZINA_PG_DSN": "postgresql:///inazina"}, cwd=ROOT)
    assert run.returncode != 0 and "copy" in run.stderr


def test_runbook_backup_commands_spell_out_the_production_dsn():
    backup_lines = [line for line in RUNBOOK.splitlines() if "database.backup" in line and line.lstrip().startswith(("INAZINA", ".venv"))]
    assert backup_lines and all("INAZINA_PG_DSN=postgresql:///inazina " in line for line in backup_lines), backup_lines
    assert RUNBOOK.count("--expect-dbname inazina") >= 2
