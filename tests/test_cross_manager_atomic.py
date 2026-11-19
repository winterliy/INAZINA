"""The cross-manager rollback contract also applies without SQLite files."""
import tempfile
from pathlib import Path

import pytest

from database import DatabaseManager
from database.db_manager import DBManager


def test_cross_manager_atomic_rollback():
    previous = DBManager.database_dir
    with tempfile.TemporaryDirectory() as root:
        DBManager.database_dir = Path(root)
        db = DatabaseManager()
        try:
            db.users.create_user('atomic-user', '시민', '미국', 1000000)
            original_rate = db.system.get_setting('base_rate')
            paths = {manager.db_path for manager in
                     (db.users, db.assets, db.accounts, db.products, db.system, db.currency)}
            assert len(paths) == 1
            with pytest.raises(RuntimeError), db.atomic(('users', 'system')) as conn:
                conn.execute("UPDATE users.users SET cash=1 WHERE user_id='atomic-user'")
                conn.execute("UPDATE system.settings SET value='7' WHERE key='base_rate'")
                raise RuntimeError('rollback')
            assert db.users.get_user('atomic-user')['cash'] == 1000000
            assert db.system.get_setting('base_rate') == original_rate
        finally:
            db.close()
            DBManager.database_dir = previous
