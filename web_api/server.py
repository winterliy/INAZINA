"""Private, read-only HTTP adapter for the existing SQLite database."""

import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE = Path(os.environ.get("INAZINA_DB_PATH", ROOT / "database" / "inazina.db")).resolve()
INTERNAL_TOKEN = os.environ.get("INAZINA_INTERNAL_TOKEN", "")
PORT = int(os.environ.get("INAZINA_PYTHON_PORT", "4313"))
BOOTSTRAP_ADMIN_IDS = {"1076861147560542278"}


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
        assets = connection.execute(
            "SELECT code,name,type AS kind,price,exchange_id FROM market_assets "
            "WHERE status='active' ORDER BY code"
        ).fetchall()
        companies = connection.execute(
            "SELECT company_code AS code,name,share_class AS kind,share_price AS price,exchange_id "
            "FROM company WHERE status='active' ORDER BY company_code"
        ).fetchall()
    rows = [dict(row) for row in (*assets, *companies)]
    return sorted(rows, key=lambda row: row["code"])


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


class Handler(BaseHTTPRequestHandler):
    server_version = "InazinaInternal/1.0"

    def do_GET(self):
        if not INTERNAL_TOKEN or not hmac.compare_digest(
            self.headers.get("x-inazina-internal-token", ""), INTERNAL_TOKEN
        ):
            self.send_json(401, {"error": "internal_authentication_required"})
            return

        try:
            if self.path == "/internal/health":
                with connect_readonly() as connection:
                    connection.execute("SELECT 1").fetchone()
                self.send_json(200, {"status": "ok", "service": "inazina-python-data"})
            elif self.path == "/internal/me":
                user_id = self.headers.get("x-inazina-user-id", "")
                user = identity(user_id)
                if not user:
                    self.send_json(403, {"error": "active_user_required"})
                else:
                    self.send_json(200, {"user": user})
            elif self.path == "/internal/market/instruments":
                self.send_json(200, {"instruments": instruments()})
            elif self.path == "/internal/admin/overview":
                user = identity(self.headers.get("x-inazina-user-id", ""))
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "admin_required"})
                else:
                    self.send_json(200, admin_overview())
            else:
                self.send_json(404, {"error": "not_found"})
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
