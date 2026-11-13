"""PostgreSQL behind the sqlite3 interface the app already uses (INAZINA_DB_BACKEND=postgres).

The app's SQL is written for SQLite against the old table layout. Statements are
translated once (cached) with the same rules that built database/postgres/schema.sql:
dialect, wide-money helpers, renamed/merged tables (saving -> bank_products rows of
kind 'deposit', ...). PRAGMA and sqlite_master reads are answered from the PostgreSQL
catalog. Errors are raised as the sqlite3 exception classes callers already catch,
and NUMERIC values come back exactly as the SQLite wide-money decoder returns them.
"""
import os
import re
import sqlite3
import sys
import threading
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import psycopg
import sqlglot
from sqlglot import exp

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
from build_postgres_schema import DROPPED, rewrite, target_table  # noqa: E402
from build_postgres_triggers import _filter_merged  # noqa: E402

DSN_ENV = "INAZINA_PG_DSN"
_MARK = "\x00P\x00"
# BEGIN IMMEDIATE reserves SQLite's single writer; one advisory lock does the same.
WRITER_LOCK = "SELECT pg_advisory_xact_lock(4243)"
_NO_OP_PRAGMAS = {"foreign_keys", "busy_timeout", "query_only", "journal_mode", "synchronous",
                  "legacy_alter_table", "optimize", "wal_checkpoint", "defer_foreign_keys",
                  "cache_size", "temp_store", "mmap_size", "recursive_triggers"}


def enabled():
    return os.environ.get("INAZINA_DB_BACKEND", "sqlite").lower() == "postgres"


def dsn():
    return os.environ.get(DSN_ENV, "postgresql:///inazina_dev")


# --------------------------------------------------------------------------- values

def _decode(value):
    """NUMERIC as the SQLite wide-money decoder returns it (int, float or ExactDecimal)."""
    if isinstance(value, Decimal):
        from database.wide_values import decoded
        return decoded(str(value))
    return value


class Row(tuple):
    """sqlite3.Row look-alike: index, name and keys() access."""

    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                lowered = [n.lower() for n in self._names]
                return tuple.__getitem__(self, lowered.index(key.lower()))
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


# --------------------------------------------------------------------------- SQL translation

def _pragma(sql):
    match = re.fullmatch(r"\s*PRAGMA\s+(?:main\.)?(\w+)\s*(?:\(\s*['\"]?([\w.]+)['\"]?\s*\)|=\s*(.+?))?\s*;?\s*",
                         sql, re.I | re.S)
    if not match:
        return None
    name, argument = match[1].lower(), match[2]
    if name == "table_info" and argument:
        table = target_table(argument)
        return ("SELECT c.ordinal_position - 1 AS cid, c.column_name AS name, upper(c.data_type) AS type, "
                "CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, c.column_default AS dflt_value, "
                "COALESCE((SELECT k.ordinal_position FROM information_schema.key_column_usage k "
                "JOIN information_schema.table_constraints tc ON tc.constraint_name = k.constraint_name "
                "AND tc.table_name = k.table_name AND tc.constraint_type = 'PRIMARY KEY' "
                "WHERE k.table_name = c.table_name AND k.column_name = c.column_name), 0) AS pk "
                f"FROM information_schema.columns c WHERE c.table_schema = 'public' AND c.table_name = '{table}' "
                "ORDER BY c.ordinal_position"), ()
    if name == "table_list":
        return ("SELECT 'main' AS schema, table_name AS name, "
                "CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS type, 0 AS ncol, 0 AS wr, 0 AS strict "
                "FROM information_schema.tables WHERE table_schema = 'public'"), ()
    if name == "foreign_key_list" and (argument or match[3]):
        table = target_table((argument or match[3]).strip("'\" "))
        return ("SELECT row_number() OVER () - 1 AS id, 0 AS seq, ccu.table_name AS \"table\", "
                "kcu.column_name AS \"from\", ccu.column_name AS \"to\", rc.update_rule AS on_update, "
                "rc.delete_rule AS on_delete, 'NONE' AS match "
                "FROM information_schema.referential_constraints rc "
                "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = rc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = rc.unique_constraint_name "
                f"WHERE kcu.table_name = '{table}'"), ()
    if name == "foreign_key_check":
        return "SELECT NULL AS \"table\" WHERE false", ()
    if name in _NO_OP_PRAGMAS or name in {"user_version", "schema_version", "data_version"}:
        return "SELECT 0 WHERE false", ()
    return None


_CATALOG_LOCK = threading.Lock()
_catalog_cache = {}


def _catalog(query):
    """{table: [column, ...]} for a (table, column) catalog query, cached per database."""
    key = (dsn(), query)
    with _CATALOG_LOCK:
        if key not in _catalog_cache:
            with psycopg.connect(dsn()) as conn:
                rows = conn.execute(query).fetchall()
            grouped = {}
            for table, column in rows:
                grouped.setdefault(table, []).append(column)
            _catalog_cache[key] = grouped
        return _catalog_cache[key]


def _integer_keys():
    """table -> its single bigint primary key column (what SQLite's rowid aliases)."""
    primary = ("SELECT c.table_name, c.column_name FROM information_schema.columns c "
               "JOIN information_schema.key_column_usage k ON k.table_name = c.table_name "
               "AND k.column_name = c.column_name JOIN information_schema.table_constraints t "
               "ON t.constraint_name = k.constraint_name AND t.constraint_type = 'PRIMARY KEY' "
               "WHERE c.table_schema = 'public'")
    keys, bigint_keys = _catalog(primary), _catalog(primary + " AND c.data_type = 'bigint'")
    return {table: columns[0] for table, columns in keys.items()
            if len(columns) == 1 and columns == bigint_keys.get(table)}


def _replace_to_upsert(tree):
    """INSERT OR REPLACE -> INSERT ... ON CONFLICT (primary key) DO UPDATE every other column."""
    table = target_table(tree.find(exp.Table).name)
    keys = _catalog("SELECT k.table_name, k.column_name FROM information_schema.key_column_usage k "
                    "JOIN information_schema.table_constraints t ON t.constraint_name = k.constraint_name "
                    "AND t.constraint_type = 'PRIMARY KEY' WHERE k.table_schema = 'public' "
                    "ORDER BY k.ordinal_position").get(table, [])
    target = tree.this.expressions if isinstance(tree.this, exp.Schema) else None
    names = [c.name for c in target] if target else _catalog(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name <> 'rowid' ORDER BY ordinal_position").get(table, [])
    tree.set("alternative", None)
    updates = [exp.EQ(this=exp.column(n), expression=exp.column(n, table="EXCLUDED")) for n in names if n not in keys]
    tree.set("conflict", exp.OnConflict(conflict_keys=[exp.to_identifier(k) for k in keys],
                                        action=exp.var("DO UPDATE" if updates else "DO NOTHING"),
                                        expressions=updates or None))


def _any_value_for_bare_columns(tree):
    """SQLite lets a grouped SELECT name columns outside GROUP BY (any row's value);
    PostgreSQL needs that said: ANY_VALUE(column)."""
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if not group:
            continue
        grouped = {g.sql() for g in group.expressions}
        aliases = {e.alias for e in select.expressions if e.alias}
        for expression in select.expressions:
            for column in list(expression.find_all(exp.Column)):
                if column.sql() in grouped or column.name in aliases or column.find_ancestor(exp.AggFunc, exp.Subquery):
                    continue
                if column.find_ancestor(exp.Select) is not select:
                    continue
                column.replace(exp.AnyValue(this=column.copy()))


def _drop_order_on_single_row_aggregates(tree):
    """SELECT agg(...) ... ORDER BY col (no GROUP BY) returns one row; SQLite ignores the
    ORDER BY, PostgreSQL rejects it."""
    for select in tree.find_all(exp.Select):
        order = select.args.get("order")
        if not order or select.args.get("group"):
            continue
        if any(e.find(exp.AggFunc) for e in select.expressions):
            select.set("order", None)


def _map_rowid(tree):
    """rowid / _rowid_ / oid -> the table's integer primary key."""
    columns = [c for c in tree.find_all(exp.Column) if c.name.lower() in {"rowid", "_rowid_", "oid"}]
    if not columns:
        return
    keys = _integer_keys()
    tables = {t.alias_or_name: target_table(t.name) for t in tree.find_all(exp.Table)}
    for column in columns:
        table = tables.get(column.table) if column.table else (next(iter(tables.values())) if len(tables) == 1 else None)
        if table and table in keys:
            column.set("this", exp.to_identifier(keys[table]))


@lru_cache(maxsize=20000)
def translate(sql):
    """SQLite statement -> (PostgreSQL statement with %s/%(name)s placeholders, returning key)."""
    pragma = _pragma(sql)
    if pragma:
        return pragma[0], None
    stripped = sql.strip().rstrip(";").strip()
    upper = stripped.upper()
    if upper in {"BEGIN", "BEGIN TRANSACTION", "BEGIN DEFERRED", "BEGIN DEFERRED TRANSACTION"}:
        return "BEGIN", None
    if upper.startswith("BEGIN IMMEDIATE") or upper.startswith("BEGIN EXCLUSIVE"):
        return "BEGIN; " + WRITER_LOCK, None
    if upper in {"COMMIT", "END", "END TRANSACTION", "COMMIT TRANSACTION"}:
        return "COMMIT", None
    savepoint = re.fullmatch(r"(SAVEPOINT|RELEASE(?: SAVEPOINT)?|ROLLBACK(?: TRANSACTION)? TO(?: SAVEPOINT)?)\s+(\w+)",
                             stripped, re.I)
    if savepoint:
        verb = savepoint[1].upper().split()[0]
        return f"{verb}:{savepoint[2]}", None
    if re.match(r"(CREATE|DROP)\s+TRIGGER\b", upper):
        # Triggers come from database/postgres/triggers.sql; runtime trigger DDL is SQLite schema setup.
        return "SELECT 1 WHERE false", None
    from database.unified import sql as without_legacy_prefixes
    tree = sqlglot.parse_one(without_legacy_prefixes(stripped), read="sqlite")
    if isinstance(tree, exp.Insert) and str(tree.args.get("alternative") or "").upper() == "REPLACE":
        _replace_to_upsert(tree)
    tree = _filter_merged(tree)
    _map_rowid(tree)
    _any_value_for_bare_columns(tree)
    _drop_order_on_single_row_aggregates(tree)
    tree = rewrite(tree)
    for table in tree.find_all(exp.Table):
        if table.name in DROPPED:
            raise sqlite3.OperationalError(f"no such table: {table.name} (not in the PostgreSQL schema)")
        table.set("this", exp.to_identifier(target_table(table.name)))
    returning = None
    if isinstance(tree, exp.Insert) and not tree.args.get("returning"):
        returning = tree.find(exp.Table).name
    # Placeholders become a marker so literal '%' can be escaped for the driver.
    for placeholder in tree.find_all(exp.Placeholder):
        name = placeholder.this or ""  # None for "?", the name for ":name"
        placeholder.replace(exp.Var(this=f"{_MARK}{name}{_MARK}"))
    text = tree.sql(dialect="postgres").replace("%", "%%")
    text = re.sub(re.escape(_MARK) + r"(\w*)" + re.escape(_MARK),
                  lambda m: f"%({m[1]})s" if m[1] else "%s", text)
    return text, returning


# --------------------------------------------------------------------------- errors

def _sqlite_error(error):
    message = str(error).splitlines()[0] if str(error) else type(error).__name__
    if isinstance(error, (psycopg.errors.IntegrityError,)) or getattr(error, "sqlstate", "") == "23514":
        return sqlite3.IntegrityError(message)
    if isinstance(error, (psycopg.errors.LockNotAvailable, psycopg.errors.SerializationFailure,
                          psycopg.errors.DeadlockDetected, psycopg.errors.QueryCanceled)):
        return sqlite3.OperationalError(f"database is locked: {message}")
    if isinstance(error, psycopg.errors.UndefinedTable):
        return sqlite3.OperationalError(f"no such table: {message}")
    if isinstance(error, psycopg.errors.UndefinedColumn):
        return sqlite3.OperationalError(f"no such column: {message}")
    if isinstance(error, psycopg.errors.DataError):
        return sqlite3.IntegrityError(message)
    if isinstance(error, psycopg.Error):
        return sqlite3.OperationalError(message)
    return error


# --------------------------------------------------------------------------- connection

class PgCursor:
    def __init__(self, connection):
        self.connection = connection
        self._cursor = connection._raw.cursor()
        self.lastrowid = None
        self.rowcount = -1
        self.description = None
        self._rows = None
        self._hide_rowid = False

    def _params(self, parameters):
        if isinstance(parameters, dict):
            return {key: _param(value) for key, value in parameters.items()}
        return tuple(_param(value) for value in (parameters or ()))

    def execute(self, sql, parameters=()):
        connection = self.connection
        text, returning = translate(sql)
        if text == "BEGIN" or text.startswith("BEGIN;"):
            connection._begin(immediate=text.startswith("BEGIN;"))
            return self
        if text == "COMMIT":
            connection.commit()
            return self
        if text.startswith(("SAVEPOINT:", "RELEASE:", "ROLLBACK:")):
            connection._savepoint(*text.split(":", 1))
            return self
        is_write = not text.lstrip().upper().startswith(("SELECT", "WITH", "VALUES"))
        if is_write and connection.isolation_level is not None and not connection.in_transaction:
            connection._begin(immediate=False)  # sqlite3 legacy: implicit BEGIN before DML
        key = connection._identity(returning) if returning else None
        if key:
            text = f"{text} RETURNING \"{key}\""
        try:
            self._cursor.execute(text, self._params(parameters))
        except psycopg.Error as error:
            raise _sqlite_error(error) from error
        self.rowcount = self._cursor.rowcount
        self.description = self._cursor.description
        # The rowid column PostgreSQL tables carry for SQLite code shows up only when asked for.
        self._hide_rowid = "rowid" not in sql.lower()
        if self.description and self._hide_rowid and any(d.name == "rowid" for d in self.description):
            self.description = tuple(d for d in self.description if d.name != "rowid")
        if key:
            row = self._cursor.fetchone()
            self.lastrowid = row[0] if row else None
            self.description = None
        return self

    def executemany(self, sql, seq):
        total = 0
        for parameters in seq:
            self.execute(sql, parameters)
            total += max(self.rowcount, 0)
        self.rowcount = total
        return self

    def _convert(self, row):
        names = [d.name for d in self.description or ()]
        if self._hide_rowid and "rowid" in names:
            index = names.index("rowid")
            row, names = row[:index] + row[index + 1:], names[:index] + names[index + 1:]
        values = tuple(_decode(v) for v in row)
        factory = self.connection.row_factory
        if factory is None:
            return values
        if factory is sqlite3.Row or factory is Row:
            return Row(names, values)
        return factory(self, values)

    def fetchone(self):
        if self.description is None:
            return None
        row = self._cursor.fetchone()
        return None if row is None else self._convert(row)

    def fetchall(self):
        if self.description is None:
            return []
        return [self._convert(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=100):
        return [self._convert(row) for row in self._cursor.fetchmany(size)]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self):
        self._cursor.close()


def _param(value):
    from functions.exact_money import ExactDecimal
    if isinstance(value, ExactDecimal):
        return Decimal(str(value))
    if isinstance(value, str) and value.startswith(("~M~", "~R~")):
        return Decimal(value[3:])
    return value


class PgConnection:
    """The subset of sqlite3.Connection the app uses."""

    def __init__(self, conninfo=None, *, readonly=False):
        # Client-side binding: SQLite code passes parameters PostgreSQL cannot type on its
        # own (``? IS NULL``), and values are sent as literals either way.
        self._raw = psycopg.connect(conninfo or dsn(), autocommit=True, cursor_factory=psycopg.ClientCursor)
        self._lock = threading.RLock()
        self.isolation_level = ""
        self.row_factory = None
        self.readonly = readonly
        self.legacy_aliases = True
        self.wide_money = True
        self._in_transaction = False
        self._identities = {}

    # transactions -------------------------------------------------------
    @property
    def in_transaction(self):
        return self._in_transaction

    def _begin(self, immediate):
        if not self._in_transaction:
            self._raw.execute("BEGIN")
            self._in_transaction = True
        if immediate:
            self._raw.execute(WRITER_LOCK)

    def _savepoint(self, verb, name):
        """SQLite semantics: SAVEPOINT outside a transaction starts one, and releasing
        that outermost savepoint commits it."""
        if verb == "SAVEPOINT":
            if not self._in_transaction:
                self._begin(immediate=False)
                self._savepoint_root = name
            self._raw.execute(f'SAVEPOINT "{name}"')
        elif verb == "RELEASE":
            if getattr(self, "_savepoint_root", None) == name:
                self._savepoint_root = None
                self.commit()
            else:
                self._raw.execute(f'RELEASE SAVEPOINT "{name}"')
        else:
            self._raw.execute(f'ROLLBACK TO SAVEPOINT "{name}"')

    def commit(self):
        if self._in_transaction:
            try:
                self._raw.execute("COMMIT")
            except psycopg.Error as error:
                raise _sqlite_error(error) from error
            finally:
                self._in_transaction = False

    def rollback(self):
        if self._in_transaction:
            self._raw.execute("ROLLBACK")
            self._in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.commit()
        else:
            self.rollback()
        return False

    # statements ---------------------------------------------------------
    def cursor(self, factory=None):
        return PgCursor(self)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, script):
        self.commit()
        for statement in sqlglot.transpile(script, read="sqlite", write="sqlite"):
            if statement.strip():
                self.execute(statement)
        self.commit()

    def _identity(self, table):
        table = target_table(table)
        if table not in self._identities:
            row = self._raw.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name=%s AND is_identity='YES'", (table,)).fetchone()
            self._identities[table] = row[0] if row else None
        return self._identities[table]

    # SQLite-only hooks ---------------------------------------------------
    def create_function(self, name, *args, **kwargs):
        """SQL functions exist in the PostgreSQL schema (database/postgres/triggers.sql)."""

    def create_aggregate(self, *args, **kwargs):
        pass

    def create_collation(self, *args, **kwargs):
        pass

    def set_authorizer(self, *args, **kwargs):
        pass

    def close(self):
        try:
            self.rollback()
        finally:
            self._raw.close()


def connect(*, readonly=False):
    return PgConnection(readonly=readonly)
