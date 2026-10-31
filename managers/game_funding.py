"""Resolve one eligible source for games without transferring or converting funds."""
from functions.amount_limits import MAX_NATIVE
from managers.currency_manager import CurrencyManager
from database.user_db import ACTIVE_OR_TREASURY, IS_TREASURY


def game_funds(conn, user_id, source=None):
    user_id = str(user_id)
    user = conn.execute(f"SELECT cash,status FROM users WHERE user_id=? AND {ACTIVE_OR_TREASURY}", (user_id,)).fetchone()
    if not user:
        raise ValueError('등록된 활성 사용자만 이용할 수 있습니다.')
    home = CurrencyManager.home(conn, user_id)
    source = str(source or '').strip()
    treasury = conn.execute(f"SELECT 1 FROM users WHERE user_id=? AND {IS_TREASURY}", (user_id,)).fetchone()
    wallet = (MAX_NATIVE if treasury else user['cash'], None)
    if source == '__wallet__':
        return wallet
    rows = conn.execute(
        "SELECT account_number,balance FROM accounts WHERE user_id=? "
        "AND quote_currency=? AND resolution_status='active' AND managed_by IS NULL "
        "AND account_type='saving' ORDER BY account_number", (user_id, home),
    ).fetchall()
    if source:
        for row in rows:
            if row['account_number'] == source:
                return row['balance'], row['account_number']
        raise ValueError('본인 명의의 활성 비운용 통장 중 자국 통화 계좌만 사용할 수 있습니다.')
    return max([wallet] + [(row['balance'], row['account_number']) for row in rows], key=lambda item: item[0])
