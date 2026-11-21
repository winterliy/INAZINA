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
