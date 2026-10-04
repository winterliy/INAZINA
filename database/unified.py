"""One SQLite file, with a lossless, resumable upgrade from the legacy files."""
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

ALIASES = ("users", "accounts", "assets", "product", "company", "pot", "tax", "system")
# Ignore SQL strings and comments; legacy schema qualifiers are an internal
# compatibility layer, never attached copies of the same physical database.
_SQL = re.compile(r"' (?:[^']|'')* ' | --[^\n]* | /\*.*?\*/ | "
                  r'\b(?:users|accounts|assets|product|company|pot|tax|system)\.(?=[A-Za-z_"\[])',
                  re.X | re.S)

def sql(statement):
    return _SQL.sub(lambda m: m.group() if m.group().startswith(("'", "--", "/*")) else "", statement)

class UnifiedCursor(sqlite3.Cursor):
    def execute(self, statement, parameters=()):
        return super().execute(sql(statement), parameters)

    def executemany(self, statement, parameters):
        return super().executemany(sql(statement), parameters)

class UnifiedConnection(sqlite3.Connection):
    def execute(self, statement, parameters=()):
        return super().execute(sql(statement), parameters)

    def executemany(self, statement, parameters):
        return super().executemany(sql(statement), parameters)

    def cursor(self, factory=UnifiedCursor):
        return super().cursor(factory)

@contextmanager
def _legacy_snapshot(root):
    # Acquire writer reservations for every source before copying any table.
    # A concurrent old-version transfer cannot straddle the snapshot boundary.
    with closing(sqlite3.connect(':memory:',timeout=30,isolation_level=None)) as source:
        attached=[]
        for alias in ALIASES:
            path=root/f'{alias}.db'
            if path.exists():
                source.execute(f'ATTACH DATABASE ? AS {alias}',(str(path),))
                attached.append(alias)
        source.execute('BEGIN IMMEDIATE')
        try:
            yield source,attached
        finally:
            source.rollback()

def migrate(root):
    """Build a verified staging file, then atomically publish it. Keep originals."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "inazina.db"
    if target.exists():
        return target
    staging = root / "inazina.migrating"
    # A previous unsuccessful upgrade has no authority over the legacy files.
    if staging.exists():
        staging.unlink()
    counts = {}
    with _legacy_snapshot(root) as (src,attached), closing(sqlite3.connect(staging)) as dest:
        try:
            dest.execute("BEGIN IMMEDIATE")
            objects = []
            for alias in attached:
                schema = src.execute(f"SELECT type, name, sql FROM {alias}.sqlite_master "
                                     "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                                     "ORDER BY type, name").fetchall()
                for kind, name, ddl in schema:
                    if kind != "table":
                        objects.append(ddl)
                        continue
                    if name in counts:
                        raise RuntimeError(f"중복 테이블로 DB 통합 중단: {name}")
                    dest.execute(ddl)
                    escaped = name.replace('"', '""')
                    rows = src.execute(f'SELECT * FROM {alias}."{escaped}"')
                    width = len(rows.description)
                    insert = f'INSERT INTO "{escaped}" VALUES ({",".join("?" for _ in range(width))})'
                    count = 0
                    while batch := rows.fetchmany(1000):
                        dest.executemany(insert, batch)
                        count += len(batch)
                    counts[name] = count
                if src.execute(f"SELECT 1 FROM {alias}.sqlite_master WHERE name='sqlite_sequence'").fetchone():
                    for name, seq in src.execute(f"SELECT name, seq FROM {alias}.sqlite_sequence"):
                        dest.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (seq, name))
            for ddl in objects:
                dest.execute(ddl)
            for name, expected in counts.items():
                escaped = name.replace('"', '""')
                if dest.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0] != expected:
                    raise RuntimeError(f"DB 통합 건수 검증 실패: {name}")
            dest.execute("CREATE TABLE migration_manifest (source TEXT PRIMARY KEY, imported_at TEXT "
                         "NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            dest.executemany("INSERT INTO migration_manifest(source) VALUES (?)", [(a,) for a in attached])
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("DB 무결성 검증 실패")
            dest.commit()
        except BaseException:
            dest.rollback()
            raise
    os.replace(staging, target)
    return target
