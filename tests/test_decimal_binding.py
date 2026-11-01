import sqlite3
from decimal import Decimal
import pytest
from database.unified import UnifiedConnection

class LoanAmount(Decimal):
    pass


def test_decimal_loan_amount_bindings_support_named_and_batch_parameters():
    with sqlite3.connect(':memory:',factory=UnifiedConnection) as conn:
        conn.execute('CREATE TABLE repayments(principal INTEGER,interest REAL)')
        conn.execute('INSERT INTO repayments VALUES (:principal,:interest)',{'principal':LoanAmount('365000'),'interest':LoanAmount('0.25')})
        conn.executemany('INSERT INTO repayments VALUES (?,?)',[(LoanAmount('100'),Decimal('0.5'))])
        assert conn.execute('SELECT SUM(principal),SUM(interest) FROM repayments').fetchone()==(365100,.75)


@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity'])
def test_nonfinite_decimal_is_rejected_before_storage(value):
    with sqlite3.connect(':memory:',factory=UnifiedConnection) as conn:
        with pytest.raises(ValueError):conn.execute('SELECT ?',(LoanAmount(value),))
