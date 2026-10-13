"""Silence detection for audio files using ffmpeg's silencedetect filter.

Provides boundary detection so the transcriber can split audio at natural
pause points instead of blind fixed-size chunks.
"""
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def find_silence_boundaries(
    audio_path: str,
    min_silence_duration: float = 0.8,
    silence_threshold_db: float = -40.0,
) -> list[tuple[float, float]]:
    """Find silence boundaries in an audio file using ffmpeg silencedetect.

    Args:
        audio_path: Path to the audio file (WAV, MP3, etc.).
        min_silence_duration: Minimum silence duration in seconds to report.
            Silences shorter than this are ignored (they're just pauses
            within speech, not segment boundaries).
        silence_threshold_db: Volume threshold in dB below which audio is
            considered silent. -40 dB is a reasonable default for speech.

    Returns:
        List of (start_seconds, end_seconds) tuples for each silence region,
        sorted by start time. Empty list if no silence detected.
    """
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}",
        "-f", "null", "-"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("silencedetect timed out for %s, returning empty", audio_path)
        return []
    except FileNotFoundError:
        logger.error("ffmpeg not found — silence detection unavailable")
        return []

    # silencedetect outputs to stderr in format:
    # [silencedetect @ ...] silence_start: 1.234
    # [silencedetect @ ...] silence_end: 2.345 | silence_duration: 1.111
    boundaries: list[tuple[float, float]] = []
    current_start: float | None = None

    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[-1].strip())
            except (ValueError, IndexError):
                continue
        elif "silence_end:" in line and current_start is not None:
            try:
                end = float(line.split("silence_end:")[-1].split("|")[0].strip())
                boundaries.append((current_start, end))
                current_start = None
            except (ValueError, IndexError):
                current_start = None
                continue

    # If file ends mid-silence, close the last boundary at file duration
    if current_start is not None:
        duration = _get_audio_duration(audio_path)
        if duration and duration > current_start:
            boundaries.append((current_start, duration))

    boundaries.sort(key=lambda b: b[0])
    return boundaries


def _get_audio_duration(audio_path: str) -> float | None:
    """Get audio duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json",
        audio_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return None
