"""Dividend receipt workflow for internal fund owners."""

from decimal import Decimal, ROUND_HALF_UP

from database.fund_accounting import FundCashLedger, account

SCALE = 1_000_000


class FundDividendReceiver:
    """Post a fund's dividend receipt to its native-currency cash ledger."""

    def __init__(self, conn, ledger=None):
        self.conn = conn
        self.ledger = ledger or FundCashLedger(conn)

    def receive(self, owner, amount, *, currency_code=None, reference=None,
                recorded_epoch=None):
        fund = account(self.conn, owner)
        if not fund:
            raise ValueError("배당을 받을 펀드 계정을 찾을 수 없습니다.")
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0:
            raise ValueError("펀드 배당액은 0 이상의 유한한 금액이어야 합니다.")
        if currency_code is None:
            currency_code = self.conn.execute(
                "SELECT quote_currency FROM investment WHERE product_code=?",
                (fund["product_code"],),
            ).fetchone()[0]
        entry_id = self.ledger.deposit(
            owner, value, currency_code=currency_code, event="dividend",
            reference=reference, recorded_epoch=recorded_epoch,
        )
        return {
            "entry_id": entry_id,
            "product_code": str(fund["product_code"]),
            "currency_code": str(currency_code).upper(),
            "net_amount_units": int((value * SCALE).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)),
        }
