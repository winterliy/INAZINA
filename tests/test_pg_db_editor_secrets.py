"""Check secret masking in the database editor on PostgreSQL."""
import tempfile
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from web_api import server


def test_db_editor_masks_secrets_and_audit_payload():
    previous = DBManager.database_dir
    with tempfile.TemporaryDirectory() as root:
        DBManager.database_dir = Path(root)
        db = DatabaseManager()
        try:
            db.users.create_user('editor-admin', '관리자', '낙원', 0, 'admin')
            # PostgreSQL's migrated text-key tables carry a dedicated rowid column.
            db.system.execute('CREATE TABLE editor_secret_probe (rowid BIGINT UNIQUE, id TEXT PRIMARY KEY, password TEXT, note TEXT)')
            db.system.execute("INSERT INTO editor_secret_probe(rowid,id,password,note) VALUES (1,'one','secret-value','old')")
            row = server.db_rows('editor_secret_probe')['rows'][0]
            assert row['password'] == server.HIDDEN_VALUE
            updated = server.db_update('editor-admin', 'editor_secret_probe', row['__rowid__'],
                                       {'password': server.HIDDEN_VALUE, 'note': 'new'})
            assert updated['note'] == 'new'
            assert db.system.execute_one("SELECT password FROM editor_secret_probe WHERE id='one'")[0] == 'secret-value'
            audits = db.system.execute("SELECT payload FROM admin_audit WHERE action='db.update'")
            assert audits and all('secret-value' not in row[0] for row in audits)
        finally:
            db.close()
            DBManager.database_dir = previous
