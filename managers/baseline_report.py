"""Where the books rest on a declared baseline instead of a linked record, and what a past wallet balance was.

Every ledger that could not reconstruct an old balance from records keeps an explicit baseline (a table of rows or a state
marker). This lists them all in one place with their size, so the close gate shows how much of the history is declared and
how much is proven, and a wallet balance at any past time can be rebuilt from the wallet ledger.
"""

LABELS = {
    "customer_wallet_baseline_adjustments": "고객 지갑 기준선 조정(근거가 있는 과거 해지 계좌 정리)",
    "financial_amount_snapshot_retirements": "삭제된 행의 금액 스냅샷을 근거와 함께 정리한 기록",
    "market_execution_primary_recipient_overrides": "체결 대금 수취인을 기록으로 바로잡은 예외",
    "business_cash_baseline_values": "선물·스왑 상품이 이미 들고 있던 현금 기준값",
    "market_order_wallet_baselines": "주문 예약금과 지갑을 잇기 전의 기존 주문",
    "market_execution_order_baselines": "체결 전기 이전의 기존 주문",
    "market_execution_order_baselines_v2": "체결 전기 이전의 기존 주문(2차)",
    "market_execution_journal_baselines": "체결 분개 이전의 기존 체결",
    "market_execution_holding_baselines": "체결 보유 연결 이전의 기존 보유",
    "treasury_bond_cash_link_baselines": "국채 지급과 지갑을 잇기 전의 기존 지급",
    "tax_transaction_wallet_baselines": "세금과 지갑을 잇기 전의 기존 세금 거래",
    "option_exercise_link_baseline": "옵션 행사 연결 이전의 기존 행사",
    "option_exercise_asset_baseline": "옵션 행사 자산 연결 이전의 기존 행사",
    "option_order_fill_baselines": "옵션 체결 전기 이전의 기존 체결",
}
STATE_TABLES = ("treasury_bond_cash_link_baseline_state", "tax_transaction_wallet_baseline_state",
                "dividend_payment_settlement_baseline_state")


class BaselineReport:
    @staticmethod
    def _tables(conn):
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return sorted(name for name in names if name in LABELS or name in STATE_TABLES or "baseline" in name)

    @classmethod
    def collect(cls, conn):
        """One row per baseline table: {table, label, rows}. Tables without rows are listed too (declared, not used)."""
        result = []
        for table in cls._tables(conn):
            count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            result.append({"table": table, "label": LABELS.get(table, "기준 상태 표시" if table in STATE_TABLES else table), "rows": count})
        return result

    @staticmethod
    def wallet_as_of(conn, user_id, currency, epoch):
        """A wallet's balance (minor units) at ``epoch``, rebuilt from the wallet ledger, with how it can be trusted.

        ``baselined`` is true when a baseline adjustment for the wallet falls at or before ``epoch``: the balance then rests
        partly on that declared adjustment instead of linked records alone."""
        currency, epoch = str(currency).upper(), int(epoch)
        total = conn.execute("SELECT COALESCE(SUM(delta_minor_units),0) FROM customer_wallet_ledger WHERE user_id=? AND currency_code=? "
                             "AND recorded_epoch<=?", (str(user_id), currency, epoch)).fetchone()[0]
        adjusted = conn.execute("SELECT 1 FROM customer_wallet_baseline_adjustments a JOIN customer_wallet_ledger w ON w.entry_id=a.wallet_entry_id "
                                "WHERE w.user_id=? AND w.currency_code=? AND w.recorded_epoch<=? LIMIT 1",
                                (str(user_id), currency, epoch)).fetchone()
        return {"balance": int(total), "baselined": bool(adjusted)}
