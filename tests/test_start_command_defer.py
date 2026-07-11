"""Tests for Bug #24 — start_command defers interaction before voice join.

Discord requires an interaction to be responded to within 3 seconds.
``start_command`` calls ``voice_channel.connect()`` which can take longer
under high latency (gateway state change, WebSocket handshake, encryption
key exchange).  The fix defers the interaction before the join attempt.

These tests verify:
- ``defer(ephemeral=True)`` is called before ``voice_channel.connect()``
- Post-defer responses use ``followup.send()``, not ``response.send_message()``
- Early-return paths (permission denied, active session, no voice channel)
  do NOT defer — they respond immediately with ``response.send_message()``
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock, AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Import setup_commands without requiring a full Discord setup.
# Same mocking strategy as test_commands.py / test_recording_callback.py.
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

from bot.commands import setup_commands  # noqa: E402
import bot.commands as _bc  # noqa: E402  — keep module reference alive for patching

# Restore original sys.modules so other test files get the real discord.
for _key in list(sys.modules):
    if _key == "discord" or _key.startswith("discord."):
        if _key in _original:
            sys.modules[_key] = _original[_key]
        else:
            del sys.modules[_key]

if "bot.commands" in sys.modules:
    del sys.modules["bot.commands"]
if "bot" in sys.modules:
    del sys.modules["bot"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeVoiceChannel:
    """Fake voice channel with an async connect() method."""

    def __init__(self, name: str = "Test Channel", connect_result=None):
        self.name = name
        self.id = 999
        if connect_result is not None:
            self.connect = AsyncMock(return_value=connect_result)
        else:
            self.connect = AsyncMock(return_value=MagicMock())


class FakeVoiceState:
    def __init__(self, channel=None):
        self.channel = channel


class FakeRole:
    def __init__(self, name: str, role_id: int):
        self.name = name
        self.id = role_id


class FakeUser:
    def __init__(self, roles=None, voice=None):
        self.roles = roles or []
        self.voice = voice


class FakeResponse:
    """Tracks defer() and send_message() calls on interaction.response."""

    def __init__(self):
        self.deferred = False
        self.defer_ephemeral = None
        self.send_message_calls = []

    async def defer(self, ephemeral=False):
        self.deferred = True
        self.defer_ephemeral = ephemeral

    async def send_message(self, content=None, **kwargs):
        self.send_message_calls.append({"content": content, **kwargs})


class FakeFollowup:
    """Tracks send() calls on interaction.followup."""

    def __init__(self):
        self.send_calls = []

    async def send(self, content=None, **kwargs):
        self.send_calls.append({"content": content, **kwargs})


class FakeInteraction:
    """Fake Discord interaction with response and followup."""

    def __init__(self, user, guild_id=123, channel_id=456):
        self.user = user
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel = MagicMock()
        self.channel.name = "general"

    @property
    def guild(self):
        g = MagicMock()
        g.voice_client = None
        return g


class FakeDB:
    def __init__(self, active_session=None):
        self._active = active_session
        self.created_sessions = []
        self.failed_sessions = []

    def get_any_active_session(self):
        return self._active

    def create_session(self, session_id, guild_id, channel_id):
        self.created_sessions.append({
            "session_id": session_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
        })

    def fail_session(self, session_id):
        self.failed_sessions.append(session_id)

    def get_active_session(self, guild_id):
        return self._active


class FakeLogger:
    def info(self, msg): pass
    def error(self, msg): pass
    def warning(self, msg): pass


class FakeConfig:
    restrict_commands = False
    allowed_roles: list = []


class FakeBot:
    """Minimal bot mock for setup_commands.

    The ``slash_command`` decorator captures command closures in
    ``self._commands`` so tests can invoke them directly.
    """

    def __init__(self, db):
        self.db = db
        self.logger = FakeLogger()
        self.config = FakeConfig()
        self.user = MagicMock()
        self.user.id = 555
        self._commands = {}

    def slash_command(self, **kwargs):
        def decorator(func):
            name = kwargs.get("name", func.__name__)
            self._commands[name] = func
            return func
        return decorator

    def add_application_command(self, cmd):
        pass

    def get_guild(self, guild_id):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bot(db=None):
    bot = FakeBot(db or FakeDB())
    setup_commands(bot)
    return bot


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStartCommandDefer:
    """Tests that /start defers the interaction before voice join."""

    async def test_defer_called_before_connect(self):
        """defer(ephemeral=True) must be called before voice_channel.connect()."""
        voice_channel = FakeVoiceChannel()
        user = FakeUser(voice=FakeVoiceState(channel=voice_channel))
        interaction = FakeInteraction(user=user)

        bot = _make_bot(FakeDB(active_session=None))
        cmd = bot._commands["start"]
        await cmd(interaction)

        assert interaction.response.deferred, \
            "interaction.response.defer() was not called"
        assert interaction.response.defer_ephemeral is True, \
            "defer() was not called with ephemeral=True"
        voice_channel.connect.assert_awaited(), \
            "voice_channel.connect() was not called"

    async def test_success_uses_followup_not_response(self):
        """After deferring, the success message must use followup.send()."""
        voice_channel = FakeVoiceChannel(name="General Voice")
        user = FakeUser(voice=FakeVoiceState(channel=voice_channel))
        interaction = FakeInteraction(user=user)

        bot = _make_bot(FakeDB(active_session=None))
        cmd = bot._commands["start"]
        await cmd(interaction)

        # After defer, must use followup.send, not response.send_message
        assert len(interaction.followup.send_calls) == 1, \
            "Expected exactly one followup.send() call"
        assert "Recording started" in interaction.followup.send_calls[0]["content"]
        # response.send_message should NOT be called after defer
        assert len(interaction.response.send_message_calls) == 0

    async def test_voice_join_failure_uses_followup(self):
        """When voice join fails, error must use followup.send() (not response)."""
        voice_channel = FakeVoiceChannel()
        voice_channel.connect = AsyncMock(side_effect=ConnectionError("Gateway timeout"))
        user = FakeUser(voice=FakeVoiceState(channel=voice_channel))
        interaction = FakeInteraction(user=user)

        db = FakeDB(active_session=None)
        bot = _make_bot(db)
        cmd = bot._commands["start"]
        await cmd(interaction)

        assert interaction.response.deferred, \
            "defer must be called before the voice join attempt"
        assert len(interaction.followup.send_calls) == 1, \
            "Expected followup.send() for error message"
        assert "Failed to join" in interaction.followup.send_calls[0]["content"]
        assert interaction.followup.send_calls[0]["ephemeral"] is True
        # fail_session must be called when voice join fails
        assert len(db.failed_sessions) == 1

    async def test_permission_denied_does_not_defer(self):
        """When permission is denied, respond immediately — no defer needed."""
        user = FakeUser(roles=[FakeRole("Player", 42)])
        interaction = FakeInteraction(user=user)

        bot = _make_bot(FakeDB())
        bot.config.restrict_commands = True
        bot.config.allowed_roles = ["DM"]

        cmd = bot._commands["start"]
        await cmd(interaction)

        # Permission denial should use response.send_message, NOT defer
        assert not interaction.response.deferred, \
            "defer() should not be called for permission denial"
        assert len(interaction.response.send_message_calls) == 1
        assert "permission" in interaction.response.send_message_calls[0]["content"].lower()

    async def test_active_session_does_not_defer(self):
        """When a session is already active, respond immediately — no defer."""
        voice_channel = FakeVoiceChannel()
        user = FakeUser(voice=FakeVoiceState(channel=voice_channel))
        interaction = FakeInteraction(user=user)

        bot = _make_bot(FakeDB(active_session={"id": "existing-session"}))
        cmd = bot._commands["start"]
        await cmd(interaction)

        assert not interaction.response.deferred, \
            "defer() should not be called when a session is already active"
        assert len(interaction.response.send_message_calls) == 1
        assert "already in progress" in interaction.response.send_message_calls[0]["content"]

    async def test_no_voice_channel_does_not_defer(self):
        """When user is not in a voice channel, respond immediately — no defer."""
        user = FakeUser(voice=FakeVoiceState(channel=None))
        interaction = FakeInteraction(user=user)

        bot = _make_bot(FakeDB(active_session=None))
        cmd = bot._commands["start"]
        await cmd(interaction)

        assert not interaction.response.deferred, \
            "defer() should not be called when user is not in a voice channel"
        assert len(interaction.response.send_message_calls) == 1
        assert "voice channel" in interaction.response.send_message_calls[0]["content"].lower()

    async def test_defer_ephemeral_is_true(self):
        """The defer call must use ephemeral=True so only the caller sees
        'Recording started' — other server members don't need to see it."""
        voice_channel = FakeVoiceChannel()
        user = FakeUser(voice=FakeVoiceState(channel=voice_channel))
        interaction = FakeInteraction(user=user)

        bot = _make_bot(FakeDB(active_session=None))
        cmd = bot._commands["start"]
        await cmd(interaction)

        assert interaction.response.defer_ephemeral is True

    async def test_session_created_before_voice_join_failure(self):
        """The DB session record should be created before deferring — if the
        voice join fails, fail_session() needs the session to already exist."""
        voice_channel = FakeVoiceChannel()
        voice_channel.connect = AsyncMock(side_effect=RuntimeError("boom"))
        user = FakeUser(voice=FakeVoiceState(channel=voice_channel))
        interaction = FakeInteraction(user=user)

        db = FakeDB(active_session=None)
        bot = _make_bot(db)
        cmd = bot._commands["start"]
        await cmd(interaction)

        # Session must have been created (before the voice join attempt)
        assert len(db.created_sessions) == 1
        # And fail_session must have been called with the same session ID
        assert len(db.failed_sessions) == 1
        assert db.failed_sessions[0] == db.created_sessions[0]["session_id"]
