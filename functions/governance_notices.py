"""Discord side of 나루 주주총회 and 회생·파산 (web_api/governance.py holds the records).

- Convening (from /주총소집 or 나루) DMs every holder with a vote the agenda, the deadline
  and 참석·불참 buttons that answer on the meeting itself.
- When a meeting ends (closed by its convener or when voting time runs out) every holder
  and the convener receive the minutes: attendance and each item's result.
- An administrator's decision on a 회생·파산 filing is sent to the applicant.
"""
import asyncio
import os
import time
from datetime import datetime

import discord

from functions.notices import notice_embed

from database.user_db import TREASURY_USER_ID
from web_api import governance

CHOICE = {"for": "찬성", "against": "반대", "abstain": "기권"}


def _where(path):
    base = os.environ.get("NARU_PUBLIC_URL", "").rstrip("/")
    return f"{base}{path}" if base else f"나루 › {'주주총회' if path == '/meetings' else '회생 · 파산'} 탭"


def _when(epoch):
    return f"<t:{int(epoch)}:F>"


def _votes(value):
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _db(bot):
    return bot.db.atomic(("system",))


def _read(bot, fn, *args):
    with _db(bot) as connection:
        governance.ensure_tables(connection)
        return fn(connection, *args)


async def _send(bot, user_id, **kwargs):
    try:
        target = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        return await target.send(allowed_mentions=discord.AllowedMentions.none(), **kwargs)
    except (discord.HTTPException, ValueError, TypeError):
        return None


class MeetingAttendanceView(discord.ui.View):
    """참석·불참 on the invitation, recorded on the meeting (votes are cast in 나루)."""

    def __init__(self, bot, meeting_id, holder_id):
        super().__init__(timeout=86400)
        self.bot, self.meeting_id, self.holder_id = bot, int(meeting_id), str(holder_id)

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.holder_id

    async def _answer(self, interaction, attendance):
        user = {"userId": self.holder_id, "role": "user"}
        try:
            await asyncio.to_thread(self._respond, user, attendance)
        except ValueError as exc:
            text = {"meeting_closed": "이미 끝난 주주총회입니다.", "not_a_holder": "이 주주총회의 의결권이 없습니다."}.get(str(exc), str(exc))
            return await interaction.response.send_message(f"❌ {text}", ephemeral=True)
        label = "참석" if attendance == "attend" else "불참"
        done = notice_embed(f"✅ `{label}`으로 응답했습니다. 의안 투표는 {_where('/meetings')}에서 할 수 있습니다.")
        await interaction.response.edit_message(content=None, embeds=[done, *interaction.message.embeds], view=None)

    def _respond(self, user, attendance):
        with _db(self.bot) as connection:
            governance.ensure_tables(connection)
            governance.respond(connection, user, self.meeting_id, {"attendance": attendance})

    @discord.ui.button(label="참석", style=discord.ButtonStyle.success)
    async def attend(self, interaction, _button):
        await self._answer(interaction, "attend")

    @discord.ui.button(label="불참", style=discord.ButtonStyle.secondary)
    async def absent(self, interaction, _button):
        await self._answer(interaction, "absent")


async def invite_holders(bot, meeting_id):
    """DM the agenda to every holder with a vote; returns a summary line."""
    meeting, holders = await asyncio.to_thread(
        lambda: (_read(bot, governance.meeting_for, meeting_id), _read(bot, governance.holder_ids, meeting_id)))
    embed = discord.Embed(title=f"📣 {meeting['company']} 주주총회 소집 통지", color=discord.Color.gold(),
                          description=f"{meeting['convener']} 님이 주주총회를 소집했습니다. 아래 버튼으로 참석 여부를 알려주세요.")
    embed.add_field(name="일시", value=_when(meeting["meetingEpoch"]), inline=True)
    embed.add_field(name="투표 마감", value=_when(meeting["closeEpoch"]), inline=True)
    embed.add_field(name="장소", value=meeting["place"][:1024], inline=False)
    embed.add_field(name="의안", value="\n".join(f"제{item['seq']}호 {item['title']}" for item in meeting["items"])[:1024], inline=False)
    embed.add_field(name="투표", value=f"의안별 찬성·반대·기권은 {_where('/meetings')}에서 마감 전까지 할 수 있습니다.", inline=False)
    sent = failed = 0
    for holder in holders:
        if not str(holder).isdecimal():
            continue
        message = await _send(bot, holder, embed=embed, view=MeetingAttendanceView(bot, meeting_id, holder))
        sent, failed = (sent + 1, failed) if message else (sent, failed + 1)
    return f"✅ 주주총회 소집 통지: {sent}명 전달 · {failed}명 실패"


def _convene(bot, user_id, role, code, place, agenda):
    user_row = bot.db.users.get_user(user_id)
    nickname = (user_row["nickname"] if user_row else None) or user_id
    items = [part.strip() for part in str(agenda or "").replace("\n", ";").split(";") if part.strip()] or ["일반 안건"]
    with _db(bot) as connection:
        governance.ensure_tables(connection)
        return governance.convene(connection, {"userId": user_id, "nickname": nickname, "role": role},
                                  {"code": code, "place": place, "items": items, "votingHours": 24}, TREASURY_USER_ID)


ERRORS = {"largest_holder_required": "해당 종목 최대주주(지배력 기준) 또는 관리자만 소집할 수 있습니다.",
          "preferred_no_vote": "우선주는 의결권이 없습니다. 보통주 코드로 소집해주세요.",
          "company_not_found": "회사 주식 코드를 찾을 수 없습니다.", "no_holders": "의결권이 있는 주주가 없습니다.",
          "items_required": "의안은 1~10개여야 합니다.", "item_title_required": "의안 제목을 확인해주세요.",
          "place_required": "장소를 입력해주세요."}


async def convene_from_discord(bot, user_id, role, code, place, agenda=None):
    """/주총소집: the same meeting record as 나루 (register frozen, agenda, votes), then the invitations."""
    try:
        result = await asyncio.to_thread(_convene, bot, str(user_id), role, str(code).strip().upper(), place, agenda)
    except ValueError as exc:
        return "❌ " + ERRORS.get(str(exc), str(exc))
    return await invite_holders(bot, result["meetingId"])


def _results_embed(meeting, viewer_vote=None):
    embed = discord.Embed(title=f"📜 {meeting['company']} 주주총회 결과", color=discord.Color.dark_teal(),
                          description=f"{_when(meeting['meetingEpoch'])} · {meeting['place'][:200]}")
    attended = sum(row["shares"] for row in meeting["attendees"])
    embed.add_field(name="참석", value=f"{len(meeting['attendees'])}/{meeting['holderCount']}명 · 의결권 {_votes(attended)}/{_votes(meeting['totalShares'])}표", inline=False)
    for item in meeting["items"][:20]:
        tally = item["tally"]
        mine = f"\n내 투표: {CHOICE[viewer_vote[item['itemId']]]}" if viewer_vote and viewer_vote.get(item["itemId"]) else ""
        embed.add_field(name=f"제{item['seq']}호 {item['title'][:200]} — {'✅ 가결' if item['passed'] else '❌ 부결'}",
                        value=f"찬성 {_votes(tally['for'])} · 반대 {_votes(tally['against'])} · 기권 {_votes(tally['abstain'])}{mine}", inline=False)
    embed.set_footer(text="회의록은 나루 주주총회 탭에서 다시 볼 수 있습니다.")
    return embed


def _pending_results(bot):
    with _db(bot) as connection:
        governance.ensure_tables(connection)
        governance.finish_expired(connection)
        out = []
        for meeting_id in governance.results_to_notify(connection):
            row = connection.execute("SELECT convener_id FROM shareholder_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
            recipients = {holder: governance.meeting_for(connection, meeting_id, holder) for holder in governance.holder_ids(connection, meeting_id)}
            recipients.setdefault(str(row[0]), governance.meeting_for(connection, meeting_id, str(row[0])))
            # Marked first: a failed DM is not retried on every tick.
            governance.mark_notified(connection, meeting_id)
            out.append(recipients)
        return out


async def send_meeting_results(bot):
    """Called from the bot's job loop: DM the minutes of every meeting that just ended."""
    for recipients in await asyncio.to_thread(_pending_results, bot):
        for user_id, meeting in recipients.items():
            if str(user_id).isdecimal():
                votes = {item["itemId"]: item["myVote"] for item in meeting["items"]}
                await _send(bot, user_id, embed=_results_embed(meeting, votes))


async def send_court_decision(bot, decided):
    color = {"approved": discord.Color.green(), "rejected": discord.Color.red()}.get(decided["status"], discord.Color.blurple())
    year = datetime.fromtimestamp(decided["createdEpoch"]).year
    case = f"{year}{'개회' if decided['kind'] == 'rehabilitation' else '하단'}{decided['filingId']}"
    embed = discord.Embed(title=f"⚖️ {decided['label']} 사건 {case} · {decided['statusLabel']}", color=color,
                          description=f"신청하신 {decided['label']} 사건의 진행 상태가 바뀌었습니다.")
    if decided.get("note"):
        embed.add_field(name="사유" if decided["status"] == "rejected" else "안내", value=decided["note"][:1024], inline=False)
    if decided.get("outcome"):
        embed.add_field(name="처리 결과", value=decided["outcome"][:1024], inline=False)
    embed.add_field(name="확인", value=_where("/court"), inline=False)
    return "✅ 신청자에게 DM을 보냈습니다." if await _send(bot, decided["userId"], embed=embed) else "⚠️ 신청자에게 DM을 보내지 못했습니다."
