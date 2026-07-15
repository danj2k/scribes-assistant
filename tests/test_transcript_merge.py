"""Tests for transcript merge logic in transcriber.main.

Tests the _format_timestamp helper, _build_interleaved_transcript merge
function (both timestamp-based and fallback modes), and _build_segments
in worker.py.
"""

import pytest
from unittest.mock import MagicMock
from transcriber.worker import TranscriptionWorker, TranscriptSegment
from transcriber.main import _format_timestamp, _build_interleaved_transcript
from shared.database import Database


@pytest.fixture
def db(tmp_path):
    """Create a fresh test database."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    yield database
    database.close()


class TestFormatTimestamp:
    """Tests for _format_timestamp."""

    def test_zero(self):
        assert _format_timestamp(0.0) == "00:00:00"

    def test_seconds(self):
        assert _format_timestamp(5.0) == "00:00:05"

    def test_minutes(self):
        assert _format_timestamp(65.0) == "00:01:05"

    def test_hours(self):
        assert _format_timestamp(3661.0) == "01:01:01"

    def test_truncates_fractional(self):
        assert _format_timestamp(3.9) == "00:00:03"


class TestBuildInterleavedTranscript:
    """Tests for _build_interleaved_transcript."""

    def test_with_segments(self, db):
        """Timestamped segments are interleaved by start_time."""
        db.create_session("s1", "g", "c")
        db.add_audio_file("s1", "/tmp/a.wav", discord_user_id="100", speaker_name="Alice")
        db.add_audio_file("s1", "/tmp/b.wav", discord_user_id="200", speaker_name="Bob")

        # Insert out of order to verify merge sorts by start_time
        db.add_transcript_segment("s1", file_id=1, start_time=10.0, text="Alice later", seq=0)
        db.add_transcript_segment("s1", file_id=2, start_time=2.0, text="Bob early", seq=0)
        db.add_transcript_segment("s1", file_id=1, start_time=5.0, text="Alice mid", seq=1)

        result = _build_interleaved_transcript(db, "s1")
        lines = result.rstrip("\n").split("\n")
        assert lines[0] == "[00:00:02] Bob: Bob early"
        assert lines[1] == "[00:00:05] Alice: Alice mid"
        assert lines[2] == "[00:00:10] Alice: Alice later"
        assert result.endswith("\n")

    def test_no_segments_fallback_to_per_speaker(self, db):
        """When no segments exist, falls back to per-speaker blocks."""
        db.create_session("s1", "g", "c")
        db.add_audio_file("s1", "/tmp/a.wav", discord_user_id="100", speaker_name="Alice")
        db.add_audio_file("s1", "/tmp/b.wav", discord_user_id="200", speaker_name="Bob")
        db.add_transcript(1, "Alice said something.")
        db.add_transcript(2, "Bob replied.")

        result = _build_interleaved_transcript(db, "s1")
        # Speaker name on same line as dialogue (no newline after colon)
        assert "Alice: Alice said something." in result
        assert "Bob: Bob replied." in result
        # No newline between speaker name and dialogue
        assert "Alice:\n" not in result
        assert "Bob:\n" not in result
        # Trailing newline
        assert result.endswith("\n")

    def test_no_segments_no_transcripts(self, db):
        """No segments and no transcripts returns empty string."""
        db.create_session("s1", "g", "c")
        db.add_audio_file("s1", "/tmp/a.wav")
        result = _build_interleaved_transcript(db, "s1")
        assert result == ""

    def test_no_files(self, db):
        """No audio files at all returns empty string."""
        db.create_session("s1", "g", "c")
        result = _build_interleaved_transcript(db, "s1")
        assert result == ""

    def test_missing_speaker_name_uses_user_id(self, db):
        """When speaker_name is None, falls back to discord_user_id."""
        db.create_session("s1", "g", "c")
        db.add_audio_file("s1", "/tmp/a.wav", discord_user_id="42", speaker_name=None)
        db.add_transcript_segment("s1", file_id=1, start_time=1.0, text="Hello", seq=0)
        result = _build_interleaved_transcript(db, "s1")
        assert "[00:00:01] 42: Hello" in result

    def test_missing_both_speaker_and_id(self, db):
        """When both speaker_name and discord_user_id are None, uses 'Unknown'."""
        db.create_session("s1", "g", "c")
        db.add_audio_file("s1", "/tmp/a.wav")
        db.add_transcript_segment("s1", file_id=1, start_time=1.0, text="Hello", seq=0)
        result = _build_interleaved_transcript(db, "s1")
        assert "[00:00:01] Unknown: Hello" in result


class TestBuildSegments:
    """Tests for TranscriptionWorker._build_segments.

    _build_segments now reads result.segment_texts and result.segment_timestamps
    (populated by enable_segment_timestamps=True) rather than the old token-level
    result.tokens/result.timestamps fields.
    """

    def test_no_segment_data_returns_empty(self):
        """No segment timestamp data returns empty list (fallback path)."""
        result = MagicMock()
        result.segment_texts = None
        result.segment_timestamps = None
        segs = TranscriptionWorker._build_segments(result)
        assert segs == []

    def test_empty_segments_returns_empty(self):
        """Empty segment lists return empty."""
        result = MagicMock()
        result.segment_texts = []
        result.segment_timestamps = []
        segs = TranscriptionWorker._build_segments(result)
        assert segs == []

    def test_mismatched_lengths_returns_empty(self):
        """Mismatched segment_texts/segment_timestamps lengths return empty."""
        result = MagicMock()
        result.segment_texts = ["Hello world.", "Bye."]
        result.segment_timestamps = [1.0]
        segs = TranscriptionWorker._build_segments(result)
        assert segs == []

    def test_single_segment(self):
        """A single segment produces one TranscriptSegment."""
        result = MagicMock()
        result.segment_texts = ["Hello world."]
        result.segment_timestamps = [0.5]
        segs = TranscriptionWorker._build_segments(result)
        assert len(segs) == 1
        assert segs[0].start_time == 0.5
        assert segs[0].text == "Hello world."

    def test_multiple_segments(self):
        """Multiple segments produce multiple TranscriptSegments."""
        result = MagicMock()
        result.segment_texts = ["Hi.", "Bye."]
        result.segment_timestamps = [1.0, 3.0]
        segs = TranscriptionWorker._build_segments(result)
        assert len(segs) == 2
        assert segs[0].start_time == 1.0
        assert segs[0].text == "Hi."
        assert segs[1].start_time == 3.0
        assert segs[1].text == "Bye."

    def test_strips_whitespace(self):
        """Leading/trailing whitespace is stripped from segment text."""
        result = MagicMock()
        result.segment_texts = ["  Hello world.  "]
        result.segment_timestamps = [0.0]
        segs = TranscriptionWorker._build_segments(result)
        assert len(segs) == 1
        assert segs[0].text == "Hello world."

    def test_skips_empty_text_segments(self):
        """Segments with only whitespace are skipped."""
        result = MagicMock()
        result.segment_texts = ["Hello.", "   ", "World."]
        result.segment_timestamps = [1.0, 2.0, 3.0]
        segs = TranscriptionWorker._build_segments(result)
        assert len(segs) == 2
        assert segs[0].text == "Hello."
        assert segs[1].text == "World."
