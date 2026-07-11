"""Tests for bot/delivery.py — DeliveryLoop polling and transcript delivery."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import DeliveryLoop without requiring the full discord stack.
#
# bot/delivery.py imports discord, which may need sub-modules not available
# in all test environments. We mock the discord sub-modules during import,
# then restore sys.modules so other test files get the real discord.
# ---------------------------------------------------------------------------

_original: dict[str, types.ModuleType | None] = {}
for _key in list(sys.modules):
    if _key == "discord" or _key.startswith("discord."):
        _original[_key] = sys.modules[_key]

_mock_modules: dict[str, MagicMock] = {}
for _mod_name in ("discord", "discord.ext", "discord.ext.commands", "discord.commands"):
    if _mod_name not in sys.modules:
        _mock_modules[_mod_name] = MagicMock()
        sys.modules[_mod_name] = _mock_modules[_mod_name]

from bot.delivery import DeliveryLoop  # noqa: E402

# Restore sys.modules
for _key in list(sys.modules):
    if _key == "discord" or _key.startswith("discord."):
        if _key in _original:
            sys.modules[_key] = _original[_key]
        else:
            del sys.modules[_key]

if "bot.delivery" in sys.modules:
    del sys.modules["bot.delivery"]
if "bot" in sys.modules:
    del sys.modules["bot"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_delivery_loop():
    """Create a DeliveryLoop with mocked dependencies."""
    bot = MagicMock()
    db = MagicMock()
    logger = MagicMock()
    loop = DeliveryLoop(bot, db, logger)
    return loop, bot, db, logger


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeliveryLoopInit:

    def test_init_sets_attributes(self):
        bot = MagicMock()
        db = MagicMock()
        logger = MagicMock()
        loop = DeliveryLoop(bot, db, logger)
        assert loop.bot is bot
        assert loop.db is db
        assert loop.logger is logger
        assert loop.poll_interval == 5
        assert loop._task is None

    def test_is_running_false_when_no_task(self):
        loop, *_ = _make_delivery_loop()
        assert loop.is_running is False

    def test_is_running_false_when_task_done(self):
        loop, *_ = _make_delivery_loop()
        mock_task = MagicMock()
        mock_task.done.return_value = True
        loop._task = mock_task
        assert loop.is_running is False

    def test_is_running_true_when_task_active(self):
        loop, *_ = _make_delivery_loop()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        loop._task = mock_task
        assert loop.is_running is True


class TestDeliveryLoopStart:

    def test_start_creates_task(self):
        loop, *_ = _make_delivery_loop()
        # Replace run with a non-async mock so no coroutine is created
        loop._task = None
        with patch.object(loop, "run", new_callable=MagicMock):
            with patch("bot.delivery.asyncio.create_task") as mock_create:
                loop.start()
                mock_create.assert_called_once()

    def test_start_is_idempotent(self):
        """Calling start() when already running is a no-op."""
        loop, *_ = _make_delivery_loop()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        loop._task = mock_task

        loop.start()
        # Task should not be replaced
        assert loop._task is mock_task


class TestDeliveryLoopPoll:

    @pytest.mark.asyncio
    async def test_poll_calls_get_sessions_for_delivery(self):
        loop, bot, db, logger = _make_delivery_loop()
        db.get_sessions_for_delivery.return_value = []
        await loop._poll()
        db.get_sessions_for_delivery.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_delivers_each_session(self):
        loop, bot, db, logger = _make_delivery_loop()
        sessions = [{"id": "s1"}, {"id": "s2"}]
        db.get_sessions_for_delivery.return_value = sessions

        with patch.object(loop, "_deliver", new_callable=AsyncMock) as mock_deliver:
            await loop._poll()
            assert mock_deliver.await_count == 2
            mock_deliver.assert_any_call(sessions[0])
            mock_deliver.assert_any_call(sessions[1])

    @pytest.mark.asyncio
    async def test_poll_with_no_sessions_does_nothing(self):
        loop, bot, db, logger = _make_delivery_loop()
        db.get_sessions_for_delivery.return_value = []

        with patch.object(loop, "_deliver", new_callable=AsyncMock) as mock_deliver:
            await loop._poll()
            mock_deliver.assert_not_awaited()


class TestDeliveryLoopDeliver:

    @pytest.mark.asyncio
    async def test_deliver_creates_thread_and_sends_transcript(self, tmp_path):
        """Deliver a short transcript — creates a thread and sends content."""
        loop, bot, db, logger = _make_delivery_loop()

        # Write a short transcript file
        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("Hello world")

        session = {
            "id": "s1",
            "transcript_path": str(transcript_file),
            "discord_channel_id": "123456",
        }

        # Mock the bot's channel and thread
        mock_thread = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)
        bot.get_channel.return_value = mock_channel
        bot.config.transcript_channel_id = None  # use session's channel

        await loop._deliver(session)

        bot.get_channel.assert_called_once_with(123456)
        mock_channel.create_thread.assert_awaited_once()
        mock_thread.send.assert_awaited_once()
        db.set_thread_id.assert_called_once_with("s1", str(mock_thread.id))

    @pytest.mark.asyncio
    async def test_deliver_splits_long_transcript(self, tmp_path):
        """Transcripts over 2000 chars are split into multiple messages."""
        loop, bot, db, logger = _make_delivery_loop()

        # Write a transcript longer than 2000 chars
        long_line = "x" * 100
        long_text = "\n".join([long_line] * 30)  # ~3030 chars
        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text(long_text)

        session = {
            "id": "s1",
            "transcript_path": str(transcript_file),
            "discord_channel_id": "123456",
        }

        mock_thread = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)
        bot.get_channel.return_value = mock_channel
        bot.config.transcript_channel_id = None

        await loop._deliver(session)

        # Should have sent multiple messages
        assert mock_thread.send.await_count > 1

    @pytest.mark.asyncio
    async def test_deliver_uses_config_channel_when_set(self, tmp_path):
        """When transcript_channel_id is configured, deliver there instead."""
        loop, bot, db, logger = _make_delivery_loop()

        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("Test")

        session = {
            "id": "s1",
            "transcript_path": str(transcript_file),
            "discord_channel_id": "123456",
        }

        mock_thread = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)
        bot.get_channel.return_value = mock_channel
        bot.config.transcript_channel_id = 999  # configured channel

        await loop._deliver(session)

        # Should use the configured channel, not the session's
        bot.get_channel.assert_called_once_with(999)

    @pytest.mark.asyncio
    async def test_deliver_channel_not_found_logs_warning(self, tmp_path):
        """If the channel can't be found, log a warning and return."""
        loop, bot, db, logger = _make_delivery_loop()

        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("Test")

        session = {
            "id": "s1",
            "transcript_path": str(transcript_file),
            "discord_channel_id": "123456",
        }

        bot.get_channel.return_value = None
        bot.config.transcript_channel_id = None

        await loop._deliver(session)

        logger.warning.assert_called_once()
        db.set_thread_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_deliver_missing_transcript_file_logs_warning(self, tmp_path):
        """If the transcript file doesn't exist, log a warning and return."""
        loop, bot, db, logger = _make_delivery_loop()

        session = {
            "id": "s1",
            "transcript_path": str(tmp_path / "nonexistent.txt"),
            "discord_channel_id": "123456",
        }

        mock_channel = MagicMock()
        bot.get_channel.return_value = mock_channel
        bot.config.transcript_channel_id = None

        await loop._deliver(session)

        logger.warning.assert_called_once()
        db.set_thread_id.assert_not_called()
