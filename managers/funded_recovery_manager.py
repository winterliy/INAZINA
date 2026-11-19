"""Charge-off recovery funded from the borrower's own home-currency wallet."""
from decimal import Decimal

from managers.accounting_journal import AccountingJournal
from managers.compliance_manager import ComplianceManager
from managers.credit_risk_manager import CreditRiskManager
from managers.currency_manager import CurrencyManager
from managers.rate_manager import RateManager


def record_funded_recovery(conn, *, loan_id, amount_minor_units, event_key, actor_id, now=None):
    """Debit actual borrower cash and record the bank recovery in one transaction."""
    if not conn.in_transaction:
        raise ValueError('caller must start a transaction')
    ComplianceManager._require_admin(conn, actor_id)
    key = str(event_key or '').strip()
    if (type(loan_id) is not int or loan_id <= 0 or type(amount_minor_units) is not int
            or amount_minor_units <= 0 or not key or len(key) > 160):
        raise ValueError('invalid funded recovery')
    epoch = RateManager._validated_epoch(now, 'invalid_loan_recovery_epoch')
    loan = conn.execute('SELECT user_id,status,quote_currency,charged_off_balance_minor_units FROM accounts.loans WHERE loan_id=?', (loan_id,)).fetchone()
    if not loan or loan['status'] != 'charged_off' or not loan['quote_currency']:
        raise ValueError('charged-off loan with currency required')
    borrower, currency = str(loan['user_id']), str(loan['quote_currency']).upper()
    places = CurrencyManager.precision(conn, currency)
    if CurrencyManager.home(conn, borrower) != currency:
        raise ValueError('borrower home currency must match loan currency')
    prior = conn.execute("SELECT event_id,loan_id,event_type,amount_minor_units,currency_code FROM accounts.loan_chargeoff_events WHERE event_key=?", (key,)).fetchone()
    if prior:
        linked = conn.execute("""SELECT w.user_id,w.currency_code,w.delta_minor_units FROM business_cash_events e
            JOIN business_cash_links l ON l.event_id=e.event_id JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
            WHERE e.kind='funded_chargeoff_recovery' AND e.reference=?""", (key,)).fetchall()
        if (prior['event_type'] != 'recovery' or int(prior['loan_id']) != loan_id
                or int(prior['amount_minor_units']) != amount_minor_units or prior['currency_code'] != currency
                or len(linked) != 1 or tuple(linked[0]) != (borrower, currency, -amount_minor_units)):
            raise ValueError('recovery key already used with different details')
        return {'eventId': int(prior['event_id']), 'duplicate': True}
    if loan['charged_off_balance_minor_units'] is None or amount_minor_units > int(loan['charged_off_balance_minor_units']):
        raise ValueError('recovery exceeds charged-off balance')
    if CurrencyManager.balance(conn, borrower, currency) < amount_minor_units * 10 ** (6 - places):
        raise ValueError('borrower wallet has insufficient funds')
    conn.execute('SAVEPOINT funded_recovery')
    try:
        mark = AccountingJournal.cash_mark(conn)
        CurrencyManager.change(conn, borrower, currency, -amount_minor_units * 10 ** (6 - places))
        result = CreditRiskManager.record_chargeoff_recovery(
            conn, loan_id=loan_id, amount=Decimal(amount_minor_units) / (10 ** places),
            event_key=key, actor_id=actor_id, now=epoch, memo='funded from borrower home wallet')
        if result['duplicate'] or AccountingJournal.link_cash(conn, 'funded_chargeoff_recovery', key, mark, epoch) != 1:
            raise ValueError('funded recovery wallet link mismatch')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT funded_recovery')
        conn.execute('RELEASE SAVEPOINT funded_recovery')
        raise
    conn.execute('RELEASE SAVEPOINT funded_recovery')
    return result
