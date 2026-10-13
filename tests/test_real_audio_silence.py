"""Integration test against real audio files from 2026-10-07 session.

Verifies that silence detection and splitting work correctly on the actual
recordings that exhibited the blind-chunking bug (out-of-order lines,
[BLANK_AUDIO] spam).
"""
import os
import pytest
import numpy as np
import soundfile as sf

from transcriber.silence import find_silence_boundaries
from transcriber.splitter import split_at_silence

# Path to last week's recordings (mounted into the workspace)
REAL_AUDIO_DIR = "/workspace/2026-10-07_18-19-13"

# All per-speaker WAVs from the session
REAL_AUDIO_FILES = [
    "bella_russo (Bella_Russo).wav",
    "danj2k (Danj).wav",
    "kodabean (Kodabean).wav",
    "kravidad (Kravidad).wav",       # the file with all the [BLANK_AUDIO] spam
    "llenikcud (Duckinell_DM).wav",
]


def _skip_if_no_audio():
    if not os.path.isdir(REAL_AUDIO_DIR):
        pytest.skip(f"Real audio directory not found: {REAL_AUDIO_DIR}")


@pytest.mark.parametrize("filename", REAL_AUDIO_FILES)
def test_silence_detection_on_real_audio(filename):
    """Silence detection runs without error and returns sorted boundaries."""
    _skip_if_no_audio()
    path = os.path.join(REAL_AUDIO_DIR, filename)
    if not os.path.exists(path):
        pytest.skip(f"File not found: {path}")

    boundaries = find_silence_boundaries(path)

    # Must return a list of tuples
    assert isinstance(boundaries, list)
    for b in boundaries:
        assert len(b) == 2
        start, end = b
        assert isinstance(start, float)
        assert isinstance(end, float)
        assert end > start, f"Silence end ({end}) must be > start ({start})"

    # Must be sorted by start time
    starts = [b[0] for b in boundaries]
    assert starts == sorted(starts), "Silence boundaries must be sorted by start time"

    # Each silence must have minimum duration (0.8s default)
    for start, end in boundaries:
        assert end - start >= 0.8, f"Silence too short: {end - start:.2f}s"


@pytest.mark.parametrize("filename", REAL_AUDIO_FILES)
def test_splitter_produces_ordered_chunks(filename):
    """Splitter produces chunks in chronological order with no gaps/overlaps."""
    _skip_if_no_audio()
    path = os.path.join(REAL_AUDIO_DIR, filename)
    if not os.path.exists(path):
        pytest.skip(f"File not found: {path}")

    boundaries = find_silence_boundaries(path)
    chunks = split_at_silence(path, boundaries, max_duration=25.0)

    assert len(chunks) > 0, "Splitter must produce at least one chunk"

    # Get total file duration and sample rate for comparison
    with sf.SoundFile(path) as f:
        total_duration = len(f) / f.samplerate
        sample_rate = f.samplerate

    # Verify chronological order
    for i in range(len(chunks) - 1):
        t_curr, _ = chunks[i]
        t_next, _ = chunks[i + 1]
        assert t_curr < t_next, (
            f"Chunks out of order: chunk {i} at {t_curr:.2f}s >= "
            f"chunk {i+1} at {t_next:.2f}s"
        )

    # Verify no chunk exceeds max_duration (with small tolerance for rounding)
    for i, (start, audio) in enumerate(chunks):
        duration = len(audio) / sample_rate
        assert duration <= 25.1, (
            f"Chunk {i} exceeds 25s: {duration:.2f}s (start={start:.2f}s)"
        )

    # Verify total coverage: sum of chunk durations should be close to
    # total file duration (allowing for small gaps at split points)
    chunk_total = sum(len(audio) / sample_rate for _, audio in chunks)
    assert abs(chunk_total - total_duration) < 2.0, (
        f"Chunk coverage mismatch: chunks={chunk_total:.1f}s, "
        f"file={total_duration:.1f}s"
    )

    # First chunk starts at or near 0
    assert chunks[0][0] < 1.0, f"First chunk starts at {chunks[0][0]:.2f}s, expected ~0"


@pytest.mark.parametrize("filename", REAL_AUDIO_FILES)
def test_no_blind_28s_intervals(filename):
    """Verify chunks are NOT at fixed 28s intervals (the old bug)."""
    _skip_if_no_audio()
    path = os.path.join(REAL_AUDIO_DIR, filename)
    if not os.path.exists(path):
        pytest.skip(f"File not found: {path}")

    boundaries = find_silence_boundaries(path)
    chunks = split_at_silence(path, boundaries, max_duration=25.0)

    if len(chunks) < 3:
        pytest.skip("Too few chunks to check for fixed intervals")

    # Compute inter-chunk gaps
    starts = [t for t, _ in chunks]
    gaps = [starts[i+1] - starts[i] for i in range(len(starts) - 1)]

    # Count how many gaps are within 0.5s of 28s (the old blind interval)
    near_28s = sum(1 for g in gaps if abs(g - 28.0) < 0.5)

    # If MORE than half the gaps are at 28s, we're still doing blind chunking
    assert near_28s < len(gaps) / 2, (
        f"Too many gaps at 28s ({near_28s}/{len(gaps)}): "
        f"silence-aware splitting may not be working. "
        f"Gaps: {[f'{g:.1f}' for g in gaps[:10]]}..."
    )


def test_kravidad_has_significant_silence():
    """kravidad's file had massive [BLANK_AUDIO] spam — verify it has
    long silent stretches that the new code should skip."""
    _skip_if_no_audio()
    path = os.path.join(REAL_AUDIO_DIR, "kravidad (Kravidad).wav")
    if not os.path.exists(path):
        pytest.skip("kravidad file not found")

    boundaries = find_silence_boundaries(path)

    # This file is 380MB / ~16kHz / stereo-ish = very long.
    # It should have substantial silence (the [BLANK_AUDIO] regions).
    total_silence = sum(end - start for start, end in boundaries)
    with sf.SoundFile(path) as f:
        total_duration = len(f) / f.samplerate

    silence_pct = (total_silence / total_duration) * 100

    # We expect at least 20% silence given the transcript evidence
    assert silence_pct > 20, (
        f"Expected >20% silence in kravidad file, got {silence_pct:.1f}% "
        f"({total_silence:.0f}s silence / {total_duration:.0f}s total)"
    )

    # And the splitter should produce chunks that respect silence boundaries
    # (not necessarily fewer chunks — the goal is better split points, not fewer chunks)
    chunks = split_at_silence(path, boundaries, max_duration=25.0)
    
    # Verify that most chunks start near a silence boundary (within 2 seconds)
    # This ensures we're splitting at natural pauses, not mid-speech
    chunks_near_silence = 0
    for chunk_start, _ in chunks:
        # Check if this chunk starts within 2 seconds of any silence boundary
        near_silence = False
        for sil_start, sil_end in boundaries:
            sil_midpoint = (sil_start + sil_end) / 2.0
            if abs(chunk_start - sil_midpoint) < 2.0:
                near_silence = True
                break
            # Also check if chunk starts within the silence region itself
            if sil_start <= chunk_start <= sil_end:
                near_silence = True
                break
        if near_silence or chunk_start < 1.0:  # First chunk starts at 0
            chunks_near_silence += 1
    
    # At least 80% of chunks should start near silence (or be the first chunk)
    pct_near_silence = (chunks_near_silence / len(chunks)) * 100
    assert pct_near_silence > 80, (
        f"Only {pct_near_silence:.1f}% of chunks start near silence boundaries, "
        f"expected >80%"
    )
