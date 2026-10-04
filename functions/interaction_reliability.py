"""Small retry/fallback helpers for transient Discord interaction API failures."""

import asyncio

import discord


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
