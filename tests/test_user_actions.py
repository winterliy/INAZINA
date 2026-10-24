import re
import sqlite3
import unittest
from pathlib import Path

from database.user_db import TREASURY_USER_ID, citizen_filter
from functions import user_actions

# Discord-only server/onboarding commands and views the HTS already has (search, chart, wallet).
DISCORD_ONLY = {"설치", "등록", "서버등록", "서버탈퇴", "도움말", "검색", "그래프", "내지갑", "귀화신청"}
# Offered in the HTS through its own DR issue/redeem panel, not the command list.
HTS_PANEL = {"dr전환"}


class UserActionTests(unittest.TestCase):
    def test_hts_offers_every_discord_user_command(self):
        source = Path("functions/slash_commands.py").read_text(encoding="utf-8")
        definitions = source[source.index("definitions = ("):source.index("for name, description, callback in definitions")]
        slash = set(re.findall(r'\(\s*"([^"]+)",\s*"', definitions))
        offered = {item["name"] for item in user_actions.catalog()}
        self.assertEqual(slash - DISCORD_ONLY - HTS_PANEL, offered - {"주문취소"})

    def test_values_are_validated_before_running(self):
        with self.assertRaisesRegex(ValueError, "알 수 없는"):
            user_actions.parse_values("관리자", {})
        with self.assertRaisesRegex(ValueError, "종목"):
            user_actions.parse_values("매수", {"asset": " ", "quantity": "1"})
        with self.assertRaisesRegex(ValueError, "입력값"):
            user_actions.parse_values("매수", {"asset": "A", "quantity": "1", "extra": "x"})
        with self.assertRaisesRegex(ValueError, "유형"):
            user_actions.parse_values("옵션작성", {"underlying": "A", "type": "swap", "strike": "1", "days": "1", "contracts": "1", "premium": "1"})
        values = user_actions.parse_values("송금", {"recipient": " B ", "amount": "5"})
        self.assertEqual(values, {"recipient": "B", "amount": "5", "source": ""})

    def test_citizen_filter_excludes_treasuries_and_country_accounts(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(f"""
            CREATE TABLE users (user_id TEXT PRIMARY KEY, role TEXT);
            CREATE TABLE country_currencies (nationality TEXT PRIMARY KEY);
            CREATE TABLE country_treasuries (nationality TEXT, user_id TEXT);
            INSERT INTO country_currencies VALUES ('낙원'), ('A국');
            INSERT INTO country_treasuries VALUES ('A국', 'treasury-a');
            INSERT INTO users VALUES ('{TREASURY_USER_ID}', 'nation'), ('treasury-a', 'user'),
                ('낙원', 'nation'), ('A국', 'user'), ('legacy-nation', 'nation'), ('citizen', 'user'), ('admin', 'admin');
        """)
        rows = connection.execute(f"SELECT user_id FROM users WHERE {citizen_filter()} ORDER BY user_id").fetchall()
        self.assertEqual([row[0] for row in rows], ["admin", "citizen"])


if __name__ == "__main__":
    unittest.main()
