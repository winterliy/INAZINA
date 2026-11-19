"""Consistent online backups of the PostgreSQL database (pg_dump, custom format).

SQLite backups ended on 2026-11-20 with SQLite support; the old SQLite function is kept below as a comment.
Keeps the newest ``keep`` dumps. Run ``python -m database.backup`` for one now.
"""
# import sqlite3   (only the retired SQLite backup below needed it)
import time
from pathlib import Path

DEFAULT_KEEP = 14


# ---- SQLite online backup: retired 2026-11-20 (the app runs on PostgreSQL only); kept for reference, not used ----
# def backup(db_path, directory=None, keep=DEFAULT_KEEP):
#     db_path = Path(db_path)
#     directory = Path(directory) if directory else db_path.parent / "backups"
#     directory.mkdir(parents=True, exist_ok=True)
#     target = directory / f"{db_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
#     source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
#     try:
#         destination = sqlite3.connect(target)
#         try:
#             # CHECK constraints call these; integrity_check fails without them.
#             from database.exact_cost_basis import register
#             register(destination)
#             from database.wide_values import register as register_wide
#             register_wide(destination)
#             source.backup(destination, pages=1024, sleep=0.01)
#             if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
#                 raise RuntimeError("백업 파일 무결성 검사에 실패했습니다.")
#         finally:
#             destination.close()
#     finally:
#         source.close()
#     for old in sorted(directory.glob(f"{db_path.stem}-*.db"))[:-keep]:
#         old.unlink()
#     return target


def pg_tool(name):
    """Path of a PostgreSQL client tool (pg_dump, pg_restore, ...), on PATH or in a Homebrew location."""
    import os
    import shutil
    found = shutil.which(name) or next((f"{base}/{name}" for base in ("/opt/homebrew/opt/postgresql@17/bin", "/opt/homebrew/bin", "/usr/local/bin")
                                        if os.path.exists(f"{base}/{name}")), None)
    if not found:
        raise RuntimeError(f"{name}를 찾을 수 없습니다. PostgreSQL 클라이언트 도구를 설치해주세요.")
    return found


def backup_postgres(directory=None, keep=DEFAULT_KEEP, dsn=None):
    """pg_dump of the PostgreSQL database in custom format (restorable with pg_restore); keeps the newest ``keep``."""
    import subprocess
    from database import pg_backend
    directory = Path(directory) if directory else Path(__file__).resolve().parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    pg_dump = pg_tool("pg_dump")
    target = directory / f"inazina-pg-{time.strftime('%Y%m%d-%H%M%S')}.dump"
    result = subprocess.run([pg_dump, "--format=custom", "--file", str(target), dsn or pg_backend.dsn()],
                            capture_output=True, text=True, timeout=1800)
    if result.returncode or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump 실패: {result.stderr.strip()[:300]}")
    for old in sorted(directory.glob("inazina-pg-*.dump"))[:-keep]:
        old.unlink()
    return target


def backup_current(db_path=None, directory=None, keep=DEFAULT_KEEP):
    """The backup of the database the app runs on (PostgreSQL). ``db_path`` is accepted for old callers and ignored."""
    return backup_postgres(directory, keep)


def latest(directory):
    files = sorted(Path(directory).glob("inazina-pg-*.dump"))
    return files[-1] if files else None


if __name__ == "__main__":
    print(backup_current())
