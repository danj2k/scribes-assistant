"""Tests for transcriber.silence — silence boundary detection."""

import pytest
import numpy as np
import soundfile as sf
from pathlib import Path

from transcriber.silence import find_silence_boundaries


@pytest.fixture
def speech_with_gaps(tmp_path):
    """1s speech + 1s silence + 1s speech + 1s silence + 1s speech = 5s."""
    sr = 16000
    speech = np.random.randn(sr).astype(np.float32) * 0.3
    silence = np.zeros(sr, dtype=np.float32)
    audio = np.concatenate([speech, silence, speech, silence, speech])
    path = tmp_path / "speech_with_gaps.wav"
    sf.write(str(path), audio, sr)
    return str(path)


@pytest.fixture
def continuous_speech(tmp_path):
    """5s of noise (no silence)."""
    sr = 16000
    audio = np.random.randn(5 * sr).astype(np.float32) * 0.3
    path = tmp_path / "continuous_speech.wav"
    sf.write(str(path), audio, sr)
    return str(path)


def test_find_silence_boundaries_returns_sorted_list(speech_with_gaps):
    """Returns list of (start, end) tuples sorted by start time."""
    boundaries = find_silence_boundaries(speech_with_gaps)
    assert isinstance(boundaries, list)
    for start, end in boundaries:
        assert end > start
    # Verify sorted order
    for i in range(len(boundaries) - 1):
        assert boundaries[i][0] <= boundaries[i + 1][0]


def test_find_silence_boundaries_respects_threshold(speech_with_gaps):
    """Only reports silences longer than the minimum duration."""
    boundaries = find_silence_boundaries(
        speech_with_gaps,
        min_silence_duration=0.5,
    )
    for start, end in boundaries:
        assert (end - start) >= 0.5


def test_find_silence_boundaries_empty_for_continuous_speech(continuous_speech):
    """Returns empty list when no silence detected."""
    boundaries = find_silence_boundaries(continuous_speech)
    assert boundaries == []


def test_find_silence_boundaries_file_not_found():
    """Raises FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        find_silence_boundaries("/nonexistent/audio.wav")
