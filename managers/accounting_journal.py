"""Shared, immutable accounting posting boundary for all internal ledgers.

Native subledgers keep their existing public schemas. This journal records the
same posting in product-currency minor units, with explicit source-account
mapping and a stable idempotency key, in the caller's SQLite transaction.
"""
from functions.money import base_currency_code

import hashlib
import json


class AccountingJournal:
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
            fund_watermark=None):
        """Append a bond cash event and its balanced common posting atomically."""
        cls.ensure_schema(conn)
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
        for local, _, _ in lines:
            cls.register_mapping(conn, "treasury_bond", local, local)
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
        if net:
            fund_owner = conn.execute("SELECT product_code FROM fund_accounts WHERE owner_id=?",
                                      (str(user_id),)).fetchone()
            if fund_owner:
                if currency != base_currency_code(conn) or fund_watermark is None:
                    raise ValueError("펀드 국채 지급의 현금 원장 기준점이 없습니다.")
                fund_entry = conn.execute("""SELECT entry_id FROM fund_cash_ledger
                    WHERE entry_id>? AND product_code=? AND event=? AND reference=? AND amount=?
                    ORDER BY entry_id""",
                    (int(fund_watermark), str(fund_owner["product_code"]),
                     "bond_coupon" if event_type == "coupon" else "bond_redemption",
                     str(product_code), net)).fetchone()
                if not fund_entry:
                    raise RuntimeError("국채 지급 이벤트에 대응하는 펀드 현금 입금이 없습니다.")
                conn.execute("INSERT INTO treasury_bond_cash_fund_links(event_key,fund_entry_id) VALUES (?,?)",
                             (source_key, int(fund_entry["entry_id"])))
            else:
                if wallet_watermark is None:
                    raise ValueError("국채 지급의 고객 지갑 기준점이 없습니다.")
                wallet_entry = conn.execute("""SELECT entry_id FROM customer_wallet_ledger
                    WHERE entry_id>? AND user_id=? AND delta_minor_units>0 ORDER BY entry_id""",
                    (int(wallet_watermark), str(user_id))).fetchone()
                if not wallet_entry:
                    raise RuntimeError("국채 지급 이벤트에 대응하는 고객 지갑 입금이 없습니다.")
                conn.execute("INSERT INTO treasury_bond_cash_wallet_links(event_key,wallet_entry_id) VALUES (?,?)",
                             (source_key, int(wallet_entry["entry_id"])))

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
            if fund_owner:
                expected_event = "bond_coupon" if event["event_type"] == "coupon" else "bond_redemption"
                if (net > 0 and (not fund_link or fund_link["product_code"] != fund_owner["product_code"]
                        or fund_link["event"] != expected_event
                        or fund_link["reference"] != event["product_code"]
                        or int(fund_link["amount"] or 0) != net)
                        or (net == 0 and fund_link) or wallet_link):
                    gaps.append({"event_key":event["event_key"],
                                 "status":"missing_or_invalid_fund_settlement_link"})
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
                  AND e.event_key=a.source_key)""").fetchone()[0])
        if orphan_count:
            gaps.append({"orphan_common_entries":orphan_count,"status":"mismatch"})
        orphan_links = int(conn.execute("""SELECT
            (SELECT COUNT(*) FROM treasury_bond_cash_wallet_links w
             LEFT JOIN treasury_bond_cash_events e USING(event_key) WHERE e.event_key IS NULL)+
            (SELECT COUNT(*) FROM treasury_bond_cash_fund_links w
             LEFT JOIN treasury_bond_cash_events e USING(event_key) WHERE e.event_key IS NULL)""").fetchone()[0])
        if orphan_links:
            gaps.append({"orphan_settlement_links":orphan_links,"status":"mismatch"})
        return {"event_count":int(conn.execute("SELECT COUNT(*) FROM treasury_bond_cash_events").fetchone()[0]),
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
            issuer = conn.execute("""SELECT COALESCE(parent_company_code,company_code)
                FROM company WHERE company_code=? AND dividend_funding='issuer'""",
                (asset_code,)).fetchone()
            corporate_bond = conn.execute("""SELECT issuer_code FROM product.investment
                WHERE product_code=? AND product_type='corporate_bond'""",
                (asset_code,)).fetchone()
            if issuer:
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
        status = ("matched" if wallet == common and gaps == 0 and posting_gaps == 0
                  else "mismatch")
        checks.append({"ledger_scope": "customer", "native_balances": native,
                       "wallet_entries": wallet, "common_entries": common,
                       "balance_gaps": gaps, "posting_gaps": posting_gaps,
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
        common_checks = cls.reconcile_subledgers(conn)
        common_findings = [dict(row) for row in common_checks
                           if row.get("status") != "matched"]
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
                    + [{"scope": "common_journals", **finding}
                       for finding in common_findings]
                    + [{"scope": "institution_subledgers", **finding}
                       for finding in institution_findings])
        coverage = (
            "amount_snapshot_registry",
            "amount_schema_coverage",
            "foreign_key_integrity",
            "loan_schedule_references",
            "common_journals",
            "institution_subledgers",
            "institution_pnl",
            "customer_deposits",
            "loan_receivables",
            "ecl_allowances",
            "charged_off_memo_balances",
            "loan_payment_schedules",
            "collateral_liquidations",
            "deposit_insurance_claims",
            "journal_invariants",
        )
        return {
            "status": "matched" if not findings else "mismatch",
            "coverage": coverage,
            "institution_count": len(issuers),
            "amount_snapshot_findings": amount_findings,
            "amount_field_coverage": amount_field_coverage,
            "foreign_key_findings": foreign_key_findings,
            "common_journal_checks": common_checks,
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
