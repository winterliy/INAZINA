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


def latest(directory):
    files = sorted(Path(directory).glob("*.db"))
    return files[-1] if files else None


if __name__ == "__main__":
    from database.db_manager import DBManager
    print(backup(Path(DBManager.database_dir) / "inazina.db"))
