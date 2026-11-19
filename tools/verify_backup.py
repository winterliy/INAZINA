#!/usr/bin/env python3
"""Inspect a SQLite backup; this does not restore it or validate business ledgers."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path, help='SQLite backup snapshot to inspect')
    args = parser.parse_args()
    try:
        # Project CHECK constraints depend on deterministic SQLite functions.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from database.exact_cost_basis import register as register_cost
        from database.wide_values import register as register_wide
        path = args.backup.resolve(strict=True)
        with path.open('rb') as source:
            if source.read(16) != b'SQLite format 3\x00':
                raise ValueError('Not a complete SQLite database file')
        uri = path.as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as db:
            register_cost(db)
            register_wide(db)
            integrity = [row[0] for row in db.execute('PRAGMA integrity_check')]
            violations = [dict(zip(('table', 'rowid', 'parent', 'fk_index'), row))
                          for row in db.execute('PRAGMA foreign_key_check')]
        report = {'ok': integrity == ['ok'] and not violations,
                  'integrity': integrity, 'foreign_key_violations': violations}
    except (OSError, ValueError, sqlite3.Error) as exc:
        report = {'ok': False, 'error': str(exc)}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
