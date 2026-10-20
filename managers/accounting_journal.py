"""Shared, immutable accounting posting boundary for all internal ledgers.

Native subledgers keep their existing public schemas. This journal records the
same posting in product-currency minor units, with explicit source-account
mapping and a stable idempotency key, in the caller's SQLite transaction.
"""

import hashlib
import json


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
        conn.execute("""CREATE TABLE IF NOT EXISTS accounting_period_closings (
            closing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_scope TEXT NOT NULL, entity_id TEXT NOT NULL, currency_code TEXT NOT NULL,
            period_start_epoch INTEGER NOT NULL, period_end_epoch INTEGER NOT NULL,
            closed_epoch INTEGER NOT NULL, actor_id TEXT NOT NULL,
            entry_count INTEGER NOT NULL CHECK(entry_count>=0),
            closing_digest TEXT NOT NULL,
            CHECK(period_start_epoch<period_end_epoch),
            UNIQUE(ledger_scope,entity_id,currency_code,period_start_epoch,period_end_epoch))""")
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_accounting_period_closings_{operation.lower()}_immutable
                BEFORE {operation} ON accounting_period_closings
                BEGIN SELECT RAISE(ABORT,'accounting period closings are append-only'); END""")
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
        rows = conn.execute("""SELECT e.entry_id,e.source_key,l.account_code,
                l.debit_units,l.credit_units
            FROM accounting_entries e JOIN accounting_lines l USING(entry_id)
            WHERE e.ledger_scope=? AND e.entity_id=? AND e.currency_code=?
              AND e.recorded_epoch>=? AND e.recorded_epoch<?
            ORDER BY e.entry_id,l.account_code""",
            (scope, entity, currency, start, end)).fetchall()
        normalized = [tuple(row) for row in rows]
        digest = hashlib.sha256(json.dumps(normalized, separators=(",", ":"),
                                 ensure_ascii=False).encode()).hexdigest()
        prior = conn.execute("""SELECT closing_id,entry_count,closing_digest FROM accounting_period_closings
            WHERE ledger_scope=? AND entity_id=? AND currency_code=?
              AND period_start_epoch=? AND period_end_epoch=?""",
            (scope, entity, currency, start, end)).fetchone()
        if prior:
            if (int(prior[1]), str(prior[2])) != (len({row[0] for row in normalized}), digest):
                raise ValueError("기존 회계기간 마감 스냅샷과 현재 원장이 다릅니다.")
            return int(prior[0])
        cursor = conn.execute("""INSERT INTO accounting_period_closings
            (ledger_scope,entity_id,currency_code,period_start_epoch,period_end_epoch,
             closed_epoch,actor_id,entry_count,closing_digest)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (scope, entity, currency, start, end, closed_at, actor,
             len({row[0] for row in normalized}), digest))
        return int(cursor.lastrowid)

    @classmethod
    def post_treasury_bond_cash_event(cls, conn, *, event_key, event_type,
            holding_id, product_code, user_id, currency_code, decimal_places,
            principal_minor_units, interest_minor_units, tax_minor_units,
            net_minor_units, recorded_epoch):
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

    @classmethod
    def reconcile_treasury_bond_cash_events(cls, conn):
        """Check immutable bond cash events against common-journal lines and keys."""
        gaps = []
        seen = set()
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
        orphan_count = int(conn.execute("""SELECT COUNT(*) FROM accounting_entries a
            WHERE a.ledger_scope='treasury_bond' AND NOT EXISTS (
                SELECT 1 FROM treasury_bond_cash_events e WHERE e.holding_id=a.entity_id
                  AND e.event_key=a.source_key)""").fetchone()[0])
        if orphan_count:
            gaps.append({"orphan_common_entries":orphan_count,"status":"mismatch"})
        return {"event_count":int(conn.execute("SELECT COUNT(*) FROM treasury_bond_cash_events").fetchone()[0]),
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
                "SELECT entry_id,entity_id,source_key,currency_code,decimal_places,recorded_epoch "
                "FROM accounting_entries WHERE ledger_scope=?", (scope,),
            ).fetchall()
            common_by_key = {(str(row["entity_id"]), str(row["source_key"])): row
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
                common_entry = common_by_key.get((entity_id, source_key))
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
        # A standard limit-buy's locked cash is an asset subledger balance,
        # distinct from customer spendable cash. Validate it against remaining
        # quantity and the order's quote-currency limit price.
        from functions.money import currency_precision, to_minor_units
        active = {"scheduled", "open", "partial"}
        reservation_gaps = []
        orders = conn.execute(
            "SELECT order_id,side,status,limit_price,remaining_quantity,quote_currency,"
            "reserved_cash_minor_units,fractional_order,order_kind FROM market_orders "
            "WHERE order_kind='asset' AND COALESCE(fractional_order,0)=0"
        ).fetchall()
        for order in orders:
            currency = str(order["quote_currency"] or "NWX")
            places = currency_precision(conn, currency)
            expected = (to_minor_units(order["limit_price"], places)*int(order["remaining_quantity"])
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
            currency = str(order["quote_currency"] or "NWX").upper()
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
            o.filled_quantity,COALESCE(b.baseline_filled_quantity,0) baseline_filled
            FROM market_orders o LEFT JOIN market_execution_order_baselines b USING(order_id)
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
            if expected != recorded or expected < 0:
                execution_gaps.append({"order_id": order_id,
                    "expected_new_filled_quantity": expected,
                    "recorded_execution_quantity": recorded, "status": "mismatch"})
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
        checks.append({"ledger_scope": "market_executions",
                       "tracked_order_count": len(tracked_orders),
                       "execution_event_count": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_events").fetchone()[0]),
                       "execution_gaps": execution_gaps,
                       "common_journal_gaps": market_journal_gaps,
                       "wallet_link_gaps": wallet_link_gaps,
                       "unjournaled_legacy_baselines": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_journal_baselines").fetchone()[0]),
                       "historical_baseline_policy": "pre-migration fills are baseline-only; only subsequent fills are event-reconciled",
                       "status": "matched" if not execution_gaps and not market_journal_gaps
                       and not wallet_link_gaps else "mismatch"})
        checks.append({"ledger_scope":"treasury_bond_cash",
                       **AccountingJournal.reconcile_treasury_bond_cash_events(conn)})
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
        from functions.money import currency_precision, to_minor_units
        tax_gaps = []
        tax_rows = conn.execute("SELECT * FROM tax_transactions ORDER BY transaction_id").fetchall()
        for row in tax_rows:
            currency = str(row["currency_code"] or "NWX").upper()
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
        checks.append({"ledger_scope": "tax_transactions",
                       "transaction_count": len(tax_rows), "gaps": tax_gaps,
                       "status": "matched" if not tax_gaps else "mismatch"})
        return checks
