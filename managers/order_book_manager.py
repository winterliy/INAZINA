"""A small, price/time-priority limit order book backed by SQLite."""

import time
from database.user_db import TREASURY_USER_ID
from functions.money import round_money, parse_money
from managers.market_session_manager import MarketSessionManager


class OrderBookManager:
    MAX_LIMIT_PRICE = 2**53 - 1
    MAX_QUANTITY = 2**53 - 1

    def __init__(self, db, user_manager):
        self.db = db
        self.user_manager = user_manager
        self.option_manager = None
        self.futures_manager = None

    def place_limit_order(self, user_id, asset_code, side, quantity, limit_price):
        error = MarketSessionManager(self.db).trading_error()
        if error:
            return False, error
        user_id = str(user_id)
        asset_code = str(asset_code).strip()
        side = str(side).casefold()
        product = self.db.products.execute_one('SELECT product_type,face_value FROM investment WHERE product_code=?',(asset_code,))
        if product and product['product_type']=='els':
            if side!='buy':return False,'❌ ELS는 모집 중 액면 청약만 가능합니다. 중도 매도·환매는 지원하지 않습니다.'
            if isinstance(limit_price,bool) or not isinstance(limit_price,int) or not 1 <= limit_price <= self.MAX_LIMIT_PRICE:
                return False,'❌ 지정가는 1 이상의 정수 NWX여야 합니다.'
            if limit_price < product['face_value']:return False,'❌ ELS 청약 지정가는 액면가 이상이어야 합니다.'
            from managers.els_manager import ELSManager
            return ELSManager(self.db,self.user_manager).subscribe(user_id,asset_code,quantity)
        foreign = self.db.assets.execute_one("SELECT quote_currency FROM market_assets WHERE code=? UNION ALL SELECT quote_currency FROM company WHERE company_code=? UNION ALL SELECT quote_currency FROM investment WHERE product_code=?",(asset_code,asset_code,asset_code))
        if foreign and foreign['quote_currency']!='NWX':
            from managers.foreign_securities_manager import ForeignSecuritiesManager
            return ForeignSecuritiesManager(self).order(user_id,asset_code,side,quantity,limit_price)
        if self.futures_manager and self.futures_manager.is_future(asset_code):
            return self.futures_manager.place_limit_order(user_id, asset_code, side, quantity, limit_price)
        if self.option_manager and self.option_manager.is_option(asset_code):
            return self.option_manager.place_limit_order(
                user_id, asset_code, side, quantity, limit_price
            )
        if side not in {"buy", "sell"}:
            return False, "❌ 주문 방향은 매수 또는 매도여야 합니다."
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= self.MAX_QUANTITY:
            return False, "❌ 주문 수량은 1 이상의 정수여야 합니다."
        if isinstance(limit_price, bool) or not isinstance(limit_price, int) or not 1 <= limit_price <= self.MAX_LIMIT_PRICE:
            return False, "❌ 지정가는 1 이상의 정수 NWX여야 합니다."

        interest_error = self.user_manager._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            # Matured lots are paid before calculating how many units can be offered.
            self.user_manager.bond_manager.redeem_matured(user_id=user_id)
        except Exception as exc:
            return False, f"❌ 만기 국채 정산을 완료하지 못해 주문을 진행하지 않았습니다: {exc}"

        now = int(time.time())
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                error = MarketSessionManager(self.db).trading_error(conn=conn, now=now)
                if error:
                    return False, error
                asset, source = self.user_manager._asset_row(conn, asset_code)
                if not asset:
                    return False, "❌ 존재하지 않는 자산 코드입니다."
                if asset["type"] in {"mutual_fund", "index_fund"}:
                    return False, "❌ 일반·인덱스 펀드는 `/펀드가입`·`/펀드환매`에서 NAV 기준으로 거래합니다."
                if side == "buy" and asset["status"] != "active":
                    return False, "❌ 매수 주문은 현재 거래 중인 자산만 가능합니다."
                if side == "sell" and asset["status"] not in {"active", "matured"}:
                    return False, "❌ 현재 매도 주문이 불가능한 자산입니다."
                if float(asset["price"] or 0) <= 0:
                    return False, "❌ 자산 가격이 올바르지 않습니다."

                funding_account = None
                reserved_cash = 0
                if side == "buy":
                    # A finite-supply instrument must not let one buyer reserve more
                    # units than they could ever own. Include this buyer's existing
                    # position and resting bids so repeated orders cannot bypass the
                    # per-holder cap. Primary issuance is separately capped at fill.
                    supply = asset.get("supply")
                    if source != "treasury_bond" and supply is not None:
                        try:
                            total_supply = max(0, int(supply))
                        except (TypeError, ValueError, OverflowError):
                            return False, "❌ 자산의 총 발행 수량 설정이 올바르지 않습니다."
                        owned_row = conn.execute(
                            "SELECT COALESCE(SUM(quantity), 0) AS quantity "
                            "FROM assets.user_assets WHERE user_id = ? AND asset_code = ?",
                            (user_id, asset_code),
                        ).fetchone()
                        collateral_row = conn.execute(
                            "SELECT COALESCE(SUM(quantity), 0) AS quantity "
                            "FROM assets.option_collateral_lots WHERE writer_id = ? "
                            "AND underlying_code = ?",
                            (user_id, asset_code),
                        ).fetchone()
                        reserved_row = conn.execute(
                            "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                            "FROM assets.market_orders WHERE order_kind = 'asset' "
                            "AND user_id = ? AND asset_code = ? "
                            "AND side = 'buy' AND status IN ('open', 'partial')",
                            (user_id, asset_code),
                        ).fetchone()
                        owned_quantity = int(owned_row["quantity"] or 0) + int(collateral_row["quantity"] or 0)
                        reserved_quantity = int(reserved_row["quantity"] or 0)
                        additional_capacity = max(
                            0, total_supply - owned_quantity - reserved_quantity
                        )
                        if quantity > additional_capacity:
                            return False, (
                                f"❌ 총 발행량 한도({total_supply:,}개)를 초과하는 매수 주문입니다. "
                                f"현재 보유 {owned_quantity:,}개, 미체결 매수 "
                                f"{reserved_quantity:,}개, 추가 주문 가능 "
                                f"{additional_capacity:,}개입니다."
                            )
                    reserved_cash = limit_price * quantity
                    funds = self.user_manager._choose_funds(conn, user_id)
                    if not funds:
                        return False, "❌ 등록된 사용자만 주문할 수 있습니다."
                    balance, funding_account = funds
                    if balance < reserved_cash or not self.user_manager._debit_source(
                        conn, user_id, funding_account, reserved_cash
                    ):
                        return False, (
                            f"❌ 잔액이 부족합니다. 지정가 기준 예치 필요액: "
                            f"{reserved_cash:,.0f} NWX"
                        )
                else:
                    if source == "treasury_bond":
                        owned_quantity = self.user_manager.bond_manager.available_units(
                            conn, user_id, asset_code, now=now
                        )
                    else:
                        positions = conn.execute(
                            "SELECT COALESCE(SUM(quantity), 0) AS quantity "
                            "FROM assets.user_assets WHERE user_id = ? AND asset_code = ?",
                            (user_id, asset_code),
                        ).fetchone()
                        owned_quantity = int(positions["quantity"] or 0)
                    already_reserved = conn.execute(
                        "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                        "FROM assets.market_orders WHERE order_kind = 'asset' "
                        "AND user_id = ? AND asset_code = ? "
                        "AND side = 'sell' AND status IN ('open', 'partial')",
                        (user_id, asset_code),
                    ).fetchone()
                    available = owned_quantity - int(already_reserved["quantity"] or 0)
                    if available < quantity:
                        return False, f"❌ 매도 가능 수량이 부족합니다. (주문 가능 {max(0, available):,}개)"

                cursor = conn.execute(
                    "INSERT INTO assets.market_orders "
                    "(user_id, asset_code, side, limit_price, quantity, remaining_quantity, "
                    "reserved_cash, funding_account, order_kind, status, created_epoch, updated_epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'asset', 'open', ?, ?)",
                    (user_id, asset_code, side, limit_price, quantity, quantity,
                     reserved_cash, funding_account, now, now),
                )
                order_id = int(cursor.lastrowid)
                fill_count, filled_quantity, traded_value = self._match_incoming_order(
                    conn, order_id, now
                )
                order = conn.execute(
                    "SELECT remaining_quantity, status, reserved_cash "
                    "FROM assets.market_orders WHERE order_id = ?", (order_id,)
                ).fetchone()

            filled_text = f"즉시 체결 {filled_quantity:,}개 · 체결대금 {traded_value:,.0f} NWX"
            if order["remaining_quantity"]:
                pending_text = (
                    f"미체결 {int(order['remaining_quantity']):,}개는 호가에 남았습니다."
                )
                if side == "buy":
                    pending_text += f" 매수 예치액 {float(order['reserved_cash']):,.0f} NWX는 잠금 상태입니다."
                else:
                    pending_text += " 미체결 수량은 다른 매도에 사용할 수 없습니다."
            else:
                pending_text = "주문이 전량 체결되었습니다."
            return True, (
                f"✅ 주문 #{order_id} 등록 ({'매수' if side == 'buy' else '매도'} · "
                f"지정가 {limit_price:,} NWX · {quantity:,}개)\n"
                f"{filled_text}\n{pending_text}"
            )
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 주문 처리 중 오류가 발생했습니다: {exc}"

    def _match_incoming_order(self, conn, incoming_order_id, now):
        if MarketSessionManager(self.db).trading_error(conn=conn, now=now):
            return 0, 0, 0
        fill_count = 0
        filled_quantity = 0
        traded_value = 0
        while True:
            incoming = conn.execute(
                "SELECT * FROM assets.market_orders WHERE order_id = ?",
                (incoming_order_id,),
            ).fetchone()
            if not incoming or int(incoming["remaining_quantity"]) <= 0:
                break

            opposite_side = "sell" if incoming["side"] == "buy" else "buy"
            comparison = "<=" if incoming["side"] == "buy" else ">="
            sort_order = "ASC" if incoming["side"] == "buy" else "DESC"
            maker = conn.execute(
                "SELECT * FROM assets.market_orders "
                "WHERE order_kind = 'asset' AND asset_code = ? AND side = ? "
                "AND status IN ('open', 'partial') "
                f"AND limit_price {comparison} ? AND user_id <> ? "
                f"ORDER BY limit_price {sort_order}, created_epoch ASC, order_id ASC LIMIT 1",
                (incoming["asset_code"], opposite_side, incoming["limit_price"], incoming["user_id"]),
            ).fetchone()

            # Finite unissued company/ETF supply is offered by the treasury at
            # the current reference price. It bootstraps newly listed assets
            # without providing unlimited secondary-market liquidity.
            if incoming["side"] == "buy":
                asset, source = self.user_manager._asset_row(conn, incoming["asset_code"])
                supply = asset.get("supply") if asset else None
                primary_remaining = 0
                reference_price = int(float(asset["price"] or 0)) if asset else 0
                if source == "treasury_bond":
                    face_value = int(asset["face_value"])
                    primary_remaining = max(
                        0,
                        (int(asset["total_supply"]) - int(asset["sold_amount"] or 0))
                        // face_value,
                    )
                    # The issuer's remaining primary offer stays at par. The last
                    # market trade only sets the secondary-market reference price.
                    reference_price = face_value
                elif supply is not None and reference_price > 0:
                    issued = conn.execute(
                        "SELECT (SELECT COALESCE(SUM(quantity), 0) FROM assets.user_assets "
                        "WHERE asset_code = ?) + (SELECT COALESCE(SUM(quantity), 0) "
                        "FROM assets.option_collateral_lots WHERE underlying_code = ?) AS quantity",
                        (incoming["asset_code"], incoming["asset_code"]),
                    ).fetchone()
                    primary_remaining = max(0, int(supply) - int(issued["quantity"] or 0))
                if (
                    primary_remaining > 0
                    and reference_price <= int(incoming["limit_price"])
                    and (not maker or reference_price < int(maker["limit_price"]))
                ):
                    quantity = min(int(incoming["remaining_quantity"]), primary_remaining)
                    self._settle_primary_fill(
                        conn, incoming, asset, source, quantity, reference_price, now
                    )
                    self._reduce_order(conn, incoming, quantity, now)
                    self._apply_execution_price(
                        conn, incoming["asset_code"], reference_price, now
                    )
                    conn.execute(
                        "INSERT INTO assets.market_trade_history "
                        "(asset_code, side, quantity, price, recorded_epoch) "
                        "VALUES (?, 'buy', ?, ?, ?)",
                        (incoming["asset_code"], quantity, reference_price, now),
                    )
                    fill_count += 1
                    filled_quantity += quantity
                    traded_value += reference_price * quantity
                    continue
            if not maker:
                break

            buy_order = incoming if incoming["side"] == "buy" else maker
            sell_order = incoming if incoming["side"] == "sell" else maker
            quantity = min(
                int(incoming["remaining_quantity"]), int(maker["remaining_quantity"])
            )
            # Price-time priority: an incoming order executes at the resting maker's price.
            execution_price = int(maker["limit_price"])
            self._settle_fill(conn, buy_order, sell_order, quantity, execution_price, now)
            self._reduce_order(conn, buy_order, quantity, now)
            self._reduce_order(conn, sell_order, quantity, now)
            self._apply_execution_price(
                conn, incoming["asset_code"], execution_price, now
            )
            conn.execute(
                "INSERT INTO assets.market_trade_history "
                "(asset_code, side, quantity, price, recorded_epoch) VALUES (?, ?, ?, ?, ?)",
                (incoming["asset_code"], incoming["side"], quantity, execution_price, now),
            )
            fill_count += 1
            filled_quantity += quantity
            traded_value += execution_price * quantity
        return fill_count, filled_quantity, traded_value

    def _settle_primary_fill(self, conn, buy_order, asset, source, quantity, price, now):
        buyer_id = str(buy_order["user_id"])
        buyer_account = buy_order["funding_account"]
        improvement = (int(buy_order["limit_price"]) - price) * quantity
        if improvement < 0:
            raise RuntimeError("기준가가 매수 지정가를 초과했습니다.")
        if improvement:
            self._credit_reserved_source(conn, buyer_id, buyer_account, improvement, now)
        if source == "treasury_bond":
            face_value = int(asset["face_value"])
            principal = face_value * int(quantity)
            sold = conn.execute(
                "UPDATE product.treasury_bond_products SET sold_amount = sold_amount + ? "
                "WHERE product_code = ? AND status = 'active' "
                "AND sold_amount + ? <= total_supply",
                (principal, asset["code"], principal),
            )
            if sold.rowcount != 1:
                raise RuntimeError("국채 잔여 발행한도가 변경되어 체결을 취소했습니다.")
            maturity = int(now) + int(asset["maturity_days"]) * 86400
            conn.execute(
                "INSERT INTO accounts.treasury_bond_holdings "
                "(user_id, product_code, product_name, annual_rate, maturity_days, "
                "principal, face_value, acquisition_cost, purchased_epoch, maturity_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (buyer_id, asset["code"], asset["name"], float(asset["annual_rate"]),
                 int(asset["maturity_days"]), principal, face_value,
                 int(price) * int(quantity), int(now), maturity),
            )
            treasury = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                (principal, TREASURY_USER_ID),
            )
            if treasury.rowcount != 1:
                raise RuntimeError("국고 계정을 찾을 수 없어 국채 매수를 취소했습니다.")
            return
        conn.execute(
            """INSERT INTO assets.user_assets
               (user_id, asset_code, account_number, quantity, average_cost)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, asset_code, IFNULL(account_number, '')) DO UPDATE SET
                   average_cost = ((quantity * average_cost) +
                       (excluded.quantity * excluded.average_cost)) / (quantity + excluded.quantity),
                   quantity = quantity + excluded.quantity""",
            (buyer_id, buy_order["asset_code"], buyer_account, quantity, price),
        )
        proceeds = int(price * quantity)
        product = conn.execute("SELECT product_type, issuer_code FROM product.investment WHERE product_code = ?",
                               (buy_order["asset_code"],)).fetchone()
        if source=='company' and conn.execute("SELECT 1 FROM company WHERE company_code=? AND dividend_funding='issuer'",(buy_order['asset_code'],)).fetchone():
            owner=conn.execute('SELECT COALESCE(parent_company_code,company_code) FROM company WHERE company_code=?',(buy_order['asset_code'],)).fetchone()[0]
            conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(owner,))
            cursor=conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=? AND balance<=?',(proceeds,owner,self.MAX_LIMIT_PRICE-proceeds))
        elif product and product["product_type"] == "corporate_bond":
            cursor = conn.execute("UPDATE company.company SET capital = capital + ? WHERE company_code = ?",
                                  (proceeds, product["issuer_code"]))
            if conn.execute('SELECT 1 FROM issuer_cash_accounts WHERE company_code=?',(product['issuer_code'],)).fetchone():
                cash_credit=conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=? AND balance<=?',(proceeds,product['issuer_code'],self.MAX_LIMIT_PRICE-proceeds))
                if cash_credit.rowcount!=1:raise ValueError('법인 현금 한도를 초과합니다.')
        else:
            cursor = conn.execute("UPDATE users.users SET cash = cash + ? WHERE user_id = ?", (proceeds, TREASURY_USER_ID))
        if cursor.rowcount != 1:
            raise RuntimeError("국고 계정을 찾을 수 없어 발행주식 매수를 취소했습니다.")
        if source == "company":
            self.user_manager._refresh_max_shareholder(conn, buy_order["asset_code"])

    @staticmethod
    def _apply_execution_price(conn, asset_code, execution_price, now):
        """Make the last execution the reference price for ordinary listings."""
        record_price = False
        market_asset = conn.execute(
            "SELECT is_fixed FROM assets.market_assets WHERE code = ?", (asset_code,)
        ).fetchone()
        if market_asset and not int(market_asset["is_fixed"] or 0):
            conn.execute(
                "UPDATE assets.market_assets SET price = ? WHERE code = ? AND is_fixed = 0",
                (execution_price, asset_code),
            )
            record_price = True
        company = conn.execute(
            "SELECT is_fixed FROM company.company WHERE company_code = ?", (asset_code,)
        ).fetchone()
        if company and not int(company["is_fixed"] or 0):
            conn.execute(
                "UPDATE company.company SET share_price = ? "
                "WHERE company_code = ? AND is_fixed = 0",
                (execution_price, asset_code),
            )
            record_price = True
        investment = conn.execute(
            "SELECT is_fixed, underlying_exchange_id FROM product.investment "
            "WHERE product_code = ?", (asset_code,)
        ).fetchone()
        if (
            investment and not investment["underlying_exchange_id"]
            and not int(investment["is_fixed"] or 0)
        ):
            conn.execute(
                "UPDATE product.investment SET unit_price = ? "
                "WHERE product_code = ? AND is_fixed = 0 "
                "AND underlying_exchange_id IS NULL",
                (execution_price, asset_code),
            )
            record_price = True
        bond = conn.execute(
            "SELECT 1 FROM product.treasury_bond_products WHERE product_code = ?",
            (asset_code,),
        ).fetchone()
        if bond:
            conn.execute(
                "UPDATE product.treasury_bond_products SET market_price = ? "
                "WHERE product_code = ?",
                (int(execution_price), asset_code),
            )
            record_price = True
        if record_price:
            conn.execute(
                "INSERT INTO assets.market_price_history "
                "(asset_code, price, recorded_epoch) VALUES (?, ?, ?) "
                "ON CONFLICT(asset_code, recorded_epoch) DO UPDATE SET price = excluded.price",
                (asset_code, execution_price, now),
            )

    def _settle_fill(self, conn, buy_order, sell_order, quantity, execution_price, now):
        buyer_id = str(buy_order["user_id"])
        seller_id = str(sell_order["user_id"])
        buy_limit = int(buy_order["limit_price"])
        buyer_account = buy_order["funding_account"]

        # The buyer's full limit amount was reserved at placement. Refund the
        # price improvement when an ask executes below that limit.
        price_improvement = (buy_limit - execution_price) * quantity
        if price_improvement < 0:
            raise RuntimeError("체결 가격이 매수 지정가를 초과했습니다.")
        if price_improvement:
            self._credit_reserved_source(conn, buyer_id, buyer_account, price_improvement, now)

        asset, source = self.user_manager._asset_row(conn, buy_order["asset_code"])
        if source == "treasury_bond":
            basis = self.user_manager.bond_manager.transfer_units(
                conn, seller_id, buyer_id, buy_order["asset_code"], quantity,
                execution_price, now,
            )
            taxable_gain = max(0, int(execution_price) * int(quantity) - int(basis))
            tax = self.user_manager._tax(conn, seller_id, "bond_sale_gain", taxable_gain)
            proceeds = max(0, int(execution_price) * int(quantity) - tax)
            conn.execute(
                "UPDATE users.users SET cash = cash + ? "
                "WHERE user_id = ? AND (status = 'active' OR user_id='__treasury__')",
                (proceeds, seller_id),
            )
            return

        conn.execute(
            """INSERT INTO assets.user_assets
               (user_id, asset_code, account_number, quantity, average_cost)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, asset_code, IFNULL(account_number, '')) DO UPDATE SET
                   average_cost = ((quantity * average_cost) +
                       (excluded.quantity * excluded.average_cost)) / (quantity + excluded.quantity),
                   quantity = quantity + excluded.quantity""",
            (buyer_id, buy_order["asset_code"], buyer_account, quantity, execution_price),
        )

        positions = conn.execute(
            "SELECT holding_id, account_number, quantity, average_cost "
            "FROM assets.user_assets WHERE user_id = ? AND asset_code = ? "
            "ORDER BY holding_id",
            (seller_id, sell_order["asset_code"]),
        ).fetchall()
        left = quantity
        sold = []
        taxable_gain = 0
        for position in positions:
            take = min(left, int(position["quantity"]))
            if take <= 0:
                continue
            average_cost = int(round(position["average_cost"] or 0))
            taxable_gain += max(0, execution_price - average_cost) * take
            sold.append((position, take))
            left -= take
            if left == 0:
                break
        if left:
            raise RuntimeError("매도 주문의 예약 수량과 실제 보유량이 달라 체결을 취소했습니다.")

        total_tax = self.user_manager._tax(
            conn, seller_id, "stock_sale_gain", taxable_gain
        )
        tax_left = total_tax
        quantity_left = quantity
        for position, take in sold:
            tax_share = tax_left if take == quantity_left else round_money(total_tax * take / quantity)
            old_quantity = int(position["quantity"])
            if take == old_quantity:
                cursor = conn.execute(
                    "DELETE FROM assets.user_assets WHERE holding_id = ? AND quantity = ?",
                    (position["holding_id"], old_quantity),
                )
            else:
                cursor = conn.execute(
                    "UPDATE assets.user_assets SET quantity = quantity - ? "
                    "WHERE holding_id = ? AND quantity > ?",
                    (take, position["holding_id"], take),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("보유 수량이 변경되어 체결을 취소했습니다.")
            net_proceeds = max(0, execution_price * take - tax_share)
            self.user_manager._credit_source(
                conn, seller_id, position["account_number"], net_proceeds
            )
            tax_left -= tax_share
            quantity_left -= take

        if self.user_manager._asset_row(conn, sell_order["asset_code"])[1] == "company":
            from managers.corporate_actions_manager import CorporateActionsManager
            CorporateActionsManager.record_issue_fill(conn, sell_order["order_id"], quantity, execution_price)
            self.user_manager._refresh_max_shareholder(conn, sell_order["asset_code"])

    @staticmethod
    def _credit_reserved_source(conn, user_id, account_number, amount, now):
        amount = parse_money(amount, minimum=0)
        if amount <= 0:
            return
        if account_number is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ? AND (status = 'active' OR user_id='__treasury__')",
                (amount, user_id),
            )
        else:
            foreign = conn.execute('SELECT currency_code FROM fx_accounts WHERE account_number=? AND user_id=?',(account_number,str(user_id))).fetchone()
            if foreign:
                from managers.foreign_securities_manager import ForeignSecuritiesManager
                from managers.fx_manager import SCALE
                ForeignSecuritiesManager.credit_units(conn,user_id,foreign['currency_code'],amount*SCALE)
                return
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance = balance + ?, "
                "principal = COALESCE(principal, 0) + ?, last_payment_epoch = ?, "
                "last_payment = CURRENT_TIMESTAMP WHERE account_number = ? AND user_id = ?",
                (amount, amount, now, account_number, user_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("매수 주문 예치금을 환불할 계좌를 찾지 못했습니다.")

    @staticmethod
    def _reduce_order(conn, order, quantity, now):
        old_remaining = int(order["remaining_quantity"])
        new_remaining = old_remaining - quantity
        if new_remaining < 0:
            raise RuntimeError("주문 잔량보다 많은 수량이 체결되었습니다.")
        reserved_cash = int(round(order["reserved_cash"] or 0))
        if order["side"] == "buy":
            reserved_cash = max(0, reserved_cash - int(order["limit_price"]) * quantity)
        status = "filled" if new_remaining == 0 else "partial"
        conn.execute(
            "UPDATE assets.market_orders SET remaining_quantity = ?, "
            "filled_quantity = filled_quantity + ?, reserved_cash = ?, "
            "status = ?, updated_epoch = ? WHERE order_id = ?",
            (new_remaining, quantity, reserved_cash, status, now, order["order_id"]),
        )

    def cancel_order(self, user_id, order_id):
        user_id = str(user_id)
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return False, "❌ 올바른 주문 번호를 입력해주세요."
        if self.futures_manager and self.db.assets.execute_one(
            "SELECT 1 FROM market_orders WHERE order_id = ? AND user_id = ? AND order_kind = 'future'", (order_id, user_id)):
            return self.futures_manager.cancel_order(user_id, order_id)
        if self.option_manager:
            option_order = self.db.assets.execute_one(
                "SELECT 1 FROM market_orders WHERE order_id = ? AND user_id = ? "
                "AND order_kind = 'option'",
                (order_id, user_id),
            )
            if option_order:
                return self.option_manager.cancel_order(user_id, order_id)
        now = int(time.time())
        try:
            with self.db.atomic(("users", "accounts", "assets")) as conn:
                order = conn.execute(
                    "SELECT * FROM assets.market_orders WHERE order_id = ? AND user_id = ? "
                    "AND order_kind = 'asset'",
                    (order_id, user_id),
                ).fetchone()
                if not order:
                    return False, "❌ 본인의 주문을 찾을 수 없습니다."
                if order["status"] not in {"open", "partial"}:
                    return False, f"❌ 이미 {order['status']} 상태라 취소할 수 없습니다."
                refund = int(round(order["reserved_cash"] or 0)) if order["side"] == "buy" else 0
                if refund:
                    self._credit_reserved_source(
                        conn, user_id, order["funding_account"], refund, now
                    )
                conn.execute(
                    "UPDATE assets.market_orders SET status = 'cancelled', "
                    "reserved_cash = 0, updated_epoch = ? "
                    "WHERE order_id = ?",
                    (now, order_id),
                )
            message = f"✅ 주문 #{order_id}을 취소했습니다."
            if refund:
                message += f" 예치금 {refund:,.0f} NWX를 반환했습니다."
            return True, message
        except Exception as exc:
            return False, f"❌ 주문 취소 중 오류가 발생했습니다: {exc}"

    def get_user_open_orders(self, user_id, limit=25):
        return self.db.assets.execute(
            "SELECT order_id, asset_code, side, limit_price, quantity, filled_quantity, "
            "remaining_quantity, reserved_cash, status, created_epoch, order_kind, option_order_type "
            "FROM market_orders WHERE user_id = ? AND status IN ('open', 'partial') "
            "ORDER BY order_id DESC LIMIT ?",
            (str(user_id), max(1, min(int(limit), 100))),
        )

    def get_user_orders(self, user_id, limit=25):
        return self.db.assets.execute(
            "SELECT order_id, asset_code, side, limit_price, quantity, filled_quantity, "
            "remaining_quantity, reserved_cash, status, created_epoch, updated_epoch, "
            "order_kind, option_order_type "
            "FROM market_orders WHERE user_id = ? AND (status IN ('open', 'partial') "
            "OR updated_epoch >= ?) ORDER BY "
            "CASE WHEN status IN ('open', 'partial') THEN 0 ELSE 1 END, "
            "updated_epoch DESC LIMIT ?",
            (str(user_id), int(time.time()) - 86400, max(1, min(int(limit), 100))),
        )

    def get_order_book(self, asset_code, depth=10):
        rows = self.db.assets.execute(
            "SELECT side, limit_price, SUM(remaining_quantity) AS quantity, COUNT(*) AS order_count "
            "FROM market_orders WHERE asset_code = ? AND status IN ('open', 'partial') "
            "GROUP BY side, limit_price",
            (str(asset_code),),
        )
        bids = [dict(row) for row in rows if row["side"] == "buy"]
        asks = [dict(row) for row in rows if row["side"] == "sell"]
        bids.sort(key=lambda row: int(row["limit_price"]), reverse=True)
        asks.sort(key=lambda row: int(row["limit_price"]))
        return {"bids": bids[:depth], "asks": asks[:depth]}
