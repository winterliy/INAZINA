import unittest

import test_web_api as fixtures
from web_api import server

ADMIN = {"userId": "admin-1", "role": "admin"}


class BoardApiTests(unittest.TestCase):
    execute = fixtures.WebApiTests.execute

    def setUp(self):
        fixtures.WebApiTests.setUp(self)
        self.opens = server.board.WRITE_OPENS_EPOCH
        server.board.WRITE_OPENS_EPOCH = 0

    def tearDown(self):
        server.board.WRITE_OPENS_EPOCH = self.opens
        server._board_ready = False
        fixtures.WebApiTests.tearDown(self)

    def test_write_read_moderate_round_trip(self):
        server._board_ready = False
        created = server.board_post("/internal/board/posts", ADMIN,
                                    {"board": "notice", "title": "공지", "body": "본문", "anonymous": True, "clientIp": "203.0.113.7"})
        post_id = created["postId"]
        listed = server.board_get("/internal/board/posts", {"board": ["notice"]}, None)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["posts"][0]["author"], "익명")
        read = server.board_get(f"/internal/board/posts/{post_id}", {"view": ["1"]}, "admin-1")["post"]
        self.assertTrue(read["mine"])
        self.assertEqual(read["view_count"], 1)
        self.assertNotIn("author_ip", read)
        self.assertEqual(self.execute("SELECT author_ip FROM board_posts"), [("203.0.113.7",)])
        server.board_post(f"/internal/board/posts/{post_id}/comments", ADMIN, {"body": "댓글", "clientIp": "203.0.113.7"})
        self.assertTrue(server.board_post(f"/internal/board/posts/{post_id}/like", ADMIN, {})["liked"])
        self.assertIn("✅", server.admin_board_action("admin-1", "post", str(post_id), "hide"))
        self.assertEqual(server.board_get("/internal/board/posts", {}, None)["total"], 0)
        self.assertEqual(self.execute("SELECT action FROM admin_audit")[-1][0], "board.post.hide")

    def test_unknown_board_paths_are_not_found(self):
        with self.assertRaises(LookupError):
            server.board_post("/internal/board/nothing", ADMIN, {})
