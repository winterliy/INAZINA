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
    if name == "foreign_key_check":
        return "SELECT NULL AS \"table\" WHERE false", ()
    if name in _NO_OP_PRAGMAS or name in {"user_version", "schema_version", "data_version"}:
        return "SELECT 0 WHERE false", ()
    return None


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
    tree = _filter_merged(sqlglot.parse_one(stripped, read="sqlite"))
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
        values = tuple(_decode(v) for v in row)
        factory = self.connection.row_factory
        if factory is None:
            return values
        names = [d.name for d in self.description or ()]
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
        self._raw = psycopg.connect(conninfo or dsn(), autocommit=True)
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
