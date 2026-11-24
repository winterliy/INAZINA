"""The direct message that asks a member to agree to an unfavourable revision, and the button that records the answer.

The button is a dynamic item: its custom_id (``consent:<campaign>``) carries everything needed, so it keeps working after the bot restarts
and one registration serves every campaign (``bot.add_dynamic_items(ConsentButton)``).
"""
import datetime
import zoneinfo

import discord

from managers.document_consent import DocumentConsent

KST = zoneinfo.ZoneInfo("Asia/Seoul")
GUIDE_URL = "https://dramatic-bread-b4a.notion.site/NAKWON-BOTS-3ea27b50c7bf80e0904ef783e0e5dfc4"  # the bot guide (도움말), which links the full documents


def _when(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), KST).strftime("%Y년 %m월 %d일 %H:%M")


def consent_embed(item):
    description = (
        f"이용자에게 불리하거나 중요한 변경이 있는 개정안입니다. **{_when(item['effective_epoch'])}(한국시간)**부터 적용됩니다.\n\n"
        f"**변경된 부분**\n{item.get('summary') or '변경된 내용은 도움말의 약관 문서에서 확인할 수 있습니다.'}\n\n"
        "아래 버튼으로 동의해 주세요. 시행일까지 동의하지 않으면, 시행 후에는 동의하거나 탈퇴할 때까지 서비스 이용이 제한됩니다. "
        "전체 문서는 도움말에서 볼 수 있습니다.")
    return discord.Embed(title=f"📢 {item['title']} 개정 동의 요청", description=description[:4096], color=discord.Color.orange())


def consent_view(campaign_id):
    view = discord.ui.View(timeout=None)
    view.add_item(ConsentButton(campaign_id))
    view.add_item(discord.ui.Button(label="도움말 보기", style=discord.ButtonStyle.link, url=GUIDE_URL, emoji="📚"))
    return view


class ConsentButton(discord.ui.DynamicItem[discord.ui.Button], template=r"consent:(?P<campaign>\d+)"):
    def __init__(self, campaign_id):
        super().__init__(discord.ui.Button(label="동의합니다", style=discord.ButtonStyle.success, custom_id=f"consent:{int(campaign_id)}", emoji="✅"))
        self.campaign_id = int(campaign_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["campaign"]))

    async def callback(self, interaction):
        bot, user_id = interaction.client, str(interaction.user.id)

        def record():
            with bot.db.atomic() as conn:
                return DocumentConsent.consent(conn, self.campaign_id, user_id)
        outcome = await bot._run_background_write(record)
        if outcome == "unknown":
            await interaction.response.send_message("이미 종료되었거나 동의를 요청받지 않은 개정안입니다.", ephemeral=True)
            return
        embeds = list(interaction.message.embeds) if interaction.message else []
        await interaction.response.edit_message(
            content="✅ 동의가 기록되었습니다. 감사합니다." if outcome == "consented" else "✅ 이미 동의하셨습니다.", embeds=embeds, view=None)


async def deliver_consent_requests(bot, limit=20):
    """Send each pending consent request as a DM. A member whose DMs are closed is marked failed (the web and the command gate still ask)."""
    def pending():
        with bot.db.atomic() as conn:
            return DocumentConsent.pending_dm(conn, limit)

    def mark(item, ok):
        with bot.db.atomic() as conn:
            DocumentConsent.mark_dm(conn, item["campaign_id"], item["user_id"], ok)

    for item in await bot._run_background_write(pending):
        ok = False
        try:
            user_id = int(item["user_id"])
            recipient = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await recipient.send(embed=consent_embed(item), view=consent_view(item["campaign_id"]))
            ok = True
        except (discord.HTTPException, ValueError, TypeError):
            pass
        await bot._run_background_write(mark, item, ok)
