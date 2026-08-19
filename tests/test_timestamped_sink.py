"""Tests for bot/timestamped_sink.py — silence padding for temporal alignment.

TimestampedWaveSink must produce per-user WAV files where:
  1. Every user's file starts at the recording's zero point (initial delay
     padded with silence).
  2. DTX gaps (silence periods) are padded so each user's timeline matches
     real time.

These tests exercise the silence-padding logic by calling write() with
mocked VoiceData packets and inspecting the bytes written to each user's
AudioData buffer.
"""

from __future__ import annotations

import io
import time
from unittest.mock import MagicMock, patch

import pytest

from bot.timestamped_sink import (
    TimestampedWaveSink,
    _BYTES_PER_FRAME,
    _MAX_PLAUSIBLE_GAP,
    _SAMPLES_PER_FRAME,
    _SAMPLE_RATE,
    _CHANNELS,
    _SAMPLE_WIDTH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_voice_data(pcm: bytes, rtp_ts: int | None = None) -> MagicMock:
    """Build a mock VoiceData with .pcm and .packet.timestamp attributes.

    Sets __class__ to VoiceData so isinstance() in write() passes.
    """
    from discord.voice.packets import VoiceData

    data = MagicMock()
    data.pcm = pcm
    data.__class__ = VoiceData
    data.packet = MagicMock()
    data.packet.timestamp = rtp_ts
    return data


def _make_sink() -> TimestampedWaveSink:
    """Create a TimestampedWaveSink with a fresh audio_data dict."""
    sink = TimestampedWaveSink()
    sink.audio_data = {}
    return sink


def _get_user_audio(sink: TimestampedWaveSink, user) -> bytes:
    """Extract written bytes for a user from the sink's audio_data.

    The sink now writes to temp files (not BytesIO), so we use seek+read
    instead of MemoryIO-specific getvalue().
    """
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
        assert _BYTES_PER_FRAME == 960 * 1 * 2  # samples * channels * sample_width

    def test_frame_duration_20ms(self):
        """960 samples at 48 kHz = 20 ms — the Opus standard frame size."""
        assert _SAMPLES_PER_FRAME / _SAMPLE_RATE == 0.02


# ---------------------------------------------------------------------------
# Initial delay padding
# ---------------------------------------------------------------------------


class TestInitialDelayPadding:
    """First packet from a user should pad silence for the delay between
    recording start (first packet from any user) and this user's first
    packet."""

    def test_first_user_no_initial_padding(self):
        """The very first user to speak defines the recording start.

        Their initial offset is ~0, so no silence padding is needed.
        """
        sink = _make_sink()
        user = MagicMock(name="user_a")
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)

        written = _get_user_audio(sink, user)
        # No initial padding: written data is exactly the PCM payload.
        assert written == pcm

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
        pcm = b"\x01" * _BYTES_PER_FRAME

        # Use fixed monotonic times so the padding is deterministic.
        # t0 = first packet, t1 = t0 + 2.0s (second user's first packet).
        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user_a)

        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 2.0):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user_b)

        written_b = _get_user_audio(sink, user_b)
        # Expected silence: 2.0 s * 48000 Hz * 1 ch * 2 bytes = 192 000 bytes
        expected_silence = int(2.0 * _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH)
        assert len(written_b) == expected_silence + len(pcm)
        # First part is silence (all zeros).
        assert written_b[:expected_silence] == b"\x00" * expected_silence
        # Last part is the actual PCM.
        assert written_b[expected_silence:] == pcm

    def test_sub_5ms_offset_no_padding(self):
        """Offsets below 5 ms are treated as jitter — no silence padded."""
        sink = _make_sink()
        user_a = MagicMock(name="user_a")
        user_b = MagicMock(name="user_b")
        pcm = b"\x01" * _BYTES_PER_FRAME

        t0 = 100.0
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user_a)

        # 3 ms delay — below the 5 ms threshold.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0 + 0.003):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user_b)

        written_b = _get_user_audio(sink, user_b)
        # No padding: data is just the PCM.
        assert written_b == pcm

    def test_recording_start_set_on_first_packet(self):
        """_recording_start should be set after the first packet."""
        sink = _make_sink()
        assert sink._recording_start is None

        user = MagicMock()
        with patch("bot.timestamped_sink.time.monotonic", return_value=100.0):
            sink.write(_make_voice_data(b"\x01" * _BYTES_PER_FRAME, rtp_ts=0), user)

        assert sink._recording_start == 100.0

    def test_explicit_recording_start_used_directly(self):
        """When recording_start is given, it should be used directly and
        never overridden by the first-packet lazy init."""
        explicit_start = 42.0
        sink = TimestampedWaveSink(recording_start=explicit_start)
        # Must be set before any writes.
        assert sink._recording_start == 42.0

        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        # Even with a different monotonic time at write, the explicit
        # recording_start should prevail.
        with patch("bot.timestamped_sink.time.monotonic", return_value=100.0):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user)

        assert sink._recording_start == 42.0  # unchanged

    def test_explicit_start_silence_padding(self):
        """With an explicit recording_start, a user whose first packet
        arrives after that start gets silence padding for the offset."""
        explicit_start = 100.0
        sink = TimestampedWaveSink(recording_start=explicit_start)
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        # User's first packet arrives 3 seconds after recording_start.
        with patch("bot.timestamped_sink.time.monotonic", return_value=explicit_start + 3.0):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user)

        written = _get_user_audio(sink, user)
        # Expected silence: 3.0 s of PCM silence.
        expected_silence = int(3.0 * _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH)
        assert len(written) == expected_silence + len(pcm)
        assert written[:expected_silence] == b"\x00" * expected_silence
        assert written[expected_silence:] == pcm


# ---------------------------------------------------------------------------
# DTX gap padding
# ---------------------------------------------------------------------------


class TestDTXGapPadding:
    """Gaps between consecutive packets from the same user should be
    padded with silence based on the RTP timestamp delta."""

    def test_no_gap_no_padding(self):
        """Consecutive frames with no gap (delta = 1 frame) get no silence."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)
        # Second packet: RTP delta = exactly 1 frame (960 samples).
        sink.write(_make_voice_data(pcm, rtp_ts=_SAMPLES_PER_FRAME), user)

        written = _get_user_audio(sink, user)
        # Two frames of PCM, no silence.
        assert written == pcm + pcm

    def test_dtx_gap_padded_with_silence(self):
        """A gap of N frames should be padded with N-1 frames of silence.

        If the RTP timestamp jumps by 5 frames (5 * 960 = 4800), there
        are 4 missing frames, so 4 frames of silence should be inserted
        before the new PCM data. The RTP timestamp marks the start of
        each packet, so a 5-frame gap = 4 frames of silence + 1 frame
        of current audio.
        """
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)
        gap_frames = 5
        sink.write(
            _make_voice_data(pcm, rtp_ts=gap_frames * _SAMPLES_PER_FRAME),
            user,
        )

        written = _get_user_audio(sink, user)
        silence = b"\x00" * (4 * _BYTES_PER_FRAME)
        assert written == pcm + silence + pcm

    def test_large_gap_near_max_plausible(self):
        """A gap just under _MAX_PLAUSIBLE_GAP should still be padded."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)

        gap_ts = _MAX_PLAUSIBLE_GAP - _SAMPLES_PER_FRAME
        sink.write(_make_voice_data(pcm, rtp_ts=gap_ts), user)

        written = _get_user_audio(sink, user)
        gap_frames = gap_ts // _SAMPLES_PER_FRAME
        silence_frames = gap_frames - 1
        silence = b"\x00" * (silence_frames * _BYTES_PER_FRAME)
        assert written == pcm + silence + pcm

    def test_gap_exceeding_max_not_padded(self):
        """A gap larger than _MAX_PLAUSIBLE_GAP is treated as an SSRC
        reset and should NOT be padded."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)

        gap_ts = _MAX_PLAUSIBLE_GAP + _SAMPLES_PER_FRAME
        sink.write(_make_voice_data(pcm, rtp_ts=gap_ts), user)

        written = _get_user_audio(sink, user)
        # No silence padding — data is just the two PCM payloads.
        assert written == pcm + pcm

    def test_rtp_wraparound_handled(self):
        """RTP timestamp is 32-bit unsigned and wraps.  The subtraction
        should use unsigned arithmetic so a wraparound produces the
        correct positive gap."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        ts1 = 0xFFFFFFF0
        sink.write(_make_voice_data(pcm, rtp_ts=ts1), user)

        gap_frames = 3
        ts2 = (ts1 + gap_frames * _SAMPLES_PER_FRAME) & 0xFFFFFFFF
        sink.write(_make_voice_data(pcm, rtp_ts=ts2), user)

        written = _get_user_audio(sink, user)
        # Gap is 3 frames -> 2 frames of silence.
        silence = b"\x00" * (2 * _BYTES_PER_FRAME)
        assert written == pcm + silence + pcm

    def test_no_rtp_timestamp_no_dtx_padding(self):
        """When rtp_ts is None (raw bytes, not VoiceData), no DTX gap
        detection occurs — data is written as-is."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)
        sink.write(_make_voice_data(pcm, rtp_ts=None), user)

        written = _get_user_audio(sink, user)
        # No silence padded — just two PCM payloads.
        assert written == pcm + pcm

    def test_first_packet_rtp_ts_stored(self):
        """The first packet's RTP timestamp must be stored so the DTX
        check on the second packet finds a non-None last_ts.

        This is a regression test for a bug where _last_rtp_ts was
        only updated in the else branch (not the first-packet branch),
        causing DTX detection to never fire.
        """
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        sink.write(_make_voice_data(pcm, rtp_ts=42), user)

        assert user in sink._last_rtp_ts
        assert sink._last_rtp_ts[user] == 42


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: empty PCM, multiple users, Filters.container guard."""

    def test_empty_pcm_skipped(self):
        """Packets with empty PCM should be skipped entirely."""
        sink = _make_sink()
        user = MagicMock()
        vd = _make_voice_data(b"", rtp_ts=0)

        sink.write(vd, user)

        # No audio_data entry should have been created.
        assert user not in sink.audio_data

    def test_raw_bytes_not_voicedata(self):
        """When data is raw bytes (not VoiceData), pcm = data and
        rtp_ts = None.  No DTX detection occurs."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x02" * _BYTES_PER_FRAME

        # Pass raw bytes — isinstance(data, VoiceData) is False.
        sink.write(pcm, user)

        written = _get_user_audio(sink, user)
        assert written == pcm

    def test_multiple_users_independent_tracking(self):
        """Each user should have independent RTP timestamp tracking."""
        sink = _make_sink()
        user_a = MagicMock(name="a")
        user_b = MagicMock(name="b")
        pcm = b"\x01" * _BYTES_PER_FRAME

        t0 = 100.0
        # User A: two packets with no gap.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm, rtp_ts=0), user_a)
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm, rtp_ts=_SAMPLES_PER_FRAME), user_a)

        # User B: first packet at same time (no initial offset), then a gap.
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(_make_voice_data(pcm, rtp_ts=1000), user_b)
        with patch("bot.timestamped_sink.time.monotonic", return_value=t0):
            sink.write(
                _make_voice_data(pcm, rtp_ts=1000 + 4 * _SAMPLES_PER_FRAME),
                user_b,
            )

        written_a = _get_user_audio(sink, user_a)
        written_b = _get_user_audio(sink, user_b)

        # User A: no gaps, no initial padding -> 2 * _BYTES_PER_FRAME.
        assert len(written_a) == 2 * _BYTES_PER_FRAME

        # User B: 4-frame gap -> 3 frames silence (gap_frames - 1).
        silence = b"\x00" * (3 * _BYTES_PER_FRAME)
        assert written_b == pcm + silence + pcm

    def test_filtered_users_skips_write(self):
        """Filters.container: if filtered_users is set and user is NOT
        in it, write() should return None (no data written)."""
        sink = _make_sink()
        user = MagicMock()
        pcm = b"\x01" * _BYTES_PER_FRAME

        # Set up filtered_users so the Filters.container decorator
        # short-circuits. The user must NOT be in this set.
        other_user = MagicMock(name="other")
        sink.filtered_users = {other_user}

        sink.write(_make_voice_data(pcm, rtp_ts=0), user)

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
        pcm = b"\x01" * _BYTES_PER_FRAME
        num_threads = 10
        writes_per_thread = 50

        def writer():
            for _ in range(writes_per_thread):
                sink.write(_make_voice_data(pcm, rtp_ts=0), user)

        threads = [threading.Thread(target=writer) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        written = _get_user_audio(sink, user)
        # All threads wrote the same PCM with rtp_ts=0, so no DTX padding.
        # (rtp_ts=0 every time means gap=0, which is < _SAMPLES_PER_FRAME.)
        expected_len = num_threads * writes_per_thread * _BYTES_PER_FRAME
        assert len(written) == expected_len
