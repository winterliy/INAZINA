"""Every bank is also a 법인 with its own 법인 계정."""

import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_companies import INDUSTRY, ensure, ensure_all
from managers.market_session_manager import MarketSessionManager


class BankCompanyTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def bank(self, conn, issuer, bank_id=None, capital=2_000_000):
        conn.execute("INSERT INTO financial_institutions(issuer,nationality,equity_capital,institution_id) "
                     "VALUES (?, '낙원', ?, ?)", (issuer, capital, bank_id))
        return conn.execute("SELECT institution_id FROM financial_institutions WHERE issuer=?", (issuer,)).fetchone()[0]

    def test_existing_banks_get_an_unlisted_company_and_its_account_once(self):
        with self.db.atomic() as conn:
            first, second = self.bank(conn, "첫째은행", "FIRST"), self.bank(conn, "둘째은행")
            created = ensure_all(conn)
            self.assertIn(first, created)
            self.assertIn(second, created)
            company = conn.execute("SELECT * FROM company WHERE company_code=?", (first,)).fetchone()
            self.assertEqual((company["name"], company["industry"], company["exchange_id"]),
                             ("첫째은행", INDUSTRY, MarketSessionManager.UNLISTED))
            self.assertEqual(company["capital"], 2_000_000)
            account = conn.execute("SELECT u.role FROM company_accounts a JOIN users u ON u.user_id=a.user_id "
                                   "WHERE a.company_code=?", (first,)).fetchone()
            self.assertEqual(account["role"], "company")
            self.assertEqual(ensure_all(conn), [])

    def test_a_new_bank_gets_its_company_at_creation(self):
        with self.db.atomic() as conn:
            bank_id = self.bank(conn, "새은행", "NEWBANK")
            self.assertEqual(ensure(conn, "새은행"), bank_id)
            self.assertEqual(ensure(conn, "새은행"), bank_id)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_accounts WHERE company_code=?",
                                          (bank_id,)).fetchone()[0], 1)

    def test_a_product_already_using_the_bank_id_is_never_taken_over(self):
        self.db.companies.create_company("CLASH", "Listed", "Industry", 100000, 10000, 10, "x", "EX")
        with self.db.atomic() as conn:
            self.bank(conn, "충돌은행", "CLASH")
            self.assertIsNone(ensure(conn, "충돌은행"))
            self.assertEqual(conn.execute("SELECT name FROM company WHERE company_code='CLASH'").fetchone()[0], "Listed")


if __name__ == "__main__":
    unittest.main()
