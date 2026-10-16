"""Administrator trading warnings (투자주의 · 투자경고 · 투자위험) shown wherever an instrument is.

A warning is a market notice: it does not halt trading. Discord views, the HTS,
the dashboard and order confirmations show it until it is cleared or expires.
"""
import time

LEVELS = {"caution": "투자주의", "warning": "투자경고", "danger": "투자위험"}
ALIASES = {"주의": "caution", "투자주의": "caution", "caution": "caution",
           "경고": "warning", "투자경고": "warning", "warning": "warning",
           "위험": "danger", "투자위험": "danger", "danger": "danger"}
CLEAR = {"해제", "clear", "off", "없음"}
ICONS = {"caution": "🟡", "warning": "🟠", "danger": "🔴"}


class TradingWarnings:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def active(conn, now=None):
        now = time.time() if now is None else now
        return {str(row["asset_code"]): dict(row) for row in conn.execute(
            "SELECT * FROM trading_warnings WHERE expires_epoch IS NULL OR expires_epoch>?", (now,))}

    @staticmethod
    def of(conn, code, now=None):
        now = time.time() if now is None else now
        row = conn.execute("SELECT * FROM trading_warnings WHERE asset_code=? AND (expires_epoch IS NULL OR expires_epoch>?)",
                           (str(code), now)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def label(warning, *, reason=True):
        if not warning:
            return ""
        text = f"{ICONS[warning['level']]} {LEVELS[warning['level']]}"
        return text + (f" · {warning['reason']}" if reason and warning.get("reason") else "")

    def set(self, actor, code, level, reason="", days=None):
        """Designate (or with 해제, clear) a warning on any listed instrument."""
        code = str(code).strip()
        reason = str(reason or "").strip()[:200]
        now = time.time()
        with self.db.atomic() as conn:
            user = conn.execute("SELECT role FROM users WHERE user_id=?", (str(actor),)).fetchone()
            if not user or str(user["role"]).strip().lower() != "admin":
                raise ValueError("DB role=admin인 관리자만 사용할 수 있습니다.")
            if str(level).strip() in CLEAR:
                removed = conn.execute("DELETE FROM trading_warnings WHERE asset_code=?", (code,)).rowcount
                self._audit(conn, actor, "warning.clear", {"code": code})
                return f"✅ `{code}` 거래경고를 해제했습니다." if removed else f"ℹ️ `{code}`에는 거래경고가 없습니다."
            key = ALIASES.get(str(level).strip())
            if not key:
                raise ValueError("단계는 주의·경고·위험 또는 해제입니다.")
            if not self.db.code_locations(conn, code):
                raise ValueError("존재하지 않는 종목·상품 코드입니다.")
            expires = None
            if days not in (None, ""):
                days = float(days)
                if not 0 < days <= 3650:
                    raise ValueError("경고 기간은 0보다 크고 3650일 이하입니다.")
                expires = now + days * 86400
            conn.execute(
                "INSERT INTO trading_warnings(asset_code,level,reason,set_by,created_epoch,expires_epoch) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(asset_code) DO UPDATE SET level=excluded.level,reason=excluded.reason,set_by=excluded.set_by,"
                "created_epoch=excluded.created_epoch,expires_epoch=excluded.expires_epoch",
                (code, key, reason, str(actor), now, expires))
            self._audit(conn, actor, "warning.set", {"code": code, "level": key, "reason": reason, "expires": expires})
        until = f" · <t:{int(expires)}:f>까지" if expires else ""
        return f"✅ `{code}`을(를) {ICONS[key]} {LEVELS[key]} 종목으로 지정했습니다{until}." + (f" 사유: {reason}" if reason else "")

    def list(self):
        with self.db.readonly() as conn:
            return sorted(self.active(conn).values(), key=lambda row: (-list(LEVELS).index(row["level"]), row["asset_code"]))

    def notice(self, code):
        """A line to append to an order confirmation for a warned instrument."""
        with self.db.readonly() as conn:
            warning = self.of(conn, code)
        return f"\n⚠️ 이 종목은 {self.label(warning)} 종목입니다. 투자에 유의하세요." if warning else ""

    @staticmethod
    def _audit(conn, actor, action, payload):
        import json
        conn.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
                     (str(actor), action, json.dumps(payload, ensure_ascii=False), time.time()))
