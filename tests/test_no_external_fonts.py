"""The screens must not load fonts (or anything) from Google: that would send every visitor's IP address abroad (the privacy policy
says nothing leaves). Fonts come from the npm packages bundled into each app."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = ("site", "dashboard", "hts", "id")
EXTERNAL = re.compile(r"fonts\.(googleapis|gstatic)\.com")


def _sources(app):
    base = ROOT / "apps" / app
    yield base / "index.html"
    for path in (base / "src").rglob("*"):
        if path.suffix in {".css", ".ts", ".tsx", ".html"} and not path.name.endswith(".test.ts"):
            yield path


def test_no_app_loads_google_fonts():
    found = [f"{path.relative_to(ROOT)}" for app in APPS for path in _sources(app) if path.exists() and EXTERNAL.search(path.read_text(encoding="utf-8", errors="ignore"))]
    assert not found, found


def test_every_font_family_in_use_is_bundled_from_a_package():
    packages = {
        "site": {"@fontsource/crimson-text", "@fontsource/dm-mono", "@fontsource/dm-sans", "@fontsource/noto-sans-kr", "@fontsource/noto-serif-kr"},
        "dashboard": {"@fontsource/archivo", "@fontsource/dm-sans", "@fontsource/ibm-plex-mono", "@fontsource/ibm-plex-sans-kr"},
        "id": {"@fontsource/archivo", "@fontsource/dm-sans", "@fontsource/ibm-plex-mono", "@fontsource/ibm-plex-sans-kr"},
        "hts": {"@fontsource/rethink-sans"},
    }
    for app, wanted in packages.items():
        main = next((ROOT / "apps" / app / "src" / name for name in ("main.tsx", "main.ts") if (ROOT / "apps" / app / "src" / name).exists()))
        imported = set(re.findall(r'import "(@fontsource/[^/"]+)', main.read_text(encoding="utf-8")))
        assert wanted <= imported, (app, wanted - imported)
