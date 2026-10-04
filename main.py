import discord
import asyncio
from discord import app_commands
from database import DatabaseManager
from managers.user_manager import UserManager
from managers.market_manager import MarketManager
from managers.bank_manager import BankManager
from managers.admin_manager import AdminManager
from managers.interest_manager import InterestManager
from managers.order_book_manager import OrderBookManager
from managers.option_manager import OptionManager
from managers.rate_manager import RateManager
from managers.investment_manager import InvestmentManager
from managers.futures_manager import FuturesManager
from managers.fx_manager import FXManager
from managers.admin_control_manager import AdminControlManager
from managers.corporate_actions_manager import CorporateActionsManager
from managers.market_session_manager import MarketSessionManager
from functions.admin_prefix import AdminPrefixRegistry
from functions.admin_access import BOOTSTRAP_ADMIN_IDS
from functions.interaction_reliability import (
    retry_discord_request,
    send_interaction_notice,
)
from functions.registration import RegistrationRequired


class InazinaBot(discord.Client):
    MARKET_UPDATE_INTERVAL_SECONDS = 1
    LOAN_PAYMENT_POLL_INTERVAL_SECONDS = 5
    BACKGROUND_ERROR_LOG_INTERVAL_SECONDS = 60

    def __init__(self, token):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents, activity=discord.Game(name="숏치기"))
        
        self.token = token
        self.db = DatabaseManager()
        self.user_mgr = UserManager(self.db)
        self.order_book = OrderBookManager(self.db, self.user_mgr)
        self.option_mgr = OptionManager(self.db, self.user_mgr)
        self.order_book.option_manager = self.option_mgr
        self.rate_mgr = RateManager(self.db)
        self.investment_mgr = InvestmentManager(self.db, self.user_mgr)
        self.futures_mgr = FuturesManager(self.db, self.user_mgr)
        self.fx_mgr = FXManager(self.db)
        from managers.lottery_manager import LotteryManager
        self.lottery_mgr = LotteryManager(self.db)
        self.order_book.futures_manager = self.futures_mgr
        self.market_mgr = MarketManager(self.db)
        self.bank_mgr = BankManager(self.db)
        self.admin_mgr = AdminManager(self.db)
        self.interest_mgr = InterestManager(self.db)
        self.treasury_bond_mgr = self.user_mgr.bond_manager
        self.tree = app_commands.CommandTree(self)
        self.command_registry = None
        self.admin_control = AdminControlManager(self)
        self.corporate_actions = CorporateActionsManager(self)
        self.market_session = MarketSessionManager(self.db)
        self.prefix_registry = AdminPrefixRegistry(self)
        self._guild_commands_cleaned = False
        self._background_loops_started = False
        self._last_market_error_log = 0.0
        self._last_loan_error_log = 0.0
        self._last_option_error_log = 0.0
        self._last_product_error_log = 0.0
        self._resources_closed = False

    async def setup_hook(self):
        from functions.slash_commands import SlashCommandRegistry

        self.command_registry = SlashCommandRegistry(self)
        self.command_registry.register()
        self.tree.error(self.on_app_command_error)
        print(f"Prepared {len(self.tree.get_commands())} slash commands for guild sync.")

    async def on_app_command_error(self, interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, RegistrationRequired):
            return
        command_name = interaction.command.name if interaction.command else "unknown"
        if isinstance(original, discord.NotFound) and getattr(original, "code", None) == 10062:
            print(f"[Expired Interaction] /{command_name} — Discord 응답 제한시간 초과")
            return
        if isinstance(original, discord.DiscordServerError):
            print(f"[Discord API {original.status}] /{command_name} — 일시적 서버 오류")
            message = (
                "⚠️ Discord 서버 응답이 일시적으로 불안정합니다. "
                "잠시 후 명령어를 다시 실행해주세요."
            )
        else:
            print(f"[Slash Command Error] /{command_name}: {original}")
            message = "❌ 명령어 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        await send_interaction_notice(interaction, message)

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('Inazina System Ready.')
        if self.user.name != 'Inazina':
            try:
                await self.user.edit(username='Inazina')
            except discord.HTTPException as exc:
                print(f'[Bot Name Update Error] {exc}')
        if not self._guild_commands_cleaned:
            try:
                guilds = list(self.guilds)
                if not guilds:
                    # Keep global commands available if the client has not joined
                    # any guild (or the guild cache is unexpectedly empty).
                    synced = await self.tree.sync()
                    print(f"Slash commands registered globally: {len(synced)}")
                else:
                    synced_total = 0
                    for guild in guilds:
                        # Guild commands update immediately. Replace stale guild
                        # copies with the current definitions before removing the
                        # global copies, so commands appear without waiting for
                        # global-command propagation and are not duplicated long-term.
                        self.tree.copy_global_to(guild=guild)
                        synced_total += len(await self.tree.sync(guild=guild))
                    self.tree.clear_commands(guild=None)
                    await self.tree.sync()
                    print(
                        f"Synced {synced_total} guild slash commands across "
                        f"{len(guilds)} server(s); removed global copies."
                    )
                self._guild_commands_cleaned = True
            except discord.HTTPException as exc:
                print(f"[Guild Slash Command Sync Error] {exc}")
        cached_nicknames = {}
        for guild in self.guilds:
            for member in guild.members:
                if not member.bot:
                    cached_nicknames.setdefault(str(member.id), member.display_name)
        if cached_nicknames:
            await asyncio.to_thread(
                self.db.users.update_nicknames, cached_nicknames.items()
            )
        if not self._background_loops_started:
            self._background_loops_started = True
            print(
                "Market Price Auto-Update Loop Started "
                f"(Every {self.MARKET_UPDATE_INTERVAL_SECONDS}s)."
            )
            self.loop.create_task(self.market_update_loop(), name="market-price-worker")
            self.loop.create_task(self.scheduled_loan_payment_loop(), name="scheduled-loan-worker")

    async def on_member_update(self, before, after):
        if not after.bot and before.display_name != after.display_name:
            await asyncio.to_thread(
                self.db.users.update_nickname, str(after.id), after.display_name
            )

    async def on_message(self, message):
        await self.prefix_registry.handle(message)

    async def market_update_loop(self):
        """Update prices off the Discord event loop; busy DB ticks are skipped."""
        while not self.is_closed():
            try:
                await asyncio.to_thread(self.market_mgr.update_prices)
            except Exception as e:
                now = asyncio.get_running_loop().time()
                if now - self._last_market_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL_SECONDS:
                    self._last_market_error_log = now
                    print(f"[Market Worker] {type(e).__name__}: {e}")
            await asyncio.sleep(self.MARKET_UPDATE_INTERVAL_SECONDS)

    async def scheduled_loan_payment_loop(self):
        """Poll scheduled installments; interest itself is settled lazily on account access."""
        while not self.is_closed():
            try:
                try:
                    await asyncio.to_thread(self.market_session.process_due)
                    await asyncio.to_thread(self.corporate_actions.process_due)
                except Exception as exc:
                    now = asyncio.get_running_loop().time()
                    if now - self._last_product_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL_SECONDS:
                        self._last_product_error_log = now
                        print(f"[Dividend/Session Worker] {type(exc).__name__}: {exc}")
                try:
                    await asyncio.to_thread(self.lottery_mgr.process_due)
                except Exception as exc:
                    now = asyncio.get_running_loop().time()
                    if now - self._last_product_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL_SECONDS:
                        self._last_product_error_log = now
                        print(f"[Lottery Worker] {type(exc).__name__}: {exc}")
                try:
                    future_results = await asyncio.to_thread(self.futures_mgr.settle_positions)
                    corporate_results = await asyncio.to_thread(self.investment_mgr.redeem_corporate_bonds)
                    for result in future_results + corporate_results:
                        try:
                            recipient_id = int(result["user_id"])
                            recipient = self.get_user(recipient_id) or await self.fetch_user(recipient_id)
                            reason = {"futures_maturity": "선물 만기", "futures_liquidation": "선물 유지증거금 미달 청산"}.get(result.get("reason"), "회사채 만기")
                            await recipient.send(embed=discord.Embed(title=f"📄 {reason}",
                                description=f"`{result['product_code']}` 정산 · 지갑 입금 **{result['net']:,} NWX**", color=discord.Color.gold()))
                        except (discord.HTTPException, ValueError, TypeError):
                            pass
                except Exception as exc:
                    now = asyncio.get_running_loop().time()
                    if now - self._last_product_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL_SECONDS:
                        self._last_product_error_log = now
                        print(f"[Product Worker] {type(exc).__name__}: {exc}")
                try:
                    option_expirations = await asyncio.to_thread(
                        self.option_mgr.expire_matured
                    )
                except Exception as e:
                    option_expirations = []
                    now = asyncio.get_running_loop().time()
                    if now - self._last_option_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL_SECONDS:
                        self._last_option_error_log = now
                        print(f"[Option Worker] {type(e).__name__}: {e}")
                for result in option_expirations:
                    try:
                        recipient_id = int(result["user_id"])
                        recipient = self.get_user(recipient_id) or await self.fetch_user(recipient_id)
                        option_name = "콜" if result["option_type"] == "call" else "풋"
                        notice = discord.Embed(
                            title=("📄 옵션 작성 계약 만기" if result.get("is_writer") else "📄 옵션 만기 정산"),
                            description=(
                                f"`{result['contract_code']}` · {result['underlying_code']} "
                                f"{option_name} · 만기 주가 {result['spot_price']:,} NWX · "
                                f"행사가 {result['strike_price']:,} NWX"
                            ),
                            color=(discord.Color.green() if result["exercised"] else discord.Color.orange()),
                        )
                        notice.add_field(
                            name="행사 수량",
                            value=f"{result['exercised']:,} / {result['quantity']:,}계약",
                            inline=True,
                        )
                        if result.get("is_writer"):
                            settlement = (
                                f"행사로 배정된 계약 {result['exercised']:,}건 · "
                                "남은 주식 또는 현금 담보는 작성자에게 반환했습니다."
                            )
                        elif result["option_type"] == "call":
                            settlement = (
                                f"기초주식 {result['exercised']:,}주 인도 · "
                                f"행사대금 {result['cash_paid']:,} NWX 지급"
                                if result["exercised"] else "행사하지 않은 계약은 만료되었습니다."
                            )
                        else:
                            settlement = (
                                f"기초주식 {result['exercised']:,}주 인도 · "
                                f"정산 입금 {result['cash_received']:,} NWX"
                                if result["exercised"] else "행사하지 않은 계약은 만료되었습니다."
                            )
                        notice.add_field(name="정산", value=settlement, inline=False)
                        await recipient.send(embed=notice)
                    except Exception as exc:
                        print(f"[Option Expiry Notice Error] {result['user_id']}: {exc}")
                bond_redemptions = await asyncio.to_thread(
                    self.treasury_bond_mgr.redeem_matured
                )
                for bond in bond_redemptions:
                    try:
                        recipient_id = int(bond["user_id"])
                        recipient = self.get_user(recipient_id) or await self.fetch_user(recipient_id)
                        notice = discord.Embed(
                            title="🏛️ 국채 만기 자동 상환",
                            description=f"**{bond['name']}** 국채가 만기되어 자동 상환됐습니다.",
                            color=discord.Color.gold(),
                        )
                        notice.add_field(name="원금", value=f"{bond['principal']:,} NWX", inline=True)
                        notice.add_field(name="이자", value=f"{bond['interest']:,} NWX", inline=True)
                        notice.add_field(name="세금", value=f"{bond['tax']:,} NWX", inline=True)
                        notice.add_field(name="지갑 입금", value=f"**{bond['net']:,} NWX**", inline=False)
                        await recipient.send(embed=notice)
                    except (discord.HTTPException, ValueError, TypeError):
                        pass
                paid_count, missed_count, payment_notifications = await asyncio.to_thread(
                    self.bank_mgr.process_scheduled_loan_payments
                )
                for result in payment_notifications:
                    try:
                        recipient_id = int(result["user_id"])
                        recipient = self.get_user(recipient_id) or await self.fetch_user(recipient_id)
                        notice = discord.Embed(
                            title="💳 대출 정기납부 결과",
                            description=f"상품: **{result['product_name']}**",
                            color=(discord.Color.green() if not result["missed_count"] else discord.Color.orange()),
                        )
                        if result["paid_count"]:
                            notice.add_field(
                                name="자동 상환",
                                value=f"{result['paid_count']}회 · {result['paid_amount']:,.0f} NWX",
                                inline=False,
                            )
                        if result["missed_count"]:
                            notice.add_field(
                                name="미납 및 신용 점수",
                                value=f"미납 {result['missed_count']}회 · 회당 1점 차감",
                                inline=False,
                            )
                        notice.add_field(
                            name="남은 원리금", value=f"{result['remaining']:,.0f} NWX", inline=False
                        )
                        await recipient.send(embed=notice)
                    except (discord.HTTPException, ValueError, TypeError) as exc:
                        print(f"[Loan Repayment Notice Error] {result['user_id']}: {exc}")
            except Exception as e:
                now = asyncio.get_running_loop().time()
                if now - self._last_loan_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL_SECONDS:
                    self._last_loan_error_log = now
                    print(f"[Loan Worker] {type(e).__name__}: {e}")
            await asyncio.sleep(self.LOAN_PAYMENT_POLL_INTERVAL_SECONDS)

    async def ensure_user(self, user_id, nickname):
        user_id = str(user_id)
        nickname = str(nickname or user_id)
        user = self.db.users.get_user(user_id)
        if not user:
            if user_id not in BOOTSTRAP_ADMIN_IDS:
                return None
            self.db.users.register_user(
                user_id, nickname, "낙원", 1000000, role="admin"
            )
            return self.db.users.get_user(user_id)
        changed = False
        if user_id in BOOTSTRAP_ADMIN_IDS and str(user["role"] or "").lower() != "admin":
            self.db.users.update_role(user_id, "admin")
            changed = True
        if user["nickname"] != nickname:
            self.db.users.update_nickname(user_id, nickname)
            changed = True
        return self.db.users.get_user(user_id) if changed else user

    async def register_user(self, user_id, nickname):
        """Create a citizen only after they accept the registration notice."""
        user_id = str(user_id)
        nickname = str(nickname or user_id)
        role = "admin" if user_id in BOOTSTRAP_ADMIN_IDS else "user"
        created = await asyncio.to_thread(
            self.db.users.register_user,
            user_id,
            nickname,
            "낙원",
            1000000,
            role,
        )
        if not created:
            await asyncio.to_thread(self.db.users.update_nickname, user_id, nickname)
        return created
    
    async def close(self):
        # Discord closes the HTTP session before in-flight event handlers finish.
        # Close SQLite off the event loop, since a command worker may briefly own
        # the shared DB lock during Ctrl+C shutdown.
        await super().close()
        if not self._resources_closed:
            self._resources_closed = True
            await asyncio.to_thread(self.db.close)

    def run(self, token):
        super().run(token)


# Preserve integrations that imported the old class while the product is renamed.
NaconomicsBot = InazinaBot
