from pathlib import Path
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

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_download_model_creates_dir(self, mock_urllib, mock_tarfile, tmp_path):
        """download_model creates the model directory."""
        from transcriber.worker import download_model

        def fake_extractall(path):
            files = ["encoder-epoch-99-avg-1.onnx", "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.onnx"]
            for f in files:
                (Path(path) / f).write_bytes(b"fake model data")

        mock_tar = MagicMock()
        mock_tar.extractall.side_effect = fake_extractall
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        result = download_model(model_dir, model_url="http://example.com/model.tar.gz")

        assert tmp_path.joinpath("models").is_dir()


class TestProgressHookDedup:
    """Tests for _progress_hook log deduplication."""

    def setup_method(self):
        """Reset deduplication state before each test."""
        from transcriber.worker import _reset_progress_state
        _reset_progress_state()

    def test_logs_first_call(self, caplog):
        """First call should always log."""
        import logging
        from transcriber.worker import _progress_hook
        with caplog.at_level(logging.INFO):
            _progress_hook(1, 1024, 10240)
        assert any("Downloading" in r.message for r in caplog.records)

    def test_suppresses_duplicate(self, caplog):
        """Second call with same values should not log."""
        import logging
        from transcriber.worker import _progress_hook
        with caplog.at_level(logging.INFO):
            _progress_hook(1, 1024, 10240)
            caplog.clear()
            _progress_hook(1, 1024, 10240)
        assert not any("Downloading" in r.message for r in caplog.records)

    def test_logs_on_mb_change(self, caplog):
        """Should log when MB downloaded changes even if pct is the same."""
        import logging
        from transcriber.worker import _progress_hook
        total = 1024 * 1024 * 100  # 100 MB total
        block_size = 1024 * 1024    # 1 MB blocks
        with caplog.at_level(logging.INFO):
            _progress_hook(1, block_size, total)   # 1 MB = 1%
            caplog.clear()
            _progress_hook(1, block_size, total)   # same -- suppressed
            assert not any("Downloading" in r.message for r in caplog.records)
            caplog.clear()
            _progress_hook(2, block_size, total)   # 2 MB = 2% -- logged
        assert any("2%" in r.message for r in caplog.records)

    def test_logs_on_pct_change(self, caplog):
        """Should log when percentage changes even if MB is the same."""
        import logging
        from transcriber.worker import _progress_hook
        # 200 KB total: 1 block = 0%, 2 blocks = 1%, both 0 MB
        total = 200 * 1024
        block_size = 1024
        with caplog.at_level(logging.INFO):
            _progress_hook(1, block_size, total)
            caplog.clear()
            _progress_hook(2, block_size, total)
        assert any("Downloading" in r.message for r in caplog.records)

    def test_uses_integer_percentage(self, caplog):
        """Percentage should be an integer, not a decimal."""
        import logging
        from transcriber.worker import _progress_hook
        total = 10240
        with caplog.at_level(logging.INFO):
            _progress_hook(3, 1024, total)
        for r in caplog.records:
            if "Downloading" in r.message:
                assert "." not in r.message.split("%")[0].split(":")[-1]

    def test_reset_allows_relogging(self, caplog):
        """After reset, same values should be logged again."""
        import logging
        from transcriber.worker import _progress_hook, _reset_progress_state
        with caplog.at_level(logging.INFO):
            _progress_hook(1, 1024, 10240)
            caplog.clear()
            _progress_hook(1, 1024, 10240)
            assert not any("Downloading" in r.message for r in caplog.records)
            _reset_progress_state()
            caplog.clear()
            _progress_hook(1, 1024, 10240)
        assert any("Downloading" in r.message for r in caplog.records)

    def test_unknown_total_still_deduplicates(self, caplog):
        """Unknown total_size path should also deduplicate by MB."""
        import logging
        from transcriber.worker import _progress_hook
        with caplog.at_level(logging.INFO):
            _progress_hook(1, 1024, 0)
            caplog.clear()
            _progress_hook(1, 1024, 0)
        assert not any("Downloading" in r.message for r in caplog.records)
