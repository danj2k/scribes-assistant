"""Tests for bot/timestamped_sink.py — wall-clock-based temporal alignment.

TimestampedWaveSink must produce per-user WAV files where:
  1. Every user's file starts at the recording's zero point (initial delay
     padded with silence).
  2. DTX gaps (silence periods) are padded so each user's timeline matches
     real time.

These tests exercise the wall-clock-based padding logic by calling write()
with mocked VoiceData packets and inspecting the bytes written to each user's
temporary file.
"""

from __future__ import annotations

import io
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from bot.timestamped_sink import (
    TimestampedWaveSink,
    _BYTES_PER_FRAME,
    _SAMPLES_PER_FRAME,
    _SAMPLE_RATE,
    _CHANNELS,
    _SAMPLE_WIDTH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_voice_data(pcm: bytes) -> MagicMock:
    """Build a mock VoiceData with .pcm attribute.

    Sets __class__ to VoiceData so isinstance() in write() passes.
    The PCM should be stereo (interleaved L/R) as produced by the Opus decoder.
    """
    from discord.voice.packets import VoiceData

    data = MagicMock()
    data.pcm = pcm
    data.__class__ = VoiceData
    data.packet = MagicMock()
    data.packet.ssrc = 12345
    data.packet.sequence = 1
    return data


def _make_stereo_pcm(mono_samples: int, value: int = 1) -> bytes:
    """Generate stereo PCM (interleaved L/R) for testing.

    The Opus decoder produces stereo output even for mono streams.
    Each sample is 16-bit signed.
    """
    import array
    # Create stereo: L, R, L, R, ... where L=value, R=0
    samples = []
    for _ in range(mono_samples):
        samples.append(value)  # Left channel
        samples.append(0)      # Right channel (silence)
    return array.array("h", samples).tobytes()


def _make_sink() -> TimestampedWaveSink:
    """Create a TimestampedWaveSink with a fresh audio_data dict."""
    sink = TimestampedWaveSink()
    sink.audio_data = {}
    return sink


def _get_user_audio(sink: TimestampedWaveSink, user) -> bytes:
    """Extract written bytes for a user from the sink's temp file."""
    f = sink.audio_data[user].file
    pos = f.tell()
    f.seek(0)
    data = f.read()
    f.seek(pos)
    return data


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify Opus frame constants match the codec spec."""

    def test_sample_rate_is_48k(self):
        assert _SAMPLE_RATE == 48_000

    def test_channels_is_mono(self):
        assert _CHANNELS == 1

    def test_sample_width_is_16bit(self):
        assert _SAMPLE_WIDTH == 2

    def test_samples_per_frame_960(self):
        assert _SAMPLES_PER_FRAME == 960

    def test_bytes_per_frame_1920(self):
        assert _BYTES_PER_FRAME == 960 * 1 * 2  # mono, 16-bit

    def test_frame_duration_20ms(self):
        """960 samples at 48 kHz = 20 ms — the Opus standard frame size."""
        assert _SAMPLES_PER_FRAME / _SAMPLE_RATE == 0.02


# ---------------------------------------------------------------------------
# Initial delay padding (wall-clock based)
# ---------------------------------------------------------------------------


class TestInitialDelayPadding:
    """First packet from a user should pad silence for the delay between
    recording start (first packet from any user) and this user's first
    packet, using wall-clock time."""

    def test_first_user_no_initial_padding(self):
        """The very first user to speak defines the recording start.

        Their initial offset is ~0, so no silence padding is needed.
        """
        sink = _make_sink()
        user = MagicMock(name="user_a")
        # Stereo PCM: 960 mono samples, downmixed to 960 mono samples
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user)

        written = _get_user_audio(sink, user)
        # After downmix: 960 samples * 2 bytes = 1920 bytes
        expected_mono = _SAMPLES_PER_FRAME * _SAMPLE_WIDTH
        assert len(written) == expected_mono
        # No initial padding: written data is exactly the downmixed PCM payload.
        # The left channel (value=1) becomes the mono output.
        import array
        expected_pcm = array.array("h", [1] * _SAMPLES_PER_FRAME).tobytes()
        assert written == expected_pcm

    def test_late_joiner_gets_silence_padding(self):
        """A user who speaks 2 seconds after recording start should have
        ~2 seconds of silence prepended to their file.

        We control time.monotonic() to simulate a 2-second delay between
        the first user's packet (which sets _recording_start) and the
        second user's first packet.
        """
        sink = _make_sink()
        user_a = MagicMock(name="user_a")
        user_b = MagicMock(name="user_b")
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        # Use fixed monotonic times so the padding is deterministic.
        # t0 = first packet, t1 = t0 + 2.0s (second user's first packet).
        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user_a)

        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 2.0):
            sink.write(_make_voice_data(pcm), user_b)

        written_b = _get_user_audio(sink, user_b)
        # Expected silence: 2.0 s * 48000 Hz * 1 ch * 2 bytes = 192 000 bytes
        expected_silence = int(2.0 * _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH)
        # After downmix, PCM is mono: 960 samples * 2 bytes = 1920 bytes
        assert len(written_b) == expected_silence + _BYTES_PER_FRAME
        # First part is silence (all zeros).
        assert written_b[:expected_silence] == b"\x00" * expected_silence
        # Last part is the actual PCM (downmixed to mono).
        assert len(written_b[expected_silence:]) == _BYTES_PER_FRAME

    def test_sub_5ms_offset_no_padding(self):
        """Offsets below 5 ms are treated as jitter — no silence padded."""
        sink = _make_sink()
        user_a = MagicMock(name="user_a")
        user_b = MagicMock(name="user_b")
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user_a)

        # 3 ms delay — below the 5 ms threshold.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.003):
            sink.write(_make_voice_data(pcm), user_b)

        written_b = _get_user_audio(sink, user_b)
        # No padding: data is just the downmixed PCM (mono).
        assert len(written_b) == _BYTES_PER_FRAME

    def test_recording_start_set_on_first_packet(self):
        """_recording_start should be set after the first packet."""
        sink = _make_sink()
        assert sink._recording_start is None

        user = MagicMock()
        with patch("bot.timestamped_sink.time.monotonic", return_value=100.0):
            sink.write(_make_voice_data(b"\x01" * _BYTES_PER_FRAME), user)

        assert sink._recording_start == 100.0

    def test_explicit_recording_start_used_directly(self):
        """When recording_start is given, it should be used directly and
        never overridden by the first-packet lazy init."""
        explicit_start = 42.0
        sink = TimestampedWaveSink(recording_start=explicit_start)
        # Must be set before any writes.
        assert sink._recording_start == 42.0

        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        # Even with a different monotonic time at write, the explicit
        # recording_start should prevail.
        with patch("bot.timestamped_sink.time.monotonic", return_value=100.0):
            sink.write(_make_voice_data(pcm), user)

        assert sink._recording_start == 42.0  # unchanged

    def test_explicit_start_silence_padding(self):
        """With an explicit recording_start, a user whose first packet
        arrives after that start gets silence padding for the offset."""
        explicit_start = 100.0
        sink = TimestampedWaveSink(recording_start=explicit_start)
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        # User's first packet arrives 3 seconds after recording_start.
        with patch("bot.timestamped_sink.time.monotonic", return_value=explicit_start + 3.0):
            sink.write(_make_voice_data(pcm), user)

        written = _get_user_audio(sink, user)
        # Expected silence: 3.0 s of PCM silence.
        expected_silence = int(3.0 * _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH)
        # After downmix, PCM is mono
        assert len(written) == expected_silence + _BYTES_PER_FRAME
        assert written[:expected_silence] == b"\x00" * expected_silence
        assert len(written[expected_silence:]) == _BYTES_PER_FRAME


# ---------------------------------------------------------------------------
# DTX gap padding (wall-clock based)
# ---------------------------------------------------------------------------


class TestDTXGapPadding:
    """Gaps between consecutive packets from the same user should be
    padded with silence based on wall-clock elapsed time."""

    def test_no_gap_no_padding(self):
        """Consecutive frames with no time gap get no silence."""
        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user)
        # Second packet: 20ms later (1 frame duration).
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.020):
            sink.write(_make_voice_data(pcm), user)

        written = _get_user_audio(sink, user)
        # Two frames of mono PCM, no silence (deficit < 1 frame).
        assert len(written) == 2 * _BYTES_PER_FRAME

    def test_dtx_gap_padded_with_silence(self):
        """A gap of 100ms should be padded with silence.

        The wall-clock approach compares expected samples (based on elapsed
        time) vs actual samples written, and pads the deficit.
        """
        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user)

        # 100ms gap — should pad with ~100ms of silence.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.120):
            sink.write(_make_voice_data(pcm), user)

        written = _get_user_audio(sink, user)
        # Expected: first PCM + ~100ms silence + second PCM
        # 100ms = 0.1s * 48000 Hz * 2 bytes = 9600 bytes
        expected_silence = int(0.1 * _SAMPLE_RATE * _SAMPLE_WIDTH)
        # After downmix, each PCM frame is _BYTES_PER_FRAME bytes
        assert len(written) == _BYTES_PER_FRAME + expected_silence + _BYTES_PER_FRAME
        assert written[:_BYTES_PER_FRAME] != b"\x00" * _BYTES_PER_FRAME  # First frame is not silence
        assert written[_BYTES_PER_FRAME:_BYTES_PER_FRAME + expected_silence] == b"\x00" * expected_silence
        assert written[_BYTES_PER_FRAME + expected_silence:] != b"\x00" * _BYTES_PER_FRAME  # Second frame is not silence

    def test_small_gap_below_frame_threshold_not_padded(self):
        """Gaps smaller than one frame (20ms) are treated as jitter."""
        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user)

        # 10ms gap — below the 20ms threshold.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.030):
            sink.write(_make_voice_data(pcm), user)

        written = _get_user_audio(sink, user)
        # No silence padded — just two mono PCM payloads.
        assert len(written) == 2 * _BYTES_PER_FRAME

    def test_large_gap_capped_at_5_seconds(self):
        """Gaps larger than 5 seconds are capped to avoid huge silence blocks."""
        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user)

        # 10 second gap — should be capped at 5 seconds.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 10.0):
            sink.write(_make_voice_data(pcm), user)

        written = _get_user_audio(sink, user)
        # Expected: first PCM + 5s silence + second PCM
        max_silence = int(5.0 * _SAMPLE_RATE * _SAMPLE_WIDTH)
        # After downmix, each PCM frame is _BYTES_PER_FRAME bytes
        assert len(written) == _BYTES_PER_FRAME + max_silence + _BYTES_PER_FRAME


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: empty PCM, multiple users, Filters.container guard."""

    def test_empty_pcm_skipped(self):
        """Packets with empty PCM should be skipped entirely."""
        sink = _make_sink()
        user = MagicMock()
        vd = _make_voice_data(b"")

        sink.write(vd, user)

        # No audio_data entry should have been created.
        assert user not in sink.audio_data

    def test_raw_bytes_not_voicedata(self):
        """When data is raw bytes (not VoiceData), pcm = data."""
        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=2)

        # Pass raw bytes — isinstance(data, VoiceData) is False.
        sink.write(pcm, user)

        written = _get_user_audio(sink, user)
        assert written == pcm

    def test_multiple_users_independent_tracking(self):
        """Each user should have independent sample tracking."""
        sink = _make_sink()
        user_a = MagicMock(name="a")
        user_b = MagicMock(name="b")
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        t0 = 100.0
        # User A: two packets with no gap.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user_a)
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.020):
            sink.write(_make_voice_data(pcm), user_a)

        # User B: first packet at same time (no initial offset), then a gap.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm), user_b)
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.100):
            sink.write(_make_voice_data(pcm), user_b)

        written_a = _get_user_audio(sink, user_a)
        written_b = _get_user_audio(sink, user_b)

        # User A: no gaps, no initial padding -> 2 frames of mono PCM.
        assert len(written_a) == 2 * _BYTES_PER_FRAME

        # User B: 80ms gap -> ~80ms silence
        # Allow for small floating-point precision differences
        expected_silence = int(0.080 * _SAMPLE_RATE * _SAMPLE_WIDTH)
        # After downmix, each PCM frame is _BYTES_PER_FRAME bytes
        # Allow ±10 bytes for floating-point precision
        assert abs(len(written_b) - (_BYTES_PER_FRAME + expected_silence + _BYTES_PER_FRAME)) <= 10

    def test_filtered_users_skips_write(self):
        """Filters.container: if filtered_users is set and user is NOT
        in it, write() should return None (no data written)."""
        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)

        # Set up filtered_users so the Filters.container decorator
        # short-circuits. The user must NOT be in this set.
        other_user = MagicMock(name="other")
        sink.filtered_users = {other_user}

        sink.write(_make_voice_data(pcm), user)

        # The container decorator returns None when user not in filtered_users.
        assert user not in sink.audio_data


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """The sink uses a threading.Lock to serialise write access."""

    def test_lock_exists(self):
        sink = _make_sink()
        assert hasattr(sink, "_lock")
        assert sink._lock is not None

    def test_concurrent_writes_no_corruption(self):
        """Concurrent writes from different users should not corrupt each
        other's audio data."""
        import threading

        sink = _make_sink()
        user = MagicMock()
        pcm = _make_stereo_pcm(_SAMPLES_PER_FRAME, value=1)
        num_threads = 10
        writes_per_thread = 50

        def writer():
            for _ in range(writes_per_thread):
                sink.write(_make_voice_data(pcm), user)

        threads = [threading.Thread(target=writer) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        written = _get_user_audio(sink, user)
        # All threads wrote the same PCM with no time gap, so no padding.
        # Each write produces _BYTES_PER_FRAME bytes of mono PCM.
        expected_len = num_threads * writes_per_thread * _BYTES_PER_FRAME
        assert len(written) == expected_len


# ---------------------------------------------------------------------------
# format_audio() -- mono WAV header override
# ---------------------------------------------------------------------------


class TestFormatAudio:
    """Verify that format_audio() writes a correct mono WAV from raw PCM.

    The override replaces the stereo header from WaveSink.format_audio() with
    a correct mono header (1 channel, 16-bit, 48 kHz).
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_pcm(num_samples: int, value: int = 0) -> bytes:
        """Generate ``num_samples`` mono 16-bit PCM samples of the given value.

        Each sample is 2 bytes (little-endian signed 16-bit).
        """
        return value.to_bytes(2, "little", signed=True) * num_samples

    @staticmethod
    def _open_wav(file: io.BytesIO):
        """Open a BytesIO as a WAV file and return the wave.Wave_read object."""
        import wave

        file.seek(0)
        return wave.open(file, "rb")

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_format_audio_produces_valid_wav(self):
        """Given 1 second of mono PCM, format_audio() produces a valid WAV
        whose content exactly matches the input."""
        from discord.sinks import AudioData

        num_samples = _SAMPLE_RATE  # 1 second at 48 kHz
        pcm = self._make_pcm(num_samples, value=42)
        audio_data = AudioData(file=io.BytesIO(pcm))

        sink = _make_sink()
        sink.format_audio(audio_data)

        wav = self._open_wav(audio_data.file)
        assert wav.getnchannels() == 1, "must be mono"
        assert wav.getsampwidth() == 2, "must be 16-bit"
        assert wav.getframerate() == _SAMPLE_RATE, "must be 48 kHz"
        assert wav.getnframes() == num_samples, "must preserve sample count"
        result_pcm = wav.readframes(num_samples)
        assert result_pcm == pcm, "PCM data must be preserved verbatim"

    def test_format_audio_empty_pcm(self):
        """An empty PCM buffer (zero-length data) produces a valid zero-length
        WAV with correct header values but no frames."""
        from discord.sinks import AudioData

        audio_data = AudioData(file=io.BytesIO(b""))

        sink = _make_sink()
        sink.format_audio(audio_data)

        wav = self._open_wav(audio_data.file)
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == _SAMPLE_RATE
        assert wav.getnframes() == 0

    def test_format_audio_writes_mono_header(self):
        """The WAV header declares exactly 1 channel -- the core fix for the
        0-byte .wav bug (the original WaveSink wrote stereo headers for mono
        data)."""
        from discord.sinks import AudioData

        pcm = self._make_pcm(_SAMPLES_PER_FRAME, value=128)
        audio_data = AudioData(file=io.BytesIO(pcm))

        sink = _make_sink()
        sink.format_audio(audio_data)

        wav = self._open_wav(audio_data.file)
        assert wav.getnchannels() == 1

    def test_format_audio_replaces_file_attribute(self):
        """After format_audio(), audio_data.file must be a new BytesIO (not the
        original raw PCM)."""
        from discord.sinks import AudioData

        original = io.BytesIO(self._make_pcm(_SAMPLES_PER_FRAME))
        audio_data = AudioData(file=original)

        sink = _make_sink()
        sink.format_audio(audio_data)

        # The file should now be a WAV BytesIO, not the original
        assert audio_data.file is not original
        # It must still be seekable and readable as WAV
        self._open_wav(audio_data.file)
