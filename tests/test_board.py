import sqlite3
import time
import unittest
from pathlib import Path

from web_api import board
import test_inazina as fixtures

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
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM board_posts").fetchone()[0], 0)

    def test_comments_likes_and_counts(self):
        post_id = self.post()
        comment = board.add_comment(self.conn, BOB, post_id, {"body": "댓글", "anonymous": True}, "10.0.0.2")
        self.assertEqual(board.react(self.conn, BOB, "post", post_id, 1)["reaction"], 1)
        post = board.read_post(self.conn, post_id, viewer="bob")
        self.assertEqual((post["comment_count"], post["like_count"], post["my_reaction"]), (1, 1, 1))
        self.assertEqual(post["comments"][0]["author"], "익명")
        self.assertTrue(post["comments"][0]["mine"])
        self.assertEqual(board.react(self.conn, BOB, "post", post_id, 1)["reaction"], 0)
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
        board.react(self.conn, BOB, "post", post_id, 1)
        board.report(self.conn, BOB, {"postId": post_id, "reason": "r"}, "ip")
        board.admin_action(self.conn, "post", post_id, "purge")
        for table in ("board_posts", "board_comments", "board_post_reactions", "board_reports"):
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

    def test_author_deletion_removes_the_post_and_everything_on_it_at_once(self):
        gone, kept = self.post(), self.post(BOB)
        comment = board.add_comment(self.conn, BOB, gone, {"body": "c"}, "ip")
        board.react(self.conn, BOB, "post", gone, 1)
        board.react(self.conn, ALICE, "comment", comment, -1)
        board.report(self.conn, BOB, {"postId": gone, "reason": "r"}, "ip")
        board.delete_post(self.conn, ALICE, gone)
        self.assertEqual([row[0] for row in self.conn.execute("SELECT post_id FROM board_posts")], [kept])
        for table in ("board_comments", "board_post_reactions", "board_comment_reactions", "board_reports"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)


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
        self.assertEqual(board.react(self.conn, BOB, "post", post_id, -1)["reaction"], -1)
        self.assertEqual(board.list_posts(self.conn)["writeOpensEpoch"], board.WRITE_OPENS_EPOCH)

    def test_board_opened_on_2026_10_17_kst(self):
        import datetime, zoneinfo
        opens = datetime.datetime.fromtimestamp(self.opens, zoneinfo.ZoneInfo("Asia/Seoul"))
        self.assertEqual((opens.year, opens.month, opens.day, opens.hour), (2026, 10, 17, 0))


class BoardReactionAndReplyTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def test_like_dislike_switch_and_cancel_on_posts_and_comments(self):
        post_id = self.post()
        comment = board.add_comment(self.conn, ALICE, post_id, {"body": "c"}, "ip")
        self.assertEqual(board.react(self.conn, BOB, "post", post_id, 1), {"reaction": 1, "likes": 1, "dislikes": 0})
        self.assertEqual(board.react(self.conn, BOB, "post", post_id, -1), {"reaction": -1, "likes": 0, "dislikes": 1})
        board.react(self.conn, {"userId": "carol", "role": "user"}, "post", post_id, -1)
        self.assertEqual(board.react(self.conn, BOB, "post", post_id, -1), {"reaction": 0, "likes": 0, "dislikes": 1})
        board.react(self.conn, BOB, "comment", comment, -1)
        post = board.read_post(self.conn, post_id, viewer="bob")
        self.assertEqual((post["like_count"], post["dislike_count"], post["my_reaction"]), (0, 1, 0))
        self.assertEqual((post["comments"][0]["dislike_count"], post["comments"][0]["my_reaction"]), (1, -1))
        self.assertEqual(board.list_posts(self.conn)["posts"][0]["dislike_count"], 1)
        with self.assertRaisesRegex(ValueError, "좋아요 또는 싫어요"):
            board.react(self.conn, BOB, "post", post_id, 2)
        with self.assertRaisesRegex(ValueError, "좋아요 또는 싫어요"):
            board.react(self.conn, BOB, "post", post_id, True)

    def test_replies_stay_one_level_and_reports_reach_them(self):
        post_id, other = self.post(), self.post(BOB)
        top = board.add_comment(self.conn, ALICE, post_id, {"body": "top"}, "ip")
        reply = board.add_comment(self.conn, BOB, post_id, {"body": "reply", "parentId": top}, "ip")
        nested = board.add_comment(self.conn, ALICE, post_id, {"body": "re-reply", "parentId": reply}, "ip")
        parents = dict(self.conn.execute("SELECT comment_id,parent_id FROM board_comments").fetchall())
        self.assertEqual((parents[reply], parents[nested]), (top, top))
        with self.assertRaisesRegex(ValueError, "답글"):
            board.add_comment(self.conn, ALICE, other, {"body": "x", "parentId": top}, "ip")
        board.report(self.conn, ALICE, {"commentId": reply, "reason": "r"}, "ip")
        self.assertEqual(board.admin_list(self.conn)["reports"][0]["comment_id"], reply)
        self.assertEqual(board.react(self.conn, ALICE, "comment", reply, 1)["likes"], 1)

    def test_deleting_a_comment_removes_it_and_keeps_replies_as_comments(self):
        post_id = self.post()
        top = board.add_comment(self.conn, ALICE, post_id, {"body": "top"}, "ip")
        reply = board.add_comment(self.conn, BOB, post_id, {"body": "reply", "parentId": top}, "ip")
        board.react(self.conn, BOB, "comment", top, 1)
        board.delete_comment(self.conn, ALICE, top)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM board_comments WHERE comment_id=?", (top,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM board_comment_reactions").fetchone()[0], 0)
        comments = board.read_post(self.conn, post_id)["comments"]
        self.assertEqual([(c["comment_id"], c["parent_id"], c["body"]) for c in comments], [(reply, None, "reply")])

    def test_hidden_parent_stays_as_a_placeholder_while_replies_live(self):
        post_id = self.post()
        top = board.add_comment(self.conn, ALICE, post_id, {"body": "top"}, "ip")
        reply = board.add_comment(self.conn, BOB, post_id, {"body": "reply", "parentId": top}, "ip")
        board.admin_action(self.conn, "comment", top, "hide")
        comments = board.read_post(self.conn, post_id)["comments"]
        self.assertEqual([(c["comment_id"], c["removed"], c["body"]) for c in comments],
                         [(top, True, "숨겨진 댓글입니다."), (reply, False, "reply")])
        board.admin_action(self.conn, "comment", top, "purge")
        self.assertEqual([c["parent_id"] for c in board.read_post(self.conn, post_id)["comments"]], [None])

    def test_rows_marked_deleted_by_older_versions_are_removed(self):
        post_id = self.post()
        top = board.add_comment(self.conn, ALICE, post_id, {"body": "top"}, "ip")
        reply = board.add_comment(self.conn, BOB, post_id, {"body": "reply", "parentId": top}, "ip")
        self.conn.execute("UPDATE board_comments SET status='deleted' WHERE comment_id=?", (top,))
        board.ensure_tables(self.conn)
        self.assertEqual(self.conn.execute("SELECT comment_id,parent_id FROM board_comments").fetchall()[0][:], (reply, None))

    def test_old_likes_become_likes(self):
        post_id = self.post()
        self.conn.executescript("""
            CREATE TABLE board_likes (post_id INTEGER, user_id TEXT, created_epoch INTEGER);""")
        self.conn.execute("INSERT INTO board_likes VALUES (?, 'bob', 1)", (post_id,))
        board.ensure_tables(self.conn)
        self.assertEqual(board.read_post(self.conn, post_id, viewer="bob")["my_reaction"], 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='board_likes'").fetchone())


class BoardStructureTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def test_admin_posts_and_comments_carry_the_admin_mark(self):
        post_id = self.post(ADMIN, anonymous=True)
        board.add_comment(self.conn, ADMIN, post_id, {"body": "운영진 답변"}, "ip")
        board.add_comment(self.conn, BOB, post_id, {"body": "c"}, "ip")
        post = board.read_post(self.conn, post_id)
        self.assertEqual((post["author"], post["author_admin"]), ("익명", 1))
        self.assertEqual([c["author_admin"] for c in post["comments"]], [1, 0])
        self.assertEqual(board.list_posts(self.conn)["posts"][0]["author_admin"], 1)
        self.assertEqual(board.read_post(self.conn, self.post(BOB))["author_admin"], 0)

    def test_notice_and_pin_lead_their_own_board(self):
        free, market = self.post(), self.post(BOB, board="market")
        pinned = self.post({"userId": "carol", "role": "user"})
        notice = self.post(ADMIN, board="market", notice=True)
        board.admin_action(self.conn, "post", pinned, "pin")
        ids = lambda **kw: [p["post_id"] for p in board.list_posts(self.conn, **kw)["posts"]]
        # 공지 stays in its own board, above that board's pinned posts.
        self.assertEqual(ids(board="free"), [pinned, free])
        self.assertEqual(ids(board="market"), [notice, market])
        self.assertEqual(ids(), [notice, pinned, market, free])
        board.admin_action(self.conn, "post", free, "notice")
        self.assertEqual(ids(board="free"), [free, pinned])
        board.admin_action(self.conn, "post", notice, "unnotice")
        self.assertEqual(ids(board="market"), [notice, market])  # back to newest first
        self.assertEqual(board.read_post(self.conn, notice)["notice"], 0)
        # Users cannot pin or make 공지 by themselves.
        self.assertEqual(board.read_post(self.conn, self.post(BOB, notice=True, pinned=True))["notice"], 0)

    def test_boards_are_created_edited_and_deleted_by_moving_posts(self):
        board.admin_action(self.conn, "board", "new", "create",
                           {"boardId": "qna", "name": "질문", "description": "묻고 답하기", "adminOnly": False, "sortOrder": 5})
        post_id = self.post(board="qna")
        self.assertEqual(board.read_post(self.conn, post_id)["board_name"], "질문")
        board.admin_action(self.conn, "board", "qna", "edit", {"name": "질문과 답변", "adminOnly": True, "sortOrder": 5})
        with self.assertRaisesRegex(ValueError, "관리자만"):
            self.post(BOB, board="qna")
        with self.assertRaisesRegex(ValueError, "옮길"):
            board.admin_action(self.conn, "board", "qna", "delete", {})
        board.admin_action(self.conn, "board", "qna", "delete", {"moveTo": "free"})
        self.assertEqual(board.read_post(self.conn, post_id)["board"], "free")
        self.assertNotIn("qna", [b["board_id"] for b in board.boards(self.conn)])
        for bad in ({"boardId": "Bad ID", "name": "x"}, {"boardId": "free", "name": "x"}, {"boardId": "new1", "name": "자유"}):
            with self.assertRaises(ValueError):
                board.admin_action(self.conn, "board", "new", "create", bad)

    def test_the_last_board_cannot_be_deleted(self):
        board.admin_action(self.conn, "board", "notice", "delete", {})
        board.admin_action(self.conn, "board", "market", "delete", {})
        board.admin_action(self.conn, "board", "news", "delete", {})
        with self.assertRaisesRegex(ValueError, "하나 이상"):
            board.admin_action(self.conn, "board", "free", "delete", {})

    def test_categories_and_moving_posts(self):
        talk = board.admin_action(self.conn, "category", "new", "create", {"boardId": "free", "name": "잡담"})
        tip = board.admin_action(self.conn, "category", "new", "create", {"boardId": "market", "name": "분석"})
        with self.assertRaisesRegex(ValueError, "같은 이름"):
            board.admin_action(self.conn, "category", "new", "create", {"boardId": "free", "name": "잡담"})
        post_id = self.post(categoryId=talk)
        with self.assertRaisesRegex(ValueError, "카테고리"):
            self.post(BOB, categoryId=tip)
        self.assertEqual(board.list_posts(self.conn, board="free", category=talk)["total"], 1)
        self.assertEqual(board.read_post(self.conn, post_id)["category_name"], "잡담")
        board.admin_action(self.conn, "category", talk, "edit", {"name": "수다"})
        self.assertEqual(board.boards(self.conn)[1]["categories"][0]["name"], "수다")
        # Moving to another board takes that board's category (or none).
        board.admin_action(self.conn, "post", post_id, "move", {"board": "market", "categoryId": tip})
        moved = board.read_post(self.conn, post_id)
        self.assertEqual((moved["board"], moved["category_id"]), ("market", tip))
        board.admin_action(self.conn, "post", post_id, "category", {"categoryId": None})
        self.assertIsNone(board.read_post(self.conn, post_id)["category_id"])
        board.admin_action(self.conn, "post", post_id, "category", {"categoryId": tip})
        board.admin_action(self.conn, "category", tip, "delete")
        self.assertIsNone(board.read_post(self.conn, post_id)["category_id"])

    def test_old_three_board_table_is_rebuilt_keeping_everything_attached(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE users (user_id TEXT PRIMARY KEY, nickname TEXT);
            INSERT INTO users VALUES ('alice','앨리스'),('bob','밥');
            CREATE TABLE board_posts (
                post_id INTEGER PRIMARY KEY AUTOINCREMENT,
                board TEXT NOT NULL CHECK(board IN ('notice','free','market')),
                author_id TEXT NOT NULL REFERENCES users(user_id), anonymous INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL, body TEXT NOT NULL, asset_code TEXT, pinned INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', hidden_reason TEXT, author_ip TEXT NOT NULL,
                view_count INTEGER NOT NULL DEFAULT 0, created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL);
            CREATE TABLE board_comments (
                comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES board_posts(post_id) ON DELETE CASCADE,
                author_id TEXT NOT NULL REFERENCES users(user_id), anonymous INTEGER NOT NULL DEFAULT 0,
                body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', hidden_reason TEXT,
                author_ip TEXT NOT NULL, created_epoch INTEGER NOT NULL);
            INSERT INTO board_posts(board,author_id,title,body,pinned,author_ip,view_count,created_epoch,updated_epoch)
                VALUES ('market','alice','t','b',1,'1.1.1.1',7,1,1);
            INSERT INTO board_comments(post_id,author_id,body,author_ip,created_epoch) VALUES (1,'bob','c','ip',2);
        """)
        board.ensure_tables(conn)
        post = board.read_post(conn, 1)
        self.assertEqual((post["board_name"], post["pinned"], post["view_count"], post["comment_count"]), ("시장 토론", 1, 7, 1))
        self.assertIn("REFERENCES board_boards", conn.execute("SELECT sql FROM sqlite_master WHERE name='board_posts'").fetchone()[0])
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        board.delete_post(conn, ALICE, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM board_comments").fetchone()[0], 0)
        conn.close()


class BoardAttachmentAndPollTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def upload(self, user=ADMIN, name="공지.pdf", stored=None):
        stored = stored or ("a" * 31 + str(len(name) % 10)) + "." + name.rsplit(".", 1)[1].lower()
        return board.register_upload(self.conn, user, {"name": name, "storedName": stored, "size": 1234, "sha256": "f" * 64})

    def test_admin_notice_with_files_and_links(self):
        file_id = self.upload()
        post_id = self.post(ADMIN, notice=True, attachments=[file_id],
                            links=[{"title": "안내", "url": "https://example.com/a"}, {"url": "http://example.org"}])
        attachments = board.read_post(self.conn, post_id)["attachments"]
        self.assertEqual([(a["kind"], a["name"]) for a in attachments],
                         [("file", "공지.pdf"), ("link", "안내"), ("link", "http://example.org")])
        self.assertNotIn("stored_name", attachments[0])
        self.assertTrue(board.list_posts(self.conn)["posts"][0]["has_attachments"])
        meta = board.file_meta(self.conn, file_id)
        self.assertEqual((meta["name"], meta["mime"]), ("공지.pdf", "application/pdf"))
        board.admin_action(self.conn, "post", post_id, "hide")
        with self.assertRaises(LookupError):
            board.file_meta(self.conn, file_id)

    def test_anyone_may_attach_and_uploads_are_limited_and_validated(self):
        user_file = self.upload(BOB, stored="9" * 32 + ".pdf")
        post_id = self.post(BOB, attachments=[user_file], links=[{"url": "https://example.com"}])
        self.assertEqual(len(board.read_post(self.conn, post_id)["attachments"]), 2)
        with self.assertRaisesRegex(ValueError, "올린 파일"):  # someone else's upload
            self.post(ALICE, attachments=[self.upload(stored="8" * 32 + ".pdf")])
        now = int(time.time())
        self.conn.executemany("INSERT INTO board_attachments(post_id,uploader_id,kind,name,stored_name,size,sha256,created_epoch) "
                              "VALUES (NULL,'carol','file','x.pdf',?,1,?,?)",
                              [(f"{n:032x}.pdf", "0" * 64, now) for n in range(board.UPLOADS_PER_HOUR)])
        with self.assertRaisesRegex(ValueError, "한 시간에"):
            self.upload({"userId": "carol", "role": "user"}, stored="7" * 32 + ".pdf")
        with self.assertRaisesRegex(ValueError, "올릴 수 있는 파일"):
            self.upload(name="run.exe", stored="b" * 32 + ".exe")
        with self.assertRaisesRegex(ValueError, "파일 정보"):
            self.upload(name="a.pdf", stored="../../etc/passwd")
        for url in ("javascript:alert(1)", "ftp://x", "https://a b"):
            with self.assertRaisesRegex(ValueError, "http"):
                self.post(ADMIN, links=[{"url": url}])
        # An upload can be attached once, by its uploader only.
        file_id = self.upload()
        self.post(ADMIN, attachments=[file_id])
        with self.assertRaisesRegex(ValueError, "올린 파일"):
            self.post(ADMIN, attachments=[file_id])

    def test_editing_removes_and_adds_attachments_and_deleting_cleans_files(self):
        import tempfile
        from pathlib import Path
        folder = tempfile.TemporaryDirectory()
        first = self.upload(stored="c" * 32 + ".pdf")
        post_id = self.post(ADMIN, attachments=[first])
        second = self.upload(name="b.png", stored="d" * 32 + ".png")
        board.edit_post(self.conn, ADMIN, post_id, {"title": "t", "body": "b", "removeAttachments": [first], "attachments": [second]})
        self.assertEqual([a["name"] for a in board.read_post(self.conn, post_id)["attachments"]], ["b.png"])
        for name in ("c" * 32 + ".pdf", "d" * 32 + ".png", "e" * 32 + ".txt"):
            (Path(folder.name) / name).write_text("x")
        self.conn.execute("INSERT INTO board_attachments(post_id,uploader_id,kind,name,stored_name,size,sha256,created_epoch) "
                          "VALUES (NULL,'admin','file','old.txt',?,1,?,0)", ("e" * 32 + ".txt", "0" * 64))
        later = int(time.time()) + 3600
        self.assertEqual(board.cleanup_files(self.conn, folder.name, now=later), 2)  # removed from the post + stale upload
        self.assertEqual(sorted(p.name for p in Path(folder.name).iterdir()), ["d" * 32 + ".png"])
        board.delete_post(self.conn, ADMIN, post_id)
        board.cleanup_files(self.conn, folder.name, now=later)
        self.assertEqual(list(Path(folder.name).iterdir()), [])
        folder.cleanup()

    def test_poll_single_choice_anonymous_with_scheduled_close(self):
        closes = int(time.time()) + 3600
        post_id = self.post(ADMIN, poll={"question": "회식 메뉴?", "options": ["고기", "회", "피자"], "anonymous": True, "closesEpoch": closes})
        poll = board.read_post(self.conn, post_id)["poll"]
        self.assertEqual((poll["anonymous"], poll["multiple"], poll["closes_epoch"], poll["open"]), (1, 0, closes, True))
        first, second = poll["options"][0]["option_id"], poll["options"][1]["option_id"]
        board.vote(self.conn, BOB, poll["poll_id"], [first])
        view = board.vote(self.conn, BOB, poll["poll_id"], [second])  # changing the vote
        self.assertEqual(([o["votes"] for o in view["options"]], view["my_votes"], view["voters"]), ([0, 1, 0], [second], 1))
        self.assertNotIn("voters", view["options"][0])
        with self.assertRaisesRegex(ValueError, "하나만"):
            board.vote(self.conn, BOB, poll["poll_id"], [first, second])
        self.conn.execute("UPDATE board_polls SET closes_epoch=?", (int(time.time()) - 1,))
        with self.assertRaisesRegex(ValueError, "마감"):
            board.vote(self.conn, ALICE, poll["poll_id"], [first])
        self.assertFalse(board.read_post(self.conn, post_id)["poll"]["open"])

    def test_poll_named_multiple_choice_and_admin_close(self):
        post_id = self.post(ADMIN, poll={"question": "가능한 요일", "options": ["월", "화"], "anonymous": False, "multiple": True})
        poll = board.read_post(self.conn, post_id, viewer="bob")["poll"]
        ids = [o["option_id"] for o in poll["options"]]
        view = board.vote(self.conn, BOB, poll["poll_id"], ids)
        self.assertEqual([o["voters"] for o in view["options"]], [["밥"], ["밥"]])
        board.vote(self.conn, BOB, poll["poll_id"], [])  # withdraw
        self.assertEqual(board.read_post(self.conn, post_id)["poll"]["voters"], 0)
        board.admin_action(self.conn, "poll", poll["poll_id"], "close")
        with self.assertRaisesRegex(ValueError, "마감"):
            board.vote(self.conn, BOB, poll["poll_id"], ids[:1])
        board.admin_action(self.conn, "poll", poll["poll_id"], "reopen")
        board.vote(self.conn, BOB, poll["poll_id"], ids[:1])

    def test_anyone_may_open_a_poll_and_only_its_author_or_an_admin_closes_it(self):
        post_id = self.post(BOB, poll={"question": "q", "options": ["a", "b"]})
        poll_id = board.read_post(self.conn, post_id)["poll"]["poll_id"]
        with self.assertRaisesRegex(ValueError, "만든 사람"):
            board.poll_action(self.conn, poll_id, "close", ALICE)
        self.assertEqual(board.poll_action(self.conn, poll_id, "close", BOB), 1)
        self.assertEqual(board.poll_action(self.conn, poll_id, "reopen", ADMIN), 1)

    def test_polls_are_validated(self):
        for poll, message in (({"question": "q", "options": ["a"]}, "선택지"), ({"question": "q", "options": ["a", "a"]}, "선택지"),
                              ({"question": "q", "options": ["a", "b"], "closesEpoch": 1}, "예약 마감"),
                              ({"question": "q", "options": ["a", "b"], "closesEpoch": int(time.time()) + 100 * 86400}, "90일")):
            with self.assertRaisesRegex(ValueError, message):
                self.post(ADMIN, poll=poll)


class SecretAndSuspendedBoardTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def make_secret(self):
        board.admin_action(self.conn, "board", "new", "create", {"boardId": "staff", "name": "운영진 방", "secret": True})
        board.admin_action(self.conn, "board", "staff", "member_add", {"userId": "bob"})

    def test_secret_board_is_invisible_to_everyone_but_members_and_admins(self):
        self.make_secret()
        post_id = self.post(BOB, board="staff", title="비밀 글")
        ids = lambda viewer=None, admin=False: [b["board_id"] for b in board.boards(self.conn, viewer, admin)]
        self.assertNotIn("staff", ids())
        self.assertNotIn("staff", ids("alice"))
        self.assertIn("staff", ids("bob"))
        self.assertIn("staff", ids("carol", admin=True))
        admin_view = next(b for b in board.boards(self.conn, "admin", True) if b["board_id"] == "staff")
        self.assertEqual([m["user_id"] for m in admin_view["members"]], ["bob"])
        # Lists, posts and comments behave as if the board did not exist.
        self.assertEqual(board.list_posts(self.conn, viewer="alice")["total"], 0)
        self.assertEqual(board.list_posts(self.conn, viewer="bob")["total"], 1)
        self.assertEqual(board.list_posts(self.conn, board="staff", viewer="bob")["total"], 1)
        with self.assertRaisesRegex(ValueError, "게시판을 확인"):
            board.list_posts(self.conn, board="staff", viewer="alice")
        with self.assertRaisesRegex(ValueError, "찾을 수 없거나"):
            board.read_post(self.conn, post_id, viewer="alice")
        self.assertEqual(board.read_post(self.conn, post_id, viewer="admin", admin=True)["title"], "비밀 글")
        for attempt in (lambda: self.post(ALICE, board="staff"),
                        lambda: board.add_comment(self.conn, ALICE, post_id, {"body": "c"}, "ip"),
                        lambda: board.react(self.conn, ALICE, "post", post_id, 1),
                        lambda: board.report(self.conn, ALICE, {"postId": post_id, "reason": "r"}, "ip")):
            with self.assertRaises(ValueError):
                attempt()
        board.add_comment(self.conn, BOB, post_id, {"body": "멤버 댓글"}, "ip")
        # Removing a member takes their access away at once.
        board.admin_action(self.conn, "board", "staff", "member_remove", {"userId": "bob"})
        with self.assertRaises(ValueError):
            board.read_post(self.conn, post_id, viewer="bob")

    def test_secret_board_files_need_access(self):
        self.make_secret()
        file_id = board.register_upload(self.conn, BOB, {"name": "a.pdf", "storedName": "1" * 32 + ".pdf", "size": 1, "sha256": "0" * 64})
        self.post(BOB, board="staff", attachments=[file_id])
        self.assertEqual(board.file_meta(self.conn, file_id, "bob")["name"], "a.pdf")
        self.assertEqual(board.file_meta(self.conn, file_id, "x", admin=True)["name"], "a.pdf")
        for viewer in (None, "alice"):
            with self.assertRaises(LookupError):
                board.file_meta(self.conn, file_id, viewer)

    def test_member_must_be_a_registered_user(self):
        self.make_secret()
        with self.assertRaisesRegex(ValueError, "등록된 사용자"):
            board.admin_action(self.conn, "board", "staff", "member_add", {"userId": "nobody"})

    def test_suspended_board_is_read_only_except_reports_and_admins(self):
        post_id = self.post(BOB)
        board.admin_action(self.conn, "board", "free", "suspend", {"reason": "도배 정리 중"})
        info = next(b for b in board.boards(self.conn) if b["board_id"] == "free")
        self.assertIn("도배 정리 중", info["suspension"])
        for attempt in (lambda: self.post(ALICE),
                        lambda: board.add_comment(self.conn, ALICE, post_id, {"body": "c"}, "ip"),
                        lambda: board.react(self.conn, ALICE, "post", post_id, 1),
                        lambda: board.edit_post(self.conn, BOB, post_id, {"title": "t", "body": "b"})):
            with self.assertRaisesRegex(ValueError, "정지"):
                attempt()
        self.assertEqual(board.read_post(self.conn, post_id)["post_id"], post_id)
        board.report(self.conn, ALICE, {"postId": post_id, "reason": "r"}, "ip")
        self.post(ADMIN)  # administrators still write
        board.delete_post(self.conn, BOB, post_id)  # authors may still delete
        board.admin_action(self.conn, "board", "free", "resume")
        self.post(ALICE)

    def test_suspension_with_an_end_time_lifts_by_itself(self):
        board.admin_action(self.conn, "board", "free", "suspend", {"until": int(time.time()) + 60})
        with self.assertRaisesRegex(ValueError, "까지"):
            self.post(ALICE)
        self.conn.execute("UPDATE board_boards SET suspended_until=? WHERE board_id='free'", (int(time.time()) - 1,))
        self.post(ALICE)
        with self.assertRaisesRegex(ValueError, "이후"):
            board.admin_action(self.conn, "board", "free", "suspend", {"until": 1})


class BoardNewsTests(unittest.TestCase):
    setUp, tearDown, post = BoardTests.setUp, BoardTests.tearDown, BoardTests.post

    def news(self, **impact):
        values = {"eventType": "good", "severity": 10, "durationSeconds": 3600, **impact}
        return self.post(ADMIN, board="news", title="속보", news=values)

    def market_tables(self):
        self.conn.executescript("""
            CREATE TABLE market_events (event_id INTEGER PRIMARY KEY, starts_epoch INTEGER, ends_epoch INTEGER);
            CREATE TABLE market_event_targets (event_id INTEGER, asset_code TEXT, state TEXT);
            INSERT INTO market_events VALUES (7, 0, 99999999999);
            INSERT INTO market_event_targets VALUES (7, 'SELO', 'active');""")

    def test_news_stays_hidden_until_published_then_reads_like_any_post(self):
        post_id = self.news(publishEpoch=int(time.time()) + 3600)
        self.assertEqual(board.list_posts(self.conn, viewer="bob")["total"], 0)
        with self.assertRaises(ValueError):
            board.read_post(self.conn, post_id, viewer="bob")
        with self.assertRaises(ValueError):
            board.react(self.conn, BOB, "post", post_id, 1)
        admin_view = board.read_post(self.conn, post_id, viewer="admin", admin=True)
        self.assertEqual((admin_view["news"]["status"], admin_view["news"]["event_type"]), ("scheduled", "good"))
        self.assertEqual(board.due_news(self.conn), [])
        due = board.due_news(self.conn, now=int(time.time()) + 3601)
        self.assertEqual([item["post_id"] for item in due], [post_id])
        board.mark_news(self.conn, post_id, event_id=7)
        public = board.read_post(self.conn, post_id, viewer="bob")
        self.assertEqual(public["news"], {"status": "live", "publish_epoch": public["news"]["publish_epoch"]})
        self.assertEqual(board.list_posts(self.conn)["posts"][0]["news_status"], "live")
        # Like other posts: reactions, comments and replies.
        board.react(self.conn, BOB, "post", post_id, -1)
        top = board.add_comment(self.conn, BOB, post_id, {"body": "c"}, "ip")
        board.add_comment(self.conn, ALICE, post_id, {"body": "r", "parentId": top}, "ip")
        self.assertEqual(board.read_post(self.conn, post_id)["comment_count"], 2)

    def test_failed_publication_keeps_the_news_hidden(self):
        post_id = self.news()
        board.mark_news(self.conn, post_id, error="❌ 종목 없음")
        with self.assertRaises(ValueError):
            board.read_post(self.conn, post_id, viewer="bob")
        self.assertEqual(board.read_post(self.conn, post_id, viewer="admin", admin=True)["news"]["status"], "failed")

    def test_news_that_disappears_stops_its_market_event(self):
        self.market_tables()
        for remove in (lambda post_id: board.delete_post(self.conn, ADMIN, post_id),
                       lambda post_id: board.admin_action(self.conn, "post", post_id, "hide"),
                       lambda post_id: board.admin_action(self.conn, "post", post_id, "purge")):
            self.conn.execute("UPDATE market_event_targets SET state='active'")
            self.conn.execute("UPDATE market_events SET ends_epoch=99999999999")
            post_id = self.news()
            board.mark_news(self.conn, post_id, event_id=7)
            remove(post_id)
            self.assertEqual(self.conn.execute("SELECT state FROM market_event_targets").fetchone()[0], "superseded")
            self.assertLessEqual(self.conn.execute("SELECT ends_epoch FROM market_events").fetchone()[0], time.time() + 1)

    def test_news_hidden_by_reports_stops_its_market_event(self):
        self.market_tables()
        post_id = self.news()
        board.mark_news(self.conn, post_id, event_id=7)
        for name in ("bob", "carol", "dave"):
            board.report(self.conn, {"userId": name, "role": "user"}, {"postId": post_id, "reason": "가짜"}, "ip")
        self.assertEqual(self.conn.execute("SELECT state FROM market_event_targets").fetchone()[0], "superseded")
        self.assertEqual(self.conn.execute("SELECT status FROM board_news").fetchone()[0], "ended")

    def test_news_is_validated_and_admin_only(self):
        with self.assertRaisesRegex(ValueError, "관리자"):
            self.post(BOB, board="free", news={"eventType": "good", "severity": 1, "durationSeconds": 60})
        with self.assertRaisesRegex(ValueError, "뉴스 게시판이 아닙니다"):
            self.post(ADMIN, board="free", news={"eventType": "good", "severity": 1, "durationSeconds": 60})
        for impact, message in (({"eventType": "maybe"}, "호재 또는 악재"), ({"severity": 101}, "강도"),
                                ({"durationSeconds": 10}, "영향 시간"), ({"targetPrice": 100}, "목표가"),
                                ({"publishEpoch": int(time.time()) + 100 * 86400}, "90일")):
            with self.assertRaisesRegex(ValueError, message):
                self.news(**impact)
        # A target price needs an instrument.
        post_id = self.post(ADMIN, board="news", assetCode="SELO", news={"eventType": "bad", "severity": 5, "durationSeconds": 60, "targetPrice": 900})
        self.assertEqual(board.read_post(self.conn, post_id, admin=True)["news"]["asset_code"], "SELO")


class NewsMarketIntegrationTests(unittest.TestCase):
    """A real market event from create_market_event ends when its news goes away."""
    setUp, tearDown, seed = fixtures.InazinaTests.setUp, fixtures.InazinaTests.tearDown, fixtures.InazinaTests.seed

    def test_deleting_news_supersedes_the_registered_event(self):
        self.seed()
        conn = self.db.users.conn
        board.ensure_tables(conn)
        created = {}
        ok, message = self.bot.admin_mgr.create_market_event("admin", "good", 10, None, 3600, created=created)
        self.assertTrue(ok, message)
        event_id = created["event_id"]
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM market_event_targets WHERE event_id=? AND state='active'", (event_id,)).fetchone()[0], 0)
        opens, board.WRITE_OPENS_EPOCH = board.WRITE_OPENS_EPOCH, 0
        try:
            post_id = board.create_post(conn, {"userId": "admin", "role": "admin"},
                                        {"board": "news", "title": "속보", "body": "b", "news": {"eventType": "good", "severity": 10, "durationSeconds": 3600}}, "ip")
        finally:
            board.WRITE_OPENS_EPOCH = opens
        board.mark_news(conn, post_id, event_id)
        board.delete_post(conn, {"userId": "admin", "role": "admin"}, post_id)
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_event_targets WHERE event_id=? AND state='active'", (event_id,)).fetchone()[0], 0)
        self.assertLessEqual(conn.execute("SELECT ends_epoch FROM market_events WHERE event_id=?", (event_id,)).fetchone()[0], time.time() + 1)


class StackingNewsEventTests(unittest.TestCase):
    """News events add up on the price engine; an ordinary event still replaces them."""
    setUp, tearDown, seed = fixtures.InazinaTests.setUp, fixtures.InazinaTests.tearDown, fixtures.InazinaTests.seed

    def active(self, code):
        return [row[0] for row in self.db.system.execute(
            "SELECT event_id FROM market_event_targets WHERE asset_code=? AND state='active' ORDER BY event_id", (code,))]

    def event(self, code, severity, now, stacking=True, duration=1000):
        created = {}
        ok, message = self.bot.admin_mgr.create_market_event("admin", "good", severity, code, duration, now=now,
                                                            created=created, stacking=stacking)
        self.assertTrue(ok, message)
        return created["event_id"]

    def price(self, code):
        return self.bot.market_mgr.get_asset_info(code)["price"]

    def test_two_news_on_one_asset_compound_and_removing_one_keeps_the_other(self):
        self.seed()
        code = self.db.system.execute_one("SELECT code FROM market_assets WHERE status='active' AND type='stock' LIMIT 1") \
            or self.db.system.execute_one("SELECT company_code FROM company WHERE status='active' LIMIT 1")
        code = code[0]
        start = int(time.time())
        first = self.event(code, 10, start)
        second = self.event(code, 10, start)
        self.assertEqual(self.active(code), [first, second])  # the second did not stop the first
        before = self.price(code)
        self.bot.market_mgr.update_prices(now=start + 1000)
        after = self.price(code)
        # Two +10% bridges over the same period: about +21%, not +10%.
        self.assertGreater(after / before, 1.17)
        # Removing one news stops only its share.
        third, fourth = self.event(code, 10, start + 1000), self.event(code, 20, start + 1000)
        self.db.system.execute("UPDATE market_event_targets SET state='superseded' WHERE event_id=?", (fourth,))
        base = self.price(code)
        self.bot.market_mgr.update_prices(now=start + 2000)
        ratio = self.price(code) / base
        self.assertGreater(ratio, 1.07)
        self.assertLess(ratio, 1.14)
        # An ordinary administrator event still replaces whatever is running.
        ordinary = self.event(code, 5, start + 2000, stacking=False)
        self.assertEqual(self.active(code), [ordinary])
