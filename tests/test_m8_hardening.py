"""M8 hardening: database failures reach clients only as a generic message with a request id, the bootstrap administrators come
from configuration, and the backup/access-review warnings are read-only checks."""
import ast
import logging
import os
import time
from datetime import date
from pathlib import Path

from web_api import controls_overview, server

ROOT = Path(__file__).resolve().parents[1]


def _handler_class():
    for value in vars(server).values():
        if isinstance(value, type) and hasattr(value, "send_db_failure"):
            return value
    raise AssertionError("no handler class with send_db_failure")


def test_database_error_text_stays_in_the_log_and_the_client_gets_a_request_id(caplog):
    handler = object.__new__(_handler_class())
    sent = []
    handler.send_json = lambda status, payload: sent.append((status, payload))
    handler.path = "/internal/admin/commands"
    secret = 'duplicate key value violates unique constraint "users_pkey" DETAIL: Key (user_id)=(top-secret-id)'
    with caplog.at_level(logging.ERROR, logger="inazina.web"):
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            handler.send_db_failure("POST", 400, "constraint_failed", "입력이 데이터 제약 조건에 맞지 않아 처리하지 못했습니다.")
    status, payload = sent[0]
    assert status == 400 and payload["error"] == "constraint_failed"
    assert "top-secret-id" not in repr(payload) and "users_pkey" not in repr(payload)
    request_id = payload["requestId"]
    assert request_id in payload["message"]
    assert request_id in caplog.text and "top-secret-id" in caplog.text      # the detail is kept, joined by the id


def test_no_database_error_handler_puts_the_exception_into_a_response():
    tree = ast.parse((ROOT / "web_api" / "server.py").read_text(encoding="utf-8"))
    leaks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name and "sqlite3" in ast.unparse(node.type or ast.Constant(0)):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "send_json":
                    if any(isinstance(n, ast.Name) and n.id == node.name for n in ast.walk(call)):
                        leaks.append(call.lineno)
    assert not leaks, leaks


def test_bootstrap_admins_come_from_configuration_with_the_old_default():
    assert server.bootstrap_admin_ids({}) == {"1076861147560542278"}
    assert server.bootstrap_admin_ids({"INAZINA_BOOTSTRAP_ADMIN_IDS": "  "}) == {"1076861147560542278"}
    assert server.bootstrap_admin_ids({"INAZINA_BOOTSTRAP_ADMIN_IDS": "11, 22 ,,33"}) == {"11", "22", "33"}


def test_backup_freshness_warns_when_stale_or_missing(tmp_path):
    assert controls_overview.backup_status(tmp_path)["status"] == "missing"
    dump = tmp_path / "inazina-pg-20261101-000000.dump"
    dump.write_bytes(b"PGDMP")
    now = time.time()
    os.utime(dump, (now - 3600, now - 3600))
    assert controls_overview.backup_status(tmp_path, now=now, max_age_hours=48)["status"] == "ok"
    os.utime(dump, (now - 100 * 3600, now - 100 * 3600))
    stale = controls_overview.backup_status(tmp_path, now=now, max_age_hours=48)
    assert stale["status"] == "stale" and stale["ageHours"] == 100.0
    (tmp_path / "inazina-pg-20261102-000000.dump").write_bytes(b"")     # an empty file is not a backup
    assert controls_overview.backup_status(tmp_path, now=now)["count"] == 1
    assert list(tmp_path.iterdir()) and len(list(tmp_path.iterdir())) == 2        # read-only: nothing created or removed


def test_access_review_is_overdue_after_35_days():
    reviews = [{"date": "2026-10-01"}, {"date": "2026-10-20"}]
    assert controls_overview.access_review_status(reviews, date(2026, 11, 20))["status"] == "ok"
    late = controls_overview.access_review_status(reviews, date(2026, 11, 30))
    assert late["status"] == "overdue" and late["ageDays"] == 41
    assert controls_overview.access_review_status([], date(2026, 11, 30))["status"] == "missing"


def test_audited_reads_and_samples_name_their_actions():
    text = (ROOT / "web_api" / "server.py").read_text(encoding="utf-8")
    for action in ("login_history.read", "admin_audit.read", "dr_monitor.sample"):
        assert f'"{action}"' in text, action
