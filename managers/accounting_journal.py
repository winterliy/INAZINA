"""Shared, immutable accounting posting boundary for all internal ledgers.

Native subledgers keep their existing public schemas. This journal records the
same posting in product-currency minor units, with explicit source-account
mapping and a stable idempotency key, in the caller's SQLite transaction.
"""
from functions.money import base_currency_code

import hashlib
import json


class AccountingJournal:
    @classmethod
    def reconstruct_closed_account_wallet(cls, conn, account_number, *, actor_id, now):
        """Append a present-period correction only when a bank close proves the missing withdrawal."""
        from managers.bank_journal import BankJournal
        from functions.money import currency_precision
        account, actor = str(account_number).strip(), str(actor_id).strip()
        epoch = int(BankJournal.validate_epoch(now))
        if not account or not actor:
            raise ValueError('계좌와 조정 실행자를 지정해야 합니다.')
        conn.execute('SAVEPOINT legacy_wallet_closure')
        try:
            prior = conn.execute("""SELECT a.*,w.user_id wallet_user,w.source_type,w.source_account,w.source_key,
                w.currency_code wallet_currency,w.decimal_places wallet_places,w.delta_minor_units,w.balance_minor_units,
                w.recorded_epoch wallet_epoch,b.event_type,b.status bank_status,b.reference_key,
                b.currency_code bank_currency,b.decimal_places bank_places,
                (SELECT COALESCE(SUM(debit_units),0) FROM bank_journal_lines
                 WHERE entry_id=b.entry_id AND account_code='customer_deposits_payable') bank_debit
                FROM legacy_wallet_closure_adjustments a
                LEFT JOIN customer_wallet_ledger w ON w.entry_id=a.wallet_entry_id
                LEFT JOIN bank_journal_entries b ON b.entry_id=a.bank_entry_id
                WHERE a.account_number=?""", (account,)).fetchone()
            if prior:
                if (not prior['actor_id'] or int(prior['amount_minor_units']) <= 0 or
                    (prior['wallet_user'],prior['source_type'],prior['source_account'],prior['source_key'],
                     prior['wallet_currency'],prior['wallet_places'],prior['delta_minor_units'],prior['balance_minor_units'],prior['wallet_epoch']) !=
                    (prior['user_id'],'account',account,f'legacy-close-adjustment:{account}',prior['currency_code'],
                     prior['decimal_places'],-int(prior['amount_minor_units']),0,prior['recorded_epoch'])
                    or (prior['event_type'],prior['bank_status'],prior['reference_key'],prior['bank_currency'],prior['bank_places'],prior['bank_debit']) !=
                    ('deposit_close','posted',f'account:{account}',prior['currency_code'],prior['decimal_places'],prior['amount_minor_units'])):
                    raise ValueError('기존 계좌 지갑 조정 근거가 일치하지 않습니다.')
                result = int(prior['wallet_entry_id'])
            else:
                if conn.execute('SELECT 1 FROM accounts WHERE account_number=?', (account,)).fetchone():
                    raise ValueError('삭제되지 않은 계좌는 과거 해지 복구 대상이 아닙니다.')
                rows = conn.execute("""SELECT user_id,currency_code,decimal_places,delta_minor_units,
                    balance_minor_units,recorded_epoch FROM customer_wallet_ledger
                    WHERE source_type='account' AND source_account=? ORDER BY entry_id""", (account,)).fetchall()
                if not rows:
                    raise ValueError('계좌 지갑 이력 근거가 없습니다.')
                identities = {(str(r['user_id']),str(r['currency_code']),int(r['decimal_places'])) for r in rows}
                if len(identities) != 1:
                    raise ValueError('계좌 지갑 이력의 소유자 또는 통화 근거가 일치하지 않습니다.')
                owner, currency, places = identities.pop()
                amount = sum(int(r['delta_minor_units']) for r in rows)
                if (amount <= 0 or int(rows[-1]['balance_minor_units']) != amount
                    or epoch < int(rows[-1]['recorded_epoch']) or currency_precision(conn, currency) != places
                    or not conn.execute('SELECT 1 FROM users WHERE user_id=?', (owner,)).fetchone()):
                    raise ValueError('계좌 지갑 잔액 또는 소유자 근거를 확인할 수 없습니다.')
                banks = conn.execute("""SELECT b.entry_id,b.issuer,b.reference_key,b.currency_code,b.decimal_places,b.created_epoch,
                    COALESCE(SUM(CASE WHEN l.account_code='customer_deposits_payable' THEN l.debit_units ELSE 0 END),0) debit
                    FROM bank_journal_entries b JOIN bank_journal_lines l USING(entry_id)
                    WHERE b.event_type='deposit_close' AND b.reference_key=? AND b.status='posted'
                    GROUP BY b.entry_id""", (f'account:{account}',)).fetchall()
                if len(banks) != 1 or (str(banks[0]['currency_code']),int(banks[0]['decimal_places']),int(banks[0]['debit'])) != (currency,places,amount):
                    raise ValueError('계좌 해지 금액을 입증하는 금융기관 분개 근거가 없습니다.')
                bank = banks[0]
                if float(bank['created_epoch']) < float(rows[-1]['recorded_epoch']) or epoch < int(float(bank['created_epoch'])):
                    raise ValueError('계좌 해지 분개 시각이 지갑 이력 근거와 일치하지 않습니다.')
                common = conn.execute("""SELECT e.entry_id,e.recorded_epoch FROM accounting_entries e JOIN accounting_lines l USING(entry_id)
                    WHERE e.ledger_scope='institution' AND e.entity_type='financial_institution'
                    AND e.entity_id=? AND e.source_key=? AND e.currency_code=? AND e.decimal_places=?
                    GROUP BY e.entry_id HAVING SUM(CASE WHEN l.account_code=(SELECT common_code FROM accounting_account_mappings
                        WHERE ledger_scope='institution' AND local_code='customer_deposits_payable' AND active=1)
                        THEN l.debit_units ELSE 0 END)=?""",
                    (str(bank['issuer']),f"deposit_close:{bank['reference_key']}",currency,places,amount)).fetchone()
                if not common:
                    raise ValueError('계좌 해지의 공통 분개 근거가 없습니다.')
                expected = {}
                bank_lines = conn.execute('SELECT account_code,debit_units,credit_units FROM bank_journal_lines WHERE entry_id=?', (bank['entry_id'],)).fetchall()
                for line in bank_lines:
                    mapping = conn.execute("SELECT common_code FROM accounting_account_mappings WHERE ledger_scope='institution' AND local_code=? AND active=1", (line['account_code'],)).fetchone()
                    if not mapping:
                        raise ValueError('계좌 해지의 공통 계정 매핑 근거가 없습니다.')
                    debit, credit = expected.get(mapping[0], (0, 0))
                    expected[mapping[0]] = (debit+int(line['debit_units']), credit+int(line['credit_units']))
                actual = {}
                for line in conn.execute('SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=?', (common['entry_id'],)):
                    debit, credit = actual.get(line['account_code'], (0, 0))
                    actual[line['account_code']] = (debit+int(line['debit_units']), credit+int(line['credit_units']))
                if (len(bank_lines)<2 or sum(v[0] for v in expected.values()) != sum(v[1] for v in expected.values())
                    or expected != actual or int(common['recorded_epoch']) != int(float(bank['created_epoch']))):
                    raise ValueError('계좌 해지 원장 간 분개 근거가 일치하지 않습니다.')
                cursor = conn.execute("""INSERT INTO customer_wallet_ledger
                    (source_key,user_id,source_type,source_account,currency_code,decimal_places,
                     delta_minor_units,balance_minor_units,recorded_epoch)
                    VALUES (?,?,'account',?,?,?,?,0,?)""",
                    (f'legacy-close-adjustment:{account}',owner,account,currency,places,-amount,epoch))
                result = int(cursor.lastrowid)
                conn.execute("""INSERT INTO legacy_wallet_closure_adjustments
                    (account_number,user_id,currency_code,decimal_places,amount_minor_units,bank_entry_id,wallet_entry_id,actor_id,recorded_epoch)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (account,owner,currency,places,amount,int(bank['entry_id']),result,actor,epoch))
        except Exception:
            conn.execute('ROLLBACK TO legacy_wallet_closure')
            conn.execute('RELEASE legacy_wallet_closure')
            raise
        conn.execute('RELEASE legacy_wallet_closure')
        return result

    @staticmethod
    def link_tax_bank_remittance(conn, transaction_id, issuer, reference_key, source_currency, settlement_currency, fx_quote,
                                 *, settlement_zero=False):
        """Link the actual bank remittance legs created for a withholding transaction."""
        cross_currency = str(source_currency).upper() != str(settlement_currency).upper()
        references = ([f'{reference_key}:source'] + ([] if settlement_zero else [f'{reference_key}:settlement'])
                      if cross_currency else [reference_key])
        entries = []
        for reference, currency in zip(references, (source_currency, settlement_currency)):
            row = conn.execute("""SELECT entry_id FROM bank_journal_entries
                WHERE issuer=? AND event_type='tax_remittance' AND reference_key=?
                AND currency_code=? AND status='posted'""",
                (str(issuer), reference, str(currency).upper())).fetchone()
            if not row:
                raise RuntimeError('세금 송금에 대응하는 금융기관 분개가 없습니다.')
            entries.append(int(row[0]))
        from functions.money import currency_precision
        conn.execute('SAVEPOINT tax_bank_remittance_link')
        try:
            conn.execute("""INSERT INTO tax_bank_remittance_links
                (transaction_id,source_entry_id,settlement_entry_id) VALUES (?,?,?)""",
                (int(transaction_id), entries[0], entries[1] if len(entries) > 1 else None))
            conn.execute("""INSERT INTO tax_bank_remittance_quotes
                (transaction_id,source_currency,settlement_currency,source_places,settlement_places,rate_numerator,rate_denominator)
                VALUES (?,?,?,?,?,?,?)""", (int(transaction_id), str(source_currency).upper(), str(settlement_currency).upper(),
                    currency_precision(conn, source_currency), currency_precision(conn, settlement_currency),
                    str(fx_quote.numerator), str(fx_quote.denominator)))
        except Exception:
            conn.execute('ROLLBACK TO tax_bank_remittance_link')
            conn.execute('RELEASE tax_bank_remittance_link')
            raise
        conn.execute('RELEASE tax_bank_remittance_link')

    @classmethod
    def link_tax_transaction_wallet_entries(cls, conn, transaction_id, wallet_watermark,
                                            treasury_user_id):
        """Attach the wallet receipts created by one tax remittance."""
        schema = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='tax_transaction_wallet_links'").fetchone()
        if not schema:
            raise RuntimeError("세금-국고 지갑 연결 스키마가 초기화되지 않았습니다.")
        tax = conn.execute("SELECT tax_amount_minor_units FROM tax_transactions "
                           "WHERE transaction_id=?", (int(transaction_id),)).fetchone()
        if not tax or int(tax[0] or 0) <= 0:
            return 0
        cursor = conn.execute("""INSERT OR IGNORE INTO tax_transaction_wallet_links
            (transaction_id,wallet_entry_id,treasury_user_id)
            SELECT ?,entry_id,? FROM customer_wallet_ledger
            WHERE entry_id>? AND user_id=? AND delta_minor_units>0
            ORDER BY entry_id""",
            (int(transaction_id), str(treasury_user_id), int(wallet_watermark),
             str(treasury_user_id)))
        linked = conn.execute("SELECT COUNT(*) FROM tax_transaction_wallet_links "
                              "WHERE transaction_id=?", (int(transaction_id),)).fetchone()[0]
        if not linked:
            raise RuntimeError("세금 원천징수 기록에 대응하는 국고 지갑 입금이 없습니다.")
        return int(cursor.rowcount)

    @staticmethod
    def ensure_schema(conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS accounting_account_mappings (
            ledger_scope TEXT NOT NULL, local_code TEXT NOT NULL,
            common_code TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                CHECK(active IN (0,1)),
            PRIMARY KEY(ledger_scope,local_code))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounting_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_scope TEXT NOT NULL, entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL, source_key TEXT NOT NULL,
            currency_code TEXT NOT NULL, decimal_places INTEGER NOT NULL
                CHECK(decimal_places BETWEEN 0 AND 6),
            recorded_epoch INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
            UNIQUE(ledger_scope,entity_type,entity_id,source_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounting_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES accounting_entries(entry_id),
            account_code TEXT NOT NULL, debit_units INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(debit_units)='integer' AND debit_units>=0),
            credit_units INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(credit_units)='integer' AND credit_units>=0),
            CHECK((debit_units=0)<>(credit_units=0)))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounting_lines_entry ON accounting_lines(entry_id,account_code)")
        conn.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_events (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL CHECK(event_type IN ('coupon','redemption')),
            holding_id INTEGER NOT NULL, product_code TEXT NOT NULL, user_id TEXT NOT NULL,
            currency_code TEXT NOT NULL, decimal_places INTEGER NOT NULL
                CHECK(decimal_places BETWEEN 0 AND 6),
            principal_minor_units INTEGER NOT NULL CHECK(typeof(principal_minor_units)='integer' AND principal_minor_units>=0),
            interest_minor_units INTEGER NOT NULL CHECK(typeof(interest_minor_units)='integer' AND interest_minor_units>=0),
            tax_minor_units INTEGER NOT NULL CHECK(typeof(tax_minor_units)='integer' AND tax_minor_units>=0),
            net_minor_units INTEGER NOT NULL CHECK(typeof(net_minor_units)='integer' AND net_minor_units>=0),
            recorded_epoch INTEGER NOT NULL)""")
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_treasury_bond_cash_events_{operation.lower()}_immutable
                BEFORE {operation} ON treasury_bond_cash_events
                BEGIN SELECT RAISE(ABORT,'treasury bond cash events are append-only'); END""")
        conn.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_link_baseline_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            through_rowid INTEGER NOT NULL CHECK(through_rowid>=0))""")
        conn.execute("""INSERT OR IGNORE INTO treasury_bond_cash_link_baseline_state
            (singleton_id,through_rowid)
            SELECT 1,COALESCE(MAX(rowid),0) FROM treasury_bond_cash_events""")
        conn.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_link_baselines (
            event_key TEXT PRIMARY KEY REFERENCES treasury_bond_cash_events(event_key),
            reason TEXT NOT NULL DEFAULT 'pre-link historical bond cash event')""")
        conn.execute("""INSERT OR IGNORE INTO treasury_bond_cash_link_baselines(event_key)
            SELECT event_key FROM treasury_bond_cash_events
            WHERE rowid<=(SELECT through_rowid FROM treasury_bond_cash_link_baseline_state
                         WHERE singleton_id=1)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_wallet_links (
            event_key TEXT PRIMARY KEY REFERENCES treasury_bond_cash_events(event_key),
            wallet_entry_id INTEGER NOT NULL UNIQUE REFERENCES customer_wallet_ledger(entry_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_fund_links (
            event_key TEXT PRIMARY KEY REFERENCES treasury_bond_cash_events(event_key),
            fund_entry_id INTEGER NOT NULL UNIQUE REFERENCES fund_cash_ledger(entry_id))""")
        for table, message in (
            ("treasury_bond_cash_link_baselines", "treasury bond cash baselines are append-only"),
            ("treasury_bond_cash_wallet_links", "treasury bond wallet links are append-only"),
            ("treasury_bond_cash_fund_links", "treasury bond fund links are append-only"),
            ("treasury_bond_cash_link_baseline_state", "treasury bond baseline watermark is immutable"),
        ):
            for operation in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT,'{message}'); END""")
        from database.currency_db import ensure_treasury_bond_fx_settlements
        ensure_treasury_bond_fx_settlements(conn.execute)
        from database.accounting_periods import ensure_period_guards
        ensure_period_guards(conn)
        for table in ("accounting_entries", "accounting_lines"):
            for operation in ("UPDATE", "DELETE"):
                name = f"trg_{table}_{operation.lower()}_immutable"
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT,'common accounting journal is append-only'); END""")

    @classmethod
    def register_mapping(cls, conn, ledger_scope, local_code, common_code):
        cls.register_mappings(conn, ledger_scope, [(local_code, common_code)])

    @classmethod
    def register_mappings(cls, conn, ledger_scope, mappings):
        """Register several mappings while initializing the journal schema once."""
        cls.ensure_schema(conn)
        scope = str(ledger_scope)
        for local_code, common_code in mappings:
            local, common = map(str, (local_code, common_code))
            if not scope or not local or not common:
                raise ValueError("원장 계정 매핑은 빈 값일 수 없습니다.")
            row = conn.execute(
                "SELECT common_code FROM accounting_account_mappings WHERE ledger_scope=? AND local_code=?",
                (scope, local),
            ).fetchone()
            if row and row[0] != common:
                raise ValueError(f"기존 계정 매핑과 충돌합니다: {scope}/{local}")
            conn.execute(
                "INSERT OR IGNORE INTO accounting_account_mappings(ledger_scope,local_code,common_code) VALUES (?,?,?)",
                (scope, local, common),
            )

    @classmethod
    def post(cls, conn, *, ledger_scope, entity_type, entity_id, source_key,
             currency_code, decimal_places, recorded_epoch, lines, description=""):
        """Record already-rounded integer minor-unit lines in caller transaction.

        A savepoint makes partial failures safe even when the surrounding
        transaction catches an exception. Replays must have byte-for-byte equal
        normalized content or fail loudly as an idempotency-key collision.
        """
        cls.ensure_schema(conn)
        scope, kind, entity, key = map(str, (ledger_scope, entity_type, entity_id, source_key))
        currency = str(currency_code).upper()
        places = int(decimal_places)
        if not all((scope, kind, entity, key, currency)) or not 0 <= places <= 6:
            raise ValueError("공통 원장 식별자 또는 통화 정밀도가 올바르지 않습니다.")
        from functions.money import currency_precision
        if places != currency_precision(conn, currency):
            raise ValueError("공통 원장 정밀도는 통화의 실제 최소단위와 같아야 합니다.")
        combined = {}
        for local, side, units in lines:
            if isinstance(units, bool):
                raise ValueError("공통 분개는 0 이상의 정수 최소단위와 차변/대변을 사용해야 합니다.")
            try:
                parsed_units = int(units)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("공통 분개 금액은 정수 최소단위여야 합니다.") from None
            if parsed_units != units:
                raise ValueError("공통 분개 금액은 정수 최소단위여야 합니다.")
            units = parsed_units
            if units < 0 or side not in ("debit", "credit"):
                raise ValueError("공통 분개는 0 이상의 정수 최소단위와 차변/대변을 사용해야 합니다.")
            mapping = conn.execute(
                "SELECT common_code FROM accounting_account_mappings "
                "WHERE ledger_scope=? AND local_code=? AND active=1", (scope, str(local)),
            ).fetchone()
            if not mapping:
                raise ValueError(f"공통 계정 매핑이 없습니다: {scope}/{local}")
            code = str(mapping[0])
            debit, credit = combined.get(code, (0, 0))
            combined[code] = (debit + (units if side == "debit" else 0),
                              credit + (units if side == "credit" else 0))
        normalized = sorted((code, debit, credit) for code, (debit, credit) in combined.items()
                            if debit or credit)
        if len(normalized) < 2 or sum(x[1] for x in normalized) <= 0 or sum(x[1] for x in normalized) != sum(x[2] for x in normalized):
            raise ValueError("공통 분개가 통화 최소단위 기준으로 균형을 이루지 않습니다.")
        prior = conn.execute(
            "SELECT entry_id,currency_code,decimal_places FROM accounting_entries "
            "WHERE ledger_scope=? AND entity_type=? AND entity_id=? AND source_key=?",
            (scope, kind, entity, key),
        ).fetchone()
        if prior:
            old = conn.execute(
                "SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=? ORDER BY account_code",
                (prior[0],),
            ).fetchall()
            if (prior[1], int(prior[2]), [tuple(row) for row in old]) != (currency, places, normalized):
                raise ValueError("동일한 공통 원장 멱등성 키에 다른 금액/분개가 들어왔습니다.")
            return int(prior[0])

        closed = conn.execute("""SELECT 1 FROM accounting_period_closings
            WHERE ledger_scope=? AND entity_id=? AND currency_code=?
              AND ? >= period_start_epoch AND ? < period_end_epoch LIMIT 1""",
            (scope, entity, currency, int(recorded_epoch), int(recorded_epoch)),
        ).fetchone()
        if closed:
            raise ValueError("마감된 회계기간에는 소급 전기할 수 없습니다. 다음 기간에 조정전기를 기록하세요.")

        conn.execute("SAVEPOINT common_accounting_post")
        try:
            cur = conn.execute(
                "INSERT INTO accounting_entries(ledger_scope,entity_type,entity_id,source_key,currency_code,decimal_places,recorded_epoch,description) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (scope, kind, entity, key, currency, places, int(recorded_epoch), str(description)[:500]),
            )
            entry_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) VALUES (?,?,?,?)",
                [(entry_id, code, debit, credit) for code, debit, credit in normalized],
            )
        except Exception:
            conn.execute("ROLLBACK TO common_accounting_post")
            conn.execute("RELEASE common_accounting_post")
            raise
        conn.execute("RELEASE common_accounting_post")
        return entry_id

    @classmethod
    def close_period(cls, conn, *, ledger_scope, entity_id, currency_code,
                     period_start_epoch, period_end_epoch, actor_id, closed_epoch):
        """Seal a common-ledger period after balance and integrity checks.

        Epoch interval is half-open [start,end). Corrections must be posted in a
        later open period and reference the closing in their source key.
        """
        cls.ensure_schema(conn)
        scope, entity, currency, actor = map(
            str, (ledger_scope, entity_id, currency_code, actor_id)
        )
        start, end, closed_at = (int(period_start_epoch), int(period_end_epoch),
                                 int(closed_epoch))
        if not all((scope, entity, currency, actor)) or start < 0 or end <= start or closed_at < 0:
            raise ValueError("회계기간 마감 인자가 올바르지 않습니다.")
        unbalanced = conn.execute("""SELECT e.entry_id
            FROM accounting_entries e LEFT JOIN accounting_lines l USING(entry_id)
            WHERE e.ledger_scope=? AND e.entity_id=? AND e.currency_code=?
              AND e.recorded_epoch>=? AND e.recorded_epoch<?
            GROUP BY e.entry_id
            HAVING COUNT(l.line_id)<2 OR SUM(l.debit_units)<>SUM(l.credit_units)
            LIMIT 1""", (scope, entity, currency, start, end)).fetchone()
        if unbalanced:
            raise ValueError(f"불균형 분개가 있어 기간을 마감할 수 없습니다: {unbalanced[0]}")
        scales = conn.execute("""SELECT COUNT(DISTINCT decimal_places) FROM accounting_entries
            WHERE ledger_scope=? AND entity_id=? AND currency_code=?
              AND recorded_epoch>=? AND recorded_epoch<?""",
            (scope, entity, currency, start, end)).fetchone()[0]
        if scales > 1:
            raise ValueError("통화 정밀도가 서로 다른 분개가 있어 기간을 마감할 수 없습니다.")
        # The gate: nothing the consolidated trial balance flags for this scope may be sealed.
        blockers = sorted({finding["status"] for finding in cls.consolidated_trial_balance(conn)["findings"]
                           if finding.get("ledger_scope") == scope
                           and finding.get("currency_code", currency) == currency
                           and finding.get("entity_id", entity) == entity
                           and finding["status"] != "unbalanced_entry"})
        if blockers:
            raise ValueError(f"원장 무결성 문제가 있어 기간을 마감할 수 없습니다: {', '.join(blockers)}")
        prior = conn.execute("""SELECT closing_id,entry_count,closing_digest FROM accounting_period_closings
            WHERE ledger_scope=? AND entity_id=? AND currency_code=?
              AND period_start_epoch=? AND period_end_epoch=?""",
            (scope, entity, currency, start, end)).fetchone()
        version = 1 if prior and not str(prior['closing_digest']).startswith('v2:') else 2
        count, digest = cls._period_snapshot(conn, scope, entity, currency, start, end, version=version)
        if prior:
            if (int(prior[1]), str(prior[2])) != (count, digest):
                raise ValueError("기존 회계기간 마감 스냅샷과 현재 원장이 다릅니다.")
            return int(prior[0])
        cursor = conn.execute("""INSERT INTO accounting_period_closings
            (ledger_scope,entity_id,currency_code,period_start_epoch,period_end_epoch,
             closed_epoch,actor_id,entry_count,closing_digest)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (scope, entity, currency, start, end, closed_at, actor,
             count, digest))
        return int(cursor.lastrowid)

    @staticmethod
    def _period_snapshot(conn, scope, entity, currency, start, end, *, version=2):
        if version == 1:
            query = """SELECT e.entry_id,e.source_key,l.account_code,
                l.debit_units,l.credit_units
            FROM accounting_entries e JOIN accounting_lines l USING(entry_id)
            WHERE e.ledger_scope=? AND e.entity_id=? AND e.currency_code=?
              AND e.recorded_epoch>=? AND e.recorded_epoch<?
            ORDER BY e.entry_id,l.account_code"""
        else:
            query = """SELECT e.entry_id,e.entity_type,e.source_key,e.currency_code,e.decimal_places,
                e.recorded_epoch,e.description,l.line_id,l.account_code,l.debit_units,l.credit_units
            FROM accounting_entries e JOIN accounting_lines l USING(entry_id)
            WHERE e.ledger_scope=? AND e.entity_id=? AND e.currency_code=?
              AND e.recorded_epoch>=? AND e.recorded_epoch<?
            ORDER BY e.entry_id,l.account_code,l.line_id"""
        rows = conn.execute(query, (scope, entity, currency, start, end)).fetchall()
        normalized = [tuple(row) for row in rows]
        digest = hashlib.sha256(json.dumps(normalized, separators=(",", ":"),
                                 ensure_ascii=False).encode()).hexdigest()
        return len({row[0] for row in normalized}), ('v2:' + digest if version == 2 else digest)

    @classmethod
    def reconcile_period_closings(cls, conn):
        """Recheck the immutable digest and entry count of each sealed period."""
        gaps, count = [], 0
        for row in conn.execute("SELECT * FROM accounting_period_closings ORDER BY closing_id"):
            count += 1
            actual = cls._period_snapshot(conn, row["ledger_scope"], row["entity_id"],
                                          row["currency_code"], row["period_start_epoch"],
                                          row["period_end_epoch"],
                                          version=2 if str(row['closing_digest']).startswith('v2:') else 1)
            if actual != (row["entry_count"], row["closing_digest"]):
                gaps.append({"closing_id": row["closing_id"], "ledger_scope": row["ledger_scope"],
                             "entity_id": row["entity_id"], "currency_code": row["currency_code"],
                             "expected_count": row["entry_count"], "actual_count": actual[0],
                             "status": "closing_snapshot_mismatch"})
        return {"closing_count": count, "gaps": gaps, "status": "mismatch" if gaps else "matched"}

    @staticmethod
    def trial_balance(conn, *, ledger_scope, entity_id, currency_code,
                      period_start_epoch=None, period_end_epoch=None):
        """Return a minor-unit trial balance for one entity and currency.

        Optional epochs use the same half-open interval as period closing.
        A currency's scale is snapshotted on every entry; mixed scales are
        treated as corrupted data rather than silently combined.
        """
        scope, entity, currency = map(
            str, (ledger_scope, entity_id, currency_code)
        )
        if not scope or not entity or not currency:
            raise ValueError("시산표 원장·주체·통화를 지정해야 합니다.")
        currency = currency.upper()
        from functions.money import currency_precision
        places = currency_precision(conn, currency)
        start = None if period_start_epoch is None else int(period_start_epoch)
        end = None if period_end_epoch is None else int(period_end_epoch)
        if (start is not None and start < 0) or (end is not None and end < 0):
            raise ValueError("시산표 기간 epoch은 음수일 수 없습니다.")
        if start is not None and end is not None and end <= start:
            raise ValueError("시산표 기간은 시작 포함·종료 제외의 유효한 구간이어야 합니다.")
        where = ["e.ledger_scope=?", "e.entity_id=?", "e.currency_code=?"]
        params = [scope, entity, currency]
        if start is not None:
            where.append("e.recorded_epoch>=?")
            params.append(start)
        if end is not None:
            where.append("e.recorded_epoch<?")
            params.append(end)
        rows = conn.execute(f"""SELECT l.account_code,
                SUM(l.debit_units) debit_units,SUM(l.credit_units) credit_units,
                COUNT(DISTINCT e.entry_id) entry_count,
                MIN(e.decimal_places) min_places,MAX(e.decimal_places) max_places
            FROM accounting_entries e JOIN accounting_lines l USING(entry_id)
            WHERE {' AND '.join(where)} GROUP BY l.account_code
            ORDER BY l.account_code""", params).fetchall()
        result = []
        for row in rows:
            if int(row["min_places"]) != places or int(row["max_places"]) != places:
                raise ValueError(
                    f"시산표 원장에 통화 정밀도 snapshot 불일치가 있습니다: {scope}/{entity}/{currency}"
                )
            debit, credit = int(row["debit_units"]), int(row["credit_units"])
            result.append({"account_code": str(row["account_code"]),
                           "currency_code": currency, "decimal_places": places,
                           "debit_units": debit, "credit_units": credit,
                           "balance_units": debit-credit,
                           "entry_count": int(row["entry_count"])})
        return result

    @classmethod
    def post_treasury_bond_cash_event(cls, conn, *, event_key, event_type,
            holding_id, product_code, user_id, currency_code, decimal_places,
            principal_minor_units, interest_minor_units, tax_minor_units,
            net_minor_units, recorded_epoch, wallet_watermark=None,
            fund_watermark=None, fx_settlement=None, wallet_entry_id=None):
        """Append a bond cash event and its balanced common posting atomically.

        ``fx_settlement`` describes a net payment converted into another
        currency: ``{"currency", "decimal_places", "minor_units", "rate"}``
        with ``rate`` a Fraction of settlement currency per bond currency.

        The holder's wallet row is linked only when exactly one unlinked credit
        matches the holder, paid currency, scale and amount: ``wallet_entry_id``
        names it outright, otherwise it is searched after ``wallet_watermark``.
        A refused event leaves nothing behind even if the caller keeps its transaction.
        """
        conn.execute("SAVEPOINT treasury_bond_cash_event")
        try:
            cls._post_treasury_bond_cash_event(
                conn, event_key=event_key, event_type=event_type, holding_id=holding_id,
                product_code=product_code, user_id=user_id, currency_code=currency_code,
                decimal_places=decimal_places, principal_minor_units=principal_minor_units,
                interest_minor_units=interest_minor_units, tax_minor_units=tax_minor_units,
                net_minor_units=net_minor_units, recorded_epoch=recorded_epoch,
                wallet_watermark=wallet_watermark, fund_watermark=fund_watermark,
                fx_settlement=fx_settlement, wallet_entry_id=wallet_entry_id)
        except Exception:
            conn.execute("ROLLBACK TO treasury_bond_cash_event")
            raise
        finally:
            conn.execute("RELEASE treasury_bond_cash_event")

    @classmethod
    def _post_treasury_bond_cash_event(cls, conn, *, event_key, event_type,
            holding_id, product_code, user_id, currency_code, decimal_places,
            principal_minor_units, interest_minor_units, tax_minor_units,
            net_minor_units, recorded_epoch, wallet_watermark, fund_watermark,
            fx_settlement, wallet_entry_id):
        event_type = str(event_type)
        if event_type not in {"coupon", "redemption"}:
            raise ValueError("국채 현금 이벤트 유형이 올바르지 않습니다.")
        principal, interest, tax, net = map(int, (
            principal_minor_units, interest_minor_units, tax_minor_units, net_minor_units
        ))
        if principal + interest != tax + net or min(principal, interest, tax, net) < 0:
            raise ValueError("국채 지급 이벤트 금액이 최소단위 기준으로 일치하지 않습니다.")
        if event_type == "coupon" and principal != 0:
            raise ValueError("쿠폰 이벤트에 원금 상환액을 포함할 수 없습니다.")
        currency = str(currency_code).upper()
        entity = str(holding_id)
        source_key = str(event_key)
        lines = []
        if principal:
            lines.append(("bond_principal_payable", "debit", principal))
        if interest:
            lines.append(("bond_interest_expense", "debit", interest))
        if net:
            lines.append(("customer_cash_payable", "credit", net))
        if tax:
            lines.append(("tax_payable", "credit", tax))
        cls.register_mappings(conn, "treasury_bond", [(local, local) for local, _, _ in lines])
        cls.post(conn, ledger_scope="treasury_bond", entity_type="holding",
            entity_id=entity, source_key=source_key, currency_code=currency,
            decimal_places=int(decimal_places), recorded_epoch=int(recorded_epoch),
            lines=lines, description=f"Treasury bond {event_type}")
        conn.execute("""INSERT INTO treasury_bond_cash_events
            (event_key,event_type,holding_id,product_code,user_id,currency_code,decimal_places,
             principal_minor_units,interest_minor_units,tax_minor_units,net_minor_units,recorded_epoch)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_key,event_type,int(holding_id),str(product_code),str(user_id),currency,
             int(decimal_places),principal,interest,tax,net,int(recorded_epoch)))
        settled = None
        if fx_settlement is not None:
            settled = cls._post_treasury_bond_fx_settlement(
                conn, source_key, entity, currency, int(decimal_places), net,
                fx_settlement, int(recorded_epoch))
        if net and settled != 0:
            fund_owner = conn.execute("SELECT product_code FROM fund_accounts WHERE owner_id=?",
                                      (str(user_id),)).fetchone()
            if fund_owner:
                if currency != base_currency_code(conn) or fund_watermark is None:
                    raise ValueError("펀드 국채 지급의 현금 원장 기준점이 없습니다.")
                fund_entries = conn.execute("""SELECT entry_id FROM fund_cash_ledger
                    WHERE entry_id>? AND product_code=? AND event=? AND reference=? AND amount=?
                      AND entry_id NOT IN (SELECT fund_entry_id FROM treasury_bond_cash_fund_links)
                    ORDER BY entry_id""",
                    (int(fund_watermark), str(fund_owner["product_code"]),
                     "bond_coupon" if event_type == "coupon" else "bond_redemption",
                     str(product_code), net)).fetchall()
                if len(fund_entries) != 1:
                    raise RuntimeError("국채 지급 이벤트에 대응하는 펀드 현금 입금이 없거나 하나로 특정되지 않습니다.")
                conn.execute("INSERT INTO treasury_bond_cash_fund_links(event_key,fund_entry_id) VALUES (?,?)",
                             (source_key, int(fund_entries[0]["entry_id"])))
            else:
                if wallet_entry_id is None and wallet_watermark is None:
                    raise ValueError("국채 지급의 고객 지갑 기준점이 없습니다.")
                # Identify the payment by everything known about it — holder,
                # currency, scale and exact amount — never by "first credit".
                paid_currency, paid_places, paid_units = (
                    (str(fx_settlement["currency"]).upper(), int(fx_settlement["decimal_places"]), settled)
                    if fx_settlement is not None else (currency, int(decimal_places), net))
                scope, bound = (("entry_id=?", int(wallet_entry_id)) if wallet_entry_id is not None
                                else ("entry_id>?", int(wallet_watermark)))
                wallet_entries = conn.execute(f"""SELECT entry_id FROM customer_wallet_ledger
                    WHERE {scope} AND user_id=? AND currency_code=? AND decimal_places=?
                      AND delta_minor_units=?
                      AND entry_id NOT IN (SELECT wallet_entry_id FROM treasury_bond_cash_wallet_links)
                    ORDER BY entry_id""",
                    (bound, str(user_id), paid_currency, paid_places, paid_units)).fetchall()
                if len(wallet_entries) != 1:
                    raise RuntimeError("국채 지급 이벤트에 대응하는 고객 지갑 입금이 없거나 하나로 특정되지 않습니다.")
                conn.execute("INSERT INTO treasury_bond_cash_wallet_links(event_key,wallet_entry_id) VALUES (?,?)",
                             (source_key, int(wallet_entries[0]["entry_id"])))

    @classmethod
    def _post_treasury_bond_fx_settlement(cls, conn, event_key, entity, currency, places, net,
                                          fx_settlement, recorded_epoch):
        """Record the conversion snapshot and move the payable through FX clearing."""
        from fractions import Fraction
        from functions.money import convert_minor_units, currency_precision

        target = str(fx_settlement["currency"]).upper()
        target_places = int(fx_settlement["decimal_places"])
        settled = int(fx_settlement["minor_units"])
        rate = Fraction(fx_settlement["rate"])
        if net <= 0 or target == currency or rate <= 0:
            raise ValueError("국채 지급 환산 정보가 올바르지 않습니다.")
        if target_places != currency_precision(conn, target):
            raise ValueError("국채 지급 환산 통화의 정밀도가 통화 설정과 다릅니다.")
        if settled != convert_minor_units(net, places, rate, target_places):
            raise ValueError("국채 지급 환산액이 기록할 환율과 일치하지 않습니다.")
        conn.execute("""INSERT INTO treasury_bond_cash_fx_settlements
            (event_key,source_currency,source_decimal_places,source_minor_units,
             settlement_currency,settlement_decimal_places,settlement_minor_units,
             rate_numerator,rate_denominator,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (event_key, currency, places, net, target, target_places, settled,
             str(rate.numerator), str(rate.denominator), recorded_epoch))
        # customer_cash_payable was mapped by the event posting itself (net > 0).
        cls.register_mapping(conn, "treasury_bond", "fx_conversion_clearing", "fx_conversion_clearing")
        cls.post(conn, ledger_scope="treasury_bond", entity_type="holding",
            entity_id=entity, source_key=f"{event_key}:fx:source", currency_code=currency,
            decimal_places=places, recorded_epoch=recorded_epoch,
            lines=(("customer_cash_payable", "debit", net),
                   ("fx_conversion_clearing", "credit", net)),
            description="Treasury bond payment · bond currency into FX clearing")
        if settled:
            # Below one minor unit of the holder's currency nothing is paid, so
            # no settlement-currency entry or wallet row may exist either.
            cls.post(conn, ledger_scope="treasury_bond", entity_type="holding",
                entity_id=entity, source_key=f"{event_key}:fx:settlement", currency_code=target,
                decimal_places=target_places, recorded_epoch=recorded_epoch,
                lines=(("fx_conversion_clearing", "debit", settled),
                       ("customer_cash_payable", "credit", settled)),
                description="Treasury bond payment · holder currency out of FX clearing")
        return settled

    @staticmethod
    def wallet_watermark(conn):
        return conn.execute("SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger").fetchone()[0]

    @staticmethod
    def _currency_cash_amount(conn, currency, units):
        from functions.money import currency_precision
        places = currency_precision(conn, currency)
        minor, remainder = divmod(int(units), 10 ** (6 - places))
        if remainder or not minor:
            raise ValueError("환전 결제액은 통화 최소단위에 맞아야 합니다.")
        return places, minor

    @classmethod
    def post_currency_cash(cls, conn, kind, transaction_id, epoch, legs):
        """Keep the two currencies separate and trace split funding to actual wallet rows."""
        for role, owner, currency, units, before, after in legs:
            places, delta = cls._currency_cash_amount(conn, currency, units)
            wallet_rows = conn.execute("""SELECT entry_id,user_id,source_type,source_account,
                currency_code,decimal_places,delta_minor_units FROM customer_wallet_ledger WHERE entry_id>? AND entry_id<=? ORDER BY entry_id""",
                (before, after)).fetchall()
            if (any((r["user_id"], r["currency_code"], r["decimal_places"]) != (owner, currency, places)
                    or (r["delta_minor_units"] > 0) != (delta > 0) for r in wallet_rows)
                    or sum(r["delta_minor_units"] for r in wallet_rows) != delta):
                raise RuntimeError("환전 또는 송금의 지갑 변동이 결제액과 일치하지 않습니다.")
            key = f"{kind}:{transaction_id}:{role}"
            dr, cr = {"source": ("fx_cash_control", "fx_settlement_clearing"),
                      "target": ("fx_settlement_clearing", "fx_cash_control"),
                      "fee": ("fx_cash_control", "fx_fee_revenue")}[role]
            for account in (dr, cr):
                cls.register_mapping(conn, "currency_cash", account, account)
            entry = cls.post(conn, ledger_scope="currency_cash", entity_type=kind,
                             entity_id=str(transaction_id), source_key=key, currency_code=currency,
                             decimal_places=places, recorded_epoch=int(epoch),
                             lines=((dr, "debit", abs(delta)), (cr, "credit", abs(delta))),
                             description=f"{kind} {role} settlement")
            conn.execute("""INSERT INTO currency_cash_postings
                (transaction_kind,transaction_id,role,user_id,currency_code,decimal_places,
                 delta_minor_units,journal_entry_id) VALUES (?,?,?,?,?,?,?,?)""",
                (kind, transaction_id, role, owner, currency, places, delta, entry))
            bank_index = 0
            for wallet in wallet_rows:
                issuer = bank_entry = None
                if wallet["source_type"] == "account":
                    row = conn.execute("""SELECT COALESCE(s.issuer,p.issuer)
                        FROM accounts a LEFT JOIN saving s ON s.product_code=a.product_code
                        LEFT JOIN liability p ON p.product_code=a.product_code
                        WHERE a.account_number=?""", (wallet["source_account"],)).fetchone()
                    issuer = row[0] if row else None
                if issuer:
                    from functions.money import from_minor_units
                    from managers.bank_journal import BankJournal

                    bank_index += 1
                    outgoing = delta < 0
                    suffix = f"out:{bank_index}" if outgoing else "in" if role == "target" else "fee"
                    bank_event = f"customer_{kind}_{'out' if outgoing else 'in' if role == 'target' else 'fee'}"
                    reference = f"{kind}:{transaction_id}:{suffix}"
                    amount = from_minor_units(abs(wallet["delta_minor_units"]), places)
                    liability = "customer_deposits_payable"
                    asset = "settlement_asset"
                    BankJournal.post(conn, issuer, bank_event, reference, currency,
                                     [(liability if outgoing else asset, "debit", amount),
                                      (asset if outgoing else liability, "credit", amount)], now=epoch)
                    bank_entry = conn.execute("""SELECT entry_id FROM bank_journal_entries
                        WHERE issuer=? AND event_type=? AND reference_key=? AND currency_code=?""",
                        (issuer, bank_event, reference, currency)).fetchone()[0]
                conn.execute("""INSERT INTO currency_cash_wallet_links
                    (transaction_kind,transaction_id,role,wallet_entry_id,institution_id,bank_entry_id)
                    VALUES (?,?,?,?,?,?)""", (kind, transaction_id, role, wallet["entry_id"], issuer, bank_entry))

    @classmethod
    def post_currency_fee(cls, conn, kind, transaction_id, epoch, fee, receipt, before):
        if not fee:
            return
        from functions.money import currency_precision, to_minor_units

        treasury, base, currency, units = receipt
        places = currency_precision(conn, currency)
        after = cls.wallet_watermark(conn)
        if units:
            _, minor = cls._currency_cash_amount(conn, currency, units)
            cls.post_currency_cash(conn, kind, transaction_id, epoch,
                                   (("fee", treasury, currency, units, before, after),))
        else:
            minor = 0
            if after != before:
                raise RuntimeError("0으로 반올림된 환전 수수료에 지갑 변동이 발생했습니다.")
        conn.execute("""INSERT INTO currency_cash_fee_settlements
            (transaction_kind,transaction_id,treasury_id,quote_currency,quote_fee_minor_units,
             receipt_currency,receipt_minor_units,receipt_decimal_places) VALUES (?,?,?,?,?,?,?,?)""",
            (kind, transaction_id, treasury, base, to_minor_units(fee, currency_precision(conn, base)),
             currency, minor, places))

    @classmethod
    def reconcile_currency_cash(cls, conn):
        """Compare new source and target movements to native transactions and common postings."""
        postings = {}
        for r in conn.execute("""SELECT p.*, j.ledger_scope,j.entity_type,j.entity_id,j.source_key,
            j.currency_code journal_currency,j.decimal_places journal_places,j.recorded_epoch,
            dr.debit_units debit,cr.credit_units credit,
            (SELECT COUNT(*) FROM accounting_lines l WHERE l.entry_id=j.entry_id) line_count
            FROM currency_cash_postings p LEFT JOIN accounting_entries j ON j.entry_id=p.journal_entry_id
            LEFT JOIN accounting_lines dr ON dr.entry_id=j.entry_id AND dr.account_code=
                CASE p.role WHEN 'target' THEN 'fx_settlement_clearing' ELSE 'fx_cash_control' END
            LEFT JOIN accounting_lines cr ON cr.entry_id=j.entry_id AND cr.account_code=
                CASE p.role WHEN 'source' THEN 'fx_settlement_clearing'
                    WHEN 'fee' THEN 'fx_fee_revenue' ELSE 'fx_cash_control' END"""):
            postings[(r["transaction_kind"], r["transaction_id"], r["role"])] = r
        receipts = {}
        for r in conn.execute("""SELECT l.*,w.user_id,w.currency_code,w.decimal_places,w.delta_minor_units,
            b.issuer bank_issuer,b.event_type bank_event,b.currency_code bank_currency,
            b.decimal_places bank_places,b.status bank_status,
            d.debit_units deposit_debit,d.credit_units deposit_credit,
            a.debit_units settlement_debit,a.credit_units settlement_credit,
            (SELECT COUNT(*) FROM bank_journal_lines bl WHERE bl.entry_id=b.entry_id) bank_line_count
            FROM currency_cash_wallet_links l LEFT JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
            LEFT JOIN bank_journal_entries b ON b.entry_id=l.bank_entry_id
            LEFT JOIN bank_journal_lines d ON d.entry_id=b.entry_id AND d.account_code='customer_deposits_payable'
            LEFT JOIN bank_journal_lines a ON a.entry_id=b.entry_id AND a.account_code='settlement_asset'"""):
            receipts.setdefault((r["transaction_kind"], r["transaction_id"], r["role"]), []).append(r)
        from functions.money import currency_precision, to_minor_units
        fee_rows = {(r["transaction_kind"], r["transaction_id"]): r for r in conn.execute(
            "SELECT * FROM currency_cash_fee_settlements")}
        seen_fees = set()
        gaps, seen = [], set()
        for kind, table, source, target in (
            ("exchange", "currency_transactions", "user_id", "user_id"),
            ("transfer", "currency_transfer_transactions", "sender_id", "recipient_id"),
        ):
            for tx in conn.execute(f"""SELECT * FROM {table} WHERE transaction_id>
                (SELECT through_transaction_id FROM currency_cash_activation WHERE transaction_kind=?)""", (kind,)):
                tid = tx["transaction_id"]
                legs = [("source", tx[source], tx["source_currency"], -tx["source_units"]),
                        ("target", tx[target], tx["target_currency"], tx["target_units"])]
                if tx["fee_nwx"]:
                    seen_fees.add((kind, tid))
                    fee = fee_rows.get((kind, tid))
                    if not fee:
                        gaps.append({"transaction_kind": kind, "transaction_id": tid,
                                     "role": "fee", "status": "missing_fee_settlement"})
                    else:
                        base = fee["quote_currency"]
                        if (fee["quote_fee_minor_units"] != to_minor_units(tx["fee_nwx"], currency_precision(conn, base))
                                or fee["receipt_decimal_places"] != currency_precision(conn, fee["receipt_currency"])):
                            gaps.append({"transaction_kind": kind, "transaction_id": tid,
                                         "role": "fee", "status": "invalid_fee_settlement"})
                        if fee["receipt_minor_units"]:
                            legs.append(("fee", fee["treasury_id"], fee["receipt_currency"],
                                         fee["receipt_minor_units"] * 10 ** (6 - fee["receipt_decimal_places"])))
                for role, owner, currency, units in legs:
                    k = kind, tid, role
                    seen.add(k)
                    finding = {"transaction_kind": kind, "transaction_id": tid, "role": role}
                    post = postings.get(k)
                    try:
                        places, delta = cls._currency_cash_amount(conn, currency, units)
                    except ValueError:
                        gaps.append({**finding, "status": "invalid_native_amount"})
                        continue
                    if post is None:
                        gaps.append({**finding, "status": "missing_posting"})
                        continue
                    if (post["user_id"], post["currency_code"], post["decimal_places"],
                            post["delta_minor_units"], post["ledger_scope"], post["entity_type"],
                            post["entity_id"], post["source_key"], post["journal_currency"],
                            post["journal_places"], post["recorded_epoch"], post["debit"], post["credit"],
                            post["line_count"]) != (owner, currency, places, delta, "currency_cash", kind,
                            str(tid), f"{kind}:{tid}:{role}", currency, places,
                            int(tx["recorded_epoch"]), abs(delta), abs(delta), 2):
                        gaps.append({**finding, "status": "journal_mismatch"})
                    linked = receipts.get(k, [])
                    if (any((r["user_id"], r["currency_code"], r["decimal_places"]) != (owner, currency, places)
                            or ((r["delta_minor_units"] or 0) > 0) != (delta > 0) for r in linked)
                            or sum(r["delta_minor_units"] or 0 for r in linked) != delta):
                        gaps.append({**finding, "status": "wallet_mismatch"})
                    for r in linked:
                        if r["institution_id"] is None:
                            continue
                        debit = max(0, -(r["delta_minor_units"] or 0))
                        credit = max(0, r["delta_minor_units"] or 0)
                        suffix = {"source": "out", "target": "in", "fee": "fee"}[role]
                        if (r["bank_issuer"], r["bank_event"], r["bank_currency"], r["bank_places"],
                                r["bank_status"], r["deposit_debit"], r["deposit_credit"],
                                r["settlement_debit"], r["settlement_credit"], r["bank_line_count"]) != (
                                r["institution_id"], f"customer_{kind}_{suffix}", currency, places,
                                "posted", debit, credit, credit, debit, 2):
                            gaps.append({**finding, "wallet_entry_id": r["wallet_entry_id"],
                                         "status": "institution_mismatch"})
        for kind, tid, role in sorted((set(postings) | set(receipts)) - seen):
            gaps.append({"transaction_kind": kind, "transaction_id": tid, "role": role, "status": "orphan_posting"})
        for kind, tid in sorted(set(fee_rows) - seen_fees):
            gaps.append({"transaction_kind": kind, "transaction_id": tid, "role": "fee",
                         "status": "orphan_fee_settlement"})
        for row in conn.execute("""SELECT source_key FROM accounting_entries j WHERE ledger_scope='currency_cash'
            AND NOT EXISTS(SELECT 1 FROM currency_cash_postings p WHERE p.journal_entry_id=j.entry_id)"""):
            gaps.append({"source_key": row[0], "status": "missing_native_posting"})
        return {"event_count": len(postings), "gaps": gaps,
                "historical_baseline_policy": "pre-activation transactions lack historical cash links",
                "status": "mismatch" if gaps else "matched"}

    @classmethod
    def post_option_exercise(cls, conn, contract, holder, quantity, tax, epoch, *, payer_range, recipient_start, recipient_fund_start):
        """Record the gross exercise and tax in the contract's settlement currency."""
        currency = base_currency_code(conn)
        gross = int(contract["strike_price"]) * int(quantity)
        tax = int(tax)
        key = f"option-exercise:{contract['contract_id']}:{holder}"
        lines = [("exercise_cash_clearing", "debit", gross),
                 ("exercise_seller_payable", "credit", gross-tax)]
        if tax:
            lines.append(("exercise_tax_payable", "credit", tax))
        for account, _, _ in lines:
            cls.register_mapping(conn, "option_exercise", account, account)
        entry = cls.post(conn, ledger_scope="option_exercise", entity_type="option_contract",
                         entity_id=contract["contract_code"], source_key=key,
                         currency_code=currency, decimal_places=0, recorded_epoch=epoch,
                         lines=lines, description="Option exercise consideration")
        conn.execute("""INSERT INTO option_exercise_events
            (event_key,contract_id,contract_code,option_type,holder_id,writer_id,quantity,
             gross_minor_units,tax_minor_units,currency_code,decimal_places,recorded_epoch,journal_entry_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (key,contract["contract_id"],contract["contract_code"],contract["option_type"],
             str(holder),contract["writer_id"],quantity,gross,tax,currency,epoch,entry))

        payer = str(holder) if contract["option_type"] == "call" else None
        recipient = str(contract["writer_id"]) if payer else str(holder)
        if payer:
            debits = conn.execute("""SELECT entry_id,user_id,currency_code,decimal_places,delta_minor_units
                FROM customer_wallet_ledger WHERE entry_id>? AND entry_id<=? ORDER BY entry_id""", payer_range).fetchall()
            if (any((r["user_id"],r["currency_code"],r["decimal_places"]) != (payer,currency,0)
                    or r["delta_minor_units"]>=0 for r in debits)
                    or sum(r["delta_minor_units"] for r in debits) != -gross):
                raise RuntimeError("옵션 행사 출금과 행사가액이 일치하지 않습니다.")
            for r in debits:
                conn.execute("INSERT INTO option_exercise_cash_links VALUES (?,?,?,NULL,?)",
                             (key,"payer",r["entry_id"],r["delta_minor_units"]))
        net = gross-tax
        if net:
            from database.fund_accounting import FundCashLedger
            fund = FundCashLedger(conn).account(recipient)
            if fund:
                rows = conn.execute("""SELECT entry_id,amount_minor_units FROM fund_cash_ledger
                    WHERE entry_id>? AND product_code=? AND event='option_settlement'
                    AND currency_code=? AND decimal_places=0""", (recipient_fund_start,fund["product_code"],currency)).fetchall()
                if len(rows)!=1 or rows[0]["amount_minor_units"] != net:
                    raise RuntimeError("옵션 행사 펀드 입금과 순대금이 일치하지 않습니다.")
                conn.execute("INSERT INTO option_exercise_cash_links VALUES (?,?,NULL,?,?)",
                             (key,"recipient",rows[0]["entry_id"],net))
            else:
                rows = conn.execute("""SELECT entry_id,user_id,currency_code,decimal_places,delta_minor_units
                    FROM customer_wallet_ledger WHERE entry_id>? ORDER BY entry_id""", (recipient_start,)).fetchall()
                if (len(rows)!=1 or (rows[0]["user_id"],rows[0]["currency_code"],rows[0]["decimal_places"],
                        rows[0]["delta_minor_units"]) != (recipient,currency,0,net)):
                    raise RuntimeError("옵션 행사 수취 지갑과 순대금이 일치하지 않습니다.")
                conn.execute("INSERT INTO option_exercise_cash_links VALUES (?,?,?,NULL,?)",
                             (key,"recipient",rows[0]["entry_id"],net))

    @staticmethod
    def record_option_exercise_assets(conn, contract, holder, quantity, before, after):
        from decimal import Decimal
        key = f"option-exercise:{contract['contract_id']}:{holder}"
        is_call = contract['option_type'] == 'call'
        expected = {'collateral': -Decimal(quantity if is_call else quantity * int(contract['strike_price'])),
                    'recipient': Decimal(quantity)}
        if not is_call:
            expected['seller'] = -Decimal(quantity)
        if set(before) != set(expected) or set(after) != set(expected):
            raise ValueError('옵션 행사 자산 이동 범위가 올바르지 않습니다.')
        for role, delta in expected.items():
            if after[role] - before[role] != delta:
                raise RuntimeError(f'옵션 행사 {role} 이동 수량이 일치하지 않습니다.')
            user = str(holder) if role == 'seller' or (role == 'recipient' and is_call) else str(contract['writer_id'])
            asset = base_currency_code(conn) if role == 'collateral' and not is_call else contract['underlying_code']
            conn.execute("""INSERT INTO option_exercise_asset_movements
                (event_key,role,user_id,asset_code,quantity,before_quantity,after_quantity)
                VALUES (?,?,?,?,?,?,?)""", (key,role,user,asset,str(delta),str(before[role]),str(after[role])))

    @classmethod
    def post_option_collateral_cash(cls, conn, contract_id, collateral_id, writer, movement,
                                    amount, wallet_id, epoch, *, remaining):
        currency = base_currency_code(conn)
        key = f"option-collateral:{contract_id}:{collateral_id}:{movement}:{remaining}"
        debit, credit = (("collateral_cash", "collateral_funding") if movement == 'deposit'
                         else ("collateral_funding", "collateral_cash"))
        for code in (debit,credit):
            cls.register_mapping(conn, 'option_collateral', code, code)
        journal = cls.post(conn, ledger_scope='option_collateral', entity_type='option_contract',
            entity_id=str(contract_id), source_key=key, currency_code=currency, decimal_places=0,
            recorded_epoch=epoch, lines=[(debit,'debit',amount),(credit,'credit',amount)])
        conn.execute("""INSERT INTO option_collateral_cash_events
            (event_key,contract_id,collateral_id,writer_id,movement,currency_code,decimal_places,
             amount_minor_units,wallet_entry_id,journal_entry_id,recorded_epoch)
            VALUES (?,?,?,?,?,?,0,?,?,?,?)""",
            (key,contract_id,collateral_id,str(writer),movement,currency,amount,wallet_id,journal,epoch))

    @staticmethod
    def reconcile_option_collateral_cash(conn):
        gaps, count = [], 0
        lines_by_entry = {}
        for line in conn.execute("""SELECT l.entry_id,l.account_code,l.debit_units,l.credit_units
            FROM option_collateral_cash_events e
            JOIN accounting_lines l ON l.entry_id=e.journal_entry_id"""):
            lines_by_entry.setdefault(line['entry_id'], []).append(line)
        for row in conn.execute("""SELECT e.*,c.writer_id contract_writer,c.option_type,
                w.user_id wallet_user,w.currency_code wallet_currency,w.decimal_places wallet_places,
                w.delta_minor_units wallet_delta,j.ledger_scope,j.entity_type,j.entity_id,j.source_key,
                j.currency_code journal_currency,j.decimal_places journal_places,j.recorded_epoch journal_epoch
            FROM option_collateral_cash_events e LEFT JOIN option_contracts c USING(contract_id)
            LEFT JOIN customer_wallet_ledger w ON w.entry_id=e.wallet_entry_id
            LEFT JOIN accounting_entries j ON j.entry_id=e.journal_entry_id"""):
            count += 1
            amount = row['amount_minor_units']
            deposit = row['movement']=='deposit'
            expected_delta = -amount if deposit else amount
            expected = ({'collateral_cash':(amount,0),'collateral_funding':(0,amount)} if deposit
                        else {'collateral_funding':(amount,0),'collateral_cash':(0,amount)})
            lines = lines_by_entry.get(row['journal_entry_id'], [])
            actual = {r['account_code']:(r['debit_units'],r['credit_units']) for r in lines}
            if ((row['contract_writer'],row['option_type']) != (row['writer_id'],'put')
                or row['decimal_places'] != 0
                or (row['wallet_user'],row['wallet_currency'],row['wallet_places'],row['wallet_delta']) !=
                   (row['writer_id'],row['currency_code'],0,expected_delta)
                or (row['ledger_scope'],row['entity_type'],row['entity_id'],row['source_key'],
                    row['journal_currency'],row['journal_places'],row['journal_epoch']) !=
                   ('option_collateral','option_contract',str(row['contract_id']),row['event_key'],
                    row['currency_code'],0,row['recorded_epoch']) or actual!=expected or len(lines)!=2):
                gaps.append({'event_key':row['event_key'],'status':'collateral_cash_mismatch'})
        for row in conn.execute("""SELECT c.contract_id,c.strike_price,c.issued_contracts,c.status,
                COALESCE(e.deposited,0) deposited,COALESCE(e.released,0) released,
                COALESCE(x.exercised,0) exercised,COALESCE(l.balance,0) balance
            FROM option_contracts c
            LEFT JOIN (SELECT contract_id,SUM(CASE WHEN movement='deposit' THEN amount_minor_units ELSE 0 END) deposited,
                SUM(CASE WHEN movement='release' THEN amount_minor_units ELSE 0 END) released
                FROM option_collateral_cash_events GROUP BY contract_id) e USING(contract_id)
            LEFT JOIN (SELECT contract_id,SUM(gross_minor_units) exercised FROM option_exercise_events
                WHERE option_type='put' GROUP BY contract_id) x USING(contract_id)
            LEFT JOIN (SELECT contract_id,SUM(amount) balance FROM option_collateral_cash_lots GROUP BY contract_id) l USING(contract_id)
            WHERE c.option_type='put' AND c.contract_id>(SELECT through_contract_id
                FROM option_collateral_cash_activation WHERE singleton_id=1)"""):
            expected = int(row['strike_price']) * int(row['issued_contracts'])
            if int(row['deposited']) != expected:
                gaps.append({'contract_id':row['contract_id'], 'status':'deposit_total_mismatch',
                             'expected_minor_units':expected,'actual_minor_units':int(row['deposited'])})
            if int(row['deposited']) - int(row['released']) - int(row['exercised']) != int(row['balance']):
                gaps.append({'contract_id':row['contract_id'], 'status':'collateral_balance_mismatch'})
        for row in conn.execute("""SELECT source_key FROM accounting_entries j
            WHERE ledger_scope='option_collateral' AND NOT EXISTS
            (SELECT 1 FROM option_collateral_cash_events e WHERE e.journal_entry_id=j.entry_id)"""):
            gaps.append({'source_key':row[0],'status':'orphan_journal'})
        return {'event_count':count,'gaps':gaps,'status':'mismatch' if gaps else 'matched',
                'historical_baseline_policy':'prior deposits and releases have no wallet links'}

    @staticmethod
    def reconcile_option_collateral(conn):
        """Reconcile live written obligations, positions and collateral without altering history."""
        from decimal import Decimal

        contracts = {r['contract_id']: r for r in conn.execute(
            "SELECT contract_id,contract_code,option_type,writer_id,underlying_code,strike_price,"
            "outstanding_contracts,status FROM option_contracts")}
        outstanding, unfilled, share_lots, cash_lots, gaps = {}, {}, {}, {}, []
        for row in conn.execute("SELECT contract_id,user_id,quantity FROM option_positions"):
            if row['contract_id'] not in contracts:
                gaps.append({'contract_id':row['contract_id'], 'user_id':row['user_id'],
                             'status':'orphan_position'})
            outstanding[row['contract_id']] = outstanding.get(row['contract_id'], 0) + int(row['quantity'])
        contracts_by_code = {contract['contract_code']: contract for contract in contracts.values()}
        for row in conn.execute("""SELECT order_id,user_id,asset_code,remaining_quantity FROM market_orders
            WHERE order_kind='option' AND option_order_type='write' AND side='sell'
              AND status IN ('open','partial')"""):
            contract = contracts_by_code.get(row['asset_code'])
            if not contract:
                gaps.append({'order_id':row['order_id'], 'asset_code':row['asset_code'],
                             'status':'orphan_write_order'})
            elif row['user_id'] != contract['writer_id']:
                gaps.append({'order_id':row['order_id'], 'contract_id':contract['contract_id'],
                             'status':'write_order_owner_mismatch'})
            if contract and contract['status'] != 'active':
                gaps.append({'order_id':row['order_id'], 'contract_id':contract['contract_id'],
                             'status':'inactive_contract_write_order'})
            unfilled[row['asset_code']] = unfilled.get(row['asset_code'], 0) + int(row['remaining_quantity'])
        for table, field, totals in (("option_collateral_lots", "quantity", share_lots),
                                     ("option_collateral_cash_lots", "amount", cash_lots)):
            for row in conn.execute(f"SELECT * FROM {table}"):
                contract = contracts.get(row['contract_id'])
                if (not contract or row['writer_id'] != contract['writer_id'] or
                    (field == 'quantity' and row['underlying_code'] != contract['underlying_code'])):
                    gaps.append({'contract_id':row['contract_id'], 'status':'invalid_collateral_owner', 'source':table})
                totals[row['contract_id']] = totals.get(row['contract_id'], Decimal(0)) + Decimal(str(row[field]))
        for cid, contract in contracts.items():
            active = contract['status'] == 'active'
            held = outstanding.get(cid, 0)
            written = int(contract['outstanding_contracts'])
            if (active and held != written) or (not active and held):
                gaps.append({'contract_id':cid, 'status':'position_mismatch',
                             'held_contracts':held, 'outstanding_contracts':written})
            exposure = written + unfilled.get(contract['contract_code'], 0) if active else 0
            shares, cash = share_lots.get(cid, Decimal(0)), cash_lots.get(cid, Decimal(0))
            expected_shares = Decimal(exposure if contract['option_type']=='call' else 0)
            expected_cash = Decimal(exposure * int(contract['strike_price']) if contract['option_type']=='put' else 0)
            if shares != expected_shares or cash != expected_cash:
                gaps.append({'contract_id':cid, 'status':'collateral_mismatch',
                             'share_quantity':str(shares), 'expected_shares':str(expected_shares),
                             'cash_minor_units':str(cash), 'expected_cash_minor_units':str(expected_cash)})
        return {'contract_count':len(contracts), 'gaps':gaps,
                'status':'mismatch' if gaps else 'matched',
                'coverage':'current positions and collateral; historical collateral movements are not reconstructed'}

    @staticmethod
    def reconcile_option_exercises(conn):
        gaps, count = [], 0
        cash_links = {}
        for link in conn.execute("""SELECT l.*,w.user_id,w.currency_code,w.decimal_places,w.delta_minor_units wallet_delta,
            f.amount_minor_units fund_delta,f.currency_code fund_currency,f.decimal_places fund_places,
            a.owner_id fund_owner FROM option_exercise_cash_links l
            LEFT JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
            LEFT JOIN fund_cash_ledger f ON f.entry_id=l.fund_entry_id
            LEFT JOIN fund_accounts a ON a.product_code=f.product_code"""):
            cash_links.setdefault(link['event_key'], []).append(link)
        rows = conn.execute("""SELECT x.*,c.contract_code actual_code,c.option_type actual_type,
            c.writer_id actual_writer,c.strike_price,c.underlying_code,j.ledger_scope,j.entity_type,j.entity_id,j.source_key,
            j.currency_code journal_currency,j.decimal_places journal_places,j.recorded_epoch journal_epoch
            FROM option_exercise_events x LEFT JOIN option_contracts c USING(contract_id)
            LEFT JOIN accounting_entries j ON j.entry_id=x.journal_entry_id""")
        for r in rows:
            count += 1
            expected = {"exercise_cash_clearing": (r["gross_minor_units"], 0),
                        "exercise_seller_payable": (0, r["gross_minor_units"]-r["tax_minor_units"])}
            if not expected["exercise_seller_payable"][1]:
                del expected["exercise_seller_payable"]
            if r["tax_minor_units"]:
                expected["exercise_tax_payable"] = (0,r["tax_minor_units"])
            journal_lines = conn.execute(
                "SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=?",
                (r["journal_entry_id"],)).fetchall()
            actual = {line[0]:(line[1],line[2]) for line in journal_lines}
            baseline = conn.execute("SELECT 1 FROM option_exercise_link_baseline WHERE event_key=?", (r["event_key"],)).fetchone()
            if not baseline:
                links = cash_links.get(r['event_key'], [])
                totals = {"payer":0,"recipient":0}
                for link in links:
                    role = link["role"]
                    owner = r["holder_id"] if role=="payer" or r["option_type"]=="put" else r["writer_id"]
                    if link["wallet_entry_id"] is not None:
                        valid = (link["user_id"],link["currency_code"],link["decimal_places"],link["wallet_delta"]) == (owner,r["currency_code"],0,link["delta_minor_units"])
                    else:
                        valid = (link["fund_owner"],link["fund_currency"],link["fund_places"],link["fund_delta"]) == (owner,r["currency_code"],0,link["delta_minor_units"])
                    if not valid or (link["delta_minor_units"] >= 0 if role=="payer" else link["delta_minor_units"] <= 0):
                        gaps.append({"event_key":r["event_key"],"status":"cash_link_mismatch"})
                    totals[role] += link["delta_minor_units"]
                expected_payer = -r["gross_minor_units"] if r["option_type"]=="call" else 0
                if totals != {"payer":expected_payer,"recipient":r["gross_minor_units"]-r["tax_minor_units"]}:
                    gaps.append({"event_key":r["event_key"],"status":"cash_amount_mismatch"})
            if not conn.execute('SELECT 1 FROM option_exercise_asset_baseline WHERE event_key=?', (r['event_key'],)).fetchone():
                from decimal import Decimal, InvalidOperation
                moves = conn.execute('SELECT * FROM option_exercise_asset_movements WHERE event_key=?', (r['event_key'],)).fetchall()
                call = r['option_type'] == 'call'
                wanted = {'collateral': -Decimal(r['quantity'] if call else r['gross_minor_units']), 'recipient': Decimal(r['quantity'])}
                if not call:
                    wanted['seller'] = -Decimal(r['quantity'])
                if {m['role'] for m in moves} != set(wanted):
                    gaps.append({'event_key': r['event_key'], 'status': 'missing_asset_movements'})
                for m in moves:
                    owner = r['holder_id'] if m['role']=='seller' or (m['role']=='recipient' and call) else r['writer_id']
                    asset = r['currency_code'] if m['role']=='collateral' and not call else r['underlying_code']
                    try:
                        valid = (Decimal(m['quantity']) == wanted[m['role']] and
                                 Decimal(m['after_quantity']) - Decimal(m['before_quantity']) == wanted[m['role']] and
                                 Decimal(m['before_quantity']) >= 0 and Decimal(m['after_quantity']) >= 0)
                    except (InvalidOperation, KeyError):
                        valid = False
                    if not valid or m['user_id'] != owner or m['asset_code'] != asset:
                        gaps.append({'event_key': r['event_key'], 'status': 'asset_movement_mismatch'})
            if ((r["actual_code"],r["actual_type"],r["actual_writer"]) !=
                    (r["contract_code"],r["option_type"],r["writer_id"])
                or r["gross_minor_units"] != r["quantity"] * (r["strike_price"] or 0)
                or (r["ledger_scope"],r["entity_type"],r["entity_id"],r["source_key"],r["journal_currency"],
                    r["journal_places"],r["journal_epoch"]) !=
                   ("option_exercise","option_contract",r["contract_code"],r["event_key"],r["currency_code"],0,r["recorded_epoch"])
                or len(journal_lines) != len(expected) or actual != expected):
                gaps.append({"event_key":r["event_key"],"status":"exercise_mismatch"})
        for orphan in conn.execute("""SELECT entry_id,source_key FROM accounting_entries j
            WHERE j.ledger_scope='option_exercise' AND NOT EXISTS
            (SELECT 1 FROM option_exercise_events x WHERE x.journal_entry_id=j.entry_id)"""):
            gaps.append({"entry_id": orphan["entry_id"], "source_key": orphan["source_key"],
                         "status": "orphan_journal"})
        for table in ("option_exercise_cash_links", "option_exercise_asset_movements",
                      "option_exercise_link_baseline", "option_exercise_asset_baseline"):
            for orphan in conn.execute(f"""SELECT DISTINCT l.event_key FROM {table} l
                LEFT JOIN option_exercise_events e ON e.event_key=l.event_key WHERE e.event_key IS NULL"""):
                gaps.append({"event_key": orphan[0], "source": table, "status": "orphan_link"})
        return {"event_count":count,"gaps":gaps,"status":"mismatch" if gaps else "matched",
                "historical_baseline_policy":"only exercises after activation are represented"}

    @classmethod
    def post_option_premium(cls, conn, buy_order, sell_order, quantity, price, now):
        """Post the transfer from reserved premium and link the actual receipt."""
        from database.fund_accounting import FundCashLedger
        from functions.money import currency_precision
        from managers.currency_manager import CurrencyManager, SCALE

        contract = str(buy_order["asset_code"])
        buyer, seller = str(buy_order["user_id"]), str(sell_order["user_id"])
        buy_id, sell_id = int(buy_order["order_id"]), int(sell_order["order_id"])
        key = f"option-premium:{buy_id}:{sell_id}:{int(buy_order['filled_quantity']) + quantity}"
        currency = base_currency_code(conn)
        if (currency_precision(conn, currency) != 0
                or any(order["quote_currency"] != currency for order in (buy_order, sell_order))):
            raise ValueError("옵션 주문의 프리미엄 결제 통화가 일치하지 않습니다.")
        premium = price * quantity
        accounts = (("option_reserved_cash", "debit", premium),
                    ("option_seller_cash", "credit", premium))
        for account, _, _ in accounts:
            cls.register_mapping(conn, "option", account, account)
        entry = cls.post(conn, ledger_scope="option", entity_type="option_contract",
                         entity_id=contract, source_key=key, currency_code=currency,
                         decimal_places=0, recorded_epoch=now, lines=accounts,
                         description="Option premium settlement")
        wallet_entry = fund_entry = None
        fund = FundCashLedger(conn)
        if fund.account(seller):
            fund_entry = fund.deposit(seller, premium, currency, "option_premium", key, now)
        else:
            watermark = conn.execute(
                "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
            ).fetchone()[0]
            CurrencyManager.change(conn, seller, currency, premium * SCALE)
            receipts = conn.execute("""SELECT entry_id FROM customer_wallet_ledger
                WHERE entry_id>? AND user_id=? AND currency_code=? AND decimal_places=0
                AND delta_minor_units=?""", (watermark, seller, currency, premium)).fetchall()
            if len(receipts) != 1:
                raise RuntimeError("옵션 프리미엄에 대응하는 지갑 입금이 없습니다.")
            wallet_entry = receipts[0][0]
        conn.execute("""INSERT INTO option_premium_events
            (event_key,contract_code,buy_order_id,sell_order_id,buyer_id,seller_id,quantity,
             price_minor_units,premium_minor_units,currency_code,decimal_places,recorded_epoch,
             journal_entry_id,wallet_entry_id,fund_entry_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
            (key, contract, buy_id, sell_id, buyer, seller, quantity, price, premium,
             currency, now, entry, wallet_entry, fund_entry))

    @staticmethod
    def reconcile_option_premiums(conn):
        """Verify each recorded premium against its orders, posting and receipt."""
        rows = conn.execute("""SELECT e.*,
            b.user_id buy_user,s.user_id sell_user,
            j.ledger_scope,j.entity_type,j.entity_id,j.source_key,
            j.currency_code journal_currency,j.decimal_places journal_places,
            j.recorded_epoch journal_epoch,
            dr.debit_units reserved_debit,cr.credit_units seller_credit,
            (SELECT COUNT(*) FROM accounting_lines l WHERE l.entry_id=j.entry_id) line_count,
            w.user_id wallet_owner,w.currency_code wallet_currency,
            w.decimal_places wallet_places,w.delta_minor_units wallet_delta,
            a.owner_id fund_owner,f.currency_code fund_currency,f.decimal_places fund_places,
            f.amount_minor_units fund_delta,f.event fund_event,f.reference fund_reference
            FROM option_premium_events e
            LEFT JOIN market_orders b ON b.order_id=e.buy_order_id AND b.side='buy'
                AND b.order_kind='option' AND b.asset_code=e.contract_code AND b.quote_currency=e.currency_code
            LEFT JOIN market_orders s ON s.order_id=e.sell_order_id AND s.side='sell'
                AND s.order_kind='option' AND s.asset_code=e.contract_code AND s.quote_currency=e.currency_code
            LEFT JOIN accounting_entries j ON j.entry_id=e.journal_entry_id
            LEFT JOIN accounting_lines dr ON dr.entry_id=j.entry_id AND dr.account_code='option_reserved_cash'
            LEFT JOIN accounting_lines cr ON cr.entry_id=j.entry_id AND cr.account_code='option_seller_cash'
            LEFT JOIN customer_wallet_ledger w ON w.entry_id=e.wallet_entry_id
            LEFT JOIN fund_cash_ledger f ON f.entry_id=e.fund_entry_id
            LEFT JOIN fund_accounts a ON a.product_code=f.product_code
            ORDER BY e.event_key""").fetchall()
        gaps = []
        for row in rows:
            key, contract, premium = row["event_key"], row["contract_code"], row["premium_minor_units"]
            currency, places = row["currency_code"], row["decimal_places"]
            receipt = (row["seller_id"], currency, places, premium)
            if (row["buy_user"], row["sell_user"]) != (row["buyer_id"], row["seller_id"]):
                gaps.append({"event_key": key, "status": "order_mismatch"})
            if (row["ledger_scope"], row["entity_type"], row["entity_id"], row["source_key"],
                    row["journal_currency"], row["journal_places"], row["journal_epoch"],
                    row["reserved_debit"], row["seller_credit"], row["line_count"]) != (
                    "option", "option_contract", contract, key, currency, places, row["recorded_epoch"],
                    premium, premium, 2):
                gaps.append({"event_key": key, "status": "journal_mismatch"})
            if row["wallet_entry_id"] is not None:
                actual = tuple(row[k] for k in ("wallet_owner", "wallet_currency", "wallet_places", "wallet_delta"))
                if actual != receipt:
                    gaps.append({"event_key": key, "status": "wallet_mismatch"})
            else:
                actual = tuple(row[k] for k in ("fund_owner", "fund_currency", "fund_places", "fund_delta"))
                if actual != receipt or (row["fund_event"], row["fund_reference"]) != ("option_premium", key):
                    gaps.append({"event_key": key, "status": "fund_mismatch"})
        orphaned = conn.execute("""SELECT j.source_key FROM accounting_entries j
            WHERE j.ledger_scope='option' AND j.entity_type='option_contract'
              AND NOT EXISTS(SELECT 1 FROM option_premium_events e WHERE e.journal_entry_id=j.entry_id)""")
        gaps.extend({"event_key": row[0], "status": "missing_premium_event"} for row in orphaned)
        cash_links = {}
        for link in conn.execute("""SELECT l.*,o.user_id,o.quote_currency,o.funding_account,
            w.user_id wallet_owner,w.source_type,w.source_account,w.currency_code wallet_currency,
            w.decimal_places wallet_places,w.delta_minor_units wallet_delta,
            a.owner_id fund_owner,f.currency_code fund_currency,f.decimal_places fund_places,
            f.amount_minor_units fund_delta,f.event fund_event
            FROM option_order_cash_links l
            LEFT JOIN market_orders o ON o.order_id=l.order_id AND o.order_kind='option' AND o.side='buy'
            LEFT JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
            LEFT JOIN fund_cash_ledger f ON f.entry_id=l.fund_entry_id
            LEFT JOIN fund_accounts a ON a.product_code=f.product_code"""):
            order_id = link["order_id"]
            expected = (link["user_id"], link["quote_currency"], 0)
            if link["wallet_entry_id"] is not None:
                amount = link["wallet_delta"]
                actual = (link["wallet_owner"], link["wallet_currency"], link["wallet_places"])
                account = link["funding_account"]
                valid = (link["source_type"], link["source_account"]) == (
                    ("cash", link["user_id"]) if account is None else ("account", account))
            else:
                amount = link["fund_delta"]
                actual = (link["fund_owner"], link["fund_currency"], link["fund_places"])
                valid = link["fund_event"] == "option_order_" + link["movement"]
            reserve = link["movement"] == "reserve"
            if (not valid or not expected[0] or actual != expected or amount is None
                    or (amount >= 0 if reserve else amount <= 0)):
                gaps.append({"order_id": order_id, "link_id": link["link_id"], "status": "cash_link_mismatch"})
            cash_links.setdefault(order_id, []).append((reserve, int(amount or 0)))
        for order in conn.execute("""WITH fill_legs AS (
                SELECT buy_order_id order_id,quantity,premium_minor_units premium FROM option_premium_events
                UNION ALL SELECT sell_order_id,quantity,0 FROM option_premium_events
            ), fills AS (
                SELECT order_id,SUM(quantity) quantity,SUM(premium) premium FROM fill_legs GROUP BY order_id
            )
            SELECT o.order_id,o.side,o.quantity,o.limit_price_minor_units,o.filled_quantity,
                o.reserved_cash_minor_units,COALESCE(f.quantity,0) audited_quantity,
                COALESCE(f.premium,0) premium,COALESCE(b.filled_quantity,0) baseline_quantity,
                o.order_id>(SELECT through_order_id FROM option_audit_activation WHERE singleton_id=1) tracked
            FROM market_orders o LEFT JOIN fills f USING(order_id)
            LEFT JOIN option_order_fill_baselines b USING(order_id)
            WHERE o.order_kind='option'"""):
            order_id = order["order_id"]
            if order["filled_quantity"] != order["baseline_quantity"] + order["audited_quantity"]:
                gaps.append({"order_id": order_id, "status": "fill_quantity_mismatch"})
            if order["side"] == "buy" and order["tracked"]:
                links = cash_links.get(order_id, [])
                reserves = [amount for reserve, amount in links if reserve]
                expected_debit = order["premium"] + int(order["reserved_cash_minor_units"] or 0)
                initial = order["quantity"] * order["limit_price_minor_units"]
                if reserves != [-initial] or -sum(amount for _, amount in links) != expected_debit:
                    gaps.append({"order_id": order_id, "status": "reserve_reconciliation_mismatch"})
        return {"event_count": len(rows), "gaps": gaps,
                "historical_baseline_policy": "old fill quantities are baseline-only; legacy reserves have no cash links",
                "status": "mismatch" if gaps else "matched"}

    @staticmethod
    def _treasury_bond_fx_settlement_problem(conn, event, fx, wallet_link, fund_link):
        """Recompute one converted bond payment from its own rate snapshot."""
        from fractions import Fraction
        from functions.money import convert_minor_units, currency_precision

        net, places = int(event["net_minor_units"]), int(event["decimal_places"])
        currency = str(event["currency_code"])
        try:
            numerator, denominator = int(fx["rate_numerator"]), int(fx["rate_denominator"])
            target = str(fx["settlement_currency"])
            target_places = int(fx["settlement_decimal_places"])
            settled = int(fx["settlement_minor_units"])
            valid = (numerator > 0 and denominator > 0 and target != currency
                     and fx["source_currency"] == currency
                     and int(fx["source_decimal_places"]) == places
                     and int(fx["source_minor_units"]) == net
                     and target_places == currency_precision(conn, target)
                     and settled == convert_minor_units(
                         net, places, Fraction(numerator, denominator), target_places))
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            return {"status": "fx_settlement_mismatch"}

        def lines(suffix):
            entry = conn.execute("""SELECT entry_id,currency_code,decimal_places,recorded_epoch
                FROM accounting_entries WHERE ledger_scope='treasury_bond' AND entity_type='holding'
                  AND entity_id=? AND source_key=?""",
                (str(event["holding_id"]), f"{event['event_key']}:fx:{suffix}")).fetchone()
            if not entry:
                return None
            return ((entry["currency_code"], int(entry["decimal_places"]), int(entry["recorded_epoch"])),
                    {str(line["account_code"]): (int(line["debit_units"]), int(line["credit_units"]))
                     for line in conn.execute(
                         "SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=?",
                         (entry["entry_id"],))})
        epoch = int(event["recorded_epoch"])
        expected_source = ((currency, places, epoch), {
            "customer_cash_payable": (net, 0), "fx_conversion_clearing": (0, net)})
        expected_settlement = ((target, target_places, epoch), {
            "fx_conversion_clearing": (settled, 0), "customer_cash_payable": (0, settled)}) if settled else None
        if lines("source") != expected_source or lines("settlement") != expected_settlement:
            return {"status": "fx_settlement_journal_mismatch"}
        if fund_link or bool(wallet_link) != bool(settled):
            return {"status": "missing_or_invalid_wallet_settlement_link"}
        if settled and (wallet_link["user_id"] != event["user_id"]
                        or wallet_link["currency_code"] != target
                        or int(wallet_link["decimal_places"] or 0) != target_places
                        or int(wallet_link["delta_minor_units"] or 0) != settled):
            return {"expected_user_id": event["user_id"], "actual_user_id": wallet_link["user_id"],
                    "expected_currency_code": target, "actual_currency_code": wallet_link["currency_code"],
                    "expected_decimal_places": target_places,
                    "actual_decimal_places": int(wallet_link["decimal_places"] or 0),
                    "expected_minor_units": settled,
                    "actual_minor_units": int(wallet_link["delta_minor_units"] or 0),
                    "status": "wallet_settlement_amount_or_currency_mismatch"}
        return None

    @staticmethod
    def _treasury_bond_fx_clearing_positions(conn, gaps):
        """Per-currency FX clearing balances, each explained by the conversion snapshots.

        Clearing is not netted across currencies: the bond currency stays credited
        and the holder's currency debited, like the exchange and tax-remittance
        clearing accounts. The open balance is the simulator's own FX position, so
        every unit of it must come from a recorded conversion.
        """
        expected = {}
        for row in conn.execute("""SELECT source_currency,source_decimal_places,source_minor_units,
                settlement_currency,settlement_decimal_places,settlement_minor_units
                FROM treasury_bond_cash_fx_settlements"""):
            key = (str(row[0]), int(row[1]))
            debit, credit = expected.get(key, (0, 0))
            expected[key] = (debit, credit + int(row[2]))
            if int(row[5]):
                key = (str(row[3]), int(row[4]))
                debit, credit = expected.get(key, (0, 0))
                expected[key] = (debit + int(row[5]), credit)
        actual = {}
        for row in conn.execute(
            """SELECT e.currency_code,e.decimal_places,l.debit_units,l.credit_units
               FROM accounting_entries e JOIN accounting_lines l ON l.entry_id=e.entry_id
               WHERE e.ledger_scope='treasury_bond' AND l.account_code=(
                   SELECT common_code FROM accounting_account_mappings
                   WHERE ledger_scope='treasury_bond' AND local_code='fx_conversion_clearing' AND active=1)"""):
            key = (str(row[0]), int(row[1]))
            debit, credit = actual.get(key, (0, 0))
            actual[key] = (debit + int(row[2]), credit + int(row[3]))
        positions = []
        for key in sorted(set(expected) | set(actual)):
            have, want = actual.get(key, (0, 0)), expected.get(key, (0, 0))
            if key in actual:
                positions.append({"currency_code": key[0], "decimal_places": key[1],
                                  "debit_units": have[0], "credit_units": have[1],
                                  "balance_units": have[0] - have[1]})
            if have != want:
                gaps.append({"currency_code": key[0], "decimal_places": key[1],
                             "expected_debit_units": want[0], "expected_credit_units": want[1],
                             "actual_debit_units": have[0], "actual_credit_units": have[1],
                             "status": "fx_clearing_position_mismatch"})
        return positions

    @staticmethod
    def treasury_fx_valuation(conn, positions):
        """Indicative spot value of clearing positions; no gain or posting is inferred."""
        from functions.money import base_currency_code, currency_precision, convert_minor_units
        from managers.currency_manager import CurrencyManager

        base = base_currency_code(conn)
        places = currency_precision(conn, base)
        valued = []
        for position in positions:
            currency = position['currency_code']
            rate = CurrencyManager.quote(conn, currency, base)
            units = int(position['balance_units'])
            value = convert_minor_units(abs(units), position['decimal_places'], rate, places)
            valued.append({**position, 'rate_numerator': str(rate.numerator),
                           'rate_denominator': str(rate.denominator),
                           'base_minor_units': value if units >= 0 else -value})
        return {'base_currency': base, 'base_decimal_places': places, 'positions': valued,
                'total_base_minor_units': sum(row['base_minor_units'] for row in valued),
                'simulation_only': True,
                'method': 'current spot value; historical base cost and revaluation gain are not calculated'}

    @classmethod
    def reconcile_treasury_bond_cash_events(cls, conn):
        """Check bond cash events against journal and recipient settlement rows."""
        gaps = []
        seen = set()
        baseline = int(conn.execute("SELECT through_rowid FROM treasury_bond_cash_link_baseline_state "
                                    "WHERE singleton_id=1").fetchone()[0])
        for event in conn.execute("SELECT * FROM treasury_bond_cash_events ORDER BY event_key"):
            journal = conn.execute("""SELECT entry_id,currency_code,decimal_places,recorded_epoch
                FROM accounting_entries WHERE ledger_scope='treasury_bond' AND entity_type='holding'
                  AND entity_id=? AND source_key=?""",
                (str(event["holding_id"]),event["event_key"])).fetchone()
            expected = {}
            amounts = (("bond_principal_payable",int(event["principal_minor_units"])),
                       ("bond_interest_expense",int(event["interest_minor_units"])),
                       ("customer_cash_payable",-int(event["net_minor_units"])),
                       ("tax_payable",-int(event["tax_minor_units"])))
            for local, signed in amounts:
                if not signed:
                    continue
                mapping = conn.execute("SELECT common_code FROM accounting_account_mappings "
                    "WHERE ledger_scope='treasury_bond' AND local_code=? AND active=1",(local,)).fetchone()
                if not mapping:
                    gaps.append({"event_key":event["event_key"],"missing_mapping":local})
                    continue
                code = str(mapping[0])
                debit,credit = expected.get(code,(0,0))
                expected[code] = (debit+max(0,signed),credit+max(0,-signed))
            actual = {}
            if journal:
                seen.add(int(journal["entry_id"]))
                for line in conn.execute("SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=?",
                                         (journal["entry_id"],)):
                    actual[str(line["account_code"])] = (int(line["debit_units"]),int(line["credit_units"]))
            if (int(event["principal_minor_units"])+int(event["interest_minor_units"])
                    != int(event["tax_minor_units"])+int(event["net_minor_units"])
                    or not journal or journal["currency_code"] != event["currency_code"]
                    or int(journal["decimal_places"]) != int(event["decimal_places"])
                    or int(journal["recorded_epoch"]) != int(event["recorded_epoch"])
                    or actual != expected):
                gaps.append({"event_key":event["event_key"],"expected_lines":expected,
                             "actual_lines":actual,"status":"mismatch"})
            event_rowid = conn.execute("SELECT rowid FROM treasury_bond_cash_events WHERE event_key=?",
                                       (str(event["event_key"]),)).fetchone()[0]
            if int(event_rowid) <= baseline:
                continue
            fund_owner = conn.execute("SELECT product_code FROM fund_accounts WHERE owner_id=?",
                                      (str(event["user_id"]),)).fetchone()
            wallet_link = conn.execute("""SELECT w.wallet_entry_id,l.user_id,l.currency_code,
                l.decimal_places,l.delta_minor_units FROM treasury_bond_cash_wallet_links w
                LEFT JOIN customer_wallet_ledger l ON l.entry_id=w.wallet_entry_id
                WHERE w.event_key=?""", (str(event["event_key"]),)).fetchone()
            fund_link = conn.execute("""SELECT w.fund_entry_id,l.product_code,l.event,l.reference,l.amount
                FROM treasury_bond_cash_fund_links w LEFT JOIN fund_cash_ledger l
                ON l.entry_id=w.fund_entry_id WHERE w.event_key=?""",
                (str(event["event_key"]),)).fetchone()
            net = int(event["net_minor_units"])
            fx = conn.execute("SELECT * FROM treasury_bond_cash_fx_settlements WHERE event_key=?",
                              (str(event["event_key"]),)).fetchone()
            if fx and (fund_owner or net <= 0):
                gaps.append({"event_key":event["event_key"],"status":"unexpected_fx_settlement"})
            if fund_owner:
                expected_event = "bond_coupon" if event["event_type"] == "coupon" else "bond_redemption"
                if (net > 0 and (not fund_link or fund_link["product_code"] != fund_owner["product_code"]
                        or fund_link["event"] != expected_event
                        or fund_link["reference"] != event["product_code"]
                        or int(fund_link["amount"] or 0) != net)
                        or (net == 0 and fund_link) or wallet_link):
                    gaps.append({"event_key":event["event_key"],
                                 "status":"missing_or_invalid_fund_settlement_link"})
            elif net > 0 and fx:
                problem = cls._treasury_bond_fx_settlement_problem(conn, event, fx, wallet_link, fund_link)
                if problem:
                    gaps.append({"event_key":event["event_key"], **problem})
            elif net > 0:
                if not wallet_link or fund_link:
                    gaps.append({"event_key":event["event_key"],
                                 "status":"missing_or_invalid_wallet_settlement_link"})
                else:
                    actual_minor = int(wallet_link["delta_minor_units"] or 0)
                    actual_places = int(wallet_link["decimal_places"] or 0)
                    if (wallet_link["user_id"] != event["user_id"]
                            or wallet_link["currency_code"] != event["currency_code"]
                            or actual_places != int(event["decimal_places"])
                            or actual_minor != net):
                        gaps.append({"event_key":event["event_key"],
                            "expected_user_id":event["user_id"],
                            "actual_user_id":wallet_link["user_id"],
                            "expected_currency_code":event["currency_code"],
                            "actual_currency_code":wallet_link["currency_code"],
                            "expected_decimal_places":int(event["decimal_places"]),
                            "actual_decimal_places":actual_places,
                            "expected_minor_units":net,
                            "actual_minor_units":actual_minor,
                            "status":"wallet_settlement_amount_or_currency_mismatch"})
            elif wallet_link or fund_link:
                gaps.append({"event_key":event["event_key"],
                             "status":"unexpected_settlement_link_for_zero_net"})
        orphan_count = int(conn.execute("""SELECT COUNT(*) FROM accounting_entries a
            WHERE a.ledger_scope='treasury_bond' AND NOT EXISTS (
                SELECT 1 FROM treasury_bond_cash_events e WHERE e.holding_id=a.entity_id
                  AND e.event_key=a.source_key)
              AND NOT EXISTS (
                SELECT 1 FROM treasury_bond_cash_fx_settlements f
                JOIN treasury_bond_cash_events e ON e.event_key=f.event_key
                WHERE e.holding_id=a.entity_id AND f.event_key=CASE
                    WHEN a.source_key LIKE '%:fx:source'
                        THEN substr(a.source_key,1,length(a.source_key)-10)
                    WHEN a.source_key LIKE '%:fx:settlement'
                        THEN substr(a.source_key,1,length(a.source_key)-14) END)""").fetchone()[0])
        if orphan_count:
            gaps.append({"orphan_common_entries":orphan_count,"status":"mismatch"})
        orphan_links = int(conn.execute("""SELECT
            (SELECT COUNT(*) FROM treasury_bond_cash_wallet_links w
             LEFT JOIN treasury_bond_cash_events e USING(event_key) WHERE e.event_key IS NULL)+
            (SELECT COUNT(*) FROM treasury_bond_cash_fund_links w
             LEFT JOIN treasury_bond_cash_events e USING(event_key) WHERE e.event_key IS NULL)+
            (SELECT COUNT(*) FROM treasury_bond_cash_fx_settlements w
             LEFT JOIN treasury_bond_cash_events e USING(event_key) WHERE e.event_key IS NULL)""").fetchone()[0])
        if orphan_links:
            gaps.append({"orphan_settlement_links":orphan_links,"status":"mismatch"})
        fx_clearing_positions = cls._treasury_bond_fx_clearing_positions(conn, gaps)
        try:
            fx_valuation = cls.treasury_fx_valuation(conn, fx_clearing_positions)
        except (ValueError, ArithmeticError) as exc:
            fx_valuation = {'available': False, 'reason': str(exc)}
        return {"event_count":int(conn.execute("SELECT COUNT(*) FROM treasury_bond_cash_events").fetchone()[0]),
                "fx_settlement_count":int(conn.execute(
                    "SELECT COUNT(*) FROM treasury_bond_cash_fx_settlements").fetchone()[0]),
                "fx_clearing_positions":fx_clearing_positions,
                "fx_valuation":fx_valuation,
                "legacy_unlinked_baseline_through_rowid":baseline,
                "common_journal_gaps":gaps,"status":"matched" if not gaps else "mismatch"}

    @staticmethod
    def audit_unbalanced(conn, ledger_scope=None):
        sql = ("SELECT e.entry_id,e.ledger_scope,e.entity_type,e.entity_id,e.currency_code "
               "FROM accounting_entries e LEFT JOIN accounting_lines l USING(entry_id)")
        params = []
        if ledger_scope is not None:
            sql += " WHERE e.ledger_scope=?"
            params.append(str(ledger_scope))
        sql += " GROUP BY e.entry_id HAVING COUNT(l.line_id)<2 OR SUM(l.debit_units)<>SUM(l.credit_units)"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    @classmethod
    def rebuild_from_subledgers(cls, conn):
        """Idempotently reconstruct common rows from posted company/bank journals."""
        cls.ensure_schema(conn)
        rebuilt = {"company": 0, "institution": 0}
        for entry in conn.execute(
            "SELECT e.entry_id,e.company_code,e.source_key,e.recorded_epoch,e.description,f.currency,f.decimal_places "
            "FROM financial_entries e JOIN financial_entities f USING(company_code) "
            "WHERE e.status='posted' ORDER BY e.entry_id"
        ).fetchall():
            legs = conn.execute("SELECT account,amount FROM financial_lines WHERE entry_id=?", (entry[0],)).fetchall()
            mapped = []
            for account, units in legs:
                cls.register_mapping(conn, "company", account, account)
                mapped.append((account, "debit" if units > 0 else "credit", abs(int(units))))
            before = conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='company' AND entity_id=? AND source_key=?", (entry[1], entry[2])).fetchone()[0]
            cls.post(conn, ledger_scope="company", entity_type="company", entity_id=entry[1],
                     source_key=entry[2], currency_code=entry[5], decimal_places=entry[6],
                     recorded_epoch=int(entry[3]), lines=mapped, description=entry[4])
            rebuilt["company"] += int(not before)
        for entry in conn.execute(
            "SELECT entry_id,issuer,event_type,reference_key,currency_code,decimal_places,created_epoch,description "
            "FROM bank_journal_entries WHERE status='posted' ORDER BY entry_id"
        ).fetchall():
            legs = conn.execute("SELECT account_code,debit_units,credit_units FROM bank_journal_lines WHERE entry_id=?", (entry[0],)).fetchall()
            mapped = []
            for account, debit, credit in legs:
                cls.register_mapping(conn, "institution", account, account)
                if debit:
                    mapped.append((account, "debit", int(debit)))
                if credit:
                    mapped.append((account, "credit", int(credit)))
            key = f"{entry[2]}:{entry[3]}"
            before = conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='institution' AND entity_id=? AND source_key=?", (entry[1], key)).fetchone()[0]
            cls.post(conn, ledger_scope="institution", entity_type="financial_institution", entity_id=entry[1],
                     source_key=key, currency_code=entry[4], decimal_places=entry[5],
                     recorded_epoch=int(entry[6]), lines=mapped, description=entry[7])
            rebuilt["institution"] += int(not before)
        return rebuilt

    @staticmethod
    def reconcile_subledgers(conn):
        """Compare native rows/lines with common postings; never repair gaps silently."""
        checks = []
        scopes = (
            ("company", "SELECT e.entry_id,e.company_code entity_id,e.source_key,"
             "e.recorded_epoch,f.currency currency_code,f.decimal_places "
             "FROM financial_entries e JOIN financial_entities f USING(company_code) "
             "WHERE e.status='posted' ORDER BY e.entry_id"),
            ("institution", "SELECT entry_id,issuer entity_id,event_type,reference_key,"
             "currency_code,decimal_places,created_epoch FROM bank_journal_entries "
             "WHERE status='posted' ORDER BY entry_id"),
        )
        for scope, native_query in scopes:
            native_rows = conn.execute(native_query).fetchall()
            native = len(native_rows)
            common = int(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope=?", (scope,)).fetchone()[0])
            common_rows = conn.execute(
                "SELECT entry_id,entity_type,entity_id,source_key,currency_code,decimal_places,recorded_epoch "
                "FROM accounting_entries WHERE ledger_scope=?", (scope,),
            ).fetchall()
            common_by_key = {(str(row["entity_type"]), str(row["entity_id"]), str(row["source_key"])): row
                             for row in common_rows}
            common_lines = {}
            for line in conn.execute(
                    "SELECT l.entry_id,l.account_code,l.debit_units,l.credit_units "
                    "FROM accounting_lines l JOIN accounting_entries e USING(entry_id) "
                    "WHERE e.ledger_scope=?", (scope,)).fetchall():
                entry_lines = common_lines.setdefault(int(line["entry_id"]), {})
                code = str(line["account_code"])
                old_debit, old_credit = entry_lines.get(code, (0, 0))
                entry_lines[code] = (old_debit+int(line["debit_units"]),
                                     old_credit+int(line["credit_units"]))
            mapping = {str(row["local_code"]): str(row["common_code"])
                       for row in conn.execute(
                           "SELECT local_code,common_code FROM accounting_account_mappings "
                           "WHERE ledger_scope=? AND active=1", (scope,)).fetchall()}
            native_lines = {}
            if scope == "company":
                source_lines = conn.execute(
                    "SELECT entry_id,account,amount FROM financial_lines ORDER BY entry_id,line_id"
                ).fetchall()
                for line in source_lines:
                    entry_lines = native_lines.setdefault(int(line["entry_id"]), {})
                    code = str(line["account"])
                    entry_lines[code] = (entry_lines.get(code, (0,))[0]
                                         +int(line["amount"]),)
            else:
                source_lines = conn.execute(
                    "SELECT l.entry_id,l.account_code,l.debit_units,l.credit_units "
                    "FROM bank_journal_lines l JOIN bank_journal_entries e USING(entry_id) "
                    "WHERE e.status='posted' ORDER BY l.entry_id,l.line_id"
                ).fetchall()
                for line in source_lines:
                    entry_lines = native_lines.setdefault(int(line["entry_id"]), {})
                    code = str(line["account_code"])
                    old_debit, old_credit = entry_lines.get(code, (0, 0))
                    entry_lines[code] = (old_debit+int(line["debit_units"]),
                                         old_credit+int(line["credit_units"]))
            posting_gaps = 0
            seen_common = set()
            for entry in native_rows:
                entity_id = str(entry["entity_id"])
                if scope == "company":
                    source_key = str(entry["source_key"])
                    currency = str(entry["currency_code"])
                    places = int(entry["decimal_places"])
                    recorded = int(entry["recorded_epoch"])
                else:
                    source_key = f"{entry['event_type']}:{entry['reference_key']}"
                    currency = str(entry["currency_code"])
                    places = int(entry["decimal_places"])
                    recorded = int(float(entry["created_epoch"]))
                entity_type = 'company' if scope == 'company' else 'financial_institution'
                common_entry = common_by_key.get((entity_type, entity_id, source_key))
                if (common_entry is None or str(common_entry["currency_code"]) != currency
                        or int(common_entry["decimal_places"]) != places
                        or int(common_entry["recorded_epoch"]) != recorded):
                    posting_gaps += 1
                    continue
                seen_common.add(int(common_entry["entry_id"]))
                expected = {}
                for local_code, amounts in native_lines.get(int(entry["entry_id"]), {}).items():
                    common_code = mapping.get(local_code)
                    if common_code is None:
                        posting_gaps += 1
                        expected = None
                        break
                    if scope == "company":
                        amount = amounts[0]
                        debit, credit = (amount, 0) if amount > 0 else (0, -amount)
                    else:
                        debit, credit = amounts
                    old_debit, old_credit = expected.get(common_code, (0, 0))
                    expected[common_code] = (old_debit+debit, old_credit+credit)
                if expected is not None and common_lines.get(int(common_entry["entry_id"]), {}) != expected:
                    posting_gaps += 1
            # An equal row count can conceal an orphan common entry substituted
            # for a missing native posting; row-level lookup above detects it.
            if len(seen_common) != native:
                posting_gaps = max(posting_gaps, native-len(seen_common))
            checks.append({"ledger_scope": scope, "native_entries": native,
                           "common_entries": common, "missing_entries": native-common,
                           "posting_gaps": posting_gaps,
                           "status": "matched" if native == common and posting_gaps == 0
                           else "mismatch"})
        from decimal import Decimal
        from functions.money import to_minor_units
        company_bond_gaps = []
        financial_entities = conn.execute("SELECT company_code,currency,decimal_places "
            "FROM financial_entities ORDER BY company_code").fetchall()
        tracked_bond_issuers = {str(entity["company_code"]) for entity in financial_entities}
        for entity in financial_entities:
            issuer = str(entity["company_code"])
            currency = str(entity["currency"]).upper()
            places = int(entity["decimal_places"])
            bond_rows = conn.execute("""SELECT p.product_code,
                COALESCE(p.quote_currency,base_currency_code()) currency_code,
                p.face_value,COALESCE(h.quantity,0) quantity
                FROM product.investment p LEFT JOIN assets.user_assets h
                  ON h.asset_code=p.product_code
                WHERE p.product_type='corporate_bond' AND p.issuer_code=?
                ORDER BY p.product_code,h.user_id""",
                (issuer,)).fetchall()
            expected_signed_units = 0
            unsupported = []
            for bond in bond_rows:
                bond_currency = str(bond["currency_code"]).upper()
                if bond_currency != currency:
                    unsupported.append({"product_code": bond["product_code"],
                        "currency_code": bond_currency})
                    continue
                face_value = Decimal(str(bond["face_value"])) * int(bond["quantity"])
                expected_signed_units -= to_minor_units(face_value, places)
            discount_units = int(conn.execute("SELECT COALESCE(SUM(discount_units),0) "
                "FROM financial_bond_basis WHERE company_code=?", (issuer,)).fetchone()[0])
            expected_signed_units += discount_units
            actual_signed_units = int(conn.execute("""SELECT COALESCE(SUM(l.amount),0)
                FROM financial_lines l JOIN financial_entries e USING(entry_id)
                WHERE e.company_code=? AND e.status='posted'
                  AND l.account IN ('bonds','bond_discount')""",
                (issuer,)).fetchone()[0])
            if unsupported or actual_signed_units != expected_signed_units:
                company_bond_gaps.append({"issuer": issuer, "currency_code": currency,
                    "expected_signed_minor_units": expected_signed_units,
                    "actual_signed_minor_units": actual_signed_units,
                    "difference_minor_units": actual_signed_units-expected_signed_units,
                    "unsupported_product_currencies": unsupported,
                    "status": "unsupported_currency" if unsupported else "mismatch"})
        unbooked_bond_issuers = conn.execute("""SELECT p.issuer_code,
            p.product_code,COALESCE(p.quote_currency,base_currency_code()) currency_code,
            p.face_value,SUM(h.quantity) quantity
            FROM product.investment p JOIN assets.user_assets h
              ON h.asset_code=p.product_code
            WHERE p.product_type='corporate_bond' AND p.issuer_code IS NOT NULL
            GROUP BY p.issuer_code,p.product_code,COALESCE(p.quote_currency,base_currency_code()),p.face_value
            ORDER BY p.issuer_code,p.product_code""").fetchall()
        for bond in unbooked_bond_issuers:
            issuer = str(bond["issuer_code"])
            if issuer not in tracked_bond_issuers:
                company_bond_gaps.append({"issuer": issuer,
                    "currency_code": str(bond["currency_code"]).upper(),
                    "product_code": str(bond["product_code"]),
                    "outstanding_face_amount": int(bond["face_value"]) * int(bond["quantity"]),
                    "status": "missing_financial_entity"})
        checks.append({"ledger_scope": "company_bond_liabilities",
                       "issuer_count": len(financial_entities),
                       "gaps": company_bond_gaps,
                       "status": "matched" if not company_bond_gaps else "mismatch"})
        # Standard asset and option limit-buy locks are subledger balances,
        # distinct from customer spendable cash. Validate against the
        # remaining quantity and the quote-currency limit price.
        from functions.money import currency_precision, to_minor_units
        active = {"scheduled", "open", "partial"}
        reservation_gaps = []
        orders = conn.execute(
            "SELECT order_id,side,status,limit_price,limit_price_minor_units,"
            "limit_price_decimal_places,remaining_quantity,quote_currency,"
            "reserved_cash_minor_units,fractional_order,order_kind FROM market_orders "
            "WHERE order_kind IN ('asset','option') AND COALESCE(fractional_order,0)=0"
        ).fetchall()
        for order in orders:
            currency = str(order["quote_currency"] or base_currency_code(conn))
            places = currency_precision(conn, currency)
            canonical_price = int(order["limit_price_minor_units"] or 0)
            if (int(order["limit_price_decimal_places"] or 0) != places
                    or canonical_price != to_minor_units(order["limit_price"], places)):
                reservation_gaps.append({"order_id": int(order["order_id"]),
                    "expected_limit_price_minor_units": to_minor_units(order["limit_price"], places),
                    "actual_limit_price_minor_units": canonical_price,
                    "status": "price_mismatch"})
            expected = (canonical_price*int(order["remaining_quantity"])
                        if order["side"] == "buy" and order["status"] in active else 0)
            actual = int(order["reserved_cash_minor_units"] or 0)
            if actual != expected:
                reservation_gaps.append({"order_id": int(order["order_id"]),
                                         "currency_code": currency,
                                         "expected_minor_units": expected,
                                         "actual_minor_units": actual,
                                         "status": "mismatch"})
        checks.append({"ledger_scope": "market_order_reservations",
                       "order_count": len(orders),
                       "reservation_gaps": reservation_gaps,
                       "status": "matched" if not reservation_gaps else "mismatch"})
        order_wallet_gaps = []
        tracked_cash_orders = conn.execute("""SELECT o.order_id,o.user_id,o.quote_currency,
            o.status,o.reserved_cash_minor_units FROM market_orders o
            WHERE o.order_kind='asset' AND o.side='buy'
              AND COALESCE(o.fractional_order,0)=0
              AND NOT EXISTS(SELECT 1 FROM market_order_wallet_baselines b
                             WHERE b.order_id=o.order_id)
            ORDER BY o.order_id""").fetchall()
        for order in tracked_cash_orders:
            order_id = int(order["order_id"])
            currency = str(order["quote_currency"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            linked = conn.execute("""SELECT l.user_id,l.currency_code,l.decimal_places,
                l.delta_minor_units FROM market_order_wallet_links w
                JOIN customer_wallet_ledger l ON l.entry_id=w.wallet_entry_id
                WHERE w.order_id=? ORDER BY l.entry_id""", (order_id,)).fetchall()
            linked_net_debit = 0
            for movement in linked:
                if (str(movement["user_id"]) != str(order["user_id"])
                        or str(movement["currency_code"]).upper() != currency
                        or int(movement["decimal_places"]) != places):
                    order_wallet_gaps.append({"order_id": order_id,
                        "wallet_user": movement["user_id"],
                        "wallet_currency": movement["currency_code"],
                        "status": "incompatible_link"})
                linked_net_debit -= int(movement["delta_minor_units"])
            executed = int(conn.execute("""SELECT COALESCE(SUM(gross_minor_units),0)
                FROM market_execution_events WHERE buy_order_id=?""", (order_id,)).fetchone()[0])
            expected_net_debit = executed + int(order["reserved_cash_minor_units"] or 0)
            if linked_net_debit != expected_net_debit:
                order_wallet_gaps.append({"order_id": order_id,
                    "linked_net_debit_minor_units": linked_net_debit,
                    "execution_plus_reserve_minor_units": expected_net_debit,
                    "status": "mismatch"})
        orphan_order_wallet_links = int(conn.execute("""SELECT COUNT(*)
            FROM market_order_wallet_links w LEFT JOIN market_orders o USING(order_id)
            LEFT JOIN customer_wallet_ledger l ON l.entry_id=w.wallet_entry_id
            WHERE o.order_id IS NULL OR l.entry_id IS NULL""").fetchone()[0])
        if orphan_order_wallet_links:
            order_wallet_gaps.append({"orphan_market_order_wallet_links": orphan_order_wallet_links,
                                      "status": "mismatch"})
        checks.append({"ledger_scope": "market_order_wallet_cash",
                       "tracked_buy_order_count": len(tracked_cash_orders),
                       "wallet_link_gaps": order_wallet_gaps,
                       "legacy_baseline_count": int(conn.execute(
                           "SELECT COUNT(*) FROM market_order_wallet_baselines").fetchone()[0]),
                       "status": "matched" if not order_wallet_gaps else "mismatch"})
        execution_gaps = []
        tracked_orders = conn.execute("""SELECT o.order_id,o.side,o.asset_code,o.user_id,
            o.filled_quantity,COALESCE(b.baseline_filled_quantity,0) baseline_filled,
            COALESCE(b.baseline_execution_quantity,0) baseline_execution
            FROM market_orders o LEFT JOIN market_execution_order_baselines_v2 b USING(order_id)
            WHERE o.order_kind='asset' AND COALESCE(o.fractional_order,0)=0""").fetchall()
        for order in tracked_orders:
            order_id = int(order["order_id"])
            field = "buy_order_id" if order["side"] == "buy" else "sell_order_id"
            if field == "sell_order_id":
                recorded = int(conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM market_execution_events "
                    "WHERE execution_type='secondary' AND sell_order_id=?", (order_id,)
                ).fetchone()[0])
            else:
                recorded = int(conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM market_execution_events "
                    "WHERE buy_order_id=?", (order_id,)
                ).fetchone()[0])
            expected = int(order["filled_quantity"])-int(order["baseline_filled"])
            recorded_delta = recorded - int(order["baseline_execution"])
            if expected != recorded_delta or expected < 0 or recorded_delta < 0:
                execution_gaps.append({"order_id": order_id,
                    "expected_new_filled_quantity": expected,
                    "recorded_new_execution_quantity": recorded_delta,
                    "baseline_execution_quantity": int(order["baseline_execution"]),
                    "status": "mismatch"})
        malformed = int(conn.execute("""SELECT COUNT(*) FROM market_execution_events e
            LEFT JOIN market_orders b ON b.order_id=e.buy_order_id
            LEFT JOIN market_orders s ON s.order_id=e.sell_order_id
            WHERE b.order_id IS NULL OR b.side<>'buy' OR b.asset_code<>e.asset_code
              OR b.user_id<>e.buyer_id
              OR (e.execution_type='secondary' AND (s.order_id IS NULL OR s.side<>'sell'
                  OR s.asset_code<>e.asset_code OR s.user_id<>e.seller_id
                  OR b.quote_currency<>s.quote_currency))
              OR e.gross_minor_units<>e.price_minor_units*e.quantity
              OR (e.execution_type='secondary' AND
                  e.tax_minor_units+e.seller_proceeds_minor_units<>e.gross_minor_units)
              OR (e.execution_type='primary' AND
                  (e.tax_minor_units<>0 OR e.seller_proceeds_minor_units<>0))""").fetchone()[0])
        if malformed:
            execution_gaps.append({"malformed_execution_events": malformed,
                                   "status": "mismatch"})
        market_journal_gaps = []
        market_events = conn.execute("""SELECT e.* FROM market_execution_events e
            LEFT JOIN market_execution_journal_baselines b USING(execution_key)
            WHERE b.execution_key IS NULL ORDER BY e.execution_key""").fetchall()
        for event in market_events:
            journal = conn.execute("""SELECT entry_id,currency_code,decimal_places,recorded_epoch
                FROM accounting_entries WHERE ledger_scope='market' AND entity_type='market_trade'
                  AND entity_id=? AND source_key=?""",
                (event["asset_code"], event["execution_key"])).fetchone()
            expected_local = ([
                ("buyer_trade_receivable", int(event["gross_minor_units"]), 0),
                ("issuer_proceeds_clearing", 0, int(event["gross_minor_units"])),
            ] if event["execution_type"] == "primary" else [
                ("buyer_trade_receivable", int(event["gross_minor_units"]), 0),
                ("seller_trade_payable", 0, int(event["seller_proceeds_minor_units"])),
                ("tax_payable", 0, int(event["tax_minor_units"])),
            ])
            expected = {}
            for local, debit, credit in expected_local:
                if debit == 0 and credit == 0:
                    continue
                mapping = conn.execute("""SELECT common_code FROM accounting_account_mappings
                    WHERE ledger_scope='market' AND local_code=? AND active=1""", (local,)).fetchone()
                if not mapping:
                    market_journal_gaps.append({"execution_key": event["execution_key"],
                                                "missing_mapping": local})
                    continue
                code = str(mapping[0])
                old_debit, old_credit = expected.get(code, (0, 0))
                expected[code] = (old_debit+debit, old_credit+credit)
            actual = {}
            if journal:
                for line in conn.execute("SELECT account_code,debit_units,credit_units "
                    "FROM accounting_lines WHERE entry_id=?", (journal["entry_id"],)):
                    code = str(line["account_code"])
                    debit, credit = actual.get(code, (0, 0))
                    actual[code] = (debit+int(line["debit_units"]),
                                    credit+int(line["credit_units"]))
            if (not journal or journal["currency_code"] != event["currency_code"]
                    or int(journal["decimal_places"]) != int(event["decimal_places"])
                    or int(journal["recorded_epoch"]) != int(event["recorded_epoch"])
                    or actual != expected):
                market_journal_gaps.append({"execution_key": event["execution_key"],
                    "expected_lines": expected, "actual_lines": actual,
                    "status": "mismatch"})
        orphan_market_posts = int(conn.execute("""SELECT COUNT(*) FROM accounting_entries a
            WHERE a.ledger_scope='market' AND NOT EXISTS (
                SELECT 1 FROM market_execution_events e
                WHERE e.asset_code=a.entity_id AND e.execution_key=a.source_key)""").fetchone()[0])
        if orphan_market_posts:
            market_journal_gaps.append({"orphan_market_journal_entries": orphan_market_posts,
                                        "status": "mismatch"})
        wallet_link_gaps = []
        linked_wallet_rows = conn.execute("""SELECT e.execution_key,e.execution_type,
            e.seller_id,e.seller_proceeds_minor_units,e.currency_code,e.decimal_places,
            w.wallet_entry_id,l.user_id,l.currency_code wallet_currency,
            l.decimal_places wallet_places,l.delta_minor_units
            FROM market_execution_wallet_links w
            LEFT JOIN market_execution_events e USING(execution_key)
            LEFT JOIN customer_wallet_ledger l ON l.entry_id=w.wallet_entry_id
            ORDER BY w.execution_key,w.wallet_entry_id""").fetchall()
        seller_credits = {}
        for link in linked_wallet_rows:
            if (link["execution_type"] is None or link["user_id"] is None
                    or link["wallet_currency"] != link["currency_code"]
                    or int(link["wallet_places"]) != int(link["decimal_places"])):
                wallet_link_gaps.append({"execution_key": link["execution_key"],
                                         "wallet_entry_id": int(link["wallet_entry_id"]),
                                         "status": "missing_or_incompatible_wallet_entry"})
                continue
            if (link["execution_type"] == "secondary"
                    and str(link["user_id"]) == str(link["seller_id"])
                    and int(link["delta_minor_units"]) > 0):
                key = (str(link["execution_key"]), str(link["seller_id"]))
                seller_credits[key] = seller_credits.get(key, 0) + int(link["delta_minor_units"])
        for event in market_events:
            if event["execution_type"] != "secondary":
                continue
            proceeds = int(event["seller_proceeds_minor_units"])
            if proceeds <= 0:
                continue
            actual = seller_credits.get((str(event["execution_key"]), str(event["seller_id"])), 0)
            if actual != proceeds:
                wallet_link_gaps.append({"execution_key": event["execution_key"],
                    "seller_id": event["seller_id"],
                    "expected_seller_credit_minor_units": proceeds,
                    "linked_seller_credit_minor_units": actual,
                    "status": "mismatch"})
        orphan_wallet_links = int(conn.execute("""SELECT COUNT(*)
            FROM market_execution_wallet_links w LEFT JOIN market_execution_events e USING(execution_key)
            WHERE e.execution_key IS NULL""").fetchone()[0])
        if orphan_wallet_links:
            wallet_link_gaps.append({"orphan_wallet_links": orphan_wallet_links,
                                     "status": "mismatch"})
        primary_settlement_gaps = []
        from managers.currency_manager import CurrencyManager
        for event in market_events:
            if event["execution_type"] != "primary":
                continue
            asset_code = str(event["asset_code"])
            counterparty_kind = "treasury"
            recipient_id = None
            override = conn.execute("""SELECT recipient_id FROM market_execution_primary_recipient_overrides
                WHERE execution_key=?""", (event["execution_key"],)).fetchone()
            issuer = None if override else conn.execute("""SELECT COALESCE(parent_company_code,company_code)
                FROM company WHERE company_code=? AND dividend_funding='issuer'""",
                (asset_code,)).fetchone()
            corporate_bond = None if override else conn.execute("""SELECT issuer_code FROM product.investment
                WHERE product_code=? AND product_type='corporate_bond'""",
                (asset_code,)).fetchone()
            if override:
                counterparty_kind = "recorded_recipient"
                recipient_id = str(override[0])
            elif issuer:
                counterparty_kind = "issuer_cash"
                issuer_code = str(issuer[0])
                company_account = conn.execute(
                    "SELECT user_id FROM company_accounts WHERE company_code=?",
                    (issuer_code,)).fetchone()
                recipient_id = str(company_account[0]) if company_account else None
            elif corporate_bond:
                counterparty_kind = "issuer_cash"
                issuer_code = str(corporate_bond[0])
                company_account = conn.execute(
                    "SELECT user_id FROM company_accounts WHERE company_code=?",
                    (issuer_code,)).fetchone()
                recipient_id = str(company_account[0]) if company_account else None
            else:
                recipient_id = str(CurrencyManager.exchange_treasury(conn, asset_code))
            if not recipient_id:
                primary_settlement_gaps.append({
                    "execution_key": event["execution_key"],
                    "counterparty_kind": counterparty_kind,
                    "counterparty_id": issuer_code,
                    "status": "counterparty_cash_not_wallet_linkable",
                })
                continue
            receipt_rows = conn.execute("""SELECT l.currency_code,l.decimal_places,
                SUM(l.delta_minor_units) amount_minor_units
                FROM market_execution_wallet_links w
                JOIN customer_wallet_ledger l ON l.entry_id=w.wallet_entry_id
                WHERE w.execution_key=? AND l.user_id=? AND l.delta_minor_units>0
                GROUP BY l.currency_code,l.decimal_places""",
                (event["execution_key"], recipient_id)).fetchall()
            if (len(receipt_rows) != 1
                    or str(receipt_rows[0]["currency_code"]).upper() != str(event["currency_code"]).upper()
                    or int(receipt_rows[0]["decimal_places"]) != int(event["decimal_places"])
                    or int(receipt_rows[0]["amount_minor_units"]) != int(event["gross_minor_units"])):
                primary_settlement_gaps.append({
                    "execution_key": event["execution_key"],
                    "counterparty_kind": counterparty_kind,
                    "counterparty_id": recipient_id,
                    "expected_currency": event["currency_code"],
                    "expected_minor_units": int(event["gross_minor_units"]),
                    "linked_receipts": [dict(row) for row in receipt_rows],
                    "status": "missing_or_mismatched_primary_receipt",
                })
        holding_movement_gaps = []
        holding_events = conn.execute("""SELECT e.* FROM market_execution_events e
            LEFT JOIN market_execution_holding_baselines b USING(execution_key)
            WHERE b.execution_key IS NULL ORDER BY e.execution_key""").fetchall()
        for event in holding_events:
            movements = conn.execute("""SELECT party_role,user_id,inventory_type,signed_quantity
                FROM market_execution_holding_movements WHERE execution_key=?
                ORDER BY party_role""", (event["execution_key"],)).fetchall()
            expected = {"buyer": (str(event["buyer_id"]), int(event["quantity"]))}
            if event["execution_type"] == "secondary":
                expected["seller"] = (str(event["seller_id"]), -int(event["quantity"]))
            actual = {str(row["party_role"]): (
                str(row["user_id"]), int(row["signed_quantity"]),
                str(row["inventory_type"])) for row in movements}
            expected_type = ("treasury_bond" if conn.execute(
                "SELECT 1 FROM product.treasury_bond_products WHERE product_code=?",
                (event["asset_code"],)).fetchone() else "user_asset")
            normalized = {role: (user_id, quantity, expected_type)
                          for role, (user_id, quantity) in expected.items()}
            if actual != normalized:
                holding_movement_gaps.append({"execution_key": event["execution_key"],
                    "expected_movements": normalized, "actual_movements": actual,
                    "status": "mismatch"})
        orphan_holding_movements = int(conn.execute("""SELECT COUNT(*)
            FROM market_execution_holding_movements m LEFT JOIN market_execution_events e
              USING(execution_key) WHERE e.execution_key IS NULL""").fetchone()[0])
        if orphan_holding_movements:
            holding_movement_gaps.append({"orphan_holding_movements": orphan_holding_movements,
                                          "status": "mismatch"})
        checks.append({"ledger_scope": "market_executions",
                       "tracked_order_count": len(tracked_orders),
                       "execution_event_count": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_events").fetchone()[0]),
                       "execution_order_baseline_count": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_order_baselines_v2").fetchone()[0]),
                       "execution_gaps": execution_gaps,
                       "common_journal_gaps": market_journal_gaps,
                       "wallet_link_gaps": wallet_link_gaps,
                       "primary_settlement_gaps": primary_settlement_gaps,
                       "holding_movement_gaps": holding_movement_gaps,
                       "legacy_holding_baseline_count": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_holding_baselines").fetchone()[0]),
                       "unjournaled_legacy_baselines": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_journal_baselines").fetchone()[0]),
                       "historical_baseline_policy": "pre-migration fills are baseline-only; only subsequent fills are event-reconciled",
                       "status": "matched" if not execution_gaps and not market_journal_gaps
                       and not wallet_link_gaps and not primary_settlement_gaps
                       and not holding_movement_gaps else "mismatch"})
        checks.append({"ledger_scope": "lottery", **AccountingJournal.reconcile_lottery(conn)})
        checks.append({"ledger_scope": "issuer_cash", **AccountingJournal.reconcile_issuer_cash(conn)})
        checks.append({"ledger_scope": "business_cash", **AccountingJournal.reconcile_business_cash(conn)})
        checks.append({"ledger_scope": "futures_cash", **AccountingJournal.reconcile_futures_cash(conn)})
        checks.append({"ledger_scope": "swap_cash", **AccountingJournal.reconcile_swap_cash(conn)})
        checks.append({"ledger_scope": "els_locked", **AccountingJournal.reconcile_els_locked(conn)})
        checks.append({"ledger_scope": "settlement_records", **AccountingJournal.reconcile_settlement_records(conn)})
        checks.append({"ledger_scope": "period_closings",
                       **AccountingJournal.reconcile_period_closings(conn)})
        checks.append({"ledger_scope": "currency_cash",
                       **AccountingJournal.reconcile_currency_cash(conn)})
        checks.append({"ledger_scope": "option_collateral_cash",
                       **AccountingJournal.reconcile_option_collateral_cash(conn)})
        checks.append({"ledger_scope": "option_collateral",
                       **AccountingJournal.reconcile_option_collateral(conn)})
        checks.append({"ledger_scope": "option_exercises",
                       **AccountingJournal.reconcile_option_exercises(conn)})
        checks.append({"ledger_scope": "option_premiums",
                       **AccountingJournal.reconcile_option_premiums(conn)})
        checks.append({"ledger_scope":"treasury_bond_cash",
                       **AccountingJournal.reconcile_treasury_bond_cash_events(conn)})
        native = int(conn.execute("""SELECT
            (SELECT COUNT(*) FROM users WHERE cash_minor<>0) +
            (SELECT COUNT(*) FROM accounts WHERE user_id IS NOT NULL AND balance_minor_units<>0)""").fetchone()[0])
        common = int(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='customer'").fetchone()[0])
        wallet = int(conn.execute("SELECT COUNT(*) FROM customer_wallet_ledger").fetchone()[0])
        gaps = int(conn.execute("""SELECT COUNT(*) FROM (
            SELECT u.user_id,'cash' source_type,u.user_id source_account,
                   COALESCE(n.currency_code,base_currency_code()) currency_code,u.cash_minor expected
            FROM users u LEFT JOIN country_currencies n ON n.nationality=u.nationality
            UNION ALL
            SELECT a.user_id,'account',a.account_number,a.quote_currency,a.balance_minor_units
            FROM accounts a WHERE a.user_id IS NOT NULL
        ) live LEFT JOIN (
            SELECT user_id,source_type,source_account,currency_code,SUM(delta_minor_units) actual
            FROM customer_wallet_ledger GROUP BY user_id,source_type,source_account,currency_code
        ) ledger USING(user_id,source_type,source_account,currency_code)
        WHERE COALESCE(live.expected,0)<>COALESCE(ledger.actual,0)""").fetchone()[0])
        orphan_query = """SELECT ledger.user_id,ledger.source_type,ledger.source_account,ledger.currency_code,ledger.actual FROM (
            SELECT user_id,source_type,source_account,currency_code,SUM(delta_minor_units) actual
            FROM customer_wallet_ledger GROUP BY user_id,source_type,source_account,currency_code
        ) ledger LEFT JOIN (
            SELECT u.user_id,'cash' source_type,u.user_id source_account,
                   COALESCE(n.currency_code,base_currency_code()) currency_code
            FROM users u LEFT JOIN country_currencies n ON n.nationality=u.nationality
            UNION ALL
            SELECT a.user_id,'account',a.account_number,a.quote_currency
            FROM accounts a WHERE a.user_id IS NOT NULL
        ) live USING(user_id,source_type,source_account,currency_code)
        WHERE live.user_id IS NULL AND ledger.actual<>0"""
        orphan_balance_gaps = int(conn.execute(f'SELECT COUNT(*) FROM ({orphan_query})').fetchone()[0])
        orphan_balances = [dict(row) for row in conn.execute(
            orphan_query + ' ORDER BY ledger.user_id,ledger.source_type,ledger.source_account,ledger.currency_code LIMIT 100'
        ).fetchall()] if orphan_balance_gaps else []
        posting_gaps = int(conn.execute("""SELECT COUNT(*) FROM customer_wallet_ledger w
            LEFT JOIN accounting_entries e ON e.ledger_scope='customer'
              AND e.entity_type='wallet_holder' AND e.entity_id=w.user_id
              AND e.source_key='wallet:'||w.entry_id
            LEFT JOIN (
              SELECT l.entry_id,COUNT(*) line_count,
                SUM(CASE WHEN l.account_code=(SELECT common_code
                    FROM accounting_account_mappings WHERE ledger_scope='customer'
                      AND local_code='customer_funds_payable') THEN l.debit_units ELSE 0 END) payable_debit,
                SUM(CASE WHEN l.account_code=(SELECT common_code
                    FROM accounting_account_mappings WHERE ledger_scope='customer'
                      AND local_code='customer_funds_payable') THEN l.credit_units ELSE 0 END) payable_credit,
                SUM(CASE WHEN l.account_code=(SELECT common_code
                    FROM accounting_account_mappings WHERE ledger_scope='customer'
                      AND local_code='wallet_clearing') THEN l.debit_units ELSE 0 END) clearing_debit,
                SUM(CASE WHEN l.account_code=(SELECT common_code
                    FROM accounting_account_mappings WHERE ledger_scope='customer'
                      AND local_code='wallet_clearing') THEN l.credit_units ELSE 0 END) clearing_credit
              FROM accounting_lines l GROUP BY l.entry_id
            ) lines ON lines.entry_id=e.entry_id
            WHERE e.entry_id IS NULL OR e.currency_code<>w.currency_code
              OR e.decimal_places<>w.decimal_places OR e.recorded_epoch<>w.recorded_epoch
              OR COALESCE(lines.line_count,0)<>2
              OR COALESCE(lines.payable_debit,0)<>CASE WHEN w.delta_minor_units<0
                    THEN -w.delta_minor_units ELSE 0 END
              OR COALESCE(lines.payable_credit,0)<>CASE WHEN w.delta_minor_units>0
                    THEN w.delta_minor_units ELSE 0 END
              OR COALESCE(lines.clearing_debit,0)<>CASE WHEN w.delta_minor_units>0
                    THEN w.delta_minor_units ELSE 0 END
              OR COALESCE(lines.clearing_credit,0)<>CASE WHEN w.delta_minor_units<0
                    THEN -w.delta_minor_units ELSE 0 END""").fetchone()[0])
        legacy_closure_gaps = int(conn.execute("""SELECT COUNT(*) FROM legacy_wallet_closure_adjustments a
            LEFT JOIN customer_wallet_ledger w ON w.entry_id=a.wallet_entry_id
            LEFT JOIN bank_journal_entries b ON b.entry_id=a.bank_entry_id
            LEFT JOIN (SELECT entry_id,SUM(debit_units) debit FROM bank_journal_lines
                       WHERE account_code='customer_deposits_payable' GROUP BY entry_id) lines ON lines.entry_id=b.entry_id
            WHERE w.entry_id IS NULL OR b.entry_id IS NULL OR w.user_id<>a.user_id
              OR w.source_type<>'account' OR w.source_account<>a.account_number
              OR w.source_key<>'legacy-close-adjustment:'||a.account_number
              OR w.currency_code<>a.currency_code OR w.decimal_places<>a.decimal_places
              OR w.delta_minor_units<>-a.amount_minor_units OR w.balance_minor_units<>0
              OR w.recorded_epoch<>a.recorded_epoch OR a.actor_id=''
              OR b.event_type<>'deposit_close' OR b.status<>'posted'
              OR b.reference_key<>'account:'||a.account_number OR b.currency_code<>a.currency_code
              OR b.decimal_places<>a.decimal_places OR COALESCE(lines.debit,0)<>a.amount_minor_units""").fetchone()[0])
        status = ("matched" if wallet == common and gaps == 0 and orphan_balance_gaps == 0 and posting_gaps == 0
                  and legacy_closure_gaps == 0
                  else "mismatch")
        baseline_adjustments = int(conn.execute(
            "SELECT COUNT(*) FROM customer_wallet_baseline_adjustments").fetchone()[0])
        checks.append({"ledger_scope": "customer", "baseline_adjustments": baseline_adjustments,
                       "native_balances": native,
                       "wallet_entries": wallet, "common_entries": common,
                       "balance_gaps": gaps, "posting_gaps": posting_gaps,
                       "orphan_balance_gaps": orphan_balance_gaps,
                       "orphan_balances": orphan_balances,
                       "legacy_closure_gaps": legacy_closure_gaps,
                       "missing_entries": wallet-common,
                       "status": status})
        fund_rows = conn.execute("""SELECT entry_id,product_code,event,amount,balance,
            reference,recorded_epoch,currency_code,decimal_places,amount_minor_units,
            balance_minor_units FROM fund_cash_ledger ORDER BY entry_id""").fetchall()
        fund_posting_gaps = []
        seen_fund_entries = set()
        for row in fund_rows:
            entity_id = str(row["product_code"])
            source_key = f"fund-cash:{int(row['entry_id'])}"
            common_entry = conn.execute("""SELECT entry_id,currency_code,decimal_places,
                recorded_epoch FROM accounting_entries WHERE ledger_scope='fund'
                AND entity_type='fund_cash' AND entity_id=? AND source_key=?""",
                (entity_id, source_key)).fetchone()
            if (not common_entry or common_entry["currency_code"] != row["currency_code"]
                    or int(common_entry["decimal_places"]) != int(row["decimal_places"])
                    or int(common_entry["recorded_epoch"]) != int(row["recorded_epoch"])):
                fund_posting_gaps.append({"entry_id": int(row["entry_id"]),
                                          "status": "missing_or_mismatched_posting"})
                continue
            common_id = int(common_entry["entry_id"])
            seen_fund_entries.add(common_id)
            amount = int(row["amount_minor_units"] or 0)
            expected = {
                "fund_cash": (max(amount, 0), max(-amount, 0)),
                "fund_cash_clearing": (max(-amount, 0), max(amount, 0)),
            }
            actual = {str(line["account_code"]):
                      (int(line["debit_units"]), int(line["credit_units"]))
                      for line in conn.execute("""SELECT account_code,
                          SUM(debit_units) debit_units,SUM(credit_units) credit_units
                          FROM accounting_lines WHERE entry_id=? GROUP BY account_code""",
                          (common_id,)).fetchall()}
            if actual != expected:
                fund_posting_gaps.append({"entry_id": int(row["entry_id"]),
                                          "status": "line_mismatch"})
        common_fund_entries = int(conn.execute(
            "SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='fund'"
        ).fetchone()[0])
        if common_fund_entries != len(seen_fund_entries):
            fund_posting_gaps.append({"status": "orphan_common_entry",
                                      "common_entries": common_fund_entries,
                                      "native_entries": len(fund_rows)})
        fund_balance_gaps = []
        for balance in conn.execute("""SELECT product_code,currency_code,decimal_places,
            balance_minor_units FROM fund_cash_currency_balances
            ORDER BY product_code,currency_code""").fetchall():
            ledger_balance = int(conn.execute("""SELECT COALESCE(SUM(amount_minor_units),0)
                FROM fund_cash_ledger WHERE product_code=? AND currency_code=?""",
                (balance["product_code"], balance["currency_code"])).fetchone()[0])
            if int(balance["balance_minor_units"]) != ledger_balance:
                fund_balance_gaps.append({"product_code": balance["product_code"],
                    "currency_code": balance["currency_code"],
                    "native_balance": int(balance["balance_minor_units"]),
                    "ledger_balance": ledger_balance})
        for account_row in conn.execute("""SELECT a.product_code,a.cash,i.quote_currency
            FROM fund_accounts a JOIN investment i USING(product_code)""").fetchall():
            native = conn.execute("""SELECT balance_minor_units FROM fund_cash_currency_balances
                WHERE product_code=? AND currency_code=?""",
                (account_row["product_code"], account_row["quote_currency"])).fetchone()
            if not native or int(native[0]) != int(account_row["cash"]):
                fund_balance_gaps.append({"product_code": account_row["product_code"],
                    "currency_code": str(account_row["quote_currency"]),
                    "native_balance": int(account_row["cash"]),
                    "bucket_balance": int(native[0]) if native else None})
        checks.append({"ledger_scope": "fund", "native_entries": len(fund_rows),
                       "common_entries": common_fund_entries,
                       "balance_gaps": fund_balance_gaps,
                       "posting_gaps": fund_posting_gaps,
                       "status": "matched" if not fund_balance_gaps and not fund_posting_gaps
                       else "mismatch"})
        from functions.money import currency_precision, to_minor_units
        tax_gaps = []
        tax_wallet_gaps = []
        baseline = conn.execute("SELECT through_transaction_id FROM "
                                 "tax_transaction_wallet_baseline_state WHERE singleton_id=1").fetchone()
        legacy_tax_through = int(baseline[0]) if baseline else 0
        bank_baseline = conn.execute("SELECT through_transaction_id FROM tax_bank_remittance_activation WHERE singleton_id=1").fetchone()
        legacy_bank_through = int(bank_baseline[0]) if bank_baseline else 0
        quote_baseline = conn.execute("SELECT through_transaction_id FROM tax_bank_remittance_quote_activation WHERE singleton_id=1").fetchone()
        legacy_quote_through = int(quote_baseline[0]) if quote_baseline else 0
        tax_rows = conn.execute("SELECT * FROM tax_transactions ORDER BY transaction_id").fetchall()
        for row in tax_rows:
            currency = str(row["currency_code"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            gross = to_minor_units(row["gross_amount"], places)
            tax = to_minor_units(row["tax_amount"], places)
            net = to_minor_units(row["net_amount"], places)
            if (int(row["gross_amount_minor_units"] or 0) != gross
                    or int(row["tax_amount_minor_units"] or 0) != tax
                    or int(row["net_amount_minor_units"] or 0) != net
                    or gross != tax + net):
                tax_gaps.append({"transaction_id": int(row["transaction_id"]),
                    "currency_code": currency, "gross_minor_units": gross,
                    "tax_minor_units": tax, "net_minor_units": net,
                    "status": "mismatch"})
            transaction_id = int(row["transaction_id"])
            if tax > 0 and transaction_id > legacy_tax_through:
                links = conn.execute("""SELECT l.treasury_user_id,w.user_id,w.currency_code,w.decimal_places,
                    w.delta_minor_units,w.entry_id FROM tax_transaction_wallet_links l
                    JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
                    WHERE l.transaction_id=? ORDER BY w.entry_id""",
                    (transaction_id,)).fetchall()
                quote = (conn.execute("SELECT * FROM tax_bank_remittance_quotes WHERE transaction_id=?", (transaction_id,)).fetchone()
                         if row['source'] == 'saving_interest' and transaction_id > legacy_quote_through else None)
                zero_settlement = False
                if quote and not links:
                    try:
                        numerator, denominator = int(quote['rate_numerator']), int(quote['rate_denominator'])
                        source_places, target_places = int(quote['source_places']), int(quote['settlement_places'])
                        zero_settlement = (numerator > 0 and denominator > 0 and 0 <= source_places <= 6 and 0 <= target_places <= 6
                            and quote['source_currency'] == currency and source_places == places
                            and quote['settlement_currency'] != currency
                            and tax * numerator * 10**target_places // (denominator * 10**source_places) == 0)
                    except (ValueError, TypeError, OverflowError):
                        pass
                if not links and not zero_settlement:
                    tax_wallet_gaps.append({"transaction_id": transaction_id,
                        "tax_minor_units": tax, "status": "missing_treasury_receipt"})
                    continue
                wrong = [link for link in links if
                         str(link["treasury_user_id"]) != str(link["user_id"])
                         or not (str(link["user_id"]) == "20230216" or conn.execute(
                             "SELECT 1 FROM country_treasuries WHERE user_id=?",
                             (str(link["user_id"]),)).fetchone())
                         or int(link["delta_minor_units"]) <= 0]
                if wrong:
                    tax_wallet_gaps.append({"transaction_id": transaction_id,
                        "wallet_entry_ids": [int(link["entry_id"]) for link in wrong],
                        "status": "invalid_treasury_receipt"})
                same_currency = [link for link in links
                                 if str(link["currency_code"]).upper() == currency]
                wrong_precision = [link for link in same_currency if int(link["decimal_places"]) != places]
                if wrong_precision:
                    tax_wallet_gaps.append({"transaction_id": transaction_id,
                        "wallet_entry_ids": [int(link["entry_id"]) for link in wrong_precision],
                        "status": "invalid_treasury_receipt_precision"})
                if same_currency and sum(int(link["delta_minor_units"])
                                         for link in same_currency) != tax:
                    tax_wallet_gaps.append({"transaction_id": transaction_id,
                        "tax_minor_units": tax,
                        "received_minor_units": sum(int(link["delta_minor_units"])
                                                     for link in same_currency),
                        "status": "same_currency_receipt_amount_mismatch"})
                if row['source'] == 'saving_interest' and transaction_id > legacy_bank_through:
                    bank_link = conn.execute("SELECT source_entry_id,settlement_entry_id FROM tax_bank_remittance_links WHERE transaction_id=?",
                                             (transaction_id,)).fetchone()
                    valid = bank_link is not None
                    if bank_link:
                        source = conn.execute("SELECT * FROM bank_journal_entries WHERE entry_id=?", (bank_link['source_entry_id'],)).fetchone()
                        settlement = (conn.execute("SELECT * FROM bank_journal_entries WHERE entry_id=?", (bank_link['settlement_entry_id'],)).fetchone()
                                      if bank_link['settlement_entry_id'] is not None else None)
                        cross = (quote['source_currency'] != quote['settlement_currency'] if quote
                                 else bank_link['settlement_entry_id'] is not None)
                        valid = bool(source and source['event_type'] == 'tax_remittance' and source['status'] == 'posted'
                                     and source['currency_code'] == currency and source['decimal_places'] == places)
                        if source:
                            source_lines = conn.execute("SELECT account_code,debit_units,credit_units FROM bank_journal_lines WHERE entry_id=?", (source['entry_id'],)).fetchall()
                            expected = {'tax_payable': (tax, 0), ('fx_conversion_clearing' if cross else 'settlement_asset'): (0, tax)}
                            valid = valid and len(source_lines) == 2 and {line[0]:(line[1],line[2]) for line in source_lines} == expected
                        if cross:
                            valid = bool(valid and source['reference_key'].endswith(':source'))
                            if zero_settlement:
                                valid = bool(valid and settlement is None)
                            else:
                                valid = bool(valid and settlement and settlement['event_type'] == 'tax_remittance'
                                             and settlement['status'] == 'posted' and settlement['issuer'] == source['issuer']
                                             and settlement['reference_key'] == source['reference_key'][:-7] + ':settlement')
                            if settlement:
                                target_links = [link for link in links if link['currency_code'] == settlement['currency_code']
                                                and link['decimal_places'] == settlement['decimal_places']]
                                received = sum(int(link['delta_minor_units']) for link in target_links)
                                target_lines = conn.execute("SELECT account_code,debit_units,credit_units FROM bank_journal_lines WHERE entry_id=?", (settlement['entry_id'],)).fetchall()
                                valid = bool(valid and len(target_links) == len(links) and received > 0 and len(target_lines) == 2
                                             and {line[0]:(line[1],line[2]) for line in target_lines} ==
                                             {'fx_conversion_clearing': (received, 0), 'settlement_asset': (0, received)})
                        else:
                            valid = bool(valid and len(same_currency) == len(links))
                    if not valid:
                        tax_wallet_gaps.append({'transaction_id': transaction_id, 'status': 'bank_remittance_mismatch'})
                    if transaction_id > legacy_quote_through:
                        valid_quote = False
                        if quote:
                            try:
                                numerator, denominator = int(quote['rate_numerator']), int(quote['rate_denominator'])
                                source_places, target_places = int(quote['source_places']), int(quote['settlement_places'])
                                if (numerator > 0 and denominator > 0 and 0 <= source_places <= 6 and 0 <= target_places <= 6
                                    and quote['source_currency'] == currency and source_places == places):
                                    expected_received = tax * numerator * 10**target_places // (denominator * 10**source_places)
                                    target_links = [link for link in links if link['currency_code'] == quote['settlement_currency']
                                                    and link['decimal_places'] == target_places]
                                    valid_quote = len(target_links) == len(links) and sum(int(link['delta_minor_units']) for link in target_links) == expected_received
                            except (ValueError, TypeError, OverflowError):
                                pass
                        if not valid_quote:
                            tax_wallet_gaps.append({'transaction_id': transaction_id, 'status': 'bank_remittance_fx_mismatch'})
        checks.append({"ledger_scope": "tax_transactions",
                       "transaction_count": len(tax_rows), "gaps": tax_gaps,
                       "wallet_gaps": tax_wallet_gaps,
                       "legacy_wallet_baseline_through": legacy_tax_through,
                       "legacy_bank_remittance_baseline_through": legacy_bank_through,
                       "legacy_bank_remittance_quote_baseline_through": legacy_quote_through,
                       "status": "matched" if not tax_gaps and not tax_wallet_gaps else "mismatch"})
        checks.append({"ledger_scope": "dividend_payment_settlements",
                       **AccountingJournal.reconcile_dividend_payment_settlements(conn)})
        return checks

    # Where every registered amount table stands against the common ledger.
    # "connected": the named checks reconcile its money to ledger or wallet rows.
    # "reference": prices, terms, limits and model inputs; nothing is paid from them.
    # "unconnected": money moves, but only as a wallet balance change — no event
    # link or business journal entry ties the amount to its cause yet.
    CONNECTED_AMOUNT_TABLES = {
        "users": ("customer",), "accounts": ("customer", "customer_deposits"),
        "fund_accounts": ("fund",), "fund_cash_ledger": ("fund",),
        "tax_transactions": ("tax_transactions",),
        "dividend_distributions": ("dividend_payment_settlements",),
        "dividend_payments": ("dividend_payment_settlements",),
        "dr_dividend_withholding_events": ("dividend_payment_settlements",),
        "currency_transactions": ("currency_cash",),
        "currency_transfer_transactions": ("currency_cash",),
        "market_orders": ("market_order_reservations", "market_order_wallet_cash"),
        "option_collateral_cash_lots": ("option_collateral_cash",),
        "option_collateral_lots": ("option_collateral",),
        "option_contracts": ("option_exercises", "option_premiums"),
        "option_positions": ("option_collateral",),
        "treasury_bond_holdings": ("treasury_bond_cash",),
        "treasury_bond_products": ("treasury_bond_cash",),
        "company": ("company",), "investment": ("company_bond_liabilities",),
        "financial_institution_ledger": ("institution_pnl",),
        "financial_institutions": ("institution",),
        "loans": ("loan_receivables", "ecl_allowances", "charged_off_memo_balances"),
        "loan_payment_schedule": ("loan_payment_schedules",),
        "loan_ecl_assessments": ("ecl_allowances",),
        "loan_collateral_assets": ("collateral_liquidations",),
        "collateral_liquidation_events": ("collateral_liquidations",),
        "deposit_insurance_claims": ("deposit_insurance_claims",),
        "lottery_rounds": ("lottery",), "lottery_pool_ledger": ("lottery",),
        "lottery_tickets": ("lottery",), "lottery_purchases": ("lottery",),
        "lottery_payouts": ("lottery",),
        "issuer_cash_accounts": ("issuer_cash",), "issuer_cash_transfers": ("issuer_cash",),
        "corporate_liquidation_payments": ("business_cash",),
        "els_settlements": ("business_cash",), "els_subscriptions": ("business_cash",),
        "futures_positions": ("futures_cash",), "swap_contracts": ("swap_cash",),
        "hybrid_bond_events": ("business_cash",),
        "els_products": ("els_locked",),
        "product_settlements": ("settlement_records",), "preferred_dividend_claims": ("settlement_records",),
        "company_income_log": ("settlement_records",),
    }
    REFERENCE_AMOUNT_TABLES = frozenset((
        "aml_alerts", "aml_monitoring_rules", "bank_deficit_alerts", "corporate_equity_offerings",
        "depositary_receipt_movements", "dr_custody_instructions", "dr_deviation_alert_events",
        "dr_deviation_samples", "els_underlyings", "fund_components", "leveraged_etf_state",
        "liability", "saving", "market_assets", "market_auto_target_history", "market_auto_targets",
        "market_event_targets", "market_exchange_index_components", "market_future_expiry_prices",
        "market_last_trade", "market_price_history", "market_quote_overrides",
        "market_simulation_state", "market_trade_history", "user_assets",
    ))
    # Nothing is left here; a new money table that is not yet reconciled belongs in this dict.
    UNCONNECTED_AMOUNT_TABLES = {}

    @staticmethod
    def cash_mark(conn):
        """Remember where the wallet and fund-cash ledgers end before a business record moves cash."""
        return (int(conn.execute("SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger").fetchone()[0]),
                int(conn.execute("SELECT COALESCE(MAX(entry_id),0) FROM fund_cash_ledger").fetchone()[0]))

    @staticmethod
    def link_cash(conn, kind, reference, mark, now):
        """Tie every wallet and fund-cash row made since ``mark`` to one business record.

        Rows another record already claimed are left alone; returns how many rows were linked.
        """
        event_id = conn.execute(
            "INSERT INTO business_cash_events(kind,reference,recorded_epoch) VALUES (?,?,?)",
            (str(kind), str(reference), int(now))).lastrowid
        wallet = conn.execute("""INSERT INTO business_cash_links(wallet_entry_id,event_id)
            SELECT w.entry_id,? FROM customer_wallet_ledger w WHERE w.entry_id>?
              AND NOT EXISTS (SELECT 1 FROM business_cash_links l WHERE l.wallet_entry_id=w.entry_id)""",
            (event_id, int(mark[0])))
        fund = conn.execute("""INSERT INTO business_cash_fund_links(fund_entry_id,event_id)
            SELECT f.entry_id,? FROM fund_cash_ledger f WHERE f.entry_id>?
              AND NOT EXISTS (SELECT 1 FROM business_cash_fund_links l WHERE l.fund_entry_id=f.entry_id)""",
            (event_id, int(mark[1])))
        return int(wallet.rowcount) + int(fund.rowcount)

    @staticmethod
    def _futures_held(conn):
        """Cash sitting inside the futures product: position collateral plus open-order margin."""
        from decimal import Decimal
        rows = (list(conn.execute("SELECT collateral FROM futures_positions"))
                + list(conn.execute("SELECT reserved_cash FROM market_orders WHERE order_kind='future' "
                                    "AND status IN ('open','partial')")))
        held = sum((Decimal(str(row[0] or 0)) for row in rows), Decimal(0))
        # Amounts waiting for their receiver to choose, or in a temporary account, are still the product's cash.
        for minor, places in conn.execute("SELECT minor_units,decimal_places FROM payout_requests "
                                          "WHERE kind='futures' AND status IN ('pending','temp_open')"):
            held += Decimal(int(minor)) / (10 ** int(places))
        return held

    @classmethod
    def futures_cash_mark(cls, conn, now):
        """``cash_mark`` for a futures movement; the first one also fixes what was already held."""
        conn.execute("INSERT OR IGNORE INTO business_cash_baseline_values(kind,amount,recorded_epoch) "
                     "VALUES ('futures',?,?)", (str(cls._futures_held(conn)), int(now)))
        return cls.cash_mark(conn)

    @classmethod
    def link_futures_cash(cls, conn, tag, mark, now):
        import uuid
        return cls.link_cash(conn, "futures", f"{tag}:{uuid.uuid4().hex}", mark, now)

    @staticmethod
    def _swap_held(conn):
        """Margin sitting inside open swaps, per currency (native units)."""
        from decimal import Decimal
        held = {}
        for row in conn.execute("SELECT currency,margin FROM swap_contracts WHERE margin<>0"):
            held[str(row[0])] = held.get(str(row[0]), Decimal(0)) + Decimal(str(row[1] or 0))
        return held

    @classmethod
    def swap_cash_mark(cls, conn, currency, now):
        """``cash_mark`` for a swap movement; the first in a currency also fixes the margin already held."""
        currency = str(currency).upper()
        conn.execute("INSERT OR IGNORE INTO business_cash_baseline_values(kind,amount,recorded_epoch) "
                     "VALUES (?,?,?)", (f"swap:{currency}", str(cls._swap_held(conn).get(currency, 0)), int(now)))
        return cls.cash_mark(conn)

    @classmethod
    def link_swap_cash(cls, conn, tag, mark, now):
        import uuid
        return cls.link_cash(conn, "swap", f"{tag}:{uuid.uuid4().hex}", mark, now)

    @classmethod
    def reconcile_swap_cash(cls, conn):
        """Per currency, the margin swaps hold since linking began equals the linked wallet outflow.

        Margin, net interest against the citizen's treasury, early-termination payments and the
        margin returned are all wallet rows, so what a swap holds beyond its baseline is exactly
        the negative of their sum.
        """
        from decimal import Decimal
        baselines = {str(row[0])[len("swap:"):]: Decimal(str(row[1])) for row in conn.execute(
            "SELECT kind,amount FROM business_cash_baseline_values WHERE kind LIKE 'swap:%'")}
        if not baselines:
            return {"active": False, "event_count": 0, "held": 0, "gaps": [], "status": "matched"}
        held = cls._swap_held(conn)
        linked = {}
        for row in conn.execute("""SELECT w.currency_code,w.decimal_places,w.delta_minor_units
            FROM business_cash_links l JOIN business_cash_events e ON e.event_id=l.event_id
            JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id WHERE e.kind='swap'"""):
            linked[str(row[0])] = linked.get(str(row[0]), Decimal(0)) + Decimal(int(row[2])) / (10 ** int(row[1]))
        gaps = []
        for currency, baseline in sorted(baselines.items()):
            difference = held.get(currency, Decimal(0)) - baseline + linked.get(currency, Decimal(0))
            if difference != 0:
                gaps.append({"currency_code": currency, "held": str(held.get(currency, 0)),
                             "baseline": str(baseline), "linked_wallet_cash": str(linked.get(currency, 0)),
                             "difference": str(difference), "status": "swap_cash_mismatch"})
        events = int(conn.execute("SELECT COUNT(*) FROM business_cash_events WHERE kind='swap'").fetchone()[0])
        return {"active": True, "event_count": events, "held": int(sum(held.values(), Decimal(0))),
                "gaps": gaps, "status": "matched" if not gaps else "mismatch"}

    @classmethod
    def reconcile_futures_cash(cls, conn):
        """Cash held by futures since linking began must equal the linked wallet outflow.

        Margin, daily marks against the listing treasury, refunds, tax and
        payouts are all wallet rows, so whatever the product holds beyond its
        baseline is exactly the negative of their sum.
        """
        from decimal import Decimal
        baseline = conn.execute(
            "SELECT amount FROM business_cash_baseline_values WHERE kind='futures'").fetchone()
        if not baseline:
            return {"active": False, "event_count": 0, "held": 0, "gaps": [], "status": "matched"}
        from functions.money import base_currency_code
        held = cls._futures_held(conn)
        linked = Decimal(0)
        # Futures are margined in the base currency; a treasury credited in another currency for
        # tax exchanged out of the product is not part of the cash it holds.
        for row in conn.execute("""SELECT w.decimal_places,w.delta_minor_units FROM business_cash_links l
            JOIN business_cash_events e ON e.event_id=l.event_id
            JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
            WHERE e.kind='futures' AND w.currency_code=?""", (base_currency_code(conn),)):
            linked += Decimal(int(row[1])) / (10 ** int(row[0]))
        exchanged_tax = Decimal(0)
        for row in conn.execute("""SELECT source_decimal_places,source_minor_units FROM cash_fx_settlements
            WHERE kind IN ('tax:futures_realized_gain','payout:futures') AND source_currency=?
              AND recorded_epoch>=?""",
            (base_currency_code(conn), int(conn.execute(
                "SELECT recorded_epoch FROM business_cash_baseline_values WHERE kind='futures'").fetchone()[0]))):
            exchanged_tax += Decimal(int(row[1])) / (10 ** int(row[0]))
        linked += exchanged_tax          # left the product exchanged (tax, payouts), not as wallet cash
        for row in conn.execute("""SELECT f.amount FROM business_cash_fund_links l
            JOIN business_cash_events e ON e.event_id=l.event_id
            JOIN fund_cash_ledger f ON f.entry_id=l.fund_entry_id WHERE e.kind='futures'"""):
            linked += Decimal(str(row[0]))
        difference = held - Decimal(str(baseline[0])) + linked
        gaps = ([] if difference == 0 else
                [{"held": str(held), "baseline": str(baseline[0]), "linked_wallet_cash": str(linked),
                  "difference": str(difference), "status": "futures_cash_mismatch"}])
        events = int(conn.execute("SELECT COUNT(*) FROM business_cash_events WHERE kind='futures'").fetchone()[0])
        return {"active": True, "event_count": events, "held": int(held), "gaps": gaps,
                "status": "matched" if not gaps else "mismatch"}

    # kind -> (table, key column, recipient column, SQL for the recipient's expected cash in major units)
    CASH_LINKED_RECORDS = {
        "liquidation": ("corporate_liquidation_payments", "payment_id", "user_id", "paid_amount-tax_amount"),
        "els_settlement": ("els_settlements", "settlement_id", "user_id", "net_amount"),
        "els_subscription": ("els_subscriptions", "subscription_id", "user_id", "-principal"),
        # A conversion pays the holder the fractional-share residual; a warrant exercise takes its cost.
        "hybrid_bond": ("hybrid_bond_events", "event_id", "user_id",
                        "CASE WHEN kind='warrant' THEN -cash ELSE cash END"),
    }

    @classmethod
    def reconcile_business_cash(cls, conn):
        """Every record of a linked kind must own wallet rows that add up to what it paid or took.

        Records older than the kind's first linked one are its baseline. A
        payment exchanged into another currency is compared with its recorded
        exchange instead of the source amount.
        """
        from decimal import Decimal
        from database.fund_accounting import PREFIX as FUND_PREFIX

        gaps, baseline, checked = [], {}, 0
        events = {}
        for row in conn.execute("SELECT event_id,kind,reference FROM business_cash_events"):
            events.setdefault(str(row["kind"]), {})[str(row["reference"])] = int(row["event_id"])
        received = {}
        for row in conn.execute("""SELECT l.event_id,w.user_id,w.currency_code,w.decimal_places,w.delta_minor_units
            FROM business_cash_links l JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id"""):
            key = (int(row["event_id"]), str(row["user_id"]), str(row["currency_code"]))
            received[key] = received.get(key, Decimal(0)) + (
                Decimal(int(row["delta_minor_units"])) / (10 ** int(row["decimal_places"])))
        fund_received = {}
        for row in conn.execute("""SELECT l.event_id,f.product_code,f.amount
            FROM business_cash_fund_links l JOIN fund_cash_ledger f ON f.entry_id=l.fund_entry_id"""):
            key = (int(row["event_id"]), FUND_PREFIX + str(row["product_code"]))
            fund_received[key] = fund_received.get(key, Decimal(0)) + Decimal(str(row["amount"]))
        exchanged = {(str(row["kind"]), str(row["reference"])): (
            str(row["currency"]), Decimal(int(row["minor_units"])) / (10 ** int(row["decimal_places"])))
            for row in conn.execute("SELECT kind,reference,currency,decimal_places,minor_units FROM cash_fx_settlements")}
        orphan_links = int(conn.execute("""SELECT
            (SELECT COUNT(*) FROM business_cash_links l LEFT JOIN customer_wallet_ledger w
             ON w.entry_id=l.wallet_entry_id WHERE w.entry_id IS NULL)+
            (SELECT COUNT(*) FROM business_cash_links l LEFT JOIN business_cash_events e
             ON e.event_id=l.event_id WHERE e.event_id IS NULL)""").fetchone()[0])
        if orphan_links:
            gaps.append({"orphan_links": orphan_links, "status": "orphan_cash_link"})
        for kind, (table, key, recipient, expected_sql) in cls.CASH_LINKED_RECORDS.items():
            linked = events.get(kind, {})
            first = min((int(reference) for reference in linked), default=None)
            records = {}
            for row in conn.execute(f"SELECT {key} AS record_id,{recipient} AS recipient,"
                                    f"{expected_sql} AS expected FROM {table}"):
                record_id = int(row["record_id"])
                if first is None or record_id < first:
                    baseline[kind] = baseline.get(kind, 0) + 1
                    continue
                records[str(record_id)] = row
            for reference in linked:
                if reference not in records:
                    gaps.append({"kind": kind, "reference": reference, "status": "orphan_cash_event"})
            for reference, row in records.items():
                event_id = linked.get(reference)
                expected = Decimal(str(row["expected"] or 0))
                if event_id is None:
                    if expected:
                        gaps.append({"kind": kind, "reference": reference, "status": "missing_cash_event"})
                    continue
                checked += 1
                user = str(row["recipient"])
                swapped = exchanged.get((kind, reference))
                if swapped:
                    actual = received.get((event_id, user, swapped[0]), Decimal(0))
                    expected = swapped[1]
                else:
                    actual = (sum((amount for (event, holder, _), amount in received.items()
                                   if event == event_id and holder == user), Decimal(0))
                              + fund_received.get((event_id, user), Decimal(0)))
                if actual != expected and not conn.execute(
                        "SELECT 1 FROM country_treasuries WHERE user_id=?", (user,)).fetchone():
                    gaps.append({"kind": kind, "reference": reference, "expected": str(expected),
                                 "actual": str(actual), "status": "cash_amount_mismatch"})
        return {"event_count": sum(len(group) for group in events.values()), "checked_records": checked,
                "baseline_records": baseline, "gaps": gaps,
                "status": "matched" if not gaps else "mismatch"}

    @staticmethod
    def reconcile_settlement_records(conn):
        """Compare the logs that restate payments with the records those payments left.

        ``product_settlements`` is a per-user statement: its ELS and liquidation
        rows must each have the matching payment record. Preferred arrears claims
        must be paid by exactly their arrears settlements plus liquidation arrears.
        A company income row posted on its own must equal its company entry; rows
        booked in one aggregate entry or still pending are only counted.
        """
        from collections import Counter
        from decimal import Decimal
        from functions.money import currency_precision

        def amount(value):
            return Decimal(str(value or 0))

        gaps = []
        payments = Counter()
        for row in conn.execute("SELECT user_id,product_code,gross_amount,tax_amount,recorded_epoch FROM els_settlements"):
            payments[("els", str(row[0]), str(row[1]), amount(row[2]), amount(row[3]), int(row[4]))] += 1
        liquidation_arrears = {}
        for row in conn.execute("SELECT user_id,asset_code,kind,paid_amount,tax_amount,recorded_epoch "
                                "FROM corporate_liquidation_payments"):
            payments[(f"liquidation:{row[2]}", str(row[0]), str(row[1]), amount(row[3]), amount(row[4]), int(row[5]))] += 1
            if row[2] == "arrears":
                key = (str(row[0]), str(row[1]))
                liquidation_arrears[key] = liquidation_arrears.get(key, Decimal(0)) + amount(row[3])
        mirrored = unverified = 0
        arrears_paid = {}
        for row in conn.execute("SELECT settlement_id,user_id,product_code,kind,gross_amount,tax_amount,"
                                "recorded_epoch FROM product_settlements ORDER BY settlement_id"):
            kind, gross, tax = str(row["kind"]), amount(row["gross_amount"]), amount(row["tax_amount"])
            if abs(tax) > abs(gross):
                gaps.append({"settlement_id": int(row["settlement_id"]), "status": "settlement_tax_exceeds_gross"})
            if kind.startswith("preferred_arrears:"):
                claim_id = kind.split(":")[1]
                arrears_paid[claim_id] = arrears_paid.get(claim_id, Decimal(0)) + gross
                continue
            source = "els" if kind.startswith("els_") else kind if kind.startswith("liquidation:") else None
            if source is None:
                unverified += 1
                continue
            key = (source, str(row["user_id"]), str(row["product_code"]), gross, tax, int(row["recorded_epoch"]))
            if payments[key] > 0:
                payments[key] -= 1
                mirrored += 1
            else:
                gaps.append({"settlement_id": int(row["settlement_id"]), "kind": kind,
                             "status": "settlement_without_payment"})
        claims = conn.execute("SELECT claim_id,user_id,asset_code,gross_amount,paid_amount,status "
                              "FROM preferred_dividend_claims ORDER BY claim_id").fetchall()
        paid_by_holder, explained_by_holder = {}, {}
        for row in claims:
            gross, paid = amount(row["gross_amount"]), amount(row["paid_amount"])
            holder = (str(row["user_id"]), str(row["asset_code"]))
            paid_by_holder[holder] = paid_by_holder.get(holder, Decimal(0)) + paid
            explained_by_holder[holder] = (explained_by_holder.get(holder, Decimal(0))
                                           + arrears_paid.get(str(row["claim_id"]), Decimal(0)))
            if not 0 <= paid <= gross or (row["status"] == "paid") != (paid == gross and gross > 0) \
                    and row["status"] in ("paid", "partial", "pending", "open"):
                gaps.append({"claim_id": int(row["claim_id"]), "status": "claim_status_mismatch"})
        for holder, paid in paid_by_holder.items():
            if paid != explained_by_holder[holder] + liquidation_arrears.get(holder, Decimal(0)):
                gaps.append({"user_id": holder[0], "asset_code": holder[1], "paid": str(paid),
                             "status": "claim_paid_amount_unexplained"})
        matched = pending = aggregate = 0
        entries = {}
        for row in conn.execute("""SELECT e.source_key,l.account,l.amount FROM financial_entries e
            JOIN financial_lines l ON l.entry_id=e.entry_id
            WHERE e.status='posted' AND e.source_key LIKE 'income-log:%'"""):
            lines = entries.setdefault(str(row[0]), {})
            lines[str(row[1])] = lines.get(str(row[1]), Decimal(0)) + amount(row[2])
        scale = 10 ** currency_precision(conn, base_currency_code(conn))
        for row in conn.execute("SELECT entry_id,kind,gross_nwx,tax_nwx,booked FROM company_income_log"):
            lines = entries.get(f"income-log:{row['entry_id']}")
            if lines is None:
                if int(row["booked"] or 0):
                    aggregate += 1
                else:
                    pending += 1
                continue
            gross, tax = amount(row["gross_nwx"]) * scale, amount(row["tax_nwx"]) * scale
            cash = gross - tax if row["kind"] in ("dividend", "transfer_in", "interest_income") else -gross
            if lines.get("cash", Decimal(0)) != cash or sum(lines.values()) != 0:
                gaps.append({"entry_id": int(row["entry_id"]), "status": "income_entry_mismatch"})
            else:
                matched += 1
        return {"mirrored_settlements": mirrored, "unverified_settlements": unverified,
                "claim_count": len(claims), "income_rows_matched": matched, "income_rows_pending": pending,
                "income_rows_booked_in_aggregate": aggregate, "gaps": gaps,
                "status": "matched" if not gaps else "mismatch"}

    @staticmethod
    def reconcile_els_locked(conn):
        """An ELS product's locked principal and coupon must equal its open subscriptions.

        Subscriptions still ``subscribed`` or ``issued`` are the money the product
        holds; once they are settled or cancelled the product holds nothing.
        """
        from decimal import Decimal

        def amount(value):
            return Decimal(str(value or 0))

        open_totals = {}
        for row in conn.execute("SELECT product_code,principal,coupon_reserved FROM els_subscriptions "
                                "WHERE status IN ('subscribed','issued')"):
            principal, coupon = open_totals.get(str(row[0]), (Decimal(0), Decimal(0)))
            open_totals[str(row[0])] = (principal + amount(row[1]), coupon + amount(row[2]))
        gaps = []
        products = conn.execute(
            "SELECT product_code,principal_amount,coupon_amount,locked_amount FROM els_products").fetchall()
        for row in products:
            principal, coupon = open_totals.get(str(row["product_code"]), (Decimal(0), Decimal(0)))
            if (amount(row["principal_amount"]), amount(row["coupon_amount"]), amount(row["locked_amount"])) \
                    != (principal, coupon, principal + coupon):
                gaps.append({"product_code": str(row["product_code"]), "status": "els_locked_mismatch"})
        return {"product_count": len(products), "gaps": gaps,
                "status": "matched" if not gaps else "mismatch"}

    @staticmethod
    def reconcile_issuer_cash(conn):
        """Issuer cash must equal its company account's wallet, and each logged transfer must add up.

        The balance is mirrored into the company account's cash by trigger, which
        is what carries it into the customer wallet ledger; an issuer without a
        company account has no wallet side and is only counted.
        """
        from decimal import Decimal

        def amount(value):
            return Decimal(str(value or 0))

        gaps, unmirrored = [], 0
        accounts = conn.execute("""SELECT i.company_code,i.balance,c.user_id,u.cash
            FROM issuer_cash_accounts i LEFT JOIN company_accounts c ON c.company_code=i.company_code
            LEFT JOIN users u ON u.user_id=c.user_id ORDER BY i.company_code""").fetchall()
        for row in accounts:
            if row["user_id"] is None:
                unmirrored += 1
            elif row["cash"] is None or amount(row["balance"]) != amount(row["cash"]):
                gaps.append({"company_code": str(row["company_code"]), "status": "company_account_cash_mismatch"})
        transfers = conn.execute(
            "SELECT transfer_id,old_balance,new_balance,delta FROM issuer_cash_transfers").fetchall()
        for row in transfers:
            if amount(row["old_balance"]) + amount(row["delta"]) != amount(row["new_balance"]):
                gaps.append({"transfer_id": int(row["transfer_id"]), "status": "transfer_arithmetic_mismatch"})
        return {"account_count": len(accounts), "without_company_account": unmirrored,
                "transfer_count": len(transfers), "gaps": gaps,
                "status": "matched" if not gaps else "mismatch"}

    @staticmethod
    def reconcile_lottery(conn):
        """Check each lottery round's pool ledger against its sales, tickets, payouts and exchanges.

        Rounds sold before the pool ledger existed have no allocation rows; they
        are counted as a baseline rather than rebuilt from guesses. This checks
        the lottery's own books — the players' wallet rows are not linked to it.
        """
        import json
        from decimal import Decimal

        def number(value):
            return int(Decimal(str(value or 0)))

        gaps, legacy_rounds = [], 0
        ledger = {}
        for row in conn.execute("SELECT round_id,kind,rank,amount,ticket_id FROM lottery_pool_ledger"):
            ledger.setdefault(int(row["round_id"]), []).append(
                (str(row["kind"]), int(row["rank"] or 0), number(row["amount"]), row["ticket_id"]))
        ticket_sales = {}
        for row in conn.execute("SELECT round_id,price FROM lottery_tickets"):
            ticket_sales[int(row["round_id"])] = ticket_sales.get(int(row["round_id"]), 0) + number(row["price"])
        rounds = conn.execute("SELECT round_id,sales_total,reserve_total,treasury_total,pools_json "
                              "FROM lottery_rounds ORDER BY round_id").fetchall()
        for round_row in rounds:
            round_id, rows = int(round_row["round_id"]), ledger.get(int(round_row["round_id"]), [])
            sales = number(round_row["sales_total"])

            def total(*kinds, rank=None):
                return sum(amount for kind, row_rank, amount, _ in rows
                           if kind in kinds and (rank is None or row_rank == rank))
            if not any(kind in ("sale", "reserve", "treasury") for kind, _, _, _ in rows):
                if sales:
                    legacy_rounds += 1
                continue
            if (total("sale", "reserve", "treasury") != sales
                    or total("reserve") != number(round_row["reserve_total"])
                    or total("treasury") != number(round_row["treasury_total"])):
                gaps.append({"round_id": round_id, "sales_total": sales,
                             "allocated": total("sale", "reserve", "treasury"),
                             "status": "sales_allocation_mismatch"})
            if ticket_sales.get(round_id, 0) != sales:
                gaps.append({"round_id": round_id, "sales_total": sales,
                             "ticket_total": ticket_sales.get(round_id, 0),
                             "status": "ticket_sales_mismatch"})
            for rank, pool in enumerate(json.loads(round_row["pools_json"]), 1):
                if total("carry_in", "sale", "payout", rank=rank) != int(pool):
                    gaps.append({"round_id": round_id, "rank": rank, "pool": int(pool),
                                 "ledger": total("carry_in", "sale", "payout", rank=rank),
                                 "status": "pool_balance_mismatch"})
        payout_entries = {(round_id, ticket_id): amount for round_id, rows in ledger.items()
                          for kind, _, amount, ticket_id in rows if kind == "payout"}
        payouts = conn.execute("SELECT ticket_id,round_id,gross,tax,net FROM lottery_payouts").fetchall()
        payout_net = {}
        for row in payouts:
            gross, tax, net = number(row["gross"]), number(row["tax"]), number(row["net"])
            payout_net[str(row["ticket_id"])] = net
            if gross != tax + net:
                gaps.append({"ticket_id": int(row["ticket_id"]), "status": "payout_amount_mismatch"})
            if payout_entries.get((int(row["round_id"]), row["ticket_id"])) != -gross:
                gaps.append({"ticket_id": int(row["ticket_id"]), "status": "payout_without_pool_entry"})
        purchase_total = {str(row["request_id"]): number(row["quantity"]) * number(row["price"])
                          for row in conn.execute("SELECT request_id,quantity,price FROM lottery_purchases")}
        settlements = conn.execute("SELECT * FROM lottery_fx_settlements").fetchall()
        for row in settlements:
            try:
                numerator, denominator = int(row["rate_numerator"]), int(row["rate_denominator"])
                base, scale = int(row["base_minor_units"]), 10 ** int(row["decimal_places"])
                # A purchase is rounded up to the player's minor unit, a payout down.
                expected_source, expected = ((purchase_total.get(str(row["reference"])),
                                              -(-base * numerator * scale // denominator))
                                             if row["kind"] == "purchase" else
                                             (payout_net.get(str(row["reference"])),
                                              base * numerator * scale // denominator))
                valid = (numerator > 0 and denominator > 0 and expected_source == base
                         and int(row["minor_units"]) == expected)
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                gaps.append({"kind": str(row["kind"]), "reference": str(row["reference"]),
                             "status": "fx_settlement_mismatch"})
        return {"round_count": len(rounds), "legacy_unledgered_rounds": legacy_rounds,
                "payout_count": len(payouts), "fx_settlement_count": len(settlements),
                "gaps": gaps, "status": "matched" if not gaps else "mismatch"}

    @classmethod
    def consumer_coverage(cls, conn):
        """Classify every registered amount table; an unlisted table is a finding.

        PostgreSQL does not carry the amount-field registry table, so there the
        code registry it is generated from (database.money_columns.REGISTERED)
        supplies the tables; a registered table nobody classified is a finding.
        """
        from database import pg_backend
        if pg_backend.enabled():
            from database.money_columns import REGISTERED
            tables = sorted({*REGISTERED, *cls.CONNECTED_AMOUNT_TABLES, *cls.REFERENCE_AMOUNT_TABLES,
                             *cls.UNCONNECTED_AMOUNT_TABLES})
        else:
            tables = sorted({str(row[0]) for row in conn.execute(
                "SELECT DISTINCT source_table FROM financial_amount_field_registry")})
        coverage = {"connected": [], "reference": [], "unconnected": [], "unclassified": []}
        for table in tables:
            if table in cls.CONNECTED_AMOUNT_TABLES:
                coverage["connected"].append({"source_table": table,
                                              "checks": list(cls.CONNECTED_AMOUNT_TABLES[table])})
            elif table in cls.REFERENCE_AMOUNT_TABLES:
                coverage["reference"].append({"source_table": table})
            elif table in cls.UNCONNECTED_AMOUNT_TABLES:
                coverage["unconnected"].append({"source_table": table,
                                                "reason": cls.UNCONNECTED_AMOUNT_TABLES[table]})
            else:
                coverage["unclassified"].append(table)
        return coverage

    @staticmethod
    def consolidated_trial_balance(conn):
        """Trial balance over every common-ledger scope, by currency and scale.

        Unlike ``trial_balance`` this never raises on bad data: a scale that
        disagrees with the currency, mixed scales, unbalanced or lineless
        entries, lines without an entry, unmapped accounts and one source posted
        under two entity types are all returned as findings.
        """
        places_by_currency = {str(row[0]): int(row[1]) for row in conn.execute(
            "SELECT code,decimal_places FROM currencies")}
        findings, totals, accounts = [], [], []
        scales = {}
        for row in conn.execute("""SELECT e.ledger_scope,e.currency_code,e.decimal_places,
                COUNT(DISTINCT e.entry_id),COUNT(DISTINCT e.entity_type||':'||e.entity_id),
                COALESCE(SUM(l.debit_units),0),COALESCE(SUM(l.credit_units),0)
            FROM accounting_entries e LEFT JOIN accounting_lines l ON l.entry_id=e.entry_id
            GROUP BY e.ledger_scope,e.currency_code,e.decimal_places
            ORDER BY e.ledger_scope,e.currency_code,e.decimal_places"""):
            scope, currency, places = str(row[0]), str(row[1]), int(row[2])
            debit, credit = int(row[5]), int(row[6])
            totals.append({"ledger_scope": scope, "currency_code": currency, "decimal_places": places,
                           "entry_count": int(row[3]), "entity_count": int(row[4]),
                           "debit_units": debit, "credit_units": credit,
                           "difference_units": debit - credit})
            scales.setdefault((scope, currency), []).append(places)
            if currency not in places_by_currency:
                findings.append({"ledger_scope": scope, "currency_code": currency,
                                 "decimal_places": places, "status": "unknown_currency"})
            elif places_by_currency[currency] != places:
                findings.append({"ledger_scope": scope, "currency_code": currency,
                                 "decimal_places": places,
                                 "currency_decimal_places": places_by_currency[currency],
                                 "status": "scale_mismatch"})
        for (scope, currency), found in scales.items():
            if len(found) > 1:
                findings.append({"ledger_scope": scope, "currency_code": currency,
                                 "decimal_places": sorted(found), "status": "mixed_scale"})
        for row in conn.execute("""SELECT e.ledger_scope,e.currency_code,e.decimal_places,l.account_code,
                SUM(l.debit_units),SUM(l.credit_units)
            FROM accounting_entries e JOIN accounting_lines l ON l.entry_id=e.entry_id
            GROUP BY e.ledger_scope,e.currency_code,e.decimal_places,l.account_code
            ORDER BY e.ledger_scope,e.currency_code,e.decimal_places,l.account_code"""):
            debit, credit = int(row[4]), int(row[5])
            accounts.append({"ledger_scope": str(row[0]), "currency_code": str(row[1]),
                             "decimal_places": int(row[2]), "account_code": str(row[3]),
                             "debit_units": debit, "credit_units": credit,
                             "balance_units": debit - credit})
        for row in AccountingJournal.audit_unbalanced(conn):
            findings.append({**row, "status": "unbalanced_entry"})
        for row in conn.execute("""SELECT e.entry_id,e.ledger_scope,e.entity_id,e.currency_code,
                COUNT(*),COUNT(DISTINCT l.account_code)
            FROM accounting_entries e JOIN accounting_lines l ON l.entry_id=e.entry_id
            GROUP BY e.entry_id,e.ledger_scope,e.entity_id,e.currency_code
            HAVING COUNT(*)>COUNT(DISTINCT l.account_code) ORDER BY e.entry_id"""):
            # An entry carries each account once; repeats inflate both sides and still balance.
            findings.append({"entry_id": int(row[0]), "ledger_scope": str(row[1]), "entity_id": str(row[2]),
                             "currency_code": str(row[3]), "line_count": int(row[4]),
                             "account_count": int(row[5]), "status": "duplicate_line"})
        orphan_lines = int(conn.execute("""SELECT COUNT(*) FROM accounting_lines l
            LEFT JOIN accounting_entries e ON e.entry_id=l.entry_id
            WHERE e.entry_id IS NULL""").fetchone()[0])
        if orphan_lines:
            findings.append({"line_count": orphan_lines, "status": "orphan_line"})
        for row in conn.execute("""SELECT e.ledger_scope,l.account_code,COUNT(*)
            FROM accounting_entries e JOIN accounting_lines l ON l.entry_id=e.entry_id
            LEFT JOIN accounting_account_mappings m ON m.ledger_scope=e.ledger_scope
                AND m.common_code=l.account_code AND m.active=1
            WHERE m.common_code IS NULL
            GROUP BY e.ledger_scope,l.account_code ORDER BY e.ledger_scope,l.account_code"""):
            findings.append({"ledger_scope": str(row[0]), "account_code": str(row[1]),
                             "line_count": int(row[2]), "status": "unmapped_account"})
        for row in conn.execute("""SELECT ledger_scope,entity_id,source_key,COUNT(*)
            FROM accounting_entries GROUP BY ledger_scope,entity_id,source_key
            HAVING COUNT(*)>1 ORDER BY ledger_scope,entity_id,source_key"""):
            findings.append({"ledger_scope": str(row[0]), "entity_id": str(row[1]),
                             "source_key": str(row[2]), "entry_count": int(row[3]),
                             "status": "duplicate_source_key"})
        return {"totals": totals, "accounts": accounts, "findings": findings,
                "status": "matched" if not findings else "mismatch"}

    @staticmethod
    def rebaseline_customer_wallets(conn, *, actor_id, now, reason, dry_run=False):
        """Record every unexplained gap between a wallet and its ledger as a labelled baseline row.

        No balance is changed and no transaction is invented: one ledger row per
        wallet restates the difference, and ``customer_wallet_baseline_adjustments``
        names it as a baseline with the person and reason. A ledger total left
        behind by a wallet that no longer exists is closed the same way.
        """
        from decimal import Decimal
        from functions.money import currency_precision

        reason = str(reason or "").strip()
        if not reason or not str(actor_id or "").strip():
            raise ValueError("기준점 조정에는 담당자와 사유가 필요합니다.")

        def units(value):
            return int(Decimal(str(value or 0)))

        live = {}
        for row in conn.execute("""SELECT u.user_id,COALESCE(n.currency_code,base_currency_code()),u.cash_minor
            FROM users u LEFT JOIN country_currencies n ON n.nationality=u.nationality"""):
            live[(str(row[0]), "cash", str(row[0]), str(row[1]))] = units(row[2])
        for row in conn.execute("""SELECT user_id,account_number,quote_currency,balance_minor_units
            FROM accounts WHERE user_id IS NOT NULL"""):
            live[(str(row[0]), "account", str(row[1]), str(row[2]))] = units(row[3])
        ledger = {}
        for row in conn.execute("""SELECT user_id,source_type,source_account,currency_code,delta_minor_units
            FROM customer_wallet_ledger"""):
            key = (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            ledger[key] = ledger.get(key, 0) + units(row[4])
        gaps = sorted((key, live.get(key, 0), ledger.get(key, 0)) for key in set(live) | set(ledger)
                      if live.get(key, 0) != ledger.get(key, 0))
        orphans = sum(1 for key, _, _ in gaps if key not in live)
        if dry_run:
            return {"adjustments": len(gaps), "orphans": orphans}
        when = int(now)
        for (user, source_type, account, currency), balance, before in gaps:
            places = currency_precision(conn, currency)
            entry_id = conn.execute("""INSERT INTO customer_wallet_ledger
                (source_key,user_id,source_type,source_account,currency_code,decimal_places,
                 delta_minor_units,balance_minor_units,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?)""",
                (f"baseline:{source_type}:{account}:{currency}:{when}", user, source_type, account,
                 currency, places, balance - before, balance, when)).lastrowid
            conn.execute("""INSERT INTO customer_wallet_baseline_adjustments
                (wallet_entry_id,user_id,source_type,source_account,currency_code,decimal_places,
                 ledger_before_minor_units,balance_minor_units,difference_minor_units,orphan,
                 actor_id,reason,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, user, source_type, account, currency, places, before, balance, balance - before,
                 int((user, source_type, account, currency) not in live), str(actor_id), reason, when))
        return {"adjustments": len(gaps), "orphans": orphans}

    @classmethod
    def repair_duplicated_lines(cls, conn, *, actor_id, now, dry_run=False):
        """Remove lines that are exact repeats of an entry's own lines, keeping one of each.

        Only an entry whose every (account, debit, credit) line is repeated the
        same number of times, and which is a balanced entry with each account
        once after the repeats are dropped, is touched. Anything else, and any
        entry in a closed period, is skipped for a person to judge. This is the
        one sanctioned deletion from the append-only journal and is logged per entry.
        """
        from database import pg_backend
        from database.currency_db import ensure_accounting_line_repairs
        if not pg_backend.enabled():
            ensure_accounting_line_repairs(conn.execute)
        candidates = [int(row[0]) for row in conn.execute("""SELECT entry_id FROM accounting_lines
            GROUP BY entry_id HAVING COUNT(*)>COUNT(DISTINCT account_code) ORDER BY entry_id""")]
        plan, skipped = [], []
        for entry_id in candidates:
            entry = conn.execute("""SELECT ledger_scope,entity_id,currency_code,recorded_epoch
                FROM accounting_entries WHERE entry_id=?""", (entry_id,)).fetchone()
            lines = conn.execute("""SELECT line_id,account_code,debit_units,credit_units
                FROM accounting_lines WHERE entry_id=? ORDER BY line_id""", (entry_id,)).fetchall()
            groups = {}
            for line in lines:
                groups.setdefault((str(line[1]), int(line[2]), int(line[3])), []).append(int(line[0]))
            repeats = {len(ids) for ids in groups.values()}
            closed = entry and conn.execute("""SELECT 1 FROM accounting_period_closings
                WHERE ledger_scope=? AND entity_id=? AND currency_code=?
                  AND ?>=period_start_epoch AND ?<period_end_epoch LIMIT 1""",
                (entry[0], entry[1], entry[2], int(entry[3]), int(entry[3]))).fetchone()
            if (not entry or closed or len(repeats) != 1 or repeats == {1} or len(groups) < 2
                    or len({account for account, _, _ in groups}) != len(groups)
                    or sum(debit for _, debit, _ in groups) != sum(credit for _, _, credit in groups)):
                skipped.append(entry_id)
                continue
            plan.append((entry_id, len(lines), [line_id for ids in groups.values() for line_id in ids[1:]]))
        to_remove = sum(len(extra) for _, _, extra in plan)
        if dry_run:
            return {"repairable_entries": len(plan), "lines_to_remove": to_remove, "skipped_entries": skipped}
        if plan:
            guard = "trg_accounting_lines_delete_immutable"
            conn.execute(f"DROP TRIGGER IF EXISTS {guard}")
            try:
                for entry_id, before, extra in plan:
                    for start in range(0, len(extra), 500):
                        chunk = extra[start:start + 500]
                        conn.execute(f"DELETE FROM accounting_lines WHERE entry_id=? AND line_id IN "
                                     f"({','.join('?' for _ in chunk)})", (entry_id, *chunk))
                    conn.execute("""INSERT INTO accounting_line_repairs
                        (entry_id,lines_before,lines_after,actor_id,recorded_epoch) VALUES (?,?,?,?,?)""",
                        (entry_id, before, before - len(extra), str(actor_id), int(now)))
            finally:
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {guard} BEFORE DELETE ON accounting_lines
                    BEGIN SELECT RAISE(ABORT,'common accounting journal is append-only'); END""")
        return {"repaired_entries": len(plan), "lines_removed": to_remove, "skipped_entries": skipped}

    @staticmethod
    def retire_snapshot(conn, *, source_table, source_key, amount_field, reason, actor_id, now):
        """Stop reporting the snapshot of a deleted row, with the reason and person on record (append-only).

        Refused unless the snapshot exists and its source row really is gone. Returns False if already retired."""
        import re
        from database.currency_db import ensure_snapshot_retirements
        ensure_snapshot_retirements(conn.execute)
        if not str(reason or "").strip() or not str(actor_id or "").strip():
            raise ValueError("스냅샷 해제에는 담당자와 사유가 필요합니다.")
        snap = conn.execute(
            "SELECT source_key_field FROM financial_amount_snapshots WHERE source_table=? AND source_key=? "
            "AND amount_field=?", (str(source_table), str(source_key), str(amount_field))).fetchone()
        if not snap:
            raise ValueError("해제할 금액 스냅샷이 없습니다.")
        table, key_field = str(source_table), str(snap[0])
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in (table, key_field)):
            raise ValueError("스냅샷 식별자가 올바르지 않습니다.")
        if conn.execute(f'SELECT 1 FROM "{table}" WHERE "{key_field}"=?', (str(source_key),)).fetchone():
            raise ValueError("원본 행이 아직 있어 스냅샷을 해제할 수 없습니다.")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO financial_amount_snapshot_retirements"
            "(source_table,source_key,amount_field,reason,actor_id,recorded_epoch) VALUES (?,?,?,?,?,?)",
            (table, str(source_key), str(amount_field), str(reason).strip(), str(actor_id), int(now)))
        return cursor.rowcount == 1

    @classmethod
    def record_primary_recipient_override(cls, conn, *, execution_key, recipient_id, reason, actor_id, now):
        """Note who actually received a primary fill's proceeds (append-only, one per fill)."""
        from database import pg_backend
        from database.currency_db import ensure_primary_recipient_overrides
        if not pg_backend.enabled():
            ensure_primary_recipient_overrides(conn.execute)
        if not str(reason or "").strip() or not str(actor_id or "").strip():
            raise ValueError("수취인 기록에는 담당자와 사유가 필요합니다.")
        event = conn.execute("SELECT execution_type FROM market_execution_events WHERE execution_key=?",
                             (str(execution_key),)).fetchone()
        if not event or event[0] != "primary":
            raise ValueError("공모(primary) 체결만 수취인을 기록할 수 있습니다.")
        conn.execute("""INSERT INTO market_execution_primary_recipient_overrides
            (execution_key,recipient_id,reason,actor_id,recorded_epoch) VALUES (?,?,?,?,?)""",
            (str(execution_key), str(recipient_id), str(reason).strip(), str(actor_id), int(now)))

    @classmethod
    def reconcile_all_financial_ledgers(cls, conn):
        """Aggregate implemented amount, common-journal, and bank subledger checks.

        The report is deliberately explicit about its coverage. A clean result
        means every registered check passed; it does not certify unconnected
        legacy or special-product transaction paths.
        """
        from functions.money import audit_amount_field_coverage, audit_amount_snapshots
        from managers.bank_journal import BankJournal

        amount_findings = audit_amount_snapshots(conn)
        amount_field_coverage = audit_amount_field_coverage(conn)
        foreign_key_findings = [
            {'table': row[0], 'rowid': row[1], 'parent_table': row[2], 'foreign_key_id': row[3]}
            for row in conn.execute('PRAGMA foreign_key_check')
        ]
        loan_schedule_reference_findings = [
            {'schedule_id': int(row['schedule_id']), 'loan_id': int(row['loan_id']),
             'status': 'missing_loan'}
            for row in conn.execute("""SELECT s.schedule_id,s.loan_id FROM loan_payment_schedule s
                LEFT JOIN loans l ON l.loan_id=s.loan_id WHERE l.loan_id IS NULL""")
        ]
        loan_related_reference_findings = []
        for table, key in (
            ('loan_chargeoff_events', 'event_id'), ('loan_ecl_assessments', 'assessment_id'),
            ('loan_collateral_assets', 'collateral_id'), ('collateral_liquidation_events', 'event_id'),
            ('loan_credit_support', 'support_id'), ('loan_credit_support_events', 'event_id'),
            ('loan_future_commitments', 'loan_id'), ('loan_future_commitment_events', 'event_id'),
            ('loan_collateral_recovery_events', 'event_id'),
        ):
            for row in conn.execute(f"""SELECT s.{key} AS row_id,s.loan_id FROM {table} s
                LEFT JOIN loans l ON l.loan_id=s.loan_id WHERE l.loan_id IS NULL"""):
                loan_related_reference_findings.append({'table': table, 'row_id': int(row['row_id']),
                    'loan_id': int(row['loan_id']), 'status': 'missing_loan'})
        common_checks = cls.reconcile_subledgers(conn)
        common_findings = [dict(row) for row in common_checks
                           if row.get("status") != "matched"]
        treasury_bond_cash = next(
            (dict(row) for row in common_checks
             if row.get("ledger_scope") == "treasury_bond_cash"),
            {"ledger_scope": "treasury_bond_cash", "status": "unavailable",
             "common_journal_gaps": [{"status": "missing_check"}]},
        )
        try:
            from managers.fx_position import FxPosition
            fx_revaluation = {"status": "preview", "positions": FxPosition.preview(conn),
                              "close_check": FxPosition.close_check(conn)}
        except (ValueError, ArithmeticError) as exc:
            fx_revaluation = {"status": "unavailable", "reason": str(exc), "positions": []}
        common_trial_balance = cls.consolidated_trial_balance(conn)
        consumer_coverage = cls.consumer_coverage(conn)
        issuers = [str(row[0]) for row in conn.execute(
            "SELECT issuer FROM financial_institutions ORDER BY issuer"
        ).fetchall()]
        institution_reports = [BankJournal.reconcile_all(conn, issuer)
                               for issuer in issuers]
        institution_findings = [
            {"issuer": report["issuer"], **finding}
            for report in institution_reports
            for finding in report["findings"]
        ]
        schema_findings = [
            {"scope": "amount_schema_coverage", **finding}
            for finding in amount_field_coverage["unclassified"]
        ]
        findings = ([{"scope": "amount_snapshot_registry", **finding}
                     for finding in amount_findings]
                    + schema_findings
                    + [{"scope": "foreign_key_integrity", **finding} for finding in foreign_key_findings]
                    + [{"scope": "loan_schedule_references", **finding} for finding in loan_schedule_reference_findings]
                    + [{"scope": "loan_related_references", **finding} for finding in loan_related_reference_findings]
                    + [{"scope": "common_journals", **finding}
                       for finding in common_findings]
                    + ([{"scope": "treasury_bond_cash", "status": "missing_check"}]
                       if treasury_bond_cash["status"] == "unavailable" else [])
                    + ([{"scope": "fx_revaluation", "status": "unavailable",
                         "reason": fx_revaluation["reason"]}]
                       if fx_revaluation["status"] == "unavailable" else [])
                    + ([{"scope": "fx_revaluation", **finding}
                        for finding in fx_revaluation["close_check"]["findings"]]
                       if fx_revaluation["status"] == "preview" else [])
                    + [{"scope": "common_trial_balance", **finding}
                       for finding in common_trial_balance["findings"]]
                    + [{"scope": "consumer_coverage", "source_table": table, "status": "unclassified"}
                       for table in consumer_coverage["unclassified"]]
                    + [{"scope": "institution_subledgers", **finding}
                       for finding in institution_findings])
        coverage = (
            "amount_snapshot_registry",
            "amount_schema_coverage",
            "foreign_key_integrity",
            "loan_schedule_references",
            "loan_related_references",
            "common_journals",
            "common_trial_balance",
            "treasury_bond_cash",
            "fx_revaluation",
            "consumer_coverage",
            "institution_subledgers",
            "institution",
            "institution_pnl",
            "customer_deposits",
            "loan_receivables",
            "ecl_allowances",
            "charged_off_memo_balances",
            "loan_payment_schedules",
            "collateral_liquidations",
            "deposit_insurance_claims",
            "wholesale_funding",
            "journal_invariants",
        )
        return {
            "status": "matched" if not findings else "mismatch",
            # "matched" covers the checks that exist; the close is ready only when
            # no registered amount table is still unconnected.
            "close_ready": not findings and not consumer_coverage["unconnected"],
            "coverage": coverage,
            "common_trial_balance": common_trial_balance,
            "consumer_coverage": consumer_coverage,
            "institution_count": len(issuers),
            "amount_snapshot_findings": amount_findings,
            "amount_field_coverage": amount_field_coverage,
            "foreign_key_findings": foreign_key_findings,
            "common_journal_checks": common_checks,
            "treasury_bond_cash": treasury_bond_cash,
            "fx_revaluation": fx_revaluation,
            "institution_reports": institution_reports,
            "findings": findings,
        }

    @classmethod
    def reconcile_dividend_payment_settlements(cls, conn):
        """Reconcile new dividend receipts to exactly one wallet or fund cash row."""
        from decimal import Decimal
        from functions.money import currency_precision, to_minor_units

        baseline = conn.execute(
            "SELECT through_payment_id FROM dividend_payment_settlement_baseline_state "
            "WHERE singleton_id=1"
        ).fetchone()
        through = int(baseline[0]) if baseline else 0
        gaps = []
        rows = conn.execute("""WITH wallet_links AS (
                SELECT x.payment_id,COUNT(*) link_count,MIN(l.entry_id) entry_id,
                    MIN(l.user_id) user_id,MIN(l.currency_code) currency_code,
                    MIN(l.decimal_places) decimal_places,MIN(l.delta_minor_units) delta_minor_units
                FROM dividend_payment_wallet_links x
                LEFT JOIN customer_wallet_ledger l ON l.entry_id=x.wallet_entry_id
                GROUP BY x.payment_id
            ), fund_links AS (
                SELECT x.payment_id,COUNT(*) link_count,MIN(l.entry_id) entry_id,
                    MIN(l.product_code) product_code,MIN(l.event) event,
                    MIN(l.reference) reference,MIN(l.currency_code) currency_code,
                    MIN(l.decimal_places) decimal_places,
                    MIN(l.amount_minor_units) amount_minor_units
                FROM dividend_payment_fund_links x
                LEFT JOIN fund_cash_ledger l ON l.entry_id=x.fund_entry_id
                GROUP BY x.payment_id
            )
            SELECT p.payment_id,p.user_id,p.net_amount_units,p.quote_currency,d.asset_code,
                COALESCE(w.link_count,0) wallet_link_count,w.entry_id wallet_entry_id,
                w.user_id wallet_user_id,w.currency_code wallet_currency_code,
                w.decimal_places wallet_decimal_places,w.delta_minor_units wallet_delta_minor_units,
                COALESCE(f.link_count,0) fund_link_count,f.entry_id fund_entry_id,
                f.product_code fund_product_code,f.event fund_event,f.reference fund_reference,
                f.currency_code fund_currency_code,f.decimal_places fund_decimal_places,
                f.amount_minor_units fund_amount_minor_units,a.product_code owner_product_code
            FROM dividend_payments p
            JOIN dividend_distributions d USING(distribution_id)
            LEFT JOIN wallet_links w USING(payment_id)
            LEFT JOIN fund_links f USING(payment_id)
            LEFT JOIN fund_accounts a ON a.owner_id=p.user_id
            WHERE p.payment_id>? ORDER BY p.payment_id""", (through,)).fetchall()
        checked = 0
        currency_places = {}
        for payment in rows:
            payment_id = int(payment["payment_id"])
            currency = str(payment["quote_currency"] or base_currency_code(conn)).upper()
            if currency not in currency_places:
                currency_places[currency] = currency_precision(conn, currency)
            places = currency_places[currency]
            expected = to_minor_units(
                Decimal(int(payment["net_amount_units"] or 0)) / Decimal(1_000_000),
                places,
            )
            wallet_count = int(payment["wallet_link_count"])
            fund_count = int(payment["fund_link_count"])
            if expected <= 0:
                if wallet_count or fund_count:
                    gaps.append({"payment_id": payment_id,
                        "status": "unexpected_zero_payment_settlement_link"})
                continue
            checked += 1
            if wallet_count + fund_count != 1:
                gaps.append({"payment_id": payment_id,
                    "wallet_link_count": wallet_count, "fund_link_count": fund_count,
                    "status": "missing_or_duplicate_settlement_link"})
                continue
            if wallet_count:
                if (payment["wallet_entry_id"] is None
                        or str(payment["wallet_user_id"]) != str(payment["user_id"])
                        or str(payment["wallet_currency_code"]).upper() != currency
                        or int(payment["wallet_decimal_places"]) != places
                        or int(payment["wallet_delta_minor_units"]) != expected):
                    gaps.append({"payment_id": payment_id,
                        "expected_minor_units": expected,
                        "actual_minor_units": (
                            int(payment["wallet_delta_minor_units"])
                            if payment["wallet_delta_minor_units"] is not None else None),
                        "status": "wallet_settlement_mismatch"})
                continue
            amount = (int(payment["fund_amount_minor_units"])
                      if payment["fund_amount_minor_units"] is not None else None)
            if (payment["fund_entry_id"] is None
                    or not payment["owner_product_code"]
                    or str(payment["fund_product_code"]) != str(payment["owner_product_code"])
                    or payment["fund_event"] != "dividend"
                    or str(payment["fund_reference"]) != str(payment["asset_code"])
                    or str(payment["fund_currency_code"]).upper() != currency
                    or int(payment["fund_decimal_places"]) != places
                    or amount != expected):
                gaps.append({"payment_id": payment_id,
                    "expected_minor_units": expected,
                    "actual_minor_units": amount,
                    "status": "fund_settlement_mismatch"})
        orphan_links = int(conn.execute("""SELECT
            (SELECT COUNT(*) FROM dividend_payment_wallet_links x
             LEFT JOIN dividend_payments p USING(payment_id)
             LEFT JOIN customer_wallet_ledger l ON l.entry_id=x.wallet_entry_id
             WHERE p.payment_id IS NULL OR l.entry_id IS NULL) +
            (SELECT COUNT(*) FROM dividend_payment_fund_links x
             LEFT JOIN dividend_payments p USING(payment_id)
             LEFT JOIN fund_cash_ledger l ON l.entry_id=x.fund_entry_id
             WHERE p.payment_id IS NULL OR l.entry_id IS NULL)""").fetchone()[0])
        if orphan_links:
            gaps.append({"orphan_links": orphan_links, "status": "orphan_settlement_link"})
        return {"payment_count": checked, "gaps": gaps,
                "legacy_baseline_through": through,
                "status": "matched" if not gaps else "mismatch"}
