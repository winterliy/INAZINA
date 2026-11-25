"""Slash command registration.

Every command is registered per server: guild commands update at once and are not duplicated. Exactly one command stays global, because
Discord gives an app the "Supports Commands" badge only when it has a global command. That one is ``/약관`` (terms and privacy policy
links), which no server has as a guild copy, so nobody sees it twice; it also keeps the policy links one command away, as Discord asks.
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
    """Register every command per server and keep only ``global_commands`` global. Returns (guild commands synced, global commands)."""
    if not guilds:
        # Without any server to copy to, everything stays global.
        for command in global_commands:
            tree.add_command(command)
        return 0, len(await tree.sync())
    synced = 0
    for guild in guilds:
        tree.copy_global_to(guild=guild)
        synced += len(await tree.sync(guild=guild))
    tree.clear_commands(guild=None)  # the guild copies replace the global ones
    for command in global_commands:
        tree.add_command(command)
    return synced, len(await tree.sync())
