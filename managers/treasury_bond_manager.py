import math
import re
import time

from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.money import parse_money, round_money
from functions.tax_calc import calculate_income_tax


class TreasuryBondManager:
    """Government bond offers, citizen holdings, treasury funding and maturity redemption."""

    YEAR_DAYS = 365

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._migrate_legacy_holdings()

    def _migrate_legacy_holdings(self):
        """Backfill bond lot denomination and cost basis added for exchange trading."""
        with self.db.atomic(("accounts", "product")) as conn:
            conn.execute(
                "UPDATE accounts.treasury_bond_holdings AS h "
                "SET face_value = COALESCE((SELECT p.face_value "
                "FROM product.treasury_bond_products p WHERE p.product_code = h.product_code), "
                "face_value) "
                "WHERE face_value = 1 AND EXISTS (SELECT 1 "
                "FROM product.treasury_bond_products p WHERE p.product_code = h.product_code)"
            )
            conn.execute(
                "UPDATE accounts.treasury_bond_holdings SET acquisition_cost = principal "
                "WHERE acquisition_cost <= 0"
            )

    def issue(
        self, code, name, annual_rate, maturity_days, face_value, total_supply,
        exchange_id="BOND",
    ):
        code = str(code or "").strip().upper()
        name = str(name or "").strip()
        exchange_id = str(exchange_id or "").strip()
        try:
            face_value = parse_money(face_value, minimum=1)
            total_supply = parse_money(total_supply, minimum=1)
            maturity_days = int(maturity_days)
            annual_rate = float(annual_rate)
        except (TypeError, ValueError, OverflowError) as exc:
            return False, f"❌ 국채 조건을 확인해주세요: {exc}"
        if not re.fullmatch(r"[A-Z0-9_-]{1,32}", code):
            return False, "❌ 국채 코드는 영문 대문자·숫자·_·-만 사용해 1~32자로 입력해주세요."
        if not name or len(name) > 100:
            return False, "❌ 국채 이름은 1~100자로 입력해주세요."
        if not exchange_id or len(exchange_id) > 100:
            return False, "❌ 거래소 ID는 1~100자로 입력해주세요."
        if not math.isfinite(annual_rate) or annual_rate < 0 or annual_rate > 1000:
            return False, "❌ 연이율은 0~1,000% 사이로 입력해주세요."
        if not 1 <= maturity_days <= 36500:
            return False, "❌ 만기는 1~36,500일로 입력해주세요."
        if total_supply % face_value:
            return False, "❌ 총 발행한도는 고정 액면가의 배수여야 합니다."
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company")) as conn:
                if DatabaseManager.code_locations(conn, code):
                    return False, f"❌ 코드 `{code}`가 다른 상품·자산에서 사용 중입니다."
                if conn.execute(
                    "SELECT 1 FROM product.treasury_bond_products WHERE product_code = ?",
                    (code,),
                ).fetchone():
                    return False, f"❌ 국채 코드 `{code}`가 이미 존재합니다."
                conn.execute(
                    "INSERT INTO product.treasury_bond_products "
                    "(product_code, name, annual_rate, maturity_days, face_value, "
                    "exchange_id, market_price, total_supply) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (code, name, annual_rate, maturity_days, face_value,
                     exchange_id, face_value, total_supply),
                )
                now = int(time.time())
                conn.execute(
                    "INSERT INTO assets.market_price_history "
                    "(asset_code, price, recorded_epoch) VALUES (?, ?, ?) "
                    "ON CONFLICT(asset_code, recorded_epoch) DO UPDATE SET price = excluded.price",
                    (code, face_value, now),
                )
            return True, (
                f"✅ 국채 `{code}` · **{name}** 발행 완료\n"
                f"액면가 {face_value:,} NWX · 총 {total_supply // face_value:,}매 "
                f"(총 발행한도 {total_supply:,} NWX) · 거래소 `{exchange_id}`\n"
                f"연이율 {annual_rate:g}% · 만기 {maturity_days:,}일"
            )
        except Exception as exc:
            return False, f"❌ 국채 발행 중 오류가 발생했습니다: {exc}"

    def get_active_offerings(self):
        return self.db.products.execute(
            "SELECT product_code, name, annual_rate, maturity_days, face_value, "
            "exchange_id, market_price, total_supply, sold_amount "
            "FROM treasury_bond_products WHERE status = 'active' "
            "AND sold_amount < total_supply ORDER BY name"
        )

    @staticmethod
    def _debit_funds(conn, user_id, amount):
        user = conn.execute(
            f"SELECT cash FROM users.users WHERE user_id = ? AND (status = 'active' OR user_id='{TREASURY_USER_ID}')",
            (user_id,),
        ).fetchone()
        if not user:
            return False, "지급·매수를 진행할 등록 시민이 없습니다."
        sources = [(int(round(user["cash"] or 0)), None)]
        sources.extend(
            (int(round(row["balance"] or 0)), row["account_number"])
            for row in conn.execute(
                "SELECT account_number, balance FROM accounts.accounts WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        )
        balance, account_number = max(sources, key=lambda item: item[0])
        if balance < amount:
            return False, "현금·통장 잔액이 부족합니다."
        if account_number is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash - ? "
                "WHERE user_id = ? AND status = 'active' AND cash >= ?",
                (amount, user_id, amount),
            )
        else:
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance = balance - ?, "
                "principal = MAX(0, COALESCE(principal, 0) - ?), "
                "last_payment_epoch = unixepoch('now'), last_payment = CURRENT_TIMESTAMP "
                "WHERE account_number = ? AND user_id = ? AND balance >= ?",
                (amount, amount, account_number, user_id, amount),
            )
        return cursor.rowcount == 1, "잔액이 변경되어 국채 매수를 취소했습니다."

    def buy(self, user_id, product_code, amount):
        try:
            amount = parse_money(amount, minimum=1)
        except ValueError as exc:
            return False, f"❌ 매수 금액은 정수 NWX로 입력해주세요. ({exc})"

        try:
            from managers.interest_manager import InterestManager

            InterestManager(self.db).process_user_interests(str(user_id))
        except Exception as exc:
            return False, f"❌ 국채 매수 전 이자 정산에 실패했습니다: {exc}"

        try:
            with self.db.atomic(("users", "accounts", "assets", "product")) as conn:
                from managers.market_session_manager import MarketSessionManager
                error = MarketSessionManager(self.db).trading_error(conn=conn)
                if error:
                    return False, error
                bond = conn.execute(
                    "SELECT product_code, name, annual_rate, maturity_days, face_value, "
                    "exchange_id, market_price, total_supply, sold_amount, status "
                    "FROM product.treasury_bond_products WHERE product_code = ?",
                    (str(product_code).upper(),),
                ).fetchone()
                if not bond or bond["status"] != "active":
                    return False, "❌ 판매 중인 국채를 찾을 수 없습니다."
                face_value = int(bond["face_value"])
                if amount % face_value:
                    return False, (
                        f"❌ 이 국채는 액면가 {face_value:,} NWX 단위로만 매수할 수 있습니다. "
                        f"매수 금액을 액면가의 배수로 입력해주세요."
                    )
                remaining = int(bond["total_supply"]) - int(bond["sold_amount"] or 0)
                if amount > remaining:
                    return False, f"❌ 발행 잔여 한도를 초과했습니다. 남은 한도: {remaining:,} NWX"
                debited, reason = self._debit_funds(conn, str(user_id), amount)
                if not debited:
                    return False, f"❌ {reason}"
                update = conn.execute(
                    "UPDATE product.treasury_bond_products SET sold_amount = sold_amount + ? "
                    "WHERE product_code = ? AND status = 'active' "
                    "AND sold_amount + ? <= total_supply",
                    (amount, bond["product_code"], amount),
                )
                if update.rowcount != 1:
                    raise RuntimeError("국채 잔여 발행한도가 변경되어 매수를 취소했습니다.")
                now = int(time.time())
                maturity = now + int(bond["maturity_days"]) * 86400
                conn.execute(
                    "INSERT INTO accounts.treasury_bond_holdings "
                    "(user_id, product_code, product_name, annual_rate, maturity_days, "
                    "principal, face_value, acquisition_cost, purchased_epoch, maturity_epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(user_id), bond["product_code"], bond["name"],
                     float(bond["annual_rate"]), int(bond["maturity_days"]),
                     amount, face_value, amount, now, maturity),
                )
                treasury = conn.execute(
                    "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                    (amount, TREASURY_USER_ID),
                )
                if treasury.rowcount != 1:
                    raise RuntimeError("국고 계정을 찾을 수 없어 국채 매수를 취소했습니다.")
                conn.execute(
                    "UPDATE product.treasury_bond_products SET market_price = ? "
                    "WHERE product_code = ?",
                    (face_value, bond["product_code"]),
                )
                conn.execute(
                    "INSERT INTO assets.market_price_history "
                    "(asset_code, price, recorded_epoch) VALUES (?, ?, ?) "
                    "ON CONFLICT(asset_code, recorded_epoch) DO UPDATE SET price = excluded.price",
                    (bond["product_code"], face_value, now),
                )
            return True, (
                f"✅ **{bond['name']}** 국채를 {amount:,} NWX 매수했습니다. "
                f"(액면가 {face_value:,} NWX · {amount // face_value:,}매) "
                f"만기: <t:{maturity}:D> · 대금은 국고에 입금됐습니다."
            )
        except Exception as exc:
            return False, f"❌ 국채 매수 중 오류가 발생했습니다: {exc}"

    def redeem_matured(self, user_id=None, now=None):
        """Automatically repay matured principal plus simple interest, less tax."""
        now = int(time.time() if now is None else now)
        user_id = None if user_id is None else str(user_id)
        redeemed = []
        pending = self.db.accounts.execute_one(
            "SELECT 1 FROM treasury_bond_holdings WHERE status = 'active' "
            "AND maturity_epoch <= ? AND (? IS NULL OR user_id = ?) LIMIT 1",
            (now, user_id, user_id),
        )
        if not pending:
            return redeemed
        try:
            with self.db.atomic(("users", "accounts", "assets", "tax")) as conn:
                tax_row = conn.execute(
                    "SELECT value FROM settings WHERE key='tax_percent'"
                ).fetchone()
                try:
                    tax_percent = float(tax_row["value"] if tax_row else 3)
                except (TypeError, ValueError):
                    tax_percent = 3.0
                if not math.isfinite(tax_percent) or not 0 <= tax_percent <= 100:
                    tax_percent = 3.0
                query = (
                    "SELECT h.holding_id, h.user_id, h.product_code, h.principal, "
                    "h.purchased_epoch, h.maturity_epoch, h.maturity_days, "
                    "h.product_name AS name, h.annual_rate "
                    "FROM accounts.treasury_bond_holdings h "
                    "WHERE h.status = 'active' AND h.maturity_epoch <= ? "
                )
                params = [now]
                if user_id is not None:
                    query += "AND h.user_id = ? "
                    params.append(user_id)
                query += "ORDER BY h.maturity_epoch, h.holding_id"
                rows = conn.execute(query, params).fetchall()
                matured_codes = set()
                for holding in rows:
                    principal = int(holding["principal"])
                    days = int(holding["maturity_days"])
                    gross_interest = round_money(
                        principal * float(holding["annual_rate"]) * days
                        / (100 * self.YEAR_DAYS)
                    )
                    tax = calculate_income_tax(gross_interest, tax_percent / 100)
                    gross_payment = principal + gross_interest
                    net_payment = principal + gross_interest - tax
                    treasury_debit = conn.execute(
                        "UPDATE users.users SET cash = cash - ? WHERE user_id = ?",
                        (gross_payment, TREASURY_USER_ID),
                    )
                    if treasury_debit.rowcount != 1:
                        raise RuntimeError("국고 계정을 찾을 수 없어 국채 상환을 취소했습니다.")
                    if tax:
                        conn.execute(
                            "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                            (tax, TREASURY_USER_ID),
                        )
                    credit = conn.execute(
                        "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                        (net_payment, holding["user_id"]),
                    )
                    if credit.rowcount != 1:
                        raise RuntimeError("국채 보유자가 활성 사용자가 아니어서 상환을 취소했습니다.")
                    closed = conn.execute(
                        "UPDATE accounts.treasury_bond_holdings SET status = 'redeemed', "
                        "redeemed_epoch = ? WHERE holding_id = ? AND status = 'active'",
                        (now, holding["holding_id"]),
                    )
                    if closed.rowcount != 1:
                        raise RuntimeError("국채 상태가 변경되어 상환을 취소했습니다.")
                    redeemed.append({
                        "user_id": str(holding["user_id"]),
                        "name": str(holding["name"]),
                        "principal": principal,
                        "interest": gross_interest,
                        "tax": tax,
                        "net": net_payment,
                    })
                    matured_codes.add((str(holding["user_id"]), str(holding["product_code"])))
                for matured_user_id, product_code in matured_codes:
                    # Matured units can no longer be delivered into an exchange fill.
                    # Clear the owner's resting sell orders so they do not reserve phantom units.
                    conn.execute(
                        "UPDATE assets.market_orders SET status = 'cancelled', "
                        "remaining_quantity = 0, reserved_cash = 0, updated_epoch = ? "
                        "WHERE user_id = ? AND asset_code = ? AND side = 'sell' "
                        "AND status IN ('open', 'partial')",
                        (now, matured_user_id, product_code),
                    )
            return redeemed
        except Exception:
            raise

    def get_user_holdings(self, user_id, now=None):
        now = int(time.time() if now is None else now)
        with self.db.atomic(("accounts", "product")) as conn:
            rows = conn.execute(
                "SELECT h.holding_id, h.product_code, h.principal, h.face_value, "
                "h.purchased_epoch, h.maturity_epoch, h.maturity_days, "
                "h.product_name AS name, h.annual_rate, h.acquisition_cost, "
                "COALESCE(p.market_price, h.face_value) AS market_price "
                "FROM accounts.treasury_bond_holdings h "
                "LEFT JOIN product.treasury_bond_products p ON p.product_code = h.product_code "
                "WHERE h.user_id = ? AND h.status = 'active' ORDER BY h.maturity_epoch",
                (str(user_id),),
            ).fetchall()
        holdings = []
        for row in rows:
            days = int(row["maturity_days"])
            projected_interest = round_money(
                int(row["principal"]) * float(row["annual_rate"]) * days
                / (100 * self.YEAR_DAYS)
            )
            face_value = int(row["face_value"] or 1)
            quantity = int(row["principal"]) // face_value
            market_price = max(1, int(round(row["market_price"] or face_value)))
            holdings.append({
                "holding_id": int(row["holding_id"]),
                "product_code": str(row["product_code"]),
                "name": str(row["name"]),
                "principal": int(row["principal"]),
                "face_value": face_value,
                "quantity": quantity,
                "acquisition_cost": int(row["acquisition_cost"] or row["principal"]),
                "market_price": market_price,
                "market_value": quantity * market_price,
                "maturity_epoch": int(row["maturity_epoch"]),
                "projected_interest": projected_interest,
                "projected_total": int(row["principal"]) + projected_interest,
            })
        return holdings

    @staticmethod
    def available_units(conn, user_id, product_code, now=None):
        now = int(time.time() if now is None else now)
        rows = conn.execute(
            "SELECT principal, face_value FROM accounts.treasury_bond_holdings "
            "WHERE user_id = ? AND product_code = ? AND status = 'active' "
            "AND maturity_epoch > ?",
            (str(user_id), str(product_code), now),
        ).fetchall()
        return sum(
            int(row["principal"]) // max(1, int(row["face_value"] or 1))
            for row in rows
        )

    @staticmethod
    def transfer_units(conn, seller_id, buyer_id, product_code, quantity, price, now):
        """Move existing claims with their coupon and maturity terms to a buyer."""
        rows = conn.execute(
            "SELECT holding_id, product_name, annual_rate, maturity_days, principal, "
            "face_value, acquisition_cost, purchased_epoch, maturity_epoch "
            "FROM accounts.treasury_bond_holdings WHERE user_id = ? AND product_code = ? "
            "AND status = 'active' AND maturity_epoch > ? "
            "ORDER BY maturity_epoch, holding_id",
            (str(seller_id), str(product_code), int(now)),
        ).fetchall()
        units_left = int(quantity)
        transferred_basis = 0
        for row in rows:
            face_value = max(1, int(row["face_value"] or 1))
            principal = int(row["principal"])
            if principal % face_value:
                raise RuntimeError("국채 액면 잔액이 매수 단위와 일치하지 않습니다.")
            old_units = principal // face_value
            take_units = min(units_left, old_units)
            if take_units <= 0:
                continue
            moved_principal = take_units * face_value
            old_basis = int(row["acquisition_cost"] or principal)
            moved_basis = (
                old_basis if take_units == old_units
                else round_money(old_basis * take_units / old_units)
            )
            transferred_basis += moved_basis
            if take_units == old_units:
                cursor = conn.execute(
                    "DELETE FROM accounts.treasury_bond_holdings "
                    "WHERE holding_id = ? AND status = 'active'",
                    (row["holding_id"],),
                )
            else:
                cursor = conn.execute(
                    "UPDATE accounts.treasury_bond_holdings "
                    "SET principal = principal - ?, acquisition_cost = acquisition_cost - ? "
                    "WHERE holding_id = ? AND status = 'active' AND principal = ?",
                    (moved_principal, moved_basis, row["holding_id"], principal),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("국채 보유량이 바뀌어 거래를 취소했습니다.")
            conn.execute(
                "INSERT INTO accounts.treasury_bond_holdings "
                "(user_id, product_code, product_name, annual_rate, maturity_days, "
                "principal, face_value, acquisition_cost, purchased_epoch, maturity_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(buyer_id), str(product_code), row["product_name"],
                 float(row["annual_rate"]), int(row["maturity_days"]), moved_principal,
                 face_value, int(price) * take_units, int(row["purchased_epoch"]),
                 int(row["maturity_epoch"])),
            )
            units_left -= take_units
            if units_left == 0:
                break
        if units_left:
            raise RuntimeError("매도 주문의 예약 수량과 실제 국채 보유량이 달라 체결을 취소했습니다.")
        return transferred_basis
