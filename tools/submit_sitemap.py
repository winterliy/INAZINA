"""Tell search engines about the official site's pages (IndexNow: Bing, Yandex, Naver and others take part).

Google and Naver's own consoles also want you to submit the sitemap by hand (they need proof that the site is yours: see
apps/site/site-verification.json). IndexNow needs no account: a key file named after the key sits on the site, and one request lists the pages.

    python tools/submit_sitemap.py            # reads the live sitemap.xml and apps/site/public/<key>.txt
"""
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://winterliy.github.io/INAZINA/"


def sitemap_urls(text):
    return [element.text.strip() for element in ET.fromstring(text).iter() if element.tag.endswith("}loc") and element.text]


def find_key(public):
    """The key is the name of a <key>.txt file whose content is that same key."""
    for path in Path(public).glob("*.txt"):
        if re.fullmatch(r"[0-9a-f]{32}", path.stem) and path.read_text().strip() == path.stem:
            return path.stem
    return None


def payload(site, key, urls):
    return {"host": urlsplit(site).netloc, "key": key, "keyLocation": f"{site}{key}.txt", "urlList": urls}


def main():
    key = find_key(ROOT / "apps" / "site" / "public")
    if not key:
        sys.exit("no IndexNow key file in apps/site/public")
    urls = sitemap_urls(urllib.request.urlopen(SITE + "sitemap.xml", timeout=20).read().decode("utf-8"))
    request = urllib.request.Request("https://api.indexnow.org/IndexNow", data=json.dumps(payload(SITE, key, urls)).encode(),
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"IndexNow: HTTP {response.status} for {len(urls)} pages")
    except urllib.error.HTTPError as error:
        print(f"IndexNow: HTTP {error.code} {error.reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
