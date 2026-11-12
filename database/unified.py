"""One SQLite file, with a lossless, resumable upgrade from the legacy files."""
import os
import re
import sqlite3
from decimal import Decimal
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

ALIASES = ("users", "accounts", "assets", "product", "company", "pot", "tax", "system")
# Ignore SQL strings and comments; legacy schema qualifiers are an internal
# compatibility layer, never attached copies of the same physical database.
_SQL = re.compile(r"' (?:[^']|'')* ' | --[^\n]* | /\*.*?\*/ | "
                  r'\b(?:users|accounts|assets|product|company|pot|tax|system)\.(?=[A-Za-z_"\[])',
                  re.X | re.S)

def sql(statement):
    return _SQL.sub(lambda m: m.group() if m.group().startswith(("'", "--", "/*")) else "", statement)

_NEVER_TAGGED={'user_id','account_number','name','nickname','nationality','status','role','payload','description'}
_ALWAYS_NAMES={'total','gross','net','amount','balance','debit','credit'}


@lru_cache(maxsize=4096)
def _column_plan(names):
    """Per column: (decode every text value, decode only tagged text, integral)."""
    from .money_columns import possible_money
    plan=[]
    for name in names:
        name=name.lower()
        always=possible_money(name) or 'wide_' in name or name in _ALWAYS_NAMES
        if_tagged=not always and name not in _NEVER_TAGGED and not name.endswith(('_code','_id','_name'))
        integral=name.endswith(('_minor','_minor_units','_subminor_units')) or name in {'balance_units','cash_minor'}
        plan.append((always,if_tagged,integral))
    return tuple(plan)


def _wide_row(cursor, values):
    from .wide_values import tagged,decoded
    plan=_column_plan(tuple(info[0] for info in cursor.description))
    converted=list(values)
    for index,value in enumerate(converted):
        if value.__class__ is not str:
            continue
        always,if_tagged,integral=plan[index]
        if always or (if_tagged and tagged(value)):
            try:converted[index]=decoded(value,integral)
            except (ValueError,ArithmeticError):pass
    return sqlite3.Row(cursor,tuple(converted))


def _parameters(connection,values,statement=""):
    from .wide_values import parameters
    if connection.wide_money:return parameters(values)
    guarded=set()
    if not isinstance(values,dict) and any(isinstance(v,(int,Decimal)) and abs(v)>2**63-1 for v in values):
        import sqlglot
        from sqlglot import exp
        tree=sqlglot.parse_one(statement,read='sqlite')
        guarded={i for i,node in enumerate(tree.find_all(exp.Placeholder)) if node.find_ancestor(exp.Where) is not None}
    def scalar(value,index=None):
        if index in guarded and isinstance(value,(int,Decimal)) and abs(value)>2**63-1:return str(value)
        if not isinstance(value,Decimal):return value
        if not value.is_finite():raise ValueError('금액은 유한한 숫자여야 합니다.')
        if abs(value)>2**53-1:raise ValueError('이 DB는 아직 큰 금액 저장 형식으로 이관되지 않았습니다.')
        if value and float(value)==0:raise ValueError('현재 DB에서 정확하게 저장할 수 없는 금액입니다.')
        return int(value) if value==value.to_integral_value() else float(value)
    return {key:scalar(value) for key,value in values.items()} if isinstance(values,dict) else tuple(scalar(value,index) for index,value in enumerate(values))


class UnifiedCursor(sqlite3.Cursor):
    def execute(self,statement,parameters=()):
        statement=sql(statement) if self.connection.legacy_aliases else statement
        if self.connection.wide_money:
            from .wide_sql import compile_sql
            statement=compile_sql(statement)
        return super().execute(statement,_parameters(self.connection,parameters,statement))

    def executemany(self,statement,parameters):
        statement=sql(statement) if self.connection.legacy_aliases else statement
        if self.connection.wide_money:
            from .wide_sql import compile_sql
            statement=compile_sql(statement)
        return super().executemany(statement,(_parameters(self.connection,row,statement) for row in parameters))


class UnifiedConnection(sqlite3.Connection):
    def __init__(self,*args,**kwargs):
        kwargs['detect_types']=kwargs.get('detect_types',0)|sqlite3.PARSE_DECLTYPES
        super().__init__(*args,**kwargs)
        self.legacy_aliases=True
        self.wide_money=os.environ.get('INAZINA_WIDE_MONEY')=='1' or bool(sqlite3.Connection.execute(self,"SELECT 1 FROM sqlite_master WHERE name='wide_money_schema'").fetchone())
        empty=not sqlite3.Connection.execute(self,"SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
        if empty and os.environ.get('INAZINA_WIDE_MONEY')!='0':
            self.wide_money=True
            sqlite3.Connection.execute(self,'CREATE TABLE wide_money_schema(version INTEGER NOT NULL)')
            sqlite3.Connection.execute(self,'INSERT INTO wide_money_schema VALUES (1)')
            self.commit()
        super().execute("PRAGMA foreign_keys=ON")
        self.create_function("base_currency_code",0,self._base_currency_code)
        from .exact_cost_basis import register
        register(self)
        from .wide_values import register
        register(self)

    @property
    def row_factory(self):return sqlite3.Connection.row_factory.__get__(self)

    @row_factory.setter
    def row_factory(self,value):
        sqlite3.Connection.row_factory.__set__(self,_wide_row if value is sqlite3.Row and self.wide_money else value)

    def _base_currency_code(self):
        try:row=sqlite3.Connection.execute(self,"SELECT base_currency_code FROM currency_system_settings WHERE singleton_id=1").fetchone()
        except sqlite3.OperationalError:return None
        return str(row[0]).upper() if row and row[0] else None

    def execute(self,statement,parameters=()):return self.cursor().execute(statement,parameters)
    def executemany(self,statement,parameters):return self.cursor().executemany(statement,parameters)
    def cursor(self,factory=UnifiedCursor):return super().cursor(factory)

@contextmanager
def _legacy_snapshot(root):
    # Acquire writer reservations for every source before copying any table.
    # A concurrent old-version transfer cannot straddle the snapshot boundary.
    with closing(sqlite3.connect(':memory:',timeout=30,isolation_level=None)) as source:
        attached=[]
        for alias in ALIASES:
            path=root/f'{alias}.db'
            if path.exists():
                source.execute(f'ATTACH DATABASE ? AS {alias}',(str(path),))
                attached.append(alias)
        source.execute('BEGIN IMMEDIATE')
        try:
            yield source,attached
        finally:
            source.rollback()

def migrate(root):
    """Build a verified staging file, then atomically publish it. Keep originals."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "inazina.db"
    if target.exists():
        return target
    staging = root / "inazina.migrating"
    # A previous unsuccessful upgrade has no authority over the legacy files.
    if staging.exists():
        staging.unlink()
    counts = {}
    with _legacy_snapshot(root) as (src,attached), closing(sqlite3.connect(staging)) as dest:
        try:
            dest.execute("BEGIN IMMEDIATE")
            objects = []
            for alias in attached:
                schema = src.execute(f"SELECT type, name, sql FROM {alias}.sqlite_master "
                                     "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                                     "ORDER BY type, name").fetchall()
                for kind, name, ddl in schema:
                    if kind != "table":
                        objects.append(ddl)
                        continue
                    if name in counts:
                        raise RuntimeError(f"중복 테이블로 DB 통합 중단: {name}")
                    dest.execute(ddl)
                    escaped = name.replace('"', '""')
                    rows = src.execute(f'SELECT * FROM {alias}."{escaped}"')
                    width = len(rows.description)
                    insert = f'INSERT INTO "{escaped}" VALUES ({",".join("?" for _ in range(width))})'
                    count = 0
                    while batch := rows.fetchmany(1000):
                        dest.executemany(insert, batch)
                        count += len(batch)
                    counts[name] = count
                if src.execute(f"SELECT 1 FROM {alias}.sqlite_master WHERE name='sqlite_sequence'").fetchone():
                    for name, seq in src.execute(f"SELECT name, seq FROM {alias}.sqlite_sequence"):
                        dest.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (seq, name))
            for ddl in objects:
                dest.execute(ddl)
            for name, expected in counts.items():
                escaped = name.replace('"', '""')
                if dest.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0] != expected:
                    raise RuntimeError(f"DB 통합 건수 검증 실패: {name}")
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("DB 무결성 검증 실패")
            dest.commit()
        except BaseException:
            dest.rollback()
            raise
    os.replace(staging, target)
    return target
