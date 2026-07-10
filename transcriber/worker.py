"""Sherpa-onnx transcription worker.

Handles model loading and audio file transcription.
"""

import os
import hashlib
import tarfile
import time
import logging
import tempfile
from pathlib import Path
from typing import Optional

# Module-level imports for testability -- tests patch these names
try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None

try:
    import soundfile as sf
except ImportError:
    sf = None

import urllib.request

logger = logging.getLogger(__name__)


class TranscriptionWorker:
    """Wraps sherpa-onnx for audio-to-text transcription.

    Args:
        model_path: Path to the sherpa-onnx model directory.
        num_threads: CPU threads for sherpa-onnx.
        device: Compute device (only "cpu" supported for Whisper).
    """

    def __init__(self, model_path: str, num_threads: int = 2, device: str = "cpu"):
        self.model_path = model_path
        self.num_threads = num_threads
        self.device = device
        self.recognizer = None

    def load_model(self):
        """Load the sherpa-onnx Whisper model. Call once at startup."""
        try:
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=os.path.join(self.model_path, "small-encoder.onnx"),
                decoder=os.path.join(self.model_path, "small-decoder.onnx"),
                tokens=os.path.join(self.model_path, "small-tokens.txt"),
                num_threads=self.num_threads,
                decoding_method="greedy_search",
            )
            logger.info("Loaded sherpa-onnx Whisper model from %s", self.model_path)

        except Exception as e:
            logger.error("Failed to load model: %s", e)
            raise

    def transcribe(self, audio_path: str) -> Optional[str]:
        """Transcribe an audio file. Returns text or None on error."""
        if not self.recognizer:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        try:
            audio, sample_rate = sf.read(audio_path, dtype="float32")

            if len(audio.shape) > 1:
                audio = audio.mean(axis=1)

            stream = self.recognizer.create_stream()
            stream.accept_waveform(sample_rate, audio.tolist())
            self.recognizer.decode_stream(stream)

            result = stream.result
            text = result.text.strip()

            logger.debug("Transcribed %s: %d chars", audio_path, len(text))
            return text

        except Exception as e:
            logger.error("Transcription failed for %s: %s", audio_path, e)
            return None


# Expected SHA-256 hash for the model archive
EXPECTED_SHA256 = "486a46afbb7ba798507190ffe02fea2dd726049af212e774537efac6afb210a6"

# Module-level state for progress deduplication -- only log when something changes
_last_log_pct: int = -1
_last_log_mb: int = -1


def _reset_progress_state() -> None:
    """Reset deduplication state before a new download."""
    global _last_log_pct, _last_log_mb
    _last_log_pct = -1
    _last_log_mb = -1


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file, reading in chunks to handle large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    """Log download progress, only when something changes.

    Repeated calls with the same percentage or megabyte count are suppressed
    to avoid flooding the logs.
    """
    global _last_log_pct, _last_log_mb
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, int(downloaded / total_size * 100))
        mb_downloaded = int(downloaded / (1024 * 1024))
        mb_total = int(total_size / (1024 * 1024))
        if pct != _last_log_pct or mb_downloaded != _last_log_mb:
            _last_log_pct = pct
            _last_log_mb = mb_downloaded
            logger.info(
                "Downloading model: %d%% (%d/%d MB)", pct, mb_downloaded, mb_total
            )
    else:
        mb_downloaded = int(downloaded / (1024 * 1024))
        if mb_downloaded != _last_log_mb:
            _last_log_mb = mb_downloaded
            logger.info("Downloading model: %d MB", mb_downloaded)


def download_model(model_dir: str, model_url: Optional[str] = None) -> str:
    """Download or verify the whisper model exists.

    Args:
        model_dir: Directory to store the model
        model_url: Optional override URL for model download

    Returns:
        Path to the model directory
    """
    model_path = Path(model_dir)
    model_path.mkdir(parents=True, exist_ok=True)

    # Check if model already exists
    required_files = [
        "small-encoder.onnx",
        "small-decoder.onnx",
        "small-tokens.txt",
    ]

    all_exist = all((model_path / f).exists() for f in required_files)
    if all_exist:
        logger.info("Model already downloaded at %s", model_path)
        return str(model_path)

    # Default model URL (sherpa-onnx whisper-small)
    if not model_url:
        model_url = (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "asr-models/sherpa-onnx-whisper-small.tar.bz2"
        )

    _reset_progress_state()
    logger.info("Downloading model from %s...", model_url)

    tar_path = model_path / "model.tar.bz2"

    try:
        urllib.request.urlretrieve(model_url, str(tar_path), reporthook=_progress_hook)
    except Exception as e:
        if tar_path.exists():
            tar_path.unlink()
        raise RuntimeError(f"Model download failed: {e}") from e

    # Verify SHA-256 hash
    logger.info("Verifying SHA-256 hash...")
    actual_hash = _sha256_file(tar_path)
    if actual_hash != EXPECTED_SHA256:
        tar_path.unlink()
        raise RuntimeError(
            f"SHA-256 verification failed!"
            f" Expected: {EXPECTED_SHA256}"
            f" Got: {actual_hash}"
            f" The downloaded file may be corrupted or tampered with."
        )
    logger.info("Hash verified: %s...", actual_hash[:16])

    # Extract
    try:
        with tarfile.open(tar_path, "r:bz2") as tar:
            for member in tar.getmembers():
                # Strip the top-level directory name if present
                parts = member.name.split("/", 1)
                if len(parts) > 1:
                    member.name = parts[1]
                tar.extract(member, model_path)
    except Exception as e:
        raise RuntimeError(f"Model extraction failed: {e}") from e
    finally:
        if tar_path.exists():
            tar_path.unlink()

    # Verify
    missing = [f for f in required_files if not (model_path / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"Model extraction incomplete — missing: {missing}"
        )

    logger.info("Model downloaded to %s", model_path)
    return str(model_path)
