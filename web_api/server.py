"""Private, read-only HTTP adapter for the existing SQLite database."""

import hmac
import json
import math
import os
import re
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

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
    }


def instruments():
    with connect_readonly() as connection:
        rows = []
        for query in (
            "SELECT code,name,type,price,exchange_id,quote_currency,'market_assets' AS source "
            "FROM market_assets WHERE status='active'",
            "SELECT company_code AS code,name,'stock' AS type,share_price AS price,exchange_id,quote_currency,'company' AS source "
            "FROM company WHERE status='active'",
            "SELECT product_code AS code,name,product_type AS type,unit_price AS price,exchange_id,quote_currency,'investment' AS source "
            "FROM investment WHERE status='active'",
            "SELECT product_code AS code,name,'국채' AS type,market_price AS price,exchange_id,'NWX' AS quote_currency,'treasury_bond' AS source "
            "FROM treasury_bond_products WHERE status='active'",
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
        bot = SimpleNamespace(
            db=db, user_mgr=user_manager, order_book=order_book,
            option_mgr=option_manager, futures_mgr=futures_manager,
        )
        bot.admin_control = AdminControlManager(bot)
        _domain = SimpleNamespace(
            db=db, users=user_manager, order_book=order_book,
            options=option_manager, futures=futures_manager,
            market=MarketManager(db), admin_control=bot.admin_control,
            market_session=MarketSessionManager(db),
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
            "(SELECT COUNT(*) FROM company WHERE status='active')"
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
    item = next((row for row in instruments() if row["code"] == code), None)
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
                    self.send_json(200, {"instruments": instruments()})
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
        user = identity(self.headers.get("x-inazina-user-id", ""))
        if not user:
            self.send_json(403, {"error": "active_user_required"})
            return
        try:
            payload = request_json(self)
            path = urlsplit(self.path).path
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
            elif path.startswith("/internal/admin/instruments/") and path.endswith("/status"):
                if user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    code = path.removeprefix("/internal/admin/instruments/").removesuffix("/status")
                    success, message = set_instrument_status(user["userId"], code, payload)
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
