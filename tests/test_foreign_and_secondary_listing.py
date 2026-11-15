"""Direct foreign listings, per-exchange listing requirements and 1:1 secondary (2차상장) lines."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import DatabaseManager
from database.db_manager import DBManager
from managers import listing_requirements
from managers.admin_control_manager import AdminControlManager


class ForeignAndSecondaryListingTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.system.set_setting("auto_target_enabled", "0")
        for exchange, country in (("HOME", "낙원"), ("AWAY", "타국"), ("THIRD", "타국")):
            self.db.system.execute(
                "INSERT OR IGNORE INTO market_exchanges(exchange_id,status,updated_epoch,trading_halted,nationality) "
                "VALUES (?,'open',0,0,?)", (exchange, country))
        self.db.system.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES ('타국','NWX')")
        self.db.users.create_user("admin", "Admin", "낙원", 1000000, role="admin")
        self.control = AdminControlManager(SimpleNamespace(db=self.db))
        self.control.entity("admin", "stock", "create", "ACME", name="Acme", price="100", supply="1000",
                            extra={"exchange_id": "HOME", "industry": "test", "capital": 100000})

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def rules(self, exchange, **payload):
        values = listing_requirements.normalize(payload)
        with self.db.atomic(("system",)) as conn:
            conn.execute(f"UPDATE market_exchanges SET {','.join(f'{k}=?' for k in values)} WHERE exchange_id=?",
                         (*values.values(), exchange))

    def company(self):
        with self.db.atomic(("company",)) as conn:
            return dict(conn.execute(
                "SELECT c.exchange_id,c.nationality,o.country_code FROM company c "
                "JOIN organizations o ON o.organization_code=c.company_code WHERE c.company_code='ACME'").fetchone())

    def test_company_lists_directly_abroad_and_keeps_its_country(self):
        self.control.entity("admin", "stock", "edit", "ACME", extra={"exchange_id": "AWAY"})
        self.assertEqual(self.company(), {"exchange_id": "AWAY", "nationality": "낙원", "country_code": "낙원"})
        self.db.companies.backfill_nationality_from_exchange()  # what every restart runs
        self.assertEqual(self.company()["nationality"], "낙원")

    def test_exchange_can_refuse_foreign_companies_and_small_listings(self):
        self.rules("AWAY", allowForeign=False)
        with self.assertRaisesRegex(ValueError, "해외 법인 불가"):
            self.control.entity("admin", "stock", "edit", "ACME", extra={"exchange_id": "AWAY"})
        self.rules("HOME", minShares=500, minFloatPercent=60)
        with self.assertRaisesRegex(ValueError, "60% 이상"):
            self.control.entity("admin", "stock", "edit", "ACME", extra={"listed_shares": 550})
        self.control.entity("admin", "stock", "edit", "ACME", extra={"listed_shares": 600})
        self.rules("HOME", minMarketCap="1000000")
        with self.assertRaisesRegex(ValueError, "시가총액"):
            self.control.entity("admin", "stock", "edit", "ACME", extra={"listed_shares": 700})

    def test_disclosure_and_secondary_rules(self):
        self.rules("THIRD", requiresDisclosure=True, allowSecondary=False)
        with self.db.atomic(("company",)) as conn:
            with self.assertRaisesRegex(ValueError, "2차상장을 받지"):
                listing_requirements.check(conn, "THIRD", "ACME", 10, secondary=True)
            with self.assertRaisesRegex(ValueError, "재무보고서"):
                listing_requirements.check(conn, "THIRD", "ACME", 10)

    def test_secondary_line_is_an_editable_one_to_one_receipt(self):
        self.control.entity("admin", "stock", "edit", "ACME", extra={"listed_shares": 600})
        self.control.entity("admin", "dr", "create", "ACME.AWAY", name="Acme (AWAY 2차상장)", price="100", supply="300",
                            extra={"underlying_code": "ACME", "underlying_per_receipt": 1, "receipt_type": "SECONDARY",
                                   "home_exchange_id": "HOME", "exchange_id": "AWAY"})
        with self.db.atomic(("assets",)) as conn:
            program = dict(conn.execute("SELECT receipt_supply,deposited_underlying_quantity FROM depositary_receipts "
                                        "WHERE dr_code='ACME.AWAY'").fetchone())
            # The program only caps the line at all issued shares; its size is the editable total_supply.
            self.assertEqual(program, {"receipt_supply": 1000, "deposited_underlying_quantity": 0})
            conn.execute("UPDATE market_assets SET total_supply=400 WHERE code='ACME.AWAY'")
        # A plain DR still has the whole deposit capacity: the secondary line reserves none.
        self.control.entity("admin", "dr", "create", "ACMEDR", name="Acme DR", price="100", supply="1000",
                            extra={"underlying_code": "ACME", "underlying_per_receipt": 1, "exchange_id": "THIRD"})
        with self.assertRaisesRegex(ValueError, "1:1"):
            self.control.entity("admin", "dr", "create", "ACME.X", name="x", price="100", supply="10",
                                extra={"underlying_code": "ACME", "underlying_per_receipt": 2,
                                       "receipt_type": "SECONDARY", "exchange_id": "THIRD"})


if __name__ == "__main__":
    unittest.main()
