"""Money values at the documented maximum are valid in both backends."""
import unittest

import test_inazina as fixtures
from functions.amount_limits import MAX_NATIVE


class MoneyBoundaryBackendTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_maximum_cash_is_accepted(self):
        self.db.users.execute("UPDATE users SET cash=? WHERE user_id='citizen'", (MAX_NATIVE,))
        self.assertEqual(self.db.users.get_user('citizen')['cash'], MAX_NATIVE)
