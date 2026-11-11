"""Audio splitting at silence boundaries.

Takes silence boundaries (from silence.py) and an audio file path,
and yields (start_time_seconds, numpy_array) chunks suitable for
transcription. Each chunk is capped at max_duration to stay under
Whisper's 30-second limit.

Chunks are yielded incrementally to keep memory bounded — the entire
audio file is never loaded into memory at once. Each chunk is resampled
to 16 kHz mono float32 for Whisper, regardless of the source sample rate.
"""
import logging
from pathlib import Path
from typing import Generator

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

# Whisper expects 16 kHz mono float32 input. All chunks are resampled
# to this rate regardless of the source file's sample rate.
TARGET_SAMPLE_RATE = 16000


def _resample_to_16k(audio: np.ndarray, source_rate: int) -> np.ndarray:
    """Resample audio to 16 kHz mono float32 using linear interpolation.

    Args:
        audio: 1D float32 numpy array (mono) at source_rate.
        source_rate: Sample rate of the input audio in Hz.

    Returns:
        1D float32 numpy array resampled to 16 kHz.
    """
    if source_rate == TARGET_SAMPLE_RATE:
        return audio

    # Compute target sample positions via linear interpolation.
    # This is adequate for speech; for music, a polyphase filter would
    # be better, but Whisper is trained on speech and the quality is fine.
    n_source = len(audio)
    n_target = int(n_source * TARGET_SAMPLE_RATE / source_rate)
    if n_target == 0:
        return np.array([], dtype=np.float32)

    source_indices = np.arange(n_source)
    target_indices = np.linspace(0, n_source - 1, n_target)
    resampled = np.interp(target_indices, source_indices, audio)
    return resampled.astype(np.float32)


def split_at_silence(
    audio_path: str,
    silence_boundaries: list[tuple[float, float]],
    max_duration: float = 25.0,
) -> Generator[tuple[float, np.ndarray], None, None]:
    """Split audio at silence midpoints, capping each chunk at max_duration.

    Yields chunks incrementally to keep memory bounded. Each chunk is
    resampled to 16 kHz mono float32 for Whisper, regardless of the
    source file's sample rate or channel count.

    Args:
        audio_path: Path to the audio file.
        silence_boundaries: List of (start, end) silence regions from
            find_silence_boundaries(). The audio is split at the midpoint
            of each silence region.
        max_duration: Maximum chunk duration in seconds. Chunks longer
            than this (because no silence was found within the window)
            are split at max_duration intervals.

    Yields:
        (start_time_seconds, audio_data) tuples. audio_data is a 1D
        float32 numpy array at 16 kHz mono. Chunks are in chronological
        order.
    """
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    with sf.SoundFile(audio_path) as f:
        source_rate = f.samplerate
        total_frames = len(f)
        total_duration = total_frames / source_rate
        channels = f.channels

        # Convert silence boundaries to split points (midpoints of silences)
        split_points: list[float] = []
        for start, end in silence_boundaries:
            midpoint = (start + end) / 2.0
            split_points.append(midpoint)

        # Build the final list of split points by walking through natural
        # splits and inserting forced splits wherever the gap exceeds
        # max_duration.
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

        # Yield chunks incrementally — read one chunk at a time from the
        # file instead of loading the entire audio into memory. This keeps
        # peak memory bounded to roughly one chunk's worth of audio.
        chunk_start = 0.0
        for split_point in all_splits:
            if split_point <= chunk_start:
                continue
            if split_point >= total_duration:
                break

            start_frame = int(chunk_start * source_rate)
            end_frame = int(split_point * source_rate)
            n_frames = end_frame - start_frame

            if n_frames > 0:
                f.seek(start_frame)
                chunk_data = f.read(frames=n_frames, dtype="float32")
                # Downmix stereo to mono if needed
                if channels > 1 and chunk_data.ndim > 1:
                    chunk_data = chunk_data.mean(axis=1)
                # Resample to 16 kHz for Whisper
                chunk_data = _resample_to_16k(chunk_data, source_rate)
                if len(chunk_data) > 0:
                    yield (chunk_start, chunk_data)

            chunk_start = split_point

        # Final chunk from last split to end
        if chunk_start < total_duration:
            start_frame = int(chunk_start * source_rate)
            n_frames = total_frames - start_frame
            if n_frames > 0:
                f.seek(start_frame)
                chunk_data = f.read(frames=n_frames, dtype="float32")
                # Downmix stereo to mono if needed
                if channels > 1 and chunk_data.ndim > 1:
                    chunk_data = chunk_data.mean(axis=1)
                # Resample to 16 kHz for Whisper
                chunk_data = _resample_to_16k(chunk_data, source_rate)
                if len(chunk_data) > 0:
                    yield (chunk_start, chunk_data)
