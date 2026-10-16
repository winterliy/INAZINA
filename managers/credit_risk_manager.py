"""Lightweight forward-looking loan loss estimates for the bank simulation."""

from datetime import datetime, timezone
import math

from functions.money import round_currency
from managers.rate_manager import RateManager


class CreditRiskManager:
    """Estimate 12-month/lifetime ECL and update the bank's impairment allowance.

    This first pass deliberately uses borrower data already captured by the loan
    workflow. It is an explainable scorecard, not a bureau or regulatory model.
    """

    @staticmethod
    def _bounded(value, low, high):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return low
        return max(low, min(high, number)) if math.isfinite(number) else low

    @classmethod
    def _pd_12m(cls, credit_score, dti, dsr, missed_payments, ltv, overdue):
        score = cls._bounded(credit_score, 0, 1000)
        score_risk = (max(0.0, 1000.0-score)/1000.0) ** 3
        pd = 0.002 + 0.35*score_risk
        pd *= 1.0 + min(2.0, cls._bounded(dti, 0, 200)/100.0)
        pd *= 1.0 + min(2.0, cls._bounded(dsr, 0, 200)/100.0)
        pd *= 1.0 + .25*min(4, max(0, int(missed_payments or 0)))
        if cls._bounded(ltv, 0, 10) >= .9:
            pd *= 1.5
        if overdue:
            pd *= 1.75
        return cls._bounded(pd, 0.0001, 0.95)

    @classmethod
    def _lgd(cls, conn, loan):
        ead = max(0.0, float(loan["outstanding_balance"] or 0))
        if ead <= 0:
            return 0.0
        cash_collateral = max(
            0.0,
            float(loan["collateral_amount"] or 0)
            - float(loan["collateral_released"] or 0)
            - float(loan["collateral_seized"] or 0),
        )
        asset_rows = conn.execute(
            "SELECT COALESCE(SUM(last_value),0) amount FROM loan_collateral_assets "
            "WHERE loan_id=? AND status='pledged'",
            (loan["loan_id"],),
        ).fetchone()
        asset_collateral = max(0.0, float(asset_rows["amount"] or 0))*.80
        secured = min(ead, cash_collateral+asset_collateral)
        unsecured = max(0.0, ead-secured)
        expected_recovery = secured+unsecured*.35
        return cls._bounded(1.0-expected_recovery/ead, 0.0, 1.0)

    @classmethod
    def assess_conn(cls, conn, now, *, user_id=None, product_code=None):
        """Book at most one assessment per active loan per UTC day."""
        epoch = float(now)
        as_of_day = datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()
        loans = conn.execute(
            "SELECT l.loan_id,l.user_id,l.product_code,l.outstanding_balance,l.quote_currency,"
            "l.collateral_amount,l.collateral_released,l.collateral_seized,l.underwriting_dti,"
            "l.underwriting_dsr,l.current_ltv,l.missed_payment_count,l.overdue_since_epoch,"
            "l.collection_status,l.maturity_epoch,l.created_at,l.ecl_allowance,"
            "u.credit_score,p.issuer,p.maturity_days "
            "FROM accounts.loans l JOIN users.users u ON u.user_id=l.user_id "
            "JOIN product.liability p ON p.product_code=l.product_code "
            "WHERE l.status='active' AND (? IS NULL OR l.user_id=?) "
            "AND (? IS NULL OR l.product_code=?)",
            (user_id, user_id, product_code, product_code),
        ).fetchall()
        assessed = 0
        for loan in loans:
            if conn.execute(
                "SELECT 1 FROM accounts.loan_ecl_assessments "
                "WHERE loan_id=? AND as_of_day=?",
                (loan["loan_id"], as_of_day),
            ).fetchone():
                continue
            issuer = str(loan["issuer"] or "").strip()
            if not issuer or float(loan["outstanding_balance"] or 0) <= 0:
                continue
            overdue = bool(loan["overdue_since_epoch"] is not None
                           or loan["collection_status"] in {"grace", "overdue", "accelerated"})
            pd_12m = cls._pd_12m(
                loan["credit_score"], loan["underwriting_dti"], loan["underwriting_dsr"],
                loan["missed_payment_count"], loan["current_ltv"], overdue,
            )
            lgd = cls._lgd(conn, loan)
            term_days = max(1, int(loan["maturity_days"] or 365))
            years = max(1.0/365.0, term_days/365.0)
            lifetime_pd = cls._bounded(1.0-(1.0-pd_12m)**years, 0.0, 1.0)
            stage = 2 if (overdue or int(loan["missed_payment_count"] or 0) >= 2
                          or cls._bounded(loan["current_ltv"], 0, 10) >= 1.0) else 1
            applied_pd = lifetime_pd if stage == 2 else pd_12m
            ead = max(0.0, float(loan["outstanding_balance"] or 0))
            expected_loss = round_currency(
                conn, ead*applied_pd*lgd, loan["quote_currency"]
            )
            previous_allowance = max(0.0, float(loan["ecl_allowance"] or 0))
            allowance_change = float(expected_loss)-previous_allowance
            cursor = conn.execute(
                "INSERT OR IGNORE INTO accounts.loan_ecl_assessments "
                "(loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,"
                "quote_currency,scenario,calculated_epoch) VALUES (?,?,?,?,?,?,?,?,?,'base',?)",
                (loan["loan_id"], as_of_day, stage, pd_12m, lifetime_pd, lgd,
                 ead, expected_loss, loan["quote_currency"], epoch),
            )
            if cursor.rowcount != 1:
                continue
            if allowance_change:
                RateManager.record_institution_result(
                    conn, issuer, -allowance_change, "expected_credit_loss",
                    f"loan:{loan['loan_id']}:ecl:{as_of_day}", now=epoch,
                    currency_code=loan["quote_currency"],
                )
                conn.execute(
                    "UPDATE accounts.loans SET ecl_allowance=? WHERE loan_id=?",
                    (expected_loss, loan["loan_id"]),
                )
            assessed += 1
        return assessed
