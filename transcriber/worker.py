"""Sherpa-onnx transcription worker.

Handles model loading and audio file transcription.
"""

import os
import re
import hashlib
import tarfile
import time
import logging
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import numpy as np

from shared.tar_utils import safe_extract_members

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


# RMS energy threshold below which chunks are considered silence.
# Chunks below this RMS are skipped entirely — they'd produce
# hallucinated transcripts (bracketed sound effects, "[BLANK_AUDIO]", etc.)
# rather than meaningful speech.  Tuned empirically for 16 kHz float32 audio.
_RMS_THRESHOLD: float = 0.01

# Regex patterns that match common sherpa-onnx / Whisper hallucination
# artifacts — bracketed sound effects and atmosphere descriptions that
# appear when the model is fed silence or near-silence.
_HALLUCINATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\[BLANK_AUDIO\]", re.IGNORECASE),
    re.compile(r"\[Music\]", re.IGNORECASE),
    re.compile(r"\[MUSIC\]", re.IGNORECASE),
    re.compile(r"\(music\)", re.IGNORECASE),
    re.compile(r"\(gun ?shots?\)", re.IGNORECASE),
    re.compile(r"\(gunshot(s)?\)", re.IGNORECASE),
    re.compile(r"\[Applause\]", re.IGNORECASE),
    re.compile(r"\[Sound Effects\]", re.IGNORECASE),
    re.compile(r"\[Background Noise\]", re.IGNORECASE),
    re.compile(r"\[Laughter\]", re.IGNORECASE),
    re.compile(r"\[Tones\]", re.IGNORECASE),
    re.compile(r"\[Beep\]", re.IGNORECASE),
    re.compile(r"\(beep\)", re.IGNORECASE),
    re.compile(r"\(silence\)", re.IGNORECASE),
    re.compile(r"\[Silence\]", re.IGNORECASE),
    re.compile(r"\[sigh\]", re.IGNORECASE),
    re.compile(r"\(sigh\)", re.IGNORECASE),
]


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


def _is_low_energy(chunk: np.ndarray, threshold: float = _RMS_THRESHOLD) -> bool:
    """Return True if the chunk's RMS energy is below *threshold*.

    Chunks dominated by silence or near-silence produce consistent
    bracketed hallucination artifacts rather than meaningful speech.
    Skipping them early saves compute and avoids polluting the transcript.
    """
    if len(chunk) == 0:
        return True
    rms = np.sqrt(np.mean(chunk**2))
    return rms < threshold


def _filter_hallucinated_text(text: str) -> str:
    """Remove known hallucination patterns from transcribed text.

    Returns the text with matches replaced by empty string, then
    collapsed (leading/trailing whitespace stripped, internal runs
    of whitespace normalised).
    """
    for pattern in _HALLUCINATION_PATTERNS:
        text = pattern.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _filter_hallucinated_segments(
    segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    """Remove segments whose text is entirely a hallucination pattern."""
    filtered: list[TranscriptSegment] = []
    for seg in segments:
        cleaned = _filter_hallucinated_text(seg.text)
        if cleaned:
            filtered.append(TranscriptSegment(
                start_time=seg.start_time, text=cleaned,
            ))
    return filtered


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

    def transcribe(self, audio_path: str, hotwords: Optional[str] = None,
                   confidence_threshold: float = 0.3) -> Optional[TranscriptionResult]:
        """Transcribe an audio file. Returns TranscriptionResult or None on error.

        sherpa-onnx Whisper only processes the first 30 seconds of audio and
        silently discards the rest.  To handle longer recordings, we split the
        audio into 28-second chunks, transcribe each independently, and offset
        each chunk's segment timestamps by the chunk's start time so the final
        merged segments are chronological across the entire file.

        Chunks whose RMS energy falls below *confidence_threshold* are silently
        skipped — they produce hallucinated bracketed artifacts rather than
        meaningful speech.  Remaining transcriptions are post-filtered against a
        set of known hallucination patterns.

        Args:
            audio_path: Path to the WAV file to transcribe.
            hotwords: Optional sherpa-onnx hotwords string ("Term1/Term2").
                      Applies a hard decoding bias for phonetic matches.
            confidence_threshold: RMS energy floor (0.0–1.0).  Chunks with less
                energy are treated as silence and skipped.  Default 0.3.
        """
        if not self.recognizer:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        try:
            # Stream the audio from disk in 28-second chunks rather than
            # reading the entire file into memory.  A 3-hour stereo WAV at
            # 48 kHz/16-bit is ~2 GB on disk and ~4 GB as float32 in RAM;
            # loading it whole risks OOM on a constrained server.  Using
            # sf.SoundFile we read only one chunk (~5 MB) at a time,
            # regardless of file length.
            with sf.SoundFile(audio_path) as sf_file:
                sample_rate = sf_file.samplerate
                # sherpa-onnx Whisper silently discards audio beyond 30
                # seconds.  Chunk at 28s to stay safely under the limit
                # with margin for sample-rate rounding.  Each chunk is
                # transcribed independently; segment timestamps are offset
                # by chunk_start so the merged result preserves
                # chronological order across the full file.
                chunk_samples = 28 * sample_rate
                total_frames = len(sf_file)
                all_text_parts: list[str] = []
                all_segments: list[TranscriptSegment] = []

                frame_offset = 0
                while frame_offset < total_frames:
                    sf_file.seek(frame_offset)
                    chunk = sf_file.read(frames=chunk_samples, dtype="float32")

                    # Downmix stereo to mono after reading the chunk
                    if chunk.ndim > 1:
                        chunk = chunk.mean(axis=1)

                    # Skip chunks that are too quiet — they'd produce
                    # hallucinated bracketed artifacts rather than speech.
                    if _is_low_energy(chunk, threshold=confidence_threshold):
                        frame_offset += chunk_samples
                        continue

                    chunk_start_sec = frame_offset / sample_rate

                    if len(chunk) < sample_rate * 0.1:
                        # Skip chunks shorter than 0.1s — too short to transcribe
                        break

                    stream = self.recognizer.create_stream(hotwords=hotwords or None)
                    stream.accept_waveform(sample_rate, chunk)
                    self.recognizer.decode_stream(stream)

                    result = stream.result
                    chunk_text = result.text.strip()
                    # Apply hallucination pattern filter to the raw text
                    chunk_text = _filter_hallucinated_text(chunk_text)
                    if chunk_text:
                        all_text_parts.append(chunk_text)

                    chunk_segments = self._build_segments(result)
                    # Filter segments whose text is entirely hallucination,
                    # and clean remaining segments of hallucination patterns.
                    chunk_segments = _filter_hallucinated_segments(chunk_segments)
                    # Offset each segment's start_time by the chunk's offset
                    # so timestamps are relative to the start of the full file.
                    for seg in chunk_segments:
                        all_segments.append(TranscriptSegment(
                            start_time=seg.start_time + chunk_start_sec,
                            text=seg.text,
                        ))

                    frame_offset += chunk_samples

            text = " ".join(all_text_parts)
            segments = all_segments

            logger.debug(
                "Transcribed %s: %d chars, %d segments (chunked from %d frames)",
                audio_path, len(text), len(segments), total_frames,
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
