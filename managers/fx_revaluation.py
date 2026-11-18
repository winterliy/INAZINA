"""Period-end revaluation of the system's open FX position.

The position in a foreign currency is what the FX clearing accounts hold in it (credits minus debits: received and
not yet paid out). At each period end the position is revalued at the change of the live exchange rate (base
currency per unit) since the previous period end. The first run of a currency only stores its rate. A gain or loss is
booked in the base currency (Dr/Cr fx_position_revaluation against fx_revaluation_gain_loss) and undone by an
automatic reversing entry dated the first second of the next period, so the result of a period is its own.
"""
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from functions.money import base_currency_code, currency_precision
from managers.accounting_journal import AccountingJournal

SCOPE = "fx_revaluation"
CLEARING_ACCOUNTS = ("fx_conversion_clearing", "fx_settlement_clearing")
PERIOD_ZONE = timezone(timedelta(hours=9))        # 하루 단위 기간은 한국 시간 기준


def period_key(epoch):
    return datetime.fromtimestamp(int(epoch), PERIOD_ZONE).strftime("%Y-%m-%d")


def next_period_start(key):
    day = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=PERIOD_ZONE) + timedelta(days=1)
    return int(day.timestamp())


def _round(value):
    value = Fraction(value)
    whole = abs(value.numerator) * 2 + value.denominator
    rounded = whole // (value.denominator * 2)
    return -rounded if value < 0 else rounded


class FxRevaluation:
    @staticmethod
    def positions(conn):
        """{currency: (places, minor units long)} for every foreign currency (zero when nothing is open)."""
        base = base_currency_code(conn)
        held = {str(row[0]).upper(): int(row[1]) for row in conn.execute(
            "SELECT e.currency_code,SUM(l.credit_units-l.debit_units) FROM accounting_lines l JOIN accounting_entries e ON "
            f"e.entry_id=l.entry_id WHERE l.account_code IN ({','.join('?' * len(CLEARING_ACCOUNTS))}) GROUP BY e.currency_code",
            CLEARING_ACCOUNTS)}
        result = {}
        for row in conn.execute("SELECT code,decimal_places FROM currencies WHERE status='active' ORDER BY code"):
            code = str(row[0]).upper()
            if code != base:
                result[code] = (int(row[1]), held.get(code, 0))
        return result

    @staticmethod
    def _last_rate(conn, currency):
        row = conn.execute("SELECT rate_text FROM fx_revaluation_runs WHERE currency_code=? AND kind IN ('baseline','revalue') "
                           "ORDER BY run_id DESC LIMIT 1", (currency,)).fetchone()
        return Fraction(row[0]) if row else None

    @classmethod
    def preview(cls, conn):
        """What a run now would do per currency: position, previous and current rate, resulting gain or loss."""
        from managers.currency_manager import CurrencyManager
        base = base_currency_code(conn)
        base_places = currency_precision(conn, base)
        rows = []
        for currency, (places, position) in cls.positions(conn).items():
            rate = CurrencyManager.quote(conn, currency, base)
            previous = cls._last_rate(conn, currency)
            pnl = 0 if previous is None else _round(Fraction(position, 10 ** places) * (rate - previous) * 10 ** base_places)
            rows.append({"currency": currency, "decimalPlaces": places, "position": position, "rate": f"{float(rate):.6f}",
                         "previousRate": None if previous is None else f"{float(previous):.6f}", "pnl": pnl,
                         "baseline": previous is None})
        return rows

    @classmethod
    def run(cls, conn, actor, now=None):
        """Revalue every foreign currency for the current period. Returns [(currency, kind, pnl)]; safe to repeat."""
        from managers.currency_manager import CurrencyManager
        now = int(now if now is not None else time.time())
        key = period_key(now)
        cls.reverse_due(conn, actor, now)
        base = base_currency_code(conn)
        base_places = currency_precision(conn, base)
        done = []
        for currency, (places, position) in cls.positions(conn).items():
            if conn.execute("SELECT 1 FROM fx_revaluation_runs WHERE kind IN ('baseline','revalue') AND period_key=? AND currency_code=?",
                            (key, currency)).fetchone():
                continue
            rate = CurrencyManager.quote(conn, currency, base)
            previous = cls._last_rate(conn, currency)
            if previous is None:
                kind, pnl = "baseline", 0
            else:
                kind, pnl = "revalue", _round(Fraction(position, 10 ** places) * (rate - previous) * 10 ** base_places)
            run_id = conn.execute(
                "INSERT INTO fx_revaluation_runs(kind,period_key,currency_code,decimal_places,position_minor_units,rate_text,"
                "previous_rate_text,pnl_base_minor_units,actor,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (kind, key, currency, places, position, str(rate), None if previous is None else str(previous), pnl, str(actor), now)).lastrowid
            if pnl:
                cls._post(conn, run_id, pnl, base, base_places, now, "FX position revaluation")
            done.append((currency, kind, pnl))
        return done

    @staticmethod
    def _post(conn, run_id, pnl, base, base_places, epoch, description):
        AccountingJournal.register_mappings(conn, SCOPE, [("fx_position_revaluation", "fx_position_revaluation"),
                                                          ("fx_revaluation_gain_loss", "fx_revaluation_gain_loss")])
        gain = pnl > 0
        AccountingJournal.post(
            conn, ledger_scope=SCOPE, entity_type="revaluation", entity_id=str(run_id), source_key=f"run:{run_id}",
            currency_code=base, decimal_places=base_places, recorded_epoch=int(epoch),
            lines=(("fx_position_revaluation", "debit" if gain else "credit", abs(pnl)),
                   ("fx_revaluation_gain_loss", "credit" if gain else "debit", abs(pnl))), description=description)

    @classmethod
    def reverse_due(cls, conn, actor, now=None):
        """Undo every booked revaluation whose period has ended, dated the first second of the next period."""
        now = int(now if now is not None else time.time())
        base = base_currency_code(conn)
        base_places = currency_precision(conn, base)
        today, reversed_now = period_key(now), []
        for row in conn.execute(
                "SELECT r.* FROM fx_revaluation_runs r WHERE r.kind='revalue' AND r.pnl_base_minor_units<>0 AND r.period_key<? "
                "AND NOT EXISTS (SELECT 1 FROM fx_revaluation_runs x WHERE x.kind='reverse' AND x.reverses_run_id=r.run_id) "
                "ORDER BY r.run_id", (today,)).fetchall():
            epoch = next_period_start(row["period_key"])
            run_id = conn.execute(
                "INSERT INTO fx_revaluation_runs(kind,period_key,currency_code,decimal_places,position_minor_units,rate_text,"
                "previous_rate_text,pnl_base_minor_units,reverses_run_id,actor,recorded_epoch) VALUES ('reverse',?,?,?,?,?,?,?,?,?,?)",
                (row["period_key"], row["currency_code"], row["decimal_places"], row["position_minor_units"], row["rate_text"],
                 row["previous_rate_text"], -int(row["pnl_base_minor_units"]), int(row["run_id"]), str(actor), epoch)).lastrowid
            cls._post(conn, run_id, -int(row["pnl_base_minor_units"]), base, base_places, epoch, "FX position revaluation reversed")
            reversed_now.append(int(row["run_id"]))
        return reversed_now

    @staticmethod
    def history(conn, limit=50):
        return [dict(row) for row in conn.execute(
            "SELECT run_id,kind,period_key,currency_code,decimal_places,position_minor_units,rate_text,previous_rate_text,"
            "pnl_base_minor_units,reverses_run_id,actor,recorded_epoch FROM fx_revaluation_runs ORDER BY run_id DESC LIMIT ?", (int(limit),))]
