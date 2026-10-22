"""Sherpa-onnx transcription worker.

Handles model loading and audio file transcription.
"""

import os
import hashlib
import re
import tarfile
import time
import logging
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from shared.tar_utils import safe_extract_members
from transcriber.silence import find_silence_boundaries
from transcriber.splitter import split_at_silence

# Module-level imports for testability -- tests patch these names
try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None

try:
    import soundfile as sf
except ImportError:
    sf = None

try:
    import numpy as np
except ImportError:
    np = None

import urllib.request

logger = logging.getLogger(__name__)


# Whisper hallucination patterns: non-speech descriptions that Whisper
# produces when fed low-energy audio. These are not actual transcriptions
# but Whisper's attempts to describe ambient sounds, language tags, or
# audio artifacts. Filtered to prevent them from polluting transcripts.
_HALLUCINATION_PATTERNS = [
    # Bracketed tags: [BLANK_AUDIO], [silence], [no audio], [Birds chirping], etc.
    re.compile(r'^\s*\[.*\]\s*$', re.IGNORECASE),
    # Parenthesised sound descriptions: (growling), (snoring), (Burps), etc.
    re.compile(r'^\s*\(.*\)\s*$', re.IGNORECASE),
    # Asterisk-wrapped language/sound tags: *Spanish*, *Gibberish*, *Mumbling*, etc.
    re.compile(r'^\s*\*.*\*\s*$', re.IGNORECASE),
    # "speaking in foreign language" variants
    re.compile(r'^\s*\(speaking in .+ language\)\s*$', re.IGNORECASE),
]


def _is_whisper_hallucination(text: str) -> bool:
    """Check if text matches known Whisper hallucination patterns.

    Returns True if the text appears to be a non-speech description
    rather than actual transcribed speech.
    """
    for pattern in _HALLUCINATION_PATTERNS:
        if pattern.match(text):
            return True
    return False


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
        model_size: Whisper model size (tiny, base, small, medium, large-v3).
            Used to derive the correct filenames when loading or downloading
            the model.
    """

    def __init__(self, model_path: str, num_threads: int = 0, device: str = "cpu",
                 model_size: str = "small"):
        self.model_path = model_path
        self.num_threads = num_threads
        self.device = device
        self.model_size = model_size
        self.recognizer = None

    def load_model(self):
        """Load the sherpa-onnx Whisper model. Call once at startup."""
        size = self.model_size
        try:
            # enable_segment_timestamps uses Whisper's native <|0.00|> timestamp
            # tokens to produce segment-level start times in result.segment_timestamps.
            # Unlike enable_token_timestamps (which requires the ONNX model to be
            # exported with cross-attention outputs for DTW), this works with any
            # standard Whisper ONNX model. Without this, segment_timestamps is
            # empty and _build_segments() returns an empty list, causing the
            # transcript builder to fall back to untimestamped per-speaker blocks.
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=os.path.join(self.model_path, f"{size}-encoder.onnx"),
                decoder=os.path.join(self.model_path, f"{size}-decoder.onnx"),
                tokens=os.path.join(self.model_path, f"{size}-tokens.txt"),
                num_threads=self.num_threads,
                decoding_method="greedy_search",
                enable_segment_timestamps=True,
            )
            logger.info("Loaded sherpa-onnx Whisper model from %s", self.model_path)

        except Exception as e:
            logger.error("Failed to load model: %s", e)
            raise

    def transcribe(self, audio_path: str, hotwords: Optional[str] = None) -> Optional[TranscriptionResult]:
        """Transcribe an audio file. Returns TranscriptionResult or None on error.

        Uses silence-aware chunking: detects silence boundaries in the audio
        and splits at natural pause points instead of blind fixed-size chunks.
        This produces correctly ordered segments and avoids transcribing
        long stretches of silence.

        Args:
            audio_path: Path to the WAV file to transcribe.
            hotwords: Optional sherpa-onnx hotwords string ("Term1/Term2").
                      Applies a hard decoding bias for phonetic matches.
        """
        if not self.recognizer:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        try:
            # Detect silence boundaries and split audio at natural pauses
            silence_boundaries = find_silence_boundaries(
                audio_path,
                min_silence_duration=0.8,
                silence_threshold_db=-40.0,
            )
            chunks = split_at_silence(
                audio_path,
                silence_boundaries,
                max_duration=25.0,
            )

            all_text_parts: list[str] = []
            all_segments: list[TranscriptSegment] = []

            for chunk_start_sec, chunk_audio in chunks:
                # RMS-based silence filtering: skip chunks below -46 dB.
                # This aligns with the silence detection threshold (-40 dB)
                # to prevent Whisper from hallucinating on low-energy noise
                # (background hum, room tone) that falls between the two
                # thresholds. Raised from -60 dB after observing Whisper
                # produce hallucinated non-speech descriptions like
                # "(growling)", "*Spanish*", "[Birds chirping]" on chunks
                # that passed the old -60 dB filter.
                rms = np.sqrt(np.mean(chunk_audio ** 2))
                if rms < 0.005:  # ~-46 dB threshold
                    logger.debug(
                        "Skipping silent chunk at %.2fs (RMS=%.6f)",
                        chunk_start_sec, rms,
                    )
                    continue

                stream = self.recognizer.create_stream(hotwords=hotwords or None)
                stream.accept_waveform(16000, chunk_audio)
                self.recognizer.decode_stream(stream)

                result = stream.result
                chunk_text = result.text.strip()

                # Filter Whisper hallucinations: [BLANK_AUDIO] and
                # non-speech descriptions like (growling), *Spanish*,
                # [Birds chirping], etc. These appear when Whisper is
                # fed low-energy audio and tries to describe ambient
                # sounds or language instead of transcribing speech.
                if chunk_text and not _is_whisper_hallucination(chunk_text):
                    all_text_parts.append(chunk_text)

                chunk_segments = self._build_segments(result)
                # Offset each segment's start_time by the chunk's offset
                # so timestamps are relative to the start of the full file.
                # Also filter hallucination patterns from segment text.
                for seg in chunk_segments:
                    if not _is_whisper_hallucination(seg.text):
                        all_segments.append(TranscriptSegment(
                            start_time=seg.start_time + chunk_start_sec,
                            text=seg.text,
                        ))

            text = " ".join(all_text_parts)
            segments = all_segments

            logger.debug(
                "Transcribed %s: %d chars, %d segments (%d chunks, %d silence boundaries)",
                audio_path, len(text), len(segments), len(chunks), len(silence_boundaries),
            )
            return TranscriptionResult(text=text, segments=segments)

        except Exception as e:
            logger.error("Transcription failed for %s: %s", audio_path, e)
            return None

    @staticmethod
    def _build_segments(result) -> list[TranscriptSegment]:
        """Extract timestamped segments from sherpa-onnx segment-level data.

        Uses result.segment_timestamps, result.segment_texts, and
        result.segment_durations (populated when the model is loaded with
        enable_segment_timestamps=True).  Each segment is already a
        sentence-like chunk with a start time in seconds from the start
        of the audio.
        """
        segment_texts = getattr(result, "segment_texts", None)
        segment_timestamps = getattr(result, "segment_timestamps", None)

        if not segment_texts or not segment_timestamps:
            return []

        if len(segment_texts) != len(segment_timestamps):
            return []

        segments: list[TranscriptSegment] = []
        for text, ts in zip(segment_texts, segment_timestamps):
            cleaned = text.strip()
            if cleaned:
                segments.append(TranscriptSegment(
                    start_time=float(ts), text=cleaned,
                ))
        return segments


# Expected SHA-256 hashes for each model archive.  Only populate entries
# whose hash has been verified against the real download — unlisted sizes
# will still download but a WARNING is logged instead of failing.
EXPECTED_SHA256: dict[str, str] = {
    "small": "486a46afbb7ba798507190ffe02fea2dd726049af212e774537efac6afb210a6",
}

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


def download_model(model_dir: str, model_url: Optional[str] = None,
                   model_size: str = "small") -> str:
    """Download or verify the whisper model exists.

    Args:
        model_dir: Directory to store the model
        model_url: Optional override URL for model download
        model_size: Whisper model size (tiny, base, small, medium, large-v3).
            Determines the expected filenames inside the archive and the
            default download URL.

    Returns:
        Path to the model directory
    """
    model_path = Path(model_dir)
    model_path.mkdir(parents=True, exist_ok=True)

    # Check if model already exists
    required_files = [
        f"{model_size}-encoder.onnx",
        f"{model_size}-decoder.onnx",
        f"{model_size}-tokens.txt",
    ]

    all_exist = all((model_path / f).exists() for f in required_files)
    if all_exist:
        logger.info("Model already downloaded at %s", model_path)
        return str(model_path)

    # Default model URL (sherpa-onnx whisper-<size>)
    if not model_url:
        model_url = (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            f"asr-models/sherpa-onnx-whisper-{model_size}.tar.bz2"
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

    # Verify SHA-256 hash (skip with warning if no hash known for this size)
    expected_hash = EXPECTED_SHA256.get(model_size)
    if expected_hash is None:
        logger.warning(
            "No expected SHA-256 hash for model size '%s' — "
            "skipping verification. Downloaded archive will be used as-is.",
            model_size,
        )
    else:
        logger.info("Verifying SHA-256 hash...")
        actual_hash = _sha256_file(tar_path)
        if actual_hash != expected_hash:
            tar_path.unlink()
            raise RuntimeError(
                f"SHA-256 verification failed!"
                f" Expected: {expected_hash}"
                f" Got: {actual_hash}"
                f" The downloaded file may be corrupted or tampered with."
            )
        logger.info("Hash verified: %s...", actual_hash[:16])

    # Extract — path traversal protection via safe_extract_members
    try:
        with tarfile.open(tar_path, "r:bz2") as tar:
            safe_extract_members(tar, str(model_path), strip_components=1)
    except ValueError as e:
        raise RuntimeError(f"Model extraction failed (unsafe path): {e}") from e
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
