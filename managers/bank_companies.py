"""Every bank is also a 법인 with its own 법인 계정.

The 법인 is an unlisted company whose code is the bank's ID (``financial_institutions.
institution_id``), named after the bank, with the bank's equity capital as its capital and
its country's treasury as the largest shareholder, like the other state-founded listings.
Its 법인 계정 (``company:<ID>``, see managers/company_account_manager.py) is opened at the
same time, so the bank shows up under 법인 자본 and 계정관리 and can trade, bank and borrow.

Banks are created by the dashboard, by the default bank, and by products naming a new
issuer (a database trigger), so :func:`ensure_all` runs at startup and whenever the bank
list is read; :func:`ensure` runs right after the dashboard creates a bank.
"""
from functions.exact_money import ExactDecimal, native_value
from managers.company_account_manager import CompanyAccounts
from managers.market_session_manager import MarketSessionManager

INDUSTRY = "은행"
SHARES = 1_000_000


def ensure(conn, issuer):
    """Create the bank's 법인 and 법인 계정 if missing. Returns the company code, or None."""
    bank = conn.execute("SELECT issuer, nationality, equity_capital, institution_id FROM financial_institutions "
                        "WHERE issuer=?", (str(issuer),)).fetchone()
    if not bank or not bank["institution_id"]:
        return None
    code = str(bank["institution_id"])
    company = conn.execute("SELECT industry FROM company WHERE company_code=?", (code,)).fetchone()
    if company is None:
        from database import DatabaseManager
        if DatabaseManager.code_locations(conn, code):
            # Another product already uses the bank's ID as its code; never take it over.
            return None
        from managers.currency_manager import CurrencyManager
        capital = ExactDecimal(bank["equity_capital"] or 0)
        conn.execute(
            "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,exchange_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, bank["issuer"], INDUSTRY, capital, SHARES, max(1, round(capital / SHARES)),
             CurrencyManager.treasury_id(conn, bank["nationality"]), MarketSessionManager.UNLISTED))
    elif company["industry"] != INDUSTRY:
        return None
    CompanyAccounts.account_id(conn, code)
    return code


def missing(conn):
    """Issuers of banks that have an ID but no 법인 계정 yet."""
    return [row[0] for row in conn.execute(
        "SELECT f.issuer FROM financial_institutions f WHERE f.institution_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM company_accounts a WHERE a.company_code=f.institution_id) ORDER BY f.rowid")]


def ensure_all(conn):
    """Give every bank still without one its 법인 and 법인 계정. Returns the codes created."""
    return [code for code in (ensure(conn, issuer) for issuer in missing(conn)) if code]
