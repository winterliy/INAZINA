"""Deposit insurance coverage, premium and resolution-allocation preview for one bank (M7).

Read-only simulation. There is no fund ledger yet: the fund balance is not invented, premiums are only assessed on
paper from a rate the caller supplies, and nothing is posted. Coverage follows the bank's own insurance limit per
depositor on insured savings accounts (the rule bank resolution already uses) in the bank's base currency.
"""
from decimal import Decimal, ROUND_FLOOR

from functions.money import currency_precision
from functions.recovery_waterfall import allocate_recovery
from managers.alm_manager import ALMManager
from managers.rate_manager import RateManager

SAFE = 9007199254740991


def _minor(amount, places):
    return int((Decimal(amount) * 10 ** places).to_integral_value(rounding=ROUND_FLOOR))


def preview(conn, issuer, *, premium_bps, proceeds_minor_units, general_claims_minor_units):
    if not all(type(value) is int and 0 <= value <= SAFE for value in (premium_bps, proceeds_minor_units, general_claims_minor_units)) \
            or premium_bps > 10000:
        raise ValueError("invalid_deposit_insurance_input")
    institution = conn.execute("SELECT nationality FROM financial_institutions WHERE issuer=?", (issuer,)).fetchone()
    if not institution:
        raise ValueError("financial_institution_not_found")
    sheet = RateManager.institution_balance_sheet(conn, issuer)
    base = str(sheet["base_currency"])
    places = currency_precision(conn, base)
    limit = max(Decimal(0), ALMManager._dec(RateManager.institution_components(conn, issuer)["deposit_insurance_limit"]))
    owners, accounts = {}, 0
    for row in conn.execute(
            "SELECT a.user_id,a.quote_currency,a.deposit_insured,SUM(MAX(0,CASE WHEN a.account_type='currency_saving' "
            "THEN a.balance_units/1000000.0 ELSE a.balance END)) amount,COUNT(*) n FROM accounts a "
            "JOIN saving s ON s.product_code=a.product_code WHERE s.issuer=? AND a.account_type IN ('saving','currency_saving') "
            "AND a.resolution_status='active' GROUP BY a.user_id,a.quote_currency,a.deposit_insured", (issuer,)):
        amount = ALMManager._to_base(conn, ALMManager._dec(row["amount"]), str(row["quote_currency"] or base), base)
        if amount <= 0:
            continue
        accounts += int(row["n"])
        owner = owners.setdefault(str(row["user_id"]), {"insured": Decimal(0), "uninsured": Decimal(0)})
        owner["insured" if int(row["deposit_insured"] or 0) else "uninsured"] += amount
    covered = sum((min(o["insured"], limit) for o in owners.values()), Decimal(0))
    total = sum((o["insured"] + o["uninsured"] for o in owners.values()), Decimal(0))
    covered_minor, total_minor = _minor(covered, places), _minor(total, places)
    uninsured_minor = total_minor - covered_minor
    if max(total_minor, general_claims_minor_units) > SAFE:
        raise ValueError("deposit_insurance_amount_too_large")
    split = allocate_recovery(proceeds_minor_units, [("insurer_subrogation", 1, covered_minor),
                                                     ("uninsured_depositors", 2, uninsured_minor),
                                                     ("general_creditors", 3, general_claims_minor_units)])
    return {"issuer": issuer, "currencyCode": base, "decimalPlaces": places, "limitMinorUnits": _minor(limit, places),
            "depositorCount": len(owners), "accountCount": accounts, "totalDepositsMinorUnits": total_minor,
            "coveredMinorUnits": covered_minor, "uninsuredMinorUnits": uninsured_minor,
            "premiumBps": premium_bps, "assessedPremiumMinorUnits": covered_minor * premium_bps // 10000,
            "allocations": split.allocations, "shortfalls": split.shortfalls, "unallocatedMinorUnits": split.unallocated,
            "fundLedger": None, "simulationOnly": True}


# ---- fund ledger: premiums in, payouts out (admin-triggered only; nothing runs automatically) -------------------------------------------
from managers.bank_journal import BankJournal                      # noqa: E402
from managers.compliance_manager import ComplianceManager         # noqa: E402


def collect_premium(conn, issuer, *, premium_bps, event_key, actor_id, now):
    """Charge one premium to ``issuer``: bank journal (premium expense / settlement asset) plus a fund ledger premium event."""
    if not conn.in_transaction:
        raise ValueError("caller must start a transaction")
    ComplianceManager._require_admin(conn, actor_id)
    key = str(event_key or "").strip()
    if not 8 <= len(key) <= 120 or type(now) is not int:
        raise ValueError("invalid deposit insurance premium")
    quote = preview(conn, issuer, premium_bps=premium_bps, proceeds_minor_units=0, general_claims_minor_units=0)
    amount, currency, places = quote["assessedPremiumMinorUnits"], quote["currencyCode"], quote["decimalPlaces"]
    prior = conn.execute("SELECT * FROM deposit_insurance_fund_events WHERE event_key=?", (key,)).fetchone()
    if prior:
        if (prior["kind"], prior["issuer"]) != ("premium", issuer):
            raise ValueError("premium key already used with different details")
        return {"eventId": int(prior["event_id"]), "duplicate": True, "amountMinorUnits": int(prior["amount_minor_units"])}
    if amount <= 0:
        raise ValueError("assessed premium is zero")
    conn.execute("SAVEPOINT di_premium")
    try:
        posted = BankJournal.post(conn, issuer, "deposit_insurance_premium", f"di-premium:{key}", currency,
                                  [("deposit_insurance_premium_expense", "debit", Decimal(amount) / 10 ** places),
                                   ("settlement_asset", "credit", Decimal(amount) / 10 ** places)],
                                  description=f"Deposit insurance premium {premium_bps}bp", now=now)
        if not posted:
            raise ValueError("premium journal was not posted")
        cursor = conn.execute("INSERT INTO deposit_insurance_fund_events(event_key,kind,issuer,currency_code,amount_minor_units,reference,actor_id,created_epoch) "
                              "VALUES (?,?,?,?,?,?,?,?)", (key, "premium", issuer, currency, amount, f"{premium_bps}bp", str(actor_id), now))
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT di_premium")
        conn.execute("RELEASE SAVEPOINT di_premium")
        raise
    conn.execute("RELEASE SAVEPOINT di_premium")
    return {"eventId": cursor.lastrowid, "duplicate": False, "amountMinorUnits": amount, "currencyCode": currency}


def record_claim_payouts(conn, issuer, *, actor_id, now):
    """Record, once per claim, the fund payout for each resolved deposit insurance claim the bank's resolution already paid."""
    if not conn.in_transaction:
        raise ValueError("caller must start a transaction")
    ComplianceManager._require_admin(conn, actor_id)
    recorded = 0
    for claim in conn.execute("SELECT claim_id,payout_currency,payout_amount_minor_units FROM deposit_insurance_claims WHERE issuer=? "
                              "AND payout_currency IS NOT NULL AND payout_amount_minor_units>0 ORDER BY claim_id", (issuer,)).fetchall():
        cursor = conn.execute("INSERT OR IGNORE INTO deposit_insurance_fund_events(event_key,kind,issuer,currency_code,amount_minor_units,reference,actor_id,created_epoch) "
                              "VALUES (?,?,?,?,?,?,?,?)", (f"claim:{claim['claim_id']}", "payout", issuer, str(claim["payout_currency"]),
                                                          int(claim["payout_amount_minor_units"]), f"claim {claim['claim_id']}", str(actor_id), now))
        recorded += cursor.rowcount
    return {"recorded": recorded}


def fund_overview(conn):
    """Fund balance per currency (premiums minus payouts); a negative balance is shown as a shortfall, never hidden."""
    rows = conn.execute("SELECT currency_code,kind,SUM(amount_minor_units) total,COUNT(*) n FROM deposit_insurance_fund_events GROUP BY currency_code,kind").fetchall()
    result = {}
    for row in rows:
        entry = result.setdefault(row["currency_code"], {"premiumsMinorUnits": 0, "payoutsMinorUnits": 0, "events": 0})
        entry["premiumsMinorUnits" if row["kind"] == "premium" else "payoutsMinorUnits"] = int(row["total"])
        entry["events"] += int(row["n"])
    for currency, entry in result.items():
        entry["balanceMinorUnits"] = entry["premiumsMinorUnits"] - entry["payoutsMinorUnits"]
        entry["shortfallMinorUnits"] = max(0, -entry["balanceMinorUnits"])
        entry["decimalPlaces"] = currency_precision(conn, currency)
    return result


def reconcile(conn):
    """Fund premiums against the banks' own premium journal lines; payouts against the claims' recorded payouts."""
    problems = []
    for row in conn.execute("SELECT issuer,currency_code,SUM(amount_minor_units) total FROM deposit_insurance_fund_events WHERE kind='premium' GROUP BY issuer,currency_code").fetchall():
        journal = conn.execute("SELECT COALESCE(SUM(l.debit_units-l.credit_units),0) FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
                               "WHERE e.issuer=? AND e.currency_code=? AND e.status='posted' AND l.account_code='deposit_insurance_premium_expense'",
                               (row["issuer"], row["currency_code"])).fetchone()[0]
        if int(journal) != int(row["total"]):
            problems.append({"kind": "premium", "issuer": row["issuer"], "currency": row["currency_code"], "fund": int(row["total"]), "journal": int(journal)})
    for row in conn.execute("SELECT e.event_key,e.amount_minor_units,c.payout_amount_minor_units FROM deposit_insurance_fund_events e "
                            "LEFT JOIN deposit_insurance_claims c ON 'claim:'||c.claim_id=e.event_key WHERE e.kind='payout'").fetchall():
        if row["payout_amount_minor_units"] is None or int(row["payout_amount_minor_units"]) != int(row["amount_minor_units"]):
            problems.append({"kind": "payout", "eventKey": row["event_key"], "fund": int(row["amount_minor_units"]),
                             "claim": None if row["payout_amount_minor_units"] is None else int(row["payout_amount_minor_units"])})
    return problems
