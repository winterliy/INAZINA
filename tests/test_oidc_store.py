import sqlite3
import time
import unittest

import test_web_api as fixtures
from web_api import oidc_store, server


def connect():
    connection = sqlite3.connect(":memory:")
    oidc_store.ensure_table(connection)
    return connection


class OidcStoreTests(unittest.TestCase):
    def test_upsert_find_roundtrip(self):
        c = connect()
        oidc_store.upsert(c, "Session", "s1", {"a": 1}, int(time.time()) + 60, grant_id="g1", uid="u1", user_code="c1")
        self.assertEqual(oidc_store.find(c, "Session", "s1"), {"a": 1})
        self.assertEqual(oidc_store.find_by_uid(c, "Session", "u1"), {"a": 1})
        self.assertEqual(oidc_store.find_by_user_code(c, "Session", "c1"), {"a": 1})
        oidc_store.upsert(c, "Session", "s1", {"a": 2}, int(time.time()) + 60)
        self.assertEqual(oidc_store.find(c, "Session", "s1"), {"a": 2})

    def test_expired_row_not_returned(self):
        c = connect()
        oidc_store.upsert(c, "Session", "old", {"a": 1}, int(time.time()) - 1, uid="u-old")
        self.assertIsNone(oidc_store.find(c, "Session", "old"))
        self.assertIsNone(oidc_store.find_by_uid(c, "Session", "u-old"))

    def test_consume_marks_consumed_once(self):
        c = connect()
        oidc_store.upsert(c, "AuthorizationCode", "code1", {"x": 1}, int(time.time()) + 60)
        self.assertNotIn("consumed", oidc_store.find(c, "AuthorizationCode", "code1"))
        oidc_store.consume(c, "AuthorizationCode", "code1")
        first = oidc_store.find(c, "AuthorizationCode", "code1")["consumed"]
        self.assertGreater(first, 0)
        oidc_store.consume(c, "AuthorizationCode", "code1")
        self.assertEqual(oidc_store.find(c, "AuthorizationCode", "code1")["consumed"], first)

    def test_revoke_by_grant_id_deletes_all_models(self):
        c = connect()
        far = int(time.time()) + 60
        oidc_store.upsert(c, "AccessToken", "t1", {}, far, grant_id="g1")
        oidc_store.upsert(c, "RefreshToken", "r1", {}, far, grant_id="g1")
        oidc_store.upsert(c, "AccessToken", "t2", {}, far, grant_id="g2")
        oidc_store.revoke_by_grant_id(c, "g1")
        self.assertIsNone(oidc_store.find(c, "AccessToken", "t1"))
        self.assertIsNone(oidc_store.find(c, "RefreshToken", "r1"))
        self.assertEqual(oidc_store.find(c, "AccessToken", "t2"), {})

    def test_destroy_removes_one_row(self):
        c = connect()
        oidc_store.upsert(c, "Session", "s1", {"a": 1}, int(time.time()) + 60)
        oidc_store.destroy(c, "Session", "s1")
        self.assertIsNone(oidc_store.find(c, "Session", "s1"))

    def test_models_with_the_same_id_do_not_collide(self):
        c = connect()
        far = int(time.time()) + 60
        oidc_store.upsert(c, "Session", "same", {"m": "session"}, far)
        oidc_store.upsert(c, "Grant", "same", {"m": "grant"}, far)
        self.assertEqual(oidc_store.find(c, "Grant", "same"), {"m": "grant"})


class OidcStoreRouteTests(unittest.TestCase):
    def setUp(self):
        fixtures.WebApiTests.setUp(self)

    def tearDown(self):
        fixtures.WebApiTests.tearDown(self)

    def test_route_roundtrip_and_unknown_operation(self):
        far = int(time.time()) + 60
        server.oidc_store_call("upsert", {"model": "Session", "id": "s1", "payload": {"k": "v"}, "expires_at": far})
        self.assertEqual(server.oidc_store_call("find", {"model": "Session", "id": "s1"}), {"result": {"k": "v"}})
        self.assertEqual(server.oidc_store_call("find", {"model": "Session", "id": "missing"}), {"result": None})
        with self.assertRaises(ValueError):
            server.oidc_store_call("drop_everything", {})

    def test_route_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            server.oidc_store_call("upsert", {"model": "Session", "id": "s1", "payload": "not-an-object", "expires_at": 1})
        with self.assertRaises(ValueError):
            server.oidc_store_call("find", {"model": "", "id": "s1"})
