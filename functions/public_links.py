"""Where the documents live: the official site on GitHub Pages (one page per document). The bot and the screens link here."""
import os

SITE = os.environ.get("INAZINA_PUBLIC_SITE", "https://winterliy.github.io/INAZINA/")
if not SITE.endswith("/"):
    SITE += "/"

HELP_URL = SITE + "help/"
TERMS_URL = SITE + "terms/"
PRIVACY_URL = SITE + "privacy/"
DOCUMENTS = {"help", "terms", "privacy", "operations", "internal"}


def document_url(document_id):
    """The page of one document; anything unknown goes to the help page."""
    return SITE + document_id + "/" if document_id in DOCUMENTS else HELP_URL
