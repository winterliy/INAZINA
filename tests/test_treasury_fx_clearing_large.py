import sqlite3

from managers.accounting_journal import AccountingJournal


def test_clearing_positions_sum_beyond_sqlite_integer_limit():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE treasury_bond_cash_fx_settlements (
          source_currency TEXT, source_decimal_places INTEGER, source_minor_units INTEGER,
          settlement_currency TEXT, settlement_decimal_places INTEGER, settlement_minor_units INTEGER);
        CREATE TABLE accounting_entries (entry_id INTEGER, currency_code TEXT, decimal_places INTEGER, ledger_scope TEXT);
        CREATE TABLE accounting_lines (entry_id INTEGER, account_code TEXT, debit_units INTEGER, credit_units INTEGER);
        CREATE TABLE accounting_account_mappings (ledger_scope TEXT, local_code TEXT, common_code TEXT, active INTEGER);
        INSERT INTO accounting_account_mappings VALUES ('treasury_bond', 'fx_conversion_clearing', 'fx', 1);
    ''')
    amount = 6_000_000_000_000_000_000
    for entry_id in (1, 2):
        conn.execute("INSERT INTO treasury_bond_cash_fx_settlements VALUES ('NWX',0,?,'USD',2,1)", (amount,))
        conn.execute("INSERT INTO accounting_entries VALUES (?,'NWX',0,'treasury_bond')", (entry_id,))
        conn.execute("INSERT INTO accounting_lines VALUES (?,'fx',0,?)", (entry_id, amount))
        conn.execute("INSERT INTO accounting_entries VALUES (?,'USD',2,'treasury_bond')", (entry_id + 2,))
        conn.execute("INSERT INTO accounting_lines VALUES (?,'fx',1,0)", (entry_id + 2,))
    gaps = []
    positions = AccountingJournal._treasury_bond_fx_clearing_positions(conn, gaps)
    assert not gaps
    assert next(p for p in positions if p['currency_code'] == 'NWX')['credit_units'] == 2 * amount
