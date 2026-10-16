import asyncio
import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from functions import web_jobs


class FakeDB:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row

    @contextmanager
    def atomic(self, _databases=None):
        with self.connection:
            yield self.connection


class WebJobTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.connection.execute(web_jobs.SCHEMA)

    def add(self, kind="shareholder_meeting", payload=None):
        return self.db.connection.execute(
            "INSERT INTO web_bot_jobs(kind,actor_id,payload,created_epoch,updated_epoch) VALUES (?,?,?,1,1)",
            (kind, "admin", json.dumps(payload or {"code": "AAA", "place": "회의실"}))).lastrowid

    def test_jobs_are_claimed_once_in_order(self):
        first, second = self.add(), self.add()
        self.assertEqual(web_jobs._claim(self.db)["job_id"], first)
        self.assertEqual(web_jobs._claim(self.db)["job_id"], second)
        self.assertIsNone(web_jobs._claim(self.db))
        web_jobs._finish(self.db, first, "done", "ok")
        row = self.db.connection.execute("SELECT status,result FROM web_bot_jobs WHERE job_id=?", (first,)).fetchone()
        self.assertEqual(tuple(row), ("done", "ok"))

    def test_shareholder_meeting_uses_the_shared_discord_flow(self):
        job = {"job_id": 1, "kind": "shareholder_meeting", "actor_id": "admin",
               "payload": json.dumps({"code": "AAA", "place": "회의실"})}
        with patch("functions.admin_prefix.convene_national_meeting", AsyncMock(return_value="sent")) as convene:
            self.assertEqual(asyncio.run(web_jobs.run_job("bot", job)), "sent")
        convene.assert_awaited_once_with("bot", "admin", "AAA", "회의실")
        with self.assertRaises(ValueError):
            asyncio.run(web_jobs.run_job("bot", {**job, "kind": "unknown"}))

    def test_hts_requests_reuse_the_slash_command_flows(self):
        relief = {"job_id": 2, "kind": "relief_request", "actor_id": "user-1", "payload": json.dumps({"type": "파산", "name": "닉"})}
        with patch("functions.slash_commands.submit_relief_request", AsyncMock(return_value="sent")) as submit:
            self.assertEqual(asyncio.run(web_jobs.run_job("bot", relief)), "sent")
        submit.assert_awaited_once_with("bot", "user-1", "닉", "파산")
        bot = SimpleNamespace(db=SimpleNamespace(users=SimpleNamespace(get_user=lambda _id: {"role": "user"})))
        meeting = {"job_id": 3, "kind": "user_shareholder_meeting", "actor_id": "user-1", "payload": json.dumps({"code": "AAA", "place": "홀"})}
        with patch("functions.slash_commands.convene_shareholder_meeting", AsyncMock(return_value="ok")) as convene:
            self.assertEqual(asyncio.run(web_jobs.run_job(bot, meeting)), "ok")
        self.assertEqual(convene.await_args.args[1:], ("user-1", "user", "AAA", "홀"))

    def test_loop_records_failures_and_keeps_running(self):
        job_id = self.add(kind="unknown")
        states = iter([False, False, True])
        bot = SimpleNamespace(db=self.db, is_closed=lambda: next(states))
        with patch.object(web_jobs, "POLL_SECONDS", 0):
            asyncio.run(web_jobs.web_job_loop(bot))
        row = self.db.connection.execute("SELECT status,result FROM web_bot_jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("지원하지 않는", row["result"])


if __name__ == "__main__":
    unittest.main()
