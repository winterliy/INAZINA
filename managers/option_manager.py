"""European, physically settled options backed by segregated collateral."""
from functions.exact_money import ExactDecimal
from functions.amount_limits import MAX_NATIVE
from functions.money import base_currency_code, currency_precision
from functions.quantities import quantity_number

import math
import time
from contextlib import nullcontext

from database import DatabaseManager
from database.user_db import TREASURY_USER_ID, ACTIVE_OR_TREASURY, IS_TREASURY
from functions.money import round_money
from managers.market_session_manager import MarketSessionManager
from managers.order_validation import validate_limit_order
from managers.rate_manager import RateManager


class OptionManager:
    CONTRACT_SIZE = 1  # The MVP represents one underlying share per option contract.
    MAX_CONTRACTS = 2**53 - 1
    MAX_PRICE = MAX_NATIVE
    MAX_MONEY = MAX_NATIVE

    def __init__(self, db, user_manager):
        self.db = db
        self.user_manager = user_manager

    @staticmethod
    def is_option_type(asset_type):
        return str(asset_type or "").strip().casefold() in {"option", "옵션"}

    def is_option(self, contract_code):
        return bool(self.db.assets.execute_one(
            "SELECT 1 FROM option_contracts WHERE contract_code = ?",
            (str(contract_code).strip(),),
        ))

    @staticmethod
    def theoretical_price(spot, strike, years, volatility, option_type, risk_free_rate=0):
        """Black-Scholes reference premium; rates are annual decimal fractions."""
        spot = ExactDecimal(spot or 0)
        strike = ExactDecimal(strike or 0)
        years = float(years or 0)
        volatility = float(volatility or 0)
        rate = float(risk_free_rate or 0)
        if spot <= 0 or strike <= 0:
            return 1
        discounted_strike = strike * math.exp(-rate * max(0, years))
        if years <= 0 or volatility <= 0:
            intrinsic = max(0.0, spot - discounted_strike) if option_type == "call" else max(0.0, discounted_strike - spot)
            return max(1, round_money(intrinsic))
        sigma_root_t = volatility * math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * volatility * volatility) * years) / sigma_root_t
        d2 = d1 - sigma_root_t
        cdf = lambda value: 0.5 * (1 + math.erf(value / math.sqrt(2)))
        if option_type == "call":
            value = spot * cdf(d1) - discounted_strike * cdf(d2)
        else:
            value = discounted_strike * cdf(-d2) - spot * cdf(-d1)
        return max(1, round_money(value))

    def get_contract(self, contract_code):
        row = self.db.assets.execute_one(
            "SELECT * FROM option_contracts WHERE contract_code = ?",
            (str(contract_code).strip(),),
        )
        if not row:
            return None
        contract = dict(row)
        try:
            # _asset_row validates codes across all product and account tables.
            # Its accounts.accounts lookup must also be attached on read paths.
            with self.db.atomic(("assets", "company", "product", "accounts")) as conn:
                underlying, source = self.user_manager._asset_row(
                    conn, contract["underlying_code"]
                )
            if not underlying or source not in {"market_assets", "company"}:
                return None
            spot = int(underlying["price"] or 0)
            now = int(time.time())
            years = max(0, int(contract["expiration_epoch"]) - now) / (365.0 * 86400)
            theoretical = self.theoretical_price(
                spot,
                contract["strike_price"],
                years,
                contract["implied_volatility"],
                contract["option_type"],
                self._base_rate(underlying.get("exchange_id")) / 100,
            )
            override = self.db.assets.execute_one(
                "SELECT price FROM market_quote_overrides WHERE asset_code=? "
                "UNION ALL SELECT price FROM market_last_trade WHERE asset_code=? "
                "AND NOT EXISTS(SELECT 1 FROM market_quote_overrides WHERE asset_code=?) LIMIT 1",
                (contract['contract_code'], contract['contract_code'], contract['contract_code']),
            )
            contract.update({
                "name": (
                    f"{underlying['name']} {'콜' if contract['option_type'] == 'call' else '풋'} "
                    f"{int(contract['strike_price']):,}"
                ),
                "type": "option",
                "price": int(override['price']) if override else theoretical,
                "theoretical_price": theoretical,
                "underlying_price": spot,
                "exchange_id": underlying.get("exchange_id"),
                "status": contract["status"],
                "is_fixed": 1,
                "total_supply": int(contract["issued_contracts"]),
            })
            return contract
        except (ValueError, TypeError, OverflowError):
            return None

    def _base_rate(self, exchange_id):
        from managers.rate_manager import RateManager
        with self.db.readonly() as conn:
            return RateManager.for_exchange(conn, 'base_rate', exchange_id)

    def get_active_contracts(self, underlying_code=None, limit=100):
        now = int(time.time())
        if underlying_code:
            rows = self.db.assets.execute(
                "SELECT contract_code FROM option_contracts WHERE status = 'active' "
                "AND expiration_epoch > ? AND underlying_code = ? "
                "ORDER BY expiration_epoch, strike_price, option_type LIMIT ?",
                (now, str(underlying_code).strip(), max(1, min(int(limit), 500))),
            )
        else:
            rows = self.db.assets.execute(
                "SELECT contract_code FROM option_contracts WHERE status = 'active' "
                "AND expiration_epoch > ? ORDER BY expiration_epoch, underlying_code, "
                "strike_price, option_type LIMIT ?",
                (now, max(1, min(int(limit), 500))),
            )
        return [contract for row in rows if (contract := self.get_contract(row["contract_code"]))]

    def create_and_write(
        self,
        writer_id,
        underlying_code,
        option_type,
        strike_price,
        maturity_days,
        quantity,
        limit_price,
        volatility_percent=50,
        conn=None,
    ):
        writer_id = str(writer_id)
        underlying_code = str(underlying_code).strip()
        option_type = str(option_type).strip().casefold()
        if option_type not in {"call", "put"}:
            return False, "❌ 옵션 유형은 콜 또는 풋이어야 합니다."
        from functions.money import parse_money
        try:
            if isinstance(strike_price,str):strike_price=parse_money(strike_price,minimum=1)
            if isinstance(limit_price,str):limit_price=parse_money(limit_price,minimum=1)
        except ValueError as exc:return False, f"❌ {exc}"
        for value, label in ((strike_price, "행사가"), (maturity_days, "만기 일수"),
                             (quantity, "계약 수량"), (limit_price, "프리미엄"),
                             (volatility_percent, "변동성")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return False, f"❌ {label}은 1 이상의 정수여야 합니다."
        if strike_price > self.MAX_PRICE or limit_price > self.MAX_PRICE:
            return False, "❌ 가격 한도를 초과했습니다."
        if quantity > self.MAX_CONTRACTS or maturity_days > 365 or volatility_percent > 500:
            return False, "❌ 수량·만기·변동성 설정 한도를 초과했습니다."
        if strike_price * quantity > self.MAX_MONEY or limit_price * quantity > self.MAX_MONEY:
            return False, "❌ 계약 전체 금액이 데이터베이스 금액 한도를 초과합니다."

        interest_error = self.user_manager._settle_user_interest(writer_id) if conn is None else None
        if interest_error:
            return False, interest_error

        now = int(time.time())
        try:
            with (self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) if conn is None else nullcontext(conn)) as conn:
                error = MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=underlying_code)
                if error:
                    return False, error
                underlying, source = self.user_manager._asset_row(conn, underlying_code)
                if not underlying or source not in {"market_assets", "company"}:
                    return False, "❌ 옵션 기초자산은 시장에 상장된 주식이어야 합니다."
                if str(underlying["type"]).strip().casefold() not in {"stock", "주식"}:
                    return False, "❌ 현재는 주식만 옵션의 기초자산으로 사용할 수 있습니다."
                if underlying.get('quote_currency',base_currency_code(conn))!=base_currency_code(conn):
                    return False,'❌ 현재 옵션 작성은 기준 통화 결제 주식만 지원합니다.'
                if underlying["status"] != "active" or int(underlying["price"] or 0) <= 0:
                    return False, "❌ 거래 중이며 기준가가 있는 주식만 옵션을 발행할 수 있습니다."

                call_lots = []
                cash_lots = []
                if option_type == "call":
                    from managers.asset_cost_manager import AssetCostManager
                    AssetCostManager.require_unit_cost_support(conn, str(writer_id), underlying_code)
                    positions = conn.execute(
                        "SELECT holding_id, account_number, quantity, average_cost "
                        "FROM assets.user_assets WHERE user_id = ? AND asset_code = ? "
                        "ORDER BY holding_id",
                        (writer_id, underlying_code),
                    ).fetchall()
                    reserved = conn.execute(
                        "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                        "FROM assets.market_orders WHERE order_kind = 'asset' "
                        "AND user_id = ? AND asset_code = ? AND side = 'sell' "
                        "AND status IN ('open', 'partial')",
                        (writer_id, underlying_code),
                    ).fetchone()
                    custody_reserved = conn.execute(
                        "SELECT COALESCE(SUM(r.reserved_quantity),0) FROM assets.dr_custody_reservations r "
                        "JOIN assets.dr_custody_instructions i USING(reference_key) "
                        "WHERE r.user_id=? AND r.reserved_asset_code=? AND r.released_epoch IS NULL "
                        "AND i.status IN ('requested','verified')", (writer_id, underlying_code),
                    ).fetchone()[0]
                    available = quantity_number(sum(quantity_number(row["quantity"]) for row in positions)
                                                - quantity_number(reserved["quantity"] or 0)
                                                - quantity_number(custody_reserved or 0))
                    if available < quantity:
                        return False, (
                            f"❌ 커버드 콜 담보 주식이 부족합니다. 필요 {quantity:,}주, "
                            f"주문 잠금을 제외한 보유 {max(0, available):,}주입니다."
                        )
                    left = quantity
                    for position in positions:
                        take = min(left, quantity_number(position["quantity"]))
                        if take:
                            call_lots.append((position, take))
                            left = quantity_number(left-take)
                        if left == 0:
                            break
                else:
                    required_cash = strike_price * quantity
                    if self._available_cash(conn, writer_id) < required_cash:
                        return False, (
                            f"❌ 현금 담보가 부족합니다. 풋 {quantity:,}계약에 "
                            f"{required_cash:,} 기준 통화가 필요합니다."
                        )

                cursor = conn.execute(
                    "INSERT INTO assets.option_contracts "
                    "(writer_id, underlying_code, option_type, strike_price, expiration_epoch, "
                    "implied_volatility, issued_contracts, created_epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (writer_id, underlying_code, option_type, strike_price,
                     now + maturity_days * 86400, volatility_percent / 100.0,
                     quantity, now),
                )
                contract_id = int(cursor.lastrowid)
                contract_code = f"OPT{contract_id:07d}"
                if DatabaseManager.code_locations(conn, contract_code):
                    raise ValueError(f"생성된 옵션 코드 `{contract_code}`가 기존 코드와 겹칩니다.")
                conn.execute(
                    "UPDATE assets.option_contracts SET contract_code = ? WHERE contract_id = ?",
                    (contract_code, contract_id),
                )

                if option_type == "call":
                    for position, take in call_lots:
                        current = quantity_number(position["quantity"])
                        if take == current:
                            changed = conn.execute(
                                "DELETE FROM assets.user_assets WHERE holding_id = ? AND quantity = ?",
                                (position["holding_id"], current),
                            )
                        else:
                            changed = conn.execute(
                                "UPDATE assets.user_assets SET quantity = quantity - ? "
                                "WHERE holding_id = ? AND quantity > ?",
                                (take, position["holding_id"], take),
                            )
                        if changed.rowcount != 1:
                            raise RuntimeError("기초주식 보유량이 변경되어 담보 설정을 취소했습니다.")
                        conn.execute(
                            "INSERT INTO assets.option_collateral_lots "
                            "(contract_id, writer_id, underlying_code, account_number, quantity, average_cost) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (contract_id, writer_id, underlying_code, position["account_number"],
                             take, int(position["average_cost"] or 0)),
                        )
                else:
                    cash_lots = self._debit_cash_lots(conn, writer_id, strike_price * quantity)
                    for account_number, amount in cash_lots:
                        conn.execute(
                            "INSERT INTO assets.option_collateral_cash_lots "
                            "(contract_id, writer_id, account_number, amount) VALUES (?, ?, ?, ?)",
                            (contract_id, writer_id, account_number, amount),
                        )

                order_cursor = conn.execute(
                    "INSERT INTO assets.market_orders "
                    "(user_id, asset_code, side, limit_price, quantity, remaining_quantity, "
                    "reserved_cash, funding_account, order_kind, option_order_type, status, "
                    "created_epoch, updated_epoch, quote_currency, limit_price_minor_units, "
                    "limit_price_decimal_places, reserved_cash_minor_units) "
                    "VALUES (?, ?, 'sell', ?, ?, ?, 0, NULL, 'option', 'write', 'open', ?, ?, ?, ?, 0, 0)",
                    (writer_id, contract_code, limit_price, quantity, quantity, now, now,
                     base_currency_code(conn), limit_price),
                )
                order_id = int(order_cursor.lastrowid)
            security = "주식 담보" if option_type == "call" else f"현금 담보 {strike_price * quantity:,} 기준 통화"
            return True, (
                f"✅ 옵션 `{contract_code}` 발행 주문 #{order_id}을 등록했습니다. "
                f"{underlying['name']} {('콜' if option_type == 'call' else '풋')} · "
                f"행사가 {strike_price:,} 기준 통화 · 만기 {maturity_days}일 · "
                f"{quantity:,}계약 · 지정 프리미엄 {limit_price:,} 기준 통화 · {security}. "
                "기준 프리미엄은 블랙-숄즈 참고값이며 체결가는 주문 호가로 결정됩니다."
            )
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 옵션 작성 중 오류가 발생했습니다: {exc}"

    @staticmethod
    def _cash_sources(conn, user_id):
        from managers.currency_manager import CurrencyManager

        user_id = str(user_id)
        currency = base_currency_code(conn)
        if currency_precision(conn, currency) != 0:
            raise ValueError("옵션은 기준 통화 최소단위가 정수인 통화에서만 거래할 수 있습니다.")
        user = conn.execute(
            f"SELECT cash_minor FROM users.users WHERE user_id=? AND {ACTIVE_OR_TREASURY}",
            (user_id,),
        ).fetchone()
        if not user:
            return []
        sources = []
        if CurrencyManager.home(conn, user_id) == currency:
            sources.append((int(user["cash_minor"] or 0), None))
        sources.extend(
            (int(row["balance_minor_units"] or 0), row["account_number"])
            for row in conn.execute(
                "SELECT account_number,balance_minor_units FROM accounts.accounts "
                "WHERE user_id=? AND quote_currency=? AND account_type IN ('saving','loan_payout') "
                "AND managed_by IS NULL", (user_id, currency),
            )
        )
        return sorted(sources, key=lambda item: item[0], reverse=True)

    @staticmethod
    def _available_cash(conn, user_id):
        return sum(max(0, balance) for balance, _ in OptionManager._cash_sources(conn, user_id))

    @staticmethod
    def _debit_cash_lots(conn, user_id, amount):
        from managers.user_manager import UserManager

        user_id, left = str(user_id), int(amount)
        currency = base_currency_code(conn)
        sources = OptionManager._cash_sources(conn, user_id)
        if sum(max(0, balance) for balance, _ in sources) < left:
            raise ValueError("담보를 설정할 기준통화 잔액이 부족합니다.")
        debited = []
        for balance, account_number in sources:
            take = min(max(0, balance), left)
            if take:
                if not UserManager._debit_source(conn, user_id, account_number, take, currency=currency):
                    raise RuntimeError("옵션 현금 출금 중 잔액이 변경됐습니다.")
                debited.append((account_number, take))
                left -= take
            if not left:
                return debited
        raise ValueError("담보를 설정할 기준통화 잔액이 부족합니다.")

    def place_limit_order(self, user_id, contract_code, side, quantity, limit_price):
        user_id = str(user_id)
        contract_code = str(contract_code).strip()
        validated, error = validate_limit_order(
            side, quantity, limit_price,
            max_quantity=self.MAX_CONTRACTS, max_price=self.MAX_PRICE,
            quantity_label="계약 수량", price_label="지정 프리미엄 기준 통화",
        )
        if error:
            return False, error
        side, quantity, limit_price = validated
        if limit_price * quantity > self.MAX_MONEY:
            return False, "❌ 주문 예치 금액이 데이터베이스 금액 한도를 초과합니다."
        interest_error = self.user_manager._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error

        now = int(time.time())
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                error = MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=contract_code)
                if error:
                    return False, error
                contract = conn.execute(
                    "SELECT * FROM assets.option_contracts WHERE contract_code = ?",
                    (contract_code,),
                ).fetchone()
                if not contract:
                    return False, "❌ 옵션 코드를 찾을 수 없습니다."
                if contract["status"] != "active" or int(contract["expiration_epoch"]) <= now:
                    return False, "❌ 만기 전인 거래 중 옵션만 주문할 수 있습니다."
                if side == "buy" and str(contract["writer_id"]) == user_id:
                    return False, "❌ 작성자는 자신이 발행한 옵션을 매수할 수 없습니다."

                funding_account = None
                reserved_cash = 0
                reserve_receipt = None
                base_currency = base_currency_code(conn)
                if currency_precision(conn, base_currency) != 0:
                    raise ValueError("옵션은 기준 통화 최소단위가 정수인 통화에서만 거래할 수 있습니다.")
                if side == "buy":
                    owned = conn.execute(
                        "SELECT COALESCE(SUM(quantity), 0) AS quantity FROM assets.option_positions "
                        "WHERE contract_id = ? AND user_id = ?",
                        (contract["contract_id"], user_id),
                    ).fetchone()
                    resting = conn.execute(
                        "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                        "FROM assets.market_orders WHERE order_kind = 'option' "
                        "AND asset_code = ? AND user_id = ? AND side = 'buy' "
                        "AND status IN ('open', 'partial')",
                        (contract_code, user_id),
                    ).fetchone()
                    capacity = max(
                        0, int(contract["issued_contracts"]) - int(owned["quantity"] or 0)
                        - int(resting["quantity"] or 0),
                    )
                    if quantity > capacity:
                        return False, (
                            f"❌ 해당 계약의 발행량 한도를 넘습니다. 현재 보유 및 미체결 매수 "
                            f"{int(owned['quantity'] or 0) + int(resting['quantity'] or 0):,}계약, "
                            f"추가 주문 가능 {capacity:,}계약입니다."
                        )
                    reserved_cash = limit_price * quantity
                    from database.fund_accounting import FundCashLedger
                    from managers.currency_manager import CurrencyManager
                    fund = FundCashLedger(conn)
                    if fund.account(user_id):
                        if fund.balance(user_id, base_currency) < reserved_cash:
                            return False, "❌ 옵션 프리미엄을 예치할 상품통화 잔액이 부족합니다."
                        reserve_receipt = (None, fund.withdraw(user_id, reserved_cash, base_currency,
                                                               "option_order_reserve", recorded_epoch=now))
                    else:
                        if CurrencyManager.home(conn, user_id) == base_currency:
                            funds = self.user_manager._choose_funds(conn, user_id)
                        else:
                            from managers.deposit_accounts import account
                            row = account(conn, user_id, base_currency)
                            funds = ((row["balance_minor_units"], row["account_number"])
                                     if row and row["managed_by"] is None else None)
                        if not funds:
                            return False, "❌ 옵션 프리미엄을 예치할 상품통화 계좌가 없습니다."
                        balance, funding_account = funds
                        watermark = self._wallet_watermark(conn)
                        if balance < reserved_cash or not self.user_manager._debit_source(
                            conn, user_id, funding_account, reserved_cash, currency=base_currency
                        ):
                            return False, f"❌ 잔액이 부족합니다. 지정가 기준 예치 필요액: {reserved_cash:,} 기준 통화"
                        reserve_receipt = (self._order_wallet_receipt(
                            conn, watermark, user_id, funding_account, base_currency, -reserved_cash), None)
                else:
                    owned = conn.execute(
                        "SELECT COALESCE(SUM(quantity), 0) AS quantity FROM assets.option_positions "
                        "WHERE contract_id = ? AND user_id = ?",
                        (contract["contract_id"], user_id),
                    ).fetchone()
                    resting = conn.execute(
                        "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                        "FROM assets.market_orders WHERE order_kind = 'option' "
                        "AND asset_code = ? AND user_id = ? AND side = 'sell' "
                        "AND option_order_type = 'resale' AND status IN ('open', 'partial')",
                        (contract_code, user_id),
                    ).fetchone()
                    available = int(owned["quantity"] or 0) - int(resting["quantity"] or 0)
                    if quantity > available:
                        return False, f"❌ 매도 가능한 옵션 수량이 부족합니다. 주문 가능 {max(0, available):,}계약입니다."

                cursor = conn.execute(
                    "INSERT INTO assets.market_orders "
                    "(user_id, asset_code, side, limit_price, quantity, remaining_quantity, "
                    "reserved_cash, funding_account, order_kind, option_order_type, status, "
                    "created_epoch, updated_epoch, quote_currency, limit_price_minor_units, "
                    "limit_price_decimal_places, reserved_cash_minor_units) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'option', 'resale', 'open', ?, ?, ?, ?, 0, ?)",
                    (user_id, contract_code, side, limit_price, quantity, quantity,
                     reserved_cash, funding_account, now, now, base_currency, limit_price, reserved_cash),
                )
                order_id = int(cursor.lastrowid)
                if reserve_receipt:
                    self._link_order_cash(conn, order_id, "reserve", reserve_receipt)
                filled, filled_quantity, traded_value = self._match_incoming_order(
                    conn, order_id, now
                )
                order = conn.execute(
                    "SELECT remaining_quantity, reserved_cash FROM assets.market_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
            note = (
                f"미체결 {int(order['remaining_quantity']):,}계약이 호가에 남았습니다. "
                + (f"매수 예치금 {int(order['reserved_cash']):,} 기준 통화를 잠갔습니다." if side == "buy"
                   else "보유 옵션 수량을 잠갔습니다.")
                if int(order["remaining_quantity"]) else "주문을 전량 체결했습니다."
            )
            return True, (
                f"✅ 주문 #{order_id} 등록 ({'매수' if side == 'buy' else '매도'} · "
                f"{contract_code} · 지정 프리미엄 {limit_price:,} 기준 통화 · {quantity:,}계약)\n"
                f"즉시 체결 {filled_quantity:,}계약 · 체결대금 {traded_value:,} 기준 통화\n{note}"
            )
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 옵션 주문 처리 중 오류가 발생했습니다: {exc}"

    def _match_incoming_order(self, conn, order_id, now):
        row = conn.execute("SELECT asset_code FROM market_orders WHERE order_id=?", (order_id,)).fetchone()
        if MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=row["asset_code"] if row else None):
            return 0, 0, 0
        fill_count = filled_quantity = traded_value = 0
        while True:
            incoming = conn.execute(
                "SELECT * FROM assets.market_orders WHERE order_id = ? AND order_kind = 'option'",
                (order_id,),
            ).fetchone()
            if not incoming or int(incoming["remaining_quantity"]) <= 0:
                break
            opposite = "sell" if incoming["side"] == "buy" else "buy"
            comparison = "<=" if incoming["side"] == "buy" else ">="
            sort_order = "ASC" if incoming["side"] == "buy" else "DESC"
            maker = conn.execute(
                "SELECT * FROM assets.market_orders WHERE order_kind = 'option' "
                "AND asset_code = ? AND side = ? AND status IN ('open', 'partial') "
                f"AND limit_price_minor_units {comparison} ? AND user_id <> ? "
                f"ORDER BY limit_price_minor_units {sort_order}, created_epoch, order_id LIMIT 1",
                (incoming["asset_code"], opposite, incoming["limit_price_minor_units"], incoming["user_id"]),
            ).fetchone()
            if not maker:
                break
            buy_order = incoming if incoming["side"] == "buy" else maker
            sell_order = incoming if incoming["side"] == "sell" else maker
            quantity = min(int(incoming["remaining_quantity"]), int(maker["remaining_quantity"]))
            price = int(maker["limit_price_minor_units"])
            self._settle_option_fill(conn, buy_order, sell_order, quantity, price, now)
            self._reduce_order(conn, buy_order, quantity, now)
            self._reduce_order(conn, sell_order, quantity, now)
            conn.execute(
                "INSERT INTO assets.market_trade_history "
                "(asset_code, side, quantity, price, recorded_epoch) "
                "VALUES (?, ?, ?, ?, ?)",
                (incoming["asset_code"], incoming["side"], quantity, price, now),
            )
            fill_count += 1
            filled_quantity += quantity
            traded_value += quantity * price
        return fill_count, filled_quantity, traded_value

    def _settle_option_fill(self, conn, buy_order, sell_order, quantity, price, now):
        improvement = (int(buy_order["limit_price_minor_units"]) - price) * quantity
        if improvement < 0:
            raise RuntimeError("체결 가격이 매수 프리미엄 지정가를 초과했습니다.")
        if improvement:
            self._refund_order_cash(conn, buy_order, improvement, now)
        contract = conn.execute(
            "SELECT contract_id, writer_id FROM assets.option_contracts WHERE contract_code = ?",
            (buy_order["asset_code"],),
        ).fetchone()
        buyer_id = str(buy_order["user_id"])
        seller_id = str(sell_order["user_id"])
        if sell_order["option_order_type"] == "write":
            if seller_id != str(contract["writer_id"]):
                raise RuntimeError("옵션 작성 주문자 정보가 계약과 일치하지 않습니다.")
            changed = conn.execute(
                "UPDATE assets.option_contracts SET outstanding_contracts = "
                "outstanding_contracts + ? WHERE contract_id = ? AND status = 'active'",
                (quantity, contract["contract_id"]),
            )
            if changed.rowcount != 1:
                raise RuntimeError("활성 옵션 계약을 찾지 못해 체결을 취소했습니다.")
            self._add_option_position(conn, contract["contract_id"], buyer_id, quantity, price)
        else:
            held = conn.execute(
                "SELECT quantity FROM assets.option_positions WHERE contract_id = ? AND user_id = ?",
                (contract["contract_id"], seller_id),
            ).fetchone()
            if not held or int(held["quantity"]) < quantity:
                raise RuntimeError("매도 예약 옵션 수량이 변경되어 체결을 취소했습니다.")
            if int(held["quantity"]) == quantity:
                seller_position = conn.execute(
                    "DELETE FROM assets.option_positions WHERE contract_id = ? AND user_id = ? "
                    "AND quantity = ?",
                    (contract["contract_id"], seller_id, quantity),
                )
            else:
                seller_position = conn.execute(
                    "UPDATE assets.option_positions SET quantity = quantity - ? "
                    "WHERE contract_id = ? AND user_id = ? AND quantity >= ?",
                    (quantity, contract["contract_id"], seller_id, quantity),
                )
            if seller_position.rowcount != 1:
                raise RuntimeError("매도 예약 옵션 수량이 변경되어 체결을 취소했습니다.")
            self._add_option_position(conn, contract["contract_id"], buyer_id, quantity, price)
        from managers.accounting_journal import AccountingJournal
        AccountingJournal.post_option_premium(conn, buy_order, sell_order, quantity, price, now)

    @staticmethod
    def _add_option_position(conn, contract_id, user_id, quantity, premium):
        conn.execute(
            "INSERT INTO assets.option_positions (contract_id, user_id, quantity, average_premium) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(contract_id, user_id) DO UPDATE SET "
            "average_premium = ((quantity * average_premium) + (excluded.quantity * excluded.average_premium)) "
            "/ (quantity + excluded.quantity), quantity = quantity + excluded.quantity",
            (contract_id, str(user_id), int(quantity), int(premium)),
        )

    @staticmethod
    def _reduce_order(conn, order, quantity, now):
        remaining = int(order["remaining_quantity"]) - int(quantity)
        if remaining < 0:
            raise RuntimeError("옵션 주문 잔량보다 많은 수량이 체결되었습니다.")
        reserved_cash = int(order["reserved_cash_minor_units"] or 0)
        if order["side"] == "buy":
            reserved_cash = max(0, reserved_cash - int(order["limit_price_minor_units"]) * int(quantity))
        conn.execute(
            "UPDATE assets.market_orders SET remaining_quantity = ?, "
            "filled_quantity = filled_quantity + ?, reserved_cash_minor_units = ?, "
            "status = ?, updated_epoch = ? WHERE order_id = ?",
            (remaining, quantity, reserved_cash, "filled" if remaining == 0 else "partial",
             now, order["order_id"]),
        )

    @staticmethod
    def _wallet_watermark(conn):
        return conn.execute("SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger").fetchone()[0]

    @staticmethod
    def _order_wallet_receipt(conn, watermark, user, account, currency, delta):
        rows = conn.execute("""SELECT entry_id FROM customer_wallet_ledger
            WHERE entry_id>? AND user_id=? AND source_type=? AND source_account=?
              AND currency_code=? AND decimal_places=0 AND delta_minor_units=?""",
            (watermark, user, "account" if account is not None else "cash",
             account if account is not None else user, currency, delta)).fetchall()
        if len(rows) != 1:
            raise RuntimeError("옵션 주문 예치 또는 환불에 대응하는 지갑 변동이 없습니다.")
        return rows[0][0]

    @staticmethod
    def _link_order_cash(conn, order_id, movement, receipt):
        conn.execute("""INSERT INTO option_order_cash_links
            (order_id,movement,wallet_entry_id,fund_entry_id) VALUES (?,?,?,?)""",
            (order_id, movement, *receipt))

    def _refund_order_cash(self, conn, order, amount, now):
        user, number = str(order["user_id"]), order["funding_account"]
        reserve = conn.execute("""SELECT fund_entry_id FROM option_order_cash_links
            WHERE order_id=? AND movement='reserve'""", (order["order_id"],)).fetchone()
        if not reserve:
            # Pre-link orders retain their original refund rules and are not claimed as traced.
            self._credit_reserved_source(conn, user, number, amount, now)
            return
        currency = order["quote_currency"]
        if reserve["fund_entry_id"] is not None:
            from database.fund_accounting import FundCashLedger
            receipt = (None, FundCashLedger(conn).deposit(
                user, amount, currency, "option_order_refund", recorded_epoch=now))
        else:
            watermark = self._wallet_watermark(conn)
            self.user_manager._credit_source(conn, user, number, amount, currency=currency)
            receipt = (self._order_wallet_receipt(conn, watermark, user, number, currency, amount), None)
        self._link_order_cash(conn, order["order_id"], "refund", receipt)

    @staticmethod
    def _credit_reserved_source(conn, user_id, account_number, amount, now):
        if amount <= 0:
            return
        if account_number is None:
            cursor = conn.execute(
                f"UPDATE users.users SET cash = cash + ? WHERE user_id = ? AND {ACTIVE_OR_TREASURY}",
                (int(amount), str(user_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER), "
                "principal = COALESCE(principal, 0) + ?, last_payment_epoch = ?, "
                "last_payment = CURRENT_TIMESTAMP WHERE account_number = ? AND user_id = ?",
                (int(amount), int(amount), now, account_number, str(user_id)),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("옵션 주문 예치금을 환불할 계좌를 찾지 못했습니다.")

    def cancel_order(self, user_id, order_id):
        user_id = str(user_id)
        now = int(time.time())
        try:
            with self.db.atomic(("users", "accounts", "assets")) as conn:
                order = conn.execute(
                    "SELECT * FROM assets.market_orders WHERE order_id = ? "
                    "AND user_id = ? AND order_kind = 'option'",
                    (order_id, user_id),
                ).fetchone()
                if not order:
                    return False, "❌ 본인의 옵션 주문을 찾을 수 없습니다."
                if order["status"] not in {"open", "partial"}:
                    return False, f"❌ 이미 {order['status']} 상태라 취소할 수 없습니다."
                refund = int(order["reserved_cash_minor_units"] or 0) if order["side"] == "buy" else 0
                if refund:
                    self._refund_order_cash(conn, order, refund, now)
                if order["side"] == "sell" and order["option_order_type"] == "write":
                    contract = conn.execute(
                        "SELECT * FROM assets.option_contracts WHERE contract_code = ?",
                        (order["asset_code"],),
                    ).fetchone()
                    remaining = int(order["remaining_quantity"])
                    if contract["option_type"] == "call":
                        self._release_share_collateral(conn, contract["contract_id"], remaining)
                    else:
                        self._release_cash_collateral(
                            conn, contract["contract_id"],
                            int(contract["strike_price"]) * remaining,
                        )
                    if int(contract["outstanding_contracts"]) == 0:
                        conn.execute(
                            "UPDATE assets.option_contracts SET status = 'cancelled' "
                            "WHERE contract_id = ?",
                            (contract["contract_id"],),
                        )
                conn.execute(
                    "UPDATE assets.market_orders SET status = 'cancelled', reserved_cash = 0, "
                    "updated_epoch = ? WHERE order_id = ?",
                    (now, order_id),
                )
            message = f"✅ 옵션 주문 #{order_id}을 취소했습니다."
            if refund:
                message += f" 매수 예치금 {refund:,} 기준 통화를 반환했습니다."
            return True, message
        except Exception as exc:
            return False, f"❌ 옵션 주문 취소 중 오류가 발생했습니다: {exc}"

    @staticmethod
    def _add_asset_position(conn, user_id, code, quantity, unit_cost, account_number=None):
        if quantity <= 0:
            return
        from managers.asset_cost_manager import AssetCostManager
        AssetCostManager.upsert(
            conn, user_id, code, account_number, quantity_number(quantity), unit_cost,
            schema="assets",
        )

    def _take_share_collateral(self, conn, contract_id, quantity):
        left = quantity_number(quantity)
        taken = []
        rows = conn.execute(
            "SELECT * FROM assets.option_collateral_lots WHERE contract_id = ? "
            "ORDER BY collateral_id",
            (contract_id,),
        ).fetchall()
        for row in rows:
            take = min(left, quantity_number(row["quantity"]))
            if take <= 0:
                continue
            if take == quantity_number(row["quantity"]):
                conn.execute(
                    "DELETE FROM assets.option_collateral_lots WHERE collateral_id = ?",
                    (row["collateral_id"],),
                )
            else:
                conn.execute(
                    "UPDATE assets.option_collateral_lots SET quantity = quantity - ? "
                    "WHERE collateral_id = ?",
                    (take, row["collateral_id"]),
                )
            taken.append((quantity_number(take), int(row["average_cost"] or 0)))
            left = quantity_number(left-take)
            if left == 0:
                break
        if left:
            raise RuntimeError("콜 옵션 주식 담보가 부족합니다.")
        return taken

    def _release_share_collateral(self, conn, contract_id, quantity=None):
        contract = conn.execute(
            "SELECT writer_id, underlying_code FROM assets.option_contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if not contract:
            raise RuntimeError("옵션 계약의 주식 담보 정보를 찾을 수 없습니다.")
        rows = conn.execute(
            "SELECT * FROM assets.option_collateral_lots WHERE contract_id = ? "
            "ORDER BY collateral_id",
            (contract_id,),
        ).fetchall()
        left = sum(quantity_number(row["quantity"]) for row in rows) if quantity is None else quantity_number(quantity)
        to_release = left
        for row in rows:
            take = min(left, quantity_number(row["quantity"]))
            if take <= 0:
                continue
            if take == quantity_number(row["quantity"]):
                conn.execute(
                    "DELETE FROM assets.option_collateral_lots WHERE collateral_id = ?",
                    (row["collateral_id"],),
                )
            else:
                conn.execute(
                    "UPDATE assets.option_collateral_lots SET quantity = quantity - ? "
                    "WHERE collateral_id = ?",
                    (take, row["collateral_id"]),
                )
            self._add_asset_position(
                conn, contract["writer_id"], contract["underlying_code"], take,
                int(row["average_cost"] or 0),
                self._live_account_or_wallet(conn, contract["writer_id"], row["account_number"]),
            )
            left = quantity_number(left-take)
            if left == 0:
                break
        if left:
            raise RuntimeError("반환할 주식 담보 수량이 부족합니다.")
        return to_release

    def _consume_cash_collateral(self, conn, contract_id, amount):
        left = int(amount)
        rows = conn.execute(
            "SELECT collateral_id, amount FROM assets.option_collateral_cash_lots "
            "WHERE contract_id = ? ORDER BY collateral_id",
            (contract_id,),
        ).fetchall()
        for row in rows:
            take = min(left, int(row["amount"]))
            if take == int(row["amount"]):
                conn.execute(
                    "DELETE FROM assets.option_collateral_cash_lots WHERE collateral_id = ?",
                    (row["collateral_id"],),
                )
            elif take:
                conn.execute(
                    "UPDATE assets.option_collateral_cash_lots SET amount = amount - ? "
                    "WHERE collateral_id = ?",
                    (take, row["collateral_id"]),
                )
            left -= take
            if left == 0:
                break
        if left:
            raise RuntimeError("풋 옵션 현금 담보가 부족합니다.")

    @staticmethod
    def _credit_option_cash(conn, user_id, amount):
        if amount <= 0:
            return
        from database.fund_accounting import FundCashLedger
        from managers.currency_manager import CurrencyManager, SCALE

        currency = base_currency_code(conn)
        fund = FundCashLedger(conn)
        if fund.account(user_id):
            fund.deposit(user_id, amount, currency, "option_settlement")
        else:
            CurrencyManager.change(conn, user_id, currency, int(amount) * SCALE)

    def _release_cash_collateral(self, conn, contract_id, amount=None):
        rows = conn.execute(
            "SELECT * FROM assets.option_collateral_cash_lots WHERE contract_id = ? "
            "ORDER BY collateral_id",
            (contract_id,),
        ).fetchall()
        left = sum(int(row["amount"]) for row in rows) if amount is None else int(amount)
        released = left
        now = int(time.time())
        for row in rows:
            take = min(left, int(row["amount"]))
            if take <= 0:
                continue
            if take == int(row["amount"]):
                conn.execute(
                    "DELETE FROM assets.option_collateral_cash_lots WHERE collateral_id = ?",
                    (row["collateral_id"],),
                )
            else:
                conn.execute(
                    "UPDATE assets.option_collateral_cash_lots SET amount = amount - ? "
                    "WHERE collateral_id = ?",
                    (take, row["collateral_id"]),
                )
            self.user_manager._credit_source(
                conn, row["writer_id"],
                self._live_account_or_wallet(conn, row["writer_id"], row["account_number"]),
                take,
            )
            left -= take
            if left == 0:
                break
        if left:
            raise RuntimeError("반환할 풋 옵션 현금 담보가 부족합니다.")
        return released

    @staticmethod
    def _live_account_or_wallet(conn, user_id, account_number):
        if account_number is None:
            return None
        account = conn.execute(
            "SELECT 1 FROM accounts.accounts WHERE account_number = ? AND user_id = ?",
            (account_number, str(user_id)),
        ).fetchone()
        return account_number if account else None

    def _take_user_shares(self, conn, user_id, underlying_code, quantity):
        user_id = str(user_id)
        from managers.asset_cost_manager import AssetCostManager
        AssetCostManager.require_unit_cost_support(conn, user_id, underlying_code)
        rows = conn.execute(
            "SELECT holding_id, account_number, quantity, average_cost "
            "FROM assets.user_assets WHERE user_id = ? AND asset_code = ? ORDER BY holding_id",
            (user_id, underlying_code),
        ).fetchall()
        reserved = conn.execute(
            "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
            "FROM assets.market_orders WHERE order_kind = 'asset' AND user_id = ? "
            "AND asset_code = ? AND side = 'sell' AND status IN ('open', 'partial')",
            (user_id, underlying_code),
        ).fetchone()
        available = quantity_number(sum(quantity_number(row["quantity"]) for row in rows) - quantity_number(reserved["quantity"] or 0))
        # Exercise whole option contracts while allowing delivery from fractional lots.
        left = min(int(quantity), int(max(0, available)))
        target = left
        lots = []
        for row in rows:
            take = min(left, quantity_number(row["quantity"]))
            if take <= 0:
                continue
            if take == quantity_number(row["quantity"]):
                changed = conn.execute(
                    "DELETE FROM assets.user_assets WHERE holding_id = ? AND quantity = ?",
                    (row["holding_id"], quantity_number(row["quantity"])),
                )
            else:
                changed = conn.execute(
                    "UPDATE assets.user_assets SET quantity = quantity - ? "
                    "WHERE holding_id = ? AND quantity > ?",
                    (take, row["holding_id"], take),
                )
            if changed.rowcount != 1:
                raise RuntimeError("기초주식 보유량이 변경되어 옵션 행사를 취소했습니다.")
            lots.append((take, int(row["average_cost"] or 0), row["account_number"]))
            left = quantity_number(left-take)
            if left == 0:
                break
        return target, lots

    @staticmethod
    def _debit_available_cash(conn, user_id, amount):
        if OptionManager._available_cash(conn, user_id) < int(amount):
            return False
        OptionManager._debit_cash_lots(conn, user_id, amount)
        return True

    def expire_matured(self, now=None):
        now = int(RateManager._validated_epoch(now, "invalid_option_expiration_epoch"))
        contracts = self.db.assets.execute(
            "SELECT contract_id FROM option_contracts WHERE status = 'active' "
            "AND expiration_epoch <= ? ORDER BY expiration_epoch, contract_id",
            (now,),
        )
        notifications = []
        for item in contracts:
            try:
                with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                    contract_notifications = []
                    contract = conn.execute(
                        "SELECT * FROM assets.option_contracts WHERE contract_id = ? "
                        "AND status = 'active'",
                        (item["contract_id"],),
                    ).fetchone()
                    if not contract:
                        continue
                    underlying, source = self.user_manager._asset_row(
                        conn, contract["underlying_code"]
                    )
                    if not underlying or source not in {"market_assets", "company"}:
                        raise RuntimeError("옵션 기초 주식 정보를 찾지 못해 만기 처리를 보류했습니다.")
                    spot = int(underlying["price"] or 0)

                    pending = conn.execute(
                        "SELECT * FROM assets.market_orders WHERE order_kind = 'option' "
                        "AND asset_code = ? AND status IN ('open', 'partial')",
                        (contract["contract_code"],),
                    ).fetchall()
                    for order in pending:
                        if order["side"] == "buy" and int(order["reserved_cash"] or 0):
                            self._refund_order_cash(conn, order, int(order["reserved_cash"]), now)
                        conn.execute(
                            "UPDATE assets.market_orders SET status = 'expired', reserved_cash = 0, "
                            "updated_epoch = ? WHERE order_id = ?",
                            (now, order["order_id"]),
                        )

                    holders = conn.execute(
                        "SELECT user_id, quantity FROM assets.option_positions "
                        "WHERE contract_id = ? ORDER BY user_id",
                        (contract["contract_id"],),
                    ).fetchall()
                    exercise_total = 0
                    for holder in holders:
                        holder_id = str(holder["user_id"])
                        held = int(holder["quantity"])
                        exercised = 0
                        cash_paid = 0
                        cash_received = 0
                        strike = int(contract["strike_price"])
                        if contract["option_type"] == "call" and spot > strike:
                            affordable = self._available_cash(conn, holder_id) // strike
                            exercised = min(held, affordable)
                            if exercised and not self._debit_available_cash(
                                conn, holder_id, strike * exercised
                            ):
                                raise RuntimeError("콜 옵션 행사 대금 결제를 완료하지 못했습니다.")
                            if exercised:
                                delivered_lots = self._take_share_collateral(
                                    conn, contract["contract_id"], exercised
                                )
                                basis_gain = sum(
                                    max(0, strike - basis) * lot_quantity
                                    for lot_quantity, basis in delivered_lots
                                )
                                tax = self.user_manager._tax(
                                    conn, contract["writer_id"], "stock_sale_gain", basis_gain,
                                    currency=base_currency_code(conn),
                                )
                                self._credit_option_cash(conn, contract["writer_id"], strike * exercised - tax)
                                self._add_asset_position(
                                    conn, holder_id, contract["underlying_code"], exercised,
                                    strike, None,
                                )
                                cash_paid = strike * exercised
                        elif contract["option_type"] == "put" and spot < strike:
                            exercised, lots = self._take_user_shares(
                                conn, holder_id, contract["underlying_code"], held
                            )
                            if exercised:
                                collateral_payment = strike * exercised
                                self._consume_cash_collateral(
                                    conn, contract["contract_id"], collateral_payment
                                )
                                taxable_gain = sum(
                                    max(0, strike - basis) * lot_quantity
                                    for lot_quantity, basis, _account in lots
                                )
                                tax = self.user_manager._tax(
                                    conn, holder_id, "stock_sale_gain", taxable_gain,
                                    currency=base_currency_code(conn),
                                )
                                self._credit_option_cash(conn, holder_id, collateral_payment - tax)
                                self._add_asset_position(
                                    conn, contract["writer_id"], contract["underlying_code"],
                                    exercised, strike, None,
                                )
                                cash_received = collateral_payment - tax
                        conn.execute(
                            "DELETE FROM assets.option_positions WHERE contract_id = ? AND user_id = ?",
                            (contract["contract_id"], holder_id),
                        )
                        exercise_total += exercised
                        contract_notifications.append({
                            "user_id": holder_id,
                            "contract_code": contract["contract_code"],
                            "option_type": contract["option_type"],
                            "underlying_code": contract["underlying_code"],
                            "strike_price": strike,
                            "spot_price": spot,
                            "quantity": held,
                            "exercised": exercised,
                            "cash_paid": cash_paid,
                            "cash_received": cash_received,
                        })

                    contract_notifications.append({
                        "user_id": str(contract["writer_id"]),
                        "contract_code": contract["contract_code"],
                        "option_type": contract["option_type"],
                        "underlying_code": contract["underlying_code"],
                        "strike_price": int(contract["strike_price"]),
                        "spot_price": spot,
                        "quantity": int(contract["outstanding_contracts"]),
                        "exercised": exercise_total,
                        "cash_paid": 0,
                        "cash_received": 0,
                        "is_writer": True,
                    })

                    if contract["option_type"] == "call":
                        self._release_share_collateral(conn, contract["contract_id"])
                    else:
                        self._release_cash_collateral(conn, contract["contract_id"])
                    conn.execute(
                        "UPDATE assets.option_contracts SET status = 'expired', settled_epoch = ? "
                        "WHERE contract_id = ?",
                        (now, contract["contract_id"]),
                    )
                    if source == "company":
                        self.user_manager._refresh_max_shareholder(
                            conn, contract["underlying_code"]
                        )
                notifications.extend(contract_notifications)
            except Exception as exc:
                print(f"[Option Expiry] contract #{item['contract_id']}: {type(exc).__name__}: {exc}")
        return notifications

    def get_user_positions(self, user_id):
        rows = self.db.assets.execute(
            "SELECT c.contract_code, c.option_type, c.underlying_code, c.strike_price, "
            "c.expiration_epoch, c.implied_volatility, c.writer_id, p.quantity, "
            "p.average_premium FROM option_positions p JOIN option_contracts c "
            "ON c.contract_id = p.contract_id WHERE p.user_id = ? AND c.status = 'active' "
            "ORDER BY c.expiration_epoch, c.underlying_code, c.strike_price",
            (str(user_id),),
        )
        positions = []
        for row in rows:
            contract = self.get_contract(row["contract_code"])
            if contract:
                positions.append({**dict(row), **contract, "position_type": "long"})
        return positions

    def get_user_written(self, user_id):
        rows = self.db.assets.execute(
            "SELECT c.contract_code, c.option_type, c.underlying_code, c.strike_price, "
            "c.expiration_epoch, c.issued_contracts, c.outstanding_contracts, "
            "COALESCE((SELECT SUM(remaining_quantity) FROM market_orders o "
            "WHERE o.order_kind = 'option' AND o.option_order_type = 'write' "
            "AND o.asset_code = c.contract_code AND o.status IN ('open', 'partial')), 0) AS offered, "
            "COALESCE((SELECT SUM(quantity) FROM option_collateral_lots l "
            "WHERE l.contract_id = c.contract_id), 0) AS collateral_quantity, "
            "COALESCE((SELECT SUM(amount) FROM option_collateral_cash_lots l "
            "WHERE l.contract_id = c.contract_id), 0) AS collateral_cash "
            "FROM option_contracts c WHERE c.writer_id = ? AND c.status = 'active' "
            "ORDER BY c.expiration_epoch, c.underlying_code, c.strike_price",
            (str(user_id),),
        )
        written = []
        for row in rows:
            contract = self.get_contract(row["contract_code"])
            if contract:
                written.append({**dict(row), **contract, "position_type": "writer"})
        return written
