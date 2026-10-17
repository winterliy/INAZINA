"""Privacy-minimal KYC state and configurable AML transaction monitoring.

This module stores review metadata only. It deliberately accepts no names,
government identifiers, document images, or free-text evidence.
"""

from decimal import Decimal, InvalidOperation
import json
import math


class ComplianceManager:
    KYC_STATUSES = {"pending", "verified", "rejected", "expired"}
    KYC_RISK_TIERS = {"standard", "enhanced", "restricted"}
    KYC_REASONS = {
        "review_started", "requirements_met", "requirements_not_met",
        "periodic_review", "expired", "risk_escalation", "customer_request",
    }
    AML_OUTCOMES = {
        "no_action", "false_positive", "further_review", "escalated",
        "account_restriction_considered",
    }

    @staticmethod
    def _require_admin(conn, actor_id):
        actor = conn.execute(
            "SELECT role,status FROM users WHERE user_id=?", (str(actor_id),)
        ).fetchone()
        if not actor or actor["role"] != "admin" or actor["status"] != "active":
            raise ValueError("활성 관리자만 KYC·AML 심사 정보를 변경할 수 있습니다.")

    @staticmethod
    def _audit(conn, actor_id, action, payload, now):
        conn.execute(
            "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) "
            "VALUES (?,?,?,?)",
            (str(actor_id), str(action), json.dumps(payload, sort_keys=True), float(now)),
        )

    @staticmethod
    def _validated_epoch(value, error_code):
        from managers.bank_journal import BankJournal
        try:
            return BankJournal.validate_epoch(value)
        except ValueError as exc:
            raise ValueError(error_code) from exc

    @staticmethod
    def require_financial_eligibility(conn, user_id, *, now=None):
        """Enforce an explicit KYC review on gated customer financial actions.

        Users without a KYC profile retain legacy simulator behavior. A recorded
        profile must be verified, current, and not risk-restricted. This does not
        block incoming transfers.
        """
        when = ComplianceManager._validated_epoch(now, "invalid_kyc_check_epoch")
        profile = conn.execute(
            "SELECT status,risk_tier,expires_epoch FROM kyc_profiles WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        if not profile:
            return True
        expires = (float(profile["expires_epoch"])
                   if profile["expires_epoch"] is not None else None)
        if (profile["status"] != "verified"
                or (expires is not None and (not math.isfinite(expires) or expires <= when))):
            raise ValueError("KYC 확인이 완료되지 않았거나 만료되어 금융 거래를 진행할 수 없습니다.")
        if profile["risk_tier"] not in {"standard", "enhanced"}:
            raise ValueError("제한 위험등급으로 설정되어 금융 거래를 진행할 수 없습니다.")
        return True

    @classmethod
    def record_financial_action_denial(cls, conn, user_id, action_code, *, now=None):
        """Append a privacy-minimal event for a denied, explicitly reviewed user."""
        when = cls._validated_epoch(now, "invalid_kyc_event_epoch")
        if action_code not in {"transfer", "deposit_open", "loan_apply"}:
            raise ValueError("invalid_kyc_action_code")
        profile = conn.execute(
            "SELECT status,risk_tier,expires_epoch FROM kyc_profiles WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        if not profile:
            return False
        expires = (float(profile["expires_epoch"])
                   if profile["expires_epoch"] is not None else None)
        reason = (
            "risk_restricted" if profile["risk_tier"] not in {"standard", "enhanced"}
            else "kyc_incomplete_or_expired"
        )
        # If the profile changed between the rejected read and this event insert,
        # record the immutable snapshot available now; the event remains an audit
        # of the denied action, not a claim about identity or evidence.
        expiry_valid = expires is None or (math.isfinite(expires) and expires > when)
        if (profile["status"] == "verified" and expiry_valid
                and profile["risk_tier"] in {"standard", "enhanced"}):
            reason = "eligibility_changed_during_check"
        status, tier = str(profile["status"]), str(profile["risk_tier"])
        conn.execute(
            "INSERT INTO kyc_profile_events(user_id,actor_id,event_type,prior_status,"
            "new_status,prior_risk_tier,new_risk_tier,reason_code,recorded_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(user_id), str(user_id), f"{action_code}_denied", status, status,
             tier, tier, reason, when),
        )
        return True

    @classmethod
    def check_financial_eligibility(cls, db, user_id, action_code, *, now=None):
        """Check outside the business transaction and persist a denial for review."""
        when = cls._validated_epoch(now, "invalid_kyc_check_epoch")
        try:
            with db.readonly() as conn:
                return cls.require_financial_eligibility(conn, user_id, now=when)
        except ValueError:
            with db.atomic() as conn:
                cls.record_financial_action_denial(
                    conn, user_id, action_code, now=when
                )
            raise

    @classmethod
    def set_kyc_status(cls, conn, user_id, actor_id, status, *, risk_tier="standard",
                       reason_code, expires_epoch=None, now=None):
        """Record a KYC review without persisting identity documents or PII."""
        when = cls._validated_epoch(now, "invalid_kyc_review_epoch")
        cls._require_admin(conn, actor_id)
        status, risk_tier, reason_code = (
            str(status), str(risk_tier), str(reason_code)
        )
        if status not in cls.KYC_STATUSES or risk_tier not in cls.KYC_RISK_TIERS:
            raise ValueError("KYC 상태 또는 위험등급을 확인해주세요.")
        if reason_code not in cls.KYC_REASONS:
            raise ValueError("허용되지 않은 KYC 사유 코드입니다.")
        if expires_epoch is not None:
            expires_epoch = float(expires_epoch)
            if not math.isfinite(expires_epoch) or expires_epoch <= when:
                raise ValueError("KYC 만료 시각은 현재보다 미래여야 합니다.")
        subject = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?", (str(user_id),)
        ).fetchone()
        if not subject:
            raise ValueError("KYC 대상 사용자를 찾을 수 없습니다.")
        previous = conn.execute(
            "SELECT status,risk_tier FROM kyc_profiles WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        prior_status = previous["status"] if previous else "not_started"
        prior_tier = previous["risk_tier"] if previous else "standard"
        conn.execute(
            "INSERT INTO kyc_profiles(user_id,status,risk_tier,reviewed_by,reviewed_epoch,"
            "expires_epoch,updated_epoch) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,"
            "risk_tier=excluded.risk_tier,reviewed_by=excluded.reviewed_by,"
            "reviewed_epoch=excluded.reviewed_epoch,expires_epoch=excluded.expires_epoch,"
            "updated_epoch=excluded.updated_epoch",
            (str(user_id), status, risk_tier, str(actor_id), when,
             expires_epoch, when),
        )
        conn.execute(
            "INSERT INTO kyc_profile_events(user_id,actor_id,event_type,prior_status,"
            "new_status,prior_risk_tier,new_risk_tier,reason_code,recorded_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(user_id), str(actor_id), "review", prior_status, status,
             prior_tier, risk_tier, reason_code, when),
        )
        cls._audit(conn, actor_id, "kyc.review", {
            "user_id": str(user_id), "status": status,
            "risk_tier": risk_tier, "reason_code": reason_code,
        }, when)
        return {"user_id": str(user_id), "status": status,
                "risk_tier": risk_tier, "updated_epoch": when}

    @classmethod
    def expire_due_kyc_profiles(cls, conn, actor_id, *, now=None, limit=500):
        """Apply elapsed KYC expiry dates as an explicit, auditable admin action."""
        when = cls._validated_epoch(now, "invalid_kyc_expiry_epoch")
        cls._require_admin(conn, actor_id)
        rows = conn.execute(
            "SELECT user_id,status,risk_tier FROM kyc_profiles "
            "WHERE status='verified' AND expires_epoch IS NOT NULL AND expires_epoch<=? "
            "ORDER BY expires_epoch,user_id LIMIT ?",
            (when, max(1, min(500, int(limit)))),
        ).fetchall()
        actual = 0
        for row in rows:
            changed = conn.execute(
                "UPDATE kyc_profiles SET status='expired',reviewed_by=?,reviewed_epoch=?,"
                "updated_epoch=? WHERE user_id=? AND status='verified' "
                "AND expires_epoch IS NOT NULL AND expires_epoch<=?",
                (str(actor_id), when, when, row["user_id"], when),
            )
            if changed.rowcount != 1:
                continue
            actual += 1
            conn.execute(
                "INSERT INTO kyc_profile_events(user_id,actor_id,event_type,prior_status,"
                "new_status,prior_risk_tier,new_risk_tier,reason_code,recorded_epoch) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (row["user_id"], str(actor_id), "expiry", row["status"], "expired",
                 row["risk_tier"], row["risk_tier"], "expired", when),
            )
        # A conditional UPDATE protects against concurrent review; audit the rows
        # actually transitioned, not merely the initial selection size.
        cls._audit(conn, actor_id, "kyc.expire_due", {"expired_count": int(actual)}, when)
        return {"expired_count": actual, "batch_limit": 500}

    @classmethod
    def configure_aml_rule(cls, conn, nationality, actor_id, *,
                           large_transaction_limit=0,
                           rolling_24h_amount_limit=0,
                           rolling_24h_count_limit=0,
                           enabled=True, now=None):
        """Set simulator monitoring thresholds in the country's native currency.

        Rules default to disabled and are not presented as statutory thresholds.
        """
        when = cls._validated_epoch(now, "invalid_aml_rule_epoch")
        cls._require_admin(conn, actor_id)
        country = conn.execute(
            "SELECT currency_code FROM country_currencies WHERE nationality=?",
            (str(nationality),),
        ).fetchone()
        if not country:
            raise ValueError("국적별 통화 설정을 찾을 수 없습니다.")
        def amount(value, label):
            if isinstance(value, bool):
                raise ValueError(f"{label} 기준을 확인해주세요.")
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError(f"{label} 기준을 확인해주세요.") from None
            if not parsed.is_finite() or parsed < 0:
                raise ValueError(f"{label} 기준은 0 이상이어야 합니다.")
            return float(parsed)

        large = amount(large_transaction_limit, "고액거래")
        rolling = amount(rolling_24h_amount_limit, "24시간 누적거래")
        try:
            count = int(rolling_24h_count_limit)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("24시간 거래 횟수 기준을 확인해주세요.") from None
        if count < 0 or str(count) != str(rolling_24h_count_limit):
            raise ValueError("24시간 거래 횟수 기준은 0 이상의 정수여야 합니다.")
        if isinstance(enabled, bool):
            enabled = enabled
        elif isinstance(enabled, int) and enabled in (0, 1):
            enabled = bool(enabled)
        else:
            raise ValueError("감시 활성화 값은 true 또는 false여야 합니다.")
        if enabled and not (large > 0 or rolling > 0 or count > 0):
            raise ValueError("감시를 켜려면 하나 이상의 양수 임계치를 설정해주세요.")
        currency = str(country["currency_code"])
        conn.execute(
            "INSERT INTO aml_monitoring_rules(nationality,currency_code,"
            "large_transaction_limit,rolling_24h_amount_limit,rolling_24h_count_limit,"
            "enabled,updated_epoch) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(nationality) DO UPDATE SET currency_code=excluded.currency_code,"
            "large_transaction_limit=excluded.large_transaction_limit,"
            "rolling_24h_amount_limit=excluded.rolling_24h_amount_limit,"
            "rolling_24h_count_limit=excluded.rolling_24h_count_limit,"
            "enabled=excluded.enabled,updated_epoch=excluded.updated_epoch",
            (str(nationality), currency, large, rolling, count, int(enabled), when),
        )
        cls._audit(conn, actor_id, "aml.rule.configure", {
            "nationality": str(nationality), "currency_code": currency,
            "large_transaction_limit": large,
            "rolling_24h_amount_limit": rolling,
            "rolling_24h_count_limit": count, "enabled": enabled,
        }, when)
        return {"nationality": str(nationality), "currency_code": currency,
                "enabled": enabled, "updated_epoch": when}

    @classmethod
    def monitor_transfer(cls, conn, *, user_id, nationality, transaction_id,
                         source_units, source_currency, now=None):
        """Create idempotent alerts for configured amount/velocity thresholds."""
        when = cls._validated_epoch(now, "invalid_aml_monitor_epoch")
        rule = conn.execute(
            "SELECT currency_code,large_transaction_limit,rolling_24h_amount_limit,"
            "rolling_24h_count_limit,enabled FROM aml_monitoring_rules WHERE nationality=?",
            (str(nationality),),
        ).fetchone()
        if not rule or not int(rule["enabled"]):
            return []
        from managers.currency_manager import CurrencyManager, SCALE

        home_currency = str(rule["currency_code"])
        converted_units = (int(source_units) if str(source_currency) == home_currency else
                           CurrencyManager.convert(conn, int(source_units),
                                                   str(source_currency), home_currency))
        current_amount = Decimal(converted_units)/Decimal(SCALE)
        recent = conn.execute(
            "SELECT CASE WHEN sender_id=? THEN source_currency ELSE target_currency END currency,"
            "CASE WHEN sender_id=? THEN source_units ELSE target_units END units "
            "FROM currency_transfer_transactions WHERE (sender_id=? OR recipient_id=?) "
            "AND recorded_epoch>=? AND recorded_epoch<=? ORDER BY transaction_id DESC",
            (str(user_id), str(user_id), str(user_id), str(user_id), when-86400, when),
        ).fetchall()
        rolling_units = 0
        for row in recent:
            units = int(row["units"] or 0)
            if str(row["currency"]) != home_currency:
                units = CurrencyManager.convert(
                    conn, units, str(row["currency"]), home_currency
                )
            rolling_units += units
        rolling_amount = Decimal(rolling_units)/Decimal(SCALE)
        flags = []
        if (float(rule["large_transaction_limit"] or 0) > 0
                and current_amount >= Decimal(str(rule["large_transaction_limit"]))):
            flags.append("large_transaction")
        if (float(rule["rolling_24h_amount_limit"] or 0) > 0
                and rolling_amount >= Decimal(str(rule["rolling_24h_amount_limit"]))):
            flags.append("rolling_24h_amount")
        if (int(rule["rolling_24h_count_limit"] or 0) > 0
                and len(recent) >= int(rule["rolling_24h_count_limit"])):
            flags.append("rolling_24h_count")
        opened = []
        for rule_code in flags:
            alert_amount = (rolling_amount if rule_code == "rolling_24h_amount"
                            else current_amount)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO aml_alerts(user_id,nationality,rule_code,"
                "reference_key,currency_code,amount,opened_epoch) VALUES (?,?,?,?,?,?,?)",
                (str(user_id), str(nationality), rule_code,
                 f"transfer:{int(transaction_id)}", home_currency,
                 float(alert_amount), when),
            )
            if cursor.rowcount != 1:
                continue
            alert_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO aml_case_events(alert_id,actor_id,event_type,prior_status,"
                "new_status,recorded_epoch) VALUES (?,?,?,?,?,?)",
                (alert_id, "system", "alert_opened", None, "open", when),
            )
            opened.append(alert_id)
        return opened

    @classmethod
    def review_aml_alert(cls, conn, alert_id, actor_id, status, outcome_code, *, now=None):
        """Append an analyst disposition to an AML case; closed cases are terminal."""
        when = cls._validated_epoch(now, "invalid_aml_review_epoch")
        cls._require_admin(conn, actor_id)
        status, outcome_code = str(status), str(outcome_code)
        if status not in {"reviewing", "closed"} or outcome_code not in cls.AML_OUTCOMES:
            raise ValueError("AML 검토 상태 또는 결과 코드가 올바르지 않습니다.")
        alert = conn.execute(
            "SELECT status FROM aml_alerts WHERE alert_id=?", (int(alert_id),)
        ).fetchone()
        if not alert:
            raise ValueError("AML 사례를 찾을 수 없습니다.")
        previous = str(alert["status"])
        if previous == "closed":
            raise ValueError("종결된 AML 사례는 다시 변경할 수 없습니다.")
        conn.execute(
            "UPDATE aml_alerts SET status=?,outcome_code=?,reviewed_by=?,reviewed_epoch=? "
            "WHERE alert_id=? AND status=?",
            (status, outcome_code, str(actor_id), when, int(alert_id), previous),
        )
        conn.execute(
            "INSERT INTO aml_case_events(alert_id,actor_id,event_type,prior_status,"
            "new_status,outcome_code,recorded_epoch) VALUES (?,?,?,?,?,?,?)",
            (int(alert_id), str(actor_id), "analyst_review", previous, status,
             outcome_code, when),
        )
        cls._audit(conn, actor_id, "aml.case.review", {
            "alert_id": int(alert_id), "status": status,
            "outcome_code": outcome_code,
        }, when)
        return {"alert_id": int(alert_id), "status": status,
                "outcome_code": outcome_code}
