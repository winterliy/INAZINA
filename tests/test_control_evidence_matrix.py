"""M8: every control in docs/controls_laplace.md points at code and tests that really exist."""
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


class ControlEvidenceMatrixTests(unittest.TestCase):
    def test_every_control_names_existing_code_and_existing_tests(self):
        rows = _rows()
        self.assertGreaterEqual(len(rows), 10)
        self.assertEqual(len({row[0] for row in rows}), len(rows), "control IDs must be unique")
        for control_id, description, location, evidence in rows:
            with self.subTest(control=control_id):
                self.assertTrue(description)
                self.assertTrue((ROOT / location).is_file(), f"{location} does not exist")
                references = [item.strip() for item in evidence.split(",") if item.strip()]
                self.assertTrue(references, "a control needs at least one piece of evidence")
                for reference in references:
                    path, _, test_name = reference.partition("::")
                    self.assertTrue(test_name.startswith("test_"), reference)
                    source = (ROOT / path)
                    self.assertTrue(source.is_file(), f"{path} does not exist")
                    self.assertRegex(source.read_text(encoding="utf-8"),
                                     rf"def {re.escape(test_name)}\(", f"{reference} is not defined")

    def test_default_database_directory_is_quarantined_during_tests(self):
        live = ROOT / "database"
        self.assertNotEqual(Path(DBManager.database_dir).resolve(), live.resolve())
        self.assertFalse((Path(DBManager.database_dir) / "inazina.db").resolve() == (live / "inazina.db").resolve())


if __name__ == "__main__":
    unittest.main()
