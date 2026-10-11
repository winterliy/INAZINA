import fcntl
import os
from pathlib import Path


def acquire_instance_lock():
    """Keep one Discord bot process per checkout.

    A second bot would run the same background workers and, during a schema
    rollout, could keep executing an older version of the code against the
    newly migrated database.
    """
    lock_path = Path(__file__).resolve().parent / "database" / ".inazina-bot.lock"
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise SystemExit("INAZINA bot is already running for this checkout.")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


_instance_lock = acquire_instance_lock()

from main import InazinaBot

token = os.getenv("INAZINA_TOKEN") or os.getenv("NACONOMICS_TOKEN")
if not token:
    raise SystemExit("INAZINA_TOKEN 환경변수를 설정해주세요.")

bot = InazinaBot(token)
bot.run(token)
