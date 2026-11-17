"""The bot records the bell notification first, so a person Discord cannot reach still has it."""
import asyncio
import tempfile
import types
import unittest
from pathlib import Path

import discord

from database import DatabaseManager
from database.db_manager import DBManager
from main import InazinaBot
from managers.notification_manager import NotificationManager


class _Unreachable:
    async def send(self, **_):
        raise discord.HTTPException(types.SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")


class BotNotifyTests(unittest.TestCase):
    def setUp(self):
        self.old = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user("123", "Alice", "낙원", 0)
        self.sent = []

        async def run(callback, *args):
            return callback(*args)

        async def fetch_user(user_id):
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "Unknown User")

        class Reachable:
            async def send(inner, **kwargs):
                self.sent.append(kwargs["embed"].title)

        self.bot = types.SimpleNamespace(
            db=self.db, _run_background_write=run, fetch_user=fetch_user,
            get_user=lambda user_id: Reachable() if int(user_id) == 123 else None)
        self.bot._record_notification = types.MethodType(InazinaBot._record_notification, self.bot)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old
        self.tmp.cleanup()

    def _notify(self, user_id, title="국채 만기 자동 상환", **kwargs):
        embed = discord.Embed(title=title, description="내용")
        asyncio.run(InazinaBot._notify(self.bot, user_id, embed, "bond_redemption", **kwargs))

    def _titles(self, user):
        with self.db.readonly() as conn:
            return [n["title"] for n in NotificationManager.list(conn, user)]

    def test_a_reachable_user_gets_the_bell_and_the_dm(self):
        self._notify(123)
        self.assertEqual(self._titles("123"), ["국채 만기 자동 상환"])
        self.assertEqual(self.sent, ["국채 만기 자동 상환"])

    def test_a_user_discord_cannot_find_still_gets_the_bell(self):
        self.db.users.create_user("456", "Gone", "낙원", 0)
        self._notify(456)                                     # fetch_user raises NotFound
        self.assertEqual(self._titles("456"), ["국채 만기 자동 상환"])
        self.assertEqual(self.sent, [])

    def test_a_user_whose_dms_are_closed_still_gets_the_bell(self):
        self._notify(456, recipient=_Unreachable())
        self.assertEqual(self._titles("456"), ["국채 만기 자동 상환"])


if __name__ == "__main__":
    unittest.main()
