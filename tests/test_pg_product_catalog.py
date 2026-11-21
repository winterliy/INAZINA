"""Exercise the product catalog against the real PostgreSQL schema."""
import tempfile
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.admin_commands_manager import CREATE_ARGS
from web_api import server


def test_product_catalog_covers_creatable_kinds():
    previous = DBManager.database_dir
    with tempfile.TemporaryDirectory() as root:
        DBManager.database_dir = Path(root)
        db = DatabaseManager()
        try:
            kinds = {item['kind']: item for item in server.product_catalog()['kinds']}
            assert set(kinds) == set(CREATE_ARGS) - {'currencyproduct'}
            assert {kinds[name]['group'] for name in ('saving', 'loan', 'currency')} == {'finance'}
            saving_fields = {field['key'] for field in kinds['saving']['fields']}
            assert {'account_kind', 'kind_terms', 'transaction_policy'} <= saving_fields
            assert 'status' not in {field['key'] for field in kinds['saving']['advanced']}
        finally:
            db.close()
            DBManager.database_dir = previous
