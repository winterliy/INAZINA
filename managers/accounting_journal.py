"""Shared, immutable accounting posting boundary for all internal ledgers.

Native subledgers keep their existing public schemas. This journal records the
same posting in product-currency minor units, with explicit source-account
mapping and a stable idempotency key, in the caller's SQLite transaction.
"""


class AccountingJournal:
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
        for table in ("accounting_entries", "accounting_lines"):
            for operation in ("UPDATE", "DELETE"):
                name = f"trg_{table}_{operation.lower()}_immutable"
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT,'common accounting journal is append-only'); END""")

    @classmethod
    def register_mapping(cls, conn, ledger_scope, local_code, common_code):
        cls.ensure_schema(conn)
        scope, local, common = map(str, (ledger_scope, local_code, common_code))
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
        combined = {}
        for local, side, units in lines:
            if isinstance(units, bool):
                raise ValueError("공통 분개는 0 이상의 정수 최소단위와 차변/대변을 사용해야 합니다.")
            units = int(units)
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
        """Report missing common postings without silently manufacturing balances."""
        checks = []
        for scope, native_sql in (
            ("company", "SELECT COUNT(*) FROM financial_entries WHERE status='posted'"),
            ("institution", "SELECT COUNT(*) FROM bank_journal_entries WHERE status='posted'"),
        ):
            native = int(conn.execute(native_sql).fetchone()[0])
            common = int(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope=?", (scope,)).fetchone()[0])
            checks.append({"ledger_scope": scope, "native_entries": native,
                           "common_entries": common, "missing_entries": native-common,
                           "status": "matched" if native == common else "mismatch"})
        native = int(conn.execute("""SELECT
            (SELECT COUNT(*) FROM users WHERE cash_minor<>0) +
            (SELECT COUNT(*) FROM accounts WHERE user_id IS NOT NULL AND balance_minor_units<>0)""").fetchone()[0])
        common = int(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='customer'").fetchone()[0])
        wallet = int(conn.execute("SELECT COUNT(*) FROM customer_wallet_ledger").fetchone()[0])
        gaps = int(conn.execute("""SELECT COUNT(*) FROM (
            SELECT u.user_id,'cash' source_type,u.user_id source_account,
                   COALESCE(n.currency_code,'NWX') currency_code,u.cash_minor expected
            FROM users u LEFT JOIN country_currencies n ON n.nationality=u.nationality
            UNION ALL
            SELECT a.user_id,'account',a.account_number,a.quote_currency,a.balance_minor_units
            FROM accounts a WHERE a.user_id IS NOT NULL
        ) live LEFT JOIN (
            SELECT user_id,source_type,source_account,currency_code,SUM(delta_minor_units) actual
            FROM customer_wallet_ledger GROUP BY user_id,source_type,source_account,currency_code
        ) ledger USING(user_id,source_type,source_account,currency_code)
        WHERE COALESCE(live.expected,0)<>COALESCE(ledger.actual,0)""").fetchone()[0])
        status = "matched" if wallet == common and gaps == 0 else "mismatch"
        checks.append({"ledger_scope": "customer", "native_balances": native,
                       "wallet_entries": wallet, "common_entries": common,
                       "balance_gaps": gaps, "missing_entries": wallet-common,
                       "status": status})
        return checks
