import unittest

from web_api.landing import landing_page


class LandingPageTests(unittest.TestCase):
    def test_every_landing_variant_renders(self):
        # The page is the shared sign-in template (apps/shared/gate.html) with every placeholder filled in.
        for kind in ("home", "not_found", "login_required", "login"):
            for database_ok in (True, False):
                html = landing_page(kind, "/internal/somewhere", database_ok)
                self.assertNotIn("{{", html)
                # only the sign-in variants carry the form; the first screen and the 404 page do not
                self.assertEqual(kind in ("login_required", "login"), "/__browser-access" in html)
                self.assertIn("서비스 정상" if database_ok else "서비스 점검 필요", html)
                self.assertNotIn("0000000", html)                   # the emergency passcode is never printed

    def test_a_gate_for_a_specific_path_posts_back_for_that_path_only(self):
        html = landing_page("login_required", "/internal/health", True)
        self.assertIn('var target="/internal/health"', html)
        # a path that is not under /internal/ falls back to the health check instead of being echoed into the script
        self.assertIn('var target="/internal/health"', landing_page("login_required", "/somewhere/else", True))
        self.assertNotIn("</script><script>", landing_page("login_required", "/internal/x</script><script>alert(1)", True))


if __name__ == "__main__":
    unittest.main()
