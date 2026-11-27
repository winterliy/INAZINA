import unittest

from functions.recovery_waterfall import allocate_recovery


class RecoveryWaterfallTests(unittest.TestCase):
    def test_priority_and_shortfall_are_conserved(self):
        result = allocate_recovery(100, [("senior", 1, 70), ("junior", 2, 80)])
        self.assertEqual(result.allocations, {"senior": 70, "junior": 30})
        self.assertEqual(result.unallocated, 0)
        self.assertEqual(result.shortfalls, {"senior": 0, "junior": 50})

    def test_equal_priority_is_pro_rata_with_deterministic_remainder(self):
        result = allocate_recovery(5, [("b", 1, 3), ("a", 1, 3)])
        self.assertEqual(result.allocations, {"a": 3, "b": 2})
        self.assertEqual(result.shortfalls, {"a": 0, "b": 1})

    def test_excess_and_zero_claims(self):
        result = allocate_recovery(10, [("zero", 1, 0), ("claim", 2, 4)])
        self.assertEqual(result.allocations, {"zero": 0, "claim": 4})
        self.assertEqual(result.unallocated, 6)

    def test_invalid_input_is_rejected(self):
        for amount, claims in [(-1, []), (True, []), (1, [("a", 0, 1)]),
                               (1, [("a", 1, -1)]), (1, [("a", 1, 1), ("a", 2, 1)]),
                               (1, [("a", 1, 1.5)])]:
            with self.subTest(amount=amount, claims=claims):
                with self.assertRaises(ValueError):
                    allocate_recovery(amount, claims)

    def test_charged_off_preview_uses_actual_loan_balance(self):
        import sqlite3
        from functions.recovery_waterfall import preview_charged_off_loan
        conn = sqlite3.connect(":memory:")
        conn.execute("ATTACH DATABASE ':memory:' AS accounts")
        conn.execute("CREATE TABLE accounts.loans (loan_id INTEGER, status TEXT, quote_currency TEXT, charged_off_balance_minor_units INTEGER)")
        conn.execute("INSERT INTO accounts.loans VALUES (1, 'charged_off', 'USD', 80)")
        result = preview_charged_off_loan(conn, loan_id=1, proceeds=100, bank_rank=2,
                                          other_claims=[('other', 1, 30)])
        self.assertEqual(result['currencyCode'], 'USD')
        self.assertEqual(result['allocations'], {'other': 30, 'bank': 70})
        self.assertEqual(result['shortfalls']['bank'], 10)
        self.assertEqual(conn.execute('SELECT charged_off_balance_minor_units FROM accounts.loans').fetchone()[0], 80)
        conn.close()

    def test_charged_off_preview_rejects_missing_or_ambiguous_loan(self):
        import sqlite3
        from functions.recovery_waterfall import preview_charged_off_loan
        conn = sqlite3.connect(':memory:')
        conn.execute("ATTACH DATABASE ':memory:' AS accounts")
        conn.execute('CREATE TABLE accounts.loans (loan_id INTEGER, status TEXT, quote_currency TEXT, charged_off_balance_minor_units INTEGER)')
        conn.executemany('INSERT INTO accounts.loans VALUES (?, ?, ?, ?)',
                         [(1, 'active', 'USD', 80), (2, 'charged_off', None, 80),
                          (3, 'charged_off', 'USD', None)])
        for loan in [1, 2, 3, 4, True]:
            with self.subTest(loan=loan), self.assertRaises(ValueError):
                preview_charged_off_loan(conn, loan_id=loan, proceeds=100, bank_rank=1, other_claims=[])
        conn.close()


class RecoveryApiTests(unittest.TestCase):
    def request(self, payload, role="admin", audit=None):
        import io
        import json
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import patch, Mock
        from web_api import server
        body = json.dumps(payload).encode()
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "u",
                     "content-length": str(len(body))},
            path="/internal/admin/compliance", rfile=io.BytesIO(body),
            send_json=lambda status, value: response.update(status=status, value=value))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
             patch.object(server, "identity", return_value={"userId": "u", "role": role}), \
             patch.object(server, "connect_db", return_value=nullcontext(object())), \
             patch.object(server, "audit_admin_access", audit or Mock()):
            server.Handler.do_POST(handler)
        return response

    def test_preview_is_admin_only_and_calculates(self):
        payload = {"action": "recovery_waterfall_preview", "proceedsMinorUnits": 5,
                   "claims": [{"id": "a", "rank": 1, "amountMinorUnits": 3},
                              {"id": "b", "rank": 1, "amountMinorUnits": 3}]}
        self.assertEqual(self.request(payload, "user")["status"], 403)
        response = self.request(payload)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["value"]["result"]["allocations"], {"a": 3, "b": 2})

    def test_preview_rejects_invalid_shape_and_amount(self):
        for claims in [[{"id": "a", "rank": 1, "amountMinorUnits": -1}],
                       [{"id": "a", "rank": 1, "amountMinorUnits": 1, "extra": 1}],
                       "bad"]:
            response = self.request({"action": "recovery_waterfall_preview",
                                     "proceedsMinorUnits": 5, "claims": claims})
            self.assertEqual(response["status"], 400)

    def test_contract_rate_reset_preview_requires_admin_and_applies_limit(self):
        payload = {"action": "contract_rate_reset_preview", "contractDay": 365,
                   "previousRate": "3", "referenceRate": "6",
                   "schedule": {"segments": [{"from_day": 0, "kind": "fixed", "rate": "3"},
                                               {"from_day": 365, "kind": "floating", "spread": "1"}],
                                "max_change_per_reset": "1"}}
        self.assertEqual(self.request(payload, "user")["status"], 403)
        response = self.request(payload)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["value"]["result"]["rate"], "4")
        self.assertTrue(response["value"]["result"]["simulationOnly"])

    def test_contract_rate_preview_rejects_invalid_payload(self):
        base = {"action": "contract_rate_reset_preview", "contractDay": 1,
                "previousRate": "3", "schedule": {"segments": [{"from_day": 0,
                "kind": "floating", "spread": "1"}]}}
        self.assertEqual(self.request(base)["status"], 400)  # missing reference rate
        base["referenceRate"] = "2"
        for invalid in [dict(base, contractDay=-1), dict(base, schedule={"segments": [42]}),
                        dict(base, previousRate=1.1), dict(base, previousRate="1e100"),
                        dict(base, contractDay=9007199254740992),
                        dict(base, schedule={"segments": [], "unknown": 1})]:
            self.assertEqual(self.request(invalid)["status"], 400)

    def test_recovery_curve_preview_is_admin_only_audited_and_validated(self):
        from unittest.mock import Mock
        payload = {"action": "recovery_curve_preview", "balanceMinorUnits": 1000, "annualDiscountBps": 1000,
                   "tranches": [{"label": "담보 매각", "month": 0, "amountMinorUnits": 400}, {"label": "보증인", "month": 12, "amountMinorUnits": 300}]}
        self.assertEqual(self.request(payload, "user")["status"], 403)
        audit = Mock()
        response = self.request(payload, audit=audit)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(response["value"]["result"]["presentValueMinorUnits"], 673)
        self.assertEqual(audit.call_args[0][1], "recovery_curve.preview")
        for bad in (dict(payload, extra=1), dict(payload, balanceMinorUnits=0), dict(payload, annualDiscountBps=-1),
                    dict(payload, tranches=[{"label": "a", "month": 0}]), dict(payload, tranches="x"),
                    dict(payload, tranches=[{"label": "a", "month": 0, "amountMinorUnits": 9007199254740992}])):
            audit = Mock()
            self.assertEqual(self.request(bad, audit=audit)["status"], 400)
            audit.assert_not_called()

    def test_preview_rejects_unsafe_json_integers(self):
        for amount in [9007199254740992, 1.5, True]:
            payload = {"action": "recovery_waterfall_preview", "proceedsMinorUnits": amount,
                       "claims": [{"id": "a", "rank": 1, "amountMinorUnits": 1}]}
            self.assertEqual(self.request(payload)["status"], 400)
        payload = {"action": "recovery_waterfall_preview", "proceedsMinorUnits": 1,
                   "claims": [{"id": "a", "rank": 1, "amountMinorUnits": 9007199254740992}]}
        self.assertEqual(self.request(payload)["status"], 400)

    def test_successful_previews_are_audited_without_claim_data(self):
        from unittest.mock import Mock
        audit = Mock()
        response = self.request({"action": "recovery_waterfall_preview", "proceedsMinorUnits": 5,
                                 "claims": [{"id": "private", "rank": 1, "amountMinorUnits": 3}]}, audit=audit)
        self.assertEqual(response["status"], 200)
        audit.assert_called_once_with("u", "recovery_waterfall.preview", {"claim_count": 1})
        audit.reset_mock()
        response = self.request({"action": "contract_rate_reset_preview", "contractDay": 0,
                                 "previousRate": "3", "schedule": {"segments": [
                                     {"from_day": 0, "kind": "fixed", "rate": "3"}]}}, audit=audit)
        self.assertEqual(response["status"], 200)
        audit.assert_called_once_with("u", "contract_rate_reset.preview", {"segment_count": 1})

    def test_charged_off_preview_requires_admin_and_uses_loan(self):
        from unittest.mock import Mock, patch
        from functions import recovery_waterfall
        payload = {"action": "charged_off_waterfall_preview", "loanId": 1,
                   "proceedsMinorUnits": 100, "bankRank": 1, "otherClaims": []}
        result = {"loanId": 1, "currencyCode": "USD", "bankClaimMinorUnits": 80,
                  "allocations": {"bank": 80}, "shortfalls": {"bank": 0},
                  "unallocatedMinorUnits": 20, "simulationOnly": True,
                  "otherClaimsAreAssumptions": True}
        audit = Mock()
        with patch.object(recovery_waterfall, 'preview_charged_off_loan', return_value=result) as preview:
            self.assertEqual(self.request(payload, 'user', audit=audit)['status'], 403)
            preview.assert_not_called()
            self.assertEqual(self.request(payload, audit=audit)['value']['result'], result)
        audit.assert_called_once_with('u', 'charged_off_waterfall.preview', {'loan_id': 1, 'claim_count': 0})

    def test_charged_off_preview_rejects_unsafe_input(self):
        base = {"action": "charged_off_waterfall_preview", "loanId": 1,
                "proceedsMinorUnits": 100, "bankRank": 1, "otherClaims": []}
        for change in [{"loanId": True}, {"bankRank": 0}, {"proceedsMinorUnits": 9007199254740992},
                       {"otherClaims": [{"id": "x", "rank": 1, "amountMinorUnits": 1.5}]},
                       {"otherClaims": "bad"}]:
            self.assertEqual(self.request({**base, **change})['status'], 400)


if __name__ == "__main__":
    unittest.main()


class RecoveryCurveTests(unittest.TestCase):
    def test_zero_rate_keeps_nominal_and_discounting_lowers_later_money(self):
        from functions.recovery_waterfall import recovery_curve_preview
        flat = recovery_curve_preview(1000, [("담보 매각", 6, 400), ("보증인", 12, 300)], 0)
        self.assertEqual((flat["presentValueMinorUnits"], flat["nominalRecoveryBps"], flat["presentValueLossMinorUnits"]), (700, 7000, 300))
        disc = recovery_curve_preview(1000, [("담보 매각", 0, 400), ("보증인", 12, 300)], 1000)
        self.assertEqual([r["presentValueMinorUnits"] for r in disc["tranches"]], [400, 273])     # 300 / 1.10 = 272.7 -> 273
        self.assertEqual(disc["presentValueMinorUnits"], 673)
        self.assertLess(disc["presentValueRecoveryBps"], disc["nominalRecoveryBps"])

    def test_recoveries_above_the_balance_show_no_loss(self):
        from functions.recovery_waterfall import recovery_curve_preview
        self.assertEqual(recovery_curve_preview(100, [("a", 0, 150)], 500)["presentValueLossMinorUnits"], 0)

    def test_bad_input_is_rejected(self):
        from functions.recovery_waterfall import recovery_curve_preview
        for args in ((0, [("a", 0, 1)], 0), (100, [], 0), (100, [("a", -1, 1)], 0), (100, [("a", 0, 1.5)], 0), (100, [("a", 0, True)], 0),
                     (100, [("", 0, 1)], 0), (100, [("a", 0, 1)], -1), (True, [("a", 0, 1)], 0), (100, [("a", 1201, 1)], 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                recovery_curve_preview(*args)
