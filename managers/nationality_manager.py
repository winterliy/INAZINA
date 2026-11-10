"""Changing a citizen's nationality liquidates everything into the new home currency.

Order (each step only runs when the previous one succeeded):
1. Refuse while obligations remain: futures positions, written options and their
   collateral, ELS subscriptions.
2. Cancel open/scheduled orders (deposits and reserved quantities come back).
3. Settle account interest.
4. In one transaction: sell holdings and treasury bonds to the old nationality's
   treasury at current value, close every account (no penalties: only demand and
   foreign-currency accounts exist today), and check that the collected old-currency
   cash covers outstanding loans; otherwise nothing is sold.
5. Repay loans from that cash.
6. In one transaction: exchange the cash into the new home currency at the current
   rate, paying the usual exchange fee to the old nationality's treasury, and move
   the user to the new nationality.
"""
from functions.money import base_currency_code
import json
import time
from decimal import Decimal, ROUND_CEILING

from managers.currency_manager import CurrencyManager, SCALE

OPEN_ORDER_STATES = ("open", "partial", "scheduled")
CLOSED_ELS_STATES = ("cancelled", "refunded", "settled", "redeemed")


class NationalityManager:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db

    def blockers(self, conn, user_id):
        reasons = []
        checks = (
            ("futures_positions", "user_id=?", "선물 포지션"),
            ("option_contracts", "writer_id=? AND status='active'", "작성한 옵션"),
            ("option_collateral_lots", "writer_id=?", "옵션 담보"),
            ("els_subscriptions", f"user_id=? AND status NOT IN {CLOSED_ELS_STATES}", "ELS 청약·보유"),
        )
        for table, condition, label in checks:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists and conn.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1", (str(user_id),)).fetchone():
                reasons.append(label)
        return reasons

    def change(self, actor, user_id, nationality):
        user_id, nationality = str(user_id), str(nationality).strip()
        with self.db.readonly() as conn:
            user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not user:
                raise ValueError("사용자를 찾을 수 없습니다.")
            target = conn.execute("SELECT currency_code FROM country_currencies WHERE nationality=?", (nationality,)).fetchone()
            if not target:
                raise ValueError("통화와 연결된 국적을 입력해주세요.")
            if user["nationality"] == nationality:
                return "ℹ️ 이미 같은 국적입니다."
            reasons = self.blockers(conn, user_id)
        if reasons:
            raise ValueError(f"{', '.join(reasons)}이(가) 남아 있어 국적을 바꿀 수 없습니다. 먼저 정리해주세요.")

        orders = self.db.assets.execute(
            f"SELECT order_id FROM market_orders WHERE user_id=? AND status IN {OPEN_ORDER_STATES}", (user_id,))
        for order in orders:
            ok, message = self.bot.order_book.cancel_order(user_id, order["order_id"])
            if not ok:
                raise ValueError(f"주문 #{order['order_id']}을 취소하지 못했습니다: {message}")

        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests(user_id)
        portfolio = self.bot.user_mgr.get_total_assets(user_id, settle=False)
        unit_nwx = {item["code"]: Decimal(str(item["value"])) / Decimal(str(item["qty"]))
                    for item in portfolio["stocks"] if item.get("qty") and not item.get("is_collateral")}
        bond_values = {item["holding_id"]: Decimal(str(item["market_value"])) for item in portfolio["bonds"]}

        summary = self._liquidate(actor, user_id, unit_nwx, bond_values)
        for loan in self.db.accounts.execute(
                "SELECT loan_id,product_code,outstanding_balance FROM loans WHERE user_id=? AND status='active' AND outstanding_balance>0",
                (user_id,)):
            amount = int(Decimal(str(loan["outstanding_balance"])).to_integral_value(rounding=ROUND_CEILING))
            ok, message = self.bot.bank_mgr.repay_loan(user_id, loan["product_code"], amount, loan["loan_id"])
            if not ok:
                raise ValueError(f"대출 #{loan['loan_id']}을 상환하지 못했습니다: {message}")
        return self._exchange_and_move(actor, user_id, nationality, summary)

    def _liquidate(self, actor, user_id, unit_nwx, bond_values):
        now = time.time()
        with self.db.atomic() as conn:
            old = CurrencyManager.home(conn, user_id)
            treasury = CurrencyManager.user_treasury_id(conn, user_id)
            sold = 0
            for row in conn.execute("SELECT * FROM user_assets WHERE user_id=?", (user_id,)).fetchall():
                unit = unit_nwx.get(row["asset_code"])
                if unit is None or not row["quantity"]:
                    continue
                proceeds = CurrencyManager.convert(conn, int(unit * Decimal(str(row["quantity"])) * SCALE), base_currency_code(conn), old)
                CurrencyManager.change(conn, user_id, old, proceeds)
                CurrencyManager.change(conn, treasury, old, -proceeds, allow_deficit=True)
                from functions.money import currency_precision, from_minor_units
                from managers.asset_cost_manager import AssetCostManager
                currency = AssetCostManager.currency(conn, row["asset_code"])
                cost = from_minor_units(
                    row["average_cost_minor_units"] or 0,
                    currency_precision(conn, currency),
                )
                AssetCostManager.upsert(
                    conn, treasury, row["asset_code"], None, row["quantity"], cost,
                    incoming_total_minor=AssetCostManager.total_cost_minor(conn, row['holding_id'])
                )
                conn.execute("DELETE FROM user_assets WHERE holding_id=?", (row["holding_id"],))
                sold += proceeds
            for holding in conn.execute(
                    "SELECT h.holding_id,COALESCE(p.quote_currency,base_currency_code()) AS currency FROM treasury_bond_holdings h "
                    "LEFT JOIN treasury_bond_products p ON p.product_code=h.product_code "
                    "WHERE h.user_id=? AND h.status='active'", (user_id,)).fetchall():
                value = bond_values.get(holding["holding_id"], Decimal(0))
                proceeds = CurrencyManager.convert(conn, int(value * SCALE), holding["currency"], old)
                CurrencyManager.change(conn, user_id, old, proceeds)
                CurrencyManager.change(conn, treasury, old, -proceeds, allow_deficit=True)
                conn.execute("UPDATE treasury_bond_holdings SET status='redeemed',redeemed_epoch=? WHERE holding_id=?",
                             (now, holding["holding_id"]))
                sold += proceeds
            closed = 0
            for account in conn.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchall():
                units = int(account["balance_units"] or 0)
                if units:
                    amount = units if account["quote_currency"] == old else CurrencyManager.convert(
                        conn, units, account["quote_currency"], old)
                    CurrencyManager.change(conn, user_id, old, amount)
                    closed += amount
                conn.execute("UPDATE accounts SET balance_units=0 WHERE account_number=?", (account['account_number'],))
                conn.execute("DELETE FROM accounts WHERE account_number=?", (account["account_number"],))
            loans = Decimal(0)
            for loan in conn.execute("SELECT outstanding_balance,quote_currency FROM loans WHERE user_id=? AND status='active'", (user_id,)):
                units = int(Decimal(str(loan["outstanding_balance"] or 0)) * SCALE)
                loans += units if (loan["quote_currency"] or old) == old else CurrencyManager.convert(conn, units, loan["quote_currency"], old)
            cash = CurrencyManager.balance(conn, user_id, old)
            if cash < loans:
                raise ValueError(f"자산을 모두 현금화해도 대출을 갚기에 부족합니다 (필요 {loans / SCALE:,} {old}, 보유 {cash / SCALE:,} {old}).")
            self.bot.admin_control._audit(conn, actor, "nationality.liquidate", {
                "user_id": user_id, "currency": old, "sold_units": sold, "accounts_units": closed})
        return {"sold": sold, "accounts": closed}

    def _exchange_and_move(self, actor, user_id, nationality, summary):
        with self.db.atomic() as conn:
            old = CurrencyManager.home(conn, user_id)
            treasury = CurrencyManager.user_treasury_id(conn, user_id)
            new = conn.execute("SELECT currency_code FROM country_currencies WHERE nationality=?", (nationality,)).fetchone()[0]
            units = CurrencyManager.balance(conn, user_id, old)
            fee = output = 0
            if units > 0:
                CurrencyManager.change(conn, user_id, old, -units)
                if old != new:
                    percent = Decimal(str((conn.execute(
                        "SELECT value FROM settings WHERE key='currency_exchange_fee_percent'").fetchone() or [0])[0]))
                    quantum = 10 ** (6 - CurrencyManager.precision(conn, old))
                    fee = int((Decimal(units) * percent / 100 / quantum).to_integral_value(rounding=ROUND_CEILING)) * quantum
                    fee = min(fee, units)
                    if fee:
                        # Exchange fees go to the user's nationality treasury: here, the one being left.
                        CurrencyManager.change(conn, treasury, old, fee, allow_deficit=True)
                    output = CurrencyManager.convert(conn, units - fee, old, new)
                else:
                    output = units
            conn.execute("UPDATE users SET nationality=? WHERE user_id=?", (nationality, user_id))
            if output:
                CurrencyManager.change(conn, user_id, new, output)
            self.bot.admin_control._audit(conn, actor, "nationality.change", {
                "user_id": user_id, "from_currency": old, "to": nationality, "to_currency": new,
                "exchanged_units": units, "fee_units": fee, "received_units": output, **summary})
        from managers.currency_exchange_manager import display_units
        return (f"✅ 국적을 {nationality}(으)로 바꿨습니다. 자산을 {display_units(units)} {old}로 정리한 뒤 "
                f"{display_units(output)} {new}를 지갑에 넣었습니다 (환전 수수료 {display_units(fee)} {old}, 이전 국고 귀속).")
