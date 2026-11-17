"""Bell notifications: one inbox row per event, delivered on the web and (once) to Discord."""
import json
import time
from datetime import datetime, timezone

DAY = 86400
SOON_DAYS = 7


class NotificationManager:
    MAX_LIST = 100

    @staticmethod
    def add(conn, user_id, kind, title, body="", *, action_path=None, payload=None, dedupe_key=None,
            now=None, discord_sent=False):
        """Record a notification; returns its id, or None when ``dedupe_key`` was already used.

        ``discord_sent`` marks events whose Discord message the caller already sent itself."""
        now = int(now if now is not None else time.time())
        cursor = conn.execute(
            "INSERT OR IGNORE INTO notifications(user_id,kind,title,body,action_path,payload_json,dedupe_key,"
            "created_epoch,discord_sent_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(user_id), str(kind), str(title), str(body or ""), action_path,
             json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), dedupe_key, now,
             now if discord_sent else None))
        return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    @staticmethod
    def unread_count(conn, user_id):
        return int(conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_epoch IS NULL",
                                (str(user_id),)).fetchone()[0])

    @classmethod
    def list(cls, conn, user_id, limit=None):
        rows = conn.execute(
            "SELECT notification_id,kind,title,body,action_path,payload_json,created_epoch,read_epoch "
            "FROM notifications WHERE user_id=? ORDER BY notification_id DESC LIMIT ?",
            (str(user_id), int(limit or cls.MAX_LIST))).fetchall()
        return [{"id": int(row[0]), "kind": row[1], "title": row[2], "body": row[3], "action_path": row[4],
                 "payload": json.loads(row[5] or "{}"), "created_epoch": int(row[6]),
                 "read": row[7] is not None} for row in rows]

    @staticmethod
    def mark_all_read(conn, user_id, now=None):
        """Opening the panel reads everything in it."""
        return conn.execute("UPDATE notifications SET read_epoch=? WHERE user_id=? AND read_epoch IS NULL",
                            (int(now if now is not None else time.time()), str(user_id))).rowcount

    @staticmethod
    def delete(conn, user_id, notification_id):
        return conn.execute("DELETE FROM notifications WHERE user_id=? AND notification_id=?",
                            (str(user_id), int(notification_id))).rowcount == 1

    @staticmethod
    def delete_all(conn, user_id):
        return conn.execute("DELETE FROM notifications WHERE user_id=?", (str(user_id),)).rowcount

    @staticmethod
    def pending_discord(conn, limit=50):
        """Notifications whose Discord message has not been sent yet, oldest first."""
        return [dict(row) for row in conn.execute(
            "SELECT notification_id,user_id,kind,title,body,action_path FROM notifications "
            "WHERE discord_sent_epoch IS NULL ORDER BY notification_id LIMIT ?", (int(limit),)).fetchall()]

    @staticmethod
    def mark_discord_sent(conn, notification_ids, now=None):
        now = int(now if now is not None else time.time())
        for notification_id in notification_ids:
            conn.execute("UPDATE notifications SET discord_sent_epoch=? WHERE notification_id=?",
                         (now, int(notification_id)))

    @classmethod
    def scan_due(cls, conn, now=None):
        """Raise reminders for things that become due: loan maturity and overdue, deposit maturity.

        Each reminder has a dedupe key, so scanning again never repeats one. Returns how many were added."""
        now = int(now if now is not None else time.time())
        soon = now + SOON_DAYS * DAY
        added = 0

        def note(user_id, kind, title, body, key, path=None):
            nonlocal added
            if cls.add(conn, user_id, kind, title, body, action_path=path, dedupe_key=key, now=now):
                added += 1

        for loan_id, user_id, product, maturity in conn.execute(
                "SELECT loan_id,user_id,product_code,maturity_epoch FROM loans WHERE status='active' "
                "AND maturity_epoch IS NOT NULL AND maturity_epoch>? AND maturity_epoch<=?", (now, soon)).fetchall():
            note(user_id, "loan_maturity", "대출 만기가 7일 이내입니다",
                 f"대출 #{loan_id}({product})의 만기가 {datetime.fromtimestamp(float(maturity), timezone.utc):%Y-%m-%d}입니다. "
                 "만기까지 갚지 않으면 연체됩니다.", f"loan:{loan_id}:maturity-soon")
        for loan_id, user_id, product, overdue in conn.execute(
                "SELECT loan_id,user_id,product_code,overdue_since_epoch FROM loans WHERE status='active' "
                "AND overdue_since_epoch IS NOT NULL").fetchall():
            note(user_id, "loan_overdue", "대출이 연체되었습니다",
                 f"대출 #{loan_id}({product})가 연체 중입니다. 신용점수가 내려가고 연체 이자가 붙습니다.",
                 f"loan:{loan_id}:overdue:{int(overdue)}")
        for number, user_id, product, created, days in conn.execute(
                "SELECT a.account_number,a.user_id,a.product_code,a.created_at,s.maturity_days FROM accounts a "
                "JOIN saving s ON s.product_code=a.product_code WHERE a.user_id IS NOT NULL "
                "AND COALESCE(s.maturity_days,0)>0 AND a.account_type='saving'").fetchall():
            try:
                opened = datetime.strptime(str(created), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            matures = opened + int(days) * DAY
            if now < matures <= soon:
                note(user_id, "deposit_maturity", "예금·적금 만기가 7일 이내입니다",
                     f"계좌 {number}({product})의 만기가 {datetime.fromtimestamp(matures, timezone.utc):%Y-%m-%d}입니다.",
                     f"account:{number}:maturity-soon")
            elif matures <= now:
                note(user_id, "deposit_maturity", "예금·적금이 만기되었습니다",
                     f"계좌 {number}({product})가 만기되었습니다.", f"account:{number}:matured")
        return added
