"""The bell inbox and payout choice as the web server exposes them."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager, pg_backend
from database.db_manager import DBManager
from managers.notification_manager import NotificationManager
from web_api import server


@unittest.skipIf(pg_backend.enabled(), "the web server tests open the SQLite database file directly")
class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_dir, self.old_database = DBManager.database_dir, server.DATABASE
        DBManager.database_dir = Path(self.tempdir.name)
        server.DATABASE = Path(self.tempdir.name) / "inazina.db"
        self.db = DatabaseManager()
        self.db.users.create_user("alice", "Alice", "낙원", 1000)
        self.db.users.create_user("bob", "Bob", "낙원", 1000)
        with self.db.atomic() as conn:
            for number in range(3):
                NotificationManager.add(conn, "alice", "test", f"알림 {number}", "내용", now=1000 + number)
            NotificationManager.add(conn, "bob", "test", "남의 알림", now=1000)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir, server.DATABASE = self.old_dir, self.old_database
        self.tempdir.cleanup()

    def test_inbox_lists_newest_first_with_an_unread_badge_that_opening_clears(self):
        inbox = server.user_notifications("alice")
        self.assertEqual([item["title"] for item in inbox["notifications"]], ["알림 2", "알림 1", "알림 0"])
        self.assertEqual(inbox["unread"], 3)
        self.assertEqual(server.change_notifications("alice", "read-all")["changed"], 3)
        self.assertEqual(server.user_notifications("alice")["unread"], 0)
        self.assertEqual(server.user_notifications("bob")["unread"], 1)           # others are untouched

    def test_one_notification_or_the_whole_inbox_can_be_deleted_but_only_your_own(self):
        first = server.user_notifications("alice")["notifications"][0]["id"]
        theirs = server.user_notifications("bob")["notifications"][0]["id"]
        self.assertEqual(server.change_notifications("alice", "delete", theirs)["changed"], 0)
        self.assertEqual(server.change_notifications("alice", "delete", first)["changed"], 1)
        self.assertEqual(len(server.user_notifications("alice")["notifications"]), 2)
        self.assertEqual(server.change_notifications("alice", "delete-all")["changed"], 2)
        self.assertEqual(server.user_notifications("alice")["notifications"], [])
        self.assertEqual(len(server.user_notifications("bob")["notifications"]), 1)
        with self.assertRaises(ValueError):
            server.change_notifications("alice", "explode")

    def test_the_submitter_is_told_when_their_inquiry_is_read_and_done(self):
        found = server.submit_feedback({"kind": "contact", "title": "문의드립니다", "body": "내용", "discordId": "alice",
                                        "email": "a@example.com", "consent": True})
        server.admin_feedback_action("bob", found["id"], {"status": "read"})
        server.admin_feedback_action("bob", found["id"], {"status": "done", "reply": "안내드렸습니다."})
        notes = [n for n in server.user_notifications("alice")["notifications"] if n["kind"] == "feedback"]
        self.assertEqual([n["title"] for n in notes], ["문의가 처리되었습니다", "문의가 확인되었습니다"])
        self.assertIn("답변: 안내드렸습니다.", notes[0]["body"])
        stranger = server.submit_feedback({"kind": "report", "title": "신고", "body": "내용", "discordId": "nobody",
                                           "email": "a@example.com", "consent": True})
        server.admin_feedback_action("bob", stranger["id"], {"status": "done"})   # not a player: nothing to notify

    def test_unknown_payout_actions_and_other_peoples_payouts_are_refused(self):
        with self.assertRaises(ValueError):
            server.choose_payout("alice", "steal", 1)
        with self.assertRaises(ValueError):
            server.choose_payout("alice", "convert", 999)
        self.assertEqual(server.user_payouts("alice"), {"payouts": []})


if __name__ == "__main__":
    unittest.main()
