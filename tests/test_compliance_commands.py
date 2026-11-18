"""KYC/AML are driven by admin commands now that the dashboard tab is gone."""
import unittest

import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager


class ComplianceCommandTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','A국 화폐','1')")
        cmd = AdminCommandsManager(self.bot)
        cmd.execute('admin', '서버설치', '123 A국 AAA')
        cmd.execute('admin', '서버설치', '낙원 NWX', guild_id=456)
        cmd.execute('admin', '환율수정', 'NWX AAA 3')
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','AAA')")
        self.db.currency.execute("INSERT OR IGNORE INTO currency_liquidity_state(currency_code) SELECT code FROM currencies")
        self.db.currency.execute('UPDATE currency_liquidity_state SET issuance_elasticity=0,circulation_elasticity=0')
        self.db.users.create_user('recipient', '수취인', '미국', 1000)
        cmd.execute('admin', '기본소득', '미국 100')
        self.db.system.set_setting('currency_exchange_fee_percent', '0')
        return cmd

    def test_restrict_blocks_outgoing_transfers_and_release_reopens_them(self):
        cmd = self.prepare()
        self.assertIn('프로필이 없습니다', cmd.execute('admin', 'KYC심사', '목록'))
        self.assertIn('송금', cmd.execute('admin', 'KYC심사', '제한 citizen'))
        with self.assertRaisesRegex(ValueError, '제한 위험등급'):
            self.bot.currency_mgr.transfer('citizen', 'recipient', '10')
        self.assertIn('citizen · verified · restricted', cmd.execute('admin', 'KYC심사', '목록'))
        cmd.execute('admin', 'KYC심사', '해제 citizen')
        self.bot.currency_mgr.transfer('citizen', 'recipient', '10')

    def test_set_accepts_korean_labels_and_rejects_unknown_ones(self):
        cmd = self.prepare()
        cmd.execute('admin', 'KYC심사', '설정 citizen 대기 강화 정기검토')
        self.assertIn('citizen · pending · enhanced', cmd.execute('admin', 'KYC심사', '목록'))
        with self.assertRaisesRegex(ValueError, '상태'):
            cmd.execute('admin', 'KYC심사', '설정 citizen 모름 일반')
        with self.assertRaisesRegex(ValueError, '사용법'):
            cmd.execute('admin', 'KYC심사', '설정 citizen')
        self.assertIn('0건', cmd.execute('admin', 'KYC심사', '만료처리'))

    def test_aml_rule_alert_review_and_close(self):
        cmd = self.prepare()
        self.assertIn('규칙 없음', cmd.execute('admin', 'AML경보', '규칙 미국'))
        self.assertIn('켰습니다', cmd.execute('admin', 'AML경보', '규칙 미국 설정 10 0 0'))
        self.bot.currency_mgr.transfer('citizen', 'recipient', '30')
        listing = cmd.execute('admin', 'AML경보', '목록')
        self.assertIn('large_transaction', listing)
        alert_id = listing.split()[0].lstrip('#')
        self.assertIn('검토 중', cmd.execute('admin', 'AML경보', f'검토 {alert_id}'))
        self.assertIn('종결', cmd.execute('admin', 'AML경보', f'종결 {alert_id} 오탐'))
        self.assertNotIn(f'#{alert_id} ', cmd.execute('admin', 'AML경보', '목록'))
        with self.assertRaisesRegex(ValueError, '종결된'):
            cmd.execute('admin', 'AML경보', f'검토 {alert_id}')
        self.assertIn('껐습니다', cmd.execute('admin', 'AML경보', '규칙 미국 끄기'))
        self.assertIn('비활성', cmd.execute('admin', 'AML경보', '규칙 미국'))

    def test_only_administrators_can_run_them(self):
        cmd = self.prepare()
        for command, args in (('KYC심사', '제한 recipient'), ('AML경보', '목록')):
            with self.assertRaises(Exception):
                cmd.execute('citizen', command, args)
        self.assertIn('프로필이 없습니다', cmd.execute('admin', 'KYC심사', '목록'))


if __name__ == '__main__':
    unittest.main()
