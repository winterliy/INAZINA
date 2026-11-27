"""Withdrawal (탈퇴): a member leaves in one step and everything they own passes to their nationality's treasury.

What happens, in one transaction (the member's own consent is asked by the caller):
1. Refuse while something remains that cannot be handed over safely (futures, options, ELS, swaps, funds, company stakes, loans with
   collateral, ...): the member must settle it with the operators first. Open orders are cancelled (their reserved cash and
   shares come back), accrued interest is settled.
2. Bank accounts are closed and their balances join the member's cash (as when a nationality changes); all the cash goes to the
   treasury; shares go into the treasury's own positions; loans, treasury bonds and lottery tickets are handed to the treasury.
3. Personal data is deleted: web login, notifications, board posts/comments/votes, KYC, chart settings.
4. Every remaining record about the member (ledgers, orders, tax and credit history) is kept for accounting, but the Discord ID in
   it is replaced by a random ``w-...`` ID. The Discord ID <-> random ID table (``withdrawal_records``) is kept for one year for
   fraud investigations and then deleted. The same Discord account can register again as a new member.

``COLUMN_BUCKETS`` puts every column that holds a user ID in exactly one bucket; a test fails when a new one is not listed.
"""
import json
import secrets
import sqlite3
import time

from managers.bank_journal import BankJournal
from managers.currency_manager import CurrencyManager, SCALE
from managers.nationality_manager import NationalityManager

RETENTION_SECONDS = 365 * 86400
# Columns whose name says "a user" (the same pattern the classification test uses to find new ones).
USER_COLUMN_PATTERN = (r"(^|_)(user|owner|holder|author|buyer|seller|borrower|creditor|debtor|payer|payee|actor|reporter|uploader|"
                       r"recipient|beneficiary|counterparty|member|account_holder|issuer|sender|receiver|target|admin|operator|created_by|"
                       r"updated_by|requested_by|approved_by)(_id)?$|(_user_id|_owner_id|_holder_id|user_id_)")

# bucket: users (the row itself) | transfer (handed to the treasury) | block (refuse while rows exist, see BLOCKERS) |
# delete (personal data) | rekey (kept, ID replaced) | staff (operators' own actions, not about a member).
COLUMN_BUCKETS = {
    'account_contributions': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'accounting_line_repairs': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'accounting_period_closings': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'accounts': {'bucket': 'transfer', 'columns': ['user_id'], 'staff': []},
    'admin_audit': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'admin_approval_decisions': {'bucket': 'staff', 'columns': [], 'staff': ['approver_id']},
    'admin_approval_requests': {'bucket': 'staff', 'columns': [], 'staff': ['requester_id']},
    'admin_approval_uses': {'bucket': 'staff', 'columns': [], 'staff': ['used_by']},
    'admin_role_changes': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id', 'approver_id', 'target_id']},
    'admin_login_events': {'bucket': 'staff', 'columns': [], 'staff': ['user_id']},
    'admin_national_sessions': {'bucket': 'staff', 'columns': [], 'staff': ['admin_id']},
    'aml_alerts': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'aml_case_events': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'bank_accounts': {'bucket': 'block', 'columns': ['user_id'], 'staff': ['issuer']},
    'bank_deficit_alerts': {'bucket': 'staff', 'columns': [], 'staff': ['issuer']},
    'bank_journal_entries': {'bucket': 'staff', 'columns': [], 'staff': ['issuer']},
    'bank_products': {'bucket': 'staff', 'columns': [], 'staff': ['issuer']},
    'basic_income_payments': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'board_attachments': {'bucket': 'delete', 'columns': ['uploader_id'], 'staff': []},
    'board_comments': {'bucket': 'delete', 'columns': ['author_id'], 'staff': ['author_admin']},
    'board_members': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'board_poll_votes': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'board_posts': {'bucket': 'delete', 'columns': ['author_id'], 'staff': ['author_admin']},
    'board_reactions': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'board_reports': {'bucket': 'delete', 'columns': ['reporter_id'], 'staff': []},
    'cash_fx_settlements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'chart_preferences': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'company_accounts': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'company_capital_interests': {'bucket': 'block', 'columns': ['holder_id'], 'staff': []},
    'company_income_log': {'bucket': 'staff', 'columns': [], 'staff': ['counterparty']},
    'corporate_liquidation_payments': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'country_macro_indicators': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'country_rate_history_events': {'bucket': 'staff', 'columns': [], 'staff': ['after_actor', 'before_actor']},
    'country_tax_rules': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'country_treasuries': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'court_filings': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'credit_events': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': ['actor_id']},
    'credit_risk_policy_events': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'credit_risk_policy_overrides': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'credit_score_history': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'currency_cash_postings': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'currency_transactions': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'currency_transfer_transactions': {'bucket': 'rekey', 'columns': ['recipient_id', 'sender_id'], 'staff': []},
    'customer_wallet_baseline_adjustments': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': ['actor_id']},
    'customer_wallet_ledger': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'deposit_insurance_claims': {'bucket': 'block', 'columns': ['user_id'], 'staff': ['issuer']},
    'depositary_receipt_movements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'dividend_payments': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'dr_custody_fee_ledger': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'dr_custody_instructions': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'dr_custody_process_events': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'dr_custody_reservations': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'dr_dividend_tax_rules': {'bucket': 'staff', 'columns': [], 'staff': ['created_by']},
    'dr_voting_ballots': {'bucket': 'rekey', 'columns': ['voter_user_id'], 'staff': []},
    'dr_voting_delegation_events': {'bucket': 'rekey', 'columns': ['actor_user_id'], 'staff': []},
    'dr_voting_delegations': {'bucket': 'delete', 'columns': ['owner_user_id', 'proxy_user_id'], 'staff': []},
    'dr_voting_entitlement_snapshots': {'bucket': 'rekey', 'columns': ['holder_user_id', 'voter_user_id'], 'staff': []},
    'els_settlements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'els_subscriptions': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'financial_institution_ledger': {'bucket': 'staff', 'columns': [], 'staff': ['issuer']},
    'financial_institutions': {'bucket': 'staff', 'columns': [], 'staff': ['issuer']},
    'ftp_rate_applications': {'bucket': 'staff', 'columns': [], 'staff': ['actor', 'issuer']},
    'fund_accounts': {'bucket': 'block', 'columns': ['owner_id'], 'staff': []},
    'futures_positions': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'fx_residual_clearings': {'bucket': 'rekey', 'columns': ['recipient_user_id'], 'staff': ['actor']},
    'fx_revaluation_corrections': {'bucket': 'staff', 'columns': [], 'staff': ['actor']},
    'fx_revaluation_runs': {'bucket': 'staff', 'columns': [], 'staff': ['actor']},
    'hybrid_bond_events': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'insolvency_cases': {'bucket': 'block', 'columns': ['user_id'], 'staff': ['actor_id']},
    'insolvency_policies': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'insolvency_restrictions': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'institution_funding': {'bucket': 'staff', 'columns': [], 'staff': ['counterparty', 'issuer']},
    'institution_funding_limits': {'bucket': 'staff', 'columns': [], 'staff': ['issuer', 'updated_by']},
    'issuer_cash_transfers': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'jurisdiction_rate_rules': {'bucket': 'staff', 'columns': [], 'staff': ['created_by']},
    'kyc_profile_events': {'bucket': 'delete', 'columns': ['user_id'], 'staff': ['actor_id']},
    'kyc_profiles': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'legacy_wallet_closure_adjustments': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': ['actor_id']},
    'loan_chargeoff_events': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id', 'issuer']},
    'loan_collateral_assets': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'loan_collateral_recovery_events': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'loan_credit_support': {'bucket': 'block', 'columns': ['party_user_id'], 'staff': []},
    'loan_credit_support_events': {'bucket': 'rekey', 'columns': ['party_user_id'], 'staff': ['actor_id']},
    'loan_future_commitment_events': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'loan_future_commitments': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'loans': {'bucket': 'transfer', 'columns': ['user_id'], 'staff': []},
    'lottery_fx_settlements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'lottery_payouts': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'lottery_purchases': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'lottery_tickets': {'bucket': 'transfer', 'columns': ['user_id'], 'staff': []},
    'market_events': {'bucket': 'staff', 'columns': [], 'staff': ['created_by']},
    'market_execution_events': {'bucket': 'rekey', 'columns': ['buyer_id', 'seller_id'], 'staff': []},
    'market_execution_holding_movements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'market_execution_primary_recipient_overrides': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id', 'recipient_id']},
    'market_orders': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'market_rate_curve_history_events': {'bucket': 'staff', 'columns': [], 'staff': ['after_actor', 'before_actor']},
    'naturalization_requests': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'notifications': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'option_exercise_asset_movements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'option_exercise_events': {'bucket': 'rekey', 'columns': ['holder_id'], 'staff': []},
    'option_positions': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'option_premium_events': {'bucket': 'rekey', 'columns': ['buyer_id', 'seller_id'], 'staff': []},
    'payout_requests': {'bucket': 'block', 'columns': ['payer_id', 'user_id'], 'staff': []},
    'preferred_dividend_claims': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'product_settlements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'prudential_parameters': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'shareholder_meeting_holders': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'shareholder_meeting_votes': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'shareholder_register_dr_entitlements': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'shareholder_register_dr_entries': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'shareholder_register_entries': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'shareholder_registers': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'swap_contracts': {'bucket': 'block', 'columns': ['user_id'], 'staff': []},
    'tax_transaction_wallet_links': {'bucket': 'staff', 'columns': [], 'staff': ['treasury_user_id']},
    'tax_transactions': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'treasury_bond_cash_events': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'treasury_bond_holdings': {'bucket': 'transfer', 'columns': ['user_id'], 'staff': []},
    'user_assets': {'bucket': 'transfer', 'columns': ['user_id'], 'staff': []},
    'user_realized_pnl': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'user_tax_residency_events': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'deposit_insurance_fund_events': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id', 'issuer']},
    'document_consents': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'announcement_seen': {'bucket': 'rekey', 'columns': ['user_id'], 'staff': []},
    'document_announcements': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'product_rate_schedules': {'bucket': 'staff', 'columns': [], 'staff': ['updated_by']},
    'users': {'bucket': 'users', 'columns': ['user_id'], 'staff': []},
    'web_auth_credentials': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
    'web_bot_jobs': {'bucket': 'staff', 'columns': [], 'staff': ['actor_id']},
    'web_chart_drawings': {'bucket': 'delete', 'columns': ['user_id'], 'staff': []},
}


# Extra reasons to refuse beyond NationalityManager.blockers (futures, options, ELS): table, condition (? = member), label.
BLOCKERS = (
    ("swap_contracts", "user_id=?", "스왑 계약"),
    ("fund_accounts", "owner_id=?", "펀드 계좌"),
    ("company_capital_interests", "holder_id=?", "법인 지분"),
    ("bank_accounts", "user_id=?", "은행 계좌 설정"),
    ("company_accounts", "user_id=?", "법인 계정"),
    ("deposit_insurance_claims", "user_id=?", "예금보험 청구"),
    ("dr_custody_instructions", "user_id=?", "DR 보관 지시"),
    ("dr_custody_reservations", "user_id=?", "DR 보관 예약"),
    ("insolvency_cases", "user_id=?", "도산 절차"),
    ("insolvency_restrictions", "user_id=?", "도산 제한"),
    ("loan_credit_support", "party_user_id=?", "대출 보증"),
    ("loan_collateral_assets", "user_id=?", "담보가 설정된 대출"),
    ("naturalization_requests", "user_id=? AND status='pending'", "처리 중인 귀화 신청"),
    ("payout_requests", "user_id=? AND status='pending'", "선택하지 않은 정산"),
    ("preferred_dividend_claims", "user_id=? AND status IN ('unpaid','partial')", "받지 못한 우선주 배당"),
)


def _has_table(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


class WithdrawalManager:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db

    @staticmethod
    def purge_expired(conn, now=None):
        """Delete the Discord ID <-> random ID rows a year after the withdrawal (run from the server's regular sweep). Returns how many."""
        if not _has_table(conn, "withdrawal_records"):
            return 0
        now = int(time.time() if now is None else now)
        return conn.execute("DELETE FROM withdrawal_records WHERE expires_epoch<=?", (now,)).rowcount

    # ---- checks ----------------------------------------------------------------------------------
    def _member(self, conn, user_id):
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        if not user:
            raise ValueError("사용자를 찾을 수 없습니다.")
        if str(user["role"] or "user").lower() != "user" or str(user["status"]) != "active":
            raise ValueError("일반 이용자 계정만 탈퇴할 수 있습니다.")
        return user

    def blockers(self, conn, user_id):
        user_id = str(user_id)
        reasons = list(NationalityManager(self.bot).blockers(conn, user_id))
        for table, condition, label in BLOCKERS:
            if _has_table(conn, table) and conn.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1", (user_id,)).fetchone():
                reasons.append(label)
        return reasons

    def plan(self, user_id):
        """What a withdrawal would do, without doing it."""
        user_id = str(user_id)
        with self.db.readonly() as conn:
            user = self._member(conn, user_id)
            treasury = CurrencyManager.user_treasury_id(conn, user_id)
            one = lambda sql: int(conn.execute(sql, (user_id,)).fetchone()[0] or 0)
            return {
                "treasuryId": treasury, "nationality": user["nationality"], "blockers": self.blockers(conn, user_id),
                "holdings": one("SELECT COUNT(*) FROM user_assets WHERE user_id=?"),
                "accounts": one("SELECT COUNT(*) FROM accounts WHERE user_id=?"),
                "loans": one("SELECT COUNT(*) FROM loans WHERE user_id=?"),
                "openOrders": one("SELECT COUNT(*) FROM market_orders WHERE user_id=? AND status IN ('open','partial','scheduled')"),
            }

    # ---- the withdrawal --------------------------------------------------------------------------
    def withdraw(self, user_id, *, now=None):
        user_id = str(user_id)
        with self.db.readonly() as conn:
            self._member(conn, user_id)
            reasons = self.blockers(conn, user_id)
        if reasons:
            raise ValueError(f"{', '.join(reasons)}이(가) 남아 있어 탈퇴할 수 없습니다. 운영진에게 정리를 요청해 주세요.")

        NationalityManager(self.bot).cancel_open_orders(user_id)
        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests(user_id)

        now = int(time.time() if now is None else now)
        tomb = "w-" + secrets.token_hex(8)
        with self.db.atomic() as conn:
            user = self._member(conn, user_id)
            if self.blockers(conn, user_id):  # something changed since the first look
                raise ValueError("탈퇴 처리 중 상태가 바뀌었습니다. 다시 시도해 주세요.")
            treasury = CurrencyManager.user_treasury_id(conn, user_id)
            summary = {"accounts": 0, "holdings": 0, "loans": 0, "bonds": 0, "tickets": 0, "cashUnits": 0, "retainedAuditColumns": []}
            self._close_accounts(conn, user_id, summary)
            self._move_cash(conn, user_id, treasury, summary)
            self._move_holdings(conn, user_id, treasury, summary)
            for table, key in (("treasury_bond_holdings", "bonds"), ("lottery_tickets", "tickets"), ("loans", "loans")):
                summary[key] = conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id=?", (treasury, user_id)).rowcount
            self._delete_personal_data(conn, user_id)
            summary["retainedAuditColumns"] = self._replace_id(conn, user, tomb)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS withdrawal_records (record_id INTEGER PRIMARY KEY AUTOINCREMENT, discord_user_id TEXT NOT NULL, "
                "tombstone_id TEXT NOT NULL UNIQUE, nationality TEXT, treasury_id TEXT, withdrawn_epoch INTEGER NOT NULL, "
                "expires_epoch INTEGER NOT NULL, summary TEXT)")
            WithdrawalManager.purge_expired(conn, now)
            conn.execute("INSERT INTO withdrawal_records(discord_user_id,tombstone_id,nationality,treasury_id,withdrawn_epoch,expires_epoch,summary) "
                         "VALUES (?,?,?,?,?,?,?)", (user_id, tomb, user["nationality"], treasury, now, now + RETENTION_SECONDS, json.dumps(summary)))
        return {"tombstoneId": tomb, "treasuryId": treasury, **summary}

    def _close_accounts(self, conn, user_id, summary):
        home = CurrencyManager.home(conn, user_id)
        for account in conn.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchall():
            units = int(account["balance_units"] or 0)
            if units:
                CurrencyManager.change(conn, user_id, home, units if account["quote_currency"] == home else CurrencyManager.convert(conn, units, account["quote_currency"], home))
            BankJournal.post_forced_account_removal(conn, account, time.time())
            conn.execute("UPDATE accounts SET balance_units=0 WHERE account_number=?", (account["account_number"],))
            conn.execute("DELETE FROM accounts WHERE account_number=?", (account["account_number"],))
            summary["accounts"] += 1

    def _move_cash(self, conn, user_id, treasury, summary):
        home = CurrencyManager.home(conn, user_id)
        units = CurrencyManager.balance(conn, user_id, home)
        if units:
            CurrencyManager.change(conn, user_id, home, -units)
            CurrencyManager.change(conn, treasury, home, units)
        summary["cashUnits"] = units

    def _move_holdings(self, conn, user_id, treasury, summary):
        from database.asset_cost import AssetCostManager
        from functions.money import currency_precision, from_minor_units
        for row in conn.execute("SELECT * FROM user_assets WHERE user_id=?", (user_id,)).fetchall():
            if row["quantity"]:
                cost = from_minor_units(row["average_cost_minor_units"] or 0, currency_precision(conn, AssetCostManager.currency(conn, row["asset_code"])))
                AssetCostManager.upsert(conn, treasury, row["asset_code"], None, row["quantity"], cost,
                                        incoming_total_minor=AssetCostManager.total_cost_minor(conn, row["holding_id"]))
            conn.execute("DELETE FROM user_assets WHERE holding_id=?", (row["holding_id"],))
            summary["holdings"] += 1

    def _delete_personal_data(self, conn, user_id):
        from web_api import board
        member = {"userId": user_id, "role": "user"}
        for post in conn.execute("SELECT post_id FROM board_posts WHERE author_id=?", (user_id,)).fetchall() if _has_table(conn, "board_posts") else []:
            board.delete_post(conn, member, post["post_id"])
        for comment in conn.execute("SELECT comment_id FROM board_comments WHERE author_id=?", (user_id,)).fetchall() if _has_table(conn, "board_comments") else []:
            board._remove_comment(conn, int(comment["comment_id"]))
        for table, info in COLUMN_BUCKETS.items():
            if info["bucket"] == "delete" and _has_table(conn, table):
                for column in info["columns"]:
                    conn.execute(f"DELETE FROM {table} WHERE {column}=?", (user_id,))

    def _replace_id(self, conn, user, tomb):
        """Create the replacement member row first (children point at it), re-point every kept record, then drop the original."""
        old = str(user["user_id"])
        conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,cash_minor,credit_score,status,role,registration_guild_id,annual_income,"
                     "income_stability,credit_score_computed,credit_score_epoch,tax_residency) VALUES (?,?,?,0,0,?,'withdrawn','user',NULL,?,?,?,?,?)",
                     (tomb, "탈퇴한 사용자", user["nationality"], user["credit_score"], user["annual_income"], user["income_stability"],
                      user["credit_score_computed"], user["credit_score_epoch"], user["tax_residency"]))
        retained = []
        for table, info in COLUMN_BUCKETS.items():
            if info["bucket"] == "rekey" and _has_table(conn, table):
                for column in info["columns"]:
                    try:
                        conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (tomb, old))
                    except sqlite3.IntegrityError as error:
                        # Accounting and audit tables refuse every change on purpose (database triggers). Their rows keep the
                        # Discord ID, which the privacy policy says; nothing links it to a name once this withdrawal is done.
                        if "append-only" not in str(error) and "immutable" not in str(error):
                            raise
                        retained.append(f"{table}.{column}")
        conn.execute("DELETE FROM users WHERE user_id=?", (old,))
        return retained
