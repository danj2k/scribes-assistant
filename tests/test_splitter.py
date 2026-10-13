"""Tests for transcriber.splitter — silence-aware audio splitting."""

import numpy as np
import soundfile as sf
import pytest

from transcriber.splitter import split_at_silence


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
def long_continuous(tmp_path):
    """30s of continuous noise (no silence)."""
    sr = 16000
    audio = np.random.randn(30 * sr).astype(np.float32) * 0.3
    path = tmp_path / "long_continuous.wav"
    sf.write(str(path), audio, sr)
    return str(path)


@pytest.fixture
def short_continuous(tmp_path):
    """5s of continuous noise."""
    sr = 16000
    audio = np.random.randn(5 * sr).astype(np.float32) * 0.3
    path = tmp_path / "short_continuous.wav"
    sf.write(str(path), audio, sr)
    return str(path)


def test_split_at_silence_basic(speech_with_gaps):
    """Splits audio at silence midpoints, respecting max duration."""
    boundaries = [(1.0, 2.0), (3.0, 4.0)]  # two silence regions
    chunks = split_at_silence(speech_with_gaps, boundaries, max_duration=25.0)

    # Should produce 3 chunks: 0-1.5s, 1.5-3.5s, 3.5-5.0s
    # (split at midpoint of each silence)
    assert len(chunks) == 3
    assert chunks[0][0] == 0.0  # first chunk starts at 0
    assert abs(chunks[1][0] - 1.5) < 0.1  # midpoint of first silence
    assert abs(chunks[2][0] - 3.5) < 0.1  # midpoint of second silence

    # Each chunk has audio data
    for start, audio_data in chunks:
        assert isinstance(audio_data, np.ndarray)
        assert audio_data.dtype == np.float32
        assert len(audio_data) > 0


def test_split_at_silence_respects_max_duration(long_continuous):
    """Long segments without silence are further split at max_duration."""
    chunks = split_at_silence(long_continuous, [], max_duration=25.0)
    # Should split into 2 chunks: 0-25s and 25-30s
    assert len(chunks) == 2
    assert chunks[0][0] == 0.0
    assert abs(chunks[1][0] - 25.0) < 0.1


def test_split_at_silence_no_boundaries_short_file(short_continuous):
    """File shorter than max_duration with no silence returns single chunk."""
    chunks = split_at_silence(short_continuous, [], max_duration=25.0)
    assert len(chunks) == 1
    assert chunks[0][0] == 0.0


def test_split_at_silence_file_not_found():
    """Raises FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        split_at_silence("/nonexistent/audio.wav", [])


def test_split_at_silence_empty_boundaries(long_continuous):
    """Empty boundaries list still respects max_duration."""
    chunks = split_at_silence(long_continuous, [], max_duration=10.0)
    # 30s / 10s = 3 chunks
    assert len(chunks) == 3
    assert chunks[0][0] == 0.0
    assert abs(chunks[1][0] - 10.0) < 0.1
    assert abs(chunks[2][0] - 20.0) < 0.1
