"""PostgreSQL drill cleanup must fail closed without contacting a server."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import backup_restore_drill as drill


class RestoreCleanupTests(unittest.TestCase):
    def test_cleanup_failure_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            dump = Path(temp) / 'test.dump'
            dump.write_bytes(b'PGDMP')
            admin = MagicMock()
            admin.__enter__.return_value = admin
            connect = MagicMock(side_effect=[admin, RuntimeError('cleanup unavailable')])
            output = io.StringIO()
            with patch('sys.argv', ['drill', str(dump)]), \
                 patch('psycopg.connect', connect), \
                 patch('database.backup.pg_tool', return_value='pg_restore'), \
                 patch.object(drill, 'server_dsns', return_value=('admin', lambda name: 'dbname=restored password=secret')), \
                 patch.object(drill, 'verify', return_value={'ok': True}), \
                 patch.object(drill.subprocess, 'run', return_value=MagicMock(returncode=0)) as run, \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = drill.main()
            report = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(report['ok'])
            self.assertFalse(report['cleanup_ok'])
            self.assertTrue(report['restore_database'])
            command = run.call_args.args[0]
            self.assertNotIn('secret', ' '.join(command))
            self.assertEqual(run.call_args.kwargs['env']['PGPASSWORD'], 'secret')


if __name__ == '__main__':
    unittest.main()
