"""Skip startup migrations that already ran against exactly this schema and code.

The migrations stay in the code: a restored older backup has no marker (or a
different schema), so they run on it as before. A migration is skipped only when
the code is unchanged, the schema at startup equals the schema the last complete
initialization ended with, and the schema just before the migration equals what
it was at that point last time (earlier steps may drop objects it recreates).
"""
import hashlib
import sqlite3
from functools import lru_cache
from pathlib import Path

_TABLE = "startup_migration_state"


@lru_cache(maxsize=1)
def code_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def schema_hash(connection):
    digest = hashlib.sha256()
    for row in sqlite3.Connection.execute(
            connection, "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            f"WHERE name<>'{_TABLE}' ORDER BY type,name"):
        digest.update("\x1f".join(map(str, row)).encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


def recorded(connection):
    """(pre-migration hash) when the schema still matches the last complete run, else None."""
    try:
        row = sqlite3.Connection.execute(
            connection, f"SELECT code_hash,end_hash,pre_hash FROM {_TABLE} WHERE id=1").fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] != code_hash() or row[1] != schema_hash(connection):
        return None
    return row[2]


def record(connection, pre_hash):
    sqlite3.Connection.execute(connection, f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                               "id INTEGER PRIMARY KEY CHECK(id=1), code_hash TEXT NOT NULL, "
                               "end_hash TEXT NOT NULL, pre_hash TEXT NOT NULL)")
    sqlite3.Connection.execute(
        connection, f"INSERT INTO {_TABLE}(id,code_hash,end_hash,pre_hash) VALUES (1,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET code_hash=excluded.code_hash,"
        "end_hash=excluded.end_hash,pre_hash=excluded.pre_hash",
        (code_hash(), schema_hash(connection), pre_hash))
