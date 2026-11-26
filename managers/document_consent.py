"""Consent to an unfavourable revision of the terms or the privacy policy.

When the console saves a legal document with "불리한 개정" ticked, a campaign is opened: every member gets a request. The bot sends each
one a direct message with the changes and a consent button; the button records (campaign, member, time). Once the revision is in force,
a member who was asked and has not agreed is held (bot commands and the web) until they agree or withdraw. Members who register after
the announcement agreed to the terms in force at that time, so they have no request.
"""
import time

SUMMARY_MAX = 3500  # an embed description holds 4096; the rest of the message needs room


def _has_table(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


# While a member is held these stay open: the session itself (so the screens can ask), agreeing, leaving, signing in/out.
OPEN_EXACT = {"/internal/me", "/internal/me/consent", "/internal/me/withdraw", "/internal/health"}
OPEN_PREFIXES = ("/internal/auth/", "/internal/oidc/")


class DocumentConsent:
    @staticmethod
    def path_is_exempt(path):
        return path in OPEN_EXACT or path.startswith(OPEN_PREFIXES)

    @staticmethod
    def ensure(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS document_consent_campaigns (campaign_id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, "
            "title TEXT NOT NULL, revision_sha256 TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', effective_epoch INTEGER NOT NULL, "
            "created_epoch INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS document_consents (campaign_id INTEGER NOT NULL REFERENCES document_consent_campaigns(campaign_id) "
            "ON DELETE CASCADE, user_id TEXT NOT NULL, dm_status TEXT NOT NULL DEFAULT 'pending', dm_epoch INTEGER, consented_epoch INTEGER, "
            "PRIMARY KEY (campaign_id, user_id))")

    @classmethod
    def open_campaign(cls, conn, document_id, title, revision_sha256, summary, effective_epoch, now=None):
        """Ask every active member to consent to this revision; an earlier open campaign for the same document is replaced."""
        cls.ensure(conn)
        now = int(time.time() if now is None else now)
        conn.execute("UPDATE document_consent_campaigns SET status='replaced' WHERE document_id=? AND status='open'", (document_id,))
        text = str(summary or "")
        if len(text) > SUMMARY_MAX:
            text = text[:SUMMARY_MAX - 1] + "…"
        campaign_id = conn.execute(
            "INSERT INTO document_consent_campaigns(document_id,title,revision_sha256,summary,effective_epoch,created_epoch) VALUES (?,?,?,?,?,?)",
            (document_id, title, revision_sha256, text, int(effective_epoch), now)).lastrowid
        recipients = conn.execute(
            "INSERT INTO document_consents(campaign_id,user_id) SELECT ?, user_id FROM users WHERE role='user' AND status='active'",
            (campaign_id,)).rowcount
        return {"campaignId": campaign_id, "recipients": recipients}

    @classmethod
    def cancel(cls, conn, document_id):
        """The announced revision was withdrawn: nobody is held for it. Returns how many campaigns were closed."""
        if not _has_table(conn, "document_consent_campaigns"):
            return 0
        return conn.execute("UPDATE document_consent_campaigns SET status='cancelled' WHERE document_id=? AND status='open'", (document_id,)).rowcount

    @classmethod
    def pending_dm(cls, conn, limit=20):
        if not _has_table(conn, "document_consents"):
            return []
        return [dict(row) for row in conn.execute(
            "SELECT c.campaign_id,c.document_id,c.title,c.summary,c.effective_epoch,d.user_id FROM document_consents d "
            "JOIN document_consent_campaigns c ON c.campaign_id=d.campaign_id WHERE c.status='open' AND d.dm_status='pending' "
            "ORDER BY c.campaign_id,d.user_id LIMIT ?", (int(limit),)).fetchall()]

    @classmethod
    def mark_dm(cls, conn, campaign_id, user_id, ok, now=None):
        conn.execute("UPDATE document_consents SET dm_status=?,dm_epoch=? WHERE campaign_id=? AND user_id=?",
                     ("sent" if ok else "failed", int(time.time() if now is None else now), int(campaign_id), str(user_id)))

    @classmethod
    def consent(cls, conn, campaign_id, user_id, now=None):
        """Record the member's agreement: 'consented', 'already' (the first time stays on record) or 'unknown' (not asked, or closed)."""
        if not _has_table(conn, "document_consents"):
            return "unknown"
        row = conn.execute(
            "SELECT d.consented_epoch,c.status FROM document_consents d JOIN document_consent_campaigns c ON c.campaign_id=d.campaign_id "
            "WHERE d.campaign_id=? AND d.user_id=?", (int(campaign_id), str(user_id))).fetchone()
        if not row or row["status"] != "open":
            return "unknown"
        if row["consented_epoch"] is not None:
            return "already"
        conn.execute("UPDATE document_consents SET consented_epoch=? WHERE campaign_id=? AND user_id=?",
                     (int(time.time() if now is None else now), int(campaign_id), str(user_id)))
        return "consented"

    @classmethod
    def consented(cls, conn, campaign_id, user_id):
        """True if the member agreed, False if they were asked and have not, None if they were never asked."""
        if not _has_table(conn, "document_consents"):
            return None
        row = conn.execute("SELECT consented_epoch FROM document_consents WHERE campaign_id=? AND user_id=?", (int(campaign_id), str(user_id))).fetchone()
        return None if row is None else row["consented_epoch"] is not None

    @classmethod
    def outstanding(cls, conn, user_id, now=None):
        """Revisions already in force that this member was asked about and has not agreed to."""
        if not _has_table(conn, "document_consents"):
            return []
        now = int(time.time() if now is None else now)
        return [dict(row) for row in conn.execute(
            "SELECT c.campaign_id,c.document_id,c.title,c.summary,c.effective_epoch FROM document_consents d "
            "JOIN document_consent_campaigns c ON c.campaign_id=d.campaign_id WHERE d.user_id=? AND c.status='open' "
            "AND c.effective_epoch<=? AND d.consented_epoch IS NULL ORDER BY c.campaign_id", (str(user_id), now)).fetchall()]
