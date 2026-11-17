"""Realized profit and loss, one row per sale, close or game result (table user_realized_pnl)."""
import time


class PnlLedger:
    @staticmethod
    def record(conn, user_id, kind, ref, currency, places, proceeds_minor, cost_minor, *, tax_minor=0,
               asset_code=None, now=None):
        """Append one result. ``pnl = proceeds - cost`` (negative for a loss); ``tax`` is withheld on top.

        Idempotent per (kind, ref). Amounts are integers in the currency's minor unit."""
        proceeds_minor, cost_minor = int(proceeds_minor), int(cost_minor)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO user_realized_pnl(user_id,kind,asset_code,currency_code,decimal_places,"
            "proceeds_minor_units,cost_minor_units,pnl_minor_units,tax_minor_units,ref,recorded_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(user_id), str(kind), asset_code, str(currency).upper(), int(places), proceeds_minor, cost_minor,
             proceeds_minor - cost_minor, int(tax_minor), str(ref),
             int(now if now is not None else time.time())))
        return cursor.rowcount == 1
