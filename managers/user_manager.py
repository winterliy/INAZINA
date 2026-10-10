import random
import math
import time
from datetime import datetime, timezone
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.tax_calc import calculate_income_tax
from functions.money import parse_money, round_money
from managers.treasury_bond_manager import TreasuryBondManager


class UserManager:
    """사용자의 매매·송금·도박·자산 조회를 담당합니다."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.bond_manager = TreasuryBondManager(db)

    def _settle_user_interest(self, user_id):
        """Settle only when this user actually has an interest-bearing product."""
        user_id = str(user_id)
        has_savings = self.db.accounts.execute_one(
            "SELECT 1 FROM accounts WHERE user_id = ? AND account_type = 'saving' LIMIT 1",
            (user_id,),
        )
        has_loans = self.db.accounts.execute_one(
            "SELECT 1 FROM loans WHERE user_id = ? AND status = 'active' LIMIT 1",
            (user_id,),
        )
        if not has_savings and not has_loans:
            return None
        try:
            from managers.interest_manager import InterestManager

            InterestManager(self.db).process_user_interests(user_id)
            return None
        except Exception as exc:
            return f"❌ 이자 정산을 완료하지 못해 거래를 진행하지 않았습니다: {exc}"

    @staticmethod
    def _asset_row(conn, code):
        code_locations = DatabaseManager.code_locations(conn, code)
        if len(code_locations) > 1:
            raise ValueError(f"코드 `{code}`가 중복 등록되어 있습니다: {', '.join(code_locations)}. SQL Browser에서 중복을 정리해주세요.")
        matches = []
        row = conn.execute(
            "SELECT code, name, exchange_id, price, type, status, is_fixed, total_supply AS supply "
            ", quote_currency FROM assets.market_assets WHERE code = ?", (code,)
        ).fetchone()
        if row:
            matches.append((dict(row), "market_assets"))
        row = conn.execute(
            "SELECT company_code AS code, name, exchange_id, share_price AS price, 'stock' AS type, "
            "status, is_fixed, total_shares AS supply, quote_currency FROM company.company WHERE company_code = ?", (code,)
        ).fetchone()
        if row:
            matches.append((dict(row), "company"))
        row = conn.execute(
            "SELECT product_code AS code, name, exchange_id, unit_price AS price, product_type AS type, "
            "status, is_fixed, total_supply AS supply, maturity_days, created_at, quote_currency "
            "FROM product.investment WHERE product_code = ?", (code,)
        ).fetchone()
        if row:
            matches.append((dict(row), "investment"))
        row = conn.execute(
            "SELECT product_code AS code, name, exchange_id, market_price AS price, "
            "'bond' AS type, status, 1 AS is_fixed, NULL AS supply, face_value, "
            "annual_rate, maturity_days, total_supply, sold_amount "
            "FROM product.treasury_bond_products WHERE product_code = ?",
            (code,),
        ).fetchone()
        if row:
            matches.append((dict(row), "treasury_bond"))
        if len(matches) > 1:
            raise ValueError(f"자산 코드 `{code}`가 여러 테이블에 중복 등록되어 있습니다.")
        if matches:
            asset = matches[0][0]
            price = float(asset["price"] or 0)
            asset["price"] = max(1, round_money(price)) if price > 0 else 0
        if matches and matches[0][1] == "investment":
            asset = matches[0][0]
            try:
                days = float(asset["maturity_days"] or 0)
                created = datetime.strptime(str(asset["created_at"]), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                if asset['type']!='els' and asset["status"] == "active" and days > 0 and datetime.now(timezone.utc).timestamp() >= created + days * 86400:
                    asset["status"] = "matured"
            except (TypeError, ValueError, OverflowError):
                pass
        return matches[0] if matches else (None, None)

    @staticmethod
    def _choose_funds(conn, user_id, exclude_account=None, exclude_wallet=False):
        user = conn.execute(
            f"SELECT cash FROM users.users WHERE user_id = ? AND (status = 'active' OR user_id='{TREASURY_USER_ID}')", (user_id,)
        ).fetchone()
        if not user:
            return None
        sources = [] if exclude_wallet else [(int(round(user["cash"] or 0)), None)]
        sources.extend(
            (int(round(row["balance"] or 0)), row["account_number"])
            for row in conn.execute(
                "SELECT account_number, balance FROM accounts.accounts WHERE user_id = ? "
                "AND (? IS NULL OR account_number <> ?)",
                (user_id, exclude_account, exclude_account),
            ).fetchall()
        )
        return max(sources, key=lambda source: source[0]) if sources else None

    @staticmethod
    def _change_cash(conn, user_id, amount, *, maximum=None):
        """Apply a cash delta, optionally enforcing an upper balance limit."""
        amount = int(amount)
        if amount < 0:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ? AND cash >= ?",
                (amount, str(user_id), -amount),
            )
        elif maximum is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                (amount, str(user_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ? AND cash <= ?",
                (amount, str(user_id), int(maximum) - amount),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _debit_source(conn, user_id, account_number, amount):
        if account_number is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash - ? "
                f"WHERE user_id = ? AND (status = 'active' OR user_id='{TREASURY_USER_ID}') AND cash >= ?",
                (amount, user_id, amount),
            )
        else:
            now = time.time()
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance = balance - ?, "
                "principal = MAX(0, COALESCE(principal, 0) - ?), "
                "last_payment_epoch = ?, last_payment = ? "
                "WHERE account_number = ? AND user_id = ? AND balance >= ?",
                (amount, amount, now, stamp, account_number, user_id, amount),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _credit_source(conn, user_id, account_number, amount):
        if amount <= 0:
            return
        if account_number is None:
            conn.execute("UPDATE users.users SET cash = cash + ? WHERE user_id = ?", (amount, user_id))
        else:
            now = time.time()
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE accounts.accounts SET balance = balance + ?, "
                "principal = COALESCE(principal, 0) + ?, last_payment_epoch = ?, "
                "last_payment = ? WHERE account_number = ? AND user_id = ?",
                (amount, amount, now, stamp, account_number, user_id),
            )

    @staticmethod
    def _tax(conn, user_id, source, gross_amount):
        """양수 소득에 SQL tax.tax_settings의 세율을 적용하고 내역을 기록합니다."""
        gross = max(0, round_money(gross_amount))
        if gross == 0:
            return 0
        row = conn.execute("SELECT rate_percent FROM tax.tax_settings WHERE id = 1").fetchone()
        try:
            rate = float(row["rate_percent"] if row else 3.0)
        except (TypeError, ValueError):
            rate = 3.0
        if not 0 <= rate <= 100:
            rate = 3.0
        tax_amount = calculate_income_tax(gross, rate / 100.0)
        net_income = max(0, gross - tax_amount)
        if tax_amount > 0:
            treasury = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                (tax_amount, TREASURY_USER_ID),
            )
            if treasury.rowcount != 1:
                raise RuntimeError("국고 계정을 찾을 수 없어 세금 처리를 취소했습니다.")
        return int(tax_amount)

    @staticmethod
    def _refresh_max_shareholder(conn, asset_code):
        conn.execute(
            """UPDATE company.company SET max_shareholder_id = (
                   SELECT user_id FROM (
                       SELECT user_id, quantity FROM assets.user_assets WHERE asset_code = ?
                       UNION ALL
                       SELECT writer_id AS user_id, quantity
                       FROM assets.option_collateral_lots WHERE underlying_code = ?
                   )
                   GROUP BY user_id
                   ORDER BY SUM(quantity) DESC, user_id ASC
                   LIMIT 1
               ) WHERE company_code = ?""",
            (asset_code, asset_code, asset_code),
        )

    def buy_asset(self, user_id, asset_code, quantity):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 매수 수량은 0보다 큰 정수로 입력해주세요."
        product=self.db.products.get_investment(asset_code)
        if product and product['product_type']=='els':
            from managers.els_manager import ELSManager
            return ELSManager(self.db,self).subscribe(user_id,asset_code,quantity)
        interest_error = self._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                from managers.market_session_manager import MarketSessionManager
                error = MarketSessionManager(self.db).trading_error(conn=conn)
                if error:
                    return False, error
                asset, source = self._asset_row(conn, asset_code)
                if not asset:
                    return False, "❌ 존재하지 않는 자산 코드입니다."
                if asset.get("quote_currency", "NWX") != "NWX":
                    return False, "❌ 외화 종목은 해당 외환통장을 개설하고 거래소 지정가 매수·매도로 거래해주세요."
                if asset["type"] == "stock_future":
                    return False, "❌ 주식 선물은 거래소의 지정가 매수·매도로 거래해주세요."
                if asset["status"] != "active":
                    return False, "❌ 현재 거래가 중지된 자산입니다."
                price = int(round(asset["price"] or 0))
                if price <= 0:
                    return False, "❌ 자산 가격이 올바르게 등록되지 않았습니다."
                supply = asset["supply"]
                if supply is not None:
                    if int(supply) < 0:
                        return False, "❌ 자산의 발행 수량이 올바르게 설정되지 않았습니다."
                    issued = conn.execute(
                        "SELECT (SELECT COALESCE(SUM(quantity), 0) FROM assets.user_assets "
                        "WHERE asset_code = ?) + (SELECT COALESCE(SUM(quantity), 0) "
                        "FROM assets.option_collateral_lots WHERE underlying_code = ?) AS quantity",
                        (asset_code, asset_code),
                    ).fetchone()["quantity"]
                    if int(issued) + quantity > int(supply):
                        return False, f"❌ 매수 가능 수량을 초과했습니다. (잔여 {max(0, int(supply)-int(issued)):,}주)"
                total_cost = price * quantity
                funds = self._choose_funds(conn, user_id)
                if funds is None:
                    return False, "❌ 등록된 사용자만 거래할 수 있습니다."
                balance, account_number = funds
                if balance < total_cost or not self._debit_source(conn, user_id, account_number, total_cost):
                    return False, f"❌ 잔액이 부족합니다. (필요 금액: {total_cost:,} NWX)"
                conn.execute(
                    """INSERT INTO assets.user_assets
                       (user_id, asset_code, account_number, quantity, average_cost)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, asset_code, IFNULL(account_number, '')) DO UPDATE SET
                           average_cost = ((quantity * average_cost) +
                               (excluded.quantity * excluded.average_cost)) /
                               (quantity + excluded.quantity),
                           quantity = quantity + excluded.quantity""",
                    (user_id, asset_code, account_number, quantity, price),
                )
                if asset["type"] in {"mutual_fund", "index_fund", "corporate_bond"}:
                    product = conn.execute("SELECT issuer_code FROM product.investment WHERE product_code = ?", (asset_code,)).fetchone()
                    if asset["type"] == "corporate_bond":
                        conn.execute("UPDATE company.company SET capital = capital + ? WHERE company_code = ?", (total_cost, product["issuer_code"]))
                    else:
                        conn.execute("UPDATE users.users SET cash = cash + ? WHERE user_id = ?", (total_cost, TREASURY_USER_ID))
                if source == "company":
                    self._refresh_max_shareholder(conn, asset_code)
                unit = "주" if str(asset["type"]).lower() in {"stock", "주식"} else "좌" if asset["type"] in {"mutual_fund", "index_fund"} else "개"
            return True, f"✅ {asset['name']} {quantity:,}{unit}를 {total_cost:,} NWX에 매수했습니다."
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 매수 처리 중 오류가 발생했습니다: {exc}"

    def sell_asset(self, user_id, asset_code, quantity):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 매도 수량은 0보다 큰 정수로 입력해주세요."
        product=self.db.products.get_investment(asset_code)
        if product and product['product_type']=='els':
            return False,'❌ ELS는 중도 매도·환매가 불가능하며 계약 조건에 따라 자동 상환됩니다.'
        interest_error = self._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                from managers.market_session_manager import MarketSessionManager
                error = MarketSessionManager(self.db).trading_error(conn=conn)
                if error:
                    return False, error
                asset, source = self._asset_row(conn, asset_code)
                if not asset:
                    return False, "❌ 존재하지 않는 자산 코드입니다."
                if asset.get("quote_currency", "NWX") != "NWX":
                    return False, "❌ 외화 종목은 거래소 지정가 매수·매도로 거래해주세요."
                if asset["status"] not in ("active", "matured"):
                    return False, "❌ 현재 거래가 중지된 자산입니다."
                if asset["type"] in {"stock_future", "corporate_bond"}:
                    return False, "❌ 선물·회사채는 거래소 지정가 매수·매도로 거래해주세요."
                price = int(round(asset["price"] or 0))
                positions = conn.execute(
                    "SELECT holding_id, account_number, quantity, average_cost "
                    "FROM assets.user_assets WHERE user_id = ? AND asset_code = ? "
                    "ORDER BY holding_id", (user_id, asset_code)
                ).fetchall()
                reserved = conn.execute(
                    "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                    "FROM assets.market_orders WHERE user_id = ? AND asset_code = ? "
                    "AND side = 'sell' AND status IN ('open', 'partial')",
                    (str(user_id), asset_code),
                ).fetchone()
                available = (
                    sum(int(row["quantity"]) for row in positions)
                    - int(reserved["quantity"] or 0)
                )
                if available < quantity:
                    return False, f"❌ 보유 수량이 부족합니다. (보유 {available:,}개)"

                left = quantity
                sold = []
                taxable_gain = 0
                for row in positions:
                    take = min(left, int(row["quantity"]))
                    if take <= 0:
                        continue
                    taxable_gain += max(0, price - int(round(row["average_cost"] or 0))) * take
                    sold.append((row["holding_id"], row["account_number"], take, int(row["quantity"])))
                    left -= take
                    if left == 0:
                        break

                gross_proceeds = price * quantity
                if asset["type"] in {"mutual_fund", "index_fund"}:
                    conn.execute("UPDATE users.users SET cash = cash - ? WHERE user_id = ?", (gross_proceeds, TREASURY_USER_ID))
                total_tax = self._tax(conn, user_id, "stock_sale_gain", taxable_gain)
                tax_left = total_tax
                qty_left = quantity
                for holding_id, account_number, take, old_quantity in sold:
                    share_tax = tax_left if take == qty_left else round_money(total_tax * take / quantity)
                    net_proceeds = max(0, price * take - share_tax)
                    if take == old_quantity:
                        cursor = conn.execute(
                            "DELETE FROM assets.user_assets WHERE holding_id = ? AND quantity = ?",
                            (holding_id, old_quantity),
                        )
                    else:
                        cursor = conn.execute(
                            "UPDATE assets.user_assets SET quantity = quantity - ? "
                            "WHERE holding_id = ? AND quantity > ?",
                            (take, holding_id, take),
                        )
                    if cursor.rowcount != 1:
                        raise RuntimeError("보유 수량이 변경되어 매도를 취소했습니다.")
                    self._credit_source(conn, user_id, account_number, net_proceeds)
                    tax_left -= share_tax
                    qty_left -= take
                if source == "company":
                    self._refresh_max_shareholder(conn, asset_code)
            net_proceeds = max(0, gross_proceeds - total_tax)
            unit = "주" if str(asset["type"]).lower() in {"stock", "주식"} else "좌" if asset["type"] in {"mutual_fund", "index_fund"} else "개"
            return True, (
                f"✅ {asset['name']} {quantity:,}{unit}를 {gross_proceeds:,} NWX에 매도했습니다.\n"
                f"실현이익 과세: {total_tax:,} NWX · 계좌 입금: {net_proceeds:,} NWX"
            )
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 매도 처리 중 오류가 발생했습니다: {exc}"

    def transfer(self, from_user_id, recipient, amount, source_account=None):
        if self.db.fx.execute_one("SELECT 1 FROM fx_accounts WHERE account_number IN (?,?) LIMIT 1",(str(recipient).strip(),str(source_account or '').strip())):
            try:
                from managers.fx_manager import FXManager
                return True,FXManager(self.db).transfer(from_user_id,recipient,amount,source_account)
            except ValueError as exc:
                return False,f'❌ {exc}'
        try:
            amount = parse_money(amount, minimum=1)
        except ValueError as exc:
            return False, f"❌ 송금 금액은 정수 NWX로 입력해주세요. ({exc})"
        interest_error = self._settle_user_interest(from_user_id)
        if interest_error:
            return False, interest_error

        sender_id = str(from_user_id)
        recipient = str(recipient).strip()
        if recipient.casefold() in {"국고", "treasury", TREASURY_USER_ID.casefold()}:
            recipient = TREASURY_USER_ID
        if not recipient:
            return False, "❌ 받는 사람의 사용자 ID 또는 계좌번호를 입력해주세요."
        destination_hint = self.db.accounts.execute_one(
            "SELECT user_id FROM accounts WHERE account_number = ? AND account_type = 'saving'",
            (recipient,),
        )
        if destination_hint and str(destination_hint["user_id"]) != sender_id:
            interest_error = self._settle_user_interest(destination_hint["user_id"])
            if interest_error:
                return False, interest_error
        if source_account is not None:
            source_account = str(source_account).strip()
        explicit_source = source_account is not None
        explicit_wallet = source_account == "__wallet__"
        if explicit_wallet:
            source_account = None

        try:
            with self.db.atomic(("users", "accounts")) as conn:
                if recipient == TREASURY_USER_ID:
                    recipient_user = conn.execute(
                        "SELECT user_id FROM users.users WHERE user_id = ? AND status = 'nation'",
                        (TREASURY_USER_ID,),
                    ).fetchone()
                else:
                    recipient_user = conn.execute(
                        "SELECT user_id FROM users.users WHERE user_id = ? AND status = 'active'",
                        (recipient,),
                    ).fetchone()
                recipient_account = None
                if recipient_user:
                    target_user_id = str(recipient_user["user_id"])
                else:
                    recipient_account = conn.execute(
                        "SELECT a.account_number, a.user_id FROM accounts.accounts AS a "
                        "JOIN users.users AS u ON u.user_id = a.user_id "
                        "WHERE a.account_number = ? AND a.account_type = 'saving' "
                        "AND u.status = 'active'",
                        (recipient,),
                    ).fetchone()
                    if not recipient_account:
                        return False, "❌ 받는 사용자 ID 또는 활성 저축계좌 번호를 찾을 수 없습니다."
                    target_user_id = str(recipient_account["user_id"])

                target_account_number = (
                    str(recipient_account["account_number"]) if recipient_account else None
                )
                same_user = target_user_id == sender_id
                preferred_account = None if explicit_wallet else source_account
                if same_user and explicit_source and preferred_account == target_account_number:
                    return False, "❌ 같은 계좌로는 이체할 수 없습니다. 출금·입금 계좌를 다르게 선택해주세요."

                wallet = conn.execute(
                    f"SELECT cash FROM users.users WHERE user_id = ? AND (status = 'active' OR user_id='{TREASURY_USER_ID}')",
                    (sender_id,),
                ).fetchone()
                if not wallet:
                    return False, "❌ 송금자 계정을 찾을 수 없습니다."
                sources = [(None, max(0, int(round(wallet["cash"] or 0))))]
                sources.extend(
                    (str(row["account_number"]), max(0, int(round(row["balance"] or 0))))
                    for row in conn.execute(
                        "SELECT account_number, balance FROM accounts.accounts WHERE user_id = ?",
                        (sender_id,),
                    ).fetchall()
                )

                if same_user:
                    sources = [
                        source for source in sources
                        if source[0] != target_account_number
                        and not (target_account_number is None and source[0] is None)
                    ]

                if explicit_source:
                    preferred = next(
                        (source for source in sources if source[0] == preferred_account), None
                    )
                    if preferred is None:
                        return False, "❌ 출금 계좌를 찾을 수 없거나 본인 명의가 아닙니다."
                    fallback_sources = [
                        source for source in sources if source[0] != preferred_account
                    ]
                    # When a bank account is selected, consume the other
                    # accounts before falling back to the wallet.
                    fallback_sources.sort(
                        key=lambda source: (source[0] is None, -source[1])
                    )
                    source_candidates = [preferred] + fallback_sources
                else:
                    # Keep the previous largest-balance-first behavior, then
                    # draw from other owned accounts only if the first is short.
                    source_candidates = sorted(sources, key=lambda source: source[1], reverse=True)

                available_total = sum(balance for _account_number, balance in source_candidates)
                if available_total < amount:
                    return False, (
                        f"❌ 잔액이 부족합니다. 출금 가능한 지갑·통장 합계는 "
                        f"{available_total:,} NWX입니다."
                    )

                remaining = amount
                debited_sources = []
                for account_number, balance in source_candidates:
                    debit_amount = min(balance, remaining)
                    if debit_amount <= 0:
                        continue
                    if not self._debit_source(conn, sender_id, account_number, debit_amount):
                        raise RuntimeError("출금 잔액이 변경되어 송금을 취소했습니다.")
                    debited_sources.append((account_number, debit_amount))
                    remaining -= debit_amount
                    if remaining == 0:
                        break

                if target_account_number:
                    cursor = conn.execute(
                        "UPDATE accounts.accounts SET balance = balance + ?, "
                        "principal = COALESCE(principal, 0) + ?, "
                        "last_payment_epoch = unixepoch('now'), last_payment = CURRENT_TIMESTAMP "
                        "WHERE account_number = ? AND user_id = ? AND account_type = 'saving'",
                        (amount, amount, target_account_number, target_user_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("입금 대상 계좌가 변경되어 송금을 취소했습니다.")
                    destination = f"계좌 `{target_account_number}`"
                else:
                    if target_user_id == TREASURY_USER_ID:
                        cursor = conn.execute(
                            "UPDATE users.users SET cash = cash + ? "
                            "WHERE user_id = ? AND status = 'nation'",
                            (amount, TREASURY_USER_ID),
                        )
                        destination = "국고"
                    else:
                        cursor = conn.execute(
                            "UPDATE users.users SET cash = cash + ? "
                            "WHERE user_id = ? AND status = 'active'",
                            (amount, target_user_id),
                        )
                        destination = "지갑"
                    if cursor.rowcount != 1:
                        raise RuntimeError("입금 대상 사용자가 변경되어 송금을 취소했습니다.")

            source_label = " + ".join(
                "지갑" if account_number is None else f"계좌 `{account_number}`"
                for account_number, _debit_amount in debited_sources
            )
            return True, f"✅ {source_label}에서 {destination}(으)로 {amount:,} NWX 이체를 완료했습니다."
        except Exception as exc:
            return False, f"❌ 송금 처리 중 오류가 발생했습니다: {exc}"

    def gamble(self, user_id, amount):
        try:
            amount = parse_money(amount, minimum=1)
        except ValueError as exc:
            return False, f"❌ 도박 금액은 정수 NWX로 입력해주세요. ({exc})"
        interest_error = self._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            with self.db.atomic(("users", "accounts", "pot", "tax")) as conn:
                funds = self._choose_funds(conn, str(user_id))
                if not funds:
                    return False, "❌ 등록된 사용자만 이용할 수 있습니다."
                balance, account_number = funds
                if balance < amount:
                    return False, "❌ 잔액이 부족합니다."
                row = conn.execute("SELECT chance FROM pot.probability WHERE id = 1").fetchone()
                try:
                    chance = float(row["chance"] if row else 50)
                except (TypeError, ValueError):
                    chance = 50.0
                if not 0 <= chance <= 100:
                    chance = 50.0
                return_row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'gamble_return'"
                ).fetchone()
                try:
                    return_percent = float(return_row["value"] if return_row else 100)
                except (TypeError, ValueError):
                    return_percent = 100.0
                if not math.isfinite(return_percent) or return_percent < 0:
                    return_percent = 100.0
                payout_before_tax = amount * return_percent / 100.0
                if not math.isfinite(payout_before_tax):
                    return False, "❌ 설정된 당첨 지급률로 상금을 계산할 수 없습니다."
                payout_before_tax = round_money(payout_before_tax)
                won = random.random() * 100 < chance
                if won:
                    tax = self._tax(conn, str(user_id), "gambling_winnings", payout_before_tax)
                    self._credit_source(conn, str(user_id), account_number, payout_before_tax - tax)
                    treasury = conn.execute(
                        "UPDATE users.users SET cash = cash - ? WHERE user_id = ?",
                        (payout_before_tax, TREASURY_USER_ID),
                    )
                    if treasury.rowcount != 1:
                        raise RuntimeError("국고 계정을 찾을 수 없어 당첨금 지급을 취소했습니다.")
                else:
                    if not self._debit_source(conn, str(user_id), account_number, amount):
                        return False, "❌ 잔액이 부족합니다."
                    treasury = conn.execute(
                        "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                        (amount, TREASURY_USER_ID),
                    )
                    if treasury.rowcount != 1:
                        raise RuntimeError("국고 계정을 찾을 수 없어 도박 처리를 취소했습니다.")
                    tax = 0
            if won:
                net_payout = max(0, payout_before_tax - tax)
                return True, (
                    f"🎉 당첨! 지급률 {return_percent:,.2f}% · "
                    f"상금 {payout_before_tax:,} NWX · 세금 {tax:,} NWX · "
                    f"실수령 {net_payout:,} NWX (원금 별도, 국고 지급)"
                )
            return False, f"💀 꽝! {amount:,} NWX를 잃어 국고에 입금했습니다."
        except Exception as exc:
            return False, f"❌ 도박 처리 중 오류가 발생했습니다: {exc}"

    def get_total_assets(self, user_id, settle=True):
        """Return the asset-page breakdown; optionally avoid settling balances.

        User-facing asset pages keep the default settlement behavior. Notification-
        only workflows can pass ``settle=False`` to take a read-only snapshot.
        """
        if settle:
            interest_error = self._settle_user_interest(user_id)
            if interest_error:
                raise RuntimeError(interest_error)
            self.bond_manager.redeem_matured(user_id=user_id)
        user = self.db.users.get_user(user_id)
        cash = int(round(user["cash"] or 0)) if user else 0
        nationality = str(user['nationality'] or '미설정') if user else '미설정'
        # One wallet can reference the same stock through holdings, covered-call
        # collateral and written options. Resolve it once for this snapshot.
        asset_cache = {}
        def wallet_asset(code):
            code = str(code)
            if code not in asset_cache:
                try:
                    asset_cache[code] = self._asset_from_tables(code)
                except ValueError:
                    asset_cache[code] = None
            return asset_cache[code]
        base_rate = float(self.db.system.get_setting("base_rate", "3")) / 100
        last_trades = {str(row['asset_code']):int(row['price']) for row in
            self.db.assets.execute('SELECT asset_code,price FROM market_last_trade')}
        quote_overrides = {str(row['asset_code']):int(row['price']) for row in
            self.db.assets.execute('SELECT asset_code,price FROM market_quote_overrides')}
        fx_rates = {'NWX':1}
        for row in self.db.fx.execute("SELECT c.code,a.price FROM currencies c LEFT JOIN market_assets a "
                                      "ON a.code=c.asset_code WHERE c.status='active'"):
            if row['code']=='NWX':
                continue
            if row['price'] is not None and float(row['price'])>0:
                fx_rates[str(row['code'])]=int(round(float(row['price'])))
        currency_row = self.db.fx.execute_one("SELECT n.currency_code FROM country_currencies n "
            "JOIN currencies c ON c.code=n.currency_code WHERE n.nationality=? AND c.status='active'",(nationality,))
        national_currency = currency_row['currency_code'] if currency_row else 'NWX'
        account_details = []
        total_account_balance = 0
        for account in self.db.accounts.get_user_accounts(user_id):
            balance = int(round(account["balance"] or 0))
            total_account_balance += balance
            product = self.db.products.get_saving(account["product_code"])
            if not product and account["account_type"] == "loan_payout":
                product = self.db.products.get_liability(account["product_code"])
            account_details.append({
                "number": account["account_number"],
                "product_name": (f"대출금 지급 · {product['name']}" if product and account["account_type"] == "loan_payout"
                                 else product["name"] if product else account["product_code"]),
                "balance": balance,
            })

        stock_details = []
        total_stock_value = 0
        sell_reservations = {
            str(row["asset_code"]): int(row["quantity"] or 0)
            for row in self.db.assets.execute(
                "SELECT asset_code, SUM(remaining_quantity) AS quantity "
                "FROM market_orders WHERE order_kind = 'asset' AND user_id = ? AND side = 'sell' "
                "AND status IN ('open', 'partial') GROUP BY asset_code",
                (str(user_id),),
            )
        }
        for position in self.db.assets.get_user_portfolio(user_id):
            code, quantity = position["asset_code"], int(position["quantity"])
            asset = wallet_asset(code)
            if asset and asset.get('type')=='els':
                continue
            price = int(round(asset["price"] or 0)) if asset else 0
            currency = asset.get('quote_currency','NWX') if asset else 'NWX'
            rate = fx_rates.get(currency,1)
            value = price * quantity * rate
            total_stock_value += value
            stock_details.append({
                "name": asset["name"] if asset else "확인 필요",
                "code": code, "qty": quantity, "price": price, "value": value,
                "type": asset.get("type") if asset else None,
                "quote_currency":currency,
                "sell_reserved_qty": min(quantity, sell_reservations.get(str(code), 0)),
            })

        # Long option positions are marked at the latest premium trade, or at
        # their Black-Scholes reference premium before the first trade.
        from managers.option_manager import OptionManager

        option_rows = self.db.assets.execute(
            "SELECT p.contract_id, p.quantity, p.average_premium, c.contract_code, "
            "c.option_type, c.underlying_code, c.strike_price, c.expiration_epoch, "
            "c.implied_volatility FROM option_positions p JOIN option_contracts c "
            "ON c.contract_id = p.contract_id WHERE p.user_id = ? AND c.status = 'active'",
            (str(user_id),),
        )
        option_sell_reservations = {
            int(row["contract_id"]): int(row["quantity"] or 0)
            for row in self.db.assets.execute(
                "SELECT c.contract_id, SUM(o.remaining_quantity) AS quantity "
                "FROM option_contracts c JOIN market_orders o ON o.asset_code = c.contract_code "
                "WHERE o.order_kind = 'option' AND o.option_order_type = 'resale' "
                "AND o.user_id = ? AND o.side = 'sell' AND o.status IN ('open', 'partial') "
                "GROUP BY c.contract_id",
                (str(user_id),),
            )
        }
        now = int(time.time())
        for position in option_rows:
            underlying = wallet_asset(position["underlying_code"])
            spot = int(underlying["price"] or 0) if underlying else 0
            years = max(0, int(position["expiration_epoch"]) - now) / (365.0 * 86400)
            theoretical = OptionManager.theoretical_price(
                spot, int(position["strike_price"]), years,
                float(position["implied_volatility"]), position["option_type"],
                base_rate,
            )
            contract_code=str(position["contract_code"])
            price = quote_overrides.get(contract_code,last_trades.get(contract_code,theoretical))
            quantity = int(position["quantity"])
            value = price * quantity
            total_stock_value += value
            stock_details.append({
                "name": (
                    f"{underlying['name'] if underlying else position['underlying_code']} "
                    f"{'콜' if position['option_type'] == 'call' else '풋'} "
                    f"{int(position['strike_price']):,}"
                ),
                "code": str(position["contract_code"]), "qty": quantity,
                "price": price, "value": value, "type": "option",
                "sell_reserved_qty": min(
                    quantity, option_sell_reservations.get(int(position["contract_id"]), 0)
                ),
            })

        collateral_cash_row = self.db.assets.execute_one(
            "SELECT COALESCE(SUM(l.amount), 0) AS amount FROM option_collateral_cash_lots l "
            "JOIN option_contracts c ON c.contract_id = l.contract_id "
            "WHERE l.writer_id = ? AND c.status = 'active'",
            (str(user_id),),
        )
        option_collateral_cash = int(collateral_cash_row["amount"] or 0)

        call_collateral = self.db.assets.execute(
            "SELECT c.contract_code, c.underlying_code, SUM(l.quantity) AS quantity "
            "FROM option_collateral_lots l JOIN option_contracts c "
            "ON c.contract_id = l.contract_id WHERE l.writer_id = ? AND c.status = 'active' "
            "GROUP BY c.contract_id",
            (str(user_id),),
        )
        for collateral in call_collateral:
            underlying = wallet_asset(collateral["underlying_code"])
            spot = int(underlying["price"] or 0) if underlying else 0
            quantity = int(collateral["quantity"] or 0)
            value = spot * quantity
            total_stock_value += value
            stock_details.append({
                "name": f"{underlying['name'] if underlying else collateral['underlying_code']} 콜 담보",
                "code": collateral['underlying_code'], "qty": quantity,
                "is_collateral":True,"collateral_contract_code":collateral['contract_code'],
                "price": spot, "value": value, "type": "stock",
                "sell_reserved_qty": 0,
            })

        option_short_liability = 0
        written_rows = self.db.assets.execute(
            "SELECT contract_code, option_type, underlying_code, strike_price, "
            "expiration_epoch, implied_volatility, issued_contracts, outstanding_contracts, "
            "COALESCE((SELECT SUM(remaining_quantity) FROM market_orders o WHERE o.order_kind='option' "
            "AND o.option_order_type='write' AND o.asset_code=option_contracts.contract_code "
            "AND o.status IN ('open','partial')),0) offered, "
            "COALESCE((SELECT SUM(quantity) FROM option_collateral_lots l "
            "WHERE l.contract_id=option_contracts.contract_id),0) collateral_quantity, "
            "COALESCE((SELECT SUM(amount) FROM option_collateral_cash_lots l "
            "WHERE l.contract_id=option_contracts.contract_id),0) collateral_cash "
            "FROM option_contracts WHERE writer_id = ? AND status = 'active' "
            "AND (outstanding_contracts > 0 OR issued_contracts > 0)",
            (str(user_id),),
        )
        written_options=[]
        for written in written_rows:
            underlying = wallet_asset(written["underlying_code"])
            spot = int(underlying["price"] or 0) if underlying else 0
            years = max(0, int(written["expiration_epoch"]) - now) / (365.0 * 86400)
            premium = OptionManager.theoretical_price(
                spot, int(written["strike_price"]), years,
                float(written["implied_volatility"]), written["option_type"],
                base_rate,
            )
            contract_code=str(written['contract_code'])
            mark=quote_overrides.get(contract_code,last_trades.get(contract_code,premium))
            option_short_liability += mark * int(written["outstanding_contracts"])
            written_options.append({**dict(written),
                'name':f"{underlying['name'] if underlying else written['underlying_code']} "
                       f"{'콜' if written['option_type']=='call' else '풋'} {int(written['strike_price']):,}",
                'price':mark,'theoretical_price':premium,'position_type':'writer'})

        reserved_rows = self.db.assets.execute(
            "SELECT quote_currency,COALESCE(SUM(reserved_cash), 0) AS total FROM market_orders "
            "WHERE user_id = ? AND (side = 'buy' OR order_kind = 'future') AND status IN ('open', 'partial') GROUP BY quote_currency",
            (str(user_id),),
        )
        order_reserved_total = 0
        for reserved in reserved_rows:
            order_reserved_total += int(reserved['total'] or 0)*fx_rates.get(reserved['quote_currency'] or 'NWX',1)

        futures_collateral = 0
        futures_unrealized = 0
        for position in self.db.assets.execute("SELECT * FROM futures_positions WHERE user_id = ?", (str(user_id),)):
            product = self.db.products.get_investment(position["product_code"])
            if not product:
                continue
            price = int(product["unit_price"])
            quantity = int(position["quantity"])
            collateral = int(position["collateral"])
            unrealized = quantity * int(product["contract_size"]) * (price - int(position["settlement_price"]))
            equity = max(0, collateral + unrealized)
            futures_collateral += collateral
            futures_unrealized += max(-collateral, unrealized)
            total_stock_value += equity
            stock_details.append({"name": f"{product['name']} {'롱' if quantity > 0 else '숏'}",
                "code": position["product_code"], "qty": quantity, "price": price, "value": equity,
                "type": "stock_future", "sell_reserved_qty": 0})

        bond_details = self.bond_manager.get_user_holdings(user_id)
        bond_face_total = sum(item["principal"] for item in bond_details)
        bond_total = sum(item["market_value"] for item in bond_details)

        loan_row = self.db.accounts.execute_one(
            "SELECT COALESCE(SUM(outstanding_balance), 0) AS total FROM loans "
            "WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        loan_total = int(round(loan_row["total"] or 0)) if loan_row else 0
        fx_total = 0
        fx_accounts = []
        fx_rows = self.db.fx.execute("SELECT currency_code,balance_units FROM fx_accounts WHERE user_id=?",(str(user_id),))
        if fx_rows:
            from managers.fx_manager import FXManager
            if settle:
                fx_accounts = FXManager(self.db).accounts(user_id)
            else:
                with self.db.atomic() as conn:
                    fx_accounts = [{**dict(r),'value_nwx':int(r['balance_units'])*FXManager._rate(conn,r['currency_code'])//1_000_000 if r['balance_units'] else 0}
                        for r in conn.execute("SELECT a.*,p.name AS product_name,p.annual_rate FROM fx_accounts a "
                            "LEFT JOIN fx_products p ON p.product_code=a.product_code WHERE a.user_id=? ORDER BY a.currency_code",(str(user_id),))]
            fx_total = sum(r['value_nwx'] for r in fx_accounts)
        els_positions=[]
        if getattr(self.db,'els',None) is not None:
            from managers.els_manager import ELSManager
            els_positions=ELSManager(self.db,self).get_user_positions(user_id)
        els_total=sum(int(item.get('principal',0)) for item in els_positions)
        preferred_claims=[]
        if getattr(self.db,'corporate',None) is not None:
            preferred_claims=[{**dict(row),'remaining_amount':int(row['gross_amount'])-int(row['paid_amount'])}
                for row in self.db.corporate.execute("SELECT * FROM preferred_dividend_claims WHERE user_id=? AND status IN ('unpaid','partial') ORDER BY due_epoch,claim_id",(str(user_id),))]
        preferred_claim_total=sum(int(row['remaining_amount'])*fx_rates.get(row['quote_currency'],1) for row in preferred_claims)
        grand_total = (
            cash + total_account_balance + total_stock_value + order_reserved_total
            + bond_total + option_collateral_cash - option_short_liability + fx_total+els_total+preferred_claim_total
        )

        return {
            "nationality":nationality,
            "national_currency":national_currency,
            "fx_accounts":fx_accounts,
            "written_options":written_options,
            "fx_total":fx_total,
            "els_positions":els_positions,
            "els_total":els_total,
            "preferred_dividend_claims":preferred_claims,
            "preferred_claim_total":preferred_claim_total,
            "cash": cash,
            "accounts": account_details,
            "account_total": total_account_balance,
            "stocks": stock_details,
            "stock_total": total_stock_value,
            "order_reserved_total": order_reserved_total,
            "bonds": bond_details,
            "bond_total": bond_total,
            "bond_face_total": bond_face_total,
            "option_collateral_cash": option_collateral_cash,
            "option_short_liability": option_short_liability,
            "futures_collateral": futures_collateral,
            "futures_unrealized": futures_unrealized,
            "grand_total": grand_total,
            "loan_total": loan_total,
            "net_worth": grand_total - loan_total,
        }

    def _asset_from_tables(self, code):
        matches = [
            (self.db.assets.get_asset_info(code), "market_assets"),
            (self.db.companies.get_company(code), "company"),
            (self.db.products.get_investment(code), "investment"),
        ]
        matches = [(row, source) for row, source in matches if row]
        locations = [
            (self.db.assets.execute_one("SELECT 1 FROM market_assets WHERE code=?", (code,)), "시장 자산"),
            (self.db.companies.execute_one("SELECT 1 FROM company WHERE company_code=?", (code,)), "회사 주식"),
            (self.db.products.execute_one("SELECT 1 FROM saving WHERE product_code=?", (code,)), "저축 상품"),
            (self.db.products.execute_one("SELECT 1 FROM liability WHERE product_code=?", (code,)), "대출 상품"),
            (self.db.products.execute_one("SELECT 1 FROM investment WHERE product_code=?", (code,)), "투자 상품"),
            (self.db.assets.execute_one("SELECT 1 FROM option_contracts WHERE contract_code=?", (code,)), "옵션 계약"),
            (self.db.accounts.execute_one("SELECT 1 FROM accounts WHERE account_number=?", (code,)), "계좌 번호"),
        ]
        if sum(1 for row, _label in locations if row) > 1 or len(matches) > 1:
            raise ValueError("duplicate code")
        if not matches:
            return None
        row, source = matches[0]
        if source == "company":
            return {
                "name": row["name"],
                "price": max(1, int(math.floor(float(row["share_price"] or 0) + 0.5)))
                if float(row["share_price"] or 0) > 0 else 0,
                "type": "stock",
                "quote_currency":row['quote_currency'],
            }
        if source == "investment":
            price = float(row["unit_price"] or 0)
            return {
                "name": row["name"],
                "price": max(1, int(math.floor(price + 0.5))) if price > 0 else 0,
                "type": row["product_type"],
                "quote_currency":row['quote_currency'],
            }
        asset_type = row["type"]
        price = float(row["price"] or 0)
        price = max(1, int(math.floor(price + 0.5))) if price > 0 else 0
        return {"name": row["name"], "price": price, "type": asset_type,"quote_currency":row['quote_currency']}
