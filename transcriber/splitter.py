"""Audio splitting at silence boundaries.

Takes silence boundaries (from silence.py) and an audio file path,
and yields (start_time_seconds, numpy_array) chunks suitable for
transcription. Each chunk is capped at max_duration to stay under
Whisper's 30-second limit.
"""
import logging
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


def split_at_silence(
    audio_path: str,
    silence_boundaries: list[tuple[float, float]],
    max_duration: float = 25.0,
) -> list[tuple[float, np.ndarray]]:
    """Split audio at silence midpoints, capping each chunk at max_duration.

    Args:
        audio_path: Path to the audio file.
        silence_boundaries: List of (start, end) silence regions from
            find_silence_boundaries(). The audio is split at the midpoint
            of each silence region.
        max_duration: Maximum chunk duration in seconds. Chunks longer
            than this (because no silence was found within the window)
            are split at max_duration intervals.

    Returns:
        List of (start_time_seconds, audio_data) tuples. audio_data is
        a 1D float32 numpy array. Chunks are in chronological order.
    """
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    with sf.SoundFile(audio_path) as f:
        sample_rate = f.samplerate
        total_frames = len(f)
        total_duration = total_frames / sample_rate

        # Read entire file (already memory-safe: per-speaker WAVs are
        # typically < 100 MB even for 3-hour sessions)
        f.seek(0)
        audio = f.read(dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

    # Convert silence boundaries to split points (midpoints of silences)
    split_points: list[float] = []
    for start, end in silence_boundaries:
        midpoint = (start + end) / 2.0
        split_points.append(midpoint)

    # Build the final list of split points by walking through natural splits
    # and inserting forced splits wherever the gap exceeds max_duration.
    all_splits: list[float] = []
    last_split = 0.0
    for sp in sorted(split_points):
        if sp <= last_split:
            continue
        # Insert forced splits to fill any gap > max_duration
        while sp - last_split > max_duration:
            last_split += max_duration
            all_splits.append(last_split)
        all_splits.append(sp)
        last_split = sp

    # Fill any remaining gap after the last natural split
    while total_duration - last_split > max_duration:
        last_split += max_duration
        all_splits.append(last_split)

    # Build chunks from split points
    chunks: list[tuple[float, np.ndarray]] = []
    chunk_start = 0.0
    for split_point in all_splits:
        if split_point <= chunk_start:
            continue
        if split_point >= total_duration:
            break
        start_frame = int(chunk_start * sample_rate)
        end_frame = int(split_point * sample_rate)
        chunk_data = audio[start_frame:end_frame]
        if len(chunk_data) > 0:
            chunks.append((chunk_start, chunk_data))
        chunk_start = split_point

    # Final chunk from last split to end
    if chunk_start < total_duration:
        start_frame = int(chunk_start * sample_rate)
        chunk_data = audio[start_frame:]
        if len(chunk_data) > 0:
            chunks.append((chunk_start, chunk_data))

    return chunks
