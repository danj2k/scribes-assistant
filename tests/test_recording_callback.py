"""Tests for the recording after-callback in bot/commands.py.

Tests the make_recording_after_callback function which replaces the old
broken recording_finished_callback. The key behaviours tested:

- The callback is synchronous (py-cord requires a sync after-callback)
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
import math
import os
import struct
import sys
import wave
import tempfile
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

class FakeUser:
    """Mimics a discord.User/Member as an audio_data dict key.

    pycord keys sink.audio_data by user objects, not IDs. Our code calls
    .id, str(), and passes the key to guild.get_member().
    """
    def __init__(self, id: int, display_name: str = None):
        self.id = id
        self._display_name = display_name or str(id)

    def __str__(self):
        return self._display_name

    # So guild.get_member(user_obj) lookups work when FakeGuild stores
    # members keyed by the same FakeUser instance.
    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if isinstance(other, FakeUser):
            return self.id == other.id
        return self.id == other


class FakeAudioData:
    """Mimics py-cord's audio data container, backed by a temp file on disk."""

    def __init__(self, data: bytes):
        self.file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        self.file.write(data)
        self.file.flush()
        self.file.seek(0)

    def cleanup(self):
        self.file.seek(0)


class FakeSink:
    """Mimics discord.sinks.WaveSink — just needs audio_data dict and _temp_paths."""

    def __init__(self, audio_data: dict):
        self.audio_data = audio_data
        self._temp_paths = {}
        for user, ad in audio_data.items():
            self._temp_paths[user] = ad.file.name

    def format_audio(self, audio_data):
        pass  # no-op — real WaveSink writes WAV header here


class FakeMember:
    def __init__(self, display_name: str):
        self.display_name = display_name


class FakeGuild:
    def __init__(self, members: dict):
        self._members = members

    def get_member(self, user_obj):
        # Key by user ID so lookups work with FakeUser or raw int keys
        key = user_obj if isinstance(user_obj, int) else user_obj.id
        return self._members.get(key)


class FakeDB:
    """Minimal DB mock that records add_audio_file and fail_session calls."""

    def __init__(self):
        self.audio_files = []
        self.failed_sessions = []

    def add_audio_file(self, session_id, filepath, size_bytes=None,
                       discord_user_id=None, speaker_name=None):
        self.audio_files.append({
            "session_id": session_id,
            "filepath": filepath,
            "size_bytes": size_bytes,
            "discord_user_id": discord_user_id,
            "speaker_name": speaker_name,
        })

    def fail_session(self, session_id):
        self.failed_sessions.append(session_id)


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
    """Mock disk I/O so no real filesystem access happens.

    The new code path writes WAV-formatted BytesIO data to the final
    path via _write_wav_file (in a thread pool via asyncio.to_thread),
    then removes the raw-PCM temp file.  This fixture intercepts those
    calls so tests can verify content without touching /data.
    """
    written_files: dict[str, bytes | str] = {}

    def mock_makedirs(path, exist_ok=False):
        pass  # no-op — don't touch real filesystem

    def mock_write_wav(filepath, wav_bytes, temp_path):
        written_files[filepath] = wav_bytes
        # Clean up the temp file as the real function does
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    def mock_getsize(path):
        data = written_files.get(path)
        if data is not None:
            return len(data)
        return 0

    monkeypatch.setattr(_bc_commands.os, "makedirs", mock_makedirs)
    monkeypatch.setattr(_bc_commands, "_write_wav_file", mock_write_wav)
    monkeypatch.setattr(_bc_commands.os.path, "getsize", mock_getsize)
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
        user1 = FakeUser(111)
        user2 = FakeUser(222)
        sink = FakeSink({
            user1: audio_data_1,
            user2: audio_data_2,
        })

        bot = FakeBot(db)

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        # PR #3159 calls after(sink, *args) — sink is the first positional arg
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        print("\nDB audio_files:", db.audio_files)
        print("mock_file_io keys:", list(mock_file_io.keys()))

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
        user = FakeUser(111)
        member = FakeMember("Rogar the Brave")
        guild = FakeGuild({111: member})
        bot = FakeBot(db, guild=guild)

        sink = FakeSink({user: FakeAudioData(b"audio data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        assert db.audio_files[0]["speaker_name"] == "Rogar the Brave"

    async def test_speaker_name_falls_back_to_user_id(self, mock_file_io):
        """If guild member lookup fails, fall back to str(user)."""
        db = FakeDB()
        guild = FakeGuild({})  # No members
        bot = FakeBot(db, guild=guild)

        sink = FakeSink({FakeUser(999): FakeAudioData(b"audio data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        assert db.audio_files[0]["speaker_name"] == "999"

    async def test_future_resolves_on_success(self, mock_file_io):
        """The future must resolve after processing completes."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({FakeUser(111): FakeAudioData(b"data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)
        assert future.done()
        # Future result is the audio file count (1 file in this test)
        assert future.result() == 1

    async def test_future_resolves_on_error(self):
        """The future must resolve even if processing throws an exception."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({FakeUser(111): FakeAudioData(b"data")})

        # Force an error by making os.makedirs raise
        with patch("os.makedirs", side_effect=OSError("disk full")):
            after_cb, future = make_recording_after_callback(
                bot, "test-session", "123", "456", sink,
            )

            after_cb(sink)
            await asyncio.wait_for(future, timeout=5.0)
            assert future.done()

    async def test_callback_with_no_audio_data(self, mock_file_io):
        """If nobody spoke, the callback marks the session as failed and
        resolves the future with 0 (zero audio files)."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({})  # No audio data

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        assert future.done()
        assert future.result() == 0
        assert len(db.audio_files) == 0
        # The callback must call fail_session so the session doesn't
        # stay stuck in RECORDING/QUEUED with no transcript path.
        assert db.failed_sessions == ["test-session"]

    async def test_callback_receives_sink_argument(self, mock_file_io):
        """PR #3159 passes the sink as the first positional arg, not an exception."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )

        # PR #3159 calls after(sink, *args) — sink is the first positional arg
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

    async def test_filename_sanitized(self, mock_file_io):
        """Display names with unsafe characters (slashes, etc.) must be
        sanitised so they don't create spurious directories."""
        from bot.commands import _sanitize_filename

        assert _sanitize_filename("Duckinell/DM") == "Duckinell_DM"
        assert _sanitize_filename("danj2k (Danj)") == "danj2k (Danj)"
        assert _sanitize_filename("a/b\\c:d") == "a_b_c_d"
        assert _sanitize_filename("  spaces  ") == "spaces"
        assert _sanitize_filename("") == "unknown"
        assert _sanitize_filename("///") == "_"

    async def test_writes_wav_files_to_disk(self, mock_file_io):
        """Audio data should be written as .wav files to the recording directory."""
        audio_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt ...data"
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({FakeUser(42): FakeAudioData(audio_bytes)})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )
        after_cb(sink)
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
        sink = FakeSink({FakeUser(111): FakeAudioData(b"data")})

        received_guild_ids = []
        original_get_guild = bot.get_guild

        def tracking_get_guild(gid):
            received_guild_ids.append(gid)
            return original_get_guild(gid)

        bot.get_guild = tracking_get_guild

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123456", "456", sink,
        )
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        assert received_guild_ids[0] == 123456  # Must be int, not str

    async def test_multiple_callbacks_dont_interfere(self, mock_file_io):
        """Each call to make_recording_after_callback creates independent state."""
        db1, db2 = FakeDB(), FakeDB()
        bot1 = FakeBot(db1)
        bot2 = FakeBot(db2)
        sink1 = FakeSink({FakeUser(1): FakeAudioData(b"a")})
        sink2 = FakeSink({FakeUser(2): FakeAudioData(b"b")})

        after1, future1 = make_recording_after_callback(
            bot1, "session-1", "100", "200", sink1,
        )
        after2, future2 = make_recording_after_callback(
            bot2, "session-2", "300", "400", sink2,
        )

        after1(sink1)
        after2(sink2)

        await asyncio.wait_for(future1, timeout=5.0)
        await asyncio.wait_for(future2, timeout=5.0)

        assert len(db1.audio_files) == 1
        assert len(db2.audio_files) == 1
        assert db1.audio_files[0]["session_id"] == "session-1"
        assert db2.audio_files[0]["session_id"] == "session-2"

    async def test_future_returns_audio_count(self, mock_file_io):
        """The future result is the number of audio files saved."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({
            FakeUser(1): FakeAudioData(b"a"),
            FakeUser(2): FakeAudioData(b"b"),
            FakeUser(3): FakeAudioData(b"c"),
        })

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        assert future.result() == 3
        # Non-empty recording must NOT call fail_session
        assert db.failed_sessions == []

    async def test_files_written_to_destination(self, mock_file_io):
        """WAV-formatted bytes are written to the final path."""
        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({FakeUser(111): FakeAudioData(b"data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        assert future.result() == 1
        # Verify this test actually exercised the write path
        # (mock_file_io captures files written via _write_wav_file)
        assert len(mock_file_io) == 1
        assert list(mock_file_io.keys())[0].endswith("111.wav")

    async def test_filename_sanitized_in_callback(self, mock_file_io):
        """A display name containing '/' must not create a subdirectory."""
        db = FakeDB()
        user = FakeUser(111, display_name="Duckinell/DM")
        guild = FakeGuild({111: FakeMember("Duckinell/DM")})
        bot = FakeBot(db, guild=guild)

        sink = FakeSink({user: FakeAudioData(b"audio data")})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        # The sanitised filename should contain no slash
        wav_paths = [p for p in mock_file_io if p.endswith(".wav")]
        assert len(wav_paths) == 1
        assert "/" not in wav_paths[0].split("/")[-1], \
            f"Filename contains slash: {wav_paths[0]}"
        assert wav_paths[0].endswith("Duckinell_DM.wav")

    async def test_output_wav_is_valid(self, mock_file_io):
        """Written WAV files must be valid WAV with correct headers and audio data.

        Regression test for the zero-byte output bug caused by
        format_audio() not writing back the WAV BytesIO to
        audio_data.file. Without that assignment, audio_data.file
        still pointed at the raw-PCM temp file at EOF, producing
        zero-byte files on disk.
        """
        from io import BytesIO

        # Build a genuine 1-second mono 16-bit 48kHz WAV file
        # with a 440 Hz sine wave.
        sample_rate = 48000
        duration_samples = sample_rate  # 1 second
        raw_pcm = struct.pack(
            f"<{duration_samples}h",
            *(int(0.5 * 32767 * math.sin(2 * math.pi * 440.0 * i / sample_rate))
              for i in range(duration_samples)),
        )

        buf = BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(raw_pcm)
        wav_bytes = buf.getvalue()

        # Sanity check our fixture is valid
        assert len(wav_bytes) > 44, "WAV header + data must exceed header-only"
        assert wav_bytes[:4] == b"RIFF"
        assert wav_bytes[8:12] == b"WAVE"

        db = FakeDB()
        bot = FakeBot(db)
        sink = FakeSink({FakeUser(42): FakeAudioData(wav_bytes)})

        after_cb, future = make_recording_after_callback(
            bot, "test-session", "123", "456", sink,
        )
        after_cb(sink)
        await asyncio.wait_for(future, timeout=5.0)

        # Retrieve the bytes the callback wrote via mock_file_io
        wav_paths = [p for p in mock_file_io if p.endswith("42.wav")]
        assert len(wav_paths) == 1, f"Expected one .wav but got {list(mock_file_io)}"
        written_bytes = mock_file_io[wav_paths[0]]

        # === Validity checks ===

        # 1. File is non-empty
        assert len(written_bytes) > 44, (
            f"Written WAV is too small to have a header: {len(written_bytes)} bytes"
        )

        # 2. RIFF / WAVE signature
        assert written_bytes[:4] == b"RIFF"
        assert written_bytes[8:12] == b"WAVE"

        # 3. Can be parsed by Python's wave module
        with wave.open(BytesIO(written_bytes), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == sample_rate
            assert wf.getnframes() == duration_samples

        # 4. Audio content is preserved round-trip
        with wave.open(BytesIO(written_bytes), "rb") as wf:
            decoded = wf.readframes(wf.getnframes())
        assert decoded == raw_pcm, (
            "Decoded audio frames do not match the original PCM data"
        )

        # 5. DB records a non-zero size matching the written file
        assert db.audio_files[0]["size_bytes"] > 44
        assert db.audio_files[0]["size_bytes"] == len(written_bytes)
