"""Translate the SQLite triggers into PostgreSQL (appended to database/postgres/schema.sql).

Shapes, most to least common:
- "raise when": BEFORE/AFTER ... WHEN cond BEGIN SELECT RAISE(ABORT, 'msg'); END
  -> one shared function raise_abort(msg) plus a trigger with the same WHEN. PostgreSQL
     does not allow subqueries in WHEN, so those get a small function with IF cond.
- "normalize the row it just wrote": AFTER INSERT/UPDATE ... BEGIN UPDATE same_table
  SET a = expr WHERE key = NEW.key; END -> BEFORE trigger assigning NEW.a := expr.
- anything else: the body statements run as-is inside a per-trigger function.
Triggers of merged tables move to the merged table with a guard (bank_products: kind;
country_rate_history: nationality '*'), and statements touching a merged table get the
same filter. Triggers that only guard dropped tables are skipped with the reason.
"""
import hashlib
import re
import textwrap

import sqlglot
from sqlglot import exp

from build_postgres_schema import DROPPED, FOLDED, MERGED, rewrite, target_table

# Rows of a merged/folded source table inside its new table.
ROW_FILTER = {
    "saving": ("kind", "deposit"),
    "liability": ("kind", "loan"),
    "rate_history": ("nationality", "*"),
}
# Dropped tables whose rules already exist elsewhere (verified 2026-11-14).
COVERED = {
    "market_execution_order_baselines": "table dropped (v2 kept)",
}

HEAD = re.compile(
    r"CREATE TRIGGER (?:IF NOT EXISTS )?\"?(?P<name>\w+)\"? (?P<timing>BEFORE|AFTER|INSTEAD OF) "
    r"(?P<event>INSERT|DELETE|UPDATE(?: OF [\w\", ]+?)?) ON \"?(?P<table>\w+)\"? (?:FOR EACH ROW )?"
    r"(?:WHEN (?P<when>.+?) )?BEGIN (?P<body>.+) END;?$", re.I | re.S)
RAISE_ONLY = re.compile(r"\s*SELECT RAISE\((?:ABORT|FAIL|ROLLBACK), ?'(?P<msg>(?:[^']|'')*)'\);?\s*$", re.I | re.S)

PRELUDE = r"""
CREATE OR REPLACE FUNCTION raise_abort() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = TG_ARGV[0], ERRCODE = 'check_violation';
END $$;

-- The configured base currency (SQLite: a Python function registered per connection).
CREATE OR REPLACE FUNCTION base_currency_code() RETURNS text LANGUAGE sql STABLE AS $$
    SELECT upper(base_currency_code) FROM currency_system_settings WHERE singleton_id = 1
$$;

-- An exact rational "a/b" (or integer) as numerator and denominator.
CREATE OR REPLACE FUNCTION rational_parts(value text, OUT num numeric, OUT den numeric)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    text_value text := btrim(value);
    scale int;
BEGIN
    IF position('/' IN text_value) > 0 THEN
        num := split_part(text_value, '/', 1)::numeric;
        den := split_part(text_value, '/', 2)::numeric;
    ELSE
        scale := greatest(coalesce(length(split_part(text_value, '.', 2)), 0), 0);
        num := replace(text_value, '.', '')::numeric;
        den := 10::numeric ^ scale;
    END IF;
END $$;

-- database/exact_cost_basis.remaining_cost_basis: total * new / old as a reduced
-- fraction string, exactly like Python's str(Fraction).
CREATE OR REPLACE FUNCTION remaining_cost_basis(total text, old_quantity numeric, new_quantity numeric)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    t record; o record; n record;
    num numeric; den numeric; divisor numeric;
BEGIN
    IF old_quantity <= 0 OR new_quantity < 0 OR new_quantity > old_quantity THEN
        RAISE EXCEPTION '원가 배분 수량을 확인해주세요.' USING ERRCODE = 'check_violation';
    END IF;
    t := rational_parts(total); o := rational_parts(old_quantity::text); n := rational_parts(new_quantity::text);
    num := t.num * n.num * o.den;
    den := t.den * n.den * o.num;
    IF den < 0 THEN num := -num; den := -den; END IF;
    divisor := gcd(num, den);
    IF divisor <> 0 THEN num := num / divisor; den := den / divisor; END IF;
    RETURN CASE WHEN den = 1 THEN trunc(num)::text ELSE trunc(num)::text || '/' || trunc(den)::text END;
END $$;
"""


# table -> column that SQLite's rowid aliases (single INTEGER primary key, or the key of a
# merged table, which has no rowid column of its own). Filled by translate().
ROWID_ALIAS = {}


def _map_rowid(tree, table=None):
    for column in tree.find_all(exp.Column):
        if column.name.lower() not in {"rowid", "_rowid_", "oid"}:
            continue
        owner = column.table.upper() if column.table else ""
        source = table if owner in {"", "NEW", "OLD"} and table else column.table
        alias = ROWID_ALIAS.get(source)
        if alias:
            column.set("this", exp.to_identifier(alias))
    return tree


def _expr(sql, table=None):
    tree = rewrite(_filter_merged(_map_rowid(sqlglot.parse_one(f"SELECT {sql}", read="sqlite"), table)))
    for table in tree.find_all(exp.Table):
        table.set("this", exp.to_identifier(target_table(table.name)))
    return tree.expressions[0].sql(dialect="postgres")


def _raise_chain(tree):
    """SELECT CASE WHEN c THEN RAISE(ABORT, 'm') ... END -> a PL/pgSQL IF chain, else None."""
    if not (isinstance(tree, exp.Select) and len(tree.expressions) == 1 and not tree.args.get("from")
            and isinstance(tree.expressions[0], exp.Case) and not tree.expressions[0].args.get("default")):
        return None
    branches = []
    for branch in tree.expressions[0].args["ifs"]:
        action = branch.args["true"]
        if not (isinstance(action, exp.Anonymous) and action.name.upper() == "RAISE"
                and len(action.expressions) == 2 and action.expressions[1].is_string):
            return None
        message = action.expressions[1].this.replace("'", "''")
        condition = rewrite(branch.this).sql(dialect="postgres")
        branches.append(f"({condition}) THEN\n            RAISE EXCEPTION USING MESSAGE = '{message}', "
                        f"ERRCODE = 'check_violation';")
    return "IF " + "\n        ELSIF ".join(branches) + "\n        END IF"


def _filter_merged(tree):
    """Point references to merged source tables at their rows in the new table."""
    for table in list(tree.find_all(exp.Table)):
        if table.name not in ROW_FILTER:
            continue
        column, value = ROW_FILTER[table.name]
        alias = table.alias_or_name
        statement = table.parent
        if isinstance(statement, exp.Schema) and isinstance(statement.parent, exp.Insert):
            statement = statement.parent  # INSERT INTO t (columns ...)
        if isinstance(statement, (exp.Update, exp.Delete)) and statement.this is table:
            if not table.alias:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))  # "liability.col" still resolves
            condition = exp.EQ(this=exp.column(column, table=alias), expression=exp.Literal.string(value))
            where = statement.args.get("where")
            statement.set("where", exp.Where(this=exp.and_(where.this, condition) if where else condition))
        elif isinstance(statement, exp.Insert):
            table.set("this", exp.to_identifier(target_table(table.name)))
            # INSERT INTO rate_history (...) -> INSERT INTO country_rate_history (nationality, ...)
            schema = statement.this
            if isinstance(schema, exp.Schema):
                schema.set("expressions", [exp.to_identifier(column), *schema.expressions])
            source = statement.expression
            literal = exp.Literal.string(value)
            if isinstance(source, exp.Values):
                for row in source.expressions:
                    row.set("expressions", [literal.copy(), *row.expressions])
            elif isinstance(source, exp.Select):
                source.set("expressions", [literal.copy(), *source.expressions])
        else:
            subquery = sqlglot.parse_one(
                f"SELECT * FROM {target_table(table.name)} WHERE {column} = '{value}'", read="postgres")
            table.replace(exp.Subquery(this=subquery, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree


def _statement(sql, keys=None, columns=None, table=None):
    tree = _filter_merged(_map_rowid(sqlglot.parse_one(sql, read="sqlite"), table))
    chain = _raise_chain(tree)
    if chain:
        return chain
    if isinstance(tree, exp.Insert) and str(tree.args.get("alternative") or "").upper() == "REPLACE":
        # INSERT OR REPLACE = insert, or overwrite the row with the same primary key.
        table = tree.find(exp.Table).name
        target = tree.this.expressions if isinstance(tree.this, exp.Schema) else None
        names = [c.name for c in target] if target else (columns or {}).get(table, [])
        key = (keys or {}).get(table, [])
        tree.set("alternative", None)
        tree.set("conflict", exp.OnConflict(
            conflict_keys=[exp.to_identifier(k) for k in key],
            action=exp.var("DO UPDATE"),
            expressions=[exp.EQ(this=exp.column(n), expression=exp.column(n, table="EXCLUDED"))
                         for n in names if n not in key]))
    tree = rewrite(tree)
    for table in tree.find_all(exp.Table):
        table.set("this", exp.to_identifier(target_table(table.name)))
    return tree.sql(dialect="postgres")


def _statements(body):
    return [part.strip() for part in sqlglot.transpile(body, read="sqlite", write="sqlite") if part.strip()]


def _event(event):
    event = " ".join(event.split())
    if event.upper().startswith("UPDATE OF"):
        columns = [c.strip().strip('"') for c in event[len("UPDATE OF"):].split(",")]
        return "UPDATE OF " + ", ".join(f'"{c}"' for c in columns)
    return event.upper()


def _row(event):
    return "OLD" if event.startswith("DELETE") else "NEW"


def _same_row_update(statement, table, keys):
    """'UPDATE table SET a=e WHERE key=NEW.key' -> [(a, e)], else None."""
    tree = _map_rowid(sqlglot.parse_one(statement, read="sqlite"), table)
    if not isinstance(tree, exp.Update) or tree.this.name != table:
        return None
    where = tree.args.get("where")
    if not where:
        return None
    conditions = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
    matched = set()
    for condition in conditions:
        if not isinstance(condition, exp.EQ):
            return None
        left, right = condition.this, condition.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)
                and right.table.upper() == "NEW" and left.name == right.name):
            return None
        matched.add(left.name)
    if matched != set(keys):
        return None
    assignments = []
    for setting in tree.expressions:
        column = setting.this.name
        value = rewrite(setting.expression)
        assignments.append((column, value.sql(dialect="postgres")))
    return assignments


def _identifier(name, suffix=""):
    """A quoted PostgreSQL identifier of at most 63 bytes, unique for long names."""
    full = name + suffix
    if len(full.encode()) > 63:
        full = full[:50] + "_" + hashlib.sha1(full.encode()).hexdigest()[:8] + suffix[-3:]
    return f'"{full}"'


def _function(name, body, returns):
    return (f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            f"BEGIN\n{body}\n    RETURN {returns};\nEND $$;")


def translate(conn):
    """PostgreSQL text for every portable trigger, and the names skipped with reasons."""
    out, skipped = [PRELUDE.strip()], []
    keys, columns = {}, {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        keys[table] = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        columns[table] = [r[1] for r in info]
        integer_key = [r for r in info if r[5]]
        if len(integer_key) == 1 and (integer_key[0][2] or "").upper() == "INTEGER" or (
                table in MERGED and len(keys[table]) == 1):
            ROWID_ALIAS[table] = keys[table][0]
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"):
        m = HEAD.match(" ".join(sql.split()))
        if not m:
            skipped.append((name, "unrecognized trigger form"))
            continue
        table, timing, event, when, body = m["table"], m["timing"].upper(), _event(m["event"]), m["when"], m["body"]
        if table in COVERED:
            skipped.append((name, f"covered: {COVERED[table]}"))
            continue
        if table in DROPPED or any(re.search(rf"\b{re.escape(t)}\b", body, re.I) for t in DROPPED):
            skipped.append((name, "uses a dropped table"))
            continue
        if event.startswith("UPDATE OF"):
            # SQLite fires UPDATE OF when another trigger's UPDATE writes the column; here a
            # BEFORE trigger may change it through NEW instead, which PostgreSQL's UPDATE OF
            # does not see. Fire on any UPDATE that changes one of the columns.
            watched = [c.strip().strip('"') for c in event[len("UPDATE OF"):].split(",")]
            changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in watched)
            when = f"({changed}) AND ({when})" if when else changed
            event = "UPDATE"
        if table in ROW_FILTER:
            # The trigger now sits on the merged table: fire only for the source table's rows.
            column, value = ROW_FILTER[table]
            row = "OLD" if event.startswith("DELETE") else "NEW"
            guard = f"{row}.{column} = '{value}'"
            when = f"({guard}) AND ({when})" if when else guard
        try:
            condition = _expr(when, table) if when else None
            raise_only = RAISE_ONLY.match(body)
            if raise_only:
                message = raise_only["msg"].replace("''", "'")
                literal = "'" + message.replace("'", "''") + "'"
                if condition is None or "SELECT" not in condition.upper():
                    when_sql = f" WHEN ({condition})" if condition else ""
                    out.append(f'CREATE TRIGGER {_identifier(name)} {timing} {event} ON "{target_table(table)}" '
                               f"FOR EACH ROW{when_sql} EXECUTE FUNCTION raise_abort({literal});")
                    continue
                fn = _identifier(name, "_fn")
                out.append(_function(fn, f"    IF ({condition}) THEN\n        RAISE EXCEPTION USING MESSAGE = {literal}, "
                                         f"ERRCODE = 'check_violation';\n    END IF;", _row(event)))
                out.append(f'CREATE TRIGGER {_identifier(name)} {timing} {event} ON "{target_table(table)}" '
                           f"FOR EACH ROW EXECUTE FUNCTION {fn}();")
                continue
            statements = _statements(body)
            fn = _identifier(name, "_fn")
            normalize = (len(statements) == 1 and timing == "AFTER" and not event.startswith("DELETE")
                         and _same_row_update(statements[0], table, keys.get(table, [])))
            if normalize:
                assign = "\n".join(f"        NEW.\"{column}\" := {value};" for column, value in normalize)
                block = f"    IF ({condition}) THEN\n{assign}\n    END IF;" if condition else textwrap.dedent(assign).replace("\n", "\n    ").join(("    ", ""))
                out.append(_function(fn, block, "NEW"))
                out.append(f'CREATE TRIGGER {_identifier(name)} BEFORE {event} ON "{target_table(table)}" '
                           f"FOR EACH ROW EXECUTE FUNCTION {fn}();")
                continue
            translated = "\n".join(f"        {_statement(s, keys, columns, table)};" for s in statements)
            block = f"    IF ({condition}) THEN\n{translated}\n    END IF;" if condition else textwrap.dedent(translated).replace("\n", "\n    ").join(("    ", ""))
            out.append(_function(fn, block, _row(event) if timing in {"BEFORE", "INSTEAD OF"} else "NULL"))
            out.append(f'CREATE TRIGGER {_identifier(name)} {timing} {event} ON "{target_table(table)}" '
                       f"FOR EACH ROW EXECUTE FUNCTION {fn}();")
        except sqlglot.errors.SqlglotError as error:
            skipped.append((name, f"could not translate: {str(error).splitlines()[0][:80]}"))
    return "\n".join(out) + "\n", skipped
