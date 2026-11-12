"""Entry points are run as scripts, not imported: their imports must not depend on sys.path.

The Node API starts `python web_api/server.py`; a project import placed before the
script adds the project root to sys.path only fails there, never in module imports.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_data_service_script_imports_from_any_directory():
    code = ("import runpy, sys; sys.argv=[sys.argv[1]]; "
            "runpy.run_path(sys.argv[0], run_name='entrypoint_check')")
    with tempfile.TemporaryDirectory() as elsewhere:
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(ROOT / "web_api" / "server.py")],
            cwd=elsewhere, capture_output=True, text=True, timeout=120,
        )
    assert result.returncode == 0, result.stderr[-2000:]
