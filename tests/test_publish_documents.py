"""The public site shows apps/site/public/links/*, so a change made in the console (a saved revision, its cancellation, the effective date
swapping the text in) must be committed and pushed to get there. The publisher commits only those files; documents asks for it."""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_api import documents

TOOL = Path(__file__).resolve().parents[1] / "tools" / "publish_documents.py"
spec = importlib.util.spec_from_file_location("publish_documents", TOOL)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        links = self.root / "apps" / "site" / "public" / "links"
        links.mkdir(parents=True)
        (links / "terms.txt").write_text("a\n")
        (links / "privacy.txt").write_text("b\n")
        (links / "old.next.txt").write_text("c\n")
        (self.root / "other.py").write_text("x = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.links = links

    def tearDown(self):
        self.folder.cleanup()

    def test_only_changed_document_files_are_found_whatever_else_changed(self):
        self.assertEqual(publisher.changed_paths(self.root), [])
        (self.links / "terms.txt").write_text("changed\n")
        (self.links / "new.next.txt").write_text("draft\n")        # a newly announced revision
        (self.links / "old.next.txt").unlink()                      # a revision that took effect or was withdrawn
        (self.root / "other.py").write_text("x = 2\n")             # someone else's work in the same tree
        self.assertEqual(publisher.changed_paths(self.root), [
            "apps/site/public/links/new.next.txt", "apps/site/public/links/old.next.txt", "apps/site/public/links/terms.txt"])

    def test_nothing_changed_means_no_commit(self):
        calls = []
        self.assertEqual(publisher.publish(self.root, runner=lambda *a, **k: calls.append(a) or SimpleResult()), 0)
        self.assertEqual(calls, [])

    def test_the_landing_tool_gets_exactly_the_changed_files(self):
        (self.links / "terms.txt").write_text("changed\n")
        (self.root / "other.py").write_text("x = 2\n")
        calls = []
        publisher.publish(self.root, runner=lambda command, **k: calls.append(command) or SimpleResult())
        command = calls[0]
        self.assertTrue(command[1].endswith("land_commit.py"))
        files = command[command.index("--files") + 1:command.index("--verify")]
        self.assertEqual(files, ["apps/site/public/links/terms.txt"], "never the other session's file")


class SimpleResult:
    returncode = 0


class DocumentsAskForPublishingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.saved = documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT
        documents.ROOT, documents.LINKS, documents.HISTORY = root, root, root / "history"
        documents.DOCUMENTS = {"help": ("도움말", root / "help.txt", "site"), "privacy": ("방침", root / "privacy.txt", "site")}
        (root / "help.txt").write_text("도움말\n", encoding="utf-8")
        (root / "privacy.txt").write_text("# 방침\n시행일: 2026년 10월 17일\n본문\n", encoding="utf-8")

    def tearDown(self):
        documents.DOCUMENTS, documents.HISTORY, documents.LINKS, documents.ROOT = self.saved
        self.folder.cleanup()

    def save(self, document="help", text="새 도움말\n"):
        return documents.save(document, text, documents.read_document(document)["sha256"], True, False, now=1792200000)

    def test_a_saved_document_asks_the_publisher_when_publishing_is_on(self):
        with patch.dict(os.environ, {"INAZINA_PUBLISH_DOCUMENTS": "1"}), patch.object(documents.subprocess, "Popen") as popen:
            self.save()
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(str(popen.call_args.args[0][1]).endswith("publish_documents.py"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"], "it must outlive the request")

    def test_nothing_is_started_unless_publishing_is_switched_on(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(documents.subprocess, "Popen") as popen:
            os.environ.pop("INAZINA_PUBLISH_DOCUMENTS", None)
            self.save()
        popen.assert_not_called()

    def test_announcing_cancelling_and_the_effective_date_each_ask(self):
        with patch.dict(os.environ, {"INAZINA_PUBLISH_DOCUMENTS": "1"}), patch.object(documents.subprocess, "Popen") as popen:
            self.save("privacy", "# 방침\n새 본문\n")        # announced: the draft and pending.json become public
            self.assertEqual(popen.call_count, 1)
            documents.cancel("privacy")
            self.assertEqual(popen.call_count, 2)
            self.save("privacy", "# 방침\n또 새 본문\n")
            self.assertEqual(popen.call_count, 3)
            self.assertTrue(documents.apply_due(now=1792200000 + 10 * 86400), "the announced text is swapped in")
        self.assertEqual(popen.call_count, 4, "the swap at the effective date is published too")


if __name__ == "__main__":
    unittest.main()
