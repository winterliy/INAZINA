"""Lightweight forward-looking loan loss estimates for the bank simulation."""

from datetime import datetime, timezone
import json
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

    @staticmethod
    def configure_scenarios(conn, nationality, actor_id, scenarios, *, now=None):
        """Replace the three country ECL scenarios as one audited policy change."""
        import time
        country = str(nationality or "").strip()
        from managers.compliance_manager import ComplianceManager
        ComplianceManager._require_admin(conn, actor_id)
        keys = {"optimistic", "base", "downside"}
        if not country or not isinstance(scenarios, list) or len(scenarios) != 3:
            raise ValueError("국적과 낙관·기준·하방 세 시나리오를 모두 입력해주세요.")
        normalized = {}
        for item in scenarios:
            if not isinstance(item, dict) or set(item) != {"key", "probability", "pdMultiplier", "lgdMultiplier"}:
                raise ValueError("ECL 시나리오 필드가 올바르지 않습니다.")
            key = str(item["key"])
            if key not in keys or key in normalized:
                raise ValueError("ECL 시나리오 종류가 중복되거나 잘못되었습니다.")
            values = []
            for field, low, high in (("probability", 0, 1), ("pdMultiplier", 0, 20), ("lgdMultiplier", 0, 5)):
                if isinstance(item[field], bool):
                    raise ValueError("시나리오 확률·배수는 숫자여야 합니다.")
                try:
                    value = float(item[field])
                except (TypeError, ValueError):
                    raise ValueError("시나리오 확률·배수는 숫자여야 합니다.") from None
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError("시나리오 확률은 0~1, PD 배수는 0~20, LGD 배수는 0~5여야 합니다.")
                values.append(value)
            normalized[key] = values
        if set(normalized) != keys or sum(values[0] for values in normalized.values()) <= 0:
            raise ValueError("세 시나리오 확률의 합은 0보다 커야 합니다.")
        if not conn.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (country,)).fetchone():
            raise ValueError("국적을 찾을 수 없습니다.")
        epoch = float(time.time() if now is None else now)
        for key, (probability, pd_multiplier, lgd_multiplier) in normalized.items():
            changed = conn.execute(
                "UPDATE credit_risk_scenarios SET probability=?,pd_multiplier=?,lgd_multiplier=?,updated_epoch=? "
                "WHERE nationality=? AND scenario_key=?",
                (probability, pd_multiplier, lgd_multiplier, epoch, country, key),
            )
            if changed.rowcount != 1:
                raise ValueError("ECL 시나리오 설정이 초기화되지 않았습니다. 국가 설정을 확인해주세요.")
        conn.execute(
            "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
            (str(actor_id), "credit_risk.scenarios.configure",
             json.dumps({"nationality": country, "scenarios": normalized}, separators=(",", ":")), epoch),
        )
        return {"nationality": country, "updated": len(normalized)}

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

    @staticmethod
    def _stage(status, collection_status, missed_payments, ltv, overdue):
        """Classify simulator ECL stages; an already-defaulted exposure is Stage 3."""
        if status == "defaulted" or collection_status == "defaulted":
            return 3
        if overdue or int(missed_payments or 0) >= 2 or CreditRiskManager._bounded(ltv, 0, 10) >= 1.0:
            return 2
        return 1

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
            "l.collection_status,l.status,l.maturity_epoch,l.created_at,l.ecl_allowance,"
            "u.credit_score,u.nationality,p.issuer,p.maturity_days "
            "FROM accounts.loans l JOIN users.users u ON u.user_id=l.user_id "
            "JOIN product.liability p ON p.product_code=l.product_code "
            "WHERE l.status IN ('active','defaulted') AND (? IS NULL OR l.user_id=?) "
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
            stage = cls._stage(
                loan["status"], loan["collection_status"], loan["missed_payment_count"],
                loan["current_ltv"], overdue,
            )
            if stage == 3:
                pd_12m = lifetime_pd = 1.0
            base_pd = lifetime_pd if stage == 2 else pd_12m
            ead = round_currency(
                conn, max(0.0, float(loan["outstanding_balance"] or 0)),
                loan["quote_currency"],
            )
            scenarios = conn.execute(
                "SELECT scenario_key,probability,pd_multiplier,lgd_multiplier "
                "FROM credit_risk_scenarios WHERE nationality=?",
                (str(loan["nationality"] or ""),),
            ).fetchall()
            total_weight = sum(max(0.0, float(row["probability"] or 0))
                               for row in scenarios)
            if total_weight <= 0:
                scenarios = [{"scenario_key": "base", "probability": 1.0,
                              "pd_multiplier": 1.0, "lgd_multiplier": 1.0}]
                total_weight = 1.0
            scenario_details = []
            raw_expected_loss = 0.0
            for scenario in scenarios:
                weight = max(0.0, float(scenario["probability"] or 0))/total_weight
                scenario_pd = (1.0 if stage == 3 else cls._bounded(
                    base_pd*float(scenario["pd_multiplier"] or 0), 0.0, 1.0
                ))
                scenario_lgd = cls._bounded(
                    lgd*float(scenario["lgd_multiplier"] or 0), 0.0, 1.0
                )
                raw_expected_loss += weight*ead*scenario_pd*scenario_lgd
                scenario_details.append({"key": scenario["scenario_key"],
                                         "weight": weight, "pd": scenario_pd,
                                         "lgd": scenario_lgd})
            expected_loss = round_currency(
                conn, raw_expected_loss, loan["quote_currency"]
            )
            previous_allowance = max(0.0, float(loan["ecl_allowance"] or 0))
            allowance_change = float(expected_loss)-previous_allowance
            cursor = conn.execute(
                "INSERT OR IGNORE INTO accounts.loan_ecl_assessments "
                "(loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,"
                "quote_currency,scenario,calculated_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (loan["loan_id"], as_of_day, stage, pd_12m, lifetime_pd, lgd,
                 ead, expected_loss, loan["quote_currency"],
                 json.dumps(scenario_details, separators=(",", ":")), epoch),
            )
            if cursor.rowcount != 1:
                continue
            if allowance_change:
                RateManager.record_institution_result(
                    conn, issuer, -allowance_change, "expected_credit_loss",
                    f"loan:{loan['loan_id']}:ecl:{as_of_day}", now=epoch,
                    currency_code=loan["quote_currency"],
                )
                from managers.bank_journal import BankJournal
                if allowance_change > 0:
                    lines = [("impairment_expense", "debit", allowance_change),
                             ("ecl_allowance", "credit", allowance_change)]
                else:
                    reversal = -allowance_change
                    lines = [("ecl_allowance", "debit", reversal),
                             ("impairment_reversal", "credit", reversal)]
                BankJournal.post(
                    conn, issuer, "expected_credit_loss",
                    f"loan:{loan['loan_id']}:ecl:{as_of_day}", loan["quote_currency"],
                    lines, description=f"ECL allowance reassessment for loan {loan['loan_id']}",
                    now=epoch,
                )
                conn.execute(
                    "UPDATE accounts.loans SET ecl_allowance=? WHERE loan_id=?",
                    (expected_loss, loan["loan_id"]),
                )
            assessed += 1
        return assessed
