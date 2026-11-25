"""Every link the bot and the screens give to the documents points at the public site (GitHub Pages), never at a Notion page."""
import re
import unittest
from pathlib import Path

from functions.public_links import HELP_URL, PRIVACY_URL, SITE, TERMS_URL, document_url

ROOT = Path(__file__).resolve().parents[1]


class PublicLinksTests(unittest.TestCase):
    def test_the_document_pages_live_under_the_public_site(self):
        self.assertTrue(SITE.startswith("https://") and SITE.endswith("/"))
        self.assertEqual((HELP_URL, TERMS_URL, PRIVACY_URL), (SITE + "help/", SITE + "terms/", SITE + "privacy/"))
        self.assertEqual(document_url("privacy"), PRIVACY_URL)
        self.assertEqual(document_url("operations"), SITE + "operations/")
        self.assertEqual(document_url("something-else"), HELP_URL, "an unknown document falls back to the help page")

    def test_no_source_file_links_to_notion(self):
        found = []
        for folder in ("functions", "managers", "web_api", "apps/hts/src", "apps/dashboard/src", "apps/shared", "apps/site/src", "apps/site"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".html"} and "node_modules" not in path.parts and "dist" not in path.parts:
                    if re.search(r"notion\.(site|so)", path.read_text(encoding="utf-8", errors="ignore")):
                        found.append(str(path.relative_to(ROOT)))
        self.assertEqual(sorted(set(found)), [])


if __name__ == "__main__":
    unittest.main()
