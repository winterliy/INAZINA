"""Private, read-only HTTP adapter for the existing SQLite database."""

import hmac
import hashlib
import json
import math
import os
import re
import secrets
import re
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
DATABASE = Path(os.environ.get("INAZINA_DB_PATH", ROOT / "database" / "inazina.db")).resolve()
INTERNAL_TOKEN = os.environ.get("INAZINA_INTERNAL_TOKEN", "")
PORT = int(os.environ.get("INAZINA_PYTHON_PORT", "4313"))
BOOTSTRAP_ADMIN_IDS = {"1076861147560542278"}
sys.path.insert(0, str(ROOT))
_domain = None


def connect_readonly():
    uri = DATABASE.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


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
        auth_version = connection.execute(
            "SELECT auth_version FROM web_auth_credentials WHERE user_id=?", (user_id,),
        ).fetchone()[0] if auth_table else 0
    if not row or row["status"] != "active":
        return None
    role = "admin" if user_id in BOOTSTRAP_ADMIN_IDS else str(row["role"] or "user").strip().lower()
    if role not in {"admin", "user"}:
        role = "user"
    return {
        "userId": str(row["user_id"]),
        "nickname": str(row["nickname"] or row["user_id"]),
        "nationality": str(row["nationality"] or ""),
        "cash": int(row["cash"] or 0),
        "creditScore": int(row["credit_score"] or 0),
        "role": role,
        "authVersion": int(auth_version),
    }


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


def ensure_auth_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS web_auth_credentials (
        user_id TEXT PRIMARY KEY, salt TEXT NOT NULL, password_hash TEXT NOT NULL,
        created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL,
        auth_version INTEGER NOT NULL DEFAULT 0)""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(web_auth_credentials)")}
    if "auth_version" not in columns:
        connection.execute("ALTER TABLE web_auth_credentials ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0")


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
        with DBManager.atomic(("users", "fx", "system")) as connection:
            ensure_auth_table(connection)
            connection.execute("""CREATE TABLE IF NOT EXISTS admin_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                action TEXT NOT NULL, payload TEXT NOT NULL, recorded_epoch REAL NOT NULL)""")
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
                if existing_user["role"] != "user":
                    raise ValueError("country_account_conflict")
                if connection.execute(
                    "SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,),
                ).fetchone():
                    raise ValueError("country_account_exists")
                cash = int(existing_user["cash"] or 0)
            else:
                connection.execute(
                    "INSERT INTO users(user_id,nickname,nationality,cash,role) VALUES(?,?,?,?, 'user')",
                    (user_id, f"{country_id} 국가 계정", country_id, 1_000_000),
                )
                cash = 1_000_000
            connection.execute(
                "INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch) "
                "VALUES(?,?,?,?,?)", (user_id, salt, digest, now, now),
            )
            connection.execute(
                "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES(?,?,?,?)",
                (str(actor_id), "country_account.create", json.dumps({"countryId": country_id}, ensure_ascii=False), now),
            )
    return {"countryId": country_id, "userId": user_id, "cash": cash, "status": "active"}


def save_password(user_id, password, *, create=False, nickname="", country_id=""):
    if not isinstance(user_id, str) or not user_id.isdecimal():
        raise ValueError("discord_identity_invalid")
    validate_password(password)
    db = domain().db
    with db.atomic(("users", "fx")) as connection:
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
            elif user["status"] != "active":
                raise ValueError("active_user_required")
            elif connection.execute(
                "SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,),
            ).fetchone():
                raise ValueError("account_exists")
        elif not connection.execute(
            "SELECT 1 FROM users WHERE user_id=? AND status='active'", (user_id,),
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
    with sqlite3.connect(DATABASE, timeout=5) as connection:
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


def instruments(include_inactive=False):
    status_clause = "" if include_inactive else " WHERE status='active'"
    with connect_readonly() as connection:
        rows = []
        for query in (
            f"SELECT code,name,type,price,exchange_id,quote_currency,status,'market_assets' AS source FROM market_assets{status_clause}",
            f"SELECT company_code AS code,name,'stock' AS type,share_price AS price,exchange_id,quote_currency,status,'company' AS source FROM company{status_clause}",
            f"SELECT product_code AS code,name,product_type AS type,unit_price AS price,exchange_id,quote_currency,status,'investment' AS source FROM investment{status_clause}",
            f"SELECT product_code AS code,name,'국채' AS type,market_price AS price,exchange_id,'NWX' AS quote_currency,status,'treasury_bond' AS source FROM treasury_bond_products{status_clause}",
        ):
            rows.extend(dict(row) for row in connection.execute(query).fetchall())
    return sorted(rows, key=lambda row: (str(row["name"]).casefold(), row["code"]))


def domain():
    global _domain
    if _domain is None:
        from database import DatabaseManager
        from managers.admin_control_manager import AdminControlManager
        from managers.futures_manager import FuturesManager
        from managers.market_manager import MarketManager
        from managers.market_session_manager import MarketSessionManager
        from managers.option_manager import OptionManager
        from managers.order_book_manager import OrderBookManager
        from managers.user_manager import UserManager

        db = DatabaseManager()
        user_manager = UserManager(db)
        order_book = OrderBookManager(db, user_manager)
        option_manager = OptionManager(db, user_manager)
        futures_manager = FuturesManager(db, user_manager)
        order_book.option_manager = option_manager
        order_book.futures_manager = futures_manager
        from managers.admin_manager import AdminManager

        bot = SimpleNamespace(
            db=db, user_mgr=user_manager, order_book=order_book,
            option_mgr=option_manager, futures_mgr=futures_manager,
        )
        bot.admin_control = AdminControlManager(bot)
        bot.admin_mgr = AdminManager(db)
        _domain = SimpleNamespace(
            db=db, users=user_manager, order_book=order_book,
            options=option_manager, futures=futures_manager,
            market=MarketManager(db), admin_control=bot.admin_control,
            market_session=MarketSessionManager(db),
            admin_manager=bot.admin_mgr,
            bot=bot,
        )
    return _domain


def admin_overview():
    with connect_readonly() as connection:
        active_users = connection.execute(
            "SELECT COUNT(*) FROM users WHERE status='active' AND user_id<>'__treasury__'"
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
        session = connection.execute(
            "SELECT status,reason,trading_halted,updated_epoch FROM market_session WHERE session_id=1"
        ).fetchone()
    return {
        "activeUsers": int(active_users),
        "activeInstruments": int(active_assets),
        "openOrders": int(open_orders),
        "market": dict(session) if session else None,
    }


def portfolio(user_id):
    return domain().users.get_total_assets(user_id, settle=False)


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


def chart(code, range_key):
    ranges = {"1h": 3600, "1d": 86400, "1w": 604800, "1m": 2592000, "all": None}
    if range_key not in ranges:
        raise ValueError("unsupported_chart_range")
    item = next((row for row in instruments() if row["code"] == code), None)
    if not item:
        return None
    start = 0 if ranges[range_key] is None else int(time.time()) - ranges[range_key]
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT recorded_epoch,price FROM market_price_history "
            "WHERE asset_code=? AND recorded_epoch>=? ORDER BY recorded_epoch DESC LIMIT 5000",
            (code, start),
        ).fetchall()
    points = [{"time": int(row["recorded_epoch"]), "price": float(row["price"])}
              for row in reversed(rows)]
    if len(points) > 1200:
        stride = math.ceil(len(points) / 1200)
        points = points[::stride] + ([points[-1]] if points[-1] != points[::stride][-1] else [])
    return {"instrument": item, "points": points, "range": range_key}


def save_drawings(user_id, code, drawings):
    if not any(row["code"] == code for row in instruments()):
        return False
    if not isinstance(drawings, list) or len(drawings) > 100:
        raise ValueError("drawings must contain at most 100 items")
    valid = []
    for drawing in drawings:
        if not isinstance(drawing, dict) or drawing.get("type") not in {"trend", "horizontal"}:
            raise ValueError("invalid drawing type")
        anchors = drawing.get("anchors")
        expected = 2 if drawing["type"] == "trend" else 1
        if not isinstance(anchors, list) or len(anchors) != expected:
            raise ValueError("invalid drawing anchors")
        clean = []
        for anchor in anchors:
            if not isinstance(anchor, dict):
                raise ValueError("invalid drawing anchor")
            epoch, price = anchor.get("time"), anchor.get("price")
            if (isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or
                    isinstance(price, bool) or not isinstance(price, (int, float)) or
                    not math.isfinite(epoch) or not math.isfinite(price) or epoch < 0 or price <= 0):
                raise ValueError("invalid drawing coordinates")
            clean.append({"time": int(epoch), "price": float(price)})
        valid.append({"id": str(drawing.get("id", ""))[:80], "type": drawing["type"], "anchors": clean})

    with sqlite3.connect(DATABASE, timeout=10) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("""CREATE TABLE IF NOT EXISTS chart_drawings (
            user_id TEXT NOT NULL, asset_code TEXT NOT NULL, drawings_json TEXT NOT NULL,
            updated_epoch INTEGER NOT NULL, PRIMARY KEY(user_id,asset_code))""")
        connection.execute(
            "INSERT INTO chart_drawings(user_id,asset_code,drawings_json,updated_epoch) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,asset_code) DO UPDATE SET drawings_json=excluded.drawings_json, "
            "updated_epoch=excluded.updated_epoch",
            (user_id, code, json.dumps(valid, separators=(",", ":")), int(time.time())),
        )
    return True


def get_drawings(user_id, code):
    with connect_readonly() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chart_drawings'"
        ).fetchone()
        if not exists:
            return []
        row = connection.execute(
            "SELECT drawings_json FROM chart_drawings WHERE user_id=? AND asset_code=?",
            (user_id, code),
        ).fetchone()
    return json.loads(row["drawings_json"]) if row else []


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
    return domain().market_session.control(actor, action, stage=payload.get("stage"))


def set_instrument_status(actor, code, payload):
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"active", "inactive"}:
        raise ValueError("status must be active or inactive")
    item = next((row for row in instruments(include_inactive=True) if row["code"] == code), None)
    if not item:
        return False, "자산 코드를 찾을 수 없습니다."
    category = {
            "company": "stock", "market_assets": "asset", "investment": {
            "mutual_fund": "fund", "index_fund": "indexfund", "stock_future": "future",
            "corporate_bond": "corporatebond", "commodity_spot": "commodity", "etf": "etf",
            "els": "els",
        }.get(item.get("type")),
        "treasury_bond": "bond", "option_contracts": "option",
    }.get(item.get("source"))
    if not category:
        return False, "이 상품 유형의 상태 변경은 아직 지원하지 않습니다."
    result = domain().admin_control.entity(
        actor, category, "restore" if status == "active" else "delete", code,
    )
    return True, result


def edit_instrument(actor, code, payload):
    if not isinstance(payload, dict) or set(payload) - {"name", "price", "supply"}:
        raise ValueError("상품 이름, 가격, 발행량만 수정할 수 있습니다.")
    item = next((row for row in instruments(include_inactive=True) if row["code"] == code), None)
    if not item:
        return False, "자산 코드를 찾을 수 없습니다."
    category = {
        "company": "stock", "market_assets": "asset",
        "investment": {
            "mutual_fund": "fund", "index_fund": "indexfund",
            "stock_future": "future", "corporate_bond": "corporatebond",
            "commodity_spot": "commodity", "etf": "etf", "els": "els",
        }.get(item.get("type")),
        "treasury_bond": "bond",
    }.get(item.get("source"))
    if not category:
        return False, "이 상품 유형의 설정 변경은 아직 지원하지 않습니다."
    result = domain().admin_control.entity(
        actor, category, "edit", code,
        name=payload.get("name", ""), price=payload.get("price", ""),
        supply=payload.get("supply", ""),
    )
    return True, result


def admin_users(search):
    term = f"%{search.strip()}%"
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT user_id,nickname,nationality,cash,credit_score,status,role, "
            "CASE WHEN user_id=nationality AND EXISTS(SELECT 1 FROM country_currencies c "
            "WHERE c.nationality=users.user_id) THEN 'country' ELSE 'discord' END AS account_type "
            "FROM users WHERE user_id<>'__treasury__' AND (user_id LIKE ? OR nickname LIKE ?) "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,nickname LIMIT 100",
            (term, term),
        ).fetchall()
    return [{"userId": row["user_id"], "nickname": row["nickname"],
             "nationality": row["nationality"], "cash": int(row["cash"] or 0),
             "creditScore": int(row["credit_score"] or 0), "status": row["status"],
             "role": row["role"], "accountType": row["account_type"]} for row in rows]


def admin_events():
    with connect_readonly() as connection:
        rows = connection.execute(
            "SELECT event_id,asset_code,event_type,severity,starts_epoch,ends_epoch,created_by,"
            "target_mode,target_value,volatility_percent FROM market_events "
            "ORDER BY event_id DESC LIMIT 50"
        ).fetchall()
    return [dict(row) for row in rows]


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


class Handler(BaseHTTPRequestHandler):
    server_version = "InazinaInternal/1.0"

    def do_GET(self):
        if not INTERNAL_TOKEN or not hmac.compare_digest(
            self.headers.get("x-inazina-internal-token", ""), INTERNAL_TOKEN
        ):
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
                user_id = self.headers.get("x-inazina-user-id", "")
                user = identity(user_id)
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"user": user})
            elif path == "/internal/market/instruments":
                self.send_json(200, {"instruments": instruments()})
            elif path == "/internal/market/portfolio":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"portfolio": portfolio(user["userId"])})
            elif path == "/internal/admin/overview":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, admin_overview())
            elif path == "/internal/admin/instruments":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    include_inactive = parse_qs(parsed.query).get("includeInactive", ["0"])[0] == "1"
                    self.send_json(200, {"instruments": instruments(include_inactive)})
            elif path == "/internal/admin/users":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    search = parse_qs(parsed.query).get("search", [""])[0]
                    self.send_json(200, {"users": admin_users(search)})
            elif path == "/internal/admin/events":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, {"events": admin_events()})
            elif path == "/internal/admin/audit":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    with connect_readonly() as connection:
                        table = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_audit'"
                        ).fetchone()
                        rows = connection.execute(
                            "SELECT actor_id,action,payload,recorded_epoch FROM admin_audit "
                            "ORDER BY recorded_epoch DESC LIMIT 50"
                        ).fetchall() if table else []
                    audit = []
                    for row in rows:
                        try:
                            payload = json.loads(row["payload"])
                        except (TypeError, json.JSONDecodeError):
                            payload = {}
                        audit.append({"actorId": row["actor_id"], "action": row["action"],
                                      "payload": payload, "time": row["recorded_epoch"]})
                    self.send_json(200, {"entries": audit})
            elif path == "/internal/market/orders":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    manager = domain().order_book
                    orders = [dict(row) for row in manager.get_user_orders(user["userId"], limit=50)]
                    self.send_json(200, {"orders": orders})
            elif path.startswith("/internal/market/book/"):
                code = path.removeprefix("/internal/market/book/")
                result = book(code)
                if result is None:
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    self.send_json(200, result)
            elif path.startswith("/internal/market/chart/"):
                code = path.removeprefix("/internal/market/chart/")
                range_key = parse_qs(parsed.query).get("range", ["1d"])[0]
                result = chart(code, range_key)
                if result is None:
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    self.send_json(200, result)
            elif path.startswith("/internal/market/drawings/"):
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    code = path.removeprefix("/internal/market/drawings/")
                    if not any(row["code"] == code for row in instruments()):
                        self.send_json(404, {"error": "instrument_not_found"})
                    else:
                        self.send_json(200, {"drawings": get_drawings(user["userId"], code)})
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
            verified_id = self.headers.get("x-inazina-user-id", "")
            if path == "/internal/auth/login":
                account_type = payload.get("accountType", "discord")
                if account_type == "country":
                    country_id = str(payload.get("countryId", "")).strip()
                    if not country_id or len(country_id) > 80:
                        self.send_json(401, {"error": "invalid_credentials"})
                        return
                    with connect_readonly() as connection:
                        credential_table = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_auth_credentials'"
                        ).fetchone()
                        has_account = connection.execute(
                            "SELECT 1 FROM users u JOIN country_currencies c ON c.nationality=u.user_id "
                            "JOIN web_auth_credentials w ON w.user_id=u.user_id "
                            "WHERE u.user_id=? AND u.nationality=? AND u.status='active'",
                            (country_id, country_id),
                        ).fetchone() if credential_table else None
                    if not has_account:
                        self.send_json(404, {"error": "account_not_found"})
                        return
                    user = authenticate_country_password(country_id, payload.get("password"))
                elif account_type == "discord":
                    user_id = str(payload.get("userId", "")).strip()
                    if not user_id.isdecimal():
                        self.send_json(401, {"error": "invalid_credentials"})
                        return
                    with sqlite3.connect(DATABASE, timeout=5) as connection:
                        ensure_auth_table(connection)
                        connection.row_factory = sqlite3.Row
                        has_account = connection.execute(
                            "SELECT 1 FROM web_auth_credentials WHERE user_id=?", (user_id,),
                        ).fetchone()
                    if not has_account:
                        self.send_json(404, {"error": "account_not_found"})
                        return
                    user = authenticate_discord_password(user_id, payload.get("password"))
                else:
                    self.send_json(401, {"error": "invalid_credentials"})
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
                with sqlite3.connect(DATABASE, timeout=5) as connection:
                    ensure_auth_table(connection)
                    connection.execute("UPDATE web_auth_credentials SET auth_version=auth_version+1 WHERE user_id=?", (verified_id,))
                self.send_json(200, {"success": True})
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
            if path == "/internal/market/orders":
                success, message = execute_order(user["userId"], payload)
                self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path.startswith("/internal/market/orders/") and path.endswith("/cancel"):
                order_id = int(path.removeprefix("/internal/market/orders/").removesuffix("/cancel"))
                success, message = cancel_order(user["userId"], order_id)
                self.send_json(200 if success else 400, {"success": success, "message": message})
            elif path.startswith("/internal/market/drawings/"):
                code = path.removeprefix("/internal/market/drawings/")
                if not save_drawings(user["userId"], code, payload.get("drawings")):
                    self.send_json(404, {"error": "instrument_not_found"})
                else:
                    self.send_json(200, {"success": True})
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
        except sqlite3.Error:
            self.send_json(503, {"error": "database_unavailable"})

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        return


if __name__ == "__main__":
    if not INTERNAL_TOKEN:
        raise SystemExit("INAZINA_INTERNAL_TOKEN is required")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"INAZINA Python data service listening at http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
