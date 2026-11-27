"""Announcing a change to the terms or the privacy policy.

One record is made when the console announces a revision, and the same text goes everywhere: a bell notification for every member, a post
on the 공지 board (shown as 관리자), the public list on the official site, and a first-screen modal in 나루 until each member has seen it.
Each announcement carries a before/after comparison (신구 대조) of the changed clauses, and, for an unfavourable change, the reminder that
agreeing by Discord DM is needed to keep using the service after it takes effect. Stages: announced, applied (the effective date), cancelled.
"""
import datetime
import difflib
import json
import time
import zoneinfo

KST = zoneinfo.ZoneInfo("Asia/Seoul")
LINE_MAX = 300
APPLIED_SHOWN_DAYS = 14
NOTICE_BOARD = "notice"
STAMPS = ("시행일", "개정", "이전 개정")  # the date lines change with every revision: not a change worth listing


# ---- comparison -----------------------------------------------------------------------------------------------------------------
def _lines(text):
    """(section, line) for every line that matters: the heading a line sits under, and the line without its "## " or "- " mark."""
    out, section = [], "머리말"
    for raw in str(text).replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("# "):
            continue
        if any(line.startswith(stamp + ":") or line.startswith(stamp + "：") for stamp in STAMPS):
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            out.append((section, section))
        else:
            out.append((section, line[2:].strip() if line.startswith("- ") else line))
    return out


def compare(old, new):
    """The changed clauses, grouped under their headings: [{"section", "before": [...], "after": [...]}]."""
    a, b = _lines(old), _lines(new)
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=[line for _, line in a], b=[line for _, line in b], autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        section = (b[j1][0] if j2 > j1 else a[i1][0])
        before, after = [line[:LINE_MAX] for _, line in a[i1:i2]], [line[:LINE_MAX] for _, line in b[j1:j2]]
        if changes and changes[-1]["section"] == section:
            changes[-1]["before"] += before
            changes[-1]["after"] += after
        else:
            changes.append({"section": section, "before": before, "after": after})
    return changes


# ---- storage --------------------------------------------------------------------------------------------------------------------
def ensure_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_announcements (announcement_id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, "
        "title TEXT NOT NULL, effective_epoch INTEGER NOT NULL, unfavourable INTEGER NOT NULL DEFAULT 0, changes_json TEXT NOT NULL DEFAULT '[]', "
        "status TEXT NOT NULL DEFAULT 'open', created_epoch INTEGER NOT NULL, applied_epoch INTEGER, campaign_id INTEGER, actor_id TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS announcement_seen (announcement_id INTEGER NOT NULL, user_id TEXT NOT NULL, stage TEXT NOT NULL, "
        "seen_epoch INTEGER NOT NULL, PRIMARY KEY (announcement_id, user_id, stage))")


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _record(row):
    return {"id": int(row["announcement_id"]), "announcement_id": int(row["announcement_id"]), "document_id": row["document_id"], "title": row["title"],
            "effective_epoch": int(row["effective_epoch"]), "unfavourable": bool(row["unfavourable"]), "changes": json.loads(row["changes_json"] or "[]"),
            "status": row["status"], "created_epoch": int(row["created_epoch"]), "applied_epoch": row["applied_epoch"], "campaign_id": row["campaign_id"],
            "actor_id": row["actor_id"]}


def open_announcement(conn, document_id, title, old_text, new_text, effective_epoch, unfavourable, campaign_id=None, now=None, actor=None):
    """Record an announced revision (an earlier open one for the same document is replaced). Returns its id."""
    ensure_tables(conn)
    now = int(time.time() if now is None else now)
    conn.execute("UPDATE document_announcements SET status='replaced' WHERE document_id=? AND status='open'", (document_id,))
    return conn.execute(
        "INSERT INTO document_announcements(document_id,title,effective_epoch,unfavourable,changes_json,created_epoch,campaign_id,actor_id) VALUES (?,?,?,?,?,?,?,?)",
        (document_id, title, int(effective_epoch), 1 if unfavourable else 0, json.dumps(compare(old_text, new_text), ensure_ascii=False), now, campaign_id,
         None if actor is None else str(actor))).lastrowid


def get(conn, announcement_id):
    row = conn.execute("SELECT * FROM document_announcements WHERE announcement_id=?", (int(announcement_id),)).fetchone()
    return _record(row) if row else None


def _open_for(conn, document_id):
    if not _has(conn, "document_announcements"):
        return None
    row = conn.execute("SELECT * FROM document_announcements WHERE document_id=? AND status='open' ORDER BY announcement_id DESC LIMIT 1", (document_id,)).fetchone()
    return _record(row) if row else None


def cancel(conn, document_id, now=None):
    """The announced revision was withdrawn. Returns the announcement, or None when none was open."""
    record = _open_for(conn, document_id)
    if record:
        conn.execute("UPDATE document_announcements SET status='cancelled' WHERE announcement_id=?", (record["id"],))
        record["status"] = "cancelled"
    return record


def mark_applied(conn, document_id, now=None):
    """The effective date came and the revision is in force. Returns the announcement, or None."""
    record = _open_for(conn, document_id)
    if record:
        now = int(time.time() if now is None else now)
        conn.execute("UPDATE document_announcements SET status='applied', applied_epoch=? WHERE announcement_id=?", (now, record["id"]))
        record["status"], record["applied_epoch"] = "applied", now
    return record


# ---- text -----------------------------------------------------------------------------------------------------------------------
def _when(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), KST).strftime("%Y년 %m월 %d일 %H:%M") + "(한국시간)"


def _day(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), KST).strftime("%Y년 %m월 %d일")


CONSENT = ("이용자에게 불리하거나 중요한 변경입니다. Discord DM으로 보내 드린 개정안의 '동의합니다' 버튼을 눌러야 시행일 이후에도 서비스를 계속 이용할 수 있습니다. "
           "DM을 받지 못했다면 나루 첫 화면의 안내에서도 동의할 수 있습니다.")
PLAIN = "일반 변경입니다. 시행일 이후에도 계속 이용하면 동의한 것으로 봅니다."


def render(record, stage, limit=3500):
    """(title, body) for an announcement at a stage: announced, applied or cancelled."""
    name = record["title"]
    if stage == "cancelled":
        return f"⚠️ {name} 개정 안내가 취소되었습니다", "예고했던 개정이 취소되었습니다. 지금 적용 중인 문서가 계속 적용됩니다."
    headline = f"📢 {name} 개정 안내 (시행 {_day(record['effective_epoch'])})" if stage == "announced" else f"✅ {name} 개정이 시행됩니다"
    lines = [f"시행일: {_when(record['effective_epoch'])}", CONSENT if record["unfavourable"] else PLAIN, "", "변경 내용 (신구 대조)"]
    for change in record["changes"]:
        lines.append(f"■ {change['section']}")
        lines += [f"  변경 전: {line}" for line in change["before"]] or ["  변경 전: (없음)"]
        lines += [f"  변경 후: {line}" for line in change["after"]] or ["  변경 후: (삭제됨)"]
    if not record["changes"]:
        lines.append("표시할 문구 변경이 없습니다(날짜와 개정 안내만 바뀜).")
    lines += ["", f"전문: 공식 사이트 {record['document_id']} 페이지"]
    body = "\n".join(lines)
    return headline[:100], (body if len(body) <= limit else body[:limit - 1] + "…")


# ---- the channels ---------------------------------------------------------------------------------------------------------------
def notify_members(conn, record, stage, now=None):
    """One bell notification for every active member and operator (kept in the bell: the Discord copy is marked as already sent). Returns how many."""
    from managers.notification_manager import NotificationManager
    title, body = render(record, stage, limit=900)
    count = 0
    for row in conn.execute("SELECT user_id FROM users WHERE role IN ('user','admin') AND status='active'").fetchall():
        added = NotificationManager.add(conn, row[0], "policy", title, body, action_path="/announcements", dedupe_key=f"policy:{record['id']}:{stage}:{row[0]}",
                                        payload={"announcementId": record["id"], "stage": stage}, now=now, discord_sent=True)
        count += 1 if added else 0
    return count


def post_to_board(conn, record, stage, actor_id=None, ip="system"):
    """The 공지 board post, written by the administrator who saved it but shown as 관리자. Returns the post id (None if the board refuses)."""
    from web_api import board
    actor_id = actor_id or record.get("actor_id")
    if not actor_id:
        return None
    title, body = render(record, stage, limit=board.BODY_MAX - 100)
    try:
        board.ensure_tables(conn)
        return board.create_post(conn, {"userId": str(actor_id), "role": "admin"}, {"board": NOTICE_BOARD, "title": title[:board.TITLE_MAX], "body": body, "authorLabel": "관리자", "notice": 1}, ip)
    except ValueError:
        return None  # e.g. the board is closed or throttled: the other channels still carry the announcement


def feed(conn, limit=50):
    """The public list for the official site, newest first (nothing about who saved it)."""
    if not _has(conn, "document_announcements"):
        return []
    return [{"id": r["id"], "document": r["document_id"], "title": r["title"], "status": r["status"], "announcedEpoch": r["created_epoch"],
             "effectiveEpoch": r["effective_epoch"], "unfavourable": r["unfavourable"], "changes": r["changes"]}
            for r in (_record(row) for row in conn.execute("SELECT * FROM document_announcements WHERE status<>'replaced' ORDER BY announcement_id DESC LIMIT ?", (int(limit),)).fetchall())]


# ---- the 나루 first-screen modal -------------------------------------------------------------------------------------------------
def unseen(conn, user_id, now=None):
    """What this member has not been shown yet: open announcements, and, for two weeks, the ones that have just taken effect."""
    if not _has(conn, "document_announcements"):
        return []
    now = int(time.time() if now is None else now)
    seen = {(int(r[0]), r[1]) for r in conn.execute("SELECT announcement_id,stage FROM announcement_seen WHERE user_id=?", (str(user_id),)).fetchall()} if _has(conn, "announcement_seen") else set()
    out = []
    for row in conn.execute("SELECT * FROM document_announcements WHERE status IN ('open','applied') ORDER BY announcement_id").fetchall():
        record = _record(row)
        if record["status"] == "open":
            stage = "announced"
        elif record["applied_epoch"] is not None and now - int(record["applied_epoch"]) <= APPLIED_SHOWN_DAYS * 86400:
            stage = "applied"
        else:
            continue
        if (record["id"], stage) not in seen:
            out.append({**record, "stage": stage, "text": render(record, stage)})
    return out


def mark_seen(conn, user_id, announcement_ids, now=None):
    ensure_tables(conn)
    now = int(time.time() if now is None else now)
    for announcement_id in announcement_ids:
        record = get(conn, announcement_id)
        if not record:
            continue
        stage = "announced" if record["status"] == "open" else "applied"
        conn.execute("INSERT OR IGNORE INTO announcement_seen(announcement_id,user_id,stage,seen_epoch) VALUES (?,?,?,?)", (record["id"], str(user_id), stage, now))


# ---- what the console calls -----------------------------------------------------------------------------------------------------
def announce(conn, document_id, title, old_text, new_text, effective_epoch, unfavourable, actor, campaign_id=None, now=None):
    """A revision was announced: record it, notify every member, post it on the board. Returns the record."""
    record = get(conn, open_announcement(conn, document_id, title, old_text, new_text, effective_epoch, unfavourable, campaign_id=campaign_id, now=now, actor=actor))
    notify_members(conn, record, "announced", now=now)
    post_to_board(conn, record, "announced", actor)
    return record


def retract(conn, document_id, now=None):
    """The announced revision was withdrawn: say so everywhere. Returns the record, or None when nothing was announced."""
    record = cancel(conn, document_id, now=now)
    if record:
        notify_members(conn, record, "cancelled", now=now)
        post_to_board(conn, record, "cancelled")
    return record


def take_effect(conn, document_id, now=None):
    """The effective date came: say that it is in force. Returns the record, or None when nothing was announced."""
    record = mark_applied(conn, document_id, now=now)
    if record:
        notify_members(conn, record, "applied", now=now)
        post_to_board(conn, record, "applied")
    return record


def member_view(conn, user_id, scope="unseen", now=None):
    """What 나루 shows a member: the unseen announcements (first-screen modal), or the recent list (the 공지 page).

    Each item carries the rendered text and, for an unfavourable change, the consent campaign and whether this member already agreed."""
    from managers.document_consent import DocumentConsent
    now = int(time.time() if now is None else now)
    records = unseen(conn, user_id, now=now) if scope == "unseen" else [
        {**_record(row), "stage": {"open": "announced", "applied": "applied", "cancelled": "cancelled"}.get(row["status"], row["status"])}
        for row in (conn.execute("SELECT * FROM document_announcements WHERE status<>'replaced' ORDER BY announcement_id DESC LIMIT 30").fetchall()
                    if _has(conn, "document_announcements") else [])]
    items = []
    for record in records:
        campaign = record["campaign_id"] if record["unfavourable"] else None
        title, body = render(record, record["stage"])
        items.append({"id": record["id"], "document": record["document_id"], "title": record["title"], "stage": record["stage"], "status": record["status"],
                      "effectiveEpoch": record["effective_epoch"], "announcedEpoch": record["created_epoch"], "unfavourable": record["unfavourable"],
                      "changes": record["changes"], "text": {"title": title, "body": body}, "campaignId": campaign,
                      "consented": None if campaign is None else DocumentConsent.consented(conn, campaign, user_id)})
    return items
