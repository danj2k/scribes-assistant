"""SQLite database for sessions, audio files, and lexicon storage.

Uses WAL mode for concurrent access between the bot and transcriber.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

STATUS_QUEUED = "queued"
STATUS_RECORDING = "recording"
STATUS_TRANSCRIBING = "transcribing"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"


class Database:
    """SQLite-backed storage for session metadata and lexicon terms."""

    def __init__(self, db_path: str = "/data/queue.db"):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    # -- schema --------------------------------------------------------------

    def _init_schema(self):
        with self._cursor() as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id                TEXT PRIMARY KEY,
                    discord_guild_id  TEXT NOT NULL,
                    discord_channel_id TEXT NOT NULL,
                    thread_id         TEXT,
                    status            TEXT NOT NULL DEFAULT 'recording',
                    started_at        TEXT NOT NULL,
                    ended_at          TEXT,
                    transcript_path   TEXT
                );

                CREATE TABLE IF NOT EXISTS audio_files (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    file_path  TEXT NOT NULL,
                    size_bytes INTEGER,
                    status     TEXT NOT NULL DEFAULT 'queued',
                    transcript_text TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS lexicon (
                    term        TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    added_by    TEXT,
                    added_when  TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # -- session CRUD --------------------------------------------------------

    def create_session(self, session_id: str, guild_id: str, channel_id: str):
        """Create a new session in RECORDING status."""
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO sessions (id, discord_guild_id, discord_channel_id, status, started_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, guild_id, channel_id, STATUS_RECORDING, now),
            )

    def get_session(self, session_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_active_session(self, guild_id: str) -> dict | None:
        """Return the most recent non-ended session for a guild."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM sessions
                   WHERE discord_guild_id = ? AND ended_at IS NULL
                   ORDER BY started_at DESC LIMIT 1""",
                (guild_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_queued_sessions(self) -> list[dict]:
        """Return all sessions with status 'queued'."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY started_at",
                (STATUS_QUEUED,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_sessions_for_guild(self, guild_id: str, limit: int = 10) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE discord_guild_id = ? ORDER BY started_at DESC LIMIT ?",
                (guild_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def update_session_status(self, session_id: str, status: str):
        with self._cursor() as cur:
            cur.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))

    def end_session(self, session_id: str):
        """Mark a session as ended and queue it for transcription.

        Sets ended_at and transitions status to QUEUED so the transcriber
        picks it up. The session moves to COMPLETE only when the
        transcriber calls set_transcript_path().
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE sessions SET ended_at = ?, status = ? WHERE id = ?",
                (now, STATUS_QUEUED, session_id),
            )

    def set_thread_id(self, session_id: str, thread_id: str):
        with self._cursor() as cur:
            cur.execute("UPDATE sessions SET thread_id = ? WHERE id = ?", (thread_id, session_id))

    def set_transcript_path(self, session_id: str, transcript_path: str):
        """Set the transcript file path and mark session as complete."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE sessions SET transcript_path = ?, status = ? WHERE id = ?",
                (transcript_path, STATUS_COMPLETE, session_id),
            )

    # -- audio file tracking -------------------------------------------------

    def add_audio_file(self, session_id: str, filepath: str, size_bytes: int | None = None):
        """Record an audio file associated with a session."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO audio_files (session_id, file_path, size_bytes) VALUES (?, ?, ?)",
                (session_id, filepath, size_bytes),
            )

    def get_audio_files(self, session_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM audio_files WHERE session_id = ? ORDER BY id", (session_id,))
            return [dict(r) for r in cur.fetchall()]

    def get_next_queued_file(self) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                """SELECT af.*, s.discord_channel_id AS channel_id
                   FROM audio_files af
                   JOIN sessions s ON af.session_id = s.id
                   WHERE af.status = 'queued'
                   ORDER BY af.id LIMIT 1"""
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_session_queued_files(self, session_id: str) -> list[dict]:
        """Return all queued audio files for a session."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM audio_files WHERE session_id = ? AND status = 'queued' ORDER BY id",
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def update_file_status(self, file_id: int, status: str):
        with self._cursor() as cur:
            cur.execute("UPDATE audio_files SET status = ? WHERE id = ?", (status, file_id))

    # -- transcript storage --------------------------------------------------

    def add_transcript(self, file_id: int, text: str):
        """Store the transcribed text for a completed audio file."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE audio_files SET status = 'transcribed', transcript_text = ? WHERE id = ?",
                (text, file_id),
            )

    # -- lexicon -------------------------------------------------------------

    def lexicon_add(self, term: str, description: str, added_by: str | None = None) -> bool:
        """Add a term. Returns True if added, False if already present."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO lexicon (term, description, added_by, added_when) VALUES (?, ?, ?, ?)",
                    (term, description, added_by, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def lexicon_remove(self, term: str) -> bool:
        """Remove a term. Returns True if removed."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM lexicon WHERE term = ?", (term,))
            return cur.rowcount > 0

    def lexicon_list(self) -> list[dict]:
        """Return all lexicon entries."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM lexicon ORDER BY term")
            return [dict(r) for r in cur.fetchall()]

    def lexicon_get_terms(self) -> list[str]:
        """Return just the term strings from the lexicon."""
        with self._cursor() as cur:
            cur.execute("SELECT term FROM lexicon ORDER BY term")
            return [r["term"] for r in cur.fetchall()]

    # -- lifecycle -----------------------------------------------------------

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
