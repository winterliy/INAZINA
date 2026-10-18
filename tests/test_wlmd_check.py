import shutil
import tempfile
import unittest
from pathlib import Path

from tools import wlmd_check

REAL_ROOT = Path(__file__).resolve().parents[1] / "docs" / "wide-learning"

S1_SOURCE = """
### [S-101] 감독기관 신용정보 안내
- url: https://example.org/credit
- publisher: 감독기관
- tier: S1
- accessed: 2026-10-18
- added-by: CLAUDE
- license: 확인 필요
"""

CLAIM = """
### [C-001] CLAUDE · 2026-10-18 · T-04
- type: claim
- claim: 상환이력 비중이 가장 크다.
- sources: S-101
- locator: 표 1
- tier: S1
- confidence: medium
- status: proposed
- laplace: 4
"""

AGREE = """
### [G-001] CODEX · 2026-10-18 · T-04
- type: review
- re: C-001
- verdict: agree
- checked: S-101
"""

VERIFY = """
### [C-002] CLAUDE · 2026-10-19 · T-04
- type: correction
- re: C-001
- claim: 상환이력 비중이 가장 크다.
- sources: S-101
- tier: S1
- confidence: high
- status: verified
"""

APPLY = """
### [A-001] 상환이력 가중치 점검
- basis: C-002
- laplace: 4
- change: 연체 감점 비중을 문서와 맞춘다.
- reuse: managers/credit_score_manager.py
- impact: 대출 이용자 전체
- rollback: 정책값 복원
- tests: tests/test_credit_score.py
- status: proposed
- commit: -
"""


class WlmdCheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "wide-learning"
        shutil.copytree(REAL_ROOT, self.root)

    def tearDown(self):
        self.folder.cleanup()

    def append(self, relative, text):
        with open(self.root / relative, "a", encoding="utf-8") as handle:
            handle.write(text)

    def errors(self):
        return wlmd_check.Checker(self.root).run()

    def test_repository_records_are_valid(self):
        self.assertEqual(wlmd_check.Checker(REAL_ROOT).run(), [])

    def test_claim_review_verify_and_apply_flow_passes(self):
        self.append("registry.md", S1_SOURCE)
        self.append("threads/cthread-claude.md", CLAIM + VERIFY)
        self.append("threads/gthread-codex.md", AGREE)
        self.append("apply-queue.md", APPLY)
        self.append("wide-learning.md", "\n- 상환이력 비중이 가장 크다. [C-002]\n")
        self.assertEqual(self.errors(), [])

    def test_verification_needs_the_other_cli_to_check_sources(self):
        self.append("registry.md", S1_SOURCE)
        self.append("threads/cthread-claude.md", CLAIM + VERIFY)
        self.assertTrue(any("agree 검토" in error for error in self.errors()))
        self.append("threads/gthread-codex.md", AGREE.replace("checked: S-101", "checked: none"))
        self.assertTrue(any("agree 검토" in error for error in self.errors()))

    def test_s3_sources_cannot_verify(self):
        self.append("threads/cthread-claude.md", CLAIM.replace("S-101", "S-001").replace("S1", "S3")
                    + VERIFY.replace("S-101", "S-001").replace("S1", "S3"))
        self.append("threads/gthread-codex.md", AGREE.replace("S-101", "S-001"))
        self.assertTrue(any("S1 출처" in error for error in self.errors()))

    def test_unregistered_source_and_tier_mismatch_are_reported(self):
        self.append("threads/cthread-claude.md", CLAIM)
        self.assertTrue(any("S-101가 registry.md에 없습니다" in error for error in self.errors()))
        self.append("registry.md", S1_SOURCE.replace("tier: S1", "tier: S2"))
        self.assertTrue(any("최고 등급(S2)" in error for error in self.errors()))

    def test_draft_topics_and_foreign_ids_are_rejected(self):
        self.append("registry.md", S1_SOURCE)
        self.append("threads/cthread-claude.md", CLAIM.replace("T-04", "T-05"))
        self.append("threads/gthread-codex.md", CLAIM.replace("C-001", "C-002"))
        errors = self.errors()
        self.assertTrue(any("draft" in error for error in errors))
        self.assertTrue(any("G-NNN" in error for error in errors))

    def test_corrections_and_reviews_respect_authorship(self):
        self.append("registry.md", S1_SOURCE)
        self.append("threads/cthread-claude.md", CLAIM)
        self.append("threads/gthread-codex.md", VERIFY.replace("[C-002] CLAUDE", "[G-001] CODEX"))
        self.assertTrue(any("자기 항목에만" in error for error in self.errors()))

    def test_unsettled_entries_cannot_feed_apply_queue_or_summary(self):
        self.append("registry.md", S1_SOURCE)
        self.append("threads/cthread-claude.md", CLAIM)
        self.append("apply-queue.md", APPLY.replace("C-002", "C-001"))
        self.append("wide-learning.md", "\n- 미확정 [C-001]\n")
        errors = self.errors()
        self.assertTrue(any("최종 상태가 proposed" in error for error in errors))
        self.assertTrue(any("통합본에 넣을 수 없습니다" in error for error in errors))

    def test_registry_rejects_duplicate_urls_and_foreign_ranges(self):
        self.append("registry.md", S1_SOURCE + S1_SOURCE.replace("S-101", "S-201"))
        errors = self.errors()
        self.assertTrue(any("같은 URL" in error for error in errors))
        self.assertTrue(any("ID 범위" in error for error in errors))

    def test_fenced_examples_are_ignored(self):
        self.append("threads/cthread-claude.md", "\n```markdown\n" + CLAIM + "```\n")
        self.assertEqual(self.errors(), [])

    def test_append_only_comparison(self):
        old = "# 기록\n\n### [C-001] 항목\n- type: claim\n"
        self.assertTrue(wlmd_check.appended_only(old, old + "\n### [C-002] 다음\n"))
        self.assertFalse(wlmd_check.appended_only(old, old.replace("claim", "review")))


if __name__ == "__main__":
    unittest.main()
