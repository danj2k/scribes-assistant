"""Tests for bot/voice.py — idle timeout and auto-disconnect."""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import voice module without requiring the full discord stack.
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

from bot.voice import on_voice_state_update, _idle_timeout, _idle_tasks  # noqa: E402

# Restore sys.modules
for _key in list(sys.modules):
    if _key == "discord" or _key.startswith("discord."):
        if _key in _original:
            sys.modules[_key] = _original[_key]
        else:
            del sys.modules[_key]

if "bot.voice" in sys.modules:
    del sys.modules["bot.voice"]
if "bot" in sys.modules:
    del sys.modules["bot"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeChannel:
    """A fake voice channel with a members list."""
    def __init__(self, members, name="General"):
        self.members = members
        self.name = name


class FakeVoiceClient:
    """A fake guild voice_client with a channel and recording state."""
    def __init__(self, channel):
        self.channel = channel
        self._recording = True
        self._connected = True

    def is_recording(self):
        return self._recording

    def stop_recording(self):
        self._recording = False

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self._connected = False


class FakeMember:
    """A fake Discord member."""
    def __init__(self, bot=False, guild=None):
        self.bot = bot
        self.guild = guild


class FakeGuild:
    """A fake Discord guild with a voice_client."""
    def __init__(self, guild_id, voice_client=None):
        self.id = guild_id
        self.voice_client = voice_client


class FakeVoiceState:
    """A minimal voice state (before/after don't matter for these tests)."""
    pass


def _make_bot(idle_timeout: float = 300):
    """Create a mock bot with config, db, and logger."""
    bot = MagicMock()
    bot.config.idle_timeout = idle_timeout
    bot.db = MagicMock()
    bot.db.get_active_session.return_value = {
        "id": "2024-01-01_12-00-00",
        "discord_channel_id": "123456",
    }
    bot.db.end_session = MagicMock()
    return bot


def _make_text_channel():
    """Create a fake text channel whose send() is awaitable."""
    channel = MagicMock()
    channel.send = AsyncMock()
    return channel


# ---------------------------------------------------------------------------
# Tests for on_voice_state_update
# ---------------------------------------------------------------------------

class TestOnVoiceStateUpdate:

    @pytest.mark.asyncio
    async def test_ignores_bot_members(self):
        """Bot's own voice state changes should be ignored."""
        bot = _make_bot()
        member = FakeMember(bot=True)
        guild = FakeGuild(111)
        member.guild = guild

        await on_voice_state_update(bot, member, FakeVoiceState(), FakeVoiceState())
        assert 111 not in _idle_tasks

    @pytest.mark.asyncio
    async def test_ignores_when_no_voice_client(self):
        """If the bot isn't in a voice channel, do nothing."""
        bot = _make_bot()
        guild = FakeGuild(111, voice_client=None)
        member = FakeMember(bot=False, guild=guild)

        await on_voice_state_update(bot, member, FakeVoiceState(), FakeVoiceState())
        assert 111 not in _idle_tasks

    @pytest.mark.asyncio
    async def test_starts_idle_timer_when_alone(self):
        """When all users leave, start the idle timer."""
        bot = _make_bot(idle_timeout=1)
        bot_member = FakeMember(bot=True)
        channel = FakeChannel([bot_member])
        vc = FakeVoiceClient(channel)
        guild = FakeGuild(111, voice_client=vc)
        member = FakeMember(bot=False, guild=guild)

        with patch("bot.voice.asyncio.create_task") as mock_create:
            await on_voice_state_update(bot, member, FakeVoiceState(), FakeVoiceState())
            mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancels_idle_timer_when_user_joins(self):
        """When a user joins, cancel any running idle timer."""
        bot = _make_bot()
        human_member = FakeMember(bot=False)
        channel = FakeChannel([human_member])
        vc = FakeVoiceClient(channel)
        guild = FakeGuild(222, voice_client=vc)
        member = FakeMember(bot=False, guild=guild)

        mock_task = MagicMock()
        _idle_tasks[222] = mock_task

        await on_voice_state_update(bot, member, FakeVoiceState(), FakeVoiceState())

        mock_task.cancel.assert_called_once()
        assert 222 not in _idle_tasks


# ---------------------------------------------------------------------------
# Tests for _idle_timeout
# ---------------------------------------------------------------------------

class TestIdleTimeout:

    @pytest.mark.asyncio
    async def test_idle_timeout_ends_session(self):
        """After the idle period, the session is ended and bot disconnects."""
        bot = _make_bot(idle_timeout=0.01)

        text_channel = _make_text_channel()
        guild = MagicMock()
        guild.id = 333
        guild.get_channel = MagicMock(return_value=text_channel)

        vc = FakeVoiceClient(FakeChannel([]))

        await _idle_timeout(bot, guild, vc)

        bot.db.end_session.assert_called_once_with("2024-01-01_12-00-00")
        assert not vc.is_recording()
        assert not vc.is_connected()
        text_channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idle_timeout_no_active_session_just_disconnects(self):
        """If no active session, just disconnect the bot."""
        bot = _make_bot(idle_timeout=0.01)
        bot.db.get_active_session.return_value = None

        guild = MagicMock()
        guild.id = 444
        vc = FakeVoiceClient(FakeChannel([]))

        await _idle_timeout(bot, guild, vc)

        bot.db.end_session.assert_not_called()
        assert not vc.is_connected()

    @pytest.mark.asyncio
    async def test_idle_timeout_cancellation_is_silent(self):
        """Cancelled idle timeout doesn't raise or end the session."""
        bot = _make_bot(idle_timeout=10)

        guild = MagicMock()
        guild.id = 555
        vc = FakeVoiceClient(FakeChannel([]))

        task = asyncio.create_task(_idle_timeout(bot, guild, vc))
        await asyncio.sleep(0.01)
        task.cancel()

        # _idle_timeout catches CancelledError and returns normally
        await task

        bot.db.end_session.assert_not_called()
        assert 555 not in _idle_tasks

    @pytest.mark.asyncio
    async def test_idle_timeout_cleans_up_task_dict(self):
        """The idle task removes itself from _idle_tasks on completion."""
        bot = _make_bot(idle_timeout=0.01)

        text_channel = _make_text_channel()
        guild = MagicMock()
        guild.id = 666
        guild.get_channel = MagicMock(return_value=text_channel)

        vc = FakeVoiceClient(FakeChannel([]))

        _idle_tasks[666] = MagicMock()
        await _idle_timeout(bot, guild, vc)

        assert 666 not in _idle_tasks

    @pytest.mark.asyncio
    async def test_idle_timeout_not_recording_does_not_stop(self):
        """If not recording, don't call stop_recording."""
        bot = _make_bot(idle_timeout=0.01)

        text_channel = _make_text_channel()
        guild = MagicMock()
        guild.id = 777
        guild.get_channel = MagicMock(return_value=text_channel)

        vc = FakeVoiceClient(FakeChannel([]))
        vc._recording = False

        await _idle_timeout(bot, guild, vc)

        bot.db.end_session.assert_called_once()
        assert not vc.is_connected()
