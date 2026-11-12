"""Load a copy of inazina.db into PostgreSQL for the gradual backend switch.

Usage: .venv/bin/python tools/sqlite_to_postgres.py SOURCE.db "postgresql:///inazina_dev"

Never point SOURCE at the production file while services run; make a backup copy
first (sqlite3 .backup). The target schema "public" is dropped and rebuilt.

Copied: tables, rows, primary keys, UNIQUE constraints and plain column indexes.
Not yet: triggers, views, partial/expression indexes, CHECK constraints. Exact
money columns (MONEY/MONEYUNITS/MONEYREAL) become NUMERIC; their "~M~" wide tags
are decoded to plain numbers.
"""
import hashlib
import sqlite3
import sys
from decimal import Decimal

import psycopg

WIDE_TAGS = ("~M~", "~R~")


def quoted(name):
    return '"' + str(name).replace('"', '""') + '"'


def column_type(conn, table, column, declared):
    declared = (declared or "").upper()
    if declared.startswith("MONEY"):  # MONEY, MONEYUNITS, MONEYREAL
        return "numeric"
    kinds = {row[0] for row in conn.execute(
        f"SELECT DISTINCT typeof({quoted(column)}) FROM {quoted(table)}")} - {"null"}
    if declared.startswith("INT") and kinds <= {"integer"}:
        return "bigint"
    if declared in {"REAL", "FLOAT", "DOUBLE"} and kinds <= {"real", "integer"}:
        return "double precision"
    if kinds and kinds <= {"integer"} and not declared:
        return "bigint"
    if kinds == {"blob"}:
        return "bytea"
    return "text"


def converter(kind):
    if kind == "numeric":
        def money(value):
            if value is None:
                return None
            if isinstance(value, bytes):
                value = value.decode()
            if isinstance(value, str) and value.startswith(WIDE_TAGS):
                value = value[3:]
            return Decimal(str(value))
        return money
    if kind == "text":
        return lambda value: None if value is None else (value.decode() if isinstance(value, bytes) else str(value))
    return lambda value: value


def tables(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def load(source_path, dsn):
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    report = []
    with psycopg.connect(dsn) as target:
        target.execute("DROP SCHEMA IF EXISTS public CASCADE")
        target.execute("CREATE SCHEMA public")
        for table in tables(source):
            info = list(source.execute(f"PRAGMA table_info({quoted(table)})"))
            kinds = [column_type(source, table, row[1], row[2]) for row in info]
            pk = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
            columns = [f"{quoted(row[1])} {kind}" for row, kind in zip(info, kinds)]
            if pk:
                columns.append(f"PRIMARY KEY ({', '.join(map(quoted, pk))})")
            target.execute(f"CREATE TABLE {quoted(table)} ({', '.join(columns)})")
            convert = [converter(kind) for kind in kinds]
            names = ", ".join(quoted(row[1]) for row in info)
            count = 0
            with target.cursor().copy(f"COPY {quoted(table)} ({names}) FROM STDIN") as copy:
                for row in source.execute(f"SELECT {names} FROM {quoted(table)}"):
                    copy.write_row([fn(value) for fn, value in zip(convert, row)])
                    count += 1
            for index in source.execute(f"PRAGMA index_list({quoted(table)})"):
                name, unique, origin, partial = index[1], index[2], index[3], index[4]
                if partial or origin == "pk":
                    continue
                cols = [row[2] for row in source.execute(f"PRAGMA index_info({quoted(name)})")]
                if not cols or None in cols:
                    continue  # expression index
                full = f"{table}__{name}"
                pg_name = quoted(full if len(full) <= 63 else full[:54] + "_" + hashlib.sha1(full.encode()).hexdigest()[:8])
                target.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {pg_name} "
                               f"ON {quoted(table)} ({', '.join(map(quoted, cols))})")
            report.append((table, count))
        target.execute("ANALYZE")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    rows = load(sys.argv[1], sys.argv[2])
    print(f"{len(rows)} tables, {sum(count for _, count in rows)} rows")
