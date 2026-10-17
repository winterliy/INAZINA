import json
import tempfile
import time
import unittest
from pathlib import Path

from web_api import documents

NOW = 1792200000  # 2026-10-17 10:40 KST
OCT_24, NOV_16 = 1792767600, 1794754800  # 0:00 KST, 7 and 30 days later


class DocumentEditingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.saved = documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT
        documents.ROOT, documents.LINKS, documents.HISTORY = root, root, root / "history"
        documents.DOCUMENTS = {
            "privacy": ("개인정보 처리방침", root / "privacy.txt", "site"),
            "help": ("도움말", root / "help.txt", "site"),
            "registration": ("등록 안내", root / "private.text", "discord"),
        }
        (root / "privacy.txt").write_text("# 방침\n시행일: 2026년 10월 17일\n본문\n", encoding="utf-8")
        (root / "help.txt").write_text("도움말\n", encoding="utf-8")
        (root / "private.text").write_text("안내\n", encoding="utf-8")
        self.root = root

    def tearDown(self):
        documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT = self.saved
        self.folder.cleanup()

    def edit(self, text, important=False, document="privacy"):
        base = documents.read_document(document)["sha256"]
        return documents.save(document, text, base, True, important, now=NOW)

    def test_notice_periods_are_7_or_30_days_to_0_00_kst(self):
        self.assertEqual(documents.effective_epoch(False, NOW), OCT_24)
        self.assertEqual(documents.effective_epoch(True, NOW), NOV_16)

    def test_a_legal_revision_is_announced_now_and_applied_after_the_notice(self):
        change = self.edit("# 방침\n시행일: 2026년 10월 17일\n새 본문\n")
        self.assertEqual(change["scheduled"], OCT_24)
        document = documents.read_document("privacy")
        # In force: the old text. Announced: the revision with its 시행일 filled in.
        self.assertIn("본문", document["content"])
        self.assertNotIn("새 본문", document["content"])
        self.assertIn("시행일: 2026년 10월 24일", document["pending"]["content"])
        self.assertEqual(json.loads((self.root / "pending.json").read_text())["privacy"]["effectiveEpoch"], OCT_24)
        self.assertTrue((self.root / "privacy.next.txt").exists())
        self.assertEqual(documents.apply_due(now=OCT_24 - 1), [])
        applied = documents.apply_due(now=OCT_24)
        self.assertEqual([item["document"] for item in applied], ["privacy"])
        self.assertIn("새 본문", documents.read_document("privacy")["content"])
        self.assertIsNone(documents.read_document("privacy")["pending"])
        self.assertFalse((self.root / "privacy.next.txt").exists())
        self.assertEqual(json.loads((self.root / "pending.json").read_text()), {})
        self.assertEqual(len(documents.read_document("privacy")["history"]), 1)

    def test_important_change_waits_30_days_and_a_missing_시행일_line_is_added(self):
        self.edit("# 방침\n다른 본문\n", important=True)
        pending = documents.read_document("privacy")["pending"]
        self.assertEqual(pending["effectiveEpoch"], NOV_16)
        self.assertTrue(pending["content"].startswith("# 방침\n시행일: 2026년 11월 16일\n"))

    def test_editing_again_replaces_the_announcement_and_cancel_withdraws_it(self):
        self.edit("# 방침\n시행일: x\n첫 개정\n")
        preview = documents.preview("privacy", "# 방침\n시행일: x\n둘째 개정\n", now=NOW)
        self.assertTrue(any("이미 공지한" in warning for warning in preview["warnings"]))
        self.edit("# 방침\n시행일: x\n둘째 개정\n", important=True)
        self.assertIn("둘째 개정", documents.read_document("privacy")["pending"]["content"])
        documents.cancel("privacy")
        self.assertIsNone(documents.read_document("privacy")["pending"])
        with self.assertRaisesRegex(ValueError, "공지한 개정안이 없"):
            documents.cancel("privacy")

    def test_help_and_registration_change_at_once(self):
        change = self.edit("새 도움말\n", document="help")
        self.assertIsNone(change["scheduled"])
        self.assertEqual(documents.read_document("help")["content"], "새 도움말\n")
        too_long = "가" * (documents.REGISTRATION_MAX_CHARS + 1)
        with self.assertRaisesRegex(ValueError, "모달"):
            self.edit(too_long, document="registration")

    def test_save_needs_confirmation_and_the_version_it_started_from(self):
        current = documents.read_document("privacy")
        with self.assertRaisesRegex(ValueError, "확정"):
            documents.save("privacy", "새 내용", current["sha256"], False, now=NOW)
        with self.assertRaisesRegex(ValueError, "다른 곳에서"):
            documents.save("privacy", "새 내용", "0" * 64, True, now=NOW)
        with self.assertRaisesRegex(ValueError, "찾을 수 없"):
            documents.read_document("../../etc/passwd")
        with self.assertRaisesRegex(ValueError, "빈 문서"):
            documents.preview("privacy", "   \n")

    def test_real_documents_are_all_listed(self):
        documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT = self.saved
        listed = {item["id"]: item for item in documents.list_documents()}
        self.assertEqual(set(listed), {"privacy", "terms", "operations", "internal", "help", "registration"})
        self.assertEqual({key for key, item in listed.items() if item["legal"]}, {"privacy", "terms", "operations", "internal"})
        self.assertTrue(all(item["size"] > 0 for item in listed.values()))
