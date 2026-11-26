"""A console save publishes at once (the files go to the public repository and the announcement to every channel). So wording is worked out
as a draft first: kept on this machine only, never in the public folder, and cleared when the revision is finally announced."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_api import documents


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.saved = documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT, documents.DRAFTS
        documents.ROOT, documents.LINKS, documents.HISTORY, documents.DRAFTS = root, root / "links", root / "history", root / "drafts"
        documents.LINKS.mkdir()
        documents.DOCUMENTS = {"privacy": ("개인정보 처리방침", documents.LINKS / "privacy.txt", "site"), "help": ("도움말", documents.LINKS / "help.txt", "site")}
        (documents.LINKS / "privacy.txt").write_text("# 방침\n시행일: 2026년 10월 17일\n본문\n", encoding="utf-8")
        (documents.LINKS / "help.txt").write_text("도움말\n", encoding="utf-8")

    def tearDown(self):
        documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT, documents.DRAFTS = self.saved
        self.folder.cleanup()

    def test_a_draft_is_kept_outside_the_public_folder_and_never_published(self):
        with patch.dict(os.environ, {"INAZINA_PUBLISH_DOCUMENTS": "1"}), patch.object(documents.subprocess, "Popen") as popen:
            saved = documents.save_draft("privacy", "# 방침\n고치는 중인 문구\n", now=1792200000)
        popen.assert_not_called()
        self.assertEqual(saved["updated"], 1792200000)
        self.assertEqual((documents.DRAFTS / "privacy.txt").read_text(encoding="utf-8"), "# 방침\n고치는 중인 문구\n")
        self.assertEqual(sorted(path.name for path in documents.LINKS.iterdir()), ["help.txt", "privacy.txt"], "nothing new in the public folder")
        self.assertIn("고치는 중인", (documents.DRAFTS / "privacy.txt").read_text(encoding="utf-8"))
        self.assertNotIn("고치는 중인", (documents.LINKS / "privacy.txt").read_text(encoding="utf-8"))

    def test_the_draft_comes_back_with_the_document_and_an_empty_save_discards_it(self):
        self.assertIsNone(documents.read_document("privacy")["draft"])
        documents.save_draft("privacy", "초안\n", now=1792200000)
        draft = documents.read_document("privacy")["draft"]
        self.assertEqual(draft["content"], "초안\n")
        self.assertEqual(documents.read_document("help")["draft"], None, "each document has its own")
        self.assertIsNone(documents.save_draft("privacy", "   \n"))
        self.assertIsNone(documents.read_document("privacy")["draft"])

    def test_announcing_the_revision_clears_the_draft(self):
        documents.save_draft("help", "새 도움말\n")
        base = documents.read_document("help")["sha256"]
        documents.save("help", "새 도움말\n", base, True, False, now=1792200000)
        self.assertIsNone(documents.read_document("help")["draft"])

    def test_only_known_documents_and_sane_sizes(self):
        with self.assertRaises(ValueError):
            documents.save_draft("nothing", "x")
        with self.assertRaises(ValueError):
            documents.save_draft("privacy", "가" * (documents.MAX_CHARS + 1))

    def test_the_preview_says_that_confirming_is_a_public_announcement(self):
        notes = " ".join(documents.preview("privacy", "# 방침\n시행일: 2026년 10월 17일\n새 본문\n")["notes"])
        self.assertIn("공개", notes)
        self.assertIn("이력", notes, "a cancelled revision stays in the public history")
        quiet = " ".join(documents.preview("help", "새 도움말\n")["notes"])
        self.assertNotIn("공개 저장소", quiet, "help changes at once and has no announcement")


if __name__ == "__main__":
    unittest.main()
