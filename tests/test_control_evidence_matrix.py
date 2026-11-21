"""M8: every control in docs/controls_laplace.md points at code and tests that really exist."""
import ast
import re
import unittest
from pathlib import Path

from database.db_manager import DBManager

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "controls_laplace.md"


def _rows():
    rows = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"C-\d{2}", cells[0]):
            rows.append(cells)
    return rows


def defined_tests(path):
    """Names of the functions a Python file really defines. Parsed, not searched as text: a test that was commented out
    (or only mentioned in a string) must not count as evidence."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


class ControlEvidenceMatrixTests(unittest.TestCase):
    def test_commented_out_or_quoted_tests_are_not_evidence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test_sample.py"
            path.write_text('def test_real():\n    pass\n# def test_commented(self):\n#     pass\nNOTE = "def test_quoted(self): pass"\n',
                            encoding="utf-8")
            self.assertEqual(defined_tests(path), {"test_real"})

    def test_every_control_names_existing_code_and_existing_tests(self):
        rows = _rows()
        self.assertGreaterEqual(len(rows), 10)
        self.assertEqual(len({row[0] for row in rows}), len(rows), "control IDs must be unique")
        for control_id, description, location, evidence in rows:
            with self.subTest(control=control_id):
                self.assertTrue(description)
                for part in [item.strip() for item in location.split(",") if item.strip()]:
                    self.assertTrue((ROOT / part).is_file(), f"{part} does not exist")
                references = [item.strip() for item in evidence.split(",") if item.strip()]
                self.assertTrue(references, "a control needs at least one piece of evidence")
                for reference in references:
                    path, _, test_name = reference.partition("::")
                    self.assertTrue(test_name.startswith("test_"), reference)
                    source = (ROOT / path)
                    self.assertTrue(source.is_file(), f"{path} does not exist")
                    self.assertIn(test_name, defined_tests(source), f"{reference} is not defined")

    def test_default_database_directory_is_quarantined_during_tests(self):
        live = ROOT / "database"
        self.assertNotEqual(Path(DBManager.database_dir).resolve(), live.resolve())
        self.assertFalse((Path(DBManager.database_dir) / "inazina.db").resolve() == (live / "inazina.db").resolve())


if __name__ == "__main__":
    unittest.main()
