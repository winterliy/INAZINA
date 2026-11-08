import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from database.currency_db import CurrencyDB
from managers.currency_manager import CurrencyManager, SCALE
from managers.compliance_manager import ComplianceManager
from managers.admin_commands_manager import AdminCommandsManager
from functions.currency_display import native_summary
from functions.discord_views import InfoPanelView
import discord

class CurrencyHomeTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def configure(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','A국 화폐','1')")
        cmd=AdminCommandsManager(self.bot)
        cmd.execute('admin','서버설치','123 A국 AAA')
        cmd.execute('admin','서버설치','낙원 NWX',guild_id=456)
        cmd.execute('admin','환율수정','NWX AAA 3')
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','AAA')")
        self.db.currency.execute("INSERT OR IGNORE INTO currency_liquidity_state(currency_code) SELECT code FROM currencies")
        self.db.currency.execute('UPDATE currency_liquidity_state SET issuance_elasticity=0,circulation_elasticity=0')
        return cmd

    def test_registration_uses_server_and_existing_citizens_keep_nation_and_cash(self):
        self.configure();cm=CurrencyManager(self.db)
        self.assertTrue(cm.register_user('new','신규',123))
        self.assertEqual(self.db.users.get_user('new')['nationality'],'A국')
        self.assertEqual(self.db.users.get_user('new')['registration_guild_id'],'123')
        with self.db.atomic() as conn:
            self.assertEqual(cm.balance(conn,'new','AAA'),3_000_000*SCALE)
            self.assertEqual(cm.balance(conn,'new','NWX'),0)
        self.assertFalse(cm.register_user('new','다른 이름',456))
        self.assertEqual(self.db.users.get_user('new')['nationality'],'A국')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_000)
        with self.assertRaisesRegex(ValueError,'등록 서버'):cm.register_user('dm','DM',None)
        self.assertIsNone(self.db.users.get_user('dm'))

    def test_income_pays_only_that_country_in_its_currency_from_its_treasury(self):
        cmd=self.configure()
        message=cmd.execute('admin','기본소득','미국 30000')
        self.assertIn('✅',message); self.assertIn('AAA',message)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_030_000*SCALE)
            treasury=CurrencyManager.treasury_id(conn,'미국')
            self.assertEqual(CurrencyManager.balance(conn,treasury,'AAA'),-30_000*SCALE)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_000_000)
        self.assertEqual(self.db.users.get_treasury_balance(),10_000_000)
        rows=self.db.currency.execute('SELECT * FROM basic_income_payments')
        self.assertEqual([(r['user_id'],r['source_currency'],r['target_currency']) for r in rows],[('citizen','AAA','AAA')])

    def test_income_rejects_unknown_country_and_old_usage(self):
        cmd=self.configure()
        ok,msg=self.bot.admin_mgr.distribute_basic_income('없는나라',10000)
        self.assertFalse(ok,msg)
        self.assertEqual(self.db.currency.execute_one('SELECT COUNT(*) FROM basic_income_payments')[0],0)
        with self.assertRaisesRegex(ValueError,r'\[국가\] \[금액\]'):cmd.execute('admin','기본소득','10000')

    def test_default_income_and_admin_balance_use_actors_home_currency(self):
        cmd=self.configure()
        self.db.users.execute("UPDATE users SET nationality='A국' WHERE user_id='admin'")
        cmd.execute('admin','잔고수정','citizen modify 5.5')
        cmd.execute('admin','잔고수정','citizen add 2')
        cmd.execute('admin','잔고수정','citizen del 1')
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),6_500_000)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],6.5)

    def test_wallet_and_search_classify_nwx_as_foreign_and_value_in_home(self):
        cmd=self.configure();cmd.execute('admin','기본소득','미국 30000')
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['national_currency'],'AAA')
        self.assertEqual(info['home_cash_units'],1_030_000*SCALE)
        self.assertEqual(info['foreign_cash_units'],0)
        self.assertFalse(any(r['currency_code']=='NWX' for r in info['wallets']))
        embed=discord.Embed();embed.add_field(name='💵 현금',value='```0 NWX```')
        native_summary(embed,info)
        self.assertEqual(embed.fields[0].value,'```1,030,000.00 AAA```')
        async def panel():
            view=InfoPanelView(self.bot,'citizen')
            return view._category_lines('forex')
        lines=asyncio.run(panel())
        self.assertTrue(any('1 AAA' in line and 'NWX' in line for line in lines),lines)
        self.assertFalse(any('(AAA)' in line for line in lines))

    def test_native_wallet_exchange_transfer_and_source_wallet_preserve_denominations(self):
        cmd=self.configure();cmd.execute('admin','기본소득','미국 30000')
        cmd.execute('admin','기본소득','낙원 10000')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        msg=self.bot.currency_mgr.transfer('citizen','admin','300')
        self.assertIn('300 AAA → 100 NWX',msg)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_010_100)
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('NWX_SAVE','NWX 예금',0,'NWX','saving')")
        number = self.bot.currency_mgr.open_account('citizen','NWX_SAVE')
        self.bot.currency_mgr.exchange('citizen','AAA','NWX','300')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_029_400)
        self.bot.currency_mgr.transfer('citizen','admin','10',number)
        self.assertEqual(self.db.accounts.get_account(number)['balance_units'],90*SCALE)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_029_400*SCALE)

    def test_fx_and_transfer_post_each_currency_and_link_actual_wallet_rows(self):
        from managers.accounting_journal import AccountingJournal

        self.configure()
        self.db.system.set_setting("currency_exchange_fee_percent", "0")
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) "
                                 "VALUES ('BASE_SAVE','Base account',0,'NWX','saving')")
        self.bot.currency_mgr.open_account("citizen", "BASE_SAVE")
        self.bot.currency_mgr.exchange("citizen", "AAA", "NWX", "300")
        self.bot.currency_mgr.transfer("citizen", "admin", "300")
        with self.db.readonly() as conn:
            rows = conn.execute("""SELECT transaction_kind,role,currency_code,decimal_places,
                delta_minor_units FROM currency_cash_postings ORDER BY transaction_kind,role""").fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("exchange", "source", "AAA", 2, -30000),
                ("exchange", "target", "NWX", 0, 100),
                ("transfer", "source", "AAA", 2, -30000),
                ("transfer", "target", "NWX", 0, 100),
            ])
            check = AccountingJournal.reconcile_currency_cash(conn)
            self.assertEqual((check["status"], check["event_count"]), ("matched", 4))

    def test_exchange_fee_links_the_treasury_receipt_in_its_actual_currency(self):
        from managers.accounting_journal import AccountingJournal

        self.configure()
        self.db.system.set_setting("currency_exchange_fee_percent", "1")
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) "
                                 "VALUES ('BASE_SAVE','Base account',0,'NWX','saving')")
        self.bot.currency_mgr.open_account("citizen", "BASE_SAVE")
        self.bot.currency_mgr.exchange("citizen", "AAA", "NWX", "300")
        with self.db.readonly() as conn:
            row = conn.execute("""SELECT p.currency_code,p.delta_minor_units,w.delta_minor_units,
                p.user_id FROM currency_cash_postings p JOIN currency_cash_wallet_links l
                USING(transaction_kind,transaction_id,role) JOIN customer_wallet_ledger w
                ON w.entry_id=l.wallet_entry_id WHERE p.role='fee'""").fetchone()
            self.assertEqual(tuple(row), ("AAA", 300, 300, CurrencyManager.treasury_id(conn, "미국")))
            self.assertEqual(AccountingJournal.reconcile_currency_cash(conn)["status"], "matched")

    def test_currency_audit_reports_unposted_post_activation_transaction(self):
        from managers.accounting_journal import AccountingJournal

        self.configure()
        self.db.currency.execute("""INSERT INTO currency_transactions
            (user_id,source_currency,target_currency,source_units,target_units,fee_nwx,recorded_epoch)
            VALUES ('citizen','AAA','NWX',?,?,0,1)""", (3*SCALE,SCALE))
        with self.db.readonly() as conn:
            check = AccountingJournal.reconcile_currency_cash(conn)
        self.assertEqual(check["status"], "mismatch")
        self.assertEqual({(gap["role"], gap["status"]) for gap in check["gaps"]},
                         {("source", "missing_posting"), ("target", "missing_posting")})

    def test_aml_monitors_sender_and_recipient_in_their_home_currencies(self):
        cmd=self.configure()
        self.db.users.create_user('recipient','수취인','미국',1000)
        cmd.execute('admin','기본소득','미국 100')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        with self.db.atomic() as conn:
            ComplianceManager.configure_aml_rule(
                conn,'미국','admin',large_transaction_limit=10,
                rolling_24h_amount_limit=35,enabled=True
            )
        self.bot.currency_mgr.transfer('citizen','recipient','30')
        self.bot.currency_mgr.transfer('citizen','recipient','10')
        alerts=self.db.currency.execute(
            "SELECT user_id,rule_code,currency_code,amount,amount_minor_units,"
            "amount_decimal_places FROM aml_alerts "
            "ORDER BY user_id,opened_epoch"
        )
        recipient_alerts=[r for r in alerts if r['user_id']=='recipient']
        self.assertEqual(
            [(r['rule_code'],r['currency_code']) for r in recipient_alerts],
            [('large_transaction','AAA'), ('large_transaction','AAA'),
             ('rolling_24h_amount','AAA')],
        )
        self.assertEqual([r['amount'] for r in recipient_alerts],[30,10,40])
        self.assertEqual([r['amount_minor_units'] for r in recipient_alerts], [3000,1000,4000])
        self.assertTrue(all(r['amount_decimal_places'] == 2 for r in recipient_alerts))
        sender_alerts=[r for r in alerts if r['user_id']=='citizen']
        self.assertEqual(len(sender_alerts),3)

    def test_kyc_and_aml_workflows_reject_invalid_epochs_without_records(self):
        self.configure()
        before = {
            "kyc": self.db.system.execute_one("SELECT COUNT(*) FROM kyc_profile_events")[0],
            "aml": self.db.currency.execute_one("SELECT COUNT(*) FROM aml_alerts")[0],
            "audit": self.db.system.execute_one("SELECT COUNT(*) FROM admin_audit")[0],
        }
        for invalid_epoch in (-1, float('nan'), float('inf'), 1e300, True, 'invalid'):
            calls = (
                ("invalid_kyc_check_epoch", lambda c: ComplianceManager.require_financial_eligibility(
                    c, 'citizen', now=invalid_epoch)),
                ("invalid_kyc_event_epoch", lambda c: ComplianceManager.record_financial_action_denial(
                    c, 'citizen', 'transfer', now=invalid_epoch)),
                ("invalid_kyc_review_epoch", lambda c: ComplianceManager.set_kyc_status(
                    c, 'citizen', 'admin', 'pending', reason_code='review_started', now=invalid_epoch)),
                ("invalid_kyc_expiry_epoch", lambda c: ComplianceManager.expire_due_kyc_profiles(
                    c, 'admin', now=invalid_epoch)),
                ("invalid_aml_rule_epoch", lambda c: ComplianceManager.configure_aml_rule(
                    c, '미국', 'admin', now=invalid_epoch)),
                ("invalid_aml_monitor_epoch", lambda c: ComplianceManager.monitor_transfer(
                    c, user_id='citizen', nationality='미국', transaction_id=1,
                    source_units=1, source_currency='AAA', now=invalid_epoch)),
                ("invalid_aml_review_epoch", lambda c: ComplianceManager.review_aml_alert(
                    c, 1, 'admin', 'reviewing', 'further_review', now=invalid_epoch)),
            )
            for error_code, action in calls:
                with self.subTest(invalid_epoch=invalid_epoch, error_code=error_code):
                    with self.assertRaisesRegex(ValueError, error_code):
                        with self.db.atomic() as conn:
                            action(conn)
        self.assertEqual(self.db.system.execute_one(
            "SELECT COUNT(*) FROM kyc_profile_events"
        )[0], before["kyc"])
        self.assertEqual(self.db.currency.execute_one(
            "SELECT COUNT(*) FROM aml_alerts"
        )[0], before["aml"])
        self.assertEqual(self.db.system.execute_one(
            "SELECT COUNT(*) FROM admin_audit"
        )[0], before["audit"])

    def test_aml_thresholds_reject_values_not_representable_in_storage(self):
        self.configure()
        for amount in (Decimal('1e10000'), Decimal('1e-10000')):
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(ValueError, '저장 가능한 유한 금액'):
                    with self.db.atomic() as conn:
                        ComplianceManager.configure_aml_rule(
                            conn, '미국', 'admin', large_transaction_limit=amount,
                            enabled=True,
                        )
        with self.assertRaisesRegex(ValueError, '거래 횟수 기준'):
            with self.db.atomic() as conn:
                ComplianceManager.configure_aml_rule(
                    conn, '미국', 'admin', rolling_24h_count_limit=10**1000,
                    enabled=True,
                )
        self.assertIsNone(self.db.currency.execute_one(
            "SELECT 1 FROM aml_monitoring_rules WHERE nationality='미국'"
        ))

    def test_kyc_expiry_must_be_a_supported_future_epoch(self):
        self.configure()
        before_events = self.db.system.execute_one(
            "SELECT COUNT(*) FROM kyc_profile_events"
        )[0]
        for invalid_expiry in (-1, float('nan'), float('inf'), 1e300, True, 'invalid'):
            with self.subTest(invalid_expiry=invalid_expiry):
                with self.assertRaisesRegex(ValueError, 'invalid_kyc_expiry_epoch'):
                    with self.db.atomic() as conn:
                        ComplianceManager.set_kyc_status(
                            conn, 'citizen', 'admin', 'verified',
                            reason_code='requirements_met', expires_epoch=invalid_expiry,
                        )
        self.assertIsNone(self.db.system.execute_one(
            "SELECT 1 FROM kyc_profiles WHERE user_id='citizen'"
        ))
        self.assertEqual(self.db.system.execute_one(
            "SELECT COUNT(*) FROM kyc_profile_events"
        )[0], before_events)
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn, 'citizen', 'admin', 'verified',
                reason_code='requirements_met', expires_epoch=253402300000,
                now=1000,
            )
        self.db.system.execute(
            "UPDATE kyc_profiles SET expires_epoch=253402300800 WHERE user_id='citizen'"
        )
        kyc_view = self.bot.user_mgr.get_total_assets(
            'citizen', settle=False
        )['kyc_profile']
        self.assertEqual(kyc_view['status'], 'expired')
        self.assertIsNone(kyc_view['expires_epoch'])
        with self.assertRaisesRegex(ValueError, 'KYC 확인'):
            ComplianceManager.check_financial_eligibility(
                self.db, 'citizen', 'transfer', now=1000
            )
        denial = self.db.system.execute_one(
            "SELECT reason_code FROM kyc_profile_events WHERE event_type='transfer_denied'"
        )
        self.assertEqual(denial['reason_code'], 'kyc_incomplete_or_expired')

    def test_explicit_kyc_review_gates_outgoing_transfers_but_not_receiving(self):
        cmd=self.configure()
        cmd.execute('admin','기본소득','미국 100')
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'citizen','admin','pending',reason_code='review_started'
            )
        self.assertEqual(
            self.bot.user_mgr.get_total_assets('citizen',settle=False)['kyc_profile']['status'],
            'pending',
        )
        with self.assertRaisesRegex(ValueError,'KYC 확인'):
            self.bot.currency_mgr.transfer('citizen','admin','30')
        denial=self.db.system.execute_one(
            "SELECT actor_id,event_type,reason_code FROM kyc_profile_events "
            "WHERE user_id='citizen' ORDER BY event_id DESC LIMIT 1"
        )
        self.assertEqual(tuple(denial),('citizen','transfer_denied','kyc_incomplete_or_expired'))
        with self.assertRaisesRegex(Exception,'append-only'):
            self.db.system.execute(
                "UPDATE kyc_profile_events SET reason_code='edited' WHERE event_type='transfer_denied'"
            )
        received=self.bot.currency_mgr.transfer('admin','citizen','30')
        self.assertIn('30 NWX →',received)
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'citizen','admin','verified',reason_code='requirements_met'
            )
        result=self.bot.currency_mgr.transfer('citizen','admin','30')
        self.assertIn('30 AAA →',result)
        self.db.currency.execute(
            "UPDATE kyc_profiles SET expires_epoch=0 WHERE user_id='citizen'"
        )
        self.assertEqual(
            self.bot.user_mgr.get_total_assets('citizen',settle=False)['kyc_profile']['status'],
            'expired',
        )
        events_before_invalid_time = self.db.system.execute_one(
            "SELECT COUNT(*) FROM kyc_profile_events WHERE user_id='citizen'"
        )[0]
        with self.assertRaisesRegex(ValueError, 'invalid_kyc_check_epoch'):
            ComplianceManager.check_financial_eligibility(
                self.db, 'citizen', 'transfer', now=-1
            )
        self.assertEqual(self.db.system.execute_one(
            "SELECT COUNT(*) FROM kyc_profile_events WHERE user_id='citizen'"
        )[0], events_before_invalid_time)
        with self.assertRaisesRegex(ValueError,'KYC 확인'):
            self.bot.currency_mgr.transfer('citizen','admin','30')
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'citizen','admin','verified',risk_tier='restricted',
                reason_code='risk_escalation'
            )
        with self.assertRaisesRegex(ValueError,'제한 위험등급'):
            self.bot.currency_mgr.transfer('citizen','admin','30')
        restricted_denial=self.db.system.execute_one(
            "SELECT reason_code,new_risk_tier FROM kyc_profile_events "
            "WHERE user_id='citizen' AND event_type='transfer_denied' "
            "ORDER BY event_id DESC LIMIT 1"
        )
        self.assertEqual(tuple(restricted_denial),('risk_restricted','restricted'))
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'citizen','admin','verified',risk_tier='enhanced',
                reason_code='periodic_review'
            )
        self.assertIn('30 AAA →',self.bot.currency_mgr.transfer('citizen','admin','30'))

    def test_kyc_denial_audit_marks_profile_change_when_unexpired_without_expiry(self):
        self.configure()
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'citizen','admin','pending',reason_code='review_started',now=1
            )
        # The denial was detected on the pending profile, but an administrator
        # completed review before the separate immutable event write.
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'citizen','admin','verified',reason_code='requirements_met',now=2
            )
        with self.db.atomic() as conn:
            ComplianceManager.record_financial_action_denial(
                conn,'citizen','transfer',now=3
            )
        event=self.db.system.execute_one(
            "SELECT reason_code FROM kyc_profile_events "
            "WHERE user_id='citizen' AND event_type='transfer_denied'"
        )
        self.assertEqual(event['reason_code'],'eligibility_changed_during_check')

    def test_migration_is_idempotent_and_all_product_denominations_present(self):
        self.configure();before=self.db.users.get_user('citizen')['cash']
        fx=CurrencyDB();fx.close()
        self.assertEqual(self.db.users.get_user('citizen')['cash'],before)
        for table in ('company','investment','market_assets','saving','liability','treasury_bond_products','accounts','loans'):
            self.assertIn('quote_currency',{r['name'] for r in self.db.currency.execute(f'PRAGMA table_info({table})')})

    def test_dynamic_currency_quote_keeps_event_price_movement(self):
        cmd=self.configure()
        self.db.assets.register_asset('CUR_T','시세 외환','FX',100,'forex')
        self.db.currency.execute("INSERT INTO currencies(code,name,asset_code) VALUES ('TST','시세 화폐','CUR_T')")
        cmd.execute('admin','환율수정','NWX TST 3')
        with self.db.atomic() as conn:
            self.assertAlmostEqual(float(CurrencyManager.rate(conn,'NWX')/CurrencyManager.rate(conn,'TST')),3)
        self.db.assets.update_price('CUR_T',200)
        with self.db.atomic() as conn:
            self.assertAlmostEqual(float(CurrencyManager.rate(conn,'NWX')/CurrencyManager.rate(conn,'TST')),1.5)

    def test_currency_precision_controls_settlement_input_and_relative_text(self):
        cmd=self.configure()
        self.db.currency.execute("UPDATE currencies SET decimal_places=2 WHERE code='AAA'")
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('KRW','원','1/1000',0)")
        from functions.currency_display import MoneyDisplay
        display=MoneyDisplay(self.db,'citizen')
        self.assertEqual(display.relative(10),'30.00 AAA')
        self.assertEqual(display.text('현금 10 NWX · 외화 1.2 AAA'),'현금 30.00 AAA · 외화 1.20 AAA')
        cmd.execute('admin','잔고수정','citizen modify 10.55')
        with self.assertRaisesRegex(ValueError,'2자리'):
            cmd.execute('admin','잔고수정','citizen modify 10.555')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('KRW_SAVE','원 예금',0,'KRW','saving')")
        self.bot.currency_mgr.open_account('citizen','KRW_SAVE')
        self.bot.currency_mgr.exchange('citizen','AAA','KRW','0.01')
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','KRW'),3*SCALE)
        with self.assertRaisesRegex(ValueError,'0자리'):
            self.bot.currency_mgr.exchange('citizen','KRW','AAA','0.1')

    def test_native_stock_order_and_cancel_use_home_cash_not_nwx(self):
        cmd=self.configure();cmd.execute('admin','기본소득','미국 30000')
        # Exchange A belongs to A국, so A국's treasury sells the primary issue in its own AAA.
        self.db.system.execute("INSERT INTO market_exchanges(exchange_id,status,updated_epoch,trading_halted,nationality) "
                               "VALUES ('A','open',0,0,'A국') ON CONFLICT(exchange_id) DO UPDATE SET nationality='A국'")
        self.bot.admin_control.entity('admin','stock','create','AAA_ST','A국 주식',100,1000,{'quote_currency':'AAA','exchange_id':'A','nationality':'A국'})
        with self.db.atomic() as conn:
            a_treasury=CurrencyManager.treasury_id(conn,'A국')
            before=CurrencyManager.balance(conn,a_treasury,'AAA')
        ok,msg=self.bot.order_book.place_limit_order('citizen','AAA_ST','buy',5,100)
        self.assertTrue(ok,msg)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_029_500*SCALE)
            # Primary proceeds now go to the issuer's cash account, not the country treasury.
            self.assertEqual(CurrencyManager.balance(conn,a_treasury,'AAA')-before,0)
            self.assertEqual(conn.execute("SELECT balance FROM issuer_cash_accounts WHERE company_code='AAA_ST'").fetchone()[0],500)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_029_500)
        ok,msg=self.bot.order_book.place_limit_order('citizen','AAA_ST','buy',5,50)
        self.assertTrue(ok,msg)
        order=self.db.assets.execute_one("SELECT order_id FROM market_orders WHERE user_id='citizen' AND status='open'")[0]
        ok,msg=self.bot.order_book.cancel_order('citizen',order)
        self.assertTrue(ok,msg)
        self.assertIn('AAA',msg)
        with self.db.atomic() as conn:self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_029_500*SCALE)

    def test_personal_response_and_pagination_display_relative_currency(self):
        self.configure()
        from functions.currency_display import MoneyDisplay
        from functions.slash_commands import SlashCommandRegistry
        from functions.discord_views import AllAssetsView
        from unittest.mock import AsyncMock
        display=MoneyDisplay(self.db,'citizen')
        response=SimpleNamespace(is_done=lambda:False,send_message=AsyncMock())
        interaction=SimpleNamespace(response=response,extras={'money_display':display},command=SimpleNamespace(name='대출상환'))
        embed=discord.Embed(description='현금 100 NWX')
        asyncio.run(SlashCommandRegistry._reply(interaction,embed=embed))
        shown=response.send_message.call_args.kwargs['embed']
        # Contract details retain their actual denomination; summaries convert explicitly.
        self.assertEqual(shown.description,'현금 100 NWX')
        from functions.currency_display import relative_embed
        class Page:
            money_display=display
            @relative_embed
            def build_embed(self):return discord.Embed(description='평가액 100 NWX')
        self.assertEqual(Page().build_embed().description,'평가액 300.00 AAA')

    def test_native_currency_deposit_is_a_home_account_not_foreign_account(self):
        self.configure()
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('A_SAVE','A국 예금',0,'AAA','saving')")
        with self.assertRaisesRegex(ValueError,'국적 통화'):
            self.bot.currency_mgr.open_account('citizen','A_SAVE')
        self.db.products.register_saving('HOME_SAVE','국적 통화 예금',0,0,0,86400,0,'Bank')
        self.db.products.execute("UPDATE saving SET quote_currency='AAA' WHERE product_code='HOME_SAVE'")
        self.assertTrue(self.bot.bank_mgr.open_account('citizen','HOME_SAVE','pass123',100)[0])
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['home_account_total_home'],100)
        self.assertEqual(info['foreign_account_total_home'],0)
        embed=discord.Embed();embed.add_field(name='🏦 통장 잔액',value='```0 NWX```')
        native_summary(embed,info)
        self.assertEqual(embed.fields[0].value,'```100.00 AAA```')

    def test_product_edit_treats_foreign_currency_savings_as_savings(self):
        cmd=self.configure()
        cmd.execute('admin','상품생성','SV saving "새 적금" 3 1000 30 86400 1 Inazina')
        self.assertIn('✅',cmd.execute('admin','상품수정','SV saving "새 적금" 4 1000 30 86400 1 Inazina'))
        self.assertEqual(self.db.products.execute_one("SELECT interest_rate FROM saving WHERE product_code='SV'")[0],4)
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('FX_SV','A 외환통장',1,'AAA','saving')")
        with self.db.atomic() as conn:
            self.assertEqual(cmd.locate(conn,'SV')[0],'saving')
            # A foreign-currency savings product is an ordinary savings product.
            self.assertEqual(cmd.locate(conn,'FX_SV')[0],'saving')

if __name__=='__main__':unittest.main()
