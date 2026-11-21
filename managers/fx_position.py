"""Read-only view of the system's open FX position and the close check of its period revaluation.

Lives at the ledger layer (L1) so the all-ledger close report in accounting_journal can include it without importing
a higher layer; booking and reversing the revaluation stays in fx_revaluation.
"""
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from functions.money import base_currency_code, currency_precision

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


class FxPosition:
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
        unavailable = sorted(code for code, amount in held.items()
                             if code != base and amount and code not in result)
        if unavailable:
            raise ValueError(f"활성 통화가 아닌 FX clearing 잔액: {', '.join(unavailable)}")
        return result

    @staticmethod
    def _last_rate(conn, currency):
        row = conn.execute("SELECT run_id,rate_text FROM fx_revaluation_runs WHERE currency_code=? AND kind IN ('baseline','revalue') "
                           "ORDER BY run_id DESC LIMIT 1", (currency,)).fetchone()
        if not row:
            return None
        fixed = conn.execute("SELECT rate_text FROM fx_revaluation_corrections WHERE run_id=? ORDER BY revision DESC LIMIT 1",
                             (row[0],)).fetchone()
        return Fraction(fixed[0] if fixed else row[1])

    @staticmethod
    def _effective(conn, run):
        """The run as last corrected: its position, rate and cumulative gain or loss, plus the correction rows."""
        fixes = [dict(row) for row in conn.execute(
            "SELECT * FROM fx_revaluation_corrections WHERE run_id=? ORDER BY revision", (int(run["run_id"]),))]
        last = fixes[-1] if fixes else run
        return {"places": int(last["decimal_places"]), "position": int(last["position_minor_units"]),
                "rate_text": last["rate_text"], "pnl": int(last["pnl_base_minor_units"]), "fixes": fixes}

    @staticmethod
    def _pnl(kind, position, places, rate, previous, base_places):
        if kind == "baseline" or previous is None:
            return 0
        return _round(Fraction(position, 10 ** places) * (rate - previous) * 10 ** base_places)

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
    def close_check(cls, conn, now=None):
        """Check the current period's position, fixing and journal (with any corrections) before close."""
        from managers.currency_manager import CurrencyManager
        now = int(now if now is not None else time.time())
        key = period_key(now)
        base = base_currency_code(conn)
        places = currency_precision(conn, base)
        positions = cls.positions(conn)
        runs = {}
        findings = []

        def posting_matches(source_key, entity_id, pnl, epoch, entity_type="revaluation"):
            entry = conn.execute("SELECT entry_id,currency_code,decimal_places,recorded_epoch FROM accounting_entries "
                                 "WHERE ledger_scope=? AND entity_type=? AND entity_id=? AND source_key=?",
                                 (SCOPE, entity_type, str(entity_id), source_key)).fetchone()
            lines = [] if not entry else [tuple(line) for line in conn.execute(
                "SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=? ORDER BY account_code",
                (entry['entry_id'],))]
            if not pnl:
                return entry is None
            expected = sorted([('fx_position_revaluation', max(pnl, 0), max(-pnl, 0)),
                               ('fx_revaluation_gain_loss', max(-pnl, 0), max(pnl, 0))])
            return bool(entry and (entry['currency_code'], int(entry['decimal_places']), int(entry['recorded_epoch']))
                        == (base, places, int(epoch)) and lines == expected)

        for row in conn.execute("SELECT r.run_id,r.period_key,r.currency_code,r.pnl_base_minor_units, "
                                "x.run_id AS reverse_id,x.pnl_base_minor_units AS reverse_pnl, "
                                "x.recorded_epoch AS reverse_epoch FROM fx_revaluation_runs r "
                                "LEFT JOIN fx_revaluation_runs x ON x.kind='reverse' AND x.reverses_run_id=r.run_id "
                                "WHERE r.kind='revalue' AND r.pnl_base_minor_units<>0 AND r.period_key<?", (key,)).fetchall():
            if row['reverse_id'] is None:
                findings.append({'currency': row['currency_code'], 'status': 'missing_reversal', 'run_id': int(row['run_id'])})
            elif (int(row['reverse_pnl']) != -int(row['pnl_base_minor_units'])
                  or int(row['reverse_epoch']) != next_period_start(row['period_key'])
                  or not posting_matches(f"run:{row['reverse_id']}", row['reverse_id'], int(row['reverse_pnl']), row['reverse_epoch'])):
                findings.append({'currency': row['currency_code'], 'status': 'reversal_mismatch', 'run_id': int(row['run_id'])})
        for row in conn.execute("SELECT c.run_id,c.revision,c.delta_base_minor_units,r.period_key,r.currency_code "
                                "FROM fx_revaluation_corrections c JOIN fx_revaluation_runs r ON r.run_id=c.run_id "
                                "WHERE c.delta_base_minor_units<>0 AND r.period_key<?", (key,)).fetchall():
            tag = f"{row['run_id']}:{row['revision']}"
            if not posting_matches(f"correction:{tag}:reverse", tag, -int(row['delta_base_minor_units']),
                                   next_period_start(row['period_key']), "revaluation_correction"):
                findings.append({'currency': row['currency_code'], 'status': 'missing_reversal', 'run_id': int(row['run_id']),
                                 'revision': int(row['revision'])})
        for row in conn.execute("SELECT * FROM fx_revaluation_runs WHERE period_key=? "
                                "AND kind IN ('baseline','revalue') ORDER BY run_id", (key,)).fetchall():
            currency = str(row['currency_code'])
            if currency in runs:
                findings.append({'currency': currency, 'status': 'duplicate_run'})
            runs[currency] = row
        for currency in sorted(set(positions) | set(runs)):
            scale, position = positions.get(currency, (None, 0))
            row = runs.get(currency)
            if not position and row is None:
                continue
            if row is None:
                findings.append({'currency': currency, 'status': 'missing_run'})
                continue
            eff = cls._effective(conn, row)
            try:
                rate = CurrencyManager.quote(conn, currency, base) if position else None
                current = (scale == eff['places']
                           and position == eff['position']
                           and (not position or Fraction(eff['rate_text']) == rate)
                           and period_key(row['recorded_epoch']) == key
                           and int(row['recorded_epoch']) <= now)
            except (ValueError, ArithmeticError, TypeError):
                current = False
            if not current:
                findings.append({'currency': currency, 'status': 'stale_or_invalid_run'})
            pnl = int(row['pnl_base_minor_units'])
            try:
                previous = row['previous_rate_text']
                if row['kind'] == 'baseline':
                    valid_calculation = previous is None and pnl == 0 and eff['pnl'] == 0
                else:
                    rate = Fraction(eff['rate_text'])
                    prior_rate = Fraction(previous)
                    valid_calculation = (rate > 0 and prior_rate > 0 and eff['pnl'] == _round(
                        Fraction(eff['position'], 10 ** eff['places'])
                        * (rate - prior_rate) * 10 ** places))
                valid_calculation = valid_calculation and eff['pnl'] == pnl + sum(int(fix['delta_base_minor_units']) for fix in eff['fixes'])
            except (ValueError, TypeError, ArithmeticError):
                valid_calculation = False
            if not valid_calculation:
                findings.append({'currency': currency, 'status': 'calculation_mismatch'})
            if not (posting_matches(f"run:{row['run_id']}", row['run_id'], pnl, row['recorded_epoch'])
                    and all(posting_matches(f"correction:{row['run_id']}:{fix['revision']}", f"{row['run_id']}:{fix['revision']}",
                                            int(fix['delta_base_minor_units']), fix['recorded_epoch'], "revaluation_correction")
                            for fix in eff['fixes'])):
                findings.append({'currency': currency, 'status': 'posting_mismatch'})
        return {'period': key, 'status': 'matched' if not findings else 'mismatch', 'findings': findings}

