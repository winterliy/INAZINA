"""Append-only company accounting, stored in the existing inazina.db."""
from .db_manager import DBManager

# Signed journal balances are debit-positive. Revenue/liability/equity are credit-normal.
ACCOUNTS = {
    'cash': ('법인 현금', 'current_asset'),
    'restricted_cash': ('잠금 예치금', 'noncurrent_asset'),
    'receivables': ('매출채권', 'current_asset'),
    'inventory': ('재고자산', 'current_asset'),
    'short_investments': ('단기금융자산', 'current_asset'),
    'investments': ('장기투자자산', 'noncurrent_asset'),
    'ppe': ('유형자산', 'noncurrent_asset'),
    'depreciation_accumulated': ('감가상각누계액', 'noncurrent_asset'),
    'intangibles': ('무형자산', 'noncurrent_asset'),
    'amortization_accumulated': ('무형자산상각누계액', 'noncurrent_asset'),
    'treasury_receivable': ('국고 보관 발행대금', 'current_asset'),
    'payables': ('매입채무', 'current_liability'),
    'accrued_expenses': ('미지급비용', 'current_liability'),
    'short_debt': ('단기차입금', 'current_liability'),
    'dividend_payable': ('미지급배당금', 'current_liability'),
    'long_debt': ('장기차입금', 'noncurrent_liability'),
    'els_liability': ('ELS 예치 부채', 'noncurrent_liability'),
    'bonds': ('회사채', 'noncurrent_liability'),
    'bond_discount': ('회사채 발행차금', 'noncurrent_liability'),
    'common_capital': ('보통주자본금', 'equity'),
    'preferred_capital': ('우선주자본금', 'equity'),
    'capital_surplus': ('자본잉여금', 'equity'),
    'retained': ('이익잉여금·배당', 'equity'),
    'treasury_stock': ('자기주식', 'equity'),
    'opening_adjustment': ('도입 전 미확인 잔액', 'equity'),
    'adjustment': ('기타 자본조정', 'equity'),
    'revenue': ('매출액', 'revenue'),
    'other_income': ('영업외수익', 'revenue'),
    'interest_income': ('이자수익', 'revenue'),
    'cogs': ('매출원가', 'expense'),
    'sga': ('판매비와관리비', 'expense'),
    'depreciation': ('감가상각비', 'expense'),
    'amortization': ('무형자산상각비', 'expense'),
    'interest_expense': ('이자비용', 'expense'),
    'fx_rounding': ('통화환산 반올림차이', 'expense'),
    'other_expense': ('영업외비용', 'expense'),
    'income_tax': ('법인세비용', 'expense'),
}


class FinancialDB(DBManager):
    def __init__(self):
        super().__init__('financial')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_entities (
            company_code TEXT PRIMARY KEY, currency TEXT NOT NULL,
            decimal_places INTEGER NOT NULL CHECK(decimal_places BETWEEN 0 AND 6),
            opened_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_accounts (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL)''')
        for code, (name, category) in ACCOUNTS.items():
            self.execute('INSERT OR IGNORE INTO financial_accounts VALUES (?,?,?)', (code,name,category))
        self.execute('''CREATE TABLE IF NOT EXISTS financial_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL, period TEXT NOT NULL, recorded_epoch INTEGER NOT NULL,
            source_key TEXT NOT NULL, description TEXT NOT NULL,
            cash_flow TEXT NOT NULL CHECK(cash_flow IN ('opening','operating','investing','financing','adjustment','none')),
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted')),
            reversal_of INTEGER UNIQUE, UNIQUE(company_code,source_key))''')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER NOT NULL,
            account TEXT NOT NULL, amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount<>0),
            FOREIGN KEY(entry_id) REFERENCES financial_entries(entry_id),
            FOREIGN KEY(account) REFERENCES financial_accounts(code))''')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_periods (
            company_code TEXT NOT NULL, period TEXT NOT NULL, closed_epoch INTEGER NOT NULL,
            report_json TEXT NOT NULL, PRIMARY KEY(company_code,period))''')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_share_history (
            company_code TEXT NOT NULL, recorded_epoch INTEGER NOT NULL, quantity_milli INTEGER NOT NULL,
            PRIMARY KEY(company_code,recorded_epoch))''')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_operating_profiles (
            company_code TEXT PRIMARY KEY, config_json TEXT NOT NULL, start_period TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)))''')
        self.execute('''CREATE TABLE IF NOT EXISTS financial_bond_basis (
            product_code TEXT PRIMARY KEY, company_code TEXT NOT NULL, discount_units INTEGER NOT NULL)''')
        self.execute('CREATE INDEX IF NOT EXISTS idx_financial_entries_period ON financial_entries(company_code,period,status)')
        self.execute('CREATE INDEX IF NOT EXISTS idx_financial_lines_entry ON financial_lines(entry_id,account)')
        # Explicit trigger validation also protects DB Browser edits when FK checking is off.
        self.execute('''CREATE TRIGGER IF NOT EXISTS fin_post_balanced BEFORE UPDATE OF status ON financial_entries
            WHEN NEW.status='posted' BEGIN
            SELECT CASE WHEN (SELECT COUNT(*) FROM financial_lines WHERE entry_id=NEW.entry_id)<2
              OR (SELECT TOTAL(amount) FROM financial_lines WHERE entry_id=NEW.entry_id)<>0
              THEN RAISE(ABORT,'unbalanced financial entry') END;
            END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS fin_entry_insert BEFORE INSERT ON financial_entries BEGIN
            SELECT CASE WHEN NEW.status<>'draft' OR NOT EXISTS(SELECT 1 FROM financial_entities WHERE company_code=NEW.company_code)
              OR EXISTS(SELECT 1 FROM financial_periods WHERE company_code=NEW.company_code AND period>=NEW.period)
              THEN RAISE(ABORT,'invalid or closed financial period') END; END''')
        for operation in ('UPDATE','DELETE'):
            self.execute(f'''CREATE TRIGGER IF NOT EXISTS fin_entry_{operation.lower()} BEFORE {operation} ON financial_entries
                WHEN OLD.status='posted' BEGIN SELECT RAISE(ABORT,'posted entries are immutable'); END''')
            self.execute(f'''CREATE TRIGGER IF NOT EXISTS fin_line_{operation.lower()} BEFORE {operation} ON financial_lines
                BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END''')
            self.execute(f'''CREATE TRIGGER IF NOT EXISTS fin_period_{operation.lower()} BEFORE {operation} ON financial_periods
                BEGIN SELECT RAISE(ABORT,'closed reports are immutable'); END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS fin_line_insert BEFORE INSERT ON financial_lines BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM financial_entries WHERE entry_id=NEW.entry_id AND status='draft')
              OR NOT EXISTS(SELECT 1 FROM financial_accounts WHERE code=NEW.account)
              THEN RAISE(ABORT,'invalid financial line') END; END''')

        self.execute('CREATE INDEX IF NOT EXISTS idx_option_collateral_financial ON option_collateral_lots(underlying_code,writer_id)')
        from .user_db import TREASURY_USER_ID
        treasury = str(TREASURY_USER_ID).replace("'", "''")
        for table,column in (('user_assets','asset_code'),('option_collateral_lots','underlying_code')):
            for operation in ('INSERT','UPDATE','DELETE'):
                rows = ('OLD','NEW') if operation=='UPDATE' else ('NEW',) if operation=='INSERT' else ('OLD',)
                statements=[]
                for row in rows:
                    statements.append(f"""INSERT INTO financial_share_history(company_code,recorded_epoch,quantity_milli)
                        SELECT c.company_code,CAST(strftime('%s','now') AS INTEGER),
                        CAST(ROUND((COALESCE((SELECT SUM(quantity) FROM user_assets WHERE asset_code=c.company_code AND user_id<>'{treasury}'),0)
                          +COALESCE((SELECT SUM(quantity) FROM option_collateral_lots WHERE underlying_code=c.company_code AND writer_id<>'{treasury}'),0))*1000) AS INTEGER)
                        FROM company c JOIN financial_entities f USING(company_code)
                        WHERE c.share_class='common' AND c.company_code={row}.{column}
                        ON CONFLICT(company_code,recorded_epoch) DO UPDATE SET quantity_milli=excluded.quantity_milli;""")
                self.execute(f"CREATE TRIGGER IF NOT EXISTS fin_shares_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN "+' '.join(statements)+' END')
