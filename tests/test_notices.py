"""Everything the bot says on Discord is a decorated embed: a colour and title that follow the leading emoji, the text, a footer."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from functions.notices import notice_embed, notice_kwargs


class NoticeEmbedTests(unittest.TestCase):
    def test_the_leading_emoji_decides_colour_and_title_and_is_not_repeated_in_the_text(self):
        for text, title, colour in (
            ("✅ 주문이 접수되었습니다.", "✅ 완료", discord.Color.green()),
            ("❌ 잔액이 부족합니다.", "❌ 실패", discord.Color.red()),
            ("⛔ 먼저 `/등록`을 실행해주세요.", "⛔ 제한", discord.Color.red()),
            ("⚠️ DM을 보내지 못했습니다.", "⚠️ 주의", discord.Color.orange()),
        ):
            embed = notice_embed(text)
            self.assertEqual((embed.title, embed.color), (title, colour), text)
            self.assertEqual(embed.description, text.split(" ", 1)[1], text)
            self.assertEqual(embed.footer.text, "INAZINA")

    def test_text_without_an_emoji_is_a_plain_information_notice(self):
        embed = notice_embed("최근 24시간 주문과 미체결 주문이 없습니다.")
        self.assertEqual((embed.title, embed.color), ("ℹ️ 안내", discord.Color.blurple()))
        self.assertEqual(embed.description, "최근 24시간 주문과 미체결 주문이 없습니다.")

    def test_a_very_long_text_still_fits_an_embed(self):
        embed = notice_embed("❌ " + "가" * 6000)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertTrue(embed.description.endswith("…"))

    def test_plain_content_is_moved_into_the_embed_but_a_message_that_already_has_one_is_left_alone(self):
        converted = notice_kwargs({"content": "✅ 완료했습니다.", "ephemeral": True})
        self.assertIsNone(converted["content"])
        self.assertEqual(converted["embed"].description, "완료했습니다.")
        self.assertTrue(converted["ephemeral"])
        own = discord.Embed(title="내 자산")
        kept = notice_kwargs({"content": "머리말", "embed": own})
        self.assertIs(kept["embed"], own)
        self.assertEqual(kept["content"], "머리말")
        self.assertEqual(notice_kwargs({"view": None}), {"view": None}, "nothing to say, nothing to change")


class ReplyUsesEmbedsTests(unittest.TestCase):
    def test_a_slash_command_reply_goes_out_as_an_embed(self):
        from functions.slash_commands import SlashCommandRegistry
        sent = []

        class Response:
            def is_done(self):
                return False

            async def send_message(self, **kwargs):
                sent.append(kwargs)

        interaction = SimpleNamespace(response=Response(), followup=SimpleNamespace(send=AsyncMock()), extras={})
        asyncio.run(SlashCommandRegistry._reply(interaction, content="❌ 상환할 대출이 없습니다."))
        self.assertIsNone(sent[0].get("content"))
        self.assertEqual(sent[0]["embed"].description, "상환할 대출이 없습니다.")
        self.assertTrue(sent[0]["ephemeral"])


class InstalledSendersTests(unittest.TestCase):
    """The library's senders are wrapped once, so any plain text that slips through still goes out decorated."""

    def wrapped(self, **options):
        from functions.notices import wrap_sender
        seen = {}

        async def original(self_, *args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
        return wrap_sender(original, **options), seen

    def run_sender(self, sender, *args, **kwargs):
        return asyncio.run(sender(object(), *args, **kwargs))

    def test_a_positional_or_keyword_text_becomes_an_embed(self):
        sender, seen = self.wrapped()
        self.run_sender(sender, "❌ 오류가 났습니다.", ephemeral=True)
        self.assertEqual(seen["args"], ())
        self.assertEqual(seen["kwargs"]["embed"].description, "오류가 났습니다.")
        self.assertIsNone(seen["kwargs"]["content"])
        self.assertTrue(seen["kwargs"]["ephemeral"])
        self.run_sender(sender, content="안내합니다.", view="V")
        self.assertEqual(seen["kwargs"]["embed"].title, "ℹ️ 안내")
        self.assertEqual(seen["kwargs"]["view"], "V")

    def test_what_already_has_an_embed_or_pings_someone_is_left_alone(self):
        sender, seen = self.wrapped()
        own = discord.Embed(title="x")
        self.run_sender(sender, content="머리말", embed=own)
        self.assertEqual((seen["kwargs"]["content"], seen["kwargs"]["embed"]), ("머리말", own))
        self.run_sender(sender, content="<@123> 님, 확인해 주세요.")
        self.assertEqual(seen["kwargs"]["content"], "<@123> 님, 확인해 주세요.", "a mention only pings in plain content")
        self.assertNotIn("embed", seen["kwargs"])

    def test_an_edit_only_turns_into_an_embed_when_it_clears_the_old_embed(self):
        sender, seen = self.wrapped(edit=True)
        self.run_sender(sender, content="⏰ 시간이 지났습니다.", view=None)                     # keeps the message's embed: untouched
        self.assertEqual(seen["kwargs"]["content"], "⏰ 시간이 지났습니다.")
        self.assertNotIn("embed", seen["kwargs"])
        self.run_sender(sender, content="✅ 완료했습니다.", embed=None, view=None)             # replaces the old embed: decorated
        self.assertEqual(seen["kwargs"]["embed"].description, "완료했습니다.")
        self.assertIsNone(seen["kwargs"]["content"])


class InstallTests(unittest.TestCase):
    def test_installing_wraps_the_real_senders_once(self):
        from functions import notices
        notices.install()
        notices.install()  # idempotent: never wrapped twice
        for owner, name in ((discord.InteractionResponse, "send_message"), (discord.InteractionResponse, "edit_message"),
                            (discord.Webhook, "send"), (discord.abc.Messageable, "send")):
            sender = getattr(owner, name)
            self.assertTrue(getattr(sender, "__notice_wrapped__", False), f"{owner.__name__}.{name}")
            self.assertFalse(getattr(sender.__wrapped__, "__notice_wrapped__", False), "not wrapped a second time")


if __name__ == "__main__":
    unittest.main()
