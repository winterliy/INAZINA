"""Rounding dust of currency conversions: recognized into the 환전 손익 account, later movable to the treasury.

Every conversion pays the receiver the converted amount rounded down to the target currency's minor unit, so a
fraction of a minor unit stays with the system. Summed per target currency it is the dust (the "FX clearing"
residual). ``recognize`` books the whole minor units of it as a gain (Dr fx_rounding_clearing / Cr fx_gain_loss, one
entry per currency) and keeps the fraction for later; the account keeps accumulating. ``transfer`` pays part of the
balance to the currency's treasury (Dr fx_gain_loss / Cr wallet_clearing, offsetting the treasury wallet row).
"""
import time
from fractions import Fraction

from functions.money import base_currency_code, currency_precision
from managers.accounting_journal import AccountingJournal

SCOPE = "fx_residual"
# table, source currency, source places, source amount, target currency, target places, target amount
CONVERSIONS = (
    ("cash_fx_settlements", "source_currency", "source_decimal_places", "source_minor_units",
     "currency", "decimal_places", "minor_units"),
    ("treasury_bond_cash_fx_settlements", "source_currency", "source_decimal_places", "source_minor_units",
     "settlement_currency", "settlement_decimal_places", "settlement_minor_units"),
)


class FxResiduals:
    @staticmethod
    def _dust(conn):
        """Exact unpaid fraction per target currency, in that currency's minor units: {currency: (places, Fraction)}."""
        dust = {}

        def add(currency, places, source_units, source_places, numerator, denominator, paid):
            exact = Fraction(int(source_units) * int(numerator) * 10 ** int(places),
                             int(denominator) * 10 ** int(source_places))
            remainder = exact - int(paid)
            entry = dust.setdefault(str(currency).upper(), [int(places), Fraction(0)])
            entry[1] += remainder

        for table, src, src_places, src_units, cur, places, units in CONVERSIONS:
            for row in conn.execute(f"SELECT t.{src_units} su,t.{src_places} sp,t.{cur} c,t.{places} p,t.{units} paid,"
                                    f"t.rate_numerator n,t.rate_denominator d FROM {table} t"):
                add(row["c"], row["p"], row["su"], row["sp"], row["n"], row["d"], row["paid"])
        base = base_currency_code(conn)
        base_places = currency_precision(conn, base)
        for row in conn.execute("SELECT base_minor_units su,currency c,decimal_places p,minor_units paid,"
                                "rate_numerator n,rate_denominator d FROM lottery_fx_settlements"):
            add(row["c"], row["p"], row["su"], base_places, row["n"], row["d"], row["paid"])
        return {currency: (places, total) for currency, (places, total) in dust.items()}

    @staticmethod
    def _booked(conn, kind):
        return {str(row[0]): int(row[1]) for row in conn.execute(
            "SELECT currency_code,SUM(minor_units) FROM fx_residual_clearings WHERE kind=? GROUP BY currency_code", (kind,))}

    @classmethod
    def report(cls, conn):
        """Per currency: exact dust, what is recognized, what waits to be recognized, what was moved, the balance."""
        recognized, transferred = cls._booked(conn, "recognize"), cls._booked(conn, "transfer")
        result = []
        for currency, (places, total) in sorted(cls._dust(conn).items()):
            whole = total.numerator // total.denominator
            done = recognized.get(currency, 0)
            result.append({"currency": currency, "decimalPlaces": places, "dust": f"{float(total):.6f}",
                           "recognized": done, "pending": max(0, whole - done), "transferred": transferred.get(currency, 0),
                           "balance": done - transferred.get(currency, 0)})
        return result

    @classmethod
    def recognize(cls, conn, actor, now=None):
        """Book the whole minor units of dust not yet recognized. Returns [(currency, units)]."""
        now = int(now if now is not None else time.time())
        booked = []
        for row in cls.report(conn):
            units = row["pending"]
            if units <= 0:
                continue
            clearing_id = conn.execute(
                "INSERT INTO fx_residual_clearings(kind,currency_code,decimal_places,minor_units,actor,recorded_epoch) "
                "VALUES ('recognize',?,?,?,?,?)", (row["currency"], row["decimalPlaces"], units, str(actor), now)).lastrowid
            AccountingJournal.register_mappings(conn, SCOPE, [("fx_rounding_clearing", "fx_rounding_clearing"),
                                                              ("fx_gain_loss", "fx_gain_loss"), ("wallet_clearing", "wallet_clearing")])
            AccountingJournal.post(conn, ledger_scope=SCOPE, entity_type="recognize", entity_id=str(clearing_id),
                                   source_key=f"recognize:{clearing_id}", currency_code=row["currency"],
                                   decimal_places=row["decimalPlaces"], recorded_epoch=now,
                                   lines=(("fx_rounding_clearing", "debit", units), ("fx_gain_loss", "credit", units)),
                                   description="FX rounding dust recognized")
            booked.append((row["currency"], units))
        return booked

    @classmethod
    def transfer(cls, conn, actor, currency, minor_units, now=None):
        """Pay ``minor_units`` of the account's balance in ``currency`` to that currency's treasury."""
        from managers.currency_manager import CurrencyManager
        now = int(now if now is not None else time.time())
        currency, units = str(currency).upper(), int(minor_units)
        row = next((item for item in cls.report(conn) if item["currency"] == currency), None)
        if units <= 0 or not row or units > row["balance"]:
            raise ValueError("이전할 수 있는 환전 손익 잔액을 넘었거나 금액이 올바르지 않습니다.")
        places = row["decimalPlaces"]
        nation = conn.execute("SELECT nationality FROM country_currencies WHERE currency_code=? ORDER BY nationality LIMIT 1",
                              (currency,)).fetchone()
        if not nation:
            raise ValueError("이 통화를 쓰는 나라의 국고를 찾을 수 없습니다.")
        treasury = CurrencyManager.treasury_id(conn, nation[0])
        if CurrencyManager.home(conn, treasury) != currency:
            raise ValueError("국고가 이 통화를 자국 통화로 보유하지 않아 환전 없이는 받을 수 없습니다.")
        mark = AccountingJournal.cash_mark(conn)
        CurrencyManager.treasury_receive(conn, treasury, currency, units * 10 ** (6 - places))
        clearing_id = conn.execute(
            "INSERT INTO fx_residual_clearings(kind,currency_code,decimal_places,minor_units,recipient_user_id,actor,recorded_epoch) "
            "VALUES ('transfer',?,?,?,?,?,?)", (currency, places, units, str(treasury), str(actor), now)).lastrowid
        AccountingJournal.register_mappings(conn, SCOPE, [("fx_gain_loss", "fx_gain_loss"), ("wallet_clearing", "wallet_clearing")])
        AccountingJournal.post(conn, ledger_scope=SCOPE, entity_type="transfer", entity_id=str(clearing_id),
                               source_key=f"transfer:{clearing_id}", currency_code=currency, decimal_places=places,
                               recorded_epoch=now, lines=(("fx_gain_loss", "debit", units), ("wallet_clearing", "credit", units)),
                               description="FX gain/loss paid to the treasury")
        AccountingJournal.link_cash(conn, "fx_residual", f"transfer:{clearing_id}", mark, now)
        return str(treasury)

    @staticmethod
    def history(conn, limit=50):
        return [dict(row) for row in conn.execute(
            "SELECT clearing_id,kind,currency_code,decimal_places,minor_units,recipient_user_id,actor,recorded_epoch "
            "FROM fx_residual_clearings ORDER BY clearing_id DESC LIMIT ?", (int(limit),))]
