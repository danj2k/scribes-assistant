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
        """Ending a session sets ended_at timestamp and status to complete."""
        _make_session(db)
        db.end_session("sess1")
        session = db.get_session("sess1")
        assert session["ended_at"] is not None
        assert session["status"] == STATUS_COMPLETE

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


class TestDatabaseLifecycle:
    """Tests for database connection lifecycle."""

    def test_close(self, db):
        """Closing the database does not raise."""
        db.close()

    def test_close_multiple_times(self, db):
        """Closing an already-closed database is safe."""
        db.close()
        db.close()  # Should not raise
