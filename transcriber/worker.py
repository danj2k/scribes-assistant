"""Sherpa-onnx transcription worker.

Handles model loading and audio file transcription.
"""

import os
import time
import logging
import tempfile
import tarfile
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
    """Wraps sherpa-onnx for audio-to-text transcription."""

    def __init__(self, model_path: str, num_threads: int = 2, device: str = "cpu"):
        self.model_path = model_path
        self.num_threads = num_threads
        self.device = device
        self.recognizer = None

    def load_model(self):
        """Load the sherpa-onnx model. Call once at startup."""
        try:
            config = sherpa_onnx.OfflineRecognizerConfig()
            config.model_config.transducer.encoder = os.path.join(
                self.model_path, "encoder-epoch-99-avg-1.onnx"
            )
            config.model_config.transducer.decoder = os.path.join(
                self.model_path, "decoder-epoch-99-avg-1.onnx"
            )
            config.model_config.transducer.joiner = os.path.join(
                self.model_path, "joiner-epoch-99-avg-1.onnx"
            )
            config.model_config.num_threads = self.num_threads

            self.recognizer = sherpa_onnx.OfflineRecognizer(config)
            logger.info("Loaded sherpa-onnx model from %s", self.model_path)

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
        "encoder-epoch-99-avg-1.onnx",
        "decoder-epoch-99-avg-1.onnx",
        "joiner-epoch-99-avg-1.onnx",
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

    logger.info("Downloading model from %s...", model_url)

    # Download
    tar_path = model_path / "model.tar.bz2"
    urllib.request.urlretrieve(model_url, tar_path)

    # Extract (guard: skip if download did not produce a file)
    if tar_path.exists():
        with tarfile.open(tar_path, "r:bz2") as tar:
            tar.extractall(model_path)
        tar_path.unlink()
    else:
        logger.warning("Download did not produce expected archive at %s", tar_path)

    logger.info("Model downloaded to %s", model_path)
    return str(model_path)
