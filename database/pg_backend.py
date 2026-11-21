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
from psycopg import sql as pg_sql
from sqlglot import exp

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
from build_postgres_schema import DROPPED, RENAMED, rewrite, target_table  # noqa: E402
from build_postgres_triggers import ROW_FILTER, _filter_merged  # noqa: E402

DSN_ENV = "INAZINA_PG_DSN"
_MARK = "\x00P\x00"
# BEGIN IMMEDIATE reserves SQLite's single writer; one advisory lock does the same.
WRITER_LOCK = "SELECT pg_advisory_xact_lock(4243)"
_NO_OP_PRAGMAS = {"foreign_keys", "busy_timeout", "query_only", "journal_mode", "synchronous",
                  "legacy_alter_table", "optimize", "wal_checkpoint", "defer_foreign_keys",
                  "cache_size", "temp_store", "mmap_size", "recursive_triggers"}


# SQLite support ended on 2026-11-20: PostgreSQL is the only database the app runs on.
# SQLite is still used for one thing, as the in-process tool that turns the code's schema definition into the PostgreSQL
# schema (tools/build_postgres_schema.py, database/pg_schema_sync.py, the test template). Those callers select it with the
# internal value "sqlite-schema"; a plain "sqlite" is refused so nobody starts the app on it by mistake.
#
# (the old switch, kept for reference)
# def enabled():
#     return os.environ.get("INAZINA_DB_BACKEND", "sqlite").lower() == "postgres"
SCHEMA_TOOL = "sqlite-schema"


def backend():
    """"postgres" (the default) or "sqlite-schema" (the schema-building tool only); anything else is refused."""
    value = (os.environ.get("INAZINA_DB_BACKEND") or "postgres").strip().lower()
    if value in ("postgres", "postgresql"):
        return "postgres"
    if value == SCHEMA_TOOL:
        return SCHEMA_TOOL
    raise RuntimeError(f"INAZINA_DB_BACKEND={value!r} is no longer supported: SQLite support ended on 2026-11-20 and the app runs on "
                       "PostgreSQL only. Remove the setting or use INAZINA_DB_BACKEND=postgres (see docs/postgres-cutover-runbook.md).")


def enabled():
    return backend() == "postgres"


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
            # Case-insensitive like sqlite3.Row, and IndexError for an unknown name (callers catch it).
            lowered = key.lower()
            for index, name in enumerate(self._names):
                if name.lower() == lowered:
                    return tuple.__getitem__(self, index)
            raise IndexError("No item with that key")
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
    if name == "busy_timeout":
        if argument is None and match[3] is None:
            return "SELECT setting::bigint AS timeout FROM pg_settings WHERE name='lock_timeout'", ()
        value = (argument or match[3]).strip()
        if re.fullmatch(r"-?\d+", value):
            # PostgreSQL uses zero to disable the timeout; SQLite uses zero to
            # fail immediately, approximated here by one millisecond.
            return f"SELECT set_config('lock_timeout', '{max(1, int(value))}ms', false)", ()
    if name == "foreign_keys" and argument is None and match[3] is None:
        # PostgreSQL enforces foreign keys unless this session has disabled
        # constraint triggers via its replication role.
        return ("SELECT CASE WHEN current_setting('session_replication_role') = 'replica' "
                "THEN 0 ELSE 1 END AS foreign_keys"), ()
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


def _nullable_names():
    """Column names that are nullable in at least one table or view. Anything else is NOT NULL wherever it appears."""
    query = ("SELECT 'c', column_name FROM information_schema.columns "
             "WHERE table_schema = 'public' AND is_nullable = 'YES'")
    return set(_catalog(query).get("c", []))


def _natural_null_order(tree):
    """SQLite sorts NULL as the smallest value, PostgreSQL as the largest, so the translation spells the order out
    (`ASC NULLS FIRST`). For a column that can never be NULL that changes nothing, but it is no longer the order a
    plain index delivers, so `ORDER BY recorded_epoch LIMIT 1` scanned the whole table (~0.3s each, 190 times for
    one dashboard page). Drop the clause where the column cannot hold NULL."""
    ordered = [o for o in tree.find_all(exp.Ordered) if isinstance(o.this, exp.Column)]
    if not ordered:
        return
    nullable = _nullable_names()
    for item in ordered:
        if item.this.name not in nullable:
            item.set("nulls_first", bool(item.args.get("desc")))


def _replace_to_upsert(tree):
    """INSERT OR REPLACE with SQLite's result: on a primary-key conflict the row is replaced,
    so listed columns take the new values and every other column returns to its DEFAULT
    (a new rowid included) instead of keeping the old row's value.

    Differences that remain: SQLite also replaces rows that conflict on another UNIQUE
    constraint, and fires no DELETE triggers for the replaced row; here the key is the
    primary key and UPDATE triggers fire."""
    source = tree.find(exp.Table).name
    table = target_table(source)
    keys = _catalog("SELECT k.table_name, k.column_name FROM information_schema.key_column_usage k "
                    "JOIN information_schema.table_constraints t ON t.constraint_name = k.constraint_name "
                    "AND t.constraint_type = 'PRIMARY KEY' WHERE k.table_schema = 'public' "
                    "ORDER BY k.ordinal_position").get(table, [])
    every = _catalog("SELECT table_name, column_name FROM information_schema.columns "
                     "WHERE table_schema = 'public' ORDER BY ordinal_position").get(table, [])
    if not keys:
        # No primary key: a view (its INSTEAD OF trigger does the replacing) or a
        # keyless table, where SQLite's REPLACE has nothing to conflict with.
        tree.set("alternative", None)
        return
    target = tree.this.expressions if isinstance(tree.this, exp.Schema) else None
    listed = [c.name for c in target] if target else [c for c in every if c != "rowid"]
    if source in ROW_FILTER:
        listed.append(ROW_FILTER[source][0])  # the merged table's kind column comes with the insert
    tree.set("alternative", None)
    updates = [exp.EQ(this=exp.column(n), expression=exp.column(n, table="EXCLUDED"))
               for n in listed if n not in keys]
    updates += [exp.EQ(this=exp.column(n), expression=exp.var("DEFAULT"))
                for n in every if n not in keys and n not in listed]
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
        for position, expression in enumerate(list(select.expressions), start=1):
            if expression.alias in grouped or str(position) in grouped:
                continue  # GROUP BY names this expression (by alias or position): it is the key
            if isinstance(expression, exp.Column) and not expression.find_ancestor(exp.AggFunc, exp.Subquery) \
                    and expression.sql() not in grouped and expression.name not in aliases:
                # A bare column keeps its name in the result row (row['issuer']), as in SQLite.
                expression.replace(exp.alias_(exp.AnyValue(this=expression.copy()), expression.name))
                continue
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
    without_comments = re.sub(r"^(?:\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/))*\s*", "", stripped, flags=re.S)
    if re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+board_(?:post|comment)_reactions\b", without_comments, re.I):
        # The board can be initialized after migration, when neither SQLite
        # reaction table was present in the source database. Create the merged
        # PostgreSQL table in full rather than whichever half is seen first.
        return ("CREATE TABLE IF NOT EXISTS board_reactions ("
                "reaction_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                "post_id BIGINT REFERENCES board_posts(post_id) ON DELETE CASCADE, "
                "comment_id BIGINT REFERENCES board_comments(comment_id) ON DELETE CASCADE, "
                "user_id TEXT NOT NULL REFERENCES users(user_id), "
                "value BIGINT NOT NULL CHECK (value IN (1, -1)), created_epoch BIGINT NOT NULL, "
                "CHECK ((post_id IS NULL) <> (comment_id IS NULL))); "
                "CREATE UNIQUE INDEX IF NOT EXISTS board_reactions_post_user "
                "ON board_reactions (post_id, user_id) WHERE post_id IS NOT NULL; "
                "CREATE UNIQUE INDEX IF NOT EXISTS board_reactions_comment_user "
                "ON board_reactions (comment_id, user_id) WHERE comment_id IS NOT NULL"), None
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
    abort = re.fullmatch(r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+"
                         r"BEFORE\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\w+)\s+(?:FOR\s+EACH\s+ROW\s+)?"
                         r"BEGIN\s+SELECT\s+RAISE\s*\(\s*ABORT\s*,\s*'((?:[^']|'')*)'\s*\)\s*;\s*END;?", stripped, re.I | re.S)
    if abort:
        # A runtime "refuse this" trigger (a test's injected failure, or a guard put
        # back after a fixture edit): the same rule with the shared raise_abort().
        # PostgreSQL has no temporary triggers; it lives until dropped or rolled back.
        name, event, table, message = abort.groups()
        if table in DROPPED:
            return "SELECT 1 WHERE false", None
        return (f'DROP TRIGGER IF EXISTS "{name}" ON "{target_table(table)}"; '
                f'CREATE TRIGGER "{name}" BEFORE {event.upper()} ON "{target_table(table)}" '
                f"FOR EACH ROW EXECUTE FUNCTION raise_abort('{message}')"), None
    dropped = re.fullmatch(r"DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?(\w+)\s*;?", stripped, re.I)
    if dropped:
        # PostgreSQL drops a trigger on a named table; find the table it is on. Only
        # tests do this at runtime: PostgreSQL mode never runs the SQLite schema setup.
        name = dropped.group(1)
        return ("DO $drop$ DECLARE t text; BEGIN "
                f"SELECT c.relname INTO t FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
                f"WHERE g.tgname = '{name}' AND NOT g.tgisinternal LIMIT 1; "
                f"IF t IS NOT NULL THEN EXECUTE 'DROP TRIGGER ' || quote_ident('{name}') || ' ON ' || quote_ident(t); END IF; "
                "END $drop$"), None
    if re.match(r"(CREATE|DROP)\s+TRIGGER\b", upper):
        # Triggers come from database/postgres/triggers.sql; runtime trigger DDL is SQLite schema setup.
        return "SELECT 1 WHERE false", None
    from database.unified import sql as without_legacy_prefixes
    tree = sqlglot.parse_one(without_legacy_prefixes(stripped), read="sqlite")
    if isinstance(tree, exp.Insert) and str(tree.args.get("alternative") or "").upper() == "REPLACE":
        _replace_to_upsert(tree)
    if isinstance(tree, exp.Insert) and tree.args.get("conflict"):
        table = tree.find(exp.Table)
        conflict = tree.args["conflict"]
        reaction_key = {"board_post_reactions": "post_id", "board_comment_reactions": "comment_id"}.get(table.name)
        keys = conflict.args.get("conflict_keys") or []
        key_names = {column.name for key in keys for column in key.find_all(exp.Column)}
        if reaction_key and key_names == {reaction_key, "user_id"} and not conflict.args.get("index_predicate"):
            # The merged table has a partial unique index for each reaction kind.
            conflict.set("index_predicate", exp.Where(this=exp.Is(
                this=exp.column(reaction_key), expression=exp.Null(), negate=True)))
    tree = _filter_merged(tree)
    _map_rowid(tree)
    _any_value_for_bare_columns(tree)
    _drop_order_on_single_row_aggregates(tree)
    # SQLite stores JSON in text columns; PostgreSQL's JSON extraction
    # functions require a JSON value instead of accepting text implicitly.
    for extract in tree.find_all(exp.JSONExtractScalar):
        extract.set("this", exp.cast(extract.this.copy(), "JSON"))
    tree = rewrite(tree)
    _natural_null_order(tree)
    if isinstance(tree, exp.Create) and tree.args.get("exists") and tree.args.get("kind") == "TABLE":
        created = tree.find(exp.Table)
        if created is not None and created.name in DROPPED:
            # Legacy SQLite startup can be invoked again after migration. Do not
            # recreate obsolete tables which PostgreSQL deliberately removed.
            return "SELECT 1 WHERE false", None
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


def _legacy_names_in_catalog_query(sql, parameters):
    """`SELECT 1 FROM sqlite_master WHERE type='table' AND name=?` asks whether a table exists. Tables that were
    renamed or merged (saving, liability -> bank_products, ...) exist under their new name only, so the old name
    is looked up as the new one. Without this every "is the saving table there?" guard answered no and the
    savings and loan product lists came back empty."""
    def mapped(value):
        return target_table(value) if isinstance(value, str) else value
    if isinstance(parameters, (tuple, list)):
        parameters = tuple(mapped(value) for value in parameters)
    sql = re.sub(r"(\bname\s*=\s*')(\w+)(')", lambda m: m[1] + target_table(m[2]) + m[3], sql, flags=re.I)
    return sql, parameters


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

def _foreign_key_check_sql(raw, table=None):
    """Build an audit query for declared foreign keys, including preexisting bad rows."""
    constraints = raw.execute("""
        WITH keys AS (
          SELECT c.*, row_number() OVER (PARTITION BY c.conrelid ORDER BY c.oid) - 1 AS key_id
          FROM pg_constraint c WHERE c.contype = 'f'
        )
        SELECT child.relname, parent_ns.nspname, parent.relname, keys.key_id, keys.confmatchtype,
          ARRAY(SELECT a.attname FROM unnest(keys.conkey) WITH ORDINALITY AS k(n, ord)
                JOIN pg_attribute a ON a.attrelid=keys.conrelid AND a.attnum=k.n ORDER BY k.ord),
          ARRAY(SELECT a.attname FROM unnest(keys.confkey) WITH ORDINALITY AS k(n, ord)
                JOIN pg_attribute a ON a.attrelid=keys.confrelid AND a.attnum=k.n ORDER BY k.ord),
          COALESCE(
            (SELECT a.attname FROM pg_attribute a WHERE a.attrelid=keys.conrelid
             AND a.attname='rowid' AND NOT a.attisdropped),
            (SELECT a.attname FROM pg_index i JOIN pg_attribute a
             ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0]
             WHERE i.indrelid=keys.conrelid AND i.indisprimary AND i.indnkeyatts=1
             AND a.atttypid IN ('int2'::regtype, 'int4'::regtype, 'int8'::regtype)))
        FROM keys JOIN pg_class child ON child.oid=keys.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=keys.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
        WHERE child_ns.nspname='public' AND (%s::text IS NULL OR child.relname=%s)
        ORDER BY child.relname, keys.key_id
    """, (table, table)).fetchall()
    checks = []
    legacy_names = {target: source for source, target in RENAMED.items()}
    for child, parent_schema, parent, key_id, match_type, child_cols, parent_cols, rowid_col in constraints:
        pairs = [pg_sql.SQL('p.{} = c.{}').format(pg_sql.Identifier(p), pg_sql.Identifier(c))
                 for c, p in zip(child_cols, parent_cols)]
        nonnull = [pg_sql.SQL('c.{} IS NOT NULL').format(pg_sql.Identifier(c)) for c in child_cols]
        missing = pg_sql.SQL('({}) AND NOT EXISTS (SELECT 1 FROM {}.{} p WHERE {})').format(
            pg_sql.SQL(' AND ').join(nonnull), pg_sql.Identifier(parent_schema),
            pg_sql.Identifier(parent), pg_sql.SQL(' AND ').join(pairs))
        if match_type == 'f' and len(child_cols) > 1:
            nulls = [pg_sql.SQL('c.{} IS NULL').format(pg_sql.Identifier(c)) for c in child_cols]
            missing = pg_sql.SQL('({}) OR (({}) AND ({}))').format(
                missing, pg_sql.SQL(' OR ').join(nulls), pg_sql.SQL(' OR ').join(nonnull))
        rowid = pg_sql.SQL('c.{}::bigint').format(pg_sql.Identifier(rowid_col)) if rowid_col else pg_sql.SQL('NULL::bigint')
        checks.append(pg_sql.SQL('SELECT {} AS "table", {} AS rowid, {} AS parent, '
                                 '{}::bigint AS fkid FROM public.{} c WHERE {}').format(
            pg_sql.Literal(legacy_names.get(child, child)), rowid,
            pg_sql.Literal(legacy_names.get(parent, parent)),
            pg_sql.Literal(key_id),
            pg_sql.Identifier(child), missing))
    if not checks:
        return 'SELECT NULL AS "table", NULL::bigint AS rowid, NULL AS parent, NULL::bigint AS fkid WHERE false'
    return pg_sql.SQL(' UNION ALL ').join(checks).as_string(raw)


class PgCursor:
    def __init__(self, connection):
        self.connection = connection
        self._cursor = connection._raw.cursor()
        self.lastrowid = None
        self.rowcount = -1
        self.description = None
        self._names, self._rows, self._next = None, [], 0

    def _params(self, parameters):
        if isinstance(parameters, dict):
            return {key: _param(value) for key, value in parameters.items()}
        return tuple(_param(value) for value in (parameters or ()))

    def execute(self, sql, parameters=()):
        connection = self.connection
        if connection._trace is not None:
            connection._trace(sql)
        foreign_key_check = re.fullmatch(
            r"\s*PRAGMA\s+(?:main\.)?foreign_key_check\s*(?:\(\s*['\"]?([\w.]+)['\"]?\s*\))?\s*;?\s*",
            sql, re.I)
        if foreign_key_check:
            table = target_table(foreign_key_check[1]) if foreign_key_check[1] else None
            text = _foreign_key_check_sql(connection._raw, table)
            returning = None
        else:
            if "sqlite_master" in sql.lower():
                sql, parameters = _legacy_names_in_catalog_query(sql, parameters)
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
        # SQLite undoes only the failed statement and keeps the transaction usable;
        # PostgreSQL aborts the whole transaction. A savepoint per statement keeps
        # SQLite's behavior for callers that catch an error and carry on.
        guarded = connection.in_transaction
        cursor = self._cursor
        try:
            if guarded:
                cursor.execute("SAVEPOINT inazina_statement")
            cursor.execute(text, self._params(parameters))
            names = [d.name for d in cursor.description] if cursor.description else None
            rows = cursor.fetchall() if names is not None else []
            self.rowcount = cursor.rowcount
            if guarded:
                cursor.execute("RELEASE SAVEPOINT inazina_statement")
        except psycopg.Error as error:
            if guarded:
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT inazina_statement")
                except psycopg.Error:
                    pass
            raise _sqlite_error(error) from error
        if key:
            self.lastrowid = rows[0][0] if rows else None
            names, rows = None, []
        # The rowid column PostgreSQL tables carry for SQLite code shows up only when asked for.
        if names is not None and "rowid" in names and "rowid" not in sql.lower() and not foreign_key_check:
            index = names.index("rowid")
            names = names[:index] + names[index + 1:]
            rows = [row[:index] + row[index + 1:] for row in rows]
        self._names, self._rows, self._next = names, rows, 0
        self.description = None if names is None else tuple((n, None, None, None, None, None, None) for n in names)
        return self

    def _batch_text(self, sql):
        """The PostgreSQL text of a plain INSERT/UPDATE/DELETE that a batch can send as one pipeline; None for anything else
        (transaction control, SELECTs, catalog reads). The RETURNING that `execute` adds for lastrowid is left off: nothing reads
        lastrowid after executemany (callers use rowcount)."""
        if "sqlite_master" in sql.lower() or re.match(r"\s*PRAGMA\b", sql, re.I):
            return None
        text, _returning = translate(sql)
        if text == "BEGIN" or text == "COMMIT" or text.startswith(("BEGIN;", "SAVEPOINT:", "RELEASE:", "ROLLBACK:")):
            return None
        return None if text.lstrip().upper().startswith(("SELECT", "WITH", "VALUES")) else text

    def executemany(self, sql, seq):
        batch = list(seq)
        connection = self.connection
        # Inside a transaction a batch is one savepoint and one pipelined executemany instead of three round trips per row.
        # Outside one (autocommit) every row stays its own statement, as before.
        text = self._batch_text(sql) if len(batch) > 1 and connection.in_transaction else None
        if text is not None:
            if connection._trace is not None:
                for _ in batch:
                    connection._trace(sql)
            cursor = self._cursor
            try:
                cursor.execute("SAVEPOINT inazina_batch")
                cursor.executemany(text, [self._params(parameters) for parameters in batch])
                total = max(cursor.rowcount, 0)
                cursor.execute("RELEASE SAVEPOINT inazina_batch")
            except psycopg.Error:
                # Undo the whole batch, then replay it row by row: the rows before the failing one stay applied and the error is
                # the one the failing statement raises, exactly as when every row was its own statement.
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT inazina_batch")
                    cursor.execute("RELEASE SAVEPOINT inazina_batch")
                except psycopg.Error:
                    pass
            else:
                self._names, self._rows, self._next, self.description, self.lastrowid = None, [], 0, None, None
                self.rowcount = total
                return self
        total = 0
        for parameters in batch:
            self.execute(sql, parameters)
            total += max(self.rowcount, 0)
        self.rowcount = total
        return self

    def _convert(self, row):
        values = tuple(_decode(v) for v in row)
        factory = self.connection.row_factory
        if factory is None:
            return values
        if factory is sqlite3.Row or factory is Row:
            return Row(self._names, values)
        return factory(self, values)

    def _take(self, count=None):
        end = len(self._rows) if count is None else min(len(self._rows), self._next + count)
        taken = self._rows[self._next:end]
        self._next = end
        return [self._convert(row) for row in taken]

    def fetchone(self):
        taken = self._take(1)
        return taken[0] if taken else None

    def fetchall(self):
        return self._take()

    def fetchmany(self, size=100):
        return self._take(size)

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
        self._trace = None

    # transactions -------------------------------------------------------
    @property
    def in_transaction(self):
        return self._in_transaction

    def _begin(self, immediate):
        started = not self._in_transaction
        if started:
            # A snapshot reader must reject writes like SQLite's query_only does, and (as on SQLite) see one
            # consistent snapshot for the whole read: with READ COMMITTED a long read such as the ledger
            # reconciliation saw an order open in one statement and cancelled in the next while LP quotes churn.
            self._raw.execute("BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ" if self.readonly else "BEGIN")
            self._in_transaction = True
        if immediate:
            try:
                if not started:
                    self._raw.execute("SAVEPOINT inazina_writer_lock")
                self._raw.execute(WRITER_LOCK)
                if not started:
                    self._raw.execute("RELEASE SAVEPOINT inazina_writer_lock")
            except psycopg.Error as error:
                if started:
                    self._raw.execute("ROLLBACK")
                    self._in_transaction = False
                else:
                    self._raw.execute("ROLLBACK TO SAVEPOINT inazina_writer_lock")
                    self._raw.execute("RELEASE SAVEPOINT inazina_writer_lock")
                raise _sqlite_error(error) from error

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

    def set_trace_callback(self, callback):
        """Called with each statement as written by the caller (sqlite3 passes the SQL it runs)."""
        self._trace = callback

    def close(self):
        try:
            self.rollback()
        finally:
            self._raw.close()


def connect(*, readonly=False):
    return PgConnection(readonly=readonly)
