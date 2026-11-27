"""Every command is global, so the app's profile lists all of them (and it earns the 'Supports Commands' badge). The per-server copies the bot
used to register are removed, so nobody sees a command twice, and every command except /약관 stays usable only inside a server."""
import asyncio
import unittest
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from functions.command_sync import policy_command, sync_slash_commands


def build_tree():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    @tree.command(name="내자산", description="x")
    async def assets(interaction): ...

    @tree.command(name="도움말", description="x")
    async def help_(interaction): ...
    calls = []

    async def fake_sync(*, guild=None):
        calls.append((guild.id if guild else None, sorted(command.name for command in tree.get_commands(guild=guild))))
        return tree.get_commands(guild=guild)
    tree.sync = fake_sync
    return tree, calls


class CommandSyncTests(unittest.TestCase):
    def test_every_command_is_global_and_the_old_server_copies_are_cleared_afterwards(self):
        tree, calls = build_tree()
        servers = [discord.Object(id=1), discord.Object(id=2)]
        asyncio.run(sync_slash_commands(tree, servers, [policy_command()]))
        self.assertEqual(calls[0], (None, ["내자산", "도움말", "약관"]), "global first, so there is never a moment without the commands")
        self.assertEqual(calls[1], (1, []))
        self.assertEqual(calls[2], (2, []))
        self.assertEqual(len(calls), 3)

    def test_without_any_server_everything_is_still_global(self):
        tree, calls = build_tree()
        asyncio.run(sync_slash_commands(tree, [], [policy_command()]))
        self.assertEqual(calls, [(None, ["내자산", "도움말", "약관"])])

    def test_only_the_policy_command_works_outside_a_server(self):
        tree, _ = build_tree()
        asyncio.run(sync_slash_commands(tree, [discord.Object(id=1)], [policy_command()]))
        by_name = {command.name: command for command in tree.get_commands()}
        self.assertTrue(by_name["내자산"].guild_only)
        self.assertTrue(by_name["도움말"].guild_only)
        self.assertFalse(by_name["약관"].guild_only)

    def test_running_it_twice_does_not_register_the_policy_command_twice(self):
        tree, calls = build_tree()
        asyncio.run(sync_slash_commands(tree, [], [policy_command()]))
        asyncio.run(sync_slash_commands(tree, [], [policy_command()]))
        self.assertEqual(calls[-1], (None, ["내자산", "도움말", "약관"]))

    def test_it_reports_how_many_server_copies_were_cleared_and_how_many_commands_are_global(self):
        tree, _ = build_tree()
        result = asyncio.run(sync_slash_commands(tree, [discord.Object(id=1), discord.Object(id=2)], [policy_command()]))
        self.assertEqual(result, (2, 3))

    def test_the_policy_command_answers_anyone_privately_with_links(self):
        command = policy_command()
        self.assertEqual(command.name, "약관")
        interaction = type("I", (), {"response": type("R", (), {"send_message": AsyncMock()})()})()
        asyncio.run(command.callback(interaction))
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("개인정보", kwargs["embed"].description)
        self.assertTrue(any(getattr(child, "url", None) for child in kwargs["view"].children), "a link button to the documents")


if __name__ == "__main__":
    unittest.main()
