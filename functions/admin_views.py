"""Administrator event form: all settings are entered in a single modal."""

import asyncio
import math

import discord

from functions.admin_access import resolve_user_role
from functions.admin_embeds import admin_response


class MarketEventModal(discord.ui.Modal):
    def __init__(self, bot, user_id, asset_code=None, duration_seconds=3600, target_price=None, *, assets=None):
        super().__init__(title="관리자 사건 설정", timeout=300)
        self.bot, self.user_id = bot, str(user_id)
        self.submitted = False
        if assets is None:
            from managers.market_manager import MarketManager
            market = getattr(bot, "market_mgr", None) or MarketManager(bot.db)
            assets = market.get_event_assets()
        self.assets = {asset["code"]: asset for asset in assets}
        self.asset_select = None
        self.asset_input = None
        if len(assets) <= 24:
            self.asset_select = discord.ui.Select(
                placeholder="사건을 적용할 자산을 선택하세요", min_values=1, max_values=1, required=True,
                options=[discord.SelectOption(label="시장 전체", value="__all__", default=asset_code is None)]
                    + [discord.SelectOption(label=f"{asset['name']} ({asset['code']})"[:100],
                        value=asset["code"], default=asset_code == asset["code"]) for asset in assets],
            )
            component = self.asset_select
            description = "현재 사건을 적용할 수 있는 자산만 표시합니다."
        else:
            # A modal select is limited to 25 choices. A text field keeps every
            # asset accessible when the market grows beyond that limit.
            self.asset_input = discord.ui.TextInput(
                placeholder="정확한 자산 코드 또는 이름 · 전체 적용은 ‘시장 전체’", max_length=100,
                default=asset_code or "시장 전체", required=True,
            )
            component = self.asset_input
            description = "자산이 25개 이상이면 코드나 이름으로 선택합니다."
        self.add_item(discord.ui.Label(text="자산 선택", description=description, component=component))

        self.duration_input = discord.ui.TextInput(
            placeholder="예: 60 (1~604800초)", default=str(duration_seconds), min_length=1, max_length=6,
        )
        self.add_item(discord.ui.Label(text="duration (초)", component=self.duration_input))
        self.kind_select = discord.ui.Select(
            placeholder="호재 또는 악재를 선택하세요", min_values=1, max_values=1, required=True,
            options=[discord.SelectOption(label="호재", value="good", default=True),
                     discord.SelectOption(label="악재", value="bad")],
        )
        self.add_item(discord.ui.Label(text="호재 / 악재", component=self.kind_select))
        self.severity_input = discord.ui.TextInput(
            placeholder="예: 30 또는 30.5 · % 기호 없이 입력", default="50", min_length=1, max_length=8,
        )
        self.add_item(discord.ui.Label(
            text="변동성 (0~100%)", component=self.severity_input,
            description="기간 중 무작위 등락의 강도입니다. 목표가격을 비우면 호재 +N%, 악재 -N%가 최종 목표입니다.",
        ))
        self.target_price_input = discord.ui.TextInput(
            placeholder="비우면 % 적용 · 개별 자산의 목표 가격(NWX)", required=False,
            default="" if target_price is None else str(target_price), max_length=16,
        )
        self.add_item(discord.ui.Label(text="목표가격 (선택 사항)", component=self.target_price_input))

    async def interaction_check(self, interaction):
        user = self.bot.db.users.get_user(str(interaction.user.id))
        if str(interaction.user.id) != self.user_id or not user or str(user['role']).strip().lower() != "admin":
            await interaction.response.send_message(**admin_response("❌ 이 사건 창을 연 관리자만 사용할 수 있습니다."), ephemeral=True)
            return False
        if self.submitted:
            await interaction.response.send_message(**admin_response("❌ 이미 처리 중이거나 적용한 사건입니다."), ephemeral=True)
            return False
        return True

    def _asset_code(self):
        if self.asset_select is not None:
            selected = self.asset_select.values
            if len(selected) != 1:
                raise ValueError("자산을 선택해주세요.")
            value = selected[0]
            if value == "__all__":
                return None
            if value not in self.assets:
                raise ValueError("현재 목록에 있는 자산을 선택해주세요.")
            return value
        value = self.asset_input.value.strip().casefold()
        if value in {"시장 전체", "전체", "__all__"}:
            return None
        matches = [asset["code"] for asset in self.assets.values()
                   if value in (asset["code"].casefold(), asset["name"].casefold(),
                                f"{asset['name']} ({asset['code']})".casefold())]
        if len(matches) != 1:
            raise ValueError("자산의 정확한 코드 또는 이름을 입력해주세요.")
        return matches[0]

    async def on_submit(self, interaction):
        if not await self.interaction_check(interaction):
            return
        try:
            asset_code = self._asset_code()
            raw_duration = self.duration_input.value.strip()
            if not raw_duration.isdecimal() or not 1 <= int(raw_duration) <= 604800:
                raise ValueError("duration은 1~604800 사이의 정수(초)로 입력해주세요.")
            duration = int(raw_duration)
            values = self.kind_select.values
            if len(values) != 1 or values[0] not in {"good", "bad"}:
                raise ValueError("호재 또는 악재를 선택해주세요.")
            try:
                percent = float(self.severity_input.value.strip())
            except (ValueError, OverflowError):
                raise ValueError("변동성은 % 없이 0~100 사이 숫자로 입력해주세요.") from None
            if not math.isfinite(percent) or not 0 <= percent <= 100:
                raise ValueError("변동성은 % 없이 0~100 사이 숫자로 입력해주세요.")
            raw_target = self.target_price_input.value.strip()
            target = None
            if raw_target:
                if not raw_target.isdecimal() or not 1 <= int(raw_target) <= 2**53 - 1:
                    raise ValueError("목표가격은 양의 정수 NWX로 입력해주세요.")
                if asset_code is None:
                    raise ValueError("목표가격을 지정하려면 개별 자산을 선택해주세요.")
                target = int(raw_target)
        except ValueError as exc:
            await interaction.response.send_message(**admin_response(f"❌ {exc} `$사건`을 다시 실행해 수정해주세요."), ephemeral=True)
            return
        self.submitted = True
        await interaction.response.defer(ephemeral=True)
        success, message = await asyncio.to_thread(
            self.bot.admin_mgr.create_market_event, self.user_id, values[0], int(round(percent)),
            asset_code, duration, target_price=target, target_percent=percent,
        )
        if not success:
            self.submitted = False
            message += "\n`$사건`을 다시 실행해 값을 수정해주세요."
        await interaction.followup.send(**admin_response(message,'Inazina · $사건'), ephemeral=True)
        if success:
            self.stop()
