import json
import io
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from database.db_manager import DBManager
from web_api import server


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_database_dir = DBManager.database_dir
        self.old_database = server.DATABASE
        root = Path(self.tempdir.name)
        DBManager.database_dir = root
        server.DATABASE = root / "inazina.db"
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                CREATE TABLE users (
                    user_id TEXT PRIMARY KEY, nickname TEXT, nationality TEXT,
                    cash INTEGER DEFAULT 0, credit_score INTEGER DEFAULT 700,
                    status TEXT DEFAULT 'active', role TEXT DEFAULT 'user');
                CREATE TABLE admin_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, recorded_epoch REAL NOT NULL);
                CREATE TABLE web_auth_credentials (
                    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_epoch INTEGER NOT NULL,
                    updated_epoch INTEGER NOT NULL, auth_version INTEGER NOT NULL DEFAULT 0, hts_id TEXT);
                CREATE TABLE country_currencies (nationality TEXT PRIMARY KEY, currency_code TEXT NOT NULL);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TRIGGER settings_rate_check BEFORE UPDATE ON settings
                    WHEN NEW.key='base_rate' AND CAST(NEW.value AS REAL) NOT BETWEEN -100 AND 100
                    BEGIN SELECT RAISE(ABORT, 'Rate is outside its permitted range'); END;
                CREATE TABLE market_price_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT NOT NULL,
                    price REAL NOT NULL, recorded_epoch INTEGER NOT NULL,
                    UNIQUE(asset_code, recorded_epoch));
                CREATE TABLE market_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT, event_type TEXT NOT NULL,
                    severity INTEGER NOT NULL, starts_epoch INTEGER NOT NULL, ends_epoch INTEGER NOT NULL,
                    created_by TEXT NOT NULL);
                INSERT INTO users(user_id,nickname,nationality,role) VALUES ('admin-1','Admin','낙원','admin');
                INSERT INTO settings(key,value) VALUES ('base_rate','3');
            """)

    def tearDown(self):
        DBManager.database_dir = self.old_database_dir
        server.DATABASE = self.old_database
        self.tempdir.cleanup()

    def execute(self, sql, params=()):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            return connection.execute(sql, params).fetchall()

    def test_dr_reference_value_and_deviation_alert(self):
        ordinary = server.dr_reference_metrics(22, 10, 2, 1)
        self.assertEqual(ordinary["referenceValue"], 20)
        self.assertAlmostEqual(ordinary["referencePremiumPercent"], 10)
        self.assertTrue(ordinary["referenceDeviationAlert"])
        self.assertFalse(server.dr_reference_metrics(21.99, 10, 2, 1)["referenceDeviationAlert"])
        self.assertTrue(server.dr_reference_metrics(18, 10, 2, 1)["referenceDeviationAlert"])
        self.assertEqual(server.dr_reference_metrics(float("nan"), 10, 2, 1), {})

    def test_compliance_overview_exposes_ecl_discount_factor(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                CREATE TABLE currencies(code TEXT PRIMARY KEY, decimal_places INTEGER);
                INSERT INTO currencies VALUES ('NWX',0);
                CREATE TABLE kyc_profiles(user_id TEXT,status TEXT,risk_tier TEXT,reviewed_by TEXT,
                    reviewed_epoch REAL,expires_epoch REAL,updated_epoch REAL);
                INSERT INTO kyc_profiles VALUES ('bad-expiry','verified','standard','admin',1,253402300800,2);
                INSERT INTO kyc_profiles VALUES ('pending-expiry','pending','standard','admin',1,1e300,3);
                CREATE TABLE aml_alerts(alert_id INTEGER,user_id TEXT,nationality TEXT,rule_code TEXT,
                    reference_key TEXT,currency_code TEXT,amount REAL,status TEXT,outcome_code TEXT,
                    opened_epoch REAL,reviewed_by TEXT,reviewed_epoch REAL);
                CREATE TABLE aml_monitoring_rules(nationality TEXT,currency_code TEXT,
                    large_transaction_limit REAL,rolling_24h_amount_limit REAL,
                    rolling_24h_count_limit INTEGER,enabled INTEGER,updated_epoch REAL);
                CREATE TABLE kyc_profile_events(event_id INTEGER,user_id TEXT,actor_id TEXT,
                    event_type TEXT,prior_status TEXT,new_status TEXT,prior_risk_tier TEXT,
                    new_risk_tier TEXT,reason_code TEXT,recorded_epoch REAL);
                INSERT INTO kyc_profile_events VALUES
                    (1,'alice','alice','transfer_denied','pending','pending',
                     'standard','standard','kyc_incomplete_or_expired',1);
                CREATE TABLE aml_case_events(event_id INTEGER,alert_id INTEGER,actor_id TEXT,
                    event_type TEXT,prior_status TEXT,new_status TEXT,outcome_code TEXT,recorded_epoch REAL);
                CREATE TABLE credit_risk_scenarios(nationality TEXT,scenario_key TEXT,
                    probability REAL,pd_multiplier REAL,lgd_multiplier REAL,updated_epoch REAL);
                CREATE TABLE loans(loan_id INTEGER PRIMARY KEY,user_id TEXT,product_code TEXT,
                    defaulted_epoch REAL);
                CREATE TABLE liability(product_code TEXT PRIMARY KEY,issuer TEXT);
                INSERT INTO liability VALUES ('ECL-PRODUCT','Bank');
                CREATE TABLE loan_ecl_assessments(assessment_id INTEGER,loan_id INTEGER,as_of_day TEXT,
                    stage INTEGER,pd_12m REAL,lifetime_pd REAL,lgd REAL,ead REAL,expected_loss REAL,
                    discount_factor REAL,effective_interest_rate REAL,expected_loss_time_years REAL,
                    macro_pd_multiplier REAL,
                    quote_currency TEXT,scenario TEXT,calculated_epoch REAL);
                INSERT INTO loans VALUES (1,'admin-1','ECL-PRODUCT',NULL);
                INSERT INTO loan_ecl_assessments VALUES
                    (1,1,'2026-10-17',2,0.1,0.2,0.4,1000,72,0.95,8,0.25,1.2,'NWX','[]',1);
            """)
        result = server.admin_compliance_overview()
        self.assertEqual(result["eclAssessments"][0]["discount_factor"], 0.95)
        self.assertEqual(result["eclAssessments"][0]["effective_interest_rate"], 8)
        self.assertEqual(result["eclAssessments"][0]["expected_loss_time_years"], 0.25)
        self.assertEqual(result["eclAssessments"][0]["macro_pd_multiplier"], 1.2)
        self.assertEqual(result["eclAssessments"][0]["expected_loss_text"], "72")
        profiles = {row["user_id"]: row for row in result["kycProfiles"]}
        self.assertEqual(profiles["bad-expiry"]["status"], "expired")
        self.assertIsNone(profiles["bad-expiry"]["expires_epoch"])
        self.assertEqual(profiles["pending-expiry"]["status"], "pending")
        self.assertIsNone(profiles["pending-expiry"]["expires_epoch"])
        self.assertEqual(result["kycEvents"][0]["event_type"], "transfer_denied")
        self.assertEqual(result["kycEvents"][0]["actor_id"], "alice")
        self.assertEqual(result["kycEvents"][0]["reason_code"], "kyc_incomplete_or_expired")
        self.assertEqual(result["eclBacktest"]["groups"], [])

    def test_admin_countries_returns_auditable_income_source_tax_overrides(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                ALTER TABLE country_currencies ADD COLUMN base_rate REAL NOT NULL DEFAULT 3;
                ALTER TABLE country_currencies ADD COLUMN legal_max_rate REAL NOT NULL DEFAULT 20;
                ALTER TABLE country_currencies ADD COLUMN tax_percent REAL NOT NULL DEFAULT 3;
                CREATE TABLE currencies(code TEXT PRIMARY KEY,name TEXT,status TEXT);
                CREATE TABLE country_treasuries(nationality TEXT PRIMARY KEY,user_id TEXT);
                CREATE TABLE guild_countries(guild_id TEXT,nationality TEXT);
                CREATE TABLE market_exchanges(exchange_id TEXT PRIMARY KEY,nationality TEXT);
                CREATE TABLE market_rate_curve(nationality TEXT,benchmark_key TEXT,tenor_days INTEGER,
                    annual_rate REAL,updated_epoch REAL);
                CREATE TABLE market_rate_pressure(nationality TEXT,benchmark_key TEXT,
                    adjustment REAL,updated_epoch REAL);
                CREATE TABLE IF NOT EXISTS country_rate_history_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT,nationality TEXT,
                    rate_key TEXT,before_percent REAL,after_percent REAL,before_epoch REAL,
                    after_epoch REAL,before_actor TEXT,after_actor TEXT,recorded_epoch REAL);
                CREATE TABLE IF NOT EXISTS market_rate_curve_history_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT,nationality TEXT,
                    benchmark_key TEXT,tenor_days INTEGER,before_rate REAL,after_rate REAL,
                    before_epoch REAL,after_epoch REAL,before_actor TEXT,after_actor TEXT,
                    recorded_epoch REAL);
                CREATE TABLE country_tax_rules(nationality TEXT,income_source TEXT,tax_percent REAL,
                    updated_by TEXT,updated_epoch REAL);
                CREATE TABLE country_macro_indicators(nationality TEXT,inflation_percent REAL,
                    real_gdp_growth_percent REAL,unemployment_percent REAL,fiscal_debt_percent REAL,
                    credit_stress_shock_bps INTEGER,fx_stress_shock_percent REAL,
                    updated_by TEXT,updated_epoch REAL);
                INSERT INTO country_currencies(nationality,currency_code) VALUES ('낙원','NWX');
                INSERT INTO currencies(code,name,status) VALUES ('NWX','나쿠','active');
                INSERT INTO currencies(code,name,status) VALUES ('AGC','가국화','active');
                INSERT INTO country_currencies(nationality,currency_code,base_rate) VALUES ('가국','AGC',8);
                INSERT INTO country_currencies(nationality,currency_code,base_rate) VALUES ('공통권','NWX',100);
                INSERT INTO country_tax_rules VALUES ('낙원','lottery_winnings',12.5,'admin-1',123);
                INSERT INTO country_macro_indicators VALUES ('낙원',3,1.5,4,55,250,12,'admin-1',120);
                INSERT INTO country_rate_history_events(operation,nationality,rate_key,
                    before_percent,after_percent,before_actor,after_actor,recorded_epoch)
                    VALUES ('update','낙원','base_rate',3,4,'admin-0','admin-1',321);
                INSERT INTO market_rate_curve_history_events(operation,nationality,benchmark_key,
                    tenor_days,before_rate,after_rate,before_actor,after_actor,recorded_epoch)
                    VALUES ('replace','낙원','bank_bond',365,5,6,'sql','admin-1',322);
            """)
        country = next(item for item in server.admin_countries() if item["nationality"] == "낙원")
        self.assertEqual(country["taxRules"], [{
            "income_source": "lottery_winnings", "tax_percent": 12.5,
            "updated_by": "admin-1", "updated_epoch": 123.0,
        }])
        self.assertEqual(country["macroIndicators"]["fiscal_debt_percent"], 55)
        self.assertEqual(country["macroIndicators"]["credit_stress_shock_bps"], 250)
        self.assertEqual(country["macroIndicators"]["fx_stress_shock_percent"], 12)
        self.assertEqual(country["externalReferenceRate"], 8)
        self.assertAlmostEqual(country["externalRatePremium"], 0.5)
        self.assertEqual(country["rateHistoryEvents"], [{
            "operation": "update", "rate_key": "base_rate", "before_percent": 3,
            "after_percent": 4, "before_actor": "admin-0", "after_actor": "admin-1",
            "before_epoch": None, "after_epoch": None, "recorded_epoch": 321.0,
        }])
        self.assertEqual(country["marketRateHistoryEvents"], [{
            "operation": "replace", "benchmark_key": "bank_bond", "tenor_days": 365,
            "before_rate": 5, "after_rate": 6, "before_actor": "sql",
            "after_actor": "admin-1", "before_epoch": None, "after_epoch": None,
            "recorded_epoch": 322.0,
        }])

    def test_macro_indicator_web_action_validates_and_audits_inputs(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.execute("""CREATE TABLE country_macro_indicators (
                nationality TEXT PRIMARY KEY,inflation_percent REAL,real_gdp_growth_percent REAL,
                unemployment_percent REAL,fiscal_debt_percent REAL,credit_stress_shock_bps INTEGER,
                fx_stress_shock_percent REAL,updated_by TEXT,updated_epoch REAL)""")
            connection.execute(
                "INSERT INTO country_macro_indicators VALUES ('낙원',2,2,5,60,200,10,'system',0)"
            )

        @contextmanager
        def atomic():
            with closing(server.sqlite3.connect(server.DATABASE)) as connection:
                with connection:
                    yield connection

        class Admin:
            @staticmethod
            def require(actor, _connection):
                if actor != "admin-1":
                    raise PermissionError("admin required")

            @staticmethod
            def _audit(connection, actor, action, payload):
                connection.execute(
                    "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) "
                    "VALUES (?,?,?,?)", (actor, action, json.dumps(payload), 10),
                )

        bot = SimpleNamespace(
            db=SimpleNamespace(
                atomic=atomic,
                users=SimpleNamespace(execute_one=lambda query, params: self.execute(query, params)[0]),
            ),
            admin_control=Admin(),
        )
        with patch("web_api.server.domain", return_value=SimpleNamespace(bot=bot)):
            server.admin_country_action("admin-1", "낙원", {
                "action": "macro_indicators", "inflationPercent": 8,
                "realGdpGrowthPercent": -2, "unemploymentPercent": 10,
                "fiscalDebtPercent": 120, "creditStressShockBps": 350,
                "fxStressShockPercent": 18.5,
            })
            with self.assertRaises(ValueError):
                server.admin_country_action("admin-1", "낙원", {
                    "action": "macro_indicators", "inflationPercent": -101,
                    "realGdpGrowthPercent": 2, "unemploymentPercent": 5,
                    "fiscalDebtPercent": 60,
                })
            with self.assertRaises(ValueError):
                server.admin_country_action("admin-1", "낙원", {
                    "action": "macro_indicators", "inflationPercent": 2,
                    "realGdpGrowthPercent": 2, "unemploymentPercent": 5,
                    "fiscalDebtPercent": 60, "creditStressShockBps": 1001,
                })
            with self.assertRaises(ValueError):
                server.admin_country_action("admin-1", "낙원", {
                    "action": "macro_indicators", "inflationPercent": 2,
                    "realGdpGrowthPercent": 2, "unemploymentPercent": 5,
                    "fiscalDebtPercent": 60, "creditStressShockBps": 200.5,
                })
            with self.assertRaises(ValueError):
                server.admin_country_action("admin-1", "낙원", {
                    "action": "macro_indicators", "inflationPercent": 2,
                    "realGdpGrowthPercent": 2, "unemploymentPercent": 5,
                    "fiscalDebtPercent": 60, "fxStressShockPercent": 100.1,
                })
            server.admin_country_action("admin-1", "낙원", {
                "action": "macro_indicators", "inflationPercent": 8,
                "realGdpGrowthPercent": -2, "unemploymentPercent": 10,
                "fiscalDebtPercent": 120,
            })
        saved = self.execute(
            "SELECT * FROM country_macro_indicators WHERE nationality='낙원'"
        )[0]
        self.assertEqual(saved[1:6], (8, -2, 10, 120, 350))
        self.assertEqual(saved[6], 18.5)
        audit = self.execute(
            "SELECT action,payload FROM admin_audit WHERE action='country.macro_indicators'"
        )[0]
        self.assertEqual(audit[0], "country.macro_indicators")
        self.assertEqual(json.loads(audit[1])["creditStressShockBps"], 350)
        self.assertEqual(json.loads(audit[1])["fxStressShockPercent"], 18.5)

    def test_market_instruments_include_bond_rate_risk_metrics(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                CREATE TABLE market_assets(code TEXT,name TEXT,type TEXT,price REAL,exchange_id TEXT,
                    quote_currency TEXT,status TEXT);
                CREATE TABLE company(company_code TEXT,name TEXT,share_price REAL,exchange_id TEXT,
                    quote_currency TEXT,status TEXT);
                CREATE TABLE investment(product_code TEXT,name TEXT,product_type TEXT,unit_price REAL,
                    exchange_id TEXT,quote_currency TEXT,status TEXT,fund_category TEXT);
                CREATE TABLE treasury_bond_products(product_code TEXT,name TEXT,market_price REAL,
                    exchange_id TEXT,quote_currency TEXT,status TEXT,face_value INTEGER,annual_rate REAL,
                    maturity_days INTEGER,coupon_frequency_days INTEGER,day_count_convention TEXT,
                    credit_spread REAL,liquidity_spread REAL,created_at TEXT);
                CREATE TABLE market_exchanges(exchange_id TEXT PRIMARY KEY,nationality TEXT);
                CREATE TABLE market_rate_curve(nationality TEXT,benchmark_key TEXT,tenor_days INTEGER,
                    annual_rate REAL);
                CREATE TABLE market_rate_pressure(nationality TEXT,benchmark_key TEXT,adjustment REAL);
                CREATE TABLE trading_warnings(asset_code TEXT,level TEXT,reason TEXT,set_by TEXT,
                    created_epoch REAL,expires_epoch REAL);
                INSERT INTO trading_warnings VALUES ('TB1','warning','bad expiry','admin',1,253402300800);
                CREATE TABLE depositary_receipts(dr_code TEXT,underlying_code TEXT,receipt_type TEXT,
                    depositary_bank TEXT,custodian TEXT,home_exchange_id TEXT,listing_exchange_id TEXT,
                    underlying_per_receipt REAL,receipt_supply INTEGER,deposited_underlying_quantity REAL,
                    program_status TEXT);
                CREATE TABLE depositary_receipt_movements(dr_code TEXT,movement_type TEXT,
                    underlying_quantity REAL,dr_quantity REAL);
                CREATE TABLE fund_components(product_code TEXT,asset_code TEXT,weight REAL,asset_class TEXT);
                INSERT INTO market_exchanges VALUES ('BOND','낙원');
                INSERT INTO market_rate_curve VALUES ('낙원','treasury',1,3),('낙원','treasury',3650,4);
                INSERT INTO treasury_bond_products VALUES ('TB1','Test Treasury',1000,'BOND','NWX',
                    'active',1000,5,365,180,'ACT/365',0.2,0.1,CURRENT_TIMESTAMP);
            """)
        with patch.object(server, "domain", return_value=None):
            bond = next(row for row in server.instruments() if row["code"] == "TB1")
        self.assertGreater(bond["bondRisk"]["modified_duration"], 0)
        self.assertGreater(bond["bondRisk"]["dv01"], 0)
        self.assertNotIn("warning", bond)

    def test_dr_web_ledgers_distinguish_holder_basis_from_custody_pool_basis(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                CREATE TABLE currencies(code TEXT PRIMARY KEY,decimal_places INTEGER);
                INSERT INTO currencies VALUES ('NWX',0);
                CREATE TABLE company(company_code TEXT,quote_currency TEXT);
                CREATE TABLE market_assets(code TEXT,quote_currency TEXT);
                CREATE TABLE depositary_receipts(dr_code TEXT,underlying_code TEXT);
                CREATE TABLE depositary_receipt_movements(movement_id INTEGER PRIMARY KEY,reference_key TEXT,
                    dr_code TEXT,user_id TEXT,movement_type TEXT,dr_quantity INTEGER,underlying_quantity INTEGER,
                    underlying_average_cost INTEGER,recorded_epoch REAL,holder_underlying_average_cost REAL,
                    holder_underlying_currency TEXT,custody_underlying_average_cost REAL,
                    custody_underlying_currency TEXT);
                INSERT INTO depositary_receipts VALUES ('BASE_DR','BASE');
                INSERT INTO depositary_receipt_movements VALUES (1,'issue','BASE_DR','alice','issue',2,2,40,10,40,'NWX',40,'NWX');
                INSERT INTO depositary_receipt_movements VALUES (2,'redeem','BASE_DR','alice','redeem',1,1,40,20,50,'NWX',40,'NWX');
            """)
        with patch.object(server, "domain", return_value=None):
            user_rows = server.user_dr_movements("alice")
            admin_rows = server.admin_dr_movements()["movements"]
        redeemed_user_row = next(row for row in user_rows if row["movement_type"] == "redeem")
        redeemed_admin_row = next(row for row in admin_rows if row["movement_type"] == "redeem")
        self.assertEqual(redeemed_user_row["underlying_average_cost"], 50)
        self.assertEqual(redeemed_admin_row["underlying_average_cost"], 40)
        self.assertEqual(redeemed_admin_row["holder_underlying_average_cost_text"], "50")

    def test_user_loan_schedules_only_returns_own_unpaid_installments(self):
        connection = server.sqlite3.connect(":memory:")
        connection.row_factory = server.sqlite3.Row
        connection.executescript("""
            CREATE TABLE currencies(code TEXT PRIMARY KEY, decimal_places INTEGER);
            INSERT INTO currencies VALUES ('NWX',0);
            CREATE TABLE loans(loan_id INTEGER PRIMARY KEY,user_id TEXT,product_code TEXT,
                quote_currency TEXT,outstanding_balance REAL,status TEXT,collection_status TEXT,
                maturity_epoch REAL,underwriting_dsr REAL,underwriting_stress_dsr REAL,
                underwriting_stress_shock_bps INTEGER,underwriting_fx_stress_shock_percent REAL,
                origination_pd_12m REAL,stage_cure_since_epoch REAL,rate_type TEXT DEFAULT 'fixed',
                contract_rate REAL,next_repricing_epoch REAL,benchmark_key TEXT,
                benchmark_tenor_days INTEGER,repricing_lag_seconds INTEGER DEFAULT 0);
            CREATE TABLE loan_payment_schedule(schedule_id INTEGER PRIMARY KEY,loan_id INTEGER,
                installment_number INTEGER,due_epoch REAL,principal_due REAL,interest_due REAL,
                fee_due REAL,principal_paid REAL,interest_paid REAL,fee_paid REAL,amount_paid REAL,status TEXT);
            CREATE TABLE contract_rate_reset_history(reset_id INTEGER PRIMARY KEY,contract_type TEXT,
                contract_id TEXT,old_rate REAL,new_rate REAL,scheduled_epoch REAL,applied_epoch REAL,
                benchmark_key TEXT,benchmark_tenor_days INTEGER);
            CREATE TABLE loan_ecl_assessments(assessment_id INTEGER PRIMARY KEY,loan_id INTEGER,
                stage INTEGER,pd_12m REAL,calculated_epoch REAL);
            INSERT INTO loans(loan_id,user_id,product_code,quote_currency,outstanding_balance,status,collection_status,maturity_epoch,underwriting_dsr,underwriting_stress_dsr,underwriting_stress_shock_bps,underwriting_fx_stress_shock_percent,origination_pd_12m,stage_cure_since_epoch)
                VALUES (1,'alice','L1','NWX',900,'active','current',3000,20,25,200,10,0.01,2000);
            INSERT INTO loans(loan_id,user_id,product_code,quote_currency,outstanding_balance,status,collection_status,maturity_epoch,underwriting_dsr,underwriting_stress_dsr,underwriting_stress_shock_bps,underwriting_fx_stress_shock_percent,origination_pd_12m,stage_cure_since_epoch)
                VALUES (2,'bob','L2','NWX',500,'active','current',3000,30,35,200,10,0.02,NULL);
            UPDATE loans SET rate_type='floating',contract_rate=5,next_repricing_epoch=2500,
                benchmark_key='bank_bond',benchmark_tenor_days=90,repricing_lag_seconds=86400
                WHERE loan_id=1;
            INSERT INTO loan_ecl_assessments VALUES (1,1,2,0.03,1000);
            INSERT INTO loan_ecl_assessments VALUES (2,2,1,0.01,1000);
            INSERT INTO loan_payment_schedule VALUES (11,1,1,1000,800,80,20,0,0,0,0,'scheduled');
            INSERT INTO loan_payment_schedule VALUES (12,1,2,2000,700,70,10,700,70,10,780,'paid');
            INSERT INTO loan_payment_schedule VALUES (21,2,1,1000,500,50,0,0,0,0,0,'scheduled');
            INSERT INTO contract_rate_reset_history VALUES (1,'loan','1',5,6,900,1000,'bank_bond',90);
            INSERT INTO contract_rate_reset_history VALUES (2,'loan','2',5,6,900,1000,'bank_bond',90);
        """)
        with patch.object(server, "domain"), patch.object(
            server, "connect_readonly", return_value=closing(connection)
        ):
            result = server.user_loan_schedules("alice")
        self.assertEqual([loan["loan_id"] for loan in result["loans"]], [1])
        self.assertEqual([row["schedule_id"] for row in result["loans"][0]["installments"]], [11])
        self.assertEqual(result["loans"][0]["decimal_places"], 0)
        self.assertEqual(result["loans"][0]["rate_resets"][0]["new_rate"], 6)
        self.assertEqual(result["loans"][0]["underwriting_stress_dsr"], 25)
        self.assertEqual(result["loans"][0]["underwriting_stress_shock_bps"], 200)
        self.assertEqual(result["loans"][0]["underwriting_fx_stress_shock_percent"], 10)
        self.assertEqual(result["loans"][0]["origination_pd_12m"], 0.01)
        self.assertEqual(result["loans"][0]["current_ecl_stage"], 2)
        self.assertEqual(result["loans"][0]["current_ecl_pd_12m"], 0.03)
        self.assertEqual(result["loans"][0]["next_repricing_epoch"], 2500)
        self.assertEqual(result["loans"][0]["benchmark_key"], "bank_bond")
        self.assertEqual(result["loans"][0]["contract_rate"], 5)
        self.assertEqual(result["loans"][0]["stage_cure_since_epoch"], 2000)

    def test_user_dividend_payments_are_private_and_currency_aware(self):
        connection = server.sqlite3.connect(":memory:")
        connection.row_factory = server.sqlite3.Row
        connection.executescript("""
            CREATE TABLE currencies(code TEXT PRIMARY KEY, decimal_places INTEGER);
            INSERT INTO currencies VALUES ('USD',2);
            CREATE TABLE dividend_distributions(distribution_id INTEGER PRIMARY KEY,asset_code TEXT,
                dividend_kind TEXT);
            CREATE TABLE dividend_payments(payment_id INTEGER PRIMARY KEY,distribution_id INTEGER,
                user_id TEXT,quantity REAL,gross_amount REAL,tax_amount REAL,net_amount_units INTEGER,
                quote_currency TEXT,created_epoch INTEGER);
            INSERT INTO dividend_distributions VALUES (1,'KO_DR','regular');
            INSERT INTO dividend_distributions VALUES (2,'KO_DR','special');
            INSERT INTO dividend_payments VALUES (10,1,'alice',2,0.02,0,20000,'USD',100);
            INSERT INTO dividend_payments VALUES (11,2,'bob',5,50,0,50000000,'NWX',200);
        """)
        with patch.object(server, "domain"), patch.object(
            server, "connect_readonly", return_value=closing(connection)
        ):
            result = server.user_dividend_payments("alice")
        self.assertEqual(len(result), 1)
        self.assertEqual(
            {key: result[0][key] for key in ("payment_id", "asset_code", "dividend_kind", "gross_amount", "quote_currency", "decimal_places")},
            {"payment_id": 10, "asset_code": "KO_DR", "dividend_kind": "regular", "gross_amount": 0.02, "quote_currency": "USD", "decimal_places": 2},
        )

    def test_candles_aggregate_open_high_low_close_per_bucket(self):
        ticks = [(0, 100), (10, 120), (30, 90), (59, 110), (60, 111), (119, 105)]
        for epoch, price in ticks:
            self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('AAA',?,?)",
                         (price, 600 + epoch))
        self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('BBB',999,610)")
        with server.connect_readonly() as connection:
            result = server.candles(connection, "AAA", 0, 60)
        self.assertEqual(result, [
            {"time": 600, "open": 100.0, "high": 120.0, "low": 90.0, "close": 110.0, "ticks": 4},
            {"time": 660, "open": 111.0, "high": 111.0, "low": 105.0, "close": 105.0, "ticks": 2},
        ])

    def test_chart_rejects_unknown_ranges_and_intervals(self):
        with patch.object(server, "instruments", return_value=[{"code": "AAA"}]):
            with self.assertRaisesRegex(ValueError, "unsupported_chart_range"):
                server.chart("AAA", "2y")
            with self.assertRaisesRegex(ValueError, "unsupported_candle_interval"):
                server.chart("AAA", "1d", 7)
            result = server.chart("AAA", "1w")
        self.assertEqual(result["interval"], 3600)
        self.assertEqual(result["candles"], [])

    def test_drawings_round_trip_per_user_and_instrument(self):
        drawings = [
            {"id": "a", "type": "trend", "anchors": [{"time": 1, "price": 10}, {"time": 2, "price": 12.5}], "extra": 1},
            {"id": "b", "type": "horizontal", "anchors": [{"time": 3, "price": 11}]},
        ]
        saved = server.save_drawings("admin-1", "AAA", drawings)
        self.assertNotIn("extra", saved[0])
        self.assertEqual(server.get_drawings("admin-1", "AAA"), saved)
        self.assertEqual(server.get_drawings("admin-1", "BBB"), [])
        self.assertEqual(server.get_drawings("other", "AAA"), [])
        server.save_drawings("admin-1", "AAA", [])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM web_chart_drawings")[0][0], 0)

    def test_drawings_reject_invalid_shapes(self):
        invalid = [
            "not a list",
            [{"id": "a", "type": "circle", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": "a", "type": "trend", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": "a", "type": "horizontal", "anchors": [{"time": 1, "price": 0}]}],
            [{"id": "a", "type": "horizontal", "anchors": [{"time": float("nan"), "price": 1}]}],
            [{"id": "", "type": "horizontal", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": str(index), "type": "horizontal", "anchors": [{"time": 1, "price": 1}]}
             for index in range(server.MAX_DRAWINGS + 1)],
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:40]), self.assertRaisesRegex(ValueError, "invalid_drawing"):
                server.normalize_drawings(value)

    def test_audit_and_events_are_read_from_the_database(self):
        self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','settings','{\"a\":1}',5)")
        self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','legacy','not json',6)")
        self.execute("INSERT INTO market_events(asset_code,event_type,severity,starts_epoch,ends_epoch,created_by) "
                     "VALUES ('AAA','good',5,1,2,'admin-1')")
        audit = server.admin_audit()
        self.assertEqual(audit[0], {"auditId": 2, "actorId": "admin-1", "action": "legacy", "payload": {"raw": "not json"}, "time": 6.0})
        self.assertEqual(audit[1]["payload"], {"a": 1})
        events = server.admin_events()
        self.assertEqual(events[0]["asset_code"], "AAA")
        self.assertIsNone(events[0]["target_mode"])
        self.assertFalse(events[0]["active"])

    def test_sensitive_admin_report_access_is_audited(self):
        server.audit_admin_access(
            "admin-1", "bank_journal.read",
            {"issuer": "Test Bank", "currency": "EUR", "since": 123.0},
        )
        entry = server.admin_audit()[0]
        self.assertEqual(entry["actorId"], "admin-1")
        self.assertEqual(entry["action"], "bank_journal.read")
        self.assertEqual(entry["payload"], {
            "issuer": "Test Bank", "currency": "EUR", "since": 123.0,
        })

    def test_bank_journal_route_records_actor_and_filters(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/bank-journal?issuer=Test%20Bank&currency=EUR&since=123",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "admin_bank_journal_report", return_value={
                    "trialBalance": [{}], "depositInsuranceClaims": [],
                }):
            server.Handler.do_GET(handler)
        self.assertEqual(response, {"status": 200, "value": {
            "trialBalance": [{}], "depositInsuranceClaims": [],
        }})
        entry = server.admin_audit()[0]
        self.assertEqual(entry["actorId"], "admin-1")
        self.assertEqual(entry["action"], "bank_journal.read")
        self.assertEqual(entry["payload"], {
            "issuer": "Test Bank", "currency": "EUR", "since": 123.0,
            "trial_balance_accounts": 1, "insurance_claims": 0,
        })

    def test_bank_journal_route_rejects_out_of_range_since_before_report_or_audit(self):
        for since in ("-1", "1e300", "nan", "true"):
            with self.subTest(since=since):
                response = {}
                handler = SimpleNamespace(
                    headers={"x-inazina-internal-token": "secret",
                             "x-inazina-user-id": "admin-1"},
                    path=f"/internal/admin/bank-journal?issuer=Test%20Bank&since={since}",
                    send_json=lambda status, value: response.update(
                        status=status, value=value
                    ),
                )
                with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                        patch.object(server, "identity", return_value={
                            "userId": "admin-1", "role": "admin",
                        }), patch.object(server, "admin_bank_journal_report") as report, \
                        patch.object(server, "audit_admin_access") as audit:
                    server.Handler.do_GET(handler)
                self.assertEqual(response["status"], 400)
                report.assert_not_called()
                audit.assert_not_called()

    def test_admin_user_search_audit_omits_search_term(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/users?search=private%40example.com",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "admin_users", return_value=[]):
            server.Handler.do_GET(handler)
        entry = server.admin_audit()[0]
        self.assertEqual(entry["action"], "admin_users.read")
        self.assertEqual(entry["payload"], {
            "kind": "people", "filtered": True, "result_count": 0,
        })

    def test_compliance_report_access_is_audited(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/compliance?nationality=한국",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "admin_compliance_overview", return_value={
                    "kycProfiles": [{}], "amlAlerts": [], "eclAssessments": [{}, {}],
                }):
            server.Handler.do_GET(handler)
        self.assertEqual(response, {"status": 200, "value": {
            "kycProfiles": [{}], "amlAlerts": [], "eclAssessments": [{}, {}],
        }})
        entry = server.admin_audit()[0]
        self.assertEqual(entry["action"], "compliance.read")
        self.assertEqual(entry["payload"], {
            "nationality": "한국", "kyc_profiles": 1,
            "aml_alerts": 0, "ecl_assessments": 2,
        })

    def test_admin_dr_movement_access_is_audited(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/dr/movements?limit=999",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "admin_dr_movements", return_value={"movements": []}):
            server.Handler.do_GET(handler)
        entry = server.admin_audit()[0]
        self.assertEqual(entry["action"], "dr_movements.read")
        self.assertEqual(entry["payload"], {"limit": 200, "result_count": 0})

    def test_admin_dr_custody_process_access_is_audited(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/dr/custody-events?limit=999",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "admin_dr_custody_events", return_value={"events": []}):
            server.Handler.do_GET(handler)
        self.assertEqual(response, {"status": 200, "value": {"events": []}})
        entry = server.admin_audit()[0]
        self.assertEqual(entry["action"], "dr_custody_events.read")
        self.assertEqual(entry["payload"], {"limit": 500, "result_count": 0})

    def test_admin_dr_custody_rejection_is_coded_and_audited(self):
        response = {}
        body = json.dumps({
            "referenceKey": "reject-me", "reasonCode": "compliance_review",
        }).encode()
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin-1",
                     "content-length": str(len(body))},
            path="/internal/admin/dr/custody-instructions/reject",
            rfile=io.BytesIO(body),
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        manager = SimpleNamespace(reject_conversion=MagicMock(return_value=(True, {
            "referenceKey": "reject-me", "status": "rejected",
            "reasonCode": "compliance_review",
        })))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "domain", return_value=SimpleNamespace(
                    bot=SimpleNamespace(db=object()),
                )), patch("managers.depositary_receipt_manager.DepositaryReceiptManager",
                          return_value=manager):
            server.Handler.do_POST(handler)
        manager.reject_conversion.assert_called_once_with(
            "admin-1", "reject-me", "compliance_review",
        )
        self.assertEqual(response["status"], 200)
        self.assertEqual(server.admin_audit()[0]["payload"], {
            "reference_key": "reject-me", "reason_code": "compliance_review",
        })

    def test_admin_dr_custody_reconciliation_is_read_only_and_audited(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin-1"},
            path="/internal/admin/dr/custody-reconciliation",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        report = {"programCount": 1, "discrepancyCount": 0, "programs": [], "discrepancies": []}
        manager = SimpleNamespace(reconcile_custody_ledger=MagicMock(return_value=report))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "domain", return_value=SimpleNamespace(
                    bot=SimpleNamespace(db=object()),
                )), patch("managers.depositary_receipt_manager.DepositaryReceiptManager",
                          return_value=manager):
            server.Handler.do_GET(handler)
        manager.reconcile_custody_ledger.assert_called_once_with()
        self.assertEqual(response, {"status": 200, "value": report})
        self.assertEqual(server.admin_audit()[0]["action"], "dr_custody_reconciliation.read")

    def test_admin_dr_voting_meeting_list_is_bounded_and_audited(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/dr/voting-meetings?limit=999",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        meetings = [{"meetingId": 4, "underlyingCode": "ACME", "agendaCount": 2}]
        manager = SimpleNamespace(list_voting_meetings=lambda limit: meetings if limit == 100 else [])
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "domain", return_value=SimpleNamespace(
                    bot=SimpleNamespace(db=object()),
                )), patch("managers.depositary_receipt_manager.DepositaryReceiptManager",
                          return_value=manager):
            server.Handler.do_GET(handler)
        self.assertEqual(response, {"status": 200, "value": {"meetings": meetings}})
        entry = server.admin_audit()[0]
        self.assertEqual(entry["action"], "dr_voting_meetings.list")
        self.assertEqual(entry["payload"], {"limit": 100, "result_count": 1})

    def test_user_dr_voting_delegations_are_scoped_to_authenticated_owner(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "citizen-1"},
            path="/internal/me/dr/voting-delegations",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        records = [{"delegation_id": 7, "dr_code": "ABC_DR", "proxy_user_id": "proxy-2"}]
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "citizen-1", "role": "user",
                }), patch.object(server, "user_dr_voting_delegations", return_value=records) as getter:
            server.Handler.do_GET(handler)
        getter.assert_called_once_with("citizen-1")
        self.assertEqual(response, {"status": 200, "value": {"delegations": records}})

    def test_proxy_dr_voting_delegations_are_scoped_to_authenticated_proxy(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "proxy-2"},
            path="/internal/me/dr/proxy-voting-delegations",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        records = [{"delegation_id": 7, "owner_user_id": "citizen-1"}]
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "proxy-2", "role": "user",
                }), patch.object(server, "user_dr_received_voting_delegations", return_value=records) as getter:
            server.Handler.do_GET(handler)
        getter.assert_called_once_with("proxy-2")
        self.assertEqual(response, {"status": 200, "value": {"delegations": records}})

    def test_user_dr_conversion_list_is_scoped_to_authenticated_user(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "citizen-1"},
            path="/internal/me/dr/conversions",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        records = [{"reference_key": "request-1", "status": "requested"}]
        manager = SimpleNamespace(list_custody_instructions=lambda **filters: records if filters == {
            "user_id": "citizen-1", "limit": 100,
        } else [])
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "citizen-1", "role": "user",
                }), patch.object(server, "domain", return_value=SimpleNamespace(
                    bot=SimpleNamespace(db=object()),
                )), patch("managers.depositary_receipt_manager.DepositaryReceiptManager",
                          return_value=manager):
            server.Handler.do_GET(handler)
        self.assertEqual(response, {"status": 200, "value": {"instructions": records}})

    def test_user_dr_conversion_endpoint_uses_delayed_request_flow(self):
        response = {}
        body = json.dumps({
            "drCode": "ABC_DR", "action": "issue", "quantity": 2,
            "referenceKey": "request-1",
        }).encode()
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "citizen-1",
                     "content-length": str(len(body))},
            path="/internal/dr/convert", rfile=io.BytesIO(body),
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        manager = SimpleNamespace(request_conversion=MagicMock(return_value=(True, {
            "status": "requested", "message": "접수됨",
        })))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "citizen-1", "role": "user",
                }), patch.object(server, "domain", return_value=SimpleNamespace(
                    bot=SimpleNamespace(db=object()),
                )), patch("managers.depositary_receipt_manager.DepositaryReceiptManager",
                          return_value=manager) as manager_type:
            server.Handler.do_POST(handler)
        manager.request_conversion.assert_called_once_with(
            "citizen-1", "ABC_DR", "issue", 2, "request-1",
        )
        self.assertEqual(response, {"status": 200, "value": {
            "success": True, "result": {"status": "requested", "message": "접수됨"},
        }})

    def test_admin_customer_detail_access_is_audited(self):
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret",
                     "x-inazina-user-id": "admin-1"},
            path="/internal/admin/user/customer-42",
            send_json=lambda status, value: response.update(status=status, value=value),
        )
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                patch.object(server, "identity", return_value={
                    "userId": "admin-1", "role": "admin",
                }), patch.object(server, "admin_user_detail", return_value={"userId": "customer-42"}):
            server.Handler.do_GET(handler)
        entry = server.admin_audit()[0]
        self.assertEqual(entry["action"], "admin_user.read")
        self.assertEqual(entry["payload"], {"subject_user_id": "customer-42", "found": True})

    def test_db_editor_hides_credentials_and_audit(self):
        names = {table["name"] for table in server.db_tables()}
        self.assertIn("settings", names)
        self.assertTrue(names.isdisjoint({"web_auth_credentials", "admin_audit"}))
        for table in ("web_auth_credentials", "admin_audit", "sqlite_master"):
            with self.subTest(table=table), self.assertRaisesRegex(ValueError, "table_not_found"):
                server.db_rows(table)

    def test_db_editor_update_insert_delete_are_audited(self):
        page = server.db_rows("settings", search="base")
        self.assertEqual(page["total"], 1)
        rowid = page["rows"][0]["__rowid__"]
        updated = server.db_update("admin-1", "settings", rowid, {"value": "4.5"})
        self.assertEqual(updated["value"], "4.5")
        inserted = server.db_insert("admin-1", "settings", {"key": "garch_alpha", "value": "0.1"})
        server.db_delete("admin-1", "settings", inserted["__rowid__"])
        actions = [(row[0], json.loads(row[1])) for row in self.execute(
            "SELECT action,payload FROM admin_audit ORDER BY audit_id")]
        self.assertEqual([action for action, _ in actions], ["db.update", "db.insert", "db.delete"])
        self.assertEqual(actions[0][1]["before"], {"value": "3"})
        self.assertEqual(actions[0][1]["after"], {"value": "4.5"})
        self.assertEqual(self.execute("SELECT COUNT(*) FROM settings")[0][0], 1)

    def test_db_editor_keeps_database_triggers_and_validates_columns(self):
        rowid = server.db_rows("settings")["rows"][0]["__rowid__"]
        with self.assertRaises(server.sqlite3.IntegrityError):
            server.db_update("admin-1", "settings", rowid, {"value": "500"})
        self.assertEqual(self.execute("SELECT value FROM settings")[0][0], "3")
        self.assertEqual(self.execute("SELECT COUNT(*) FROM admin_audit")[0][0], 0)
        for values in ({}, {"missing": 1}, {"value": ["list"]}, {"value": float("inf")}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                server.db_update("admin-1", "settings", rowid, values)
        with self.assertRaisesRegex(ValueError, "row_not_found"):
            server.db_update("admin-1", "settings", 999, {"value": "1"})

    def test_admin_command_catalog_lists_usage(self):
        catalog = server.admin_command_catalog()
        names = {item["name"] for item in catalog["commands"]}
        self.assertIn("잔고수정", names)
        self.assertTrue(all("usage" in item for item in catalog["commands"]))
        with self.assertRaisesRegex(ValueError, "알 수 없는"):
            server.run_admin_command("admin-1", {"command": "rm", "arguments": ""})

    def test_national_commands_cover_discord_treasury_commands(self):
        from functions.admin_prefix import NATIONAL_COMMANDS
        catalog = server.admin_command_catalog()
        national = {item["name"] for item in catalog["national"]}
        self.assertEqual(national | set(catalog["discordOnly"]), NATIONAL_COMMANDS)
        self.assertIn("매수", national)
        transfer = next(item for item in catalog["national"] if item["name"] == "송금")
        self.assertEqual([field["required"] for field in transfer["fields"]], [True, True, False])

    def test_national_commands_validate_before_running(self):
        cases = [
            ({"command": "도움말", "values": []}, "알 수 없는"),
            ({"command": "매수", "values": ["A"]}, "입력값"),
            ({"command": "매수", "values": ["A", " ", "1"]}, "수량"),
            ({"command": "매수", "values": ["A", 1, "1"]}, "입력값"),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, error):
                server.run_national_command("admin-1", payload)

    def test_db_editor_masks_password_columns(self):
        self.execute("CREATE TABLE accounts (account_number TEXT PRIMARY KEY, user_id TEXT, password TEXT, balance INTEGER)")
        self.execute("INSERT INTO accounts VALUES ('A1','admin-1','secret',5)")
        row = server.db_rows("accounts")["rows"][0]
        self.assertEqual(row["password"], server.HIDDEN_VALUE)
        updated = server.db_update("admin-1", "accounts", row["__rowid__"],
                                   {"password": server.HIDDEN_VALUE, "balance": 7})
        self.assertEqual(updated["balance"], 7)
        self.assertEqual(self.execute("SELECT password FROM accounts")[0][0], "secret")
        payload = self.execute("SELECT payload FROM admin_audit")[0][0]
        self.assertNotIn("secret", payload)

    def test_admin_web_password_and_login_actions(self):
        message = server.admin_set_web_password("admin-1", "admin-1", "ResetPass1234")
        self.assertIn("비밀번호", message)
        self.assertIsNotNone(server.authenticate_password("admin-1", "ResetPass1234"))
        server.admin_web_login_action("admin-1", "admin-1", "hts_id", "Admin.Web")
        user, found = server.authenticate_login_identifier("admin.web", "ResetPass1234")
        self.assertTrue(found)
        self.assertEqual(user["userId"], "admin-1")
        version = self.execute("SELECT auth_version FROM web_auth_credentials")[0][0]
        server.admin_web_login_action("admin-1", "admin-1", "logout")
        self.assertEqual(self.execute("SELECT auth_version FROM web_auth_credentials")[0][0], version + 1)
        with self.assertRaisesRegex(ValueError, "password_policy"):
            server.admin_set_web_password("admin-1", "admin-1", "short")
        audit = [row[0] for row in self.execute("SELECT payload FROM admin_audit")]
        self.assertFalse(any("ResetPass1234" in payload for payload in audit))

    def test_shareholder_meetings_are_queued_for_the_bot(self):
        message = server.queue_shareholder_meeting("admin-1", {"code": "AAA", "place": "회의실"})
        self.assertIn("#1", message)
        jobs = server.bot_jobs()
        self.assertEqual((jobs[0]["kind"], jobs[0]["status"]), ("shareholder_meeting", "pending"))
        self.assertEqual(json.loads(jobs[0]["payload"]), {"code": "AAA", "place": "회의실"})
        with self.assertRaisesRegex(ValueError, "주식 코드"):
            server.queue_shareholder_meeting("admin-1", {"code": "", "place": "x"})

    def test_help_pages_match_the_discord_help_view(self):
        from functions.admin_help_view import AdminHelpView
        pages = server.admin_help_pages()
        self.assertEqual([page["label"] for page in pages], [label for _key, label in AdminHelpView.PAGES])
        self.assertTrue(any("$상품생성" in field["value"] for page in pages for field in page["fields"]))

    def test_product_catalog_covers_every_creatable_kind(self):
        from managers.admin_commands_manager import CREATE_ARGS
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            for table, key in {("company", "company_code"), ("market_assets", "code"), ("investment", "product_code"),
                               ("treasury_bond_products", "product_code"), ("saving", "product_code"),
                               ("liability", "product_code"), ("option_contracts", "contract_code"), ("currencies", "code")}:
                connection.execute(f"CREATE TABLE {table} ({key} TEXT PRIMARY KEY, name TEXT, status TEXT, extra_setting REAL)")
        kinds = {item["kind"]: item for item in server.product_catalog()["kinds"]}
        self.assertEqual(set(kinds), set(CREATE_ARGS))
        self.assertEqual({kinds[name]["group"] for name in ("saving", "loan", "currency", "currencyproduct")}, {"finance"})
        self.assertIn("extra_setting", {field["key"] for field in kinds["saving"]["advanced"]})
        self.assertNotIn("status", {field["key"] for field in kinds["saving"]["advanced"]})

    def test_product_form_values_follow_the_positional_parser(self):
        fields = server._convert_product_fields(
            "stock", {"name": "회사", "capital": "1000", "dividend_cycle_seconds": "1주",
                      "dividend_per_share": "5%", "exchange_id": "INZ", "industry": ""},
            {"liquidity": "0.5"}, {"liquidity": "REAL"})
        self.assertEqual(fields["capital"], 1000)
        self.assertEqual(fields["dividend_cycle_seconds"], 604800)
        self.assertEqual((fields["dividend_mode"], fields["dividend_percent"]), ("percent", 5.0))
        self.assertEqual(fields["liquidity"], 0.5)
        self.assertNotIn("industry", fields)
        fund = server._convert_product_fields("fund", {"components": "A:0.6, B:0.4"}, {}, {})
        self.assertEqual(fund["components"], {"A": 0.6, "B": 0.4})
        for kind, values, advanced in (("stock", {"unknown": "1"}, {}), ("stock", {}, {"status": "x"}),
                                       ("fund", {"components": "A0.6"}, {})):
            with self.subTest(values=values, advanced=advanced), self.assertRaises(ValueError):
                server._convert_product_fields(kind, values, advanced, {"status": "TEXT"})

    def test_products_are_purged_only_when_stopped_and_unused(self):
        self.execute("CREATE TABLE market_assets (code TEXT PRIMARY KEY, name TEXT, status TEXT, quote_currency TEXT)")
        self.execute("CREATE TABLE user_assets (user_id TEXT, asset_code TEXT, quantity REAL)")
        self.execute("CREATE TABLE market_orders (order_id INTEGER PRIMARY KEY, asset_code TEXT, status TEXT)")
        self.execute("INSERT INTO market_assets VALUES ('AAA','에이','active','NWX'), ('BBB','비','inactive','NWX')")
        self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('AAA',1,1), ('BBB',1,1)")
        self.execute("INSERT INTO user_assets VALUES ('u','BBB',1)")
        self.execute("INSERT INTO market_orders VALUES (1,'BBB','filled')")
        with self.assertRaisesRegex(ValueError, "먼저 중지"):
            server.product_delete("admin-1", "AAA")
        with self.assertRaisesRegex(ValueError, "보유 중인 사용자"):
            server.product_delete("admin-1", "BBB")
        self.execute("DELETE FROM user_assets")
        self.assertIn("완전히 삭제", server.product_delete("admin-1", "BBB"))
        self.assertEqual(self.execute("SELECT code FROM market_assets"), [("AAA",)])
        self.assertEqual(self.execute("SELECT asset_code FROM market_price_history"), [("AAA",)])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM market_orders")[0][0], 1)
        self.assertEqual(self.execute("SELECT action FROM admin_audit")[-1][0], "product.purge")

    def test_savings_and_fx_accounts_share_one_form(self):
        self.assertEqual(server._form_fields("saving"), server._form_fields("currencyproduct"))
        fields = server._convert_product_fields("currencyproduct", {"name": "달러", "quote_currency": "usd", "interest_rate": "2"},
                                                {}, {})
        self.assertEqual(fields, {"name": "달러", "quote_currency": "USD", "interest_rate": 2})


if __name__ == "__main__":
    unittest.main()
