"""Versioned, internal-only cross-border DR dividend withholding rules."""
from decimal import Decimal, InvalidOperation


class DrDividendTaxManager:
    FIELDS = ("rule_name", "issuer_nationality", "underlying_nationality",
              "recipient_nationality", "recipient_residency", "product_type",
              "treaty_code", "tax_rate_percent", "tax_base", "exempt",
              "effective_from", "effective_until")

    @staticmethod
    def _text(value, label, wildcard=False, maximum=100):
        text = str(value or "").strip()
        if wildcard and text == "*":
            return text
        if not text or len(text) > maximum:
            raise ValueError(f"invalid_{label}")
        return text

    @classmethod
    def create_rule(cls, conn, actor, payload, *, now):
        name = cls._text(payload.get("rule_name"), "rule_name", maximum=120)
        issuer = cls._text(payload.get("issuer_nationality"), "issuer_nationality")
        underlying = cls._text(payload.get("underlying_nationality"), "underlying_nationality")
        recipient = cls._text(payload.get("recipient_nationality", "*"), "recipient_nationality", True)
        residency = cls._text(payload.get("recipient_residency", "*"), "recipient_residency", True)
        product = cls._text(payload.get("product_type", "depositary_receipt"), "product_type", maximum=40)
        treaty = str(payload.get("treaty_code") or "").strip() or None
        if treaty and len(treaty) > 80:
            raise ValueError("invalid_treaty_code")
        try:
            rate = Decimal(str(payload.get("tax_rate_percent")))
            if not rate.is_finite() or rate < 0 or rate > 100:
                raise ValueError
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("invalid_tax_rate_percent") from None
        if payload.get("tax_base", "gross") != "gross":
            raise ValueError("unsupported_tax_base")
        exempt = int(bool(payload.get("exempt", False)))
        try:
            start = int(payload.get("effective_from", now))
            end_raw = payload.get("effective_until")
            end = int(end_raw) if end_raw not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("invalid_effective_period") from None
        if start < 0 or (end is not None and end <= start):
            raise ValueError("invalid_effective_period")
        cursor = conn.execute(
            "INSERT INTO dr_dividend_tax_rules(rule_name,issuer_nationality,underlying_nationality,"
            "recipient_nationality,recipient_residency,product_type,treaty_code,tax_rate_percent,"
            "tax_base,exempt,effective_from,effective_until,created_by,created_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, issuer, underlying, recipient, residency, product, treaty, str(rate),
             "gross", exempt, start, end, str(actor), int(now)),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def applicable_rule(conn, *, issuer, underlying, recipient, residency, product, contract_epoch):
        return conn.execute(
            "SELECT * FROM dr_dividend_tax_rules WHERE issuer_nationality=? "
            "AND underlying_nationality=? AND recipient_nationality IN (?, '*') "
            "AND recipient_residency IN (?, '*') AND product_type IN (?, '*') "
            "AND effective_from<=? AND (effective_until IS NULL OR ?<effective_until) "
            "ORDER BY (recipient_nationality<>'*') DESC,(recipient_residency<>'*') DESC,"
            "(product_type<>'*') DESC,effective_from DESC,rule_id DESC LIMIT 1",
            (issuer, underlying, recipient, residency, product, int(contract_epoch), int(contract_epoch)),
        ).fetchone()

    @staticmethod
    def list_rules(conn, limit=200):
        return [dict(row) for row in conn.execute(
            "SELECT * FROM dr_dividend_tax_rules ORDER BY rule_id DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        )]

    @staticmethod
    def reconciliation(conn, limit=200):
        rows = [dict(row) for row in conn.execute(
            "SELECT currency_code,COUNT(*) payment_count,SUM(gross_amount) gross_minor_units,"
            "SUM(tax_amount) tax_minor_units,SUM(net_amount_units) net_minor_units,"
            "SUM(gross_amount)*1000000-SUM(tax_amount)*1000000-SUM(net_amount_units) difference_scaled "
            "FROM dr_dividend_withholding_events GROUP BY currency_code ORDER BY currency_code")]
        payments = [dict(row) for row in conn.execute(
            "SELECT e.*,p.user_id,p.quantity,p.quote_currency FROM dr_dividend_withholding_events e "
            "JOIN dividend_payments p USING(payment_id) ORDER BY e.event_id DESC LIMIT ?",
            (max(1, min(200, int(limit))),))]
        return {"currencies": rows, "payments": payments,
                "unloggedPaymentCount": conn.execute(
                    "SELECT COUNT(*) FROM dividend_payments p JOIN depositary_receipts r "
                    "ON r.underlying_code=(SELECT asset_code FROM dividend_distributions d "
                    "WHERE d.distribution_id=p.distribution_id) WHERE NOT EXISTS "
                    "(SELECT 1 FROM dr_dividend_withholding_events e WHERE e.payment_id=p.payment_id)"
                ).fetchone()[0]}
