"""Canonical per-unit cost handling for market asset positions."""
from decimal import Decimal, ROUND_HALF_UP

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

    @classmethod
    def upsert(cls, conn, user_id, asset_code, account_number, quantity, unit_cost,
               *, schema=""):
        """Add a position while keeping the weighted unit cost in native minor units."""
        table = f"{schema}.user_assets" if schema else "user_assets"
        currency = cls.currency(conn, asset_code)
        places = currency_precision(conn, currency)
        incoming_units = to_minor_units(unit_cost, places)
        incoming_quantity = Decimal(str(quantity))
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
            average_units = int((
                Decimal(old_units) * old_quantity + Decimal(incoming_units) * added_quantity
            ).__truediv__(total_quantity).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            conn.execute(
                f"UPDATE {table} SET quantity=?,average_cost_minor_units=? WHERE holding_id=?",
                (float(total_quantity), average_units, row["holding_id"]),
            )
            return average_units
        conn.execute(
            f"INSERT INTO {table} (user_id,asset_code,account_number,quantity,average_cost,"
            "average_cost_minor_units) VALUES (?,?,?,?,?,?)",
            (str(user_id), str(asset_code), account_number, float(incoming_quantity),
             float(from_minor_units(incoming_units, places)), incoming_units),
        )
        return incoming_units

    @classmethod
    def reprice(cls, conn, holding_id, asset_code, quantity, unit_cost, *, schema=""):
        """Change quantity/unit basis after a split or corporate allocation."""
        table = f"{schema}.user_assets" if schema else "user_assets"
        places = currency_precision(conn, cls.currency(conn, asset_code))
        units = to_minor_units(unit_cost, places)
        conn.execute(
            f"UPDATE {table} SET quantity=?,average_cost_minor_units=? WHERE holding_id=?",
            (float(Decimal(str(quantity))), units, int(holding_id)),
        )
        return units
