"""Tests for transcriber.worker — TranscriptionWorker with mocked sherpa-onnx."""

import pytest
from unittest.mock import patch, MagicMock


class TestWorkerInit:
    """Tests for TranscriptionWorker initialization."""

    @patch("transcriber.worker.sherpa_onnx")
    def test_worker_creation(self, mock_sherpa):
        """TranscriptionWorker can be created with a model path."""
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models")
        assert worker is not None

    @patch("transcriber.worker.sherpa_onnx")
    def test_worker_custom_threads(self, mock_sherpa):
        """Worker accepts custom thread count."""
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models", num_threads=4)
        assert worker is not None


class TestWorkerTranscribe:
    """Tests for the transcribe method."""

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_transcribe_returns_text(self, mock_sf, mock_sherpa):
        """transcribe returns text from the model."""
        from transcriber.worker import TranscriptionWorker

        mock_sf.read.return_value = (MagicMock(), 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = "Hello world"

        result = worker.transcribe("/tmp/audio.opus")
        assert result == "Hello world"

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_transcribe_empty_audio(self, mock_sf, mock_sherpa):
        """transcribe returns empty string for silence."""
        from transcriber.worker import TranscriptionWorker

        mock_sf.read.return_value = (MagicMock(), 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = ""

        result = worker.transcribe("/tmp/silence.opus")
        assert result == ""


class TestDownloadModel:
    """Tests for the download_model helper."""

    @patch("transcriber.worker.urllib")
    def test_download_model_creates_dir(self, mock_urllib, tmp_path):
        """download_model creates the model directory."""
        from transcriber.worker import download_model

        model_dir = str(tmp_path / "models")
        result = download_model(model_dir, model_url="http://example.com/model.tar.gz")

        assert tmp_path.joinpath("models").is_dir()
