"""IndexNow submission: the payload names the host, the key and where the key file is, and lists the sitemap's pages."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "submit_sitemap.py"
spec = importlib.util.spec_from_file_location("submit_sitemap", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://winterliy.github.io/INAZINA/</loc></url>
  <url><loc>https://winterliy.github.io/INAZINA/terms/</loc></url>
</urlset>"""


class SubmitSitemapTests(unittest.TestCase):
    def test_the_pages_are_read_from_the_sitemap(self):
        self.assertEqual(tool.sitemap_urls(SITEMAP), ["https://winterliy.github.io/INAZINA/", "https://winterliy.github.io/INAZINA/terms/"])

    def test_the_key_is_the_one_file_named_after_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            public = Path(folder)
            (public / "robots.txt").write_text("x")
            (public / "0123456789abcdef0123456789abcdef.txt").write_text("0123456789abcdef0123456789abcdef")
            (public / "ffffffffffffffffffffffffffffffff.txt").write_text("a different text")  # not a key: its content is not its name
            self.assertEqual(tool.find_key(public), "0123456789abcdef0123456789abcdef")
            (public / "ffffffffffffffffffffffffffffffff.txt").unlink()
            (public / "0123456789abcdef0123456789abcdef.txt").unlink()
            self.assertIsNone(tool.find_key(public))

    def test_the_payload_tells_where_the_key_can_be_checked(self):
        body = tool.payload("https://winterliy.github.io/INAZINA/", "k" * 32, ["https://winterliy.github.io/INAZINA/"])
        self.assertEqual(body["host"], "winterliy.github.io")
        self.assertEqual(body["keyLocation"], "https://winterliy.github.io/INAZINA/" + "k" * 32 + ".txt")
        self.assertEqual(body["urlList"], ["https://winterliy.github.io/INAZINA/"])


if __name__ == "__main__":
    unittest.main()
