"""Read-only interest-rate risk of one holder's treasury bond portfolio (M7).

Per-bond duration, DV01 and convexity come from ``RateManager.bond_quote`` (zero-curve discounting, +-1bp parallel shocks);
this only adds them up by holding quantity. The numbers are model estimates for a parallel curve move, not accounting values.
"""
from decimal import Decimal

from managers.treasury_bond_manager import TreasuryBondManager


def portfolio_risk(conn, holder_id, *, now):
    now = int(now)
    rows = conn.execute(
        "SELECT h.holding_id,h.product_code,h.product_name,h.principal,h.face_value,h.maturity_epoch,h.maturity_days,h.annual_rate,"
        "p.exchange_id,p.credit_spread,p.liquidity_spread,h.coupon_frequency_days,h.day_count_convention "
        "FROM accounts.treasury_bond_holdings h LEFT JOIN product.treasury_bond_products p ON p.product_code=h.product_code "
        "WHERE h.user_id=? AND h.status='active' AND h.maturity_epoch>? ORDER BY h.maturity_epoch", (str(holder_id), now)).fetchall()
    positions, value, dv01, dur_weight, conv_weight, mac_weight = [], Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    for row in rows:
        quantity = int(row["principal"]) // int(row["face_value"] or 1)
        quote = TreasuryBondManager.theoretical_price(conn, row, max(0, (int(row["maturity_epoch"]) - now) / 86400), now)
        market = Decimal(str(quote["dirty_price"])) * quantity
        position_dv01 = Decimal(str(quote["dv01"])) * quantity
        value += market
        dv01 += position_dv01
        dur_weight += Decimal(str(quote["modified_duration"])) * market
        mac_weight += Decimal(str(quote["macaulay_duration"])) * market
        conv_weight += Decimal(str(quote["convexity"])) * market
        positions.append({"holdingId": int(row["holding_id"]), "productCode": row["product_code"], "quantity": quantity,
                          "marketValue": str(market.quantize(Decimal("0.000001"))), "dv01": str(position_dv01.quantize(Decimal("0.000001"))),
                          "modifiedDuration": float(quote["modified_duration"]), "convexity": float(quote["convexity"]),
                          "daysToMaturity": max(0, (int(row["maturity_epoch"]) - now) // 86400)})
    weighted = (lambda total: str((total / value).quantize(Decimal("0.0001"))) if value > 0 else "0")
    return {"holderId": str(holder_id), "positionCount": len(positions), "marketValue": str(value.quantize(Decimal("0.000001"))),
            "dv01": str(dv01.quantize(Decimal("0.000001"))), "modifiedDuration": weighted(dur_weight),
            "macaulayDuration": weighted(mac_weight), "convexity": weighted(conv_weight), "positions": positions,
            "modelEstimate": True, "parallelShockBp": 1}
