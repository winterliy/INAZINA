"""A table, column, index or trigger missing from a running PostgreSQL database is added at start-up, and nothing else changes."""
import unittest

import psycopg

from database import pg_backend, pg_schema_sync
from tests import test_market_features as fixtures


@unittest.skipUnless(pg_backend.enabled(), "PostgreSQL-specific sync")
class PostgresSchemaSyncTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def raw(self):
        return psycopg.connect(pg_backend.dsn(), autocommit=True)

    def test_missing_table_column_index_and_trigger_are_added_and_existing_data_is_untouched(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO liquidity_providers(company_code,profile,enabled,halted,initial_capital_minor_units,last_run_epoch,"
                         "created_epoch,updated_epoch) VALUES ('KEEP','low',1,0,5,0,1,1)")
        with self.raw() as conn:
            conn.execute("DROP TABLE fx_revaluation_runs")
            conn.execute("ALTER TABLE fx_residual_clearings DROP COLUMN recipient_user_id")
            conn.execute("DROP INDEX IF EXISTS idx_user_realized_pnl_user")
            conn.execute("DROP TRIGGER IF EXISTS trg_user_realized_pnl_update ON user_realized_pnl")
            preview = pg_schema_sync.ensure_current(dry_run=True, force=True)
        self.assertEqual(preview["tables"], ["fx_revaluation_runs"])
        self.assertIn("fx_residual_clearings.recipient_user_id", preview["columns"])
        self.assertIn("idx_user_realized_pnl_user", preview["indexes"])
        self.assertIn("trg_user_realized_pnl_update", preview["triggers"])
        summary = pg_schema_sync.ensure_current(force=True)
        self.assertEqual(summary["errors"], [], summary["errors"])
        self.assertEqual(summary["tables"], ["fx_revaluation_runs"])
        with self.raw() as conn:
            self.assertIsNotNone(conn.execute("SELECT to_regclass('fx_revaluation_runs')").fetchone()[0])
            self.assertEqual(conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='fx_residual_clearings' "
                                          "AND column_name='recipient_user_id'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM liquidity_providers WHERE company_code='KEEP'").fetchone()[0], 1)
        again = pg_schema_sync.ensure_current(force=True)
        self.assertTrue(all(not items for items in again.values()), again)               # a second run adds nothing

    def test_an_unchanged_code_fingerprint_skips_the_work(self):
        self.assertIsNotNone(pg_schema_sync.ensure_current(force=True))
        pg_schema_sync._done = False
        self.assertIsNone(pg_schema_sync.ensure_current())                                # same fingerprint: nothing generated
