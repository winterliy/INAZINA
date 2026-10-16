"""Dashboard requests that need the live Discord client, processed by the bot."""
import asyncio
import json
import logging
import time

POLL_SECONDS = 5
SCHEMA = """CREATE TABLE IF NOT EXISTS web_bot_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, actor_id TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','done','failed')),
    result TEXT, created_epoch INTEGER NOT NULL, updated_epoch INTEGER NOT NULL)"""


def _claim(db):
    with db.atomic(("system",)) as conn:
        conn.execute(SCHEMA)
        row = conn.execute("SELECT * FROM web_bot_jobs WHERE status='pending' ORDER BY job_id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE web_bot_jobs SET status='running',updated_epoch=? WHERE job_id=?",
                     (int(time.time()), row["job_id"]))
        return dict(row)


def _finish(db, job_id, status, result):
    with db.atomic(("system",)) as conn:
        conn.execute("UPDATE web_bot_jobs SET status=?,result=?,updated_epoch=? WHERE job_id=?",
                     (status, str(result)[:4000], int(time.time()), job_id))


async def run_job(bot, job):
    payload = json.loads(job["payload"])
    if job["kind"] == "shareholder_meeting":
        from functions.admin_prefix import convene_national_meeting
        return await convene_national_meeting(bot, job["actor_id"], payload["code"], payload["place"])
    if job["kind"] == "relief_request":
        from functions.slash_commands import submit_relief_request
        return await submit_relief_request(bot, job["actor_id"], payload.get("name") or job["actor_id"], payload["type"])
    if job["kind"] == "user_shareholder_meeting":
        from functions.admin_access import resolve_user_role
        from functions.slash_commands import convene_shareholder_meeting
        user = await asyncio.to_thread(bot.db.users.get_user, job["actor_id"])
        role = resolve_user_role(job["actor_id"], user) if user else "user"
        return await convene_shareholder_meeting(bot, job["actor_id"], role, payload["code"], payload["place"])
    raise ValueError(f"지원하지 않는 작업입니다: {job['kind']}")


async def web_job_loop(bot):
    log = logging.getLogger(__name__)
    while not bot.is_closed():
        try:
            job = await asyncio.to_thread(_claim, bot.db)
            if job is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                result, status = await run_job(bot, job), "done"
            except (ValueError, KeyError, TypeError) as exc:
                result, status = f"❌ {exc}", "failed"
            except Exception:
                log.exception("Dashboard job %s failed", job["job_id"])
                result, status = "❌ 작업 처리 중 오류가 발생했습니다. 봇 로그를 확인해주세요.", "failed"
            await asyncio.to_thread(_finish, bot.db, job["job_id"], status, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Dashboard job loop error")
            await asyncio.sleep(POLL_SECONDS)
