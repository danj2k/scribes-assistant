"""Tests for scripts.download_model."""

import os
import sys
import tempfile
import shutil
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.download_model import (
    download_model,
    main,
    _required_files,
    _progress_hook,
    _sha256_file,
    EXPECTED_SHA256,
)


@pytest.fixture
def tmp_model_dir():
    """Create a temporary directory for model files."""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestDownloadModel:
    """Tests for download_model()."""

    def test_skip_when_files_exist(self, tmp_model_dir):
        """Should skip download if all required files already exist."""
        for f in _required_files("small"):
            (Path(tmp_model_dir) / f).write_bytes(b"fake model data")

        result = download_model(tmp_model_dir)
        assert result == tmp_model_dir

    def test_force_re_download(self, tmp_model_dir):
        """Should re-download when force=True even if files exist."""
        for f in _required_files("small"):
            (Path(tmp_model_dir) / f).write_bytes(b"fake model data")

        fake_tar = b"fake tar data"

        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(fake_tar)

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_tar = MagicMock()
                    mock_open.return_value.__enter__ = lambda s: mock_tar
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    result = download_model(tmp_model_dir, force=True)

        assert result == tmp_model_dir

    def test_download_and_extract(self, tmp_model_dir):
        """Should download, extract, and verify model files."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            # member.name has been stripped to just the filename (e.g. "small-encoder.onnx")
            (Path(path) / member.name).write_bytes(b"extracted")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_tar = MagicMock()
                    mock_tar.getmembers.return_value = [MagicMock() for _ in _required_files("small")]
                    for i, m in enumerate(mock_tar.getmembers()):
                        m.name = f'sherpa-onnx-whisper-small/{_required_files("small")[i]}'
                    mock_tar.extract.side_effect = fake_extract
                    mock_open.return_value.__enter__ = lambda s: mock_tar
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    result = download_model(tmp_model_dir)

        assert result == tmp_model_dir
        for f in _required_files("small"):
            assert (Path(tmp_model_dir) / f).exists()

    def test_download_failure(self, tmp_model_dir):
        """Should raise RuntimeError if download fails."""
        with patch(
            "scripts.download_model.urllib.request.urlretrieve",
            side_effect=URLError("network error"),
        ):
            with pytest.raises(RuntimeError, match="Download failed"):
                download_model(tmp_model_dir)

    def test_extraction_failure(self, tmp_model_dir):
        """Should raise RuntimeError if extraction fails."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"corrupt data")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_open.side_effect = Exception("bad archive")
                    with pytest.raises(RuntimeError, match="Extraction failed"):
                        download_model(tmp_model_dir)

    def test_missing_files_after_extraction(self, tmp_model_dir):
        """Should raise FileNotFoundError if required files are missing."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            # Only create one of the three required files
            (Path(path) / member.name).write_bytes(b"partial")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_tar = MagicMock()
                    # Only return one member — simulates archive with missing files
                    mock_member = MagicMock()
                    mock_member.name = f'sherpa-onnx-whisper-small/{_required_files("small")[0]}'
                    mock_tar.getmembers.return_value = [mock_member]
                    mock_tar.extract.side_effect = fake_extract
                    mock_open.return_value.__enter__ = lambda s: mock_tar
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    with pytest.raises(FileNotFoundError, match="missing"):
                        download_model(tmp_model_dir)

    def test_creates_model_directory(self, tmp_model_dir):
        """Should create the model directory if it doesn't exist."""
        nested = os.path.join(tmp_model_dir, "nested", "model")
        os.makedirs(nested, exist_ok=True)
        for f in _required_files("small"):
            (Path(nested) / f).write_bytes(b"fake model data")

        result = download_model(nested)
        assert result == nested
        assert os.path.isdir(nested)

    def test_archive_cleaned_up_on_success(self, tmp_model_dir):
        """Should remove the archive after successful extraction."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            # member.name has been stripped to just the filename (e.g. "small-encoder.onnx")
            (Path(path) / member.name).write_bytes(b"extracted")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_tar = MagicMock()
                    mock_tar.getmembers.return_value = [MagicMock() for _ in _required_files("small")]
                    for i, m in enumerate(mock_tar.getmembers()):
                        m.name = f'sherpa-onnx-whisper-small/{_required_files("small")[i]}'
                    mock_tar.extract.side_effect = fake_extract
                    mock_open.return_value.__enter__ = lambda s: mock_tar
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    download_model(tmp_model_dir)

        assert not (Path(tmp_model_dir) / "model.tar.bz2").exists()

    def test_archive_cleaned_up_on_extraction_failure(self, tmp_model_dir):
        """Should remove archive even if extraction fails."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_open.side_effect = Exception("bad archive")
                    with pytest.raises(RuntimeError):
                        download_model(tmp_model_dir)

        assert not (Path(tmp_model_dir) / "model.tar.bz2").exists()



class TestSha256Verification:
    """Tests for SHA-256 hash verification in download_model()."""

    def test_sha256_file_returns_hash(self, tmp_model_dir):
        """_sha256_file should return the SHA-256 hex digest of a file."""
        test_file = Path(tmp_model_dir) / "test.bin"
        test_file.write_bytes(b"hello world")
        result = _sha256_file(test_file)
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_hash_mismatch_raises_runtime_error(self, tmp_model_dir):
        """Should raise RuntimeError when downloaded file hash doesn't match expected."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"corrupt or tampered data")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with pytest.raises(RuntimeError, match="SHA-256 verification failed"):
                download_model(tmp_model_dir)

    def test_hash_mismatch_deletes_bad_file(self, tmp_model_dir):
        """Should delete the downloaded file when hash doesn't match."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"bad data")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with pytest.raises(RuntimeError):
                download_model(tmp_model_dir)

        archive = Path(tmp_model_dir) / "model.tar.bz2"
        assert not archive.exists(), "Bad archive should be deleted after hash mismatch"

    def test_hash_match_proceeds_to_extraction(self, tmp_model_dir):
        """Should proceed to extraction when hash matches."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            # member.name has been stripped to just the filename (e.g. "small-encoder.onnx")
            (Path(path) / member.name).write_bytes(b"extracted")

        with patch("scripts.download_model.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            with patch("scripts.download_model.tarfile.open") as mock_open:
                with patch("scripts.download_model._sha256_file", return_value=EXPECTED_SHA256["small"]):
                    mock_tar = MagicMock()
                    mock_tar.getmembers.return_value = [MagicMock() for _ in _required_files("small")]
                    for i, m in enumerate(mock_tar.getmembers()):
                        m.name = f'sherpa-onnx-whisper-small/{_required_files("small")[i]}'
                    mock_tar.extract.side_effect = fake_extract
                    mock_open.return_value.__enter__ = lambda s: mock_tar
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    result = download_model(tmp_model_dir)

        assert result == tmp_model_dir

    def test_expected_sha256_dict_has_known_hashes(self):
        """EXPECTED_SHA256 should be a dict with at least 'small' as a 64-char hex string."""
        assert isinstance(EXPECTED_SHA256, dict)
        assert "small" in EXPECTED_SHA256
        small_hash = EXPECTED_SHA256["small"]
        assert len(small_hash) == 64
        int(small_hash, 16)

    @patch("scripts.download_model.urllib.request.urlretrieve")
    @patch("scripts.download_model.tarfile.open")
    @patch("scripts.download_model._sha256_file")
    def test_unknown_model_size_skips_hash_verification(self, mock_sha256, mock_open, mock_urlretrieve, tmp_model_dir):
        """download_model with unknown model_size should skip verification, not raise."""
        def fake_urlretrieve(url, path, reporthook=None):
            Path(path).write_bytes(b"fake tar data")

        def fake_extract(member, path, **kwargs):
            (Path(path) / member.name).write_bytes(b"fake model data")

        mock_urlretrieve.side_effect = fake_urlretrieve
        mock_tar = MagicMock()
        mock_members = [MagicMock() for _ in range(3)]
        mock_members[0].name = "sherpa-onnx-whisper-tiny/tiny-encoder.onnx"
        mock_members[1].name = "sherpa-onnx-whisper-tiny/tiny-decoder.onnx"
        mock_members[2].name = "sherpa-onnx-whisper-tiny/tiny-tokens.txt"
        mock_tar.getmembers.return_value = mock_members
        mock_tar.extract.side_effect = fake_extract
        mock_open.return_value.__enter__ = lambda s: mock_tar
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = download_model(tmp_model_dir, model_size="tiny")
        assert result == tmp_model_dir
        # _sha256_file should NOT have been called since "tiny" has no known hash
        mock_sha256.assert_not_called()

class TestProgressHook:
    """Tests for _progress_hook()."""

    def test_with_total_size(self, capsys):
        """Should print progress when total_size is known."""
        _progress_hook(10, 1024, 10240)
        output = capsys.readouterr().err
        assert "Downloading" in output

    def test_without_total_size(self, capsys):
        """Should print MB downloaded when total_size is 0."""
        _progress_hook(10, 1024, 0)
        output = capsys.readouterr().err
        assert "MB" in output


class TestMain:
    """Tests for main() CLI entry point."""

    def test_main_default_args(self):
        """Should call download_model with default directory and model_size."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.return_value = "data/models/whisper-small"
            sys.argv = ["download_model.py"]
            main()
            mock_dl.assert_called_once_with(
                "data/models/whisper-small", force=False, model_size="small"
            )

    def test_main_custom_dir(self):
        """Should use custom directory from argv."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.return_value = "/tmp/model"
            sys.argv = ["download_model.py", "/tmp/model"]
            main()
            mock_dl.assert_called_once_with("/tmp/model", force=False, model_size="small")

    def test_main_force_flag(self):
        """Should pass force=True when --force is given."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.return_value = "/tmp/model"
            sys.argv = ["download_model.py", "/tmp/model", "--force"]
            main()
            mock_dl.assert_called_once_with("/tmp/model", force=True, model_size="small")

    def test_main_short_force_flag(self):
        """Should pass force=True when -f is given."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.return_value = "/tmp/model"
            sys.argv = ["download_model.py", "/tmp/model", "-f"]
            main()
            mock_dl.assert_called_once_with("/tmp/model", force=True, model_size="small")

    def test_main_model_flag(self):
        """Should pass model_size='base' when --model base is given."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.return_value = "/tmp/model"
            sys.argv = ["download_model.py", "/tmp/model", "--model", "base"]
            main()
            mock_dl.assert_called_once_with("/tmp/model", force=False, model_size="base")

    def test_main_short_model_flag(self):
        """Should pass model_size='medium' when -m medium is given."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.return_value = "/tmp/model"
            sys.argv = ["download_model.py", "/tmp/model", "-m", "medium"]
            main()
            mock_dl.assert_called_once_with("/tmp/model", force=False, model_size="medium")

    def test_main_download_error(self):
        """Should exit with code 1 on error."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.side_effect = RuntimeError("download failed")
            sys.argv = ["download_model.py"]
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_file_not_found_error(self):
        """Should exit with code 1 on FileNotFoundError."""
        with patch("scripts.download_model.download_model") as mock_dl:
            mock_dl.side_effect = FileNotFoundError("missing files")
            sys.argv = ["download_model.py"]
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
