# Sends Discord notifications to riders and drivers about ride assignments.
#
# Uses discord.py to log in as the church's bot, find a user by their
# Discord username, and send them a direct message. discord.py is built
# on asyncio, so each call spins up a short-lived client/event loop and
# tears it down when done - callers still use this as a plain synchronous
# function.

from __future__ import annotations

import asyncio
import logging

import discord

from config.settings import DISCORD_BOT_TOKEN

logger = logging.getLogger(__name__)


def send_discord_dm(username: str, message: str) -> bool:
    """Send a Discord direct message to a user by username.

    Args:
        username: The Discord username (e.g. "deshawnc") to message.
        message: The message text to send.

    Returns:
        bool: True if the DM was sent successfully, False otherwise.
    """
    try:
        return asyncio.run(_send_discord_dm(username, message))
    except Exception as exc:
        # Catches issues outside the client itself, e.g. the event loop
        # failing to start.
        logger.error("Unexpected error sending Discord DM to %s: %s", username, exc)
        return False


async def _send_discord_dm(username: str, message: str) -> bool:
    """Log in, find the user, send the DM, then always log back out.

    Args:
        username: The Discord username to search for and message.
        message: The message text to send.

    Returns:
        bool: True if the DM was sent successfully, False otherwise.
    """
    # The members intent is required to search a guild's member list by
    # username; it must also be enabled for the bot in the Discord
    # developer portal.
    intents = discord.Intents.default()
    intents.members = True

    client = discord.Client(intents=intents)
    result = {"sent": False}

    @client.event
    async def on_ready() -> None:
        try:
            member = discord.utils.find(
                lambda m: m.name == username or str(m) == username,
                client.get_all_members(),
            )

            if member is None:
                logger.error("Discord user %r not found in any guild.", username)
            else:
                await member.send(message)
                logger.info("Sent Discord DM to %s.", username)
                result["sent"] = True
        except Exception as exc:
            logger.error("Failed to send Discord DM to %s: %s", username, exc)
        finally:
            # Always close the client so the short-lived login doesn't
            # linger, whether sending succeeded or failed.
            await client.close()

    await client.start(DISCORD_BOT_TOKEN)
    return result["sent"]
