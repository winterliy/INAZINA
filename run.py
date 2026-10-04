import os
from main import InazinaBot

token = os.getenv("INAZINA_TOKEN") or os.getenv("NACONOMICS_TOKEN")
if not token:
    raise SystemExit("INAZINA_TOKEN 환경변수를 설정해주세요.")

bot = InazinaBot(token)
bot.run(token)
