"""Stock futures with cash margin, signed positions and a game treasury clearing dealer."""

import math
import time
from managers.market_session_manager import MarketSessionManager

from database.user_db import TREASURY_USER_ID
from managers.investment_manager import InvestmentManager


class FuturesManager:
    def __init__(self, db, user_manager):
        self.db, self.users = db, user_manager

    def is_future(self, code):
        row = self.db.products.get_investment(str(code).strip())
        return bool(row and row["product_type"] == "stock_future")

    @staticmethod
    def _product(conn, code):
        return conn.execute("SELECT * FROM product.investment WHERE product_code = ? AND product_type = 'stock_future'", (code,)).fetchone()

    @staticmethod
    def _margin(product, quantity, price):
        return math.ceil(abs(quantity) * int(product["contract_size"]) * int(price) * float(product["initial_margin"]))

    def _quote(self, conn, product, now):
        if int(product["is_fixed"] or 0) and now < int(product["maturity_epoch"]):
            return max(1, int(product["unit_price"]))
        setting = conn.execute("SELECT value FROM system.settings WHERE key = 'base_rate'").fetchone()
        rate = float(setting["value"] if setting else 3)
        quote = InvestmentManager.linked_price(conn, product, rate, now)
        if not quote:
            raise ValueError("선물 기초주식의 가격을 찾을 수 없습니다.")
        return quote

    def place_limit_order(self, user_id, code, side, quantity, limit_price):
        if side not in {"buy", "sell"} or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 2**53 - 1 for x in (quantity, limit_price)):
            return False, "❌ 선물 주문 수량·지정가는 양의 정수여야 합니다."
        error = self.users._settle_user_interest(str(user_id))
        if error:
            return False, error
        now, user_id, code = int(time.time()), str(user_id), str(code).strip()
        try:
            self.settle_positions(now=now)
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax", "system")) as conn:
                error = MarketSessionManager(self.db).trading_error(conn=conn, now=now)
                if error:
                    return False, error
                product = self._product(conn, code)
                if not product or product["status"] != "active" or int(product["maturity_epoch"] or 0) <= now:
                    return False, "❌ 만기 전 거래 중인 주식 선물만 주문할 수 있습니다."
                if quantity > int(product["total_supply"]):
                    return False, "❌ 선물 계약 한도를 초과했습니다."
                quote = self._quote(conn, product, now)
                unit_margin = self._margin(product, 1, max(quote, limit_price))
                reserved = unit_margin * quantity
                if reserved > 2**63 - 1:
                    return False, "❌ 증거금 금액이 너무 큽니다."
                funds = self.users._choose_funds(conn, user_id)
                if not funds or funds[0] < reserved or not self.users._debit_source(conn, user_id, funds[1], reserved):
                    return False, f"❌ 선물 주문 증거금 {reserved:,} NWX가 필요합니다."
                cursor = conn.execute("INSERT INTO assets.market_orders "
                    "(user_id, asset_code, side, limit_price, quantity, remaining_quantity, reserved_cash, funding_account, "
                    "order_kind, created_epoch, updated_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'future', ?, ?)",
                    (user_id, code, side, limit_price, quantity, quantity, reserved, funds[1], now, now))
                order_id = cursor.lastrowid
                filled = self._match(conn, order_id, product, quote, now)
            return True, f"✅ 선물 주문 #{order_id} · {'매수(롱/숏 청산)' if side == 'buy' else '매도(숏/롱 청산)'} " \
                         f"{quantity:,}계약 · 즉시 체결 {filled:,}계약 · 초기 증거금 {float(product['initial_margin'])*100:g}%"
        except (ValueError, RuntimeError) as exc:
            return False, f"❌ {exc}"

    def _match(self, conn, order_id, product, quote, now):
        if MarketSessionManager(self.db).trading_error(conn=conn, now=now):
            return 0
        filled = 0
        while True:
            incoming = conn.execute("SELECT * FROM assets.market_orders WHERE order_id = ?", (order_id,)).fetchone()
            remaining = int(incoming["remaining_quantity"])
            if not remaining:
                break
            buy = incoming["side"] == "buy"
            comparison, sorting = ("<=", "ASC") if buy else (">=", "DESC")
            maker = conn.execute(f"SELECT * FROM assets.market_orders WHERE asset_code = ? AND order_kind = 'future' "
                f"AND side = ? AND status IN ('open', 'partial') AND user_id != ? AND limit_price {comparison} ? "
                f"ORDER BY limit_price {sorting}, created_epoch, order_id LIMIT 1",
                (incoming["asset_code"], "sell" if buy else "buy", incoming["user_id"], incoming["limit_price"])).fetchone()
            # Treasury provides a fair-value quote after citizen orders have priority.
            if maker:
                quantity, price = min(remaining, int(maker["remaining_quantity"])), int(maker["limit_price"])
                self._fill_order(conn, maker, product, quantity, price, now)
            elif (buy and int(incoming["limit_price"]) >= quote) or (not buy and int(incoming["limit_price"]) <= quote):
                quantity, price = remaining, quote
            else:
                break
            self._fill_order(conn, incoming, product, quantity, price, now)
            gross = conn.execute("SELECT COALESCE(SUM(ABS(quantity)), 0) FROM assets.futures_positions WHERE product_code = ?",
                                 (product["product_code"],)).fetchone()[0]
            if gross > int(product["total_supply"]):
                raise ValueError("선물 미결제 계약 한도가 부족합니다. 주문을 줄여주세요.")
            conn.execute("INSERT INTO assets.market_trade_history (asset_code, side, quantity, price, recorded_epoch) "
                         "VALUES (?, ?, ?, ?, ?)", (product["product_code"], incoming["side"], quantity, price, now))
            filled += quantity
        return filled

    def _mark(self, conn, position, product, price, now):
        pnl = int(position["quantity"]) * int(product["contract_size"]) * (price - int(position["settlement_price"]))
        # Limited game liability: liquidation consumes margin, never creates negative user cash.
        actual_pnl = max(-int(position["collateral"]), pnl)
        collateral = int(position["collateral"]) + actual_pnl
        net_pnl = int(position["net_pnl"]) + actual_pnl
        conn.execute("UPDATE users.users SET cash = cash - ? WHERE user_id = ?", (actual_pnl, TREASURY_USER_ID))
        conn.execute("UPDATE assets.futures_positions SET settlement_price = ?, collateral = ?, net_pnl = ?, updated_epoch = ? "
                     "WHERE user_id = ? AND product_code = ?",
                     (price, collateral, net_pnl, now, position["user_id"], product["product_code"]))
        return collateral, net_pnl

    def _fill_order(self, conn, order, product, quantity, price, now):
        delta = quantity if order["side"] == "buy" else -quantity
        old = conn.execute("SELECT * FROM assets.futures_positions WHERE user_id = ? AND product_code = ?",
                           (order["user_id"], product["product_code"])).fetchone()
        old_qty, collateral, net_pnl = 0, 0, 0
        if old:
            old_qty = int(old["quantity"])
            collateral, net_pnl = self._mark(conn, old, product, price, now)
        closing = min(abs(old_qty), quantity) if old_qty * delta < 0 else 0
        returned = collateral * closing // abs(old_qty) if closing else 0
        realized_pnl = int(net_pnl * closing / abs(old_qty)) if closing else 0
        tax = self.users._tax(conn, order["user_id"], "futures_realized_gain", max(0, realized_pnl)) if closing else 0
        collateral -= returned
        net_pnl -= realized_pnl
        opening = quantity - closing
        required = self._margin(product, opening, price)
        deposit = int(order["reserved_cash"]) * quantity // int(order["remaining_quantity"])
        available = deposit + returned - tax
        if required > available:
            funds = self.users._choose_funds(conn, order["user_id"])
            extra = required - available
            if not funds or funds[0] < extra or not self.users._debit_source(conn, order["user_id"], funds[1], extra):
                raise ValueError("체결가가 변해 추가 증거금이 필요합니다. 주문을 다시 제출해주세요.")
            available += extra
        self.users._credit_source(conn, order["user_id"], order["funding_account"], available - required)
        collateral += required
        new_qty = old_qty + delta
        if new_qty:
            conn.execute("INSERT INTO assets.futures_positions "
                "(user_id, product_code, quantity, settlement_price, collateral, net_pnl, updated_epoch) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, product_code) DO UPDATE SET quantity = excluded.quantity, settlement_price = excluded.settlement_price, "
                "collateral = excluded.collateral, net_pnl = excluded.net_pnl, updated_epoch = excluded.updated_epoch",
                (order["user_id"], product["product_code"], new_qty, price, collateral, net_pnl, now))
        else:
            conn.execute("DELETE FROM assets.futures_positions WHERE user_id = ? AND product_code = ?",
                         (order["user_id"], product["product_code"]))
        remaining = int(order["remaining_quantity"]) - quantity
        conn.execute("UPDATE assets.market_orders SET remaining_quantity = ?, filled_quantity = filled_quantity + ?, "
                     "reserved_cash = reserved_cash - ?, status = ?, updated_epoch = ? WHERE order_id = ?",
                     (remaining, quantity, deposit, "filled" if remaining == 0 else "partial", now, order["order_id"]))

    def _cancel(self, conn, order, now, status="cancelled"):
        self.users._credit_source(conn, order["user_id"], order["funding_account"], int(order["reserved_cash"]))
        conn.execute("UPDATE assets.market_orders SET reserved_cash = 0, status = ?, updated_epoch = ? WHERE order_id = ?",
                     (status, now, order["order_id"]))

    def cancel_order(self, user_id, order_id):
        with self.db.atomic(("users", "accounts", "assets")) as conn:
            order = conn.execute("SELECT * FROM assets.market_orders WHERE order_id = ? AND user_id = ? AND order_kind = 'future'",
                                 (order_id, str(user_id))).fetchone()
            if not order or order["status"] not in {"open", "partial"}:
                return False, "❌ 취소 가능한 본인의 선물 주문을 찾을 수 없습니다."
            self._cancel(conn, order, int(time.time()))
            return True, f"✅ 선물 주문 #{order_id} 취소 · 증거금 {int(order['reserved_cash']):,} NWX 반환"

    def settle_positions(self, now=None):
        now = int(time.time() if now is None else now)
        results = []
        codes = self.db.products.execute("SELECT product_code FROM investment WHERE product_type = 'stock_future' AND status = 'active'")
        for item in codes:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax", "system")) as conn:
                product = self._product(conn, item["product_code"])
                if product["status"] != "active":
                    continue
                state = MarketSessionManager.process_conn(conn, now)
                if state['status'] != 'open' and now < int(product["maturity_epoch"]):
                    continue
                quote = self._quote(conn, product, now)
                expired = now >= int(product["maturity_epoch"])
                conn.execute("UPDATE product.investment SET unit_price = ? WHERE product_code = ?", (quote, product["product_code"]))
                positions = conn.execute("SELECT * FROM assets.futures_positions WHERE product_code = ?", (product["product_code"],)).fetchall()
                for position in positions:
                    collateral, net_pnl = self._mark(conn, position, product, quote, now)
                    maintenance = math.ceil(abs(int(position["quantity"])) * int(product["contract_size"]) * quote * float(product["maintenance_margin"]))
                    if not expired and (collateral >= maintenance or state['trading_halted']):
                        continue
                    tax = self.users._tax(conn, position["user_id"], "futures_realized_gain", max(0, net_pnl))
                    self.users._credit_source(conn, position["user_id"], None, collateral - tax)
                    conn.execute("DELETE FROM assets.futures_positions WHERE user_id = ? AND product_code = ?",
                                 (position["user_id"], product["product_code"]))
                    reason = "futures_maturity" if expired else "futures_liquidation"
                    conn.execute("INSERT INTO assets.product_settlements "
                        "(user_id, product_code, kind, quantity, gross_amount, tax_amount, recorded_epoch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (position["user_id"], product["product_code"], reason, position["quantity"], collateral, tax, now))
                    results.append(dict(user_id=position["user_id"], product_code=product["product_code"], reason=reason, net=collateral - tax))
                    # Liquidation must not leave an old resting order that reopens risk.
                    orders = conn.execute("SELECT * FROM assets.market_orders WHERE user_id = ? AND asset_code = ? "
                        "AND order_kind = 'future' AND status IN ('open', 'partial')", (position["user_id"], product["product_code"])).fetchall()
                    for order in orders:
                        self._cancel(conn, order, now)
                if expired:
                    orders = conn.execute("SELECT * FROM assets.market_orders WHERE asset_code = ? AND order_kind = 'future' "
                        "AND status IN ('open', 'partial')", (product["product_code"],)).fetchall()
                    for order in orders:
                        self._cancel(conn, order, now, "expired")
                    conn.execute("UPDATE product.investment SET status = 'expired' WHERE product_code = ?", (product["product_code"],))
                elif not state['trading_halted']:
                    orders = conn.execute("SELECT order_id FROM assets.market_orders WHERE asset_code = ? AND order_kind = 'future' "
                        "AND status IN ('open', 'partial') ORDER BY created_epoch, order_id", (product["product_code"],)).fetchall()
                    for order in orders:
                        conn.execute("SAVEPOINT resting_future_order")
                        try:
                            self._match(conn, order["order_id"], product, quote, now)
                        except (ValueError, RuntimeError):
                            conn.execute("ROLLBACK TO resting_future_order")
                        finally:
                            conn.execute("RELEASE resting_future_order")
        return results

    def add_margin(self, user_id, code, amount):
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 2**53 - 1:
            return False, "❌ 추가 증거금은 양의 정수 NWX여야 합니다."
        with self.db.atomic(("users", "accounts", "assets")) as conn:
            position = conn.execute("SELECT 1 FROM assets.futures_positions WHERE user_id = ? AND product_code = ?", (str(user_id), code)).fetchone()
            funds = self.users._choose_funds(conn, str(user_id))
            if not position:
                return False, "❌ 보유 선물 포지션이 없습니다."
            if not funds or funds[0] < amount or not self.users._debit_source(conn, str(user_id), funds[1], amount):
                return False, "❌ 증거금을 추가할 잔액이 부족합니다."
            conn.execute("UPDATE assets.futures_positions SET collateral = collateral + ? WHERE user_id = ? AND product_code = ?",
                         (amount, str(user_id), code))
            return True, f"✅ 선물 `{code}`에 증거금 {amount:,} NWX를 추가했습니다."
