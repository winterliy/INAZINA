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
        self.assertEqual(server.board_post(f"/internal/board/posts/{post_id}/react", ADMIN, {"value": -1})["reaction"], -1)
        comment = server.board_post(f"/internal/board/posts/{post_id}/comments", ADMIN, {"body": "c", "clientIp": "ip"})["commentId"]
        reply = server.board_post(f"/internal/board/posts/{post_id}/comments", ADMIN, {"body": "r", "parentId": comment, "clientIp": "ip"})["commentId"]
        self.assertEqual(server.board_post(f"/internal/board/comments/{reply}/react", ADMIN, {"value": 1})["likes"], 1)
        self.assertIn("✅", server.admin_board_action("admin-1", "post", str(post_id), "hide"))
        self.assertEqual(server.board_get("/internal/board/posts", {}, None)["total"], 0)
        self.assertEqual(self.execute("SELECT action FROM admin_audit")[-1][0], "board.post.hide")

    def test_unknown_board_paths_are_not_found(self):
        with self.assertRaises(LookupError):
            server.board_post("/internal/board/nothing", ADMIN, {})

    def test_boards_categories_and_moves_go_through_the_admin_api(self):
        server._board_ready = False
        self.assertIn("✅", server.admin_board_action("admin-1", "board", "new", "create",
                                                     {"action": "create", "boardId": "qna", "name": "질문"}))
        self.assertIn("✅", server.admin_board_action("admin-1", "category", "new", "create", {"boardId": "qna", "name": "버그"}))
        boards = server.board_get("/internal/board/boards", {}, None)["boards"]
        qna = next(b for b in boards if b["board_id"] == "qna")
        self.assertEqual([c["name"] for c in qna["categories"]], ["버그"])
        post_id = server.board_post("/internal/board/posts", ADMIN, {"board": "free", "title": "t", "body": "b", "clientIp": "ip"})["postId"]
        server.admin_board_action("admin-1", "post", str(post_id), "move", {"board": "qna", "categoryId": qna["categories"][0]["category_id"]})
        listed = server.board_get("/internal/board/posts", {"board": ["qna"], "category": [str(qna["categories"][0]["category_id"])]}, None)
        self.assertEqual([p["post_id"] for p in listed["posts"]], [post_id])
        audit = self.execute("SELECT action,payload FROM admin_audit")
        self.assertIn("board.post.move", [row[0] for row in audit])
