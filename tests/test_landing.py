import unittest

from web_api.landing import landing_page


class LandingPageTests(unittest.TestCase):
    def test_every_landing_variant_renders(self):
        # STYLE is a str.format template: a CSS rule written with single braces raises KeyError.
        for kind in ("home", "not_found"):
            for database_ok in (True, False):
                html = landing_page(kind, "/somewhere", database_ok)
                self.assertIn("a.card:active{transform:scale(.98)}", html)
                self.assertNotIn("{{", html)


if __name__ == "__main__":
    unittest.main()
