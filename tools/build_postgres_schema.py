"""Generate database/postgres/schema.sql from the live SQLite schema.

Usage: .venv/bin/python tools/build_postgres_schema.py SOURCE.db [OUT.sql]

The PostgreSQL schema is rebuilt, not copied: exact money is NUMERIC, so the
SQLite "wide" helper functions become plain expressions and their ~950 triggers
are not carried over (business-rule triggers are ported separately).

Tables are also consolidated on the way (see RENAMED, FOLDED, MERGED, DROPPED);
tools/sqlite_to_postgres.py moves their rows accordingly.
"""
import sqlite3
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]

# Same table under a clearer name.
RENAMED = {"accounting_entries": "journal_entries", "accounting_lines": "journal_lines"}
# Rows move into another table that keeps its own definition.
FOLDED = {"rate_history": "country_rate_history"}  # federation-wide rates use nationality '*'
# Several tables become one (built by MERGED_BUILDERS below).
MERGED = {
    "board_post_reactions": "board_reactions", "board_comment_reactions": "board_reactions",
    "saving": "bank_products", "liability": "bank_products",
}
# Not carried over at all.
DROPPED = {
    "bank_journal_entries": "mirrored entry for entry in journal_entries (ledger_scope 'institution')",
    "bank_journal_lines": "mirrored line for line in journal_lines",
    "market_execution_order_baselines": "superseded by market_execution_order_baselines_v2",
    "financial_amount_snapshots": "duplicated source amounts; reconciliation reads source tables",
    "financial_amount_field_registry": "inventory for financial_amount_snapshots",
    "market_exchange_operators": "no code reads or writes it",
    "wide_money_schema": "SQLite wide-money marker",
    "startup_migration_state": "SQLite startup marker",
}


def _cmp(a, b):
    return exp.Case(ifs=[
        exp.If(this=exp.LT(this=a, expression=b), true=exp.Literal.number(-1)),
        exp.If(this=exp.GT(this=a.copy(), expression=b.copy()), true=exp.Literal.number(1)),
    ], default=exp.Literal.number(0))


def _is_integral(value):
    return exp.EQ(this=value, expression=exp.func("trunc", value.copy()))


_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


def _wide_cmp_args(node):
    if isinstance(node, exp.Anonymous) and node.name.upper() == "WIDE_CMP" and len(node.expressions) == 2:
        return node.expressions
    return None


def _rewrite(node):
    # WIDE_CMP(a, b) <op> 0  ->  a <op> b ; WIDE_CMP(WIDE_TYPEOF(x), 'integer') = 0 -> x = trunc(x)
    if isinstance(node, _COMPARISONS) and isinstance(node.expression, exp.Literal) \
            and node.expression.is_number and node.expression.this == "0":
        args = _wide_cmp_args(node.this)
        if args:
            a, b = args
            if isinstance(a, exp.Anonymous) and a.name.upper() == "WIDE_TYPEOF" \
                    and isinstance(b, exp.Literal) and b.this == "integer":
                whole = _is_integral(a.expressions[0])
                return whole if isinstance(node, exp.EQ) else exp.Not(this=exp.Paren(this=whole))
            return type(node)(this=a, expression=b)
    if isinstance(node, exp.Anonymous):
        name, args = node.name.upper(), node.expressions
        if name == "WIDE_CMP" and len(args) == 2:
            a, b = args
            if isinstance(a, exp.Anonymous) and a.name.upper() == "WIDE_TYPEOF":
                # WIDE_CMP(WIDE_TYPEOF(x), 'integer') = 0  ->  x is a whole number
                kind = exp.If(this=_is_integral(a.expressions[0]), true=exp.Literal.string("integer"))
                a = exp.Case(ifs=[kind], default=exp.Literal.string("real"))
            return _cmp(a, b)
        if name == "WIDE_TYPEOF" and len(args) == 1:
            return exp.Case(ifs=[exp.If(this=_is_integral(args[0]), true=exp.Literal.string("integer"))],
                            default=exp.Literal.string("real"))
        if name == "WIDE_VALID_MONEY" and len(args) == 3:
            value, limit, integral = args
            in_range = exp.LT(this=exp.Abs(this=value.copy()),
                              expression=exp.Cast(this=limit, to=exp.DataType.build("numeric")))
            if integral.is_number and int(integral.this) == 0:
                valid = in_range
            else:
                valid = exp.And(this=in_range, expression=_is_integral(value.copy()))
            return exp.Paren(this=exp.Or(this=exp.Is(this=value, expression=exp.Null()),
                                         expression=exp.Paren(this=valid)))
        if name == "WIDE_IS" and len(args) == 2:
            return exp.NullSafeEQ(this=args[0], expression=args[1])
        if name == "WIDE_MUL" and len(args) == 2:
            return exp.Mul(this=args[0], expression=args[1])
        if name == "WIDE_CANONICAL" and len(args) == 1:
            return args[0]  # NUMERIC is already canonical
        if name == "VALID_COST_BASIS" and len(args) == 1:
            # A non-negative decimal or fraction such as "12", "1.5" or "7/3" (exact_cost_basis).
            return exp.RegexpLike(this=args[0], expression=exp.Literal.string(
                r"^\s*[0-9]+(\.[0-9]+)?(/[0-9]*[1-9][0-9]*)?\s*$"))
        if name == "UNIXEPOCH" and not args:
            return exp.Cast(this=exp.Extract(this=exp.var("epoch"), expression=exp.CurrentTimestamp()),
                            to=exp.DataType.build("bigint"))
    if isinstance(node, exp.Typeof):
        kind = exp.Cast(this=exp.Anonymous(this="pg_typeof", expressions=[node.this]), to=exp.DataType.build("text"))
        return exp.Case(ifs=[
            exp.If(this=exp.In(this=kind, expressions=[exp.Literal.string("bigint"), exp.Literal.string("integer")]),
                   true=exp.Literal.string("integer")),
            exp.If(this=exp.In(this=kind.copy(), expressions=[exp.Literal.string("double precision"),
                                                              exp.Literal.string("numeric")]),
                   true=exp.Literal.string("real")),
        ], default=exp.Literal.string("text"))
    if isinstance(node, exp.Collate):
        return node.this
    if isinstance(node, exp.Literal) and node.is_string and node.this.startswith(("~M~", "~R~")):
        return exp.Literal.number(node.this[3:])  # SQLite wide-money tag
    if isinstance(node, exp.CollateColumnConstraint):
        return None
    if isinstance(node, exp.ColumnConstraint) and isinstance(node.args.get("kind"), exp.CollateColumnConstraint):
        return None
    return node


def _column_type(declared, is_rowid_pk):
    declared = (declared or "").upper()
    if declared.startswith("MONEY"):
        return "NUMERIC"
    if declared.startswith("INT"):
        return "BIGINT"
    if declared in {"REAL", "FLOAT", "DOUBLE"}:
        return "DOUBLE PRECISION"
    if declared == "BLOB":
        return "BYTEA"
    return "TEXT"


def target_table(name):
    return RENAMED.get(name) or FOLDED.get(name) or MERGED.get(name) or name


def table_ddl(conn, name, sql, *, out_name=None, nullable=(), extra_columns=()):
    """PostgreSQL CREATE TABLE for one SQLite table, plus its foreign keys.

    ``nullable`` drops NOT NULL from those columns; ``extra_columns`` are
    (ColumnDef, foreign keys) pairs appended to the definition (used by merges).
    """
    info = list(conn.execute(f'PRAGMA table_info("{name}")'))
    pk_columns = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
    rowid_pk = len(pk_columns) == 1 and next(r for r in info if r[1] == pk_columns[0])[2].upper() == "INTEGER"
    tree = sqlglot.parse_one(sql.replace("MONEYUNITS TEXT", "TEXT").replace("MONEYREAL TEXT", "TEXT")
                             .replace("MONEY TEXT", "TEXT"), read="sqlite")
    schema = tree.this
    for column in list(schema.expressions):
        if not isinstance(column, exp.ColumnDef):
            continue
        row = next(r for r in info if r[1] == column.name)
        is_rowid = rowid_pk and column.name == pk_columns[0]
        kind = _column_type(row[2], False)
        if kind in {"BIGINT", "DOUBLE PRECISION"} and not is_rowid and conn.execute(
                f'SELECT 1 FROM "{name}" WHERE typeof("{column.name}") = \'text\' LIMIT 1').fetchone():
            # SQLite stored exact values (wide "~M~"/"~R~" text) in a numeric column.
            kind = "NUMERIC"
        column.set("kind", exp.DataType.build(kind, dialect="postgres"))
        if is_rowid:
            # SQLite assigns INTEGER PRIMARY KEY values itself; identity does that here.
            kept = [c for c in column.constraints
                    if not isinstance(c.args.get("kind"), exp.AutoIncrementColumnConstraint)]
            kept.insert(0, exp.ColumnConstraint(kind=exp.GeneratedAsIdentityColumnConstraint(this=False)))
            column.set("constraints", kept)
    foreign_keys = []

    def add_foreign_key(columns, reference):
        target = reference.find(exp.Table)
        target_name = target.name if target else ""
        if target_name in DROPPED:
            return
        target.set("this", exp.to_identifier(target_table(target_name)))
        foreign_keys.append(f'ALTER TABLE "{out_name or name}" ADD FOREIGN KEY ({columns}) '
                            f'{reference.sql(dialect="postgres")};')

    for item in list(schema.expressions):
        if isinstance(item, exp.ForeignKey):
            columns = ", ".join(f'"{c.name}"' for c in item.expressions)
            add_foreign_key(columns, item.args["reference"])
            schema.expressions.remove(item)
        elif isinstance(item, exp.ColumnDef):
            kept = []
            for constraint in item.constraints:
                kind = constraint.args.get("kind")
                if isinstance(kind, exp.CollateColumnConstraint):
                    continue
                if isinstance(kind, exp.Reference):
                    add_foreign_key(f'"{item.name}"', kind)
                    continue
                if item.name in nullable and isinstance(kind, exp.NotNullColumnConstraint):
                    continue
                kept.append(constraint)
            item.set("constraints", kept)
    for column in extra_columns:
        schema.append("expressions", column)
    if out_name:
        schema.this.set("this", exp.to_identifier(out_name))
    # Twice: a rewritten helper can contain another helper call as an argument.
    tree = tree.transform(_rewrite).transform(_rewrite)
    return tree.sql(dialect="postgres") + ";", foreign_keys


def _definition(conn, name):
    return conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]


def _columns(conn, name):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]


def build_bank_products(conn):
    """saving (deposit products) + liability (loan products): shared columns once,
    each kind's own columns nullable, and ``kind`` to tell them apart."""
    deposit_only = set(_columns(conn, "saving")) - set(_columns(conn, "liability"))
    loan_only = [c for c in _columns(conn, "liability") if c not in set(_columns(conn, "saving"))]
    loan_ddl, loan_fks = table_ddl(conn, "liability", _definition(conn, "liability"), out_name="bank_products",
                                   nullable=loan_only)
    loan_columns = [c for c in sqlglot.parse_one(loan_ddl[:-1], read="postgres").this.expressions
                    if isinstance(c, exp.ColumnDef) and c.name in loan_only]
    kind = sqlglot.parse_one("CREATE TABLE t (kind TEXT NOT NULL CHECK (kind IN ('deposit', 'loan')))",
                             read="postgres").this.expressions[0]
    ddl, fks = table_ddl(conn, "saving", _definition(conn, "saving"), out_name="bank_products",
                         nullable=deposit_only, extra_columns=[kind, *loan_columns])
    return ddl, fks + [fk for fk in loan_fks if any(f'"{c}"' in fk for c in loan_only)]


def build_board_reactions(conn):
    """board_post_reactions + board_comment_reactions: exactly one of post_id / comment_id,
    each still a cascading foreign key."""
    return ("CREATE TABLE board_reactions ("
            "reaction_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
            "post_id BIGINT REFERENCES board_posts (post_id) ON DELETE CASCADE, "
            "comment_id BIGINT REFERENCES board_comments (comment_id) ON DELETE CASCADE, "
            "user_id TEXT NOT NULL REFERENCES users (user_id), "
            "value BIGINT NOT NULL CHECK (value IN (1, -1)), "
            "created_epoch BIGINT NOT NULL, "
            "CHECK ((post_id IS NULL) <> (comment_id IS NULL)));\n"
            "CREATE UNIQUE INDEX board_reactions_post_user ON board_reactions (post_id, user_id) "
            "WHERE post_id IS NOT NULL;\n"
            "CREATE UNIQUE INDEX board_reactions_comment_user ON board_reactions (comment_id, user_id) "
            "WHERE comment_id IS NOT NULL;"), []


MERGED_BUILDERS = {"bank_products": build_bank_products, "board_reactions": build_board_reactions}


def build(source):
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    out = ["-- Generated by tools/build_postgres_schema.py; do not edit by hand.", ""]
    skipped = []
    foreign_keys = []
    for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if name in MERGED or name in DROPPED or name in FOLDED:
            skipped.append(name)
            continue
        ddl, fks = table_ddl(conn, name, sql, out_name=RENAMED.get(name))
        out.append(ddl)
        foreign_keys.extend(fks)
    for builder in MERGED_BUILDERS.values():
        ddl, fks = builder(conn)
        out.append(ddl)
        foreign_keys.extend(fks)
    for name, table, sql in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"):
        if table in MERGED or table in DROPPED or table in FOLDED:
            continue
        tree = sqlglot.parse_one(sql, read="sqlite").transform(_rewrite).transform(_rewrite)
        for reference in tree.find_all(exp.Table):
            reference.set("this", exp.to_identifier(target_table(reference.name)))
        out.append(tree.sql(dialect="postgres") + ";")
    out.append("")
    out.append("-- Foreign keys last, so tables may reference each other in any order.")
    out.extend(foreign_keys)
    return "\n".join(out) + "\n", skipped


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    text, skipped = build(sys.argv[1])
    target = Path(sys.argv[2]) if len(sys.argv) == 3 else ROOT / "database" / "postgres" / "schema.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print(f"wrote {target} ({text.count(chr(10))} lines); merged/dropped: {len(skipped)}")
