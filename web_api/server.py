"""Private, read-only HTTP adapter for the existing SQLite database."""

import hmac
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shlex
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
DATABASE = Path(os.environ.get("INAZINA_DB_PATH", ROOT / "database" / "inazina.db")).resolve()
INTERNAL_TOKEN = os.environ.get("INAZINA_INTERNAL_TOKEN", "")
BROWSER_PASSWORD_SHA256 = os.environ.get(
    "INAZINA_BROWSER_PASSWORD_SHA256",
    "04b2ad6979a436129b5f30cd00a3804bd87fc8bef73b0bb38640bf49a8479c64",
)
_browser_login_attempts = {}
PORT = int(os.environ.get("INAZINA_PYTHON_PORT", "4313"))
MAX_HTTP_THREADS = max(1, min(int(os.environ.get("INAZINA_MAX_HTTP_THREADS", "16")), 64))
BOOTSTRAP_ADMIN_IDS = {"1076861147560542278"}
sys.path.insert(0, str(ROOT))
from database.unified import sql as unified_sql, UnifiedConnection
from functions.exact_money import native_value
from decimal import Decimal
from database.user_db import TREASURY_USER_ID, citizen_filter
from functions.dr_pricing import (
    DEVIATION_ALERT_THRESHOLD_PERCENT as DR_REFERENCE_DEVIATION_ALERT_PERCENT,
    reference_metrics as dr_reference_metrics,
)
from functions.performance import log_slow
from web_api import admin_logins, board, documents
_domain = None
_domain_lock = threading.Lock()


class SingleFlightTTL:
    """Coalesce identical concurrent reads and briefly reuse their result."""

    def __init__(self, ttl_seconds=0.25):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._condition = threading.Condition()
        self._values = {}
        self._inflight = set()

    def get(self, key, loader):
        while True:
            with self._condition:
                cached = self._values.get(key)
                now = time.monotonic()
                if cached is not None and cached[0] > now:
                    return cached[1]
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._condition.wait()
        try:
            value = loader()
        except BaseException:
            with self._condition:
                self._inflight.discard(key)
                self._condition.notify_all()
            raise
        with self._condition:
            self._values[key] = (time.monotonic() + self.ttl_seconds, value)
            self._inflight.discard(key)
            self._condition.notify_all()
        return value

    def invalidate(self, *keys):
        with self._condition:
            if not keys:
                self._values.clear()
            for key in keys:
                self._values.pop(key, None)


_read_cache = SingleFlightTTL(float(os.environ.get("INAZINA_READ_CACHE_SECONDS", "0.25")))


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server with backpressure instead of unbounded threads."""

    def __init__(self, server_address, request_handler_class, max_threads=MAX_HTTP_THREADS):
        self._request_slots = threading.BoundedSemaphore(max_threads)
        super().__init__(server_address, request_handler_class)

    def process_request(self, request, client_address):
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


@contextmanager
def connect_readonly():
    uri = DATABASE.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2, factory=UnifiedConnection)
    connection.legacy_aliases=False
    try:
        connection.legacy_aliases = False
        connection.row_factory = sqlite3.Row
        connection.create_function("base_currency_code", 0, lambda: _configured_base_currency(connection))
        from database.exact_cost_basis import register
        register(connection)
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


class LegacyNames:
    """A connection view for the shared managers: their SQL still writes product./assets./users.
    schema prefixes, which this drops as their own UnifiedConnection does. The server's own queries
    keep plain connections, where ``users.user_id`` is a real table-qualified column."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement, parameters=()):
        return self._connection.execute(unified_sql(statement), parameters)

    def executemany(self, statement, parameters):
        return self._connection.executemany(unified_sql(statement), parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@contextmanager
def connect_db(timeout=2):
    connection = sqlite3.connect(DATABASE, timeout=timeout, factory=UnifiedConnection)
    connection.legacy_aliases=False
    write_lock = None
    write_lock_held = False
    write_actions = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
    }

    def serialize_writes(action, arg1, arg2, database_name, trigger_name):
        nonlocal write_lock, write_lock_held
        if action in write_actions and not write_lock_held:
            from database.db_manager import DBManager
            write_lock = DBManager._lock_for_path(DATABASE)
            wait_started = time.perf_counter()
            write_lock.acquire()
            write_lock_held = True
            wait_ms = (time.perf_counter() - wait_started) * 1000
            if wait_ms >= 100:
                logging.getLogger("inazina.performance").warning(
                    "sqlite_process_lock_wait %.1f ms source=web_api", wait_ms
                )
        return sqlite3.SQLITE_OK

    try:
        connection.legacy_aliases = False
        connection.row_factory = sqlite3.Row
        connection.create_function("base_currency_code", 0, lambda: _configured_base_currency(connection))
        from database.exact_cost_basis import register
        register(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
        connection.set_authorizer(serialize_writes)
        with connection:
            yield connection
    finally:
        try:
            connection.close()
        finally:
            if write_lock_held:
                write_lock.release()


def _configured_base_currency(connection):
    try:
        row = connection.execute(
            "SELECT base_currency_code FROM currency_system_settings WHERE singleton_id=1"
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]).upper() if row and row[0] else None


# An administrator signed in as a country or company account ($login on the web): the API
# passes the administrator's ID with each request of that session. Set per request thread.
_DELEGATION = threading.local()
DELEGABLE_ROLES = {"nation", "company"}


def delegated_by():
    return getattr(_DELEGATION, "by", "") or ""


def identity(user_id):
    by = delegated_by()
    if by and by != user_id:
        admin = identity_for(by)
        target = identity_for(user_id, require_credential=False)
        if not admin or admin["role"] != "admin" or not target or target["role"] not in DELEGABLE_ROLES:
            return None
        # The session lives on the administrator's credential: a password change ends it.
        return {**target, "authVersion": admin["authVersion"], "delegatedBy": admin["userId"], "delegatedByName": admin["nickname"]}
    return identity_for(user_id)


def identity_for(user_id, require_credential=True):
    with connect_readonly() as connection:
        row = connection.execute(
            "SELECT user_id,nickname,nationality,cash,credit_score,status,role "
            "FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        auth_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_auth_credentials'"
        ).fetchone()
        credential = connection.execute(
            "SELECT auth_version FROM web_auth_credentials WHERE user_id=?", (user_id,),
        ).fetchone() if auth_table else None
        currency = connection.execute(
            "SELECT currency_code FROM country_currencies WHERE nationality=?", (row["nationality"],),
        ).fetchone() if row else None
        base_currency = _configured_base_currency(connection)
    if (not row or row["status"] not in {"active", "nation"} or
            (require_credential and auth_table and credential is None)):
        return None
    role = "admin" if user_id in BOOTSTRAP_ADMIN_IDS else str(row["role"] or "user").strip().lower()
    if role not in {"admin", "user", "nation", "company"}:
        role = "user"
    return {
        "userId": str(row["user_id"]),
        "nickname": str(row["nickname"] or row["user_id"]),
        "nationality": str(row["nationality"] or ""),
        "currency": str(currency[0]) if currency else str(base_currency or ""),
        "cash": int(row["cash"] or 0),
        "creditScore": int(row["credit_score"] or 0),
        "role": role,
        "authVersion": int(credential[0]) if credential else 0,
    }


def trading_user_id(user_id):
    with connect_readonly() as connection:
        country = connection.execute(
            "SELECT u.nationality FROM users u JOIN country_currencies c ON c.nationality=u.user_id "
            "WHERE u.user_id=? AND u.nationality=u.user_id AND u.status='active'",
            (user_id,),
        ).fetchone()
        # A country account trades with its own country's treasury.
        treasury = _country_treasury(connection, country[0]) if country else None
    return treasury or user_id


def _country_treasury(connection, nationality):
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='country_treasuries'"
    ).fetchone()
    row = connection.execute(
        "SELECT user_id FROM country_treasuries WHERE nationality=?", (nationality,),
    ).fetchone() if has_table else None
    if not row:
        # Before the per-country treasuries existed the shared treasury served its own nationality.
        row = connection.execute(
            "SELECT user_id FROM users WHERE user_id=? AND nationality=?", (TREASURY_USER_ID, nationality),
        ).fetchone()
    return str(row[0]) if row else None


def auth_countries():
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT n.nationality AS country_id,c.name AS currency_name,c.code AS currency_code "
            "FROM country_currencies n JOIN currencies c ON c.code=n.currency_code "
            "WHERE c.status='active' ORDER BY n.nationality"
        ).fetchall()
    return [{"countryId": row["country_id"], "currencyName": row["currency_name"],
             "currencyCode": row["currency_code"]} for row in rows]


def password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                          n=2**14, r=8, p=1, dklen=64).hex()


def validate_password(password):
    if (not isinstance(password, str) or not 12 <= len(password) <= 128 or
            not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password)):
        raise ValueError("password_policy")


# ---------------------------------------------------------------------------
# Official site (apps/site): Contact and Report forms, read in the dashboard.
# ---------------------------------------------------------------------------
FEEDBACK_KINDS = {"contact": "문의", "report": "신고"}
FEEDBACK_STATUSES = {"new", "read", "done"}
_board_ready = False
_board_ip_purged = 0.0
# Board attachments: the API server writes the files here, this service tracks them.
BOARD_FILES_DIR = Path(os.environ.get("INAZINA_BOARD_FILES_DIR", ROOT / "database" / "board_files")).resolve()


@contextmanager
def board_db():
    """A write connection with the board tables in place.

    At most once an hour it also erases client IPs past their retention period
    (board.IP_RETENTION_SECONDS, as the privacy policy promises).
    """
    global _board_ready, _board_ip_purged
    with connect_db(timeout=5) as connection:
        if not _board_ready:
            board.ensure_tables(connection)
            _board_ready = True
        if time.time() - _board_ip_purged >= 3600:
            board.purge_expired_ips(connection)
            board.cleanup_files(connection, BOARD_FILES_DIR)
            _board_ip_purged = time.time()
        yield connection


def board_get(path, query, user):
    """Public board reads. ``user`` (signed-in identity or None) marks their own
    posts and decides which secret boards they may see."""
    first = lambda key, default="": query.get(key, [default])[0]
    viewer, admin = (user["userId"], user["role"] == "admin") if user else (None, False)
    with board_db() as connection:
        if path == "/internal/board/boards":
            return {"boards": board.boards(connection, viewer, admin)}
        if path.startswith("/internal/board/files/"):
            return board.file_meta(connection, path.removeprefix("/internal/board/files/"), viewer, admin)
        if path == "/internal/board/posts":
            return board.list_posts(connection, first("board") or None, first("search"), first("asset") or None,
                                    first("page", "1"), viewer, first("category") or None, admin)
        return {"post": board.read_post(connection, path.removeprefix("/internal/board/posts/"), viewer,
                                        count_view=first("view") == "1", admin=admin)}


def _apply_due_documents():
    """Put announced document revisions into force once their notice period is over."""
    applied = documents.apply_due()
    if applied:
        with connect_db(timeout=5) as connection:
            for change in applied:
                _audit_db(connection, "system", "document.apply", change)
    return applied


def board_cleanup():
    """Delete files of attachments that are gone (after the deleting transaction committed)."""
    with board_db() as connection:
        board.cleanup_files(connection, BOARD_FILES_DIR)


def publish_due_news(post_id=None):
    """Publish news whose time has come: register its market event, then show it.

    The board connection is closed while the event is created (that opens its
    own write transaction on the same database). With ``post_id`` (news just
    written to go out now) a failure deletes the post and raises the reason;
    scheduled news that fails stays hidden as 'failed' for administrators.
    """
    with board_db() as connection:
        due = [item for item in board.due_news(connection) if post_id is None or item["post_id"] == post_id]
    published = 0
    for item in due:
        created = {}
        try:
            success, message = domain().admin_manager.create_market_event(
                item["author_id"], item["event_type"], item["severity"], item["asset_code"], item["duration_seconds"],
                target_price=item["target_price"], created=created, stacking=True)  # news adds up
        except (ValueError, TypeError) as exc:
            success, message = False, str(exc)
        with board_db() as connection:
            if success and created.get("event_id"):
                board.mark_news(connection, item["post_id"], created["event_id"])
                _audit_db(connection, item["author_id"], "board.news.publish",
                          {"post_id": item["post_id"], "event_id": created["event_id"]})
                published += 1
            elif post_id is not None:
                connection.execute("DELETE FROM board_posts WHERE post_id=?", (item["post_id"],))
            else:
                board.mark_news(connection, item["post_id"], error=plain_message(message))
        if not success and post_id is not None:
            raise ValueError("뉴스의 시장 영향을 등록하지 못해 글을 올리지 않았습니다: " + plain_message(message))
    return published


def plain_message(message):
    return re.sub(r"^[❌✅⚠️\s]+", "", str(message or "")).strip()


def board_post(path, user, payload):
    ip = payload.get("clientIp")
    parts = path.removeprefix("/internal/board/").split("/")
    if parts == ["posts"] and payload.get("news"):
        result = _board_post(parts, user, payload, ip)
        publish = payload["news"].get("publishEpoch") if isinstance(payload["news"], dict) else None
        if not publish or publish <= time.time():
            publish_due_news(result["postId"])
        return result
    if parts[-1] in {"delete", "edit"}:
        try:
            return _board_post(parts, user, payload, ip)
        finally:
            board_cleanup()
    return _board_post(parts, user, payload, ip)


def _board_post(parts, user, payload, ip):
    with board_db() as connection:
        if parts == ["files"]:
            return {"attachmentId": board.register_upload(connection, user, payload)}
        if len(parts) == 3 and parts[0] == "polls" and parts[2] == "vote":
            return {"poll": board.vote(connection, user, parts[1], payload.get("optionIds"))}
        if len(parts) == 3 and parts[0] == "polls" and parts[2] in {"close", "reopen"}:
            return {"changed": board.poll_action(connection, parts[1], parts[2], user)}
        if parts == ["posts"]:
            return {"postId": board.create_post(connection, user, payload, ip)}
        if len(parts) == 3 and parts[0] == "posts":
            action = parts[2]
            if action == "edit":
                board.edit_post(connection, user, parts[1], payload); return {"success": True}
            if action == "delete":
                board.delete_post(connection, user, parts[1]); return {"success": True}
            if action == "comments":
                return {"commentId": board.add_comment(connection, user, parts[1], payload, ip)}
            if action == "react":
                return board.react(connection, user, "post", parts[1], payload.get("value"))
        if len(parts) == 3 and parts[0] == "comments" and parts[2] == "delete":
            board.delete_comment(connection, user, parts[1]); return {"success": True}
        if len(parts) == 3 and parts[0] == "comments" and parts[2] == "react":
            return board.react(connection, user, "comment", parts[1], payload.get("value"))
        if parts == ["reports"]:
            board.report(connection, user, payload, ip); return {"success": True}
    raise LookupError("not_found")


BOARD_ACTION_NAMES = {"hide": "숨김", "restore": "복원", "pin": "상단 고정", "unpin": "고정 해제", "notice": "공지 설정",
                      "unnotice": "공지 해제", "move": "게시판 이동", "category": "카테고리 지정", "purge": "영구 삭제",
                      "resolve": "처리 완료", "create": "생성", "edit": "수정", "delete": "삭제",
                      "block": "검색어 숨김", "unblock": "검색어 숨김 해제", "reset": "검색 집계 초기화",
                      "pin": "검색어 순위 고정", "unpin": "검색어 순위 고정 해제", "boost": "검색 수 추가"}


def admin_board_action(actor, kind, target, action, payload=None):
    payload = {key: value for key, value in (payload or {}).items() if key != "action"}
    with board_db() as connection:
        changed = board.admin_action(connection, kind, target, action, payload)
        _audit_db(connection, actor, f"board.{kind}.{action}", {"id": target, "changed": changed, **payload})
    if action in {"purge", "delete"}:
        board_cleanup()
    return f"✅ {BOARD_ACTION_NAMES.get(action, action)} 처리했습니다." if changed else "❌ 바뀐 것이 없습니다."


FEEDBACK_RETENTION_SECONDS = 365 * 86400  # 개인정보 처리방침: 접수 후 1년이 지나면 파기


def ensure_feedback_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS site_feedback (
        feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK(kind IN ('contact','report')),
        title TEXT NOT NULL, body TEXT NOT NULL,
        discord_id TEXT NOT NULL, email TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','read','done')),
        created_epoch INTEGER NOT NULL, handled_by TEXT, handled_epoch INTEGER)""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_site_feedback_status ON site_feedback(status, feedback_id)")
    # Personal data in submissions is destroyed once its retention period ends.
    connection.execute("DELETE FROM site_feedback WHERE created_epoch < ?",
                       (int(time.time()) - FEEDBACK_RETENTION_SECONDS,))


def submit_feedback(payload):
    """A public Contact/Report submission: every field is required, as on the Notion forms."""
    kind = str(payload.get("kind", "")).strip()
    if kind not in FEEDBACK_KINDS:
        raise ValueError("invalid_kind")
    if payload.get("consent") not in (True, "on", "true"):
        raise ValueError("consent_required")  # 개인정보 수집·이용 동의
    limits = {"title": 120, "body": 4000, "discordId": 64, "email": 254}
    values = {}
    for key, limit in limits.items():
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f"invalid_{key}")
        values[key] = value.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]):
        raise ValueError("invalid_email")
    with connect_db(timeout=5) as connection:
        ensure_feedback_table(connection)
        feedback_id = connection.execute(
            "INSERT INTO site_feedback(kind,title,body,discord_id,email,created_epoch) VALUES (?,?,?,?,?,?)",
            (kind, values["title"], values["body"], values["discordId"], values["email"], int(time.time())),
        ).lastrowid
    return {"id": feedback_id, "kind": kind}


def record_admin_login(login_id, success, meta, reason=None):
    """Keep a login record for administrator accounts; never lets a logging error block a login."""
    try:
        with connect_db(timeout=5) as connection:
            admin_logins.record(connection, login_id, success, meta,
                                bootstrap_ids=BOOTSTRAP_ADMIN_IDS, reason=reason)
    except (sqlite3.Error, ValueError, TypeError):
        logging.getLogger("inazina.auth").exception("admin login record failed")


def admin_feedback(status=None):
    with connect_db(timeout=5) as connection:
        ensure_feedback_table(connection)
        rows = connection.execute(
            "SELECT * FROM site_feedback WHERE (? IS NULL OR status=?) ORDER BY feedback_id DESC LIMIT 200",
            (status, status),
        ).fetchall()
        unread = connection.execute("SELECT COUNT(*) FROM site_feedback WHERE status='new'").fetchone()[0]
    return {"items": [dict(row) for row in rows], "unread": int(unread)}


def admin_naturalization():
    """Naturalization requests for the 귀화심사 tab, each with what approval would liquidate."""
    from managers.naturalization_manager import NaturalizationManager
    manager = NaturalizationManager(domain().bot)
    requests = manager.requests()
    for item in requests:
        if item["status"] == "pending":
            item["preview"] = manager.preview(item["user_id"])
    return {"requests": requests, "pending": sum(item["status"] == "pending" for item in requests)}


def admin_naturalization_decide(actor, request_id, payload):
    from managers.naturalization_manager import NaturalizationManager
    action = str(payload.get("action") or "")
    if action not in {"approve", "reject"}:
        raise ValueError("승인 또는 거절을 선택해주세요.")
    return NaturalizationManager(domain().bot).decide(actor, request_id, action == "approve", payload.get("note") or "")


def admin_feedback_delete(actor, payload):
    """Delete read submissions: the given ids, or every read one with all=true. New ones stay."""
    with connect_db(timeout=5) as connection:
        ensure_feedback_table(connection)
        if payload.get("all") is True:
            removed = connection.execute("DELETE FROM site_feedback WHERE status<>'new'").rowcount
        else:
            ids = payload.get("ids")
            if (not isinstance(ids, list) or not ids or len(ids) > 500
                    or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)):
                raise ValueError("삭제할 문의·신고를 골라 주세요.")
            marks = ",".join("?" for _ in ids)
            removed = connection.execute(
                f"DELETE FROM site_feedback WHERE status<>'new' AND feedback_id IN ({marks})", ids).rowcount
        _audit_db(connection, actor, "site.feedback.delete", {"all": payload.get("all") is True,
                                                               "ids": payload.get("ids"), "removed": removed})
    return f"✅ 읽은 문의·신고 {removed}건을 삭제했습니다." + ("" if removed else " (새 알림은 먼저 열어 확인해야 삭제할 수 있습니다.)")


def admin_feedback_action(actor, feedback_id, payload):
    status = payload.get("status")
    if status not in FEEDBACK_STATUSES:
        raise ValueError("상태는 new·read·done 중 하나입니다.")
    with connect_db(timeout=5) as connection:
        ensure_feedback_table(connection)
        changed = connection.execute(
            "UPDATE site_feedback SET status=?,handled_by=?,handled_epoch=? WHERE feedback_id=?",
            (status, actor, int(time.time()), int(feedback_id)),
        ).rowcount
        if not changed:
            raise ValueError("문의·신고를 찾을 수 없습니다.")
        _audit_db(connection, actor, "site.feedback", {"id": int(feedback_id), "status": status})
    return "✅ 처리 상태를 바꿨습니다."


def ensure_auth_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS web_auth_credentials (
        user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
        salt TEXT NOT NULL, password_hash TEXT NOT NULL,
        created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL,
        auth_version INTEGER NOT NULL DEFAULT 0)""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(web_auth_credentials)")}
    if "auth_version" not in columns:
        connection.execute("ALTER TABLE web_auth_credentials ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0")
    if "hts_id" not in columns:
        connection.execute("ALTER TABLE web_auth_credentials ADD COLUMN hts_id TEXT")
    foreign_keys = connection.execute("PRAGMA foreign_key_list(web_auth_credentials)").fetchall()
    if not any(row[2] == "users" and row[3] == "user_id" and row[4] == "user_id" for row in foreign_keys):
        connection.execute("SAVEPOINT web_auth_user_fk")
        try:
            connection.execute("DROP INDEX IF EXISTS idx_web_auth_credentials_hts_id")
            connection.execute("ALTER TABLE web_auth_credentials RENAME TO web_auth_credentials_legacy")
            connection.execute("""CREATE TABLE web_auth_credentials (
                user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL,
                auth_version INTEGER NOT NULL DEFAULT 0, hts_id TEXT)""")
            connection.execute("""INSERT INTO web_auth_credentials
                (user_id,salt,password_hash,created_epoch,updated_epoch,auth_version,hts_id)
                SELECT old.user_id,old.salt,old.password_hash,old.created_epoch,old.updated_epoch,
                       old.auth_version,old.hts_id
                FROM web_auth_credentials_legacy old
                JOIN users u ON u.user_id=old.user_id""")
            connection.execute("DROP TABLE web_auth_credentials_legacy")
            connection.execute("RELEASE SAVEPOINT web_auth_user_fk")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT web_auth_user_fk")
            connection.execute("RELEASE SAVEPOINT web_auth_user_fk")
            raise
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_auth_credentials_hts_id "
        "ON web_auth_credentials(hts_id) WHERE hts_id IS NOT NULL"
    )


def normalize_hts_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", value.strip()):
        raise ValueError("hts_id_policy")
    return value.strip().lower()


def get_hts_login_id(user_id):
    with connect_db(timeout=2) as connection:
        ensure_auth_table(connection)
        row = connection.execute(
            "SELECT hts_id FROM web_auth_credentials WHERE user_id=?", (user_id,),
        ).fetchone()
    return row[0] if row else None


def set_hts_login_id(user_id, hts_id, password):
    normalized_id = normalize_hts_id(hts_id)
    if not authenticate_password(user_id, password):
        raise ValueError("invalid_credentials")
    try:
        with connect_db(timeout=2) as connection:
            ensure_auth_table(connection)
            user = connection.execute(
                "SELECT 1 FROM users WHERE user_id=? AND status IN ('active','nation')", (user_id,),
            ).fetchone()
            if not user:
                raise ValueError("active_user_required")
            reserved = connection.execute(
                "SELECT 1 FROM users WHERE lower(user_id)=lower(?) AND user_id<>? "
                "UNION ALL SELECT 1 FROM country_currencies WHERE lower(nationality)=lower(?) LIMIT 1",
                (normalized_id, user_id, normalized_id),
            ).fetchone()
            if reserved:
                raise ValueError("hts_id_taken")
            connection.execute(
                "UPDATE web_auth_credentials SET hts_id=?,updated_epoch=? WHERE user_id=?",
                (normalized_id, int(time.time()), user_id),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("hts_id_taken") from exc
    return normalized_id


def create_country_account(actor_id, country_id, password):
    country_id = str(country_id or "").strip()
    if not country_id or len(country_id) > 80:
        raise ValueError("country_not_found")
    validate_password(password)
    now = int(time.time())
    salt = secrets.token_bytes(16).hex()
    digest = password_hash(password, salt)
    from database.db_manager import DBManager
    from managers.currency_manager import CurrencyManager
    with DBManager.admin_scope(actor_id):
        with DBManager.atomic(("users", "currency", "system")) as connection:
            ensure_auth_table(connection)
            nationality = CurrencyManager.nationality_of(connection, country_id)
            country = connection.execute(
                "SELECT 1 FROM country_currencies n JOIN currencies c ON c.code=n.currency_code "
                "WHERE n.nationality=? AND c.status='active'", (nationality,),
            ).fetchone()
            if not country:
                raise ValueError("country_not_found")
            # The country's account is its treasury; its numeric id is the login ID.
            user_id = CurrencyManager.treasury_id(connection, nationality)
            if connection.execute("SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,)).fetchone():
                raise ValueError("country_account_exists")
            connection.execute(
                "INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch) "
                "VALUES(?,?,?,?,?)", (user_id, salt, digest, now, now),
            )
            cash = connection.execute("SELECT cash FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    return {"countryId": user_id, "userId": user_id, "cash": int(cash or 0), "status": "active"}


def save_password(user_id, password, *, create=False, nickname="", country_id=""):
    if not isinstance(user_id, str) or not user_id.isdecimal():
        raise ValueError("discord_identity_invalid")
    validate_password(password)
    db = domain().db
    with db.atomic(("users", "currency")) as connection:
        ensure_auth_table(connection)
        if create:
            country = connection.execute(
                "SELECT 1 FROM country_currencies n JOIN currencies c ON c.code=n.currency_code "
                "WHERE n.nationality=? AND c.status='active'", (country_id,),
            ).fetchone()
            if not country:
                raise ValueError("country_not_found")
            user = connection.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not user:
                role = "admin" if user_id in BOOTSTRAP_ADMIN_IDS else "user"
                connection.execute(
                    "INSERT INTO users(user_id,nickname,nationality,cash,role) "
                    "VALUES (?,?,?,1000000,?)", (user_id, nickname or user_id, country_id, role),
                )
            elif user["status"] not in {"active", "nation"}:
                raise ValueError("active_user_required")
            elif connection.execute(
                "SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,),
            ).fetchone():
                raise ValueError("account_exists")
        elif not connection.execute(
            "SELECT 1 FROM users WHERE user_id=? AND status IN ('active','nation')", (user_id,),
        ).fetchone():
            raise ValueError("active_user_required")

        salt = secrets.token_bytes(16).hex()
        digest = password_hash(password, salt)
        now = int(time.time())
        connection.execute(
            "INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "salt=excluded.salt,password_hash=excluded.password_hash,updated_epoch=excluded.updated_epoch, "
            "auth_version=web_auth_credentials.auth_version+1",
            (user_id, salt, digest, now, now),
        )
    return identity(user_id)


def authenticate_password(user_id, password):
    if not isinstance(user_id, str) or not isinstance(password, str):
        return None
    with connect_db(timeout=2) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        ensure_auth_table(connection)
        connection.legacy_aliases = False
        connection.row_factory = sqlite3.Row
        credential = connection.execute(
            "SELECT salt,password_hash FROM web_auth_credentials WHERE user_id=?", (user_id,),
        ).fetchone()
    if not credential:
        return None
    digest = password_hash(password, credential["salt"])
    if not hmac.compare_digest(digest, credential["password_hash"]):
        return None
    return identity(user_id)


def authenticate_discord_password(user_id, password):
    if not isinstance(user_id, str) or not user_id.isdecimal():
        return None
    return authenticate_password(user_id, password)


def authenticate_country_password(country_id, password):
    if not isinstance(country_id, str) or not isinstance(password, str):
        return None
    with connect_readonly() as connection:
        account = connection.execute(
            "SELECT 1 FROM users u JOIN country_treasuries t ON t.user_id=u.user_id "
            "WHERE u.user_id=? AND u.status='active'", (country_id,),
        ).fetchone()
    if not account:
        return None
    return authenticate_password(country_id, password)


def create_company_login(actor_id, code, password):
    """Give a company's capital account a web/HTS login: ID = the company code."""
    validate_password(password)
    from functions import company_actions
    bot = domain().bot
    bot.admin_control.require(actor_id)
    account = company_actions.company_account(bot, str(code or "").strip())
    message = admin_set_web_password(actor_id, account, password)
    return {"companyId": str(code).strip(), "userId": account, "message": message}


def authenticate_login_identifier(identifier, password):
    if identifier.startswith("company:") or (identifier and not identifier.isdecimal()):
        account = identifier if identifier.startswith("company:") else "company:" + identifier
        with connect_db(timeout=2) as connection:
            ensure_auth_table(connection)
            company_account = connection.execute(
                "SELECT 1 FROM users u JOIN web_auth_credentials w ON w.user_id=u.user_id "
                "WHERE u.user_id=? AND u.role='company' AND u.status='active'", (account,),
            ).fetchone()
        if company_account:
            return authenticate_password(account, password), True
    with connect_db(timeout=2) as connection:
        ensure_auth_table(connection)
        connection.legacy_aliases = False
        connection.row_factory = sqlite3.Row
        country_account = connection.execute(
            "SELECT 1 FROM users u JOIN country_treasuries t ON t.user_id=u.user_id "
            "JOIN web_auth_credentials w ON w.user_id=u.user_id "
            "WHERE u.user_id=? AND u.status='active'", (identifier,),
        ).fetchone() if _table_exists(connection, "country_treasuries") else None
        hts_account = connection.execute(
            "SELECT user_id FROM web_auth_credentials WHERE hts_id=?", (identifier.lower(),),
        ).fetchone()
        discord_account = connection.execute(
            "SELECT 1 FROM web_auth_credentials WHERE user_id=?", (identifier,),
        ).fetchone() if identifier.isdecimal() else None
    if country_account:
        return authenticate_country_password(identifier, password), True
    if hts_account:
        return authenticate_password(hts_account["user_id"], password), True
    if discord_account:
        return authenticate_discord_password(identifier, password), True
    return None, False


def instruments(include_inactive=False):
    # Ensure additive schema migrations have run before the read-only adapter
    # selects newly introduced fund metadata.
    domain()
    status_clause = "" if include_inactive else " WHERE status='active'"
    with connect_readonly() as connection:
        rows = []
        for query in (
            f"SELECT code,name,type,price,exchange_id,quote_currency,status,'market_assets' AS source,NULL AS fund_category FROM market_assets{status_clause}",
            f"SELECT company_code AS code,name,'stock' AS type,share_price AS price,exchange_id,quote_currency,status,'company' AS source,NULL AS fund_category FROM company{status_clause}",
            f"SELECT product_code AS code,name,product_type AS type,unit_price AS price,exchange_id,quote_currency,status,'investment' AS source,fund_category FROM investment{status_clause}",
            f"SELECT product_code AS code,name,'국채' AS type,market_price AS price,exchange_id,quote_currency,status,'treasury_bond' AS source,NULL AS fund_category,face_value,annual_rate,maturity_days,coupon_frequency_days,day_count_convention,credit_spread,liquidity_spread,created_at FROM treasury_bond_products{status_clause}",
        ):
            rows.extend(dict(row) for row in connection.execute(query).fetchall())
        components = {}
        dr_programs = {
            row["dr_code"]: {
                "underlyingCode": row["underlying_code"],
                "receiptType": row["receipt_type"],
                "depositaryBank": row["depositary_bank"],
                "custodian": row["custodian"],
                "homeExchangeId": row["home_exchange_id"],
                "listingExchangeId": row["listing_exchange_id"],
                "underlyingPerReceipt": float(row["underlying_per_receipt"]),
                "receiptSupply": int(row["receipt_supply"]),
                "depositedUnderlyingQuantity": float(row["deposited_underlying_quantity"]),
                "programStatus": row["program_status"],
            }
            for row in connection.execute(
                "SELECT * FROM depositary_receipts ORDER BY dr_code"
            ).fetchall()
        }
        dr_custody = {
            row["dr_code"]: {
                "underlying": int(row["underlying_quantity"] or 0),
                "receipts": int(row["receipt_quantity"] or 0),
            }
            for row in connection.execute(
                "SELECT dr_code,SUM(CASE WHEN movement_type='issue' THEN underlying_quantity "
                "ELSE -underlying_quantity END) AS underlying_quantity,"
                "SUM(CASE WHEN movement_type='issue' THEN dr_quantity ELSE -dr_quantity END) AS receipt_quantity "
                "FROM depositary_receipt_movements GROUP BY dr_code"
            ).fetchall()
        }
        for code, program in dr_programs.items():
            program["custodiedUnderlyingQuantity"] = max(0, dr_custody.get(code, {}).get("underlying", 0))
            program["outstandingReceiptQuantity"] = max(0, dr_custody.get(code, {}).get("receipts", 0))
        for row in connection.execute(
            "SELECT product_code,asset_code,weight,asset_class FROM fund_components "
            "ORDER BY product_code,asset_code"
        ).fetchall():
            components.setdefault(row["product_code"], []).append({
                "assetCode": row["asset_code"], "weight": float(row["weight"]),
                "assetClass": row["asset_class"],
            })
        from managers.trading_warning_manager import TradingWarnings, LEVELS
        has_warnings = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_warnings'").fetchone()
        warnings = TradingWarnings.active(connection) if has_warnings else {}
        # 24-hour activity for the 나루 "실시간 추천" list: price change and traded value.
        since = int(time.time()) - 86400
        opening = {code: price for code, price, _ in connection.execute(
            "SELECT asset_code,price,MIN(recorded_epoch) FROM market_price_history "
            "WHERE recorded_epoch>=? GROUP BY asset_code", (since,))} if _table_exists(connection, "market_price_history") else {}
        traded = {code: (value, count, volume) for code, value, count, volume in connection.execute(
            "SELECT asset_code,SUM(quantity*price),COUNT(*),SUM(quantity) FROM market_trade_history "
            "WHERE recorded_epoch>=? GROUP BY asset_code", (since,))} if _table_exists(connection, "market_trade_history") else {}
        for row in rows:
            if row["source"] == "treasury_bond":
                try:
                    from managers.rate_manager import RateManager
                    issuer = connection.execute(
                        "SELECT nationality FROM market_exchanges WHERE exchange_id=?",
                        (row["exchange_id"],),
                    ).fetchone()
                    try:
                        created = datetime.strptime(
                            str(row["created_at"]), "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc).timestamp()
                        remaining_days = max(0, math.ceil(
                            (created+int(row["maturity_days"] or 1)*86400-time.time())/86400
                        ))
                    except (TypeError, ValueError, OverflowError):
                        remaining_days = int(row["maturity_days"] or 1)
                    valuation = RateManager.bond_quote(
                        connection, issuer["nationality"] if issuer and issuer["nationality"] else "낙원",
                        native_value(row["face_value"] or 1), float(row["annual_rate"] or 0),
                        remaining_days,
                        coupon_frequency_days=int(row["coupon_frequency_days"] or 0),
                        spread=float(row["credit_spread"] or 0)+float(row["liquidity_spread"] or 0),
                        original_maturity_days=int(row["maturity_days"] or 1),
                        day_count_convention=row["day_count_convention"] or "ACT/365",
                    )
                    row["bondRisk"] = {
                        key: float(valuation[key]) for key in (
                            "macaulay_duration", "modified_duration", "convexity", "dv01"
                        )
                    }
                    row["bondRisk"]["faceValue"] = native_value(row["face_value"] or 1)
                except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
                    row["bondRisk"] = None
            if row["type"] == "depositary_receipt":
                row["drProgram"] = dr_programs.get(row["code"])
                program = row["drProgram"]
                if program:
                    try:
                        underlying = connection.execute(
                            "SELECT share_price AS price,quote_currency FROM company WHERE company_code=? "
                            "UNION ALL SELECT price,quote_currency FROM market_assets WHERE code=? LIMIT 1",
                            (program["underlyingCode"], program["underlyingCode"]),
                        ).fetchone()
                        if underlying and float(underlying["price"] or 0) > 0:
                            from managers.currency_manager import CurrencyManager
                            fx = float(CurrencyManager.quote(
                                connection, str(underlying["quote_currency"] or connection.execute("SELECT base_currency_code()").fetchone()[0]),
                                str(row.get("quote_currency") or connection.execute("SELECT base_currency_code()").fetchone()[0]),
                            ))
                            metrics = dr_reference_metrics(
                                row["price"], underlying["price"],
                                program["underlyingPerReceipt"], fx,
                            )
                            if metrics:
                                program["underlyingPrice"] = native_value(underlying["price"])
                                program["underlyingCurrency"] = str(underlying["quote_currency"] or connection.execute("SELECT base_currency_code()").fetchone()[0])
                                program.update(metrics)
                    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
                        pass
            start = opening.get(row["code"])
            row["change24h"] = (float(row["price"]) / float(start) - 1) * 100 if start and row["price"] else None
            row["tradeValue24h"], row["trades24h"], row["volume24h"] = traded.get(row["code"], (0, 0, 0))
            if row["type"] == "mutual_fund":
                row["components"] = components.get(row["code"], [])
            warning = warnings.get(row["code"])
            if warning:
                row["warning"] = {"level": warning["level"], "label": LEVELS[warning["level"]],
                                  "reason": warning["reason"], "expiresEpoch": warning["expires_epoch"]}
        # Exchange-rate assets name the currency they price, so search can show the rate.
        has_currencies = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currencies'").fetchone()
        currency_by_asset = ({asset: code for code, asset in connection.execute("SELECT code,asset_code FROM currencies")}
                             if has_currencies else {})
        for row in rows:
            if row["type"] == "forex" and row["code"] in currency_by_asset:
                row["currency_code"] = currency_by_asset[row["code"]]
    return sorted(rows, key=lambda row: (str(row["name"]).casefold(), row["code"]))


def domain():
    global _domain
    if _domain is None:
        with _domain_lock:
            if _domain is None:
                from database import DatabaseManager
                from managers.admin_control_manager import AdminControlManager
                from managers.futures_manager import FuturesManager
                from managers.market_manager import MarketManager
                from managers.market_session_manager import MarketSessionManager
                from managers.option_manager import OptionManager
                from managers.order_book_manager import OrderBookManager
                from managers.user_manager import UserManager
                from managers.rate_manager import RateManager
                from managers.currency_exchange_manager import CurrencyExchangeManager
                from managers.lottery_manager import LotteryManager
                from managers.bank_manager import BankManager
                from managers.investment_manager import InvestmentManager

                db = DatabaseManager()
                user_manager = UserManager(db)
                order_book = OrderBookManager(db, user_manager)
                option_manager = OptionManager(db, user_manager)
                futures_manager = FuturesManager(db, user_manager)
                order_book.option_manager = option_manager
                order_book.futures_manager = futures_manager
                from managers.admin_manager import AdminManager

                market_manager = MarketManager(db)
                # The administrator command layer expects the same manager
                # attributes as the Discord bot object.
                bot = SimpleNamespace(
                    db=db, user_mgr=user_manager, order_book=order_book,
                    option_mgr=option_manager, futures_mgr=futures_manager,
                    rate_mgr=RateManager(db), currency_mgr=CurrencyExchangeManager(db),
                    lottery_mgr=LotteryManager(db), market_mgr=market_manager,
                    bank_mgr=BankManager(db), investment_mgr=InvestmentManager(db, user_manager),
                    treasury_bond_mgr=user_manager.bond_manager,
                )
                from managers.interest_manager import InterestManager
                bot.interest_mgr = InterestManager(db)
                bot.admin_control = AdminControlManager(bot)
                bot.admin_mgr = AdminManager(db)
                from managers.capital_markets_manager import CapitalMarketsManager
                bot.capital_markets = CapitalMarketsManager(db, user_manager, order_book)
                _domain = SimpleNamespace(
                    db=db, users=user_manager, order_book=order_book,
                    options=option_manager, futures=futures_manager,
                    market=market_manager, admin_control=bot.admin_control,
                    market_session=MarketSessionManager(db),
                    admin_manager=bot.admin_mgr,
                    bot=bot,
                )
    return _domain


def admin_overview():
    with connect_readonly() as connection:
        active_users = connection.execute(
            f"SELECT COUNT(*) FROM users WHERE status='active' AND {citizen_filter()}"
        ).fetchone()[0]
        active_assets = connection.execute(
            "SELECT (SELECT COUNT(*) FROM market_assets WHERE status='active') + "
            "(SELECT COUNT(*) FROM company WHERE status='active') + "
            "(SELECT COUNT(*) FROM investment WHERE status='active') + "
            "(SELECT COUNT(*) FROM treasury_bond_products WHERE status='active')"
        ).fetchone()[0]
        open_orders = connection.execute(
            "SELECT COUNT(*) FROM market_orders WHERE status IN ('open','partial')"
        ).fetchone()[0]
        sessions = connection.execute(
            "SELECT exchange_id,status,reason,trading_halted,updated_epoch,resume_epoch,circuit_stage "
            "FROM market_exchanges ORDER BY exchange_id"
        ).fetchall()
        from managers.capital_markets_manager import CapitalMarketsManager
        deficits = CapitalMarketsManager.bank_deficits(connection)
    return {
        "activeUsers": int(active_users),
        "activeInstruments": int(active_assets),
        "openOrders": int(open_orders),
        "market": [dict(session) for session in sessions],
        "bankDeficits": deficits,
    }


@log_slow("portfolio_snapshot", threshold_ms=100)
def portfolio(user_id):
    return domain().users.get_total_assets(user_id, settle=False)


def user_dr_movements(user_id, limit=50):
    # Apply additive DR migrations before this endpoint reads its tables.
    domain()
    from functions.money import currency_precision
    from decimal import Decimal
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT m.movement_id,m.dr_code,d.underlying_code,m.movement_type,m.dr_quantity,"
            "m.underlying_quantity,COALESCE(m.holder_underlying_average_cost,m.underlying_average_cost) underlying_average_cost,"
            "m.recorded_epoch,COALESCE(m.holder_underlying_currency,"
            "(SELECT quote_currency FROM company WHERE company_code=d.underlying_code),"
            "(SELECT quote_currency FROM market_assets WHERE code=d.underlying_code),base_currency_code()) underlying_currency "
            "FROM depositary_receipt_movements m JOIN depositary_receipts d USING(dr_code) "
            "WHERE m.user_id=? ORDER BY m.recorded_epoch DESC,m.movement_id DESC LIMIT ?",
            (str(user_id), max(1, min(100, int(limit)))),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            places = currency_precision(connection, row["underlying_currency"])
            row["underlying_average_cost_text"] = f"{Decimal(str(row['underlying_average_cost'] or 0)):.{places}f}"
            row["decimal_places"] = places
    return result


def user_dr_voting_delegations(user_id, limit=100):
    """Return only this owner's delegation records and their internal event history."""
    domain()
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT d.delegation_id,d.dr_code,r.underlying_code,d.proxy_user_id,"
            "d.dr_quantity,d.status,d.created_epoch,d.revoked_epoch "
            "FROM dr_voting_delegations d JOIN depositary_receipts r USING(dr_code) "
            "WHERE d.owner_user_id=? ORDER BY d.delegation_id DESC LIMIT ?",
            (str(user_id), max(1, min(100, int(limit)))),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["events"] = [dict(event) for event in connection.execute(
                "SELECT event_type,recorded_epoch FROM dr_voting_delegation_events "
                "WHERE delegation_id=? ORDER BY event_id", (row["delegation_id"],),
            ).fetchall()]
    return result


def user_dr_received_voting_delegations(user_id, limit=100):
    """Show the authenticated proxy only the active rights delegated to them."""
    domain()
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT d.delegation_id,d.dr_code,r.underlying_code,d.owner_user_id,"
            "d.dr_quantity,d.created_epoch FROM dr_voting_delegations d "
            "JOIN depositary_receipts r USING(dr_code) WHERE d.proxy_user_id=? "
            "AND d.status='active' ORDER BY d.delegation_id DESC LIMIT ?",
            (str(user_id), max(1, min(100, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]


def user_dividend_payments(user_id, limit=100):
    """Return the authenticated user's recent dividend receipts, grouped by payout currency."""
    domain()  # Apply additive corporate-action schema migrations before querying.
    from functions.money import currency_precision
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT p.payment_id,d.asset_code,p.quantity,p.gross_amount,p.tax_amount,"
            "p.net_amount_units,p.quote_currency,p.created_epoch,d.dividend_kind,p.allocation_key,"
            "e.dr_code,e.rule_id,e.treaty_code,e.tax_rate_percent "
            "FROM dividend_payments p JOIN dividend_distributions d USING(distribution_id) "
            "LEFT JOIN dr_dividend_withholding_events e USING(payment_id) "
            "WHERE p.user_id=? ORDER BY p.created_epoch DESC,p.payment_id DESC LIMIT ?",
            (str(user_id), max(1, min(100, int(limit)))),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row['decimal_places'] = currency_precision(connection, row['quote_currency'])
    return result


def user_tax_residency_profile(user_id):
    domain()
    with connect_readonly() as connection:
        row = connection.execute(
            "SELECT nationality,tax_residency FROM users WHERE user_id=?", (str(user_id),)
        ).fetchone()
        if not row:
            raise ValueError("user_not_found")
        countries = [item[0] for item in connection.execute(
            "SELECT nationality FROM country_currencies ORDER BY nationality"
        )]
    return {"nationality": row["nationality"], "taxResidency": row["tax_residency"],
            "countries": countries}


def set_user_tax_residency(user_id, residency):
    domain()
    value = str(residency or "").strip() or None
    if value is not None and len(value) > 100:
        raise ValueError("invalid_tax_residency")
    now = int(time.time())
    with connect_db() as connection:
        with connection:
            user = connection.execute(
                "SELECT nationality,tax_residency FROM users WHERE user_id=?", (str(user_id),)
            ).fetchone()
            if not user:
                raise ValueError("user_not_found")
            if value and not connection.execute(
                "SELECT 1 FROM country_currencies WHERE nationality=?", (value,)
            ).fetchone():
                raise ValueError("unknown_tax_residency")
            old = user["tax_residency"]
            connection.execute("UPDATE users SET tax_residency=? WHERE user_id=?",
                               (value, str(user_id)))
            if old != value:
                connection.execute(
                    "INSERT INTO user_tax_residency_events(user_id,old_residency,new_residency,source,recorded_epoch) "
                    "VALUES (?,?,?,?,?)", (str(user_id), old, value, "self_service", now),
                )
    return {"nationality": user["nationality"], "taxResidency": value,
            "effectiveForFutureRegisters": True}


def _credit_report(connection, user_id):
    """Score breakdown for a user, or None on databases without the credit model."""
    if not all(_table_exists(connection, table) for table in
               ("credit_events", "credit_score_history", "loans", "loan_payment_schedule")):
        return None
    from managers.credit_score_manager import CreditScoreManager
    return CreditScoreManager.report(connection, user_id)


def user_credit_report(user_id):
    """Return the authenticated user's credit score factors, events and changes."""
    domain()
    with connect_readonly() as connection:
        return _credit_report(connection, user_id)


def user_loan_schedules(user_id):
    """Return the authenticated user's upcoming loan installment breakdowns."""
    domain()
    from functions.money import currency_precision
    with connect_readonly() as connection:
        loans = [dict(row) for row in connection.execute(
            "SELECT l.loan_id,l.product_code,l.quote_currency,l.outstanding_balance,l.status,"
            "l.rate_type,l.contract_rate,l.next_repricing_epoch,l.benchmark_key,"
            "l.benchmark_tenor_days,l.repricing_lag_seconds,"
            "l.underwriting_dsr,l.underwriting_stress_dsr,l.underwriting_stress_shock_bps,"
            "l.underwriting_fx_stress_shock_percent,"
            "l.origination_pd_12m,"
            "l.collection_status,l.maturity_epoch,l.stage_cure_since_epoch,"
            "(SELECT e.stage FROM loan_ecl_assessments e WHERE e.loan_id=l.loan_id "
            "ORDER BY e.calculated_epoch DESC,e.assessment_id DESC LIMIT 1) current_ecl_stage,"
            "(SELECT e.pd_12m FROM loan_ecl_assessments e WHERE e.loan_id=l.loan_id "
            "ORDER BY e.calculated_epoch DESC,e.assessment_id DESC LIMIT 1) current_ecl_pd_12m "
            "FROM loans l WHERE l.user_id=? "
            "AND l.status IN ('active','defaulted') ORDER BY l.loan_id",
            (str(user_id),),
        ).fetchall()]
        for loan in loans:
            loan["decimal_places"] = currency_precision(connection, loan["quote_currency"])
            loan["installments"] = [dict(row) for row in connection.execute(
                "SELECT schedule_id,installment_number,due_epoch,principal_due,interest_due,fee_due,"
                "principal_paid,interest_paid,fee_paid,amount_paid,status "
                "FROM loan_payment_schedule WHERE loan_id=? AND status NOT IN ('paid','waived') "
                "ORDER BY due_epoch,installment_number LIMIT 12",
                (loan["loan_id"],),
            ).fetchall()]
            loan["rate_resets"] = [dict(row) for row in connection.execute(
                "SELECT old_rate,new_rate,scheduled_epoch,applied_epoch,benchmark_key,"
                "benchmark_tenor_days FROM contract_rate_reset_history "
                "WHERE contract_type='loan' AND contract_id=? "
                "ORDER BY applied_epoch DESC,reset_id DESC LIMIT 3",
                (str(loan["loan_id"]),),
            ).fetchall()]
    return {"loans": loans}


def admin_dr_movements(limit=100):
    """Read-only administrator view of the immutable DR conversion ledger."""
    domain()
    from functions.money import currency_precision
    from decimal import Decimal
    with connect_readonly() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT m.movement_id,m.user_id,m.dr_code,d.underlying_code,m.movement_type,"
            "m.dr_quantity,m.underlying_quantity,COALESCE(m.custody_underlying_average_cost,"
            "m.underlying_average_cost) underlying_average_cost,m.holder_underlying_average_cost,"
            "m.holder_underlying_currency,m.recorded_epoch,"
            "COALESCE(m.custody_underlying_currency,"
            "COALESCE((SELECT quote_currency FROM company WHERE company_code=d.underlying_code),"
            "(SELECT quote_currency FROM market_assets WHERE code=d.underlying_code),base_currency_code())) underlying_currency "
            "FROM depositary_receipt_movements m JOIN depositary_receipts d USING(dr_code) "
            "ORDER BY m.recorded_epoch DESC,m.movement_id DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()]
        for row in rows:
            places = currency_precision(connection, row["underlying_currency"])
            row["underlying_average_cost_text"] = f"{Decimal(str(row['underlying_average_cost'] or 0)):.{places}f}"
            holder_currency = row["holder_underlying_currency"] or row["underlying_currency"]
            holder_places = currency_precision(connection, holder_currency)
            row["holder_underlying_average_cost_text"] = f"{Decimal(str(row['holder_underlying_average_cost'] if row['holder_underlying_average_cost'] is not None else row['underlying_average_cost'] or 0)):.{holder_places}f}"
            row["holder_underlying_currency"] = holder_currency
            row["decimal_places"] = places
    return {"movements": rows}


def admin_dr_custody_events(limit=100):
    """Read-only simulator audit of DR instruction, custody-check and settlement steps."""
    domain()
    with connect_readonly() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT e.event_id,e.reference_key,e.event_sequence,e.event_type,e.dr_code,"
            "d.underlying_code,e.user_id,e.movement_type,e.dr_quantity,"
            "e.underlying_quantity,e.recorded_epoch,i.status AS instruction_status "
            "FROM dr_custody_process_events e JOIN depositary_receipts d USING(dr_code) "
            "LEFT JOIN dr_custody_instructions i USING(reference_key) "
            "ORDER BY e.recorded_epoch DESC,e.event_id DESC LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()]
    return {"events": rows}


def admin_dr_deviation_history(limit=100, dr_code=None):
    """Read-only DR reference-price observations and alert transitions."""
    domain()
    from managers.dr_deviation_monitor_manager import DrDeviationMonitorManager
    with connect_readonly() as connection:
        return DrDeviationMonitorManager.history_conn(connection, limit, dr_code)


def book(code):
    if not any(row["code"] == code for row in instruments()):
        return None
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT side,limit_price,SUM(remaining_quantity) AS quantity,COUNT(*) AS order_count "
            "FROM market_orders WHERE asset_code=? AND status IN ('open','partial') "
            "GROUP BY side,limit_price", (code,),
        ).fetchall()
    bids = sorted((dict(row) for row in rows if row["side"] == "buy"), key=lambda row: -row["limit_price"])
    asks = sorted((dict(row) for row in rows if row["side"] == "sell"), key=lambda row: row["limit_price"])
    return {"bids": bids[:12], "asks": asks[:12]}


CHART_RANGES = {"1h": 3600, "1d": 86400, "1w": 604800, "1m": 2592000, "all": None}
# Candle periods mirror the Discord chart: 1m, 5m, 10m, 30m, 1h, 6h, 12h, 1d.
CANDLE_INTERVALS = (60, 300, 600, 1800, 3600, 21600, 43200, 86400)
DEFAULT_CANDLE_INTERVAL = {"1h": 60, "1d": 600, "1w": 3600, "1m": 21600, "all": 86400}
MAX_CANDLES = 500


def candles(connection, code, start, interval):
    """OHLC buckets aggregated in SQLite so long ranges never load every tick."""
    rows = connection.execute(
        "WITH buckets AS ("
        " SELECT (recorded_epoch/?)*? AS bucket,MIN(recorded_epoch) AS first_epoch,"
        " MAX(recorded_epoch) AS last_epoch,MIN(price) AS low,MAX(price) AS high,COUNT(*) AS ticks"
        " FROM market_price_history WHERE asset_code=? AND recorded_epoch>=? GROUP BY bucket"
        " ORDER BY bucket DESC LIMIT ?)"
        " SELECT b.bucket,o.price AS open,b.high,b.low,c.price AS close,b.ticks FROM buckets b"
        " JOIN market_price_history o ON o.asset_code=? AND o.recorded_epoch=b.first_epoch"
        " JOIN market_price_history c ON c.asset_code=? AND c.recorded_epoch=b.last_epoch"
        " ORDER BY b.bucket",
        (interval, interval, code, start, MAX_CANDLES, code, code),
    ).fetchall()
    return [{"time": int(row["bucket"]), "open": native_value(row["open"]), "high": native_value(row["high"]),
             "low": native_value(row["low"]), "close": native_value(row["close"]), "ticks": int(row["ticks"])}
            for row in rows]


def candle_volumes(connection, code, start, interval):
    """Traded quantity per candle bucket — the volume the Discord /그래프 indicators use."""
    return {int(row[0]): float(row[1]) for row in connection.execute(
        "SELECT (recorded_epoch/?)*? AS bucket,SUM(quantity) FROM market_trade_history "
        "WHERE asset_code=? AND recorded_epoch>=? GROUP BY bucket", (interval, interval, code, start))}


def chart_indicators(candle_rows, upper, lower):
    """Same calculations and options as the Discord /그래프 chart, aligned to candle_rows."""
    from functions.technical_indicators import UPPER_INDICATORS, LOWER_INDICATORS, calculate_indicators
    upper = [key for key in upper if key in UPPER_INDICATORS]
    lower = [key for key in lower if key in LOWER_INDICATORS and key != "volume"]
    if not candle_rows or not (upper or lower):
        return {"upper": {}, "lower": {}}
    top, bottom = calculate_indicators(candle_rows, upper, lower)
    clean = lambda groups: {key: [{"label": item["label"], "kind": item["kind"], "levels": list(item["levels"]),
                                   "color": "#%02x%02x%02x" % tuple(item["color"]),
                                   "values": [None if value is None or not math.isfinite(value) else round(value, 6) for value in item["values"]]}
                                  for item in series] for key, series in groups.items()}
    return {"upper": clean(top), "lower": clean(bottom)}


def chart_catalog():
    from functions.technical_indicators import UPPER_INDICATORS, LOWER_INDICATORS
    return {"upper": [{"key": key, "label": label} for key, label in UPPER_INDICATORS.items()],
            "lower": [{"key": key, "label": label} for key, label in LOWER_INDICATORS.items()]}


def chart(code, range_key, interval=None, upper=(), lower=(), with_catalog=False):
    if range_key not in CHART_RANGES:
        raise ValueError("unsupported_chart_range")
    if interval is None:
        interval = DEFAULT_CANDLE_INTERVAL[range_key]
    if interval not in CANDLE_INTERVALS:
        raise ValueError("unsupported_candle_interval")
    item = next((row for row in instruments() if row["code"] == code), None)
    if not item:
        return None
    start = 0 if CHART_RANGES[range_key] is None else int(time.time()) - CHART_RANGES[range_key]
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT recorded_epoch,price FROM market_price_history "
            "WHERE asset_code=? AND recorded_epoch>=? ORDER BY recorded_epoch DESC LIMIT 5000",
            (code, start),
        ).fetchall()
        candle_rows = candles(connection, code, start, interval)
        volumes = candle_volumes(connection, code, candle_rows[0]["time"], interval) if candle_rows else {}
    for row in candle_rows:
        row["volume"] = volumes.get(row["time"], 0.0)
    points = [{"time": int(row["recorded_epoch"]), "price": native_value(row["price"])}
              for row in reversed(rows)]
    if len(points) > 1200:
        stride = math.ceil(len(points) / 1200)
        points = points[::stride] + ([points[-1]] if points[-1] != points[::stride][-1] else [])
    return {"instrument": item, "points": points, "candles": candle_rows,
            "interval": interval, "range": range_key,
            "indicators": chart_indicators(candle_rows, upper, lower),
            **({"catalog": chart_catalog()} if with_catalog else {})}


MAX_DRAWINGS = 200


def ensure_drawings_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS web_chart_drawings (
        user_id TEXT NOT NULL, asset_code TEXT NOT NULL, drawings TEXT NOT NULL,
        updated_epoch INTEGER NOT NULL, PRIMARY KEY(user_id, asset_code))""")


def _finite(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid_drawing")
    if positive and value <= 0:
        raise ValueError("invalid_drawing")
    return float(value)


def normalize_drawings(value):
    """Keep only time/price anchors so drawings survive zoom and interval changes."""
    if not isinstance(value, list) or len(value) > MAX_DRAWINGS:
        raise ValueError("invalid_drawing")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid_drawing")
        drawing_id, kind, anchors = item.get("id"), item.get("type"), item.get("anchors")
        expected = {"trend": 2, "horizontal": 1}.get(kind)
        if (not isinstance(drawing_id, str) or not 1 <= len(drawing_id) <= 64 or not expected or
                not isinstance(anchors, list) or len(anchors) != expected):
            raise ValueError("invalid_drawing")
        result.append({"id": drawing_id, "type": kind, "anchors": [
            {"time": _finite(anchor.get("time") if isinstance(anchor, dict) else None),
             "price": _finite(anchor.get("price") if isinstance(anchor, dict) else None, positive=True)}
            for anchor in anchors
        ]})
    return result


def get_drawings(user_id, code):
    with connect_db(timeout=2) as connection:
        ensure_drawings_table(connection)
        row = connection.execute(
            "SELECT drawings FROM web_chart_drawings WHERE user_id=? AND asset_code=?", (user_id, code),
        ).fetchone()
    return json.loads(row[0]) if row else []


def save_drawings(user_id, code, drawings):
    if not isinstance(code, str) or not 1 <= len(code) <= 80:
        raise ValueError("instrument_not_found")
    drawings = normalize_drawings(drawings)
    with connect_db(timeout=2) as connection:
        ensure_drawings_table(connection)
        if drawings:
            connection.execute(
                "INSERT INTO web_chart_drawings(user_id,asset_code,drawings,updated_epoch) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id,asset_code) DO UPDATE SET drawings=excluded.drawings,"
                "updated_epoch=excluded.updated_epoch",
                (user_id, code, json.dumps(drawings, separators=(",", ":")), int(time.time())),
            )
        else:
            connection.execute(
                "DELETE FROM web_chart_drawings WHERE user_id=? AND asset_code=?", (user_id, code),
            )
    return drawings


def execute_order(user_id, payload):
    from decimal import Decimal
    if not isinstance(payload, dict):
        raise ValueError("주문 정보를 확인해주세요.")
    code, side = str(payload.get("assetCode", "")).strip(), payload.get("side")
    quantity, price = payload.get("quantity"), payload.get("limitPrice")
    if not code or len(code) > 80 or side not in {"buy", "sell"}:
        raise ValueError("종목과 매수·매도 방향을 확인해주세요.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("주문 수량은 1 이상의 정수여야 합니다.")
    if isinstance(price, bool) or not isinstance(price, (int, float, str, Decimal)):
        raise ValueError("지정가는 숫자로 입력해주세요.")
    try:
        price = native_value(price)
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError("지정가는 유한한 숫자로 입력해주세요.") from None
    if price <= 0 or price > 10**24:
        raise ValueError("지정가는 0보다 크고 허용 한도 이하여야 합니다.")
    if price == int(price):
        price = int(price)
    success, message = domain().order_book.place_limit_order(user_id, code, side, quantity, price)
    return success, message


def cancel_order(user_id, order_id):
    success, message = domain().order_book.cancel_order(user_id, order_id)
    return success, message


def set_market_state(actor, payload):
    actions = {"close", "open", "pause", "resume", "circuit", "sidecar"}
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in actions:
        raise ValueError("invalid_market_action")
    return domain().market_session.control(
        actor, action, stage=payload.get("stage"), exchange_id=payload.get("exchangeId") or "ALL"
    )


def set_instrument_status(actor, code, payload):
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"active", "inactive"}:
        raise ValueError("status must be active or inactive")
    item = next((row for row in instruments(include_inactive=True) if row["code"] == code), None)
    if not item:
        return False, "자산 코드를 찾을 수 없습니다."
    category = _instrument_category(code, item, allow_option=True)
    if not category:
        return False, "이 상품 유형의 상태 변경은 아직 지원하지 않습니다."
    result = domain().admin_control.entity(
        actor, category, "restore" if status == "active" else "delete", code,
    )
    return True, result


def _instrument_category(code, item, allow_option=False):
    """Admin category of a listed instrument (bond variants by their feature)."""
    source = item.get("source")
    if source == "investment":
        if item.get("type") == "corporate_bond":
            from managers.admin_control_manager import BOND_FEATURES
            with connect_readonly() as connection:
                row = connection.execute(
                    "SELECT bond_feature FROM investment WHERE product_code=?", (code,)
                ).fetchone()
            feature = (row[0] if row else None) or "plain"
            return next((k for k, v in BOND_FEATURES.items() if v == feature), "corporatebond")
        return {
            "mutual_fund": "fund", "index_fund": "indexfund", "stock_future": "future",
            "commodity_spot": "commodity", "etf": "etf", "etn": "etn", "els": "els",
        }.get(item.get("type"))
    return {"company": "stock", "market_assets": "asset", "treasury_bond": "bond",
            **({"option_contracts": "option"} if allow_option else {})}.get(source)


def edit_instrument(actor, code, payload):
    if not isinstance(payload, dict) or set(payload) - {"name", "price", "supply", "fundCategory", "components"}:
        raise ValueError("상품 설정 필드를 확인해주세요.")
    item = next((row for row in instruments(include_inactive=True) if row["code"] == code), None)
    if not item:
        return False, "자산 코드를 찾을 수 없습니다."
    category = _instrument_category(code, item)
    if not category:
        return False, "이 상품 유형의 설정 변경은 아직 지원하지 않습니다."
    extra = {}
    if item.get("type") == "mutual_fund":
        if "fundCategory" in payload:
            extra["fund_category"] = payload["fundCategory"]
        if "components" in payload:
            if not isinstance(payload["components"], dict):
                raise ValueError("components는 자산 코드별 비중 객체여야 합니다.")
            extra["components"] = payload["components"]
    elif "fundCategory" in payload or "components" in payload:
        raise ValueError("펀드 구성은 일반 펀드에서만 수정할 수 있습니다.")
    result = domain().admin_control.entity(
        actor, category, "edit", code,
        name=payload.get("name", ""), price=payload.get("price", ""),
        supply=payload.get("supply", ""), extra=extra,
    )
    return True, result


def create_fund(actor, payload):
    allowed = {"code", "name", "unitPrice", "totalSupply", "exchangeId", "fundCategory", "components"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("펀드 생성 필드를 확인해주세요.")
    code = str(payload.get("code", "")).strip()
    name = str(payload.get("name", "")).strip()
    exchange_id = str(payload.get("exchangeId", "")).strip()
    components = payload.get("components")
    if not code or len(code) > 80 or not name or not exchange_id:
        raise ValueError("펀드 코드, 이름, 거래소를 입력해주세요.")
    if not isinstance(components, dict) or not components:
        raise ValueError("펀드 구성 자산과 비중을 입력해주세요.")
    result = domain().admin_control.entity(
        actor, "fund", "create", code, name=name,
        price=str(payload.get("unitPrice", "")), supply=str(payload.get("totalSupply", "")),
        extra={
            "exchange_id": exchange_id,
            "fund_category": payload.get("fundCategory"),
            "components": components,
        },
    )
    return True, result


def manage_fund(actor, code, payload=None):
    from managers.fund_manager import FundManager
    manager = FundManager(domain().db)
    with manager.db.readonly() as connection:
        manager._require(connection, actor, code)
    if payload is None:
        with connect_readonly() as connection:
            currency = _configured_base_currency(connection)
        return {**manager.snapshot(code), "currency": currency}
    if not isinstance(payload, dict):
        raise ValueError('펀드 작업 내용을 확인해주세요.')
    action = payload.get('action')
    if action == 'activate' and set(payload) <= {'action', 'expectedFunding'}:
        success, message = manager.activate(actor, code, expected_funding=payload.get('expectedFunding'))
    elif action == 'order' and set(payload) == {'action', 'assetCode', 'side', 'quantity', 'price'}:
        success, message = manager.order(actor, code, str(payload['assetCode']).strip(),
                                         payload['side'], payload['quantity'], payload['price'])
    elif action == 'cancel' and set(payload) == {'action', 'orderId'}:
        success, message = manager.cancel(actor, code, payload['orderId'])
    elif action == 'rebalance' and set(payload) == {'action'}:
        result = manager.rebalance(actor, code)
        with connect_readonly() as connection:
            result['fund']['currency'] = _configured_base_currency(connection)
        return result
    else:
        raise ValueError('펀드 작업 또는 입력 필드를 확인해주세요.')
    with connect_readonly() as connection:
        currency = _configured_base_currency(connection)
    return {'success': success, 'message': str(message).replace('기준 통화', currency), 'fund': {**manager.snapshot(code), 'currency': currency}}


def admin_users(search, kind="people"):
    """People and country accounts (``kind='people'``) or company capital accounts (``kind='company'``)."""
    term = f"%{search.strip()}%"
    with connect_readonly() as connection:
        has_companies = _table_exists(connection, "company_accounts")
        company = "EXISTS(SELECT 1 FROM company_accounts a WHERE a.user_id=users.user_id)" if has_companies else "0"
        rows = connection.execute(
            "SELECT user_id,nickname,nationality,"
            "cash,credit_score,status,role, "
            f"CASE WHEN {company} THEN 'company' "
            "WHEN EXISTS(SELECT 1 FROM country_treasuries t WHERE t.user_id=users.user_id) THEN 'country' ELSE 'discord' END AS account_type, "
            + ("(SELECT a.company_code FROM company_accounts a WHERE a.user_id=users.user_id)" if has_companies else "NULL")
            + " AS company_code FROM users WHERE user_id<>? AND (user_id LIKE ? OR nickname LIKE ?) "
            f"AND {'' if kind == 'company' else 'NOT '}{company} "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,nickname LIMIT 100",
            (TREASURY_USER_ID, term, term),
        ).fetchall()
    return [{"userId": row["user_id"], "nickname": row["nickname"],
             "nationality": row["nationality"], "cash": int(row["cash"] or 0),
             "creditScore": int(row["credit_score"] or 0), "status": row["status"],
             "role": row["role"], "accountType": row["account_type"],
             "companyCode": row["company_code"]} for row in rows]


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def admin_events(limit=50):
    with connect_readonly() as connection:
        if not _table_exists(connection, "market_events"):
            return []
        columns = {row[1] for row in connection.execute("PRAGMA table_info(market_events)")}
        optional = ",".join(
            column if column in columns else f"NULL AS {column}"
            for column in ("target_mode", "target_value", "volatility_percent")
        )
        rows = connection.execute(
            "SELECT event_id,asset_code,event_type,severity,starts_epoch,ends_epoch,created_by,"
            f"{optional} FROM market_events ORDER BY event_id DESC LIMIT ?", (limit,),
        ).fetchall()
    now = int(time.time())
    return [{**dict(row), "active": row["starts_epoch"] <= now < row["ends_epoch"]} for row in rows]


def admin_market_flows(limit=100):
    """Automatic price targets drawn by the market engine, newest first."""
    with connect_readonly() as connection:
        if not _table_exists(connection, "market_auto_target_history"):
            return []
        rows = connection.execute(
            "SELECT history_id,asset_code,anchor_price,target_price,start_epoch,end_epoch "
            "FROM market_auto_target_history ORDER BY history_id DESC LIMIT ?", (limit,)).fetchall()
    now = int(time.time())
    return [{**dict(row), "change_percent": (row["target_price"] / row["anchor_price"] - 1) * 100,
             "active": row["start_epoch"] <= now < row["end_epoch"]} for row in rows]


# Log tables an administrator may clear: (table, id column, condition that keeps a row deletable).
LOG_TABLES = {
    "events": ("market_events", "event_id", "ends_epoch<=:now"),
    "flows": ("market_auto_target_history", "history_id", "1"),
    "jobs": ("web_bot_jobs", "job_id", "status IN ('done','failed')"),
}
LOG_NAMES = {"events": "시장 사건", "flows": "알고리즘 시장 흐름", "jobs": "봇 작업"}


def admin_log_delete(actor, log, payload):
    """Delete log rows: the given ids, or every deletable one with all=true.

    Running market events and pending bot jobs are never deleted. The immutable
    administrator audit trail is intentionally not exposed by this endpoint.
    """
    if log not in LOG_TABLES:
        raise ValueError("삭제할 기록 종류를 확인해주세요.")
    table, key, keep = LOG_TABLES[log]
    params = {"now": int(time.time())}
    if payload.get("all") is True:
        scope = "1"
    else:
        ids = payload.get("ids")
        if (not isinstance(ids, list) or not ids or len(ids) > 1000
                or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)):
            raise ValueError("삭제할 기록을 골라 주세요.")
        scope = f"{key} IN ({','.join(f':i{n}' for n in range(len(ids)))})"
        params.update({f"i{n}": value for n, value in enumerate(ids)})
    with connect_db(timeout=5) as connection:
        if not _table_exists(connection, table):
            return f"✅ 삭제할 {LOG_NAMES[log]}이 없습니다."
        where = f"({keep}) AND {scope}"
        if log == "events" and _table_exists(connection, "market_event_targets"):
            connection.execute(f"DELETE FROM market_event_targets WHERE event_id IN (SELECT event_id FROM market_events WHERE {where})", params)
        removed = connection.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount
        _audit_db(connection, actor, f"log.{log}.delete", {"all": payload.get("all") is True,
                                                            "ids": payload.get("ids"), "removed": removed})
    skipped = " 진행 중인 사건은 삭제하지 않습니다." if log == "events" else " 대기·처리 중인 작업은 삭제하지 않습니다." if log == "jobs" else ""
    return f"✅ {LOG_NAMES[log]} {removed}건을 삭제했습니다.{skipped if not removed or payload.get('all') is True else ''}"


def admin_audit(limit=50):
    with connect_readonly() as connection:
        if not _table_exists(connection, "admin_audit"):
            return []
        rows = connection.execute(
            "SELECT audit_id,actor_id,action,payload,recorded_epoch FROM admin_audit "
            "ORDER BY audit_id DESC LIMIT ?", (limit,),
        ).fetchall()
    entries = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {"raw": row["payload"]}
        entries.append({"auditId": row["audit_id"], "actorId": row["actor_id"], "action": row["action"],
                        "payload": payload, "time": float(row["recorded_epoch"])})
    return entries


def audit_admin_access(actor_id, action, payload):
    """Append a minimal audit event for sensitive administrator reads."""
    with connect_db(timeout=5) as connection:
        if not _table_exists(connection, "admin_audit"):
            raise ValueError("admin_audit_unavailable")
        connection.execute(
            "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) "
            "VALUES (?,?,?,?)",
            (str(actor_id), str(action),
             json.dumps(payload, ensure_ascii=False, default=str), time.time()),
        )


def admin_bank_journal_report(issuer, currency=None, since_epoch=None):
    """Read-only bank-book controls for an administrator's audit workbench."""
    from managers.bank_journal import BankJournal
    from managers.accounting_journal import AccountingJournal
    from functions.money import currency_precision
    from decimal import Decimal

    def money_text(value, places):
        amount = Decimal(str(value or 0))
        return f"{amount:.{places}f}"

    from functions.banks import bank_issuer
    with connect_readonly() as plain:
        # The shared ledger managers still write legacy product./assets. prefixes.
        connection = LegacyNames(plain)
        # Banks are named by their ID; the books are keyed by the issuer name.
        issuer = bank_issuer(connection, issuer)
        trial_balance = BankJournal.trial_balance(connection, issuer, currency)
        common_currencies = [str(row[0]) for row in connection.execute(
            "SELECT DISTINCT currency_code FROM accounting_entries "
            "WHERE ledger_scope='institution' AND entity_id=? "
            + ("AND currency_code=? " if currency else "")
            + "ORDER BY currency_code",
            (str(issuer), str(currency).upper()) if currency else (str(issuer),),
        ).fetchall()]
        common_trial_balance = []
        for common_currency in common_currencies:
            places = currency_precision(connection, common_currency)
            scale = 10 ** places
            for row in AccountingJournal.trial_balance(
                    connection, ledger_scope="institution", entity_id=issuer,
                    currency_code=common_currency):
                common_trial_balance.append({
                    **row,
                    "debit_amount": money_text(Decimal(row["debit_units"]) / scale, places),
                    "credit_amount": money_text(Decimal(row["credit_units"]) / scale, places),
                    "balance_amount": money_text(Decimal(row["balance_units"]) / scale, places),
                })
        financial_close_control = AccountingJournal.reconcile_all_financial_ledgers(connection)
        common_journal_reconciliation = financial_close_control["common_journal_checks"]
        common_journal_unbalanced = AccountingJournal.audit_unbalanced(connection)
        amount_snapshot_findings = financial_close_control["amount_snapshot_findings"]
        unbalanced = BankJournal.audit_unbalanced(connection, issuer)
        deposit_reconciliation = BankJournal.reconcile_customer_deposits(connection, issuer)
        loan_reconciliation = BankJournal.reconcile_loan_receivables(connection, issuer)
        loan_schedule_reconciliation = BankJournal.reconcile_loan_payment_schedules(
            connection, issuer,
        )
        ecl_allowance_reconciliation = BankJournal.reconcile_ecl_allowances(connection, issuer)
        chargeoff_memo_reconciliation = BankJournal.reconcile_charged_off_balances(
            connection, issuer,
        )
        deposit_insurance_reconciliation = BankJournal.reconcile_deposit_insurance_claims(
            connection, issuer,
        )
        insurance_claims = [dict(row) for row in connection.execute(
            "SELECT c.user_id,c.account_number,a.quote_currency AS currency_code,"
            "c.covered_amount,c.uninsured_amount,c.payout_currency,c.payout_amount,"
            "c.payout_amount_minor_units,c.payout_decimal_places,c.resolved_epoch "
            "FROM accounts.deposit_insurance_claims c "
            "LEFT JOIN accounts.accounts a ON a.account_number=c.account_number "
            "WHERE c.issuer=? ORDER BY c.resolved_epoch DESC,c.claim_id DESC LIMIT 100",
            (str(issuer),),
        ).fetchall()]
        reconciliation = BankJournal.reconcile_institution_results(
            connection, issuer, since_epoch=since_epoch,
        )
        from managers.rate_manager import RateManager
        maturity_gap = RateManager.institution_maturity_gap(connection, issuer)
        liquidity_stress = RateManager.institution_liquidity_stress(connection, issuer)
        rate_stress = RateManager.institution_rate_stress(connection, issuer)
        fixed_rate_eve_stress = RateManager.institution_fixed_rate_eve_stress(connection, issuer)
        fx_stress = RateManager.institution_fx_stress(connection, issuer)
        institution = RateManager.institution_components(connection, issuer)
        balance_sheet = RateManager.institution_balance_sheet(connection, issuer)
        risk_report = balance_sheet["rwa_report"]
        from managers.credit_risk_manager import CreditRiskManager
        credit_stress = CreditRiskManager.portfolio_default_stress(connection, issuer)
        correlated_credit_stress = CreditRiskManager.correlated_default_stress(
            connection, issuer,
        )
        borrower_concentration = CreditRiskManager.borrower_concentration_stress(
            connection, issuer
        )
        capital_ratio = (float(institution["effective_equity"]) / risk_report["amount"]
                         if risk_report["amount"] > 0 else None)
        leverage_exposure = max(
            0.0, float(balance_sheet["deposits"])+float(institution["effective_equity"])
        )
        leverage_ratio = (float(institution["effective_equity"])/leverage_exposure
                          if leverage_exposure > 0 else None)
        prudential = {"issuer": str(issuer), "currency": balance_sheet["base_currency"],
                      "decimalPlaces": currency_precision(connection, balance_sheet["base_currency"]),
                      "status": institution["status"],
                      "effectiveEquity": institution["effective_equity"],
                      "riskWeightedAssets": risk_report["amount"],
                      "capitalRatioPercent": capital_ratio * 100 if capital_ratio is not None else None,
                      "targetCapitalRatioPercent": institution["target_capital_ratio"] * 100,
                      "leverageExposure": leverage_exposure,
                      "leverageRatioPercent": leverage_ratio*100 if leverage_ratio is not None else None,
                      "minimumLeverageRatioPercent": RateManager.MIN_LEVERAGE_RATIO*100,
                      "exposureCount": risk_report["exposureCount"],
                      "cashCollateralCovered": risk_report["cashCollateralCovered"],
                      "weightedComponents": risk_report["weightedComponents"],
                      "method": risk_report["method"]}
        for row in trial_balance:
            places = currency_precision(connection, row["currency_code"])
            scale = 10 ** places
            row["debit_amount"] = money_text(Decimal(row["debit_units"]) / scale, places)
            row["credit_amount"] = money_text(Decimal(row["credit_units"]) / scale, places)
            row["balance_amount"] = money_text(Decimal(row["balance_units"]) / scale, places)
            row["decimal_places"] = places
        for row in unbalanced:
            places = currency_precision(connection, row["currency_code"])
            scale = 10 ** places
            row["debit_amount"] = money_text(Decimal(row["debit_units"]) / scale, places)
            row["credit_amount"] = money_text(Decimal(row["credit_units"]) / scale, places)
        for row in reconciliation:
            places = currency_precision(connection, row["currency_code"])
            row["ledger_amount_text"] = money_text(row["ledger_amount"], places)
            row["journal_amount_text"] = (money_text(row["journal_amount"], places)
                                           if row["journal_amount"] is not None else None)
            row["difference_text"] = (money_text(row["difference"], places)
                                      if row["difference"] is not None else None)
        for row in deposit_reconciliation:
            scale = 10 ** row["decimal_places"]
            row["account_balance"] = money_text(Decimal(row["account_units"]) / scale,
                                                row["decimal_places"])
            row["journal_balance"] = money_text(Decimal(row["journal_units"]) / scale,
                                                row["decimal_places"])
            row["difference"] = money_text(Decimal(row["difference_units"]) / scale,
                                           row["decimal_places"])
        for row in loan_reconciliation:
            scale = 10 ** row["decimal_places"]
            row["account_balance"] = money_text(Decimal(row["account_units"]) / scale,
                                                row["decimal_places"])
            row["journal_balance"] = money_text(Decimal(row["journal_units"]) / scale,
                                                row["decimal_places"])
            row["difference"] = money_text(Decimal(row["difference_units"]) / scale,
                                           row["decimal_places"])
        for row in ecl_allowance_reconciliation:
            scale = 10 ** row["decimal_places"]
            row["account_balance"] = money_text(Decimal(row["account_units"]) / scale,
                                                row["decimal_places"])
            row["journal_balance"] = money_text(Decimal(row["journal_units"]) / scale,
                                                row["decimal_places"])
            row["difference"] = money_text(Decimal(row["difference_units"]) / scale,
                                           row["decimal_places"])
        for row in deposit_insurance_reconciliation:
            if row["decimal_places"] is None:
                row["claim_amount"] = row["journal_amount"] = row["difference"] = None
                continue
            scale = 10 ** row["decimal_places"]
            row["claim_amount"] = money_text(Decimal(row["claim_units"]) / scale,
                                             row["decimal_places"])
            row["journal_amount"] = (money_text(Decimal(row["journal_units"]) / scale,
                                                 row["decimal_places"])
                                      if row["journal_units"] is not None else None)
            row["difference"] = (money_text(Decimal(row["difference_units"]) / scale,
                                            row["decimal_places"])
                                 if row["difference_units"] is not None else None)
        for row in insurance_claims:
            row["currency_code"] = str(row["currency_code"] or balance_sheet["base_currency"])
            places = currency_precision(connection, row["currency_code"])
            row["covered_amount_text"] = money_text(row["covered_amount"], places)
            row["uninsured_amount_text"] = money_text(row["uninsured_amount"], places)
            row["decimal_places"] = places
            if row["payout_currency"] and row["payout_amount_minor_units"] is not None:
                payout_places = currency_precision(connection, row["payout_currency"])
                row["payout_amount_text"] = money_text(
                    Decimal(row["payout_amount_minor_units"]) / (10 ** payout_places),
                    payout_places,
                )
                row["payout_decimal_places"] = payout_places
            elif row["payout_currency"] and row["payout_amount"] is not None:
                payout_places = currency_precision(connection, row["payout_currency"])
                row["payout_amount_text"] = money_text(row["payout_amount"], payout_places)
                row["payout_decimal_places"] = payout_places
    return {"issuer": str(issuer), "currency": currency,
            "trialBalance": trial_balance, "unbalancedEntries": unbalanced,
            "resultReconciliation": reconciliation,
            "depositReconciliation": deposit_reconciliation,
            "loanReconciliation": loan_reconciliation,
            "loanScheduleReconciliation": loan_schedule_reconciliation,
            "eclAllowanceReconciliation": ecl_allowance_reconciliation,
            "chargeoffMemoReconciliation": chargeoff_memo_reconciliation,
            "depositInsuranceReconciliation": deposit_insurance_reconciliation,
            "depositInsuranceClaims": insurance_claims,
            "commonJournalReconciliation": common_journal_reconciliation,
            # Hundreds of thousands of snapshot findings once made this response 130 MB; the page
            # shows the first 100 and the count (all snapshot findings are in amountSnapshotAudit).
            "financialCloseControl": {
                **{key: value for key, value in financial_close_control.items()
                   if key not in ("findings", "amount_snapshot_findings")},
                "findingCount": len(financial_close_control["findings"]),
                "findings": financial_close_control["findings"][:100],
            },
            "commonTrialBalance": common_trial_balance,
            "commonJournalUnbalancedEntries": common_journal_unbalanced,
            "amountSnapshotAudit": {
                "findingCount": len(amount_snapshot_findings),
                "findings": amount_snapshot_findings[:100],
                "status": "clean" if not amount_snapshot_findings else "attention_required",
            },
            "maturityGap": maturity_gap,
            "liquidityStress": liquidity_stress, "rateStress": rate_stress,
            "fixedRateEveStress": fixed_rate_eve_stress, "fxStress": fx_stress,
            "prudential": prudential, "creditStress": credit_stress,
            "correlatedCreditStress": correlated_credit_stress,
            "borrowerConcentrationStress": borrower_concentration}


def admin_compliance_overview(nationality=None):
    """Return bounded, privacy-minimal KYC/AML queues for active administrators."""
    from functions.money import currency_precision
    from decimal import Decimal
    with connect_readonly() as connection:
        kyc_sql = ("SELECT user_id,status,risk_tier,reviewed_by,reviewed_epoch,expires_epoch,updated_epoch "
                   "FROM kyc_profiles")
        aml_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(aml_alerts)"
        ).fetchall()}
        aml_unit_columns = "amount_minor_units,amount_decimal_places," if {
            "amount_minor_units", "amount_decimal_places"
        } <= aml_columns else ""
        alert_sql = ("SELECT alert_id,user_id,nationality,rule_code,reference_key,currency_code,amount,"
                     f"{aml_unit_columns}"
                     "status,outcome_code,opened_epoch,reviewed_by,reviewed_epoch FROM aml_alerts")
        params = ()
        if nationality:
            kyc_sql += " WHERE user_id IN (SELECT user_id FROM users WHERE nationality=?)"
            alert_sql += " WHERE nationality=?"
            params = (str(nationality),)
        kyc_sql += " ORDER BY updated_epoch DESC LIMIT 100"
        alert_sql += " AND status<>'closed' ORDER BY opened_epoch DESC LIMIT 100" if nationality else " WHERE status<>'closed' ORDER BY opened_epoch DESC LIMIT 100"
        profiles = [dict(row) for row in connection.execute(kyc_sql, params).fetchall()]
        from functions.time_utils import validate_epoch
        for row in profiles:
            if row["expires_epoch"] is None:
                continue
            try:
                row["expires_epoch"] = validate_epoch(row["expires_epoch"])
            except ValueError:
                row["expires_epoch"] = None
                if row["status"] == "verified":
                    row["status"] = "expired"
            else:
                if row["status"] == "verified" and row["expires_epoch"] <= time.time():
                    row["status"] = "expired"
        alerts = [dict(row) for row in connection.execute(alert_sql, params).fetchall()]
        for row in alerts:
            places = currency_precision(connection, row["currency_code"])
            value = (Decimal(int(row["amount_minor_units"])).scaleb(
                -int(row["amount_decimal_places"])
            ) if "amount_minor_units" in row else Decimal(str(row["amount"] or 0)))
            row["amount_text"] = f"{value:.{places}f}"
        rule_sql = "SELECT nationality,currency_code,large_transaction_limit,rolling_24h_amount_limit,rolling_24h_count_limit,enabled,updated_epoch FROM aml_monitoring_rules"
        if nationality:
            rule_sql += " WHERE nationality=?"
        rule_sql += " ORDER BY nationality"
        rules = [dict(row) for row in connection.execute(rule_sql, params).fetchall()]
        kyc_event_sql = ("SELECT e.event_id,e.user_id,e.actor_id,e.event_type,e.prior_status,e.new_status,"
                         "e.prior_risk_tier,e.new_risk_tier,e.reason_code,e.recorded_epoch "
                         "FROM kyc_profile_events e")
        aml_event_sql = ("SELECT e.event_id,e.alert_id,e.actor_id,e.event_type,e.prior_status,e.new_status,"
                         "e.outcome_code,e.recorded_epoch,a.nationality,a.user_id "
                         "FROM aml_case_events e JOIN aml_alerts a ON a.alert_id=e.alert_id")
        if nationality:
            kyc_event_sql += " JOIN users u ON u.user_id=e.user_id WHERE u.nationality=?"
            aml_event_sql += " WHERE a.nationality=?"
        kyc_event_sql += " ORDER BY e.recorded_epoch DESC LIMIT 100"
        aml_event_sql += " ORDER BY e.recorded_epoch DESC LIMIT 100"
        kyc_events = [dict(row) for row in connection.execute(kyc_event_sql, params).fetchall()]
        aml_events = [dict(row) for row in connection.execute(aml_event_sql, params).fetchall()]
        scenario_sql = ("SELECT nationality,scenario_key,probability,pd_multiplier,lgd_multiplier,updated_epoch "
                        "FROM credit_risk_scenarios")
        ecl_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(loan_ecl_assessments)"
        ).fetchall()}
        loan_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(loans)"
        ).fetchall()}
        risk_grade_column = "e.risk_grade" if "risk_grade" in ecl_columns else "NULL AS risk_grade"
        nonaccrual_column = "l.nonaccrual_since_epoch" if "nonaccrual_since_epoch" in loan_columns else "NULL AS nonaccrual_since_epoch"
        ecl_sql = ("SELECT e.assessment_id,e.loan_id,e.as_of_day,e.stage,e.pd_12m,e.lifetime_pd,e.lgd,"
                   "e.ead,e.expected_loss,e.discount_factor,e.quote_currency,e.scenario,"
                   "e.effective_interest_rate,e.expected_loss_time_years,e.macro_pd_multiplier,"
                   f"e.calculated_epoch,{risk_grade_column},{nonaccrual_column},u.nationality "
                   "FROM loan_ecl_assessments e JOIN loans l USING(loan_id) "
                   "JOIN users u ON u.user_id=l.user_id")
        if nationality:
            scenario_sql += " WHERE nationality=?"
            ecl_sql += " WHERE u.nationality=?"
        scenario_sql += " ORDER BY nationality,CASE scenario_key WHEN 'optimistic' THEN 1 WHEN 'base' THEN 2 ELSE 3 END"
        ecl_sql += " ORDER BY e.calculated_epoch DESC LIMIT 100"
        scenarios = [dict(row) for row in connection.execute(scenario_sql, params).fetchall()]
        try:
            support_exists = bool(connection.execute(
                "PRAGMA accounts.table_info(loan_credit_support)"
            ).fetchall())
        except Exception:
            support_exists = False
        credit_support = []
        policy_overrides = []
        future_commitments = []
        collateral_recovery = []
        if support_exists:
            support_sql = ("SELECT s.support_id,s.loan_id,s.party_user_id,s.support_type,"
                           "s.exposure_share_bps,s.underwriting_dti,s.underwriting_dsr,"
                           "s.guarantee_limit_minor_units,s.quote_currency,"
                           "s.guarantee_recovery_bps,s.recovery_days,s.recovery_cost_bps,"
                           "s.seniority_rank,s.active,s.updated_epoch FROM accounts.loan_credit_support s")
            if nationality:
                support_sql += " JOIN accounts.loans l ON l.loan_id=s.loan_id JOIN users.users u ON u.user_id=l.user_id WHERE u.nationality=?"
            support_sql += " ORDER BY s.updated_epoch DESC LIMIT 100"
            credit_support = [dict(row) for row in connection.execute(support_sql, params).fetchall()]
            policy_overrides = [dict(row) for row in connection.execute(
                "SELECT nationality,product_code,pd_multiplier,grade_a_max,grade_b_max,"
                "grade_c_max,grade_d_max,grade_e_max,grade_f_max,updated_epoch,updated_by "
                "FROM accounts.credit_risk_policy_overrides "
                + ("WHERE nationality=? " if nationality else "")
                + "ORDER BY nationality,product_code LIMIT 200", params,
            ).fetchall()]
            future_commitments = [dict(row) for row in connection.execute(
                "SELECT c.loan_id,c.committed_limit_minor_units,l.quote_currency,"
                "c.credit_conversion_factor_bps,c.expires_epoch,c.active,c.updated_epoch,c.updated_by "
                "FROM accounts.loan_future_commitments c JOIN accounts.loans l USING(loan_id) "
                "JOIN users.users u ON u.user_id=l.user_id "
                + ("WHERE u.nationality=? " if nationality else "")
                + "ORDER BY c.updated_epoch DESC LIMIT 100", params,
            ).fetchall()]
            for commitment in future_commitments:
                places = currency_precision(connection, commitment["quote_currency"])
                commitment["decimal_places"] = places
                commitment["committed_limit_text"] = f"{Decimal(int(commitment['committed_limit_minor_units'])).scaleb(-places):.{places}f}"
            collateral_recovery = [dict(row) for row in connection.execute(
                "SELECT c.collateral_id,c.loan_id,c.asset_code,c.seniority_rank,c.recovery_days,"
                "c.recovery_cost_bps,c.last_value,l.quote_currency,c.status,u.nationality "
                "FROM accounts.loan_collateral_assets c JOIN accounts.loans l USING(loan_id) "
                "JOIN users.users u ON u.user_id=l.user_id "
                + ("WHERE u.nationality=? " if nationality else "")
                + "ORDER BY c.collateral_id DESC LIMIT 100", params,
            ).fetchall()]
        ecl_assessments = [dict(row) for row in connection.execute(ecl_sql, params).fetchall()]
        from managers.credit_risk_manager import CreditRiskManager
        ecl_backtest = CreditRiskManager.backtest_12m(connection, nationality)
        ecl_backtest["gradeTransitions"] = CreditRiskManager.grade_transition_report(
            connection, nationality,
        )["transitions"]
        ecl_backtest["gradeTransitionMethod"] = (
            "per-loan first grade to latest grade; simulator-only, not an independent migration matrix"
        )
        for row in ecl_assessments:
            places = currency_precision(connection, row["quote_currency"])
            row["decimal_places"] = places
            row["ead_text"] = f"{Decimal(str(row['ead'])):.{places}f}"
            row["expected_loss_text"] = f"{Decimal(str(row['expected_loss'])):.{places}f}"
        try:
            has_chargeoff_events = bool(connection.execute(
                "PRAGMA accounts.table_info(loan_chargeoff_events)"
            ).fetchall())
        except Exception:
            has_chargeoff_events = False
        chargeoff_events = ([dict(row) for row in connection.execute(
            "SELECT e.event_id,e.loan_id,e.event_key,e.event_type,e.amount_minor_units,"
            "e.decimal_places,e.currency_code,e.actor_id,e.created_epoch,e.memo,u.nationality "
            "FROM accounts.loan_chargeoff_events e JOIN accounts.loans l USING(loan_id) "
            "JOIN users.users u ON u.user_id=l.user_id "
            + ("WHERE u.nationality=? " if nationality else "")
            + "ORDER BY e.event_id DESC LIMIT 100", params,
        ).fetchall()] if has_chargeoff_events else [])
    return {"kycProfiles": profiles, "amlAlerts": alerts, "amlRules": rules,
            "kycEvents": kyc_events, "amlEvents": aml_events,
            "creditRiskScenarios": scenarios, "creditSupport": credit_support,
            "creditPolicyOverrides": policy_overrides,
            "futureCommitments": future_commitments,
            "collateralRecovery": collateral_recovery, "chargeoffEvents": chargeoff_events,
            "eclAssessments": ecl_assessments,
            "eclBacktest": ecl_backtest}


# $도움말 is a Discord view; the dashboard shows the same pages on its own.
# $주총 sends Discord DMs, so it is queued for the running bot (web_bot_jobs).
NATIONAL_DISCORD_ONLY = {"도움말"}


def national_command_specs():
    from functions.admin_prefix import NATIONAL, NATIONAL_COMMANDS
    return {
        name: [{"label": label, "default": default,
                "required": not ("비우면" in label or "선택" in label), "long": "JSON" in label}
               for label, default in NATIONAL[name]]
        for name in sorted(NATIONAL_COMMANDS - NATIONAL_DISCORD_ONLY)
    }


def run_national_command(actor, payload):
    """Run a $ national command as the treasury, exactly like the Discord form."""
    from functions.admin_prefix import ActionModal
    command = str(payload.get("command", "")).strip().removeprefix("$")
    specs = national_command_specs()
    if command not in specs:
        raise ValueError("알 수 없는 국가 명의 명령어입니다.")
    values = payload.get("values", [])
    fields = specs[command]
    if (not isinstance(values, list) or len(values) != len(fields) or
            any(not isinstance(value, str) or len(value) > 4000 for value in values)):
        raise ValueError("입력값을 확인해주세요.")
    values = [value.strip() for value in values]
    for field, value in zip(fields, values):
        if field["required"] and not value:
            raise ValueError(f"{field['label']}을(를) 입력해주세요.")
    if command == "주총":
        return queue_shareholder_meeting(actor, {"code": values[0], "place": values[1]})
    from decimal import InvalidOperation
    from database.db_manager import DBManager
    form = SimpleNamespace(bot=domain().bot, command=command, owner=actor)
    try:
        with DBManager.admin_scope(actor):
            result = ActionModal._execute(form, values)
    except (KeyError, InvalidOperation, sqlite3.IntegrityError) as exc:
        raise ValueError(str(exc) or "명령어를 처리하지 못했습니다.") from exc
    _read_cache.invalidate()
    return str(result or "완료했습니다.")


def admin_command_catalog():
    from managers.admin_commands_manager import COMMANDS, USAGE, CREATE_ARGS
    try:
        from functions.admin_embeds import creation_argument_lines
        # The Discord embed text uses Markdown emphasis the dashboard shows as plain text.
        creation_help = creation_argument_lines().replace("**", "").replace("`", "")
    except Exception:
        creation_help = "\n".join(f"{kind}: {' '.join(args)}" for kind, args in CREATE_ARGS.items())
    return {
        "commands": [{"name": name, "usage": USAGE.get(name, "")} for name in sorted(COMMANDS)],
        "national": [{"name": name, "fields": fields} for name, fields in national_command_specs().items()],
        "discordOnly": sorted(NATIONAL_DISCORD_ONLY),
        "creationHelp": creation_help,
    }


# Tables the dashboard must never expose or edit: credentials, the audit trail
# itself, and per-user web state.
DB_EDITOR_HIDDEN = {"web_auth_credentials", "admin_audit", "admin_audit_delete_grant", "web_chart_drawings"}
DB_EDITOR_PAGE_SIZE = 50
DB_VALUE_TYPES = (str, int, float, type(None))
# Secrets stay hidden in the editor and the audit trail; they can still be overwritten.
SENSITIVE_COLUMNS = {"password", "password_hash", "salt"}
HIDDEN_VALUE = "<hidden>"


def _editable_tables(connection):
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {row["name"]: "WITHOUT ROWID" not in str(row["sql"] or "").upper()
            for row in rows if row["name"] not in DB_EDITOR_HIDDEN}


def _table_columns(connection, table):
    return [{"name": row["name"], "type": row["type"] or "", "notNull": bool(row["notnull"]),
             "primaryKey": bool(row["pk"]), "default": row["dflt_value"]}
            for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _require_table(connection, table):
    tables = _editable_tables(connection)
    if not isinstance(table, str) or table not in tables:
        raise ValueError("table_not_found")
    return tables[table]


def _cell(value):
    return f"<blob {len(value)} bytes>" if isinstance(value, (bytes, memoryview)) else value


def _masked(row):
    return {key: HIDDEN_VALUE if key in SENSITIVE_COLUMNS and value is not None else _cell(value)
            for key, value in row.items()}


def _db_value(value):
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, DB_VALUE_TYPES) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("invalid_value")
    if isinstance(value, str) and len(value) > 20000:
        raise ValueError("invalid_value")
    return value


def _db_changes(columns, values):
    names = {column["name"] for column in columns}
    if not isinstance(values, dict) or not values or set(values) - names:
        raise ValueError("invalid_columns")
    return {name: _db_value(value) for name, value in values.items()}


def db_tables():
    with connect_readonly() as connection:
        tables = _editable_tables(connection)
        return [{"name": name, "editable": editable, "columns": _table_columns(connection, name)}
                for name, editable in tables.items()]


def db_rows(table, search="", offset=0):
    offset = max(0, int(offset))
    with connect_readonly() as connection:
        has_rowid = _require_table(connection, table)
        columns = _table_columns(connection, table)
        where, params = "", []
        term = str(search or "").strip()
        if term:
            where = " WHERE " + " OR ".join(f'CAST("{column["name"]}" AS TEXT) LIKE ?' for column in columns)
            params = [f"%{term}%"] * len(columns)
        rowid = "rowid AS __rowid__," if has_rowid else ""
        order = " ORDER BY rowid DESC" if has_rowid else ""
        total = connection.execute(f'SELECT COUNT(*) FROM "{table}"{where}', params).fetchone()[0]
        rows = connection.execute(
            f'SELECT {rowid}* FROM "{table}"{where}{order} LIMIT ? OFFSET ?',
            (*params, DB_EDITOR_PAGE_SIZE, offset),
        ).fetchall()
    return {"table": table, "editable": has_rowid, "columns": columns, "total": int(total),
            "offset": offset, "pageSize": DB_EDITOR_PAGE_SIZE,
            "rows": [_masked({key: row[key] for key in row.keys()}) for row in rows]}


def _audit_db(connection, actor, action, payload):
    if _table_exists(connection, "admin_audit"):
        connection.execute(
            "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
            (actor, action, json.dumps(payload, ensure_ascii=False, default=str), time.time()),
        )


def _row_snapshot(connection, table, rowid):
    row = connection.execute(f'SELECT * FROM "{table}" WHERE rowid=?', (rowid,)).fetchone()
    if not row:
        raise ValueError("row_not_found")
    return _masked({key: row[key] for key in row.keys()})


def db_update(actor, table, rowid, values):
    with connect_db(timeout=5) as connection:
        if not _require_table(connection, table):
            raise ValueError("table_not_editable")
        changes = _db_changes(_table_columns(connection, table), values)
        changes = {key: value for key, value in changes.items()
                   if not (key in SENSITIVE_COLUMNS and value == HIDDEN_VALUE)}
        if not changes:
            raise ValueError("invalid_columns")
        before = _row_snapshot(connection, table, rowid)
        assignments = ",".join(f'"{name}"=?' for name in changes)
        connection.execute(f'UPDATE "{table}" SET {assignments} WHERE rowid=?', (*changes.values(), rowid))
        after = _row_snapshot(connection, table, rowid)
        _audit_db(connection, actor, "db.update", {"table": table, "rowid": rowid,
                  "before": {key: before[key] for key in changes}, "after": {key: after[key] for key in changes}})
    _read_cache.invalidate()
    return after


def db_insert(actor, table, values):
    with connect_db(timeout=5) as connection:
        if not _require_table(connection, table):
            raise ValueError("table_not_editable")
        changes = _db_changes(_table_columns(connection, table), values)
        names = ",".join(f'"{name}"' for name in changes)
        marks = ",".join("?" for _ in changes)
        rowid = connection.execute(f'INSERT INTO "{table}"({names}) VALUES ({marks})', tuple(changes.values())).lastrowid
        row = _row_snapshot(connection, table, rowid)
        _audit_db(connection, actor, "db.insert", {"table": table, "rowid": rowid, "values": row})
    _read_cache.invalidate()
    return {"__rowid__": rowid, **row}


def db_delete(actor, table, rowid):
    with connect_db(timeout=5) as connection:
        if not _require_table(connection, table):
            raise ValueError("table_not_editable")
        before = _row_snapshot(connection, table, rowid)
        connection.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,))
        _audit_db(connection, actor, "db.delete", {"table": table, "rowid": rowid, "values": before})
    _read_cache.invalidate()


# ---------------------------------------------------------------------------
# Product management for every product kind ($상품생성/$상품수정/$상품삭제)
# ---------------------------------------------------------------------------
PRODUCT_KINDS = {
    # profile: (Korean label, dashboard group, $상품생성 kind token)
    "stock": ("회사 주식", "market", "company"), "preferred": ("우선주", "market", "preferred"),
    "asset": ("시장 자산", "market", "asset"), "etf": ("ETF", "market", "etf"),
    "dr": ("주식예탁증서(DR)", "market", "dr"),
    "future": ("주식선물", "market", "future"), "corporatebond": ("회사채", "market", "corporatebond"),
    "fund": ("일반 펀드", "market", "fund"), "indexfund": ("인덱스 펀드", "market", "indexfund"),
    "commodity": ("현물 원자재", "market", "commodity"), "bond": ("국채", "market", "bond"),
    "option": ("옵션", "market", "option"), "els": ("ELS", "market", "els"),
    "saving": ("저축", "finance", "saving"), "loan": ("대출", "finance", "loan"),
    "currency": ("통화", "finance", "currency"),
    "etn": ("ETN", "market", "etn"),
    "convertiblebond": ("전환사채 (CB)", "market", "convertiblebond"),
    "warrantbond": ("신주인수권부사채 (BW)", "market", "warrantbond"),
    "abs": ("자산유동화증권 (ABS)", "market", "abs"), "mbs": ("주택저당증권 (MBS)", "market", "mbs"),
    "abcp": ("자산유동화기업어음 (ABCP)", "market", "abcp"),
    "cma": ("CMA", "finance", "cma"), "ima": ("IMA (종합투자계좌)", "finance", "ima"),
    "wrap": ("랩어카운트", "finance", "wrap"),
}
# Savings and foreign-currency accounts share one form in the dashboard; only the
# account currency (and therefore who can open it) differs.
SAVING_FAMILY = {"saving"}
SAVING_FIELDS = ("name", "quote_currency", "interest_rate", "deposit_amount", "maturity_days",
                 "interest_cycle", "compound_type", "issuer")
PRODUCT_PROTECTED = {"created_at", "created_epoch", "sold_amount", "writer_id", "contract_id",
                     "issued_contracts", "status", "product_type", "outstanding_contracts",
                     "collateral_account", "settled_epoch", "max_shareholder_id"}


def _profile_category(profile):
    return "stock" if profile == "preferred" else profile


def _profile_table(profile):
    from managers.admin_control_manager import ENTITIES
    table, key, _price, _supply = ENTITIES[_profile_category(profile)]
    return table, key


def _form_fields(profile):
    from managers.admin_commands_manager import CREATE_ARGS
    return SAVING_FIELDS if profile in SAVING_FAMILY else CREATE_ARGS[profile]


def product_catalog():
    from managers.admin_commands_manager import OPTIONAL_ARGS, TEXT_ARGS
    from managers.admin_control_manager import EXAMPLES
    from functions.admin_embeds import ARG_LABELS
    from functions.banks import bank_ids
    kinds = []
    with connect_readonly() as connection:
        banks = bank_ids(connection)
        for profile, (label, group, token) in PRODUCT_KINDS.items():
            table, key = _profile_table(profile)
            columns = {row["name"]: row["type"] or "" for row in connection.execute(f'PRAGMA table_info("{table}")')}
            fields = [{"key": field,
                       "label": "발행총액" if profile == "bond" and field == "total_supply"
                                else ("결제 통화" if profile == "dr" else "통장 화폐") if field == "quote_currency"
                                else "발행 은행 ID" if field == "issuer"
                                else "상장 거래소 (새 법인은 비상장 · 법인 자본에서 상장)" if field == "exchange_id" and profile == "stock"
                                else "상장 거래소 ID (비상장이면 3DSG)" if field == "exchange_id" and profile == "preferred"
                                else ARG_LABELS.get(field, field),
                       "optional": field in OPTIONAL_ARGS.get(profile, ()),
                       "text": field in TEXT_ARGS or field == "quote_currency"} for field in _form_fields(profile)]
            listed = {field["key"] for field in fields} | {key}
            example = dict(EXAMPLES.get(_profile_category(profile), {}))
            if "issuer" in example:
                # The default bank is offered by its ID; blank when it has none yet.
                example["issuer"] = banks.get(example["issuer"], "")
            advanced = [{"key": name, "label": ARG_LABELS.get(name, name), "type": kind}
                        for name, kind in columns.items() if name not in listed and name not in PRODUCT_PROTECTED
                        and (name not in {"settlement_delay_seconds", "issue_fee_bps", "redeem_fee_bps"}
                             or profile == "dr")]
            kinds.append({"kind": profile, "token": token, "label": label, "group": group,
                          "family": "saving" if profile in SAVING_FAMILY else profile,
                          "fields": fields, "advanced": advanced,
                          "example": example,
                          "editable": profile not in {"els"}})
    return {"kinds": kinds}


def _product_kind(source, row):
    from managers.admin_control_manager import KINDS, BOND_FEATURES, SAVING_KINDS
    if source == "company":
        return "preferred" if row.get("share_class") == "preferred" else "stock"
    if source == "investment":
        if row.get("product_type") == "corporate_bond":
            feature = row.get("bond_feature") or "plain"
            return next((kind for kind, value in BOND_FEATURES.items() if value == feature), "corporatebond")
        return next((kind for kind, value in KINDS.items() if value == row.get("product_type")), None)
    if source == "saving":
        kind = row.get("product_kind") or "saving"
        return next((name for name, value in SAVING_KINDS.items() if value == kind), "saving")
    if source == "market_assets" and row.get("type") == "depositary_receipt":
        return "dr"
    return {"market_assets": "asset", "treasury_bond_products": "bond", "option_contracts": "option",
            "liability": "loan", "currencies": "currency"}.get(source)


PRODUCT_SOURCES = (
    ("company", "company_code", "share_price"), ("market_assets", "code", "price"),
    ("investment", "product_code", "unit_price"), ("treasury_bond_products", "product_code", "market_price"),
    ("option_contracts", "contract_code", "strike_price"), ("saving", "product_code", "interest_rate"),
    ("liability", "product_code", "interest_rate"), ("currencies", "code", "reference_rate"),
)


def product_list(group):
    if group not in {"market", "finance"}:
        raise ValueError("invalid_group")
    from functions.banks import bank_ids
    rows = []
    with connect_readonly() as connection:
        banks = bank_ids(connection)
        for table, key, headline in PRODUCT_SOURCES:
            if not _table_exists(connection, table):
                continue
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                item = dict(row)
                kind = _product_kind(table, item)
                if not kind or PRODUCT_KINDS[kind][1] != group:
                    continue
                product = {"code": str(item[key]), "name": str(item.get("name") or item.get("underlying_code") or item[key]),
                             "kind": kind, "status": item.get("status") or "active",
                             "exchangeId": item.get("exchange_id"), "currency": item.get("quote_currency"),
                             "value": item.get(headline),
                             # The kind's own form columns, so each kind's bento lists what matters for it.
                             "fields": {field: _cell(banks.get(item[field], item[field]) if field == "issuer" else item[field])
                                        for field in _form_fields(kind) if field in item}}
                if table == "market_assets" and item.get("type") == "depositary_receipt":
                    program = connection.execute(
                        "SELECT * FROM depositary_receipts WHERE dr_code=?", (str(item[key]),)
                    ).fetchone()
                    if program:
                        dr_program = {
                            "underlyingCode": program["underlying_code"],
                            "receiptType": program["receipt_type"],
                            "depositaryBank": program["depositary_bank"],
                            "custodian": program["custodian"],
                            "underlyingPerReceipt": float(program["underlying_per_receipt"]),
                            "receiptSupply": int(program["receipt_supply"]),
                            "depositedUnderlyingQuantity": float(program["deposited_underlying_quantity"]),
                            "programStatus": program["program_status"],
                        }
                        custody = connection.execute(
                            "SELECT COALESCE(SUM(CASE WHEN movement_type='issue' THEN underlying_quantity "
                            "ELSE -underlying_quantity END),0) AS underlying_quantity,"
                            "COALESCE(SUM(CASE WHEN movement_type='issue' THEN dr_quantity "
                            "ELSE -dr_quantity END),0) AS receipt_quantity "
                            "FROM depositary_receipt_movements WHERE dr_code=?", (str(item[key]),),
                        ).fetchone()
                        dr_program["custodiedUnderlyingQuantity"] = max(0, int(custody["underlying_quantity"] or 0))
                        dr_program["outstandingReceiptQuantity"] = max(0, int(custody["receipt_quantity"] or 0))
                        underlying = connection.execute(
                            "SELECT share_price AS price,quote_currency FROM company WHERE company_code=? "
                            "UNION ALL SELECT price,quote_currency FROM market_assets WHERE code=? LIMIT 1",
                            (program["underlying_code"], program["underlying_code"]),
                        ).fetchone()
                        if underlying and float(underlying["price"] or 0) > 0:
                            dr_program["underlyingPrice"] = native_value(underlying["price"])
                            dr_program["underlyingCurrency"] = str(underlying["quote_currency"] or connection.execute("SELECT base_currency_code()").fetchone()[0])
                            try:
                                from managers.currency_manager import CurrencyManager
                                fx = float(CurrencyManager.quote(
                                    connection, dr_program["underlyingCurrency"],
                                    str(item.get("quote_currency") or connection.execute("SELECT base_currency_code()").fetchone()[0]),
                                ))
                                dr_program.update(dr_reference_metrics(
                                    item.get("price"), dr_program["underlyingPrice"],
                                    dr_program["underlyingPerReceipt"], fx,
                                ))
                            except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
                                pass
                        product["drProgram"] = dr_program
                rows.append(product)
    return sorted(rows, key=lambda item: (item["kind"], item["name"].casefold(), item["code"]))


def _locate_product(connection, code, kind=None):
    for table, key, _headline in PRODUCT_SOURCES:
        if not _table_exists(connection, table):
            continue
        row = connection.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (code,)).fetchone()
        if row:
            found = _product_kind(table, dict(row))
            if found and (kind is None or found == kind):
                return found, dict(row)
    raise ValueError("상품 코드를 찾을 수 없습니다.")


def _shareholders(connection, code):
    """Who holds an asset, largest first, and the company's issued shares when it is one."""
    holders = connection.execute(
        "SELECT a.user_id,COALESCE(u.nickname,a.user_id) AS name,SUM(a.quantity) AS quantity FROM user_assets a "
        "LEFT JOIN users u ON u.user_id=a.user_id WHERE a.asset_code=? GROUP BY a.user_id "
        "HAVING SUM(a.quantity)>0 ORDER BY quantity DESC", (code,)).fetchall()
    total = connection.execute("SELECT total_shares FROM company WHERE company_code=?", (code,)).fetchone()
    return {"totalShares": float(total[0] or 0) if total else 0,
            "holders": [{"userId": row[0], "name": row[1], "quantity": float(row[2])} for row in holders]}


def product_detail(code):
    with connect_readonly() as connection:
        kind, row = _locate_product(connection, code)
        values = {}
        for field in _form_fields(kind):
            if field == "components":
                values[field] = ",".join(f"{item['asset_code']}:{item['weight']:g}" for item in connection.execute(
                    "SELECT asset_code,weight FROM fund_components WHERE product_code=? ORDER BY asset_code", (code,)))
            elif field == "dividend_per_share":
                values[field] = (f"{row.get('dividend_percent') or 0:g}%" if row.get("dividend_mode") == "percent"
                                 else row.get("dividend_per_share"))
            elif field == "nationality" and kind == "currency":
                found = connection.execute("SELECT nationality FROM country_currencies WHERE currency_code=? LIMIT 1", (code,)).fetchone()
                values[field] = found[0] if found else None
            elif field == "issuer":
                from functions.banks import bank_ids
                values[field] = bank_ids(connection).get(row.get(field), row.get(field))
            else:
                values[field] = row.get(field)
        if kind == "dr":
            program = connection.execute(
                "SELECT * FROM depositary_receipts WHERE dr_code=?", (code,),
            ).fetchone()
            if program:
                for field in ("underlying_code", "underlying_per_receipt", "receipt_type",
                              "depositary_bank", "custodian", "home_exchange_id", "quote_currency"):
                    values[field] = program[field]
        shareholders = _shareholders(connection, code)
    return {"code": code, "kind": kind, "status": row.get("status") or "active", "shareholders": shareholders,
            "values": {key: _cell(value) for key, value in values.items()},
            "row": _masked(row)}


def _convert_product_fields(profile, fields, advanced, columns):
    """Mirror the positional $상품생성 parser for form values, then add raw columns."""
    from managers.admin_commands_manager import TEXT_ARGS, duration_seconds
    from managers.corporate_actions_manager import CorporateActionsManager
    allowed = set(_form_fields(profile))
    if not isinstance(fields, dict) or set(fields) - allowed or not isinstance(advanced, dict):
        raise ValueError("상품 설정 필드를 확인해주세요.")
    result = {}
    for key, raw in fields.items():
        value = str(raw if raw is not None else "").strip()
        if value == "":
            continue
        if key == "components":
            result[key] = {}
            for component in value.split(","):
                asset, separator, weight = component.strip().rpartition(":")
                if not separator or not asset or asset in result[key]:
                    raise ValueError("구성 종목은 종목코드:비중,종목코드:비중 형식입니다.")
                result[key][asset] = float(weight)
        elif key == "dividend_cycle_seconds":
            result[key] = duration_seconds(value)
        elif key == "dividend_per_share":
            result.update(CorporateActionsManager.dividend_terms(value))
        elif profile == "els" and key in {"underlyings", "observation_days", "redemption_levels"}:
            result[key] = value
        elif key in TEXT_ARGS or key == "quote_currency":
            result[key] = value.upper() if key == "quote_currency" else value
        else:
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = value
    for key, raw in advanced.items():
        if key not in columns or key in PRODUCT_PROTECTED or key in result:
            raise ValueError(f"고급 설정 필드를 확인해주세요: {key}")
        value = _db_value(raw)
        if isinstance(value, str) and re.search(r"INT|REAL|NUM|FLOA|DOUB", columns[key], re.I):
            try:
                value = json.loads(value) if value.strip() else None
            except json.JSONDecodeError:
                pass
        result[key] = value
    return result


def _random_product_code(profile):
    """A free code for a new product left without one: the kind's letters and six random digits, like bank IDs."""
    import random
    prefix = re.sub(r"[^A-Z]", "", str(PRODUCT_KINDS[profile][2]).upper())[:4] or "PRD"
    with connect_readonly() as connection:
        for _ in range(50):
            code = f"{prefix}{random.randrange(10**6):06d}"
            try:
                _locate_product(connection, code)
            except ValueError:
                return code
    raise ValueError("빈 상품 코드를 찾지 못했습니다. 코드를 직접 입력해주세요.")


def product_save(actor, payload):
    from managers.admin_commands_manager import AdminCommandsManager
    from managers.admin_control_manager import ENTITIES
    operation = payload.get("operation")
    profile = payload.get("kind")
    code = str(payload.get("code", "")).strip()
    if operation not in {"create", "edit"} or profile not in PRODUCT_KINDS:
        raise ValueError("상품 종류와 작업을 확인해주세요.")
    if operation == "create" and not code and profile != "option":
        code = _random_product_code(profile)
    if (not code and profile != "option") or len(code) > 80 or re.search(r"\s", code):
        raise ValueError("상품 코드는 공백 없이 1~80자로 입력해주세요.")
    table, _key = _profile_table(profile)
    with connect_readonly() as connection:
        columns = {row["name"]: row["type"] or "" for row in connection.execute(f'PRAGMA table_info("{table}")')}
    fields = _convert_product_fields(profile, payload.get("fields") or {}, payload.get("advanced") or {}, columns)
    bot = domain().bot
    if operation == "create":
        if profile == "stock":
            # A new 법인 starts unlisted; 법인 자본 lists it, with the share of it offered.
            from managers.market_session_manager import MarketSessionManager
            fields["exchange_id"] = MarketSessionManager.UNLISTED
        token = PRODUCT_KINDS[profile][2]
        raw = f"{code} {token} {json.dumps(fields, ensure_ascii=False)}"
        result = AdminCommandsManager(bot).execute(actor, "상품생성", raw)
    else:
        with connect_readonly() as connection:
            kind, row = _locate_product(connection, code, profile)
            if kind == "dr":
                program = connection.execute(
                    "SELECT underlying_code,underlying_per_receipt,receipt_type,depositary_bank,"
                    "custodian,home_exchange_id,quote_currency FROM depositary_receipts WHERE dr_code=?",
                    (code,),
                ).fetchone()
                if program:
                    row.update(dict(program))
        if kind == "els":
            raise ValueError("ELS 발행조건은 변경할 수 없습니다. 모집 중 취소 후 새 상품을 생성해주세요.")
        fields = {key: value for key, value in fields.items() if key not in row or row[key] != value}
        if not fields:
            raise ValueError("변경된 값이 없습니다.")
        category = _profile_category(profile)
        price_key, supply_key = ENTITIES[category][2:]
        if price_key and price_key in fields:
            fields["price"] = fields.pop(price_key)
        if supply_key and supply_key in fields:
            fields["supply"] = fields.pop(supply_key)
        name, price, supply = fields.pop("name", ""), fields.pop("price", ""), fields.pop("supply", "")
        result = bot.admin_control.entity(actor, category, "edit", code, str(name), str(price), str(supply), fields)
    _read_cache.invalidate()
    return str(result)


def product_status(actor, code, active, terminate=False):
    with connect_readonly() as connection:
        kind, row = _locate_product(connection, code)
    if row.get("type") == "depositary_receipt":
        kind = "dr"
    operation = "terminate" if terminate else "restore" if active else "delete"
    result = domain().bot.admin_control.entity(actor, _profile_category(kind), operation, code)
    _read_cache.invalidate()
    return str(result)


# Live obligations that make a product impossible to delete: (table, condition, reason).
PRODUCT_BLOCKERS = (
    ('fund_accounts', 'product_code=?', '펀드 전용 계정과 회계 이력이 있습니다'),
    ("user_assets", "asset_code=? AND quantity<>0", "보유 중인 사용자가 있습니다"),
    ("market_orders", "asset_code=? AND status IN ('open','partial','scheduled')", "미체결·예약 주문이 있습니다"),
    ("accounts", "product_code=?", "이 상품으로 연 계좌가 있습니다"),
    ("loans", "product_code=? AND outstanding_balance>0", "상환되지 않은 대출이 있습니다"),
    ("treasury_bond_holdings", "product_code=?", "국채 보유분이 있습니다"),
    ("futures_positions", "product_code=?", "선물 포지션이 있습니다"),
    ("option_contracts", "underlying_code=? AND status='active'", "이 종목을 기초로 한 진행 중 옵션이 있습니다"),
    ("option_collateral_lots", "underlying_code=?", "옵션 담보로 잡힌 수량이 있습니다"),
    ("loan_collateral_assets", "asset_code=?", "대출 담보로 잡혀 있습니다"),
    ("fund_components", "asset_code=?", "다른 펀드의 구성 종목입니다"),
    ("investment", "(underlying_code=? OR issuer_code=?) AND status='active'", "이 종목을 기초·발행사로 하는 거래 중 상품이 있습니다"),
    ("company", "parent_company_code=? AND status='active'", "연결된 거래 중 우선주가 있습니다"),
    ("els_underlyings", "underlying_code=?", "ELS 기초자산으로 쓰입니다"),
    ("preferred_dividend_claims", "(asset_code=? OR issuer_code=?) AND status IN ('unpaid','partial')", "미지급 배당 권리가 남아 있습니다"),
    ("issuer_cash_accounts", "company_code=? AND (balance>0 OR locked_amount>0)", "법인 현금이 남아 있습니다 ($법인청산)"),
    ("currencies", "asset_code=?", "통화 환율 종목으로 쓰입니다"),
)
# Market data owned by the product, removed with it. Orders, trades, dividends and
# financial statements stay as history.
PRODUCT_RUNTIME = (
    ("market_price_history", "asset_code"), ("market_simulation_state", "asset_code"),
    ("market_quote_overrides", "asset_code"), ("market_auto_targets", "asset_code"),
    ("market_event_targets", "asset_code"), ("market_last_trade", "asset_code"),
    ("web_chart_drawings", "asset_code"), ("fund_components", "product_code"),
    ("leveraged_etf_state", "product_code"), ("market_exchange_index_components", "company_code"),
)
PRODUCT_TABLE = {"stock": ("company", "company_code"), "preferred": ("company", "company_code"),
                 "asset": ("market_assets", "code"), "bond": ("treasury_bond_products", "product_code"),
                 "option": ("option_contracts", "contract_code"), "saving": ("saving", "product_code"),
                 "currencyproduct": ("saving", "product_code"), "loan": ("liability", "product_code"),
                 "currency": ("currencies", "code")}
DELETED_STATES = {"inactive", "cancelled", "settled", "expired", "matured", "redeemed", "defaulted"}


def product_delete(actor, code):
    """Permanently remove a stopped product nobody holds or depends on."""
    with connect_db(timeout=5) as connection:
        kind, row = _locate_product(connection, code)
        table, key = PRODUCT_TABLE.get(kind, ("investment", "product_code"))
        if kind == "dr" or (kind == "asset" and row.get("type") == "depositary_receipt"):
            raise ValueError("DR 프로그램 정의·불변 전환 원장은 삭제하지 않습니다. 보유분을 상환하고 관리자 화면에서 프로그램 종료를 사용해주세요.")
        if (row.get("status") or "active") not in DELETED_STATES:
            raise ValueError("거래 중인 상품입니다. 먼저 중지한 뒤 삭제해주세요.")
        reasons = []
        for blocker, condition, reason in PRODUCT_BLOCKERS:
            if not _table_exists(connection, blocker):
                continue
            params = (code,) * condition.count("?")
            if connection.execute(f"SELECT 1 FROM {blocker} WHERE {condition} LIMIT 1", params).fetchone():
                reasons.append(reason)
        if kind == "currency":
            for other, column in (("country_currencies", "currency_code"), ("accounts", "quote_currency"),
                                  ("company", "quote_currency"), ("market_assets", "quote_currency"),
                                  ("investment", "quote_currency"), ("saving", "quote_currency"),
                                  ("liability", "quote_currency")):
                if _table_exists(connection, other) and connection.execute(
                        f"SELECT 1 FROM {other} WHERE {column}=? LIMIT 1", (code,)).fetchone():
                    reasons.append(f"{other}에서 이 통화를 사용합니다")
            if code == str(connection.execute("SELECT base_currency_code()").fetchone()[0]):
                reasons.append("기준 통화는 삭제할 수 없습니다")
        if reasons:
            raise ValueError("삭제할 수 없습니다: " + " · ".join(dict.fromkeys(reasons)))
        for runtime, column in PRODUCT_RUNTIME:
            if _table_exists(connection, runtime):
                connection.execute(f"DELETE FROM {runtime} WHERE {column}=?", (code,))
        connection.execute(f'DELETE FROM "{table}" WHERE "{key}"=?', (code,))
        _audit_db(connection, actor, "product.purge", {"code": code, "kind": kind, "row": _masked(row)})
    _read_cache.invalidate()
    return f"✅ {code} 상품을 완전히 삭제했습니다. 주문·배당 등 과거 기록은 남아 있습니다."


# ---------------------------------------------------------------------------
# Users, web logins and countries
# ---------------------------------------------------------------------------
USER_FIELDS = ("nickname", "nationality", "cash", "credit_score", "credit_score_reason", "annual_income", "income_stability", "status", "role")


def admin_user_detail(user_id):
    with connect_readonly() as connection:
        row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("사용자를 찾을 수 없습니다.")
        credential = connection.execute(
            "SELECT hts_id,created_epoch,updated_epoch,auth_version FROM web_auth_credentials WHERE user_id=?", (user_id,),
        ).fetchone() if _table_exists(connection, "web_auth_credentials") else None
        accounts = [dict(item) for item in connection.execute(
            "SELECT * FROM accounts WHERE user_id=? ORDER BY account_number", (user_id,))] if _table_exists(connection, "accounts") else []
        loans = [dict(item) for item in connection.execute(
            "SELECT * FROM loans WHERE user_id=? ORDER BY 1 DESC LIMIT 50", (user_id,))] if _table_exists(connection, "loans") else []
        holdings = [dict(item) for item in connection.execute(
            "SELECT asset_code,quantity FROM user_assets WHERE user_id=? AND quantity<>0 ORDER BY asset_code", (user_id,))] if _table_exists(connection, "user_assets") else []
        country = connection.execute(
            "SELECT 1 FROM country_currencies WHERE nationality=?", (user_id,)).fetchone()
        company = _table_exists(connection, "company_accounts") and connection.execute(
            "SELECT 1 FROM company_accounts WHERE user_id=?", (user_id,)).fetchone()
        credit = _credit_report(connection, user_id)
    user = {key: _cell(row[key]) for key in row.keys()}
    return {"user": user, "credit": credit, "accountType": "company" if company else "country" if country and row["nationality"] == user_id else "discord",
            "webLogin": dict(credential) if credential else None,
            "accounts": [_masked(item) for item in accounts],
            "loans": [_masked(item) for item in loans],
            "holdings": holdings}


def admin_update_user(actor, user_id, changes):
    if not isinstance(changes, dict) or not changes or set(changes) - set(USER_FIELDS):
        raise ValueError("허용된 사용자 필드를 입력해주세요.")
    result = domain().admin_control.user(actor, user_id, dict(changes))
    _read_cache.invalidate()
    return result


def admin_set_web_password(actor, user_id, password):
    validate_password(password)
    salt = secrets.token_bytes(16).hex()
    digest = password_hash(password, salt)
    now = int(time.time())
    with connect_db(timeout=5) as connection:
        ensure_auth_table(connection)
        if not connection.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            raise ValueError("사용자를 찾을 수 없습니다.")
        connection.execute(
            "INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET salt=excluded.salt,password_hash=excluded.password_hash,"
            "updated_epoch=excluded.updated_epoch,auth_version=web_auth_credentials.auth_version+1",
            (user_id, salt, digest, now, now),
        )
        _audit_db(connection, actor, "web.password_reset", {"user_id": user_id})
    return "✅ 웹 비밀번호를 설정했습니다. 기존 로그인은 모두 만료됩니다."


def admin_web_login_action(actor, user_id, action, hts_id=None):
    with connect_db(timeout=5) as connection:
        ensure_auth_table(connection)
        if not connection.execute("SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,)).fetchone():
            raise ValueError("웹 로그인이 없는 사용자입니다.")
        if action == "logout":
            connection.execute("UPDATE web_auth_credentials SET auth_version=auth_version+1 WHERE user_id=?", (user_id,))
            message = "✅ 이 사용자의 모든 웹 로그인을 만료했습니다."
        elif action == "remove":
            connection.execute("DELETE FROM web_auth_credentials WHERE user_id=?", (user_id,))
            message = "✅ 웹 로그인을 삭제했습니다. 다시 가입하거나 관리자가 비밀번호를 설정해야 합니다."
        elif action == "hts_id":
            value = normalize_hts_id(hts_id) if hts_id else None
            if value and connection.execute(
                    "SELECT 1 FROM web_auth_credentials WHERE hts_id=? AND user_id<>? UNION ALL "
                    "SELECT 1 FROM users WHERE lower(user_id)=? AND user_id<>?", (value, user_id, value, user_id)).fetchone():
                raise ValueError("hts_id_taken")
            connection.execute("UPDATE web_auth_credentials SET hts_id=?,updated_epoch=? WHERE user_id=?",
                               (value, int(time.time()), user_id))
            message = "✅ 아이디를 변경했습니다." if value else "✅ 아이디를 지웠습니다."
        else:
            raise ValueError("invalid_action")
        _audit_db(connection, actor, f"web.{action}", {"user_id": user_id, "hts_id": hts_id})
    return message


def admin_countries():
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT n.*,c.name AS currency_name,c.status AS currency_status FROM country_currencies n "
            "LEFT JOIN currencies c ON c.code=n.currency_code ORDER BY n.nationality").fetchall()
        result = []
        for row in rows:
            nationality = row["nationality"]
            foreign_rates = connection.execute(
                "SELECT AVG(base_rate) reference_rate,COUNT(*) country_count "
                "FROM country_currencies WHERE nationality<>? "
                "AND currency_code<>?", (nationality, row["currency_code"])
            ).fetchone()
            external_rate = (float(foreign_rates["reference_rate"])
                             if foreign_rates["country_count"] else None)
            external_rate_premium = (max(-2.0, min(
                2.0, (external_rate-float(row["base_rate"]))*0.10
            )) if external_rate is not None else 0.0)
            treasury = connection.execute(
                "SELECT u.user_id,u.cash FROM country_treasuries t JOIN users u ON u.user_id=t.user_id WHERE t.nationality=?",
                (nationality,)).fetchone() if _table_exists(connection, "country_treasuries") else None
            citizens = connection.execute(f"SELECT COUNT(*) FROM users WHERE nationality=? AND {citizen_filter()}",
                                          (nationality,)).fetchone()[0]
            guilds = [item[0] for item in connection.execute(
                "SELECT guild_id FROM guild_countries WHERE nationality=?", (nationality,))] if _table_exists(connection, "guild_countries") else []
            exchanges = [item[0] for item in connection.execute(
                "SELECT exchange_id FROM market_exchanges WHERE nationality=? ORDER BY exchange_id", (nationality,))] if _table_exists(connection, "market_exchanges") else []
            market_rates = [dict(item) for item in connection.execute(
                "SELECT benchmark_key,tenor_days,annual_rate,updated_epoch "
                "FROM market_rate_curve WHERE nationality=? ORDER BY benchmark_key,tenor_days",
                (nationality,))]
            market_rate_pressure = [dict(item) for item in connection.execute(
                "SELECT benchmark_key,adjustment,updated_epoch FROM market_rate_pressure "
                "WHERE nationality=? ORDER BY benchmark_key", (nationality,))]
            rate_history_events = [dict(item) for item in connection.execute(
                "SELECT operation,rate_key,before_percent,after_percent,before_actor,after_actor,"
                "before_epoch,after_epoch,recorded_epoch FROM country_rate_history_events "
                "WHERE nationality=? ORDER BY event_id DESC LIMIT 50", (nationality,)
            )] if _table_exists(connection, "country_rate_history_events") else []
            curve_history_events = [dict(item) for item in connection.execute(
                "SELECT operation,benchmark_key,tenor_days,before_rate,after_rate,before_actor,"
                "after_actor,before_epoch,after_epoch,recorded_epoch "
                "FROM market_rate_curve_history_events WHERE nationality=? "
                "ORDER BY event_id DESC LIMIT 50", (nationality,)
            )] if _table_exists(connection, "market_rate_curve_history_events") else []
            macro_indicators = connection.execute(
                "SELECT inflation_percent,real_gdp_growth_percent,unemployment_percent,"
                "fiscal_debt_percent,credit_stress_shock_bps,fx_stress_shock_percent,"
                "updated_by,updated_epoch "
                "FROM country_macro_indicators WHERE nationality=?", (nationality,)
            ).fetchone() if _table_exists(connection, "country_macro_indicators") else None
            tax_rules = [dict(item) for item in connection.execute(
                "SELECT income_source,tax_percent,updated_by,updated_epoch FROM country_tax_rules "
                "WHERE nationality=? ORDER BY income_source", (nationality,))] \
                if _table_exists(connection, "country_tax_rules") else []
            account = connection.execute(
                "SELECT u.status,u.role,w.hts_id FROM users u JOIN web_auth_credentials w ON w.user_id=u.user_id "
                "WHERE u.user_id=?", (treasury["user_id"] if treasury else None,)).fetchone() if _table_exists(connection, "web_auth_credentials") else None
            result.append({**{key: row[key] for key in row.keys()},
                           "treasuryId": treasury["user_id"] if treasury else None,
                           "treasuryCash": treasury["cash"] if treasury else None,
                           "citizens": int(citizens), "guilds": guilds, "exchanges": exchanges,
                           "marketRates": market_rates, "marketRatePressure": market_rate_pressure,
                           "rateHistoryEvents": rate_history_events,
                           "marketRateHistoryEvents": curve_history_events,
                           "macroIndicators": dict(macro_indicators) if macro_indicators else {},
                           "externalReferenceRate": external_rate,
                           "externalRatePremium": external_rate_premium,
                           "taxRules": tax_rules,
                           "account": dict(account) if account else None})
    return result


def admin_country_action(actor, nationality, payload):
    """Country settings through the same managers as $금리수정·$법정최고이자수정·$세율·$국고."""
    from managers.admin_commands_manager import AdminCommandsManager
    bot = domain().bot
    action = payload.get("action")
    nationality = str(nationality or "").strip()
    quote = lambda value: shlex.quote(str(value))
    if action == "create":
        args = [payload.get("currencyCode", ""), payload.get("baseRate", ""), payload.get("legalMaxRate", ""), payload.get("taxPercent", "")]
        args = [str(value).strip() for value in args]
        while args and not args[-1]:
            args.pop()
        result = AdminCommandsManager(bot).execute(actor, "국가생성", " ".join(quote(value) for value in [str(payload.get("countryId", "")).strip(), nationality, *args]))
    elif action == "delete":
        result = AdminCommandsManager(bot).execute(actor, "국가삭제", quote(nationality))
    elif action == "rates":
        messages = []
        commands = {"baseRate": "금리수정", "legalMaxRate": "법정최고이자수정"}
        for key, command in commands.items():
            if str(payload.get(key, "")).strip() != "":
                messages.append(AdminCommandsManager(bot).execute(actor, command, f"{quote(nationality)} modify {quote(payload[key])}"))
        if str(payload.get("taxPercent", "")).strip() != "":
            messages.append(AdminCommandsManager(bot).execute(actor, "세율", f"{quote(nationality)} {quote(payload['taxPercent'])}"))
        if not messages:
            raise ValueError("변경할 값을 입력해주세요.")
        result = "\n".join(str(message) for message in messages)
    elif action == "market_rate":
        key = str(payload.get("benchmarkKey") or "").strip()
        tenor = str(payload.get("tenorDays") or "").strip()
        annual_rate = str(payload.get("annualRate") or "").strip()
        if key not in {"funding", "bank_bond", "treasury"} or not tenor or not annual_rate:
            raise ValueError("지표·만기·시장금리를 모두 입력해주세요.")
        result = AdminCommandsManager(bot).execute(
            actor, "시장금리수정", " ".join(quote(value) for value in (nationality, key, tenor, annual_rate)),
        )
    elif action == "macro_indicators":
        import math
        bounds = {
            "inflationPercent": (-100, 1000),
            "realGdpGrowthPercent": (-100, 100),
            "unemploymentPercent": (0, 100),
            "fiscalDebtPercent": (0, 1000),
        }
        values = {}
        for key, (low, high) in bounds.items():
            try:
                value = float(payload[key])
            except (KeyError, TypeError, ValueError):
                raise ValueError("거시 지표 네 항목을 모두 숫자로 입력해주세요.") from None
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} 범위가 올바르지 않습니다: {low}~{high}")
            values[key] = value
        raw_shock = payload.get("creditStressShockBps")
        if raw_shock is None:
            current = bot.db.users.execute_one(
                "SELECT credit_stress_shock_bps FROM country_macro_indicators WHERE nationality=?",
                (nationality,),
            ) if hasattr(bot.db, "users") else None
            stress_shock_bps = int(current[0]) if current else 200
        else:
            if isinstance(raw_shock, bool):
                raise ValueError("금리 스트레스 충격은 0~1000bp 정수여야 합니다.")
            try:
                parsed_shock = float(raw_shock)
            except (TypeError, ValueError):
                raise ValueError("금리 스트레스 충격은 0~1000bp 정수여야 합니다.") from None
            if not math.isfinite(parsed_shock) or not parsed_shock.is_integer() or not 0 <= parsed_shock <= 1000:
                raise ValueError("금리 스트레스 충격은 0~1000bp 정수여야 합니다.")
            stress_shock_bps = int(parsed_shock)
        values["creditStressShockBps"] = stress_shock_bps
        raw_fx_shock = payload.get("fxStressShockPercent")
        if raw_fx_shock is None:
            current = bot.db.users.execute_one(
                "SELECT fx_stress_shock_percent FROM country_macro_indicators WHERE nationality=?",
                (nationality,),
            ) if hasattr(bot.db, "users") else None
            fx_stress_shock_percent = float(current[0]) if current else 10.0
        else:
            if isinstance(raw_fx_shock, bool):
                raise ValueError("환율 스트레스 충격은 0~100% 숫자여야 합니다.")
            try:
                fx_stress_shock_percent = float(raw_fx_shock)
            except (TypeError, ValueError):
                raise ValueError("환율 스트레스 충격은 0~100% 숫자여야 합니다.") from None
            if not math.isfinite(fx_stress_shock_percent) or not 0 <= fx_stress_shock_percent <= 100:
                raise ValueError("환율 스트레스 충격은 0~100% 숫자여야 합니다.")
        values["fxStressShockPercent"] = fx_stress_shock_percent
        with bot.db.atomic() as connection:
            bot.admin_control.require(actor, connection)
            cursor = connection.execute(
                "UPDATE country_macro_indicators SET inflation_percent=?,"
                "real_gdp_growth_percent=?,unemployment_percent=?,fiscal_debt_percent=?,"
                "credit_stress_shock_bps=?,fx_stress_shock_percent=?,"
                "updated_by=?,updated_epoch=? WHERE nationality=?",
                (values["inflationPercent"], values["realGdpGrowthPercent"],
                 values["unemploymentPercent"], values["fiscalDebtPercent"], stress_shock_bps,
                 fx_stress_shock_percent,
                 str(actor), time.time(), nationality),
            )
            if cursor.rowcount != 1:
                raise ValueError("국가의 거시 지표 행을 찾을 수 없습니다.")
            bot.admin_control._audit(connection, actor, "country.macro_indicators", {
                "nationality": nationality, **values,
            })
        result = (f"✅ {nationality} 거시 지표를 저장했습니다. 시장금리 재산정과 "
                  f"신규 대출의 금리 +{stress_shock_bps}bp·외화부채 환율 +{fx_stress_shock_percent:g}% "
                  "DSR 스트레스 심사에 반영됩니다.")
    elif action == "treasury":
        from functions.admin_prefix import ActionModal
        from database.db_manager import DBManager
        with connect_readonly() as connection:
            treasury = connection.execute(
                "SELECT user_id FROM country_treasuries WHERE nationality=?", (nationality,)).fetchone()
        if not treasury:
            raise ValueError("국고를 찾을 수 없습니다.")
        from functions.amount_limits import MAX_NATIVE
        amount = bot.admin_control._number(payload.get("amount"), -MAX_NATIVE, MAX_NATIVE, integer=True)
        reason = str(payload.get("reason", "")).strip()[:200]
        with bot.db.atomic() as connection:
            bot.admin_control.require(actor, connection)
            connection.execute("UPDATE users SET cash=? WHERE user_id=?", (amount, treasury[0]))
            bot.admin_control._audit(connection, actor, "treasury.set",
                                     {"nationality": nationality, "amount": amount, "reason": reason})
        result = f"✅ {nationality} 국고 현금 {amount:,}"
    else:
        raise ValueError("invalid_action")
    _read_cache.invalidate()
    return str(result)


# ---------------------------------------------------------------------------
# Exchanges
# ---------------------------------------------------------------------------
EXCHANGE_PRODUCT_TABLES = (("company", "company_code"), ("market_assets", "code"),
                           ("investment", "product_code"), ("treasury_bond_products", "product_code"))


def admin_exchanges():
    from functions import trading_hours
    with connect_readonly() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM market_exchanges ORDER BY exchange_id")]
        for row in rows:
            exchange = row["exchange_id"]
            row["hours"] = trading_hours.from_row(row)
            row["hours_label"] = trading_hours.describe(row["hours"])
            row["products"] = sum(connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE exchange_id=? AND status='active'", (exchange,)).fetchone()[0]
                for table, _key in EXCHANGE_PRODUCT_TABLES if _table_exists(connection, table))
            row["components"] = [dict(item) for item in connection.execute(
                "SELECT i.company_code,i.previous_price,i.total_shares,c.name FROM market_exchange_index_components i "
                "LEFT JOIN company c ON c.company_code=i.company_code WHERE i.exchange_id=? ORDER BY i.company_code",
                (exchange,))] if _table_exists(connection, "market_exchange_index_components") else []
        countries = [row[0] for row in connection.execute("SELECT nationality FROM country_currencies ORDER BY nationality")]
    return {"exchanges": rows, "countries": countries}


def admin_exchange_action(actor, exchange_id, payload):
    action = payload.get("action")
    exchange_id = str(exchange_id or "").strip()
    if not exchange_id or len(exchange_id) > 100 or re.search(r"\s", exchange_id):
        raise ValueError("거래소 ID는 공백 없이 1~100자로 입력해주세요.")
    if action in {"open", "close", "pause", "resume", "circuit", "sidecar"}:
        stage = payload.get("stage")
        success, message = domain().market_session.control(actor, action, stage=stage, exchange_id=exchange_id)
        if not success:
            raise ValueError(message.removeprefix("❌ "))
        return message
    with connect_db(timeout=5) as connection:
        role = connection.execute("SELECT role FROM users WHERE user_id=?", (actor,)).fetchone()
        if actor not in BOOTSTRAP_ADMIN_IDS and (not role or str(role["role"]).strip().lower() != "admin"):
            raise ValueError("admin_required")
        exists = connection.execute("SELECT * FROM market_exchanges WHERE exchange_id=?", (exchange_id,)).fetchone()
        if action in {"create", "nationality"}:
            nationality = str(payload.get("nationality", "")).strip()
            if not connection.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (nationality,)).fetchone():
                raise ValueError("등록된 국가를 선택해주세요.")
            if action == "create":
                if exists:
                    raise ValueError("이미 있는 거래소 ID입니다.")
                connection.execute(
                    "INSERT INTO market_exchanges(exchange_id,status,updated_epoch,trading_halted,nationality) VALUES (?,?,?,?,?)",
                    (exchange_id, "open", int(time.time()), 0, nationality))
                message = f"✅ 거래소 {exchange_id}를 만들었습니다. 상품 생성 시 거래소ID로 지정하세요."
            else:
                if not exists:
                    raise ValueError("거래소를 찾을 수 없습니다.")
                connection.execute("UPDATE market_exchanges SET nationality=? WHERE exchange_id=?", (nationality, exchange_id))
                message = f"✅ {exchange_id} 거래소 국가를 {nationality}(으)로 변경했습니다."
            _audit_db(connection, actor, f"exchange.{action}", {"exchange_id": exchange_id, "nationality": nationality})
        elif action == "hours":
            if not exists:
                raise ValueError("거래소를 찾을 수 없습니다.")
            from functions import trading_hours
            from managers.market_session_manager import MarketSessionManager
            state = MarketSessionManager.set_hours_conn(connection, exchange_id, payload.get("hours"), int(time.time()))
            schedule = trading_hours.from_row(state)
            _audit_db(connection, actor, "exchange.hours", {"exchange_id": exchange_id, **schedule})
            message = f"✅ {exchange_id} 거래 시간을 {trading_hours.describe(schedule)}(으)로 저장했습니다."
        elif action == "delete":
            if not exists:
                raise ValueError("거래소를 찾을 수 없습니다.")
            if exchange_id == "CURRENCY":
                raise ValueError("외환 거래소 CURRENCY는 삭제할 수 없습니다.")
            listed = [row[0] for table, key in EXCHANGE_PRODUCT_TABLES if _table_exists(connection, table)
                      for row in connection.execute(f"SELECT {key} FROM {table} WHERE exchange_id=?", (exchange_id,))]
            linked = [row[0] for row in connection.execute(
                "SELECT product_code FROM investment WHERE underlying_exchange_id=?", (exchange_id,))]
            if listed or linked:
                shown = ", ".join((listed + linked)[:8]) + (" 외" if len(listed + linked) > 8 else "")
                raise ValueError(f"이 거래소에 상장·연동된 상품 {len(listed + linked)}개가 있어 삭제할 수 없습니다: {shown}. "
                                 "상품을 다른 거래소로 옮기거나 중지 후 완전 삭제한 뒤 다시 시도해주세요.")
            connection.execute("DELETE FROM market_exchange_index_components WHERE exchange_id=?", (exchange_id,))
            connection.execute("DELETE FROM market_exchanges WHERE exchange_id=?", (exchange_id,))
            _audit_db(connection, actor, "exchange.delete", {"exchange_id": exchange_id})
            message = f"✅ 거래소 {exchange_id}를 삭제했습니다."
        else:
            raise ValueError("invalid_action")
    _read_cache.invalidate()
    return message


# ---------------------------------------------------------------------------
# $도움말 pages and Discord-only work queued for the bot ($주총)
# ---------------------------------------------------------------------------
def admin_help_pages():
    import asyncio
    from functions.admin_help_view import AdminHelpView
    from functions.admin_prefix import NATIONAL_COMMANDS

    async def build():
        view = AdminHelpView(SimpleNamespace(), "0", NATIONAL_COMMANDS)
        pages = []
        for index, (_key, label) in enumerate(view.PAGES):
            view.page = index
            embed = view.build_embed()
            pages.append({"label": label, "description": embed.description or "",
                          "fields": [{"name": field.name, "value": field.value} for field in embed.fields]})
        view.stop()
        return pages

    # discord.ui.View needs an event loop; the handler thread has none.
    return asyncio.run(build())


def _embed_json(embed):
    return {"title": embed.title or "", "description": embed.description or "",
            "fields": [{"name": field.name, "value": field.value, "inline": bool(field.inline)} for field in embed.fields],
            "footer": embed.footer.text if embed.footer else ""}


def info_panel(user_id, category="main", page=0):
    """/검색 on the web: the same InfoPanelView the Discord bot shows, one category page at a time."""
    import asyncio
    from functions.discord_views import InfoPanelView

    async def build():
        view = InfoPanelView(domain().bot, user_id)
        try:
            if category not in dict(view.CATEGORIES):
                raise ValueError("unknown_info_category")
            view.category, view.page = category, max(0, int(page))
            embed = view.build_embed()
            return {"categories": [{"key": key, "label": label} for key, label in view.CATEGORIES],
                    "category": view.category, "page": view.page, **_embed_json(embed),
                    "hasPrevious": not view.previous_button.disabled, "hasNext": not view.next_button.disabled}
        finally:
            view.stop()
    return asyncio.run(build())


def ensure_bot_jobs_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS web_bot_jobs (
        job_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, actor_id TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','done','failed')),
        result TEXT, created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL)""")


def queue_shareholder_meeting(actor, payload):
    code = str(payload.get("code", "")).strip()
    place = str(payload.get("place", "")).strip()
    if not code or not place or len(code) > 80 or len(place) > 1000:
        raise ValueError("주식 코드와 장소를 입력해주세요.")
    now = int(time.time())
    with connect_db(timeout=5) as connection:
        ensure_bot_jobs_table(connection)
        job_id = connection.execute(
            "INSERT INTO web_bot_jobs(kind,actor_id,payload,created_epoch,updated_epoch) VALUES ('shareholder_meeting',?,?,?,?)",
            (actor, json.dumps({"code": code, "place": place}, ensure_ascii=False), now, now)).lastrowid
        _audit_db(connection, actor, "national.주총.queued", {"job_id": job_id, "code": code})
    return f"⏳ 주총 소집을 Discord 봇에 요청했습니다 (#{job_id}). 봇이 실행 중이면 몇 초 안에 안내 DM을 보냅니다."


def queue_bot_job(kind, actor, payload):
    now = int(time.time())
    with connect_db(timeout=5) as connection:
        ensure_bot_jobs_table(connection)
        return connection.execute(
            "INSERT INTO web_bot_jobs(kind,actor_id,payload,created_epoch,updated_epoch) VALUES (?,?,?,?,?)",
            (kind, actor, json.dumps(payload, ensure_ascii=False), now, now)).lastrowid


def admin_warnings():
    from managers.trading_warning_manager import TradingWarnings
    return {"warnings": TradingWarnings(domain().db).list()}


def admin_warning_action(actor, code, payload):
    from managers.trading_warning_manager import TradingWarnings
    return TradingWarnings(domain().db).set(actor, code, payload.get("level", ""), payload.get("reason", ""),
                                            payload.get("days") or None)


BANK_SETTINGS = ("funding_spread", "operating_cost_spread", "liquidity_spread", "target_margin", "competition_adjustment",
                 "target_capital_ratio", "target_loan_deposit_ratio", "reserve_ratio", "deposit_insurance_limit", "status")


def _bank_summary(connection, row):
    issuer = row["issuer"]
    products = connection.execute(
        "SELECT (SELECT COUNT(*) FROM saving WHERE issuer=?)+(SELECT COUNT(*) FROM liability WHERE issuer=?)",
        (issuer, issuer)).fetchone()[0]
    deposits = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(a.balance),0) FROM accounts a JOIN saving p ON p.product_code=a.product_code "
        "WHERE p.issuer=?", (issuer,)).fetchone()
    loans = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(l.outstanding_balance),0) FROM loans l JOIN liability p ON p.product_code=l.product_code "
        "WHERE p.issuer=? AND l.status='active'", (issuer,)).fetchone()
    account = connection.execute(
        "SELECT b.user_id,u.cash FROM bank_accounts b LEFT JOIN users u ON u.user_id=b.user_id WHERE b.issuer=?",
        (issuer,)).fetchone()
    return {"id": row["institution_id"], "issuer": issuer, "nationality": row["nationality"], "status": row["status"],
            "equityCapital": row["equity_capital"], "retainedEarnings": row["retained_earnings"], "products": products,
            "depositAccounts": deposits[0], "deposits": deposits[1], "loans": loans[0], "loanBalance": loans[1],
            "account": account["user_id"] if account else None, "cash": account["cash"] if account else None}


def _bank_row(connection, bank_id):
    row = connection.execute("SELECT * FROM financial_institutions WHERE institution_id=?", (str(bank_id),)).fetchone()
    if not row:
        raise ValueError("financial_institution_not_found")
    return row


# Banks whose ID another product already uses: they can never get a 법인, so reads stop retrying them.
_BANKS_WITHOUT_COMPANY = set()


def _ensure_bank_companies():
    """Every bank is also a 법인 with a 법인 계정 (managers/bank_companies.py); banks made by a
    product naming a new issuer get theirs the next time the bank list is read."""
    from managers.bank_companies import ensure, missing
    with connect_readonly() as connection:
        if not _table_exists(connection, "company_accounts"):
            return
        pending = [issuer for issuer in missing(connection) if issuer not in _BANKS_WITHOUT_COMPANY]
    if not pending:
        return
    with connect_db(timeout=5) as connection:
        created = [issuer for issuer in pending if ensure(LegacyNames(connection), issuer)]
    _BANKS_WITHOUT_COMPANY.update(set(pending) - set(created))
    if created:
        _read_cache.invalidate()


def admin_banks():
    """Banks (financial institutions) by their unique ID, with what each holds, like the company list."""
    _ensure_bank_companies()
    with connect_readonly() as connection:
        rows = connection.execute("SELECT * FROM financial_institutions ORDER BY institution_id").fetchall()
        return {"banks": [_bank_summary(connection, row) for row in rows]}


def admin_bank_detail(bank_id):
    with connect_readonly() as connection:
        row = _bank_row(connection, bank_id)
        products = [dict(item) for item in connection.execute(
            "SELECT product_code AS code,name,'saving' AS kind,interest_rate,status FROM saving WHERE issuer=? "
            "UNION ALL SELECT product_code,name,'loan',interest_rate,status FROM liability WHERE issuer=? ORDER BY 3,1",
            (row["issuer"], row["issuer"]))]
        return {**_bank_summary(connection, row), "settings": {key: row[key] for key in BANK_SETTINGS},
                "productsList": products}


def admin_bank_create(actor, payload):
    issuer = str(payload.get("issuer", "")).strip()
    nationality = str(payload.get("nationality", "")).strip()
    if not issuer or len(issuer) > 60 or not nationality:
        raise ValueError("은행 이름과 국가를 입력해주세요.")
    with connect_db(timeout=5) as connection:
        if not connection.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (nationality,)).fetchone():
            raise ValueError("등록되지 않은 국가입니다.")
        if connection.execute("SELECT 1 FROM financial_institutions WHERE issuer=?", (issuer,)).fetchone():
            raise ValueError("같은 이름의 은행이 이미 있습니다.")
        capital = native_value(payload.get("equityCapital") or 1_000_000)
        if not capital > 0:
            raise ValueError("자본금은 0보다 커야 합니다.")
        bank_id = str(payload.get("id") or "").strip().upper()
        if bank_id and not re.fullmatch(r"[A-Z0-9_-]{2,20}", bank_id):
            raise ValueError("은행 ID는 영문 대문자·숫자·-·_ 2~20자로 입력해주세요.")
        if bank_id and connection.execute(
                "SELECT 1 FROM financial_institutions WHERE institution_id=?", (bank_id,)).fetchone():
            raise ValueError("이미 쓰는 은행 ID입니다.")
        # Left blank, the insert trigger fills a random BNK + six-digit ID.
        connection.execute("INSERT INTO financial_institutions(issuer,nationality,equity_capital,institution_id) VALUES (?,?,?,?)",
                           (issuer, nationality, capital, bank_id or None))
        bank_id = connection.execute("SELECT institution_id FROM financial_institutions WHERE issuer=?", (issuer,)).fetchone()[0]
        from managers.bank_companies import ensure
        company_code = ensure(LegacyNames(connection), issuer)
        _audit_db(connection, actor, "bank.create", {"id": bank_id, "issuer": issuer, "nationality": nationality,
                                                     "equityCapital": capital, "companyCode": company_code})
    _read_cache.invalidate()
    company = f" · 법인 {company_code}와 법인 계정도 함께 만들었습니다" if company_code else ""
    return f"{issuer} 은행을 만들었습니다. 고유 ID {bank_id}{company}"


def admin_bank_update(actor, bank_id, payload):
    changes = {key: payload[key] for key in BANK_SETTINGS if key in payload and payload[key] not in (None, "")}
    if not changes:
        raise ValueError("변경된 설정이 없습니다.")
    for key, value in changes.items():
        if key != "status":
            try:
                changes[key] = native_value(value) if any(word in key for word in ("capital","limit","amount","balance","reserve")) else float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 값은 숫자여야 합니다.") from None
    with connect_db(timeout=5) as connection:
        row = _bank_row(connection, bank_id)
        try:
            connection.execute(
                f"UPDATE financial_institutions SET {','.join(f'{key}=?' for key in changes)} WHERE institution_id=?",
                (*changes.values(), str(bank_id)))
        except sqlite3.IntegrityError:
            raise ValueError("허용 범위를 벗어난 값이 있습니다.") from None
        _audit_db(connection, actor, "bank.update", {"id": bank_id, "issuer": row["issuer"], "before": {key: row[key] for key in changes}, "after": changes})
    return f"{row['issuer']} 은행 설정을 저장했습니다."


def admin_companies():
    from functions import user_actions
    from managers.company_account_manager import CompanyAccounts
    with connect_readonly() as connection:
        companies = CompanyAccounts.summary(connection)
    commands = [item for item in user_actions.catalog() if not item["job"]]
    return {"companies": companies, "commands": commands}


def admin_company_detail(code):
    from functions import company_actions
    bot = domain().bot
    account = company_actions.company_account(bot, code)
    info = bot.user_mgr.get_total_assets(account)
    with connect_db(timeout=2) as connection:
        ensure_auth_table(connection)
        login = connection.execute("SELECT updated_epoch FROM web_auth_credentials WHERE user_id=?", (account,)).fetchone()
        shareholders = _shareholders(connection, code)
    return {"account": account, "webLogin": bool(login), "loginId": code, "shareholders": shareholders, "assets": json.loads(json.dumps(info, default=lambda value: dict(value) if hasattr(value, "keys") else str(value)))}


def admin_identity(actor):
    """Which treasury/company the administrator's national commands act for ($login)."""
    from functions.admin_prefix import national_identity
    bot = domain().bot
    account, name = national_identity(bot, actor)
    with connect_readonly() as connection:
        countries = [row[0] for row in connection.execute("SELECT nationality FROM country_currencies ORDER BY nationality")]
        companies = [{"code": row[0], "name": row[1]} for row in connection.execute(
            "SELECT company_code,name FROM company WHERE status='active' AND COALESCE(share_class,'common')='common' "
            "ORDER BY company_code")]
    return {"identity": {"account": account, "name": name}, "countries": countries, "companies": companies}


def admin_login_as(actor, payload):
    """$login on the web: the account the administrator signs in as — a country's own account
    (its ID is the country name) or a company's capital account."""
    from managers.company_account_manager import CompanyAccounts
    target = str(payload.get("target") or "").strip()
    with connect_db(timeout=5) as connection:
        from managers.currency_manager import CurrencyManager
        nationality = CurrencyManager.nationality_of(connection, target)
        if connection.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (nationality,)).fetchone():
            account = CurrencyManager.treasury_id(connection, nationality)
        elif connection.execute("SELECT 1 FROM company WHERE company_code=? AND status='active'", (target,)).fetchone():
            account = CompanyAccounts.account_id(connection, target)
        else:
            raise ValueError("등록된 국가 ID 또는 법인 ID를 입력해주세요.")
    user = identity_for(account, require_credential=False)
    if not user or user["role"] not in DELEGABLE_ROLES:
        raise ValueError("이 계정으로는 로그인할 수 없습니다. 국가 계정 또는 법인 계정만 가능합니다.")
    audit_admin_access(actor, "admin.login_as", {"target": target, "account": account})
    return {"userId": account, "nickname": user["nickname"], "role": user["role"]}


def admin_identity_action(actor, payload):
    from functions.admin_prefix import national_login, national_logout
    target = str(payload.get("target") or "").strip()
    return national_login(domain().bot, actor, target) if target else national_logout(domain().bot, actor)


def admin_company_financial(code, period=None):
    from managers.financial_manager import FinancialManager
    from managers.company_account_manager import CompanyAccounts
    manager = FinancialManager(domain().db)
    return {"report": CompanyAccounts.financial_report(domain().db, code, period or None), "periods": manager.periods(code)}


def own_company_financial(user, period=None):
    """A company account's own statements (read-only; editing stays with administrators)."""
    if user["role"] != "company":
        return None
    with connect_readonly() as connection:
        row = connection.execute("SELECT company_code FROM company_accounts WHERE user_id=?", (user["userId"],)).fetchone()
    return {"company": row[0], **admin_company_financial(row[0], period)} if row else None


def user_history(user_id, limit=150):
    """거래내역: transfers sent and received, basic income and currency exchanges, newest first.
    Amounts are stored in millionths of a currency unit."""
    owner = trading_user_id(user_id)
    events = []
    with connect_readonly() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        name = lambda alias: f"COALESCE((SELECT nickname FROM users WHERE user_id={alias}),{alias})"
        if "currency_transfer_transactions" in tables:
            for row in connection.execute(
                    f"SELECT t.*,{name('t.sender_id')} AS sender,{name('t.recipient_id')} AS recipient FROM currency_transfer_transactions t "
                    "WHERE t.sender_id=? OR t.recipient_id=? ORDER BY t.recorded_epoch DESC LIMIT ?", (owner, owner, limit)):
                outgoing = row["sender_id"] == owner
                events.append({"kind": "transfer_out" if outgoing else "transfer_in", "epoch": row["recorded_epoch"],
                               "currency": row["source_currency"] if outgoing else row["target_currency"],
                               "amount": (-row["source_units"] if outgoing else row["target_units"]) / 1_000_000,
                               "counterpart": row["recipient"] if outgoing else row["sender"],
                               "counterpartUserId": row["recipient_id"] if outgoing else row["sender_id"],
                               "account": row["source_account"] if outgoing else row["target_account"]})
        if "basic_income_payments" in tables:
            for row in connection.execute("SELECT * FROM basic_income_payments WHERE user_id=? ORDER BY recorded_epoch DESC LIMIT ?", (owner, limit)):
                events.append({"kind": "basic_income", "epoch": row["recorded_epoch"], "currency": row["target_currency"],
                               "amount": row["target_units"] / 1_000_000, "counterpart": "국고", "account": None})
        if "currency_transactions" in tables:
            for row in connection.execute("SELECT * FROM currency_transactions WHERE user_id=? ORDER BY recorded_epoch DESC LIMIT ?", (owner, limit)):
                events.append({"kind": "exchange", "epoch": row["recorded_epoch"], "currency": row["target_currency"],
                               "amount": row["target_units"] / 1_000_000, "counterpart": f"{row['source_currency']} {row['source_units'] / 1_000_000:g} → {row['target_currency']}", "account": None})
    events.sort(key=lambda item: item["epoch"], reverse=True)
    return events[:limit]


def admin_company_journal(code, period=None):
    from managers.financial_manager import FinancialManager
    return FinancialManager(domain().db).journal(code, period or None)


def admin_company_journal_action(actor, code, payload):
    """Adjusting entries, reversals, operating profile and month close — the Discord accounting forms."""
    from managers.financial_manager import FinancialManager
    manager = FinancialManager(domain().db)
    action = str(payload.get("action") or "")
    note = str(payload.get("description") or "").strip()
    if action == "entry":
        if not note:
            raise ValueError("거래 설명을 입력해주세요.")
        message = manager.manual(actor, code, note, payload.get("lines"), str(payload.get("flow") or "none"))
    elif action == "reverse":
        if not note:
            raise ValueError("정정 사유를 입력해주세요.")
        message = manager.manual(actor, code, note, [], reversal=int(payload.get("entryId")))
    elif action == "configure":
        message = manager.configure(actor, code, payload.get("config"))
    elif action == "close":
        message = manager.close_period(actor, code, str(payload.get("period") or ""))
    else:
        raise ValueError("invalid_action")
    audit_admin_access(actor, f"company_financial.{action}", {"company": code, **{k: v for k, v in payload.items() if k != "action"}})
    return message


def admin_company_share_history(code):
    from managers.share_restructuring_manager import ShareRestructuringManager
    return {"resolutions": ShareRestructuringManager(domain().bot).history(code)}


def admin_company_share_action(actor, code, payload):
    from managers.share_restructuring_manager import ShareRestructuringManager
    from managers.corporate_actions_manager import CorporateActionsManager
    if not isinstance(payload, dict):
        raise ValueError("요청 내용을 확인해주세요.")
    bot = domain().bot
    if payload.get("action") == "retire_issuance":
        if set(payload) != {"action"}:
            raise ValueError("주식 종류 폐지 요청을 확인해주세요.")
        success, message = CorporateActionsManager(bot).retire_share_class(actor, code)
        audit_admin_access(actor, "organization.share_class_retire", {
            "legacy_product_code": str(code), "success": bool(success),
        })
        _read_cache.invalidate()
        return str(message) if success or str(message).startswith("❌") else "❌ " + str(message)
    if payload.get("action") == "issue_shares":
        if set(payload) - {"action", "mode", "quantity", "issuePrice"} or not {"mode", "quantity"} <= set(payload):
            raise ValueError("신주 발행 유형과 수량을 확인해주세요.")
        mode = {"paid": "유상", "bonus": "무상"}.get(str(payload["mode"]))
        if not mode or (mode == "유상" and "issuePrice" not in payload) or (mode == "무상" and "issuePrice" in payload):
            raise ValueError("유상증자는 발행가를, 무상증자는 발행가 없이 입력해주세요.")
        success, message = CorporateActionsManager(bot).increase_capital(
            actor, mode, code, payload["quantity"], payload.get("issuePrice"))
        audit_admin_access(actor, "organization.share_class_issue", {
            "legacy_product_code": str(code), "mode": mode,
            "quantity": payload["quantity"], "success": bool(success),
        })
        _read_cache.invalidate()
        return str(message) if success or str(message).startswith("❌") else "❌ " + str(message)
    if payload.get("action") == "record_resolution":
        if set(payload) != {"action", "ratio", "decision", "meetingReference", "articlesReference", "parValue"}:
            raise ValueError("의결 결과·문서·현재 액면가를 모두 입력해주세요.")
        key = ShareRestructuringManager(bot).record(actor, code, payload["ratio"], payload["decision"],
            payload["meetingReference"], payload["articlesReference"], payload["parValue"])
        return f"주총 의결 #{key}을 등록했습니다. 주식 수는 변경되지 않았습니다. 집행은 관리자가 별도로 실행합니다."
    if payload.get("action") == "execute":
        if set(payload) - {"action", "mode", "ratio", "resolutionId", "oldShares", "newShares"} or "mode" not in payload:
            raise ValueError("집행 방식과 비율을 확인해주세요.")
        mode = payload["mode"]
        if mode == "ratio_split":
            if not {"oldShares", "newShares"} <= set(payload):
                raise ValueError("기존 주식 수와 새 주식 수를 입력해주세요.")
        elif "ratio" not in payload or {"oldShares", "newShares"} & set(payload):
            raise ValueError("집행 방식에 맞는 비율을 입력해주세요.")
        resolution = payload.get("resolutionId")
        if resolution is not None:
            resolution = CorporateActionsManager._integer(resolution, "의결 번호", 1)
        success, message = CorporateActionsManager(bot).adjust_face_value(actor,
            "병합" if mode == "reverse_split" else "분할", code, payload.get("ratio", 2), mode=mode, resolution_id=resolution,
            old_shares=payload.get("oldShares"), new_shares=payload.get("newShares"))
        _read_cache.invalidate()
        return str(message) if success or str(message).startswith("❌") else "❌ " + str(message)
    raise ValueError("의결 등록 또는 관리자 집행을 선택해주세요.")


def admin_company_command(actor, code, payload):
    from decimal import InvalidOperation
    from functions import company_actions, user_actions
    command = str(payload.get("command", ""))
    values = user_actions.parse_values(command, payload.get("values") or {})
    try:
        success, message, _ = company_actions.run(domain().bot, actor, code, command, values)
    except (KeyError, InvalidOperation, sqlite3.IntegrityError, RuntimeError) as exc:
        raise ValueError(str(exc) or "명령어를 처리하지 못했습니다.") from exc
    with connect_readonly() as connection:
        message = str(message).replace("기준 통화", _configured_base_currency(connection))
    return message if success or str(message).startswith("❌") else "❌ " + str(message)


def run_user_command(user, payload):
    """HTS form for a Discord user command, run as the user's trading identity."""
    from functions import user_actions
    command = str(payload.get("command", ""))
    values = user_actions.parse_values(command, payload.get("values") or {})
    if command == "파산":
        job = queue_bot_job("relief_request", user["userId"], {"type": "파산", "name": user["nickname"]})
        return True, f"⏳ 파산 신청을 Discord 봇에 전달했습니다 (#{job}). 봇이 관리자에게 DM으로 알립니다.", None
    if command == "주총소집":
        job = queue_bot_job("user_shareholder_meeting", user["userId"], {"code": values["code"], "place": values["place"]})
        return True, f"⏳ 주주총회 소집을 Discord 봇에 전달했습니다 (#{job}). 봇이 주주들에게 DM을 보냅니다.", None
    from decimal import InvalidOperation
    trader = trading_user_id(user["userId"])
    try:
        success, message, data = user_actions.execute(domain().bot, trader, command, values)
    except (KeyError, InvalidOperation, sqlite3.IntegrityError) as exc:
        raise ValueError(str(exc) or "명령어를 처리하지 못했습니다.") from exc
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    _read_cache.invalidate(("portfolio", trader), ("orders", trader))
    with connect_readonly() as connection:
        message = str(message).replace("기준 통화", _configured_base_currency(connection)).replace("기준통화", _configured_base_currency(connection))
    return success, message, data


def user_lookups(user):
    """Choices for the HTS command forms: products, the user's accounts and loans, countries."""
    trader = trading_user_id(user["userId"])
    with connect_readonly() as connection:
        def rows(sql, params=()):
            return [dict(row) for row in connection.execute(sql, params)] if _table_exists(connection, sql.split(" FROM ")[1].split()[0]) else []
        return {
            "savingProducts": rows("SELECT product_code AS code,name,interest_rate,deposit_amount,maturity_days,quote_currency,product_kind FROM saving WHERE status='active' ORDER BY product_kind,name"),
            "loanProducts": rows("SELECT product_code AS code,name,interest_rate,loan_limit,maturity_days,min_credit_score,quote_currency FROM liability WHERE status='active' ORDER BY name"),
            "accounts": [_masked(row) for row in rows("SELECT account_number,product_code,account_type,quote_currency,balance,balance_units FROM accounts WHERE user_id=? ORDER BY account_number", (trader,))],
            "loans": rows("SELECT loan_id,product_code,outstanding_balance,quote_currency,maturity_epoch,status FROM loans WHERE user_id=? AND outstanding_balance>0 ORDER BY loan_id", (trader,)),
            "futures": rows("SELECT product_code AS code,name FROM investment WHERE product_type='stock_future' AND status='active' ORDER BY name"),
            "countries": [row["nationality"] for row in rows("SELECT nationality FROM country_currencies ORDER BY nationality")],
        }


def bot_jobs(limit=20):
    with connect_db(timeout=2) as connection:
        ensure_bot_jobs_table(connection)
        return [dict(row) for row in connection.execute(
            "SELECT * FROM web_bot_jobs ORDER BY job_id DESC LIMIT ?", (limit,))]


def run_admin_command(actor, payload):
    from managers.admin_commands_manager import AdminCommandsManager, COMMANDS
    command = str(payload.get("command", "")).strip().removeprefix("$")
    arguments = payload.get("arguments", "")
    if command not in COMMANDS:
        raise ValueError("알 수 없는 관리자 명령어입니다.")
    if not isinstance(arguments, str) or len(arguments) > 8000:
        raise ValueError("명령어 인자를 확인해주세요.")
    from decimal import InvalidOperation
    try:
        result = AdminCommandsManager(domain().bot).execute(actor, command, arguments)
    except (KeyError, InvalidOperation, sqlite3.IntegrityError) as exc:
        raise ValueError(str(exc) or "명령어를 처리하지 못했습니다.") from exc
    _read_cache.invalidate()
    with connect_readonly() as connection:
        currency = _configured_base_currency(connection)
    return str(result or "완료했습니다.").replace("기준 통화", currency).replace("기준통화", currency)


def update_admin_user(actor, user_id, changes):
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object")
    return domain().admin_control.user(actor, user_id, changes)


def create_admin_event(actor, payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid_event")
    return domain().admin_manager.create_market_event(
        actor,
        payload.get("eventType"),
        payload.get("severity"),
        payload.get("assetCode") or None,
        payload.get("durationSeconds", 3600),
        target_price=payload.get("targetPrice"),
        target_percent=payload.get("targetPercent"),
    )


def request_json(handler, limit=32_768):
    try:
        size = int(handler.headers.get("content-length", "0"))
        if size < 0 or size > limit:
            raise ValueError("request_too_large")
        body = handler.rfile.read(size) if size else b"{}"
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    return value


def internal_user_id(handler):
    return unquote(handler.headers.get("x-inazina-user-id", ""))


class Handler(BaseHTTPRequestHandler):
    server_version = "InazinaInternal/1.0"

    def handle_one_request(self):
        started = time.perf_counter()
        try:
            return super().handle_one_request()
        finally:
            # The delegation header belongs to one request; keep-alive reuses the thread.
            _DELEGATION.by = ""
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed >= 100:
                import logging
                logging.getLogger("inazina.performance").warning(
                    "http_request %.1f ms", elapsed
                )

    def do_GET(self):
        _DELEGATION.by = unquote(self.headers.get("x-inazina-delegated-by", ""))
        if not INTERNAL_TOKEN or not hmac.compare_digest(
            self.headers.get("x-inazina-internal-token", ""), INTERNAL_TOKEN
        ):
            # The service root is a friendly landing page. API URLs require a
            # one-shot password; server-to-server requests keep using the token.
            browser = "text/html" in self.headers.get("accept", "")
            path = urlsplit(self.path).path
            if browser and path in {"", "/", "/internal", "/internal/"}:
                self.send_landing(path)
                return
            if browser and not getattr(self, "browser_access_granted", False):
                if path.startswith("/internal/"):
                    self.send_browser_access_page(self.path)
                else:
                    self.send_landing(path)
                return
            if not browser and not getattr(self, "browser_access_granted", False):
                self.send_json(401, {"error": "internal_authentication_required"})
                return

        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/internal/health":
                with connect_readonly() as connection:
                    connection.execute("SELECT 1").fetchone()
                self.send_json(200, {"status": "ok", "service": "inazina-python-data"})
            elif path == "/internal/auth/countries":
                self.send_json(200, {"countries": auth_countries()})
            elif path == "/internal/search-indicators":
                from web_api.search_indicators import search_indicators
                with connect_readonly() as connection:
                    self.send_json(200, search_indicators(connection, parse_qs(parsed.query).get("search", [""])[0]))
            elif path == "/internal/me":
                user_id = internal_user_id(self)
                user = identity(user_id)
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"user": user})
            elif path == "/internal/me/ownership-graph":
                user_id = internal_user_id(self)
                if not identity(user_id):
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    from managers.organization_manager import OrganizationManager
                    with connect_db(timeout=5) as connection:
                        graph = OrganizationManager.user_ownership_graph(connection, user_id)
                    self.send_json(200, {"graph": graph})
            elif path in {"/internal/board/posts", "/internal/board/boards"} or path.startswith(("/internal/board/posts/", "/internal/board/files/")):
                viewer = identity(internal_user_id(self)) if internal_user_id(self) else None
                try:
                    self.send_json(200, board_get(path, parse_qs(parsed.query), viewer))
                except LookupError:
                    self.send_json(404, {"error": "not_found"})
            elif path == "/internal/admin/documents" or path.startswith("/internal/admin/documents/"):
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                elif path == "/internal/admin/documents":
                    _apply_due_documents()
                    self.send_json(200, {"documents": documents.list_documents()})
                else:
                    self.send_json(200, documents.read_document(path.removeprefix("/internal/admin/documents/")))
            elif path == "/internal/admin/board":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    query = parse_qs(parsed.query)
                    with board_db() as connection:
                        if query.get("view", [""])[0] == "search":
                            self.send_json(200, board.admin_search_terms(connection))
                            return
                        self.send_json(200, board.admin_list(connection, query.get("status", ["all"])[0],
                                                             query.get("reported", ["0"])[0] == "1", query.get("search", [""])[0]))
            elif path == "/internal/hts/account":
                user_id = internal_user_id(self)
                if not identity(user_id):
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"htsId": get_hts_login_id(user_id)})
            elif path == "/internal/hts/commands":
                from functions.user_actions import catalog
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"commands": catalog(), "lookups": user_lookups(user)})
            elif path == "/internal/market/instruments":
                self.send_json(200, {"instruments": instruments()})
            elif path == "/internal/market/portfolio":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    trader = trading_user_id(user["userId"])
                    result = _read_cache.get(("portfolio", trader), lambda: portfolio(trader))
                    self.send_json(200, {"portfolio": result})
            elif path == "/internal/me/dr/movements":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"movements": user_dr_movements(user["userId"])})
            elif path == "/internal/me/dr/conversions":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    instructions = DepositaryReceiptManager(domain().bot.db).list_custody_instructions(
                        user_id=user["userId"], limit=100,
                    )
                    self.send_json(200, {"instructions": instructions})
            elif path == "/internal/me/dr/voting-delegations":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {
                        "delegations": user_dr_voting_delegations(user["userId"]),
                    })
            elif path == "/internal/me/dr/proxy-voting-delegations":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {
                        "delegations": user_dr_received_voting_delegations(user["userId"]),
                    })
            elif path == "/internal/me/dr/voting-agenda":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    agenda = DepositaryReceiptManager(domain().bot.db).list_user_voting_agenda(
                        user["userId"],
                    )
                    self.send_json(200, {"agenda": agenda})
            elif path == "/internal/info/panel":
                user = identity(internal_user_id(self))
                query = parse_qs(parsed.query)
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, info_panel(trading_user_id(user["userId"]), query.get("category", ["main"])[0], query.get("page", ["0"])[0]))
            elif path == "/internal/me/company-financial":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"financial": own_company_financial(user, parse_qs(parsed.query).get("period", [None])[0])})
            elif path == "/internal/me/history":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"events": user_history(user["userId"])})
            elif path == "/internal/me/dividend-payments":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"payments": user_dividend_payments(user["userId"])})
            elif path == "/internal/me/tax-residency":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "user":
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, user_tax_residency_profile(user["userId"]))
            elif path == "/internal/me/credit":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, user_credit_report(user["userId"]) or {})
            elif path == "/internal/me/loan-schedules":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, user_loan_schedules(user["userId"]))
            elif path.startswith('/internal/admin/funds/'):
                user = identity(internal_user_id(self))
                if not user or user['role'] != 'admin':
                    self.send_json(403, {'error': 'admin_required'})
                else:
                    self.send_json(200, manage_fund(user['userId'], unquote(path.removeprefix('/internal/admin/funds/'))))
            elif path == "/internal/admin/overview":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, admin_overview())
            elif path == "/internal/admin/instruments":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    include_inactive = parse_qs(parsed.query).get("includeInactive", ["0"])[0] == "1"
                    self.send_json(200, {"instruments": instruments(include_inactive)})
            elif path == "/internal/admin/users":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    query = parse_qs(parsed.query)
                    kind = "company" if query.get("kind", [""])[0] == "company" else "people"
                    search = query.get("search", [""])[0]
                    users = admin_users(search, kind)
                    audit_admin_access(user["userId"], "admin_users.read", {
                        "kind": kind, "filtered": bool(search),
                        "result_count": len(users),
                    })
                    self.send_json(200, {"users": users})
            elif path == "/internal/admin/events":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, {"events": admin_events(), "flows": admin_market_flows()})
            elif path == "/internal/admin/login-history":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    target = parse_qs(parsed.query).get("user", [""])[0].strip() or None
                    with connect_db(timeout=5) as connection:
                        self.send_json(200, {"events": admin_logins.history(connection, target),
                                             "retentionDays": admin_logins.RETENTION_SECONDS // 86400})
            elif path == "/internal/admin/audit":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, {"entries": admin_audit()})
            elif path == "/internal/admin/bank-journal":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    query = parse_qs(parsed.query)
                    issuer = query.get("issuer", [""])[0].strip()
                    currency = query.get("currency", [None])[0]
                    currency = currency.strip().upper() if currency else None
                    raw_since = query.get("since", [None])[0]
                    if raw_since:
                        from managers.bank_journal import BankJournal
                        try:
                            since = BankJournal.validate_epoch(raw_since)
                        except ValueError as exc:
                            raise ValueError("invalid_since_epoch") from exc
                    else:
                        since = None
                    if not issuer or len(issuer) > 100 or (currency and len(currency) > 12):
                        raise ValueError("invalid_bank_journal_filter")
                    report = admin_bank_journal_report(issuer, currency, since)
                    audit_admin_access(user["userId"], "bank_journal.read", {
                        "issuer": issuer, "currency": currency, "since": since,
                        "trial_balance_accounts": len(report.get("trialBalance", [])),
                        "insurance_claims": len(report.get("depositInsuranceClaims", [])),
                    })
                    self.send_json(200, report)
            elif path == "/internal/admin/dr/movements":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    raw_limit = parse_qs(parsed.query).get("limit", ["100"])[0]
                    try:
                        limit = int(raw_limit)
                    except (TypeError, ValueError):
                        raise ValueError("invalid_dr_movement_limit") from None
                    movements = admin_dr_movements(limit)
                    audit_admin_access(user["userId"], "dr_movements.read", {
                        "limit": max(1, min(limit, 200)),
                        "result_count": len(movements.get("movements", [])),
                    })
                    self.send_json(200, movements)
            elif path == "/internal/admin/dr/voting-meetings":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    raw_limit = parse_qs(parsed.query).get("limit", ["100"])[0]
                    try:
                        limit = max(1, min(int(raw_limit), 100))
                    except (TypeError, ValueError):
                        raise ValueError("invalid_dr_voting_meeting_limit") from None
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    meetings = DepositaryReceiptManager(domain().bot.db).list_voting_meetings(limit)
                    audit_admin_access(user["userId"], "dr_voting_meetings.list", {
                        "limit": limit, "result_count": len(meetings),
                    })
                    self.send_json(200, {"meetings": meetings})
            elif path.startswith("/internal/admin/dr/voting-meetings/"):
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    raw_id = path.rsplit("/", 1)[-1]
                    if not raw_id.isdigit() or int(raw_id) <= 0:
                        raise ValueError("invalid_dr_voting_meeting_id")
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    meeting = DepositaryReceiptManager(domain().bot.db).get_voting_record_snapshot(int(raw_id))
                    if meeting is None:
                        self.send_json(404, {"error": "dr_voting_meeting_not_found"})
                    else:
                        audit_admin_access(user["userId"], "dr_voting_snapshot.read", {
                            "meeting_id": int(raw_id), "voter_count": len(meeting["voters"]),
                        })
                        self.send_json(200, {"meeting": meeting})
            elif path == "/internal/admin/dr/voting-results":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    query = parse_qs(parsed.query)
                    try:
                        meeting_id = int(query.get("meetingId", [""])[0])
                        raw_item = query.get("agendaItemId", [""])[0]
                        item_id = int(raw_item) if raw_item else None
                    except (TypeError, ValueError):
                        raise ValueError("invalid_dr_voting_results_filter") from None
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    manager = DepositaryReceiptManager(domain().bot.db)
                    results = manager.get_voting_results(meeting_id, item_id)
                    audit_admin_access(user["userId"], "dr_voting_results.read", {
                        "meeting_id": meeting_id, "agenda_item_id": item_id,
                        "agenda_count": len(results),
                    })
                    self.send_json(200, {"results": results})
            elif path == "/internal/admin/dr/custody-events":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    raw_limit = parse_qs(parsed.query).get("limit", ["100"])[0]
                    try:
                        limit = int(raw_limit)
                    except (TypeError, ValueError):
                        raise ValueError("invalid_dr_custody_event_limit") from None
                    events = admin_dr_custody_events(limit)
                    audit_admin_access(user["userId"], "dr_custody_events.read", {
                        "limit": max(1, min(limit, 500)),
                        "result_count": len(events.get("events", [])),
                    })
                    self.send_json(200, events)
            elif path == "/internal/admin/dr/custody-instructions":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    instructions = DepositaryReceiptManager(domain().bot.db).list_custody_instructions(limit=200)
                    audit_admin_access(user["userId"], "dr_custody_instructions.read", {
                        "result_count": len(instructions),
                    })
                    self.send_json(200, {"instructions": instructions})
            elif path == "/internal/admin/dr/custody-reconciliation":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    from managers.depositary_receipt_manager import DepositaryReceiptManager
                    report = DepositaryReceiptManager(domain().bot.db).reconcile_custody_ledger()
                    audit_admin_access(user["userId"], "dr_custody_reconciliation.read", {
                        "program_count": report["programCount"],
                        "discrepancy_count": report["discrepancyCount"],
                    })
                    self.send_json(200, report)
            elif path == "/internal/admin/dr/dividend-tax":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    from managers.dr_dividend_tax_manager import DrDividendTaxManager
                    with domain().bot.db.atomic() as connection:
                        report = DrDividendTaxManager.reconciliation(connection)
                        report["rules"] = DrDividendTaxManager.list_rules(connection)
                    audit_admin_access(user["userId"], "dr_dividend_tax.read", {
                        "rule_count": len(report["rules"]),
                        "payment_count": len(report["payments"]),
                    })
                    self.send_json(200, report)
            elif path == "/internal/admin/dr/monitor":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    query = parse_qs(parsed.query)
                    raw_limit = query.get("limit", ["100"])[0]
                    try:
                        limit = int(raw_limit)
                    except (TypeError, ValueError):
                        raise ValueError("invalid_dr_monitor_limit") from None
                    dr_code = query.get("drCode", [None])[0]
                    self.send_json(200, admin_dr_deviation_history(limit, dr_code))
            elif path == "/internal/admin/compliance":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    nationality = parse_qs(parsed.query).get("nationality", [None])[0]
                    report = admin_compliance_overview(nationality)
                    audit_admin_access(user["userId"], "compliance.read", {
                        "nationality": nationality,
                        "kyc_profiles": len(report.get("kycProfiles", [])),
                        "aml_alerts": len(report.get("amlAlerts", [])),
                        "ecl_assessments": len(report.get("eclAssessments", [])),
                    })
                    self.send_json(200, report)
            elif path == "/internal/admin/commands":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, admin_command_catalog())
            elif path.startswith("/internal/admin/") and path.split("/")[3] in {
                    "products", "countries", "exchanges", "help", "bot-jobs", "user", "companies", "warnings", "feedback",
                    "naturalization", "banks", "organizations"}:
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                parts = [unquote(part) for part in path.split("/")[3:]]
                query = parse_qs(parsed.query)
                if parts == ["products", "catalog"]:
                    self.send_json(200, product_catalog())
                elif parts == ["products"]:
                    self.send_json(200, {"products": product_list(query.get("group", ["market"])[0])})
                elif len(parts) == 2 and parts[0] == "products":
                    self.send_json(200, product_detail(parts[1]))
                elif parts == ["countries"]:
                    self.send_json(200, {"countries": admin_countries()})
                elif parts == ["exchanges"]:
                    self.send_json(200, admin_exchanges())
                elif parts == ["help"]:
                    self.send_json(200, {"pages": admin_help_pages()})
                elif parts == ["bot-jobs"]:
                    self.send_json(200, {"jobs": bot_jobs()})
                elif len(parts) == 2 and parts[0] == "user":
                    detail = admin_user_detail(parts[1])
                    audit_admin_access(user["userId"], "admin_user.read", {
                        "subject_user_id": parts[1], "found": detail is not None,
                    })
                    self.send_json(200, detail)
                elif parts == ["companies"]:
                    self.send_json(200, admin_companies())
                elif parts == ["organizations"]:
                    from web_api.organizations import directory
                    with connect_db(timeout=5) as connection:
                        result = directory(connection)
                    for change in result.get("sizeChanges", []):
                        audit_admin_access(user["userId"], "organization.size_auto_change", change)
                    self.send_json(200, result)
                elif len(parts) == 2 and parts[0] == "organizations":
                    from web_api.organizations import directory
                    query = parse_qs(parsed.query)
                    min_percent = 0 if query.get("includeMinor", ["0"])[0] == "1" else 1
                    with connect_db(timeout=5) as connection:
                        result = directory(connection, parts[1], min_percent=min_percent)
                    for change in result.get("sizeChanges", []):
                        audit_admin_access(user["userId"], "organization.size_auto_change", change)
                    self.send_json(200, result)
                elif parts == ["companies", "identity"]:
                    self.send_json(200, admin_identity(user["userId"]))
                elif len(parts) == 3 and parts[0] == "companies" and parts[2] == "financial":
                    self.send_json(200, admin_company_financial(parts[1], query.get("period", [None])[0]))
                elif len(parts) == 3 and parts[0] == "companies" and parts[2] == "share-actions":
                    self.send_json(200, admin_company_share_history(parts[1]))
                elif len(parts) == 3 and parts[0] == "companies" and parts[2] == "journal":
                    self.send_json(200, admin_company_journal(parts[1], query.get("period", [None])[0]))
                elif parts == ["warnings"]:
                    self.send_json(200, admin_warnings())
                elif parts == ["feedback"]:
                    self.send_json(200, admin_feedback(query.get("status", [None])[0]))
                elif parts == ["naturalization"]:
                    self.send_json(200, admin_naturalization())
                elif parts == ["banks"]:
                    self.send_json(200, admin_banks())
                elif len(parts) == 2 and parts[0] == "banks":
                    self.send_json(200, admin_bank_detail(parts[1]))
                elif len(parts) == 2 and parts[0] == "companies":
                    self.send_json(200, admin_company_detail(parts[1]))
                else:
                    self.send_json(404, {"error": "not_found"})
            elif path == "/internal/admin/db/tables" or path.startswith("/internal/admin/db/tables/"):
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                elif path == "/internal/admin/db/tables":
                    self.send_json(200, {"tables": db_tables()})
                else:
                    query = parse_qs(parsed.query)
                    offset = query.get("offset", ["0"])[0]
                    if not offset.isdecimal():
                        raise ValueError("invalid_offset")
                    table = unquote(path.removeprefix("/internal/admin/db/tables/"))
                    self.send_json(200, db_rows(table, query.get("search", [""])[0], int(offset)))
            elif path.startswith("/internal/market/drawings/"):
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    code = unquote(path.removeprefix("/internal/market/drawings/"))
                    self.send_json(200, {"drawings": get_drawings(user["userId"], code)})
            elif path == "/internal/market/orders":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    manager = domain().order_book
                    trader = trading_user_id(user["userId"])
                    orders = _read_cache.get(
                        ("orders", trader),
                        lambda: [dict(row) for row in manager.get_user_orders(trader, limit=50)],
                    )
                    self.send_json(200, {"orders": orders})
            elif path.startswith("/internal/market/book/"):
                code = unquote(path.removeprefix("/internal/market/book/"))
                result = book(code)
                if result is None:
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    self.send_json(200, result)
            elif path.startswith("/internal/market/chart/"):
                code = unquote(path.removeprefix("/internal/market/chart/"))
                query = parse_qs(parsed.query)
                range_key = query.get("range", ["1d"])[0]
                raw_interval = query.get("interval", [""])[0]
                if raw_interval and not raw_interval.isdecimal():
                    raise ValueError("unsupported_candle_interval")
                pick = lambda key: [item for item in query.get(key, [""])[0].split(",") if item][:24]
                result = chart(code, range_key, int(raw_interval) if raw_interval else None, pick("upper"), pick("lower"),
                               query.get("catalog", [""])[0] == "1")
                if result is None:
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    self.send_json(200, result)
            else:
                self.send_json(404, {"error": "not_found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except (sqlite3.Error, RuntimeError) as exc:
            # Keep the real cause in the console and the response, so a broken query is not mistaken for a down DB.
            logging.getLogger("inazina.web").exception("GET %s failed", self.path)
            self.send_json(503, {"error": "database_unavailable", "message": f"데이터베이스를 읽지 못했습니다: {exc}"})

    def do_POST(self):
        _DELEGATION.by = unquote(self.headers.get("x-inazina-delegated-by", ""))
        if urlsplit(self.path).path == "/__browser-access":
            self.browser_access_login()
            return
        if not INTERNAL_TOKEN or not hmac.compare_digest(
            self.headers.get("x-inazina-internal-token", ""), INTERNAL_TOKEN
        ):
            self.send_json(401, {"error": "internal_authentication_required"})
            return
        try:
            # Whole documents (terms, privacy policy) are larger than other requests.
            limit = 262_144 if urlsplit(self.path).path.startswith("/internal/admin/documents/") else 32_768
            payload = request_json(self, limit)
            if not isinstance(payload, dict):
                raise ValueError("invalid_payload")
            path = urlsplit(self.path).path
            verified_id = internal_user_id(self)
            if path == "/internal/auth/login":
                user_id = str(payload.get("loginId", payload.get("userId", ""))).strip()
                if not user_id or len(user_id) > 80:
                    self.send_json(401, {"error": "invalid_credentials"})
                    return
                user, account_exists = authenticate_login_identifier(user_id, payload.get("password"))
                record_admin_login(user_id, bool(user), payload.get("meta"),
                                   None if user else "account_not_found" if not account_exists else "invalid_credentials")
                if not account_exists:
                    self.send_json(404, {"error": "account_not_found"})
                    return
                if not user:
                    self.send_json(401, {"error": "invalid_credentials"})
                else:
                    self.send_json(200, {"user": user})
                return
            if path == "/internal/auth/register":
                user = save_password(
                    verified_id, payload.get("password"), create=True,
                    nickname=payload.get("nickname", ""), country_id=payload.get("countryId", ""),
                )
                self.send_json(201, {"user": user})
                return
            if path == "/internal/me/tax-residency":
                user = identity(verified_id)
                if not user or user["role"] != "user":
                    self.send_json(403, {"error": "active_user_required"})
                    return
                if set(payload) != {"residency"}:
                    raise ValueError("invalid_tax_residency_payload")
                result = set_user_tax_residency(user["userId"], payload["residency"])
                self.send_json(200, result)
                return
            if path == "/internal/auth/password/reset":
                user = save_password(verified_id, payload.get("password"))
                self.send_json(200, {"user": user})
                return
            if path == "/internal/auth/logout":
                with connect_db(timeout=2) as connection:
                    ensure_auth_table(connection)
                    connection.execute("UPDATE web_auth_credentials SET auth_version=auth_version+1 WHERE user_id=?", (verified_id,))
                self.send_json(200, {"success": True})
                return

            if path == "/internal/site/feedback":
                # Public: the official site's Contact/Report forms (no login).
                self.send_json(201, {"feedback": submit_feedback(payload)})
                return
            if path == "/internal/admin/companies/login":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                self.send_json(201, {"account": create_company_login(user["userId"], payload.get("companyId"), payload.get("password"))})
                return
            if path == "/internal/admin/users/country-account":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                account = create_country_account(
                    user["userId"], payload.get("countryId"), payload.get("password"),
                )
                self.send_json(201, {"account": account})
                return
            if path == "/internal/admin/dr/voting-meetings":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                if set(payload) != {"underlyingCode", "title"}:
                    raise ValueError("invalid_dr_voting_meeting_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                success, result = DepositaryReceiptManager(domain().bot.db).create_voting_record_snapshot(
                    payload["underlyingCode"], payload["title"],
                )
                if success:
                    audit_admin_access(user["userId"], "dr_voting_snapshot.create", {
                        "meeting_id": result["meetingId"],
                        "underlying_code": result["underlyingCode"],
                        "snapshot_rows": result["snapshotRows"],
                    })
                self.send_json(201 if success else 400, {"success": success, "result": result})
                return
            if path == "/internal/admin/dr/dividend-tax/rules":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                from managers.dr_dividend_tax_manager import DrDividendTaxManager
                manager = DrDividendTaxManager
                with domain().bot.db.atomic() as connection:
                    rule_id = manager.create_rule(connection, user["userId"], payload, now=int(time.time()))
                audit_admin_access(user["userId"], "dr_dividend_tax_rule.create", {
                    "rule_id": rule_id,
                    "issuer_nationality": payload.get("issuer_nationality"),
                    "underlying_nationality": payload.get("underlying_nationality"),
                    "effective_from": payload.get("effective_from"),
                })
                self.send_json(201, {"success": True, "ruleId": rule_id})
                return
            if path in {
                "/internal/admin/dr/custody-instructions/settle",
                "/internal/admin/dr/custody-instructions/cancel",
                "/internal/admin/dr/custody-instructions/reject",
            }:
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                expected_payload = {"referenceKey", "reasonCode"} if path.endswith("/reject") else {"referenceKey"}
                if set(payload) != expected_payload:
                    raise ValueError("invalid_dr_custody_instruction_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                manager = DepositaryReceiptManager(domain().bot.db)
                if path.endswith("/settle"):
                    success, result = manager.settle_conversion(payload["referenceKey"])
                    action = "settle"
                elif path.endswith("/cancel"):
                    success, result = manager.cancel_conversion(
                        user["userId"], payload["referenceKey"], allow_admin=True,
                    )
                    action = "cancel"
                else:
                    success, result = manager.reject_conversion(
                        user["userId"], payload["referenceKey"], payload["reasonCode"],
                    )
                    action = "reject"
                if success:
                    audit_admin_access(user["userId"], f"dr_custody_instruction.{action}", {
                        "reference_key": payload["referenceKey"],
                        **({"reason_code": payload["reasonCode"]} if action == "reject" else {}),
                    })
                self.send_json(200 if success else 400, {"success": success, "result": result})
                return
            if path == "/internal/admin/dr/voting-agenda":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                if set(payload) != {"meetingId", "title", "choices"}:
                    raise ValueError("invalid_dr_voting_agenda_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                success, result = DepositaryReceiptManager(domain().bot.db).add_voting_agenda_item(
                    payload["meetingId"], payload["title"], payload["choices"],
                )
                if success:
                    audit_admin_access(user["userId"], "dr_voting_agenda.create", {
                        "meeting_id": result["meetingId"],
                        "agenda_item_id": result["agendaItemId"],
                    })
                self.send_json(201 if success else 400, {"success": success, "result": result})
                return
            if path == "/internal/admin/dr/voting-window":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                if set(payload) != {"meetingId", "opensEpoch", "closesEpoch", "quorumPercent"}:
                    raise ValueError("invalid_dr_voting_window_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                success, result = DepositaryReceiptManager(domain().bot.db).set_voting_window(
                    payload["meetingId"], payload["opensEpoch"], payload["closesEpoch"],
                    payload["quorumPercent"],
                )
                if success:
                    audit_admin_access(user["userId"], "dr_voting_window.configure", {
                        "meeting_id": result["meetingId"],
                        "opens_epoch": result["opensEpoch"],
                        "closes_epoch": result["closesEpoch"],
                        "quorum_percent": result["quorumPercent"],
                    })
                self.send_json(200 if success else 400, {"success": success, "result": result})
                return

            user = identity(verified_id)
            if not user:
                self.send_json(403, {"error": "active_user_required"})
                return
            if path == "/internal/dr/convert":
                allowed = {"drCode", "action", "quantity", "referenceKey"}
                if set(payload) != allowed:
                    raise ValueError("invalid_dr_conversion_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                bot = domain().bot
                success, result = DepositaryReceiptManager(bot.db).request_conversion(
                    user["userId"], payload["drCode"], payload["action"],
                    payload["quantity"], payload["referenceKey"],
                )
                self.send_json(200 if success else 400,
                               {"success": success, "result": result})
                return
            if path == "/internal/dr/convert/cancel":
                if set(payload) != {"referenceKey"}:
                    raise ValueError("invalid_dr_conversion_cancel_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                success, result = DepositaryReceiptManager(domain().bot.db).cancel_conversion(
                    user["userId"], payload["referenceKey"],
                )
                self.send_json(200 if success else 400, {"success": success, "result": result})
                return
            if path == "/internal/dr/voting-delegations":
                allowed = {"drCode", "proxyUserId", "quantity"}
                if set(payload) != allowed:
                    raise ValueError("invalid_dr_voting_delegation_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                bot = domain().bot
                success, result = DepositaryReceiptManager(bot.db).delegate_votes(
                    user["userId"], payload["drCode"], payload["proxyUserId"],
                    payload["quantity"],
                )
                self.send_json(201 if success else 400,
                               {"success": success, "result": result})
                return
            if path == "/internal/dr/voting-delegations/revoke":
                if set(payload) != {"delegationId"}:
                    raise ValueError("invalid_dr_voting_delegation_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                bot = domain().bot
                success, result = DepositaryReceiptManager(bot.db).revoke_vote_delegation(
                    user["userId"], payload["delegationId"],
                )
                self.send_json(200 if success else 400,
                               {"success": success, "result": result})
                return
            if path == "/internal/dr/votes":
                allowed = {"meetingId", "agendaItemId", "choice"}
                if set(payload) != allowed:
                    raise ValueError("invalid_dr_vote_payload")
                from managers.depositary_receipt_manager import DepositaryReceiptManager
                success, result = DepositaryReceiptManager(domain().bot.db).cast_vote(
                    user["userId"], payload["meetingId"], payload["agendaItemId"], payload["choice"],
                )
                self.send_json(201 if success else 400, {"success": success, "result": result})
                return
            if path == "/internal/admin/bank-journal/close":
                user = identity(verified_id)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                required = {"ledgerScope", "entityId", "currencyCode",
                            "periodStartEpoch", "periodEndEpoch"}
                if set(payload) != required:
                    raise ValueError("invalid_accounting_period_close_payload")
                from managers.accounting_journal import AccountingJournal
                from managers.bank_journal import BankJournal
                scope = str(payload["ledgerScope"])
                if scope not in {"customer", "company", "institution"}:
                    raise ValueError("invalid_accounting_ledger_scope")
                start = BankJournal.validate_epoch(payload["periodStartEpoch"])
                end = BankJournal.validate_epoch(payload["periodEndEpoch"])
                with domain().db.atomic() as connection:
                    entity_id = str(payload["entityId"]).strip()
                    if scope == "institution":
                        from functions.banks import bank_issuer
                        entity_id = bank_issuer(connection, entity_id)
                    closing_id = AccountingJournal.close_period(
                        connection, ledger_scope=scope,
                        entity_id=entity_id,
                        currency_code=str(payload["currencyCode"]).strip().upper(),
                        period_start_epoch=start, period_end_epoch=end,
                        actor_id=user["userId"], closed_epoch=int(time.time()),
                    )
                audit_admin_access(user["userId"], "accounting.period.close", {
                    "scope": scope, "entity_id": str(payload["entityId"])[:100],
                    "currency": str(payload["currencyCode"]).upper(),
                    "period_start": start, "period_end": end,
                    "closing_id": closing_id,
                })
                self.send_json(200, {"success": True, "closingId": closing_id})
                return
            if path == "/internal/admin/compliance":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                from managers.compliance_manager import ComplianceManager
                action = str(payload.get("action") or "")
                with connect_db(timeout=4) as connection:
                    if action == "kyc_review":
                        allowed = {"action", "userId", "status", "riskTier", "reasonCode", "expiresEpoch"}
                        if set(payload) - allowed or not {"userId", "status", "reasonCode"}.issubset(payload):
                            raise ValueError("invalid_kyc_review_payload")
                        result = ComplianceManager.set_kyc_status(
                            connection, payload["userId"], user["userId"], payload["status"],
                            risk_tier=payload.get("riskTier", "standard"),
                            reason_code=payload["reasonCode"],
                            expires_epoch=payload.get("expiresEpoch"),
                        )
                    elif action == "kyc_expire_due":
                        if set(payload) != {"action"}:
                            raise ValueError("invalid_kyc_expiry_payload")
                        result = ComplianceManager.expire_due_kyc_profiles(
                            connection, user["userId"],
                        )
                    elif action == "aml_rule":
                        allowed = {"action", "nationality", "largeTransactionLimit",
                                   "rolling24hAmountLimit", "rolling24hCountLimit", "enabled"}
                        if set(payload) - allowed or "nationality" not in payload:
                            raise ValueError("invalid_aml_rule_payload")
                        result = ComplianceManager.configure_aml_rule(
                            connection, payload["nationality"], user["userId"],
                            large_transaction_limit=payload.get("largeTransactionLimit", 0),
                            rolling_24h_amount_limit=payload.get("rolling24hAmountLimit", 0),
                            rolling_24h_count_limit=payload.get("rolling24hCountLimit", 0),
                            enabled=payload.get("enabled", False),
                        )
                    elif action == "aml_review":
                        if set(payload) != {"action", "alertId", "status", "outcomeCode"}:
                            raise ValueError("invalid_aml_review_payload")
                        result = ComplianceManager.review_aml_alert(
                            connection, payload["alertId"], user["userId"],
                            payload["status"], payload["outcomeCode"],
                        )
                    elif action == "credit_risk_scenarios":
                        if set(payload) != {"action", "nationality", "scenarios"}:
                            raise ValueError("invalid_credit_risk_scenarios_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.configure_scenarios(
                            connection, payload["nationality"], user["userId"], payload["scenarios"],
                        )
                    elif action == "loan_credit_support":
                        allowed = {"action", "loanId", "partyUserId", "supportType",
                                   "exposureShareBps", "guaranteeLimit", "quoteCurrency",
                                   "guaranteeRecoveryBps", "recoveryDays", "recoveryCostBps",
                                   "seniorityRank", "underwritingDti", "underwritingDsr", "active"}
                        if set(payload) - allowed or not {"action", "loanId", "partyUserId", "supportType"}.issubset(payload):
                            raise ValueError("invalid_loan_credit_support_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.configure_loan_credit_support(
                            connection, loan_id=payload["loanId"],
                            party_user_id=payload["partyUserId"], actor_id=user["userId"],
                            support_type=payload["supportType"],
                            exposure_share_bps=payload.get("exposureShareBps", 0),
                            guarantee_limit=payload.get("guaranteeLimit", 0),
                            quote_currency=payload.get("quoteCurrency"),
                            guarantee_recovery_bps=payload.get("guaranteeRecoveryBps", 10000),
                            recovery_days=payload.get("recoveryDays", 180),
                            recovery_cost_bps=payload.get("recoveryCostBps", 0),
                            seniority_rank=payload.get("seniorityRank", 1),
                            underwriting_dti=payload.get("underwritingDti"),
                            underwriting_dsr=payload.get("underwritingDsr"),
                            active=payload.get("active", True),
                        )
                    elif action == "credit_risk_policy":
                        allowed = {"action", "nationality", "productCode", "pdMultiplier", "gradeThresholds"}
                        if set(payload) - allowed or not {"action", "nationality"}.issubset(payload):
                            raise ValueError("invalid_credit_risk_policy_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.configure_policy_override(
                            connection, payload["nationality"], payload.get("productCode", "*"),
                            user["userId"], pd_multiplier=payload.get("pdMultiplier", 1),
                            grade_thresholds=payload.get("gradeThresholds"),
                        )
                    elif action == "loan_future_commitment":
                        allowed = {"action", "loanId", "committedLimit", "creditConversionFactorBps", "active", "expiresEpoch"}
                        if set(payload) - allowed or not {"action", "loanId", "committedLimit", "creditConversionFactorBps"}.issubset(payload):
                            raise ValueError("invalid_loan_future_commitment_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.configure_future_commitment(
                            connection, payload["loanId"], user["userId"],
                            payload["committedLimit"], payload["creditConversionFactorBps"],
                            active=payload.get("active", True),
                            expires_epoch=payload.get("expiresEpoch"),
                        )
                    elif action == "loan_collateral_recovery":
                        allowed = {"action", "collateralId", "seniorityRank", "recoveryDays", "recoveryCostBps"}
                        if set(payload) - allowed or not {"action", "collateralId"}.issubset(payload):
                            raise ValueError("invalid_loan_collateral_recovery_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.configure_collateral_recovery(
                            connection, payload["collateralId"], user["userId"],
                            seniority_rank=payload.get("seniorityRank", 1),
                            recovery_days=payload.get("recoveryDays", 0),
                            recovery_cost_bps=payload.get("recoveryCostBps", 0),
                        )
                    elif action == "loan_charge_off":
                        allowed = {"action", "loanId", "eventKey", "memo"}
                        if set(payload) - allowed or not {"action", "loanId", "eventKey"}.issubset(payload):
                            raise ValueError("invalid_loan_charge_off_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.charge_off_loan(
                            connection, loan_id=payload["loanId"], event_key=payload["eventKey"],
                            actor_id=user["userId"], memo=payload.get("memo", ""),
                        )
                    elif action == "loan_chargeoff_recovery":
                        allowed = {"action", "loanId", "amount", "eventKey", "memo"}
                        if set(payload) - allowed or not {"action", "loanId", "amount", "eventKey"}.issubset(payload):
                            raise ValueError("invalid_loan_chargeoff_recovery_payload")
                        from managers.credit_risk_manager import CreditRiskManager
                        result = CreditRiskManager.record_chargeoff_recovery(
                            connection, loan_id=payload["loanId"], amount=payload["amount"],
                            event_key=payload["eventKey"], actor_id=user["userId"],
                            memo=payload.get("memo", ""),
                        )
                    else:
                        raise ValueError("unsupported_compliance_action")
                self.send_json(200, {"success": True, "result": result})
                return
            if path == "/internal/admin/dr/monitor/sample":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                if payload:
                    raise ValueError("invalid_dr_monitor_sample_payload")
                from managers.dr_deviation_monitor_manager import DrDeviationMonitorManager
                db = domain().db
                with db.atomic() as connection:
                    result = DrDeviationMonitorManager.sample_conn(connection)
                    history = DrDeviationMonitorManager.history_conn(connection, limit=100)
                self.send_json(200, {"result": result, **history})
                return
            if path.startswith("/internal/board/"):
                try:
                    self.send_json(200, board_post(path, user, payload))
                except LookupError:
                    self.send_json(404, {"error": "not_found"})
                return
            if path.startswith("/internal/admin/documents/"):
                parts = path.removeprefix("/internal/admin/documents/").split("/")
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                elif len(parts) == 2 and parts[1] == "preview":
                    self.send_json(200, documents.preview(parts[0], payload.get("content"), payload.get("important") is True))
                elif len(parts) == 2 and parts[1] == "cancel":
                    change = documents.cancel(parts[0])
                    with connect_db(timeout=5) as connection:
                        _audit_db(connection, user["userId"], "document.cancel", change)
                    self.send_json(200, {"success": True, "message": "✅ 공지한 개정안을 거두었습니다. 지금 문서가 계속 적용됩니다.",
                                         "document": documents.read_document(parts[0])})
                elif len(parts) == 1:
                    change = documents.save(parts[0], payload.get("content"), payload.get("baseSha256"), payload.get("confirmed"),
                                            payload.get("important") is True)
                    with connect_db(timeout=5) as connection:
                        _audit_db(connection, user["userId"], "document.announce" if change["scheduled"] else "document.save", change)
                    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(change["scheduled"] + 9 * 3600)) if change["scheduled"] else None
                    self.send_json(200, {"success": True, "document": documents.read_document(parts[0]),
                                         "message": f"✅ 개정안을 공지했습니다. {when}(한국시간)에 적용됩니다." if when
                                         else f"✅ 저장했습니다. (+{change['added']} / -{change['removed']}줄)"})
                else:
                    self.send_json(404, {"error": "not_found"})
                return
            if path.startswith("/internal/admin/board/"):
                parts = [unquote(part) for part in path.removeprefix("/internal/admin/board/").split("/")]
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                elif len(parts) != 2:
                    self.send_json(404, {"error": "not_found"})
                else:
                    message = admin_board_action(user["userId"], parts[0], parts[1], payload.get("action"), payload)
                    self.send_json(200, {"success": not message.startswith("❌"), "message": message})
                return
            if path == "/internal/hts/commands":
                success, message, data = run_user_command(user, payload)
                self.send_json(200 if success else 400, {"success": success, "message": message, "data": data})
            elif path == "/internal/hts/account":
                hts_id = set_hts_login_id(user["userId"], payload.get("htsId"), payload.get("password"))
                self.send_json(200, {"htsId": hts_id})
            elif path == "/internal/market/orders":
                trader = trading_user_id(user["userId"])
                success, message = execute_order(trader, payload)
                if success:
                    _read_cache.invalidate(("portfolio", trader), ("orders", trader))
                self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path.startswith("/internal/market/orders/") and path.endswith("/cancel"):
                order_id = int(path.removeprefix("/internal/market/orders/").removesuffix("/cancel"))
                trader = trading_user_id(user["userId"])
                success, message = cancel_order(trader, order_id)
                if success:
                    _read_cache.invalidate(("portfolio", trader), ("orders", trader))
                self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path == "/internal/admin/market":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    success, message = set_market_state(user["userId"], payload)
                    self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path == "/internal/admin/events":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    success, message = create_admin_event(user["userId"], payload)
                    self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path.startswith("/internal/market/drawings/"):
                code = unquote(path.removeprefix("/internal/market/drawings/"))
                if not any(row["code"] == code for row in instruments()):
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    drawings = save_drawings(user["userId"], code, payload.get("drawings"))
                    self.send_json(200, {"drawings": drawings})
            elif path.startswith("/internal/admin/db/tables/"):
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    match = re.fullmatch(r"/internal/admin/db/tables/([^/]+)/rows(?:/(\d+)(/delete)?)?", path)
                    if not match:
                        self.send_json(404, {"error": "not_found"})
                        return
                    table, rowid, delete = unquote(match.group(1)), match.group(2), match.group(3)
                    if rowid is None:
                        self.send_json(201, {"row": db_insert(user["userId"], table, payload.get("values"))})
                    elif delete:
                        db_delete(user["userId"], table, int(rowid))
                        self.send_json(200, {"success": True})
                    else:
                        self.send_json(200, {"row": db_update(user["userId"], table, int(rowid), payload.get("values"))})
            elif path == "/internal/admin/commands":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    runner = run_national_command if payload.get("national") else run_admin_command
                    message = runner(user["userId"], payload)
                    self.send_json(200, {"success": not message.startswith("❌"), "message": message})
            elif path.startswith('/internal/admin/funds/'):
                if user['role'] != 'admin':
                    self.send_json(403, {'error': 'admin_required'})
                else:
                    result = manage_fund(user['userId'], unquote(path.removeprefix('/internal/admin/funds/')), payload)
                    self.send_json(200 if result['success'] else 400, result)
            elif path == "/internal/admin/funds":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    success, message = create_fund(user["userId"], payload)
                    self.send_json(201 if success else 400, {"success": success, "message": message})
            elif path == "/internal/admin/login-as":
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, admin_login_as(user["userId"], payload))
            elif path.startswith("/internal/admin/") and path.split("/")[3] in {
                    "products", "countries", "exchanges", "user", "companies", "warnings", "feedback", "logs",
                    "naturalization", "banks", "organizations"}:
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                parts = [unquote(part) for part in path.split("/")[3:]]
                actor = user["userId"]
                if parts == ["organizations"]:
                    from web_api.organizations import action as organization_action
                    with connect_db(timeout=5) as connection:
                        result = organization_action(connection, payload, actor)
                    audit_admin_access(actor, "organization.configuration", result)
                    message = f"조직 설정을 저장했습니다: {result.get('action')}"
                elif parts == ["products"]:
                    message = product_save(actor, payload)
                elif len(parts) == 3 and parts[0] == "products" and parts[2] == "status":
                    message = product_status(actor, parts[1], bool(payload.get("active")),
                                             payload.get("terminate") is True)
                elif len(parts) == 3 and parts[0] == "products" and parts[2] == "delete":
                    message = product_delete(actor, parts[1])
                elif len(parts) == 2 and parts[0] == "countries":
                    message = admin_country_action(actor, parts[1], payload)
                elif len(parts) == 2 and parts[0] == "exchanges":
                    message = admin_exchange_action(actor, parts[1], payload)
                elif parts == ["companies", "identity"]:
                    message = admin_identity_action(actor, payload)
                elif len(parts) == 3 and parts[0] == "companies" and parts[2] == "share-actions":
                    message = admin_company_share_action(actor, parts[1], payload)
                elif len(parts) == 3 and parts[0] == "companies" and parts[2] == "journal":
                    message = admin_company_journal_action(actor, parts[1], payload)
                elif len(parts) == 2 and parts[0] == "companies":
                    message = admin_company_command(actor, parts[1], payload)
                elif len(parts) == 2 and parts[0] == "warnings":
                    message = admin_warning_action(actor, parts[1], payload)
                elif len(parts) == 3 and parts[0] == "logs" and parts[2] == "delete":
                    message = admin_log_delete(actor, parts[1], payload)
                elif parts == ["feedback", "delete"]:
                    message = admin_feedback_delete(actor, payload)
                elif len(parts) == 2 and parts[0] == "naturalization":
                    message = admin_naturalization_decide(actor, parts[1], payload)
                elif parts == ["banks"]:
                    message = admin_bank_create(actor, payload)
                elif len(parts) == 2 and parts[0] == "banks":
                    message = admin_bank_update(actor, parts[1], payload)
                elif len(parts) == 2 and parts[0] == "feedback":
                    message = admin_feedback_action(actor, parts[1], payload)
                elif len(parts) == 2 and parts[0] == "user":
                    message = admin_update_user(actor, parts[1], payload.get("changes"))
                elif len(parts) == 3 and parts[0] == "user" and parts[2] == "web":
                    action = payload.get("action")
                    message = (admin_set_web_password(actor, parts[1], payload.get("password"))
                               if action == "password" else
                               admin_web_login_action(actor, parts[1], action, payload.get("htsId")))
                else:
                    self.send_json(404, {"error": "not_found"})
                    return
                self.send_json(200, {"success": not str(message).startswith("❌"), "message": str(message)})
            elif path.startswith("/internal/admin/users/"):
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    target_id = unquote(path.removeprefix("/internal/admin/users/"))
                    message = update_admin_user(user["userId"], target_id, payload)
                    self.send_json(200, {"success": True, "message": message})
            elif path.startswith("/internal/admin/instruments/") and path.endswith("/status"):
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    code = path.removeprefix("/internal/admin/instruments/").removesuffix("/status")
                    success, message = set_instrument_status(user["userId"], code, payload)
                    self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path.startswith("/internal/admin/instruments/"):
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    code = path.removeprefix("/internal/admin/instruments/")
                    success, message = edit_instrument(user["userId"], code, payload)
                    self.send_json(200 if success else 400, {"success": success, "message": message})
            else:
                self.send_json(404, {"error": "not_found"})
        except (ValueError, TypeError, OverflowError) as exc:
            self.send_json(400, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            # Constraint and trigger failures (CHECK, FOREIGN KEY, RAISE) are
            # validation errors the administrator can act on.
            self.send_json(400, {"error": "constraint_failed", "message": str(exc)})
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                self.send_json(503, {"error": "database_unavailable"})
            else:
                self.send_json(400, {"error": "database_rejected", "message": str(exc)})
        except sqlite3.Error as exc:
            logging.getLogger("inazina.web").exception("POST %s failed", self.path)
            self.send_json(503, {"error": "database_unavailable", "message": f"데이터베이스 작업에 실패했습니다: {exc}"})

    def redirect_browser_to_main(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def browser_access_login(self):
        address = self.client_address[0] if self.client_address else "local"
        now = time.time()
        count, reset_at = _browser_login_attempts.get(address, (0, now + 600))
        if reset_at > now and count >= 10:
            self.redirect_browser_to_main()
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if not 0 < length <= 2048:
                raise ValueError("invalid_request")
            payload = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            password = payload.get("password", [""])[0]
            target = payload.get("target", [""])[0]
            actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
        except (ValueError, UnicodeDecodeError):
            self.send_json(400, {"error": "invalid_request"})
            return
        if not hmac.compare_digest(actual, BROWSER_PASSWORD_SHA256):
            _browser_login_attempts[address] = (count + 1 if reset_at > now else 1,
                                                reset_at if reset_at > now else now + 600)
            self.redirect_browser_to_main()
            return
        _browser_login_attempts.pop(address, None)
        target_path = urlsplit(target).path if isinstance(target, str) else ""
        if (not isinstance(target, str) or not target_path.startswith("/internal/") or
                target.startswith("//") or "\\" in target):
            self.send_json(400, {"error": "invalid_target"})
            return
        self.path = target
        self.browser_access_granted = True
        self.do_GET()

    def send_browser_access_page(self, target):
        import html
        body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>INAZINA · 비밀번호</title><style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f8e9ee;color:#1d1d1f;font:16px -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}}main{{width:min(420px,100%);padding:32px;border-radius:24px;background:#ffffffd9;box-shadow:0 24px 60px #3c1e642e}}h1{{font-size:24px;margin:0}}form{{display:flex;gap:8px;margin-top:22px}}input,button{{font:inherit;padding:12px;border-radius:12px}}input{{min-width:0;flex:1;border:1px solid #ccc}}button{{border:0;background:#0071e3;color:white;cursor:pointer}}</style><main><h1>API 비밀번호</h1><form method="post" action="/__browser-access"><input type="hidden" name="target" value="{html.escape(target, quote=True)}"><input type="password" name="password" autocomplete="current-password" aria-label="비밀번호" required autofocus><button>확인</button></form></main></html>'''.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_landing(self, path):
        from web_api.landing import landing_page
        try:
            with connect_readonly() as connection:
                connection.execute("SELECT 1").fetchone()
            database_ok = True
        except sqlite3.Error:
            database_ok = False
        home = path in {"", "/", "/internal", "/internal/"}
        body = landing_page("home" if home else "not_found", path, database_ok).encode("utf-8")
        try:
            self.send_response(200 if home else 404)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-security-policy",
                             "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_json(self, status, payload):
        from functions.exact_money import json_amount
        from decimal import Decimal
        def safe(value):
            if isinstance(value,Decimal) or isinstance(value,int) and not isinstance(value,bool) and abs(value)>2**53-1:
                return json_amount(value)
            if isinstance(value,dict):return {key:safe(item) for key,item in value.items()}
            if isinstance(value,(list,tuple)):return [safe(item) for item in value]
            return value
        body = json.dumps(safe(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers cancel superseded dashboard fetches.  The response has
            # no further side effect, so an abandoned socket is not an error.
            return

    def log_message(self, format_string, *args):
        return


def main():
    """Serve until SIGINT/SIGTERM, then stop cleanly.

    Ctrl+C in the start_all terminal reaches this child process directly (same
    process group) as well as through the Node API's SIGTERM; both now end the
    service quietly instead of printing a KeyboardInterrupt traceback.
    """
    import signal
    if not INTERNAL_TOKEN:
        raise SystemExit("INAZINA_INTERNAL_TOKEN is required")
    # The Discord bot migrates and backs up the same database at the same moment on start_all;
    # a busy or locked database at startup is retried instead of ending the service.
    for attempt in range(1, 7):
        try:
            domain()
            break
        except KeyboardInterrupt:
            print("INAZINA Python data service stopped during startup.", flush=True)
            return
        except Exception:
            import traceback
            print(f"INAZINA Python data service: database startup failed (attempt {attempt}/6)", flush=True)
            traceback.print_exc()
            if attempt == 6:
                raise
            time.sleep(5)
    server = BoundedThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    stopping = threading.Event()

    def monitor_dr_deviations():
        from managers.dr_deviation_monitor_manager import DrDeviationMonitorManager
        while not stopping.wait(60):
            try:
                with domain().db.atomic(("assets", "company", "currency")) as connection:
                    result = DrDeviationMonitorManager.sample_conn(connection)
                if result["transitions"]:
                    logging.getLogger("inazina.dr_monitor").info(
                        "recorded %s DR deviation transitions", result["transitions"]
                    )
            except Exception:
                logging.getLogger("inazina.dr_monitor").exception(
                    "DR deviation sampling failed"
                )

    monitor = threading.Thread(target=monitor_dr_deviations,
                               name="dr-deviation-monitor", daemon=True)
    monitor.start()

    def apply_documents():
        while True:
            try:
                _apply_due_documents()
            except Exception:
                logging.getLogger("inazina.documents").exception("applying announced documents failed")
            if stopping.wait(60):
                return

    threading.Thread(target=apply_documents, name="document-revisions", daemon=True).start()

    def publish_news():
        while not stopping.wait(15):
            try:
                publish_due_news()
            except Exception:
                logging.getLogger("inazina.news").exception("publishing scheduled news failed")

    threading.Thread(target=publish_news, name="board-news", daemon=True).start()

    # The Node API starts this service with a per-start token. If the API dies without stopping
    # it, an orphan would keep the port with a stale token and refuse the next API; exit instead.
    parent = os.getppid()

    def follow_parent():
        while not stopping.wait(2):
            if os.getppid() != parent:
                logging.getLogger("inazina").warning("API process %s is gone; stopping the data service", parent)
                stop(None, None)
                return

    threading.Thread(target=follow_parent, name="parent-watch", daemon=True).start()

    def stop(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            # shutdown() waits for serve_forever(), so it must run off this thread.
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"INAZINA Python data service listening at http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        stopping.set()
        server.server_close()
    print("INAZINA Python data service stopped.", flush=True)


if __name__ == "__main__":
    main()
