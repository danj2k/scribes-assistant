"""Scribe's Assistant — Discord bot entry point.

Handles Discord gateway connection, voice channel management,
and transcription pipeline coordination.
"""
import os
import sys
import time
import logging
import asyncio
from pathlib import Path

# Add project root to path for shared module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
from discord.ext import commands

from shared.config import Config
from shared.database import Database
from shared.lexicon import Lexicon
from shared.logging_setup import setup_logging_from_config
from bot.commands import setup_commands
from bot.error_handler import setup_error_handler
from bot.voice import on_voice_state_update
from bot.delivery import DeliveryLoop

logger = logging.getLogger("scribes.bot")


class ScribesBot(commands.Bot):
    """Custom bot class that holds shared state (config, db, lexicon)."""

    def __init__(self, config: Config, db: Database, lexicon: Lexicon):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)

        self.config = config
        self.db = db
        self.lexicon = lexicon
        self.logger = logger
        self.delivery_loop: DeliveryLoop | None = None
        self._heartbeat_file = Path("/data/bot.heartbeat")
        self._heartbeat_task: asyncio.Task | None = None

    async def _write_heartbeat(self):
        """Write the current timestamp to the heartbeat file.

        The Docker health check (bot/healthcheck.py) reads this file
        to verify the bot's event loop is alive and the gateway is
        connected.  A dead event loop stops updating the file, and
        the health check declares the container unhealthy.
        """
        self._heartbeat_file.write_text(str(time.time()))

    async def _heartbeat_loop(self):
        """Periodically update the heartbeat file (every 30 seconds)."""
        while not self.is_closed():
            await self._write_heartbeat()
            await asyncio.sleep(30)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")

        # Write an initial heartbeat immediately — on_ready only fires
        # after the Discord gateway handshake completes, so this
        # confirms the bot is genuinely connected.
        await self._write_heartbeat()

        # Start the periodic heartbeat task if not already running
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Sync slash commands
        try:
            await self.sync_commands()
            logger.info("Synced slash commands")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        # Start the transcript delivery loop in the background
        if self.delivery_loop and not self.delivery_loop.is_running:
            self.delivery_loop.start()
            logger.info("Transcript delivery loop started")

    async def on_voice_state_update(self, member, before, after):
        """Delegate voice state changes to the voice module."""
        await on_voice_state_update(self, member, before, after)


def main():
    """Entry point — load config, set up bot, start event loop."""
    config_path = os.environ.get("CONFIG_PATH", "/data/config.yaml")
    config = Config(config_path)

    setup_logging_from_config(config, "bot")

    # Initialise shared components
    db = Database(config.database_path)
    lexicon = Lexicon(config.lexicon_file, config.lexicon_threshold)

    # Create and run bot
    bot = ScribesBot(config=config, db=db, lexicon=lexicon)

    # Delivery loop (posts transcripts to Discord threads)
    delivery = DeliveryLoop(bot, db, logger)
    bot.delivery_loop = delivery

    # Validate the bot token early — without it, py-cord produces a cryptic
    # traceback that gives no hint about the actual cause.
    try:
        token = config.bot_token
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Cannot start bot: {exc}")
        sys.exit(1)

    # Register slash commands
    setup_commands(bot)

    # Register global error handler
    setup_error_handler(bot)

    bot.run(token)


if __name__ == "__main__":
    main()
