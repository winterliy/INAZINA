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


if __name__ == "__main__":
    unittest.main()
