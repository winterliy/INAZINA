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
                            target_price=None, target_percent=None):
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
                return False, "❌ 목표 가격은 1~9,007,199,254,740,991 NWX 사이의 정수여야 합니다."
            mode, value = "price", target_price
        else:
            mode, value = "percent", percent
        from managers.market_manager import MarketManager
        if asset_code:
            market = MarketManager(self.db)
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
        now = int(time.time() if now is None else now)
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
            if mode=='percent' and value==0:
                conn.execute("DELETE FROM market_quote_overrides WHERE ? IS NULL OR asset_code=?",(asset_code,asset_code))
        if mode == 'percent' and value == 0:
            market.clear_runtime_targets(asset_code)
        else:
            market.register_runtime_events(plans, event_type=event_type,
                volatility_percent=percent, now=now, duration_seconds=duration_seconds)
        label = "호재" if event_type == "good" else "악재"
        target_label = f"목표 {target_price:,} NWX" if mode == "price" else f"목표 {'+' if event_type == 'good' else '-'}{value:g}%"
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

    def distribute_basic_income(self, amount, actor=None):
        """활성 시민에게 1인당 지급하며, 재원은 users.db의 국고에서 뺍니다."""
        try:
            amount = parse_money(amount, minimum=1)
        except ValueError as exc:
            return False, f"❌ 기본소득은 정수 NWX로 입력해주세요. ({exc})"

        try:
            with self.db.atomic(("users",)) as conn:
                recipients = conn.execute(
                    "SELECT user_id FROM users.users "
                    "WHERE status = 'active' AND user_id != ? ORDER BY user_id",
                    (TREASURY_USER_ID,),
                ).fetchall()
                if not recipients:
                    return False, "❌ 기본소득을 받을 활성 시민이 없습니다."

                total = amount * len(recipients)
                treasury = conn.execute(
                    "SELECT cash FROM users.users WHERE user_id = ?",
                    (TREASURY_USER_ID,),
                ).fetchone()
                balance = int(round(treasury["cash"] or 0)) if treasury else 0

                debit = conn.execute(
                    "UPDATE users.users SET cash = cash - ? WHERE user_id = ?",
                    (total, TREASURY_USER_ID),
                )
                if debit.rowcount != 1:
                    return False, "❌ 국고 잔액이 변경되어 기본소득 지급을 취소했습니다."

                for recipient in recipients:
                    credit = conn.execute(
                        "UPDATE users.users SET cash = cash + ? "
                        "WHERE user_id = ? AND status = 'active'",
                        (amount, recipient["user_id"]),
                    )
                    if credit.rowcount != 1:
                        raise RuntimeError("지급 대상이 변경되어 기본소득 지급을 취소했습니다.")
            after = balance - total
            deficit_note = f" · 국고 잔액 {after:,} NWX (적자)" if after < 0 else f" · 국고 잔액 {after:,} NWX"
            return True, (
                f"✅ {len(recipients)}명에게 1인당 {amount:,} NWX 지급 "
                f"(국고에서 {total:,} NWX 차감){deficit_note}"
            )
        except Exception as exc:
            return False, f"❌ 기본소득 처리 중 오류가 발생했습니다: {exc}"
