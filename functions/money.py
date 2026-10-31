"""Currency-aware decimal helpers.

Database values are expressed in the named currency (not always 기준 통화).  Calculated
amounts are rounded half-up to that currency's configured smallest unit.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from functions.exact_money import native_value
from functions.amount_limits import MAX_NATIVE


_WHOLE = Decimal("1")
SUBMINOR_SCALE = 1_000_000

def base_currency_code(conn):
    """Read the configured simulation base currency from the currency ledger."""
    try:
        row = conn.execute("SELECT base_currency_code FROM currency_system_settings "
                           "WHERE singleton_id=1").fetchone()
    except Exception as exc:
        if "no such table" in str(exc).lower():
            raise ValueError("기본 통화 설정이 초기화되지 않았습니다.") from None
        raise
    if not row or not str(row[0] or "").strip():
        raise ValueError("기본 통화를 설정해주세요.")
    code = str(row[0]).strip().upper()
    active = conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'", (code,)).fetchone()
    if not active:
        raise ValueError(f"활성 기본 통화를 찾을 수 없습니다: {code}")
    return code


def round_money(value, decimal_places=0):
    """Round a calculated amount half-up and return a SQLite-bindable number."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 유한한 숫자여야 합니다.") from None
    if not amount.is_finite() or abs(amount) > MAX_NATIVE:
        raise ValueError("금액은 유한한 숫자여야 합니다.")
    try:
        places = int(decimal_places)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("통화 소수 자릿수가 올바르지 않습니다.") from None
    if not 0 <= places <= 6:
        raise ValueError("통화 소수 자릿수는 0~6이어야 합니다.")
    rounded = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return native_value(rounded)


def currency_precision(conn, currency_code):
    row = conn.execute(
        "SELECT decimal_places FROM currencies WHERE code=?",
        (str(currency_code or "").upper(),),
    ).fetchone()
    if not row:
        raise ValueError(f"통화를 확인해주세요: {currency_code}")
    places = int(row[0])
    if not 0 <= places <= 6:
        raise ValueError("통화 소수 자릿수는 0~6이어야 합니다.")
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
    if not amount.is_finite() or abs(amount) > MAX_NATIVE:
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


def to_subminor_units(value, decimal_places):
    """Store a fractional currency remainder at one-millionth of a minor unit."""
    try:
        amount = Decimal(str(value))
        places = int(decimal_places)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise ValueError("금액 또는 통화 소수 자릿수가 올바르지 않습니다.") from None
    if not amount.is_finite() or abs(amount) > MAX_NATIVE or not 0 <= places <= 6:
        raise ValueError("금액 또는 통화 소수 자릿수가 올바르지 않습니다.")
    scaled = amount * (10 ** places) * SUBMINOR_SCALE
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_subminor_units(units, decimal_places):
    """Return an exact Decimal currency amount from fractional-minor units."""
    if isinstance(units, bool):
        raise ValueError("최소단위 잔여는 정수여야 합니다.")
    try:
        integer_units = int(units)
        places = int(decimal_places)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("최소단위 잔여 또는 통화 소수 자릿수가 올바르지 않습니다.") from None
    if integer_units != units or not 0 <= places <= 6:
        raise ValueError("최소단위 잔여는 정수이며 통화 소수 자릿수는 0~6이어야 합니다.")
    return Decimal(integer_units).scaleb(-(places+6))


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
    rowid_tables = {str(row[1]) for row in conn.execute("PRAGMA table_list").fetchall()
                    if len(row) > 4 and int(row[4]) == 0}
    findings = []
    registry_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_amount_field_registry'"
    ).fetchone()
    if registry_exists:
        specifications = conn.execute(
            "SELECT source_table,source_key_field,amount_field "
            "FROM financial_amount_field_registry "
            "UNION SELECT source_table,source_key_field,amount_field "
            "FROM financial_amount_snapshots"
        ).fetchall()
    else:
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
        if key_field == "rowid" and table in rowid_tables:
            columns.add("rowid")
        if key_field not in columns or field not in columns:
            findings.append({"source_table": table, "source_key": "*",
                             "amount_field": field, "status": "missing_source_field"})
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
    # Compare every snapshot with its source in one join per (table, key, field) instead of
    # two queries per snapshot row; findings keep the (table, key, field) order.
    checked = []
    for spec in conn.execute("SELECT DISTINCT source_table,source_key_field,amount_field "
                             "FROM financial_amount_snapshots").fetchall():
        table, key_field, field = (str(spec[name]) for name in
                                   ("source_table", "source_key_field", "amount_field"))
        valid = all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in (table, key_field, field))
        columns = {item[1] for item in conn.execute(f'PRAGMA table_info("{table}")')} if valid else set()
        if key_field == "rowid" and table in rowid_tables:
            columns.add("rowid")
        if not valid or key_field not in columns or field not in columns:
            status = "missing_source_field" if valid else "invalid_inventory_identifier"
            checked.extend({"source_table": table, "source_key": str(row[0]), "amount_field": field,
                            "status": status} for row in conn.execute(
                "SELECT source_key FROM financial_amount_snapshots WHERE source_table=? "
                "AND source_key_field=? AND amount_field=?", (table, key_field, field)))
            continue
        # The source key compares with the TEXT snapshot key under the column's own affinity,
        # so integer keys still use their index.
        for row in conn.execute(
                f'''SELECT snap.source_key, snap.decimal_places, snap.amount_minor_units, snap.currency_code,
                           src."{field}" AS amount, src."{key_field}" IS NULL AS absent
                    FROM financial_amount_snapshots snap LEFT JOIN "{table}" src
                      ON src."{key_field}"=snap.source_key
                    WHERE snap.source_table=? AND snap.source_key_field=? AND snap.amount_field=?''',
                (table, key_field, field)):
            key = str(row["source_key"])
            if row["absent"]:
                checked.append({"source_table": table, "source_key": key,
                                "amount_field": field, "status": "missing_source_row"})
                continue
            if row["amount"] is None:
                continue
            expected = to_minor_units(row["amount"], int(row["decimal_places"]))
            actual = int(row["amount_minor_units"])
            if expected != actual:
                checked.append({"source_table": table, "source_key": key,
                                "amount_field": field, "currency_code": row["currency_code"],
                                "expected_minor_units": expected, "snapshot_minor_units": actual,
                                "status": "mismatch"})
    checked.sort(key=lambda item: (item["source_table"], item["source_key"], item["amount_field"]))
    findings.extend(checked)
    return findings


def audit_amount_field_coverage(conn):
    """Find money-shaped numeric columns without registration or an explicit reason.

    This schema heuristic complements row-level snapshots. It intentionally
    returns candidates instead of guessing that names such as ``value`` or
    ``price`` always represent currency.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_amount_field_registry'"
    ).fetchone()
    exemption_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_amount_field_exemptions'"
    ).fetchone()
    if not table_exists or not exemption_exists:
        return {"status": "missing_inventory", "registered_count": 0,
                "exempt_count": 0, "unclassified": [{"status": "missing_inventory"}]}

    registered = {(str(row[0]), str(row[1])) for row in conn.execute(
        "SELECT source_table,amount_field FROM financial_amount_field_registry"
    ).fetchall()}
    exemptions = {(str(row[0]), str(row[1])) for row in conn.execute(
        "SELECT source_table,amount_field FROM financial_amount_field_exemptions"
    ).fetchall()}
    money_tokens = {
        "amount", "balance", "price", "cost", "fee", "tax", "cash", "principal",
        "income", "revenue", "proceeds", "premium", "capital", "allowance", "payout",
        "discount", "nav", "face", "strike", "payment", "dividend", "claim", "reserve",
        "deposit", "earning", "expense", "value",
    }
    non_amount_tokens = {
        "id", "epoch", "percent", "bps", "rate", "ratio", "count", "quantity", "shares",
        "supply", "decimal", "frequency", "days", "seconds", "years", "score", "sensitivity",
        "volatility", "liquidity", "kind", "status", "type", "cycle", "number", "mode",
        "active", "insured", "priority", "remainder", "factor", "weight", "stage", "date", "time",
    }
    numeric_types = {"REAL", "FLOAT", "DOUBLE", "NUMERIC", "INTEGER", "INT"}
    unclassified = []
    tables = conn.execute("PRAGMA table_list").fetchall()
    for table_row in tables:
        table = str(table_row[1])
        if str(table_row[2]) != "table" or table.startswith("sqlite_"):
            continue
        for column in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
            field, declared_type = str(column[1]), str(column[2] or "").upper()
            key = (table, field)
            if declared_type not in numeric_types:
                continue
            # target_value is a percentage for most events, but a quote-currency
            # amount for explicit price-target events. Do not blanket-exempt the
            # column: report it when that monetary mode is actually present.
            if key == ("market_events", "target_value"):
                has_price_targets = conn.execute(
                    "SELECT 1 FROM market_events WHERE target_mode='price' LIMIT 1"
                ).fetchone()
                if not has_price_targets:
                    continue
            if (key in registered or key in exemptions
                    or field.endswith(("_minor", "_minor_units", "_subminor_units", "_decimal_places"))):
                continue
            tokens = set(field.lower().split("_"))
            if tokens & money_tokens and not tokens & non_amount_tokens:
                unclassified.append({"source_table": table, "amount_field": field,
                                     "declared_type": declared_type,
                                     "status": "unclassified_money_candidate"})
    return {"status": "matched" if not unclassified else "unclassified",
            "registered_count": len(registered), "exempt_count": len(exemptions),
            "unclassified": unclassified}


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
    if (not amount.is_finite() or amount > MAX_NATIVE or amount < Decimal(str(minimum))
            or amount != amount.quantize(quantum)):
        raise ValueError(
            f"{currency_code} 금액은 0 이상, 소수점 {places}자리까지 입력해주세요."
        )
    return native_value(amount)


def parse_money(value, *, minimum=0):
    """Parse a whole-unit user/configured amount; reject fractional currency."""
    if isinstance(value, bool):
        raise ValueError("금액은 정수로 입력해주세요.")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 정수로 입력해주세요.") from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("금액은 소수점 없는 정수로 입력해주세요.")
    if abs(amount) > MAX_NATIVE:
        raise ValueError("금액 한도를 초과했습니다.")
    result = int(amount)
    if result < minimum:
        raise ValueError(f"금액은 {minimum:,} 이상이어야 합니다.")
    return result
