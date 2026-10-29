"""Fill resting limit orders that the startup gap-fill prices reached.

While the bot was off, managers/price_gap_filler.py draws the prices the market would
have shown. A resting buy whose limit the drawn price fell to, or a sell whose limit it
rose to, executes at its own limit when the bot starts. Nobody else traded while the bot
was off, so the counterparty is the listing country's treasury, which is already the
state counterparty for primary issues: a buy takes the treasury's own holding first and
then unissued supply, a sell is bought by the treasury. The treasury places a real order
for each fill, so the execution ledger, holding checks and journal match ordinary trades.

The fill does not move the reference price: the chart already shows the drawn path.
"""
import time

from functions.money import base_currency_code, currency_precision, from_minor_units

GAP_ORDER_REASON = "gap_fill"


def _treasury_order(conn, treasury, order, side, quantity, now):
    cursor = conn.execute(
        "INSERT INTO assets.market_orders (user_id, asset_code, side, limit_price, quantity, remaining_quantity, "
        "reserved_cash, funding_account, order_kind, status, created_epoch, updated_epoch, activation_epoch, "
        "scheduled_reason, quote_currency, reserved_cash_minor_units) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, NULL, 'asset', 'open', ?, ?, ?, ?, ?, 0)",
        (treasury, order["asset_code"], side, order["limit_price"], quantity, quantity, now, now, now,
         GAP_ORDER_REASON, order["quote_currency"]))
    return conn.execute("SELECT * FROM assets.market_orders WHERE order_id=?", (cursor.lastrowid,)).fetchone()


def _reached(conn, order, now):
    """Whether a price drawn by this start's gap fill crossed the order's limit after it was placed."""
    compare = "MIN(h.price) <= ?" if order["side"] == "buy" else "MAX(h.price) >= ?"
    row = conn.execute(
        f"SELECT {compare} FROM market_price_history h JOIN market_price_gap_fills f "
        "ON f.asset_code=h.asset_code AND h.recorded_epoch > f.start_epoch AND h.recorded_epoch < f.end_epoch "
        "WHERE f.filled_epoch=? AND h.asset_code=? AND h.recorded_epoch >= ?",
        (float(from_minor_units(int(order["limit_price_minor_units"]), _places(conn, order))), now, order["asset_code"],
         int(order["activation_epoch"] or order["created_epoch"]))).fetchone()
    return bool(row and row[0])


def _places(conn, order):
    return currency_precision(conn, str(order["quote_currency"] or base_currency_code(conn)))


def _secondary_fill(book, conn, buy_order, sell_order, quantity, price, source, now):
    wallet_watermark = int(conn.execute("SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger").fetchone()[0])
    holding_before = book._capture_execution_holding(
        conn, buy_order["asset_code"], {"buyer": str(buy_order["user_id"]), "seller": str(sell_order["user_id"])},
        source, now)
    settlement = book._settle_fill(conn, buy_order, sell_order, quantity, price, now)
    book._reduce_order(conn, buy_order, quantity, now)
    book._reduce_order(conn, sell_order, quantity, now)
    book._record_execution_event(
        conn, buy_order, sell_order, quantity, price, now, tax_amount=settlement["tax_amount"],
        seller_proceeds=settlement["seller_proceeds"], wallet_watermark=wallet_watermark,
        holding_before=holding_before, inventory_type="user_asset")


def _primary_fill(book, conn, buy_order, asset, source, quantity, price, now):
    wallet_watermark = int(conn.execute("SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger").fetchone()[0])
    holding_before = book._capture_execution_holding(
        conn, buy_order["asset_code"], {"buyer": str(buy_order["user_id"])}, source, now)
    book._settle_primary_fill(conn, buy_order, asset, source, quantity, price, now)
    book._reduce_order(conn, buy_order, quantity, now)
    book._record_execution_event(conn, buy_order, None, quantity, price, now, wallet_watermark=wallet_watermark,
                                 holding_before=holding_before, inventory_type="user_asset")


def _unissued(conn, asset):
    """Shares the issuer still has to sell; None for listings without a supply cap."""
    if asset.get("supply") is None:
        return None
    issued = conn.execute(
        "SELECT (SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets WHERE asset_code=?)"
        " + (SELECT COALESCE(SUM(quantity),0) FROM assets.option_collateral_lots WHERE underlying_code=?)",
        (asset["code"], asset["code"])).fetchone()[0]
    return max(0, int(asset["supply"]) - int(issued or 0))


def settle_gap_orders(book, now=None):
    """Execute every resting order the latest gap fill reached. Returns [(order_id, quantity), …]."""
    from managers.currency_manager import CurrencyManager, SCALE

    now = int(now if now is not None else time.time())
    filled = []
    with book.db.atomic(("users", "accounts", "assets", "product", "company", "tax", "system")) as conn:
        if not conn.execute("SELECT 1 FROM market_price_gap_fills WHERE filled_epoch=? AND points>0 LIMIT 1",
                            (now,)).fetchone():
            return filled
        orders = conn.execute(
            "SELECT * FROM assets.market_orders WHERE order_kind='asset' AND fractional_order=0 "
            "AND status IN ('open','partial') AND remaining_quantity>0 AND asset_code IN "
            "(SELECT asset_code FROM market_price_gap_fills WHERE filled_epoch=? AND points>0) "
            "ORDER BY created_epoch, order_id", (now,)).fetchall()
        for order in orders:
            asset, source = book.user_manager._asset_row(conn, order["asset_code"])
            # Treasury bonds settle through their own holding ledger; their price is not drawn anyway.
            if not asset or source == "treasury_bond" or asset["status"] != "active" or not _reached(conn, order, now):
                continue
            treasury = CurrencyManager.exchange_treasury(conn, order["asset_code"])
            if str(order["user_id"]) == treasury:
                continue
            price = from_minor_units(int(order["limit_price_minor_units"]), _places(conn, order))
            currency = str(order["quote_currency"] or base_currency_code(conn))
            units_per_minor = SCALE // (10 ** _places(conn, order))
            left = int(order["remaining_quantity"])
            if order["side"] == "sell":
                counter = _treasury_order(conn, treasury, order, "buy", left, now)
                # A treasury order reserves nothing; it pays when it fills.
                CurrencyManager.treasury_pay(conn, treasury, currency,
                                             int(order["limit_price_minor_units"]) * left * units_per_minor)
                _secondary_fill(book, conn, counter, order, left, price, source, now)
                filled.append((int(order["order_id"]), left))
                continue
            held = int(conn.execute("SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets "
                                    "WHERE user_id=? AND asset_code=? AND account_number IS NULL",
                                    (treasury, order["asset_code"])).fetchone()[0])
            done = 0
            if held:
                take = min(left, held)
                counter = _treasury_order(conn, treasury, order, "sell", take, now)
                _secondary_fill(book, conn, order, counter, take, price, source, now)
                order = conn.execute("SELECT * FROM assets.market_orders WHERE order_id=?",
                                     (order["order_id"],)).fetchone()
                done, left = take, left - take
            unissued = _unissued(conn, asset) if left else 0
            take = left if unissued is None else min(left, unissued)
            if take:
                _primary_fill(book, conn, order, asset, source, take, price, now)
                done += take
            if done:
                filled.append((int(order["order_id"]), done))
            if source == "company":
                book.user_manager._refresh_max_shareholder(conn, order["asset_code"])
    return filled
