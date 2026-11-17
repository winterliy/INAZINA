"""나루 주주총회 and 회생·파산 filings.

주주총회: the largest common shareholder (or an administrator) convenes a meeting
with a place, a date and agenda items. Convening freezes the shareholder
register shared with dividends (managers/corporate_actions_manager.py) until voting
ends, when it is released and follows holdings live again. Votes come from that
frozen register (a company's block is cast by its majority controller; cross-held
blocks have no vote); each holder may answer attendance and vote 찬성·
반대·기권 on every item until voting closes. Closed meetings remain as minutes:
where and when, who attended, each item's result and how the viewer voted.

회생·파산: a citizen files one of the two with the court; the filing and an asset
snapshot are kept, and the bot tells the administrators.

Every function takes an open connection, so the caller owns the transaction.
"""
import json
import time

KINDS = {"rehabilitation": "회생", "bankruptcy": "파산"}
CHOICES = ("for", "against", "abstain")
MAX_ITEMS = 10


def ensure_tables(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS shareholder_meetings (
        meeting_id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL, company_name TEXT NOT NULL,
        convener_id TEXT NOT NULL, convener_name TEXT NOT NULL,
        place TEXT NOT NULL, meeting_epoch INTEGER NOT NULL, close_epoch INTEGER NOT NULL,
        closed_epoch INTEGER, created_epoch INTEGER NOT NULL, result_notified_epoch INTEGER)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS shareholder_meeting_items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL REFERENCES shareholder_meetings(meeting_id),
        seq INTEGER NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""")
    connection.execute("""CREATE TABLE IF NOT EXISTS shareholder_meeting_holders (
        meeting_id INTEGER NOT NULL REFERENCES shareholder_meetings(meeting_id),
        user_id TEXT NOT NULL, nickname TEXT NOT NULL, shares REAL NOT NULL,
        attendance TEXT CHECK(attendance IN ('attend','absent')), responded_epoch INTEGER,
        PRIMARY KEY (meeting_id, user_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS shareholder_meeting_votes (
        item_id INTEGER NOT NULL REFERENCES shareholder_meeting_items(item_id),
        user_id TEXT NOT NULL, choice TEXT NOT NULL CHECK(choice IN ('for','against','abstain')),
        voted_epoch INTEGER NOT NULL, PRIMARY KEY (item_id, user_id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_sh_meeting_holder_user ON shareholder_meeting_holders(user_id, meeting_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS court_filings (
        filing_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, nickname TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('rehabilitation','bankruptcy')),
        reason TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','reviewing','approved','rejected')),
        created_epoch INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', handled_by TEXT, handled_epoch INTEGER)""")
    _add_columns(connection, "court_filings", {"note": "TEXT NOT NULL DEFAULT ''", "handled_by": "TEXT", "handled_epoch": "INTEGER"})
    _add_columns(connection, "shareholder_meetings", {"result_notified_epoch": "INTEGER"})
    connection.execute("CREATE INDEX IF NOT EXISTS idx_court_filings_user ON court_filings(user_id, filing_id)")


def _org_votes(connection, org, issuer_account, treasury_id):
    """Total votes of an organization and each record holder's votes, across its share classes:
    per-share voting rights apply; treasury stock and the system treasury carry no vote."""
    total, holders = 0.0, {}
    excluded_ids = {treasury_id, issuer_account.get(org)}
    for code, supply, per_share in connection.execute(
            "SELECT c.company_code, c.total_shares, s.voting_rights_per_share FROM share_issuances s "
            "JOIN company c ON c.share_issuance_id=s.issuance_id WHERE s.organization_code=?", (org,)).fetchall():
        supply, per_share = float(supply or 0), float(per_share or 0)
        if supply <= 0 or per_share <= 0:
            continue
        excluded = 0.0
        for user_id, quantity in connection.execute(
                "SELECT user_id, SUM(quantity) FROM (SELECT user_id, quantity FROM user_assets WHERE asset_code=? AND quantity>0 "
                "UNION ALL SELECT writer_id, quantity FROM option_collateral_lots WHERE underlying_code=? AND quantity>0) "
                "GROUP BY user_id", (code, code)).fetchall():
            if str(user_id) in excluded_ids:
                excluded += float(quantity)
            else:
                holders[str(user_id)] = holders.get(str(user_id), 0.0) + float(quantity) * per_share
        total += max(0.0, supply - excluded) * per_share
    return total, holders


def _voting_classes(connection, code):
    """The issuing organization and its voting share classes (code, votes per share)."""
    row = connection.execute(
        "SELECT s.organization_code FROM company c JOIN share_issuances s ON s.issuance_id=c.share_issuance_id "
        "WHERE c.company_code=?", (code,)).fetchone()
    if not row:
        return None, [(code, 1.0)]
    classes = [(str(c), float(v or 0)) for c, v in connection.execute(
        "SELECT c.company_code, s.voting_rights_per_share FROM share_issuances s JOIN company c ON c.share_issuance_id=s.issuance_id "
        "WHERE s.organization_code=?", (str(row[0]),)).fetchall() if float(v or 0) > 0]
    return str(row[0]), classes


def _register_key(meeting_id):
    return f"meeting:{meeting_id}:"


def _frozen_votes(connection, meeting_id, classes):
    """Record holders' votes from the meeting's frozen register (shared with dividends):
    every class's entries times its votes per share. Treasury and own shares are not on it."""
    votes = {}
    for code, per_share in classes:
        for user_id, quantity in connection.execute(
                "SELECT e.user_id, e.quantity FROM shareholder_registers r JOIN shareholder_register_entries e ON e.register_id=r.register_id "
                "WHERE r.operation_key=? UNION ALL SELECT d.user_id, d.quantity FROM shareholder_registers r "
                "JOIN shareholder_register_dr_entries d ON d.register_id=r.register_id WHERE r.operation_key=?",
                (_register_key(meeting_id) + code,) * 2).fetchall():
            votes[str(user_id)] = votes.get(str(user_id), 0.0) + float(quantity) * per_share
    return votes


def _voters(connection, root, record, treasury_id):
    """Record holders vote, as at a real meeting. A company's block is cast whole by whoever
    controls it now (a majority of its votes), followed up through parent companies; a company
    with no majority controller, or a control loop, leaves its block unexercised. Cross-holding
    (상법 369③): a holder company in which this company owns over 10% of the votes has no vote."""
    org_of, issuer_account = {}, {}
    for account, org in connection.execute(
            "SELECT ca.user_id, s.organization_code FROM company_accounts ca JOIN company c ON c.company_code=ca.company_code "
            "JOIN share_issuances s ON s.issuance_id=c.share_issuance_id").fetchall():
        org_of[str(account)] = str(org)
        issuer_account.setdefault(str(org), str(account))
    cache = {}

    def votes(org):
        if org not in cache:
            cache[org] = _org_votes(connection, org, issuer_account, str(treasury_id))
        return cache[org]

    def controller(holder, seen):
        """The person who finally casts a record holder's block, or None."""
        org = org_of.get(holder)
        if not org:
            return holder
        if org in seen or len(seen) > 8:
            return None
        total, holders = votes(org)
        top = max(holders.items(), key=lambda pair: pair[1], default=None)
        if not top or total <= 0 or top[1] <= total / 2:
            return None
        return controller(top[0], seen | {org})

    root_account = issuer_account.get(root) if root else None
    cast = {}
    for holder, amount in record.items():
        org = org_of.get(holder)
        if org and root_account:
            total, holders = votes(org)
            if total > 0 and holders.get(root_account, 0.0) > total * 0.1:
                continue  # 상호주: no vote
        person = controller(holder, {root} if root else set())
        if person:
            cast[person] = cast.get(person, 0.0) + amount
    names = {str(r[0]): r[1] or str(r[0]) for r in connection.execute(
        "SELECT user_id, nickname FROM users WHERE user_id IN (%s)" % ",".join("?" * len(cast)), tuple(cast)).fetchall()} if cast else {}
    return [{"user_id": person, "nickname": names.get(person, person), "shares": amount}
            for person, amount in sorted(cast.items(), key=lambda pair: (-pair[1], pair[0]))]


def _add_columns(connection, table, columns):
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, declaration in columns.items():
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _text(value, limit, error, *, required=True):
    text = str(value or "").strip()
    if (required and not text) or len(text) > limit:
        raise ValueError(error)
    return text


def _has_column(connection, table, column):
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def convene(connection, user, payload, treasury_id=""):
    code = _text(payload.get("code"), 32, "company_required").upper()
    share_class = "share_class" if _has_column(connection, "company", "share_class") else "NULL"
    company = connection.execute(f"SELECT name, {share_class} AS share_class FROM company WHERE company_code = ?", (code,)).fetchone()
    if not company:
        raise ValueError("company_not_found")
    if company["share_class"] == "preferred":
        raise ValueError("preferred_no_vote")
    place = _text(payload.get("place"), 200, "place_required")
    now = int(time.time())
    try:
        meeting_epoch = int(payload.get("meetingEpoch") or now)
        hours = int(payload.get("votingHours") or 24)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_schedule") from exc
    if not 1 <= hours <= 24 * 14 or meeting_epoch < now - 3600 or meeting_epoch > now + 86400 * 90:
        raise ValueError("invalid_schedule")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("items_required")
    items = [(_text(item.get("title") if isinstance(item, dict) else item, 120, "item_title_required"),
              _text(item.get("detail") if isinstance(item, dict) else "", 1000, "item_detail_too_long", required=False))
             for item in items]
    root, classes = _voting_classes(connection, code)
    if not classes:
        raise ValueError("no_holders")
    close_epoch = max(meeting_epoch, now) + hours * 3600
    meeting_id = connection.execute(
        "INSERT INTO shareholder_meetings(company_code,company_name,convener_id,convener_name,place,meeting_epoch,close_epoch,created_epoch) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (code, company["name"] or code, user["userId"], user["nickname"], place, meeting_epoch, close_epoch, now)).lastrowid
    # Convening freezes the live shareholder register (the one dividends use) until voting ends.
    from managers.corporate_actions_manager import CorporateActionsManager
    for class_code, _ in classes:
        CorporateActionsManager.freeze_meeting_register(connection, class_code, _register_key(meeting_id) + class_code,
                                                        close_epoch, user["userId"], now)
    holders = _voters(connection, root, _frozen_votes(connection, meeting_id, classes), treasury_id)
    if not holders:
        raise ValueError("no_holders")  # the caller's transaction undoes the meeting and the freeze
    largest = holders[0]["shares"]
    if user["role"] != "admin" and user["userId"] not in {row["user_id"] for row in holders if row["shares"] == largest}:
        raise ValueError("largest_holder_required")
    connection.executemany("INSERT INTO shareholder_meeting_items(meeting_id,seq,title,detail) VALUES (?,?,?,?)",
                           [(meeting_id, index + 1, title, detail) for index, (title, detail) in enumerate(items)])
    connection.executemany("INSERT INTO shareholder_meeting_holders(meeting_id,user_id,nickname,shares) VALUES (?,?,?,?)",
                           [(meeting_id, row["user_id"], row["nickname"], row["shares"]) for row in holders])
    return {"meetingId": meeting_id, "code": code, "place": place, "holders": len(holders)}


def _is_open(meeting, now):
    return meeting["closed_epoch"] is None and meeting["close_epoch"] > now


def _meeting(connection, meeting, viewer, now):
    holders = connection.execute(
        "SELECT user_id, nickname, shares, attendance FROM shareholder_meeting_holders WHERE meeting_id=? ORDER BY shares DESC",
        (meeting["meeting_id"],)).fetchall()
    total = sum(row["shares"] for row in holders) or 0
    mine = next((row for row in holders if row["user_id"] == viewer), None)
    votes = connection.execute(
        "SELECT v.item_id, v.user_id, v.choice, h.shares FROM shareholder_meeting_votes v "
        "JOIN shareholder_meeting_items i ON i.item_id=v.item_id "
        "JOIN shareholder_meeting_holders h ON h.meeting_id=i.meeting_id AND h.user_id=v.user_id WHERE i.meeting_id=?",
        (meeting["meeting_id"],)).fetchall()
    voters = {row["user_id"] for row in votes}
    items = []
    for item in connection.execute("SELECT item_id, seq, title, detail FROM shareholder_meeting_items WHERE meeting_id=? ORDER BY seq",
                                   (meeting["meeting_id"],)).fetchall():
        tally = {choice: 0.0 for choice in CHOICES}
        for row in votes:
            if row["item_id"] == item["item_id"]:
                tally[row["choice"]] += row["shares"]
        my_vote = next((row["choice"] for row in votes if row["item_id"] == item["item_id"] and row["user_id"] == viewer), None)
        items.append({"itemId": item["item_id"], "seq": item["seq"], "title": item["title"], "detail": item["detail"],
                      "tally": tally, "passed": tally["for"] > tally["against"], "myVote": my_vote})
    open_ = _is_open(meeting, now)
    return {
        "meetingId": meeting["meeting_id"], "code": meeting["company_code"], "company": meeting["company_name"],
        "convener": meeting["convener_name"], "place": meeting["place"], "meetingEpoch": meeting["meeting_epoch"],
        "closeEpoch": meeting["closed_epoch"] or meeting["close_epoch"], "open": open_, "totalShares": total,
        "canClose": open_ and meeting["convener_id"] == viewer,
        "attendees": [{"nickname": row["nickname"], "shares": row["shares"]} for row in holders
                      if row["attendance"] == "attend" or row["user_id"] in voters],
        "holderCount": len(holders),
        "me": {"shares": mine["shares"], "attendance": mine["attendance"]} if mine else None,
        "items": items,
    }


def list_meetings(connection, user):
    """Meetings the viewer holds a vote in or convened, newest first, split into open and minutes."""
    now = int(time.time())
    finish_expired(connection, now)
    rows = connection.execute(
        "SELECT * FROM shareholder_meetings m WHERE m.convener_id=? OR EXISTS ("
        "SELECT 1 FROM shareholder_meeting_holders h WHERE h.meeting_id=m.meeting_id AND h.user_id=?) "
        "ORDER BY m.meeting_epoch DESC, m.meeting_id DESC LIMIT 100", (user["userId"], user["userId"])).fetchall()
    meetings = [_meeting(connection, row, user["userId"], now) for row in rows]
    companies = [{"code": row["company_code"], "name": row["name"] or row["company_code"]} for row in connection.execute(
        "SELECT DISTINCT c.company_code, c.name FROM user_assets a JOIN company c ON c.company_code=a.asset_code "
        "WHERE a.user_id=? AND a.quantity>0 ORDER BY c.name", (user["userId"],)).fetchall()]
    return {"open": [m for m in meetings if m["open"]], "minutes": [m for m in meetings if not m["open"]], "companies": companies}


def respond(connection, user, meeting_id, payload):
    """Attendance and ballots in one call; ballots may be changed until voting closes."""
    meeting = connection.execute("SELECT * FROM shareholder_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    if not meeting:
        raise ValueError("meeting_not_found")
    if not _is_open(meeting, int(time.time())):
        raise ValueError("meeting_closed")
    if not connection.execute("SELECT 1 FROM shareholder_meeting_holders WHERE meeting_id=? AND user_id=?",
                              (meeting_id, user["userId"])).fetchone():
        raise ValueError("not_a_holder")
    now = int(time.time())
    attendance = payload.get("attendance")
    if attendance is not None:
        if attendance not in ("attend", "absent"):
            raise ValueError("invalid_attendance")
        connection.execute("UPDATE shareholder_meeting_holders SET attendance=?, responded_epoch=? WHERE meeting_id=? AND user_id=?",
                           (attendance, now, meeting_id, user["userId"]))
    votes = payload.get("votes") or {}
    if not isinstance(votes, dict):
        raise ValueError("invalid_vote")
    items = {row["item_id"] for row in connection.execute(
        "SELECT item_id FROM shareholder_meeting_items WHERE meeting_id=?", (meeting_id,)).fetchall()}
    for key, choice in votes.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_vote") from exc
        if item_id not in items or choice not in CHOICES:
            raise ValueError("invalid_vote")
        connection.execute(
            "INSERT INTO shareholder_meeting_votes(item_id,user_id,choice,voted_epoch) VALUES (?,?,?,?) "
            "ON CONFLICT(item_id,user_id) DO UPDATE SET choice=excluded.choice, voted_epoch=excluded.voted_epoch",
            (item_id, user["userId"], choice, now))
    if votes:
        # Voting is taking part: a holder who votes counts as present.
        connection.execute("UPDATE shareholder_meeting_holders SET attendance='attend', responded_epoch=? WHERE meeting_id=? AND user_id=?",
                           (now, meeting_id, user["userId"]))
    return {"ok": True}


def close(connection, user, meeting_id):
    meeting = connection.execute("SELECT * FROM shareholder_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    if not meeting:
        raise ValueError("meeting_not_found")
    if meeting["convener_id"] != user["userId"] and user["role"] != "admin":
        raise ValueError("convener_required")
    if not _is_open(meeting, int(time.time())):
        raise ValueError("meeting_closed")
    connection.execute("UPDATE shareholder_meetings SET closed_epoch=? WHERE meeting_id=?", (int(time.time()), meeting_id))
    _release(connection, meeting_id)
    return {"ok": True}


def _release(connection, meeting_id):
    """Voting is over: the register is no longer frozen and follows holdings live again."""
    from managers.corporate_actions_manager import CorporateActionsManager
    CorporateActionsManager.release_register(connection, _register_key(meeting_id))


def file_court(connection, user, payload, snapshot):
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ValueError("invalid_kind")
    if payload.get("consent") is not True:
        raise ValueError("consent_required")
    reason = _text(payload.get("reason"), 2000, "reason_required")
    plan = _text(payload.get("plan"), 2000, "plan_required", required=kind == "rehabilitation")
    if connection.execute("SELECT 1 FROM court_filings WHERE user_id=? AND status='submitted' AND created_epoch>?",
                          (user["userId"], int(time.time()) - 86400)).fetchone():
        raise ValueError("filing_pending")
    return connection.execute(
        "INSERT INTO court_filings(user_id,nickname,kind,reason,plan,snapshot,created_epoch) VALUES (?,?,?,?,?,?,?)",
        (user["userId"], user["nickname"], kind, reason, plan, json.dumps(snapshot, ensure_ascii=False, default=str),
         int(time.time()))).lastrowid


STATUSES = {"submitted": "접수", "reviewing": "심리 중", "approved": "인가", "rejected": "기각"}


def _filing(row, admin=False):
    item = {"filingId": row["filing_id"], "kind": row["kind"], "label": KINDS[row["kind"]], "reason": row["reason"],
            "plan": row["plan"], "status": row["status"], "createdEpoch": row["created_epoch"], "note": row["note"],
            "handledEpoch": row["handled_epoch"]}
    if admin:
        item.update({"userId": row["user_id"], "nickname": row["nickname"], "handledBy": row["handled_by"],
                     "snapshot": json.loads(row["snapshot"] or "{}")})
    return item


def admin_filings(connection, status=None):
    """Every filing for the dashboard's 회생·파산 심리, newest first."""
    if status and status not in STATUSES:
        raise ValueError("상태는 submitted·reviewing·approved·rejected 중 하나입니다.")
    rows = connection.execute("SELECT * FROM court_filings WHERE (? IS NULL OR status=?) ORDER BY filing_id DESC LIMIT 300",
                              (status, status)).fetchall()
    counts = {key: 0 for key in STATUSES}
    for key, count in connection.execute("SELECT status, COUNT(*) FROM court_filings GROUP BY status").fetchall():
        counts[key] = count
    return {"items": [_filing(row, True) for row in rows], "counts": counts}


def decide_filing(connection, actor, filing_id, payload):
    """An administrator moves a filing to 심리 중·인가·기각 with an optional reason for the applicant."""
    status = payload.get("status")
    if status not in STATUSES or status == "submitted":
        raise ValueError("심리 중·인가·기각 중 하나를 골라주세요.")
    note = _text(payload.get("note"), 1000, "사유는 1000자 이하로 적어주세요.", required=False)
    if status == "rejected" and not note:
        raise ValueError("기각 사유를 적어주세요.")
    row = connection.execute("SELECT * FROM court_filings WHERE filing_id=?", (int(filing_id),)).fetchone()
    if not row:
        raise ValueError("신청을 찾을 수 없습니다.")
    if row["status"] in ("approved", "rejected"):
        raise ValueError("이미 결정된 신청입니다.")
    connection.execute("UPDATE court_filings SET status=?, note=?, handled_by=?, handled_epoch=? WHERE filing_id=?",
                       (status, note, actor, int(time.time()), int(filing_id)))
    return {"userId": row["user_id"], "label": KINDS[row["kind"]], "filingId": row["filing_id"],
            "createdEpoch": row["created_epoch"], "kind": row["kind"], "status": status, "statusLabel": STATUSES[status], "note": note}


def list_filings(connection, user):
    return [_filing(row)
            for row in connection.execute("SELECT * FROM court_filings WHERE user_id=? ORDER BY filing_id DESC LIMIT 50",
                                          (user["userId"],)).fetchall()]


def finish_expired(connection, now=None):
    """Close meetings whose voting time has passed and lift their frozen registers."""
    now = int(now or time.time())
    for (expired,) in connection.execute(
            "SELECT meeting_id FROM shareholder_meetings WHERE closed_epoch IS NULL AND close_epoch<=?", (now,)).fetchall():
        connection.execute("UPDATE shareholder_meetings SET closed_epoch=close_epoch WHERE meeting_id=?", (expired,))
        _release(connection, expired)


def meeting_for(connection, meeting_id, viewer=""):
    """One meeting as the pages and the Discord notices show it."""
    row = connection.execute("SELECT * FROM shareholder_meetings WHERE meeting_id=?", (int(meeting_id),)).fetchone()
    if not row:
        raise ValueError("meeting_not_found")
    return _meeting(connection, row, viewer, int(time.time()))


def holder_ids(connection, meeting_id):
    return [r[0] for r in connection.execute(
        "SELECT user_id FROM shareholder_meeting_holders WHERE meeting_id=? ORDER BY shares DESC", (int(meeting_id),)).fetchall()]


def results_to_notify(connection):
    """Ended meetings whose results the bot has not sent yet."""
    return [r[0] for r in connection.execute(
        "SELECT meeting_id FROM shareholder_meetings WHERE closed_epoch IS NOT NULL AND result_notified_epoch IS NULL "
        "ORDER BY meeting_id LIMIT 20").fetchall()]


def mark_notified(connection, meeting_id):
    connection.execute("UPDATE shareholder_meetings SET result_notified_epoch=? WHERE meeting_id=?", (int(time.time()), int(meeting_id)))
