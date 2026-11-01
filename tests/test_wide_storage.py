import sqlite3
from contextlib import closing
from decimal import Decimal
from database.wide_values import register, canonical, decoded
from database.wide_sql import compile_sql


def test_exact_sql_add_sum_and_conditional_debit():
    conn=sqlite3.connect(':memory:')
    register(conn)
    conn.execute(compile_sql('CREATE TABLE wallets (cash INTEGER)'))
    for value in (10**22+1,10**22+2):conn.execute('INSERT INTO wallets VALUES (?)',(canonical(value),))
    total=conn.execute(compile_sql('SELECT SUM(cash) FROM wallets')).fetchone()[0]
    assert decoded(total,True)==2*10**22+3
    conn.execute(compile_sql('UPDATE wallets SET cash=cash-1 WHERE cash>=1'))
    assert decoded(conn.execute(compile_sql('SELECT SUM(cash) FROM wallets')).fetchone()[0],True)==2*10**22+1
    conn.close()


def test_money_ids_are_not_numeric_comparisons():
    conn=sqlite3.connect(':memory:');register(conn)
    conn.execute('CREATE TABLE users(user_id TEXT,cash TEXT)')
    conn.execute('INSERT INTO users VALUES (?,?)',('001',canonical(10**22)))
    assert conn.execute(compile_sql('SELECT COUNT(*) FROM users WHERE user_id=?'),('1',)).fetchone()[0]==0
    conn.close()


def test_decimal_cast_and_trigger_remain_exact():
    conn=sqlite3.connect(':memory:');register(conn)
    conn.execute(compile_sql('CREATE TABLE wallets(cash INTEGER, balance_units INTEGER)'))
    conn.execute(compile_sql('CREATE TRIGGER sync AFTER UPDATE OF cash ON wallets BEGIN UPDATE wallets SET balance_units=CAST(cash*1000000 AS INTEGER); END'))
    conn.execute('INSERT INTO wallets VALUES (?,0)',(canonical(10**18),))
    conn.execute(compile_sql('UPDATE wallets SET cash=cash+1'))
    assert decoded(conn.execute('SELECT balance_units FROM wallets').fetchone()[0],True)==(10**18+1)*10**6
    conn.close()


def test_copy_migration_keeps_source_and_exact_value(tmp_path):
    from database.wide_migration import migrate_copy
    from database.unified import UnifiedConnection
    source,target=tmp_path/'old.db',tmp_path/'new.db'
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('CREATE TABLE wallets(id INTEGER PRIMARY KEY AUTOINCREMENT,cash INTEGER)')
        conn.execute('INSERT INTO wallets(cash) VALUES (?)',(10**18+1,))
    assert migrate_copy(source,target)['wallets']==1
    with closing(sqlite3.connect(source)) as conn, conn:
        assert conn.execute('SELECT cash FROM wallets').fetchone()[0]==10**18+1
    with closing(sqlite3.connect(target,factory=UnifiedConnection)) as conn, conn:
        assert conn.wide_money
        assert Decimal(str(conn.execute('SELECT cash FROM wallets').fetchone()[0]))==10**18+1
        conn.execute('UPDATE wallets SET cash=cash-1')
        assert Decimal(str(conn.execute('SELECT cash FROM wallets').fetchone()[0]))==10**18


def test_money_average_and_computed_ordering():
    with closing(sqlite3.connect(':memory:')) as conn:
        register(conn)
        conn.execute(compile_sql('CREATE TABLE wallets(name TEXT,cash REAL)'))
        conn.executemany('INSERT INTO wallets VALUES (?,?)', [('a',canonical(10**18+1)),('a',canonical(10**18+3)),('b',canonical(9*10**17))])
        average=conn.execute(compile_sql("SELECT AVG(cash) FROM wallets WHERE name='a'")).fetchone()[0]
        assert Decimal(str(decoded(average)))==10**18+2
        rows=conn.execute(compile_sql('SELECT name,SUM(cash) AS total FROM wallets GROUP BY name ORDER BY total DESC')).fetchall()
        assert [row[0] for row in rows]==['a','b']


def test_registered_native_money_division():
    with closing(sqlite3.connect(':memory:')) as conn:
        register(conn)
        conn.execute(compile_sql('CREATE TABLE tax_transactions(gross_amount REAL)'))
        conn.execute('INSERT INTO tax_transactions VALUES (?)',(canonical(10**18+1),))
        result=conn.execute(compile_sql('SELECT gross_amount/3 FROM tax_transactions')).fetchone()[0]
        assert Decimal(str(decoded(result)))==Decimal(10**18+1)/3


def test_computed_money_sort_mixes_small_and_large_balances():
    with closing(sqlite3.connect(':memory:')) as conn:
        register(conn)
        conn.execute('CREATE TABLE wallets(name TEXT,cash)')
        conn.executemany('INSERT INTO wallets VALUES (?,?)',[('negative',canonical(-10**18)),('positive',1)])
        rows=conn.execute(compile_sql('SELECT name,SUM(cash) AS total FROM wallets GROUP BY name ORDER BY total DESC')).fetchall()
        assert [row[0] for row in rows]==['positive','negative']
