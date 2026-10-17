"""DR conversion moves ownership between original shares and issued receipts atomically."""
import sqlite3
import unittest

import test_inazina as fixtures
from managers.depositary_receipt_manager import DepositaryReceiptManager


class DepositaryReceiptManagerTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def create_program(self):
        self.db.companies.create_company(
            "UNDER", "Underlying", "test", 100_000, 1_000, 100, "admin", "INZ"
        )
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('UNDER_DR','UNDER','LSE','NWX',2,100,200)"
        )
        self.db.assets.register_asset(
            "UNDER_DR", "Underlying DR", "LSE", 200, "depositary_receipt", 100
        )
        self.db.assets.add_user_asset("citizen", "UNDER", 6, unit_cost=40)
        self.manager = DepositaryReceiptManager(self.db)

    def holding(self, code):
        row = self.db.assets.execute_one(
            "SELECT COALESCE(SUM(quantity),0) FROM user_assets "
            "WHERE user_id='citizen' AND asset_code=?", (code,)
        )
        return row[0]

    def test_issue_and_redeem_preserve_underlying_custody_and_are_idempotent(self):
        self.create_program()
        ok, message = self.manager.convert("citizen", "UNDER_DR", "issue", 3, "request-issue")
        self.assertTrue(ok, message)
        self.assertEqual(self.holding("UNDER"), 0)
        self.assertEqual(self.holding("UNDER_DR"), 3)
        process = self.db.assets.execute(
            "SELECT event_sequence,event_type FROM dr_custody_process_events "
            "WHERE reference_key='request-issue' ORDER BY event_sequence"
        )
        self.assertEqual([tuple(row) for row in process], [
            (1, "instruction_received"),
            (2, "custody_verified"),
            (3, "settled"),
        ])
        owner = self.db.companies.execute_one(
            "SELECT max_shareholder_id FROM company WHERE company_code='UNDER'"
        )
        self.assertEqual(owner["max_shareholder_id"], "citizen")

        duplicate, _ = self.manager.convert("citizen", "UNDER_DR", "issue", 3, "request-issue")
        self.assertTrue(duplicate)
        self.assertEqual(self.holding("UNDER_DR"), 3)
        self.assertEqual(
            self.db.assets.execute_one(
                "SELECT COUNT(*) FROM depositary_receipt_movements"
            )[0],
            1,
        )
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_custody_process_events WHERE reference_key='request-issue'"
        )[0], 3)

        # A secondary-market DR position also counts toward beneficial
        # ownership of the underlying for the majority-holder summary.
        self.db.users.create_user("dr-investor", "DR Investor", "낙원", 1_000)
        self.db.assets.add_user_asset("dr-investor", "UNDER_DR", 4, unit_cost=45)
        with self.db.atomic() as conn:
            from managers.user_manager import UserManager
            UserManager._refresh_max_shareholder(conn, "UNDER_DR")
        owner = self.db.companies.execute_one(
            "SELECT max_shareholder_id FROM company WHERE company_code='UNDER'"
        )
        self.assertEqual(owner["max_shareholder_id"], "dr-investor")

        # Simulate a secondary-market purchase: holder basis differs from the
        # custodian pool's original share basis.
        self.db.assets.execute(
            "UPDATE user_assets SET average_cost=100 WHERE user_id='citizen' AND asset_code='UNDER_DR'"
        )

        redeemed, message = self.manager.convert("citizen", "UNDER_DR", "redeem", 1, "request-redeem")
        self.assertTrue(redeemed, message)
        redeemed_process = self.db.assets.execute(
            "SELECT event_type FROM dr_custody_process_events "
            "WHERE reference_key='request-redeem' ORDER BY event_sequence"
        )
        self.assertEqual([row[0] for row in redeemed_process], [
            "instruction_received", "custody_verified", "settled",
        ])
        self.assertEqual(self.holding("UNDER"), 2)
        self.assertEqual(self.holding("UNDER_DR"), 2)
        custody = self.db.assets.execute_one(
            "SELECT SUM(CASE WHEN movement_type='issue' THEN underlying_quantity "
            "ELSE -underlying_quantity END) FROM depositary_receipt_movements"
        )[0]
        self.assertEqual(custody, 4)
        underlying = self.db.assets.execute_one(
            "SELECT average_cost FROM user_assets WHERE user_id='citizen' AND asset_code='UNDER'"
        )
        self.assertEqual(underlying["average_cost"], 50)
        redeemed_movement = self.db.assets.execute_one(
            "SELECT underlying_average_cost,holder_underlying_average_cost,holder_underlying_currency,"
            "custody_underlying_average_cost,custody_underlying_currency "
            "FROM depositary_receipt_movements WHERE movement_type='redeem'"
        )
        self.assertEqual(tuple(redeemed_movement), (40, 50, 'NWX', 40, 'NWX'))
        with self.db.atomic() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "UPDATE assets.dr_custody_process_events SET event_type='settled' "
                    "WHERE reference_key='request-issue' AND event_sequence=1"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "DELETE FROM assets.dr_custody_process_events "
                    "WHERE reference_key='request-issue' AND event_sequence=1"
                )

    def test_reference_key_cannot_be_reused_for_different_request(self):
        self.create_program()
        self.assertTrue(self.manager.convert("citizen", "UNDER_DR", "issue", 2, "request-a")[0])
        ok, message = self.manager.convert("citizen", "UNDER_DR", "issue", 1, "request-a")
        self.assertFalse(ok)
        self.assertIn("다른 DR 전환", message)
        self.assertEqual(self.holding("UNDER_DR"), 2)
        self.assertEqual(self.holding("UNDER"), 2)

    def test_failed_custody_verification_leaves_no_partial_process_or_movement(self):
        self.create_program()
        ok, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 4, "insufficient-custody"
        )
        self.assertFalse(ok)
        self.assertIn("원주가 부족", message)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_custody_process_events "
            "WHERE reference_key='insufficient-custody'"
        )[0], 0)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM depositary_receipt_movements "
            "WHERE reference_key='insufficient-custody'"
        )[0], 0)
        self.assertEqual(self.holding("UNDER"), 6)
        self.assertEqual(self.holding("UNDER_DR"), 0)

    def test_invalid_effective_epoch_is_rejected_without_custody_movement(self):
        self.create_program()
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                ok, message = self.manager.convert(
                    "citizen", "UNDER_DR", "issue", 1,
                    f"invalid-epoch-{invalid_epoch}", now=invalid_epoch,
                )
                self.assertFalse(ok)
                self.assertIn("유효한 UNIX 시각", message)
        self.assertEqual(self.holding("UNDER"), 6)
        self.assertEqual(self.holding("UNDER_DR"), 0)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM depositary_receipt_movements"
        )[0], 0)
        retried, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 1, "invalid-epoch-nan", now=1_000
        )
        self.assertTrue(retried, message)
        self.assertEqual(self.holding("UNDER_DR"), 1)

    def test_suspended_program_blocks_issuance_but_allows_existing_holders_to_redeem(self):
        self.create_program()
        ok, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 1, "suspended-issue"
        )
        self.assertTrue(ok, message)
        self.db.assets.execute(
            "UPDATE depositary_receipts SET program_status='suspended' WHERE dr_code='UNDER_DR'"
        )
        self.db.assets.execute(
            "UPDATE market_assets SET status='inactive' WHERE code='UNDER_DR'"
        )
        self.db.companies.execute(
            "UPDATE company SET status='inactive' WHERE company_code='UNDER'"
        )
        ok, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 1, "suspended-issue-2"
        )
        self.assertFalse(ok)
        self.assertIn("신규 발행", message)
        ok, message = self.manager.convert(
            "citizen", "UNDER_DR", "redeem", 1, "suspended-redeem"
        )
        self.assertTrue(ok, message)
        self.assertEqual(self.holding("UNDER_DR"), 0)
        self.assertEqual(self.holding("UNDER"), 6)

    def test_dr_custody_and_holder_basis_keep_quote_currency_precision(self):
        self.create_program()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','Dollar','1300',2)"
        )
        self.db.companies.execute("UPDATE company SET quote_currency='USD' WHERE company_code='UNDER'")
        self.db.assets.execute("UPDATE market_assets SET quote_currency='USD' WHERE code='UNDER_DR'")
        self.db.assets.execute(
            "UPDATE user_assets SET average_cost=40.25 WHERE user_id='citizen' AND asset_code='UNDER'"
        )
        ok, message = self.manager.convert("citizen", "UNDER_DR", "issue", 1, "usd-issue")
        self.assertTrue(ok, message)
        self.db.assets.execute(
            "UPDATE user_assets SET average_cost=101.50 WHERE user_id='citizen' AND asset_code='UNDER_DR'"
        )
        ok, message = self.manager.convert("citizen", "UNDER_DR", "redeem", 1, "usd-redeem")
        self.assertTrue(ok, message)
        returned = self.db.assets.execute_one(
            "SELECT average_cost FROM user_assets WHERE user_id='citizen' AND asset_code='UNDER'"
        )
        self.assertEqual(returned["average_cost"], 43.75)
        redemption = self.db.assets.execute_one(
            "SELECT underlying_average_cost,custody_underlying_average_cost,"
            "holder_underlying_average_cost,custody_underlying_currency "
            "FROM depositary_receipt_movements WHERE movement_type='redeem'"
        )
        self.assertEqual(tuple(redemption), (40.25, 40.25, 50.75, 'USD'))

    def test_dr_redemption_translates_holder_basis_from_receipt_currency(self):
        self.create_program()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','Dollar','1300',2)"
        )
        self.db.assets.execute("UPDATE market_assets SET quote_currency='USD' WHERE code='UNDER_DR'")
        ok, message = self.manager.convert("citizen", "UNDER_DR", "issue", 1, "fx-issue")
        self.assertTrue(ok, message)
        self.db.assets.execute(
            "UPDATE user_assets SET average_cost=0.10 WHERE user_id='citizen' AND asset_code='UNDER_DR'"
        )
        ok, message = self.manager.convert("citizen", "UNDER_DR", "redeem", 1, "fx-redeem")
        self.assertTrue(ok, message)
        redemption = self.db.assets.execute_one(
            "SELECT holder_underlying_average_cost,holder_underlying_currency "
            "FROM depositary_receipt_movements WHERE movement_type='redeem'"
        )
        self.assertEqual(tuple(redemption), (65, 'NWX'))


if __name__ == "__main__":
    unittest.main()
