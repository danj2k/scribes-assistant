"""Scribe's Assistant — Discord bot entry point.

Handles Discord gateway connection, voice channel management,
and transcription pipeline coordination.
"""
import os
import sys
import logging
from pathlib import Path

# Add project root to path for shared module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
from discord.ext import commands

from shared.config import Config
from shared.database import Database
from shared.lexicon import Lexicon
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

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        # Start the transcript delivery loop in the background
        if self.delivery_loop and not self.delivery_loop.is_running:
            self.delivery_loop.start()
            logger.info("Transcript delivery loop started")

    async def on_voice_state_update(self, member, before, after):
        """Delegate voice state changes to the voice module."""
        await on_voice_state_update(self, member, before, after)


def setup_logging(config: Config):
    """Configure file and console logging."""
    log_dir = Path("/data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))
    root_logger.addHandler(console)

    # File handler with rotation
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=config.log_max_size_mb * 1024 * 1024,
        backupCount=config.log_backup_count,
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))
    root_logger.addHandler(file_handler)


def main():
    """Entry point — load config, set up bot, start event loop."""
    config_path = os.environ.get("CONFIG_PATH", "/data/config.yaml")
    config = Config(config_path)

    setup_logging(config)

    # Initialise shared components
    db = Database(config.database_path)
    lexicon = Lexicon(config.lexicon_file)

    # Delivery loop (posts transcripts to Discord threads)
    delivery = DeliveryLoop(db=db)

    # Create and run bot
    bot = ScribesBot(config=config, db=db, lexicon=lexicon)
    bot.delivery_loop = delivery

    # Register slash commands
    setup_commands(bot)

    # Register global error handler
    setup_error_handler(bot)

    bot.run(config.bot_token)


if __name__ == "__main__":
    main()
