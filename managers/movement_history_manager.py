"""Clear prior Korean calendar days of Movement chart samples."""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from database.unified import UnifiedConnection


class MovementHistoryManager:
    DATE_KEY = "movement_history_date"
    KST = timezone(timedelta(hours=9))
    HISTORY_TABLES = (
        "market_price_history",
    )

    def __init__(self, db):
        self.db = db

    @contextmanager
    def _transaction(self):
        # Market ticks skip a busy database instead of queueing behind the
        # general transaction manager's longer timeout or process-wide locks.
        conn = sqlite3.connect(self.db.assets.db_path, timeout=0.05, isolation_level=None, factory=UnifiedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 50")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @classmethod
    def day_boundary(cls, now):
        local = datetime.fromtimestamp(int(now), cls.KST)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return local.date().isoformat(), int(midnight.timestamp())

    @staticmethod
    def _preserve_future_references(conn, now):
        products = conn.execute(
            "SELECT product_code, underlying_code, maturity_epoch "
            "FROM product.investment WHERE product_type = 'stock_future' "
            "AND status = 'active' AND underlying_code IS NOT NULL "
            "AND maturity_epoch IS NOT NULL"
        ).fetchall()
        for product in products:
            code, underlying = str(product["product_code"]), str(product["underlying_code"])
            expiry = int(product["maturity_epoch"])
            cached = conn.execute(
                "SELECT * FROM assets.market_future_expiry_prices WHERE product_code = ?",
                (code,),
            ).fetchone()
            if cached and (
                str(cached["underlying_code"]) != underlying
                or int(cached["maturity_epoch"]) != expiry
            ):
                cached = None
            if cached and int(cached["is_final"]):
                continue
            latest = conn.execute(
                "SELECT price, recorded_epoch FROM assets.market_price_history "
                "WHERE asset_code = ? AND recorded_epoch <= ? "
                "ORDER BY recorded_epoch DESC LIMIT 1",
                (underlying, min(expiry, now)),
            ).fetchone()
            if cached and (not latest or int(cached["recorded_epoch"]) > int(latest["recorded_epoch"])):
                latest = cached
            if not latest or int(latest["price"]) <= 0:
                continue
            conn.execute(
                "INSERT INTO assets.market_future_expiry_prices "
                "(product_code, underlying_code, maturity_epoch, price, recorded_epoch, is_final) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(product_code) DO UPDATE SET "
                "underlying_code = excluded.underlying_code, maturity_epoch = excluded.maturity_epoch, "
                "price = excluded.price, recorded_epoch = excluded.recorded_epoch, "
                "is_final = excluded.is_final",
                (code, underlying, expiry, int(latest["price"]), int(latest["recorded_epoch"]), int(now >= expiry)),
            )

    def cleanup(self, now=None):
        """Delete chart logs before today's KST midnight once per date.

        The date marker commits together with deletion. A restart or a failed
        attempt therefore cannot silently skip a day's cleanup. Asset holdings,
        orders, contract state, settlements and current index/ETF state are not
        chart history and remain intact.
        """
        now = int(time.time() if now is None else now)
        date, midnight = self.day_boundary(now)
        if self.db.system.get_setting(self.DATE_KEY) == date:
            return False
        try:
            with self._transaction() as conn:
                previous = conn.execute(
                    "SELECT value FROM system.settings WHERE key = ?", (self.DATE_KEY,)
                ).fetchone()
                if previous and str(previous["value"]) >= date:
                    return False
                self._preserve_future_references(conn, now)
                for table in self.HISTORY_TABLES:
                    conn.execute(
                        f"DELETE FROM assets.{table} WHERE recorded_epoch < ?", (midnight,)
                    )
                conn.execute(
                    "INSERT INTO system.settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (self.DATE_KEY, date),
                )
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                return False
            raise
        return True
