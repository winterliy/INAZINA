import asyncio
import copy
import discord
from discord import app_commands
import io
import time
import uuid
import shlex

from database.user_db import TREASURY_USER_ID
from functions.admin_access import BOOTSTRAP_ADMIN_IDS, resolve_user_role
from functions.interaction_reliability import discord_request, retry_discord_request
from functions.shared_actions import execute_shared_action
from functions.discord_views import (
    AccountCloseView,
    AccountOpenView,
    AccountProductOpenView,
    InfoPanelView,
    AssetAccountsView,
    LoanProductApplyView,
    LoanRepaymentView,
    PriceMovementView,
    ShareholderMeetingResponseView,
    MyOrdersView,
    HelpLinksView,
    RegistrationModal,
)
from functions.registration import RegistrationRequired
from managers.investment_manager import FUND_TYPES
from managers.currency_exchange_manager import display_units


MAX_DISCORD_INTEGER = 2**53 - 1


class SlashCommandRegistry:
    """Registers the bot's user and administrator commands as Discord slash commands."""

    def __init__(self, bot):
        self.bot = bot
        self.commands = {}

    def register(self):
        definitions = (
            ("설치", "서버 관리자가 INAZINA 관리자에게 서버 설치를 신청합니다", self.request_installation),
            ("등록", "이용 안내를 확인하고 Inazina 봇에 등록합니다", self.self_register),
            ("서버등록", "이 서버를 INAZINA 시스템에 등록합니다.", self.register_server),
            ("서버탈퇴", "이 서버를 INAZINA 시스템에 등록해제합니다.", self.leave_server),
            ("도움말", "Inazina 명령어 도움말", self.help),
            ("내지갑", "내 국적·현금·일반/외환통장·투자자산·대출을 확인합니다", self.assets),
            ("파산", "현재 자산 현황과 함께 관리자에게 파산을 신청합니다", self.apply_bankruptcy),
            ("송금", "송금하거나 내 외환통장으로 보내 환전합니다", self.transfer),
            ("도박", "NWX를 걸고 도박합니다", self.gamble),
            ("복권구매", "주간 6/45 복권을 구매합니다", self.lottery_buy),
            ("내복권", "내 복권 번호와 당첨·자동 입금 결과를 확인합니다", self.lottery_tickets),
            ("복권결과", "회차별 당첨번호·상금·이월·추첨 검증 자료를 확인합니다", self.lottery_result),
            ("복권규칙", "현재 회차의 가격·마감·추첨·상금 배분을 확인합니다", self.lottery_rules),
            ("옵션체인", "거래 가능한 콜·풋 옵션과 참고 프리미엄을 확인합니다", self.option_chain),
            ("옵션작성", "담보를 맡기고 주식 옵션 매도 호가를 작성합니다", self.option_write),
            ("법정최고이자", "국가별 법정 최고 이율을 확인합니다", self.legal_max_rate),
            ("선물증거금", "보유 주식 선물 포지션에 증거금을 추가합니다", self.future_margin),
            ("매수", "자산을 지정가 매수하거나 펀드를 NAV로 가입합니다", self.buy),
            ("매도", "자산을 지정가 매도하거나 펀드를 NAV로 환매합니다", self.sell),
            ("호가", "자산의 매수·매도 대기 주문을 확인합니다", self.order_book_view),
            ("내주문", "나의 미체결 및 최근 체결 주문을 확인합니다", self.my_orders),
            ("계좌개설", "일반·외환통장 상품 코드를 입력하여 계좌를 개설합니다", self.open_account),
            ("계좌해지", "보유 통장을 선택해 비밀번호 확인 후 해지합니다", self.close_account),
            ("대출신청", "대출 상품 ID를 입력해 신청합니다", self.apply_loan),
            ("대출상환", "상환할 대출과 상환액을 선택합니다", self.repay_loan),
            ("대출연장", "보유 대출 ID의 신용 조건을 확인하여 만기를 연장합니다", self.extend_loan),
            ("검색", "상품·금리·실시간차트·자산순위를 조회합니다", self.search),
            ("재무", "회사·우선주 product_id로 재무제표와 재무비율을 확인합니다", self.financial),
            ("그래프", "자산·거래소지수·기준금리·법정최고이자의 변동 그래프를 확인합니다", self.movement),
            ("주총소집", "회사 주주총회를 소집하고 주주에게 참석 여부를 묻습니다", self.shareholder_meeting),
        )
        for name, description, callback in definitions:
            command = app_commands.Command(name=name, description=description, callback=callback)
            self.bot.tree.add_command(command)
            self.commands[name] = command

        self.commands["송금"].autocomplete("수신자")(self.recipient_autocomplete)
        self.commands["송금"].autocomplete("출금계좌")(self.source_account_autocomplete)
        self.commands["주총소집"].autocomplete("주식코드")(self.company_autocomplete)
        self.commands['법정최고이자'].autocomplete('국가')(self.nationality_autocomplete)
        self.commands['서버등록'].autocomplete('국가')(self.nationality_autocomplete)
        self.commands["설치"].autocomplete("화폐")(self.currency_autocomplete)

    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(국명="등록할 국가 이름", 화폐="기본화폐 코드·약어 (예: NWX, USD)")
    async def request_installation(self, interaction: discord.Interaction, 국명: str, 화폐: str):
        guild=interaction.guild
        permissions=getattr(interaction.user,'guild_permissions',None)
        if guild is None:
            return await self._reply(interaction,content='❌ /설치는 설치를 신청할 서버에서 실행해주세요.')
        if not (str(getattr(guild,'owner_id',''))==str(interaction.user.id) or getattr(permissions,'administrator',False)):
            return await self._reply(interaction,content='❌ 이 서버의 관리자만 설치를 신청할 수 있습니다.')
        country,currency=국명.strip(),화폐.strip().upper()
        if not 1<=len(country)<=100 or not 1<=len(currency)<=32 or any(char in country+currency for char in ('\n','\r','\x00','`')):
            return await self._reply(interaction,content='❌ 국명은 1~100자, 화폐는 1~32자로 입력해주세요. 줄바꿈과 백틱은 사용할 수 없습니다.')
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            rows=await asyncio.to_thread(self.bot.db.users.execute,'SELECT user_id,role FROM users')
            known=await asyncio.to_thread(self.bot.db.currency.execute,
                "SELECT code,name FROM currencies WHERE status='active'")
        except Exception:
            return await self._reply(interaction,content='❌ 관리자·화폐 정보를 조회하지 못해 설치 신청을 전달하지 않았습니다. 잠시 후 다시 시도해주세요.')
        named=[row['code'] for row in known if str(row['name']).upper()==currency]
        if currency not in {row['code'] for row in known} and len(named)==1:
            currency=named[0]
        recipients={str(row['user_id']) for row in rows if str(row['user_id']).isdecimal() and resolve_user_role(row['user_id'],row)=='admin'}
        recipients.update(BOOTSTRAP_ADMIN_IDS)
        approval=f'$서버설치 {guild.id} {shlex.quote(country)} {shlex.quote(currency)}'
        embed=discord.Embed(title='📨 INAZINA 서버 설치 신청',color=discord.Color.blue())
        embed.add_field(name='🏠 발신 서버',value=discord.utils.escape_markdown(str(guild.name))[:100],inline=False)
        embed.add_field(name='🆔 서버 ID',value=f'```{guild.id}```',inline=False)
        embed.add_field(name='🏳️ 국명',value=discord.utils.escape_markdown(country),inline=True)
        embed.add_field(name='💱 기본화폐',value=discord.utils.escape_markdown(currency),inline=True)
        embed.add_field(name='👤 신청자',value=f"{discord.utils.escape_markdown(str(getattr(interaction.user,'display_name',interaction.user.id)))[:100]} · `{interaction.user.id}`",inline=False)
        embed.add_field(name='🛠️ 설치 승인 명령어',value=f'```{approval}```',inline=False)
        embed.set_footer(text='신청 접수 단계 · 서버 설정은 관리자가 $서버설치를 실행할 때 적용됩니다.')
        sent=failed=0
        for admin_id in sorted(recipients):
            try:
                recipient=self.bot.get_user(int(admin_id))
                if recipient is None:
                    recipient=await self.bot.fetch_user(int(admin_id))
                await recipient.send(embed=embed,allowed_mentions=discord.AllowedMentions.none())
                sent+=1
            except (discord.HTTPException,ValueError,TypeError):
                failed+=1
        if sent:
            message=f'✅ 설치 신청을 INAZINA 관리자 {sent}명에게 DM으로 전달했습니다. 관리자 승인 후 등록할 수 있습니다.'
            if failed:message+=f' (DM 전달 실패 {failed}명)'
        else:
            message='❌ INAZINA 관리자에게 설치 신청 DM을 전달하지 못했습니다. 잠시 후 다시 시도해주세요.'
        await self._reply(interaction,content=message)

    async def nationality_autocomplete(self, interaction, current):
        current = str(current or '').casefold()
        rows = self.bot.db.currency.execute(
            "SELECT nationality,currency_code FROM country_currencies ORDER BY nationality"
        )
        return [app_commands.Choice(
            name=f"{row['nationality']} ({row['currency_code']})"[:100],
            value=row['nationality'],
        ) for row in rows if not current or current in row['nationality'].casefold()][:25]

    async def currency_autocomplete(self, interaction, current):
        rows = self.bot.db.currency.execute("SELECT code,name FROM currencies WHERE status='active' ORDER BY code")
        current = current.casefold()
        user=self.bot.db.users.get_user(str(interaction.user.id))
        national=self.bot.db.currency.execute_one('SELECT currency_code FROM country_currencies WHERE nationality=?',
                                           (user['nationality'],)) if user else None
        preferred=national['currency_code'] if national else 'NWX'
        rows=sorted(rows,key=lambda r:(r['code']!=preferred,r['code']!='NWX',r['code']))
        return [app_commands.Choice(name=f"{r['name']} ({r['code']})"[:100],value=r['code'])
                for r in rows if not current or current in r['name'].casefold() or current in r['code'].casefold()][:25]


    async def currency_exchange(self,interaction: discord.Interaction,출금통화: str,입금통화: str,금액: str):
        user_id, _, _ = await self._prepare(interaction)
        try:
            message = await asyncio.to_thread(
                execute_shared_action, self.bot, user_id, "currency_exchange",
                출금통화, 입금통화, 금액,
            )
        except ValueError as exc:
            message = f"❌ {exc}"
        await self._reply(interaction, content=str(message))


    async def lottery_buy(self,interaction: discord.Interaction,수량: app_commands.Range[int,1,1000]=1,번호: str='자동'):
        from functions.lottery_views import LotteryPurchaseView,rules_embed
        user_id,_,_ = await self._prepare(interaction)
        try:
            self.bot.lottery_mgr.numbers(번호)
            round = await asyncio.to_thread(self.bot.lottery_mgr.current)
            if round['status']!='selling' or time.time()>=round['close_epoch']:
                raise ValueError('판매가 마감되었습니다. 추첨 후 다음 회차를 구매해주세요.')
            if 수량>round['rules']['max_tickets']:
                raise ValueError(f"회차별 최대 {round['rules']['max_tickets']}장까지 구매할 수 있습니다.")
        except ValueError as exc:
            return await self._reply(interaction,content=f'❌ {exc}')
        view = LotteryPurchaseView(self.bot,user_id,round,수량,번호,getattr(interaction,'id',uuid.uuid4().hex))
        embed = rules_embed(round)
        embed.title=f"🎟️ 복권 구매 확인 · {round['round_id']}회"
        embed.add_field(name='구매 요청',value=f"{수량}장 · 합계 {수량*round['rules']['ticket_price']:,} NWX\n번호: {str(번호)[:100]}",inline=False)
        await self._reply(interaction,embed=embed,view=view)

    async def lottery_tickets(self,interaction: discord.Interaction,회차: app_commands.Range[int,1,MAX_DISCORD_INTEGER] | None=None):
        from functions.lottery_views import LotteryTicketsView
        user_id,_,_ = await self._prepare(interaction)
        rows = await asyncio.to_thread(self.bot.lottery_mgr.tickets,user_id,회차)
        view = LotteryTicketsView(user_id,rows)
        await self._reply(interaction,embed=view.build_embed(),view=view)

    async def lottery_result(self,interaction: discord.Interaction,회차: app_commands.Range[int,1,MAX_DISCORD_INTEGER] | None=None):
        from functions.lottery_views import result_embed
        await self._prepare(interaction)
        round = await asyncio.to_thread(self.bot.lottery_mgr.result,회차)
        if not round:
            return await self._reply(interaction,content='해당 회차의 결과가 없거나 아직 완료된 추첨이 없습니다.')
        await self._reply(interaction,embed=result_embed(round))

    async def lottery_rules(self,interaction: discord.Interaction):
        from functions.lottery_views import rules_embed
        await self._prepare(interaction)
        round = await asyncio.to_thread(self.bot.lottery_mgr.current)
        await self._reply(interaction,embed=rules_embed(round))

    async def _rate_query(self, interaction, key, nationality):
        await self._prepare(interaction)
        label = "기준금리" if key == "base_rate" else "법정최고이자"
        note = ("관리자가 결정하며 조달·은행채·국채 수익률곡선에 전달되어 상품금리에 간접 반영됩니다." if key == "base_rate"
                else "해당 국적의 대출·신용계약에 적용되는 연이율 상한입니다. 예금과 채권 쿠폰에는 적용하지 않습니다.")
        try:
            rate = self.bot.rate_mgr.get_rate(key, nationality)
        except ValueError as exc:
            return await self._reply(interaction, content=f"❌ {exc}")
        value = f"연 {rate:g}%"
        await self._reply(interaction, content=f"**{nationality} {label}: {value}**\n{note}")

    async def base_rate(self, interaction: discord.Interaction, 국가: str):
        await self._rate_query(interaction, "base_rate", 국가)

    async def legal_max_rate(self, interaction: discord.Interaction, 국가: str):
        await self._rate_query(interaction, "legal_max_rate", 국가)




    async def subscribe_fund(self, interaction: discord.Interaction, 펀드: str, 좌수: app_commands.Range[int, 1, MAX_DISCORD_INTEGER]):
        user_id, _, _ = await self._prepare(interaction)
        success, message = await asyncio.to_thread(
            execute_shared_action, self.bot, user_id, "fund_subscribe", 펀드, 좌수
        )
        await self._reply(interaction, embed=self._result_embed("펀드 가입", message, success))

    async def redeem_fund(self, interaction: discord.Interaction, 펀드: str, 좌수: app_commands.Range[int, 1, MAX_DISCORD_INTEGER]):
        user_id, _, _ = await self._prepare(interaction)
        success, message = await asyncio.to_thread(
            execute_shared_action, self.bot, user_id, "fund_redeem", 펀드, 좌수
        )
        await self._reply(interaction, embed=self._result_embed("펀드 환매", message, success))


    async def future_margin(self, interaction: discord.Interaction, 선물: str, 금액: app_commands.Range[int, 1, MAX_DISCORD_INTEGER]):
        user_id, _, _ = await self._prepare(interaction)
        success, message = await asyncio.to_thread(self.bot.futures_mgr.add_margin, user_id, 선물, 금액)
        await self._reply(interaction, embed=self._result_embed("선물 증거금", message, success))

    async def _prepare(self, interaction, *, defer=False):
        # Most commands complete well inside Discord's response deadline. Avoid
        # a defer + follow-up round trip unless the caller performs heavy work.
        if defer and not interaction.response.is_done():
            await retry_discord_request(
                lambda: interaction.response.defer(ephemeral=True, thinking=True)
            )
        user = interaction.user
        user_id = str(user.id)
        nickname = (
            getattr(user, "display_name", None)
            or getattr(user, "global_name", None)
            or getattr(user, "name", str(user_id))
        )
        ensured_user = await self.bot.ensure_user(user_id, nickname)
        user_data = (
            ensured_user
            if hasattr(ensured_user, "keys")
            else self.bot.db.users.get_user(user_id)
        )
        command_name = getattr(getattr(interaction, "command", None), "name", "")
        try:
            status = user_data["status"] if user_data is not None else None
        except (KeyError, IndexError):
            # Keep lightweight adapters and legacy rows without a status column
            # compatible; the real users table defaults this field to active.
            status = "active"
        if user_data is None or str(status or "active") != "active":
            if command_name == "등록":
                return user_id, user_data, resolve_user_role(user_id, user_data)
            await self._reply(
                interaction,
                content="⛔ 먼저 `/등록`을 실행해 이용 안내를 확인하고 등록해주세요.",
            )
            raise RegistrationRequired()
        return user_id, user_data, resolve_user_role(user_id, user_data)

    @staticmethod
    async def _reply(interaction, *, content=None, embed=None, view=None, file=None, ephemeral=True):
        response_options = {"ephemeral": ephemeral}
        file_payload = None
        for name, value in (
            ("content", content),
            ("embed", embed),
            ("view", view),
            ("file", file),
        ):
            if value is not None:
                response_options[name] = value

        if file is not None:
            stream = file.fp
            position = stream.tell()
            stream.seek(0)
            file_payload = stream.read()
            stream.seek(position)
            file.close()

        async def send_response(send):
            options = dict(response_options)
            if file_payload is not None:
                options["file"] = discord.File(
                    io.BytesIO(file_payload), filename=file.filename
                )
            return await send(**options)

        extras = getattr(interaction, 'extras', None)
        deferred = (
            getattr(interaction.response, 'type', None)
            == discord.InteractionResponseType.deferred_channel_message
            and isinstance(extras, dict)
            and not extras.get('response_delivered')
        )
        if deferred:
            options = {
                key: value for key, value in response_options.items()
                if key not in {'ephemeral', 'file'}
            }
            if file_payload is not None:
                options['attachments'] = [
                    discord.File(io.BytesIO(file_payload), filename=file.filename)
                ]
            await discord_request(
                lambda: interaction.edit_original_response(**options), stage='complete'
            )
            extras['response_delivered'] = True
        elif interaction.response.is_done():
            await discord_request(
                lambda: send_response(interaction.followup.send), stage='followup'
            )
        else:
            await discord_request(lambda: send_response(interaction.response.send_message))
        if file_payload is not None and isinstance(view, PriceMovementView):
            view.remember_uploaded_chart(file_payload)

    @staticmethod
    def _result_embed(title, message, success):
        return discord.Embed(
            title=title,
            description=message,
            color=discord.Color.green() if success else discord.Color.red(),
        )

    async def self_register(self, interaction: discord.Interaction):
        user = interaction.user
        user_id = str(user.id)
        nickname = (
            getattr(user, "display_name", None)
            or getattr(user, "global_name", None)
            or getattr(user, "name", user_id)
        )
        # Modals must be the interaction's initial response, so do not defer here.
        ensured_user = await self.bot.ensure_user(user_id, nickname)
        user_data = (
            ensured_user
            if hasattr(ensured_user, "keys")
            else self.bot.db.users.get_user(user_id)
        )
        if user_data is not None:
            status = user_data["status"] if "status" in user_data.keys() else "active"
            if str(status or "active") != "active":
                return await interaction.response.send_message(
                    content="현재 등록이 비활성화되어 있습니다. 서버 관리자에게 문의해주세요.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                content="이미 등록된 사용자입니다. 다른 명령어를 이용해주세요.",
                ephemeral=True,
            )

        if interaction.guild is None:
            return await interaction.response.send_message(
                content="국적은 서버의 연방국가를 따릅니다. 가입된 서버에서 `/등록`을 실행해주세요.",
                ephemeral=True,
            )
        country = self.bot.db.currency.execute_one(
            'SELECT nationality FROM guild_countries WHERE guild_id=?',
            (str(interaction.guild.id),),
        )
        if not country:
            return await interaction.response.send_message(
                content="이 서버는 아직 연방국가에 가입되어 있지 않습니다. 가입된 다른 서버에서 `/등록`을 실행해주세요.",
                ephemeral=True,
            )

        try:
            notice_text = await asyncio.to_thread(RegistrationModal.load_notice_text)
            modal = RegistrationModal(
                self.bot,
                user_id,
                nickname,
                notice_text,
                interaction.guild.id,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"[Registration Notice Error] {type(exc).__name__}: {exc}")
            return await interaction.response.send_message(
                content="❌ 등록 안내문을 불러오지 못했습니다. 관리자에게 문의해주세요.",
                ephemeral=True,
            )

        await interaction.response.send_modal(modal)

    @staticmethod
    def _server_admin(interaction):
        guild = interaction.guild
        return bool(guild and (guild.owner_id == interaction.user.id or
                    getattr(interaction.user.guild_permissions, 'administrator', False)))

    async def register_server(self, interaction: discord.Interaction, 국가: str):
        if not self._server_admin(interaction):
            return await interaction.response.send_message(
                '❌ 서버 소유자 또는 Discord 관리자만 서버를 등록할 수 있습니다.', ephemeral=True)
        country = str(국가 or '').strip()
        try:
            with self.bot.db.atomic() as conn:
                row = conn.execute('SELECT currency_code FROM country_currencies WHERE nationality=?', (country,)).fetchone()
                if not row:
                    raise ValueError('INAZINA에 등록된 국가를 선택해주세요.')
                conn.execute("INSERT INTO guild_countries(guild_id,nationality) VALUES (?,?) "
                             "ON CONFLICT(guild_id) DO UPDATE SET nationality=excluded.nationality",
                             (str(interaction.guild.id), country))
            await interaction.response.send_message(
                f'✅ 이 서버를 **{country}** ({row["currency_code"]})에 편입했습니다. 새 `/등록` 사용자에게 적용됩니다.',
                ephemeral=True)
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(f'❌ {exc}', ephemeral=True)

    async def leave_server(self, interaction: discord.Interaction):
        if not self._server_admin(interaction):
            return await interaction.response.send_message(
                '❌ 서버 소유자 또는 Discord 관리자만 서버를 탈퇴할 수 있습니다.', ephemeral=True)
        with self.bot.db.atomic() as conn:
            deleted = conn.execute('DELETE FROM guild_countries WHERE guild_id=?',
                                   (str(interaction.guild.id),)).rowcount
        message = ('✅ 이 서버의 연방국가 편입을 해제했습니다. 기존 시민의 국적은 유지됩니다.' if deleted
                   else '이 서버는 연방국가에 가입되어 있지 않습니다.')
        await interaction.response.send_message(message, ephemeral=True)

    async def help(self, interaction: discord.Interaction):
        await self._prepare(interaction)
        await self._reply(interaction, view=HelpLinksView())

    async def assets(self, interaction: discord.Interaction):
        user_id, user_data, _role = await self._prepare(interaction)
        display_name = getattr(interaction.user, "display_name", None) or interaction.user.name
        info = await asyncio.to_thread(self.bot.user_mgr.get_total_assets, user_id)
        embed = discord.Embed(title=f"💰 {display_name}님의 자산 현황", color=discord.Color.gold())
        embed.description = f"국적: **{str(info.get('nationality') or user_data['nationality'])[:100]}** · 기본 통화: **{info.get('national_currency','NWX')}**"
        embed.add_field(name="💵 현금", value=f"```{info['cash']:,.0f} NWX```", inline=True)
        embed.add_field(name="🏦 통장 잔액", value=f"```{info['account_total']:,.0f} NWX```", inline=True)
        embed.add_field(name="🌐 외환통장 평가액",value=f"```{info.get('currency_total',0):,} NWX```",inline=True)
        investment_total = int(info["stock_total"]) + int(info["bond_total"]) + int(info.get('els_total', 0))
        embed.add_field(name="📈 투자자산 합계", value=f"```{investment_total:,} NWX```", inline=True)
        embed.add_field(name='🧾 ELS 인수 원금', value=f"```{int(info.get('els_total', 0)):,} NWX```\n투자자산 합계에 포함 · 인수액 기준", inline=True)
        embed.add_field(name='💰 미지급 배당 명목액', value=f"```{int(info.get('preferred_claim_total', 0)):,} NWX```\n세전 확정 채권 · 지급 시 세금 적용", inline=True)
        embed.add_field(
            name="🔒 매수 주문 예치금",
            value=f"```{info['order_reserved_total']:,.0f} NWX```",
            inline=True,
        )
        embed.add_field(
            name="🔐 풋 옵션 현금담보",
            value=f"```{info.get('option_collateral_cash', 0):,} NWX```",
            inline=True,
        )
        embed.add_field(
            name="📄 옵션 작성 의무 평가",
            value=(f"```{'−' if info.get('option_short_liability',0) else ''}{info.get('option_short_liability',0):,} NWX```\n작성한 옵션의 의무 가치 · 순자산 차감"),
            inline=True,
        )
        embed.add_field(name="🏆 총 자산", value=f"```{info['grand_total']:,.0f} NWX```", inline=False)
        embed.add_field(name="💳 대출 잔액", value=f"```{info['loan_total']:,.0f} NWX```", inline=True)
        embed.add_field(name="🧾 순자산", value=f"```{info['net_worth']:,.0f} NWX```", inline=True)
        try:
            credit_score = int(float(user_data["credit_score"] or 0))
        except (TypeError, ValueError, OverflowError):
            credit_score = 0
        embed.add_field(name="💳 신용 점수", value=f"```{credit_score:,}점```", inline=True)
        embed.add_field(
            name="💼 심사 연소득",
            value=f"```{int(user_data['annual_income'] or 0):,} NWX```",
            inline=True,
        )
        # Freeze the complete financial summary before adding category details.
        # discord.Embed.from_dict keeps the field payload by reference.  Take a
        # deep snapshot so the detail fields assembled below cannot leak back
        # into the main wallet summary.
        summary = discord.Embed.from_dict(copy.deepcopy(embed.to_dict()))
        account_lines = [
            f"• {item['product_name']} ({item['number']}): {item['balance']:,.0f} NWX"
            + (f" · 금융기관 정리 완료 · 보험금 {item['insurance_paid']:,} NWX"
               f" · 미보장채권 {item['uninsured_claim']:,} NWX"
               if item.get('resolution_status') == 'resolved' else '')
            for item in info["accounts"]
        ]
        account_lines.extend(f"• 외환통장 {str(item.get('product_name') or item['product_code'])[:60]} (`{item['account_number']}`): "
            f"{display_units(item['balance_units'])} {item['currency_code']} · 평가 {item['value_nwx']:,} NWX" for item in info.get('currency_accounts',[]))
        accounts = "\n".join(account_lines)
        embed.add_field(name="🏦 보유 통장", value=accounts[:1024] or "개설된 통장이 없습니다.", inline=False)
        position_lines = []
        for item in info["stocks"]:
            if item.get("type") == "stock_future":
                position_lines.append(f"• {item['name']} ({item['code']}): {abs(item['qty']):,}계약 · "
                    f"기준가 {item['price']:,} NWX · 증거금·손익 평가 {item['value']:,} NWX")
                continue
            is_stock = self.bot.market_mgr.is_stock_type(item.get("type"))
            company = self.bot.db.companies.get_company(item['code']) if is_stock else None
            preferred_note = ' · 우선주' if company and 'share_class' in company.keys() and company['share_class'] == 'preferred' else ''
            unit = "계약" if item.get("type") == "option" else "주" if is_stock else "개"
            price_text = self.bot.market_mgr.format_price(item["price"], item.get("type"))
            risk = (
                " · ⚠️ 위험 알림: 100 NWX 이하"
                if item["price"] <= 100 else ""
            )
            locked = int(item.get("sell_reserved_qty", 0))
            available = max(0, int(item["qty"]) - locked)
            lock_note = f" · 주문잠금 {locked:,}{unit}, 가능 {available:,}{unit}" if locked else ""
            if item.get('is_collateral'):
                lock_note=f" · 옵션담보 잠금 · 매도 불가 · 계약 {item['collateral_contract_code']}"
            position_lines.append(
                f"• {item['name']} ({item['code']}){preferred_note}: {item['qty']:,}{unit} × "
                f"{price_text} {item.get('quote_currency','NWX')} = {item['value']:,.0f} NWX{lock_note}{risk}"
            )
        positions = "\n".join(position_lines)
        bond_lines = [
            f"• {item['name']} ({item['product_code']}): 액면 {item['principal']:,} NWX · "
            f"{item['quantity']:,}매 · 시장가 {item['market_price']:,} NWX/매 · "
            f"평가액 {item['market_value']:,} NWX · "
            f"예상 만기 이자 {item['projected_interest']:,} NWX · "
            f"예상 원리금 {item['projected_total']:,} NWX(세전) · "
            f"<t:{item['maturity_epoch']}:D> 만기"
            + (" · ⚠️ 위험 알림: 100 NWX 이하" if item["market_price"] <= 100 else "")
            for item in info["bonds"]
        ]
        embed.add_field(
            name="📦 보유 투자자산 (주식·상품)",
            value=positions[:1024] or "보유 중인 주식·투자상품이 없습니다.",
            inline=False,
        )
        embed.add_field(
            name="🏛️ 보유 국채 (투자자산 합계에 포함)",
            value="\n".join(bond_lines)[:1024] or "보유 중인 미만기 국채가 없습니다.",
            inline=False,
        )
        written_lines = [f"• {item['name']} (`{item['contract_code']}`) · 작성 체결 {item['outstanding_contracts']:,}계약 · 미체결 {item['offered']:,}계약 · "
            f"담보 {item['collateral_quantity']:,}주 / {item['collateral_cash']:,} NWX · 만기 <t:{item['expiration_epoch']}:D>"
            for item in info.get('written_options',[])]
        embed.add_field(name='📄 작성 옵션',value='\n'.join(written_lines)[:1024] or '작성 중인 옵션이 없습니다.',inline=False)
        from functions.wallet_views import WalletCategoryView, els_position_lines, preferred_claim_lines
        sections={'accounts':account_lines[:len(info['accounts'])],
                  'currency_accounts':account_lines[len(info['accounts']):],
                  'treasury_bond':bond_lines,'written':written_lines,
                  'els':els_position_lines(info.get('els_positions', [])),
                  'preferred_claims':preferred_claim_lines(info.get('preferred_dividend_claims', []))}
        for item,line in zip(info['stocks'],position_lines):
            kind=str(item.get('type','')).casefold()
            if kind == 'els':
                continue
            category=('collateral' if item.get('is_collateral') else 'stocks' if kind in {'stock','주식'} else
                      kind if kind in dict(WalletCategoryView.CATEGORIES) else 'other')
            sections.setdefault(category,[]).append(line)
        for item in info.get('written_options',[]):
            if item['collateral_cash']:
                sections.setdefault('collateral',[]).append(f"• 풋 현금담보 · {item['contract_code']} · {item['collateral_cash']:,} NWX · 계약 만기까지 잠금")
        from functions.wallet_views import load_active_wallet_loans
        loans = await asyncio.to_thread(
            load_active_wallet_loans, self.bot.db, user_id, ordered=True
        )
        sections['loans']=[f"• {row['name'] or row['product_code']} ({row['product_code']}) · 대출 #{row['loan_id']} · 원금 {row['principal']:,} NWX · 남은 부채 {row['outstanding_balance']:,} NWX"
                           +f" · 명목 {float(row['contract_rate'] or 0):g}% · 실효 {float(row['contract_effective_rate'] or row['contract_rate'] or 0):.3f}% · APR {float(row['contract_apr'] or row['contract_rate'] or 0):.3f}% · {row['collection_status']}"
                           +(f" · 현금담보 {int(row['collateral_amount'] or 0)-int(row['collateral_released'] or 0)-int(row['collateral_seized'] or 0):,} NWX" if row['collateral_amount'] else '')
                           +(f" · 자산담보평가 {int(row['collateral_asset_value'] or 0):,} NWX · LTV {float(row['current_ltv'] or 0)*100:.1f}%" if row['collateral_asset_value'] else '')
                           +(f" · 만기 <t:{int(row['maturity_epoch'])}:D> · /대출연장 id:{row['loan_id']}" if row['maturity_epoch'] else '') for row in loans]
        view=WalletCategoryView(user_id,summary,sections,info.get('option_short_liability',0))
        await self._reply(interaction,embed=view.build_embed(),view=view)

    async def apply_rehabilitation(self, interaction: discord.Interaction):
        await self._submit_financial_relief_request(interaction, "회생")

    async def apply_bankruptcy(self, interaction: discord.Interaction):
        await self._submit_financial_relief_request(interaction, "파산")

    async def _submit_financial_relief_request(self, interaction, request_type):
        user_id, _user_data, _role = await self._prepare(interaction)
        display_name = (
            getattr(interaction.user, "display_name", None)
            or getattr(interaction.user, "global_name", None)
            or getattr(interaction.user, "name", user_id)
        )
        try:
            assets = await asyncio.to_thread(
                self.bot.user_mgr.get_total_assets, user_id, settle=False
            )
            users = await asyncio.to_thread(
                self.bot.db.users.execute,
                "SELECT user_id, role FROM users",
            )
        except Exception as exc:
            print(f"[Relief Request Error] {type(exc).__name__}: {exc}")
            return await self._reply(
                interaction,
                content="❌ 현재 자산 정보를 불러오지 못해 신청을 전달하지 않았습니다. 잠시 후 다시 시도해주세요.",
            )

        admin_ids = {
            str(row["user_id"])
            for row in users
            if resolve_user_role(row["user_id"], row) == "admin"
        }
        admin_ids.update(BOOTSTRAP_ADMIN_IDS)
        investment_total = int(assets["stock_total"]) + int(assets["bond_total"])
        message = (
            f"[{display_name} ({user_id})가 {request_type}을 신청했습니다.\n"
            "현재 잔액 (내 자산 페이지 구성 기준 · 미정산 이자/만기정산 제외)\n"
            f"현금: {int(assets['cash']):,} NWX · "
            f"통장: {int(assets['account_total']):,} NWX · "
            f"투자자산: {investment_total:,} NWX · "
            f"매수 주문 예치금: {int(assets['order_reserved_total']):,} NWX\n"
            f"총 자산: {int(assets['grand_total']):,} NWX · "
            f"대출 잔액: {int(assets['loan_total']):,} NWX · "
            f"순자산: {int(assets['net_worth']):,} NWX]"
        )

        sent = failed = 0
        for admin_id in sorted(admin_ids):
            try:
                recipient = self.bot.get_user(int(admin_id))
                if recipient is None:
                    recipient = await self.bot.fetch_user(int(admin_id))
                await recipient.send(
                    message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent += 1
            except (discord.HTTPException, ValueError, TypeError):
                failed += 1

        if sent:
            result = f"✅ {request_type} 신청을 관리자 {sent}명에게 DM으로 전달했습니다."
            if failed:
                result += f" (DM 전달 실패 {failed}명)"
        elif failed:
            result = f"⚠️ 신청 DM을 보내지 못했습니다. 전달 실패: {failed}명"
        else:
            result = "⚠️ DM을 받을 관리자를 찾지 못했습니다."
        await self._reply(interaction, content=result)

    @app_commands.describe(
        수신자='사용자 ID 또는 수신 계좌(환전은 내 외환통장 선택)',
        금액='내 외환통장은 입금될 외화 기준, 나머지는 출금 통화 기준',
        출금계좌='비우면 내 기본화폐 지갑에서 출금',
    )
    async def transfer(
        self,
        interaction: discord.Interaction,
        수신자: str,
        금액: str,
        출금계좌: str | None = None,
    ):
        user_id, _user_data, _role = await self._prepare(interaction)
        success, message = await asyncio.to_thread(
            execute_shared_action, self.bot, user_id, "transfer",
            수신자, 금액, 출금계좌,
        )
        await self._reply(
            interaction,
            embed=self._result_embed("💸 송금", message, success),
        )

    async def gamble(
        self,
        interaction: discord.Interaction,
        금액: app_commands.Range[int, 1, MAX_DISCORD_INTEGER],
    ):
        user_id, _user_data, _role = await self._prepare(interaction)
        success, message = self.bot.user_mgr.gamble(user_id, 금액)
        await self._reply(interaction, embed=self._result_embed("🎲 도박 결과", message, success))



    async def option_chain(self, interaction: discord.Interaction, 기초주식: str = None):
        await self._prepare(interaction)
        contracts = await asyncio.to_thread(self.bot.option_mgr.get_active_contracts, 기초주식, limit=25)
        if not contracts:
            description = (
                f"`{기초주식}` 기초주식의 활성 옵션이 없습니다." if 기초주식
                else "현재 거래 중인 옵션이 없습니다. 주식 보유자는 `/옵션작성`으로 콜·풋 호가를 만들 수 있습니다."
            )
            return await self._reply(
                interaction,
                embed=discord.Embed(title="🧾 옵션 체인", description=description, color=discord.Color.blue()),
            )
        lines = []
        for contract in contracts:
            book = await asyncio.to_thread(self.bot.order_book.get_order_book, contract["contract_code"])
            bid = int(book["bids"][0]["limit_price"]) if book["bids"] else None
            ask = int(book["asks"][0]["limit_price"]) if book["asks"] else None
            side = "콜" if contract["option_type"] == "call" else "풋"
            lines.append(
                f"**{contract['name']}** (`{contract['contract_code']}`) · {side} · "
                f"기초 {contract['underlying_price']:,} / 행사가 {int(contract['strike_price']):,} NWX\n"
                f"만기 <t:{int(contract['expiration_epoch'])}:R> · IV {float(contract['implied_volatility'])*100:g}% · "
                f"참고 프리미엄 {contract['theoretical_price']:,} NWX · "
                f"매수 {f'{bid:,}' if bid is not None else '없음'} / "
                f"매도 {f'{ask:,}' if ask is not None else '없음'} NWX"
            )
        await self._reply(
            interaction,
            embed=discord.Embed(
                title="🧾 옵션 체인",
                description=("\n\n".join(lines))[:4000],
                color=discord.Color.blue(),
            ),
        )

    @app_commands.choices(유형=[
        app_commands.Choice(name="콜", value="call"),
        app_commands.Choice(name="풋", value="put"),
    ])
    async def option_write(
        self,
        interaction: discord.Interaction,
        기초주식: str,
        유형: app_commands.Choice[str],
        행사가: app_commands.Range[int, 1, MAX_DISCORD_INTEGER],
        만기일수: app_commands.Range[int, 1, 365],
        계약수: app_commands.Range[int, 1, 100000],
        지정프리미엄: app_commands.Range[int, 1, MAX_DISCORD_INTEGER],
        변동성: app_commands.Range[int, 1, 500] = 50,
    ):
        user_id, _user_data, _role = await self._prepare(interaction)
        success, message = await asyncio.to_thread(
            execute_shared_action, self.bot, user_id, "option_write",
            (기초주식, 유형.value, 행사가, 만기일수, 계약수, 지정프리미엄, 변동성),
        )
        await self._reply(
            interaction,
            embed=self._result_embed("📝 옵션 작성", message, success),
        )





    @app_commands.describe(지정가='펀드는 비우고, 나머지 자산은 1 이상의 지정가를 입력')
    async def buy(
        self,
        interaction: discord.Interaction,
        자산: str,
        수량: app_commands.Range[int, 1, MAX_DISCORD_INTEGER],
        지정가: app_commands.Range[int, 1, MAX_DISCORD_INTEGER] | None = None,
    ):
        user_id, _user_data, _role = await self._prepare(interaction)
        asset = self.bot.market_mgr.get_asset_info(자산.strip())
        if asset and asset.get("type") in FUND_TYPES:
            success, message = await asyncio.to_thread(
                execute_shared_action, self.bot, user_id, "fund_subscribe", 자산, 수량
            )
            return await self._reply(
                interaction, embed=self._result_embed("📈 펀드 매수", message, success)
            )
        if 지정가 is None:
            return await self._reply(
                interaction,
                content="❌ 펀드가 아닌 자산은 지정가를 입력해주세요.",
            )
        success, message = await asyncio.to_thread(
            execute_shared_action, self.bot, user_id, "buy", 자산, 수량, 지정가
        )
        await self._reply(interaction, embed=self._result_embed("📈 지정가 매수", message, success))

    @app_commands.describe(지정가='펀드는 비우고, 나머지 자산은 1 이상의 지정가를 입력')
    async def sell(
        self,
        interaction: discord.Interaction,
        자산: str,
        수량: app_commands.Range[int, 1, MAX_DISCORD_INTEGER],
        지정가: app_commands.Range[int, 1, MAX_DISCORD_INTEGER] | None = None,
    ):
        user_id, _user_data, _role = await self._prepare(interaction)
        asset = self.bot.market_mgr.get_asset_info(자산.strip())
        if asset and asset.get("type") in FUND_TYPES:
            success, message = await asyncio.to_thread(
                execute_shared_action, self.bot, user_id, "fund_redeem", 자산, 수량
            )
            return await self._reply(
                interaction, embed=self._result_embed("📉 펀드 매도", message, success)
            )
        if 지정가 is None:
            return await self._reply(
                interaction,
                content="❌ 펀드가 아닌 자산은 지정가를 입력해주세요.",
            )
        success, message = await asyncio.to_thread(
            execute_shared_action, self.bot, user_id, "sell", 자산, 수량, 지정가
        )
        await self._reply(interaction, embed=self._result_embed("📉 지정가 매도", message, success))

    async def order_book_view(self, interaction: discord.Interaction, 자산: str):
        _user_id, _user_data, _role = await self._prepare(interaction)
        asset = self.bot.market_mgr.get_asset_info(자산)
        if not asset:
            return await self._reply(interaction, content="❌ 자산 코드를 찾을 수 없습니다.")
        book = self.bot.order_book.get_order_book(자산)
        if asset["type"] in FUND_TYPES:
            return await self._reply(interaction, content="일반·인덱스 펀드는 `/매수`·`/매도`에서 지정가 없이 NAV 기준으로 거래합니다.")
        if asset["type"] == "stock_future":
            gross = self.bot.db.assets.execute_one("SELECT COALESCE(SUM(ABS(quantity)), 0) AS quantity "
                "FROM futures_positions WHERE product_code = ?", (자산,))["quantity"]
            capacity = max(0, int(asset["total_supply"]) - int(gross))
            if capacity:
                for side in ("bids", "asks"):
                    book[side].append(dict(limit_price=int(asset["price"]), quantity=capacity, order_count=1, is_primary=True))
                book["bids"].sort(key=lambda row: row["limit_price"], reverse=True)
                book["asks"].sort(key=lambda row: row["limit_price"])
        elif asset.get("type") == "국채":
            bond = self.bot.db.products.get_treasury_bond(자산)
            primary_quantity = max(
                0,
                (int(bond["total_supply"]) - int(bond["sold_amount"] or 0))
                // max(1, int(bond["face_value"])),
            ) if bond else 0
            primary_price = int(bond["face_value"]) if bond else int(asset["price"])
            if primary_quantity:
                book["asks"].append({
                    "limit_price": primary_price,
                    "quantity": primary_quantity,
                    "order_count": 1,
                    "is_primary": True,
                })
                book["asks"].sort(key=lambda row: int(row["limit_price"]))
                book["asks"] = book["asks"][:10]
        elif asset.get("total_supply") is not None and not self.bot.option_mgr.is_option(자산):
            issued = self.bot.db.assets.execute_one(
                "SELECT (SELECT COALESCE(SUM(quantity), 0) FROM user_assets WHERE asset_code = ?) + "
                "(SELECT COALESCE(SUM(quantity), 0) FROM option_collateral_lots "
                "WHERE underlying_code = ?) AS quantity",
                (자산, 자산),
            )
            primary_quantity = max(
                0, int(asset["total_supply"]) - int(issued["quantity"] or 0)
            )
            if primary_quantity:
                book["asks"].append({
                    "limit_price": int(asset["price"]),
                    "quantity": primary_quantity,
                    "order_count": 1,
                    "is_primary": True,
                })
                book["asks"].sort(key=lambda row: int(row["limit_price"]))
                book["asks"] = book["asks"][:10]
        asks = []
        currency = asset.get('quote_currency','NWX')
        unit = (
            "계약" if self.bot.option_mgr.is_option(자산) or asset["type"] == "stock_future"
            else "매" if asset.get("type") in {"국채", "corporate_bond"} else "개"
        )
        for row in book["asks"]:
            maker_text = ("국고 정산 호가" if asset["type"] == "stock_future" else "신규발행") if row.get("is_primary") else f"{row['order_count']}건"
            asks.append(
                f"`{row['limit_price']:>12,}` {currency} · {row['quantity']:,}{unit} · {maker_text}"
            )
        bids = [
            f"`{row['limit_price']:>12,}` {currency} · {row['quantity']:,}{unit} · {row['order_count']}건"
            for row in book["bids"]
        ]
        best_ask = int(book["asks"][0]["limit_price"]) if book["asks"] else None
        best_bid = int(book["bids"][0]["limit_price"]) if book["bids"] else None
        spread_text = (
            f"{best_ask - best_bid:,} {currency}"
            if best_ask is not None and best_bid is not None else "양쪽 호가가 있어야 계산 가능"
        )
        best_ask_text = f"{best_ask:,} {currency}" if best_ask is not None else "없음"
        best_bid_text = f"{best_bid:,} {currency}" if best_bid is not None else "없음"
        embed = discord.Embed(
            title=f"📖 {asset['name']} ({자산}) 호가",
            description=(
                f"현재 기준가 **{self.bot.market_mgr.format_price(asset['price'], asset['type'])} {currency}**\n"
                f"최우선 매수 {best_bid_text} · 최우선 매도 {best_ask_text} · 스프레드 {spread_text}"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="🔴 매도 대기 (최저가 우선)", value="\n".join(asks) or "대기 주문 없음", inline=False)
        embed.add_field(name="🔵 매수 대기 (최고가 우선)", value="\n".join(bids) or "대기 주문 없음", inline=False)
        embed.set_footer(text="가격 우선 · 같은 가격은 먼저 등록한 주문 우선 · 체결은 메이커 지정가")
        await self._reply(interaction, embed=embed)

    async def my_orders(self, interaction: discord.Interaction):
        user_id, _user_data, _role = await self._prepare(interaction)
        orders, open_orders = await asyncio.gather(
            asyncio.to_thread(self.bot.order_book.get_user_orders, user_id),
            asyncio.to_thread(self.bot.order_book.get_user_open_orders, user_id),
        )
        if not orders:
            return await self._reply(interaction, content="최근 24시간 주문과 미체결 주문이 없습니다.")
        view = MyOrdersView(self.bot, user_id, open_orders)
        await self._reply(
            interaction,
            embed=view.build_embed(orders),
            view=view if open_orders else None,
        )

    async def recipient_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.strip().casefold()
        requester_id = str(interaction.user.id)
        try:
            citizens = self.bot.db.users.execute(
                "SELECT user_id, nickname FROM users WHERE status = 'active' "
                "AND user_id <> ? ORDER BY nickname LIMIT 1000",
                (TREASURY_USER_ID,),
            )
            accounts = self.bot.db.accounts.execute(
                "SELECT account_number, user_id, product_code FROM accounts "
                "WHERE account_type = 'saving' ORDER BY account_number LIMIT 1000"
            )
            foreign_accounts = self.bot.db.accounts.execute("SELECT a.account_number,a.user_id,a.quote_currency AS currency_code,u.nickname "
                "FROM accounts a JOIN users u ON u.user_id=a.user_id WHERE a.account_type='currency_saving' AND u.status='active' ORDER BY a.account_number LIMIT 1000")
        except Exception:
            return []

        candidates = []
        treasury_terms = ("국고", "treasury", TREASURY_USER_ID.casefold())
        if not current or any(current in term for term in treasury_terms):
            candidates.append((True, app_commands.Choice(name="🏛️ 국고 · 공공 재정 납부"[:100], value=TREASURY_USER_ID)))
        for row in citizens:
            user_id = str(row["user_id"])
            nickname = str(row["nickname"] or user_id)
            if not current or current in nickname.casefold() or current in user_id.casefold():
                candidates.append((
                    user_id.casefold().startswith(current) if current else False,
                    app_commands.Choice(name=f"👤 {nickname} · ID {user_id}"[:100], value=user_id),
                ))
        citizen_names = {str(row["user_id"]): str(row["nickname"] or row["user_id"]) for row in citizens}
        for row in accounts:
            number = str(row["account_number"])
            nickname = citizen_names.get(str(row["user_id"]))
            if nickname is None:
                continue
            code = str(row["product_code"])
            if not current or current in number.casefold() or current in nickname.casefold() or current in code.casefold():
                candidates.append((
                    number.casefold().startswith(current) if current else False,
                    app_commands.Choice(
                        name=f"🏦 {nickname} · {code} · 계좌 {number}"[:100], value=number
                    ),
                ))
        for row in foreign_accounts:
            number,code,nickname = str(row['account_number']),str(row['currency_code']),str(row['nickname'])
            own = str(row['user_id']) == requester_id
            if not current or any(current in value.casefold() for value in (number,code,nickname)):
                candidates.append((number.casefold().startswith(current) if current else False,
                    app_commands.Choice(
                        name=(f"🌐 내 {code} 외환통장 · {number}" if own else
                              f"🌐 {nickname} · {code} 외환통장 · {number}")[:100],
                        value=number,
                    )))
        candidates.sort(key=lambda item: not item[0])
        if not current:
            # Keep both recipient types discoverable in the initial autocomplete list.
            treasury = [item for item in candidates if item[1].name.startswith("🏛️")][:1]
            users = [item for item in candidates if item[1].name.startswith("👤")][:12]
            own_currency_accounts = [
                item for item in candidates if item[1].name.startswith("🌐 내 ")
            ][:10]
            other_currency_accounts = [
                item for item in candidates
                if item[1].name.startswith("🌐") and item not in own_currency_accounts
            ]
            currency_accounts = (own_currency_accounts + other_currency_accounts)[:10]
            bank_accounts = [item for item in candidates if item[1].name.startswith("🏦")][:12-len(currency_accounts)]
            candidates = treasury + users + bank_accounts + currency_accounts
        return [item[1] for item in candidates[:25]]

    async def source_account_autocomplete(self, interaction: discord.Interaction, current: str):
        user_id = str(interaction.user.id)
        try:
            user = self.bot.db.users.get_user(user_id)
            accounts = self.bot.db.accounts.execute(
                "SELECT account_number, product_code, account_type, balance FROM accounts "
                "WHERE user_id = ? AND account_type<>'currency_saving' ORDER BY account_type, created_at",
                (user_id,),
            )
            foreign_accounts = self.bot.db.accounts.execute("SELECT *,quote_currency AS currency_code FROM accounts WHERE user_id=? AND account_type='currency_saving' ORDER BY quote_currency",(user_id,))
        except Exception:
            return []
        current = current.strip().casefold()
        options = []
        wallet_balance = float(user["cash"] or 0) if user else 0
        try:
            home = self.bot.currency_mgr.national_currency(user_id)[1]
        except ValueError:
            home = 'NWX'
        wallet_label = f"내 지갑 · {wallet_balance:,.0f} {home}"
        if not current or current in "지갑" or current in "__wallet__":
            options.append(app_commands.Choice(name=wallet_label[:100], value="__wallet__"))
        for row in accounts:
            number = str(row["account_number"])
            code = str(row["product_code"])
            if not current or current in number.casefold() or current in code.casefold():
                options.append(app_commands.Choice(
                    name=f"{code} · {number} · {float(row['balance'] or 0):,.0f} {home}"[:100],
                    value=number,
                ))
        for row in foreign_accounts:
            number,code = str(row['account_number']),str(row['currency_code'])
            if not current or current in number.casefold() or current in code.casefold():
                options.append(app_commands.Choice(name=f"{code} 외환통장 · {number} · {display_units(row['balance_units'])} {code}"[:100],value=number))
        return options[:25]

    async def open_account(self, interaction: discord.Interaction, 상품: str):
        user_id, _user_data, _role = await self._prepare(interaction)
        code = 상품.strip()
        foreign = self.bot.db.products.execute_one("SELECT * FROM saving WHERE product_code=? AND product_kind='currency_saving' AND status='active'",(code,))
        if foreign:
            try:
                number = await asyncio.to_thread(self.bot.currency_mgr.open_account,user_id,code)
                return await self._reply(interaction,content=f"✅ {foreign['name']} 외환통장 개설: `{number}`\n/송금으로 입금하고 /내지갑에서 잔액을 확인하세요.")
            except ValueError as exc:
                return await self._reply(interaction,content=f'❌ {exc}')
        product = self.bot.db.products.get_saving(code)
        if not product or product['status']!='active':
            return await self._reply(interaction,content='❌ 가입 가능한 일반·외환통장 상품 코드를 입력해주세요. /검색에서 코드를 확인할 수 있습니다.')
        view = AccountProductOpenView(self.bot,user_id,code)
        embed = discord.Embed(
            title="🏦 계좌 개설",
            description=f"**{product['name']}** (`{code}`)\n버튼을 눌러 비밀번호와 초기 예치금을 입력하세요.",
            color=discord.Color.blue(),
        )
        await self._reply(interaction, embed=embed, view=view)

    async def close_account(self, interaction: discord.Interaction):
        user_id, _user_data, _role = await self._prepare(interaction)
        await asyncio.to_thread(self.bot.interest_mgr.process_user_interests, user_id)
        rows = self.bot.db.accounts.execute(
            "SELECT account_number, product_code, balance FROM accounts "
            "WHERE user_id = ? AND account_type = 'saving' ORDER BY created_at",
            (user_id,),
        )
        if not rows:
            return await self._reply(interaction, content="❌ 해지할 수 있는 본인 명의 통장이 없습니다.")
        options = []
        for row in rows:
            product = self.bot.db.products.get_saving(row["product_code"])
            name = product["name"] if product else row["product_code"]
            options.append({
                "account_number": row["account_number"],
                "label": f"{name} · 끝번호 {str(row['account_number'])[-4:]}",
                "description": f"잔액 {float(row['balance'] or 0):,.0f} NWX",
            })
        view = AccountCloseView(self.bot, user_id, options)
        embed = discord.Embed(
            title="🏦 계좌 해지",
            description="통장을 선택한 뒤 비밀번호를 확인하세요. 잔액과 통장 귀속 자산은 본인 다른 통장(없으면 지갑)으로 이동합니다.",
            color=discord.Color.orange(),
        )
        await self._reply(interaction, embed=embed, view=view)

    @app_commands.describe(id='신청할 대출 상품 ID (/검색 → 대출 상품에서 확인)')
    async def apply_loan(self, interaction: discord.Interaction, id: str):
        user_id, _user_data, _role = await self._prepare(interaction)
        code = id.strip()
        product = self.bot.db.products.get_liability(code)
        if not product or product['status'] != 'active':
            return await self._reply(interaction, content='❌ 신청 가능한 대출 상품 ID를 입력해주세요. /검색 → 대출 상품에서 ID를 확인할 수 있습니다.')
        view = LoanProductApplyView(self.bot, user_id, code)
        embed = discord.Embed(
            title="💳 대출 신청",
            description=f"**{product['name']}** (`{code}`)\n버튼을 눌러 신청 금액을 입력하세요.",
            color=discord.Color.blue(),
        )
        await self._reply(interaction, embed=embed, view=view)

    @app_commands.describe(id='본인 보유 대출 번호 또는 상품 ID (/내지갑 → 대출에서 확인)')
    async def extend_loan(self, interaction: discord.Interaction, id: str):
        user_id, _user_data, _role = await self._prepare(interaction)
        success, message = await asyncio.to_thread(self.bot.bank_mgr.extend_loan, user_id, id.strip())
        await self._reply(interaction, embed=self._result_embed('💳 대출 만기 연장', message, success))

    async def repay_loan(self, interaction: discord.Interaction):
        user_id, _user_data, _role = await self._prepare(interaction)
        await asyncio.to_thread(self.bot.interest_mgr.process_user_interests, user_id)
        rows = self.bot.db.accounts.execute(
            "SELECT loan_id, product_code, outstanding_balance FROM loans "
            "WHERE user_id = ? AND status IN ('active','defaulted') ORDER BY created_at",
            (user_id,),
        )
        if not rows:
            return await self._reply(interaction, content="❌ 상환할 대출이 없습니다.")
        loans = []
        for row in rows:
            product = self.bot.db.products.get_liability(row["product_code"])
            name = product["name"] if product else row["product_code"]
            loans.append({
                "loan_id": row["loan_id"],
                "label": name,
                "description": f"남은 원리금 {float(row['outstanding_balance'] or 0):,.0f} NWX",
            })
        view = LoanRepaymentView(self.bot, user_id, loans)
        embed = discord.Embed(
            title="💳 대출 상환",
            description="상환할 대출을 선택한 뒤 금액을 입력하세요.",
            color=discord.Color.blue(),
        )
        await self._reply(interaction, embed=embed, view=view)

    async def search(self, interaction: discord.Interaction):
        user_id, _user_data, _role = await self._prepare(interaction)
        view = InfoPanelView(self.bot, user_id)
        try:
            await self._reply(interaction, embed=view.build_embed(), view=view)
        except ValueError as exc:
            await self._reply(interaction, content=f"❌ 정보 목록을 불러오지 못했습니다: {exc}")


    @app_commands.describe(product_id="회사 주식 또는 우선주 종목 코드")
    async def financial(self, interaction: discord.Interaction, product_id: str):
        user_id, _, _ = await self._prepare(interaction, defer=True)
        from managers.financial_manager import FinancialManager
        from functions.financial_views import FinancialReportView
        manager = FinancialManager(self.bot.db)
        try:
            report = await asyncio.to_thread(manager.report, product_id.strip())
            periods = await asyncio.to_thread(manager.periods, product_id.strip())
            view = FinancialReportView(self.bot.db, user_id, report, periods)
            await self._reply(interaction, embed=view.build_embed(), view=view)
        except ValueError as exc:
            await self._reply(interaction, content=f"❌ {exc}")

    async def movement(self, interaction: discord.Interaction, 종목: str):
        자산 = 종목.strip()
        user_id, _user_data, _role = await self._prepare(interaction, defer=True)
        if not 자산:
            return await self._reply(interaction,content='❌ 종목 코드를 입력해주세요. `/검색`에서 코드를 확인할 수 있습니다.')

        try:
            view = PriceMovementView(self.bot, user_id, 자산)
            embed, png = await asyncio.to_thread(view.render)
        except Exception as exc:
            return await self._reply(
                interaction, content=f"❌ 그래프를 만들지 못했습니다: {exc}"
            )
        file = discord.File(io.BytesIO(png), filename="movement.png")
        await self._reply(interaction, embed=embed, view=view, file=file)

    async def company_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = self.bot.db.companies.execute(
            "SELECT company_code, name FROM company WHERE status = 'active' ORDER BY name"
        )
        current = current.casefold()
        return [
            app_commands.Choice(name=f"{row['name']} ({row['company_code']})"[:100], value=str(row["company_code"]))
            for row in rows
            if 0 < len(str(row["company_code"])) <= 100
            and (not current or current in str(row["name"]).casefold() or current in str(row["company_code"]).casefold())
        ][:25]

    async def shareholder_meeting(
        self,
        interaction: discord.Interaction,
        주식코드: str,
        장소: app_commands.Range[str, 1, 500],
    ):
        user_id, _user_data, role = await self._prepare(interaction)
        company = self.bot.db.companies.get_company(주식코드)
        if not company:
            return await self._reply(interaction, content="❌ 회사 주식 코드를 찾을 수 없습니다.")
        if 'share_class' in company.keys() and company['share_class'] == 'preferred':
            return await self._reply(interaction, content='❌ 우선주는 의결권이 없습니다. 존속 법인의 보통주 코드로 주주총회를 소집해주세요.')
        holders = self.bot.db.assets.execute(
            "SELECT user_id, SUM(quantity) AS shares FROM ("
            "SELECT user_id, quantity FROM user_assets WHERE asset_code = ? AND quantity > 0 "
            "UNION ALL SELECT writer_id AS user_id, quantity FROM option_collateral_lots "
            "WHERE underlying_code = ? AND quantity > 0) "
            "GROUP BY user_id ORDER BY shares DESC, user_id ASC",
            (주식코드, 주식코드),
        )
        if not holders:
            return await self._reply(interaction, content="❌ 해당 주식을 보유한 주주가 없습니다.")
        largest = holders[0]["shares"]
        largest_ids = {str(row["user_id"]) for row in holders if row["shares"] == largest}
        if role != "admin" and user_id not in largest_ids:
            return await self._reply(interaction, content="❌ 해당 종목 최대주주 또는 관리자만 소집할 수 있습니다.")

        notice = discord.Embed(
            title=f"📣 {company['name']} 주주총회 공지",
            description="아래 버튼으로 참석 여부를 선택해주세요. 응답은 DM 수신 후 1시간 동안 가능합니다.",
            color=discord.Color.gold(),
        )
        notice.add_field(name="장소", value=장소[:1024], inline=False)
        notice.add_field(name="주식 코드", value=주식코드, inline=True)
        sent = failed = 0
        for holder in holders:
            try:
                holder_id = int(holder["user_id"])
                target = self.bot.get_user(holder_id) or await self.bot.fetch_user(holder_id)
                view = ShareholderMeetingResponseView(
                    self.bot, holder_id, int(user_id), company["name"], 주식코드, 장소
                )
                view.message = await target.send(embed=notice, view=view)
                sent += 1
            except (discord.HTTPException, ValueError, TypeError):
                failed += 1
        await interaction.followup.send(
            content=f"✅ 주주총회 DM 전달: {sent}명 성공 · {failed}명 실패",
            ephemeral=True,
        )
