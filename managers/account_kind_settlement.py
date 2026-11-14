"""통장 종류별 해지 정산과 연말정산 (rules in managers/deposit_accounts.py KIND_TERMS).

- 주택청약: interest is paid only at closing, simple interest on every
  payment at the rate of the whole holding period (주택청약종합저축 이자율 고시).
- ISA: interest was credited untaxed; closing after the 의무가입기간 taxes the
  part above the tax-free amount at the separate rate, earlier closing loses the
  benefit and pays the ordinary rate.
- IRP: contributions that received a 세액공제 refund and the untaxed interest are
  taxed at closing: 기타소득세 before pension age, 연금소득세 after.
- IRP 세액공제 itself is settled once a year (연말정산) for the past year.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from functions.exact_money import ExactDecimal
from functions.money import round_currency

YEAR = 365 * 86400
KST = timezone(timedelta(hours=9))


def _terms(row):
    return json.loads(row["kind_terms"] or "{}") if row["kind_terms"] else {}


def _opened(row):
    return datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _tier_rate(tiers, held_seconds):
    for tier in tiers:
        if tier.get("days") is None or held_seconds < int(tier["days"]) * 86400:
            return ExactDecimal(tier.get("rate", 0))
    return ExactDecimal(0)


def closing(conn, row, currency, now=None):
    """(interest the bank adds, tax withheld, label) for closing ``row``."""
    from managers.interest_manager import InterestManager
    now = time.time() if now is None else now
    kind, terms = row["account_kind"], _terms(row)
    held = now - _opened(row)
    if kind == "housing":
        rate = _tier_rate(terms.get("rate_tiers") or [], held)
        interest = ExactDecimal(0)
        for paid in conn.execute("SELECT units,epoch FROM account_contributions WHERE account_number=?",
                                 (row["account_number"],)):
            interest += (ExactDecimal(paid["units"]) / 1000000 * rate / 100
                         * ExactDecimal(max(0, now - paid["epoch"])) / YEAR)
        interest = round_currency(conn, interest, currency)
        tax = round_currency(conn, interest * InterestManager._tax_rate(conn, row["user_id"], "saving_interest") / 100,
                             currency)
        return interest, tax, f"주택청약 이자(가입기간 금리 연 {rate}%)"
    deferred = ExactDecimal(row["deferred_interest_units"] or 0) / 1000000
    if kind == "isa":
        if held >= int(terms.get("lock_days", 0)) * 86400:
            taxable = max(ExactDecimal(0), deferred - ExactDecimal(terms.get("tax_free", 0)))
            tax = round_currency(conn, taxable * ExactDecimal(terms.get("separate_tax_percent", 0)) / 100, currency)
            return 0, tax, "ISA 비과세·분리과세 정산"
        tax = round_currency(conn, deferred * InterestManager._tax_rate(conn, row["user_id"], "saving_interest") / 100,
                             currency)
        return 0, tax, "ISA 의무가입기간 전 해지(일반과세)"
    if kind == "irp":
        base = ExactDecimal(row["credited_contribution_units"] or 0) / 1000000 + deferred
        pension = held >= int(terms.get("pension_after_days", 0)) * 86400
        percent = terms.get("pension_tax_percent" if pension else "early_tax_percent", 0)
        tax = round_currency(conn, base * ExactDecimal(percent) / 100, currency)
        return 0, tax, ("IRP 연금소득세" if pension else "IRP 중도해지 기타소득세")
    return 0, 0, ""


def settle_irp_tax_credits(conn, now=None):
    """연말정산: refund 세액공제 on last years' IRP contributions to each owner's wallet.

    Contributions of a calendar year (KST) are settled once that year is over,
    up to the yearly credit limit, at the low-income rate when the owner's
    annual income is at most the threshold. Returns notices.
    """
    from managers.currency_manager import CurrencyManager, SCALE
    now = time.time() if now is None else now
    this_year = datetime.fromtimestamp(now, KST).year
    rows = conn.execute(
        "SELECT c.contribution_id,c.account_number,c.user_id,c.units,c.epoch,a.kind_terms,a.quote_currency "
        "FROM account_contributions c JOIN accounts a ON a.account_number=c.account_number "
        "WHERE c.account_kind='irp' AND c.credited_epoch IS NULL ORDER BY c.epoch").fetchall()
    groups = {}
    for row in rows:
        year = datetime.fromtimestamp(row["epoch"], KST).year
        if year < this_year:
            groups.setdefault((row["user_id"], year), []).append(row)
    notices = []
    for (user_id, year), items in groups.items():
        terms = json.loads(items[0]["kind_terms"] or "{}")
        already_this_year = sum(
            int(r["credit_units"]) for r in conn.execute(
                "SELECT credit_units,epoch FROM account_contributions WHERE user_id=? AND account_kind='irp' "
                "AND credited_epoch IS NOT NULL", (user_id,))
            if datetime.fromtimestamp(r["epoch"], KST).year == year)
        room = max(0, int(round(ExactDecimal(terms.get("credit_limit", 0)) * SCALE)) - already_this_year)
        income = conn.execute("SELECT annual_income FROM users WHERE user_id=?", (user_id,)).fetchone()
        low = income is not None and ExactDecimal(income[0] or 0) <= ExactDecimal(terms.get("income_threshold", 0))
        percent = ExactDecimal(terms.get("credit_percent_low_income" if low else "credit_percent", 0))
        home = CurrencyManager.home(conn, user_id)
        refund_units = 0
        for item in items:
            eligible = min(int(item["units"]), room)
            room -= eligible
            conn.execute("UPDATE account_contributions SET credit_units=?,credited_epoch=? WHERE contribution_id=?",
                         (eligible, now, item["contribution_id"]))
            if eligible:
                conn.execute("UPDATE accounts SET credited_contribution_units=credited_contribution_units+? "
                             "WHERE account_number=?", (eligible, item["account_number"]))
                refund = int(ExactDecimal(eligible) * percent / 100)
                refund_units += (CurrencyManager.convert(conn, refund, item["quote_currency"], home)
                                 if item["quote_currency"] != home else refund)
        if refund_units:
            treasury = CurrencyManager.user_treasury_id(conn, user_id)
            CurrencyManager.change(conn, treasury, home, -refund_units, allow_deficit=True)
            CurrencyManager.change(conn, user_id, home, refund_units)
            amount = ExactDecimal(refund_units) / SCALE
            conn.execute("INSERT INTO tax_transactions(user_id,source,gross_amount,tax_amount,net_amount,currency_code) "
                         "VALUES (?,?,?,?,?,?)", (user_id, "irp_tax_credit", 0, -amount, amount, home))
            notices.append({"user_id": user_id, "year": year, "refund": amount, "currency": home,
                            "percent": float(percent)})
    return notices
