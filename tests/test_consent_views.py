"""The consent DM and its button: delivery marks each attempt, the button records the first agreement and survives restarts (dynamic item)."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import test_inazina as fixtures
from functions.consent_views import ConsentButton, consent_embed, consent_view, deliver_consent_requests
from managers.document_consent import DocumentConsent

NOW = 1_800_000_000


class FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeBot:
    def __init__(self, db, users):
        self.db, self.users = db, users

    async def _run_background_write(self, callback, *args):
        return callback(*args)

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_user(self, user_id):
        raise ValueError("unknown user")  # nobody outside the cache in these tests


class ConsentViewTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def campaign(self, *ids):
        with self.db.atomic() as conn:
            for user_id in ids:
                conn.execute("INSERT OR REPLACE INTO users(user_id,nickname,nationality,cash,status,role) VALUES (?,?,'낙원',0,'active','user')", (user_id, user_id))
            return DocumentConsent.open_campaign(conn, 'terms', '서비스 이용약관', 'abc', "＋ 제10조 탈퇴 시 재산은 국고로 귀속됩니다.\n－ 제10조 옛 문장", NOW + 30 * 86400, now=NOW)['campaignId']

    def test_the_message_names_the_document_the_date_and_the_changes(self):
        self.seed()
        campaign = self.campaign()
        with self.db.atomic() as conn:
            item = next(row for row in DocumentConsent.pending_dm(conn) if row['user_id'] == 'citizen')
        embed = consent_embed(item)
        self.assertIn("서비스 이용약관", embed.title)
        self.assertIn("제10조 탈퇴 시 재산은 국고로 귀속됩니다", embed.description)
        self.assertIn("2027", embed.description + embed.title, 'the date it takes effect (KST)')
        self.assertLessEqual(len(embed.description), 4096)
        buttons = [child for child in consent_view(campaign).children]
        self.assertTrue(any(getattr(child, 'custom_id', None) == f"consent:{campaign}" for child in buttons))

    def test_delivery_sends_to_reachable_members_and_marks_the_rest_failed(self):
        self.seed()
        campaign = self.campaign('111', '222')
        reachable = FakeUser()
        bot = FakeBot(self.db, {111: reachable})
        asyncio.run(deliver_consent_requests(bot))
        self.assertEqual(len(reachable.sent), 1)
        self.assertIn("embed", reachable.sent[0])
        self.assertIsNotNone(reachable.sent[0]["view"])
        with self.db.atomic() as conn:
            states = {row[0]: row[1] for row in conn.execute("SELECT user_id,dm_status FROM document_consents WHERE campaign_id=?", (campaign,))}
        self.assertEqual((states['111'], states['222'], states['citizen']), ('sent', 'failed', 'failed'))
        asyncio.run(deliver_consent_requests(bot))
        self.assertEqual(len(reachable.sent), 1, 'a delivered message is not sent again')

    def test_the_button_records_the_first_agreement_and_survives_a_restart(self):
        self.seed()
        campaign = self.campaign('111')
        self.assertTrue(ConsentButton.__discord_ui_compiled_template__.fullmatch(f"consent:{campaign}"), 'one pattern serves every campaign')
        interaction = SimpleNamespace(client=FakeBot(self.db, {}), user=SimpleNamespace(id=111), message=SimpleNamespace(embeds=[consent_embed({
            'title': '서비스 이용약관', 'summary': 's', 'effective_epoch': NOW})]),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        asyncio.run(ConsentButton(campaign).callback(interaction))
        interaction.response.edit_message.assert_awaited()
        self.assertIsNone(interaction.response.edit_message.await_args.kwargs.get("view"), 'the button is removed once answered')
        with self.db.atomic() as conn:
            self.assertEqual(conn.execute("SELECT consented_epoch IS NOT NULL FROM document_consents WHERE campaign_id=? AND user_id='111'", (campaign,)).fetchone()[0], 1)
        asyncio.run(ConsentButton(campaign).callback(interaction))  # pressed twice: harmless

    def test_someone_who_was_not_asked_gets_a_plain_answer(self):
        self.seed()
        campaign = self.campaign('111')
        interaction = SimpleNamespace(client=FakeBot(self.db, {}), user=SimpleNamespace(id=999), message=None,
                                      response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        asyncio.run(ConsentButton(campaign).callback(interaction))
        interaction.response.send_message.assert_awaited()
        interaction.response.edit_message.assert_not_awaited()


    def test_a_held_member_can_only_agree_or_ask_for_help(self):
        from functions.registration import RegistrationRequired
        from functions.slash_commands import SlashCommandRegistry
        self.seed()
        campaign = self.campaign('111')
        sent = []

        class Response:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **_):
                self.done = True

            async def send_message(self, **kwargs):
                self.done = True
                sent.append(kwargs)

        class Followup:
            async def send(self, **kwargs):
                sent.append(kwargs)

        async def ensure_user(_id, _nick):
            return self.db.users.get_user('111')

        bot = SimpleNamespace(ensure_user=ensure_user, db=self.db)

        def run(command):
            interaction = SimpleNamespace(response=Response(), followup=Followup(), user=SimpleNamespace(id=111, display_name='m', name='m'),
                                          command=SimpleNamespace(name=command))
            return asyncio.run(SlashCommandRegistry(bot)._prepare(interaction))

        # not in force yet: nothing is held
        self.assertEqual(run('내자산')[0], '111')
        with self.db.atomic() as conn:
            conn.execute("UPDATE document_consent_campaigns SET effective_epoch=1 WHERE campaign_id=?", (campaign,))
        with self.assertRaises(RegistrationRequired):
            run('내자산')
        self.assertIn("동의", sent[-1]["content"])
        self.assertIn("embed", sent[-1])
        self.assertTrue(any(getattr(child, 'custom_id', '') == f"consent:{campaign}" for child in sent[-1]["view"].children))
        self.assertEqual(run('도움말')[0], '111')
        self.assertEqual(run('등록')[0], '111')
        with self.db.atomic() as conn:
            DocumentConsent.consent(conn, campaign, '111')
        self.assertEqual(run('내자산')[0], '111', 'agreeing lifts the hold')

if __name__ == '__main__':
    unittest.main()
