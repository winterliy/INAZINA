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
# bank_journal_entries/lines mirror journal_* (ledger_scope 'institution') but stay for now:
# settlement links and bank_entry_id columns point at their own entry ids. Folding them into
# the journal is an app refactor for both backends.
DROPPED = {
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

# Column names that are not all lowercase. SQLite matches them case-insensitively;
# PostgreSQL keeps their case only when quoted, so they are always quoted.
MIXED_CASE = {"fractal": "Fractal"}


def _already_numeric_round(node):
    def is_cast(value, kind):
        return isinstance(value, exp.Cast) and value.to.is_type(kind)
    decimals = node.args.get("decimals")
    return is_cast(node.this, "numeric") and (decimals is None or is_cast(decimals, "int"))


def _wide_cmp_args(node):
    if isinstance(node, exp.Anonymous) and node.name.upper() == "WIDE_CMP" and len(node.expressions) == 2:
        return node.expressions
    return None


def _rewrite(node):
    if isinstance(node, exp.Identifier) and not node.quoted and node.this.lower() in MIXED_CASE:
        return exp.Identifier(this=MIXED_CASE[node.this.lower()], quoted=True)
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
        # Exact arithmetic is native on NUMERIC. Decimal // and % truncate toward
        # zero, like trunc() and mod().
        binary = {"WIDE_ADD": exp.Add, "WIDE_SUB": exp.Sub, "WIDE_MUL": exp.Mul, "WIDE_DIV_REAL": exp.Div}
        if name in binary and len(args) == 2:
            return exp.Paren(this=binary[name](this=args[0], expression=args[1]))
        if name == "WIDE_DIV" and len(args) == 2:
            a, b = args
            quotient = exp.Div(this=a, expression=b, typed=True)
            if any(isinstance(arg, exp.Anonymous) and arg.name.upper() == "WIDE_REAL" for arg in args):
                return exp.Paren(this=quotient)  # a real operand: ordinary division (wide_values.py)
            both_whole = exp.And(this=_is_integral(a.copy()), expression=_is_integral(b.copy()))
            return exp.Case(ifs=[exp.If(this=both_whole, true=exp.func("trunc", quotient.copy()))],
                            default=quotient)
        if name == "WIDE_MOD" and len(args) == 2:
            return exp.func("mod", args[0], args[1])
        if name == "WIDE_ROUND" and 1 <= len(args) <= 2:
            return exp.Round(this=args[0], decimals=args[1] if len(args) == 2 else exp.Literal.number(0))
        if name == "WIDE_INT" and len(args) == 1:
            return exp.func("trunc", args[0])
        if name in {"WIDE_REAL", "WIDE_TEXT"} and len(args) == 1:
            return exp.Cast(this=args[0], to=exp.DataType.build("numeric" if name == "WIDE_REAL" else "text"))
        if name == "WIDE_ABS" and len(args) == 1:
            return exp.Abs(this=args[0])
        aggregates = {"WIDE_SUM": exp.Sum, "WIDE_MAX_AGG": exp.Max, "WIDE_MIN_AGG": exp.Min, "WIDE_AVG": exp.Avg}
        if name in aggregates and len(args) == 1:
            return aggregates[name](this=args[0])
        if name == "WIDE_TOTAL" and len(args) == 1:
            return exp.Coalesce(this=exp.Sum(this=args[0]), expressions=[exp.Literal.number(0)])
        if name == "WIDE_CANONICAL" and len(args) == 1:
            return args[0]  # NUMERIC is already canonical
        if name == "VALID_COST_BASIS" and len(args) == 1:
            # A non-negative decimal or fraction such as "12", "1.5" or "7/3" (exact_cost_basis).
            return exp.RegexpLike(this=args[0], expression=exp.Literal.string(
                r"^\s*[0-9]+(\.[0-9]+)?(/[0-9]*[1-9][0-9]*)?\s*$"))
        if name == "JSON_VALID" and len(args) == 1:
            valid = exp.Anonymous(this="pg_input_is_valid", expressions=[args[0], exp.Literal.string("json")])
            return exp.Case(ifs=[exp.If(this=valid, true=exp.Literal.number(1))], default=exp.Literal.number(0))
        if name == "LAST_INSERT_ROWID" and not args:
            return exp.Anonymous(this="lastval", expressions=[])
        if name == "PRINTF" and len(args) == 2 and args[0].is_string:
            import re as _re
            match = _re.fullmatch(r"(.*)%0(\d+)d", args[0].this)
            if match:
                padded = exp.Anonymous(this="lpad", expressions=[
                    exp.Cast(this=args[1], to=exp.DataType.build("text")),
                    exp.Literal.number(match[2]), exp.Literal.string("0")])
                return exp.DPipe(this=exp.Literal.string(match[1]), expression=padded)
        if name == "JULIANDAY" and len(args) == 1 and args[0].is_string and args[0].this == "now":
            return sqlglot.parse_one("(EXTRACT(epoch FROM now()) / 86400.0 + 2440587.5)", read="postgres")
        if name == "DATETIME" and len(args) == 1 and args[0].is_string and args[0].this == "now":
            return sqlglot.parse_one("to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')", read="postgres")
        if name == "DATETIME" and len(args) == 2 and args[1].is_string and args[1].this == "unixepoch":
            stamp = exp.Anonymous(this="to_timestamp", expressions=[args[0]])
            return exp.Anonymous(this="to_char", expressions=[
                exp.AtTimeZone(this=stamp, zone=exp.Literal.string("UTC")),
                exp.Literal.string("YYYY-MM-DD HH24:MI:SS")])
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
    if isinstance(node, exp.Is) and not isinstance(node.expression, (exp.Null, exp.Boolean)):
        return exp.NullSafeEQ(this=node.this, expression=node.expression)  # SQLite "a IS b"
    conflict = node.args.get("conflict") if isinstance(node, exp.Insert) else None
    if conflict is not None and conflict.expressions and not node.meta.get("qualified"):
        # SQLite upsert: a bare column in DO UPDATE means the existing row; PostgreSQL
        # needs it qualified with the target table.
        table = node.find(exp.Table)
        owner = table.alias_or_name if table else None
        for assignment in conflict.expressions:
            for column in assignment.expression.find_all(exp.Column):
                if not column.table and owner:
                    column.set("table", exp.to_identifier(owner))
        node.meta["qualified"] = True
    if isinstance(node, exp.Insert) and str(node.args.get("alternative") or "").upper() == "IGNORE":
        node.set("alternative", None)
        node.set("conflict", exp.OnConflict(action=exp.var("DO NOTHING")))
        return node
    if isinstance(node, exp.Hex) and isinstance(node.this, exp.Anonymous) \
            and node.this.name.upper() == "RANDOMBLOB":
        # HEX(RANDOMBLOB(n)): 2n random hex digits (only used for unique ledger keys).
        size = int(node.this.expressions[0].this) * 2
        digest = exp.Anonymous(this="md5", expressions=[sqlglot.parse_one(
            "CAST(random() AS TEXT) || CAST(clock_timestamp() AS TEXT)", read="postgres")])
        return exp.Substring(this=digest, start=exp.Literal.number(1), length=exp.Literal.number(size))
    if isinstance(node, exp.JSONType):
        # SQLite json_type(): 'integer' / 'real' / 'text' / 'true' / 'false' / 'object' ...
        value = node.this
        return sqlglot.parse_one(
            "CASE jsonb_typeof(CAST(x AS JSONB)) WHEN 'number' THEN CASE WHEN x ~ '^-?[0-9]+$' "
            "THEN 'integer' ELSE 'real' END WHEN 'string' THEN 'text' WHEN 'boolean' THEN x "
            "ELSE jsonb_typeof(CAST(x AS JSONB)) END", read="postgres"
        ).transform(lambda n: value.copy() if isinstance(n, exp.Column) and n.name == "x" else n)
    if isinstance(node, exp.TimeToStr) and node.args.get("format") is not None \
            and node.args["format"].this == "%s":
        # strftime('%s', 'now'): the current UNIX time.
        return exp.Cast(this=exp.Extract(this=exp.var("epoch"), expression=exp.CurrentTimestamp()),
                        to=exp.DataType.build("bigint"))
    if isinstance(node, exp.OnConflict) and any(isinstance(k, exp.Ordered) for k in node.args.get("conflict_keys") or []):
        node.set("conflict_keys", [k.this if isinstance(k, exp.Ordered) else k for k in node.args["conflict_keys"]])
        return node
    if isinstance(node, exp.Glob) and isinstance(node.expression, exp.Literal):
        # SQLite GLOB (case-sensitive, whole string): * ? [..] -> an anchored regex.
        regex, pattern, i = "", node.expression.this, 0
        while i < len(pattern):
            char = pattern[i]
            if char == "*":
                regex += ".*"
            elif char == "?":
                regex += "."
            elif char == "[":
                end = pattern.index("]", i + 2 if pattern[i + 1:i + 2] in ("^", "]") else i + 1)
                regex += pattern[i:end + 1]
                i = end
            else:
                regex += "\\" + char if char in ".+()|{}$^\\" else char
            i += 1
        return exp.RegexpLike(this=node.this, expression=exp.Literal.string(f"^{regex}$"))
    if isinstance(node, exp.Date) and isinstance(node.args.get("zone"), exp.Literal):
        # SQLite date(x, '-3 months'): a date shifted by the modifier.
        shifted = exp.Add(this=exp.Cast(this=node.this, to=exp.DataType.build("date")),
                          expression=exp.Cast(this=node.args["zone"], to=exp.DataType.build("interval")))
        return exp.Cast(this=shifted, to=exp.DataType.build("date"))
    if isinstance(node, (exp.And, exp.Or, exp.Not, exp.Where)):
        # SQLite treats 0/1 as false/true in boolean positions.
        for key in ("this", "expression"):
            child = node.args.get(key)
            if isinstance(child, exp.Literal) and child.is_number:
                node.set(key, exp.Boolean(this=child.this not in ("0", "0.0")))
    if isinstance(node, exp.Rand):
        # SQLite random() is a signed 64-bit integer; callers take abs() and a modulus.
        scaled = exp.Mul(this=exp.Anonymous(this="random", expressions=[]),
                         expression=exp.Literal.number(9007199254740991))
        return exp.Cast(this=exp.Floor(this=scaled), to=exp.DataType.build("bigint"))
    if isinstance(node, exp.Round) and not _already_numeric_round(node):
        # PostgreSQL rounds exactly only on NUMERIC, and takes an int place count.
        return exp.Round(this=exp.Cast(this=node.this, to=exp.DataType.build("numeric")),
                         decimals=exp.Cast(this=node.args["decimals"], to=exp.DataType.build("int"))
                         if node.args.get("decimals") else None)
    if isinstance(node, exp.Div) and not node.args.get("typed"):
        # No float cast: bigint/bigint divides like SQLite, NUMERIC divides exactly.
        node.set("typed", True)
        return node
    if isinstance(node, exp.Collate):
        return node.this
    if isinstance(node, exp.Literal) and node.is_string and node.this.startswith(("~M~", "~R~")):
        return exp.Literal.number(node.this[3:])  # SQLite wide-money tag
    if isinstance(node, exp.CollateColumnConstraint):
        return None
    if isinstance(node, exp.ColumnConstraint) and isinstance(node.args.get("kind"), exp.CollateColumnConstraint):
        return None
    return node


def rewrite(tree):
    """Apply _rewrite until nothing changes: a rewritten helper can wrap another one."""
    for _ in range(10):
        before = tree.sql(dialect="postgres")
        tree = tree.transform(_rewrite)
        if tree.sql(dialect="postgres") == before:
            return tree
    raise ValueError("rewrite did not settle")


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


def fractional_integer_columns(conn):
    """INTEGER-declared columns the schema itself rounds to decimals (e.g. fractional share
    quantities): SQLite stores 1.235 there anyway, so PostgreSQL needs NUMERIC."""
    import re
    found = set()
    for table, sql in conn.execute("SELECT tbl_name, sql FROM sqlite_master WHERE type IN ('trigger', 'table')"):
        for column, places in re.findall(r'ROUND\(\s*(?:NEW\.)?"?(\w+)"?\s*,\s*(\d+)\s*\)', sql or "", re.I):
            if int(places) > 0:
                found.add((table, column))
    return found


def table_ddl(conn, name, sql, *, out_name=None, nullable=(), extra_columns=()):
    """PostgreSQL CREATE TABLE for one SQLite table, plus its foreign keys.

    ``nullable`` drops NOT NULL from those columns; ``extra_columns`` are
    (ColumnDef, foreign keys) pairs appended to the definition (used by merges).
    """
    info = list(conn.execute(f'PRAGMA table_info("{name}")'))
    pk_columns = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
    fractional = fractional_integer_columns(conn)
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
        if kind == "BIGINT" and (name, column.name) in fractional:
            kind = "NUMERIC"
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
    if not rowid_pk and not out_name:
        # SQLite gives every table a rowid; code reads it, so keep one (last, so positional
        # INSERT ... VALUES still fills the declared columns).
        schema.append("expressions", sqlglot.parse_one(
            "CREATE TABLE t (rowid BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE)", read="postgres").this.expressions[0])
    if out_name:
        schema.this.set("this", exp.to_identifier(out_name))
    tree = rewrite(tree)
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
            "post_id BIGINT, comment_id BIGINT, user_id TEXT NOT NULL, "
            "value BIGINT NOT NULL CHECK (value IN (1, -1)), "
            "created_epoch BIGINT NOT NULL, "
            "CHECK ((post_id IS NULL) <> (comment_id IS NULL)));\n"
            "CREATE UNIQUE INDEX board_reactions_post_user ON board_reactions (post_id, user_id) "
            "WHERE post_id IS NOT NULL;\n"
            "CREATE UNIQUE INDEX board_reactions_comment_user ON board_reactions (comment_id, user_id) "
            "WHERE comment_id IS NOT NULL;"), [
        'ALTER TABLE "board_reactions" ADD FOREIGN KEY ("post_id") REFERENCES board_posts (post_id) ON DELETE CASCADE;',
        'ALTER TABLE "board_reactions" ADD FOREIGN KEY ("comment_id") REFERENCES board_comments (comment_id) '
        'ON DELETE CASCADE;',
        'ALTER TABLE "board_reactions" ADD FOREIGN KEY ("user_id") REFERENCES users (user_id);']


MERGED_BUILDERS = {"bank_products": build_bank_products, "board_reactions": build_board_reactions}


def build(source):
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    unknown = sorted({row[1] for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      for row in conn.execute(f'PRAGMA table_info("{table}")')
                      if row[1] != row[1].lower() and row[1].lower() not in MIXED_CASE})
    if unknown:
        raise SystemExit(f"add these mixed-case columns to MIXED_CASE: {unknown}")
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
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for merged, builder in MERGED_BUILDERS.items():
        sources = {name for name, target in MERGED.items() if target == merged}
        if not sources <= present:
            continue  # created at runtime (e.g. the board) or not in this database
        ddl, fks = builder(conn)
        out.append(ddl)
        foreign_keys.extend(fks)
    for name, table, sql in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"):
        if table in MERGED or table in DROPPED or table in FOLDED:
            continue
        tree = rewrite(sqlglot.parse_one(sql, read="sqlite"))
        for reference in tree.find_all(exp.Table):
            reference.set("this", exp.to_identifier(target_table(reference.name)))
        out.append(tree.sql(dialect="postgres") + ";")
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type='view' ORDER BY name"):
        tree = rewrite(sqlglot.parse_one(sql, read="sqlite"))
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
    from build_postgres_triggers import translate
    text, skipped = build(sys.argv[1])
    target = Path(sys.argv[2]) if len(sys.argv) == 3 else ROOT / "database" / "postgres" / "schema.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    triggers, not_ported = translate(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True))
    trigger_file = target.with_name("triggers.sql")
    notes = "".join(f"-- not ported: {name} ({reason})\n" for name, reason in not_ported
                    if not name.startswith("trg_amount_snapshot_"))
    trigger_file.write_text("-- Generated by tools/build_postgres_schema.py; do not edit by hand.\n"
                            f"-- {len(not_ported)} SQLite triggers not ported, listed here except the "
                            "financial_amount_snapshots ones (table dropped).\n" + notes + "\n" + triggers)
    print(f"wrote {target} ({text.count(chr(10))} lines; merged/dropped tables: {len(skipped)}) "
          f"and {trigger_file.name} ({triggers.count('CREATE TRIGGER')} triggers)")
