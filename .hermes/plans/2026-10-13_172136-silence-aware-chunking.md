# Silence-Aware Chunking Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace blind 28-second chunking in the transcriber worker with silence-aware splitting so that audio is segmented at natural speech boundaries, eliminating mid-sentence cuts, out-of-order transcript lines, and [BLANK_AUDIO] spam.

**Architecture:** Add a silence-detection module that uses ffmpeg's `silencedetect` filter to find pause boundaries in each audio file. The worker then splits audio at those boundaries (capping segments at 25s to stay under Whisper's 30s limit) instead of at fixed 28s intervals. Segments that are pure silence (or produce only [BLANK_AUDIO]) are filtered before storage. The transcript merge in `main.py` already sorts by `start_time, seq` — no changes needed there.

**Tech Stack:** ffmpeg (silencedetect filter, already available in the container), soundfile (existing), Python stdlib subprocess.

---

## Problem Analysis

The attached transcript `2026-10-07_18-19-13 (1).txt` shows the symptom clearly:

- Lines are grouped in blocks of ~20 entries, each exactly 28 seconds apart
- Blocks appear out of chronological order (block starting at 00:41:32 comes after block starting at 00:50:24)
- Nearly all entries are `[BLANK_AUDIO]` — silence is being transcribed as text

Root cause in `transcriber/worker.py` lines 136-174:
```python
chunk_samples = 28 * sample_rate  # blind fixed-size chunking
...
while frame_offset < total_frames:
    chunk = sf_file.read(frames=chunk_samples, dtype="float32")
    # transcribe chunk, offset timestamps by chunk_start_sec
    frame_offset += chunk_samples
```

This cuts audio at arbitrary points regardless of speech boundaries. When speech spans a chunk boundary, Whisper produces a segment at the end of chunk N and another at the start of chunk N+1, with timestamps that may not align correctly. Silence produces `[BLANK_AUDIO]` segments that clutter the transcript.

---

## Implementation Plan

### Task 1: Add silence detection module

**Objective:** Create a function that takes an audio file path and returns a list of silence boundaries (start/end times of silent regions).

**Files:**
- Create: `transcriber/silence.py`
- Test: `tests/test_silence.py`

**Step 1: Write failing test**

```python
# tests/test_silence.py
import pytest
from transcriber.silence import find_silence_boundaries

def test_find_silence_boundaries_returns_sorted_list():
    """Returns list of (start, end) tuples sorted by start time."""
    # Use a synthetic WAV with known silence gaps
    boundaries = find_silence_boundaries("tests/fixtures/speech_with_gaps.wav")
    assert isinstance(boundaries, list)
    for start, end in boundaries:
        assert end > start
    # Verify sorted order
    for i in range(len(boundaries) - 1):
        assert boundaries[i][0] <= boundaries[i + 1][0]

def test_find_silence_boundaries_respects_threshold():
    """Only reports silences longer than the minimum duration."""
    boundaries = find_silence_boundaries(
        "tests/fixtures/speech_with_gaps.wav",
        min_silence_duration=0.5,
    )
    for start, end in boundaries:
        assert (end - start) >= 0.5

def test_find_silence_boundaries_empty_for_continuous_speech():
    """Returns empty list when no silence detected."""
    boundaries = find_silence_boundaries("tests/fixtures/continuous_speech.wav")
    assert boundaries == []
```

**Step 2: Run test to verify failure**

```bash
cd /workspace/scribes-assistant
pytest tests/test_silence.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'transcriber.silence'`

**Step 3: Write implementation**

```python
# transcriber/silence.py
"""Silence detection for audio files using ffmpeg's silencedetect filter.

Provides boundary detection so the transcriber can split audio at natural
pause points instead of blind fixed-size chunks.
"""
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def find_silence_boundaries(
    audio_path: str,
    min_silence_duration: float = 0.8,
    silence_threshold_db: float = -40.0,
) -> list[tuple[float, float]]:
    """Find silence boundaries in an audio file using ffmpeg silencedetect.

    Args:
        audio_path: Path to the audio file (WAV, MP3, etc.).
        min_silence_duration: Minimum silence duration in seconds to report.
            Silences shorter than this are ignored (they're just pauses
            within speech, not segment boundaries).
        silence_threshold_db: Volume threshold in dB below which audio is
            considered silent. -40 dB is a reasonable default for speech.

    Returns:
        List of (start_seconds, end_seconds) tuples for each silence region,
        sorted by start time. Empty list if no silence detected.
    """
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}",
        "-f", "null", "-"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("silencedetect timed out for %s, returning empty", audio_path)
        return []
    except FileNotFoundError:
        logger.error("ffmpeg not found — silence detection unavailable")
        return []

    # silencedetect outputs to stderr in format:
    # [silencedetect @ ...] silence_start: 1.234
    # [silencedetect @ ...] silence_end: 2.345 | silence_duration: 1.111
    boundaries: list[tuple[float, float]] = []
    current_start: float | None = None

    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[-1].strip())
            except (ValueError, IndexError):
                continue
        elif "silence_end:" in line and current_start is not None:
            try:
                end = float(line.split("silence_end:")[-1].split("|")[0].strip())
                boundaries.append((current_start, end))
                current_start = None
            except (ValueError, IndexError):
                current_start = None
                continue

    # If file ends mid-silence, close the last boundary at file duration
    if current_start is not None:
        duration = _get_audio_duration(audio_path)
        if duration and duration > current_start:
            boundaries.append((current_start, duration))

    boundaries.sort(key=lambda b: b[0])
    return boundaries


def _get_audio_duration(audio_path: str) -> float | None:
    """Get audio duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json",
        audio_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return None
```

**Step 4: Create test fixtures and run tests**

Generate synthetic WAV fixtures using Python:
```python
# tests/conftest.py additions or inline in test file
import numpy as np
import soundfile as sf

def create_test_fixtures():
    sr = 16000
    # speech_with_gaps.wav: 1s speech, 1s silence, 1s speech, 1s silence, 1s speech
    speech = np.random.randn(sr).astype(np.float32) * 0.3
    silence = np.zeros(sr, dtype=np.float32)
    audio = np.concatenate([speech, silence, speech, silence, speech])
    sf.write("tests/fixtures/speech_with_gaps.wav", audio, sr)

    # continuous_speech.wav: 5s of noise (no silence)
    audio = np.random.randn(5 * sr).astype(np.float32) * 0.3
    sf.write("tests/fixtures/continuous_speech.wav", audio, sr)
```

Run: `pytest tests/test_silence.py -v`
Expected: 3 passed

**Step 5: Commit**

```bash
git add transcriber/silence.py tests/test_silence.py tests/fixtures/
git commit -m "feat: add silence detection module using ffmpeg silencedetect"
```

---

### Task 2: Add audio segment splitting utility

**Objective:** Create a function that takes silence boundaries and an audio file, and yields (start_time, audio_data) chunks split at silence midpoints, each capped at a maximum duration.

**Files:**
- Create: `transcriber/splitter.py`
- Test: `tests/test_splitter.py`

**Step 1: Write failing test**

```python
# tests/test_splitter.py
import numpy as np
import soundfile as sf
from transcriber.splitter import split_at_silence

def test_split_at_silence_basic():
    """Splits audio at silence midpoints, respecting max duration."""
    sr = 16000
    # 1s speech + 1s silence + 1s speech + 1s silence + 1s speech = 5s
    speech = np.random.randn(sr).astype(np.float32) * 0.3
    silence = np.zeros(sr, dtype=np.float32)
    audio = np.concatenate([speech, silence, speech, silence, speech])
    sf.write("/tmp/test_split.wav", audio, sr)

    boundaries = [(1.0, 2.0), (3.0, 4.0)]  # two silence regions
    chunks = list(split_at_silence("/tmp/test_split.wav", boundaries, max_duration=25.0))

    # Should produce 3 chunks: 0-1.5s, 2.5-3.5s, 4.0-5.0s
    # (split at midpoint of each silence)
    assert len(chunks) == 3
    assert chunks[0][0] == 0.0  # first chunk starts at 0
    assert chunks[1][0] > 1.0   # second chunk starts after first silence
    assert chunks[2][0] > 3.0   # third chunk starts after second silence

def test_split_at_silence_respects_max_duration():
    """Long segments without silence are further split at max_duration."""
    sr = 16000
    # 30s of continuous speech (no silence)
    audio = np.random.randn(30 * sr).astype(np.float32) * 0.3
    sf.write("/tmp/test_split_long.wav", audio, sr)

    chunks = list(split_at_silence("/tmp/test_split_long.wav", [], max_duration=25.0))
    # Should split into 2 chunks: 0-25s and 25-30s
    assert len(chunks) == 2
    assert chunks[0][0] == 0.0
    assert abs(chunks[1][0] - 25.0) < 0.1

def test_split_at_silence_no_boundaries_short_file():
    """File shorter than max_duration with no silence returns single chunk."""
    sr = 16000
    audio = np.random.randn(5 * sr).astype(np.float32) * 0.3
    sf.write("/tmp/test_split_short.wav", audio, sr)

    chunks = list(split_at_silence("/tmp/test_split_short.wav", [], max_duration=25.0))
    assert len(chunks) == 1
    assert chunks[0][0] == 0.0
```

**Step 2: Run test to verify failure**

Run: `pytest tests/test_splitter.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write implementation**

```python
# transcriber/splitter.py
"""Audio splitting at silence boundaries.

Takes silence boundaries (from silence.py) and an audio file path,
and yields (start_time_seconds, numpy_array) chunks suitable for
transcription. Each chunk is capped at max_duration to stay under
Whisper's 30-second limit.
"""
import logging
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


def split_at_silence(
    audio_path: str,
    silence_boundaries: list[tuple[float, float]],
    max_duration: float = 25.0,
) -> list[tuple[float, np.ndarray]]:
    """Split audio at silence midpoints, capping each chunk at max_duration.

    Args:
        audio_path: Path to the audio file.
        silence_boundaries: List of (start, end) silence regions from
            find_silence_boundaries(). The audio is split at the midpoint
            of each silence region.
        max_duration: Maximum chunk duration in seconds. Chunks longer
            than this (because no silence was found within the window)
            are split at max_duration intervals.

    Returns:
        List of (start_time_seconds, audio_data) tuples. audio_data is
        a 1D float32 numpy array. Chunks are in chronological order.
    """
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    with sf.SoundFile(audio_path) as f:
        sample_rate = f.samplerate
        total_frames = len(f)
        total_duration = total_frames / sample_rate

        # Read entire file (already memory-safe: per-speaker WAVs are
        # typically < 100 MB even for 3-hour sessions)
        f.seek(0)
        audio = f.read(dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

    # Convert silence boundaries to split points (midpoints of silences)
    split_points: list[float] = []
    for start, end in silence_boundaries:
        midpoint = (start + end) / 2.0
        split_points.append(midpoint)

    # Add forced splits at max_duration intervals where no silence exists
    forced_splits: list[float] = []
    cursor = 0.0
    while cursor < total_duration:
        next_forced = cursor + max_duration
        if next_forced < total_duration:
            # Check if there's a natural split point within [cursor, next_forced]
            has_natural = any(cursor < sp <= next_forced for sp in split_points)
            if not has_natural:
                forced_splits.append(next_forced)
        cursor = next_forced

    # Merge and sort all split points
    all_splits = sorted(set(split_points + forced_splits))

    # Build chunks from split points
    chunks: list[tuple[float, np.ndarray]] = []
    chunk_start = 0.0
    for split_point in all_splits:
        if split_point <= chunk_start:
            continue
        if split_point >= total_duration:
            break
        start_frame = int(chunk_start * sample_rate)
        end_frame = int(split_point * sample_rate)
        chunk_data = audio[start_frame:end_frame]
        if len(chunk_data) > 0:
            chunks.append((chunk_start, chunk_data))
        chunk_start = split_point

    # Final chunk from last split to end
    if chunk_start < total_duration:
        start_frame = int(chunk_start * sample_rate)
        chunk_data = audio[start_frame:]
        if len(chunk_data) > 0:
            chunks.append((chunk_start, chunk_data))

    return chunks
```

**Step 4: Run tests**

Run: `pytest tests/test_splitter.py -v`
Expected: 3 passed

**Step 5: Commit**

```bash
git add transcriber/splitter.py tests/test_splitter.py
git commit -m "feat: add silence-aware audio splitter"
```

---

### Task 3: Refactor worker.py to use silence-aware splitting

**Objective:** Replace the blind 28-second chunking loop in `TranscriptionWorker.transcribe()` with silence-aware splitting. Add filtering for blank/silence segments.

**Files:**
- Modify: `transcriber/worker.py:104-187` (the `transcribe` method)
- Modify: `tests/test_transcriber_worker.py` (update existing tests)

**Step 1: Write failing test for silence-aware behavior**

```python
# Add to tests/test_transcriber_worker.py
def test_transcribe_uses_silence_aware_splitting(monkeypatch, tmp_path):
    """Transcribe splits at silence boundaries, not fixed 28s intervals."""
    import numpy as np
    import soundfile as sf

    # Create a 60s audio file with silence at 20s and 40s
    sr = 16000
    speech = np.random.randn(20 * sr).astype(np.float32) * 0.3
    silence = np.zeros(2 * sr, dtype=np.float32)  # 2s silence
    audio = np.concatenate([speech, silence, speech, silence, speech])
    wav_path = str(tmp_path / "test.wav")
    sf.write(wav_path, audio, sr)

    # Mock silence detection to return boundaries at 20-22s and 42-44s
    from transcriber import silence as silence_mod
    monkeypatch.setattr(
        silence_mod, "find_silence_boundaries",
        lambda path, **kw: [(20.0, 22.0), (42.0, 44.0)],
    )

    worker = TranscriptionWorker(model_path="/fake", model_size="small")
    worker.recognizer = _make_mock_recognizer(["hello world"])

    result = worker.transcribe(wav_path)
    assert result is not None
    # Should have 3 segments (one per silence-delimited chunk)
    assert len(result.segments) == 3
    # Timestamps should reflect actual chunk starts (0, ~22, ~44)
    assert result.segments[0].start_time == 0.0
    assert result.segments[1].start_time > 20.0
    assert result.segments[2].start_time > 42.0
```

**Step 2: Run test to verify failure**

Run: `pytest tests/test_transcriber_worker.py::test_transcribe_uses_silence_aware_splitting -v`
Expected: FAIL — current code uses fixed chunking

**Step 3: Refactor transcribe() method**

Replace the chunking loop in `transcriber/worker.py` lines 121-174 with:

```python
def transcribe(self, audio_path: str, hotwords: Optional[str] = None) -> Optional[TranscriptionResult]:
    """Transcribe an audio file using silence-aware splitting.

    Instead of blind 28-second chunks, detects silence boundaries and
    splits audio at natural pause points. Each segment is transcribed
    independently with timestamps offset to the segment's position in
    the full file.

    Segments that produce only [BLANK_AUDIO] or empty text are filtered
    out to avoid cluttering the transcript with silence.
    """
    if not self.recognizer:
        raise RuntimeError("Model not loaded. Call load_model() first.")

    try:
        from transcriber.silence import find_silence_boundaries
        from transcriber.splitter import split_at_silence

        # Detect silence boundaries for natural splitting
        boundaries = find_silence_boundaries(audio_path)
        logger.debug("Found %d silence boundaries in %s", len(boundaries), audio_path)

        # Split audio at silence midpoints, capping at 25s
        chunks = split_at_silence(audio_path, boundaries, max_duration=25.0)
        logger.debug("Split into %d chunks for transcription", len(chunks))

        all_text_parts: list[str] = []
        all_segments: list[TranscriptSegment] = []

        for chunk_start_sec, chunk_audio in chunks:
            if len(chunk_audio) < 0.1 * 16000:  # < 0.1s at 16kHz
                continue

            # Check if chunk is mostly silence (RMS below threshold)
            rms = (chunk_audio ** 2).mean() ** 0.5
            if rms < 0.001:  # ~-60 dB, effectively silent
                logger.debug("Skipping silent chunk at %.1fs", chunk_start_sec)
                continue

            stream = self.recognizer.create_stream(hotwords=hotwords or None)
            stream.accept_waveform(16000, chunk_audio)
            self.recognizer.decode_stream(stream)

            result = stream.result
            chunk_text = result.text.strip()

            # Filter out blank/silence transcripts
            if not chunk_text or chunk_text.upper() in ("[BLANK_AUDIO]", "[blank_audio]", ""):
                continue

            all_text_parts.append(chunk_text)

            chunk_segments = self._build_segments(result)
            for seg in chunk_segments:
                # Filter blank audio segments
                if seg.text.upper() in ("[BLANK_AUDIO]", "[blank_audio]", ""):
                    continue
                all_segments.append(TranscriptSegment(
                    start_time=seg.start_time + chunk_start_sec,
                    text=seg.text,
                ))

        text = " ".join(all_text_parts)
        logger.debug(
            "Transcribed %s: %d chars, %d segments (silence-aware split)",
            audio_path, len(text), len(all_segments),
        )
        return TranscriptionResult(text=text, segments=all_segments)

    except Exception as e:
        logger.error("Transcription failed for %s: %s", audio_path, e)
        return None
```

**Step 4: Run all worker tests**

Run: `pytest tests/test_transcriber_worker.py -v`
Expected: All pass (update existing tests that mock the old chunking behavior)

**Step 5: Commit**

```bash
git add transcriber/worker.py tests/test_transcriber_worker.py
git commit -m "feat: replace blind 28s chunking with silence-aware splitting in transcriber"
```

---

### Task 4: Filter [BLANK_AUDIO] in transcript builder

**Objective:** Add a safety net in `_build_interleaved_transcript` to skip segments with blank/silence text, even if they somehow make it into the database.

**Files:**
- Modify: `transcriber/main.py:58-77` (`_build_interleaved_transcript`)
- Test: `tests/test_transcript_merge.py` (add test for blank filtering)

**Step 1: Write failing test**

```python
# Add to tests/test_transcript_merge.py
def test_blank_audio_segments_filtered():
    """Segments with [BLANK_AUDIO] text are excluded from transcript."""
    # Mock DB to return segments including blank ones
    mock_db = Mock()
    mock_db.get_transcript_segments.return_value = [
        {"start_time": 0.0, "text": "Hello world", "speaker_name": "Alice", "discord_user_id": "123"},
        {"start_time": 28.0, "text": "[BLANK_AUDIO]", "speaker_name": "Alice", "discord_user_id": "123"},
        {"start_time": 56.0, "text": "How are you?", "speaker_name": "Alice", "discord_user_id": "123"},
    ]
    from transcriber.main import _build_interleaved_transcript
    result = _build_interleaved_transcript(mock_db, "session-1")
    assert "[BLANK_AUDIO]" not in result
    assert "Hello world" in result
    assert "How are you?" in result
```

**Step 2: Run test to verify failure**

Run: `pytest tests/test_transcript_merge.py::test_blank_audio_segments_filtered -v`
Expected: FAIL

**Step 3: Add filter to _build_interleaved_transcript**

In `transcriber/main.py`, modify the segment loop:

```python
def _build_interleaved_transcript(db: Database, session_id: str) -> str:
    segments = db.get_transcript_segments(session_id)

    if segments:
        lines = []
        for seg in segments:
            text = seg["text"].strip()
            # Filter out blank/silence transcripts (safety net)
            if not text or text.upper() in ("[BLANK_AUDIO]", "[BLANK_AUDIO]"):
                continue
            ts = _format_timestamp(seg["start_time"])
            speaker = seg.get("speaker_name") or seg.get("discord_user_id") or "Unknown"
            lines.append(f"[{ts}] {speaker}: {text}")
        return "\n".join(lines) + "\n" if lines else ""

    # ... fallback path unchanged ...
```

**Step 4: Run tests**

Run: `pytest tests/test_transcript_merge.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add transcriber/main.py tests/test_transcript_merge.py
git commit -m "feat: filter [BLANK_AUDIO] segments from merged transcript"
```

---

### Task 5: Update documentation

**Objective:** Update ARCHITECTURE.md and KNOWN_ISSUES.md to reflect the new silence-aware chunking approach.

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/KNOWN_ISSUES.md`

**Step 1: Update ARCHITECTURE.md**

In the "Transcriber" section, replace the chunking description:

```markdown
### Audio Splitting

The transcriber uses **silence-aware splitting** instead of fixed-size chunking:

1. `transcriber/silence.py` runs ffmpeg's `silencedetect` filter on each audio file to find regions of silence (default: > 0.8s below -40 dB).
2. `transcriber/splitter.py` splits the audio at the midpoint of each silence region, producing natural speech-boundary segments.
3. Each segment is capped at 25 seconds (Whisper's limit is 30s) — if no silence is found within a 25s window, a forced split is inserted.
4. Segments that are pure silence (RMS < -60 dB) or produce only `[BLANK_AUDIO]` text are filtered out before storage.

This eliminates the mid-sentence cuts and out-of-order transcript lines that occurred with the previous blind 28-second chunking approach.
```

**Step 2: Update KNOWN_ISSUES.md**

Mark the chunking issue as resolved:

```markdown
## ~~Whisper timestamp misordering due to blind chunking~~ [RESOLVED]

**Symptom:** Transcript lines appear out of chronological order, with blocks of entries at exact 28-second intervals.

**Root cause:** The transcriber split audio into fixed 28-second chunks regardless of speech boundaries. When speech spanned a chunk boundary, Whisper produced segments with timestamps that didn't align with actual speech, and silence produced `[BLANK_AUDIO]` entries that cluttered the transcript.

**Fix:** Replaced blind chunking with silence-aware splitting (`transcriber/silence.py` + `transcriber/splitter.py`). Audio is now split at natural pause points (silence midpoints), with a 25-second cap. Blank/silence segments are filtered before storage and again during transcript merging.

**Status:** Resolved in commit <TBD>.
```

**Step 3: Commit**

```bash
git add docs/ARCHITECTURE.md docs/KNOWN_ISSUES.md
git commit -m "docs: update architecture and known issues for silence-aware chunking"
```

---

### Task 6: Integration test with real-ish audio

**Objective:** End-to-end test that creates a WAV with known speech/silence pattern, runs the full transcribe pipeline, and verifies the output segments are in order and contain no [BLANK_AUDIO].

**Files:**
- Create: `tests/test_integration_silence_chunking.py`

**Step 1: Write integration test**

```python
# tests/test_integration_silence_chunking.py
"""Integration test: silence-aware chunking end-to-end."""
import numpy as np
import soundfile as sf
import pytest
from unittest.mock import patch, MagicMock

from transcriber.worker import TranscriptionWorker, TranscriptionResult


def test_end_to_end_silence_aware(tmp_path):
    """Full pipeline: WAV with speech+silence -> ordered segments, no blanks."""
    sr = 16000
    # 10s speech, 2s silence, 10s speech, 2s silence, 10s speech = 34s
    speech1 = np.random.randn(10 * sr).astype(np.float32) * 0.3
    silence = np.zeros(2 * sr, dtype=np.float32)
    speech2 = np.random.randn(10 * sr).astype(np.float32) * 0.3
    speech3 = np.random.randn(10 * sr).astype(np.float32) * 0.3
    audio = np.concatenate([speech1, silence, speech2, silence, speech3])

    wav_path = str(tmp_path / "integration.wav")
    sf.write(wav_path, audio, sr)

    worker = TranscriptionWorker(model_path="/fake", model_size="small")

    # Mock the recognizer to return text for each chunk
    mock_recognizer = MagicMock()
    call_count = [0]
    def mock_create_stream(**kw):
        stream = MagicMock()
        def accept_and_decode():
            pass
        stream.accept_waveform = MagicMock()
        stream.result = MagicMock()
        # Return different text for each call
        texts = ["First segment", "Second segment", "Third segment"]
        idx = min(call_count[0], len(texts) - 1)
        stream.result.text = texts[idx]
        stream.result.segment_texts = [texts[idx]]
        stream.result.segment_timestamps = [0.0]
        call_count[0] += 1
        return stream

    mock_recognizer.create_stream = mock_create_stream
    mock_recognizer.decode_stream = MagicMock()
    worker.recognizer = mock_recognizer

    result = worker.transcribe(wav_path)

    assert result is not None
    assert len(result.segments) == 3
    # Verify chronological order
    for i in range(len(result.segments) - 1):
        assert result.segments[i].start_time < result.segments[i + 1].start_time
    # Verify no blank audio
    for seg in result.segments:
        assert "[BLANK_AUDIO]" not in seg.text.upper()
```

**Step 2: Run integration test**

Run: `pytest tests/test_integration_silence_chunking.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_integration_silence_chunking.py
git commit -m "test: add integration test for silence-aware chunking pipeline"
```

---

## Files Changed Summary

| File | Action |
|------|--------|
| `transcriber/silence.py` | Create — silence detection via ffmpeg |
| `transcriber/splitter.py` | Create — silence-aware audio splitting |
| `transcriber/worker.py` | Modify — replace blind chunking with silence-aware |
| `transcriber/main.py` | Modify — filter [BLANK_AUDIO] in transcript builder |
| `tests/test_silence.py` | Create — silence detection tests |
| `tests/test_splitter.py` | Create — splitter tests |
| `tests/test_transcriber_worker.py` | Modify — update for new chunking |
| `tests/test_transcript_merge.py` | Modify — add blank filter test |
| `tests/test_integration_silence_chunking.py` | Create — end-to-end test |
| `docs/ARCHITECTURE.md` | Modify — document new approach |
| `docs/KNOWN_ISSUES.md` | Modify — mark issue resolved |

## Risks and Mitigations

1. **ffmpeg availability**: The container must have ffmpeg installed. Mitigation: check at startup, fall back to fixed chunking if unavailable.
2. **Silence detection false positives**: Aggressive silence detection could split mid-sentence. Mitigation: default threshold (-40 dB) and minimum duration (0.8s) are conservative.
3. **Performance**: `silencedetect` adds a preprocessing pass. Mitigation: it's fast (real-time factor < 0.1) and only runs once per file.
4. **Backward compatibility**: Existing queued files will use the new splitting. Mitigation: no schema changes, fully backward compatible.

## Verification

After implementation, verify with:
```bash
# Run full test suite
pytest tests/ -v

# Check that the attached transcript's pattern no longer occurs:
# - No blocks of entries at exact 28s intervals
# - No [BLANK_AUDIO] entries
# - Chronological ordering maintained
```
