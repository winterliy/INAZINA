"""Load a copy of inazina.db into PostgreSQL using database/postgres/schema.sql.

Usage: .venv/bin/python tools/sqlite_to_postgres.py SOURCE.db "postgresql:///inazina_dev"

Never point SOURCE at the production file while services run; make a backup copy
first (sqlite3 .backup). The target schema "public" is dropped and rebuilt from
the generated schema (tools/build_postgres_schema.py), and rows follow its table
consolidation: renamed, folded and merged tables, dropped duplicates.
"""
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_postgres_schema import DROPPED, FOLDED, MERGED, RENAMED, build  # noqa: E402

WIDE_TAGS = ("~M~", "~R~")


def quoted(name):
    return '"' + str(name).replace('"', '""') + '"'


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def converter(pg_type):
    if pg_type == "numeric":
        def money(value):
            value = _text(value)
            if value is None:
                return None
            if isinstance(value, str) and value.startswith(WIDE_TAGS):
                value = value[3:]
            return Decimal(str(value))
        return money
    if pg_type == "text":
        return lambda value: None if value is None else str(_text(value))
    if pg_type == "bigint":
        return lambda value: None if value is None else int(Decimal(str(_text(value))))
    if pg_type == "double precision":
        return lambda value: None if value is None else float(str(_text(value)).removeprefix("~R~").removeprefix("~M~"))
    return _text


# Per source table: (target table, function(row dict) -> row dict) for consolidated tables.
def _fold_rate(row):
    row.pop("history_id", None)
    return {**row, "nationality": "*"}


ROW_MAPPERS = {
    "rate_history": _fold_rate,
    "saving": lambda row: {**row, "kind": "deposit"},
    "liability": lambda row: {**row, "kind": "loan"},
    "board_post_reactions": lambda row: row,
    "board_comment_reactions": lambda row: row,
}


def load(source_path, dsn):
    schema_sql, _ = build(source_path)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    report = []
    with psycopg.connect(dsn) as target:
        target.execute("DROP SCHEMA IF EXISTS public CASCADE")
        target.execute("CREATE SCHEMA public")
        target.execute(schema_sql)
        # Rows arrive table by table; foreign keys and triggers are checked by the
        # schema once loaded, not row by row in load order.
        target.execute("SET session_replication_role = replica")
        pg_columns = {}
        for table, column, data_type in target.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public'"):
            pg_columns.setdefault(table, {})[column] = data_type
        for (name,) in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            if name in DROPPED:
                continue
            table = RENAMED.get(name) or FOLDED.get(name) or MERGED.get(name) or name
            mapper = ROW_MAPPERS.get(name, lambda row: row)
            columns = pg_columns[table]
            rows = [mapper(dict(row)) for row in source.execute(f"SELECT * FROM {quoted(name)}")]
            if not rows:
                report.append((name, table, 0))
                continue
            names = [c for c in rows[0] if c in columns]
            convert = [converter(columns[c]) for c in names]
            with target.cursor().copy(
                    f"COPY {quoted(table)} ({', '.join(map(quoted, names))}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row([fn(row[c]) for fn, c in zip(convert, names)])
            report.append((name, table, len(rows)))
        target.execute("SET session_replication_role = origin")
        # Identity columns continue after the copied ids.
        for table, column in target.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND is_identity='YES'").fetchall():
            target.execute(f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                           f"COALESCE((SELECT MAX({quoted(column)}) FROM {quoted(table)}), 0) + 1, false)",
                           (table, column))
        target.execute("ANALYZE")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    rows = load(sys.argv[1], sys.argv[2])
    print(f"{len(rows)} source tables, {sum(count for *_, count in rows)} rows")
