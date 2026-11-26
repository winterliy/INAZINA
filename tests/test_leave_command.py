"""The /탈퇴 command: one modal asks once, and typing the confirmation withdraws the member (the same withdrawal as the web button)."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_inazina as fixtures
from functions.discord_views import WithdrawalModal
from functions.slash_commands import SlashCommandRegistry
from managers.withdrawal_manager import WithdrawalManager


class FakeResponse:
    def __init__(self):
        self.modal = None
        self.message = None
        self.deferred = None

    async def send_modal(self, modal):
        self.modal = modal

    async def send_message(self, *args, **kwargs):
        self.message = {"args": args, **kwargs}

    async def defer(self, **kwargs):
        self.deferred = kwargs


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, **kwargs})


def interaction_of(user_id):
    return SimpleNamespace(response=FakeResponse(), followup=FakeFollowup(), user=SimpleNamespace(id=user_id, display_name=user_id))


def text_of(payload):
    parts = [str(arg) for arg in payload.get("args", ())] + [str(payload.get("content") or "")]
    embed = payload.get("embed")
    if embed is not None:
        parts += [str(embed.title or ""), str(embed.description or "")]
    return " ".join(parts)


class LeaveCommandTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def test_the_command_is_registered(self):
        class Tree:
            def __init__(self):
                self.commands = []

            def add_command(self, command):
                self.commands.append(command)

        tree = Tree()
        SlashCommandRegistry(SimpleNamespace(tree=tree)).register()
        names = {command.name: command for command in tree.commands}
        self.assertIn("탈퇴", names)
        self.assertLessEqual(len(names["탈퇴"].description), 100)

    def test_a_member_gets_the_modal_as_the_first_response_and_nothing_happens_yet(self):
        self.seed()
        interaction = interaction_of("citizen")
        asyncio.run(SlashCommandRegistry(self.bot).leave_service(interaction))
        self.assertIsInstance(interaction.response.modal, WithdrawalModal)
        self.assertIsNone(interaction.response.message)
        self.assertIsNotNone(self.db.users.get_user("citizen"), "asking is not leaving")

    def test_someone_who_is_not_a_member_gets_a_message_instead_of_a_modal(self):
        self.seed()
        interaction = interaction_of("nobody-here")
        asyncio.run(SlashCommandRegistry(self.bot).leave_service(interaction))
        self.assertIsNone(interaction.response.modal)
        self.assertIn("❌", text_of(interaction.response.message))

    def test_leftovers_that_block_a_withdrawal_are_listed_and_no_modal_opens(self):
        self.seed()
        plan = {"treasuryId": "t", "nationality": "낙원", "blockers": ["선물 포지션", "옵션 계약"], "holdings": 0, "accounts": 0, "loans": 0, "openOrders": 0}
        interaction = interaction_of("citizen")
        with patch.object(WithdrawalManager, "plan", return_value=plan):
            asyncio.run(SlashCommandRegistry(self.bot).leave_service(interaction))
        self.assertIsNone(interaction.response.modal)
        shown = text_of(interaction.response.message)
        self.assertIn("선물 포지션", shown)
        self.assertIn("옵션 계약", shown)
        self.assertIsNotNone(self.db.users.get_user("citizen"))

    def test_the_modal_says_what_happens_and_asks_for_the_confirmation_word(self):
        self.seed()
        plan = WithdrawalManager(self.bot).plan("citizen")
        modal = WithdrawalModal(self.bot, "citizen", plan)
        shown = modal.notice_display.content
        self.assertIn("되돌릴 수 없", shown)
        self.assertIn(str(plan["nationality"]), shown, "names the treasury the belongings go to")
        self.assertIn("탈퇴", modal.confirmation_field.text)

    def test_a_wrong_confirmation_does_not_withdraw(self):
        self.seed()
        modal = WithdrawalModal(self.bot, "citizen", WithdrawalManager(self.bot).plan("citizen"))
        modal.confirmation_input._value = "취소"
        interaction = interaction_of("citizen")
        asyncio.run(modal.on_submit(interaction))
        self.assertIsNotNone(self.db.users.get_user("citizen"))
        self.assertIn("탈퇴하지 않았습니다", text_of(interaction.response.message))

    def test_the_right_confirmation_withdraws_and_says_so_privately(self):
        self.seed()
        modal = WithdrawalModal(self.bot, "citizen", WithdrawalManager(self.bot).plan("citizen"))
        modal.confirmation_input._value = " 탈퇴 "
        interaction = interaction_of("citizen")
        asyncio.run(modal.on_submit(interaction))
        self.assertIsNone(self.db.users.get_user("citizen"), "the Discord ID is gone from the members")
        self.assertTrue(interaction.response.deferred["ephemeral"])
        sent = interaction.followup.sent[-1]
        self.assertTrue(sent["ephemeral"])
        self.assertIn("탈퇴가 완료", text_of(sent))
        self.assertIn("/등록", text_of(sent), "tells how to come back")

    def test_a_failure_while_withdrawing_is_reported_and_keeps_the_member(self):
        self.seed()
        modal = WithdrawalModal(self.bot, "citizen", WithdrawalManager(self.bot).plan("citizen"))
        modal.confirmation_input._value = "탈퇴"
        interaction = interaction_of("citizen")
        with patch.object(WithdrawalManager, "withdraw", side_effect=ValueError("선물 포지션이(가) 남아 있어 탈퇴할 수 없습니다.")):
            asyncio.run(modal.on_submit(interaction))
        self.assertIsNotNone(self.db.users.get_user("citizen"))
        self.assertIn("선물 포지션", text_of(interaction.followup.sent[-1]))


if __name__ == "__main__":
    unittest.main()
