"""Slash command registration.

Every command is global: Discord lists an app's global commands on its profile (the "명령어" box) and gives it the "Supports Commands"
badge, so the profile shows them all. The bot used to register everything per server and keep only ``/약관`` global; those per-server copies
are removed here so nobody sees a command twice. Commands registered per server never worked in DMs, so every command except ``/약관``
(terms and privacy links, which Discord asks to keep one command away) is marked guild-only to keep that behaviour.
"""
import discord
from discord import app_commands

from functions.public_links import PRIVACY_URL, TERMS_URL


def policy_command():
    @app_commands.command(name="약관", description="이용약관과 개인정보 처리방침을 확인합니다")
    async def policy(interaction: discord.Interaction):
        embed = discord.Embed(
            title="📜 이용약관과 개인정보 처리방침",
            description="INAZINA의 이용약관과 개인정보 처리방침은 아래 버튼에서 로그인 없이 볼 수 있습니다. "
                        "삭제·탈퇴는 나루 또는 콘솔의 ‘내 계정’에서 할 수 있습니다.",
            color=discord.Color.blurple())
        view = discord.ui.View(timeout=180)
        view.add_item(discord.ui.Button(label="이용약관", style=discord.ButtonStyle.link, url=TERMS_URL, emoji="📜"))
        view.add_item(discord.ui.Button(label="개인정보 처리방침", style=discord.ButtonStyle.link, url=PRIVACY_URL, emoji="🔒"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    return policy


async def sync_slash_commands(tree, guilds, global_commands):
    """Make every command global and clear the old per-server copies. Returns (server copies cleared, global commands)."""
    known = {command.name for command in tree.get_commands()}
    for command in global_commands:
        if command.name not in known:
            tree.add_command(command)
    only_anywhere = {command.name for command in global_commands}
    for command in tree.get_commands():
        if command.name not in only_anywhere:
            command.guild_only = True
    global_count = len(await tree.sync())  # global first, so there is never a moment without the commands
    cleared = 0
    for guild in guilds:
        tree.clear_commands(guild=guild)
        await tree.sync(guild=guild)  # an empty set replaces (deletes) the copies registered earlier
        cleared += 1
    return cleared, global_count
