"""M6: applying FTP to product offer rates is off by default, guarded, append-only and reversible; existing contracts keep their rate."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.alm_manager import ALMManager

NOW = 1_800_000_000


class FtpApplyTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO financial_institutions(issuer,nationality,equity_capital,retained_earnings,deposit_insurance_limit) "
            "VALUES ('Bank','낙원',1000,0,300)")
        for code, days in (("S2", 0), ("S1", 30)):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency) "
                "VALUES (?,?,2,0,?,86400,0,'Bank','NWX')", (code, code, days))
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('L1','loan',6,10000,730,86400,0,'Bank','NWX')")
        self.db.currency.execute("DELETE FROM market_rate_curve WHERE nationality='낙원' AND benchmark_key='funding'")
        for tenor, rate in ((365, 2.0), (730, 3.0)):
            self.db.currency.execute("INSERT INTO market_rate_curve VALUES ('낙원','funding',?,?,0.75,0)", (tenor, rate))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def enable(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "ftp_apply_enabled", 1, actor="admin", now=NOW)

    def rate(self, table, code):
        return self.db.products.execute(f"SELECT interest_rate FROM {table} WHERE product_code=?", (code,))[0][0]

    def apply(self, kind, code, margin, reason="test"):
        with self.db.atomic() as conn:
            return ALMManager.apply_ftp_rate(conn, "Bank", kind, code, margin, actor="admin", reason=reason, now=NOW)

    def refused(self, message, *args, **kwargs):
        with self.assertRaisesRegex(ValueError, message):
            self.apply(*args, **kwargs)

    def test_it_is_off_by_default_and_changes_nothing(self):
        self.refused("ftp_apply_disabled", "loan", "L1", 150)
        self.assertEqual(self.rate("liability", "L1"), 6)

    def test_loan_rate_becomes_ftp_plus_margin_for_new_contracts_and_is_recorded(self):
        self.enable()
        result = self.apply("loan", "L1", 150)                         # FTP 3.30% + 1.50%
        self.assertEqual((result["oldRateBps"], result["newRateBps"], result["ftpBps"]), (600, 480, 330))
        self.assertAlmostEqual(self.rate("liability", "L1"), 4.8, places=9)
        with self.db.readonly() as conn:
            report = ALMManager.report(conn, "Bank", now=NOW)
        self.assertTrue(report["ftp"]["applyEnabled"])
        self.assertEqual([(a["action"], a["old_rate_bps"], a["new_rate_bps"], a["actor"], a["reverted"]) for a in report["ftp"]["applications"]],
                         [("apply", 600, 480, "admin", False)])

    def test_deposit_rate_becomes_ftp_minus_margin(self):
        self.enable()
        self.assertEqual(self.apply("deposit", "S2", 100)["newRateBps"], 230)     # 3.30% - 1.00%
        self.refused("ftp_rate_unchanged", "deposit", "S1", 30)                    # 2.30% - 0.30% = 2.00% = today's rate

    def test_guards_refuse_without_writing(self):
        self.enable()
        for message, args in (("ftp_rate_above_legal_maximum", ("loan", "L1", 5000)), ("invalid_ftp_margin", ("loan", "L1", -1)),
                              ("invalid_ftp_margin", ("loan", "L1", True)), ("invalid_ftp_apply_request", ("loan", "L1", 100, "  ")),
                              ("ftp_product_not_found", ("loan", "NOPE", 100)), ("invalid_ftp_product_kind", ("bond", "L1", 100)),
                              ("ftp_rate_out_of_range", ("deposit", "S2", 1000))):       # 3.30% - 10% is below zero
            with self.subTest(message=message, args=args):
                self.refused(message, *args)
        self.assertEqual(self.rate("liability", "L1"), 6)
        self.assertEqual(self.db.currency.execute("SELECT COUNT(*) FROM ftp_rate_applications")[0][0], 0)

    def test_a_contract_without_a_rate_snapshot_blocks_the_change(self):
        self.enable()
        self.db.users.create_user("u1", "u1", "낙원", 0)
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,account_type,quote_currency,balance_units,"
            "created_at,resolution_status) VALUES ('A1','u1','S2',10,10,'saving','NWX',0,datetime(?,'unixepoch'),'active')", (NOW,))
        self.refused("existing_contracts_without_rate_snapshot", "deposit", "S2", 100)
        self.db.accounts.execute("UPDATE accounts SET contract_rate=2 WHERE account_number='A1'")
        self.assertEqual(self.apply("deposit", "S2", 100)["newRateBps"], 230)
        self.assertEqual(self.db.accounts.execute("SELECT contract_rate FROM accounts WHERE account_number='A1'")[0][0], 2)   # untouched

    def test_floating_rate_products_are_refused(self):
        self.enable()
        self.db.products.execute("UPDATE liability SET rate_type='floating',rate_nationality='낙원',rate_floor=0,benchmark_key='policy',repricing_cycle_seconds=86400 WHERE product_code='L1'")
        self.refused("ftp_apply_requires_fixed_rate_product", "loan", "L1", 150)

    def test_revert_restores_the_old_rate_once_and_never_deletes(self):
        self.enable()
        app_id = self.apply("loan", "L1", 150)["applicationId"]
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "ftp_apply_enabled", 0, actor="admin", now=NOW)   # a revert works while off
            result = ALMManager.revert_ftp_rate(conn, "Bank", app_id, actor="admin2", reason="undo", now=NOW + 1)
            self.assertEqual(result["restoredRateBps"], 600)
            for call, message in ((lambda: ALMManager.revert_ftp_rate(conn, "Bank", app_id, actor="a", reason="again", now=NOW), "already_reverted"),
                                  (lambda: ALMManager.revert_ftp_rate(conn, "Bank", 999, actor="a", reason="x", now=NOW), "ftp_application_not_found"),
                                  (lambda: ALMManager.revert_ftp_rate(conn, "Other", app_id, actor="a", reason="x", now=NOW), "ftp_application_not_found")):
                conn.execute("SAVEPOINT t")
                with self.assertRaisesRegex(ValueError, message):
                    call()
                conn.execute("ROLLBACK TO SAVEPOINT t")
        self.assertEqual(self.rate("liability", "L1"), 6)
        rows = self.db.currency.execute("SELECT action,reverts_app_id,actor FROM ftp_rate_applications ORDER BY app_id")
        self.assertEqual([tuple(r) for r in rows], [("apply", None, "admin"), ("revert", app_id, "admin2")])

    def test_revert_is_refused_when_the_rate_was_changed_afterwards(self):
        self.enable()
        app_id = self.apply("loan", "L1", 150)["applicationId"]
        self.db.products.execute("UPDATE liability SET interest_rate=7 WHERE product_code='L1'")
        with self.db.atomic() as conn, self.assertRaisesRegex(ValueError, "product_rate_changed_since_apply"):
            ALMManager.revert_ftp_rate(conn, "Bank", app_id, actor="a", reason="x", now=NOW)

    def test_history_cannot_be_edited_or_deleted(self):
        self.enable()
        self.apply("loan", "L1", 150)
        for statement in ("UPDATE ftp_rate_applications SET new_rate_bps=1", "DELETE FROM ftp_rate_applications"):
            with self.subTest(statement=statement), self.assertRaisesRegex(Exception, "append-only"):
                self.db.currency.execute(statement)


if __name__ == "__main__":
    unittest.main()
