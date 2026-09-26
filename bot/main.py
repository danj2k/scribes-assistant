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

# --- py-cord PR #3159 (DAVE voice reception) residual workarounds.
# PR #3159 ("refactor(voice): Strict type checking in voice internals
# & DAVE Support (rec)") implements DAVE E2E decryption for voice
# reception, fixing pycord issue #3139 ("Voice reception is currently
# broken due to Discord's DAVE protocol").  We install it from the PR
# branch in requirements.txt until it merges and a pycord release ships.
#
# The PR also fixes most of the Sink/VoiceClient bugs we previously
# monkey-patched (Sink.__sink_listeners__, walk_children, is_opus,
# Sink.recording, Sink.write VoiceData handling, WaveSink using
# OpusDecoder class attributes instead of vc.decoder).  Those patches
# have been removed.  Two patches remain for issues the PR does NOT fix:
#
# 1. RTPPacket.type — reader.py logs packet.type for unexpected RTCP
#    packets; RTPPacket lacks the `type` class attribute that
#    RTCPPacket defines (None: same default as RTCPPacket base).
#
# 2. VoiceClient.start_recording — the DAVE refactor commented out the
#    assignment of sink._client in AudioReader.__init__, so sink.vc is
#    never set and sink.client returns None.  We patch start_recording
#    to set sink.vc = self before creating the AudioReader.  Without
#    this, PacketDecoder._process_packet hits
#    "assert self.sink.client" (AssertionError) on the first audio packet.
#
# Both patches are guarded with hasattr() so they are no-ops if py-cord
# fixes them upstream or a future PR build resolves them.

from discord.voice.packets.rtp import RTPPacket as _RTPPacket

if not hasattr(_RTPPacket, "type"):
    _RTPPacket.type = None

from discord.voice.client import VoiceClient as _VoiceClient

# Patch start_recording to set sink.vc before creating AudioReader.
# The DAVE refactor commented out the assignment of sink._client in
# AudioReader.__init__ (reader.py: "# self.sink._client = client").
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

# --- Voice receive diagnostics ---
# The recorder has historically had failures where one Discord user's audio
# is healthy while another user's audio is corrupted.  The failure can occur
# inside py-cord before ``Sink.write()`` is called, so logging only at the
# sink is insufficient.  These wrappers deliberately log metadata only —
# never encrypted packets or raw audio — and leave py-cord behaviour alone.
#
# The patch is guarded so it remains harmless if py-cord moves or renames
# these internals.  DEBUG logging gives per-packet detail; warnings/errors
# are emitted for actual decoder/DAVE failures.

def _install_voice_receive_diagnostics() -> None:
    """Add lightweight diagnostics around py-cord's DAVE/Opus receive path."""
    try:
        from discord.opus import PacketDecoder as _PacketDecoder
    except (ImportError, AttributeError):
        logger.warning("Voice diagnostics unavailable: PacketDecoder not found")
        return

    if getattr(_PacketDecoder, "_scribes_diagnostics_patched", False):
        return

    voice_logger = logging.getLogger("scribes.voice.diagnostics")

    # Helper to extract packet metadata for logging
    def _packet_meta(self, packet):
        if packet is None:
            return "None"
        return f"ssrc={getattr(packet, 'ssrc', '?')} seq={getattr(packet, 'sequence', '?')} ts={getattr(packet, 'timestamp', '?')}"

    # Wrap _decode_packet to log decode attempts and failures
    def _patched_decode_packet(self, data):
        voice_logger.debug(
            "RX decode attempt: %s user_id=%s",
            _packet_meta(self, data),
            getattr(self, "_cached_id", None),
        )
        try:
            result = _PacketDecoder._original_decode_packet(self, data)
        except Exception as exc:
            voice_logger.error(
                "RX decode FAILED: %s user_id=%s error=%s dave_status=%s",
                _packet_meta(self, data),
                getattr(self, "_cached_id", None),
                exc,
                getattr(self, "_dave_status", None) if hasattr(self, "_dave_status") else None,
            )
            raise

        decoded_packet, pcm = result
        voice_logger.debug(
            "RX decode succeeded: %s user_id=%s pcm_bytes=%s",
            _packet_meta(self, decoded_packet),
            getattr(self, "_cached_id", None),
            len(pcm) if pcm else 0,
        )
        return result

    # Store original and install wrapper
    _PacketDecoder._original_decode_packet = _PacketDecoder._decode_packet
    _PacketDecoder._decode_packet = _patched_decode_packet
    _PacketDecoder._scribes_diagnostics_patched = True

    voice_logger.info(
        "Installed py-cord voice receive diagnostics (PacketDecoder DAVE/Opus path)"
    )

_install_voice_receive_diagnostics()

from shared.config import Config
from shared.database import Database
from shared.lexicon import Lexicon
from shared.logging_setup import setup_logging_from_config
from bot.commands import setup_commands
from bot.error_handler import setup_error_handler
from bot.voice import on_voice_state_update
from bot.delivery import DeliveryLoop
from bot.retention import retention_loop

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
        self._retention_task: asyncio.Task | None = None

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

        # Recover orphaned recording sessions — sessions left in 'recording'
        # status across a restart have no in-memory state (voice client, sink,
        # futures) and cannot be ended normally.  Fail them so the guild can
        # record again.
        orphaned = self.db.get_orphaned_sessions()
        if orphaned:
            logger.warning(
                f"Found {len(orphaned)} orphaned recording session(s) "
                f"from a previous instance — failing them"
            )
            for sess in orphaned:
                session_id = sess["id"]
                guild_id = sess["discord_guild_id"]
                logger.info(
                    f"Failing orphaned session {session_id} "
                    f"(guild={guild_id})"
                )
                self.db.fail_session(session_id)
        else:
            logger.info("No orphaned recording sessions found")

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

        # Start the recording retention sweep — purges old WAV files
        # to reclaim disk space.  Runs immediately then periodically.
        if self._retention_task is None or self._retention_task.done():
            self._retention_task = asyncio.create_task(
                retention_loop(self.config, self.db)
            )
            logger.info(
                f"Recording retention sweep started "
                f"(retention={self.config.recording_retention_days} days, "
                f"interval={self.config.purge_interval_hours}h)"
            )

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
            # Cancel the retention sweep so it doesn't log after close.
            if self._retention_task and not self._retention_task.done():
                self._retention_task.cancel()
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
