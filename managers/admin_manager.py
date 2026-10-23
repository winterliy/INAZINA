from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.money import parse_money
from functions.admin_access import resolve_user_role
import time
import math
import json
from decimal import Decimal, ROUND_HALF_UP

class AdminManager:
    """관리자 전용 기능을 관리하는 클래스"""
    def __init__(self, db: DatabaseManager):
        self.db = db

    def setup_user(self, user_id, nickname=None, nationality="낙원"):
        """특정 유저를 시스템에 등록"""
        nickname = str(nickname or user_id)
        existing = self.db.users.get_user(user_id)
        if existing:
            if existing["nickname"] != nickname:
                self.db.users.update_nickname(user_id, nickname)
                return f"ℹ️ 사용자 {user_id}는 이미 등록되어 있어 닉네임만 `{nickname}`으로 갱신했습니다."
            return f"ℹ️ 사용자 {user_id}는 이미 등록되어 있습니다."
        self.db.users.create_user(user_id, nickname, nationality)
        return f"✅ 사용자 {user_id}가 성공적으로 등록되었습니다."

    def create_market_event(self, actor_id, event_type, severity, asset_code=None, duration_seconds=3600, now=None,
                            target_price=None, target_percent=None, created=None, stacking=False):
        """Register a 호재/악재. ``created`` (a dict, optional) receives the new ``event_id``.

        A ``stacking`` event (news) adds its move to events already running on
        the same assets; an ordinary event replaces them.
        """
        actor = self.db.users.get_user(str(actor_id))
        if not actor or str(actor['role']).strip().lower() != "admin":
            return False, "❌ 관리자 전용 기능입니다."
        if event_type not in {"good", "bad"} or isinstance(severity, bool) or not isinstance(severity, int) or not 0 <= severity <= 100:
            return False, "❌ 호재/악재와 0~100 사이 정수 강도를 선택해주세요."
        if not isinstance(duration_seconds, int) or isinstance(duration_seconds, bool) or not 1 <= duration_seconds <= 604800:
            return False, "❌ 사건 지속시간은 1~604,800초여야 합니다."
        asset_code = (str(asset_code).strip() or None) if asset_code else None
        try:
            percent = float(severity if target_percent is None else target_percent)
            if isinstance(target_percent, bool) or not math.isfinite(percent) or not 0 <= percent <= 100:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            return False, "❌ 변동률은 0~100% 사이의 숫자여야 합니다."
        if target_price is not None:
            if not asset_code:
                return False, "❌ 목표 가격을 지정하려면 개별 자산을 선택해주세요."
            if isinstance(target_price, bool) or not isinstance(target_price, int) or not 1 <= target_price <= 2**53 - 1:
                return False, "❌ 목표 가격은 1~9,007,199,254,740,991 기준 통화 사이의 정수여야 합니다."
            mode, value = "price", target_price
        else:
            mode, value = "percent", percent
        from managers.market_manager import MarketManager
        market = MarketManager(self.db)
        if asset_code:
            try:
                asset = market.get_asset_info(asset_code)
                if not asset:
                    matches = [item for item in market.get_event_assets()
                               if asset_code.casefold() in (item["code"].casefold(), item["name"].casefold(),
                                                           f"{item['name']} ({item['code']})".casefold())]
                    if len(matches) == 1:
                        asset, asset_code = matches[0], matches[0]["code"]
                if not asset:
                    return False, "❌ 선택한 자산을 찾을 수 없습니다. 모달에서 현재 거래 중인 자산을 선택해주세요."
                if not market.event_asset_eligible(asset):
                    return False, "❌ 만료·고정 상품은 사건을 적용할 수 없습니다."
            except ValueError as exc:
                return False, f"❌ {exc}"
        from managers.rate_manager import RateManager
        now = int(RateManager._validated_epoch(now, "invalid_market_event_epoch"))
        plans = {}
        with self.db.atomic(("system", "assets", "company", "product")) as conn:
            role = conn.execute('SELECT role FROM users WHERE user_id=?',(str(actor_id),)).fetchone()
            if not role or str(role['role']).strip().lower()!='admin':
                return False, '❌ 관리자 권한이 취소되었습니다.'
            for schema, table, code_column, price_column in (
                ("assets", "market_assets", "code", "price"),
                ("company", "company", "company_code", "share_price"),
                ("product", "investment", "product_code", "unit_price"),
            ):
                rows = conn.execute(f"SELECT * FROM {schema}.{table} WHERE status = 'active'").fetchall()
                for row in rows:
                    code = str(row[code_column])
                    if asset_code and code != asset_code:
                        continue
                    if int(row["is_fixed"] or 0):
                        continue
                    if schema == "assets" and row["type"] in {"option", "국채", "corporate_bond", "stock_future", "mutual_fund", "index_fund"}:
                        continue
                    if schema == "product" and (
                        row["underlying_exchange_id"] or row["underlying_code"]
                        or row["product_type"] in {"option", "국채", "stock_future", "corporate_bond", "mutual_fund", "index_fund"}
                        or MarketManager._investment_matured(row["created_at"], row["maturity_days"])
                    ) and not asset_code:
                        continue
                    quote=conn.execute('SELECT price FROM market_quote_overrides WHERE asset_code=?',(code,)).fetchone()
                    price = MarketManager.normalize_market_price(quote['price'] if quote else row[price_column])
                    if price <= 0:
                        continue
                    if code in plans:
                        return False, f"❌ 자산 코드 `{code}`가 중복 등록되어 있습니다."
                    if mode == "price":
                        if (event_type == "good" and value < price) or (event_type == "bad" and value > price):
                            return False, "❌ 호재 목표는 현재 가격 이상, 악재 목표는 현재 가격 이하로 지정해주세요."
                        target = int(value)
                    else:
                        sign = 1 if event_type == "good" else -1
                        target = min(2**53-1,max(1, int((Decimal(price) * (1 + sign * Decimal(str(value)) / 100))
                                           .quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
                    if mode == "price" or value > 0:
                        plans[code] = (price, target)
            # Options and treasury bonds use a quote overlay: changing a quote
            # must never alter the strike, face value, collateral or redemption.
            if asset_code and asset_code not in plans and asset['type'] in {'option', '국채'}:
                quote=conn.execute('SELECT price FROM market_quote_overrides WHERE asset_code=?',(asset_code,)).fetchone()
                price = int(quote['price'] if quote else asset['price'])
                if mode == 'price' and ((event_type == 'good' and value < price) or (event_type == 'bad' and value > price)):
                    return False, "❌ 호재는 현재가 이상, 악재는 현재가 이하의 목표를 지정해주세요."
                target = int(value) if mode == 'price' else min(2**53-1,max(1, int((Decimal(price)*(1+(1 if event_type=='good' else -1)*Decimal(str(value))/100)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))))
                if mode == 'price' or value > 0:
                    plans[asset_code] = (price,target)
            if not plans and (mode == "price" or value > 0):
                return False, "❌ 사건을 적용할 변동 가격 자산이 없습니다. 고정·만료·연동 상품은 제외됩니다."
            event_id = conn.execute(
                "INSERT INTO system.market_events(asset_code,event_type,severity,starts_epoch,ends_epoch,created_by,target_mode,target_value,volatility_percent,stacking) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (asset_code or None, event_type, severity, now, now + duration_seconds,
                 str(actor_id), mode, value, percent, 1 if stacking else 0),
            ).lastrowid
            if created is not None:
                created["event_id"] = event_id
            if mode=='percent' and value==0 and not stacking:
                conn.execute(
                    "UPDATE market_event_targets SET state='superseded' WHERE state='active' "
                    "AND (? IS NULL OR asset_code=?)", (asset_code, asset_code)
                )
                conn.execute("DELETE FROM market_quote_overrides WHERE ? IS NULL OR asset_code=?",(asset_code,asset_code))
            for code, (price, target) in plans.items():
                if not stacking:
                    conn.execute(
                        "UPDATE system.market_event_targets SET state='superseded' "
                        "WHERE asset_code=? AND state='active'", (code,)
                    )
                conn.execute(
                    "INSERT INTO system.market_event_targets(event_id,asset_code,start_price,target_price,start_epoch,end_epoch,last_tick_epoch,last_price) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, code, price, target, now, now + duration_seconds, now, price),
                )
        label = "호재" if event_type == "good" else "악재"
        target_label = f"목표 {target_price:,} 기준 통화" if mode == "price" else f"목표 {'+' if event_type == 'good' else '-'}{value:g}%"
        return True, (f"✅ {asset_code or '시장 전체'}에 {label} 사건을 등록했습니다. "
                      f"{duration_seconds:,}초 동안 {percent:g}% 강도로 무작위 등락하며 {target_label}에 도달합니다.")

    def setup_all(self, member_list):
        """서버 멤버 리스트를 받아 일괄 등록"""
        count = 0
        existed = 0
        renamed = 0
        for member in member_list:
            if not member.bot:
                user_id = str(member.id)
                existing = self.db.users.get_user(user_id)
                if existing:
                    existed += 1
                    nickname = str(member.display_name)
                    if existing["nickname"] != nickname:
                        self.db.users.update_nickname(user_id, nickname)
                        renamed += 1
                else:
                    self.db.users.create_user(user_id, member.display_name, "낙원")
                    count += 1
        return f"✅ 신규 등록 {count}명 · 기존 등록 {existed}명 · 닉네임 갱신 {renamed}명입니다."

    def distribute_basic_income(self, nationality, amount, actor=None):
        """Pay ``amount`` of one country's currency to each of its active citizens from its treasury."""
        from managers.currency_manager import CurrencyManager
        from managers.currency_exchange_manager import amount_units, display_units
        import uuid
        try:
            nationality=str(nationality).strip()
            units=amount_units(amount)
            with self.db.atomic() as conn:
                row=conn.execute("SELECT n.currency_code FROM country_currencies n JOIN currencies c ON c.code=n.currency_code "
                                 "WHERE n.nationality=? AND c.status='active'",(nationality,)).fetchone()
                if not row:
                    raise ValueError(f'등록된 국가가 아닙니다: {nationality}')
                currency=str(row[0])
                CurrencyManager.validate_units(conn,units,currency)
                from database.user_db import citizen_filter
                recipients=[r[0] for r in conn.execute(
                    f"SELECT user_id FROM users WHERE status='active' AND nationality=? AND {citizen_filter()} ORDER BY user_id",
                    (nationality,)).fetchall()]
                if not recipients:
                    return False,f'❌ {nationality}에 기본소득을 받을 활성 시민이 없습니다.'
                treasury=CurrencyManager.treasury_id(conn,nationality)
                rate=str(CurrencyManager.rate_fraction(conn,currency))
                total=units*len(recipients)
                after=CurrencyManager.change(conn,treasury,currency,-total,allow_deficit=True)
                batch=uuid.uuid4().hex; now=time.time()
                for user_id in recipients:
                    CurrencyManager.change(conn,user_id,currency,units)
                    conn.execute('INSERT INTO basic_income_payments(batch_id,user_id,source_currency,target_currency,source_units,target_units,source_rate,target_rate,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?)',
                                 (batch,user_id,currency,currency,units,units,rate,rate,now))
            return True,(f'✅ {nationality} 시민 {len(recipients)}명에게 1인당 {display_units(units)} {currency} 지급 · {nationality} 국고 {display_units(total)} {currency} 차감 · 잔액 {display_units(after)} {currency}'+(' (적자)' if after<0 else ''))
        except (ValueError,OverflowError) as exc:
            return False,f'❌ 기본소득 지급 실패: {exc}'
