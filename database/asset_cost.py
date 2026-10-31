"""Canonical per-unit cost handling for market asset positions."""
from functions.exact_money import ExactDecimal
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from database.exact_cost_basis import rounded_nonnegative

from functions.money import (
    base_currency_code,
    currency_precision,
    from_minor_units,
    to_minor_units,
)


class AssetCostManager:
    @staticmethod
    def currency(conn, asset_code):
        row = conn.execute(
            "SELECT quote_currency FROM market_assets WHERE code=? "
            "UNION ALL SELECT quote_currency FROM company WHERE company_code=? "
            "UNION ALL SELECT quote_currency FROM investment WHERE product_code=? "
            "LIMIT 1",
            (str(asset_code), str(asset_code), str(asset_code)),
        ).fetchone()
        return str(row[0] or base_currency_code(conn)).upper() if row else base_currency_code(conn)

    @staticmethod
    def total_cost_minor(conn, holding_id):
        exact = conn.execute("SELECT total_minor_units FROM asset_cost_basis WHERE holding_id=?", (int(holding_id),)).fetchone()
        if exact:
            return Fraction(exact[0])
        row = conn.execute("SELECT quantity,average_cost_minor_units FROM user_assets WHERE holding_id=?", (int(holding_id),)).fetchone()
        if not row:
            raise ValueError('보유분을 찾을 수 없습니다.')
        return Fraction(int(row['average_cost_minor_units'] or 0)) * Fraction(str(row['quantity']))

    @classmethod
    def sale_cost_minor(cls, conn, holding_id, quantity):
        from database.exact_cost_basis import remaining_cost_basis
        row = conn.execute("SELECT quantity,average_cost_minor_units FROM user_assets WHERE holding_id=?", (int(holding_id),)).fetchone()
        if not row or not 0 <= Decimal(str(quantity)) <= Decimal(str(row['quantity'])):
            raise ValueError('원가 배분 수량을 확인해주세요.')
        exact = conn.execute("SELECT total_minor_units FROM asset_cost_basis WHERE holding_id=?", (int(holding_id),)).fetchone()
        if exact:
            total = Fraction(exact[0])
            remaining = Fraction(str(row['quantity'])) - Fraction(str(quantity))
            return total - Fraction(remaining_cost_basis(str(total), row['quantity'], str(remaining)))
        return Fraction(int(row['average_cost_minor_units'] or 0)) * Fraction(str(quantity))

    @staticmethod
    def _save_total(conn, holding_id, total):
        units = Fraction(str(total))
        if units < 0:
            raise ValueError('취득원가는 음수일 수 없습니다.')
        conn.execute("INSERT INTO asset_cost_basis VALUES (?,?) ON CONFLICT(holding_id) DO UPDATE SET total_minor_units=excluded.total_minor_units",
                     (int(holding_id), str(units)))

    @staticmethod
    def decimal_minor(value):
        value = Fraction(str(value))
        with localcontext() as context:
            context.prec = 100
            return Decimal(value.numerator) / Decimal(value.denominator)

    @staticmethod
    def rounded_minor(value):
        return rounded_nonnegative(Fraction(str(value)))

    @staticmethod
    def require_unit_cost_support(conn, user_id=None, asset_code=None):
        """Refuse custody paths that cannot carry a fractional per-share basis yet."""
        rows = conn.execute("SELECT u.quantity,u.average_cost_minor_units,b.total_minor_units FROM user_assets u "
                            "JOIN asset_cost_basis b USING(holding_id) WHERE (? IS NULL OR u.user_id=?) "
                            "AND (? IS NULL OR u.asset_code=?)", (user_id, user_id, asset_code, asset_code))
        if any(Fraction(row['total_minor_units']) != Fraction(str(row['quantity'])) * int(row['average_cost_minor_units'] or 0) for row in rows):
            raise ValueError('분할된 보유분의 총 취득원가를 이 담보·예탁·합병 경로로 정확히 이전할 수 없습니다.')

    @classmethod
    def upsert(cls, conn, user_id, asset_code, account_number, quantity, unit_cost,
               *, schema="", incoming_total_minor=None):
        """Add a position while keeping the weighted unit cost in native minor units."""
        table = f"{schema}.user_assets" if schema else "user_assets"
        currency = cls.currency(conn, asset_code)
        places = currency_precision(conn, currency)
        incoming_units = to_minor_units(unit_cost, places)
        incoming_quantity = Decimal(str(quantity))
        incoming_total = Fraction(str(incoming_total_minor)) if incoming_total_minor is not None else Fraction(incoming_units) * Fraction(str(incoming_quantity))
        if incoming_total_minor is not None:
            incoming_units = rounded_nonnegative(incoming_total / Fraction(str(incoming_quantity)))
        row = conn.execute(
            f"SELECT holding_id,quantity,average_cost,average_cost_minor_units FROM {table} "
            "WHERE user_id=? AND asset_code=? AND account_number IS ? ORDER BY holding_id LIMIT 1",
            (str(user_id), str(asset_code), account_number),
        ).fetchone()
        if row:
            old_quantity = Decimal(str(row["quantity"] or 0))
            added_quantity = incoming_quantity
            total_quantity = old_quantity + added_quantity
            old_units = int(row["average_cost_minor_units"] or 0)
            if old_units == 0 and row["average_cost"]:
                old_units = to_minor_units(row["average_cost"], places)
            exact = conn.execute("SELECT total_minor_units FROM asset_cost_basis WHERE holding_id=?", (row['holding_id'],)).fetchone()
            old_total = Fraction(exact[0]) if exact else Fraction(old_units) * Fraction(str(old_quantity))
            new_total = old_total + incoming_total
            average_units = rounded_nonnegative(new_total / Fraction(str(total_quantity)))
            conn.execute(
                f"UPDATE {table} SET quantity=?,average_cost_minor_units=? WHERE holding_id=?",
                (float(total_quantity), average_units, row["holding_id"]),
            )
            if exact or incoming_total_minor is not None:
                cls._save_total(conn, row['holding_id'], new_total)
            return average_units
        created = conn.execute(
            f"INSERT INTO {table} (user_id,asset_code,account_number,quantity,average_cost,"
            "average_cost_minor_units) VALUES (?,?,?,?,?,?)",
            (str(user_id), str(asset_code), account_number, float(incoming_quantity),
             ExactDecimal(from_minor_units(incoming_units, places)), incoming_units),
        )
        if incoming_total_minor is not None:
            cls._save_total(conn, created.lastrowid, incoming_total)
        return incoming_units

    @classmethod
    def reprice(cls, conn, holding_id, asset_code, quantity, unit_cost, *, schema="", preserve_total=False):
        """Change quantity/unit basis after a split or corporate allocation."""
        table = f"{schema}.user_assets" if schema else "user_assets"
        places = currency_precision(conn, cls.currency(conn, asset_code))
        units = to_minor_units(unit_cost, places)
        exact = conn.execute("SELECT total_minor_units FROM asset_cost_basis WHERE holding_id=?", (int(holding_id),)).fetchone()
        old_total = cls.total_cost_minor(conn, holding_id) if preserve_total else None
        if preserve_total:
            units = rounded_nonnegative(Fraction(str(old_total)) / Fraction(str(quantity)))
        conn.execute(
            f"UPDATE {table} SET quantity=?,average_cost_minor_units=? WHERE holding_id=?",
            (float(Decimal(str(quantity))), units, int(holding_id)),
        )
        if preserve_total or exact:
            cls._save_total(conn, holding_id, old_total if preserve_total else Decimal(str(quantity)) * units)
        return units
