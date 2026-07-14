"""Scribe's Assistant — Discord bot entry point.

Handles Discord gateway connection, voice channel management,
and transcription pipeline coordination.
"""
import os
import sys
import time
import signal
import logging
import asyncio
from pathlib import Path

# Add project root to path for shared module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
from discord.ext import commands

# --- py-cord 2.8.0 workaround: Sink is missing __sink_listeners__ and
# walk_children, which the SinkEventRouter (voice/receive/router.py)
# expects.  Without these, start_recording() raises AttributeError
# immediately.  This is a known py-cord 2.8.0 bug — the voice reception
# refactor added SinkEventRouter but never updated the Sink class.
# walk_children returns an empty iterator because WaveSink (the only
# sink we use) has no child sinks.  __sink_listeners__ is an empty list
# because we don't register any sink event listeners.
from discord.sinks import Sink as _Sink

if not hasattr(_Sink, "__sink_listeners__"):
    _Sink.__sink_listeners__ = []

if not hasattr(_Sink, "walk_children"):
    def _walk_children(self):
        return iter(())
    _Sink.walk_children = _walk_children

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

    async def setup_hook(self):
        """Register the SIGTERM handler for graceful shutdown.

        Called by py-cord after the event loop starts but before
        on_ready.  Registering here (rather than at module level)
        ensures the handler runs on the correct event loop.

        Docker sends SIGTERM on `docker stop`.  Without this handler,
        the process is SIGKILLed after the grace period with no
        cleanup — the delivery loop may be mid-send, the database
        may not be closed, and the heartbeat file goes stale.

        We use loop.add_signal_handler (not signal.signal) because
        signal.signal's handler runs in a thread context and cannot
        safely schedule coroutines on the event loop.
        """
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._request_shutdown)

    def _request_shutdown(self):
        """SIGTERM handler — trigger graceful shutdown.

        Stops the delivery loop, then closes the Discord gateway.
        py-cord's close() cancels pending tasks and disconnects
        cleanly, which is what we want.
        """
        logger.info("SIGTERM received — shutting down gracefully")

        async def _shutdown():
            # Stop the delivery loop first so a transcript delivery
            # in flight is not abandoned mid-send.
            if self.delivery_loop and self.delivery_loop.is_running:
                await self.delivery_loop.stop()
            await self.close()

        asyncio.ensure_future(_shutdown())


def main():
    """Entry point — load config, set up bot, start event loop."""
    config_path = os.environ.get("CONFIG_PATH", "/data/config.yaml")

    # Check that the config file exists before loading.  Without this,
    # Config() silently falls back to all-defaults and the bot starts
    # with no guild_id or transcript_channel_id, sitting forever doing
    # nothing useful while Docker's restart policy keeps relaunching it.
    if not Path(config_path).exists():
        # Logging isn't configured yet — print to stderr as a fallback.
        print(
            f"Config file not found: {config_path}. "
            f"Ensure config.yaml is mounted (see docker-compose.yml) "
            f"and CONFIG_PATH is set correctly.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config(config_path)

    # Set up logging early so validation errors are captured properly.
    setup_logging_from_config(config, "bot")

    # Validate critical config — refuse to start if required values
    # are missing, rather than running silently with defaults.
    errors = config.validate(config_path)
    if errors:
        for err in errors:
            logger.error(err)
        logger.error(
            "Bot will not start due to configuration errors. "
            "Fix config.yaml and restart."
        )
        sys.exit(1)

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
