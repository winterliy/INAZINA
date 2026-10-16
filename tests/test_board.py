import sqlite3
import time
import unittest

from web_api import board

ADMIN = {"userId": "admin", "role": "admin"}
ALICE = {"userId": "alice", "role": "user"}
BOB = {"userId": "bob", "role": "user"}


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE users (user_id TEXT PRIMARY KEY, nickname TEXT);
            INSERT INTO users VALUES ('admin','관리자'),('alice','앨리스'),('bob','밥'),('carol','캐롤'),('dave','데이브');
            CREATE TABLE market_assets (code TEXT); CREATE TABLE company (company_code TEXT);
            CREATE TABLE investment (product_code TEXT); CREATE TABLE saving (product_code TEXT);
            CREATE TABLE liability (product_code TEXT); CREATE TABLE treasury_bond_products (product_code TEXT);
            CREATE TABLE els_products (product_code TEXT); CREATE TABLE option_contracts (contract_code TEXT);
            CREATE TABLE currencies (asset_code TEXT);
            INSERT INTO company VALUES ('SELO');
        """)
        board.ensure_tables(self.conn)
        # Behave as after the announced opening date.
        self.opens = board.WRITE_OPENS_EPOCH
        board.WRITE_OPENS_EPOCH = 0

    def tearDown(self):
        board.WRITE_OPENS_EPOCH = self.opens
        self.conn.close()

    def post(self, user=ALICE, **payload):
        values = {"board": "free", "title": "제목", "body": "내용", **payload}
        return board.create_post(self.conn, user, values, "10.0.0.1")

    def test_anonymous_post_hides_author_but_keeps_id_and_ip(self):
        post_id = self.post(anonymous=True)
        public = board.read_post(self.conn, post_id)
        self.assertEqual(public["author"], "익명")
        self.assertNotIn("author_id", public)
        self.assertNotIn("author_ip", public)
        row = self.conn.execute("SELECT author_id,author_ip FROM board_posts").fetchone()
        self.assertEqual(tuple(row), ("alice", "10.0.0.1"))
        admin = board.admin_list(self.conn)["posts"][0]
        self.assertEqual((admin["author_id"], admin["author_ip"]), ("alice", "10.0.0.1"))

    def test_reading_marks_only_the_viewers_own_posts(self):
        post_id = self.post()
        self.assertTrue(board.read_post(self.conn, post_id, viewer="alice")["mine"])
        self.assertFalse(board.read_post(self.conn, post_id, viewer="bob")["mine"])
        self.assertFalse(board.list_posts(self.conn)["posts"][0]["mine"])

    def test_only_admins_write_notices_and_pinned_posts_come_first(self):
        with self.assertRaisesRegex(ValueError, "관리자"):
            self.post(board="notice")
        notice = self.post(ADMIN, board="notice")
        later = self.post(ADMIN, board="notice", title="나중")
        board.admin_action(self.conn, "post", notice, "pin")
        self.assertEqual([p["post_id"] for p in board.list_posts(self.conn, "notice")["posts"]], [notice, later])

    def test_market_posts_need_a_real_instrument(self):
        with self.assertRaisesRegex(ValueError, "종목"):
            self.post(board="market", assetCode="NOPE")
        post_id = self.post(board="market", assetCode="SELO")
        self.assertEqual([p["post_id"] for p in board.list_posts(self.conn, asset_code="SELO")["posts"]], [post_id])
        self.assertIsNone(board.read_post(self.conn, self.post(BOB, assetCode="SELO"))["asset_code"])

    def test_authors_edit_and_delete_only_their_own(self):
        post_id = self.post()
        with self.assertRaisesRegex(ValueError, "본인"):
            board.edit_post(self.conn, BOB, post_id, {"title": "x", "body": "y"})
        board.edit_post(self.conn, ALICE, post_id, {"title": "새 제목", "body": "새 내용"})
        self.assertEqual(board.read_post(self.conn, post_id)["title"], "새 제목")
        board.delete_post(self.conn, ALICE, post_id)
        with self.assertRaises(ValueError):
            board.read_post(self.conn, post_id)
        self.assertEqual(self.conn.execute("SELECT status FROM board_posts").fetchone()[0], "deleted")

    def test_comments_likes_and_counts(self):
        post_id = self.post()
        comment = board.add_comment(self.conn, BOB, post_id, {"body": "댓글", "anonymous": True}, "10.0.0.2")
        self.assertTrue(board.toggle_like(self.conn, BOB, post_id))
        post = board.read_post(self.conn, post_id, viewer="bob")
        self.assertEqual((post["comment_count"], post["like_count"], post["liked"]), (1, 1, True))
        self.assertEqual(post["comments"][0]["author"], "익명")
        self.assertTrue(post["comments"][0]["mine"])
        self.assertFalse(board.toggle_like(self.conn, BOB, post_id))
        board.delete_comment(self.conn, BOB, comment)
        self.assertEqual(board.read_post(self.conn, post_id)["comment_count"], 0)

    def test_three_reports_hide_until_an_admin_restores(self):
        post_id = self.post()
        for name in ("bob", "carol"):
            board.report(self.conn, {"userId": name, "role": "user"}, {"postId": post_id, "reason": "도배"}, "1.1.1.1")
        with self.assertRaisesRegex(ValueError, "이미"):
            board.report(self.conn, BOB, {"postId": post_id, "reason": "도배"}, "1.1.1.1")
        self.assertEqual(board.list_posts(self.conn)["total"], 1)
        board.report(self.conn, {"userId": "dave", "role": "user"}, {"postId": post_id, "reason": "도배"}, "1.1.1.1")
        self.assertEqual(board.list_posts(self.conn)["total"], 0)
        self.assertEqual(len(board.admin_list(self.conn, reported=True)["posts"]), 1)
        board.admin_action(self.conn, "post", post_id, "restore")
        self.assertEqual(board.list_posts(self.conn)["total"], 1)
        self.assertEqual(board.admin_list(self.conn)["reports"], [])

    def test_posting_is_throttled_and_validated(self):
        self.post(); self.post()
        with self.assertRaisesRegex(ValueError, "1분에 2개"):
            self.post()
        with self.assertRaisesRegex(ValueError, "제목"):
            self.post(BOB, title="  ")
        with self.assertRaisesRegex(ValueError, "100자"):
            self.post(BOB, title="가" * 101)
        with self.assertRaisesRegex(ValueError, "접속"):
            board.create_post(self.conn, BOB, {"board": "free", "title": "a", "body": "b"}, "")

    def test_purge_removes_post_with_its_comments_likes_and_reports(self):
        post_id = self.post()
        board.add_comment(self.conn, BOB, post_id, {"body": "c"}, "ip")
        board.toggle_like(self.conn, BOB, post_id)
        board.report(self.conn, BOB, {"postId": post_id, "reason": "r"}, "ip")
        board.admin_action(self.conn, "post", post_id, "purge")
        for table in ("board_posts", "board_comments", "board_likes", "board_reports"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_search_and_paging(self):
        for n in range(25):
            board.create_post(self.conn, {"userId": ["alice", "bob", "carol", "dave", "admin"][n % 5], "role": "user"},
                              {"board": "free", "title": f"글 {n}", "body": "사과" if n % 2 else "배"}, "ip")
            self.conn.execute("UPDATE board_posts SET created_epoch=?", (int(time.time()) - 3600,))
        self.assertEqual(len(board.list_posts(self.conn, page=2)["posts"]), 5)
        self.assertEqual(board.list_posts(self.conn, search="사과")["total"], 12)


class BoardRetentionTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def test_ips_older_than_a_year_are_erased_but_rows_stay(self):
        old = self.post()
        board.add_comment(self.conn, BOB, old, {"body": "c"}, "10.0.0.2")
        board.report(self.conn, BOB, {"postId": old, "reason": "r"}, "10.0.0.3")
        year_ago = int(time.time()) - board.IP_RETENTION_SECONDS - 1
        for table in ("board_posts", "board_comments", "board_reports"):
            self.conn.execute(f"UPDATE {table} SET created_epoch=?", (year_ago,))
        fresh = self.post(BOB)
        self.assertEqual(board.purge_expired_ips(self.conn), 3)
        self.assertEqual(self.conn.execute("SELECT author_ip FROM board_posts WHERE post_id=?", (old,)).fetchone()[0], "")
        self.assertEqual(self.conn.execute("SELECT author_ip FROM board_posts WHERE post_id=?", (fresh,)).fetchone()[0], "10.0.0.1")
        self.assertEqual(self.conn.execute("SELECT reporter_ip FROM board_reports").fetchone()[0], "")
        self.assertEqual(board.read_post(self.conn, old)["title"], "제목")
        self.assertEqual(board.purge_expired_ips(self.conn), 0)

    def test_author_deleted_posts_are_destroyed_after_a_year(self):
        gone, kept = self.post(), self.post(BOB)
        board.delete_post(self.conn, ALICE, gone)
        self.conn.execute("UPDATE board_posts SET created_epoch=?", (int(time.time()) - board.IP_RETENTION_SECONDS - 1,))
        board.purge_expired_ips(self.conn)
        self.assertEqual([row[0] for row in self.conn.execute("SELECT post_id FROM board_posts")], [kept])


class BoardOpeningTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def test_board_is_read_only_until_the_announced_date(self):
        post_id = self.post()
        board.WRITE_OPENS_EPOCH = int(time.time()) + 86400
        for attempt in (lambda: self.post(BOB),
                        lambda: board.add_comment(self.conn, BOB, post_id, {"body": "c"}, "ip"),
                        lambda: board.report(self.conn, BOB, {"postId": post_id, "reason": "r"}, "ip")):
            with self.assertRaisesRegex(ValueError, "읽기만"):
                attempt()
        self.assertEqual(board.list_posts(self.conn)["total"], 1)
        self.assertTrue(board.toggle_like(self.conn, BOB, post_id))
        self.assertEqual(board.list_posts(self.conn)["writeOpensEpoch"], board.WRITE_OPENS_EPOCH)

    def test_opening_date_is_2026_11_16_kst(self):
        import datetime, zoneinfo
        opens = datetime.datetime.fromtimestamp(self.opens, zoneinfo.ZoneInfo("Asia/Seoul"))
        self.assertEqual((opens.year, opens.month, opens.day, opens.hour), (2026, 11, 16, 0))
