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

# --- py-cord 2.8.0 workaround: the DAVE voice reception refactor broke
# five methods/attributes that the Sink, RTPPacket, and VoiceClient classes
# all expect but none define.  Without these, start_recording() and the
# subsequent audio pipeline raise AttributeError at various stages.
# Tracked upstream as pycord issue #3139 (still unfixed on master).
#
# 1. Sink.__sink_listeners__ — SinkEventRouter.register_events() reads it
#    (empty list: we register no sink event listeners).
# 2. Sink.walk_children() — SinkEventRouter iterates it
#    (empty iterator: WaveSink has no child sinks).
# 3. Sink.is_opus() — PacketDecoder.__init__/_process_packet calls it to
#    decide whether to create an Opus Decoder
#    (False: WaveSink wants PCM, so decoding is required).
# 4. RTPPacket.type — reader.py logs packet.type for unexpected RTCP
#    packets; RTPPacket lacks the `type` class attribute that RTCPPacket
#    defines (None: same default as RTCPPacket base).
# 5. VoiceClient.recording — WaveSink.format_audio checks vc.recording
#    (property delegating to is_recording(), which already exists).
# 6. VoiceClient.decoder — WaveSink.format_audio reads vc.decoder.CHANNELS,
#    .SAMPLE_SIZE, .SAMPLING_RATE for WAV header
#    (property returning a fresh opus.Decoder, which inherits these from
#    _OpusStruct).
#
# 7. Sink.write() — the DAVE router passes VoiceData objects to
#    sink.write(data, user), but the original write() expects raw bytes.
#    We wrap the original to extract .pcm (decoded PCM bytes) from
#    VoiceData before handing it to AudioData.write().
#
# 8. VoiceClient.start_recording — the DAVE refactor commented out the
#    assignment of sink._client in AudioReader.__init__ (reader.py:89),
#    so sink.vc is never set and sink.client returns None.  We patch
#    start_recording to set sink.vc = self before creating the AudioReader.
#    Without this, PacketDecoder._process_packet hits
#    "assert self.sink.client" (AssertionError) on the first audio packet.
#
# All patches are guarded with hasattr() so they are no-ops if py-cord
# ever fixes this upstream or if downgrading to 2.6.3.

from discord.sinks import Sink as _Sink

if not hasattr(_Sink, "__sink_listeners__"):
    _Sink.__sink_listeners__ = []

if not hasattr(_Sink, "walk_children"):
    def _walk_children(self):
        return iter(())
    _Sink.walk_children = _walk_children

if not hasattr(_Sink, "is_opus"):
    _Sink.is_opus = lambda self: False

# Patch Sink.write to handle VoiceData objects from the DAVE router.
# The original (Filters.container-decorated) write() expects raw bytes,
# but router._do_run passes VoiceData.  We unwrap .pcm before delegating.
if not getattr(_Sink, "_write_patched", False):
    _original_write = _Sink.write

    def _patched_write(self, data, user):
        if hasattr(data, "pcm"):
            data = data.pcm
        return _original_write(self, data, user)

    _Sink.write = _patched_write
    _Sink._write_patched = True

from discord.voice.packets.rtp import RTPPacket as _RTPPacket

if not hasattr(_RTPPacket, "type"):
    _RTPPacket.type = None

from discord.voice.client import VoiceClient as _VoiceClient

if not hasattr(_VoiceClient, "recording"):
    _VoiceClient.recording = property(lambda self: self.is_recording())

if not hasattr(_VoiceClient, "decoder"):
    from discord.opus import Decoder as _Decoder
    _VoiceClient.decoder = property(lambda self: _Decoder())

# Patch start_recording to set sink.vc before creating AudioReader.
# The DAVE refactor commented out the assignment of sink._client in
# AudioReader.__init__ (reader.py line 89: "# self.sink._client = client").
# Sink.client is a property that returns self.vc, so without this patch
# sink.client is None and PacketDecoder._process_packet hits
# "assert self.sink.client" (AssertionError) on the first audio packet.
# The old API was Sink.init(vc) which set self.vc = vc; we replicate that.
if not getattr(_VoiceClient, "_start_recording_patched", False):
    _original_start_recording = _VoiceClient.start_recording

    def _patched_start_recording(self, sink, callback, *args, **kwargs):
        sink.vc = self
        return _original_start_recording(self, sink, callback, *args, **kwargs)

    _VoiceClient.start_recording = _patched_start_recording
    _VoiceClient._start_recording_patched = True

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
