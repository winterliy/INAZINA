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
    if _template is not None and (not root.exists() or not any(root.iterdir())):
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_template, root / "inazina.db")
    _original_init(self)


def pytest_configure(config):
    global _template
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
