"""Currency-aware decimal helpers.

Database values are expressed in the named currency (not always NWX).  Calculated
amounts are rounded half-up to that currency's configured smallest unit.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


_WHOLE = Decimal("1")


def round_money(value, decimal_places=0):
    """Round a calculated amount half-up and return a SQLite-bindable number."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 유한한 숫자여야 합니다.") from None
    if not amount.is_finite():
        raise ValueError("금액은 유한한 숫자여야 합니다.")
    try:
        places = int(decimal_places)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("통화 소수 자릿수가 올바르지 않습니다.") from None
    if not 0 <= places <= 6:
        raise ValueError("통화 소수 자릿수는 0~6이어야 합니다.")
    rounded = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(rounded) if places == 0 else float(rounded)


def currency_precision(conn, currency_code):
    row = conn.execute(
        "SELECT decimal_places FROM currencies WHERE code=?",
        (str(currency_code or "").upper(),),
    ).fetchone()
    if not row:
        raise ValueError(f"통화를 확인해주세요: {currency_code}")
    places = int(row[0])
    if not 0 <= places <= 2:
        raise ValueError("통화 소수 자릿수는 0~2여야 합니다.")
    return places


def to_minor_units(value, decimal_places, *, rounding=ROUND_HALF_UP):
    """Convert a monetary amount to exact integer minor units.

    This is the accounting/storage boundary: callers should keep the returned
    integer authoritative and only convert back for presentation or legacy
    interfaces.  The amount is rounded once, using the currency's scale.
    """
    try:
        amount = Decimal(str(value))
        places = int(decimal_places)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise ValueError("금액 또는 통화 소수 자릿수가 올바르지 않습니다.") from None
    if not amount.is_finite():
        raise ValueError("금액은 유한한 숫자여야 합니다.")
    if not 0 <= places <= 6:
        raise ValueError("통화 소수 자릿수는 0~6이어야 합니다.")
    scaled = (amount * (10 ** places)).quantize(Decimal("1"), rounding=rounding)
    return int(scaled)


def from_minor_units(units, decimal_places):
    """Return an exact Decimal amount from integer currency minor units."""
    if isinstance(units, bool):
        raise ValueError("최소단위 금액은 정수여야 합니다.")
    try:
        integer_units = int(units)
        places = int(decimal_places)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("최소단위 금액 또는 통화 소수 자릿수가 올바르지 않습니다.") from None
    if integer_units != units or not 0 <= places <= 6:
        raise ValueError("최소단위 금액은 정수이며 통화 소수 자릿수는 0~6이어야 합니다.")
    return Decimal(integer_units).scaleb(-places)


def audit_amount_snapshots(conn):
    """Check the explicit money-field inventory against its integer snapshot.

    Identifiers come only from the internal registry and are still validated
    before interpolation. Missing source rows and stale amount values are
    returned as findings rather than silently repaired.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_amount_snapshots'"
    ).fetchone()
    if not exists:
        return [{"status": "missing_inventory"}]
    findings = []
    specifications = conn.execute(
        "SELECT DISTINCT source_table,source_key_field,amount_field "
        "FROM financial_amount_snapshots"
    ).fetchall()
    for spec in specifications:
        table, key_field, field = (str(spec[name]) for name in
                                   ("source_table", "source_key_field", "amount_field"))
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                   for name in (table, key_field, field)):
            continue
        columns = {item[1] for item in conn.execute(f'PRAGMA table_info("{table}")')}
        if key_field not in columns or field not in columns:
            continue
        missing = conn.execute(
            f'''SELECT CAST(src."{key_field}" AS TEXT) source_key
                FROM "{table}" src LEFT JOIN financial_amount_snapshots snap
                  ON snap.source_table=? AND snap.source_key_field=?
                 AND snap.source_key=CAST(src."{key_field}" AS TEXT)
                 AND snap.amount_field=?
                WHERE src."{field}" IS NOT NULL AND snap.source_key IS NULL''',
            (table, key_field, field),
        ).fetchall()
        findings.extend({"source_table": table, "source_key": str(row[0]),
                         "amount_field": field, "status": "missing_snapshot"}
                        for row in missing)
    rows = conn.execute("SELECT * FROM financial_amount_snapshots ORDER BY source_table,source_key,amount_field")
    for row in rows:
        table, key_field, key, field = (str(row[name]) for name in
                                        ("source_table", "source_key_field", "source_key", "amount_field"))
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in (table,key_field,field)):
            findings.append({"source_table": table, "source_key": key,
                             "amount_field": field, "status": "invalid_inventory_identifier"})
            continue
        columns = {item[1] for item in conn.execute(f'PRAGMA table_info("{table}")')}
        if key_field not in columns or field not in columns:
            findings.append({"source_table": table, "source_key": key,
                             "amount_field": field, "status": "missing_source_field"})
            continue
        source = conn.execute(
            f'SELECT "{field}" FROM "{table}" WHERE "{key_field}"=?', (key,)
        ).fetchone()
        if source is None:
            findings.append({"source_table": table, "source_key": key,
                             "amount_field": field, "status": "missing_source_row"})
            continue
        if source[0] is None:
            continue
        expected = to_minor_units(source[0], int(row["decimal_places"]))
        actual = int(row["amount_minor_units"])
        if expected != actual:
            findings.append({"source_table": table, "source_key": key,
                             "amount_field": field, "currency_code": row["currency_code"],
                             "expected_minor_units": expected, "snapshot_minor_units": actual,
                             "status": "mismatch"})
    return findings


def round_currency(conn, value, currency_code):
    return round_money(value, currency_precision(conn, currency_code))


def parse_currency_money(conn, value, currency_code, *, minimum=0):
    """Parse a native-currency amount without accepting sub-minimum-unit digits."""
    if isinstance(value, bool):
        raise ValueError(f"{currency_code} 금액을 확인해주세요.")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{currency_code} 금액을 확인해주세요.") from None
    places = currency_precision(conn, currency_code)
    quantum = Decimal(1).scaleb(-places)
    if (not amount.is_finite() or amount < Decimal(str(minimum))
            or amount != amount.quantize(quantum)):
        raise ValueError(
            f"{currency_code} 금액은 0 이상, 소수점 {places}자리까지 입력해주세요."
        )
    return int(amount) if places == 0 else float(amount)


def parse_money(value, *, minimum=0):
    """Parse a whole-NWX user/configured amount; reject fractional currency."""
    if isinstance(value, bool):
        raise ValueError("금액은 정수 NWX로 입력해주세요.")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 정수 NWX로 입력해주세요.") from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("금액은 소수점 없는 정수 NWX로 입력해주세요.")
    result = int(amount)
    if result < minimum:
        raise ValueError(f"금액은 {minimum:,} NWX 이상이어야 합니다.")
    return result
