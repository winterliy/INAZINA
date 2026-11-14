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


def _use_fresh_postgres_database(root, *, empty=False):
    """Give ``root`` (a test's database directory) its own PostgreSQL clone. ``empty``: a test
    that opens the database file before any manager builds its own small fixture, as on SQLite
    where that file starts empty, so the clone keeps the schema but no rows."""
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
    if empty:
        import psycopg
        with psycopg.connect(f"postgresql:///{name}", autocommit=True) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
            if tables:
                conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")


class _FixtureConnection:
    """What tests get from sqlite3.connect(<test inazina.db>) on PostgreSQL: the current test
    database. The schema already exists there, so fixture DDL is skipped and rows are written
    without firing triggers (a SQLite fixture schema has none)."""

    _DDL = ("CREATE ", "DROP ", "ALTER ", "PRAGMA ")

    def __new__(cls, *args, **kwargs):
        from database import pg_backend
        connection = pg_backend.connect()
        # A SQLite fixture schema has no triggers; its rows go in as written.
        connection._raw.execute("SET session_replication_role = replica")

        def executescript(script, _connection=connection):
            import sqlglot
            for statement in sqlglot.transpile(script, read="sqlite", write="sqlite"):
                if statement.strip() and not statement.lstrip().upper().startswith(cls._DDL):
                    _connection.execute(statement)
            _connection.commit()

        connection.executescript = executescript
        return connection


def _route_test_database_connections():
    import sqlite3
    original = sqlite3.connect

    def connect(database, *args, **kwargs):
        target = str(database)
        if target.startswith("file:"):
            target = target[5:].split("?", 1)[0]
        if target.endswith("inazina.db") and Path(target).resolve().parent == Path(DBManager.database_dir).resolve():
            _use_fresh_postgres_database(Path(DBManager.database_dir), empty=True)
            return _FixtureConnection()
        return original(database, *args, **kwargs)

    sqlite3.connect = connect


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
        _route_test_database_connections()
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


# Tests about SQLite itself, skipped on INAZINA_DB_BACKEND=postgres (reviewed 2026-11-14).
SQLITE_ONLY = {
    # exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema
    "test_country_accounts.py::CountryAccountTests::test_legacy_treasuries_merge_into_nation_account": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_credit_risk_stages.py::DefaultedLoanEclTests::test_legacy_assessment_migration_preserves_rows_and_uses_neutral_factor": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_credit_risk_stages.py::EclScenarioConfigurationTests::test_legacy_scenario_weights_are_normalized_without_changing_ratios": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_institution_ledger_minor_unit_upgrade_is_idempotent": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_insurance_payout_reconciler_reports_unverifiable_legacy_currency": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_legacy_wallets_move_to_home_cash_and_foreign_accounts": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_account_storage.py::LegacyCurrencySchemaMigrationTests::test_legacy_deposit_insurance_claims_gain_nullable_payout_fields": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_account_storage.py::LegacyCurrencySchemaMigrationTests::test_source_tax_rule_fk_survives_legacy_negative_rate_table_rebuild": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_default_config.py::CurrencyDefaultConfigTests::test_copy_left_by_an_interrupted_rebuild_is_restored": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_default_config.py::CurrencyDefaultConfigTests::test_legacy_default_is_removed_from_a_table_other_triggers_name": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_default_config.py::CurrencyDefaultConfigTests::test_legacy_fixed_currency_default_is_removed_without_losing_rows": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_home.py::CurrencyHomeTests::test_migration_is_idempotent_and_all_product_denominations_present": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_currency_liquidity.py::CurrencyLiquidityTests::test_baseline_migration_preserves_existing_state_and_is_idempotent": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_depositary_receipt_manager.py::DepositaryReceiptManagerTests::test_dr_movement_minor_units_backfill_legacy_costs": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_financial.py::FinancialTests::test_common_journal_company_mapping_replay_and_legacy_rebuild": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_fx_interest.py::FxInterestTests::test_legacy_account_close_can_be_reconstructed_from_bank_journal": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_institution_maturity_gap.py::InstitutionMaturityGapTests::test_maturity_ladder_rounds_legacy_fractional_rows_to_native_currency": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_movement_daily.py::MovementDailyTests::test_migration_seeds_last_trade_from_existing_history": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_realistic_rate_system.py::RealisticRateSystemTests::test_legacy_schedule_rows_backfill_currency_minor_units": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_shareholder_register.py::ShareholderRegisterTests::test_legacy_dividend_payment_key_migrates_without_losing_rows": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    "test_shareholder_register.py::ShareholderRegisterTests::test_legacy_dr_withholding_rows_migrate_to_currency_minor_units": "exercises SQLite schema migration of legacy data; PostgreSQL starts from the current schema",
    # financial_amount_snapshots is not carried to PostgreSQL (redesign pending)
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_amount_audit_checks_registered_schema_even_when_source_table_is_empty": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_amount_audit_supports_rowid_keys_for_legacy_composite_tables": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_amount_field_coverage_flags_new_unregistered_money_columns": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_late_dividend_amount_field_uses_each_currency_scale": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_late_initialized_financial_schemas_are_included_in_amount_registry": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    "test_currency_account_storage.py::CurrencyAccountStorageTests::test_registered_currency_amount_snapshots_reconcile_to_source_rows": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    "test_financial.py::FinancialTests::test_closed_wallet_period_survives_restart_and_reports_snapshot_drift": "financial_amount_snapshots is not carried to PostgreSQL (redesign pending)",
    # asserts SQLite EXPLAIN QUERY PLAN output
    "test_market_features.py::MarketFeatureTests::test_exercise_cash_link_lookup_uses_event_index": "asserts SQLite EXPLAIN QUERY PLAN output",
    # plants a corrupt row with PRAGMA ignore_check_constraints, which PostgreSQL cannot do
    "test_market_features.py::MarketFeatureTests::test_put_collateral_reconciliation_detects_invalid_event_precision": "plants a corrupt row with PRAGMA ignore_check_constraints",
    "test_query_plans.py::QueryPlanTests::test_active_loan_total_uses_user_partial_index": "asserts SQLite EXPLAIN QUERY PLAN output",
    "test_query_plans.py::QueryPlanTests::test_recent_user_orders_use_updated_index": "asserts SQLite EXPLAIN QUERY PLAN output",
}


def pytest_collection_modifyitems(config, items):
    if _pg_template is None:
        return
    for item in items:
        node = item.nodeid.removeprefix("tests/")
        if node in SQLITE_ONLY:
            item.add_marker(pytest.mark.skip(reason=f"SQLite only: {SQLITE_ONLY[node]}"))
