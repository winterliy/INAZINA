"""Naturalization requests: a citizen asks (Discord /귀화신청), an administrator decides.

Asking changes nothing. Approval runs :class:`NationalityManager.change`, which
liquidates the citizen's holdings, bonds, accounts and loans and moves the cash
into the new home currency; that step cannot be undone, and a request cannot be
withdrawn once made.
"""
import time

from managers.nationality_manager import NationalityManager

STATUS = {"pending": "심사 대기", "approved": "승인", "rejected": "거절"}


class NaturalizationManager:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.db.users.execute(
            "CREATE TABLE IF NOT EXISTS naturalization_requests ("
            "request_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            "from_nationality TEXT NOT NULL, to_nationality TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
            "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),"
            "requested_epoch REAL NOT NULL, decided_by TEXT, decided_epoch REAL, note TEXT)")
        self.db.users.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS naturalization_one_pending "
            "ON naturalization_requests(user_id) WHERE status='pending'")

    def check(self, user_id, nationality):
        """The citizen and target country, or a ValueError saying why a request is not possible."""
        user_id, nationality = str(user_id), str(nationality).strip()
        with self.db.readonly() as conn:
            user = conn.execute("SELECT nickname,nationality,role FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not user or user["role"] in {"company", "nation"}:
                raise ValueError("등록된 시민만 귀화를 신청할 수 있습니다.")
            if not conn.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (nationality,)).fetchone():
                raise ValueError("통화와 연결된 국가를 선택해주세요.")
            if user["nationality"] == nationality:
                raise ValueError("이미 그 나라 국적입니다.")
            if conn.execute("SELECT 1 FROM naturalization_requests WHERE user_id=? AND status='pending'", (user_id,)).fetchone():
                raise ValueError("심사 중인 귀화 신청이 이미 있습니다.")
            blockers = NationalityManager(self.bot).blockers(conn, user_id)
        return dict(user), blockers

    def preview(self, user_id):
        """What approval would liquidate, in the citizen's own words."""
        assets = self.bot.user_mgr.get_total_assets(str(user_id), settle=False)
        stocks = [item for item in assets.get("stocks", []) if item.get("qty")]
        return {
            "holdings": len(stocks), "bonds": len(assets.get("bonds", [])),
            "accounts": len(assets.get("accounts", [])), "loans": int(assets.get("loan_total") or 0),
            "netWorth": int(assets.get("net_worth") or 0), "currency": assets.get("national_currency") or "",
        }

    def request(self, user_id, nationality, reason=""):
        user, _ = self.check(user_id, nationality)
        with self.db.atomic() as conn:
            cursor = conn.execute(
                "INSERT INTO naturalization_requests(user_id,from_nationality,to_nationality,reason,requested_epoch) "
                "VALUES (?,?,?,?,?)", (str(user_id), user["nationality"], str(nationality).strip(), str(reason or "")[:300], time.time()))
            return cursor.lastrowid

    def requests(self, limit=100):
        with self.db.readonly() as conn:
            rows = conn.execute(
                "SELECT r.*,u.nickname FROM naturalization_requests r LEFT JOIN users u ON u.user_id=r.user_id "
                "ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.request_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def decide(self, actor, request_id, approve, note=""):
        control = self.bot.admin_control
        control.require(actor)
        with self.db.readonly() as conn:
            row = conn.execute("SELECT * FROM naturalization_requests WHERE request_id=?", (int(request_id),)).fetchone()
        if not row or row["status"] != "pending":
            raise ValueError("심사 대기 중인 귀화 신청 번호를 입력해주세요.")
        # Approval liquidates first; if that fails the request stays pending.
        moved = NationalityManager(self.bot).change(actor, row["user_id"], row["to_nationality"]) if approve else ""
        status = "approved" if approve else "rejected"
        with self.db.atomic() as conn:
            conn.execute("UPDATE naturalization_requests SET status=?,decided_by=?,decided_epoch=?,note=? WHERE request_id=?",
                         (status, str(actor), time.time(), str(note or "")[:300], row["request_id"]))
            control._audit(conn, actor, f"naturalization.{status}", {"request_id": row["request_id"], "user_id": row["user_id"],
                                                                    "to": row["to_nationality"], "note": note})
        if approve:
            return f"✅ 귀화 신청 #{row['request_id']}을 승인했습니다. {moved}"
        return f"✅ 귀화 신청 #{row['request_id']}을 거절했습니다."
