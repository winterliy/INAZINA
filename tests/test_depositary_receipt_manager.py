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
        instruction = self.db.assets.execute_one(
            "SELECT status,requested_epoch,updated_epoch,settled_epoch "
            "FROM dr_custody_instructions WHERE reference_key='request-issue'"
        )
        self.assertEqual(instruction["status"], "settled")
        self.assertIsNotNone(instruction["settled_epoch"])
        self.assertEqual([row[0] for row in self.db.assets.execute(
            "SELECT event_type FROM dr_custody_instruction_events "
            "WHERE reference_key='request-issue' ORDER BY event_sequence"
        )], ["requested", "verified", "settled"])
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
            with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid DR custody instruction transition"):
                conn.execute(
                    "UPDATE assets.dr_custody_instructions SET status='requested' "
                    "WHERE reference_key='request-issue'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "retained for audit"):
                conn.execute(
                    "DELETE FROM assets.dr_custody_instructions WHERE reference_key='request-issue'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "DELETE FROM assets.dr_custody_instruction_events "
                    "WHERE reference_key='request-issue' AND event_sequence=1"
                )
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

    def test_delayed_conversion_reserves_input_until_cancel_or_settlement(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=60 WHERE code='UNDER_DR'"
        )
        now = 1_800_000_000
        accepted, request = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "delayed-issue", now=now,
        )
        self.assertTrue(accepted, request)
        self.assertEqual(request["status"], "requested")
        self.assertEqual(request["settleAfterEpoch"], now+60)
        self.assertEqual(self.holding("UNDER"), 6)
        repeated, same = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "delayed-issue", now=now+1,
        )
        self.assertTrue(repeated, same)
        self.assertEqual(same["status"], "requested")
        from managers.order_book_manager import OrderBookManager
        from managers.user_manager import UserManager
        direct_sale, sale_message = UserManager(self.db).sell_asset("citizen", "UNDER", 3)
        self.assertFalse(direct_sale)
        self.assertIn("보유 수량이 부족", sale_message)
        queued_sale, sale_message = OrderBookManager(self.db, UserManager(self.db)).place_limit_order(
            "citizen", "UNDER", "sell", 3, 50,
        )
        self.assertFalse(queued_sale)
        self.assertIn("매도 가능 수량이 부족", sale_message)
        bypass, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 1, "bypass-delay", now=now+2,
        )
        self.assertFalse(bypass)
        self.assertIn("대기기간", message)
        competing, message = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "second-delayed-issue", now=now+2,
        )
        self.assertFalse(competing)
        self.assertIn("부족", message)
        early, message = self.manager.settle_conversion("delayed-issue", now=now+59)
        self.assertFalse(early)
        self.assertIn("대기시간", message)
        settled, message = self.manager.settle_conversion("delayed-issue", now=now+60)
        self.assertTrue(settled, message)
        self.assertEqual(self.holding("UNDER"), 2)
        self.assertEqual(self.holding("UNDER_DR"), 2)
        status = self.db.assets.execute_one(
            "SELECT status FROM dr_custody_instructions WHERE reference_key='delayed-issue'"
        )[0]
        self.assertEqual(status, "settled")
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COALESCE(SUM(reserved_quantity),0) FROM dr_custody_reservations "
            "WHERE reference_key='delayed-issue' AND released_epoch IS NULL"
        )[0], 0)

    def test_delayed_conversion_cancellation_releases_reservation_and_is_audited(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=120 WHERE code='UNDER_DR'"
        )
        accepted, request = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "cancel-issue", now=1_800_000_000,
        )
        self.assertTrue(accepted, request)
        wrong_owner, message = self.manager.cancel_conversion(
            "other", "cancel-issue", now=1_800_000_010,
        )
        self.assertFalse(wrong_owner)
        self.assertIn("본인의", message)
        cancelled, result = self.manager.cancel_conversion(
            "citizen", "cancel-issue", now=1_800_000_010,
        )
        self.assertTrue(cancelled, result)
        self.assertEqual(self.holding("UNDER"), 6)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT status FROM dr_custody_instructions WHERE reference_key='cancel-issue'"
        )[0], "cancelled")
        self.assertEqual([row[0] for row in self.db.assets.execute(
            "SELECT event_type FROM dr_custody_instruction_events "
            "WHERE reference_key='cancel-issue' ORDER BY event_sequence"
        )], ["requested", "cancelled"])

    def test_due_conversion_worker_settles_only_due_requests(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=60 WHERE code='UNDER_DR'"
        )
        now = 1_800_000_000
        accepted, _ = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "worker-due", now=now,
        )
        self.assertTrue(accepted)
        before_due = self.manager.process_due_conversions(now=now+59)
        self.assertEqual(before_due, {"settled": [], "failed": [], "expired": []})
        due = self.manager.process_due_conversions(now=now+60)
        self.assertEqual(due, {"settled": ["worker-due"], "failed": [], "expired": []})
        self.assertEqual(self.holding("UNDER_DR"), 2)
        self.assertEqual(self.holding("UNDER"), 2)

    def test_failed_settlement_attempt_is_audited_and_keeps_reservation(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=60 WHERE code='UNDER_DR'"
        )
        now = 1_800_000_000
        accepted, _ = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "worker-failure", now=now,
        )
        self.assertTrue(accepted)
        self.db.companies.execute(
            "UPDATE company SET status='inactive' WHERE company_code='UNDER'"
        )
        result = self.manager.process_due_conversions(now=now+60)
        self.assertEqual(result["settled"], [])
        self.assertEqual([item["reference_key"] for item in result["failed"]], ["worker-failure"])
        attempt = self.db.assets.execute_one(
            "SELECT outcome,detail FROM dr_custody_settlement_attempts "
            "WHERE reference_key='worker-failure'"
        )
        self.assertEqual(attempt["outcome"], "failed")
        self.assertTrue(attempt["detail"])
        self.assertEqual(self.db.assets.execute_one(
            "SELECT status FROM dr_custody_instructions WHERE reference_key='worker-failure'"
        )[0], "requested")
        self.assertIsNone(self.db.assets.execute_one(
            "SELECT released_epoch FROM dr_custody_reservations WHERE reference_key='worker-failure'"
        )[0])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.db.assets.execute(
                "DELETE FROM dr_custody_settlement_attempts WHERE reference_key='worker-failure'"
            )
        listed = self.manager.list_custody_instructions()[0]
        self.assertEqual(listed["settlement_attempt_count"], 1)
        self.assertEqual(listed["last_settlement_outcome"], "failed")

    def test_automatic_settlement_stops_after_retry_limit_but_keeps_request_reserved(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=60 WHERE code='UNDER_DR'"
        )
        now = 1_800_000_000
        accepted, _ = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "retry-limit", now=now,
        )
        self.assertTrue(accepted)
        self.db.companies.execute(
            "UPDATE company SET status='inactive' WHERE company_code='UNDER'"
        )
        for _ in range(self.manager.AUTO_SETTLEMENT_MAX_ATTEMPTS):
            processed = self.manager.process_due_conversions(now=now+60)
            self.assertEqual(len(processed["failed"]), 1)
        stopped = self.manager.process_due_conversions(now=now+60)
        self.assertEqual(stopped, {"settled": [], "failed": [], "expired": []})
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_custody_settlement_attempts WHERE reference_key='retry-limit'"
        )[0], self.manager.AUTO_SETTLEMENT_MAX_ATTEMPTS)
        self.assertIsNone(self.db.assets.execute_one(
            "SELECT released_epoch FROM dr_custody_reservations WHERE reference_key='retry-limit'"
        )[0])
        self.db.companies.execute(
            "UPDATE company SET status='active' WHERE company_code='UNDER'"
        )
        manual_retry, _ = self.manager.settle_conversion("retry-limit", now=now+61)
        self.assertTrue(manual_retry)

    def test_admin_rejection_uses_reason_code_and_releases_reserved_units(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=120 WHERE code='UNDER_DR'"
        )
        now = 1_800_000_000
        accepted, _ = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "reject-request", now=now,
        )
        self.assertTrue(accepted)
        invalid, _ = self.manager.reject_conversion(
            "admin", "reject-request", "freeform-text", now=now+1,
        )
        self.assertFalse(invalid)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT status FROM dr_custody_instructions WHERE reference_key='reject-request'"
        )[0], "requested")
        rejected, result = self.manager.reject_conversion(
            "admin", "reject-request", "compliance_review", now=now+2,
        )
        self.assertTrue(rejected, result)
        self.assertEqual(result["reasonCode"], "compliance_review")
        self.assertEqual(self.db.assets.execute_one(
            "SELECT status FROM dr_custody_instructions WHERE reference_key='reject-request'"
        )[0], "rejected")
        self.assertEqual(self.db.assets.execute_one(
            "SELECT released_epoch FROM dr_custody_reservations WHERE reference_key='reject-request'"
        )[0], now+2)
        event = self.db.assets.execute_one(
            "SELECT event_type,detail_code FROM dr_custody_instruction_events "
            "WHERE reference_key='reject-request' ORDER BY event_sequence DESC LIMIT 1"
        )
        self.assertEqual(tuple(event), ("rejected", "compliance_review"))

    def test_custody_reconciliation_is_read_only_and_detects_holder_mismatch(self):
        self.create_program()
        issued, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 2, "reconcile-issue",
        )
        self.assertTrue(issued, message)
        clean = self.manager.reconcile_custody_ledger()
        self.assertEqual(clean["discrepancyCount"], 0)
        program = clean["programs"][0]
        self.assertEqual(program["netDrQuantity"], 2)
        self.assertEqual(program["holderDrQuantity"], 2)
        self.assertEqual(program["custodyUnderlyingQuantity"], 4)
        self.db.assets.execute(
            "UPDATE user_assets SET quantity=quantity-1 WHERE user_id='citizen' AND asset_code='UNDER_DR'"
        )
        broken = self.manager.reconcile_custody_ledger()
        self.assertEqual(broken["discrepancyCount"], 1)
        self.assertEqual(broken["discrepancies"][0]["code"], "outstanding_dr_mismatch")
        self.assertEqual(self.holding("UNDER_DR"), 1)

    def test_stale_request_expires_after_grace_and_releases_reservation(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=60 WHERE code='UNDER_DR'"
        )
        now = 1_800_000_000
        accepted, _ = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "stale-expiration", now=now,
        )
        self.assertTrue(accepted)
        expires = now+60+self.manager.STALE_REQUEST_GRACE_SECONDS
        self.assertEqual(self.manager.expire_stale_conversions(now=expires-1), [])
        self.assertEqual(self.manager.expire_stale_conversions(now=expires), ["stale-expiration"])
        self.assertEqual(self.db.assets.execute_one(
            "SELECT status FROM dr_custody_instructions WHERE reference_key='stale-expiration'"
        )[0], "rejected")
        self.assertEqual(self.db.assets.execute_one(
            "SELECT released_epoch FROM dr_custody_reservations WHERE reference_key='stale-expiration'"
        )[0], expires)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT detail_code FROM dr_custody_instruction_events "
            "WHERE reference_key='stale-expiration' ORDER BY event_sequence DESC LIMIT 1"
        )[0], "expired_unsettled")
        self.assertEqual(self.manager.expire_stale_conversions(now=expires+1), [])

    def test_configured_conversion_fee_debits_currency_account_and_posts_balanced_ledger(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET issue_fee_bps=100 WHERE code='UNDER_DR'"
        )
        insufficient, _ = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 2, "fee-insufficient",
        )
        self.assertFalse(insufficient)
        self.assertEqual(self.holding("UNDER_DR"), 0)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM depositary_receipt_movements WHERE reference_key='fee-insufficient'"
        )[0], 0)
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,account_type,quote_currency,balance_units) "
            "VALUES ('DR_FEE_ACCOUNT','citizen','DR_FEE_PRODUCT','saving','NWX',0)"
        )
        self.db.accounts.execute(
            "UPDATE accounts SET balance_units=10000000 WHERE account_number='DR_FEE_ACCOUNT'"
        )
        charged, message = self.manager.convert(
            "citizen", "UNDER_DR", "issue", 2, "fee-success",
        )
        self.assertTrue(charged, message)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT balance_units FROM accounts WHERE account_number='DR_FEE_ACCOUNT'"
        )[0], 6_000_000)
        fee = self.db.assets.execute_one(
            "SELECT fee_currency,fee_minor_units,fee_bps,notional_minor_units,revenue_account "
            "FROM dr_custody_fee_ledger WHERE reference_key='fee-success'"
        )
        self.assertEqual(tuple(fee), ("NWX", 4, 100, 400, "custodian_fee_income"))
        entry = self.db.currency.execute_one(
            "SELECT COUNT(*),SUM(l.debit_units),SUM(l.credit_units) "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer='DR_CUSTODY' AND e.reference_key='dr-fee:fee-success'"
        )
        self.assertEqual(entry[0], 2)
        self.assertEqual(entry[1], entry[2])
        fee_report = self.manager.reconcile_custody_ledger()["feeReconciliation"]
        self.assertEqual(fee_report[0]["differenceMinorUnits"], 0)
        self.assertEqual(fee_report[0]["feeMinorUnits"], 4)
        self.db.assets.execute(
            "UPDATE market_assets SET issue_fee_bps=0,redeem_fee_bps=100 WHERE code='UNDER_DR'"
        )
        redeemed, message = self.manager.convert(
            "citizen", "UNDER_DR", "redeem", 1, "fee-redeem",
        )
        self.assertTrue(redeemed, message)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT fee_minor_units FROM dr_custody_fee_ledger WHERE reference_key='fee-redeem'"
        )[0], 2)
        fee_report = self.manager.reconcile_custody_ledger()["feeReconciliation"][0]
        self.assertEqual(fee_report["feeCount"], 2)
        self.assertEqual(fee_report["differenceMinorUnits"], 0)

    def test_delayed_request_locks_fee_terms_at_acceptance(self):
        self.create_program()
        self.db.assets.execute(
            "UPDATE market_assets SET settlement_delay_seconds=60,issue_fee_bps=100 WHERE code='UNDER_DR'"
        )
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,account_type,quote_currency,balance_units) "
            "VALUES ('DR_LOCKED_FEE','citizen','DR_FEE_PRODUCT','saving','NWX',0)"
        )
        self.db.accounts.execute(
            "UPDATE accounts SET balance_units=10000000 WHERE account_number='DR_LOCKED_FEE'"
        )
        now = 1_800_000_000
        accepted, _ = self.manager.request_conversion(
            "citizen", "UNDER_DR", "issue", 2, "locked-fee", now=now,
        )
        self.assertTrue(accepted)
        snapshot = self.db.assets.execute_one(
            "SELECT fee_bps,fee_currency,fee_price FROM dr_custody_instructions "
            "WHERE reference_key='locked-fee'"
        )
        self.assertEqual(tuple(snapshot), (100, "NWX", 200))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid DR custody instruction transition"):
            self.db.assets.execute(
                "UPDATE dr_custody_instructions SET fee_bps=300 WHERE reference_key='locked-fee'"
            )
        self.db.assets.execute(
            "UPDATE market_assets SET issue_fee_bps=500,price=400 WHERE code='UNDER_DR'"
        )
        settled, message = self.manager.settle_conversion("locked-fee", now=now+60)
        self.assertTrue(settled, message)
        fee = self.db.assets.execute_one(
            "SELECT fee_bps,notional_minor_units,fee_minor_units FROM dr_custody_fee_ledger "
            "WHERE reference_key='locked-fee'"
        )
        self.assertEqual(tuple(fee), (100, 400, 4))

    def test_dr_voting_rights_can_be_partially_delegated_and_revoked(self):
        self.create_program()
        issued, message = self.manager.convert("citizen", "UNDER_DR", "issue", 3, "vote-issue")
        self.assertTrue(issued, message)
        self.db.users.create_user("proxy", "Proxy", "낙원", 1_000)
        ok, result = self.manager.delegate_votes(
            "citizen", "UNDER_DR", "proxy", 2, now=1_800_000_000,
        )
        self.assertTrue(ok, result)
        delegation_id = result["delegationId"]
        row = self.db.assets.execute_one(
            "SELECT dr_quantity,status FROM dr_voting_delegations WHERE delegation_id=?",
            (delegation_id,),
        )
        self.assertEqual(tuple(row), (2, "active"))
        overdrawn, message = self.manager.delegate_votes(
            "citizen", "UNDER_DR", "proxy", 2, now=1_800_000_001,
        )
        self.assertFalse(overdrawn)
        self.assertIn("위임 가능한", message)
        redeem, message = self.manager.convert(
            "citizen", "UNDER_DR", "redeem", 2, "delegated-redeem",
        )
        self.assertFalse(redeem)
        self.assertIn("위임된 의결권", message)
        from managers.user_manager import UserManager
        direct_sell, message = UserManager(self.db).sell_asset("citizen", "UNDER_DR", 2)
        self.assertFalse(direct_sell)
        self.assertIn("보유 수량이 부족", message)
        from managers.order_book_manager import OrderBookManager
        queued_sell, message = OrderBookManager(self.db, UserManager(self.db)).place_limit_order(
            "citizen", "UNDER_DR", "sell", 2, 200,
        )
        self.assertFalse(queued_sell)
        self.assertIn("매도 가능 수량이 부족", message)
        unauthorized, _ = self.manager.revoke_vote_delegation(
            "proxy", delegation_id, now=1_800_000_002,
        )
        self.assertFalse(unauthorized)
        revoked, result = self.manager.revoke_vote_delegation(
            "citizen", delegation_id, now=1_800_000_003,
        )
        self.assertTrue(revoked, result)
        row = self.db.assets.execute_one(
            "SELECT status,revoked_epoch FROM dr_voting_delegations WHERE delegation_id=?",
            (delegation_id,),
        )
        self.assertEqual(tuple(row), ("revoked", 1_800_000_003))
        events = self.db.assets.execute(
            "SELECT event_type,actor_user_id FROM dr_voting_delegation_events "
            "WHERE delegation_id=? ORDER BY event_id", (delegation_id,),
        )
        self.assertEqual([tuple(event) for event in events], [
            ("delegated", "citizen"), ("revoked", "citizen"),
        ])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit event"):
            self.db.assets.execute(
                "UPDATE dr_voting_delegations SET proxy_user_id='attacker' WHERE delegation_id=?",
                (delegation_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "retained for audit"):
            self.db.assets.execute(
                "DELETE FROM dr_voting_delegations WHERE delegation_id=?",
                (delegation_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.db.assets.execute(
                "DELETE FROM dr_voting_delegation_events WHERE delegation_id=?",
                (delegation_id,),
            )

    def test_voting_record_snapshot_freezes_direct_and_delegated_dr_entitlements(self):
        self.create_program()
        self.db.users.create_user("proxy", "Proxy", "낙원", 1_000)
        self.db.assets.add_user_asset("citizen", "UNDER_DR", 3, unit_cost=45)
        delegated, delegation = self.manager.delegate_votes("citizen", "UNDER_DR", "proxy", 1)
        self.assertTrue(delegated, delegation)

        created, meeting = self.manager.create_voting_record_snapshot(
            "UNDER", "정기 주주총회", now=1_800_000_000,
        )
        self.assertTrue(created, meeting)
        self.assertEqual(meeting["snapshotRows"], 3)
        self.assertEqual(self.manager.get_voting_entitlements(meeting["meetingId"]), [
            {"voterUserId": "citizen", "quantity": 10},
            {"voterUserId": "proxy", "quantity": 2},
        ])
        # Trading and delegation changes must not rewrite the record-date franchise.
        self.db.assets.add_user_asset("citizen", "UNDER_DR", 2, unit_cost=50)
        self.assertTrue(self.manager.revoke_vote_delegation("citizen", delegation["delegationId"])[0])
        self.assertEqual(self.manager.get_voting_entitlements(meeting["meetingId"]), [
            {"voterUserId": "citizen", "quantity": 10},
            {"voterUserId": "proxy", "quantity": 2},
        ])
        self.db.assets.add_user_asset("citizen", "UNDER_DR", 1, unit_cost=45)
        self.assertEqual(self.manager.get_voting_entitlements(meeting["meetingId"]), [
            {"voterUserId": "citizen", "quantity": 10},
            {"voterUserId": "proxy", "quantity": 2},
        ])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db.assets.execute(
                "UPDATE dr_voting_entitlement_snapshots SET quantity=999 WHERE meeting_id=?",
                (meeting["meetingId"],),
            )

    def test_record_date_voters_can_cast_one_weighted_ballot_per_agenda(self):
        self.create_program()
        self.db.users.create_user("proxy", "Proxy", "낙원", 1_000)
        self.db.users.create_user("outsider", "Outsider", "낙원", 1_000)
        self.db.assets.add_user_asset("citizen", "UNDER_DR", 3, unit_cost=45)
        self.assertTrue(self.manager.delegate_votes("citizen", "UNDER_DR", "proxy", 1)[0])
        created, meeting = self.manager.create_voting_record_snapshot(
            "UNDER", "정기 주주총회", now=1_800_000_000,
        )
        self.assertTrue(created, meeting)
        window_ok, window = self.manager.set_voting_window(
            meeting["meetingId"], 1_800_000_010, 1_800_000_100, 100,
            now=1_800_000_001,
        )
        self.assertTrue(window_ok, window)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db.assets.execute(
                "UPDATE dr_voting_meetings SET voting_close_epoch=? WHERE meeting_id=?",
                (1_800_000_200, meeting["meetingId"]),
            )
        agenda_ok, agenda = self.manager.add_voting_agenda_item(
            meeting["meetingId"], "배당안", ["찬성", "반대", "기권"], now=1_800_000_001,
        )
        self.assertTrue(agenda_ok, agenda)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db.assets.execute(
                "UPDATE dr_voting_agenda_items SET title='바뀐 안건' WHERE agenda_item_id=?",
                (agenda["agendaItemId"],),
            )
        early, message = self.manager.cast_vote(
            "citizen", meeting["meetingId"], agenda["agendaItemId"], "찬성", now=1_800_000_009,
        )
        self.assertFalse(early)
        self.assertIn("시작 전", message)
        yes, yes_result = self.manager.cast_vote(
            "citizen", meeting["meetingId"], agenda["agendaItemId"], "찬성", now=1_800_000_020,
        )
        no, no_result = self.manager.cast_vote(
            "proxy", meeting["meetingId"], agenda["agendaItemId"], "반대", now=1_800_000_030,
        )
        self.assertTrue(yes, yes_result)
        self.assertTrue(no, no_result)
        citizen_agenda = self.manager.list_user_voting_agenda(
            "citizen", now=1_800_000_031,
        )
        self.assertEqual(len(citizen_agenda), 1)
        self.assertEqual(citizen_agenda[0]["myChoice"], "찬성")
        self.assertEqual(citizen_agenda[0]["votingPower"], 10)
        self.assertEqual(citizen_agenda[0]["state"], "open")
        duplicate, message = self.manager.cast_vote(
            "citizen", meeting["meetingId"], agenda["agendaItemId"], "반대", now=1_800_000_040,
        )
        self.assertFalse(duplicate)
        self.assertIn("이미 투표", message)
        outsider, message = self.manager.cast_vote(
            "outsider", meeting["meetingId"], agenda["agendaItemId"], "찬성", now=1_800_000_050,
        )
        self.assertFalse(outsider)
        self.assertIn("의결권이 없는", message)
        results = self.manager.get_voting_results(meeting["meetingId"], now=1_800_000_101)
        self.assertEqual(results, [{
            "agendaItemId": agenda["agendaItemId"], "title": "배당안",
            "choices": {
                "찬성": {"votingPower": 10, "ballots": 1},
                "반대": {"votingPower": 2, "ballots": 1},
                "기권": {"votingPower": 0, "ballots": 0},
            },
            "eligibleVotingPower": 12, "turnoutVotingPower": 12,
            "turnoutPercent": 100, "votingOpensEpoch": 1_800_000_010,
            "votingClosesEpoch": 1_800_000_100, "closed": True,
            "quorumPercent": 100, "quorumMet": True,
        }])
        closed, message = self.manager.cast_vote(
            "citizen", meeting["meetingId"], agenda["agendaItemId"], "기권", now=1_800_000_100,
        )
        self.assertFalse(closed)
        self.assertIn("종료", message)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db.assets.execute(
                "DELETE FROM dr_voting_ballots WHERE agenda_item_id=?", (agenda["agendaItemId"],),
            )

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
            "SELECT COUNT(*) FROM dr_custody_instructions "
            "WHERE reference_key='insufficient-custody'"
        )[0], 0)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_custody_instruction_events "
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
            "holder_underlying_average_cost,custody_underlying_currency,"
            "underlying_average_cost_minor_units,holder_underlying_average_cost_minor_units,"
            "custody_underlying_average_cost_minor_units,underlying_average_cost_decimal_places "
            "FROM depositary_receipt_movements WHERE movement_type='redeem'"
        )
        self.assertEqual(tuple(redemption), (40.25, 40.25, 50.75, 'USD', 4025, 5075, 4025, 2))

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

    def test_dr_movement_minor_units_backfill_legacy_costs(self):
        self.create_program()
        ok, message = self.manager.convert("citizen", "UNDER_DR", "issue", 1, "legacy-cost")
        self.assertTrue(ok, message)
        self.db.assets.execute("DROP TRIGGER trg_dr_movements_immutable_update")
        self.db.assets.execute(
            "UPDATE depositary_receipt_movements SET underlying_average_cost_minor_units=NULL,"
            "underlying_average_cost_decimal_places=NULL,underlying_average_cost_currency=NULL,"
            "holder_underlying_average_cost_minor_units=NULL,"
            "holder_underlying_average_cost_decimal_places=NULL,holder_underlying_currency=NULL,"
            "custody_underlying_average_cost_minor_units=NULL,"
            "custody_underlying_average_cost_decimal_places=NULL,custody_underlying_currency=NULL "
            "WHERE reference_key='legacy-cost'"
        )
        self.db.assets.execute("""CREATE TRIGGER trg_dr_movements_immutable_update
            BEFORE UPDATE ON depositary_receipt_movements
            BEGIN SELECT RAISE(ABORT,'DR custody movements are append-only'); END""")
        self.db.currency._ensure_dr_movement_cost_units()
        row = self.db.assets.execute_one(
            "SELECT underlying_average_cost_minor_units,holder_underlying_average_cost_minor_units,"
            "custody_underlying_average_cost_minor_units,underlying_average_cost_currency,"
            "holder_underlying_currency,custody_underlying_currency "
            "FROM depositary_receipt_movements WHERE reference_key='legacy-cost'"
        )
        self.assertEqual(tuple(row), (40, 40, 40, 'NWX', 'NWX', 'NWX'))


if __name__ == "__main__":
    unittest.main()
