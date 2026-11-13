"""Shared pytest setup.

Almost every test builds a fresh database in an empty temporary directory, and
building the schema from nothing runs ~1,400 statements (~0.3s). Each pytest
process builds it once as a template; later empty directories start from a copy
and DatabaseManager initialization still runs on top of it, so schema upgrades
and seeds keep executing exactly as in production. Directories that already
hold files (legacy migration fixtures) are left alone.
"""
import atexit
import shutil
import tempfile
from pathlib import Path

import pytest
from _pytest._io.saferepr import saferepr
from _pytest.subtests import SubtestReport

import database
from database.db_manager import DBManager

_original_init = database.DatabaseManager.__init__
_template = None


def _build_template():
    root = Path(tempfile.mkdtemp(prefix="inazina-test-template-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    previous = DBManager.database_dir
    DBManager.database_dir = root
    try:
        db = database.DatabaseManager.__new__(database.DatabaseManager)
        _original_init(db)
        db.close()  # the last close checkpoints WAL into inazina.db
    finally:
        DBManager.database_dir = previous
    return root / "inazina.db"


def _init_from_template(self):
    root = Path(DBManager.database_dir)
    if _pg_template is not None:
        _use_fresh_postgres_database(root)
    elif _template is not None and (not root.exists() or not any(root.iterdir())):
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_template, root / "inazina.db")
    _original_init(self)


# INAZINA_DB_BACKEND=postgres: the same tests on PostgreSQL. Each worker converts its
# SQLite template once into a PostgreSQL template database; every test directory gets
# a fresh clone (CREATE DATABASE ... TEMPLATE), and the previous clone is dropped.
_pg_template = None
_pg_current = {}


def _pg_admin():
    import psycopg
    return psycopg.connect("postgresql:///postgres", autocommit=True)


def _build_postgres_template(sqlite_template):
    import os
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from sqlite_to_postgres import load
    name = f"inazina_test_tpl_{os.getpid()}"
    with _pg_admin() as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    load(str(sqlite_template), f"postgresql:///{name}")
    atexit.register(_drop_postgres_databases)
    return name


def _use_fresh_postgres_database(root):
    import hashlib
    import os
    key = str(root.resolve())
    if _pg_current.get("root") == key:
        return  # same test directory: keep its database (several managers per test)
    name = f"inazina_test_{os.getpid()}_{hashlib.sha1(key.encode()).hexdigest()[:10]}"
    with _pg_admin() as admin:
        if _pg_current.get("name"):
            admin.execute(f'DROP DATABASE IF EXISTS "{_pg_current["name"]}" WITH (FORCE)')
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    _pg_current.update(root=key, name=name)
    os.environ["INAZINA_PG_DSN"] = f"postgresql:///{name}"


def _drop_postgres_databases():
    try:
        with _pg_admin() as admin:
            for name in (_pg_current.get("name"), _pg_template):
                if name:
                    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except Exception:
        pass


def pytest_configure(config):
    global _template, _pg_template
    from database import pg_backend
    if pg_backend.enabled():
        # The SQLite template is built with the SQLite backend, then converted.
        import os
        os.environ["INAZINA_DB_BACKEND"] = "sqlite"
        try:
            _template = _build_template()
        finally:
            os.environ["INAZINA_DB_BACKEND"] = "postgres"
        _pg_template = _build_postgres_template(_template)
    else:
        _template = _build_template()
    database.DatabaseManager.__init__ = _init_from_template


@pytest.hookimpl(tryfirst=True)
def pytest_report_to_serializable(config, report):
    # pytest-xdist ships reports between processes and cannot serialize
    # subTest(amount=Decimal(...)) or callables; send their repr instead.
    if isinstance(report, SubtestReport):
        data = report._to_json()
        context = data["_subtest.context"]
        context["kwargs"] = {
            key: value if isinstance(value, (str, int, float, bool, type(None))) else saferepr(value)
            for key, value in context["kwargs"].items()
        }
        return data
    return None
