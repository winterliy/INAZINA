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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every Inazina connection enforces declared REFERENCES (see database/integrity.py).
        super().execute("PRAGMA foreign_keys=ON")
        self.create_function("base_currency_code", 0, self._base_currency_code)

    def _base_currency_code(self):
        """Resolve the configured base denomination inside SQL expressions/triggers."""
        try:
            row = sqlite3.Connection.execute(
                self,
                "SELECT base_currency_code FROM currency_system_settings WHERE singleton_id=1",
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return str(row[0]).upper() if row and row[0] else None

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
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("DB 무결성 검증 실패")
            dest.commit()
        except BaseException:
            dest.rollback()
            raise
    os.replace(staging, target)
    return target
