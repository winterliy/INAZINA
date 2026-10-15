"""Small retry/fallback helpers for transient Discord interaction API failures."""

import asyncio
import logging
from time import perf_counter

import discord


async def discord_request(operation, *, stage='response'):
    """Measure one Discord request; discord.py owns transport retries."""
    started = perf_counter()
    result = 'error'
    try:
        value = await operation()
        result = 'ok'
        return value
    finally:
        elapsed = (perf_counter() - started) * 1000
        if elapsed >= 100 or result != 'ok':
            logging.getLogger('inazina.performance').info(
                'discord.%s %.1f ms %s', stage, elapsed, result
            )


async def prepare_response(interaction, work, *, thinking=False, delay=0.4):
    """Run fast work directly and acknowledge slow interactions before completion."""
    task = asyncio.create_task(work())
    deferred = False
    try:
        done, _ = await asyncio.wait({task}, timeout=delay)
        if not done and not interaction.response.is_done():
            await discord_request(
                lambda: interaction.response.defer(ephemeral=True, thinking=thinking),
                stage='ack',
            )
            deferred = True
        return await task, deferred
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif not task.cancelled():
            task.exception()


async def retry_discord_request(operation, attempts=2, delay_seconds=0.2):
    """Retry a Discord request once when Discord returns a transient 5xx."""
    for attempt in range(attempts):
        try:
            return await operation()
        except discord.DiscordServerError as exc:
            if attempt + 1 >= attempts:
                raise
            await asyncio.sleep(delay_seconds * (attempt + 1))


async def send_interaction_notice(interaction, message):
    """Try to show a private fallback without letting another API error escape."""
    try:
        if interaction.response.is_done():
            await retry_discord_request(
                lambda: interaction.followup.send(message, ephemeral=True)
            )
        else:
            await retry_discord_request(
                lambda: interaction.response.send_message(message, ephemeral=True)
            )
        return True
    except discord.HTTPException:
        return False
