"""Every change to the terms or the privacy policy is announced the same way everywhere: a record with a before/after comparison, a bell
notification for every member, a post on the 공지 board written as "관리자", a public list for the official site, and a first-screen
modal in 나루 until each member has seen it."""
import unittest
from unittest.mock import patch

import test_inazina as fixtures
from web_api import announcements, board

NOW = 1_800_000_000
DAY = 86400

OLD = """# 서비스 이용약관
시행일: 2026년 10월 17일
개정: 이전 개정
## 제1조 (목적)
- A
- B
## 제2조 (정의)
- C
"""
NEW = """# 서비스 이용약관
시행일: 2026년 12월 25일
개정: 새 개정
## 제1조 (목적)
- A
- B2
## 제2조 (정의)
- C
## 제3조 (신설)
- D
"""


class CompareTests(unittest.TestCase):
    def test_only_the_changed_clauses_are_listed_under_their_headings_and_the_date_lines_are_ignored(self):
        changes = announcements.compare(OLD, NEW)
        self.assertEqual(changes, [
            {"section": "제1조 (목적)", "before": ["B"], "after": ["B2"]},
            {"section": "제3조 (신설)", "before": [], "after": ["제3조 (신설)", "D"]},
        ])

    def test_a_removed_line_and_identical_texts(self):
        self.assertEqual(announcements.compare("## 제1조\n- A\n- B\n", "## 제1조\n- A\n"), [{"section": "제1조", "before": ["B"], "after": []}])
        self.assertEqual(announcements.compare(OLD, OLD), [])

    def test_a_very_long_line_is_cut(self):
        changes = announcements.compare("## 제1조\n- A\n", "## 제1조\n- " + "가" * 900 + "\n")
        self.assertLessEqual(len(changes[0]["after"][0]), 301)


class AnnouncementTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def members(self, *rows):
        with self.db.atomic() as conn:
            for user_id, role, status in rows:
                conn.execute("INSERT OR REPLACE INTO users(user_id,nickname,nationality,cash,status,role) VALUES (?,?,'낙원',0,?,?)", (user_id, user_id, status, role))

    def open(self, document="terms", unfavourable=True, effective=NOW + 30 * DAY, old=OLD, new=NEW):
        with self.db.atomic() as conn:
            return announcements.open_announcement(conn, document, "서비스 이용약관", old, new, effective, unfavourable, now=NOW)

    def test_the_record_keeps_the_comparison_and_a_new_one_replaces_the_open_one(self):
        self.seed()
        first = self.open()
        second = self.open(effective=NOW + 40 * DAY)
        with self.db.atomic() as conn:
            self.assertEqual(announcements.get(conn, first)["status"], "replaced")
            record = announcements.get(conn, second)
        self.assertEqual((record["status"], record["document_id"], record["unfavourable"]), ("open", "terms", True))
        self.assertEqual(record["changes"][0]["section"], "제1조 (목적)")

    def test_the_text_tells_when_it_applies_and_only_an_unfavourable_change_asks_for_consent(self):
        self.seed()
        hard, easy = self.open(unfavourable=True), self.open(document="privacy", unfavourable=False)
        with self.db.atomic() as conn:
            title, body = announcements.render(announcements.get(conn, hard), "announced")
            self.assertIn("서비스 이용약관", title)
            self.assertIn("(시행 2027년 02월 14일)", title, "the whole date, not a cut-off one")
            self.assertIn("2027년 02월", body)
            self.assertIn("B2", body)
            self.assertIn("동의", body)
            self.assertIn("DM", body)
            _, plain = announcements.render(announcements.get(conn, easy), "announced")
            self.assertNotIn("동의합니다", plain)
            self.assertIn("계속 이용하면 동의한 것으로 봅니다", plain)
            self.assertIn("취소", announcements.render(announcements.get(conn, easy), "cancelled")[1])
            self.assertIn("시행", announcements.render(announcements.get(conn, easy), "applied")[0])

    def test_every_active_member_gets_one_bell_notification_per_stage_and_no_extra_discord_message(self):
        self.seed()
        self.members(("m1", "user", "active"), ("gone", "user", "withdrawn"), ("boss", "admin", "active"))
        record = self.open()
        with self.db.atomic() as conn:
            first = announcements.notify_members(conn, announcements.get(conn, record), "announced", now=NOW)
            again = announcements.notify_members(conn, announcements.get(conn, record), "announced", now=NOW)
            who = {row[0] for row in conn.execute("SELECT user_id FROM notifications WHERE kind='policy'")}
            unsent = conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='policy' AND discord_sent_epoch IS NULL").fetchone()[0]
        self.assertTrue({"citizen", "m1"} <= who)
        self.assertFalse(who & {"gone", "boss"})
        self.assertEqual(first, len(who))
        self.assertEqual(again, 0, "the same stage is never notified twice")
        self.assertEqual(unsent, 0, "the bell only: the Discord delivery loop must not send these again")

    def test_the_post_goes_to_the_notice_board_and_is_shown_as_the_administrators(self):
        self.seed()
        opens = board.WRITE_OPENS_EPOCH
        board.WRITE_OPENS_EPOCH = 0
        self.addCleanup(setattr, board, "WRITE_OPENS_EPOCH", opens)
        record = self.open()
        with self.db.atomic() as conn:
            board.ensure_tables(conn)
            post = announcements.post_to_board(conn, announcements.get(conn, record), "announced", "admin")
            shown = board.read_post(conn, post)
            listed = board.list_posts(conn, board="notice")["posts"][0]
        self.assertEqual(shown["board"], "notice")
        self.assertEqual(shown["author"], "관리자")
        self.assertEqual(listed["author"], "관리자")
        self.assertIn("B2", shown["body"])
        self.assertLessEqual(len(shown["title"]), board.TITLE_MAX)

    def test_the_public_list_is_newest_first_with_the_comparison_and_the_state(self):
        self.seed()
        first = self.open(document="privacy", unfavourable=False)
        with self.db.atomic() as conn:
            announcements.cancel(conn, "privacy", now=NOW + 1)
        self.open()
        with self.db.atomic() as conn:
            feed = announcements.feed(conn)
        self.assertEqual([item["document"] for item in feed], ["terms", "privacy"])
        self.assertEqual(feed[0]["changes"][0]["after"], ["B2"])
        self.assertEqual((feed[0]["status"], feed[1]["status"]), ("open", "cancelled"))
        self.assertTrue(feed[0]["unfavourable"] and not feed[1]["unfavourable"])
        self.assertNotIn("actor", repr(feed), "nothing about who saved it is public")
        self.assertEqual(first, feed[1]["id"])

    def test_a_member_sees_each_announcement_once_when_first_entering(self):
        self.seed()
        self.members(("m1", "user", "active"))
        record = self.open()
        with self.db.atomic() as conn:
            self.assertEqual([item["id"] for item in announcements.unseen(conn, "m1", now=NOW + DAY)], [record])
            announcements.mark_seen(conn, "m1", [record], now=NOW + DAY)
            self.assertEqual(announcements.unseen(conn, "m1", now=NOW + 2 * DAY), [])
            self.assertEqual(len(announcements.unseen(conn, "citizen", now=NOW + DAY)), 1, "each member has their own")
            announcements.mark_applied(conn, "terms", now=NOW + 30 * DAY)
            self.assertEqual([item["stage"] for item in announcements.unseen(conn, "m1", now=NOW + 30 * DAY)], ["applied"], "told again when it takes effect")
            self.assertEqual(announcements.unseen(conn, "m1", now=NOW + 60 * DAY), [], "but not forever")

    def test_a_cancelled_announcement_is_not_shown_in_the_modal(self):
        self.seed()
        self.open()
        with self.db.atomic() as conn:
            cancelled = announcements.cancel(conn, "terms", now=NOW + DAY)
            self.assertEqual(cancelled["document_id"], "terms")
            self.assertEqual(announcements.unseen(conn, "citizen", now=NOW + 2 * DAY), [])
            self.assertIsNone(announcements.cancel(conn, "terms"), "nothing left to cancel")


class OrchestrationTests(unittest.TestCase):
    """The console calls these three: announcing, withdrawing and the effective date; each reaches every channel."""
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def board_open(self):
        opens = board.WRITE_OPENS_EPOCH
        board.WRITE_OPENS_EPOCH = 0
        self.addCleanup(setattr, board, "WRITE_OPENS_EPOCH", opens)

    def counts(self, conn):
        return (conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='policy'").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM board_posts WHERE board='notice'").fetchone()[0])

    def test_announcing_a_revision_reaches_the_bell_the_board_and_the_public_list(self):
        self.seed()
        self.board_open()
        with self.db.atomic() as conn:
            record = announcements.announce(conn, "terms", "서비스 이용약관", OLD, NEW, NOW + 30 * DAY, True, "admin", campaign_id=7, now=NOW)
            bell, posts = self.counts(conn)
            self.assertGreaterEqual(bell, 1)
            self.assertEqual(posts, 1)
            self.assertEqual(announcements.feed(conn)[0]["id"], record["id"])
            self.assertEqual(announcements.get(conn, record["id"])["campaign_id"], 7)

    def test_withdrawing_and_the_effective_date_each_notify_again_and_close_the_record(self):
        self.seed()
        self.board_open()
        with self.db.atomic() as conn:
            announcements.announce(conn, "terms", "서비스 이용약관", OLD, NEW, NOW + 30 * DAY, False, "admin", now=NOW)
            first = self.counts(conn)
            self.assertIsNone(announcements.retract(conn, "privacy"), "nothing announced for that document")
            applied = announcements.take_effect(conn, "terms", now=NOW + 30 * DAY)
            second = self.counts(conn)
            self.assertEqual(applied["status"], "applied")
            self.assertGreater(second[0], first[0], "a second notification: it is in force now")
            self.assertEqual(second[1], first[1] + 1)
            self.assertIsNone(announcements.take_effect(conn, "terms"), "only once")
            announcements.announce(conn, "terms", "서비스 이용약관", NEW, OLD, NOW + 60 * DAY, False, "admin", now=NOW + 31 * DAY)
            retracted = announcements.retract(conn, "terms", now=NOW + 32 * DAY)
            self.assertEqual(retracted["status"], "cancelled")
            self.assertEqual(announcements.feed(conn)[0]["status"], "cancelled")

    def test_a_board_that_refuses_does_not_stop_the_other_channels(self):
        self.seed()  # the board is still closed to writers (its opening date is in the future)
        opens = board.WRITE_OPENS_EPOCH
        board.WRITE_OPENS_EPOCH = 4_000_000_000
        self.addCleanup(setattr, board, "WRITE_OPENS_EPOCH", opens)
        with self.db.atomic() as conn:
            record = announcements.announce(conn, "terms", "서비스 이용약관", OLD, NEW, NOW + DAY, False, "admin", now=NOW)
            self.assertIsNotNone(record)
            self.assertGreaterEqual(self.counts(conn)[0], 1)


class MemberViewTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def test_the_first_screen_gets_what_is_unseen_with_the_text_and_whether_agreement_is_still_open(self):
        from managers.document_consent import DocumentConsent
        self.seed()
        opens = board.WRITE_OPENS_EPOCH
        board.WRITE_OPENS_EPOCH = 0
        self.addCleanup(setattr, board, "WRITE_OPENS_EPOCH", opens)
        with self.db.atomic() as conn:
            campaign = DocumentConsent.open_campaign(conn, "terms", "서비스 이용약관", "abc", "s", NOW + 30 * DAY, now=NOW)["campaignId"]
            announcements.announce(conn, "terms", "서비스 이용약관", OLD, NEW, NOW + 30 * DAY, True, "admin", campaign_id=campaign, now=NOW)
            items = announcements.member_view(conn, "citizen", "unseen", now=NOW + DAY)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual((item["document"], item["stage"], item["unfavourable"], item["campaignId"], item["consented"]), ("terms", "announced", True, campaign, False))
            self.assertIn("B2", item["text"]["body"])
            self.assertEqual(item["changes"][0]["after"], ["B2"])
            DocumentConsent.consent(conn, campaign, "citizen", now=NOW + 2 * DAY)
            self.assertIs(announcements.member_view(conn, "citizen", "unseen", now=NOW + 2 * DAY)[0]["consented"], True)

    def test_a_plain_change_has_no_consent_to_give_and_the_list_shows_everything_recent(self):
        self.seed()
        opens = board.WRITE_OPENS_EPOCH
        board.WRITE_OPENS_EPOCH = 0
        self.addCleanup(setattr, board, "WRITE_OPENS_EPOCH", opens)
        with self.db.atomic() as conn:
            announcements.announce(conn, "privacy", "개인정보 처리방침", OLD, NEW, NOW + 7 * DAY, False, "admin", now=NOW)
            only = announcements.member_view(conn, "citizen", "unseen", now=NOW)[0]
            self.assertIsNone(only["campaignId"])
            self.assertIsNone(only["consented"])
            announcements.mark_seen(conn, "citizen", [only["id"]], now=NOW)
            self.assertEqual(announcements.member_view(conn, "citizen", "unseen", now=NOW), [])
            everything = announcements.member_view(conn, "citizen", "all", now=NOW)
            self.assertEqual([item["document"] for item in everything], ["privacy"])
            self.assertNotIn("actor", repr(everything))


if __name__ == "__main__":
    unittest.main()
