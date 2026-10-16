"""Hybrid bonds, interest-rate swaps, wrap accounts and IMA settlement.

These products reuse existing engines:
- CB/BW are corporate bonds (coupons, redemption, pricing) with a conversion or
  warrant right that issues new shares of the issuer.
- Swaps exchange a fixed rate for the nationality's policy rate on a notional,
  with the citizen's nationality treasury as counterparty.
- Wrap accounts are savings accounts whose cash and holdings a named manager
  trades on the owner's behalf for an annual management fee.
- IMA accounts are principal-protected savings accounts that share a reference
  product's gain; the issuing bank's head office pays the excess.
"""
import time
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from managers.currency_manager import CurrencyManager, SCALE
from managers.user_manager import UserManager

YEAR = 365 * 86400
SWAP_MARGIN_PERCENT = Decimal(5)
MAX_MONEY = 2**53 - 1


def _available_units(conn, user_id, code):
    held = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?",
                        (user_id, code)).fetchone()[0]
    selling = conn.execute("SELECT COALESCE(SUM(remaining_quantity),0) FROM market_orders WHERE user_id=? "
                           "AND asset_code=? AND side='sell' AND status IN ('scheduled','open','partial')",
                           (user_id, code)).fetchone()[0]
    return int(held or 0) - int(selling or 0)


def _take_units(conn, user_id, code, quantity):
    left = quantity
    for lot in conn.execute("SELECT holding_id,quantity FROM user_assets WHERE user_id=? AND asset_code=? "
                            "ORDER BY account_number IS NOT NULL, holding_id", (user_id, code)).fetchall():
        take = min(left, int(lot["quantity"]))
        if take <= 0:
            continue
        if take == int(lot["quantity"]):
            conn.execute("DELETE FROM user_assets WHERE holding_id=?", (lot["holding_id"],))
        else:
            conn.execute("UPDATE user_assets SET quantity=quantity-? WHERE holding_id=?", (take, lot["holding_id"]))
        left -= take
        if not left:
            return
    raise ValueError("보유 수량이 부족합니다.")


def _issue_shares(conn, user_id, company, shares, unit_cost, capital):
    conn.execute(
        "INSERT INTO user_assets(user_id,asset_code,account_number,quantity,average_cost) VALUES (?,?,NULL,?,?) "
        "ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET "
        "average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),"
        "quantity=quantity+excluded.quantity",
        (user_id, company["company_code"], shares, unit_cost))
    changed = conn.execute(
        "UPDATE company SET total_shares=total_shares+?,capital=COALESCE(capital,0)+? "
        "WHERE company_code=? AND COALESCE(capital,0)<=?",
        (shares, capital, company["company_code"], MAX_MONEY - capital))
    if changed.rowcount != 1:
        raise ValueError("발행 법인의 자본금 한도를 초과합니다.")
    UserManager._refresh_max_shareholder(conn, company["company_code"])


class CapitalMarketsManager:
    def __init__(self, db, user_manager, order_book=None):
        self.db, self.users, self.order_book = db, user_manager, order_book

    # ------------------------------------------------------------------ CB / BW
    @staticmethod
    def _hybrid(conn, code, feature):
        bond = conn.execute("SELECT * FROM investment WHERE product_code=? AND product_type='corporate_bond'",
                            (str(code),)).fetchone()
        label = "전환사채" if feature == "convertible" else "신주인수권부사채"
        if not bond or (bond["bond_feature"] or "plain") != feature:
            raise ValueError(f"{label} 코드를 확인해주세요.")
        if bond["status"] != "active" or int(bond["maturity_epoch"] or 0) <= time.time():
            raise ValueError(f"만기 전 거래 중인 {label}만 권리를 행사할 수 있습니다.")
        company = conn.execute("SELECT * FROM company WHERE company_code=? AND status='active'",
                               (bond["issuer_code"],)).fetchone()
        if not company:
            raise ValueError("발행 회사가 거래 중이 아닙니다.")
        # Open the issuer's books before holdings change so the opening balance is right.
        from managers.financial_manager import FinancialManager
        FinancialManager.ensure(conn, company["company_code"])
        return bond, company

    def convert_bond(self, user_id, code, quantity):
        """CB: retire bonds and receive new shares at the conversion price."""
        user_id = str(user_id)
        try:
            quantity = int(quantity)
            if quantity <= 0:
                raise ValueError("전환할 사채 수량은 1 이상이어야 합니다.")
            with self.db.atomic() as conn:
                bond, company = self._hybrid(conn, code, "convertible")
                if _available_units(conn, user_id, bond["product_code"]) < quantity:
                    raise ValueError("매도 주문에 걸리지 않은 보유 사채가 부족합니다.")
                price = int(bond["conversion_price"])
                face_total = quantity * int(bond["face_value"])
                shares = face_total // price
                if shares <= 0:
                    raise ValueError(f"전환가격 {price:,}보다 큰 액면만큼 전환해주세요.")
                residual = face_total - shares * price
                if residual:
                    # Fractional shares are settled in cash from the issuer.
                    paid = conn.execute("UPDATE issuer_cash_accounts SET balance=balance-? "
                                        "WHERE company_code=? AND balance>=?",
                                        (residual, company["company_code"], residual))
                    if paid.rowcount != 1:
                        raise ValueError(f"단주 대금 {residual:,}을 지급할 법인 현금이 부족합니다. "
                                         f"액면 합계가 전환가격 {price:,}의 배수가 되도록 수량을 조정해주세요.")
                    UserManager._change_cash(conn, user_id, residual)
                _take_units(conn, user_id, bond["product_code"], quantity)
                conn.execute("UPDATE investment SET total_supply=MAX(0,total_supply-?) WHERE product_code=?",
                             (quantity, bond["product_code"]))
                _issue_shares(conn, user_id, company, shares, price, shares * price)
                self._book(conn, company["company_code"], "전환사채 주식 전환",
                           [("bonds", face_total), ("common_capital", -shares * price)]
                           + ([("cash", -residual)] if residual else []))
                conn.execute("INSERT INTO hybrid_bond_events(user_id,product_code,kind,bond_units,shares,cash,recorded_epoch) "
                             "VALUES (?,?,?,?,?,?,?)",
                             (user_id, bond["product_code"], "conversion", quantity, shares, residual, time.time()))
            return True, (f"✅ `{code}` {quantity:,}매를 `{company['company_code']}` {shares:,}주로 전환했습니다 "
                          f"(전환가 {price:,}" + (f" · 단주 대금 {residual:,} 입금" if residual else "") + ").")
        except ValueError as exc:
            return False, f"❌ {exc}"

    def exercise_warrant(self, user_id, code, quantity):
        """BW: each bond unit lets its holder buy face/exercise-price new shares once."""
        user_id = str(user_id)
        try:
            quantity = int(quantity)
            if quantity <= 0:
                raise ValueError("행사할 사채 수량은 1 이상이어야 합니다.")
            with self.db.atomic() as conn:
                bond, company = self._hybrid(conn, code, "warrant")
                used = conn.execute("SELECT COALESCE(SUM(bond_units),0) FROM hybrid_bond_events "
                                    "WHERE user_id=? AND product_code=? AND kind='warrant'",
                                    (user_id, bond["product_code"])).fetchone()[0]
                used_total = conn.execute("SELECT COALESCE(SUM(bond_units),0) FROM hybrid_bond_events "
                                          "WHERE product_code=? AND kind='warrant'", (bond["product_code"],)).fetchone()[0]
                held = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?",
                                    (user_id, bond["product_code"])).fetchone()[0]
                if int(held) - int(used) < quantity or int(used_total) + quantity > int(bond["total_supply"]):
                    raise ValueError(f"행사 가능한 신주인수권이 부족합니다. (보유 {int(held):,}매 · 행사 {int(used):,}매)")
                price = int(bond["warrant_exercise_price"])
                shares = quantity * int(bond["face_value"]) // price
                if shares <= 0:
                    raise ValueError("행사로 받을 신주가 없습니다. 수량을 늘려주세요.")
                cost = shares * price
                funds = UserManager._choose_funds(conn, user_id)
                if not funds or funds[0] < cost or not UserManager._debit_source(conn, user_id, funds[1], cost):
                    raise ValueError(f"행사 대금 {cost:,}이 부족합니다.")
                conn.execute("INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)", (company["company_code"],))
                conn.execute("UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=?",
                             (cost, company["company_code"]))
                _issue_shares(conn, user_id, company, shares, price, cost)
                self._book(conn, company["company_code"], "신주인수권 행사", [("cash", cost), ("common_capital", -cost)])
                conn.execute("INSERT INTO hybrid_bond_events(user_id,product_code,kind,bond_units,shares,cash,recorded_epoch) "
                             "VALUES (?,?,?,?,?,?,?)",
                             (user_id, bond["product_code"], "warrant", quantity, shares, cost, time.time()))
            return True, (f"✅ `{code}` 신주인수권 {quantity:,}매를 행사해 `{company['company_code']}` "
                          f"{shares:,}주를 받았습니다 (행사가 {price:,} · 납입 {cost:,}).")
        except ValueError as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def _book(conn, issuer, description, lines):
        from managers.financial_manager import FinancialManager
        FinancialManager.event(conn, issuer, description, lines, "financing")

    # -------------------------------------------------------------------- Swaps
    @staticmethod
    def _floating(conn, nationality, epoch=None):
        from managers.rate_manager import RateManager
        return RateManager.market_rate(conn, nationality, "policy", 0, epoch=epoch)

    @staticmethod
    def _fixed(conn, nationality, days):
        from managers.rate_manager import RateManager
        try:
            return RateManager.market_rate(conn, nationality, "treasury", days)
        except ValueError:
            return RateManager.market_rate(conn, nationality, "policy", 0)

    def open_swap(self, user_id, notional, side, tenor_days):
        """Enter a fixed-for-floating swap; 5% of notional is posted as margin."""
        user_id = str(user_id)
        try:
            side = {"고정지급": "pay_fixed", "pay_fixed": "pay_fixed",
                    "고정수취": "receive_fixed", "receive_fixed": "receive_fixed"}.get(str(side).strip())
            if not side:
                raise ValueError("방향은 고정지급(pay_fixed) 또는 고정수취(receive_fixed)입니다.")
            notional, tenor_days = int(notional), int(tenor_days)
            if not 1 <= notional <= MAX_MONEY or not 1 <= tenor_days <= 3650:
                raise ValueError("명목금액과 만기일수(1~3650일)를 확인해주세요.")
            margin = int((Decimal(notional) * SWAP_MARGIN_PERCENT / 100).to_integral_value(rounding=ROUND_HALF_UP))
            now = time.time()
            with self.db.atomic() as conn:
                user = conn.execute("SELECT nationality FROM users WHERE user_id=? AND status='active'", (user_id,)).fetchone()
                if not user:
                    raise ValueError("등록된 활성 사용자만 스왑을 체결할 수 있습니다.")
                nationality = user["nationality"]
                currency = CurrencyManager.home(conn, user_id)
                if not UserManager._change_cash(conn, user_id, -margin):
                    raise ValueError(f"증거금 {margin:,} {currency}가 부족합니다.")
                fixed = self._fixed(conn, nationality, tenor_days)
                swap_id = conn.execute(
                    "INSERT INTO swap_contracts(user_id,nationality,currency,notional,side,fixed_rate,tenor_days,"
                    "start_epoch,maturity_epoch,last_settle_epoch,margin) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, nationality, currency, notional, side, fixed, tenor_days,
                     now, now + tenor_days * 86400, now, margin)).lastrowid
            direction = "고정 지급 · 변동 수취" if side == "pay_fixed" else "고정 수취 · 변동 지급"
            return True, (f"✅ 금리스왑 #{swap_id} 체결 · {direction} · 명목 {notional:,} {currency} · "
                          f"고정 {fixed:.3f}% · {tenor_days}일 · 증거금 {margin:,} {currency}")
        except ValueError as exc:
            return False, f"❌ {exc}"

    def _settle_swap(self, conn, swap, now):
        """Exchange the net interest accrued since the last settlement."""
        end = min(now, float(swap["maturity_epoch"]))
        elapsed = max(0.0, end - float(swap["last_settle_epoch"]))
        if elapsed <= 0:
            return 0
        floating = self._floating(conn, swap["nationality"])
        spread = Decimal(str(floating)) - Decimal(str(swap["fixed_rate"]))
        if swap["side"] == "receive_fixed":
            spread = -spread
        net = int((Decimal(swap["notional"]) * spread / 100 * Decimal(str(elapsed)) / YEAR)
                  .to_integral_value(rounding=ROUND_HALF_UP))
        treasury = CurrencyManager.user_treasury_id(conn, swap["user_id"])
        margin = int(swap["margin"])
        if net > 0:
            CurrencyManager.treasury_pay(conn, treasury, swap["currency"], net * SCALE)
            UserManager._change_cash(conn, swap["user_id"], net)
        elif net < 0:
            owed = -net
            from_cash = owed if UserManager._change_cash(conn, swap["user_id"], -owed) else 0
            from_margin = min(margin, owed - from_cash)
            margin -= from_margin
            CurrencyManager.treasury_receive(conn, treasury, swap["currency"], (from_cash + from_margin) * SCALE)
            if from_cash + from_margin < owed:
                # The citizen cannot pay: the treasury keeps the margin and terminates the swap.
                conn.execute("UPDATE swap_contracts SET margin=0,realized=realized-?,last_settle_epoch=?,"
                             "status='defaulted',closed_epoch=? WHERE swap_id=?",
                             (from_cash + from_margin, end, now, swap["swap_id"]))
                return net
        conn.execute("UPDATE swap_contracts SET margin=?,realized=realized+?,last_settle_epoch=? WHERE swap_id=?",
                     (margin, net, end, swap["swap_id"]))
        if end >= float(swap["maturity_epoch"]):
            self._finish_swap(conn, swap["swap_id"], "matured", now)
        return net

    @staticmethod
    def _finish_swap(conn, swap_id, status, now):
        swap = conn.execute("SELECT * FROM swap_contracts WHERE swap_id=?", (swap_id,)).fetchone()
        if int(swap["margin"]):
            UserManager._change_cash(conn, swap["user_id"], int(swap["margin"]))
        conn.execute("UPDATE swap_contracts SET margin=0,status=?,closed_epoch=? WHERE swap_id=?", (status, now, swap_id))

    def close_swap(self, user_id, swap_id):
        """Terminate early at the mark-to-market value of the remaining fixed leg."""
        user_id = str(user_id)
        try:
            now = time.time()
            with self.db.atomic() as conn:
                swap = conn.execute("SELECT * FROM swap_contracts WHERE swap_id=? AND user_id=? AND status='open'",
                                    (int(swap_id), user_id)).fetchone()
                if not swap:
                    raise ValueError("진행 중인 내 스왑 번호를 확인해주세요.")
                self._settle_swap(conn, swap, now)
                swap = conn.execute("SELECT * FROM swap_contracts WHERE swap_id=?", (swap["swap_id"],)).fetchone()
                if swap["status"] != "open":
                    return True, f"✅ 스왑 #{swap_id}은(는) 이미 {swap['status']} 상태로 정산되었습니다."
                remaining = max(0.0, float(swap["maturity_epoch"]) - now)
                current = self._fixed(conn, swap["nationality"], max(1, int(remaining // 86400)))
                spread = Decimal(str(current)) - Decimal(str(swap["fixed_rate"]))
                if swap["side"] == "receive_fixed":
                    spread = -spread
                value = int((Decimal(swap["notional"]) * spread / 100 * Decimal(str(remaining)) / YEAR)
                            .to_integral_value(rounding=ROUND_HALF_UP))
                treasury = CurrencyManager.user_treasury_id(conn, user_id)
                if value > 0:
                    CurrencyManager.treasury_pay(conn, treasury, swap["currency"], value * SCALE)
                    UserManager._change_cash(conn, user_id, value)
                elif value < 0:
                    owed = -value
                    margin = int(swap["margin"])
                    from_margin = min(margin, owed)
                    if owed > from_margin and not UserManager._change_cash(conn, user_id, -(owed - from_margin)):
                        raise ValueError(f"해지 정산금 {owed:,} {swap['currency']}를 낼 잔액이 부족합니다.")
                    conn.execute("UPDATE swap_contracts SET margin=? WHERE swap_id=?", (margin - from_margin, swap["swap_id"]))
                    CurrencyManager.treasury_receive(conn, treasury, swap["currency"], owed * SCALE)
                conn.execute("UPDATE swap_contracts SET realized=realized+? WHERE swap_id=?", (value, swap["swap_id"]))
                self._finish_swap(conn, swap["swap_id"], "closed", now)
            return True, f"✅ 스왑 #{swap_id}을(를) 해지했습니다. 해지 정산 {value:+,} {swap['currency']} · 증거금 반환"
        except ValueError as exc:
            return False, f"❌ {exc}"

    def swaps(self, user_id):
        with self.db.readonly() as conn:
            rows = conn.execute("SELECT * FROM swap_contracts WHERE user_id=? ORDER BY swap_id DESC LIMIT 25",
                                (str(user_id),)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                if row["status"] == "open":
                    item["floating_rate"] = self._floating(conn, row["nationality"])
                result.append(item)
            return result

    # --------------------------------------------------------------- Wrap / IMA
    def wrap_order(self, manager_id, account_number, asset, side, quantity, price):
        """The wrap manager trades the client's wrap account."""
        with self.db.readonly() as conn:
            account = conn.execute("SELECT user_id,managed_by FROM accounts WHERE account_number=?",
                                   (str(account_number),)).fetchone()
        if not account or account["managed_by"] != str(manager_id):
            return False, "❌ 내가 운용하는 랩어카운트 계좌번호를 입력해주세요."
        book = self.order_book
        if book is None:
            from managers.order_book_manager import OrderBookManager
            book = OrderBookManager(self.db, self.users)
        return book.place_limit_order(account["user_id"], asset, side, quantity, price,
                                      funding_account=str(account_number))

    def managed_accounts(self, manager_id):
        with self.db.readonly() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT a.account_number,a.user_id,a.product_code,a.balance,a.quote_currency FROM accounts a "
                "WHERE a.managed_by=? ORDER BY a.account_number", (str(manager_id),))]

    @staticmethod
    def charge_wrap_fees(conn, now):
        """Accrue each wrap account's annual management fee to its manager."""
        rows = conn.execute(
            "SELECT a.account_number,a.user_id,a.managed_by,a.balance_units,a.quote_currency,a.fee_paid_epoch,"
            "s.management_fee_percent FROM accounts a JOIN saving s ON s.product_code=a.product_code "
            "WHERE a.managed_by IS NOT NULL").fetchall()
        for row in rows:
            start = float(row["fee_paid_epoch"] or now)
            elapsed = now - start
            if elapsed < 86400:
                continue
            fee = int(Decimal(int(row["balance_units"] or 0)) * Decimal(str(row["management_fee_percent"] or 0)) / 100
                      * Decimal(str(elapsed)) / YEAR)
            fee = min(fee, int(row["balance_units"] or 0))
            quantum = 10 ** (6 - CurrencyManager.precision(conn, row["quote_currency"]))
            fee -= fee % quantum
            if fee > 0:
                conn.execute("UPDATE accounts SET balance_units=balance_units-? WHERE account_number=?",
                             (fee, row["account_number"]))
                CurrencyManager.change(conn, row["managed_by"], row["quote_currency"], fee) \
                    if CurrencyManager.home(conn, row["managed_by"]) == row["quote_currency"] else \
                    CurrencyManager.change(conn, row["managed_by"], CurrencyManager.home(conn, row["managed_by"]),
                                           CurrencyManager.convert(conn, fee, row["quote_currency"],
                                                                   CurrencyManager.home(conn, row["managed_by"])))
            conn.execute("UPDATE accounts SET fee_paid_epoch=? WHERE account_number=?", (now, row["account_number"]))

    @staticmethod
    def ima_settlement(conn, account, balance):
        """IMA payout: the larger of the guaranteed balance and the shared reference gain.

        Returns (payout, top_up) in the account currency; the issuing bank pays top_up.
        """
        product = conn.execute("SELECT * FROM saving WHERE product_code=?", (account["product_code"],)).fetchone()
        if not product or product["product_kind"] != "ima" or not account["reference_price"]:
            return balance, 0
        from managers.investment_manager import InvestmentManager
        quote = InvestmentManager.component_quote(conn, product["reference_code"], active_only=False)
        if not quote or float(quote["price"] or 0) <= 0:
            return balance, 0
        principal = Decimal(str(account["principal"] or 0))
        growth = Decimal(str(quote["price"])) / Decimal(str(account["reference_price"])) - 1
        share = principal * max(Decimal(0), growth) * Decimal(str(product["participation_percent"])) / 100
        target = (principal + share).to_integral_value(rounding=ROUND_FLOOR)
        top_up = max(0, int(target) - int(balance))
        return int(balance) + top_up, top_up

    def bank_deficit_alerts(self, now=None):
        """Head offices that newly fell below zero (each warned once until it recovers)."""
        now = time.time() if now is None else now
        alerts = []
        with self.db.atomic() as conn:
            for row in conn.execute("SELECT b.issuer,b.user_id,u.cash,u.nationality FROM bank_accounts b "
                                    "JOIN users u ON u.user_id=b.user_id").fetchall():
                if float(row["cash"] or 0) >= 0:
                    conn.execute("DELETE FROM bank_deficit_alerts WHERE issuer=?", (row["issuer"],))
                    continue
                if conn.execute("SELECT 1 FROM bank_deficit_alerts WHERE issuer=?", (row["issuer"],)).fetchone():
                    continue
                conn.execute("INSERT INTO bank_deficit_alerts(issuer,cash,alerted_epoch) VALUES (?,?,?)",
                             (row["issuer"], float(row["cash"]), now))
                alerts.append({"issuer": row["issuer"], "account": row["user_id"], "cash": float(row["cash"]),
                               "currency": CurrencyManager.home(conn, row["user_id"])})
        return alerts

    @staticmethod
    def bank_deficits(conn):
        """Head offices currently in deficit (for dashboards)."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_accounts'").fetchone():
            return []
        return [dict(row) for row in conn.execute(
            "SELECT b.issuer,b.user_id account,u.cash,u.nationality FROM bank_accounts b JOIN users u "
            "ON u.user_id=b.user_id WHERE u.cash<0 ORDER BY u.cash")]

    def process_due(self, now=None):
        now = time.time() if now is None else now
        with self.db.atomic() as conn:
            for swap in conn.execute("SELECT * FROM swap_contracts WHERE status='open' "
                                     "AND (last_settle_epoch<=? OR maturity_epoch<=?)",
                                     (now - 86400, now)).fetchall():
                self._settle_swap(conn, swap, now)
            self.charge_wrap_fees(conn, now)
            # Book trades that settled later (fills, dividends, interest) on company accounts.
            from managers.company_account_manager import CompanyAccounts
            for row in conn.execute("SELECT company_code FROM company_accounts").fetchall():
                CompanyAccounts.reconcile(conn, row[0])
        return []
