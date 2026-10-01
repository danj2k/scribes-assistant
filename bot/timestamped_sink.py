"""WaveSink subclass that preserves temporal alignment across speakers.

Discord uses Discontinuous Transmission (DTX): when a user is silent,
no RTP packets are sent for them. The base ``WaveSink`` simply appends
PCM data as it arrives, so each user's WAV file:

  1. Starts at their first speech, not at recording start.
  2. Has all silence gaps removed (DTX pauses are omitted).

This means timestamps from different speakers' WAV files are not
comparable — speaker A's 00:30 and speaker B's 00:30 may correspond
to very different absolute times. When the transcriber merges segments
by timestamp, dialogue becomes nonsensical: questions appear after
answers, interjections are displaced, etc.

This sink fixes both problems:

  - **Initial offset** — pads each user's file with silence for the
    delay between recording start and their first packet, so every
    user's file starts at the same zero point. Uses wall-clock time
    (``time.monotonic``) because RTP timestamps are per-SSRC and not
    comparable across users.

  - **DTX gaps** — pads silence for gaps between consecutive packets
    from the same user, so pauses are preserved. Uses RTP timestamp
    deltas, which are precise within a single SSRC (48 kHz clock, one
    frame = 960 samples = 20 ms).
"""

from __future__ import annotations

import array
import io
import logging
import os
import tempfile
import threading
import time
import wave

from discord.sinks import WaveSink
from discord.sinks.core import AudioData, Filters

logger = logging.getLogger(__name__)

# Opus frame parameters — must match discord.opus._OpusStruct.
# These are fixed by the Opus codec standard and Discord's voice
# transport; they will not change at runtime.
_SAMPLE_RATE: int = 48_000
_CHANNELS: int = 1  # Mono — Discord per-user streams are mono
_SAMPLE_WIDTH: int = 2  # bytes per sample (16-bit signed)
_SAMPLES_PER_FRAME: int = 960  # 48 000 Hz * 20 ms
_BYTES_PER_FRAME: int = _SAMPLES_PER_FRAME * _CHANNELS * _SAMPLE_WIDTH  # 1 920

# Maximum plausible RTP timestamp gap (60 s of frames at 48 kHz).
# Gaps larger than this almost certainly indicate an SSRC reset (the
# user disconnected and reconnected with a new timestamp base) rather
# than a genuine silence period. We skip silence padding for those.
_MAX_PLAUSIBLE_GAP: int = 60 * _SAMPLE_RATE


class TimestampedWaveSink(WaveSink):
    """WaveSink that pads silence to keep all speakers on a shared timeline.

    Produces per-user WAV files where timestamp 0 is the recording start
    for every user, and real-time pauses are preserved. This makes
    Whisper segment timestamps comparable across speakers, so the
    merged transcript has correct dialogue ordering.
    """

    def __init__(self, *, filters=None, recording_start: float | None = None):
        super().__init__(filters=filters)

        # Shared zero point for all speakers' timelines.
        # When recording_start is provided (from the /start command's
        # monotonic clock), every user's WAV file is aligned to the
        # same reference — not to whichever user happened to send the
        # first audio packet.  This eliminates timeline shifts caused
        # by per-user DAVE MLS handshake timing differences.
        if recording_start is not None:
            self._recording_start = recording_start
        else:
            self._recording_start = None

        # Per-user state, keyed by the user/member/object passed to write().
        # Wall-clock based padding: track samples written vs time since recording start.
        # This replaces the old RTP-delta approach which broke when users reconnected
        # with new SSRCs (RTP timestamp base would jump, making deltas meaningless).
        self._samples_written: dict = {}  # user -> total samples written to file
        self._first_packet_time: dict = {}  # user -> monotonic time of first packet

        # Maps user -> temporary file path for the raw PCM data written
        # during this recording session. Files are renamed to their final
        # paths by the after-callback once recording stops.  Using
        # disk-backed files avoids buffering all audio in memory.
        self._temp_paths: dict = {}

        # Serialises access to the per-user tracking state.  py-cord's
        # router already serialises sink.write calls via its own RLock,
        # but this guard makes the subclass self-contained.
        self._lock = threading.Lock()

        # Per-(user, SSRC) diagnostic counters for tracking packet flow
        # and detecting suspicious PCM patterns (e.g., decoder garbage).
        self._diagnostic_packet_count: dict = {}
        self._diagnostic_zero_pcm_count: dict = {}
        self._diagnostic_high_variance_count: dict = {}

    @Filters.container
    def write(self, data, user):
        """Write audio data, inserting silence padding for temporal alignment.

        Uses wall-clock arrival time to determine padding, not RTP timestamps.
        RTP timestamps are unreliable: they reset when a user reconnects with a
        new SSRC, and the old code keyed _last_rtp_ts by user (not SSRC), so a
        reconnect caused a meaningless delta that either skipped padding or
        inserted huge silence blocks.

        Wall-clock approach: on each packet, compute how many samples *should*
        have been written by now (based on elapsed time since recording start),
        compare to how many *were* written, and pad the difference with silence.
        This correctly handles reconnects, DTX gaps, and late joiners.
        """
        from discord.voice.packets import VoiceData

        if isinstance(data, VoiceData):
            pcm = self._pcm_to_mono(data.pcm)
        else:
            pcm = data

        if not pcm:
            return

        now = time.monotonic()
        samples_in_packet = len(pcm) // _SAMPLE_WIDTH

        with self._lock:
            # Set the shared zero point on the very first packet from any
            # user, but only if an explicit recording_start wasn't already
            # provided in __init__.  When it was, every user's WAV is
            # aligned to the session-level reference from the start.
            if self._recording_start is None:
                self._recording_start = now

            # Create the AudioData entry on first packet for this user.
            if user not in self.audio_data:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                self._temp_paths[user] = tmp.name
                self.audio_data[user] = AudioData(file=tmp)
                self._samples_written[user] = 0
                self._first_packet_time[user] = now

                # Pad initial silence so this user's file starts at the
                # recording's zero point, not at their first speech.
                initial_offset = now - self._recording_start
                if initial_offset > 0.005:  # ignore sub-5ms jitter
                    silence_samples = int(initial_offset * _SAMPLE_RATE)
                    silence_bytes = silence_samples * _SAMPLE_WIDTH
                    self.audio_data[user].write(b"\x00" * silence_bytes)
                    self._samples_written[user] += silence_samples
            else:
                # Wall-clock padding: compare expected samples vs actual.
                # This handles DTX gaps, reconnects, and late joiners uniformly.
                elapsed = now - self._recording_start
                expected_samples = int(elapsed * _SAMPLE_RATE)
                actual_samples = self._samples_written.get(user, 0)
                deficit = expected_samples - actual_samples

                # Only pad if we're behind by more than one frame (20ms).
                # Small deficits are normal jitter; padding them would
                # insert audible clicks.  Cap at 5 seconds to avoid inserting
                # huge silence blocks if the clock jumps or the system hiccups.
                if _SAMPLES_PER_FRAME < deficit < _SAMPLE_RATE * 5:
                    silence_bytes = deficit * _SAMPLE_WIDTH
                    self.audio_data[user].write(b"\x00" * silence_bytes)
                    self._samples_written[user] += deficit

            self.audio_data[user].write(pcm)
            self._samples_written[user] = self._samples_written.get(user, 0) + samples_in_packet
            self._log_receive_diagnostics(data, user, pcm)

    def _pcm_to_mono(self, pcm: bytes) -> bytes:
        """Downmix stereo PCM to mono.

        py-cord's ``opus.Decoder`` uses ``CHANNELS = 2`` (stereo).  For
        a mono Discord stream, the decoded output contains R[i] = L[i+1]
        (a 1-sample offset), which corrupts the waveform and causes
        Whisper to hallucinate.  Taking every other sample (the left
        channel) recovers the clean mono signal.

        The silence padding elsewhere in this module uses
        ``_BYTES_PER_FRAME = 1920`` (mono).  Downmixing here keeps the
        PCM frame size consistent with the silence padding.
        """
        if not pcm:
            return pcm
        arr = array.array("h", pcm)
        return array.array("h", arr[0::2]).tobytes()

    def _log_receive_diagnostics(self, data, user, pcm: bytes) -> None:
        """Record/log safe receive metadata for diagnosing per-user corruption.

        The py-cord DAVE/Opus decoder can fail before this sink is called, so
        bot/main.py also instruments PacketDecoder. This sink-side telemetry
        tells us what actually reached the recorder and whether a particular
        SSRC/user is producing suspicious PCM.
        """
        packet = getattr(data, "packet", None)
        if packet is None:
            return

        ssrc = getattr(packet, "ssrc", None)
        sequence = getattr(packet, "sequence", None)
        user_id = getattr(user, "id", None)
        key = (user_id, ssrc)
        now = time.monotonic()

        # Track packet count per (user, SSRC)
        count = self._diagnostic_packet_count.get(key, 0) + 1
        self._diagnostic_packet_count[key] = count

        # Detect suspicious PCM patterns
        pcm_len = len(pcm)
        if pcm_len == 0:
            zero_count = self._diagnostic_zero_pcm_count.get(key, 0) + 1
            self._diagnostic_zero_pcm_count[key] = zero_count
            if zero_count == 1 or zero_count % 100 == 0:
                logger.warning(
                    "RX zero PCM: user_id=%s ssrc=%s seq=%s count=%d",
                    user_id, ssrc, sequence, zero_count,
                )
            return

        # Check for all-zero PCM (silence or decoder failure)
        if pcm == b"\x00" * pcm_len:
            zero_count = self._diagnostic_zero_pcm_count.get(key, 0) + 1
            self._diagnostic_zero_pcm_count[key] = zero_count
            if zero_count == 1 or zero_count % 100 == 0:
                logger.warning(
                    "RX all-zero PCM: user_id=%s ssrc=%s seq=%s bytes=%d count=%d",
                    user_id, ssrc, sequence, pcm_len, zero_count,
                )
            return

        # Check for high-variance noise (random decoder garbage)
        # Sample a subset to avoid expensive full-array computation
        if pcm_len >= 1000:
            sample_size = min(1000, pcm_len)
            sample = array.array("h", pcm[:sample_size * 2])
            # Compute variance proxy: sum of absolute differences
            variance_proxy = sum(abs(sample[i] - sample[i-1]) for i in range(1, len(sample)))
            # High variance proxy suggests random noise (typical of decoder failure)
            # Normal speech has variance_proxy < 50000 for 1000 samples
            if variance_proxy > 100000:
                high_var_count = self._diagnostic_high_variance_count.get(key, 0) + 1
                self._diagnostic_high_variance_count[key] = high_var_count
                if high_var_count == 1 or high_var_count % 50 == 0:
                    logger.warning(
                        "RX high-variance PCM (possible decoder garbage): "
                        "user_id=%s ssrc=%s seq=%s variance_proxy=%d count=%d",
                        user_id, ssrc, sequence, variance_proxy, high_var_count,
                    )

        # Log first packet and periodic summaries
        if count == 1:
            logger.info(
                "RX first packet: user_id=%s ssrc=%s seq=%s pcm_bytes=%d",
                user_id, ssrc, sequence, pcm_len,
            )
        elif count % 500 == 0:
            logger.debug(
                "RX packet summary: user_id=%s ssrc=%s count=%d zero_count=%d high_var_count=%d",
                user_id, ssrc, count,
                self._diagnostic_zero_pcm_count.get(key, 0),
                self._diagnostic_high_variance_count.get(key, 0),
            )

    def format_audio(self, audio_data):
        """Override to write a mono WAV header.

        Discord voice per-user streams are mono.  The base
        ``WaveSink.format_audio()`` writes header channels from
        ``self.vc.decoder.CHANNELS``, which is ``2``, even though the
        audio data is already downmixed to mono by ``_pcm_to_mono()``.
        This override writes a correct mono WAV header (1 channel,
        16-bit, 48 kHz).
        """
        audio_data.file.seek(0)
        data = audio_data.file.read()
        result = io.BytesIO()
        with wave.open(result, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)  # 16-bit PCM
            f.setframerate(_SAMPLE_RATE)
            f.writeframes(data)
        audio_data.file = result