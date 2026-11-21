"""Period-end revaluation of the system's open FX position.

The position in a foreign currency is what the FX clearing accounts hold in it (credits minus debits: received and
not yet paid out). At each period end the position is revalued at the change of the live exchange rate (base
currency per unit) since the previous period end. The first run of a currency only stores its rate. A gain or loss is
booked in the base currency (Dr/Cr fx_position_revaluation against fx_revaluation_gain_loss) and undone by an
automatic reversing entry dated the first second of the next period, so the result of a period is its own.
"""
import time
from fractions import Fraction

from functions.money import base_currency_code, currency_precision
from managers.accounting_journal import AccountingJournal
from managers.fx_position import (CLEARING_ACCOUNTS, PERIOD_ZONE, SCOPE, FxPosition, _round, next_period_start, period_key)  # noqa: F401

class FxRevaluation(FxPosition):
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
            existing = conn.execute("SELECT * FROM fx_revaluation_runs WHERE kind IN ('baseline','revalue') AND period_key=? "
                                    "AND currency_code=?", (key, currency)).fetchone()
            if existing:
                done.extend(cls._correct(conn, existing, currency, places, position, actor, now, base, base_places))
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

    @classmethod
    def _correct(cls, conn, run, currency, places, position, actor, now, base, base_places):
        """A position or fixing that changed after the period's run is booked as an appended correction (the delta), never an edit."""
        from managers.currency_manager import CurrencyManager
        eff = cls._effective(conn, run)
        rate = CurrencyManager.quote(conn, currency, base)
        if (places, position, rate) == (eff["places"], eff["position"], Fraction(eff["rate_text"])):
            return []
        previous = None if run["previous_rate_text"] is None else Fraction(run["previous_rate_text"])
        pnl = cls._pnl(run["kind"], position, places, rate, previous, base_places)
        delta, revision = pnl - eff["pnl"], len(eff["fixes"]) + 1
        tag = f"{int(run['run_id'])}:{revision}"
        if delta:
            cls._post(conn, tag, delta, base, base_places, now, "FX position revaluation correction",
                      source_key=f"correction:{tag}", entity_type="revaluation_correction")
        conn.execute(
            "INSERT INTO fx_revaluation_corrections(run_id,revision,decimal_places,position_minor_units,rate_text,"
            "pnl_base_minor_units,delta_base_minor_units,actor,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(run["run_id"]), revision, places, position, str(rate), pnl, delta, str(actor), now))
        return [(currency, "correction", pnl)]

    @staticmethod
    def _post(conn, run_id, pnl, base, base_places, epoch, description, source_key=None, entity_type="revaluation"):
        AccountingJournal.register_mappings(conn, SCOPE, [("fx_position_revaluation", "fx_position_revaluation"),
                                                          ("fx_revaluation_gain_loss", "fx_revaluation_gain_loss")])
        gain = pnl > 0
        AccountingJournal.post(
            conn, ledger_scope=SCOPE, entity_type=entity_type, entity_id=str(run_id), source_key=source_key or f"run:{run_id}",
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
        for row in conn.execute(
                "SELECT c.run_id,c.revision,c.delta_base_minor_units,r.period_key FROM fx_revaluation_corrections c "
                "JOIN fx_revaluation_runs r ON r.run_id=c.run_id WHERE c.delta_base_minor_units<>0 AND r.period_key<? "
                "ORDER BY c.correction_id", (today,)).fetchall():
            tag = f"{row['run_id']}:{row['revision']}"
            if conn.execute("SELECT 1 FROM accounting_entries WHERE ledger_scope=? AND source_key=?",
                            (SCOPE, f"correction:{tag}:reverse")).fetchone():
                continue
            cls._post(conn, tag, -int(row["delta_base_minor_units"]), base, base_places, next_period_start(row["period_key"]),
                      "FX position revaluation correction reversed", source_key=f"correction:{tag}:reverse",
                      entity_type="revaluation_correction")
        return reversed_now

    @staticmethod
    def history(conn, limit=50):
        return [dict(row) for row in conn.execute(
            "SELECT run_id,kind,period_key,currency_code,decimal_places,position_minor_units,rate_text,previous_rate_text,"
            "pnl_base_minor_units,reverses_run_id,actor,recorded_epoch FROM fx_revaluation_runs ORDER BY run_id DESC LIMIT ?", (int(limit),))]
