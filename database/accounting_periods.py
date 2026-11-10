"""Storage-level guards shared by startup and common journal posting."""


def ensure_period_guards(conn):
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
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_accounting_period_closings_insert_immutable
        BEFORE INSERT ON accounting_period_closings
        WHEN EXISTS (SELECT 1 FROM accounting_period_closings c
            WHERE c.closing_id=NEW.closing_id OR
                (c.ledger_scope=NEW.ledger_scope AND c.entity_id=NEW.entity_id
                 AND c.currency_code=NEW.currency_code AND c.period_start_epoch=NEW.period_start_epoch
                 AND c.period_end_epoch=NEW.period_end_epoch))
        BEGIN SELECT RAISE(ABORT,'accounting period closings are append-only'); END""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_accounting_entry_period
        ON accounting_entries(ledger_scope,entity_id,currency_code,recorded_epoch)""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_accounting_closing_mixed_precision
        BEFORE INSERT ON accounting_period_closings
        WHEN (SELECT COUNT(DISTINCT decimal_places) FROM accounting_entries e
            WHERE e.ledger_scope=NEW.ledger_scope AND e.entity_id=NEW.entity_id
            AND e.currency_code=NEW.currency_code AND e.recorded_epoch>=NEW.period_start_epoch
            AND e.recorded_epoch<NEW.period_end_epoch)>1
        BEGIN SELECT RAISE(ABORT,'mixed currency precision in accounting period'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_accounting_closing_unbalanced
        BEFORE INSERT ON accounting_period_closings
        WHEN EXISTS (SELECT e.entry_id FROM accounting_entries e
            LEFT JOIN accounting_lines l ON l.entry_id=e.entry_id
            WHERE e.ledger_scope=NEW.ledger_scope AND e.entity_id=NEW.entity_id
            AND e.currency_code=NEW.currency_code AND e.recorded_epoch>=NEW.period_start_epoch
            AND e.recorded_epoch<NEW.period_end_epoch
            GROUP BY e.entry_id
            HAVING COUNT(l.line_id)<2 OR SUM(l.debit_units)<>SUM(l.credit_units))
        BEGIN SELECT RAISE(ABORT,'unbalanced accounting period'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_accounting_entries_closed_insert
        BEFORE INSERT ON accounting_entries
        WHEN EXISTS(SELECT 1 FROM accounting_period_closings c
            WHERE c.ledger_scope=NEW.ledger_scope AND c.entity_id=NEW.entity_id
            AND c.currency_code=NEW.currency_code AND NEW.recorded_epoch>=c.period_start_epoch
            AND NEW.recorded_epoch<c.period_end_epoch)
        OR EXISTS(SELECT 1 FROM accounting_entries e JOIN accounting_period_closings c
            ON c.ledger_scope=e.ledger_scope AND c.entity_id=e.entity_id AND c.currency_code=e.currency_code
            AND e.recorded_epoch>=c.period_start_epoch AND e.recorded_epoch<c.period_end_epoch
            WHERE e.entry_id=NEW.entry_id OR
              (e.ledger_scope=NEW.ledger_scope AND e.entity_type=NEW.entity_type
               AND e.entity_id=NEW.entity_id AND e.source_key=NEW.source_key))
        BEGIN SELECT RAISE(ABORT,'closed accounting period'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_accounting_lines_closed_insert
        BEFORE INSERT ON accounting_lines
        WHEN EXISTS(SELECT 1 FROM accounting_entries e JOIN accounting_period_closings c
            ON c.ledger_scope=e.ledger_scope AND c.entity_id=e.entity_id AND c.currency_code=e.currency_code
            AND e.recorded_epoch>=c.period_start_epoch AND e.recorded_epoch<c.period_end_epoch
            WHERE e.entry_id=NEW.entry_id OR e.entry_id=(
                SELECT entry_id FROM accounting_lines WHERE line_id=NEW.line_id))
        BEGIN SELECT RAISE(ABORT,'closed accounting period'); END""")
