#!/usr/bin/env python3
"""Restore a standalone SQLite backup into temporary storage and inspect it."""
import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path, help='complete, standalone SQLite backup file')
    args = parser.parse_args()
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
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('verify_backup.py')), str(restored)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError('restored backup verification could not run')
            verification = json.loads(result.stdout)
            report = {'ok': result.returncode == 0 and verification.get('ok') is True,
                      'verification': verification}
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        report = {'ok': False, 'error': str(exc)}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
