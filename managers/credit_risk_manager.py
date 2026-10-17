"""Lightweight forward-looking loan loss estimates for the bank simulation."""

from datetime import datetime, timezone
import json
import math

from functions.money import currency_precision, round_currency
from managers.rate_manager import RateManager


class CreditRiskManager:
    """Estimate 12-month/lifetime ECL and update the bank's impairment allowance.

    This first pass deliberately uses borrower data already captured by the loan
    workflow. It is an explainable scorecard, not a bureau or regulatory model.
    """
    STAGE_2_CURE_DAYS = 90

    @classmethod
    def backtest_12m(cls, conn, nationality=None, *, issuer=None, now=None):
        """Compare each loan's latest matured PD snapshot with 12-month defaults.

        This is a compact calibration diagnostic, not an independent validation:
        snapshots are sampled from the simulator's own scorecard and defaults.
        """
        import time
        epoch = float(time.time() if now is None else now)
        if not math.isfinite(epoch) or epoch < 0:
            raise ValueError("invalid_ecl_backtest_epoch")
        issuer = str(issuer or "").strip() or None
        horizon = 365*86400
        rows = conn.execute(
            "WITH ranked AS ("
            " SELECT a.loan_id,a.pd_12m,a.calculated_epoch,l.defaulted_epoch,u.nationality,p.issuer,"
            " ROW_NUMBER() OVER (PARTITION BY a.loan_id "
            " ORDER BY a.calculated_epoch DESC,a.assessment_id DESC) AS month_rank "
            " FROM loan_ecl_assessments a "
            " JOIN loans l ON l.loan_id=a.loan_id "
            " JOIN users u ON u.user_id=l.user_id "
            " JOIN liability p ON p.product_code=l.product_code "
            " WHERE (? IS NULL OR p.issuer=?) AND a.stage IN (1,2) AND a.calculated_epoch>? "
            " AND a.calculated_epoch<=? AND (? IS NULL OR u.nationality=?)"
            ") SELECT * FROM ranked WHERE month_rank=1 "
            "ORDER BY calculated_epoch DESC,loan_id LIMIT 5000",
            (issuer, issuer, epoch-6*horizon, epoch-horizon,
             nationality, nationality),
        ).fetchall()
        bands = ((0.0, .01, "<1%"), (.01, .05, "1–5%"),
                 (.05, .20, "5–20%"), (.20, 1.000001, "≥20%"))
        grouped = {}
        for row in rows:
            pd = cls._bounded(row["pd_12m"], 0, 1)
            band = next(label for low, high, label in bands if low <= pd < high)
            key = (str(row["issuer"] or ""), str(row["nationality"] or ""), band)
            stats = grouped.setdefault(key, {"count": 0, "defaults": 0,
                                               "predicted": 0.0, "brier": 0.0})
            default_epoch = row["defaulted_epoch"]
            observed = int(default_epoch is not None
                           and row["calculated_epoch"] < float(default_epoch)
                           <= row["calculated_epoch"]+horizon)
            stats["count"] += 1
            stats["defaults"] += observed
            stats["predicted"] += pd
            stats["brier"] += (pd-observed)**2
        result = []
        for (bank, country, band), stats in sorted(grouped.items()):
            count = stats["count"]
            observed_rate = stats["defaults"]/count
            z = 1.959963984540054
            denominator = 1+(z*z/count)
            center = (observed_rate+(z*z/(2*count)))/denominator
            margin = z*math.sqrt(
                observed_rate*(1-observed_rate)/count+(z*z/(4*count*count))
            )/denominator
            result.append({"issuer": bank, "nationality": country, "pdBand": band,
                           "sampleCount": count, "observedDefaults": stats["defaults"],
                           "meanPredictedPd": stats["predicted"]/count,
                           "observedDefaultRate": observed_rate,
                           "observedRateLower95": max(0.0, center-margin),
                           "observedRateUpper95": min(1.0, center+margin),
                           "brierScore": stats["brier"]/count})
        return {"issuer": issuer, "horizonDays": 365,
                "maturedThroughEpoch": epoch-horizon, "groups": result,
                "method": "most recent matured Stage 1/2 assessment per loan; includes only snapshots with a full 365-day outcome window; outcome is recorded default within the following 12 months; capped at 5000 loans; Wilson intervals are loan-level approximations and do not cluster multiple loans by borrower; simulator-only calibration diagnostic"}


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

    @classmethod
    def _macro_pd_multiplier(cls, conn, nationality):
        macro = conn.execute(
            "SELECT inflation_percent,real_gdp_growth_percent,unemployment_percent "
            "FROM country_macro_indicators WHERE nationality=?",
            (str(nationality or ""),),
        ).fetchone()
        inflation = float(macro["inflation_percent"] if macro else 2.0)
        growth = float(macro["real_gdp_growth_percent"] if macro else 2.0)
        unemployment = float(macro["unemployment_percent"] if macro else 5.0)
        return cls._bounded(1.0-.08*(growth-2.0)+.04*(unemployment-5.0)
                            +.02*(inflation-2.0), 0.5, 2.5)

    @classmethod
    def origination_pd_12m(cls, conn, nationality, credit_score, dti, dsr, ltv):
        """Persist a comparable, macro-adjusted PD baseline for SICR monitoring."""
        base = cls._pd_12m(credit_score, dti, dsr, 0, ltv, False)
        return cls._bounded(base*cls._macro_pd_multiplier(conn, nationality), 0.0001, 1.0)

    @staticmethod
    def _stage(status, collection_status, missed_payments, ltv, overdue,
               overdue_days=None, origination_pd_12m=None, current_pd_12m=None):
        """Classify simulator ECL stages; an already-defaulted exposure is Stage 3.

        Thirty days past due is the arrears-based significant-increase trigger.
        If an older record has no delinquency start timestamp, retain the prior
        conservative behavior and treat its overdue state as Stage 2.
        """
        if status == "defaulted" or collection_status == "defaulted":
            return 3
        arrears_trigger = overdue and (
            overdue_days is None or float(overdue_days) >= 30.0
        )
        pd_deterioration = (
            origination_pd_12m is not None and current_pd_12m is not None
            and float(origination_pd_12m) > 0
            and float(current_pd_12m) >= 2.0*float(origination_pd_12m)
        )
        if (arrears_trigger or pd_deterioration or int(missed_payments or 0) >= 2
                or CreditRiskManager._bounded(ltv, 0, 10) >= 1.0):
            return 2
        return 1

    @classmethod
    def _apply_stage_cure_period(cls, conn, loan_id, proposed_stage, now):
        """Require 90 days of continuously improved indicators before Stage 1 cure."""
        loan = conn.execute(
            "SELECT stage_cure_since_epoch FROM accounts.loans WHERE loan_id=?",
            (int(loan_id),),
        ).fetchone()
        if not loan:
            return proposed_stage
        cure_since = loan["stage_cure_since_epoch"]
        if proposed_stage != 1:
            if cure_since is not None:
                conn.execute(
                    "UPDATE accounts.loans SET stage_cure_since_epoch=NULL WHERE loan_id=?",
                    (int(loan_id),),
                )
            return proposed_stage
        prior = conn.execute(
            "SELECT stage FROM accounts.loan_ecl_assessments WHERE loan_id=? "
            "ORDER BY calculated_epoch DESC,assessment_id DESC LIMIT 1",
            (int(loan_id),),
        ).fetchone()
        if cure_since is None:
            if prior and int(prior["stage"]) == 2:
                conn.execute(
                    "UPDATE accounts.loans SET stage_cure_since_epoch=? WHERE loan_id=?",
                    (float(now), int(loan_id)),
                )
                return 2
            return 1
        if float(now)-float(cure_since) < cls.STAGE_2_CURE_DAYS*86400:
            return 2
        conn.execute(
            "UPDATE accounts.loans SET stage_cure_since_epoch=NULL WHERE loan_id=?",
            (int(loan_id),),
        )
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
    def portfolio_default_stress(cls, conn, issuer):
        """Estimate pro-rata portfolio loss under deterministic default-share shocks."""
        issuer = str(issuer or "").strip()
        if not issuer:
            raise ValueError("financial_institution_not_found")
        sheet = RateManager.institution_balance_sheet(conn, issuer)
        base = sheet["base_currency"]
        terms = RateManager.institution_components(conn, issuer)
        from managers.currency_manager import CurrencyManager, SCALE

        def to_base(amount, currency):
            amount = round_currency(conn, max(0.0, float(amount or 0)), currency)
            if currency == base:
                return amount
            units = int(round(float(amount)*SCALE))
            return CurrencyManager.convert(conn, units, currency, base)/SCALE

        loans = conn.execute(
            "SELECT l.loan_id,l.outstanding_balance,l.quote_currency,l.collateral_amount,"
            "l.collateral_released,l.collateral_seized,l.ecl_allowance,l.status,l.collection_status "
            "FROM loans l JOIN liability p ON p.product_code=l.product_code "
            "WHERE p.issuer=? AND l.status='active' AND l.collection_status<>'defaulted' "
            "AND l.outstanding_balance>0", (issuer,),
        ).fetchall()
        performing_ead = expected_loss = allowance = 0.0
        for loan in loans:
            currency = str(loan["quote_currency"] or "NWX")
            ead = max(0.0, float(loan["outstanding_balance"] or 0))
            loss = ead*cls._lgd(conn, loan)
            performing_ead += to_base(ead, currency)
            expected_loss += to_base(loss, currency)
            allowance += to_base(loan["ecl_allowance"], currency)
        scenarios = []
        for percent in (5, 10, 20, 40):
            stressed_ead = round_currency(conn, performing_ead*percent/100, base)
            gross_loss = round_currency(conn, expected_loss*percent/100, base)
            allowance_applied = min(gross_loss, round_currency(conn, allowance*percent/100, base))
            incremental = round_currency(conn, max(0.0, gross_loss-allowance_applied), base)
            equity_after = round_currency(
                conn, float(terms["effective_equity"])-incremental, base
            )
            scenarios.append({
                "defaultSharePercent": percent,
                "defaultedEad": stressed_ead,
                "grossLoss": gross_loss,
                "existingAllowanceApplied": allowance_applied,
                "incrementalProvision": incremental,
                "equityAfterProvision": equity_after,
                "capitalConsumedPercent": (
                    incremental/float(terms["effective_equity"])*100
                    if terms["effective_equity"] > 0 else None
                ),
            })
        return {"issuer": issuer, "currency": base,
                "decimalPlaces": currency_precision(conn, base),
                "performingExposureCount": len(loans),
                "performingEad": round_currency(conn, performing_ead, base),
                "estimatedFullDefaultLoss": round_currency(conn, expected_loss, base),
                "existingAllowance": round_currency(conn, allowance, base),
                "scenarios": scenarios,
                "method": "deterministic pro-rata default share of performing loans; collateral-adjusted LGD; existing allowance applied pro-rata; no default correlation, recovery timing, macroeconomic feedback or tax effects"}

    @staticmethod
    def _ead_at_expected_loss(conn, loan_id, balance, as_of_epoch, loss_time_years,
                              stage, currency):
        """Proxy exposure at midpoint after scheduled, not-yet-due principal payments."""
        current = max(0.0, float(balance or 0))
        if stage == 3:
            return round_currency(conn, current, currency), "defaulted_current_balance"
        horizon = float(as_of_epoch)+max(0.0, float(loss_time_years))*365*86400
        rows = conn.execute(
            "SELECT principal_due,principal_paid FROM accounts.loan_payment_schedule "
            "WHERE loan_id=? AND due_epoch>? AND due_epoch<=? "
            "AND status IN ('scheduled','partial')",
            (int(loan_id), float(as_of_epoch), horizon),
        ).fetchall()
        scheduled_principal = sum(
            max(0.0, float(row["principal_due"] or 0)-float(row["principal_paid"] or 0))
            for row in rows
        )
        if not rows:
            return round_currency(conn, current, currency), "current_balance_no_future_schedule"
        projected = round_currency(conn, max(0.0, current-scheduled_principal), currency)
        return projected, "midpoint_after_scheduled_principal"

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
            "l.contract_rate,l.contract_effective_rate,"
            "l.origination_pd_12m,"
            "u.credit_score,u.nationality,p.issuer,p.maturity_days,p.interest_rate product_rate "
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
            macro_pd_multiplier = cls._macro_pd_multiplier(conn, loan["nationality"])
            pd_12m = cls._bounded(pd_12m*macro_pd_multiplier, 0.0, 1.0)
            lgd = cls._lgd(conn, loan)
            term_days = max(1, int(loan["maturity_days"] or 365))
            remaining_days = max(1.0, term_days)
            if loan["maturity_epoch"] is not None:
                remaining_days = max(1.0, (float(loan["maturity_epoch"])-epoch)/86400.0)
            years = max(1.0/365.0, remaining_days/365.0)
            lifetime_pd = cls._bounded(1.0-(1.0-pd_12m)**years, 0.0, 1.0)
            stage = cls._stage(
                loan["status"], loan["collection_status"], loan["missed_payment_count"],
                loan["current_ltv"], overdue,
                (max(0.0, (epoch-float(loan["overdue_since_epoch"]))/86400.0)
                 if loan["overdue_since_epoch"] is not None else None),
                loan["origination_pd_12m"], pd_12m,
            )
            stage = cls._apply_stage_cure_period(conn, loan["loan_id"], stage, epoch)
            if stage == 3:
                pd_12m = lifetime_pd = 1.0
            base_pd = lifetime_pd if stage == 2 else pd_12m
            # Discount expected shortfalls at the contract EIR. The expected
            # timing is a transparent midpoint proxy (not a cash-flow model).
            expected_loss_time = (
                min(0.5, years/2) if stage == 1 else
                0.5 if stage == 3 else years/2
            )
            raw_eir = loan["contract_effective_rate"]
            if raw_eir is None:
                raw_eir = loan["contract_rate"]
            if raw_eir is None:
                raw_eir = loan["product_rate"]
            eir_percent = cls._bounded(raw_eir, -99.0, 1000.0) if raw_eir is not None else 0.0
            discount_factor = (1.0+eir_percent/100.0)**(-expected_loss_time)
            if not math.isfinite(discount_factor) or discount_factor <= 0:
                discount_factor = 1.0
            ead, ead_method = cls._ead_at_expected_loss(
                conn, loan["loan_id"], loan["outstanding_balance"], epoch,
                expected_loss_time, stage, loan["quote_currency"],
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
                raw_expected_loss += weight*ead*scenario_pd*scenario_lgd*discount_factor
                scenario_details.append({"key": scenario["scenario_key"],
                                         "weight": weight, "pd": scenario_pd,
                                         "lgd": scenario_lgd,
                                         "discountFactor": discount_factor,
                                         "macroPdMultiplier": macro_pd_multiplier,
                                         "effectiveRatePercent": eir_percent,
                                         "expectedLossTimeYears": expected_loss_time,
                                         "eadMethod": ead_method})
            expected_loss = round_currency(
                conn, raw_expected_loss, loan["quote_currency"]
            )
            previous_allowance = max(0.0, float(loan["ecl_allowance"] or 0))
            allowance_change = float(expected_loss)-previous_allowance
            cursor = conn.execute(
                "INSERT OR IGNORE INTO accounts.loan_ecl_assessments "
                "(loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,"
                "quote_currency,scenario,discount_factor,effective_interest_rate,"
                "expected_loss_time_years,macro_pd_multiplier,calculated_epoch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (loan["loan_id"], as_of_day, stage, pd_12m, lifetime_pd, lgd,
                 ead, expected_loss, loan["quote_currency"],
                 json.dumps(scenario_details, separators=(",", ":")),
                 discount_factor, eir_percent, expected_loss_time,
                 macro_pd_multiplier, epoch),
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
