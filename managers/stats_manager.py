"""Personal statistics: what a citizen earned, lost and paid in tax, from the records the system already keeps.

Every number comes from an existing table (taxes, dividends, interest, lottery, ELS, loans, fees, basic income,
holdings, executions) plus the realized profit-and-loss ledger for sales, closes and game results, which are the
only results nothing else stores. Amounts are converted to the citizen's own currency at the live exchange rate on
every call, so asking again after a rate move gives the new figures.
"""
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from functions.money import base_currency_code, convert_minor_units, currency_precision, from_minor_units, to_minor_units

DAY = 86400
PERIODS = ("all", "year", "30d", "7d")

TAX_LABEL = {
    "stock_sale_gain": "주식·상품 양도세", "foreign_security_sale_gain": "외화 주식 양도세",
    "fractional_sale_gain": "소수 단위 양도세", "bond_sale_gain": "채권 양도세",
    "futures_realized_gain": "선물 양도세", "gambling_winnings": "도박 당첨세", "lottery_winnings": "복권 당첨세",
    "saving_interest": "예금 이자세", "treasury_bond_interest": "국채 이자세", "corporate_bond_interest": "회사채 이자세",
    "els_profit": "ELS 수익세", "special_dividend": "배당소득세", "special_dividend_reversal": "배당소득세 환급",
    "liquidation": "청산 배당세",
}
INCOME_KINDS = {"stock_sale": "주식·상품 매매", "bond_sale": "채권 매매", "futures": "선물", "gamble": "도박"}
# Before the realized-result ledger existed only the taxed gain of these survives (in the tax record), never the loss.
TAX_GAIN_KIND = {"stock_sale_gain": "stock_sale", "foreign_security_sale_gain": "stock_sale",
                 "fractional_sale_gain": "stock_sale", "bond_sale_gain": "bond_sale",
                 "futures_realized_gain": "futures", "gambling_winnings": "gamble"}


def period_start(period, now):
    if period == "all":
        return None
    if period == "30d":
        return now - 30 * DAY
    if period == "7d":
        return now - 7 * DAY
    if period == "year":
        return int(datetime(datetime.fromtimestamp(now, timezone.utc).year, 1, 1, tzinfo=timezone.utc).timestamp())
    raise ValueError("기간은 all, year, 30d, 7d 중 하나여야 합니다.")


class StatsManager:
    @staticmethod
    def _rate(conn, rates, currency, home):
        from managers.currency_manager import CurrencyManager
        currency = str(currency).upper()
        if currency not in rates:
            rates[currency] = (CurrencyManager.quote(conn, currency, home) if currency != home else None)
        return rates[currency]

    @classmethod
    def build(cls, conn, user_id, period="all", now=None):
        from managers.currency_manager import CurrencyManager
        now = int(now if now is not None else time.time())
        since = period_start(period, now)
        user_id = str(user_id)
        home = CurrencyManager.home(conn, user_id)
        home_places = currency_precision(conn, home)
        base = base_currency_code(conn)
        rates, places_of = {}, {}

        def places(currency):
            currency = str(currency).upper()
            if currency not in places_of:
                places_of[currency] = currency_precision(conn, currency)
            return places_of[currency]

        # (section, key, label) -> {"minor": {currency: units}, "count": n}
        cells = defaultdict(lambda: {"minor": defaultdict(int), "count": 0})
        epoch_ok = lambda epoch: since is None or (epoch is not None and float(epoch) >= since)

        def add(section, key, label, currency, minor, count=1):
            cell = cells[(section, key, label)]
            cell["minor"][str(currency).upper()] += int(minor)
            cell["count"] += count

        first_epoch = conn.execute("SELECT MIN(recorded_epoch) FROM user_realized_pnl").fetchone()[0]
        partial = set()
        # --- realized results (the ledger): sales, futures closes, game results
        trade_results = []
        for row in conn.execute("SELECT kind,asset_code,currency_code,decimal_places,pnl_minor_units,tax_minor_units,"
                                "recorded_epoch FROM user_realized_pnl WHERE user_id=? ORDER BY event_id", (user_id,)):
            if not epoch_ok(row["recorded_epoch"]):
                continue
            pnl, currency = int(row["pnl_minor_units"]), row["currency_code"]
            label = INCOME_KINDS.get(row["kind"], row["kind"])
            add("income" if pnl >= 0 else "loss", row["kind"], label, currency, abs(pnl))
            if row["kind"] != "gamble":
                trade_results.append((pnl, currency, int(row["decimal_places"])))

        # --- taxes (every kind), with interest income read from the same rows
        cutoff = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if since is not None else None
        for row in conn.execute(
                "SELECT source,currency_code,gross_amount_minor_units gross,tax_amount_minor_units tax,created_at "
                "FROM tax_transactions WHERE user_id=?" + (" AND created_at>=?" if cutoff else ""),
                (user_id, cutoff) if cutoff else (user_id,)):
            kind = str(row["source"]).split(":", 1)[0]
            add("tax", kind, TAX_LABEL.get(kind, kind), row["currency_code"], int(row["tax"]))
            if kind in TAX_GAIN_KIND:
                taxed_at = datetime.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                if first_epoch is None or taxed_at < first_epoch:
                    key = TAX_GAIN_KIND[kind]
                    add("income", key, INCOME_KINDS[key], row["currency_code"], int(row["gross"]))
                    partial.add(key)
            if kind in {"saving_interest", "treasury_bond_interest", "corporate_bond_interest"}:
                add("income", "interest", "예금·채권 이자", row["currency_code"], int(row["gross"]))

        # --- dividends (reversals come back as negative settlements)
        for row in conn.execute("SELECT gross_amount,quote_currency,created_epoch FROM dividend_payments WHERE user_id=?",
                                (user_id,)):
            if epoch_ok(row["created_epoch"]):
                add("income", "dividend", "배당", row["quote_currency"],
                    to_minor_units(row["gross_amount"], places(row["quote_currency"])))
        for row in conn.execute("SELECT gross_amount,kind,recorded_epoch FROM product_settlements "
                                "WHERE user_id=? AND kind LIKE 'special_dividend_reversal%'", (user_id,)):
            if epoch_ok(row["recorded_epoch"]):
                currency = str(row["kind"]).split(":", 1)[1] if ":" in str(row["kind"]) else base
                add("loss", "dividend", "배당", currency, abs(to_minor_units(row["gross_amount"], places(currency))))

        # --- lottery, ELS, basic income
        for row in conn.execute("SELECT price,created_epoch FROM lottery_tickets WHERE user_id=?", (user_id,)):
            if epoch_ok(row["created_epoch"]):
                add("loss", "lottery", "복권", base, to_minor_units(row["price"], places(base)))
        for row in conn.execute("SELECT gross,recorded_epoch FROM lottery_payouts WHERE user_id=?", (user_id,)):
            if epoch_ok(row["recorded_epoch"]):
                add("income", "lottery", "복권", base, to_minor_units(row["gross"], places(base)))
        for row in conn.execute("SELECT principal,gross_amount,recorded_epoch FROM els_settlements WHERE user_id=?", (user_id,)):
            if epoch_ok(row["recorded_epoch"]):
                pnl = to_minor_units(row["gross_amount"], places(base)) - to_minor_units(row["principal"], places(base))
                add("income" if pnl >= 0 else "loss", "els", "ELS", base, abs(pnl))
        for row in conn.execute("SELECT target_currency,target_units,recorded_epoch FROM basic_income_payments WHERE user_id=?",
                                (user_id,)):
            if epoch_ok(row["recorded_epoch"]):
                add("income", "basic_income", "기본소득", row["target_currency"],
                    int(row["target_units"]) // (10 ** (6 - places(row["target_currency"]))))

        # --- costs: loan interest and fees, exchange and transfer fees
        for row in conn.execute(
                "SELECT s.interest_paid,s.fee_paid,s.paid_epoch,l.quote_currency FROM loan_payment_schedule s "
                "JOIN loans l ON l.loan_id=s.loan_id WHERE l.user_id=? AND s.paid_epoch IS NOT NULL", (user_id,)):
            if epoch_ok(row["paid_epoch"]):
                currency = row["quote_currency"] or base
                interest = to_minor_units(row["interest_paid"] or 0, places(currency))
                fee = to_minor_units(row["fee_paid"] or 0, places(currency))
                if interest:
                    add("loss", "loan_interest", "대출 이자", currency, interest)
                if fee:
                    add("loss", "loan_fee", "대출 수수료", currency, fee)
        for table, who, label in (("currency_transactions", "user_id", "환전 수수료"),
                                  ("currency_transfer_transactions", "sender_id", "송금 수수료")):
            for row in conn.execute(f"SELECT fee_nwx,recorded_epoch FROM {table} WHERE {who}=?", (user_id,)):
                fee = to_minor_units(row["fee_nwx"] or 0, places(base))
                if fee and epoch_ok(row["recorded_epoch"]):
                    add("loss", "exchange_fee", label, base, fee)

        # --- holdings not sold yet (unrealized), valued at today's price
        holdings, unrealized = [], defaultdict(int)
        from managers.user_manager import UserManager
        for row in conn.execute("SELECT asset_code,SUM(quantity) quantity,SUM(average_cost_minor_units*quantity) cost "
                                "FROM user_assets WHERE user_id=? GROUP BY asset_code", (user_id,)):
            try:
                asset, _ = UserManager._asset_row(conn, row["asset_code"])
            except ValueError:
                continue
            if not asset or not asset.get("price"):
                continue
            currency = str(asset.get("quote_currency") or base).upper()
            asset_places = places(currency)
            value = to_minor_units(Decimal(str(asset["price"])) * Decimal(str(row["quantity"])), asset_places)
            cost = int(row["cost"] or 0)
            if cost <= 0:
                continue
            pnl = value - cost
            unrealized[currency] += pnl
            holdings.append({"code": row["asset_code"], "name": asset.get("name") or row["asset_code"],
                             "currency": currency, "pnl_minor": pnl, "cost_minor": cost, "places": asset_places})

        # --- trading counts
        trades = {"fills": 0, "buys": 0, "sells": 0}
        for row in conn.execute("SELECT buyer_id,seller_id,recorded_epoch FROM market_execution_events "
                                "WHERE buyer_id=? OR seller_id=?", (user_id, user_id)):
            if epoch_ok(row["recorded_epoch"]):
                trades["fills"] += 1
                trades["buys"] += int(row["buyer_id"] == user_id)
                trades["sells"] += int(row["seller_id"] == user_id)

        # --- convert everything to the home currency at today's rate
        def to_home(currency, minor):
            rate = cls._rate(conn, rates, currency, home)
            if rate is None:
                return int(minor)
            # Round the size, not the sign, so a loss converts to exactly the mirror image of the same gain.
            size = convert_minor_units(abs(int(minor)), places(currency), rate, home_places)
            return -size if int(minor) < 0 else size

        def home_total(minor_by_currency):
            return sum(to_home(c, m) for c, m in minor_by_currency.items())

        def shown(minor):
            return str(from_minor_units(int(minor), home_places))

        sources = {}
        for (section, key, label), cell in cells.items():
            if section not in ("income", "loss"):
                continue
            entry = sources.setdefault(key, {"key": key, "label": label, "income": 0, "loss": 0, "count": 0})
            entry[section] += home_total(cell["minor"])
            entry["count"] += cell["count"]
        taxes = [{"key": key, "label": label, "amount": shown(home_total(cell["minor"])), "count": cell["count"]}
                 for (section, key, label), cell in sorted(cells.items()) if section == "tax"]
        income = sum(item["income"] for item in sources.values())
        loss = sum(item["loss"] for item in sources.values())
        tax = sum(home_total(cell["minor"]) for (section, _, _), cell in cells.items() if section == "tax")
        results = [to_home(currency, pnl) for pnl, currency, _ in trade_results]
        gains = [value for value in results if value > 0]
        losses = [value for value in results if value < 0]
        return {
            "period": period, "since": since, "asOf": now, "home": home, "decimalPlaces": home_places,
            "rates": {currency: str(rate) for currency, rate in rates.items() if rate is not None},
            "totals": {"income": shown(income), "loss": shown(loss), "tax": shown(tax),
                       "net": shown(income - loss - tax)},
            "sources": [{"key": item["key"], "label": item["label"], "income": shown(item["income"]),
                         "loss": shown(item["loss"]), "net": shown(item["income"] - item["loss"]),
                         "count": item["count"], "gainsOnly": item["key"] in partial} for item in sorted(sources.values(), key=lambda i: -(i["income"] + i["loss"]))],
            "taxes": sorted(taxes, key=lambda item: -Decimal(item["amount"])),
            "unrealized": {"total": shown(home_total(unrealized)),
                           "items": sorted(({"code": h["code"], "name": h["name"], "currency": h["currency"],
                                             "pnl": str(from_minor_units(h["pnl_minor"], h["places"])),
                                             "returnPercent": round(h["pnl_minor"] * 100 / h["cost_minor"], 2)}
                                            for h in holdings), key=lambda item: -abs(Decimal(item["pnl"])))[:10]},
            "trades": {**trades, "results": len(results), "wins": len(gains),
                       "winRate": round(len(gains) * 100 / len(results), 1) if results else None,
                       "avgGain": shown(sum(gains) // len(gains)) if gains else None,
                       "avgLoss": shown(sum(losses) // len(losses)) if losses else None,
                       "maxGain": shown(max(gains)) if gains else None,
                       "maxLoss": shown(min(losses)) if losses else None},
            "recordingSince": int(first_epoch) if first_epoch is not None else None,
            "notRecorded": ["소수 단위 거래·옵션의 확정 손익은 기록 대상이 아니라 총수익·총손실에 들어가지 않습니다."],
        }
