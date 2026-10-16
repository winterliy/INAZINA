"""HTS board: notices, free talk and per-instrument market discussion.

Anyone may read; writing, commenting, liking and reporting need a login. A
post or comment may be shown as 익명, but the author ID and the client IP are
always stored, and only administrators can see them.

Every function takes an open connection, so the caller owns the transaction.
Deleting by an author only marks the row; administrators may purge it.
"""
import time

from database.integrity import PRODUCT_CATALOG

BOARDS = {"notice": "공지", "free": "자유", "market": "시장 토론"}
PAGE_SIZE = 20
TITLE_MAX, BODY_MAX, COMMENT_MAX, REASON_MAX = 100, 10_000, 1_000, 300
# Per author: at most this many posts / comments in the last 60 seconds.
POSTS_PER_MINUTE, COMMENTS_PER_MINUTE = 2, 10
# Distinct reporters that hide a post or comment until an administrator reviews it.
AUTO_HIDE_REPORTS = 3
ANONYMOUS = "익명"
# 개인정보 처리방침: 게시판 접속 IP는 작성(신고) 후 1년이 지나면 파기한다.
# The rows stay; only the address is erased.
IP_RETENTION_SECONDS = 365 * 86400
# Writing stores the client IP, a collection the privacy policy announced on
# 2026-10-17 to start 30 days later. Until then the board is read-only:
# posts, comments and reports (which record IPs) are refused; likes are not.
WRITE_OPENS_EPOCH = 1794754800  # 2026-11-16 00:00 KST
WRITE_OPENS_TEXT = "2026년 11월 16일"


def _require_open(now=None):
    if (now if now is not None else time.time()) < WRITE_OPENS_EPOCH:
        raise ValueError(f"게시판 글쓰기·댓글·신고는 {WRITE_OPENS_TEXT}부터 이용할 수 있습니다. 지금은 읽기만 가능합니다.")


def ensure_tables(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS board_posts (
            post_id INTEGER PRIMARY KEY AUTOINCREMENT,
            board TEXT NOT NULL CHECK(board IN ('notice','free','market')),
            author_id TEXT NOT NULL REFERENCES users(user_id),
            anonymous INTEGER NOT NULL DEFAULT 0 CHECK(anonymous IN (0,1)),
            title TEXT NOT NULL, body TEXT NOT NULL, asset_code TEXT,
            pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','hidden','deleted')),
            hidden_reason TEXT, author_ip TEXT NOT NULL,
            view_count INTEGER NOT NULL DEFAULT 0,
            created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_board_posts_list ON board_posts(board, status, pinned, post_id);
        CREATE INDEX IF NOT EXISTS idx_board_posts_asset ON board_posts(asset_code, status, post_id);
        CREATE INDEX IF NOT EXISTS idx_board_posts_author ON board_posts(author_id, created_epoch);
        CREATE TABLE IF NOT EXISTS board_comments (
            comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL REFERENCES board_posts(post_id) ON DELETE CASCADE,
            author_id TEXT NOT NULL REFERENCES users(user_id),
            anonymous INTEGER NOT NULL DEFAULT 0 CHECK(anonymous IN (0,1)),
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','hidden','deleted')),
            hidden_reason TEXT, author_ip TEXT NOT NULL, created_epoch INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_board_comments_post ON board_comments(post_id, comment_id);
        CREATE INDEX IF NOT EXISTS idx_board_comments_author ON board_comments(author_id, created_epoch);
        CREATE TABLE IF NOT EXISTS board_likes (
            post_id INTEGER NOT NULL REFERENCES board_posts(post_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(user_id),
            created_epoch INTEGER NOT NULL,
            PRIMARY KEY(post_id, user_id));
        CREATE TABLE IF NOT EXISTS board_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER REFERENCES board_posts(post_id) ON DELETE CASCADE,
            comment_id INTEGER REFERENCES board_comments(comment_id) ON DELETE CASCADE,
            reporter_id TEXT NOT NULL REFERENCES users(user_id),
            reason TEXT NOT NULL, reporter_ip TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','done')),
            created_epoch INTEGER NOT NULL,
            CHECK((post_id IS NULL) <> (comment_id IS NULL)));
        CREATE UNIQUE INDEX IF NOT EXISTS idx_board_reports_post ON board_reports(reporter_id, post_id) WHERE post_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_board_reports_comment ON board_reports(reporter_id, comment_id) WHERE comment_id IS NOT NULL;
    """)


def purge_expired_ips(connection, now=None):
    """Erase client addresses older than the retention period; returns how many were erased.

    Posts and comments their authors deleted are destroyed on the same schedule.
    """
    cutoff = int(now if now is not None else time.time()) - IP_RETENTION_SECONDS
    connection.execute("DELETE FROM board_comments WHERE status='deleted' AND created_epoch<?", (cutoff,))
    connection.execute("DELETE FROM board_posts WHERE status='deleted' AND created_epoch<?", (cutoff,))
    erased = 0
    for table, column in (("board_posts", "author_ip"), ("board_comments", "author_ip"), ("board_reports", "reporter_ip")):
        erased += connection.execute(
            f"UPDATE {table} SET {column}='' WHERE created_epoch<? AND {column}<>''", (cutoff,)).rowcount
    return erased


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _text(value, name, limit):
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValueError(f"{name}을(를) 입력해주세요.")
    if len(text) > limit:
        raise ValueError(f"{name}은(는) {limit:,}자 이하로 입력해주세요.")
    return text


def _ip(value):
    ip = str(value or "").strip()[:64]
    if not ip:
        raise ValueError("접속 정보를 확인할 수 없습니다.")
    return ip


def _id(value, name="글"):
    if isinstance(value, bool):
        raise ValueError(f"{name} 번호를 확인해주세요.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 번호를 확인해주세요.") from None
    if number <= 0:
        raise ValueError(f"{name} 번호를 확인해주세요.")
    return number


def _throttle(connection, table, author_id, limit, label):
    recent = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE author_id=? AND created_epoch>=?",
        (author_id, int(time.time()) - 60)).fetchone()[0]
    if recent >= limit:
        raise ValueError(f"{label}은(는) 1분에 {limit}개까지 쓸 수 있습니다. 잠시 후 다시 시도해주세요.")


def _asset(connection, board, code):
    code = str(code or "").strip()
    if board != "market" or not code:
        return None
    if not connection.execute(f"SELECT 1 FROM ({PRODUCT_CATALOG}) WHERE code=?", (code,)).fetchone():
        raise ValueError("종목 코드를 확인해주세요.")
    return code


def _post(connection, post_id):
    row = connection.execute("SELECT * FROM board_posts WHERE post_id=?", (post_id,)).fetchone()
    if not row or row["status"] == "deleted":
        raise ValueError("글을 찾을 수 없습니다.")
    return row


# ---------------------------------------------------------------------------
# Reading (no login needed)
# ---------------------------------------------------------------------------
_PUBLIC_POST = (
    "SELECT p.post_id,p.board,p.title,p.asset_code,p.pinned,p.anonymous,p.view_count,p.created_epoch,p.updated_epoch,"
    "p.author_id,CASE WHEN p.anonymous=1 THEN ? ELSE COALESCE(u.nickname,'알 수 없음') END AS author,"
    "(SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.post_id AND c.status='active') AS comment_count,"
    "(SELECT COUNT(*) FROM board_likes l WHERE l.post_id=p.post_id) AS like_count "
    "FROM board_posts p LEFT JOIN users u ON u.user_id=p.author_id ")


def _public(row, viewer):
    item = dict(row)
    item["mine"] = bool(viewer) and item.pop("author_id") == viewer
    item.pop("author_id", None)
    return item


def list_posts(connection, board=None, search="", asset_code=None, page=1, viewer=None):
    """Active posts, pinned first then newest. ``board=None`` lists every board."""
    if board is not None and board not in BOARDS:
        raise ValueError("게시판을 확인해주세요.")
    page = max(1, min(_id(page, "페이지"), 10_000))
    where, params = ["p.status='active'"], [ANONYMOUS]
    if board:
        where.append("p.board=?"); params.append(board)
    if asset_code:
        where.append("p.asset_code=?"); params.append(str(asset_code).strip())
    term = str(search or "").strip()[:50]
    if term:
        where.append("(p.title LIKE ? OR p.body LIKE ?)"); params += [f"%{term}%", f"%{term}%"]
    clause = " AND ".join(where)
    total = connection.execute(f"SELECT COUNT(*) FROM board_posts p WHERE {clause}", params[1:]).fetchone()[0]
    rows = connection.execute(
        _PUBLIC_POST + f"WHERE {clause} ORDER BY p.pinned DESC, p.post_id DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    return {"posts": [_public(row, viewer) for row in rows], "total": total, "page": page, "pageSize": PAGE_SIZE,
            "writeOpensEpoch": WRITE_OPENS_EPOCH}


def read_post(connection, post_id, viewer=None, count_view=False):
    post_id = _id(post_id)
    if count_view:
        connection.execute("UPDATE board_posts SET view_count=view_count+1 WHERE post_id=? AND status='active'", (post_id,))
    row = connection.execute(_PUBLIC_POST + "WHERE p.post_id=? AND p.status='active'", (ANONYMOUS, post_id)).fetchone()
    if not row:
        raise ValueError("글을 찾을 수 없거나 숨겨진 글입니다.")
    post = _public(row, viewer)
    post["body"] = connection.execute("SELECT body FROM board_posts WHERE post_id=?", (post_id,)).fetchone()[0]
    post["liked"] = bool(viewer) and connection.execute(
        "SELECT 1 FROM board_likes WHERE post_id=? AND user_id=?", (post_id, viewer)).fetchone() is not None
    comments = connection.execute(
        "SELECT c.comment_id,c.body,c.created_epoch,c.author_id,"
        "CASE WHEN c.anonymous=1 THEN ? ELSE COALESCE(u.nickname,'알 수 없음') END AS author "
        "FROM board_comments c LEFT JOIN users u ON u.user_id=c.author_id "
        "WHERE c.post_id=? AND c.status='active' ORDER BY c.comment_id", (ANONYMOUS, post_id)).fetchall()
    post["comments"] = [_public(comment, viewer) for comment in comments]
    return post


# ---------------------------------------------------------------------------
# Writing (login needed)
# ---------------------------------------------------------------------------
def create_post(connection, user, payload, ip):
    _require_open()
    board = payload.get("board")
    if board not in BOARDS:
        raise ValueError("게시판을 확인해주세요.")
    if board == "notice" and user["role"] != "admin":
        raise ValueError("공지는 관리자만 쓸 수 있습니다.")
    title = _text(payload.get("title"), "제목", TITLE_MAX)
    body = _text(payload.get("body"), "내용", BODY_MAX)
    asset = _asset(connection, board, payload.get("assetCode"))
    _throttle(connection, "board_posts", user["userId"], POSTS_PER_MINUTE, "글")
    now = int(time.time())
    cursor = connection.execute(
        "INSERT INTO board_posts(board,author_id,anonymous,title,body,asset_code,author_ip,created_epoch,updated_epoch) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (board, user["userId"], 1 if payload.get("anonymous") is True else 0, title, body, asset, _ip(ip), now, now))
    return cursor.lastrowid


def edit_post(connection, user, post_id, payload):
    row = _post(connection, _id(post_id))
    if row["author_id"] != user["userId"]:
        raise ValueError("본인이 쓴 글만 수정할 수 있습니다.")
    if row["status"] != "active":
        raise ValueError("숨겨진 글은 수정할 수 없습니다.")
    connection.execute(
        "UPDATE board_posts SET title=?,body=?,asset_code=?,anonymous=?,updated_epoch=? WHERE post_id=?",
        (_text(payload.get("title"), "제목", TITLE_MAX), _text(payload.get("body"), "내용", BODY_MAX),
         _asset(connection, row["board"], payload.get("assetCode")),
         1 if payload.get("anonymous") is True else 0, int(time.time()), row["post_id"]))


def delete_post(connection, user, post_id):
    row = _post(connection, _id(post_id))
    if row["author_id"] != user["userId"]:
        raise ValueError("본인이 쓴 글만 삭제할 수 있습니다.")
    connection.execute("UPDATE board_posts SET status='deleted',pinned=0 WHERE post_id=?", (row["post_id"],))


def add_comment(connection, user, post_id, payload, ip):
    _require_open()
    row = _post(connection, _id(post_id))
    if row["status"] != "active":
        raise ValueError("숨겨진 글에는 댓글을 달 수 없습니다.")
    body = _text(payload.get("body"), "댓글", COMMENT_MAX)
    _throttle(connection, "board_comments", user["userId"], COMMENTS_PER_MINUTE, "댓글")
    cursor = connection.execute(
        "INSERT INTO board_comments(post_id,author_id,anonymous,body,author_ip,created_epoch) VALUES (?,?,?,?,?,?)",
        (row["post_id"], user["userId"], 1 if payload.get("anonymous") is True else 0, body, _ip(ip), int(time.time())))
    return cursor.lastrowid


def delete_comment(connection, user, comment_id):
    row = connection.execute("SELECT author_id,status FROM board_comments WHERE comment_id=?", (_id(comment_id, "댓글"),)).fetchone()
    if not row or row["status"] == "deleted":
        raise ValueError("댓글을 찾을 수 없습니다.")
    if row["author_id"] != user["userId"]:
        raise ValueError("본인이 쓴 댓글만 삭제할 수 있습니다.")
    connection.execute("UPDATE board_comments SET status='deleted' WHERE comment_id=?", (int(comment_id),))


def toggle_like(connection, user, post_id):
    """Like or unlike; returns whether the post is liked afterwards."""
    row = _post(connection, _id(post_id))
    if row["status"] != "active":
        raise ValueError("숨겨진 글입니다.")
    removed = connection.execute("DELETE FROM board_likes WHERE post_id=? AND user_id=?", (row["post_id"], user["userId"])).rowcount
    if not removed:
        connection.execute("INSERT INTO board_likes(post_id,user_id,created_epoch) VALUES (?,?,?)",
                           (row["post_id"], user["userId"], int(time.time())))
    return not removed


def report(connection, user, payload, ip):
    """Report a post or a comment once. Enough distinct reports hide it for review."""
    _require_open()
    reason = _text(payload.get("reason"), "신고 사유", REASON_MAX)
    if payload.get("postId") is not None:
        target, table, key = _id(payload.get("postId")), "board_posts", "post_id"
        _post(connection, target)
    elif payload.get("commentId") is not None:
        target, table, key = _id(payload.get("commentId"), "댓글"), "board_comments", "comment_id"
        if not connection.execute("SELECT 1 FROM board_comments WHERE comment_id=? AND status<>'deleted'", (target,)).fetchone():
            raise ValueError("댓글을 찾을 수 없습니다.")
    else:
        raise ValueError("신고할 글이나 댓글을 골라 주세요.")
    if connection.execute(f"SELECT 1 FROM board_reports WHERE reporter_id=? AND {key}=?", (user["userId"], target)).fetchone():
        raise ValueError("이미 신고했습니다.")
    connection.execute(
        f"INSERT INTO board_reports({key},reporter_id,reason,reporter_ip,created_epoch) VALUES (?,?,?,?,?)",
        (target, user["userId"], reason, _ip(ip), int(time.time())))
    open_reports = connection.execute(
        f"SELECT COUNT(DISTINCT reporter_id) FROM board_reports WHERE {key}=? AND status='new'", (target,)).fetchone()[0]
    if open_reports >= AUTO_HIDE_REPORTS:
        connection.execute(f"UPDATE {table} SET status='hidden',hidden_reason='reports' WHERE {key}=? AND status='active'", (target,))


# ---------------------------------------------------------------------------
# Administration (dashboard)
# ---------------------------------------------------------------------------
def admin_list(connection, status="all", reported=False, search=""):
    """Posts with author ID and IP. ``reported`` keeps only those with unresolved reports."""
    where, params = [], []
    if status in {"active", "hidden", "deleted"}:
        where.append("p.status=?"); params.append(status)
    if reported:
        where.append("EXISTS(SELECT 1 FROM board_reports r WHERE r.status='new' AND (r.post_id=p.post_id "
                     "OR r.comment_id IN (SELECT comment_id FROM board_comments c WHERE c.post_id=p.post_id)))")
    term = str(search or "").strip()[:50]
    if term:
        where.append("(p.title LIKE ? OR p.body LIKE ? OR p.author_id LIKE ?)"); params += [f"%{term}%"] * 3
    rows = connection.execute(
        "SELECT p.post_id,p.board,p.title,p.body,p.asset_code,p.pinned,p.status,p.hidden_reason,p.anonymous,"
        "p.author_id,COALESCE(u.nickname,'') AS nickname,p.author_ip,p.view_count,p.created_epoch,"
        "(SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.post_id AND c.status='active') AS comment_count,"
        "(SELECT COUNT(*) FROM board_reports r WHERE r.post_id=p.post_id AND r.status='new') AS open_reports "
        f"FROM board_posts p LEFT JOIN users u ON u.user_id=p.author_id {'WHERE ' + ' AND '.join(where) if where else ''} "
        "ORDER BY p.post_id DESC LIMIT 200", params).fetchall()
    reports = connection.execute(
        "SELECT r.report_id,r.post_id,r.comment_id,COALESCE(r.post_id,c.post_id) AS target_post,r.reporter_id,"
        "r.reason,r.reporter_ip,r.created_epoch,c.body AS comment_body,c.author_id AS comment_author,"
        "c.author_ip AS comment_ip,c.status AS comment_status "
        "FROM board_reports r LEFT JOIN board_comments c ON c.comment_id=r.comment_id "
        "WHERE r.status='new' ORDER BY r.report_id DESC LIMIT 200").fetchall()
    return {"posts": [dict(row) for row in rows], "reports": [dict(row) for row in reports]}


def admin_action(connection, kind, target, action):
    """hide / restore / pin / unpin / purge a post, hide / restore / purge a comment, resolve a report."""
    target = _id(target, "대상")
    if kind == "post":
        if not connection.execute("SELECT 1 FROM board_posts WHERE post_id=?", (target,)).fetchone():
            raise ValueError("글을 찾을 수 없습니다.")
        statements = {
            "hide": "UPDATE board_posts SET status='hidden',hidden_reason='admin',pinned=0 WHERE post_id=?",
            "restore": "UPDATE board_posts SET status='active',hidden_reason=NULL WHERE post_id=?",
            "pin": "UPDATE board_posts SET pinned=1 WHERE post_id=? AND status='active'",
            "unpin": "UPDATE board_posts SET pinned=0 WHERE post_id=?",
            "purge": "DELETE FROM board_posts WHERE post_id=?",
        }
    elif kind == "comment":
        if not connection.execute("SELECT 1 FROM board_comments WHERE comment_id=?", (target,)).fetchone():
            raise ValueError("댓글을 찾을 수 없습니다.")
        statements = {
            "hide": "UPDATE board_comments SET status='hidden',hidden_reason='admin' WHERE comment_id=?",
            "restore": "UPDATE board_comments SET status='active',hidden_reason=NULL WHERE comment_id=?",
            "purge": "DELETE FROM board_comments WHERE comment_id=?",
        }
    elif kind == "report":
        statements = {"resolve": "UPDATE board_reports SET status='done' WHERE report_id=?"}
    else:
        raise ValueError("대상 종류를 확인해주세요.")
    if action not in statements:
        raise ValueError("작업을 확인해주세요.")
    changed = connection.execute(statements[action], (target,)).rowcount
    if action == "restore":
        # Restoring after review closes the reports that hid it.
        connection.execute(f"UPDATE board_reports SET status='done' WHERE {'post_id' if kind == 'post' else 'comment_id'}=?", (target,))
    return changed
