"""A revision marked unfavourable is sent to every member by DM with a consent button; members who have not agreed once it is in force are held."""
import unittest

import test_inazina as fixtures
from managers.document_consent import DocumentConsent

NOW = 1_800_000_000
DAY = 86400


class DocumentConsentTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def open(self, effective=NOW + 30 * DAY, document="terms", sha="abc"):
        with self.db.atomic() as conn:
            return DocumentConsent.open_campaign(conn, document, "서비스 이용약관", sha, "- 제10조가 바뀝니다.", effective, now=NOW)

    def members(self, *extra):
        with self.db.atomic() as conn:
            for user_id, role, status in extra:
                conn.execute("INSERT OR REPLACE INTO users(user_id,nickname,nationality,cash,status,role) VALUES (?,?,?,0,?,?)", (user_id, user_id, '낙원', status, role))

    def test_every_active_member_gets_a_request_and_nobody_else(self):
        self.seed()
        self.members(('m1', 'user', 'active'), ('gone', 'user', 'withdrawn'), ('boss', 'admin', 'active'), ('state', 'nation', 'nation'), ('corp', 'company', 'active'))
        campaign = self.open()
        with self.db.atomic() as conn:
            ids = {row[0] for row in conn.execute("SELECT user_id FROM document_consents WHERE campaign_id=?", (campaign['campaignId'],))}
        self.assertTrue({'citizen', 'm1'} <= ids)
        self.assertFalse(ids & {'gone', 'boss', 'state', 'corp'})
        self.assertEqual(campaign['recipients'], len(ids))

    def test_dm_state_follows_each_attempt(self):
        self.seed()
        self.members(('m1', 'user', 'active'))
        campaign = self.open()
        with self.db.atomic() as conn:
            pending = DocumentConsent.pending_dm(conn)
            self.assertTrue(any(item['user_id'] == 'citizen' for item in pending))
            first = next(item for item in pending if item['user_id'] == 'citizen')
            self.assertEqual((first['title'], first['campaign_id']), ("서비스 이용약관", campaign['campaignId']))
            DocumentConsent.mark_dm(conn, campaign['campaignId'], 'citizen', ok=True, now=NOW)
            self.assertFalse(any(item['user_id'] == 'citizen' for item in DocumentConsent.pending_dm(conn)), 'a sent DM is not sent twice')
        campaign2 = self.open(document="privacy", sha="def")
        with self.db.atomic() as conn:
            DocumentConsent.mark_dm(conn, campaign2['campaignId'], 'm1', ok=False, now=NOW)
            self.assertEqual(conn.execute("SELECT dm_status FROM document_consents WHERE campaign_id=? AND user_id='m1'", (campaign2['campaignId'],)).fetchone()[0], 'failed')
            self.assertFalse(any(item['user_id'] == 'm1' and item['campaign_id'] == campaign2['campaignId'] for item in DocumentConsent.pending_dm(conn)),
                             'a member whose DMs are closed is not retried forever')

    def test_consenting_is_recorded_once_and_closed_campaigns_refuse_it(self):
        self.seed()
        campaign = self.open()['campaignId']
        self.members(('m1', 'user', 'active'))  # registered after the announcement: never asked
        with self.db.atomic() as conn:
            self.assertEqual(DocumentConsent.consent(conn, campaign, 'citizen', now=NOW + 5), 'consented')
            self.assertEqual(DocumentConsent.consent(conn, campaign, 'citizen', now=NOW + 99), 'already')
            self.assertEqual(conn.execute("SELECT consented_epoch FROM document_consents WHERE campaign_id=? AND user_id='citizen'", (campaign,)).fetchone()[0], NOW + 5,
                             'the first time stays on record')
            self.assertEqual(DocumentConsent.consent(conn, campaign, 'stranger', now=NOW), 'unknown')
            self.assertEqual(DocumentConsent.consent(conn, 9999, 'citizen', now=NOW), 'unknown')
            DocumentConsent.cancel(conn, 'terms')
            self.assertEqual(DocumentConsent.consent(conn, campaign, 'citizen', now=NOW), 'unknown', 'a withdrawn revision takes no more consent')

    def test_members_are_held_only_after_the_revision_is_in_force_and_until_they_agree(self):
        self.seed()
        campaign = self.open(effective=NOW + 30 * DAY)['campaignId']
        with self.db.atomic() as conn:
            self.assertEqual(DocumentConsent.outstanding(conn, 'citizen', now=NOW + 29 * DAY), [], 'not before the effective time')
            held = DocumentConsent.outstanding(conn, 'citizen', now=NOW + 30 * DAY)
            self.assertEqual([item['campaign_id'] for item in held], [campaign])
            self.assertEqual((held[0]['title'], held[0]['document_id']), ("서비스 이용약관", "terms"))
            DocumentConsent.consent(conn, campaign, 'citizen', now=NOW + 31 * DAY)
            self.assertEqual(DocumentConsent.outstanding(conn, 'citizen', now=NOW + 32 * DAY), [])

    def test_someone_who_registered_after_the_announcement_is_not_held(self):
        self.seed()
        self.open(effective=NOW + 30 * DAY)
        self.members(('late', 'user', 'active'))  # registered later: agreed to the terms in force at registration
        with self.db.atomic() as conn:
            self.assertEqual(DocumentConsent.outstanding(conn, 'late', now=NOW + 40 * DAY), [])

    def test_cancelling_or_replacing_the_announced_revision_lifts_the_hold(self):
        self.seed()
        self.open(effective=NOW + DAY)
        with self.db.atomic() as conn:
            self.assertEqual(DocumentConsent.cancel(conn, 'terms'), 1)
            self.assertEqual(DocumentConsent.outstanding(conn, 'citizen', now=NOW + 2 * DAY), [])
        self.open(effective=NOW + DAY, sha="one")
        second = self.open(effective=NOW + 3 * DAY, sha="two")['campaignId']  # a new save replaces the announced one
        with self.db.atomic() as conn:
            self.assertEqual([item['campaign_id'] for item in DocumentConsent.outstanding(conn, 'citizen', now=NOW + 4 * DAY)], [second])
            self.assertEqual(DocumentConsent.outstanding(conn, 'citizen', now=NOW + 2 * DAY), [], "the replaced one no longer holds, the new one is not in force yet")

    def test_a_long_summary_is_cut_to_fit_a_direct_message(self):
        self.seed()
        with self.db.atomic() as conn:
            campaign = DocumentConsent.open_campaign(conn, 'terms', '서비스 이용약관', 'abc', "줄 " * 5000, NOW + DAY, now=NOW)
            stored = conn.execute("SELECT summary FROM document_consent_campaigns WHERE campaign_id=?", (campaign['campaignId'],)).fetchone()[0]
        self.assertLessEqual(len(stored), 3500)


if __name__ == '__main__':
    unittest.main()


class ConsentGatePaths(unittest.TestCase):
    """While a member is held, only what is needed to agree, leave or see the state stays open."""

    def test_these_requests_stay_open(self):
        for path in ("/internal/me", "/internal/me/consent", "/internal/me/withdraw", "/internal/auth/logout", "/internal/auth/login",
                     "/internal/oidc/store/find", "/internal/health"):
            self.assertTrue(DocumentConsent.path_is_exempt(path), path)

    def test_everything_else_a_member_does_is_held(self):
        for path in ("/internal/me/portfolio", "/internal/me/tax-residency", "/internal/orders", "/internal/market/instruments", "/internal/me/consent-x",
                     "/internal/me/", "/internal/me/withdraw/extra"):
            self.assertFalse(DocumentConsent.path_is_exempt(path), path)
