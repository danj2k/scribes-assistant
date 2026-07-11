"""Tests for the recording after-callback in bot/commands.py.

Tests the make_recording_after_callback function which replaces the old
broken recording_finished_callback. The key behaviours tested:

- The callback is synchronous (py-cord 2.8.0 requires a sync after-callback)
- The callback schedules async audio processing on the event loop
- Audio files are written to disk and registered in the DB
- Speaker names are resolved from the guild member cache
- The returned future resolves when processing completes
- The future resolves even if processing errors out (no hang on /stop)
"""

from __future__ import annotations

import asyncio
import inspect
import io
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import make_recording_after_callback without requiring a full Discord setup.
# Same mocking strategy as test_commands.py for _check_permission.
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

from bot.commands import make_recording_after_callback  # noqa: E402
import bot.commands as _bc_commands  # noqa: E402

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

class FakeAudioData:
    """Mimics py-cord's BytesIO-based audio data container."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def getbuffer(self):
        return self._buf.getbuffer()


class FakeSink:
    """Mimics discord.sinks.WaveSink — just needs audio_data dict."""

    def __init__(self, audio_data: dict):
        self.audio_data = audio_data


class FakeMember:
    def __init__(self, display_name: str):
        self.display_name = display_name


class FakeGuild:
    def __init__(self, members: dict):
        self._members = members

    def get_member(self, user_id):
        return self._members.get(user_id)


class FakeDB:
    """Minimal DB mock that records add_audio_file calls."""

    def __init__(self):
        self.audio_files = []

    def add_audio_file(self, session_id, filepath, size_bytes=None,
                       discord_user_id=None, speaker_name=None):
        self.audio_files.append({
            "session_id": session_id,
            "filepath": filepath,
            "size_bytes": size_bytes,
            "discord_user_id": discord_user_id,
            "speaker_name": speaker_name,
        })


class FakeLogger:
    def info(self, msg): pass
    def error(self, msg): pass
    def warning(self, msg): pass


class FakeBot:
    """Minimal bot mock with db, logger, and get_guild."""

    def __init__(self, db, guild=None):
        self.db = db
        self.logger = FakeLogger()
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild


@pytest.fixture
def mock_file_io(monkeypatch):
    """Mock os.makedirs and builtins.open so no real disk I/O happens.

    Files are tracked in a dict mapping path -> bytes written, so tests
    can verify content without touching /data.
    """
    written_files: dict[str, bytes | str] = {}

    def mock_makedirs(path, exist_ok=False):
        pass  # no-op — paths are virtual

    def mock_open(path, mode, *args, **kwargs):
        if "w" in mode and "b" in mode:
            buf = io.BytesIO()
            # Capture writes on close
            original_close = buf.close
            def capture_close():
                written_files[str(path)] = buf.getvalue()
                original_close()
            buf.close = capture_close
            return buf
        elif "w" in mode:
            buf = io.StringIO()
            original_close = buf.close
            def capture_close():
                written_files[str(path)] = buf.getvalue()
                original_close()
            buf.close = capture_close
            return buf
        raise NotImplementedError(f"mock_open: mode {mode} not supported")

    monkeypatch.setattr(_bc_commands.os, "makedirs", mock_makedirs)
    monkeypatch.setattr("builtins.open", mock_open)
    return written_files


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMakeRecordingAfterCallback:
    """Tests for the recording after-callback factory."""

    async def test_returns_sync_callable_and_future(self):
        """The callback must be a sync callable, and a future is returned."""
        bot = FakeBot(FakeDB())
        sink = FakeSink({})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        assert callable(after_cb)
        assert not inspect.iscoroutinefunction(after_cb)
        assert isinstance(future, asyncio.Future)
        assert not future.done()

    async def test_callback_processes_audio_and_registers_in_db(self, mock_file_io):
        """When invoked, the callback writes WAV files and registers them in DB."""
        db = FakeDB()
        audio_data_1 = FakeAudioData(b"RIFF\x24\x00\x00\x00WAVEfmt ...")
        audio_data_2 = FakeAudioData(b"RIFF\x24\x00\x00\x00WAVEfmt ...")
        sink = FakeSink({
            111: audio_data_1,
            222: audio_data_2,
        })

        bot = FakeBot(db)

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)

        assert len(db.audio_files) == 2
        paths = [f["filepath"] for f in db.audio_files]
        assert any("111" in p for p in paths)
        assert any("222" in p for p in paths)

        for af in db.audio_files:
            assert af["session_id"] == "test-session"
            assert af["discord_user_id"] is not None
            assert af["size_bytes"] is not None

    async def test_speaker_name_resolved_from_guild(self, mock_file_io):
        """Speaker display names should be resolved from the guild member cache."""
        db = FakeDB()
        member = FakeMember("Rogar the Brave")
        guild = FakeGuild({111: member})
        bot = FakeBot(db, guild=guild)

        sink = FakeSink({111: FakeAudioData(b"audio data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)

        assert len(db.audio_files) == 1
        assert db.audio_files[0]["speaker_name"] == "Rogar the Brave"

    async def test_speaker_name_falls_back_to_user_id(self, mock_file_io):
        """If guild member lookup fails, fall back to str(user_id)."""
        db = FakeDB()
        guild = FakeGuild({})  # No members
        bot = FakeBot(db, guild=guild)

        sink = FakeSink({999: FakeAudioData(b"audio data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)

        assert len(db.audio_files) == 1
        assert db.audio_files[0]["speaker_name"] == "999"

    async def test_future_resolves_on_success(self, mock_file_io):
        """The future must resolve after processing completes."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({111: FakeAudioData(b"data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)
        assert future.done()
        assert future.result() is None

    async def test_future_resolves_on_error(self):
        """The future must resolve even if processing throws an exception."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({111: FakeAudioData(b"data")})

        # Force an error by making os.makedirs raise
        with patch("os.makedirs", side_effect=OSError("disk full")):
            after_cb, future = make_recording_after_callback(
                bot, "test-session", "123", "456", sink,
            )

            after_cb(None)
            await asyncio.wait_for(future, timeout=5.0)
            assert future.done()

    async def test_callback_with_no_audio_data(self, mock_file_io):
        """If nobody spoke, the callback should still complete without error."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({})  # No audio data

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)

        assert future.done()
        assert len(db.audio_files) == 0

    async def test_callback_receives_exception_argument(self, mock_file_io):
        """The sync callback must accept an exception argument (py-cord contract)."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        # py-cord calls after(exc) — exc can be None or an Exception
        after_cb(None)  # No error
        await asyncio.wait_for(future, timeout=5.0)

    async def test_writes_wav_files_to_disk(self, mock_file_io):
        """Audio data should be written as .wav files to the recording directory."""
        audio_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt ...data"
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({42: FakeAudioData(audio_bytes)})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )
        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)

        # File content should be captured by mock_file_io
        wav_paths = [p for p in mock_file_io if p.endswith("42.wav")]
        assert len(wav_paths) == 1
        assert mock_file_io[wav_paths[0]] == audio_bytes

    async def test_guild_id_converted_to_int_for_lookup(self, mock_file_io):
        """guild_id is passed as a string; get_guild should receive an int."""
        db = FakeDB()
        guild = FakeGuild({111: FakeMember("TestUser")})
        bot = FakeBot(db, guild=guild)
        sink = FakeSink({111: FakeAudioData(b"data")})

        received_guild_ids = []
        original_get_guild = bot.get_guild

        def tracking_get_guild(gid):
            received_guild_ids.append(gid)
            return original_get_guild(gid)

        bot.get_guild = tracking_get_guild

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123456", "456", sink,
        )
        after_cb(None)
        await asyncio.wait_for(future, timeout=5.0)

        assert len(received_guild_ids) == 1
        assert received_guild_ids[0] == 123456  # Must be int, not str

    async def test_multiple_callbacks_dont_interfere(self, mock_file_io):
        """Each call to make_recording_after_callback creates independent state."""
        db1, db2 = FakeDB(), FakeDB()
        bot1 = FakeBot(db1)
        bot2 = FakeBot(db2)
        sink1 = FakeSink({1: FakeAudioData(b"a")})
        sink2 = FakeSink({2: FakeAudioData(b"b")})

        after1, future1 = make_recording_after_callback(
            bot1, "session-1", "100", "200", sink1,
        )
        after2, future2 = make_recording_after_callback(
            bot2, "session-2", "300", "400", sink2,
        )

        after1(None)
        after2(None)

        await asyncio.wait_for(future1, timeout=5.0)
        await asyncio.wait_for(future2, timeout=5.0)

        assert len(db1.audio_files) == 1
        assert len(db2.audio_files) == 1
        assert db1.audio_files[0]["session_id"] == "session-1"
        assert db2.audio_files[0]["session_id"] == "session-2"
