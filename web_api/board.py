"""HTS board: boards (공지·자유·시장 토론 and any an administrator adds), each
with optional categories (말머리).

Anyone may read; writing, commenting, liking and reporting need a login. A
post or comment may be shown as 익명, but the author ID and the client IP are
always stored, and only administrators can see them.

Every function takes an open connection, so the caller owns the transaction.
When an author deletes a post or comment it is removed from the database at
once (a post takes its comments, reactions and reports with it). Replies to a
deleted comment stay, as ordinary comments. Administrators may also hide.
"""
import os
import re
import time
from pathlib import Path

from database.integrity import PRODUCT_CATALOG

# Boards that exist from the start; administrators can edit, add and delete boards.
DEFAULT_BOARDS = (
    # board_id, name, description, admin_only, allow_asset, sort_order
    ("notice", "공지", "운영진이 알리는 소식입니다.", 1, 0, 0),
    ("free", "자유", "자유롭게 이야기하는 곳입니다.", 0, 0, 1),
    ("market", "시장 토론", "종목 코드를 붙여 종목별로 이야기합니다.", 0, 1, 2),
)
BOARD_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
BOARD_NAME_MAX, BOARD_DESCRIPTION_MAX, CATEGORY_NAME_MAX = 30, 200, 20
PAGE_SIZE = 20
# Anyone who can write may attach files and links and open a poll.
FILE_MAX_BYTES = 10 * 1024 * 1024
FILES_PER_POST, LINKS_PER_POST = 5, 10
UPLOADS_PER_HOUR = 20
FILE_TYPES = {  # extension -> media type sent on download
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "txt": "text/plain", "csv": "text/csv", "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "hwp": "application/x-hwp", "hwpx": "application/x-hwp",
}
STORED_NAME = re.compile(r"[a-f0-9]{32}\.[a-z0-9]{1,5}")
# Uploads not attached to a post within a day are discarded.
PENDING_UPLOAD_SECONDS = 86400
POLL_OPTIONS_MIN, POLL_OPTIONS_MAX = 2, 10
POLL_QUESTION_MAX, POLL_OPTION_MAX = 200, 50
POLL_LONGEST_SECONDS = 90 * 86400
TITLE_MAX, BODY_MAX, COMMENT_MAX, REASON_MAX = 100, 10_000, 1_000, 300
# Per author: at most this many posts / comments in the last 60 seconds.
POSTS_PER_MINUTE, COMMENTS_PER_MINUTE = 2, 10
# Distinct reporters that hide a post or comment until an administrator reviews it.
AUTO_HIDE_REPORTS = 3
ANONYMOUS = "익명"
# 개인정보 처리방침: 게시판 접속 IP는 작성(신고) 후 1년이 지나면 파기한다.
# The rows stay; only the address is erased.
IP_RETENTION_SECONDS = 365 * 86400
# Writing stores the client IP. Until WRITE_OPENS_EPOCH the board is read-only:
# posts, comments, uploads and reports are refused; reactions and votes are
# not. Policy changes no longer close the board: an announced revision only
# takes effect after its notice period (web_api/documents.py) and the version
# in force applies meanwhile. The board opened 2026-10-17.
WRITE_OPENS_EPOCH = 1792162800  # 2026-10-17 00:00 KST


def write_opens_epoch():
    return WRITE_OPENS_EPOCH


def _require_open(now=None):
    opens = write_opens_epoch()
    if (now if now is not None else time.time()) < opens:
        day = time.strftime("%Y년 %-m월 %-d일", time.gmtime(opens + 9 * 3600))
        raise ValueError(f"게시판 글쓰기·댓글·신고는 {day}부터 이용할 수 있습니다. 지금은 읽기만 가능합니다.")


_POSTS_TABLE = """CREATE TABLE {name} (
            post_id INTEGER PRIMARY KEY AUTOINCREMENT,
            board TEXT NOT NULL REFERENCES board_boards(board_id),
            category_id INTEGER REFERENCES board_categories(category_id) ON DELETE SET NULL,
            author_id TEXT NOT NULL REFERENCES users(user_id),
            author_admin INTEGER NOT NULL DEFAULT 0 CHECK(author_admin IN (0,1)),
            anonymous INTEGER NOT NULL DEFAULT 0 CHECK(anonymous IN (0,1)),
            title TEXT NOT NULL, body TEXT NOT NULL, asset_code TEXT,
            pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
            notice INTEGER NOT NULL DEFAULT 0 CHECK(notice IN (0,1)),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','hidden','deleted')),
            hidden_reason TEXT, author_ip TEXT NOT NULL,
            view_count INTEGER NOT NULL DEFAULT 0,
            created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL)"""
_POSTS_INDEXES = """
        CREATE INDEX IF NOT EXISTS idx_board_posts_list ON board_posts(board, status, pinned, post_id);
        CREATE INDEX IF NOT EXISTS idx_board_posts_notice ON board_posts(notice, status, post_id);
        CREATE INDEX IF NOT EXISTS idx_board_posts_category ON board_posts(category_id);
        CREATE INDEX IF NOT EXISTS idx_board_posts_asset ON board_posts(asset_code, status, post_id);
        CREATE INDEX IF NOT EXISTS idx_board_posts_author ON board_posts(author_id, created_epoch);"""


def ensure_tables(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS board_boards (
            board_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            admin_only INTEGER NOT NULL DEFAULT 0 CHECK(admin_only IN (0,1)),
            allow_asset INTEGER NOT NULL DEFAULT 0 CHECK(allow_asset IN (0,1)),
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_epoch INTEGER NOT NULL);
        -- Who may see a secret board (administrators always can).
        CREATE TABLE IF NOT EXISTS board_members (
            board_id TEXT NOT NULL REFERENCES board_boards(board_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(user_id),
            added_epoch INTEGER NOT NULL,
            PRIMARY KEY(board_id, user_id));
        CREATE TABLE IF NOT EXISTS board_categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id TEXT NOT NULL REFERENCES board_boards(board_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(board_id, name));
    """)
    board_columns = {row[1] for row in connection.execute("PRAGMA table_info(board_boards)")}
    for column, definition in (("secret", "INTEGER NOT NULL DEFAULT 0"), ("suspended", "INTEGER NOT NULL DEFAULT 0"),
                               ("suspended_reason", "TEXT NOT NULL DEFAULT ''"), ("suspended_until", "INTEGER")):
        if column not in board_columns:
            connection.execute(f"ALTER TABLE board_boards ADD COLUMN {column} {definition}")
    connection.executemany(
        "INSERT OR IGNORE INTO board_boards(board_id,name,description,admin_only,allow_asset,sort_order,created_epoch) "
        "VALUES (?,?,?,?,?,?,0)", DEFAULT_BOARDS)
    connection.commit()
    existing = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='board_posts'").fetchone()
    if not existing:
        connection.executescript(_POSTS_TABLE.format(name="board_posts") + ";" + _POSTS_INDEXES)
    elif "category_id" not in existing[0]:
        _rebuild_posts(connection)
    connection.executescript("""
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
        -- One reaction per person: 1 = 좋아요, -1 = 싫어요.
        CREATE TABLE IF NOT EXISTS board_post_reactions (
            post_id INTEGER NOT NULL REFERENCES board_posts(post_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(user_id),
            value INTEGER NOT NULL CHECK(value IN (1,-1)),
            created_epoch INTEGER NOT NULL,
            PRIMARY KEY(post_id, user_id));
        CREATE TABLE IF NOT EXISTS board_comment_reactions (
            comment_id INTEGER NOT NULL REFERENCES board_comments(comment_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(user_id),
            value INTEGER NOT NULL CHECK(value IN (1,-1)),
            created_epoch INTEGER NOT NULL,
            PRIMARY KEY(comment_id, user_id));
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
        -- Files (stored on disk under stored_name) and links. post_id is NULL
        -- for an upload that has not been attached to a post yet.
        CREATE TABLE IF NOT EXISTS board_attachments (
            attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER REFERENCES board_posts(post_id) ON DELETE CASCADE,
            uploader_id TEXT NOT NULL REFERENCES users(user_id),
            kind TEXT NOT NULL CHECK(kind IN ('file','link')),
            name TEXT NOT NULL, url TEXT, stored_name TEXT UNIQUE, size INTEGER, sha256 TEXT,
            created_epoch INTEGER NOT NULL,
            CHECK((kind='file') = (stored_name IS NOT NULL)), CHECK((kind='link') = (url IS NOT NULL)));
        CREATE INDEX IF NOT EXISTS idx_board_attachments_post ON board_attachments(post_id, attachment_id);
        CREATE TABLE IF NOT EXISTS board_polls (
            poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL UNIQUE REFERENCES board_posts(post_id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            anonymous INTEGER NOT NULL DEFAULT 1 CHECK(anonymous IN (0,1)),
            multiple INTEGER NOT NULL DEFAULT 0 CHECK(multiple IN (0,1)),
            closes_epoch INTEGER, closed_epoch INTEGER,
            created_epoch INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS board_poll_options (
            option_id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL REFERENCES board_polls(poll_id) ON DELETE CASCADE,
            label TEXT NOT NULL, sort_order INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS board_poll_votes (
            poll_id INTEGER NOT NULL REFERENCES board_polls(poll_id) ON DELETE CASCADE,
            option_id INTEGER NOT NULL REFERENCES board_poll_options(option_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(user_id),
            created_epoch INTEGER NOT NULL,
            PRIMARY KEY(option_id, user_id));
        CREATE INDEX IF NOT EXISTS idx_board_poll_votes_poll ON board_poll_votes(poll_id, user_id);
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(board_comments)")}
    # Replies: a comment may answer one top-level comment of the same post.
    if "parent_id" not in columns:
        connection.execute("ALTER TABLE board_comments ADD COLUMN parent_id INTEGER "
                           "REFERENCES board_comments(comment_id) ON DELETE CASCADE")
    if "author_admin" not in columns:
        connection.execute("ALTER TABLE board_comments ADD COLUMN author_admin INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_board_comments_parent ON board_comments(parent_id)")
    # Earlier versions kept likes only; carry them over as 좋아요.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='board_likes'").fetchone():
        connection.execute("INSERT OR IGNORE INTO board_post_reactions(post_id,user_id,value,created_epoch) "
                           "SELECT post_id,user_id,1,created_epoch FROM board_likes")
        connection.execute("DROP TABLE board_likes")
    # Earlier versions only marked author deletions; remove those rows for good.
    connection.execute("UPDATE board_comments SET parent_id=NULL WHERE parent_id IN "
                       "(SELECT comment_id FROM board_comments WHERE status='deleted')")
    connection.execute("DELETE FROM board_comments WHERE status='deleted'")
    connection.execute("DELETE FROM board_posts WHERE status='deleted'")


def _rebuild_posts(connection):
    """Move board_posts off the fixed three-board CHECK onto board_boards.

    Foreign keys are off while the table is swapped, so comments, reactions and
    reports that point at posts are kept (dropping the old table would
    otherwise cascade into them). The check afterwards proves nothing dangles.
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript(
            "BEGIN;" + _POSTS_TABLE.format(name="board_posts_rebuilt") + ";"
            "INSERT INTO board_posts_rebuilt(post_id,board,author_id,anonymous,title,body,asset_code,pinned,status,"
            "hidden_reason,author_ip,view_count,created_epoch,updated_epoch) "
            "SELECT post_id,board,author_id,anonymous,title,body,asset_code,pinned,status,"
            "hidden_reason,author_ip,view_count,created_epoch,updated_epoch FROM board_posts;"
            "DROP TABLE board_posts;"
            "ALTER TABLE board_posts_rebuilt RENAME TO board_posts;" + _POSTS_INDEXES + "COMMIT;")
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"board_posts rebuild left dangling references: {problems[:5]}")
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def purge_expired_ips(connection, now=None):
    """Erase client addresses older than the retention period; returns how many were erased."""
    cutoff = int(now if now is not None else time.time()) - IP_RETENTION_SECONDS
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


def _flag(value):
    return 1 if value is True or value == 1 else 0


def _board(connection, board_id):
    row = connection.execute("SELECT * FROM board_boards WHERE board_id=?", (str(board_id or ""),)).fetchone()
    if not row:
        raise ValueError("게시판을 확인해주세요.")
    return row


# A board is visible to everyone unless it is secret; a secret board only to
# administrators and its members. Hidden boards answer like missing ones.
_VISIBLE = "(b.secret=0 OR ?=1 OR EXISTS(SELECT 1 FROM board_members m WHERE m.board_id=b.board_id AND m.user_id=?))"


def _can_see(connection, board, user_id, admin):
    return (not board["secret"] or admin or bool(user_id) and connection.execute(
        "SELECT 1 FROM board_members WHERE board_id=? AND user_id=?", (board["board_id"], user_id)).fetchone() is not None)


def _visible_board(connection, board_id, user_id=None, admin=False):
    board = _board(connection, board_id)
    if not _can_see(connection, board, user_id, admin):
        raise ValueError("게시판을 확인해주세요.")
    return board


def _suspension(board, now=None):
    """The board's suspension notice while it is in force, else None."""
    now = now if now is not None else time.time()
    if board["suspended"] and (board["suspended_until"] is None or now < board["suspended_until"]):
        until = board["suspended_until"]
        when = time.strftime(" %Y-%m-%d %H:%M까지", time.gmtime(until + 9 * 3600)) if until else ""
        return f"{board['name']} 게시판은 운영진이 정지했습니다{when}." + (f" 사유: {board['suspended_reason']}" if board["suspended_reason"] else "")
    return None


def _writable(connection, board_id, user):
    """The board row when ``user`` may write in it: visible to them and, unless they are an administrator, not suspended."""
    admin = user["role"] == "admin"
    board = _visible_board(connection, board_id, user["userId"], admin)
    notice = None if admin else _suspension(board)
    if notice:
        raise ValueError(notice)
    return board


def _category(connection, board_id, category_id):
    """A category of ``board_id`` (None when not given)."""
    if category_id in (None, ""):
        return None
    row = connection.execute("SELECT category_id FROM board_categories WHERE category_id=? AND board_id=?",
                             (_id(category_id, "카테고리"), board_id)).fetchone()
    if not row:
        raise ValueError("이 게시판의 카테고리를 골라 주세요.")
    return row[0]


def _asset(connection, board, code):
    """``board`` is a board_boards row; only boards that allow it keep an instrument code."""
    code = str(code or "").strip()
    if not board["allow_asset"] or not code:
        return None
    if not connection.execute(f"SELECT 1 FROM ({PRODUCT_CATALOG}) WHERE code=?", (code,)).fetchone():
        raise ValueError("종목 코드를 확인해주세요.")
    return code


def _visible_post(connection, post_id, user_id, admin):
    """The post row when its board is visible to the viewer; hidden boards look like missing posts."""
    row = connection.execute("SELECT * FROM board_posts WHERE post_id=?", (post_id,)).fetchone()
    if not row or not _can_see(connection, _board(connection, row["board"]), user_id, admin):
        raise ValueError("글을 찾을 수 없거나 숨겨진 글입니다.")
    return row


def _post_for(connection, user, post_id, frozen_too=False):
    """The post ``user`` acts on: on a board they can see and (unless ``frozen_too``
    or they are an administrator) one that is not suspended."""
    admin = user["role"] == "admin"
    row = _visible_post(connection, _id(post_id), user["userId"], admin)
    if row["status"] == "deleted":
        raise ValueError("글을 찾을 수 없습니다.")
    notice = None if admin or frozen_too else _suspension(_board(connection, row["board"]))
    if notice:
        raise ValueError(notice)
    return row


def _post(connection, post_id):
    row = connection.execute("SELECT * FROM board_posts WHERE post_id=?", (post_id,)).fetchone()
    if not row or row["status"] == "deleted":
        raise ValueError("글을 찾을 수 없습니다.")
    return row


# ---------------------------------------------------------------------------
# Reading (no login needed)
# ---------------------------------------------------------------------------
_PUBLIC_POST = (
    "SELECT p.post_id,p.board,b.name AS board_name,p.category_id,k.name AS category_name,p.title,p.asset_code,"
    "p.pinned,p.notice,p.author_admin,p.anonymous,p.view_count,p.created_epoch,p.updated_epoch,"
    "p.author_id,CASE WHEN p.anonymous=1 THEN ? ELSE COALESCE(u.nickname,'알 수 없음') END AS author,"
    "(SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.post_id AND c.status='active') AS comment_count,"
    "(SELECT COUNT(*) FROM board_post_reactions r WHERE r.post_id=p.post_id AND r.value=1) AS like_count,"
    "(SELECT COUNT(*) FROM board_post_reactions r WHERE r.post_id=p.post_id AND r.value=-1) AS dislike_count,"
    "EXISTS(SELECT 1 FROM board_attachments a WHERE a.post_id=p.post_id) AS has_attachments,"
    "EXISTS(SELECT 1 FROM board_polls v WHERE v.post_id=p.post_id) AS has_poll "
    "FROM board_posts p LEFT JOIN users u ON u.user_id=p.author_id "
    "JOIN board_boards b ON b.board_id=p.board LEFT JOIN board_categories k ON k.category_id=p.category_id ")
REMOVED = {"hidden": "숨겨진 댓글입니다."}


def _public(row, viewer):
    item = dict(row)
    item["mine"] = bool(viewer) and item.pop("author_id") == viewer
    item.pop("author_id", None)
    return item


def _reaction(connection, table, key, target, viewer):
    if not viewer:
        return 0
    row = connection.execute(f"SELECT value FROM {table} WHERE {key}=? AND user_id=?", (target, viewer)).fetchone()
    return row[0] if row else 0


def boards(connection, viewer=None, admin=False):
    """Boards the viewer may see, in display order, with categories, suspension
    and (for administrators) the members of secret boards."""
    categories = {}
    for row in connection.execute("SELECT category_id,board_id,name,sort_order FROM board_categories "
                                  "ORDER BY sort_order,category_id"):
        categories.setdefault(row["board_id"], []).append(
            {"category_id": row["category_id"], "name": row["name"], "sort_order": row["sort_order"]})
    rows = connection.execute(
        "SELECT b.*,(SELECT COUNT(*) FROM board_posts p WHERE p.board=b.board_id AND p.status='active') AS post_count "
        f"FROM board_boards b WHERE {_VISIBLE} ORDER BY b.sort_order,b.name", (1 if admin else 0, viewer or "")).fetchall()
    result = []
    for row in rows:
        item = {**dict(row), "categories": categories.get(row["board_id"], []), "suspension": _suspension(row)}
        if admin:
            item["members"] = [dict(member) for member in connection.execute(
                "SELECT m.user_id,COALESCE(u.nickname,'') AS nickname FROM board_members m LEFT JOIN users u "
                "ON u.user_id=m.user_id WHERE m.board_id=? ORDER BY m.added_epoch", (row["board_id"],))]
        result.append(item)
    return result


def list_posts(connection, board=None, search="", asset_code=None, page=1, viewer=None, category=None, admin=False):
    """Active posts, newest first.

    A post an administrator marks 공지 (any board, not only the 공지 board)
    leads its board's list with a [공지] label, then that board's pinned posts.
    The all-boards list (``board=None``) also starts with every 공지.
    """
    if board:
        _visible_board(connection, board, viewer, admin)
    page = max(1, min(_id(page, "페이지"), 10_000))
    where, params = ["p.status='active'", _VISIBLE], [ANONYMOUS, 1 if admin else 0, viewer or ""]
    if board:
        where.append("p.board=?"); params.append(board)
    if category not in (None, ""):
        where.append("p.category_id=?"); params.append(_id(category, "카테고리"))
    if asset_code:
        where.append("p.asset_code=?"); params.append(str(asset_code).strip())
    term = str(search or "").strip()[:50]
    if term:
        where.append("(p.title LIKE ? OR p.body LIKE ?)"); params += [f"%{term}%", f"%{term}%"]
    clause = " AND ".join(where)
    total = connection.execute(f"SELECT COUNT(*) FROM board_posts p JOIN board_boards b ON b.board_id=p.board WHERE {clause}",
                               params[1:]).fetchone()[0]
    order = "p.notice DESC," + (" p.pinned DESC," if board else "") + " p.post_id DESC"
    rows = connection.execute(
        _PUBLIC_POST + f"WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    return {"posts": [_public(row, viewer) for row in rows], "total": total, "page": page, "pageSize": PAGE_SIZE,
            "writeOpensEpoch": write_opens_epoch()}


def read_post(connection, post_id, viewer=None, count_view=False, admin=False):
    post_id = _id(post_id)
    _visible_post(connection, post_id, viewer, admin)
    if count_view:
        connection.execute("UPDATE board_posts SET view_count=view_count+1 WHERE post_id=? AND status='active'", (post_id,))
    row = connection.execute(_PUBLIC_POST + "WHERE p.post_id=? AND p.status='active'", (ANONYMOUS, post_id)).fetchone()
    if not row:
        raise ValueError("글을 찾을 수 없거나 숨겨진 글입니다.")
    post = _public(row, viewer)
    post["body"] = connection.execute("SELECT body FROM board_posts WHERE post_id=?", (post_id,)).fetchone()[0]
    post["my_reaction"] = _reaction(connection, "board_post_reactions", "post_id", post_id, viewer)
    rows = connection.execute(
        "SELECT c.comment_id,c.parent_id,c.status,c.body,c.created_epoch,c.author_id,c.author_admin,"
        "CASE WHEN c.anonymous=1 THEN ? ELSE COALESCE(u.nickname,'알 수 없음') END AS author,"
        "(SELECT COUNT(*) FROM board_comment_reactions r WHERE r.comment_id=c.comment_id AND r.value=1) AS like_count,"
        "(SELECT COUNT(*) FROM board_comment_reactions r WHERE r.comment_id=c.comment_id AND r.value=-1) AS dislike_count,"
        "(SELECT r.value FROM board_comment_reactions r WHERE r.comment_id=c.comment_id AND r.user_id=?) AS my_reaction "
        "FROM board_comments c LEFT JOIN users u ON u.user_id=c.author_id "
        "WHERE c.post_id=? ORDER BY c.comment_id", (ANONYMOUS, viewer or "", post_id)).fetchall()
    live_parents = {row["parent_id"] for row in rows if row["parent_id"] and row["status"] == "active"}
    comments = []
    for row in rows:
        if row["status"] != "active":
            # A hidden comment stays only as a placeholder above its live replies.
            if row["parent_id"] or row["comment_id"] not in live_parents or row["status"] not in REMOVED:
                continue
            comments.append({"comment_id": row["comment_id"], "parent_id": None, "removed": True,
                             "body": REMOVED[row["status"]], "author": "", "author_admin": 0, "created_epoch": row["created_epoch"],
                             "like_count": 0, "dislike_count": 0, "my_reaction": 0, "mine": False})
            continue
        comment = _public(row, viewer)
        comment.pop("status")
        comment["removed"] = False
        comment["my_reaction"] = comment["my_reaction"] or 0
        comments.append(comment)
    post["comments"] = comments
    post["attachments"] = [dict(row) for row in connection.execute(
        "SELECT attachment_id,kind,name,url,size FROM board_attachments WHERE post_id=? ORDER BY attachment_id", (post_id,))]
    poll = connection.execute("SELECT * FROM board_polls WHERE post_id=?", (post_id,)).fetchone()
    post["poll"] = _poll_view(connection, poll, viewer) if poll else None
    return post


# ---------------------------------------------------------------------------
# Writing (login needed)
# ---------------------------------------------------------------------------
def create_post(connection, user, payload, ip):
    _require_open()
    board = _writable(connection, payload.get("board"), user)
    admin = user["role"] == "admin"
    if board["admin_only"] and not admin:
        raise ValueError(f"{board['name']} 게시판은 관리자만 쓸 수 있습니다.")
    title = _text(payload.get("title"), "제목", TITLE_MAX)
    body = _text(payload.get("body"), "내용", BODY_MAX)
    category = _category(connection, board["board_id"], payload.get("categoryId"))
    asset = _asset(connection, board, payload.get("assetCode"))
    extras = _extras(user, payload)  # attachments and poll are checked before anything is written
    _throttle(connection, "board_posts", user["userId"], POSTS_PER_MINUTE, "글")
    now = int(time.time())
    # Only administrators may pin a post or make it a 공지 (also later, from the post).
    pinned, notice = (_flag(payload.get("pinned")), _flag(payload.get("notice"))) if admin else (0, 0)
    cursor = connection.execute(
        "INSERT INTO board_posts(board,category_id,author_id,author_admin,anonymous,title,body,asset_code,pinned,notice,"
        "author_ip,created_epoch,updated_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (board["board_id"], category, user["userId"], 1 if admin else 0, _flag(payload.get("anonymous")),
         title, body, asset, pinned, notice, _ip(ip), now, now))
    post_id = cursor.lastrowid
    _attach(connection, user, post_id, extras)
    if extras["poll"]:
        _create_poll(connection, post_id, extras["poll"])
    return post_id


def edit_post(connection, user, post_id, payload):
    row = _post_for(connection, user, post_id)
    if row["author_id"] != user["userId"]:
        raise ValueError("본인이 쓴 글만 수정할 수 있습니다.")
    extras = _extras(user, {**payload, "poll": None})
    if row["status"] != "active":
        raise ValueError("숨겨진 글은 수정할 수 없습니다.")
    board = _board(connection, row["board"])
    connection.execute(
        "UPDATE board_posts SET title=?,body=?,category_id=?,asset_code=?,anonymous=?,updated_epoch=? WHERE post_id=?",
        (_text(payload.get("title"), "제목", TITLE_MAX), _text(payload.get("body"), "내용", BODY_MAX),
         _category(connection, board["board_id"], payload.get("categoryId")),
         _asset(connection, board, payload.get("assetCode")),
         _flag(payload.get("anonymous")), int(time.time()), row["post_id"]))
    remove = payload.get("removeAttachments") or []
    if not isinstance(remove, list):
        raise ValueError("지울 첨부를 확인해주세요.")
    for attachment_id in remove:
        connection.execute("DELETE FROM board_attachments WHERE attachment_id=? AND post_id=?",
                           (_id(attachment_id, "첨부"), row["post_id"]))
    _attach(connection, user, row["post_id"], extras)


def delete_post(connection, user, post_id):
    row = _post(connection, _id(post_id))
    if row["author_id"] != user["userId"]:
        raise ValueError("본인이 쓴 글만 삭제할 수 있습니다.")
    # Comments, replies, reactions and reports go with it (ON DELETE CASCADE).
    connection.execute("DELETE FROM board_posts WHERE post_id=?", (row["post_id"],))


def add_comment(connection, user, post_id, payload, ip):
    _require_open()
    row = _post_for(connection, user, post_id)
    if row["status"] != "active":
        raise ValueError("숨겨진 글에는 댓글을 달 수 없습니다.")
    body = _text(payload.get("body"), "댓글", COMMENT_MAX)
    parent = None
    if payload.get("parentId") is not None:
        target = connection.execute("SELECT comment_id,post_id,parent_id,status FROM board_comments WHERE comment_id=?",
                                    (_id(payload.get("parentId"), "댓글"),)).fetchone()
        if not target or target["post_id"] != row["post_id"] or target["status"] != "active":
            raise ValueError("답글을 달 댓글을 찾을 수 없습니다.")
        # One level of replies: answering a reply joins the same thread.
        parent = target["parent_id"] or target["comment_id"]
    _throttle(connection, "board_comments", user["userId"], COMMENTS_PER_MINUTE, "댓글")
    cursor = connection.execute(
        "INSERT INTO board_comments(post_id,parent_id,author_id,author_admin,anonymous,body,author_ip,created_epoch) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (row["post_id"], parent, user["userId"], 1 if user["role"] == "admin" else 0, _flag(payload.get("anonymous")),
         body, _ip(ip), int(time.time())))
    return cursor.lastrowid


def delete_comment(connection, user, comment_id):
    row = connection.execute("SELECT author_id,status FROM board_comments WHERE comment_id=?", (_id(comment_id, "댓글"),)).fetchone()
    if not row or row["status"] == "deleted":
        raise ValueError("댓글을 찾을 수 없습니다.")
    if row["author_id"] != user["userId"]:
        raise ValueError("본인이 쓴 댓글만 삭제할 수 있습니다.")
    _remove_comment(connection, int(comment_id))


def _remove_comment(connection, comment_id):
    """Delete a comment for good; its replies stay as ordinary comments."""
    connection.execute("UPDATE board_comments SET parent_id=NULL WHERE parent_id=?", (comment_id,))
    connection.execute("DELETE FROM board_comments WHERE comment_id=?", (comment_id,))


def react(connection, user, kind, target_id, value):
    """좋아요(1) / 싫어요(-1) on a post or comment.

    Pressing the same reaction again removes it; the other one switches.
    Returns the viewer's reaction afterwards (1, -1 or 0) and both counts.
    """
    if isinstance(value, bool) or value not in (1, -1):
        raise ValueError("좋아요 또는 싫어요를 골라 주세요.")
    if kind == "post":
        table, key = "board_post_reactions", "post_id"
        target = _post_for(connection, user, target_id)
        if target["status"] != "active":
            raise ValueError("숨겨진 글입니다.")
        target = target["post_id"]
    elif kind == "comment":
        table, key = "board_comment_reactions", "comment_id"
        row = connection.execute("SELECT comment_id,post_id,status FROM board_comments WHERE comment_id=?", (_id(target_id, "댓글"),)).fetchone()
        if row:
            _post_for(connection, user, row["post_id"])
        if not row or row["status"] != "active":
            raise ValueError("댓글을 찾을 수 없습니다.")
        target = row["comment_id"]
    else:
        raise ValueError("대상 종류를 확인해주세요.")
    current = _reaction(connection, table, key, target, user["userId"])
    if current == value:
        connection.execute(f"DELETE FROM {table} WHERE {key}=? AND user_id=?", (target, user["userId"]))
        mine = 0
    else:
        connection.execute(f"INSERT INTO {table}({key},user_id,value,created_epoch) VALUES (?,?,?,?) "
                           f"ON CONFLICT({key},user_id) DO UPDATE SET value=excluded.value,created_epoch=excluded.created_epoch",
                           (target, user["userId"], value, int(time.time())))
        mine = value
    likes, dislikes = connection.execute(
        f"SELECT COALESCE(SUM(value=1),0),COALESCE(SUM(value=-1),0) FROM {table} WHERE {key}=?", (target,)).fetchone()
    return {"reaction": mine, "likes": likes, "dislikes": dislikes}


def report(connection, user, payload, ip):
    """Report a post or a comment once. Enough distinct reports hide it for review."""
    _require_open()
    reason = _text(payload.get("reason"), "신고 사유", REASON_MAX)
    if payload.get("postId") is not None:
        target, table, key = _id(payload.get("postId")), "board_posts", "post_id"
        _post_for(connection, user, target, frozen_too=True)  # reports stay open on a suspended board
    elif payload.get("commentId") is not None:
        target, table, key = _id(payload.get("commentId"), "댓글"), "board_comments", "comment_id"
        parent = connection.execute("SELECT post_id FROM board_comments WHERE comment_id=?", (target,)).fetchone()
        if parent:
            _post_for(connection, user, parent[0], frozen_too=True)
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
        "SELECT p.post_id,p.board,b.name AS board_name,p.category_id,k.name AS category_name,p.title,p.body,p.asset_code,"
        "p.pinned,p.notice,p.author_admin,p.status,p.hidden_reason,p.anonymous,"
        "p.author_id,COALESCE(u.nickname,'') AS nickname,p.author_ip,p.view_count,p.created_epoch,"
        "(SELECT COUNT(*) FROM board_comments c WHERE c.post_id=p.post_id AND c.status='active') AS comment_count,"
        "(SELECT COUNT(*) FROM board_reports r WHERE r.post_id=p.post_id AND r.status='new') AS open_reports "
        "FROM board_posts p LEFT JOIN users u ON u.user_id=p.author_id JOIN board_boards b ON b.board_id=p.board "
        f"LEFT JOIN board_categories k ON k.category_id=p.category_id {'WHERE ' + ' AND '.join(where) if where else ''} "
        "ORDER BY p.post_id DESC LIMIT 200", params).fetchall()
    reports = connection.execute(
        "SELECT r.report_id,r.post_id,r.comment_id,COALESCE(r.post_id,c.post_id) AS target_post,r.reporter_id,"
        "r.reason,r.reporter_ip,r.created_epoch,c.body AS comment_body,c.author_id AS comment_author,"
        "c.author_ip AS comment_ip,c.status AS comment_status "
        "FROM board_reports r LEFT JOIN board_comments c ON c.comment_id=r.comment_id "
        "WHERE r.status='new' ORDER BY r.report_id DESC LIMIT 200").fetchall()
    return {"posts": [dict(row) for row in rows], "reports": [dict(row) for row in reports]}


def admin_action(connection, kind, target, action, payload=None):
    """Moderation and board structure.

    post: hide / restore / pin / unpin / notice / unnotice / move / category / purge
    comment: hide / restore / purge; report: resolve
    board: create / edit / delete; category: create / edit / delete
    Returns how many rows changed (for create: the new id).
    """
    payload = payload or {}
    if kind == "board":
        return _board_action(connection, target, action, payload)
    if kind == "category":
        return _category_action(connection, target, action, payload)
    if kind == "poll":
        return poll_action(connection, target, action)
    target = _id(target, "대상")
    if kind == "post":
        row = connection.execute("SELECT * FROM board_posts WHERE post_id=?", (target,)).fetchone()
        if not row:
            raise ValueError("글을 찾을 수 없습니다.")
        if action == "move":
            board = _board(connection, payload.get("board"))
            category = _category(connection, board["board_id"], payload.get("categoryId"))
            return connection.execute(
                "UPDATE board_posts SET board=?,category_id=?,asset_code=CASE WHEN ?=1 THEN asset_code END,"
                "pinned=CASE WHEN board=? THEN pinned ELSE 0 END WHERE post_id=?",
                (board["board_id"], category, board["allow_asset"], board["board_id"], target)).rowcount
        if action == "category":
            return connection.execute("UPDATE board_posts SET category_id=? WHERE post_id=?",
                                      (_category(connection, row["board"], payload.get("categoryId")), target)).rowcount
        statements = {
            "hide": "UPDATE board_posts SET status='hidden',hidden_reason='admin',pinned=0,notice=0 WHERE post_id=?",
            "restore": "UPDATE board_posts SET status='active',hidden_reason=NULL WHERE post_id=?",
            "pin": "UPDATE board_posts SET pinned=1 WHERE post_id=? AND status='active'",
            "unpin": "UPDATE board_posts SET pinned=0 WHERE post_id=?",
            "notice": "UPDATE board_posts SET notice=1 WHERE post_id=? AND status='active'",
            "unnotice": "UPDATE board_posts SET notice=0 WHERE post_id=?",
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
    if kind == "comment" and action == "purge":
        # Replies to a purged comment stay, as ordinary comments.
        connection.execute("UPDATE board_comments SET parent_id=NULL WHERE parent_id=?", (target,))
    changed = connection.execute(statements[action], (target,)).rowcount
    if action == "restore":
        # Restoring after review closes the reports that hid it.
        connection.execute(f"UPDATE board_reports SET status='done' WHERE {'post_id' if kind == 'post' else 'comment_id'}=?", (target,))
    return changed


def _board_action(connection, board_id, action, payload):
    if action == "create":
        board_id = str(payload.get("boardId") or "").strip().lower()
        if not BOARD_ID.fullmatch(board_id):
            raise ValueError("게시판 ID는 영문 소문자·숫자·밑줄·하이픈으로 1~32자 입력해주세요.")
        if connection.execute("SELECT 1 FROM board_boards WHERE board_id=?", (board_id,)).fetchone():
            raise ValueError("이미 있는 게시판 ID입니다.")
        values = _board_values(connection, payload, None)
        connection.execute(
            "INSERT INTO board_boards(board_id,name,description,admin_only,allow_asset,sort_order,secret,created_epoch) "
            "VALUES (?,?,?,?,?,?,?,?)", (board_id, *values, int(time.time())))
        return 1
    board = _board(connection, board_id)
    if action == "edit":
        values = _board_values(connection, payload, board["board_id"])
        return connection.execute(
            "UPDATE board_boards SET name=?,description=?,admin_only=?,allow_asset=?,sort_order=?,secret=? WHERE board_id=?",
            (*values, board["board_id"])).rowcount
    if action == "suspend":
        # Writing stops (reading and reports go on); administrators still write. No end time: until resumed.
        reason = str(payload.get("reason") or "").strip()
        if len(reason) > 200:
            raise ValueError("정지 사유는 200자 이하로 입력해주세요.")
        until = payload.get("until")
        if until not in (None, ""):
            if isinstance(until, bool) or not isinstance(until, (int, float)) or int(until) <= time.time():
                raise ValueError("정지 해제 시각은 지금 이후로 정해 주세요.")
            until = int(until)
        else:
            until = None
        return connection.execute("UPDATE board_boards SET suspended=1,suspended_reason=?,suspended_until=? WHERE board_id=?",
                                  (reason, until, board["board_id"])).rowcount
    if action == "resume":
        return connection.execute("UPDATE board_boards SET suspended=0,suspended_reason='',suspended_until=NULL WHERE board_id=?",
                                  (board["board_id"],)).rowcount
    if action in {"member_add", "member_remove"}:
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            raise ValueError("Discord 아이디를 입력해주세요.")
        if action == "member_remove":
            return connection.execute("DELETE FROM board_members WHERE board_id=? AND user_id=?", (board["board_id"], user_id)).rowcount
        if not connection.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            raise ValueError("등록된 사용자의 Discord 아이디가 아닙니다.")
        return connection.execute("INSERT OR IGNORE INTO board_members(board_id,user_id,added_epoch) VALUES (?,?,?)",
                                  (board["board_id"], user_id, int(time.time()))).rowcount
    if action == "delete":
        if connection.execute("SELECT COUNT(*) FROM board_boards").fetchone()[0] <= 1:
            raise ValueError("게시판은 하나 이상 있어야 합니다.")
        if connection.execute("SELECT 1 FROM board_posts WHERE board=?", (board["board_id"],)).fetchone():
            target = payload.get("moveTo")
            if not target or target == board["board_id"]:
                raise ValueError("글이 있는 게시판입니다. 글을 옮길 다른 게시판을 골라 주세요.")
            target = _board(connection, target)
            # The board's categories go with it, so moved posts lose their category.
            connection.execute(
                "UPDATE board_posts SET board=?,category_id=NULL,pinned=0,asset_code=CASE WHEN ?=1 THEN asset_code END "
                "WHERE board=?", (target["board_id"], target["allow_asset"], board["board_id"]))
        return connection.execute("DELETE FROM board_boards WHERE board_id=?", (board["board_id"],)).rowcount
    raise ValueError("작업을 확인해주세요.")


def _board_values(connection, payload, own_id):
    name = _text(payload.get("name"), "게시판 이름", BOARD_NAME_MAX)
    if connection.execute("SELECT 1 FROM board_boards WHERE name=? AND board_id IS NOT ?", (name, own_id)).fetchone():
        raise ValueError("같은 이름의 게시판이 있습니다.")
    description = str(payload.get("description") or "").strip()
    if len(description) > BOARD_DESCRIPTION_MAX:
        raise ValueError(f"설명은 {BOARD_DESCRIPTION_MAX}자 이하로 입력해주세요.")
    return (name, description, _flag(payload.get("adminOnly")), _flag(payload.get("allowAsset")),
            _order(payload.get("sortOrder")), _flag(payload.get("secret")))


def _order(value):
    try:
        return max(-1000, min(1000, int(value or 0)))
    except (TypeError, ValueError):
        raise ValueError("순서는 정수로 입력해주세요.") from None


def _category_action(connection, category_id, action, payload):
    if action == "create":
        board = _board(connection, payload.get("boardId"))
        name = _text(payload.get("name"), "카테고리 이름", CATEGORY_NAME_MAX)
        if connection.execute("SELECT 1 FROM board_categories WHERE board_id=? AND name=?", (board["board_id"], name)).fetchone():
            raise ValueError("이 게시판에 같은 이름의 카테고리가 있습니다.")
        return connection.execute("INSERT INTO board_categories(board_id,name,sort_order) VALUES (?,?,?)",
                                  (board["board_id"], name, _order(payload.get("sortOrder")))).lastrowid
    row = connection.execute("SELECT * FROM board_categories WHERE category_id=?", (_id(category_id, "카테고리"),)).fetchone()
    if not row:
        raise ValueError("카테고리를 찾을 수 없습니다.")
    if action == "edit":
        name = _text(payload.get("name"), "카테고리 이름", CATEGORY_NAME_MAX)
        if connection.execute("SELECT 1 FROM board_categories WHERE board_id=? AND name=? AND category_id<>?",
                              (row["board_id"], name, row["category_id"])).fetchone():
            raise ValueError("이 게시판에 같은 이름의 카테고리가 있습니다.")
        return connection.execute("UPDATE board_categories SET name=?,sort_order=? WHERE category_id=?",
                                  (name, _order(payload.get("sortOrder")), row["category_id"])).rowcount
    if action == "delete":
        # Its posts stay in the board, without a category (ON DELETE SET NULL).
        return connection.execute("DELETE FROM board_categories WHERE category_id=?", (row["category_id"],)).rowcount
    raise ValueError("작업을 확인해주세요.")


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def file_extension(name):
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in FILE_TYPES:
        raise ValueError("올릴 수 있는 파일: " + ", ".join(sorted(FILE_TYPES)))
    return extension


def register_upload(connection, user, payload):
    """Record a file the API server has just written; returns its pending attachment id."""
    _require_open()
    recent = connection.execute("SELECT COUNT(*) FROM board_attachments WHERE uploader_id=? AND kind='file' AND created_epoch>=?",
                                (user["userId"], int(time.time()) - 3600)).fetchone()[0]
    if recent >= UPLOADS_PER_HOUR:
        raise ValueError(f"파일은 한 시간에 {UPLOADS_PER_HOUR}개까지 올릴 수 있습니다. 잠시 후 다시 시도해주세요.")
    name = _text(payload.get("name"), "파일 이름", 120).replace("/", "_").replace("\\", "_")
    stored = str(payload.get("storedName") or "")
    if not STORED_NAME.fullmatch(stored) or stored.rsplit(".", 1)[1] != file_extension(name):
        raise ValueError("파일 정보를 확인해주세요.")
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= FILE_MAX_BYTES:
        raise ValueError(f"파일은 {FILE_MAX_BYTES // (1024 * 1024)}MB 이하만 올릴 수 있습니다.")
    sha256 = str(payload.get("sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("파일 정보를 확인해주세요.")
    return connection.execute(
        "INSERT INTO board_attachments(post_id,uploader_id,kind,name,stored_name,size,sha256,created_epoch) "
        "VALUES (NULL,?,'file',?,?,?,?,?)", (user["userId"], name, stored, size, sha256, int(time.time()))).lastrowid


def _extras(user, payload):
    """Validate attachments, links and poll of a post before it is written."""
    files, links, poll = payload.get("attachments") or [], payload.get("links") or [], payload.get("poll") or None
    if not isinstance(files, list) or not isinstance(links, list):
        raise ValueError("첨부를 확인해주세요.")
    clean = []
    for link in links:
        if not isinstance(link, dict):
            raise ValueError("링크를 확인해주세요.")
        url = str(link.get("url") or "").strip()
        if len(url) > 500 or not re.fullmatch(r"https?://[^\s<>\"]+", url):
            raise ValueError("링크는 http:// 또는 https://로 시작하는 500자 이하 주소여야 합니다.")
        clean.append((str(link.get("title") or "").strip()[:100] or url, url))
    return {"files": [_id(item, "첨부") for item in files], "links": clean,
            "poll": _poll_values(poll) if poll is not None else None}


def _attach(connection, user, post_id, extras):
    for attachment_id in extras["files"]:
        attached = connection.execute(
            "UPDATE board_attachments SET post_id=? WHERE attachment_id=? AND post_id IS NULL AND uploader_id=? AND kind='file'",
            (post_id, attachment_id, user["userId"])).rowcount
        if not attached:
            raise ValueError("올린 파일을 찾을 수 없습니다. 다시 올려 주세요.")
    for title, url in extras["links"]:
        connection.execute("INSERT INTO board_attachments(post_id,uploader_id,kind,name,url,created_epoch) VALUES (?,?,'link',?,?,?)",
                           (post_id, user["userId"], title, url, int(time.time())))
    counts = dict(connection.execute("SELECT kind,COUNT(*) FROM board_attachments WHERE post_id=? GROUP BY kind", (post_id,)).fetchall())
    if counts.get("file", 0) > FILES_PER_POST or counts.get("link", 0) > LINKS_PER_POST:
        raise ValueError(f"파일은 {FILES_PER_POST}개, 링크는 {LINKS_PER_POST}개까지 붙일 수 있습니다.")


def file_meta(connection, attachment_id, viewer=None, admin=False):
    """What the API server needs to send a file: only files of posts the viewer can read."""
    row = connection.execute(
        "SELECT a.name,a.stored_name,a.size FROM board_attachments a JOIN board_posts p ON p.post_id=a.post_id "
        f"JOIN board_boards b ON b.board_id=p.board WHERE a.attachment_id=? AND a.kind='file' AND p.status='active' AND {_VISIBLE}",
        (_id(attachment_id, "첨부"), 1 if admin else 0, viewer or "")).fetchone()
    if not row:
        raise LookupError("not_found")
    return {"name": row["name"], "storedName": row["stored_name"], "size": row["size"],
            "mime": FILE_TYPES[row["stored_name"].rsplit(".", 1)[1]]}


def cleanup_files(connection, directory, now=None, min_age=60):
    """Drop stale pending uploads, then delete files no attachment row points to.

    ``min_age`` spares files the API server is writing right now (the row comes
    just after the file). Returns the number of files deleted.
    """
    now = int(now if now is not None else time.time())
    connection.execute("DELETE FROM board_attachments WHERE post_id IS NULL AND created_epoch<?", (now - PENDING_UPLOAD_SECONDS,))
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    known = {row[0] for row in connection.execute("SELECT stored_name FROM board_attachments WHERE stored_name IS NOT NULL")}
    removed = 0
    for path in directory.iterdir():
        if path.is_file() and STORED_NAME.fullmatch(path.name) and path.name not in known:
            if now - path.stat().st_mtime >= min_age:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# Polls (설문/투표)
# ---------------------------------------------------------------------------
def _poll_values(poll):
    if not isinstance(poll, dict):
        raise ValueError("설문을 확인해주세요.")
    question = _text(poll.get("question"), "설문 질문", POLL_QUESTION_MAX)
    options = poll.get("options")
    if not isinstance(options, list):
        raise ValueError("선택지를 확인해주세요.")
    labels = [_text(option, "선택지", POLL_OPTION_MAX) for option in options]
    if not POLL_OPTIONS_MIN <= len(labels) <= POLL_OPTIONS_MAX or len(set(labels)) != len(labels):
        raise ValueError(f"선택지는 서로 다르게 {POLL_OPTIONS_MIN}~{POLL_OPTIONS_MAX}개 입력해주세요.")
    now = int(time.time())
    closes = poll.get("closesEpoch")
    if closes in (None, ""):
        closes = None
    else:
        if isinstance(closes, bool) or not isinstance(closes, (int, float)):
            raise ValueError("마감 시각을 확인해주세요.")
        closes = int(closes)
        if not now < closes <= now + POLL_LONGEST_SECONDS:
            raise ValueError("예약 마감은 지금 이후 90일 안으로 정해 주세요.")
    # Anonymous unless asked otherwise: only counts are shown.
    return {"question": question, "labels": labels, "anonymous": _flag(poll.get("anonymous", True)),
            "multiple": _flag(poll.get("multiple")), "closes": closes}


def _create_poll(connection, post_id, values):
    poll_id = connection.execute(
        "INSERT INTO board_polls(post_id,question,anonymous,multiple,closes_epoch,created_epoch) VALUES (?,?,?,?,?,?)",
        (post_id, values["question"], values["anonymous"], values["multiple"], values["closes"], int(time.time()))).lastrowid
    connection.executemany("INSERT INTO board_poll_options(poll_id,label,sort_order) VALUES (?,?,?)",
                           [(poll_id, label, order) for order, label in enumerate(values["labels"])])
    return poll_id


def _poll_open(poll, now=None):
    now = now if now is not None else time.time()
    return poll["closed_epoch"] is None and (poll["closes_epoch"] is None or now < poll["closes_epoch"])


def _poll_view(connection, poll, viewer):
    """Counts for everyone; voter names only on a poll that is not anonymous."""
    options = connection.execute(
        "SELECT o.option_id,o.label,(SELECT COUNT(*) FROM board_poll_votes v WHERE v.option_id=o.option_id) AS votes "
        "FROM board_poll_options o WHERE o.poll_id=? ORDER BY o.sort_order", (poll["poll_id"],)).fetchall()
    view = [dict(option) for option in options]
    if not poll["anonymous"]:
        for option in view:
            option["voters"] = [row[0] for row in connection.execute(
                "SELECT COALESCE(u.nickname,v.user_id) FROM board_poll_votes v LEFT JOIN users u ON u.user_id=v.user_id "
                "WHERE v.option_id=? ORDER BY v.created_epoch", (option["option_id"],))]
    mine = [row[0] for row in connection.execute(
        "SELECT option_id FROM board_poll_votes WHERE poll_id=? AND user_id=?", (poll["poll_id"], viewer or ""))]
    voters = connection.execute("SELECT COUNT(DISTINCT user_id) FROM board_poll_votes WHERE poll_id=?", (poll["poll_id"],)).fetchone()[0]
    return {"poll_id": poll["poll_id"], "question": poll["question"], "anonymous": poll["anonymous"],
            "multiple": poll["multiple"], "closes_epoch": poll["closes_epoch"], "closed_epoch": poll["closed_epoch"],
            "open": _poll_open(poll), "options": view, "my_votes": mine, "voters": voters}


def vote(connection, user, poll_id, option_ids):
    """Cast or change a vote; an empty list withdraws it. Returns the poll view."""
    poll = connection.execute(
        "SELECT v.* FROM board_polls v JOIN board_posts p ON p.post_id=v.post_id WHERE v.poll_id=? AND p.status='active'",
        (_id(poll_id, "설문"),)).fetchone()
    if not poll:
        raise ValueError("설문을 찾을 수 없습니다.")
    _post_for(connection, user, poll["post_id"])
    if not _poll_open(poll):
        raise ValueError("마감된 설문입니다.")
    if not isinstance(option_ids, list):
        raise ValueError("선택지를 골라 주세요.")
    chosen = {_id(option, "선택지") for option in option_ids}
    valid = {row[0] for row in connection.execute("SELECT option_id FROM board_poll_options WHERE poll_id=?", (poll["poll_id"],))}
    if not chosen <= valid:
        raise ValueError("이 설문의 선택지를 골라 주세요.")
    if len(chosen) > 1 and not poll["multiple"]:
        raise ValueError("하나만 고를 수 있는 설문입니다.")
    connection.execute("DELETE FROM board_poll_votes WHERE poll_id=? AND user_id=?", (poll["poll_id"], user["userId"]))
    now = int(time.time())
    connection.executemany("INSERT INTO board_poll_votes(poll_id,option_id,user_id,created_epoch) VALUES (?,?,?,?)",
                           [(poll["poll_id"], option, user["userId"], now) for option in sorted(chosen)])
    return _poll_view(connection, poll, user["userId"])


def poll_action(connection, poll_id, action, user=None):
    """close: end voting now; reopen: open again without a scheduled close.

    ``user`` (when given) must be the post's author or an administrator.
    """
    poll_id = _id(poll_id, "설문")
    if user is not None and user["role"] != "admin":
        owner = connection.execute("SELECT p.author_id FROM board_polls v JOIN board_posts p ON p.post_id=v.post_id "
                                   "WHERE v.poll_id=?", (poll_id,)).fetchone()
        if not owner or owner[0] != user["userId"]:
            raise ValueError("설문을 만든 사람만 마감하거나 다시 열 수 있습니다.")
    if action == "close":
        return connection.execute("UPDATE board_polls SET closed_epoch=? WHERE poll_id=? AND closed_epoch IS NULL",
                                  (int(time.time()), poll_id)).rowcount
    if action == "reopen":
        return connection.execute("UPDATE board_polls SET closed_epoch=NULL,closes_epoch=NULL WHERE poll_id=?", (poll_id,)).rowcount
    raise ValueError("작업을 확인해주세요.")
