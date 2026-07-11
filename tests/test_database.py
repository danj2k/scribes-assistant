"""Tests for shared.database — SQLite session and lexicon storage."""

import pytest
from shared.database import Database, STATUS_QUEUED, STATUS_RECORDING, STATUS_COMPLETE, STATUS_FAILED


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

    def test_get_any_active_session_finds_across_guilds(self, db):
        """get_any_active_session finds an active session regardless of guild."""
        _make_session(db, sid="sess1", guild="111", channel="222")
        active = db.get_any_active_session()
        assert active is not None
        assert active["id"] == "sess1"

    def test_get_any_active_session_none(self, db):
        """get_any_active_session returns None when no session is active."""
        active = db.get_any_active_session()
        assert active is None

    def test_get_any_active_session_blocks_different_guild(self, db):
        """Regression for Bug #9: an active session in guild A blocks
        /start in guild B. This bot serves one D&D group, so only one
        session should run at a time globally.
        """
        _make_session(db, sid="sess1", guild="111", channel="222")
        # A session is active in guild 111 — get_any_active_session must
        # return it even when queried from the context of guild 222.
        active = db.get_any_active_session()
        assert active is not None
        assert active["discord_guild_id"] == "111"

    def test_get_any_active_session_excludes_ended(self, db):
        """Ended sessions are not returned by get_any_active_session."""
        _make_session(db, sid="sess1", guild="111", channel="222")
        db.end_session("sess1")
        assert db.get_any_active_session() is None

    def test_get_any_active_session_excludes_failed(self, db):
        """Failed sessions are not returned by get_any_active_session."""
        _make_session(db, sid="sess1", guild="111", channel="222")
        db.fail_session("sess1")
        assert db.get_any_active_session() is None

    def test_single_session_allows_new_after_previous_ends(self, db):
        """After a session ends, a new session can be started in any guild."""
        _make_session(db, sid="sess1", guild="111", channel="222")
        db.end_session("sess1")
        assert db.get_any_active_session() is None

        _make_session(db, sid="sess2", guild="333", channel="444")
        active = db.get_any_active_session()
        assert active is not None
        assert active["id"] == "sess2"

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

    def test_get_sessions_for_delivery_returns_complete_with_transcript(self, db):
        """Only complete sessions with a transcript path and no thread_id are returned."""
        _make_session(db, sid="s1", guild="111", channel="222")
        db.end_session("s1")
        db.set_transcript_path("s1", "/data/transcripts/s1.txt")
        sessions = db.get_sessions_for_delivery()
        ids = [s["id"] for s in sessions]
        assert "s1" in ids

    def test_get_sessions_for_delivery_excludes_undelivered_without_transcript(self, db):
        """Sessions without a transcript path are not returned."""
        _make_session(db, sid="s1", guild="111", channel="222")
        db.end_session("s1")
        sessions = db.get_sessions_for_delivery()
        assert sessions == []

    def test_get_sessions_for_delivery_excludes_already_delivered(self, db):
        """Sessions that already have a thread_id are not returned."""
        _make_session(db, sid="s1", guild="111", channel="222")
        db.end_session("s1")
        db.set_transcript_path("s1", "/data/transcripts/s1.txt")
        db.set_thread_id("s1", "thread123")
        sessions = db.get_sessions_for_delivery()
        assert sessions == []

    def test_get_sessions_for_delivery_excludes_incomplete(self, db):
        """Sessions that are not complete are not returned."""
        _make_session(db, sid="s1", guild="111", channel="222")
        # Session is still recording (status = RECORDING)
        sessions = db.get_sessions_for_delivery()
        assert sessions == []

    def test_get_sessions_for_delivery_ordered_by_ended_at(self, db):
        """Oldest completed sessions are delivered first."""
        _make_session(db, sid="s1", guild="111", channel="222")
        db.end_session("s1")
        db.set_transcript_path("s1", "/data/transcripts/s1.txt")

        _make_session(db, sid="s2", guild="333", channel="444")
        db.end_session("s2")
        db.set_transcript_path("s2", "/data/transcripts/s2.txt")

        sessions = db.get_sessions_for_delivery()
        ids = [s["id"] for s in sessions]
        assert ids == ["s1", "s2"]


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


class TestFailSession:
    """Tests for fail_session and get_queued_sessions_without_files."""

    def test_fail_session_sets_status_and_ended_at(self, db):
        """fail_session marks the session as failed and sets ended_at."""
        _make_session(db)
        assert db.get_session("sess1")["ended_at"] is None  # still recording

        db.fail_session("sess1")
        session = db.get_session("sess1")
        assert session["status"] == STATUS_FAILED
        assert session["ended_at"] is not None

    def test_fail_session_clears_active(self, db):
        """A failed session is no longer returned by get_active_session."""
        _make_session(db, sid="sess1", guild="111")
        assert db.get_active_session("111") is not None

        db.fail_session("sess1")
        assert db.get_active_session("111") is None

    def test_update_status_failed_does_not_clear_active(self, db):
        """Regression for Bug #8: update_session_status(FAILED) does NOT set
        ended_at, so the session stays visible to get_active_session().

        This is the bug: start_command used update_session_status(STATUS_FAILED)
        on voice join failure, leaving ended_at=NULL. get_active_session()
        filters on ended_at IS NULL, so the dead session blocked all future
        /start commands in the guild. The fix uses fail_session() which sets
        both status and ended_at.
        """
        _make_session(db, sid="sess1", guild="111")

        # The wrong approach (what the bug did):
        db.update_session_status("sess1", STATUS_FAILED)
        assert db.get_session("sess1")["status"] == STATUS_FAILED
        assert db.get_session("sess1")["ended_at"] is None  # the bug!
        assert db.get_active_session("111") is not None  # still blocks!

        # The correct approach (what the fix does):
        db.fail_session("sess1")
        assert db.get_session("sess1")["status"] == STATUS_FAILED
        assert db.get_session("sess1")["ended_at"] is not None
        assert db.get_active_session("111") is None  # no longer blocks

    def test_failed_voice_join_allows_new_session(self, db):
        """Regression for Bug #8: after a failed voice join (fail_session),
        a new /start in the same guild should succeed — get_active_session
        must not return the dead session.
        """
        # First session: voice join fails, fail_session is called
        _make_session(db, sid="sess1", guild="111")
        db.fail_session("sess1")
        assert db.get_active_session("111") is None

        # Second session in the same guild should work
        _make_session(db, sid="sess2", guild="111")
        active = db.get_active_session("111")
        assert active is not None
        assert active["id"] == "sess2"

    def test_get_queued_sessions_without_files_finds_empty(self, db):
        """A queued session with zero audio files is returned."""
        _make_session(db, sid="s1")
        db.end_session("s1")  # sets to QUEUED, no audio files added

        result = db.get_queued_sessions_without_files()
        assert len(result) == 1
        assert result[0]["id"] == "s1"

    def test_get_queued_sessions_without_files_excludes_with_files(self, db):
        """A queued session that has audio files is NOT returned."""
        _make_session(db, sid="s1")
        db.add_audio_file("s1", "/tmp/a.wav")
        db.end_session("s1")

        result = db.get_queued_sessions_without_files()
        assert len(result) == 0

    def test_get_queued_sessions_without_files_excludes_non_queued(self, db):
        """Sessions not in QUEUED status are excluded (even with no files)."""
        _make_session(db, sid="s1")  # still RECORDING, no files

        result = db.get_queued_sessions_without_files()
        assert len(result) == 0

    def test_get_queued_sessions_without_files_excludes_failed(self, db):
        """A failed session with no files is not returned (already handled)."""
        _make_session(db, sid="s1")
        db.fail_session("s1")

        result = db.get_queued_sessions_without_files()
        assert len(result) == 0

    def test_get_queued_sessions_without_files_multiple(self, db):
        """Multiple empty queued sessions are all returned."""
        _make_session(db, sid="s1", guild="111", channel="222")
        _make_session(db, sid="s2", guild="333", channel="444")
        _make_session(db, sid="s3", guild="555", channel="666")

        db.end_session("s1")
        db.end_session("s2")
        db.add_audio_file("s3", "/tmp/a.wav")
        db.end_session("s3")

        result = db.get_queued_sessions_without_files()
        ids = [r["id"] for r in result]
        assert "s1" in ids
        assert "s2" in ids
        assert "s3" not in ids


class TestDatabaseLifecycle:
    """Tests for database connection lifecycle."""

    def test_close(self, db):
        """Closing the database does not raise."""
        db.close()

    def test_close_multiple_times(self, db):
        """Closing an already-closed database is safe."""
        db.close()
        db.close()  # Should not raise
