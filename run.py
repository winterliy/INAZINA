import os
from main import InazinaBot
from managers.catalog_manager import CatalogManager

token = os.getenv("INAZINA_TOKEN") or os.getenv("NACONOMICS_TOKEN")
if not token:
    raise SystemExit("INAZINA_TOKEN 환경변수를 설정해주세요.")

bot = InazinaBot(token)
CatalogManager(bot.db).seed()
CatalogManager(bot.db).seed_foreign()
bot.run(token)
