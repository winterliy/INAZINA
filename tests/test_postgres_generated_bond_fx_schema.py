"""The checked-in PostgreSQL files carry the bond FX settlement table exactly as generated."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager, pg_backend
from database.db_manager import DBManager

ROOT = Path(__file__).resolve().parents[1]
TABLE = "treasury_bond_cash_fx_settlements"


def _mentions(text):
    return [line for line in text.splitlines() if TABLE in line]


def _function_bodies(text):
    """Every generated trigger function for the table, header through END."""
    lines, bodies, index = text.splitlines(), [], 0
    while index < len(lines):
        if lines[index].startswith(f'CREATE OR REPLACE FUNCTION "trg_{TABLE}'):
            end = next(i for i in range(index, len(lines)) if lines[i].startswith("END $$;"))
            bodies.append("\n".join(lines[index:end + 1]))
            index = end
        index += 1
    return bodies


@unittest.skipIf(pg_backend.enabled(), "generates from a SQLite database file")
class PostgresGeneratedBondFxSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old_root = DBManager.database_dir
        cls.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(cls.tmp.name)
        try:
            DatabaseManager().close()
        finally:
            DBManager.database_dir = old_root
        source = str(Path(cls.tmp.name) / "inazina.db")
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import build_postgres_schema
            from build_postgres_triggers import translate
            build_postgres_schema.CODE_TYPES.update(build_postgres_schema.code_column_types())
            cls.schema, _ = build_postgres_schema.build(source)
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as conn:
                cls.triggers, _ = translate(conn)
        finally:
            sys.path.remove(str(ROOT / "tools"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_table_and_foreign_key_match_the_generator(self):
        generated = _mentions(self.schema)
        self.assertEqual(len(generated), 2, generated)  # CREATE TABLE and its foreign key
        self.assertEqual(_mentions((ROOT / "database/postgres/schema.sql").read_text()), generated)

    def test_append_only_triggers_match_the_generator(self):
        checked_in = (ROOT / "database/postgres/triggers.sql").read_text()
        generated = _mentions(self.triggers)
        self.assertEqual(sum("CREATE TRIGGER" in line for line in generated), 3, generated)
        self.assertEqual(_mentions(checked_in), generated)
        self.assertEqual(_function_bodies(checked_in), _function_bodies(self.triggers))
        self.assertEqual(len(_function_bodies(self.triggers)), 1)


if __name__ == "__main__":
    unittest.main()
