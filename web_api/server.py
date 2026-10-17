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
PORT = int(os.environ.get("INAZINA_PYTHON_PORT", "4313"))
MAX_HTTP_THREADS = max(1, min(int(os.environ.get("INAZINA_MAX_HTTP_THREADS", "16")), 64))
BOOTSTRAP_ADMIN_IDS = {"1076861147560542278"}
sys.path.insert(0, str(ROOT))
from database.user_db import TREASURY_USER_ID, citizen_filter
from functions.dr_pricing import (
    DEVIATION_ALERT_THRESHOLD_PERCENT as DR_REFERENCE_DEVIATION_ALERT_PERCENT,
    reference_metrics as dr_reference_metrics,
)
from functions.performance import log_slow
from web_api import board
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
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


@contextmanager
def connect_db(timeout=2):
    connection = sqlite3.connect(DATABASE, timeout=timeout)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
        with connection:
            yield connection
    finally:
        connection.close()


def identity(user_id):
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
        country_account = connection.execute(
            "SELECT 1 FROM country_currencies WHERE nationality=?", (user_id,),
        ).fetchone() if row and row["user_id"] == row["nationality"] else None
        treasury_id = _country_treasury(connection, row["nationality"]) if country_account else None
        treasury = connection.execute(
            "SELECT cash FROM users WHERE user_id=?", (treasury_id,),
        ).fetchone() if treasury_id else None
        currency = connection.execute(
            "SELECT currency_code FROM country_currencies WHERE nationality=?", (row["nationality"],),
        ).fetchone() if row else None
    if (not row or row["status"] not in {"active", "nation"} or
            (auth_table and credential is None)):
        return None
    role = "admin" if user_id in BOOTSTRAP_ADMIN_IDS else str(row["role"] or "user").strip().lower()
    if role not in {"admin", "user", "nation", "company"}:
        role = "user"
    return {
        "userId": str(row["user_id"]),
        "nickname": str(row["nickname"] or row["user_id"]),
        "nationality": str(row["nationality"] or ""),
        "currency": str(currency[0]) if currency else "NWX",
        "cash": int((treasury["cash"] if treasury else row["cash"]) or 0),
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
            _board_ip_purged = time.time()
        yield connection


def board_get(path, query, viewer):
    """Public board reads; ``viewer`` (or None) only marks the viewer's own posts."""
    first = lambda key, default="": query.get(key, [default])[0]
    with board_db() as connection:
        if path == "/internal/board/posts":
            return board.list_posts(connection, first("board") or None, first("search"), first("asset") or None,
                                    first("page", "1"), viewer)
        return {"post": board.read_post(connection, path.removeprefix("/internal/board/posts/"), viewer,
                                        count_view=first("view") == "1")}


def board_post(path, user, payload):
    ip = payload.get("clientIp")
    parts = path.removeprefix("/internal/board/").split("/")
    with board_db() as connection:
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
            if action == "like":
                return {"liked": board.toggle_like(connection, user, parts[1])}
        if len(parts) == 3 and parts[0] == "comments" and parts[2] == "delete":
            board.delete_comment(connection, user, parts[1]); return {"success": True}
        if parts == ["reports"]:
            board.report(connection, user, payload, ip); return {"success": True}
    raise LookupError("not_found")


BOARD_ACTION_NAMES = {"hide": "숨김", "restore": "복원", "pin": "상단 고정", "unpin": "고정 해제", "purge": "영구 삭제", "resolve": "처리 완료"}


def admin_board_action(actor, kind, target, action):
    with board_db() as connection:
        changed = board.admin_action(connection, kind, target, action)
        _audit_db(connection, actor, f"board.{kind}.{action}", {"id": target, "changed": changed})
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


def admin_feedback(status=None):
    with connect_db(timeout=5) as connection:
        ensure_feedback_table(connection)
        rows = connection.execute(
            "SELECT * FROM site_feedback WHERE (? IS NULL OR status=?) ORDER BY feedback_id DESC LIMIT 200",
            (status, status),
        ).fetchall()
        unread = connection.execute("SELECT COUNT(*) FROM site_feedback WHERE status='new'").fetchone()[0]
    return {"items": [dict(row) for row in rows], "unread": int(unread)}


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
    user_id = country_id
    now = int(time.time())
    salt = secrets.token_bytes(16).hex()
    digest = password_hash(password, salt)
    from database.db_manager import DBManager
    with DBManager.admin_scope(actor_id):
        with DBManager.atomic(("users", "currency", "system")) as connection:
            ensure_auth_table(connection)
            country = connection.execute(
                "SELECT 1 FROM country_currencies n JOIN currencies c ON c.code=n.currency_code "
                "WHERE n.nationality=? AND c.status='active'", (country_id,),
            ).fetchone()
            if not country:
                raise ValueError("country_not_found")
            existing_user = connection.execute(
                "SELECT nationality,status,cash,role FROM users WHERE user_id=?", (user_id,),
            ).fetchone()
            if existing_user:
                if existing_user["nationality"] != country_id:
                    raise ValueError("country_account_conflict")
                if existing_user["status"] != "active":
                    raise ValueError("active_user_required")
                if existing_user["role"] not in {"user", "nation"}:
                    raise ValueError("country_account_conflict")
                if connection.execute(
                    "SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,),
                ).fetchone():
                    raise ValueError("country_account_exists")
                connection.execute("UPDATE users SET cash=0,role='nation' WHERE user_id=?", (user_id,))
                cash = 0
            else:
                connection.execute(
                    "INSERT INTO users(user_id,nickname,nationality,cash,role) VALUES(?,?,?,?, 'nation')",
                    (user_id, f"{country_id} 국가 계정", country_id, 0),
                )
                cash = 0
            connection.execute(
                "INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch) "
                "VALUES(?,?,?,?,?)", (user_id, salt, digest, now, now),
            )
    return {"countryId": country_id, "userId": user_id, "cash": cash, "status": "active"}


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
            "SELECT 1 FROM users u JOIN country_currencies c ON c.nationality=u.user_id "
            "WHERE u.user_id=? AND u.nationality=? AND u.status='active'",
            (country_id, country_id),
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
        connection.row_factory = sqlite3.Row
        country_account = connection.execute(
            "SELECT 1 FROM users u JOIN country_currencies c ON c.nationality=u.user_id "
            "JOIN web_auth_credentials w ON w.user_id=u.user_id "
            "WHERE u.user_id=? AND u.nationality=? AND u.status='active'",
            (identifier, identifier),
        ).fetchone()
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
        # 24-hour activity for the HTS "실시간 추천" list: price change and traded value.
        since = int(time.time()) - 86400
        opening = {code: price for code, price, _ in connection.execute(
            "SELECT asset_code,price,MIN(recorded_epoch) FROM market_price_history "
            "WHERE recorded_epoch>=? GROUP BY asset_code", (since,))} if _table_exists(connection, "market_price_history") else {}
        traded = {code: (value, count) for code, value, count in connection.execute(
            "SELECT asset_code,SUM(quantity*price),COUNT(*) FROM market_trade_history "
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
                        float(row["face_value"] or 1), float(row["annual_rate"] or 0),
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
                    row["bondRisk"]["faceValue"] = float(row["face_value"] or 1)
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
                                connection, str(underlying["quote_currency"] or "NWX"),
                                str(row.get("quote_currency") or "NWX"),
                            ))
                            metrics = dr_reference_metrics(
                                row["price"], underlying["price"],
                                program["underlyingPerReceipt"], fx,
                            )
                            if metrics:
                                program["underlyingPrice"] = float(underlying["price"])
                                program["underlyingCurrency"] = str(underlying["quote_currency"] or "NWX")
                                program.update(metrics)
                    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
                        pass
            start = opening.get(row["code"])
            row["change24h"] = (float(row["price"]) / float(start) - 1) * 100 if start and row["price"] else None
            row["tradeValue24h"], row["trades24h"] = traded.get(row["code"], (0, 0))
            if row["type"] == "mutual_fund":
                row["components"] = components.get(row["code"], [])
            warning = warnings.get(row["code"])
            if warning:
                row["warning"] = {"level": warning["level"], "label": LEVELS[warning["level"]],
                                  "reason": warning["reason"], "expiresEpoch": warning["expires_epoch"]}
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
            "(SELECT quote_currency FROM market_assets WHERE code=d.underlying_code),'NWX') underlying_currency "
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


def user_dividend_payments(user_id, limit=100):
    """Return the authenticated user's recent dividend receipts, grouped by payout currency."""
    domain()  # Apply additive corporate-action schema migrations before querying.
    from functions.money import currency_precision
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT p.payment_id,d.asset_code,p.quantity,p.gross_amount,p.tax_amount,"
            "p.net_amount_units,p.quote_currency,p.created_epoch,d.dividend_kind "
            "FROM dividend_payments p JOIN dividend_distributions d USING(distribution_id) "
            "WHERE p.user_id=? ORDER BY p.created_epoch DESC,p.payment_id DESC LIMIT ?",
            (str(user_id), max(1, min(100, int(limit)))),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row['decimal_places'] = currency_precision(connection, row['quote_currency'])
    return result


def user_loan_schedules(user_id):
    """Return the authenticated user's upcoming loan installment breakdowns."""
    domain()
    from functions.money import currency_precision
    with connect_readonly() as connection:
        loans = [dict(row) for row in connection.execute(
            "SELECT loan_id,product_code,quote_currency,outstanding_balance,status,"
            "underwriting_dsr,underwriting_stress_dsr,"
            "collection_status,maturity_epoch FROM loans WHERE user_id=? "
            "AND status IN ('active','defaulted') ORDER BY loan_id",
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
            "(SELECT quote_currency FROM market_assets WHERE code=d.underlying_code),'NWX')) underlying_currency "
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
    return [{"time": int(row["bucket"]), "open": float(row["open"]), "high": float(row["high"]),
             "low": float(row["low"]), "close": float(row["close"]), "ticks": int(row["ticks"])}
            for row in rows]


def chart(code, range_key, interval=None):
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
    points = [{"time": int(row["recorded_epoch"]), "price": float(row["price"])}
              for row in reversed(rows)]
    if len(points) > 1200:
        stride = math.ceil(len(points) / 1200)
        points = points[::stride] + ([points[-1]] if points[-1] != points[::stride][-1] else [])
    return {"instrument": item, "points": points, "candles": candle_rows,
            "interval": interval, "range": range_key}


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
    if not isinstance(payload, dict):
        raise ValueError("invalid_order")
    code, side = str(payload.get("assetCode", "")).strip(), payload.get("side")
    quantity, price = payload.get("quantity"), payload.get("limitPrice")
    if (not code or len(code) > 80 or side not in {"buy", "sell"} or
            isinstance(quantity, bool) or not isinstance(quantity, int) or
            isinstance(price, bool) or not isinstance(price, int)):
        raise ValueError("invalid_order")
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
        return manager.snapshot(code)
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
        return manager.rebalance(actor, code)
    else:
        raise ValueError('펀드 작업 또는 입력 필드를 확인해주세요.')
    return {'success': success, 'message': message, 'fund': manager.snapshot(code)}


def admin_users(search, kind="people"):
    """People and country accounts (``kind='people'``) or company capital accounts (``kind='company'``)."""
    term = f"%{search.strip()}%"
    with connect_readonly() as connection:
        has_companies = _table_exists(connection, "company_accounts")
        company = "EXISTS(SELECT 1 FROM company_accounts a WHERE a.user_id=users.user_id)" if has_companies else "0"
        rows = connection.execute(
            "SELECT user_id,nickname,nationality,"
            "CASE WHEN user_id=nationality AND EXISTS(SELECT 1 FROM country_currencies c "
            "WHERE c.nationality=users.user_id) THEN COALESCE((SELECT t.cash FROM users t "
            "WHERE t.user_id=? AND t.nationality=users.nationality),cash) "
            "ELSE cash END AS cash,credit_score,status,role, "
            f"CASE WHEN {company} THEN 'company' "
            "WHEN user_id=nationality AND EXISTS(SELECT 1 FROM country_currencies c "
            "WHERE c.nationality=users.user_id) THEN 'country' ELSE 'discord' END AS account_type, "
            + ("(SELECT a.company_code FROM company_accounts a WHERE a.user_id=users.user_id)" if has_companies else "NULL")
            + " AS company_code FROM users WHERE user_id<>? AND (user_id LIKE ? OR nickname LIKE ?) "
            f"AND {'' if kind == 'company' else 'NOT '}{company} "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,nickname LIMIT 100",
            (TREASURY_USER_ID, TREASURY_USER_ID, term, term),
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


def admin_bank_journal_report(issuer, currency=None, since_epoch=None):
    """Read-only bank-book controls for an administrator's audit workbench."""
    from managers.bank_journal import BankJournal
    from functions.money import currency_precision
    from decimal import Decimal

    def money_text(value, places):
        amount = Decimal(str(value or 0))
        return f"{amount:.{places}f}"

    with connect_readonly() as connection:
        if not connection.execute(
                "SELECT 1 FROM financial_institutions WHERE issuer=?", (str(issuer),)).fetchone():
            raise ValueError("financial_institution_not_found")
        trial_balance = BankJournal.trial_balance(connection, issuer, currency)
        unbalanced = BankJournal.audit_unbalanced(connection, issuer)
        deposit_reconciliation = BankJournal.reconcile_customer_deposits(connection, issuer)
        loan_reconciliation = BankJournal.reconcile_loan_receivables(connection, issuer)
        ecl_allowance_reconciliation = BankJournal.reconcile_ecl_allowances(connection, issuer)
        insurance_claims = [dict(row) for row in connection.execute(
            "SELECT c.user_id,c.account_number,a.quote_currency AS currency_code,"
            "c.covered_amount,c.uninsured_amount,c.resolved_epoch "
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
        fx_stress = RateManager.institution_fx_stress(connection, issuer)
        institution = RateManager.institution_components(connection, issuer)
        balance_sheet = RateManager.institution_balance_sheet(connection, issuer)
        risk_report = balance_sheet["rwa_report"]
        from managers.credit_risk_manager import CreditRiskManager
        credit_stress = CreditRiskManager.portfolio_default_stress(connection, issuer)
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
        for row in insurance_claims:
            row["currency_code"] = str(row["currency_code"] or balance_sheet["base_currency"])
            places = currency_precision(connection, row["currency_code"])
            row["covered_amount_text"] = money_text(row["covered_amount"], places)
            row["uninsured_amount_text"] = money_text(row["uninsured_amount"], places)
            row["decimal_places"] = places
    return {"issuer": str(issuer), "currency": currency,
            "trialBalance": trial_balance, "unbalancedEntries": unbalanced,
            "resultReconciliation": reconciliation,
            "depositReconciliation": deposit_reconciliation,
            "loanReconciliation": loan_reconciliation,
            "eclAllowanceReconciliation": ecl_allowance_reconciliation,
            "depositInsuranceClaims": insurance_claims,
            "maturityGap": maturity_gap,
            "liquidityStress": liquidity_stress, "rateStress": rate_stress, "fxStress": fx_stress,
            "prudential": prudential, "creditStress": credit_stress}


def admin_compliance_overview(nationality=None):
    """Return bounded, privacy-minimal KYC/AML queues for active administrators."""
    from functions.money import currency_precision
    from decimal import Decimal
    with connect_readonly() as connection:
        kyc_sql = ("SELECT user_id,status,risk_tier,reviewed_by,reviewed_epoch,expires_epoch,updated_epoch "
                   "FROM kyc_profiles")
        alert_sql = ("SELECT alert_id,user_id,nationality,rule_code,reference_key,currency_code,amount,"
                     "status,outcome_code,opened_epoch,reviewed_by,reviewed_epoch FROM aml_alerts")
        params = ()
        if nationality:
            kyc_sql += " WHERE user_id IN (SELECT user_id FROM users WHERE nationality=?)"
            alert_sql += " WHERE nationality=?"
            params = (str(nationality),)
        kyc_sql += " ORDER BY updated_epoch DESC LIMIT 100"
        alert_sql += " AND status<>'closed' ORDER BY opened_epoch DESC LIMIT 100" if nationality else " WHERE status<>'closed' ORDER BY opened_epoch DESC LIMIT 100"
        profiles = [dict(row) for row in connection.execute(kyc_sql, params).fetchall()]
        alerts = [dict(row) for row in connection.execute(alert_sql, params).fetchall()]
        for row in alerts:
            places = currency_precision(connection, row["currency_code"])
            row["amount_text"] = f"{Decimal(str(row['amount'] or 0)):.{places}f}"
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
        ecl_sql = ("SELECT e.assessment_id,e.loan_id,e.as_of_day,e.stage,e.pd_12m,e.lifetime_pd,e.lgd,"
                   "e.ead,e.expected_loss,e.discount_factor,e.quote_currency,e.scenario,"
                   "e.effective_interest_rate,e.expected_loss_time_years,"
                   "e.calculated_epoch,u.nationality "
                   "FROM loan_ecl_assessments e JOIN loans l USING(loan_id) "
                   "JOIN users u ON u.user_id=l.user_id")
        if nationality:
            scenario_sql += " WHERE nationality=?"
            ecl_sql += " WHERE u.nationality=?"
        scenario_sql += " ORDER BY nationality,CASE scenario_key WHEN 'optimistic' THEN 1 WHEN 'base' THEN 2 ELSE 3 END"
        ecl_sql += " ORDER BY e.calculated_epoch DESC LIMIT 100"
        scenarios = [dict(row) for row in connection.execute(scenario_sql, params).fetchall()]
        ecl_assessments = [dict(row) for row in connection.execute(ecl_sql, params).fetchall()]
        for row in ecl_assessments:
            places = currency_precision(connection, row["quote_currency"])
            row["decimal_places"] = places
            row["ead_text"] = f"{Decimal(str(row['ead'])):.{places}f}"
            row["expected_loss_text"] = f"{Decimal(str(row['expected_loss'])):.{places}f}"
    return {"kycProfiles": profiles, "amlAlerts": alerts, "amlRules": rules,
            "kycEvents": kyc_events, "amlEvents": aml_events,
            "creditRiskScenarios": scenarios, "eclAssessments": ecl_assessments}


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
    "saving": ("저축 · 국적 통화", "finance", "saving"), "loan": ("대출", "finance", "loan"),
    "currency": ("통화", "finance", "currency"), "currencyproduct": ("저축 · 외환통장", "finance", "currencyproduct"),
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
SAVING_FAMILY = {"saving", "currencyproduct"}
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
    kinds = []
    with connect_readonly() as connection:
        for profile, (label, group, token) in PRODUCT_KINDS.items():
            table, key = _profile_table(profile)
            columns = {row["name"]: row["type"] or "" for row in connection.execute(f'PRAGMA table_info("{table}")')}
            fields = [{"key": field,
                       "label": "발행총액" if profile == "bond" and field == "total_supply"
                                else ("결제 통화" if profile == "dr" else "통장 화폐") if field == "quote_currency" else ARG_LABELS.get(field, field),
                       "optional": field in OPTIONAL_ARGS.get(profile, ()),
                       "text": field in TEXT_ARGS or field == "quote_currency"} for field in _form_fields(profile)]
            listed = {field["key"] for field in fields} | {key}
            advanced = [{"key": name, "label": ARG_LABELS.get(name, name), "type": kind}
                        for name, kind in columns.items() if name not in listed and name not in PRODUCT_PROTECTED]
            kinds.append({"kind": profile, "token": token, "label": label, "group": group,
                          "family": "saving" if profile in SAVING_FAMILY else profile,
                          "fields": fields, "advanced": advanced,
                          "example": EXAMPLES.get(_profile_category(profile), {}),
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
    rows = []
    with connect_readonly() as connection:
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
                             "value": item.get(headline)}
                if source == "market_assets" and item.get("type") == "depositary_receipt":
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
                            dr_program["underlyingPrice"] = float(underlying["price"])
                            dr_program["underlyingCurrency"] = str(underlying["quote_currency"] or "NWX")
                            try:
                                from managers.currency_manager import CurrencyManager
                                fx = float(CurrencyManager.quote(
                                    connection, dr_program["underlyingCurrency"],
                                    str(item.get("quote_currency") or "NWX"),
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
            elif field == "currency_code" and kind == "currencyproduct":
                values[field] = row.get("quote_currency")
            elif field == "annual_rate" and kind == "currencyproduct":
                values[field] = row.get("interest_rate")
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
    return {"code": code, "kind": kind, "status": row.get("status") or "active",
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


def product_save(actor, payload):
    from managers.admin_commands_manager import AdminCommandsManager
    from managers.admin_control_manager import ENTITIES
    operation = payload.get("operation")
    profile = payload.get("kind")
    code = str(payload.get("code", "")).strip()
    if operation not in {"create", "edit"} or profile not in PRODUCT_KINDS:
        raise ValueError("상품 종류와 작업을 확인해주세요.")
    if (not code and profile != "option") or len(code) > 80 or re.search(r"\s", code):
        raise ValueError("상품 코드는 공백 없이 1~80자로 입력해주세요.")
    table, _key = _profile_table(profile)
    with connect_readonly() as connection:
        columns = {row["name"]: row["type"] or "" for row in connection.execute(f'PRAGMA table_info("{table}")')}
    fields = _convert_product_fields(profile, payload.get("fields") or {}, payload.get("advanced") or {}, columns)
    bot = domain().bot
    if operation == "create":
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
            if code == "NWX":
                reasons.append("기준 통화 NWX는 삭제할 수 없습니다")
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
USER_FIELDS = ("nickname", "nationality", "cash", "credit_score", "annual_income", "income_stability", "status", "role")


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
    user = {key: _cell(row[key]) for key in row.keys()}
    return {"user": user, "accountType": "company" if company else "country" if country and row["nationality"] == user_id else "discord",
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
            macro_indicators = connection.execute(
                "SELECT inflation_percent,real_gdp_growth_percent,unemployment_percent,"
                "fiscal_debt_percent,updated_by,updated_epoch "
                "FROM country_macro_indicators WHERE nationality=?", (nationality,)
            ).fetchone() if _table_exists(connection, "country_macro_indicators") else None
            tax_rules = [dict(item) for item in connection.execute(
                "SELECT income_source,tax_percent,updated_by,updated_epoch FROM country_tax_rules "
                "WHERE nationality=? ORDER BY income_source", (nationality,))] \
                if _table_exists(connection, "country_tax_rules") else []
            account = connection.execute(
                "SELECT u.status,u.role,w.hts_id FROM users u LEFT JOIN web_auth_credentials w ON w.user_id=u.user_id "
                "WHERE u.user_id=? AND u.nationality=?", (nationality, nationality)).fetchone() if _table_exists(connection, "web_auth_credentials") else None
            result.append({**{key: row[key] for key in row.keys()},
                           "treasuryId": treasury["user_id"] if treasury else None,
                           "treasuryCash": treasury["cash"] if treasury else None,
                           "citizens": int(citizens), "guilds": guilds, "exchanges": exchanges,
                           "marketRates": market_rates, "marketRatePressure": market_rate_pressure,
                           "macroIndicators": dict(macro_indicators) if macro_indicators else {},
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
        result = AdminCommandsManager(bot).execute(actor, "국가생성", " ".join(quote(value) for value in [nationality, *args]))
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
        with bot.db.atomic() as connection:
            bot.admin_control.require(actor, connection)
            cursor = connection.execute(
                "UPDATE country_macro_indicators SET inflation_percent=?,"
                "real_gdp_growth_percent=?,unemployment_percent=?,fiscal_debt_percent=?,"
                "updated_by=?,updated_epoch=? WHERE nationality=?",
                (values["inflationPercent"], values["realGdpGrowthPercent"],
                 values["unemploymentPercent"], values["fiscalDebtPercent"],
                 str(actor), time.time(), nationality),
            )
            if cursor.rowcount != 1:
                raise ValueError("국가의 거시 지표 행을 찾을 수 없습니다.")
            bot.admin_control._audit(connection, actor, "country.macro_indicators", {
                "nationality": nationality, **values,
            })
        result = f"✅ {nationality} 거시 지표를 저장했습니다. 시장금리 재산정에 반영됩니다."
    elif action == "treasury":
        from functions.admin_prefix import ActionModal
        from database.db_manager import DBManager
        with connect_readonly() as connection:
            treasury = connection.execute(
                "SELECT user_id FROM country_treasuries WHERE nationality=?", (nationality,)).fetchone()
        if not treasury:
            raise ValueError("국고를 찾을 수 없습니다.")
        amount = bot.admin_control._number(payload.get("amount"), -2**53 + 1, 2**53 - 1, integer=True)
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
    with connect_readonly() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM market_exchanges ORDER BY exchange_id")]
        for row in rows:
            exchange = row["exchange_id"]
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
    return {"account": account, "webLogin": bool(login), "loginId": code, "assets": json.loads(json.dumps(info, default=lambda value: dict(value) if hasattr(value, "keys") else str(value)))}


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


def admin_identity_action(actor, payload):
    from functions.admin_prefix import national_login, national_logout
    target = str(payload.get("target") or "").strip()
    return national_login(domain().bot, actor, target) if target else national_logout(domain().bot, actor)


def admin_company_financial(code, period=None):
    from managers.financial_manager import FinancialManager
    manager = FinancialManager(domain().db)
    return {"report": manager.report(code, period or None), "periods": manager.periods(code)}


def admin_company_command(actor, code, payload):
    from decimal import InvalidOperation
    from functions import company_actions, user_actions
    command = str(payload.get("command", ""))
    values = user_actions.parse_values(command, payload.get("values") or {})
    try:
        success, message, _ = company_actions.run(domain().bot, actor, code, command, values)
    except (KeyError, InvalidOperation, sqlite3.IntegrityError, RuntimeError) as exc:
        raise ValueError(str(exc) or "명령어를 처리하지 못했습니다.") from exc
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
    return str(result or "완료했습니다.")


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


def request_json(handler):
    try:
        size = int(handler.headers.get("content-length", "0"))
        if size < 0 or size > 32_768:
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
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed >= 100:
                import logging
                logging.getLogger("inazina.performance").warning(
                    "http_request %.1f ms", elapsed
                )

    def do_GET(self):
        if not INTERNAL_TOKEN or not hmac.compare_digest(
            self.headers.get("x-inazina-internal-token", ""), INTERNAL_TOKEN
        ):
            # A person who opens this port in a browser gets a page, not raw JSON;
            # the API server always calls with the token and Accept: */*.
            if "text/html" in self.headers.get("accept", ""):
                self.send_landing(urlsplit(self.path).path)
                return
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
            elif path == "/internal/me":
                user_id = internal_user_id(self)
                user = identity(user_id)
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"user": user})
            elif path == "/internal/board/posts" or path.startswith("/internal/board/posts/"):
                viewer = identity(internal_user_id(self)) if internal_user_id(self) else None
                self.send_json(200, board_get(path, parse_qs(parsed.query), viewer["userId"] if viewer else None))
            elif path == "/internal/admin/board":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    query = parse_qs(parsed.query)
                    with board_db() as connection:
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
            elif path == "/internal/me/dividend-payments":
                user = identity(internal_user_id(self))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"payments": user_dividend_payments(user["userId"])})
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
                    self.send_json(200, {"users": admin_users(query.get("search", [""])[0], kind)})
            elif path == "/internal/admin/events":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, {"events": admin_events(), "flows": admin_market_flows()})
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
                    since = float(raw_since) if raw_since else None
                    if raw_since and (not math.isfinite(since) or since < 0):
                        raise ValueError("invalid_since_epoch")
                    if not issuer or len(issuer) > 100 or (currency and len(currency) > 12):
                        raise ValueError("invalid_bank_journal_filter")
                    self.send_json(200, admin_bank_journal_report(issuer, currency, since))
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
                    self.send_json(200, admin_dr_movements(limit))
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
                    self.send_json(200, admin_compliance_overview(nationality))
            elif path == "/internal/admin/commands":
                user = identity(internal_user_id(self))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, admin_command_catalog())
            elif path.startswith("/internal/admin/") and path.split("/")[3] in {
                    "products", "countries", "exchanges", "help", "bot-jobs", "user", "companies", "warnings", "feedback"}:
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
                    self.send_json(200, admin_user_detail(parts[1]))
                elif parts == ["companies"]:
                    self.send_json(200, admin_companies())
                elif parts == ["companies", "identity"]:
                    self.send_json(200, admin_identity(user["userId"]))
                elif len(parts) == 3 and parts[0] == "companies" and parts[2] == "financial":
                    self.send_json(200, admin_company_financial(parts[1], query.get("period", [None])[0]))
                elif parts == ["warnings"]:
                    self.send_json(200, admin_warnings())
                elif parts == ["feedback"]:
                    self.send_json(200, admin_feedback(query.get("status", [None])[0]))
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
                result = chart(code, range_key, int(raw_interval) if raw_interval else None)
                if result is None:
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    self.send_json(200, result)
            else:
                self.send_json(404, {"error": "not_found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except (sqlite3.Error, RuntimeError):
            self.send_json(503, {"error": "database_unavailable"})

    def do_POST(self):
        if not INTERNAL_TOKEN or not hmac.compare_digest(
            self.headers.get("x-inazina-internal-token", ""), INTERNAL_TOKEN
        ):
            self.send_json(401, {"error": "internal_authentication_required"})
            return
        try:
            payload = request_json(self)
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
                success, message = DepositaryReceiptManager(bot.db).convert(
                    user["userId"], payload["drCode"], payload["action"],
                    payload["quantity"], payload["referenceKey"],
                )
                self.send_json(200 if success else 400, {"success": success, "message": message})
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
            if path.startswith("/internal/admin/board/"):
                parts = path.removeprefix("/internal/admin/board/").split("/")
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                elif len(parts) != 2:
                    self.send_json(404, {"error": "not_found"})
                else:
                    message = admin_board_action(user["userId"], parts[0], parts[1], payload.get("action"))
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
            elif path.startswith("/internal/admin/") and path.split("/")[3] in {
                    "products", "countries", "exchanges", "user", "companies", "warnings", "feedback", "logs"}:
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                    return
                parts = [unquote(part) for part in path.split("/")[3:]]
                actor = user["userId"]
                if parts == ["products"]:
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
                elif len(parts) == 2 and parts[0] == "companies":
                    message = admin_company_command(actor, parts[1], payload)
                elif len(parts) == 2 and parts[0] == "warnings":
                    message = admin_warning_action(actor, parts[1], payload)
                elif len(parts) == 3 and parts[0] == "logs" and parts[2] == "delete":
                    message = admin_log_delete(actor, parts[1], payload)
                elif parts == ["feedback", "delete"]:
                    message = admin_feedback_delete(actor, payload)
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
        except sqlite3.Error:
            self.send_json(503, {"error": "database_unavailable"})

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
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
    try:
        domain()
    except KeyboardInterrupt:
        print("INAZINA Python data service stopped during startup.", flush=True)
        return
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
