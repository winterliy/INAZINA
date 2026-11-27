"""Dated lending rules by country, customer type and product type (M7).

A rule applies from its effective epoch. A contract looks up the rule in force on its own contract date, and only rules registered
before the contract was signed bind it (a rule dated backwards never changes a signed contract). The most specific rule wins (customer and product both named beats one
named beats neither), then the latest effective date; each field falls through to the next candidate when a rule leaves
it empty. With no rule the caller keeps its old behaviour (the country's ``legal_max_rate``). Simulator rules only.
"""
CUSTOMER_TYPES = ("*", "individual", "corporate")
PRODUCT_TYPES = ("*", "loan", "deposit")
FIELDS = ("max_rate_bps", "apr_display", "tax_bps")


def resolve(conn, nationality, *, customer_type="individual", product_type="loan", epoch):
    """The rule values in force on ``epoch`` as ``{field: value or None, "rule_ids": [...]}``, or None when no rule matches."""
    rows = conn.execute(
        "SELECT * FROM jurisdiction_rate_rules WHERE nationality=? AND effective_epoch<=? AND created_epoch<=? "
        "AND customer_type IN ('*',?) AND product_type IN ('*',?)",
        (str(nationality), int(epoch), int(epoch), customer_type, product_type)).fetchall()
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: ((r["customer_type"] != "*") + (r["product_type"] != "*"),
                                       r["effective_epoch"], r["rule_id"]), reverse=True)
    result, used = {field: None for field in FIELDS}, []
    for row in rows:
        for field in FIELDS:
            if result[field] is None and row[field] is not None:
                result[field] = row[field]
                if row["rule_id"] not in used:
                    used.append(row["rule_id"])
    if not used:
        return None
    return {**result, "rule_ids": used}


def max_rate_percent(conn, nationality, *, customer_type="individual", product_type="loan", epoch):
    """The rule's maximum annual percent for the contract date, or None to keep the country's legal_max_rate."""
    if epoch is None or not nationality:
        return None
    rule = resolve(conn, nationality, customer_type=customer_type, product_type=product_type, epoch=epoch)
    return None if rule is None or rule["max_rate_bps"] is None else rule["max_rate_bps"] / 100


def add_rule(conn, *, nationality, customer_type, product_type, effective_epoch, max_rate_bps=None, apr_display=None,
             tax_bps=None, note="", actor, now):
    """Append a rule. Existing rows are never changed; a correction is a new rule with a later effective date."""
    nationality = str(nationality or "").strip()
    if not nationality or len(nationality) > 100 or not conn.execute(
            "SELECT 1 FROM country_currencies WHERE nationality=?", (nationality,)).fetchone():
        raise ValueError("unknown country")
    if customer_type not in CUSTOMER_TYPES or product_type not in PRODUCT_TYPES:
        raise ValueError("invalid customer or product type")
    for value, low, high in ((effective_epoch, 0, 4_102_444_800), (max_rate_bps, 0, 10000), (tax_bps, 0, 10000)):
        if value is not None and (type(value) is not int or not low <= value <= high):
            raise ValueError("invalid rule number")
    if effective_epoch is None:
        raise ValueError("effective date required")
    if apr_display not in (None, "required", "optional"):
        raise ValueError("invalid apr display")
    if max_rate_bps is None and apr_display is None and tax_bps is None:
        raise ValueError("a rule needs at least one value")
    note = str(note or "").strip()
    if len(note) > 300:
        raise ValueError("note too long")
    if conn.execute("SELECT 1 FROM jurisdiction_rate_rules WHERE nationality=? AND customer_type=? AND product_type=? "
                    "AND effective_epoch=?", (nationality, customer_type, product_type, effective_epoch)).fetchone():
        raise ValueError("a rule for that scope and date already exists")
    cursor = conn.execute(
        "INSERT INTO jurisdiction_rate_rules(nationality,customer_type,product_type,effective_epoch,max_rate_bps,apr_display,"
        "tax_bps,note,created_by,created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (nationality, customer_type, product_type, effective_epoch, max_rate_bps, apr_display, tax_bps, note, str(actor), int(now)))
    return cursor.lastrowid


def list_rules(conn, nationality=None):
    sql = "SELECT * FROM jurisdiction_rate_rules"
    rows = conn.execute(sql + (" WHERE nationality=?" if nationality else "") + " ORDER BY nationality,effective_epoch DESC,rule_id DESC",
                        (nationality,) if nationality else ()).fetchall()
    return [dict(row) for row in rows]


def customer_type_for(user_id):
    """Company accounts are ``company:<code>``; every other account is an individual."""
    return "corporate" if str(user_id or "").startswith("company:") else "individual"


def contract_max_rate_percent(conn, nationality, user_id, contract_date, *, product_type="loan"):
    """The rule's maximum annual percent on the contract date (a UTC ``YYYY-MM-DD HH:MM:SS`` text or epoch), or None."""
    from datetime import datetime, timezone
    if contract_date is None or not nationality:
        return None
    if isinstance(contract_date, datetime):
        contract_date = (contract_date if contract_date.tzinfo else contract_date.replace(tzinfo=timezone.utc)).timestamp()
    elif isinstance(contract_date, str):
        try:
            contract_date = contract_date.split(".")[0].replace("T", " ")
            contract_date = datetime.strptime(contract_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    return max_rate_percent(conn, nationality, customer_type=customer_type_for(user_id), product_type=product_type,
                            epoch=int(contract_date))


def apr_display_for(conn, user_id, *, epoch, product_type="loan"):
    """The country rule's APR display setting ('required', 'optional') for this user's country today, or None when no rule says."""
    holder = conn.execute("SELECT nationality FROM users WHERE user_id=?", (str(user_id),)).fetchone()
    if not holder or not holder["nationality"]:
        return None
    rule = resolve(conn, holder["nationality"], customer_type=customer_type_for(user_id), product_type=product_type, epoch=int(epoch))
    return None if rule is None else rule["apr_display"]
