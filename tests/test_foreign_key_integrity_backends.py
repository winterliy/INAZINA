"""Foreign-key audits must inspect rows even after constraint triggers were disabled."""
import tempfile
from pathlib import Path

import pytest

from database import DatabaseManager, pg_backend
from database.db_manager import DBManager


class _Rollback(Exception):
    pass


def test_foreign_key_check_finds_orphaned_accounting_line():
    previous = DBManager.database_dir
    with tempfile.TemporaryDirectory() as root:
        DBManager.database_dir = Path(root)
        db = DatabaseManager()
        try:
            with pytest.raises(_Rollback), db.atomic() as conn:
                if pg_backend.enabled():
                    conn.execute("SET LOCAL session_replication_role = replica")
                else:
                    conn.execute("PRAGMA defer_foreign_keys=ON")
                conn.execute(
                    "INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) "
                    "VALUES (987654321,'cash',1,0)"
                )
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                assert any(row[0] == 'accounting_lines' and row[2] == 'accounting_entries'
                           for row in problems), problems
                assert any(row[0] == 'accounting_lines' and row[1] is not None for row in problems)
                selected = conn.execute("PRAGMA foreign_key_check('accounting_lines')").fetchall()
                assert any(row[0] == 'accounting_lines' and row[2] == 'accounting_entries'
                           for row in selected), selected
                assert not conn.execute("PRAGMA foreign_key_check('accounting_entries')").fetchall()
                raise _Rollback()
        finally:
            db.close()
            DBManager.database_dir = previous
