"""Add the opt-in flag without rewriting existing positions or contract units."""


def initialize_fractional(db, table, type_column=None):
    columns = {row['name'] for row in db.execute(f'PRAGMA table_info("{table}")')}
    if 'Fractal' not in columns:
        supported = "'stock','주식','etf','mutual_fund','index_fund','commodity_spot'"
        eligibility = f' AND (Fractal=0 OR {type_column} IN ({supported}))' if type_column else ''
        db.execute(f'ALTER TABLE "{table}" ADD COLUMN Fractal INTEGER NOT NULL DEFAULT 0 '
                   f'CHECK(Fractal IN (0,1){eligibility})')
    if table == 'market_assets':
        order_columns = {row['name'] for row in db.execute('PRAGMA table_info(market_orders)')}
        for name, definition in (
            ('fractional_order', 'INTEGER NOT NULL DEFAULT 0 CHECK(fractional_order IN (0,1))'),
            ('reserved_cash_units', 'INTEGER NOT NULL DEFAULT 0 CHECK(reserved_cash_units>=0)'),
            ('executed_value_units', 'INTEGER NOT NULL DEFAULT 0 CHECK(executed_value_units>=0)'),
        ):
            if name not in order_columns:
                db.execute(f'ALTER TABLE market_orders ADD COLUMN {name} {definition}')
        for lots, primary in (('user_assets', 'holding_id'), ('option_collateral_lots', 'collateral_id')):
            # Keep arithmetic performed by older whole-contract delivery paths canonical.
            fields = {row['name'] for row in db.execute(f'PRAGMA table_info({lots})')}
            if primary not in fields:
                primary = 'lot_id'
            for action in ('INSERT', 'UPDATE OF quantity'):
                suffix = 'insert' if action == 'INSERT' else 'update'
                db.execute(f'CREATE TRIGGER IF NOT EXISTS trg_{lots}_quantity_precision_{suffix} '
                           f'AFTER {action} ON {lots} WHEN NEW.quantity<>ROUND(NEW.quantity,3) BEGIN '
                           f'UPDATE {lots} SET quantity=ROUND(NEW.quantity,3) '
                           f'WHERE {primary}=NEW.{primary}; END')
