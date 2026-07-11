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
from dataclasses import dataclass, field

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


@dataclass
class TranscriptSegment:
    """A timestamped chunk of recognised speech.

    Attributes:
        start_time: Seconds from the start of the audio file.
        text: The recognised text for this segment.
    """
    start_time: float
    text: str


@dataclass
class TranscriptionResult:
    """Result of transcribing one audio file.

    Attributes:
        text: The full transcribed text (all segments joined).
        segments: Timestamped segments for interleaved merging.
            Empty list if the recogniser didn't produce timestamps.
    """
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)


class TranscriptionWorker:
    """Wraps sherpa-onnx for audio-to-text transcription.

    Args:
        model_path: Path to the sherpa-onnx model directory.
        num_threads: CPU threads for sherpa-onnx.
        device: Compute device (only "cpu" supported for Whisper).
    """

    def __init__(self, model_path: str, num_threads: int = 0, device: str = "cpu"):
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

    def transcribe(self, audio_path: str, hotwords: Optional[str] = None) -> Optional[TranscriptionResult]:
        """Transcribe an audio file. Returns TranscriptionResult or None on error.

        The result contains the full text and a list of timestamped segments.
        Segments are grouped by sentence boundaries (punctuation tokens) or
        gaps in audio > 1 second. Each segment's start_time is the timestamp
        of its first token.

        Args:
            audio_path: Path to the WAV file to transcribe.
            hotwords: Optional sherpa-onnx hotwords string ("Term1/Term2").
                      Applies a hard decoding bias for phonetic matches.
        """
        if not self.recognizer:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        try:
            audio, sample_rate = sf.read(audio_path, dtype="float32")

            if len(audio.shape) > 1:
                audio = audio.mean(axis=1)

            stream = self.recognizer.create_stream(hotwords=hotwords)
            stream.accept_waveform(sample_rate, audio.tolist())
            self.recognizer.decode_stream(stream)

            result = stream.result
            text = result.text.strip()

            # Extract timestamped segments from token-level data.
            # sherpa-onnx provides result.tokens (list of token strings)
            # and result.timestamps (list of floats, seconds from start).
            segments = self._build_segments(result)

            logger.debug(
                "Transcribed %s: %d chars, %d segments",
                audio_path, len(text), len(segments),
            )
            return TranscriptionResult(text=text, segments=segments)

        except Exception as e:
            logger.error("Transcription failed for %s: %s", audio_path, e)
            return None

    @staticmethod
    def _build_segments(result) -> list[TranscriptSegment]:
        """Group token-level timestamps into sentence-like segments.

        Tokens are grouped into segments by:
        1. Sentence-ending punctuation (".", "?", "!") starts a new segment
        2. A gap > 1 second between consecutive tokens starts a new segment

        Each segment's start_time is the timestamp of its first token.
        """
        tokens = getattr(result, "tokens", None)
        timestamps = getattr(result, "timestamps", None)

        # No timestamp data available — return empty list (caller falls
        # back to per-speaker blocks).
        if not tokens or not timestamps or len(tokens) != len(timestamps):
            return []

        segments: list[TranscriptSegment] = []
        current_tokens: list[str] = []
        current_start: float = 0.0
        prev_time: float = 0.0
        sentence_endings = {".", "?", "!", "。", "?", "!", "？", "！"}

        for i, (token, ts) in enumerate(zip(tokens, timestamps)):
            if not current_tokens:
                current_start = ts

            current_tokens.append(token)

            # Check if this token ends a sentence or has a large gap before next
            is_sentence_end = token.strip() in sentence_endings
            is_last = i == len(tokens) - 1

            if is_last:
                next_ts = None
            else:
                next_ts = timestamps[i + 1]

            gap = (next_ts - ts) if next_ts is not None else 0.0
            large_gap = gap > 1.0

            if is_sentence_end or is_last or large_gap:
                seg_text = "".join(current_tokens).strip()
                if seg_text:
                    segments.append(TranscriptSegment(
                        start_time=current_start, text=seg_text
                    ))
                current_tokens = []

            prev_time = ts

        return segments


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
