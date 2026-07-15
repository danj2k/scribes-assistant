"""Tests for transcriber.worker — TranscriptionWorker with mocked sherpa-onnx."""

from pathlib import Path
from transcriber.worker import _sha256_file, EXPECTED_SHA256
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

    @patch("transcriber.worker.sherpa_onnx")
    def test_worker_default_model_size(self, mock_sherpa):
        """Worker defaults to 'small' model size."""
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models")
        assert worker.model_size == "small"

    @patch("transcriber.worker.sherpa_onnx")
    def test_worker_custom_model_size(self, mock_sherpa):
        """Worker accepts custom model size."""
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models", model_size="base")
        assert worker.model_size == "base"


class TestWorkerLoadModel:
    """Tests that load_model uses the correct model_size in filenames."""

    @patch("transcriber.worker.sherpa_onnx")
    def test_load_model_uses_model_size_in_filenames(self, mock_sherpa):
        """load_model should construct paths using the model_size parameter."""
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models", model_size="base")
        worker.load_model()

        call_kwargs = mock_sherpa.OfflineRecognizer.from_whisper.call_args[1]
        assert call_kwargs["encoder"].endswith("base-encoder.onnx")
        assert call_kwargs["decoder"].endswith("base-decoder.onnx")
        assert call_kwargs["tokens"].endswith("base-tokens.txt")

    @patch("transcriber.worker.sherpa_onnx")
    def test_load_model_default_small_filenames(self, mock_sherpa):
        """load_model with default model_size should use 'small' in filenames."""
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models")
        worker.load_model()

        call_kwargs = mock_sherpa.OfflineRecognizer.from_whisper.call_args[1]
        assert call_kwargs["encoder"].endswith("small-encoder.onnx")
        assert call_kwargs["decoder"].endswith("small-decoder.onnx")
        assert call_kwargs["tokens"].endswith("small-tokens.txt")

    @patch("transcriber.worker.sherpa_onnx")
    def test_load_model_enables_token_timestamps(self, mock_sherpa):
        """load_model must enable token timestamps for timestamped transcripts.

        Without enable_token_timestamps=True, sherpa-onnx Whisper produces
        empty result.timestamps, causing _build_segments() to return [] and
        the transcript builder to fall back to untimestamped output.
        """
        from transcriber.worker import TranscriptionWorker
        worker = TranscriptionWorker(model_path="/tmp/models")
        worker.load_model()

        call_kwargs = mock_sherpa.OfflineRecognizer.from_whisper.call_args[1]
        assert call_kwargs["enable_token_timestamps"] is True


class TestWorkerTranscribe:
    """Tests for the transcribe method."""

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_transcribe_returns_text(self, mock_sf, mock_sherpa):
        """transcribe returns TranscriptionResult with text from the model."""
        from transcriber.worker import TranscriptionWorker

        mock_sf.read.return_value = (MagicMock(), 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.from_whisper.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = "Hello world"
        # No token-level timestamp data on the mock → segments empty
        mock_stream.result.tokens = None
        mock_stream.result.timestamps = None

        result = worker.transcribe("/tmp/audio.opus")
        assert result is not None
        assert result.text == "Hello world"
        assert result.segments == []

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_transcribe_empty_audio(self, mock_sf, mock_sherpa):
        """transcribe returns empty text for silence."""
        from transcriber.worker import TranscriptionWorker

        mock_sf.read.return_value = (MagicMock(), 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.from_whisper.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = ""
        mock_stream.result.tokens = None
        mock_stream.result.timestamps = None

        result = worker.transcribe("/tmp/silence.opus")
        assert result is not None
        assert result.text == ""


class TestTranscribeNoNumpyToList:
    """Tests that transcribe passes the numpy array directly to accept_waveform.

    Bug #19: calling .tolist() on a large numpy array creates millions of
    Python float objects, wasting memory and CPU.  sherpa-onnx's pybind11
    bindings accept numpy arrays via the buffer protocol natively.
    """

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_accept_waveform_receives_array_not_list(self, mock_sf, mock_sherpa):
        """accept_waveform must receive the audio object directly, not audio.tolist()."""
        import numpy as np
        from transcriber.worker import TranscriptionWorker

        audio = np.array([0.0, 0.1, 0.2, 0.3], dtype="float32")
        mock_sf.read.return_value = (audio, 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.from_whisper.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = "test"
        mock_stream.result.tokens = None
        mock_stream.result.timestamps = None

        worker.transcribe("/tmp/audio.wav")

        # accept_waveform should receive the numpy array itself
        call_args = mock_stream.accept_waveform.call_args
        assert call_args is not None
        passed_audio = call_args[0][1]  # second positional arg
        assert passed_audio is audio, (
            "accept_waveform should receive the numpy array directly, "
            "not a converted copy"
        )

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_tolist_not_called(self, mock_sf, mock_sherpa):
        """The audio array's .tolist() method must never be called."""
        import numpy as np
        from transcriber.worker import TranscriptionWorker

        audio = np.array([0.0, 0.1, 0.2, 0.3], dtype="float32")
        mock_sf.read.return_value = (audio, 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.from_whisper.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = "test"
        mock_stream.result.tokens = None
        mock_stream.result.timestamps = None

        worker.transcribe("/tmp/audio.wav")

        # The mock_stream is a MagicMock, so we can't check audio.tolist
        # directly on it.  Instead verify the audio array was not converted
        # by checking that accept_waveform got the exact same object.
        call_args = mock_stream.accept_waveform.call_args
        passed_audio = call_args[0][1]
        assert not isinstance(passed_audio, list), (
            "accept_waveform received a Python list — .tolist() was called"
        )

    @patch("transcriber.worker.sherpa_onnx")
    @patch("transcriber.worker.sf")
    def test_stereo_audio_passed_as_mono_array(self, mock_sf, mock_sherpa):
        """After stereo→mono downmix, the result should still be a numpy array."""
        import numpy as np
        from transcriber.worker import TranscriptionWorker

        # Stereo audio: 4 samples, 2 channels
        stereo = np.array(
            [[0.0, 0.1], [0.2, 0.3], [0.4, 0.5], [0.6, 0.7]],
            dtype="float32",
        )
        mock_sf.read.return_value = (stereo, 16000)

        worker = TranscriptionWorker(model_path="/tmp/models")
        mock_recognizer = MagicMock()
        mock_sherpa.OfflineRecognizer.from_whisper.return_value = mock_recognizer
        worker.load_model()

        mock_stream = MagicMock()
        mock_recognizer.create_stream.return_value = mock_stream
        mock_stream.result.text = "test"
        mock_stream.result.tokens = None
        mock_stream.result.timestamps = None

        worker.transcribe("/tmp/stereo.wav")

        call_args = mock_stream.accept_waveform.call_args
        passed_audio = call_args[0][1]
        # After mean(axis=1), result should be a 1-D numpy array, not a list
        assert isinstance(passed_audio, np.ndarray), (
            "Downmixed audio should remain a numpy array"
        )
        assert passed_audio.ndim == 1


class TestDownloadModel:
    """Tests for the download_model helper."""

    @patch("transcriber.worker._sha256_file", return_value=EXPECTED_SHA256["small"])
    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_download_model_creates_dir(self, mock_urllib, mock_tarfile, mock_sha256, tmp_path):
        """download_model creates the model directory."""
        from transcriber.worker import download_model

        def fake_extract(member, path, **kwargs):
            (Path(path) / member.name).write_bytes(b"fake model data")

        mock_tar = MagicMock()
        mock_members = [MagicMock() for _ in range(3)]
        mock_members[0].name = "sherpa-onnx-whisper-small/small-encoder.onnx"
        mock_members[1].name = "sherpa-onnx-whisper-small/small-decoder.onnx"
        mock_members[2].name = "sherpa-onnx-whisper-small/small-tokens.txt"
        mock_tar.getmembers.return_value = mock_members
        mock_tar.extract.side_effect = fake_extract
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        result = download_model(model_dir, model_url="http://example.com/model.tar.gz")

        assert tmp_path.joinpath("models").is_dir()

    @patch("transcriber.worker._sha256_file")
    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_download_model_base_size_uses_base_filenames(self, mock_urllib, mock_tarfile, mock_sha256, tmp_path):
        """download_model with model_size='base' checks for base-*.onnx files."""
        from transcriber.worker import download_model

        # No known hash for 'base' — should skip verification with a warning
        mock_sha256.return_value = "0" * 64

        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            (Path(path) / member.name).write_bytes(b"fake model data")

        mock_urllib.request.urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_members = [MagicMock() for _ in range(3)]
        mock_members[0].name = "sherpa-onnx-whisper-base/base-encoder.onnx"
        mock_members[1].name = "sherpa-onnx-whisper-base/base-decoder.onnx"
        mock_members[2].name = "sherpa-onnx-whisper-base/base-tokens.txt"
        mock_tar.getmembers.return_value = mock_members
        mock_tar.extract.side_effect = fake_extract
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        result = download_model(model_dir, model_url="http://example.com/model.tar.bz2",
                                model_size="base")
        assert result == model_dir

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_download_model_default_url_uses_model_size(self, mock_urllib, mock_tarfile, tmp_path):
        """download_model without model_url should build URL from model_size."""
        from transcriber.worker import download_model

        captured_url = []

        def fake_urlretrieve(url, path, reporthook=None):
            captured_url.append(url)
            Path(path).write_bytes(b"fake tar data")

        mock_urllib.request.urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_tar.getmembers.return_value = []
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        # Use model_size with no known hash so verification is skipped
        try:
            download_model(model_dir, model_size="medium")
        except FileNotFoundError:
            pass  # Expected — no files extracted in this mock

        assert len(captured_url) > 0
        assert "sherpa-onnx-whisper-medium" in captured_url[0]



class TestSha256Verification:
    """Tests for SHA-256 hash verification in transcriber download_model()."""

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_sha256_file_returns_hash(self, mock_urllib, mock_tarfile, tmp_path):
        """_sha256_file should return correct SHA-256 hex digest."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"hello world")
        result = _sha256_file(test_file)
        # SHA-256 of "hello world" is well-known
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_hash_mismatch_raises_runtime_error(self, mock_urllib, mock_tarfile, tmp_path):
        """Should raise RuntimeError when downloaded file hash doesn't match."""
        from transcriber.worker import download_model

        def fake_urlretrieve(url, path, reporthook=None):
            # Write data that won't match the expected hash
            Path(path).write_bytes(b"corrupt or tampered data")

        mock_urllib.request.urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        with pytest.raises(RuntimeError, match="SHA-256 verification failed"):
            download_model(model_dir, model_url="http://example.com/model.tar.bz2")

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    def test_hash_mismatch_deletes_bad_file(self, mock_urllib, mock_tarfile, tmp_path):
        """Should delete the downloaded file when hash doesn't match."""
        from transcriber.worker import download_model

        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"bad data")

        mock_urllib.request.urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        with pytest.raises(RuntimeError):
            download_model(model_dir, model_url="http://example.com/model.tar.bz2")

        # Verify bad archive was deleted
        archive = Path(model_dir) / "model.tar.bz2"
        assert not archive.exists(), "Bad archive should be deleted after hash mismatch"

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    @patch("transcriber.worker._sha256_file")
    def test_hash_match_proceeds_to_extraction(self, mock_sha256, mock_urllib, mock_tarfile, tmp_path):
        """Should proceed to extraction when hash matches."""
        from transcriber.worker import download_model, EXPECTED_SHA256
        mock_sha256.return_value = EXPECTED_SHA256["small"]

        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            (Path(path) / member.name).write_bytes(b"fake model data")

        mock_urllib.request.urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_members = [MagicMock() for _ in range(3)]
        mock_members[0].name = "sherpa-onnx-whisper-small/small-encoder.onnx"
        mock_members[1].name = "sherpa-onnx-whisper-small/small-decoder.onnx"
        mock_members[2].name = "sherpa-onnx-whisper-small/small-tokens.txt"
        mock_tar.getmembers.return_value = mock_members
        mock_tar.extract.side_effect = fake_extract
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        result = download_model(model_dir, model_url="http://example.com/model.tar.bz2")
        assert result == model_dir

    def test_expected_sha256_dict_has_known_hashes(self):
        """EXPECTED_SHA256 should be a dict with at least 'small' as a 64-char hex string."""
        assert isinstance(EXPECTED_SHA256, dict)
        assert "small" in EXPECTED_SHA256
        small_hash = EXPECTED_SHA256["small"]
        assert len(small_hash) == 64
        # All characters should be valid hex
        int(small_hash, 16)

    @patch("transcriber.worker.tarfile")
    @patch("transcriber.worker.urllib")
    @patch("transcriber.worker._sha256_file")
    def test_unknown_model_size_skips_hash_verification(self, mock_sha256, mock_urllib, mock_tarfile, tmp_path):
        """download_model with unknown model_size should skip verification, not raise."""
        from transcriber.worker import download_model

        # _sha256_file should not be called for unlisted sizes
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            (Path(path) / member.name).write_bytes(b"fake model data")

        mock_urllib.request.urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_members = [MagicMock() for _ in range(3)]
        mock_members[0].name = "sherpa-onnx-whisper-tiny/tiny-encoder.onnx"
        mock_members[1].name = "sherpa-onnx-whisper-tiny/tiny-decoder.onnx"
        mock_members[2].name = "sherpa-onnx-whisper-tiny/tiny-tokens.txt"
        mock_tar.getmembers.return_value = mock_members
        mock_tar.extract.side_effect = fake_extract
        mock_tarfile.open.return_value.__enter__ = lambda s: mock_tar
        mock_tarfile.open.return_value.__exit__ = MagicMock(return_value=False)

        model_dir = str(tmp_path / "models")
        result = download_model(model_dir, model_url="http://example.com/model.tar.bz2",
                                model_size="tiny")
        assert result == model_dir
        # _sha256_file should NOT have been called since "tiny" has no known hash
        mock_sha256.assert_not_called()

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


class TestRunWorkerConfigPath:
    """Tests for run_worker config path resolution.

    Verifies that run_worker reads CONFIG_PATH from env, falling back
    to /data/config.yaml — consistent with the bot's config path.
    """

    def test_explicit_config_path_used(self):
        """Explicit config_path argument takes precedence over env."""
        from transcriber.main import run_worker
        with patch("transcriber.main.Config") as mock_config, \
             patch.dict("os.environ", {"CONFIG_PATH": "/env/config.yaml"}):
            # Config succeeds, but Database fails — that's fine,
            # we only care about which path Config was called with.
            mock_config.return_value = MagicMock()
            with patch("transcriber.main.setup_logging_from_config"), \
                 patch("transcriber.main.Database", side_effect=RuntimeError("stop")):
                with pytest.raises(RuntimeError, match="stop"):
                    run_worker(config_path="/custom/path.yaml")
                mock_config.assert_called_once_with("/custom/path.yaml")

    def test_env_config_path_used_when_no_argument(self):
        """CONFIG_PATH env var used when config_path is None."""
        from transcriber.main import run_worker
        with patch("transcriber.main.Config") as mock_config, \
             patch.dict("os.environ", {"CONFIG_PATH": "/env/config.yaml"}):
            mock_config.return_value = MagicMock()
            with patch("transcriber.main.setup_logging_from_config"), \
                 patch("transcriber.main.Database", side_effect=RuntimeError("stop")):
                with pytest.raises(RuntimeError, match="stop"):
                    run_worker()
                mock_config.assert_called_once_with("/env/config.yaml")

    def test_default_config_path_when_no_env(self):
        """Falls back to /data/config.yaml when no env or argument."""
        from transcriber.main import run_worker
        with patch("transcriber.main.Config") as mock_config, \
             patch.dict("os.environ", {}, clear=True):
            mock_config.return_value = MagicMock()
            with patch("transcriber.main.setup_logging_from_config"), \
                 patch("transcriber.main.Database", side_effect=RuntimeError("stop")):
                with pytest.raises(RuntimeError, match="stop"):
                    run_worker()
                mock_config.assert_called_once_with("/data/config.yaml")
