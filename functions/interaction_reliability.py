"""Measured Discord requests and deadline-aware preparation."""

import asyncio
import logging
from time import perf_counter

import discord


async def discord_request(operation, *, stage='response'):
    """discord.py owns rate-limit/5xx retries; don't replay an exhausted send."""
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
                'discord.%s %.1f ms %s', stage, elapsed, result)


async def prepare_response(interaction, work, *, thinking=False, delay=0.4):
    """Fast work needs one response; slow work gets an ACK before completion.

    Return whether this helper deferred, including for minimal test adapters.
    The caller must await this helper before responding, avoiding ACK races.
    """
    task = asyncio.create_task(work())
    deferred = False
    try:
        done, _ = await asyncio.wait({task}, timeout=delay)
        if not done and not interaction.response.is_done():
            await discord_request(
                lambda: interaction.response.defer(ephemeral=True, thinking=thinking), stage='ack')
            deferred = True
        return await task, deferred
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif task.cancelled() is False:
            # Retrieve errors even when the ACK failed before we awaited work.
            task.exception()


async def send_interaction_notice(interaction, message):
    """Try to show a private fallback without letting another API error escape."""
    try:
        if interaction.response.is_done():
            await discord_request(
                lambda: interaction.followup.send(message, ephemeral=True)
            )
        else:
            await discord_request(
                lambda: interaction.response.send_message(message, ephemeral=True)
            )
        return True
    except discord.HTTPException:
        return False
