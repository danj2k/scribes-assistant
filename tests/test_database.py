"""Tests for shared.database — SQLite session and lexicon storage."""

import pytest
from shared.database import Database, STATUS_QUEUED, STATUS_RECORDING, STATUS_COMPLETE


@pytest.fixture
def db(tmp_path):
    """Create a fresh test database."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    yield database
    database.close()


def _make_session(db, sid="sess1", guild="123456", channel="789012"):
    """Helper: create a session and return its ID."""
    db.create_session(sid, guild, channel)
    return sid


class TestSessionManagement:
    """Tests for session CRUD operations."""

    def test_create_session(self, db):
        """A session can be created with session/guild/channel IDs."""
        _make_session(db)
        session = db.get_session("sess1")
        assert session is not None
        assert session["discord_guild_id"] == "123456"
        assert session["status"] == STATUS_RECORDING

    def test_get_session(self, db):
        """A created session can be retrieved."""
        _make_session(db)
        session = db.get_session("sess1")
        assert session is not None
        assert session["discord_channel_id"] == "789012"

    def test_update_session_status(self, db):
        """Session status can be updated."""
        _make_session(db)
        db.update_session_status("sess1", STATUS_COMPLETE)
        session = db.get_session("sess1")
        assert session["status"] == STATUS_COMPLETE

    def test_end_session(self, db):
        """Ending a session sets ended_at timestamp and queues for transcription."""
        _make_session(db)
        db.end_session("sess1")
        session = db.get_session("sess1")
        assert session["ended_at"] is not None
        assert session["status"] == STATUS_QUEUED

    def test_set_thread_id(self, db):
        """Thread ID can be stored on a session."""
        _make_session(db)
        db.set_thread_id("sess1", "999888")
        session = db.get_session("sess1")
        assert session["thread_id"] == "999888"

    def test_get_active_session(self, db):
        """get_active_session finds a non-ended session for a guild."""
        _make_session(db, sid="sess1", guild="111")
        active = db.get_active_session("111")
        assert active is not None
        assert active["id"] == "sess1"

    def test_get_active_session_none(self, db):
        """get_active_session returns None when no active session exists."""
        active = db.get_active_session("999999")
        assert active is None

    def test_get_queued_sessions(self, db):
        """Queued sessions are returned."""
        _make_session(db, sid="s1", guild="111", channel="222")
        _make_session(db, sid="s2", guild="333", channel="444")
        # Sessions start as recording; explicitly set to queued for this test
        db.update_session_status("s1", STATUS_QUEUED)
        db.update_session_status("s2", STATUS_QUEUED)
        queued = db.get_queued_sessions()
        ids = [s["id"] for s in queued]
        assert "s1" in ids
        assert "s2" in ids


class TestAudioFiles:
    """Tests for audio file tracking."""

    def test_add_audio_file(self, db):
        """Audio files can be added to a session."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/chunk1.opus", size_bytes=1024)
        files = db.get_audio_files("sess1")
        assert len(files) == 1
        assert files[0]["file_path"] == "/tmp/chunk1.opus"

    def test_get_audio_files_multiple(self, db):
        """Multiple audio files can be retrieved."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/chunk1.opus", 512)
        db.add_audio_file("sess1", "/tmp/chunk2.opus", 1024)
        files = db.get_audio_files("sess1")
        assert len(files) == 2


class TestLexicon:
    """Tests for lexicon storage in the database."""

    def test_lexicon_add(self, db):
        """Terms can be added to the lexicon."""
        result = db.lexicon_add("Theron", "A noble elf name", "test_user")
        assert result is True

    def test_lexicon_list(self, db):
        """Added terms appear in the lexicon list."""
        db.lexicon_add("Theron", "A noble elf name", "test_user")
        terms = db.lexicon_list()
        assert len(terms) == 1
        assert terms[0]["term"] == "Theron"

    def test_lexicon_get_terms(self, db):
        """lexicon_get_terms returns just the term strings."""
        db.lexicon_add("Theron", "Elf name", "user")
        db.lexicon_add("Grimjaw", "Dwarf name", "user")
        terms = db.lexicon_get_terms()
        assert "Theron" in terms
        assert "Grimjaw" in terms

    def test_lexicon_remove(self, db):
        """Terms can be removed from the lexicon."""
        db.lexicon_add("Theron", "Elf name", "user")
        result = db.lexicon_remove("Theron")
        assert result is True
        terms = db.lexicon_list()
        assert len(terms) == 0

    def test_lexicon_remove_nonexistent(self, db):
        """Removing a non-existent term returns False."""
        result = db.lexicon_remove("Nobody")
        assert result is False


class TestTranscriptSegments:
    """Tests for transcript_segments table and related methods."""

    def test_add_transcript_segment(self, db):
        """A segment can be inserted and retrieved."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav", discord_user_id="100", speaker_name="Theron")
        db.add_transcript_segment("sess1", file_id=1, start_time=3.5, text="Hello world", seq=0)
        segs = db.get_transcript_segments("sess1")
        assert len(segs) == 1
        assert segs[0]["text"] == "Hello world"
        assert segs[0]["start_time"] == 3.5
        assert segs[0]["speaker_name"] == "Theron"

    def test_get_transcript_segments_ordered_by_time(self, db):
        """Segments are returned sorted by start_time, then seq."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav", discord_user_id="100", speaker_name="Alice")
        db.add_audio_file("sess1", "/tmp/b.wav", discord_user_id="200", speaker_name="Bob")

        # Insert out of time order to verify sorting
        db.add_transcript_segment("sess1", file_id=1, start_time=10.0, text="Alice later", seq=0)
        db.add_transcript_segment("sess1", file_id=2, start_time=2.0, text="Bob early", seq=0)
        db.add_transcript_segment("sess1", file_id=1, start_time=5.0, text="Alice mid", seq=1)

        segs = db.get_transcript_segments("sess1")
        assert len(segs) == 3
        assert segs[0]["start_time"] == 2.0
        assert segs[0]["speaker_name"] == "Bob"
        assert segs[1]["start_time"] == 5.0
        assert segs[1]["speaker_name"] == "Alice"
        assert segs[2]["start_time"] == 10.0

    def test_get_transcript_segments_empty(self, db):
        """No segments returns empty list."""
        _make_session(db)
        segs = db.get_transcript_segments("sess1")
        assert segs == []

    def test_get_transcript_segments_includes_speaker(self, db):
        """Retrieved segments include speaker_name from joined audio_files."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav", discord_user_id="42", speaker_name="Gandalf")
        db.add_transcript_segment("sess1", file_id=1, start_time=0.0, text="You shall not pass!", seq=0)
        segs = db.get_transcript_segments("sess1")
        assert segs[0]["speaker_name"] == "Gandalf"
        assert segs[0]["discord_user_id"] == "42"


class TestSessionFileStatus:
    """Tests for get_session_file_status."""

    def test_status_all_queued(self, db):
        """All files queued."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav")
        db.add_audio_file("sess1", "/tmp/b.wav")
        status = db.get_session_file_status("sess1")
        assert status["total"] == 2
        assert status["queued"] == 2
        assert status["transcribing"] == 0
        assert status["transcribed"] == 0

    def test_status_mixed(self, db):
        """Mixed statuses are counted correctly."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav")  # queued
        db.add_audio_file("sess1", "/tmp/b.wav")  # will be transcribed
        db.update_file_status(2, "transcribing")
        db.add_transcript(2, "hello")  # sets to transcribed
        status = db.get_session_file_status("sess1")
        assert status["total"] == 2
        assert status["queued"] == 1
        assert status["transcribing"] == 0
        assert status["transcribed"] == 1

    def test_status_with_failed(self, db):
        """Failed files are counted."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav")
        db.add_audio_file("sess1", "/tmp/b.wav")
        db.update_file_status(1, "failed")
        status = db.get_session_file_status("sess1")
        assert status["total"] == 2
        assert status["failed"] == 1
        assert status["queued"] == 1

    def test_status_empty_session(self, db):
        """No files returns all zeros."""
        _make_session(db)
        status = db.get_session_file_status("sess1")
        assert status["total"] == 0
        assert status["queued"] == 0


class TestAudioFileSpeakerFields:
    """Tests for discord_user_id and speaker_name on audio_files."""

    def test_add_audio_file_with_speaker(self, db):
        """Audio file stores discord_user_id and speaker_name."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav", discord_user_id="123", speaker_name="Theron")
        files = db.get_audio_files("sess1")
        assert files[0]["discord_user_id"] == "123"
        assert files[0]["speaker_name"] == "Theron"

    def test_add_audio_file_without_speaker(self, db):
        """Audio file works without speaker fields (defaults to None)."""
        _make_session(db)
        db.add_audio_file("sess1", "/tmp/a.wav")
        files = db.get_audio_files("sess1")
        assert files[0]["discord_user_id"] is None
        assert files[0]["speaker_name"] is None


class TestDatabaseLifecycle:
    """Tests for database connection lifecycle."""

    def test_close(self, db):
        """Closing the database does not raise."""
        db.close()

    def test_close_multiple_times(self, db):
        """Closing an already-closed database is safe."""
        db.close()
        db.close()  # Should not raise
