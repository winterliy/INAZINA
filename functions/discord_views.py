import asyncio
import copy
import discord
import io
import json
import math
import secrets
import time
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from functions.interaction_reliability import (
    retry_discord_request,
    send_interaction_notice,
)
from functions.price_chart import render_price_chart, render_rate_chart
from functions.technical_indicators import UPPER_INDICATORS, LOWER_INDICATORS, candle_interval, KST
from managers.market_manager import EXCHANGE_INDEX_PREFIX
from managers.rate_manager import RateManager
from managers.fx_manager import FXManager


def inverse_rate_text(rate):
    return format(Decimal(1) / Decimal(str(rate)), '.12g')


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

    def __init__(self, bot, user_id, nickname, notice_text):
        super().__init__(title="Inazina 등록 안내")
        self.bot = bot
        self.user_id = str(user_id)
        self.nickname = str(nickname or user_id)
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
            created = await self.bot.register_user(self.user_id, self.nickname)
        except Exception as exc:
            print(f"[Registration Error] {type(exc).__name__}: {exc}")
            return await interaction.response.send_message(
                "❌ 등록을 완료하지 못했습니다. 잠시 후 `/등록`을 다시 실행해주세요.",
                ephemeral=True,
            )

        if created:
            message = (
                f"✅ {self.nickname}님, 등록이 완료됐습니다. "
                "초기 자산 1,000,000 NWX가 지급됐습니다."
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
            label="초기 예치금 (NWX · 숫자만)",
            default=str(deposit_default),
            placeholder=f"최소 {self.minimum_deposit:,} NWX",
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
                    f"**{current_minimum:,} NWX**입니다. 아래 버튼으로 입력값을 수정해주세요."
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
                    color=discord.Color.green(),
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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message("❌ 본인에게 전송된 계좌 설정만 수정할 수 있습니다.", ephemeral=True)
        return False

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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message("❌ 계좌 개설은 명령어를 입력한 본인만 진행할 수 있습니다.", ephemeral=True)
        return False

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
        if str(interaction.user.id)==self.owner:
            return True
        await interaction.response.send_message('❌ 본인의 계좌 개설 요청만 사용할 수 있습니다.',ephemeral=True)
        return False

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
        color = discord.Color.green() if success else discord.Color.red()
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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "❌ 본인에게 전송된 계좌 해지 메뉴만 사용할 수 있습니다.", ephemeral=True
        )
        return False

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
            label="상환 금액 (NWX · 숫자만)",
            placeholder=f"남은 원리금 {outstanding_balance:,.0f} NWX 이하",
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
        color = discord.Color.green() if success else discord.Color.red()
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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "❌ 본인에게 전송된 대출 상환 메뉴만 사용할 수 있습니다.", ephemeral=True
        )
        return False

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
        if str(interaction.user.id) == self.invited_user_id:
            return True
        await interaction.response.send_message(
            "❌ 이 주주총회 초대장을 받은 사람만 응답할 수 있습니다.", ephemeral=True
        )
        return False

    async def _submit_response(self, interaction: discord.Interaction, accepted: bool):
        await interaction.response.defer(ephemeral=True)
        try:
            organizer = self.bot.get_user(self.organizer_id) or await self.bot.fetch_user(self.organizer_id)
            status = "참석 수락" if accepted else "참석 거절"
            response_embed = discord.Embed(
                title=f"📣 {self.company_name} 주주총회 응답",
                color=discord.Color.green() if accepted else discord.Color.red(),
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
            label="대출 신청 금액 (NWX · 숫자만)",
            placeholder="예: 1000000",
            min_length=1,
            max_length=15,
        )
        self.add_item(self.amount_input)

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

        await interaction.response.defer(ephemeral=True, thinking=True)
        success, message = await asyncio.to_thread(
            self.bot.bank_mgr.apply_loan, self.user_id, self.product_code, int(amount_text))
        color = discord.Color.green() if success else discord.Color.red()
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
        country,currency = self.bot.fx_mgr.national_currency(self.subject_id)
        rates = self.bot.fx_mgr.rates()
        pages = max(1,(len(rates)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page = min(max(0,self.page),pages-1)
        embed = discord.Embed(title='🌐 환율',description=f'국적 **{str(country)[:100]}** · 기본 통화 **{currency}**\n게임 내 시세 · 1 NWX 기준 외화 금액 (수수료 제외)',color=discord.Color.blue())
        for row in rates[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]:
            note = f"그래프 코드 `{row['asset_code']}`" if row['asset_code'] else '기준 통화'
            embed.add_field(name=f"{row['code']} · {row['name']}"[:80],value=f"1 NWX = **{inverse_rate_text(row['rate'])} {row['code']}**\n{note}"[:120],inline=False)
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
    """Page NWX and foreign accounts together on the asset summary."""
    PAGE_SIZE = 4

    def __init__(self,owner,embed,lines,sections=None):
        super().__init__(timeout=300)
        self.owner,self.embed,self.lines,self.page = str(owner),embed,lines,0
        self.field_index = next(i for i,f in enumerate(embed.fields) if f.name=='🏦 보유 통장')
        self.sections = {'🏦 보유 통장':lines,**(sections or {})}

    async def interaction_check(self,interaction):
        if str(interaction.user.id)==self.owner:
            return True
        await interaction.response.send_message('❌ 본인 자산 창만 사용할 수 있습니다.',ephemeral=True)
        return False

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
    CATEGORIES = (
        ("main", "메인"),
        ("indices", "거래소 지수"),
        ("rates", "기준금리 · 법정이자"),
        ("stocks", "주식"),
        ("etf", "ETF"),
        ("treasury_bond", "국채"),
        ("corporate_bond", "회사채"),
        ("stock_future", "주식 선물"),
        ("commodity_futures", "원자재 선물"),
        ("mutual_fund", "일반 펀드"),
        ("index_fund", "인덱스 펀드"),
        ("commodity_spot", "현물 원자재"),
        ("option", "옵션"),
        ("forex", "외환"),
        ("fx_savings", "외환통장 상품"),
        ("savings", "저축 상품"),
        ("loan_products", "대출 상품"),
    )

    def __init__(self, bot, user_id, subject_id=None):
        super().__init__(timeout=300)
        self.bot = bot
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
        for asset in assets:
            details = metadata.get(asset['code'], {})
            for field in ('share_class', 'parent_company_code', 'dividend_cycle_seconds', 'dividend_per_share'):
                if field in details:
                    asset[field] = details[field]
        stocks = [
            item for item in assets
            if str(item["type"]).strip().casefold() in {"stock", "주식"}
        ]
        investments = [item for item in assets if item not in stocks]
        return stocks, investments

    @staticmethod
    def _dividend_note(item):
        cycle = float(item.get('dividend_cycle_seconds') or 0)
        per_share = float(item.get('dividend_per_share') or 0)
        if cycle <= 0 or per_share <= 0:
            return ''
        period = f'{cycle / 86400:g}일' if cycle % 86400 == 0 else f'{cycle:g}초'
        return f" · 정기배당 {period}마다 주·좌당 {per_share:,.0f} NWX"

    def _category_lines(self, category, market_groups=None):
        stocks, investments = market_groups if market_groups is not None else self._market_groups()
        if category == "forex":
            return [f"• **{r['name']}** ({r['code']}) · 1 NWX = **{inverse_rate_text(r['rate'])} {r['code']}**"
                    + (f" · 그래프 `{r['asset_code']}`" if r['asset_code'] else '')
                    for r in FXManager(self.bot.db).rates() if r['code'] != 'NWX']
        if category == 'indices':
            return [f"• **{row['exchange_id']} 거래소 지수** · **{float(row['index_value']):,.2f} pt**\n"
                    f"  ID / 그래프 종목: `{EXCHANGE_INDEX_PREFIX}{row['exchange_id']}`"
                    for row in self.bot.market_mgr.get_exchange_indices()]
        if category == 'rates':
            manager = getattr(self.bot, 'rate_mgr', None) or RateManager(self.bot.db)
            return [f"• **{label}** · 연 **{manager.get_rate(key):g}%**\n"
                    f"  ID / 그래프 종목: `{RateManager.chart_code(key)}`"
                    for key, label in RateManager.KEYS.items()]
        if category == "fx_savings":
            return [f"• **{r['name']}** (`{r['product_code']}`) · {r['currency_code']} · 연 {r['annual_rate']:g}%"
                    for r in self.bot.db.products.execute("SELECT * FROM fx_products WHERE status='active' ORDER BY name")]
        if category in {"etf", "treasury_bond", "corporate_bond", "stock_future", "commodity_futures", "mutual_fund", "index_fund", "commodity_spot", "option", "forex"}:
            def kind(item):
                value = str(item['type']).casefold()
                return {"국채": "treasury_bond", "etf": "etf", "옵션": "option"}.get(value, value)
            return [f"• **{r['name']}** (`{r['code']}`) · {r['price']:,} {r.get('quote_currency','NWX')} · {r['status']}"
                    +(f" · 액면 {r['face_value']:,} NWX · 연 {r['annual_rate']:g}% · {r['maturity_days']}일" if category=='treasury_bond' else '')
                    +self._dividend_note(r)
                    for r in investments if kind(r) == category]
        if category == "stocks":
            lines = []
            for item in stocks:
                preferred = item.get('share_class') == 'preferred'
                class_note = ' · **우선주 · 의결권 없음**' if preferred else ''
                price_text = self.bot.market_mgr.format_price(item["price"], item["type"])
                risk = " · ⚠️ 위험 알림 (100 NWX 이하)" if item["price"] <= 100 else ""
                lines.append(
                    f"• **{item['name']}** (`{item['code']}`) — {price_text} {item.get('quote_currency','NWX')} · "
                    f"{item['status']}{class_note}{self._dividend_note(item)}{risk}"
                )
            return lines
        if category == "savings":
            return [
                f"• **{row['name']}** (`{row['product_code']}`) — 연 {self._number(row['interest_rate']):g}% · "
                f"최소 예치 {self._number(row['deposit_amount']):,.0f} NWX · "
                f"만기 {self._number(row['maturity_days']):g}일 · "
                f"이자주기 {self._number(row['interest_cycle']):g}초 · "
                f"{'복리' if self._number(row['compound_type']) == 1 else '단리'}"
                for row in self.bot.db.products.get_active_savings()
            ]
        if category == "loan_products":
            lines = []
            for row in self.bot.db.products.get_active_liabilities():
                limit = self._number(row["loan_limit"])
                limit_text = "한도 없음" if limit == 0 else f"한도 {limit:,.0f} NWX"
                cycle = self._number(row["repayment_cycle_seconds"])
                installment = self._number(row["repayment_amount"])
                repayment_text = (
                    f" · 정기납부 {installment:,.0f} NWX/{cycle}초"
                    if cycle > 0 and installment > 0 else " · 정기납부 미설정"
                )
                extension_text = (f" · 연장 신용 {self._number(row['extension_min_credit_score']):g}점 이상/"
                                  f"{self._number(row['extension_days']):g}일"
                                  if 'extension_days' in row.keys() and self._number(row['extension_days']) > 0 else ' · 만기 연장 불가')
                lines.append(
                    f"• **{row['name']}** (`{row['product_code']}`) — 연 "
                    f"{self._number(row['interest_rate']):g}% · {limit_text} · "
                    f"최소 신용 {self._number(row['min_credit_score']):g}점{repayment_text}{extension_text}"
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
                    f"잔액 {float(row['balance'] or 0):,.0f} NWX"
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
                    risk = " · ⚠️ 위험 알림 (100 NWX 이하)" if asset["price"] <= 100 else ""
                    lines.append(
                        f"• 보유 **{asset['name']}** (`{row['asset_code']}`) · "
                        f"{int(row['quantity']):,}{unit} · 주당/단가 {price_text} NWX · "
                        f"평가액 {value:,.0f} NWX{risk}"
                    )

            try:
                bond_holdings = self.bot.treasury_bond_mgr.get_user_holdings(self.user_id)
            except Exception:
                bond_holdings = []
            for bond in bond_holdings:
                lines.append(
                    f"• 국채 **{bond['name']}** (`{bond['product_code']}`) · "
                    f"{bond['quantity']:,}매 · 시장가 {bond['market_price']:,} NWX/매 · "
                    f"평가액 {bond['market_value']:,} NWX · "
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
                    f"{float(row['outstanding_balance'] or 0):,.0f} NWX"
                )
            return lines
        return []

    def build_embed(self):
        labels = dict(self.CATEGORIES)
        if self.category == "main":
            stocks, investments = self._market_groups()
            groups = [(label,self._category_lines(category,(stocks,investments)))
                      for category,label in self.CATEGORIES if category!='main']
            embed = discord.Embed(
                title="ℹ️ 메인",
                description="카테고리를 선택하면 해당 종목 목록과 코드를 확인할 수 있습니다.",
                color=discord.Color.blue(),
            )
            for title, lines in groups:
                embed.add_field(name=title,value=f'{len(lines):,}개',inline=True)
            self.previous_button.disabled = True
            self.next_button.disabled = True
            embed.set_footer(text="항목별 전체 목록은 선택 메뉴에서 확인하세요.")
            return embed

        lines = self._category_lines(self.category)
        page_size = FXRatesView.PAGE_SIZE if self.category == 'forex' else self.PAGE_SIZE
        total_pages = max(1, (len(lines) + page_size - 1) // page_size)
        self.page = min(self.page, total_pages - 1)
        start = self.page * page_size
        page_lines = lines[start:start + page_size]
        embed = discord.Embed(
            title=f"ℹ️ {labels[self.category]}",
            description="\n".join(line[:190] for line in page_lines)[:3900] if page_lines else "항목이 없습니다.",
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(lines)}개 · {self.page + 1}/{total_pages} 페이지")
        if self.category == 'forex':
            embed.description = '1 NWX 기준 외화 금액 · 수수료 제외\n\n' + embed.description
            embed.set_footer(text=f"{len(lines)}개 통화 · {self.page + 1}/{total_pages} 페이지 · 페이지 이동 시 최신 환율 조회")
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= total_pages - 1
        return embed

    async def select_category(self, interaction: discord.Interaction):
        self.category = self.category_select.values[0]
        self.page = 0
        for option in self.category_select.options:
            option.default = option.value == self.category
        if self.category == "my_accounts":
            await asyncio.to_thread(
                self.bot.interest_mgr.process_user_interests, self.user_id
            )
        embed = await asyncio.to_thread(self.build_embed)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        if self.category == "my_accounts":
            await asyncio.to_thread(
                self.bot.interest_mgr.process_user_interests, self.user_id
            )
        embed = await asyncio.to_thread(self.build_embed)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        if self.category == "my_accounts":
            await asyncio.to_thread(
                self.bot.interest_mgr.process_user_interests, self.user_id
            )
        embed = await asyncio.to_thread(self.build_embed)
        await interaction.response.edit_message(embed=embed, view=self)

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
            color=discord.Color.green(),
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
        self.bot = bot
        self.user_id = str(user_id)
        self.asset_code = str(asset_code)
        self.rate_key = RateManager.rate_key_from_code(self.asset_code)
        if self.rate_key is not None:
            self.asset_code = RateManager.chart_code(self.rate_key)
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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "❌ 이 그래프를 연 사용자만 기간을 바꿀 수 있습니다.", ephemeral=True
        )
        return False

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

        png = render_price_chart(
            points, view_seconds, current_price=current_price,
            integer_prices=integer_prices, volumes=volumes,
            upper_indicators=self.upper_indicators, lower_indicators=self.lower_indicators,
            anchor_epoch=self.anchor_epoch, end_epoch=now,
            trades=None if is_exchange_index else [(int(row["recorded_epoch"]), int(row["quantity"]), float(row["price"])) for row in trade_rows],
            candle_seconds=self.period_seconds,
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
        current_measure = (
            f"현재 지수: **{current_price_text} pt**"
            if is_exchange_index else f"현재가: **{current_price_text} {asset.get('quote_currency','NWX')}**"
        )
        change_measure = (
            f"기간 변동: **{change_text} pt ({change_percent:+.2f}%)**"
            if is_exchange_index else f"기간 변동: **{change_text} {asset.get('quote_currency','NWX')} ({change_percent:+.2f}%)**"
        )
        volume_note = " · 체결량은 기능 적용 이후 거래부터 집계"
        risk_warning = (
            "\n\n⚠️ **위험 알림: 가격이 100 NWX 이하입니다.**"
            if not is_exchange_index and asset.get('quote_currency','NWX')=='NWX' and current_price <= 100 else ""
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
                   else discord.Color.green() if change >= 0 else discord.Color.red()),
        )
        embed.set_image(url="attachment://movement.png")
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

    async def _edit_chart(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            embed, png = await asyncio.to_thread(self.render)
        except Exception as exc:
            await send_interaction_notice(
                interaction, f"❌ 그래프를 만들지 못했습니다: {exc}"
            )
            return
        try:
            await retry_discord_request(
                lambda: interaction.edit_original_response(
                    embed=embed,
                    attachments=[discord.File(io.BytesIO(png), filename="movement.png")],
                    view=self,
                )
            )
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
                        f"잔량 {int(order['remaining_quantity']):,} · "
                        f"지정가 {int(order['limit_price']):,} {order.get('quote_currency','NWX')}"
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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "❌ 이 주문 목록을 연 사용자만 조작할 수 있습니다.", ephemeral=True
        )
        return False

    def build_embed(self, orders, notice=None):
        status_names = {
            "open": "대기", "partial": "부분체결", "filled": "전량체결",
            "cancelled": "취소", "expired": "만료",
        }
        lines = []
        for order in orders:
            order = dict(order)
            side = "매수" if order["side"] == "buy" else "매도"
            active = order["status"] in {"open", "partial"}
            asset = self.bot.market_mgr.get_asset_info(order["asset_code"])
            asset_type = asset.get("type") if asset else None
            unit = (
                "계약" if asset_type in {"option", "stock_future"} else "매" if asset_type in {"국채", "corporate_bond"}
                else "주" if self.bot.market_mgr.is_stock_type(asset_type) else "개"
            )
            reserve = (
                f" · 주문 증거금 {int(order['reserved_cash']):,} NWX" if order.get("order_kind") == "future" and active else
                f" · 예치 {int(order['reserved_cash']):,} {order.get('quote_currency','NWX')}"
                if order["side"] == "buy" and active
                else (
                    " · 옵션 발행 담보 잠금" if order.get("option_order_type") == "write"
                    else " · 매도수량 잠금"
                ) if order["side"] == "sell" and active else ""
            )
            lines.append(
                f"**#{order['order_id']} {side} · {status_names.get(order['status'], order['status'])}** "
                f"`{order['asset_code']}` · 체결 {int(order['filled_quantity']):,}/"
                f"{int(order['quantity']):,}{unit}"
                + (f" · 잔량 {int(order['remaining_quantity']):,}{unit}" if active else "")
                + f" · 지정가 {int(order['limit_price']):,} {order.get('quote_currency','NWX')}{reserve}"
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
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "❌ 이 자산 목록의 명령 실행자만 페이지를 넘길 수 있습니다.", ephemeral=True
        )
        return False

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
                    f"현금 {info['cash']:,.0f} · 통장 {info['account_total']:,.0f} NWX\n"
                    f"투자 {investment_total:,} · 총자산 {info['grand_total']:,.0f} NWX\n"
                    f"대출 {info['loan_total']:,.0f} NWX"
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
