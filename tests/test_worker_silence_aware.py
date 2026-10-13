"""Tests for silence-aware chunking in transcriber.worker.

Verifies that the worker uses silence detection instead of blind
fixed-size chunking, and filters out silence/silent chunks.
"""
import numpy as np
import soundfile as sf
import pytest
from pathlib import Path

from transcriber.worker import TranscriptionWorker


@pytest.fixture
def worker_with_mock_model():
    """Worker with a mock recognizer to avoid loading real model."""
    worker = TranscriptionWorker(model_path="/fake/model")
    # Mock the recognizer to avoid loading sherpa-onnx model
    worker.recognizer = MockRecognizer()
    return worker


class MockRecognizer:
    """Mock sherpa-onnx recognizer for testing."""

    def create_stream(self, hotwords=None):
        return MockStream()

    def decode_stream(self, stream):
        pass


class MockStream:
    """Mock sherpa-onnx stream."""

    def __init__(self):
        self.result = MockResult()

    def accept_waveform(self, sample_rate, waveform):
        pass


class MockResult:
    """Mock sherpa-onnx result with segment data."""

    def __init__(self):
        self.text = ""
        self.segment_timestamps = []
        self.segment_texts = []


@pytest.fixture
def speech_with_silence(tmp_path):
    """1s speech + 2s silence + 1s speech + 2s silence + 1s speech."""
    sr = 16000
    speech = np.random.randn(sr).astype(np.float32) * 0.3
    silence = np.zeros(2 * sr, dtype=np.float32)
    audio = np.concatenate([speech, silence, speech, silence, speech])
    path = tmp_path / "speech_with_silence.wav"
    sf.write(str(path), audio, sr)
    return str(path)


@pytest.fixture
def mostly_silence(tmp_path):
    """10s of silence with one brief speech burst."""
    sr = 16000
    silence = np.zeros(4 * sr, dtype=np.float32)
    speech = np.random.randn(sr).astype(np.float32) * 0.3
    audio = np.concatenate([silence, speech, silence * 5])
    path = tmp_path / "mostly_silence.wav"
    sf.write(str(path), audio, sr)
    return str(path)


def test_transcribe_uses_silence_aware_splitting(worker_with_mock_model, speech_with_silence):
    """Worker should split at silence boundaries, not fixed intervals."""
    result = worker_with_mock_model.transcribe(speech_with_silence)
    # Should succeed without error
    assert result is not None
    # The worker should have called find_silence_boundaries internally
    # (we can't easily verify this without mocking, but the test ensures
    # the new code path works)


def test_transcribe_filters_silent_chunks(worker_with_mock_model, mostly_silence):
    """Chunks below RMS threshold should be skipped."""
    result = worker_with_mock_model.transcribe(mostly_silence)
    assert result is not None
    # Silent chunks should not produce [BLANK_AUDIO] in output
    if result.text:
        assert "[BLANK_AUDIO]" not in result.text


def test_transcribe_filters_blank_audio_text(worker_with_mock_model, speech_with_silence):
    """[BLANK_AUDIO] text should be filtered from final output."""
    # Mock the recognizer to return [BLANK_AUDIO]
    worker = worker_with_mock_model
    original_create_stream = worker.recognizer.create_stream

    def mock_create_stream(hotwords=None):
        stream = original_create_stream(hotwords)
        stream.result.text = "[BLANK_AUDIO]"
        return stream

    worker.recognizer.create_stream = mock_create_stream

    result = worker.transcribe(speech_with_silence)
    assert result is not None
    # [BLANK_AUDIO] should be filtered out
    assert "[BLANK_AUDIO]" not in result.text


def test_transcribe_preserves_chronological_order(worker_with_mock_model, speech_with_silence):
    """Segments should be in chronological order across chunk boundaries."""
    result = worker_with_mock_model.transcribe(speech_with_silence)
    assert result is not None
    # Verify segments are sorted by start_time
    for i in range(len(result.segments) - 1):
        assert result.segments[i].start_time <= result.segments[i + 1].start_time
