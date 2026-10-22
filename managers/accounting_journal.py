"""Shared, immutable accounting posting boundary for all internal ledgers.

Native subledgers keep their existing public schemas. This journal records the
same posting in product-currency minor units, with explicit source-account
mapping and a stable idempotency key, in the caller's SQLite transaction.
"""

import hashlib
import json


class AccountingJournal:
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
                if currency != "NWX" or fund_watermark is None:
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
            "SELECT order_id,side,status,limit_price,limit_price_minor_units,"
            "limit_price_decimal_places,remaining_quantity,quote_currency,"
            "reserved_cash_minor_units,fractional_order,order_kind FROM market_orders "
            "WHERE order_kind='asset' AND COALESCE(fractional_order,0)=0"
        ).fetchall()
        for order in orders:
            currency = str(order["quote_currency"] or "NWX")
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
                       "holding_movement_gaps": holding_movement_gaps,
                       "legacy_holding_baseline_count": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_holding_baselines").fetchone()[0]),
                       "unjournaled_legacy_baselines": int(conn.execute(
                           "SELECT COUNT(*) FROM market_execution_journal_baselines").fetchone()[0]),
                       "historical_baseline_policy": "pre-migration fills are baseline-only; only subsequent fills are event-reconciled",
                       "status": "matched" if not execution_gaps and not market_journal_gaps
                       and not wallet_link_gaps and not holding_movement_gaps else "mismatch"})
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
        fund_rows = conn.execute("""SELECT entry_id,product_code,event,amount,balance,
            reference,recorded_epoch FROM fund_cash_ledger ORDER BY entry_id""").fetchall()
        fund_posting_gaps = []
        seen_fund_entries = set()
        for row in fund_rows:
            entity_id = str(row["product_code"])
            source_key = f"fund-cash:{int(row['entry_id'])}"
            common_entry = conn.execute("""SELECT entry_id,currency_code,decimal_places,
                recorded_epoch FROM accounting_entries WHERE ledger_scope='fund'
                AND entity_type='fund_cash' AND entity_id=? AND source_key=?""",
                (entity_id, source_key)).fetchone()
            if (not common_entry or common_entry["currency_code"] != "NWX"
                    or int(common_entry["decimal_places"]) != 0
                    or int(common_entry["recorded_epoch"]) != int(row["recorded_epoch"])):
                fund_posting_gaps.append({"entry_id": int(row["entry_id"]),
                                          "status": "missing_or_mismatched_posting"})
                continue
            common_id = int(common_entry["entry_id"])
            seen_fund_entries.add(common_id)
            amount = int(row["amount"])
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
        fund_balance_gaps = [dict(row) for row in conn.execute("""SELECT a.product_code,
            a.cash native_cash,COALESCE(SUM(l.amount),0) ledger_cash
            FROM fund_accounts a LEFT JOIN fund_cash_ledger l USING(product_code)
            GROUP BY a.product_code HAVING a.cash<>COALESCE(SUM(l.amount),0)
            ORDER BY a.product_code""").fetchall()]
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
            transaction_id = int(row["transaction_id"])
            if tax > 0 and transaction_id > legacy_tax_through:
                links = conn.execute("""SELECT l.treasury_user_id,w.user_id,w.currency_code,
                    w.delta_minor_units,w.entry_id FROM tax_transaction_wallet_links l
                    JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id
                    WHERE l.transaction_id=? ORDER BY w.entry_id""",
                    (transaction_id,)).fetchall()
                if not links:
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
                if same_currency and sum(int(link["delta_minor_units"])
                                         for link in same_currency) != tax:
                    tax_wallet_gaps.append({"transaction_id": transaction_id,
                        "tax_minor_units": tax,
                        "received_minor_units": sum(int(link["delta_minor_units"])
                                                     for link in same_currency),
                        "status": "same_currency_receipt_amount_mismatch"})
        checks.append({"ledger_scope": "tax_transactions",
                       "transaction_count": len(tax_rows), "gaps": tax_gaps,
                       "wallet_gaps": tax_wallet_gaps,
                       "legacy_wallet_baseline_through": legacy_tax_through,
                       "status": "matched" if not tax_gaps and not tax_wallet_gaps else "mismatch"})
        checks.append({"ledger_scope": "dividend_payment_settlements",
                       **AccountingJournal.reconcile_dividend_payment_settlements(conn)})
        return checks

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
        rows = conn.execute("""SELECT p.payment_id,p.user_id,p.net_amount_units,
            p.quote_currency,p.allocation_key,d.asset_code
            FROM dividend_payments p JOIN dividend_distributions d USING(distribution_id)
            WHERE p.payment_id>? ORDER BY p.payment_id""", (through,)).fetchall()
        checked = 0
        for payment in rows:
            payment_id = int(payment["payment_id"])
            currency = str(payment["quote_currency"] or "NWX").upper()
            places = currency_precision(conn, currency)
            expected = to_minor_units(
                Decimal(int(payment["net_amount_units"] or 0)) / Decimal(1_000_000),
                places,
            )
            wallet = conn.execute("""SELECT l.entry_id,l.user_id,l.currency_code,
                l.decimal_places,l.delta_minor_units FROM dividend_payment_wallet_links x
                LEFT JOIN customer_wallet_ledger l ON l.entry_id=x.wallet_entry_id
                WHERE x.payment_id=?""", (payment_id,)).fetchall()
            fund = conn.execute("""SELECT l.entry_id,l.product_code,l.event,l.reference,l.amount
                FROM dividend_payment_fund_links x LEFT JOIN fund_cash_ledger l
                ON l.entry_id=x.fund_entry_id WHERE x.payment_id=?""", (payment_id,)).fetchall()
            if expected <= 0:
                if wallet or fund:
                    gaps.append({"payment_id": payment_id,
                        "status": "unexpected_zero_payment_settlement_link"})
                continue
            checked += 1
            if len(wallet) + len(fund) != 1:
                gaps.append({"payment_id": payment_id,
                    "wallet_link_count": len(wallet), "fund_link_count": len(fund),
                    "status": "missing_or_duplicate_settlement_link"})
                continue
            if wallet:
                receipt = wallet[0]
                if (receipt["entry_id"] is None
                        or str(receipt["user_id"]) != str(payment["user_id"])
                        or str(receipt["currency_code"]).upper() != currency
                        or int(receipt["decimal_places"]) != places
                        or int(receipt["delta_minor_units"]) != expected):
                    gaps.append({"payment_id": payment_id,
                        "expected_minor_units": expected,
                        "actual_minor_units": (int(receipt["delta_minor_units"])
                                               if receipt["delta_minor_units"] is not None else None),
                        "status": "wallet_settlement_mismatch"})
                continue
            receipt = fund[0]
            owner = conn.execute(
                "SELECT product_code FROM fund_accounts WHERE owner_id=?",
                (str(payment["user_id"]),),
            ).fetchone()
            amount = (to_minor_units(receipt["amount"], 0)
                      if receipt["amount"] is not None and currency == "NWX" else None)
            if (receipt["entry_id"] is None or currency != "NWX" or not owner
                    or str(receipt["product_code"]) != str(owner["product_code"])
                    or receipt["event"] != "dividend"
                    or str(receipt["reference"]) != str(payment["asset_code"])
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
