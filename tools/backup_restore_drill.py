#!/usr/bin/env python3
"""Restore a standalone SQLite backup into temporary storage and inspect it."""
import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path, help='complete, standalone SQLite backup file')
    parser.add_argument('--require-table', action='append', default=[], metavar='NAME',
                        help='require this table after restore (repeatable)')
    parser.add_argument('--reconcile', action='store_true',
                        help='run implemented financial close checks on the restored copy')
    args = parser.parse_args()
    start = time.monotonic()
    try:
        source_path = args.backup.resolve(strict=True)
        for suffix in ('-wal', '-journal'):
            if Path(str(source_path) + suffix).exists():
                raise ValueError('A SQLite journal sidecar exists; supply a standalone backup')
        with source_path.open('rb') as source_file:
            if source_file.read(16) != b'SQLite format 3\x00':
                raise ValueError('Not a complete SQLite database file')
        # immutable prevents SQLite from creating sidecars or changing the source.
        # A live WAL database is not a standalone backup and must not be used here.
        source_uri = source_path.as_uri() + '?mode=ro&immutable=1'
        with tempfile.TemporaryDirectory(prefix='inazina-restore-drill-') as directory:
            restored = Path(directory) / 'restored.db'
            with closing(sqlite3.connect(source_uri, uri=True)) as source, \
                    closing(sqlite3.connect(restored)) as target:
                source.backup(target)
            command = [sys.executable, str(Path(__file__).with_name('verify_backup.py')), str(restored)]
            for name in args.require_table:
                command.extend(['--require-table', name])
            result = subprocess.run(
                command,
                capture_output=True, text=True, check=False,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError('restored backup verification could not run')
            verification = json.loads(result.stdout)
            report = {'ok': result.returncode == 0 and verification.get('ok') is True,
                      'verification': verification}
            if args.reconcile and report['ok']:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from database.unified import UnifiedConnection
                from managers.accounting_journal import AccountingJournal
                with closing(sqlite3.connect(restored.as_uri() + '?mode=ro', uri=True,
                                             factory=UnifiedConnection)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute('PRAGMA query_only=ON')
                    check = AccountingJournal.reconcile_all_financial_ledgers(conn)
                report['reconciliation'] = {
                    'status': check['status'], 'close_ready': check['close_ready'],
                    'coverage': check['coverage'], 'finding_count': len(check['findings']),
                    'unconnected_tables': check['consumer_coverage']['unconnected'],
                }
                report['ok'] = check['status'] == 'matched' and check['close_ready']

    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        report = {'ok': False, 'error': str(exc)}
    report['elapsed_seconds'] = round(time.monotonic() - start, 3)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
