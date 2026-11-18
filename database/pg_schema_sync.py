"""Additive schema sync for PostgreSQL.

SQLite creates missing tables itself at start-up; PostgreSQL gets its layout from the generated files. So that a new
table, column, index or trigger in the code reaches a running PostgreSQL database without a manual step, the first
database start of a process builds the current layout (an empty SQLite database made by the current code, converted by
tools/build_postgres_schema.py) and adds whatever the live database lacks. It only ever ADDS: existing tables, rows,
columns and constraints are left alone, and a statement that fails is reported, not forced.

A fingerprint of the code that defines the schema is stored in ``schema_sync_state``; while it is unchanged the sync
costs one query. ``python -m database.pg_schema_sync --dry-run`` lists what would be added.
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_KEY = 727_272_001
_done = False

_INIT = """
import sys, tempfile
sys.path.insert(0, {root!r})
from pathlib import Path
from database.db_manager import DBManager
DBManager.database_dir = Path({folder!r})
from database import DatabaseManager
DatabaseManager().close()
"""


def fingerprint():
    """Hash of the code that can define tables (database/, managers/, web_api/, functions/, the generator)."""
    digest = hashlib.sha256()
    for folder in ("database", "managers", "web_api", "functions"):
        for path in sorted((ROOT / folder).glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    digest.update((ROOT / "tools" / "build_postgres_schema.py").read_bytes())
    return digest.hexdigest()


def generate():
    """(schema_sql, triggers_sql) of the layout the current code creates."""
    env = {**os.environ, "INAZINA_DB_BACKEND": "sqlite"}
    env.pop("INAZINA_PG_DSN", None)
    with tempfile.TemporaryDirectory(prefix="inazina-pg-sync-") as folder:
        subprocess.run([sys.executable, "-c", _INIT.format(root=str(ROOT), folder=folder)], env=env, check=True,
                       capture_output=True, timeout=600)
        out = Path(folder) / "out"
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_postgres_schema.py"), str(Path(folder) / "inazina.db"),
                        str(out / "schema.sql")], env=env, check=True, capture_output=True, timeout=600)
        return (out / "schema.sql").read_text(), (out / "triggers.sql").read_text()


def _statements(schema, triggers):
    tables, indexes, views, constraints, functions, trigger_lines = {}, [], [], [], [], []
    for line in schema.splitlines():
        if line.startswith("CREATE TABLE "):
            tables[re.match(r'CREATE TABLE "?([A-Za-z0-9_]+)"?', line).group(1)] = line
        elif line.startswith(("CREATE INDEX ", "CREATE UNIQUE INDEX ")):
            indexes.append((re.search(r'INDEX "?([A-Za-z0-9_]+)"? ON "?([A-Za-z0-9_]+)"?', line).groups(), line))
        elif line.startswith("CREATE VIEW "):
            views.append((re.match(r'CREATE VIEW "?([A-Za-z0-9_]+)"?', line).group(1), line))
        elif line.startswith("ALTER TABLE ") and ("ADD FOREIGN KEY" in line or "ADD CONSTRAINT" in line):
            constraints.append((re.match(r'ALTER TABLE "?([A-Za-z0-9_]+)"?', line).group(1), line))
    functions = re.findall(r"CREATE OR REPLACE FUNCTION .*?\$\$;", triggers, flags=re.S)
    for line in triggers.splitlines():
        if line.startswith("CREATE TRIGGER "):
            trigger_lines.append((re.match(r'CREATE TRIGGER "?([A-Za-z0-9_]+)"?', line).group(1),
                                  re.search(r' ON "?([A-Za-z0-9_]+)"? ', line).group(1), line))
    return tables, indexes, views, constraints, functions, trigger_lines


def _missing_columns(statement, existing):
    """ALTER statements for columns the generated CREATE TABLE has and the live table lacks."""
    import sqlglot
    from sqlglot import exp
    tree = sqlglot.parse_one(statement, read="postgres")
    table = tree.this.this.name if hasattr(tree.this, "this") and hasattr(tree.this.this, "name") else None
    out = []
    for column in tree.find_all(exp.ColumnDef):
        if column.name not in existing:
            out.append((column.name, column.sql(dialect="postgres")))
    return table, out


def apply(conn, schema, triggers, dry_run=False):
    """Add what is missing through an autocommit psycopg connection. Returns a summary dict."""
    tables, indexes, views, constraints, functions, trigger_lines = _statements(schema, triggers)
    summary = {"tables": [], "columns": [], "indexes": [], "views": [], "constraints": [], "triggers": [], "errors": []}

    def run(sql, label):
        if dry_run:
            return True
        try:
            conn.execute(sql)
            return True
        except Exception as exc:
            summary["errors"].append(f"{label}: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            return False

    present = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")}
    created = set()
    for name, statement in tables.items():
        if name not in present and run(statement, f"table {name}"):
            summary["tables"].append(name)
            created.add(name)
    columns = {}
    for table, column in conn.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema()"):
        columns.setdefault(table, set()).add(column)
    for name, statement in tables.items():
        if name in present and name in columns:
            _, missing = _missing_columns(statement, columns[name])
            for column, definition in missing:
                if run(f'ALTER TABLE "{name}" ADD COLUMN {definition}', f"column {name}.{column}") or (
                        dry_run is False and run(f'ALTER TABLE "{name}" ADD COLUMN {definition.replace(" NOT NULL", "")}',
                                                 f"column {name}.{column} (nullable)")):
                    summary["columns"].append(f"{name}.{column}")
                elif dry_run:
                    summary["columns"].append(f"{name}.{column}")
    have_indexes = {row[0] for row in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()")}
    for (index, table), statement in indexes:
        if index not in have_indexes and (table in created or table in present or dry_run) and run(statement, f"index {index}"):
            summary["indexes"].append(index)
    have_views = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.views WHERE table_schema=current_schema()")}
    for name, statement in views:
        if name not in have_views and run(statement, f"view {name}"):
            summary["views"].append(name)
    for table, statement in constraints:          # foreign keys carry no names, so only new tables get theirs
        if table in created and run(statement, f"foreign key on {table}"):
            summary["constraints"].append(table)
    have_triggers = {row[0] for row in conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")}
    wanted = [item for item in trigger_lines if item[0] not in have_triggers]
    if wanted:
        for function in functions:               # replacing is idempotent; the triggers below need their functions
            run(function, "function")
        for name, table, statement in wanted:
            if run(statement, f"trigger {name}"):
                summary["triggers"].append(name)
    return summary


def ensure_current(dsn=None, dry_run=False, force=False):
    """Run the sync once per process (skipped while the code fingerprint matches what the database last synced to)."""
    global _done
    if _done and not force:
        return None
    from . import pg_backend
    import psycopg
    key = fingerprint()
    with psycopg.connect(dsn or pg_backend.dsn(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM schema_sync_state WHERE key='fingerprint'").fetchone()
            if row and row[0] == key and not force:
                _done = True
                return None
            summary = apply(conn, *generate(), dry_run=dry_run)
            if not dry_run and not summary["errors"]:
                conn.execute("INSERT INTO schema_sync_state(key,value) VALUES ('fingerprint',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key,))
            _done = not dry_run
            return summary
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


if __name__ == "__main__":
    result = ensure_current(dry_run="--dry-run" in sys.argv, force=True)
    for kind, items in (result or {}).items():
        print(f"{kind}: {len(items)}" + (f" -> {', '.join(items[:12])}" if items else ""))
