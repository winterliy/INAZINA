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
    def charge_off_loan(cls, conn, *, loan_id, event_key, actor_id, now=None, memo=""):
        """Write off a defaulted loan against its ECL allowance, then loss expense.

        The borrower's legal obligation is retained in ``charged_off_balance`` as
        an off-balance memorandum amount; this operation only removes the gross
        receivable from recognized assets. Call inside the DB transaction.
        """
        from managers.compliance_manager import ComplianceManager
        from functions.money import currency_precision, to_minor_units
        from managers.bank_journal import BankJournal

        ComplianceManager._require_admin(conn, actor_id)
        key = str(event_key or "").strip()
        if not key or len(key) > 160:
            raise ValueError("상각 참조 키가 올바르지 않습니다.")
        try:
            loan_id = int(loan_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("대출 식별자가 올바르지 않습니다.") from None
        if isinstance(loan_id, bool) or loan_id <= 0:
            raise ValueError("대출 식별자가 올바르지 않습니다.")
        prior = conn.execute(
            "SELECT event_id FROM accounts.loan_chargeoff_events WHERE event_key=?", (key,)
        ).fetchone()
        if prior:
            return {"eventId": int(prior["event_id"]), "duplicate": True}
        epoch = RateManager._validated_epoch(now, "invalid_loan_chargeoff_epoch")
        loan = conn.execute("""SELECT l.loan_id,l.outstanding_balance,l.ecl_allowance,
            l.quote_currency,l.status,l.collection_status,p.issuer
            FROM accounts.loans l JOIN product.liability p USING(product_code)
            WHERE l.loan_id=?""", (loan_id,)).fetchone()
        if (not loan or loan["status"] != "defaulted"
                or loan["collection_status"] != "defaulted"):
            raise ValueError("부도 상태인 대출만 상각할 수 있습니다.")
        currency = str(loan["quote_currency"] or "NWX").upper()
        places = currency_precision(conn, currency)
        gross = to_minor_units(max(0, loan["outstanding_balance"] or 0), places)
        allowance = min(gross, to_minor_units(max(0, loan["ecl_allowance"] or 0), places))
        if gross <= 0:
            raise ValueError("상각할 대출 잔액이 없습니다.")
        excess_allowance = max(0, to_minor_units(max(0, loan["ecl_allowance"] or 0), places)-allowance)
        loss = gross-allowance
        conn.execute("""INSERT INTO accounts.loan_chargeoff_events
            (loan_id,issuer,event_key,event_type,amount_minor_units,decimal_places,
             currency_code,actor_id,created_epoch,memo)
            VALUES (?,?,?,'charge_off',?,?,?,?,?,?)""",
            (loan_id, str(loan["issuer"]), key, gross, places, currency,
             str(actor_id), epoch, str(memo or "")[:500]))
        conn.execute("""UPDATE accounts.loans SET outstanding_balance=0,
            charged_off_balance=charged_off_balance+?,ecl_allowance=0,status='charged_off',
            collection_status='charged_off',nonaccrual_since_epoch=COALESCE(nonaccrual_since_epoch,?)
            WHERE loan_id=?""", (float(loan["outstanding_balance"] or 0), epoch, loan_id))
        reference = f"loan:{loan_id}:charge-off:{key}"
        lines = []
        if allowance:
            lines.append(("ecl_allowance", "debit", allowance / (10 ** places)))
        if loss:
            lines.append(("credit_loss_expense", "debit", loss / (10 ** places)))
        lines.append(("loans_receivable", "credit", gross / (10 ** places)))
        BankJournal.post(conn, loan["issuer"], "loan_charge_off", reference, currency,
                         lines, description=f"Loan {loan_id} charge-off", now=epoch)
        if loss:
            RateManager.record_institution_result(
                conn, loan["issuer"], -(loss / (10 ** places)), "credit_loss_expense",
                reference, now=epoch, currency_code=currency,
            )
        if excess_allowance:
            excess = excess_allowance / (10 ** places)
            excess_ref = f"{reference}:excess-allowance"
            BankJournal.post(conn, loan["issuer"], "ecl_allowance_release", excess_ref,
                             currency, [("ecl_allowance", "debit", excess),
                                        ("impairment_reversal", "credit", excess)],
                             description=f"Excess ECL allowance release for loan {loan_id}",
                             now=epoch)
            RateManager.record_institution_result(
                conn, loan["issuer"], excess, "expected_credit_loss_release", excess_ref,
                now=epoch, currency_code=currency,
            )
        ComplianceManager._audit(conn, actor_id, "credit_risk.loan_charge_off", {
            "loan_id": loan_id, "event_key": key, "gross_minor_units": gross,
            "allowance_minor_units": allowance, "loss_minor_units": loss,
            "currency_code": currency,
        }, epoch)
        return {"eventId": int(conn.execute(
            "SELECT event_id FROM accounts.loan_chargeoff_events WHERE event_key=?", (key,)
        ).fetchone()[0]), "duplicate": False, "grossMinorUnits": gross,
            "allowanceMinorUnits": allowance, "lossMinorUnits": loss,
            "currencyCode": currency, "decimalPlaces": places}

    @classmethod
    def record_chargeoff_recovery(cls, conn, *, loan_id, amount, event_key,
                                  actor_id, now=None, memo=""):
        """Record cash recovered on a charged-off loan, bounded by its memo balance."""
        from managers.compliance_manager import ComplianceManager
        from functions.money import currency_precision, to_minor_units
        from managers.bank_journal import BankJournal

        ComplianceManager._require_admin(conn, actor_id)
        key = str(event_key or "").strip()
        if not key or len(key) > 160:
            raise ValueError("회수 참조 키가 올바르지 않습니다.")
        try:
            loan_id = int(loan_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("대출 식별자가 올바르지 않습니다.") from None
        if isinstance(loan_id, bool) or loan_id <= 0:
            raise ValueError("대출 식별자가 올바르지 않습니다.")
        prior = conn.execute(
            "SELECT event_id FROM accounts.loan_chargeoff_events WHERE event_key=?", (key,)
        ).fetchone()
        if prior:
            return {"eventId": int(prior["event_id"]), "duplicate": True}
        epoch = RateManager._validated_epoch(now, "invalid_loan_recovery_epoch")
        loan = conn.execute("""SELECT l.loan_id,l.charged_off_balance,l.quote_currency,
            l.status,p.issuer FROM accounts.loans l JOIN product.liability p USING(product_code)
            WHERE l.loan_id=?""", (loan_id,)).fetchone()
        if not loan or loan["status"] != "charged_off":
            raise ValueError("상각 완료된 대출만 회수할 수 있습니다.")
        currency = str(loan["quote_currency"] or "NWX").upper()
        places = currency_precision(conn, currency)
        amount_units = to_minor_units(amount, places)
        remaining = to_minor_units(loan["charged_off_balance"] or 0, places)
        if amount_units <= 0 or amount_units > remaining:
            raise ValueError("회수액은 양수이며 미회수 상각잔액 이하여야 합니다.")
        conn.execute("""INSERT INTO accounts.loan_chargeoff_events
            (loan_id,issuer,event_key,event_type,amount_minor_units,decimal_places,
             currency_code,actor_id,created_epoch,memo)
            VALUES (?,?,?,'recovery',?,?,?,?,?,?)""",
            (loan_id, str(loan["issuer"]), key, amount_units, places, currency,
             str(actor_id), epoch, str(memo or "")[:500]))
        conn.execute("UPDATE accounts.loans SET charged_off_balance=charged_off_balance-? WHERE loan_id=?",
                     (amount_units / (10 ** places), loan_id))
        value = amount_units / (10 ** places)
        reference = f"loan:{loan_id}:recovery:{key}"
        BankJournal.post(conn, loan["issuer"], "loan_chargeoff_recovery", reference,
                         currency, [("settlement_asset", "debit", value),
                                    ("credit_loss_recovery_income", "credit", value)],
                         description=f"Recovery on charged-off loan {loan_id}", now=epoch)
        RateManager.record_institution_result(
            conn, loan["issuer"], value, "credit_loss_recovery_income", reference,
            now=epoch, currency_code=currency,
        )
        ComplianceManager._audit(conn, actor_id, "credit_risk.loan_chargeoff_recovery", {
            "loan_id": loan_id, "event_key": key, "amount_minor_units": amount_units,
            "currency_code": currency,
        }, epoch)
        return {"eventId": int(conn.execute(
            "SELECT event_id FROM accounts.loan_chargeoff_events WHERE event_key=?", (key,)
        ).fetchone()[0]), "duplicate": False, "recoveryMinorUnits": amount_units,
            "remainingMinorUnits": remaining-amount_units, "currencyCode": currency,
            "decimalPlaces": places}

    @classmethod
    def configure_loan_credit_support(cls, conn, *, loan_id, party_user_id, actor_id,
                                     support_type, exposure_share_bps=0,
                                     guarantee_limit=0, quote_currency=None,
                                     guarantee_recovery_bps=10000, recovery_days=180,
                                     recovery_cost_bps=0, seniority_rank=1,
                                     underwriting_dti=None, underwriting_dsr=None,
                                     active=True, now=None):
        """Version credit support and co-borrower links through an audited admin action."""
        from managers.compliance_manager import ComplianceManager
        from functions.money import currency_precision, to_minor_units
        ComplianceManager._require_admin(conn, actor_id)
        epoch = RateManager._validated_epoch(now, "invalid_credit_support_epoch")
        try:
            loan_id = int(loan_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("대출 식별자가 올바르지 않습니다.") from None
        if isinstance(loan_id, bool) or loan_id <= 0:
            raise ValueError("대출 식별자가 올바르지 않습니다.")
        party = str(party_user_id or "").strip()
        kind = str(support_type or "")
        loan = conn.execute("SELECT loan_id,quote_currency FROM accounts.loans WHERE loan_id=?",
                            (loan_id,)).fetchone()
        if not loan or not party or not conn.execute(
                "SELECT 1 FROM users.users WHERE user_id=?", (party,)).fetchone():
            raise ValueError("대출 또는 지원 당사자를 찾을 수 없습니다.")
        if kind not in {"co_borrower", "guarantor", "connected_party"}:
            raise ValueError("신용지원 유형이 올바르지 않습니다.")
        def integer(value, name, low, high):
            if isinstance(value, bool):
                raise ValueError(f"{name} 값이 올바르지 않습니다.")
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{name} 값이 올바르지 않습니다.") from None
            if parsed != value or not low <= parsed <= high:
                raise ValueError(f"{name} 값이 범위를 벗어났습니다.")
            return parsed
        share = integer(exposure_share_bps, "공동차주 비율", 0, 10000)
        recovery = integer(guarantee_recovery_bps, "보증 회수율", 0, 10000)
        delay = integer(recovery_days, "회수기간", 0, 3650)
        cost = integer(recovery_cost_bps, "회수비용", 0, 10000)
        rank = integer(seniority_rank, "회수순위", 1, 100)
        def ratio(value, name):
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError(f"{name} 값이 올바르지 않습니다.")
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{name} 값이 올바르지 않습니다.") from None
            if not math.isfinite(parsed) or not 0 <= parsed <= 200:
                raise ValueError(f"{name} 값은 0~200% 범위여야 합니다.")
            return parsed
        dti = ratio(underwriting_dti, "공동차주 DTI")
        dsr = ratio(underwriting_dsr, "공동차주 DSR")
        if not isinstance(active, bool):
            raise ValueError("지원 상태는 boolean이어야 합니다.")
        currency = str(quote_currency or loan["quote_currency"] or "NWX").strip().upper()
        if kind == "co_borrower" and (share <= 0 or float(guarantee_limit or 0) != 0):
            raise ValueError("공동차주는 양수의 익스포저 비율만 설정할 수 있습니다.")
        if kind == "guarantor" and share != 0:
            raise ValueError("보증인은 공동차주 익스포저 비율을 설정할 수 없습니다.")
        if kind == "connected_party" and (share != 0 or float(guarantee_limit or 0) != 0):
            raise ValueError("연결차주는 집중도 식별용으로만 등록할 수 있습니다.")
        if kind == "guarantor" and currency != str(loan["quote_currency"]).upper():
            raise ValueError("보증통화는 대출 계약통화와 같아야 합니다.")
        places = currency_precision(conn, currency)
        limit_units = to_minor_units(guarantee_limit, places)
        if limit_units < 0 or (kind == "guarantor" and limit_units <= 0):
            raise ValueError("보증한도는 양수여야 합니다.")
        if kind != "guarantor" and limit_units != 0:
            raise ValueError("보증한도는 보증인 유형에만 설정할 수 있습니다.")
        if kind == "co_borrower":
            total = conn.execute("""SELECT COALESCE(SUM(exposure_share_bps),0)
                FROM accounts.loan_credit_support WHERE loan_id=? AND support_type='co_borrower'
                  AND active=1 AND NOT (party_user_id=?)""", (loan_id, party)).fetchone()[0]
            if int(total)+share > 10000:
                raise ValueError("공동차주 익스포저 비율의 합은 10000bp를 넘을 수 없습니다.")
        values = (share, dti, dsr, limit_units, currency, recovery, delay, cost, rank, int(active), epoch)
        prior = conn.execute("""SELECT support_id FROM accounts.loan_credit_support
            WHERE loan_id=? AND party_user_id=? AND support_type=?""",
            (loan_id, party, kind)).fetchone()
        if prior:
            support_id = int(prior[0])
            conn.execute("""UPDATE accounts.loan_credit_support SET exposure_share_bps=?,
                underwriting_dti=?,underwriting_dsr=?,guarantee_limit_minor_units=?,quote_currency=?,guarantee_recovery_bps=?,
                recovery_days=?,recovery_cost_bps=?,seniority_rank=?,active=?,updated_epoch=?
                WHERE support_id=?""", (*values, support_id))
            event_type = "updated" if active else "released"
        else:
            cursor = conn.execute("""INSERT INTO accounts.loan_credit_support
                (loan_id,party_user_id,support_type,exposure_share_bps,underwriting_dti,underwriting_dsr,
                 guarantee_limit_minor_units,quote_currency,guarantee_recovery_bps,recovery_days,recovery_cost_bps,
                 seniority_rank,active,created_epoch,updated_epoch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (loan_id, party, kind, *values[:-1], epoch, epoch))
            support_id = int(cursor.lastrowid)
            event_type = "created" if active else "released"
        snapshot = {"supportId": support_id, "loanId": loan_id, "partyUserId": party,
                    "supportType": kind, "exposureShareBps": share,
                    "underwritingDti": dti, "underwritingDsr": dsr,
                    "guaranteeLimitMinorUnits": limit_units, "currency": currency,
                    "guaranteeRecoveryBps": recovery, "recoveryDays": delay,
                    "recoveryCostBps": cost, "seniorityRank": rank, "active": active}
        conn.execute("""INSERT INTO accounts.loan_credit_support_events
            (support_id,loan_id,party_user_id,actor_id,event_type,snapshot,recorded_epoch)
            VALUES (?,?,?,?,?,?,?)""",
            (support_id, loan_id, party, str(actor_id), event_type,
             json.dumps(snapshot, sort_keys=True, separators=(",", ":")), epoch))
        conn.execute(
            "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
            (str(actor_id), "credit_risk.support.configure",
             json.dumps(snapshot, sort_keys=True, separators=(",", ":")), epoch),
        )
        return snapshot

    @staticmethod
    def configure_policy_override(conn, nationality, product_code, actor_id, *,
                                 pd_multiplier=1, grade_thresholds=None, now=None):
        """Configure jurisdiction-wide ('*') or product-specific scorecard settings."""
        from managers.compliance_manager import ComplianceManager
        from managers.rate_manager import RateManager
        ComplianceManager._require_admin(conn, actor_id)
        country = str(nationality or "").strip()
        product = str(product_code or "*").strip() or "*"
        if not country or not conn.execute(
                "SELECT 1 FROM country_currencies WHERE nationality=?", (country,)).fetchone():
            raise ValueError("관할 국가를 찾을 수 없습니다.")
        if product != "*" and not conn.execute(
                "SELECT 1 FROM product.liability WHERE product_code=?", (product,)).fetchone():
            raise ValueError("대출 상품을 찾을 수 없습니다.")
        if isinstance(pd_multiplier, bool):
            raise ValueError("PD 배수는 0.1~10 사이 숫자여야 합니다.")
        multiplier = float(pd_multiplier)
        if not math.isfinite(multiplier) or not 0.1 <= multiplier <= 10:
            raise ValueError("PD 배수는 0.1~10 사이 숫자여야 합니다.")
        keys = ("a", "b", "c", "d", "e", "f")
        thresholds = [0.005, 0.01, 0.025, 0.05, 0.10, 0.20]
        if grade_thresholds is not None:
            if not isinstance(grade_thresholds, dict) or set(grade_thresholds) != set(keys):
                raise ValueError("A~F PD 등급 기준 6개를 입력해야 합니다.")
            try:
                thresholds = [float(grade_thresholds[key]) for key in keys]
            except (TypeError, ValueError, OverflowError):
                raise ValueError("PD 등급 기준은 유한한 숫자여야 합니다.") from None
            if any(not math.isfinite(value) for value in thresholds) or any(
                    low <= 0 or high >= 1 or low >= high
                    for low, high in zip(thresholds, thresholds[1:])):
                raise ValueError("A~F PD 기준은 0과 1 사이에서 오름차순이어야 합니다.")
        epoch = RateManager._validated_epoch(now, "정책 적용 시각은 유효한 UNIX 시각이어야 합니다.")
        conn.execute("""INSERT INTO accounts.credit_risk_policy_overrides
            (nationality,product_code,pd_multiplier,grade_a_max,grade_b_max,grade_c_max,
             grade_d_max,grade_e_max,grade_f_max,updated_epoch,updated_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(nationality,product_code) DO UPDATE SET
             pd_multiplier=excluded.pd_multiplier,grade_a_max=excluded.grade_a_max,
             grade_b_max=excluded.grade_b_max,grade_c_max=excluded.grade_c_max,
             grade_d_max=excluded.grade_d_max,grade_e_max=excluded.grade_e_max,
             grade_f_max=excluded.grade_f_max,updated_epoch=excluded.updated_epoch,
             updated_by=excluded.updated_by""",
            (country, product, multiplier, *thresholds, epoch, str(actor_id)))
        conn.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
                     (str(actor_id), "credit_risk.policy.configure",
                      json.dumps({"nationality": country, "productCode": product,
                                  "pdMultiplier": multiplier, "gradeThresholds": dict(zip(keys, thresholds))},
                                 sort_keys=True, separators=(",", ":")), epoch))
        snapshot = {"nationality": country, "productCode": product,
                    "pdMultiplier": multiplier,
                    "gradeThresholds": dict(zip(keys, thresholds))}
        conn.execute("""INSERT INTO accounts.credit_risk_policy_events
            (nationality,product_code,actor_id,snapshot,recorded_epoch) VALUES (?,?,?,?,?)""",
            (country, product, str(actor_id),
             json.dumps(snapshot, sort_keys=True, separators=(",", ":")), epoch))
        return {"nationality": country, "productCode": product,
                "pdMultiplier": multiplier, "gradeThresholds": dict(zip(keys, thresholds))}

    @staticmethod
    def configure_future_commitment(conn, loan_id, actor_id, committed_limit,
                                    credit_conversion_factor_bps, *, active=True,
                                    expires_epoch=None, now=None):
        """Version an internal undrawn facility assumption in native minor units."""
        from managers.compliance_manager import ComplianceManager
        from managers.rate_manager import RateManager
        from functions.money import currency_precision, to_minor_units
        ComplianceManager._require_admin(conn, actor_id)
        try:
            loan_id = int(loan_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("대출 식별자가 올바르지 않습니다.") from None
        if isinstance(loan_id, bool) or loan_id <= 0:
            raise ValueError("대출 식별자가 올바르지 않습니다.")
        loan = conn.execute("SELECT outstanding_balance,quote_currency FROM accounts.loans WHERE loan_id=?",
                            (loan_id,)).fetchone()
        if not loan:
            raise ValueError("대출을 찾을 수 없습니다.")
        if isinstance(credit_conversion_factor_bps, bool):
            raise ValueError("신용환산율은 0~10000bp 정수여야 합니다.")
        try:
            ccf = int(credit_conversion_factor_bps)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("신용환산율은 0~10000bp 정수여야 합니다.") from None
        if ccf != credit_conversion_factor_bps or not 0 <= ccf <= 10000:
            raise ValueError("신용환산율은 0~10000bp 정수여야 합니다.")
        if not isinstance(active, bool):
            raise ValueError("약정 상태는 boolean이어야 합니다.")
        currency = str(loan["quote_currency"] or "NWX").upper()
        places = currency_precision(conn, currency)
        limit_units = to_minor_units(committed_limit, places)
        outstanding_units = to_minor_units(loan["outstanding_balance"], places)
        if limit_units < outstanding_units:
            raise ValueError("약정한도는 현재 대출잔액보다 작을 수 없습니다.")
        expiry = None
        if expires_epoch is not None:
            expiry = RateManager._validated_epoch(expires_epoch, "약정 만료시각이 올바르지 않습니다.")
        epoch = RateManager._validated_epoch(now, "정책 적용 시각은 유효한 UNIX 시각이어야 합니다.")
        snapshot = {"loanId": loan_id, "committedLimitMinorUnits": limit_units,
                    "currency": currency, "creditConversionFactorBps": ccf,
                    "expiresEpoch": expiry, "active": active}
        conn.execute("""INSERT INTO accounts.loan_future_commitments
            (loan_id,committed_limit_minor_units,credit_conversion_factor_bps,expires_epoch,
             active,updated_epoch,updated_by) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(loan_id) DO UPDATE SET
             committed_limit_minor_units=excluded.committed_limit_minor_units,
             credit_conversion_factor_bps=excluded.credit_conversion_factor_bps,
             expires_epoch=excluded.expires_epoch,active=excluded.active,
             updated_epoch=excluded.updated_epoch,updated_by=excluded.updated_by""",
            (loan_id, limit_units, ccf, expiry, int(active), epoch, str(actor_id)))
        conn.execute("""INSERT INTO accounts.loan_future_commitment_events
            (loan_id,actor_id,snapshot,recorded_epoch) VALUES (?,?,?,?)""",
            (loan_id, str(actor_id), json.dumps(snapshot, sort_keys=True, separators=(",", ":")), epoch))
        conn.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
                     (str(actor_id), "credit_risk.commitment.configure",
                      json.dumps(snapshot, sort_keys=True, separators=(",", ":")), epoch))
        return snapshot

    @staticmethod
    def configure_collateral_recovery(conn, collateral_id, actor_id, *,
                                     seniority_rank=1, recovery_days=0,
                                     recovery_cost_bps=0, now=None):
        """Set audited simulator recovery waterfall assumptions for pledged collateral."""
        from managers.compliance_manager import ComplianceManager
        from managers.rate_manager import RateManager
        ComplianceManager._require_admin(conn, actor_id)
        def bounded_integer(value, low, high, name):
            if isinstance(value, bool):
                raise ValueError(f"{name} 값이 올바르지 않습니다.")
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{name} 값이 올바르지 않습니다.") from None
            if parsed != value or not low <= parsed <= high:
                raise ValueError(f"{name} 값이 범위를 벗어났습니다.")
            return parsed
        try:
            collateral_id = int(collateral_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("담보 식별자가 올바르지 않습니다.") from None
        if isinstance(collateral_id, bool) or collateral_id <= 0:
            raise ValueError("담보 식별자가 올바르지 않습니다.")
        row = conn.execute("SELECT collateral_id,loan_id,status,seniority_rank,recovery_days,recovery_cost_bps "
                           "FROM accounts.loan_collateral_assets WHERE collateral_id=?",
                           (collateral_id,)).fetchone()
        if not row or row["status"] != "pledged":
            raise ValueError("현재 설정 가능한 질권 담보를 찾을 수 없습니다.")
        rank = bounded_integer(seniority_rank, 1, 100, "회수순위")
        days = bounded_integer(recovery_days, 0, 3650, "회수기간")
        cost = bounded_integer(recovery_cost_bps, 0, 10000, "회수비용")
        epoch = RateManager._validated_epoch(now, "정책 적용 시각은 유효한 UNIX 시각이어야 합니다.")
        prior = {key: row[key] for key in ("seniority_rank", "recovery_days", "recovery_cost_bps")}
        current = {"seniority_rank": rank, "recovery_days": days, "recovery_cost_bps": cost}
        conn.execute("""UPDATE accounts.loan_collateral_assets SET seniority_rank=?,recovery_days=?,
            recovery_cost_bps=? WHERE collateral_id=?""", (rank, days, cost, collateral_id))
        conn.execute("""INSERT INTO accounts.loan_collateral_recovery_events
            (collateral_id,loan_id,actor_id,prior_snapshot,new_snapshot,recorded_epoch)
            VALUES (?,?,?,?,?,?)""", (collateral_id, int(row["loan_id"]), str(actor_id),
            json.dumps(prior, sort_keys=True, separators=(",", ":")),
            json.dumps(current, sort_keys=True, separators=(",", ":")), epoch))
        conn.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
                     (str(actor_id), "credit_risk.collateral_recovery.configure",
                      json.dumps({"collateralId": collateral_id, **current}, sort_keys=True,
                                 separators=(",", ":")), epoch))
        return {"collateralId": collateral_id, "loanId": int(row["loan_id"]), **current}

    @classmethod
    def backtest_12m(cls, conn, nationality=None, *, issuer=None, now=None):
        """Compare each loan's latest matured PD snapshot with 12-month defaults.

        This is a compact calibration diagnostic, not an independent validation:
        snapshots are sampled from the simulator's own scorecard and defaults.
        """
        epoch = RateManager._validated_epoch(now, "invalid_ecl_backtest_epoch")
        issuer = str(issuer or "").strip() or None
        horizon = 365*86400
        rows = conn.execute(
            "WITH ranked AS ("
            " SELECT a.loan_id,a.pd_12m,a.calculated_epoch,l.defaulted_epoch,l.user_id,u.nationality,p.issuer,"
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
        diagnostics = {}
        recent_cutoff = epoch-3*horizon
        for row in rows:
            pd = cls._bounded(row["pd_12m"], 0, 1)
            band = next(label for low, high, label in bands if low <= pd < high)
            key = (str(row["issuer"] or ""), str(row["nationality"] or ""), band)
            scope = key[:2]
            diagnostic = diagnostics.setdefault(scope, {
                "labels": [], "pd": [],
                "cohorts": {"older": {label: 0 for _, _, label in bands},
                            "recent": {label: 0 for _, _, label in bands}},
            })
            stats = grouped.setdefault(key, {"count": 0, "defaults": 0,
                                               "predicted": 0.0, "brier": 0.0,
                                               "borrower_loans": {}})
            default_epoch = row["defaulted_epoch"]
            observed = int(default_epoch is not None
                           and row["calculated_epoch"] < float(default_epoch)
                           <= row["calculated_epoch"]+horizon)
            diagnostic["labels"].append(observed)
            diagnostic["pd"].append(pd)
            cohort = "older" if float(row["calculated_epoch"]) < recent_cutoff else "recent"
            diagnostic["cohorts"][cohort][band] += 1
            stats["count"] += 1
            stats["defaults"] += observed
            stats["predicted"] += pd
            stats["brier"] += (pd-observed)**2
            borrower_id = str(row["user_id"])
            stats["borrower_loans"][borrower_id] = (
                stats["borrower_loans"].get(borrower_id, 0) + 1
            )
        result = []
        for (bank, country, band), stats in sorted(grouped.items()):
            count = stats["count"]
            observed_rate = stats["defaults"]/count
            borrower_count = len(stats["borrower_loans"])
            # Kish effective sample size conservatively discounts correlated loans
            # from the same borrower when computing the Wilson interval.
            cluster_square_sum = sum(n*n for n in stats["borrower_loans"].values())
            effective_count = count*count/cluster_square_sum if cluster_square_sum else 0.0
            z = 1.959963984540054
            denominator = 1+(z*z/effective_count)
            center = (observed_rate+(z*z/(2*effective_count)))/denominator
            margin = z*math.sqrt(
                observed_rate*(1-observed_rate)/effective_count
                +(z*z/(4*effective_count*effective_count))
            )/denominator
            lower95 = max(0.0, center-margin)
            upper95 = min(1.0, center+margin)
            mean_predicted = stats["predicted"]/count
            result.append({"issuer": bank, "nationality": country, "pdBand": band,
                           "sampleCount": count, "observedDefaults": stats["defaults"],
                           "borrowerCount": borrower_count,
                           "effectiveSampleCount": effective_count,
                           "meanPredictedPd": mean_predicted,
                           "observedDefaultRate": observed_rate,
                           "calibrationGap": observed_rate-mean_predicted,
                           "observedToExpected": (stats["defaults"]/stats["predicted"]
                                                   if stats["predicted"] > 0 else None),
                           "observedRateLower95": lower95,
                           "observedRateUpper95": upper95,
                           "predictedPdWithinObserved95": lower95 <= mean_predicted <= upper95,
                           "brierScore": stats["brier"]/count})
        discrimination = []
        stability = []
        for (bank, country), stats in sorted(diagnostics.items()):
            # Mann–Whitney rank statistic; ties receive average ranks.
            ordered = sorted(zip(stats["pd"], stats["labels"]), key=lambda item: item[0])
            positives = sum(stats["labels"])
            negatives = len(stats["labels"])-positives
            auc = None
            if positives and negatives:
                positive_rank_sum = 0.0
                start = 0
                while start < len(ordered):
                    end = start+1
                    while end < len(ordered) and ordered[end][0] == ordered[start][0]:
                        end += 1
                    average_rank = ((start+1)+end)/2
                    positive_rank_sum += average_rank*sum(
                        int(label) for _, label in ordered[start:end]
                    )
                    start = end
                auc = (positive_rank_sum-positives*(positives+1)/2)/(positives*negatives)
            discrimination.append({"issuer": bank, "nationality": country,
                                   "sampleCount": len(ordered), "defaults": positives,
                                   "auroc": auc,
                                   "method": "Mann–Whitney rank AUC on matured out-of-time loan outcomes; simulator-only"})
            older = stats["cohorts"]["older"]
            recent = stats["cohorts"]["recent"]
            older_count, recent_count = sum(older.values()), sum(recent.values())
            psi = None
            if older_count and recent_count:
                psi = 0.0
                for _, _, label in bands:
                    old_share = (older[label]+0.5)/(older_count+0.5*len(bands))
                    recent_share = (recent[label]+0.5)/(recent_count+0.5*len(bands))
                    psi += (recent_share-old_share)*math.log(recent_share/old_share)
            stability.append({"issuer": bank, "nationality": country,
                              "olderCount": older_count, "recentCount": recent_count,
                              "populationStabilityIndex": psi,
                              "cohortSplitEpoch": recent_cutoff,
                              "method": "older (6–3y matured snapshots) vs recent (3–1y matured snapshots); smoothed PD-band PSI; simulator-only"})
        return {"issuer": issuer, "horizonDays": 365,
                "maturedThroughEpoch": epoch-horizon, "groups": result,
                "discrimination": discrimination, "stability": stability,
                "method": "most recent matured Stage 1/2 assessment per loan; includes only snapshots with a full 365-day outcome window; outcome is recorded default within the following 12 months; capped at 5000 loans; Wilson intervals use Kish effective sample size to discount repeated loans per borrower, as a conservative clustering approximation; simulator-only calibration diagnostic"}

    @staticmethod
    def grade_transition_report(conn, nationality=None, *, issuer=None):
        """Compare each loan's first and most recent persisted PD grade."""
        if "risk_grade" not in {row[1] for row in conn.execute(
                "PRAGMA table_info(loan_ecl_assessments)").fetchall()}:
            return {"transitions": [], "method": "historical schema has no persisted risk grades"}
        issuer = str(issuer or "").strip() or None
        rows = conn.execute("""WITH ranked AS (
                SELECT a.loan_id,a.risk_grade,a.calculated_epoch,a.assessment_id,
                       u.nationality,p.issuer,
                       ROW_NUMBER() OVER (PARTITION BY a.loan_id ORDER BY a.calculated_epoch,a.assessment_id) first_rank,
                       ROW_NUMBER() OVER (PARTITION BY a.loan_id ORDER BY a.calculated_epoch DESC,a.assessment_id DESC) last_rank
                FROM loan_ecl_assessments a JOIN loans l ON l.loan_id=a.loan_id
                JOIN users u ON u.user_id=l.user_id JOIN liability p ON p.product_code=l.product_code
                WHERE a.risk_grade IS NOT NULL AND (? IS NULL OR p.issuer=?)
                  AND (? IS NULL OR u.nationality=?)
            ), firsts AS (SELECT * FROM ranked WHERE first_rank=1),
                 lasts AS (SELECT * FROM ranked WHERE last_rank=1)
            SELECT f.issuer,f.nationality,f.risk_grade from_grade,l.risk_grade to_grade,
                   COUNT(*) loan_count
            FROM firsts f JOIN lasts l ON l.loan_id=f.loan_id
            GROUP BY f.issuer,f.nationality,f.risk_grade,l.risk_grade
            ORDER BY f.issuer,f.nationality,f.risk_grade,l.risk_grade LIMIT 500""",
            (issuer, issuer, nationality, nationality),
        ).fetchall()
        return {"transitions": [dict(row) for row in rows],
                "method": "per-loan first recorded grade to latest recorded grade; historical assessments are immutable; simulator-only, not a migration matrix"}


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
        total_probability = sum(values[0] for values in normalized.values())
        if set(normalized) != keys or not math.isclose(
                total_probability, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("세 시나리오 확률의 합은 1이어야 합니다.")
        if not conn.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (country,)).fetchone():
            raise ValueError("국적을 찾을 수 없습니다.")
        epoch = RateManager._validated_epoch(
            now, "ECL 시나리오 적용 시각은 유효한 UNIX 시각이어야 합니다."
        )
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
    def risk_grade(pd_12m, thresholds=None):
        """Map simulator PD to a stable A–G ordinal grade for transition review."""
        pd = CreditRiskManager._bounded(pd_12m, 0.0, 1.0)
        thresholds = thresholds or (.005, .01, .025, .05, .10, .20)
        for threshold, grade in zip(thresholds, "ABCDEF"):
            if pd <= threshold:
                return grade
        return "G"

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
        currency = str(loan["quote_currency"] or "NWX").upper()
        cash_collateral = max(
            0.0,
            float(loan["collateral_amount"] or 0)
            - float(loan["collateral_released"] or 0)
            - float(loan["collateral_seized"] or 0),
        )
        eir = 0.0
        for key in ("contract_effective_rate", "contract_rate"):
            try:
                if loan[key] is not None:
                    eir = cls._bounded(loan[key], -99, 1000)/100
                    break
            except (KeyError, IndexError):
                pass
        recoveries = [(0, cash_collateral, 0, 0)]
        asset_rows = conn.execute(
            "SELECT last_value,haircut,seniority_rank,recovery_days,recovery_cost_bps "
            "FROM loan_collateral_assets WHERE loan_id=? AND status='pledged' "
            "ORDER BY seniority_rank,collateral_id", (loan["loan_id"],),
        ).fetchall()
        recoveries.extend((
            int(row["seniority_rank"]),
            max(0.0, float(row["last_value"] or 0))*(1-cls._bounded(row["haircut"],0,1)),
            int(row["recovery_days"]), int(row["recovery_cost_bps"]),
        ) for row in asset_rows)
        from functions.money import currency_precision, from_minor_units
        places = currency_precision(conn, currency)
        guarantees = conn.execute("""SELECT guarantee_limit_minor_units,guarantee_recovery_bps,
                recovery_days,recovery_cost_bps,seniority_rank
            FROM accounts.loan_credit_support WHERE loan_id=? AND support_type='guarantor'
              AND active=1 AND quote_currency=? ORDER BY seniority_rank,support_id""",
            (loan["loan_id"], currency),
        ).fetchall()
        for row in guarantees:
            recoveries.append((int(row["seniority_rank"]),
                float(from_minor_units(row["guarantee_limit_minor_units"], places))
                    *int(row["guarantee_recovery_bps"])/10000,
                int(row["recovery_days"]), int(row["recovery_cost_bps"])))
        remaining, expected_recovery = ead, 0.0
        for _, gross, recovery_days, cost_bps in sorted(recoveries, key=lambda item:item[0]):
            claim_covered = min(remaining, max(0.0, gross))
            discount = (1+eir)**(-max(0, recovery_days)/365)
            net_recovery = claim_covered*(1-max(0,min(10000,int(cost_bps)))/10000)*discount
            recognized = min(remaining, max(0.0, net_recovery))
            expected_recovery += recognized
            remaining -= recognized
        expected_recovery += max(0.0, remaining)*.35
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
        totals_by_currency = {}
        for loan in loans:
            currency = str(loan["quote_currency"] or "NWX")
            ead = round_currency(
                conn, max(0.0, float(loan["outstanding_balance"] or 0)), currency
            )
            loss = round_currency(conn, ead*cls._lgd(conn, loan), currency)
            amounts = totals_by_currency.setdefault(
                currency, {"ead": 0.0, "loss": 0.0, "allowance": 0.0}
            )
            amounts["ead"] += ead
            amounts["loss"] += loss
            amounts["allowance"] += round_currency(
                conn, max(0.0, float(loan["ecl_allowance"] or 0)), currency
            )
        performing_ead = sum(
            to_base(values["ead"], currency)
            for currency, values in totals_by_currency.items()
        )
        expected_loss = sum(
            to_base(values["loss"], currency)
            for currency, values in totals_by_currency.items()
        )
        allowance = sum(
            to_base(values["allowance"], currency)
            for currency, values in totals_by_currency.items()
        )
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

    @classmethod
    def correlated_default_stress(cls, conn, issuer, *, asset_correlation=0.20,
                                  confidence=0.99):
        """One-factor Gaussian tail stress using latest simulated 12m PD snapshots."""
        issuer = str(issuer or "").strip()
        rho, quantile = float(asset_correlation), float(confidence)
        if not issuer or not math.isfinite(rho) or not 0 <= rho < 1:
            raise ValueError("invalid_correlated_credit_stress_parameters")
        if not math.isfinite(quantile) or not 0.5 < quantile < 1:
            raise ValueError("invalid_correlated_credit_stress_parameters")
        sheet = RateManager.institution_balance_sheet(conn, issuer)
        base = str(sheet["base_currency"])
        from statistics import NormalDist
        from managers.currency_manager import CurrencyManager, SCALE
        normal = NormalDist()
        adverse_factor = -normal.inv_cdf(quantile)
        rows = conn.execute("""WITH latest AS (
                SELECT loan_id,pd_12m,ROW_NUMBER() OVER (
                    PARTITION BY loan_id ORDER BY calculated_epoch DESC,assessment_id DESC) rn
                FROM loan_ecl_assessments
            )
            SELECT l.*,u.credit_score,u.nationality,x.pd_12m
            FROM loans l JOIN users u ON u.user_id=l.user_id
            JOIN liability p ON p.product_code=l.product_code
            LEFT JOIN latest x ON x.loan_id=l.loan_id AND x.rn=1
            WHERE p.issuer=? AND l.status='active' AND l.collection_status<>'defaulted'
              AND l.outstanding_balance>0""",
            (issuer,),
        ).fetchall()
        independent, correlated, total_ead = 0.0, 0.0, 0.0
        for loan in rows:
            currency = str(loan["quote_currency"] or base)
            ead = round_currency(conn, max(0.0, float(loan["outstanding_balance"] or 0)), currency)
            if currency != base:
                ead = CurrencyManager.convert(conn, int(round(float(ead)*SCALE)), currency, base)/SCALE
            pd = (float(loan["pd_12m"]) if loan["pd_12m"] is not None else cls._pd_12m(
                loan["credit_score"], loan["underwriting_dti"], loan["underwriting_dsr"],
                loan["missed_payment_count"], loan["current_ltv"],
                loan["overdue_since_epoch"] is not None,
            ))
            pd = cls._bounded(pd, 0.0001, 0.9999)
            conditional_pd = normal.cdf(
                (normal.inv_cdf(pd)-math.sqrt(rho)*adverse_factor)/math.sqrt(1-rho)
            )
            lgd = cls._lgd(conn, loan)
            independent += ead*pd*lgd
            correlated += ead*conditional_pd*lgd
            total_ead += ead
        return {"issuer": issuer, "currency": base,
                "decimalPlaces": currency_precision(conn, base),
                "confidence": quantile, "assetCorrelation": rho,
                "exposureCount": len(rows),
                "performingEad": round_currency(conn, total_ead, base),
                "independentExpectedLoss": round_currency(conn, independent, base),
                "correlatedTailLoss": round_currency(conn, correlated, base),
                "method": "single-factor Gaussian conditional default probability at adverse systematic quantile; latest simulator PD or scorecard fallback; current EAD and collateral LGD; not a regulatory capital formula"}

    @classmethod
    def borrower_concentration_stress(cls, conn, issuer):
        """Aggregate loan exposure by borrower and stress each top borrower default."""
        issuer = str(issuer or "").strip()
        if not issuer:
            raise ValueError("financial_institution_not_found")
        sheet = RateManager.institution_balance_sheet(conn, issuer)
        base = str(sheet["base_currency"])
        terms = RateManager.institution_components(conn, issuer)
        from managers.currency_manager import CurrencyManager, SCALE

        def to_base(amount, currency):
            amount = round_currency(conn, max(0.0, float(amount or 0)), currency)
            if currency == base:
                return amount
            return CurrencyManager.convert(
                conn, int(round(float(amount)*SCALE)), currency, base
            )/SCALE

        parent = {}

        def root(user_id):
            parent.setdefault(user_id, user_id)
            while parent[user_id] != user_id:
                parent[user_id] = parent[parent[user_id]]
                user_id = parent[user_id]
            return user_id

        for link in conn.execute(
            "SELECT l.user_id,s.party_user_id FROM accounts.loan_credit_support s "
            "JOIN accounts.loans l ON l.loan_id=s.loan_id "
            "JOIN product.liability p ON p.product_code=l.product_code "
            "WHERE p.issuer=? AND s.support_type='connected_party' AND s.active=1",
            (issuer,),
        ):
            left, right = root(str(link["user_id"])), root(str(link["party_user_id"]))
            if left != right:
                parent[max(left, right)] = min(left, right)

        borrowers = {}
        for loan in conn.execute(
            "SELECT l.loan_id,l.user_id,l.outstanding_balance,l.quote_currency,"
            "l.collateral_amount,l.collateral_released,l.collateral_seized,l.ecl_allowance "
            "FROM loans l JOIN liability p ON p.product_code=l.product_code "
            "WHERE p.issuer=? AND l.status IN ('active','defaulted') "
            "AND l.outstanding_balance>0",
            (issuer,),
        ):
            user_id = root(str(loan["user_id"] or ""))
            row = borrowers.setdefault(user_id, {
                "exposureByCurrency": {}, "grossLossByCurrency": {},
                "allowanceByCurrency": {}, "loanCount": 0,
            })
            currency = str(loan["quote_currency"] or base)
            ead = round_currency(
                conn, max(0.0, float(loan["outstanding_balance"] or 0)), currency
            )
            loss = round_currency(conn, ead*cls._lgd(conn, loan), currency)
            row["exposureByCurrency"][currency] = (
                row["exposureByCurrency"].get(currency, 0.0)+ead
            )
            row["grossLossByCurrency"][currency] = (
                row["grossLossByCurrency"].get(currency, 0.0)+loss
            )
            row["allowanceByCurrency"][currency] = (
                row["allowanceByCurrency"].get(currency, 0.0)
                +round_currency(
                    conn, max(0.0, float(loan["ecl_allowance"] or 0)), currency
                )
            )
            row["loanCount"] += 1

        for row in borrowers.values():
            row["exposure"] = sum(
                to_base(amount, currency)
                for currency, amount in row.pop("exposureByCurrency").items()
            )
            row["grossLoss"] = sum(
                to_base(amount, currency)
                for currency, amount in row.pop("grossLossByCurrency").items()
            )
            row["allowance"] = sum(
                to_base(amount, currency)
                for currency, amount in row.pop("allowanceByCurrency").items()
            )

        total = sum(row["exposure"] for row in borrowers.values())
        equity = float(terms["effective_equity"])
        ranked = sorted(borrowers.items(), key=lambda item: (-item[1]["exposure"], item[0]))
        result = []
        for user_id, row in ranked[:5]:
            gross = round_currency(conn, row["grossLoss"], base)
            allowance = min(gross, round_currency(conn, row["allowance"], base))
            incremental = round_currency(conn, max(0.0, gross-allowance), base)
            result.append({
                "borrowerId": user_id, "loanCount": row["loanCount"],
                "exposure": round_currency(conn, row["exposure"], base),
                "exposureSharePercent": row["exposure"]/total*100 if total else 0.0,
                "grossLoss": gross, "existingAllowanceApplied": allowance,
                "incrementalProvision": incremental,
                "equityAfterProvision": round_currency(conn, equity-incremental, base),
            })
        return {
            "issuer": issuer, "currency": base,
            "decimalPlaces": currency_precision(conn, base),
            "borrowerCount": len(borrowers),
            "totalLoanExposure": round_currency(conn, total, base),
            "largestBorrowerSharePercent": result[0]["exposureSharePercent"] if result else 0.0,
            "topFiveExposureSharePercent": sum(
                row["exposureSharePercent"] for row in result
            ),
            "borrowers": result,
            "method": "single-borrower/explicit connected-party-group default stress across issuer active/defaulted loans; collateral and guarantee adjusted simulator LGD and group-recorded ECL allowance; native exposures converted to institution base currency; excludes tax effects and loss correlation across distinct borrower groups",
        }

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
        projected = (round_currency(conn, max(0.0, current-scheduled_principal), currency)
                     if rows else round_currency(conn, current, currency))
        commitment = conn.execute(
            "SELECT committed_limit_minor_units,credit_conversion_factor_bps "
            "FROM accounts.loan_future_commitments WHERE loan_id=? AND active=1 "
            "AND (expires_epoch IS NULL OR expires_epoch>=?)",
            (int(loan_id), horizon),
        ).fetchone()
        if not commitment:
            return projected, ("midpoint_after_scheduled_principal" if rows
                               else "current_balance_no_future_schedule")
        from functions.money import currency_precision, from_minor_units
        places = currency_precision(conn, currency)
        limit = float(from_minor_units(commitment["committed_limit_minor_units"], places))
        undrawn = max(0.0, limit-projected)
        ead = round_currency(
            conn, projected+undrawn*int(commitment["credit_conversion_factor_bps"])/10000,
            currency,
        )
        prefix = "midpoint_after_scheduled_principal" if rows else "current_balance_no_future_schedule"
        return ead, prefix+"_plus_undrawn_commitment"

    @classmethod
    def assess_conn(cls, conn, now, *, user_id=None, product_code=None):
        """Book at most one assessment per active loan per UTC day."""
        epoch = RateManager._validated_epoch(now, "invalid_ecl_assessment_epoch")
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
            co_borrowers = conn.execute(
                "SELECT party_user_id,exposure_share_bps,underwriting_dti,underwriting_dsr FROM accounts.loan_credit_support "
                "WHERE loan_id=? AND support_type='co_borrower' AND active=1 "
                "ORDER BY party_user_id",
                (loan["loan_id"],),
            ).fetchall()
            if co_borrowers:
                shares = sum(max(0, min(10000, int(row["exposure_share_bps"] or 0)))
                             for row in co_borrowers)
                if shares > 10000:
                    raise ValueError("co_borrower_exposure_share_exceeds_total")
                weighted_pd = pd_12m * (10000-shares)
                for support in co_borrowers:
                    party = conn.execute(
                        "SELECT credit_score FROM users.users WHERE user_id=?",
                        (support["party_user_id"],),
                    ).fetchone()
                    share = max(0, min(10000, int(support["exposure_share_bps"] or 0)))
                    co_pd = (cls._pd_12m(
                        party["credit_score"],
                        (support["underwriting_dti"] if support["underwriting_dti"] is not None else loan["underwriting_dti"]),
                        (support["underwriting_dsr"] if support["underwriting_dsr"] is not None else loan["underwriting_dsr"]),
                        loan["missed_payment_count"],
                        loan["current_ltv"], overdue,
                    ) if party else pd_12m)
                    weighted_pd += co_pd*share
                pd_12m = weighted_pd/10000
            policy = conn.execute(
                "SELECT * FROM accounts.credit_risk_policy_overrides "
                "WHERE nationality=? AND product_code IN (?, '*') "
                "ORDER BY CASE WHEN product_code=? THEN 0 ELSE 1 END LIMIT 1",
                (str(loan["nationality"] or ""), str(loan["product_code"]),
                 str(loan["product_code"])),
            ).fetchone()
            policy_pd_multiplier = float(policy["pd_multiplier"]) if policy else 1.0
            pd_12m = cls._bounded(pd_12m*policy_pd_multiplier, 0.0, 1.0)
            grade_thresholds = ([float(policy[f"grade_{letter}_max"]) for letter in "abcdef"]
                                if policy else None)
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
                conn.execute(
                    "UPDATE accounts.loans SET nonaccrual_since_epoch="
                    "COALESCE(nonaccrual_since_epoch,defaulted_epoch,?) WHERE loan_id=?",
                    (epoch, loan["loan_id"]),
                )
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
                                         "policyPdMultiplier": policy_pd_multiplier,
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
                "expected_loss_time_years,macro_pd_multiplier,calculated_epoch,risk_grade) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (loan["loan_id"], as_of_day, stage, pd_12m, lifetime_pd, lgd,
                 ead, expected_loss, loan["quote_currency"],
                 json.dumps(scenario_details, separators=(",", ":")),
                 discount_factor, eir_percent, expected_loss_time,
                 macro_pd_multiplier, epoch, cls.risk_grade(pd_12m, grade_thresholds)),
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
