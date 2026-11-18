"""Consistent online backups of the single SQLite database.

Uses SQLite's backup API, which copies a transactionally consistent snapshot
while the bot keeps running (WAL readers and writers are not blocked for long).
Keeps the newest ``keep`` files. Run ``python -m database.backup`` for one now.
"""
import sqlite3
import time
from pathlib import Path

DEFAULT_KEEP = 14


def backup(db_path, directory=None, keep=DEFAULT_KEEP):
    db_path = Path(db_path)
    directory = Path(directory) if directory else db_path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{db_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        destination = sqlite3.connect(target)
        try:
            # CHECK constraints call these; integrity_check fails without them.
            from database.exact_cost_basis import register
            register(destination)
            from database.wide_values import register as register_wide
            register_wide(destination)
            source.backup(destination, pages=1024, sleep=0.01)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("백업 파일 무결성 검사에 실패했습니다.")
        finally:
            destination.close()
    finally:
        source.close()
    for old in sorted(directory.glob(f"{db_path.stem}-*.db"))[:-keep]:
        old.unlink()
    return target


def backup_postgres(directory=None, keep=DEFAULT_KEEP, dsn=None):
    """pg_dump of the PostgreSQL database in custom format (restorable with pg_restore); keeps the newest ``keep``."""
    import os
    import shutil
    import subprocess
    from database import pg_backend
    directory = Path(directory) if directory else Path(__file__).resolve().parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    pg_dump = shutil.which("pg_dump") or next((c for c in ("/opt/homebrew/opt/postgresql@17/bin/pg_dump", "/opt/homebrew/bin/pg_dump",
                                                          "/usr/local/bin/pg_dump") if os.path.exists(c)), None)
    if not pg_dump:
        raise RuntimeError("pg_dump를 찾을 수 없습니다. PostgreSQL 클라이언트 도구를 설치해주세요.")
    target = directory / f"inazina-pg-{time.strftime('%Y%m%d-%H%M%S')}.dump"
    result = subprocess.run([pg_dump, "--format=custom", "--file", str(target), dsn or pg_backend.dsn()],
                            capture_output=True, text=True, timeout=1800)
    if result.returncode or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump 실패: {result.stderr.strip()[:300]}")
    for old in sorted(directory.glob("inazina-pg-*.dump"))[:-keep]:
        old.unlink()
    return target


def backup_current(db_path, directory=None, keep=DEFAULT_KEEP):
    """The backup of whichever database the app runs on."""
    from database import pg_backend
    return backup_postgres(directory, keep) if pg_backend.enabled() else backup(db_path, directory, keep)


def latest(directory):
    files = sorted(Path(directory).glob("*.db"))
    return files[-1] if files else None


if __name__ == "__main__":
    from database.db_manager import DBManager
    print(backup_current(Path(DBManager.database_dir) / "inazina.db"))
