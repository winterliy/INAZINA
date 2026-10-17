import unittest

from managers.credit_risk_manager import CreditRiskManager


class CreditRiskStageTests(unittest.TestCase):
    def test_performing_and_significantly_deteriorated_loans(self):
        self.assertEqual(CreditRiskManager._stage("active", "current", 0, .5, False), 1)
        self.assertEqual(CreditRiskManager._stage("active", "overdue", 0, .5, True), 2)
        self.assertEqual(CreditRiskManager._stage("active", "current", 2, .5, False), 2)
        self.assertEqual(CreditRiskManager._stage("active", "current", 0, 1.0, False), 2)

    def test_defaulted_exposure_is_stage_three(self):
        self.assertEqual(CreditRiskManager._stage("defaulted", "defaulted", 0, 0, True), 3)
        self.assertEqual(CreditRiskManager._stage("active", "defaulted", 0, 0, True), 3)


if __name__ == "__main__":
    unittest.main()
