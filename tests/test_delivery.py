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


class _FakeTask:
    """Minimal awaitable fake for asyncio.Task — supports cancel/done/await.

    The delivery loop's stop() method does:

        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    This fake simulates a task that is active, gets cancelled, and
    raises CancelledError when awaited (as a real cancelled task would).
    """

    def __init__(self, done=False):
        self._done = done
        self.cancel_called = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancel_called = True

    def __await__(self):
        raise asyncio.CancelledError()
        yield  # never reached, but makes this a generator


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


class TestDeliveryLoopStop:

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self):
        """stop() cancels an active task and awaits it."""
        loop, bot, db, logger = _make_delivery_loop()

        task = _FakeTask()
        loop._task = task
        await loop.stop()

        assert task.cancel_called

    @pytest.mark.asyncio
    async def test_stop_no_op_when_no_task(self):
        """stop() is safe to call when no task was ever started."""
        loop, *_ = _make_delivery_loop()
        loop._task = None
        await loop.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_no_op_when_task_done(self):
        """stop() is safe to call when the task has already completed."""
        loop, *_ = _make_delivery_loop()
        task = _FakeTask(done=True)
        loop._task = task
        await loop.stop()
        assert not task.cancel_called

    @pytest.mark.asyncio
    async def test_stop_logs_message(self):
        """stop() logs that the delivery loop has stopped."""
        loop, bot, db, logger = _make_delivery_loop()

        loop._task = _FakeTask()
        await loop.stop()
        logger.info.assert_called_once_with("Delivery loop stopped")


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
    async def test_deliver_creates_public_thread(self, tmp_path):
        """Threads must be public — py-cord defaults to private_thread when
        no message is passed, which makes the transcript invisible to users."""
        loop, bot, db, logger = _make_delivery_loop()
        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("Hello world")
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

        _, kwargs = mock_channel.create_thread.call_args
        # py-cord defaults type to private_thread when no message is passed.
        # We must explicitly pass type=public_thread so users can see it.
        assert "type" in kwargs, "type must be explicitly passed to avoid private_thread default"
        assert kwargs["type"] is not None

    @pytest.mark.asyncio
    async def test_deliver_attaches_transcript_file(self, tmp_path):
        """Transcript is attached as a .txt file, not posted inline.

        A 3-hour session produces tens of thousands of characters — far too
        large for Discord's 2000-char message limit.  A file attachment lets
        the user download or view it cleanly regardless of length.
        """
        loop, bot, db, logger = _make_delivery_loop()

        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("Hello world")

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

        # Must send a message with a file attachment
        mock_thread.send.assert_awaited_once()
        _, kwargs = mock_thread.send.call_args
        assert "file" in kwargs, "transcript must be sent as a file attachment"

        # Verify discord.File was constructed with the right filename.
        # discord was mocked during import, so access it via the function's
        # globals (the module-level discord reference from import time).
        delivery_discord = DeliveryLoop._deliver.__globals__["discord"]
        file_kwargs = delivery_discord.File.call_args.kwargs
        assert file_kwargs["filename"] == "s1.txt"

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
