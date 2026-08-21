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
        # _last_rtp_ts: RTP timestamp of the previous packet (for DTX gap detection).
        self._last_rtp_ts: dict = {}

        # Maps user -> temporary file path for the raw PCM data written
        # during this recording session. Files are renamed to their final
        # paths by the after-callback once recording stops.  Using
        # disk-backed files avoids buffering all audio in memory.
        self._temp_paths: dict = {}

        # Serialises access to the per-user tracking state.  py-cord's
        # router already serialises sink.write calls via its own RLock,
        # but this guard makes the subclass self-contained.
        self._lock = threading.Lock()

    @Filters.container
    def write(self, data, user):
        """Write audio data, inserting silence padding for temporal alignment.

        On each user's first packet, pads the beginning of their file
        with silence for the elapsed time since recording start. On
        subsequent packets, pads silence for any DTX gap detected via
        the RTP timestamp delta.
        """
        from discord.voice.packets import VoiceData

        if isinstance(data, VoiceData):
            pcm = self._pcm_to_mono(data.pcm)
            rtp_ts = getattr(data.packet, "timestamp", None)
        else:
            pcm = data
            rtp_ts = None

        if not pcm:
            return

        now = time.monotonic()

        with self._lock:
            # Set the shared zero point on the very first packet from any
            # user, but only if an explicit recording_start wasn't already
            # provided in __init__.  When it was, every user's WAV is
            # aligned to the session-level reference from the start.
            if self._recording_start is None:
                self._recording_start = now

            # Create the AudioData entry on first packet for this user.
            # Use a disk-backed temp file instead of an in-memory BytesIO
            # to avoid buffering hours of PCM data in RAM.
            if user not in self.audio_data:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                self._temp_paths[user] = tmp.name
                self.audio_data[user] = AudioData(file=tmp)

                # Pad initial silence so this user's file starts at the
                # recording's zero point, not at their first speech.
                # Without this, a user who joins late or waits before
                # speaking would have their timestamps offset.
                initial_offset = now - self._recording_start
                if initial_offset > 0.005:  # ignore sub-5ms jitter
                    silence_bytes = int(
                        initial_offset
                        * _SAMPLE_RATE
                        * _CHANNELS
                        * _SAMPLE_WIDTH
                    )
                    self.audio_data[user].write(b"\x00" * silence_bytes)
            else:
                # DTX gap detection: if the RTP timestamp jumped by
                # more than one frame, Discord did not send packets
                # during that period (the user was silent). Pad with
                # silence to keep this user's timeline aligned with
                # real time. Without this, a user who listens a lot
                # would have their timestamps compressed relative to
                # a user who talks continuously.
                if rtp_ts is not None:
                    last_ts = self._last_rtp_ts.get(user)
                    if last_ts is not None:
                        # Unsigned 32-bit subtraction handles wraparound.
                        gap = (rtp_ts - last_ts) & 0xFFFFFFFF
                        if _SAMPLES_PER_FRAME < gap < _MAX_PLAUSIBLE_GAP:
                            gap_frames = gap // _SAMPLES_PER_FRAME
                            # Subtract 1 frame: the RTP timestamp marks
                            # the start of each packet, so a gap of N
                            # frames means N-1 frames of silence between
                            # the end of the previous packet's PCM and
                            # the start of the current one.
                            silence_frames = gap_frames - 1
                            silence_bytes = silence_frames * _BYTES_PER_FRAME
                            if silence_bytes > 0:
                                self.audio_data[user].write(b"\x00" * silence_bytes)

            # Always record the last RTP timestamp for this user,
            # regardless of whether this was their first or subsequent
            # packet.  Without this on the first packet, the next
            # packet's DTX gap check finds last_ts=None and skips
            # padding — defeating the entire gap detection logic.
            if rtp_ts is not None:
                self._last_rtp_ts[user] = rtp_ts

            self.audio_data[user].write(pcm)

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