"""Banks are named by their unique ID everywhere a person types one.

The default bank, Inazina, always has the ID INAZINA.

Rows still link products and books to a bank by its issuer name; these helpers
translate between the ID people use and that stored name.
"""
import sqlite3

DEFAULT_BANK = ("Inazina", "INAZINA")


def bank_issuer(conn, bank_id):
    """The issuer name of the bank with this ID; an unknown ID is refused instead of creating a bank."""
    value = str(bank_id or "").strip().upper()
    try:
        row = conn.execute("SELECT issuer FROM financial_institutions WHERE institution_id=?", (value,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row and value == DEFAULT_BANK[1]:
        # The default bank is created with its first product and always answers to INAZINA.
        return DEFAULT_BANK[0]
    if not row:
        raise ValueError(f"등록되지 않은 은행 ID입니다: {bank_id} (은행 · 원장 탭에서 은행 ID를 확인하세요)")
    return row[0]


def bank_ids(conn):
    """issuer name → bank ID, for showing IDs where rows store names."""
    try:
        rows = conn.execute("SELECT issuer,institution_id FROM financial_institutions WHERE institution_id IS NOT NULL")
    except sqlite3.OperationalError:
        return {}
    return {row[0]: row[1] for row in rows}
