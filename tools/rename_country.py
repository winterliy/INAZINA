"""Rename a country key (the nationality name) across the PostgreSQL database in one transaction.

The country name is the key that users, companies, the treasury, the currency setting, the exchange and
the rate history all point at, so it has to change everywhere at once. Names of companies, currencies and
other things that merely contain the old name are left alone. Without --apply the whole change runs and
is rolled back, so it doubles as a rehearsal.

    INAZINA_DB_BACKEND=postgres python tools/rename_country.py 남방연방 "United Southerns" [--apply] [--dsn DSN]
"""
import argparse
import os
import sys

# Every column that stores the country name as a key (found by scanning all text columns of the live database).
KEY_COLUMNS = [
    ("aml_monitoring_rules", "nationality"), ("bank_products", "rate_nationality"), ("company", "nationality"),
    ("country_currencies", "nationality"), ("country_macro_indicators", "nationality"),
    ("country_rate_history", "nationality"), ("country_rate_history_events", "nationality"),
    ("country_treasuries", "nationality"), ("credit_risk_scenarios", "nationality"),
    ("financial_institutions", "nationality"), ("market_exchanges", "nationality"),
    ("market_rate_curve", "nationality"), ("market_rate_curve_history", "nationality"),
    ("market_rate_curve_history_events", "nationality"), ("market_rate_pressure", "nationality"),
    ("market_rate_pressure_history", "nationality"), ("organizations", "country_code"), ("users", "nationality"),
    # Tables that may hold other countries only; renamed too if they hold this one.
    ("country_tax_rules", "nationality"), ("financial_holidays", "nationality"), ("guild_countries", "nationality"),
]


def rename(conn, old, new, apply):
    cur = conn.cursor()
    present = {}
    for table, column in KEY_COLUMNS:
        cur.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}"=%s', (old,))
        present[(table, column)] = cur.fetchone()[0]
        cur.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}"=%s', (new,))
        if cur.fetchone()[0]:
            raise SystemExit(f"{table}.{column} already holds '{new}'.")
    cur.execute("SELECT count(*) FROM users WHERE nickname=%s AND nationality=%s", (old, old))
    treasury_nick = cur.fetchone()[0]
    # The country's own account is nicknamed after the country; it follows the rename.
    # replica mode switches off the user triggers (the append-only history guards and the nationality rescale)
    # and the foreign keys for this transaction only; integrity is verified below before anything is committed.
    cur.execute("SET LOCAL session_replication_role = replica")
    cur.execute("UPDATE users SET nickname=%s WHERE nickname=%s AND nationality=%s", (new, old, old))
    for (table, column), count in present.items():
        if not count:
            continue
        cur.execute(f'UPDATE "{table}" SET "{column}"=%s WHERE "{column}"=%s', (new, old))
        if cur.rowcount != count:
            raise SystemExit(f"{table}.{column}: expected {count} rows, changed {cur.rowcount}.")
    cur.execute("SET LOCAL session_replication_role = origin")
    for table, column in KEY_COLUMNS:
        cur.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}"=%s', (old,))
        if cur.fetchone()[0]:
            raise SystemExit(f"{table}.{column} still holds '{old}'.")
    cur.execute("SELECT conrelid::regclass::text, a.attname, confrelid::regclass::text, fa.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1] "
                "JOIN pg_attribute fa ON fa.attrelid=c.confrelid AND fa.attnum=c.confkey[1] "
                "WHERE c.contype='f' AND c.confrelid='country_currencies'::regclass AND array_length(c.conkey,1)=1")
    for child, child_col, parent, parent_col in cur.fetchall():
        cur.execute(f'SELECT count(*) FROM "{child}" c WHERE c."{child_col}" IS NOT NULL AND NOT EXISTS '
                    f'(SELECT 1 FROM "{parent}" p WHERE p."{parent_col}"=c."{child_col}")')
        if cur.fetchone()[0]:
            raise SystemExit(f"Orphans left in {child}.{child_col}.")
    cur.execute("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled<>'O'")
    if cur.fetchone()[0]:
        raise SystemExit("Some triggers are not enabled.")
    summary = {f"{t}.{c}": n for (t, c), n in present.items() if n}
    summary["users.nickname"] = treasury_nick
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("INAZINA_PG_DSN"))
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("Set INAZINA_PG_DSN or pass --dsn.")
    import psycopg
    with psycopg.connect(args.dsn) as conn:
        conn.execute("SET lock_timeout='15s'")
        summary = rename(conn, args.old, args.new, args.apply)
    print(("APPLIED" if args.apply else "REHEARSAL (rolled back)"), f"{args.old} -> {args.new}")
    for key, count in summary.items():
        print(f"  {key}: {count}")


if __name__ == "__main__":
    sys.exit(main())
