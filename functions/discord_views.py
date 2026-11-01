import asyncio
import copy
import discord
import io
import hashlib
import json
import math
import secrets
import time
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from database.user_db import TREASURY_USER_ID
from functions.interaction_reliability import (
    discord_request,
    send_interaction_notice,
)
from functions.interaction_checks import owner_interaction_check
from functions.price_chart import render_price_chart, render_rate_chart
from functions.technical_indicators import UPPER_INDICATORS, LOWER_INDICATORS, candle_interval, build_candles, KST
from managers.market_manager import EXCHANGE_INDEX_PREFIX
from managers.rate_manager import RateManager
from managers.currency_exchange_manager import CurrencyExchangeManager
from functions.wallet_views import els_status_text


def inverse_rate_text(rate):
    return format(Decimal(1) / Decimal(str(rate)), '.12g')


def _els_manager(bot):
    manager = getattr(bot, 'els_mgr', None)
    if manager is None:
        from managers.els_manager import ELSManager
        manager = ELSManager(bot.db)
    return manager


class HelpLinksView(discord.ui.View):
    """Help panel links to the bot guide and its public policies."""

    DOCUMENT_URL = (
        "https://dramatic-bread-b4a.notion.site/"
        "NAKWON-BOTS-3ea27b50c7bf80e0904ef783e0e5dfc4"
    )

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.Button(
            label="도움말 바로가기",
            style=discord.ButtonStyle.link,
            url=self.DOCUMENT_URL,
            emoji="📚",
        ))


class RegistrationModal(discord.ui.Modal):
    """Private registration form with the notice in a scrollable text area."""

    @classmethod
    def load_notice_text(cls):
        path = Path(__file__).resolve().parents[1] / "database" / "private.text"
        return path.read_text(encoding="utf-8").strip()

    def __init__(self, bot, user_id, nickname, notice_text, guild_id=None):
        super().__init__(title="Inazina 등록 안내")
        self.bot = bot
        self.user_id = str(user_id)
        self.nickname = str(nickname or user_id)
        self.guild_id = str(guild_id) if guild_id is not None else None
        self.confirmation_code = secrets.token_hex(3).upper()
        self.display_content = (
            f"{str(notice_text or '').strip()}\n\n"
            f"[📚 상세 약관 바로가기]({HelpLinksView.DOCUMENT_URL})\n\n"
            f"안내를 끝까지 확인한 뒤 아래 코드를 입력하세요: {self.confirmation_code}"
        )
        if not str(notice_text or "").strip():
            raise ValueError("database/private.text is empty")
        if len(self.display_content) > 4000:
            raise ValueError("database/private.text must fit in one Discord modal field")

        # TextDisplay is read-only, unlike a prefilled TextInput, and supports
        # Markdown links inside modals in current Discord components.
        self.notice_display = discord.ui.TextDisplay(self.display_content)
        self.confirmation_input = discord.ui.TextInput(
            placeholder="안내를 아래까지 내려 확인하세요",
            min_length=6,
            max_length=6,
            required=True,
        )
        self.confirmation_field = discord.ui.Label(
            text="안내 맨 끝의 확인 코드를 입력하세요",
            description="안내를 끝까지 읽고 표시된 6자리 코드를 입력하세요.",
            component=self.confirmation_input,
        )
        self.add_item(self.notice_display)
        self.add_item(self.confirmation_field)

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirmation_input.value.strip().upper() != self.confirmation_code:
            return await interaction.response.send_message(
                "확인 코드가 맞지 않습니다. 안내문 맨 아래 코드를 확인하고 `/등록`을 다시 실행해주세요.",
                ephemeral=True,
            )

        try:
            if self.guild_id is None:  # Legacy direct modal construction in tests.
                created = await self.bot.register_user(self.user_id, self.nickname)
            else:
                created = await self.bot.register_user(self.user_id, self.nickname, self.guild_id)
        except Exception as exc:
            print(f"[Registration Error] {type(exc).__name__}: {exc}")
            return await interaction.response.send_message(
                "❌ 등록을 완료하지 못했습니다. 잠시 후 `/등록`을 다시 실행해주세요.",
                ephemeral=True,
            )

        if created:
            message = (
                f"✅ {self.nickname}님, 등록이 완료됐습니다. "
                "등록 서버 국가의 기본 화폐로 초기 자산이 지급됐습니다."
            )
        else:
            message = "이미 등록된 사용자입니다. 닉네임 정보를 최신 상태로 갱신했습니다."
        await interaction.response.send_message(message, ephemeral=True)


# --- UI components for Account Opening ---

class AccountOpenModal(discord.ui.Modal):
    def __init__(
        self, bot, user_id, product_code, product_name, minimum_deposit,
        initial_deposit=None,
    ):
        super().__init__(title="🏦 계좌 상세 설정")
        self.bot = bot
        self.user_id = user_id
        self.product_code = product_code
        self.product_name = product_name
        self.minimum_deposit = self._normalise_minimum_deposit(minimum_deposit)
        deposit_default = self.minimum_deposit if initial_deposit is None else max(0, int(initial_deposit))

        self.password_input = discord.ui.TextInput(
            label="계좌 비밀번호",
            placeholder="4~16자 · 공백도 비밀번호에 포함됩니다",
            min_length=4,
            max_length=16,
        )
        self.deposit_input = discord.ui.TextInput(
            label="초기 예치금 (기준 통화 · 숫자만)",
            default=str(deposit_default),
            placeholder=f"최소 {self.minimum_deposit:,} 기준 통화",
        )
        self.add_item(self.password_input)
        self.add_item(self.deposit_input)

    @staticmethod
    def _normalise_minimum_deposit(value):
        try:
            minimum_deposit = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(minimum_deposit):
            return 0
        return max(0, math.ceil(minimum_deposit))

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("❌ 본인의 계좌 개설 창에서만 진행할 수 있습니다.", ephemeral=True)
            return

        deposit_text = self.deposit_input.value.strip()
        if not deposit_text.isascii() or not deposit_text.isdigit():
            await interaction.response.send_message(
                "❌ 초기 예치금은 쉼표·소수점·부호 없이 0 이상의 숫자만 입력해주세요.",
                ephemeral=True,
            )
            return

        deposit_amount = int(deposit_text)
        product = self.bot.db.products.get_saving(self.product_code)
        current_minimum = self._normalise_minimum_deposit(
            product["deposit_amount"] if product else self.minimum_deposit
        )
        if deposit_amount < current_minimum:
            embed = discord.Embed(
                title="⚠️ 초기 예치금이 최소 금액보다 적습니다",
                description=(
                    f"**{self.product_name}**의 최소 예치금은 "
                    f"**{current_minimum:,} 기준 통화**입니다. 아래 버튼으로 입력값을 수정해주세요."
                ),
                color=discord.Color.orange(),
            )
            retry_view = AccountDepositRetryView(
                self.bot,
                self.user_id,
                self.product_code,
                self.product_name,
                current_minimum,
                deposit_amount,
            )
            await interaction.response.send_message(embed=embed, view=retry_view, ephemeral=True)
            return

        success, msg = self.bot.bank_mgr.open_account(
            self.user_id, self.product_code, self.password_input.value, deposit_amount
        )
        if success:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=(
                        f"{msg}\n\n🔐 비밀번호는 보안을 위해 원문 대신 일방향 해시로 저장됩니다. "
                        "입력한 문자와 공백을 그대로 기억해주세요."
                    ),
                    color=discord.Color.default(),
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(content=msg, ephemeral=True)


class AccountDepositRetryView(discord.ui.View):
    def __init__(self, bot, user_id, product_code, product_name, minimum_deposit, attempted_amount):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = str(user_id)
        self.product_code = str(product_code)
        self.product_name = str(product_name)
        self.minimum_deposit = int(minimum_deposit)
        self.attempted_amount = int(attempted_amount)

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id, "❌ 본인에게 전송된 계좌 설정만 수정할 수 있습니다."
        )

    @discord.ui.button(label="예치금 수정", style=discord.ButtonStyle.primary)
    async def edit_deposit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            AccountOpenModal(
                self.bot,
                self.user_id,
                self.product_code,
                self.product_name,
                self.minimum_deposit,
                initial_deposit=self.attempted_amount,
            )
        )

class AccountOpenView(discord.ui.View):
    def __init__(self, bot, user_id, products):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        options = [
            discord.SelectOption(
                label=f"{product['name']} ({product['interest_rate']}%)"[:100],
                value=product['product_code'],
            )
            for product in products[:25]
            if 0 < len(str(product['product_code'])) <= 100
        ]
        self.product_select = discord.ui.Select(
            placeholder="가입하실 저축 상품을 선택하세요",
            options=options,
        )
        self.product_select.callback = self.select_product
        self.add_item(self.product_select)

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id, "❌ 계좌 개설은 명령어를 입력한 본인만 진행할 수 있습니다."
        )

    async def select_product(self, interaction: discord.Interaction):
        product_code = self.product_select.values[0]
        product = self.bot.db.products.get_saving(product_code)
        name = product['name'] if product else "알 수 없는 상품"
        minimum_deposit = product['deposit_amount'] if product else 0
        await interaction.response.send_modal(
            AccountOpenModal(self.bot, self.user_id, product_code, name, minimum_deposit)
        )


class AccountProductOpenView(discord.ui.View):
    def __init__(self,bot,owner,product_code):
        super().__init__(timeout=180)
        self.bot,self.owner,self.product_code = bot,str(owner),product_code

    async def interaction_check(self,interaction):
        return await owner_interaction_check(
            interaction, self.owner, '❌ 본인의 계좌 개설 요청만 사용할 수 있습니다.'
        )

    @discord.ui.button(label='계좌 상세 설정',style=discord.ButtonStyle.primary)
    async def configure(self,interaction,button):
        product = self.bot.db.products.get_saving(self.product_code)
        if not product or product['status']!='active':
            await interaction.response.send_message('❌ 현재 가입할 수 없는 상품입니다.',ephemeral=True)
            return
        await interaction.response.send_modal(AccountOpenModal(self.bot,self.owner,self.product_code,product['name'],product['deposit_amount']))

class AccountCloseModal(discord.ui.Modal):
    def __init__(self, bot, user_id, account_number):
        super().__init__(title="🔐 계좌 해지 확인")
        self.bot = bot
        self.user_id = user_id
        self.account_number = account_number
        self.password_input = discord.ui.TextInput(
            label="선택한 통장의 비밀번호",
            placeholder="개설할 때 입력한 문자를 공백까지 그대로 입력하세요",
            min_length=4,
            max_length=16,
        )
        self.add_item(self.password_input)

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "❌ 본인의 계좌 해지 창에서만 진행할 수 있습니다.", ephemeral=True
            )
            return
        success, message = self.bot.bank_mgr.close_account(
            self.user_id,
            self.account_number,
            password=self.password_input.value,
        )
        color = discord.Color.default() if success else discord.Color.red()
        await interaction.response.send_message(
            embed=discord.Embed(description=message, color=color), ephemeral=True
        )


class AccountCloseView(discord.ui.View):
    def __init__(self, bot, user_id, accounts):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        options = [
            discord.SelectOption(
                label=account["label"][:100],
                value=account["account_number"],
                description=account["description"][:100],
            )
            for account in accounts[:25]
            if 0 < len(str(account["account_number"])) <= 100
        ]
        self.account_select = discord.ui.Select(
            placeholder="해지할 본인 통장을 선택하세요",
            options=options,
        )
        self.account_select.callback = self.select_account
        self.add_item(self.account_select)

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id,
            "❌ 본인에게 전송된 계좌 해지 메뉴만 사용할 수 있습니다."
        )

    async def select_account(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            AccountCloseModal(self.bot, self.user_id, self.account_select.values[0])
        )


class LoanRepaymentModal(discord.ui.Modal):
    def __init__(self, bot, user_id, loan_id, product_code, outstanding_balance):
        super().__init__(title="💳 대출 상환")
        self.bot = bot
        self.user_id = user_id
        self.loan_id = int(loan_id)
        self.product_code = product_code
        self.amount_input = discord.ui.TextInput(
            label="상환 금액 (기준 통화 · 숫자만)",
            placeholder=f"남은 원리금 {outstanding_balance:,.0f} 기준 통화 이하",
            min_length=1,
            max_length=15,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "❌ 본인의 대출 상환 창에서만 진행할 수 있습니다.", ephemeral=True
            )
            return
        amount_text = self.amount_input.value.strip()
        if not amount_text.isascii() or not amount_text.isdigit() or int(amount_text) <= 0:
            await interaction.response.send_message(
                "❌ 상환액은 쉼표 없이 0보다 큰 정수로 입력해주세요.", ephemeral=True
            )
            return
        success, message = self.bot.bank_mgr.repay_loan(
            self.user_id,
            self.product_code,
            int(amount_text),
            loan_id=self.loan_id,
        )
        color = discord.Color.default() if success else discord.Color.red()
        await interaction.response.send_message(
            embed=discord.Embed(description=message, color=color), ephemeral=True
        )


class LoanRepaymentView(discord.ui.View):
    def __init__(self, bot, user_id, loans):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        options = [
            discord.SelectOption(
                label=loan["label"][:100],
                value=str(loan["loan_id"]),
                description=loan["description"][:100],
            )
            for loan in loans[:25]
        ]
        self.loan_select = discord.ui.Select(
            placeholder="상환할 대출 상품을 선택하세요",
            options=options,
        )
        self.loan_select.callback = self.select_loan
        self.add_item(self.loan_select)

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id,
            "❌ 본인에게 전송된 대출 상환 메뉴만 사용할 수 있습니다."
        )

    async def select_loan(self, interaction: discord.Interaction):
        loan_id = int(self.loan_select.values[0])
        loan = self.bot.db.accounts.execute_one(
            "SELECT product_code, outstanding_balance FROM loans "
            "WHERE loan_id = ? AND user_id = ? AND status = 'active'",
            (loan_id, self.user_id),
        )
        if not loan:
            await interaction.response.send_message(
                "❌ 선택한 대출은 이미 상환되었거나 찾을 수 없습니다.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            LoanRepaymentModal(
                self.bot,
                self.user_id,
                loan_id,
                loan["product_code"],
                float(loan["outstanding_balance"] or 0),
            )
        )


class ShareholderMeetingResponseView(discord.ui.View):
    def __init__(self, bot, invited_user_id, organizer_id, company_name, stock_code, place):
        super().__init__(timeout=3600)
        self.bot = bot
        self.invited_user_id = str(invited_user_id)
        self.organizer_id = int(organizer_id)
        self.company_name = str(company_name)
        self.stock_code = str(stock_code)
        self.place = str(place)
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.invited_user_id,
            "❌ 이 주주총회 초대장을 받은 사람만 응답할 수 있습니다."
        )

    async def _submit_response(self, interaction: discord.Interaction, accepted: bool):
        await interaction.response.defer(ephemeral=True)
        try:
            organizer = self.bot.get_user(self.organizer_id) or await self.bot.fetch_user(self.organizer_id)
            status = "참석 수락" if accepted else "참석 거절"
            response_embed = discord.Embed(
                title=f"📣 {self.company_name} 주주총회 응답",
                color=discord.Color.default() if accepted else discord.Color.red(),
            )
            response_embed.add_field(name="응답자", value=f"{interaction.user} (<@{interaction.user.id}>)", inline=False)
            response_embed.add_field(name="응답", value=status, inline=True)
            response_embed.add_field(name="주식 코드", value=self.stock_code, inline=True)
            response_embed.add_field(name="장소", value=self.place[:1024], inline=False)
            await organizer.send(embed=response_embed)
        except (discord.HTTPException, ValueError, TypeError) as exc:
            await interaction.followup.send(
                "❌ 개최자에게 DM을 보내지 못했습니다. DM 설정을 확인한 뒤 다시 시도해주세요.",
                ephemeral=True,
            )
            print(f"[Meeting Response Error] organizer={self.organizer_id}: {exc}")
            return

        await interaction.followup.send(
            f"✅ `{status}` 응답을 개최자에게 DM으로 전달했습니다.", ephemeral=True
        )
        try:
            await interaction.message.edit(content="응답이 전달되었습니다. 이 초대장은 더 이상 응답할 수 없습니다.", view=None)
        except discord.HTTPException as exc:
            print(f"[Meeting Message Update Error] {exc}")

    @discord.ui.button(label="참석 수락", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._submit_response(interaction, True)

    @discord.ui.button(label="참석 거절", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._submit_response(interaction, False)

    async def on_timeout(self):
        if self.message is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(
                content="⏰ 응답 시간이 종료되었습니다. (1시간)", view=self
            )
        except discord.HTTPException:
            pass


class LoanAmountModal(discord.ui.Modal):
    def __init__(self, bot, user_id, product_code):
        super().__init__(title="💳 대출 신청")
        self.bot = bot
        self.user_id = str(user_id)
        self.product_code = product_code
        self.amount_input = discord.ui.TextInput(
            label="대출 신청 금액 (기준 통화 · 숫자만)",
            placeholder="예: 1000000",
            min_length=1,
            max_length=15,
        )
        self.collateral_input = discord.ui.TextInput(
            label="현금 담보 금액 (선택 · 기준 통화)",
            placeholder="없으면 0",
            default="0",
            required=False,
            max_length=15,
        )
        self.collateral_asset_input = discord.ui.TextInput(
            label="자산 담보 코드 (선택)",
            placeholder="예: INZST001",
            required=False,
            max_length=60,
        )
        self.collateral_quantity_input = discord.ui.TextInput(
            label="자산 담보 수량 (선택)",
            placeholder="없으면 0",
            default="0",
            required=False,
            max_length=20,
        )
        self.add_item(self.amount_input)
        self.add_item(self.collateral_input)
        self.add_item(self.collateral_asset_input)
        self.add_item(self.collateral_quantity_input)

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "❌ 본인의 대출 신청 창에서만 진행할 수 있습니다.", ephemeral=True
            )
            return

        amount_text = self.amount_input.value.strip()
        if not amount_text.isascii() or not amount_text.isdigit() or int(amount_text) <= 0:
            await interaction.response.send_message(
                "❌ 대출 금액은 쉼표 없이 0보다 큰 정수로 입력해주세요.", ephemeral=True
            )
            return
        collateral_text = (self.collateral_input.value or "0").strip() or "0"
        if not collateral_text.isascii() or not collateral_text.isdigit():
            await interaction.response.send_message(
                "❌ 담보 금액은 쉼표 없이 0 이상의 정수로 입력해주세요.", ephemeral=True
            )
            return
        asset_code = (self.collateral_asset_input.value or "").strip().upper()
        quantity_text = (self.collateral_quantity_input.value or "0").strip() or "0"
        try:
            asset_quantity = float(quantity_text)
        except ValueError:
            asset_quantity = -1
        if (asset_code and asset_quantity <= 0) or (not asset_code and asset_quantity != 0):
            await interaction.response.send_message(
                "❌ 자산 담보 코드와 0보다 큰 수량을 함께 입력해주세요.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        success, message = await asyncio.to_thread(
            self.bot.bank_mgr.apply_loan, self.user_id, self.product_code,
            int(amount_text), int(collateral_text), asset_code or None, asset_quantity)
        color = discord.Color.default() if success else discord.Color.red()
        await interaction.followup.send(embed=discord.Embed(description=message, color=color), ephemeral=True)


class LoanProductApplyView(discord.ui.View):
    """An owner-only amount form for an explicitly entered product code."""

    def __init__(self, bot, user_id, product_code):
        super().__init__(timeout=180)
        self.bot, self.user_id, self.product_code = bot, str(user_id), str(product_code)

    async def interaction_check(self, interaction):
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message('❌ 본인의 대출 신청 창만 사용할 수 있습니다.', ephemeral=True)
        return False

    @discord.ui.button(label='대출 신청 금액 입력', style=discord.ButtonStyle.primary)
    async def apply_button(self, interaction, button):
        product = self.bot.db.products.get_liability(self.product_code)
        if not product or product['status'] != 'active':
            await interaction.response.send_message('❌ 현재 신청할 수 없는 대출 상품입니다.', ephemeral=True)
            return
        await interaction.response.send_modal(LoanAmountModal(self.bot, self.user_id, self.product_code))


class LoanRequestModal(LoanAmountModal):
    """The search panel also takes a code rather than a limited product select."""

    def __init__(self, bot, user_id):
        super().__init__(bot, user_id, '')
        self.remove_item(self.amount_input)
        self.product_input = discord.ui.TextInput(label='대출 상품 ID', placeholder='예: INZLN001', max_length=100)
        self.add_item(self.product_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction):
        self.product_code = self.product_input.value.strip()
        await super().on_submit(interaction)




class FXRatesView(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(self,bot,owner,subject_id=None):
        super().__init__(timeout=300)
        self.bot,self.owner = bot,str(owner)
        self.subject_id = str(subject_id if subject_id is not None else owner)
        self.page = 0

    async def interaction_check(self,interaction):
        allowed = str(interaction.user.id)==self.owner
        if allowed and self.subject_id!=self.owner:
            user = self.bot.db.users.get_user(self.owner)
            allowed = bool(user and str(user['role']).strip().lower()=='admin')
        if allowed:
            return True
        await interaction.response.send_message('❌ 이 환율 조회를 실행한 사용자만 사용할 수 있습니다.',ephemeral=True)
        return False

    def build_embed(self):
        country,currency = self.bot.currency_mgr.national_currency(self.subject_id)
        rates = self.bot.currency_mgr.rates()
        pages = max(1,(len(rates)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page = min(max(0,self.page),pages-1)
        embed = discord.Embed(title='🌐 환율',description=f'국적 **{str(country)[:100]}** · 기본 통화 **{currency}**\n1 기준 통화 기준 외화 금액 (수수료 제외)',color=discord.Color.blue())
        for row in rates[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]:
            note = f"그래프 코드 `{row['asset_code']}`" if row['asset_code'] else '기준 통화'
            embed.add_field(name=f"{row['code']} · {row['name']}"[:80],value=f"1 기준 통화 = **{inverse_rate_text(row['rate'])} {row['code']}**\n{note}"[:120],inline=False)
        embed.set_footer(text=f'{len(rates)}개 통화 · {self.page+1}/{pages} 페이지 · 페이지 이동 시 최신 환율 조회')
        self.previous.disabled = self.page==0
        self.next.disabled = self.page==pages-1
        return embed

    @discord.ui.button(label='◀ 이전',style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        self.page -= 1
        await interaction.response.edit_message(embed=await asyncio.to_thread(self.build_embed),view=self)

    @discord.ui.button(label='다음 ▶',style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        self.page += 1
        await interaction.response.edit_message(embed=await asyncio.to_thread(self.build_embed),view=self)

class AssetAccountsView(discord.ui.View):
    """Page 기준 통화 and foreign accounts together on the asset summary."""
    PAGE_SIZE = 4

    def __init__(self,owner,embed,lines,sections=None):
        super().__init__(timeout=300)
        self.owner,self.embed,self.lines,self.page = str(owner),embed,lines,0
        self.field_index = next(i for i,f in enumerate(embed.fields) if f.name=='🏦 보유 통장')
        self.sections = {'🏦 보유 통장':lines,**(sections or {})}

    async def interaction_check(self,interaction):
        return await owner_interaction_check(
            interaction, self.owner, '❌ 본인 자산 창만 사용할 수 있습니다.'
        )

    def build_embed(self):
        pages = max(1,max((len(lines)+self.PAGE_SIZE-1)//self.PAGE_SIZE for lines in self.sections.values()))
        self.page = min(max(0,self.page),pages-1)
        embed = discord.Embed.from_dict(copy.deepcopy(self.embed.to_dict()))
        for label,lines in self.sections.items():
            index = next(i for i,field in enumerate(embed.fields) if field.name==label)
            section_pages = max(1,(len(lines)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
            page = min(self.page,section_pages-1)
            visible = lines[page*self.PAGE_SIZE:(page+1)*self.PAGE_SIZE]
            embed.set_field_at(index,name=f'{label} · {page+1}/{section_pages} 페이지',value='\n'.join(line[:240] for line in visible) or '보유 내역이 없습니다.',inline=False)
        embed.set_footer(text=f'자산 상세 {self.page+1}/{pages} 페이지 · 금액 합계는 전체 보유분 기준')
        self.previous.disabled = self.page==0
        self.next.disabled = self.page==pages-1
        return embed

    @discord.ui.button(label='◀ 이전 통장',style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='다음 통장 ▶',style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

class InfoPanelView(discord.ui.View):
    PAGE_SIZE = 20
    RANKINGS = {
        'rank_turnover': 'turnover',
        'rank_volume': 'volume',
        'rank_gainers': 'gainers',
        'rank_losers': 'losers',
    }
    ASSET_RANKING = 'asset_ranking'
    CATEGORIES = (
        ("main", "메인"),
        ("rank_turnover", "실시간차트: 거래대금"),
        ("rank_volume", "실시간차트: 거래량"),
        ("rank_gainers", "실시간차트: 급상승"),
        ("rank_losers", "실시간차트: 급하락"),
        (ASSET_RANKING, "전체 자산 순위"),
        ("indices", "거래소 지수"),
        ("rates", "기준·시장금리 · 법정최고이자"),
        ("stocks", "주식"),
        ("etf", "ETF"),
        ("etn", "ETN"),
        ("els", "ELS"),
        ("treasury_bond", "국채"),
        ("corporate_bond", "회사채"),
        ("stock_future", "주식 선물"),
        ("commodity_futures", "원자재 선물"),
        ("mutual_fund", "일반 펀드"),
        ("index_fund", "인덱스 펀드"),
        ("commodity_spot", "현물 원자재"),
        ("depositary_receipt", "주식예탁증서(DR)"),
        ("option", "옵션"),
        ("forex", "외환"),
        ("savings", "저축 상품 (외환통장 포함)"),
        ("loan_products", "대출 상품"),
    )

    def __init__(self, bot, user_id, subject_id=None):
        super().__init__(timeout=300)
        self.bot = bot
        InfoPanelView._bot_db = bot.db  # read source for the trading-warning badges
        self.owner_id = str(user_id)
        self.user_id = str(subject_id if subject_id is not None else user_id)
        self.category = "main"
        self.page = 0
        self.category_select = discord.ui.Select(
            placeholder="확인할 정보 종류를 선택하세요",
            options=[
                discord.SelectOption(label=label, value=value, default=(value == self.category))
                for value, label in self.CATEGORIES
            ],
            row=0,
        )
        self.category_select.callback = self.select_category
        self.add_item(self.category_select)

    @staticmethod
    def _number(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _effective_rate(row):
        try:
            return f"{RateManager.effective_annual_rate(row['interest_rate'], row['interest_cycle']):.3f}%"
        except (ValueError, OverflowError):
            return "산정 범위 초과"

    @classmethod
    def _rate_model_note(cls, row):
        if row['rate_type'] != 'floating':
            return "고정금리"
        spread = cls._number(row['base_rate_spread'])
        benchmark = {
            'policy': '기준금리', 'funding': '조달금리',
            'bank_bond': '은행채금리', 'treasury': '국채금리',
        }.get(row['benchmark_key'], str(row['benchmark_key']))
        tenor = int(row['benchmark_tenor_days'] or row['maturity_days'] or 0)
        cycle = cls._number(row['repricing_cycle_seconds'])
        cycle_text = f"{cycle/86400:g}일" if cycle % 86400 == 0 else f"{cycle:g}초"
        bounds = f" · 하한 {cls._number(row['rate_floor']):g}%"
        if row['rate_cap'] is not None:
            bounds += f" · 상한 {cls._number(row['rate_cap']):g}%"
        return (
            f"변동금리 · {row['rate_nationality']} {benchmark}"
            + (f" {tenor}일물" if row['benchmark_key'] != 'policy' else "")
            + f" {spread:+g}%p · {cycle_text}마다 재산정"
            + (f" · {cls._number(row['repricing_lag_seconds'])/86400:g}일 전 지표" if row['repricing_lag_seconds'] else "")
            + bounds
        )

    async def interaction_check(self, interaction: discord.Interaction):
        if str(interaction.user.id) == self.owner_id:
            if self.user_id != self.owner_id:
                user=self.bot.db.users.get_user(self.owner_id)
                if not user or str(user['role']).strip().lower()!='admin':
                    await interaction.response.send_message('❌ 국가 정보 조회 권한이 취소되었습니다.',ephemeral=True)
                    return False
            return True
        await interaction.response.send_message("❌ 본인에게 전송된 정보 창만 사용할 수 있습니다.", ephemeral=True)
        return False

    def _market_groups(self):
        assets = [dict(item) for item in self.bot.market_mgr.get_market_assets()]
        metadata = {}
        for table, code_field in (('company', 'company_code'), ('investment', 'product_code'), ('market_assets', 'code')):
            for row in self.bot.db.products.execute(f'SELECT * FROM {table}'):
                metadata[row[code_field]] = dict(row)
        dr_metadata = {row['dr_code']: dict(row)
                       for row in self.bot.db.assets.execute('SELECT * FROM depositary_receipts')}
        for asset in assets:
            details = metadata.get(asset['code'], {})
            for field in ('share_class', 'parent_company_code', 'dividend_cycle_seconds', 'dividend_per_share',
                          'dividend_mode', 'dividend_percent', 'dividend_is_active', 'dividend_priority',
                          'cumulative_dividend', 'liquidation_value_per_share', 'dividend_funding'):
                if field in details:
                    asset[field] = details[field]
            if asset.get('type') == 'depositary_receipt':
                asset.update(dr_metadata.get(asset['code'], {}))
        stocks = [
            item for item in assets
            if str(item["type"]).strip().casefold() in {"stock", "주식"}
        ]
        investments = [item for item in assets if item not in stocks]
        return stocks, investments

    @staticmethod
    def _dividend_note(item):
        return InfoPanelView._dividend_text(item) + InfoPanelView._warning_note(item.get('code'))

    _warnings, _warnings_at = {}, 0.0

    @classmethod
    def _warning_note(cls, code):
        """Administrator trading warning (투자주의·경고·위험) for an instrument line."""
        from managers.trading_warning_manager import TradingWarnings
        if time.time() - cls._warnings_at > 5 and cls._bot_db is not None:
            with cls._bot_db.readonly() as conn:
                cls._warnings, cls._warnings_at = TradingWarnings.active(conn), time.time()
        warning = cls._warnings.get(str(code))
        return f" · **{TradingWarnings.label(warning)}**" if warning else ""

    _bot_db = None

    @staticmethod
    def _dividend_text(item):
        cycle = float(item.get('dividend_cycle_seconds') or 0)
        per_share = float(item.get('dividend_per_share') or 0)
        percent = float(item.get('dividend_percent') or 0)
        mode = item.get('dividend_mode') or 'fixed'
        if cycle <= 0 or (percent if mode == 'percent' else per_share) <= 0:
            return ''
        period = f'{cycle / 86400:g}일' if cycle % 86400 == 0 else f'{cycle:g}초'
        inactive = item.get('dividend_is_active') == 0
        status = ' **OFF · 지급 중지**' if inactive else ''
        payment = (f'지급 시 가격의 {percent:g}%/주·좌' if mode == 'percent'
                   else f"주·좌당 {per_share:,.0f} {item.get('quote_currency') or '—'}")
        return f" · 정기배당{status} {period}마다 {payment}"

    def _category_lines(self, category, market_groups=None):
        stocks, investments = market_groups if market_groups is not None else self._market_groups()
        if category == "forex":
            # Quote every other currency against the viewer's home currency.
            manager = CurrencyExchangeManager(self.bot.db)
            _country, home = manager.national_currency(self.user_id)
            rates = manager.rates()
            home_rate = next((Decimal(str(r['rate'])) for r in rates if r['code'] == home), Decimal(1))
            return [f"• **{r['name']}** ({r['code']}) · 1 {home} = **{format(home_rate / Decimal(str(r['rate'])), '.12g')} {r['code']}**"
                    + (f" · 그래프 `{r['asset_code']}`" if r['asset_code'] else '')
                    for r in rates if r['code'] != home]
        if category == 'indices':
            return [f"• **{row['exchange_id']} 거래소 지수** · **{float(row['index_value']):,.2f} pt**"
                    for row in self.bot.market_mgr.get_exchange_indices()]
        if category == 'rates':
            countries = self.bot.db.currency.execute(
                "SELECT nationality,currency_code,base_rate,legal_max_rate "
                "FROM country_currencies ORDER BY nationality"
            )
            lines = []
            for row in countries:
                curve = self.bot.db.currency.execute(
                    "SELECT benchmark_key,tenor_days,annual_rate FROM market_rate_curve "
                    "WHERE nationality=? ORDER BY benchmark_key,tenor_days", (row['nationality'],)
                )
                curve_text = ' · '.join(
                    f"{ {'funding':'조달','bank_bond':'은행채','treasury':'국채'}[point['benchmark_key']] }"
                    f" {point['tenor_days']}일 {float(point['annual_rate']):g}%" for point in curve
                )
                lines.append(
                    f"• **{row['nationality']}** ({row['currency_code']}) · 기준금리 연 "
                    f"**{float(row['base_rate']):g}%** · 대출 법정최고이자 연 "
                    f"**{float(row['legal_max_rate']):g}%**\n  {curve_text}"
                )
            return lines
        if category == 'els':
            lines = []
            manager = None
            for item in investments:
                if str(item['type']).casefold() != 'els':
                    continue
                manager = manager or _els_manager(self.bot)
                details = manager.get_details(item['code'])
                if not details:
                    continue
                underlyings = ', '.join(f"`{row['code']}`" for row in details.get('underlyings', []))
                next_epoch = details.get('next_observation_epoch')
                next_text = f"<t:{int(next_epoch)}:f>" if next_epoch else '발행 후 확정'
                next_level = details.get('next_redemption_level')
                next_level_text = f'{float(next_level)*100:g}%' if next_level is not None else '없음'
                subscription_end = details.get('subscription_end_epoch')
                subscription_note = (f" · 모집 마감 <t:{int(subscription_end)}:f>"
                                     if details.get('status') == 'subscription' and subscription_end else '')
                lines.append(
                    f"• **{str(details.get('name') or item['name'])[:70]}** (`{item['code']}`) · **{els_status_text(details)}**\n"
                    f"  액면 {float(details['face_value']):,.0f} 기준 통화/좌 · 연 쿠폰 {float(details['annual_coupon']):g}% · "
                    f"낙인 {'발생' if details.get('knocked_in') else '미발생'}\n"
                    f"  기초 {underlyings} · 만기 {float(details.get('maturity_days') or 0):g}일 · "
                    f"다음 평가 {next_text} · 상환 {next_level_text} · "
                    f"만기기준 {float(details['maturity_level'])*100:g}% · 낙인기준 {float(details['knock_in_level']) * 100:g}%"
                    f"{subscription_note}"
                )
            return lines
        if category in {"etf", "etn", "treasury_bond", "corporate_bond", "stock_future", "commodity_futures", "mutual_fund", "index_fund", "commodity_spot", "depositary_receipt", "option", "forex"}:
            def kind(item):
                value = str(item['type']).casefold()
                return {"국채": "treasury_bond", "etf": "etf", "옵션": "option"}.get(value, value)
            fund_names = {'equity':'주식형','bond':'채권형','mixed':'혼합형','other':'기타형'}
            return [f"• **{r['name']}** (`{r['code']}`)"
                    +(f" · 원주 `{r.get('underlying_code')}` · {float(r.get('underlying_per_receipt') or 0):g}주/DR · "
                      f"상장 {r.get('listing_exchange_id') or r.get('exchange_id')}" if category=='depositary_receipt' else '')
                    +(f" · {fund_names.get(r.get('fund_category'), '미분류')}" if category in {'mutual_fund','index_fund'} else '')
                    +f" · {r['price']:,} {r.get('quote_currency') or self.bot.db.base_currency_code()} · {r['status']}"
                    +(f" · 액면 {r['face_value']:,} 기준 통화 · 연 {r['annual_rate']:g}% · {r['maturity_days']}일" if category=='treasury_bond' else '')
                    +self._dividend_note(r)
                    for r in investments if kind(r) == category]
        if category == "stocks":
            lines = []
            for item in stocks:
                preferred = item.get('share_class') == 'preferred'
                class_note = ' · **우선주 · 의결권 없음**' if preferred else ''
                if preferred:
                    class_note += (' · 배당 우선' if int(item.get('dividend_priority') or 0) > 0 else '')
                    class_note += ' · 누적형' if item.get('cumulative_dividend') else ' · 비누적형'
                    class_note += f" · 청산 우선한도 {float(item.get('liquidation_value_per_share') or 0):,.0f} 기준 통화/주"
                price_text = self.bot.market_mgr.format_price(item["price"], item["type"])
                risk = " · ⚠️ 위험 알림 (100 기준 통화 이하)" if item["price"] <= 100 else ""
                lines.append(
                    f"• **{item['name']}** (`{item['code']}`) — {price_text} {item.get('quote_currency') or self.bot.db.base_currency_code()} · "
                    f"{item['status']}{class_note}{self._dividend_note(item)}{risk}"
                )
            return lines
        if category == "savings":
            return [
                # Savings and foreign-currency accounts are one product family; only the currency differs.
                f"• **{row['name']}** (`{row['product_code']}`) · {row['quote_currency']}"
                f" — 연 {self._number(row['interest_rate']):g}% · "
                f"실효 연 {self._effective_rate(row)} · "
                f"최소 예치 {self._number(row['deposit_amount']):,g} {row['quote_currency']} · "
                f"만기 {self._number(row['maturity_days']):g}일 · "
                f"이자주기 {self._number(row['interest_cycle']):g}초 · "
                f"{'복리' if self._number(row['compound_type']) == 1 else '단리'} · "
                f"중도해지 연 {self._number(row['early_withdrawal_rate']):g}%"
                f"/수수료 {self._number(row['early_withdrawal_fee_percent']):g}% · "
                f"{'예금보호' if row['deposit_insured'] else '비보호'} · "
                f"{self._rate_model_note(row)}"
                for row in self.bot.db.products.get_active_savings()
            ]
        if category == "loan_products":
            lines = []
            for row in self.bot.db.products.get_active_liabilities():
                limit = self._number(row["loan_limit"])
                limit_text = "한도 없음" if limit == 0 else f"한도 {limit:,.0f} 기준 통화"
                cycle = self._number(row["repayment_cycle_seconds"])
                installment = self._number(row["repayment_amount"])
                repayment_text = (
                    f" · 정기납부 {installment:,.0f} 기준 통화/{cycle}초"
                    if cycle > 0 and installment > 0 else " · 정기납부 미설정"
                )
                amortization_names = {
                    'legacy': '기존 정액', 'bullet': '만기일시',
                    'equal_principal': '원금균등', 'annuity': '원리금균등',
                }
                amortization = amortization_names.get(
                    row['amortization_type'], row['amortization_type']
                )
                extension_text = (f" · 연장 신용 {self._number(row['extension_min_credit_score']):g}점 이상/"
                                  f"{self._number(row['extension_days']):g}일"
                                  if 'extension_days' in row.keys() and self._number(row['extension_days']) > 0 else ' · 만기 연장 불가')
                lines.append(
                    f"• **{row['name']}** (`{row['product_code']}`) — 연 "
                    f"{self._number(row['interest_rate']):g}% · {limit_text} · "
                    f"최소 신용 {self._number(row['min_credit_score']):g}점 · "
                    f"{self._rate_model_note(row)} · 신용가산 최대 {self._number(row['credit_spread_max']):g}%p"
                    f" · 담보요건 {self._number(row['collateral_required_ratio'])*100:g}%"
                    f" · 취급수수료 {self._number(row['origination_fee_percent']):g}%"
                    f" · 의무비용 {self._number(row['mandatory_cost_percent']):g}%"
                    f" · DTI/DSR 한도 {self._number(row['max_dti_percent']):g}%/"
                    f"{self._number(row['max_dsr_percent']):g}%"
                    f" · 연체가산 {self._number(row['delinquency_spread']):g}%p"
                    f" · 상환 {amortization}/{self._number(row['payment_frequency_days']):g}일"
                    f" · 일수계산 {row['day_count_convention']}"
                    f" · 영업일 {row['business_day_convention']}"
                    f"{repayment_text}{extension_text}"
                )
            return lines
        if category == "my_accounts":
            lines = []
            accounts = self.bot.db.accounts.execute(
                "SELECT account_number, product_code, account_type, balance FROM accounts "
                "WHERE user_id = ? ORDER BY created_at",
                (self.user_id,),
            )
            for row in accounts:
                if row["account_type"] == "saving":
                    product = self.bot.db.products.get_saving(row["product_code"])
                else:
                    product = self.bot.db.products.get_liability(row["product_code"])
                name = product["name"] if product else row["product_code"]
                lines.append(
                    f"• 통장 **{name}** · 끝번호 {str(row['account_number'])[-4:]} · "
                    f"잔액 {float(row['balance'] or 0):,.0f} 기준 통화"
                )

            positions = self.bot.db.assets.execute(
                "SELECT asset_code, SUM(quantity) AS quantity FROM user_assets "
                "WHERE user_id = ? GROUP BY asset_code ORDER BY asset_code",
                (self.user_id,),
            )
            for row in positions:
                try:
                    asset = self.bot.market_mgr.get_asset_info(row["asset_code"])
                except ValueError:
                    asset = None
                if asset:
                    value = float(asset["price"] or 0) * int(row["quantity"] or 0)
                    is_stock = self.bot.market_mgr.is_stock_type(asset["type"])
                    unit = "주" if is_stock else "개"
                    price_text = self.bot.market_mgr.format_price(asset["price"], asset["type"])
                    risk = " · ⚠️ 위험 알림 (100 기준 통화 이하)" if asset["price"] <= 100 else ""
                    lines.append(
                        f"• 보유 **{asset['name']}** (`{row['asset_code']}`) · "
                        f"{int(row['quantity']):,}{unit} · 주당/단가 {price_text} 기준 통화 · "
                        f"평가액 {value:,.0f} 기준 통화{risk}"
                    )

            try:
                bond_holdings = self.bot.treasury_bond_mgr.get_user_holdings(self.user_id)
            except Exception:
                bond_holdings = []
            for bond in bond_holdings:
                lines.append(
                    f"• 국채 **{bond['name']}** (`{bond['product_code']}`) · "
                    f"{bond['quantity']:,}매 · 시장가 {bond['market_price']:,} 기준 통화/매 · "
                    f"평가액 {bond['market_value']:,} 기준 통화 · "
                    f"만기 <t:{bond['maturity_epoch']}:D>"
                )

            loans = self.bot.db.accounts.execute(
                "SELECT loan_id, product_code, outstanding_balance FROM loans "
                "WHERE user_id = ? AND status = 'active' ORDER BY created_at",
                (self.user_id,),
            )
            for row in loans:
                product = self.bot.db.products.get_liability(row["product_code"])
                name = product["name"] if product else row["product_code"]
                lines.append(
                    f"• 대출 **{name}** · 남은 원리금 "
                    f"{float(row['outstanding_balance'] or 0):,.0f} 기준 통화"
                )
            return lines
        return []

    def build_embed(self):
        labels = dict(self.CATEGORIES)
        if self.category == "main":
            stocks, investments = self._market_groups()
            groups = []
            for category, label in self.CATEGORIES:
                if category == 'main':
                    continue
                if category in self.RANKINGS:
                    value = '최대 100위 · 10개씩'
                elif category == self.ASSET_RANKING:
                    value = '20위 · 5명씩'
                else:
                    value = f'{len(self._category_lines(category, (stocks, investments))):,}개'
                groups.append((label, value))
            embed = discord.Embed(
                title="ℹ️ 메인",
                description="카테고리를 선택하면 해당 종목 목록과 코드를 확인할 수 있습니다.",
                color=discord.Color.blue(),
            )
            for title, value in groups:
                embed.add_field(name=title, value=value, inline=True)
            self.previous_button.disabled = True
            self.next_button.disabled = True
            embed.set_footer(text="항목별 전체 목록은 선택 메뉴에서 확인하세요.")
            return embed

        if self.category in self.RANKINGS:
            return self._ranking_embed()
        if self.category == self.ASSET_RANKING:
            return self._asset_ranking_embed()

        lines = self._category_lines(self.category)
        page_size = (5 if self.category == 'els' else 10 if self.category == 'stocks'
                     else FXRatesView.PAGE_SIZE if self.category == 'forex' else self.PAGE_SIZE)
        total_pages = max(1, (len(lines) + page_size - 1) // page_size)
        self.page = min(self.page, total_pages - 1)
        start = self.page * page_size
        page_lines = lines[start:start + page_size]
        embed = discord.Embed(
            title=f"ℹ️ {labels[self.category]}",
            description="\n".join(line[:700 if self.category == 'els' else 350 if self.category == 'stocks' else 190] for line in page_lines)[:3900] if page_lines else "항목이 없습니다.",
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(lines)}개 · {self.page + 1}/{total_pages} 페이지")
        if self.category == 'forex':
            embed.description = '1 기준 통화 기준 외화 금액 · 수수료 제외\n\n' + embed.description
            embed.set_footer(text=f"{len(lines)}개 통화 · {self.page + 1}/{total_pages} 페이지 · 페이지 이동 시 최신 환율 조회")
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= total_pages - 1
        return embed

    def _ranking_embed(self):
        now = int(time.time())
        metric = self.RANKINGS[self.category]
        rows = self.bot.market_mgr.get_realtime_rankings(metric, self.user_id, now=now)
        page_size = 10
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * page_size
        notes = {
            'turnover': '오늘 누적 체결대금 · 내 기본화폐 환산 기준',
            'volume': '오늘 누적 체결 수량 기준',
            'gainers': '오늘 첫 가격 기록 대비 상승률 기준',
            'losers': '오늘 첫 가격 기록 대비 하락률 기준',
        }
        embed = discord.Embed(
            title=f'📊 {dict(self.CATEGORIES)[self.category]}',
            description=f"**{notes[metric]}**\n한국시간 00:00부터 집계 · 갱신 <t:{now}:T>",
            color=discord.Color.blue(),
        )

        def money(value, places):
            if hasattr(value, 'numerator'):
                value = Decimal(value.numerator) / Decimal(value.denominator)
            return f'{value:,.{places}f}'

        for rank, row in enumerate(rows[start:start + page_size], start + 1):
            medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, '▪️')
            value = (
                f"`{row['code'][:100]}` · 현재가 "
                f"**{money(row['price'], row['decimal_places'])} {row['quote_currency']}**\n"
            )
            if metric == 'turnover':
                value += (
                    f"💰 거래대금 **{money(row['turnover'], row['home_decimal_places'])} "
                    f"{row['home_currency']}**"
                )
            elif metric == 'volume':
                value += f"📦 거래량 **{row['volume']:,}**"
            else:
                value += (
                    f"{'🔺' if metric == 'gainers' else '🔻'} 등락률 "
                    f"**{float(row['change_percent']):+.2f}%**"
                )
            embed.add_field(
                name=f"{medal} {rank}위 · {row['name']}"[:256],
                value=value[:1024],
                inline=False,
            )
        if not rows:
            embed.description += '\n\n현재 조건에 해당하는 종목이 없습니다.'
        span = f'{start + 1}–{min(start + page_size, len(rows))}위 · ' if rows else ''
        embed.set_footer(
            text=f'{span}{self.page + 1}/{pages} 페이지 · 최대 100위 · 페이지 이동·새로고침 시 갱신'
        )
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= pages - 1
        return embed

    def _asset_ranking_embed(self):
        from database.user_db import citizen_filter
        users = self.bot.db.users.execute(
            f"SELECT user_id,nickname FROM users WHERE status='active' AND {citizen_filter()} ORDER BY user_id"
        )
        ranked = []
        for user in users:
            info = self.bot.user_mgr.get_total_assets(str(user['user_id']))
            ranked.append({
                'user_id': str(user['user_id']),
                'nickname': str(user['nickname'] or user['user_id']),
                'grand_total': int(info['grand_total']),
                'loan_total': int(info['loan_total']),
                'net_worth': int(info['net_worth']),
            })
        ranked.sort(key=lambda row: (-row['grand_total'], row['user_id']))
        ranked = ranked[:20]
        page_size = 5
        pages = max(1, (len(ranked) + page_size - 1) // page_size)
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * page_size
        embed = discord.Embed(
            title='🏆 전체 자산 순위',
            description='총자산 기준 상위 20명입니다.',
            color=discord.Color.gold(),
        )
        for rank, row in enumerate(ranked[start:start + page_size], start + 1):
            medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, '▪️')
            embed.add_field(
                name=f"{medal} {rank}위 · {row['nickname']}"[:256],
                value=(
                    f"총자산 **{row['grand_total']:,} 기준 통화** · "
                    f"대출 {row['loan_total']:,} 기준 통화 · 순자산 {row['net_worth']:,} 기준 통화"
                ),
                inline=False,
            )
        if not ranked:
            embed.description += '\n\n등록된 시민이 없습니다.'
        span = f'{start + 1}–{min(start + page_size, len(ranked))}위 · ' if ranked else ''
        embed.set_footer(text=f'{span}{self.page + 1}/{pages} 페이지 · 최대 20위 · 5명씩')
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= pages - 1
        return embed

    async def _edit_panel(self, interaction):
        if self.category in self.RANKINGS or self.category == self.ASSET_RANKING:
            if not interaction.response.is_done():
                await interaction.response.defer()
            embed = await asyncio.to_thread(self.build_embed)
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            embed = await asyncio.to_thread(self.build_embed)
            await interaction.response.edit_message(embed=embed, view=self)

    async def select_category(self, interaction: discord.Interaction):
        self.category = self.category_select.values[0]
        self.page = 0
        for option in self.category_select.options:
            option.default = option.value == self.category
        if self.category == "my_accounts":
            await asyncio.to_thread(
                self.bot.interest_mgr.process_user_interests, self.user_id
            )
        await self._edit_panel(interaction)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        if self.category == "my_accounts":
            await asyncio.to_thread(
                self.bot.interest_mgr.process_user_interests, self.user_id
            )
        await self._edit_panel(interaction)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        if self.category == "my_accounts":
            await asyncio.to_thread(
                self.bot.interest_mgr.process_user_interests, self.user_id
            )
        await self._edit_panel(interaction)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._edit_panel(interaction)

    @discord.ui.button(label="계좌 개설", style=discord.ButtonStyle.success, row=2)
    async def open_account_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        products = self.bot.db.products.get_active_savings()
        view = AccountOpenView(self.bot, self.user_id, products)
        if not view.product_select.options:
            await interaction.response.send_message("❌ 가입 가능한 저축 상품이 없습니다.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🏦 계좌 개설",
            description="상품을 선택한 다음 계좌 비밀번호와 초기 예치금을 입력하세요.",
            color=discord.Color.default(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="대출 신청", style=discord.ButtonStyle.primary, row=2)
    async def apply_loan_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LoanRequestModal(self.bot, self.user_id))




class VWAPAnchorModal(discord.ui.Modal, title="고정 VWAP 기준점"):
    def __init__(self, view):
        super().__init__(timeout=180)
        self.chart_view = view
        self.date_input = discord.ui.TextInput(label="기준 일시 (한국시간)",
            placeholder="2026-10-02 09:00", default=datetime.fromtimestamp(view.anchor_epoch, KST).strftime("%Y-%m-%d %H:%M"))
        self.add_item(self.date_input)

    async def on_submit(self, interaction):
        if not await self.chart_view.interaction_check(interaction):
            return
        try:
            epoch = int(datetime.strptime(self.date_input.value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp())
            if not 0 <= epoch <= time.time():
                raise ValueError()
        except (ValueError, OverflowError):
            await interaction.response.send_message("❌ 과거 또는 현재 일시를 YYYY-MM-DD HH:MM 형식으로 입력해주세요.", ephemeral=True)
            return
        self.chart_view.anchor_epoch = epoch
        self.chart_view._save_preferences()
        await self.chart_view._edit_chart(interaction)


class PriceMovementView(discord.ui.View):
    """Private time-range selector for an asset's persisted price history."""

    PERIODS = (
        ("1분", 60),
        ("5분", 5 * 60),
        ("10분", 10 * 60),
        ("30분", 30 * 60),
        ("1시간", 3600),
        ("6시간", 6 * 3600),
        ("12시간", 12 * 3600),
        ("1일", 86400),
    )
    VIEW_WINDOWS = {
        60: 10 * 60,
        5 * 60: 60 * 60,
        10 * 60: 6 * 3600,
        30 * 60: 12 * 3600,
        3600: 24 * 3600,
        6 * 3600: 24 * 3600,
        12 * 3600: 24 * 3600,
        86400: 24 * 3600,
    }

    def period_label(self):
        return next((label for label,seconds in self.PERIODS if seconds==self.period_seconds),f'{self.period_seconds}초')

    @staticmethod
    def _duration_label(seconds):
        if seconds % 86400 == 0:
            return f'{seconds//86400}일'
        if seconds % 3600 == 0:
            return f'{seconds//3600}시간'
        return f'{seconds//60}분'

    def view_seconds(self):
        return self.VIEW_WINDOWS[self.period_seconds]

    def __init__(self, bot, user_id, asset_code):
        super().__init__(timeout=300)
        self._chart_lock = asyncio.Lock()
        self._chart_revision = 0
        self._preferences_pending = False
        self._uploaded_chart_digest = None
        self.bot = bot
        self.user_id = str(user_id)
        self.asset_code = str(asset_code)
        self.rate_key = RateManager.rate_key_from_code(self.asset_code)
        if self.rate_key is not None:
            self.asset_code = RateManager.chart_code(self.rate_key)
        self.els_code = None
        self.els_details = None
        if self.rate_key is None and not self.asset_code.startswith(EXCHANGE_INDEX_PREFIX):
            asset = self.bot.market_mgr.get_asset_info(self.asset_code)
            if asset and asset.get('type') == 'els':
                self.els_code = self.asset_code
                self.els_details = _els_manager(self.bot).get_details(self.els_code)
                underlyings = self.els_details.get('underlyings', []) if self.els_details else []
                if not underlyings:
                    raise ValueError('ELS 기초자산 정보를 찾을 수 없습니다.')
                self.asset_code = str(underlyings[0]['code'])
        self.period_seconds = 3600
        self.period_select = discord.ui.Select(
            placeholder="캔들·X축 집계 주기를 선택하세요",
            options=[
                discord.SelectOption(
                    label=label,
                    value=str(seconds),
                    description=((f'{seconds//60}분봉' if seconds<3600 else
                                  f'{seconds//3600}시간봉' if seconds<86400 else '1일봉')+
                                 f' · 총 {self._duration_label(self.VIEW_WINDOWS[seconds])}'),
                    default=(seconds == self.period_seconds),
                )
                for label, seconds in self.PERIODS
            ],
            row=0,
        )
        self.period_select.callback = self.select_period
        self.add_item(self.period_select)
        if self.rate_key is not None:
            # Rates are dated step functions; stock indicators, trading volume
            # and VWAP controls do not describe this data.
            self.remove_item(self.anchor_button)
            return
        self.upper_indicators = ["sma"]
        self.lower_indicators = ["volume"]
        self.anchor_epoch = int(time.time()) - self.period_seconds
        saved = self.bot.market_mgr.db.assets.execute_one(
            "SELECT * FROM chart_preferences WHERE user_id = ?", (self.user_id,))
        if saved:
            try:
                self.upper_indicators = [k for k in json.loads(saved["upper_indicators"]) if k in UPPER_INDICATORS]
                self.lower_indicators = [k for k in json.loads(saved["lower_indicators"]) if k in LOWER_INDICATORS]
                self.anchor_epoch = int(saved["anchor_epoch"] or self.anchor_epoch)
            except (ValueError, TypeError):
                pass
        self.upper_select = self._indicator_select(UPPER_INDICATORS, self.upper_indicators, "상단 지표 보기/숨기기", 1)
        self.lower_select = self._indicator_select(LOWER_INDICATORS, self.lower_indicators, "하단 지표 보기/숨기기", 2)
        self.upper_select.callback = self.select_upper
        self.lower_select.callback = self.select_lower
        self.add_item(self.upper_select)
        self.add_item(self.lower_select)
        if self.els_code:
            self.underlying_select = discord.ui.Select(
                placeholder='ELS 기초자산 그래프를 선택하세요', row=4,
                options=[discord.SelectOption(
                    label=f"{row.get('name') or row['code']} ({row['code']})"[:100],
                    value=str(row['code']), default=str(row['code']) == self.asset_code,
                ) for row in self.els_details['underlyings']],
            )
            self.underlying_select.callback = self.select_underlying
            self.add_item(self.underlying_select)

    async def select_underlying(self, interaction):
        selected = self.underlying_select.values[0]
        valid_codes = {str(row['code']) for row in self.els_details['underlyings']}
        if selected not in valid_codes:
            return await send_interaction_notice(interaction, '❌ ELS의 기초자산을 선택해주세요.')
        self.asset_code = selected
        for option in self.underlying_select.options:
            option.default = option.value == selected
        await self._edit_chart(interaction)

    def _els_levels(self):
        if not self.els_code:
            return []
        self.els_details = _els_manager(self.bot).get_details(self.els_code)
        if not self.els_details:
            raise ValueError('ELS 발행 조건을 찾을 수 없습니다.')
        row = next((item for item in self.els_details['underlyings'] if str(item['code']) == self.asset_code), None)
        initial = float(row.get('initial_price') or 0) if row else 0
        if initial <= 0:
            return []
        redemption = self.els_details.get('next_redemption_level')
        if redemption is None:
            redemption = self.els_details['maturity_level']
        knock_in = float(self.els_details['knock_in_level'])
        return [
            {'label': 'INITIAL 100%', 'value': initial, 'color': (166, 181, 201)},
            {'label': f'REDEEM {float(redemption)*100:g}%', 'value': initial*float(redemption), 'color': (74, 222, 128)},
            {'label': f'KNOCK-IN {knock_in*100:g}%', 'value': initial*knock_in, 'color': (251, 191, 36)},
        ]

    @staticmethod
    def _indicator_select(catalog, selected, placeholder, row):
        return discord.ui.Select(placeholder=placeholder, min_values=1, max_values=len(catalog), row=row,
            options=[discord.SelectOption(label="모두 숨기기", value="none", default=not selected)] +
                [discord.SelectOption(label=label, value=key, default=key in selected) for key, label in catalog.items()])

    def _save_preferences(self):
        self.bot.market_mgr.db.assets.execute(
            "INSERT INTO chart_preferences (user_id, upper_indicators, lower_indicators, anchor_epoch) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET upper_indicators = excluded.upper_indicators, "
            "lower_indicators = excluded.lower_indicators, anchor_epoch = excluded.anchor_epoch",
            (self.user_id, json.dumps(self.upper_indicators), json.dumps(self.lower_indicators), self.anchor_epoch))

    async def _select_indicators(self, interaction, is_upper):
        control = self.upper_select if is_upper else self.lower_select
        selected = [] if "none" in control.values else list(control.values)
        if is_upper:
            self.upper_indicators = selected
        else:
            self.lower_indicators = selected
        for option in control.options:
            option.default = option.value in selected or (option.value == "none" and not selected)
        self._save_preferences()
        await self._edit_chart(interaction)

    async def select_upper(self, interaction):
        await self._select_indicators(interaction, True)

    async def select_lower(self, interaction):
        await self._select_indicators(interaction, False)

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id,
            "❌ 이 그래프를 연 사용자만 기간을 바꿀 수 있습니다."
        )

    def render(self):
        if self.rate_key is not None:
            return self._render_rate()
        now = int(time.time())
        view_seconds = self.view_seconds()
        since = now-view_seconds
        # Load preceding candles for indicator warm-up, and the complete VWAP anchor.
        data_since = since - 150 * self.period_seconds
        if "anchored_vwap" in self.upper_indicators:
            data_since = min(data_since, self.anchor_epoch)
        if "vwap" in self.upper_indicators:
            midnight = datetime.fromtimestamp(since, KST).replace(hour=0, minute=0, second=0, microsecond=0)
            data_since = min(data_since, int(midnight.timestamp()))
        is_exchange_index = self.bot.market_mgr.is_exchange_index_code(self.asset_code)
        if is_exchange_index:
            exchange_id = self.asset_code[len(EXCHANGE_INDEX_PREFIX):]
            index = self.bot.market_mgr.get_exchange_index_info(exchange_id)
            if not index:
                raise ValueError(f"거래소 `{exchange_id}`의 지수를 찾을 수 없습니다.")
            asset = {
                "name": f"{exchange_id} 거래소 지수",
                "code": self.asset_code,
                "type": "index",
                "price": float(index["index_value"]),
            }
            rows = self.bot.market_mgr.get_exchange_index_history(exchange_id, data_since)
            points = [
                (int(row["recorded_epoch"]), float(row["index_value"]))
                for row in rows
            ]
            trade_rows = self.bot.market_mgr.get_exchange_trade_history(exchange_id, data_since)
            volumes = [(int(epoch), int(quantity)) for epoch, quantity in trade_rows]
            integer_prices = False
            volume_unit = "주"
        else:
            asset = self.bot.market_mgr.get_asset_info(self.asset_code)
            if not asset:
                raise ValueError(f"자산 코드 `{self.asset_code}`를 찾을 수 없습니다.")
            rows = self.bot.market_mgr.get_price_history(self.asset_code, data_since)
            points = [(int(row["recorded_epoch"]), float(row["price"])) for row in rows]
            trade_rows = self.bot.market_mgr.get_trade_history(self.asset_code, data_since)
            volumes = [
                (int(row["recorded_epoch"]), int(row["quantity"]))
                for row in trade_rows
            ]
            integer_prices = True
            volume_unit = (
                "주" if self.bot.market_mgr.is_stock_type(asset["type"])
                else "계약" if asset["type"] in {"option", "stock_future"}
                else "매" if asset["type"] in {"국채", "corporate_bond"}
                else "좌" if asset["type"] in {"mutual_fund", "index_fund"} else "개"
            )
        volumes = [
            (int(epoch), int(quantity)) for epoch, quantity in volumes
        ]
        volume_total = sum(quantity for epoch, quantity in volumes if epoch >= since)
        current_price = float(asset["price"] or 0)
        if integer_prices:
            points = [
                (epoch, self.bot.market_mgr.normalize_market_price(price))
                for epoch, price in points
            ]
            current_price = self.bot.market_mgr.normalize_market_price(current_price)
        else:
            points = [(epoch, round(price, 2)) for epoch, price in points]
            current_price = round(current_price, 2)
        if not points:
            points = [(now, current_price)]
        elif points[-1][0] < now:
            points.append((now, current_price))

        els_levels = self._els_levels()
        png = render_price_chart(
            points, view_seconds, current_price=current_price,
            integer_prices=integer_prices, volumes=volumes,
            upper_indicators=self.upper_indicators, lower_indicators=self.lower_indicators,
            anchor_epoch=self.anchor_epoch, end_epoch=now,
            trades=None if is_exchange_index else [(int(row["recorded_epoch"]), int(row["quantity"]), float(row["price"])) for row in trade_rows],
            candle_seconds=self.period_seconds,
            horizontal_levels=els_levels,
            price_candles=[{key: value for key, value in candle.items() if key != 'volume'}
                           for candle in build_candles(points, [], self.period_seconds)],
        )
        visible_points = [(epoch, price) for epoch, price in points if epoch >= since] or [(now, current_price)]
        first_price, last_price = visible_points[0][1], visible_points[-1][1]
        change = last_price - first_price
        change_percent = (change / first_price * 100) if first_price else 0.0
        period_label = self.period_label()
        history_note = (
            " 아직 저장된 기록이 없어 현재가만 표시 중입니다."
            if not any(int(row["recorded_epoch"]) >= since for row in rows) else ""
        )
        current_price_text = f"{current_price:,.0f}" if integer_prices else f"{current_price:,.2f}"
        change_text = f"{change:+,.0f}" if integer_prices else f"{change:+,.2f}"
        base_currency = (asset.get('quote_currency') or
                         (self.bot.db.base_currency_code() if getattr(self.bot, 'db', None)
                          else "기준 통화"))
        current_measure = (
            f"현재 지수: **{current_price_text} pt**"
            if is_exchange_index else f"현재가: **{current_price_text} {base_currency}**"
        )
        change_measure = (
            f"기간 변동: **{change_text} pt ({change_percent:+.2f}%)**"
            if is_exchange_index else f"기간 변동: **{change_text} {base_currency} ({change_percent:+.2f}%)**"
        )
        volume_note = " · 체결량은 기능 적용 이후 거래부터 집계"
        risk_warning = (
            "\n\n⚠️ **위험 알림: 가격이 100 기준 통화 이하입니다.**"
            if not is_exchange_index and getattr(self.bot, 'db', None)
            and base_currency == self.bot.db.base_currency_code() and current_price <= 100 else ""
        )
        embed = discord.Embed(
            title=(
                f"📊 {asset['name']} ({exchange_id}) 변동"
                if is_exchange_index
                else f"📈 {asset['name']} ({self.asset_code}) 가격 변동"
            ),
            description=(
                f"캔들 주기: **{period_label}봉** · 총 기간 **{self._duration_label(view_seconds)}** · {current_measure}\n"
                f"{change_measure}\n"
                f"기록 {sum(int(row['recorded_epoch']) >= since for row in rows):,}개를 OHLC 캔들로 집계 · 거래량 **{volume_total:,}{volume_unit}**"
                f"{volume_note}"
                " · 한국시간 날짜가 바뀌면 전날 차트 기록 삭제"
                f"{history_note}{risk_warning}"
            ),
            color=(discord.Color.red() if not is_exchange_index and current_price <= 100
                   else discord.Color.default() if change >= 0 else discord.Color.red()),
        )
        embed.set_image(url="attachment://movement.png")
        if self.els_code:
            details = self.els_details
            next_epoch = details.get('next_observation_epoch')
            next_text = f"<t:{int(next_epoch)}:f>" if next_epoch else '발행 후 확정'
            barrier = '발생' if details.get('knocked_in') else '미발생'
            embed.title = f"📊 ELS {self.els_code} · {asset['name']}"[:256]
            embed.add_field(
                name='📜 ELS 발행 조건',
                value=(f"**{els_status_text(details)}** · 액면 {float(details['face_value']):,.0f} 기준 통화/좌 · "
                       f"연 쿠폰 {float(details['annual_coupon']):g}%\n"
                       f"낙인 **{barrier}** · 다음 평가 {next_text}\n"
                       "여러 기초자산 중 최초기준가 대비 가장 낮은 비율로 상환을 판단합니다. "
                       "이 이미지는 선택한 기초자산 가격이며 ELS 평가가격이 아닙니다."),
                inline=False,
            )
            if not els_levels:
                embed.add_field(name='기준선 안내', value='모집 종료·발행 시 최초기준가와 상환·낙인 기준선이 확정됩니다.', inline=False)
        embed.add_field(name="상단 지표", value=" · ".join(UPPER_INDICATORS[k] for k in self.upper_indicators) or "모두 숨김", inline=False)
        embed.add_field(name="하단 지표", value=" · ".join(LOWER_INDICATORS[k] for k in self.lower_indicators) or "모두 숨김", inline=False)
        if "anchored_vwap" in self.upper_indicators:
            embed.add_field(name="고정 VWAP 기준점", value=f"<t:{self.anchor_epoch}:f>", inline=False)
            if self.anchor_epoch < (now + 9 * 3600) // 86400 * 86400 - 9 * 3600:
                embed.add_field(name="기준점 기록 안내", value="전날 가격·체결 기록은 매일 삭제됩니다. 고정 VWAP은 당일 남아 있는 기록부터 계산합니다.", inline=False)
        embed.set_footer(
            text=(
                "시가총액 가중 지수 · 기준 1,000 | 캔들: 상승·보합 빨강 · 하락 파랑 | 하단 막대: 구성주 거래량"
                if is_exchange_index
                else "캔들: 상승·보합 빨강 · 하락 파랑 | 하단 막대: 매수·매도 합산 수량"
            )
        )
        return embed, png

    def _render_rate(self):
        now = time.time()
        view_seconds=self.view_seconds()
        since=now-view_seconds
        manager = getattr(self.bot, "rate_mgr", None) or RateManager(self.bot.market_mgr.db)
        points = manager.get_rate_history(self.rate_key, since, now=now)
        label = RateManager.KEYS[self.rate_key]
        first, last = points[0][1], points[-1][1]
        change = last - first
        changes = sum(left[1] != right[1] for left, right in zip(points, points[1:]))
        period_label = self.period_label()
        png = render_rate_chart(points, view_seconds, end_epoch=now,sample_seconds=self.period_seconds)
        embed = discord.Embed(title=f"📉 {label} 변동 그래프",
            description=(f"표시 주기: **{period_label}** · 총 기간 **{self._duration_label(view_seconds)}** · 현재 연이율: **{last:g}%**\n"
                         f"구간 변동: **{change:+g}%p** · 구간 내 변경 **{changes:,}회**\n"
                         "DB에 저장된 적용 시각을 기준으로 표시합니다. 금리 변경 이력은 날짜가 바뀌어도 유지됩니다."),
            color=discord.Color.blue())
        embed.set_image(url="attachment://movement.png")
        embed.set_footer(text="단위: 연이율(%) · 변동: 퍼센트포인트(%p) | 변경 시점의 계단형 그래프 | 한국시간")
        return embed, png

    def remember_uploaded_chart(self, png):
        self._uploaded_chart_digest = hashlib.sha256(png).digest()

    async def _edit_chart(self, interaction, *, save_preferences=False):
        self._chart_revision += 1
        revision = self._chart_revision
        self._preferences_pending |= save_preferences
        if not interaction.response.is_done():
            await discord_request(lambda: interaction.response.defer(), stage='ack')
        async with self._chart_lock:
            if revision != self._chart_revision:
                return
            snapshot = copy.copy(self)

            def prepare():
                if snapshot._preferences_pending:
                    snapshot._save_preferences()
                return snapshot.render()

            try:
                embed, png = await asyncio.to_thread(prepare)
            except Exception as exc:
                if revision == self._chart_revision:
                    await send_interaction_notice(
                        interaction, f"❌ 그래프를 만들지 못했습니다: {exc}"
                    )
                return
            if revision != self._chart_revision:
                return
            self._preferences_pending = False
            self.els_details = snapshot.els_details
            existing = getattr(getattr(interaction, 'message', None), 'attachments', ())
            reuse = (
                hashlib.sha256(png).digest() == self._uploaded_chart_digest
                and any(item.filename == 'movement.png' for item in existing)
            )
            options = dict(embed=embed, view=self)
            if not reuse:
                options['attachments'] = [
                    discord.File(io.BytesIO(png), filename='movement.png')
                ]
            try:
                await discord_request(
                    lambda: interaction.edit_original_response(**options), stage='chart_edit'
                )
                self.remember_uploaded_chart(png)
            except discord.HTTPException:
                await send_interaction_notice(
                    interaction,
                    "⚠️ Discord 응답이 일시적으로 불안정해 그래프를 갱신하지 못했습니다. 다시 시도해주세요.",
                )

    async def select_period(self, interaction: discord.Interaction):
        self.period_seconds = int(self.period_select.values[0])
        for option in self.period_select.options:
            option.default = int(option.value) == self.period_seconds
        await self._edit_chart(interaction)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, row=3)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._edit_chart(interaction)

    @discord.ui.button(label="고정 VWAP 기준점", style=discord.ButtonStyle.secondary, row=3)
    async def anchor_button(self, interaction, button):
        await interaction.response.send_modal(VWAPAnchorModal(self))


class MyOrdersView(discord.ui.View):
    """Owner-only order list with an interactive cancel control."""

    def __init__(self, bot, user_id, open_orders):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = str(user_id)
        self.open_orders = [dict(order) for order in open_orders]
        self.selected_order_id = None

        if self.open_orders:
            options = []
            for order in self.open_orders[:25]:
                side = "매수" if order["side"] == "buy" else "매도"
                options.append(discord.SelectOption(
                    label=f"#{order['order_id']} {side} · {order['asset_code']}"[:100],
                    description=(
                        f"잔량 {order['remaining_quantity']:,} · "
                        f"지정가 {int(order['limit_price']):,} {order.get('quote_currency') or self.bot.db.base_currency_code()}"
                    )[:100],
                    value=str(order["order_id"]),
                ))
            self.order_select = discord.ui.Select(
                placeholder="취소할 미체결 주문을 선택하세요",
                options=options,
                min_values=1,
                max_values=1,
            )
            self.order_select.callback = self.select_order
            self.add_item(self.order_select)
            self.cancel_button = discord.ui.Button(
                label="선택 주문 취소", style=discord.ButtonStyle.danger, disabled=True
            )
            self.cancel_button.callback = self.cancel_selected
            self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id,
            "❌ 이 주문 목록을 연 사용자만 조작할 수 있습니다."
        )

    def build_embed(self, orders, notice=None):
        status_names = {
            "scheduled": "예약", "open": "대기", "partial": "부분체결", "filled": "전량체결",
            "cancelled": "취소", "expired": "만료",
        }
        lines = []
        for order in orders:
            order = dict(order)
            side = "매수" if order["side"] == "buy" else "매도"
            active = order["status"] in {"scheduled", "open", "partial"}
            asset = self.bot.market_mgr.get_asset_info(order["asset_code"])
            asset_type = asset.get("type") if asset else None
            unit = (
                "계약" if asset_type in {"option", "stock_future"} else "매" if asset_type in {"국채", "corporate_bond"}
                else "주" if self.bot.market_mgr.is_stock_type(asset_type) else "개"
            )
            reserve = (
                f" · 주문 증거금 {int(order['reserved_cash']):,} 기준 통화" if order.get("order_kind") == "future" and active else
                f" · 예치 {int(order['reserved_cash']):,} {order.get('quote_currency') or self.bot.db.base_currency_code()}"
                if order["side"] == "buy" and active
                else (
                    " · 옵션 발행 담보 잠금" if order.get("option_order_type") == "write"
                    else " · 매도수량 잠금"
                ) if order["side"] == "sell" and active else ""
            )
            lines.append(
                f"**#{order['order_id']} {side} · {status_names.get(order['status'], order['status'])}** "
                f"`{order['asset_code']}` · 체결 {order['filled_quantity']:,}/"
                f"{order['quantity']:,}{unit}"
                + (f" · 잔량 {order['remaining_quantity']:,}{unit}" if active else "")
                + (f" · {order.get('scheduled_reason') or '개장 대기'}" if order['status'] == 'scheduled' else "")
                + f" · 지정가 {int(order['limit_price']):,} {order.get('quote_currency') or self.bot.db.base_currency_code()}{reserve}"
            )
        embed = discord.Embed(
            title="📋 나의 주문 (최근 24시간 · 미체결 포함)",
            description=(f"{notice}\n\n" if notice else "") + ("\n".join(lines)[:3800] or "주문이 없습니다."),
            color=discord.Color.blue(),
        )
        selected = f"선택 주문: #{self.selected_order_id} · " if self.selected_order_id else ""
        embed.set_footer(
            text=selected + ("아래 메뉴에서 선택 후 취소할 수 있습니다." if self.open_orders else "취소 가능한 미체결 주문이 없습니다.")
        )
        return embed

    async def select_order(self, interaction: discord.Interaction):
        self.selected_order_id = int(self.order_select.values[0])
        self.cancel_button.disabled = False
        orders = await asyncio.to_thread(self.bot.order_book.get_user_orders, self.user_id)
        await interaction.response.edit_message(embed=self.build_embed(orders), view=self)

    async def cancel_selected(self, interaction: discord.Interaction):
        if self.selected_order_id is None:
            return await interaction.response.send_message(
                "먼저 취소할 주문을 선택해주세요.", ephemeral=True
            )
        success, message = await asyncio.to_thread(
            self.bot.order_book.cancel_order, self.user_id, self.selected_order_id
        )
        orders, open_orders = await asyncio.gather(
            asyncio.to_thread(self.bot.order_book.get_user_orders, self.user_id),
            asyncio.to_thread(self.bot.order_book.get_user_open_orders, self.user_id),
        )
        refreshed_view = MyOrdersView(self.bot, self.user_id, open_orders)
        await interaction.response.edit_message(
            embed=refreshed_view.build_embed(orders, message), view=refreshed_view
        )


class AllAssetsView(discord.ui.View):
    """Ephemeral, owner-only pagination for the server-wide citizen asset list."""

    PAGE_SIZE = 10

    def __init__(self, bot, user_id, users):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = str(user_id)
        self.users = [dict(user) for user in users]
        self.page = 0

    async def interaction_check(self, interaction: discord.Interaction):
        return await owner_interaction_check(
            interaction, self.user_id,
            "❌ 이 자산 목록의 명령 실행자만 페이지를 넘길 수 있습니다."
        )

    def build_embed(self):
        total = len(self.users)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(max(0, self.page), total_pages - 1)
        start = self.page * self.PAGE_SIZE
        visible = self.users[start:start + self.PAGE_SIZE]
        embed = discord.Embed(
            title="🌐 전체 시민 자산 현황",
            color=discord.Color.gold(),
        )
        for citizen in visible:
            user_id = str(citizen["user_id"])
            info = self.bot.user_mgr.get_total_assets(user_id)
            nickname = str(citizen.get("nickname") or user_id)
            investment_total = int(info["stock_total"]) + int(info["bond_total"])
            embed.add_field(
                name=f"{nickname[:230]} · `{user_id}`",
                value=(
                    f"현금 {info['cash']:,.0f} · 통장 {info['account_total']:,.0f} 기준 통화\n"
                    f"투자 {investment_total:,} · 총자산 {info['grand_total']:,.0f} 기준 통화\n"
                    f"대출 {info['loan_total']:,.0f} 기준 통화"
                ),
                inline=False,
            )
        end = min(start + len(visible), total)
        embed.set_footer(text=f"{start + 1}–{end} / {total}명 · {self.page + 1}/{total_pages} 페이지")
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= total_pages - 1
        return embed

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        embed = await asyncio.to_thread(self.build_embed)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        embed = await asyncio.to_thread(self.build_embed)
        await interaction.response.edit_message(embed=embed, view=self)
