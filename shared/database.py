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
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      TEXT NOT NULL,
                    file_path       TEXT NOT NULL,
                    size_bytes      INTEGER,
                    status          TEXT NOT NULL DEFAULT 'queued',
                    transcript_text TEXT,
                    discord_user_id TEXT,
                    speaker_name    TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS transcript_segments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    file_id     INTEGER NOT NULL,
                    start_time  REAL NOT NULL,
                    end_time    REAL,
                    text        TEXT NOT NULL,
                    seq         INTEGER NOT NULL,
                    FOREIGN KEY (file_id) REFERENCES audio_files(id)
                );

                CREATE TABLE IF NOT EXISTS lexicon (
                    term        TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    added_by    TEXT,
                    added_when  TEXT NOT NULL
                );
                """
            )
        self._migrate_schema()

    def _migrate_schema(self):
        """Add columns that were introduced after the initial schema.

        CREATE TABLE IF NOT EXISTS won't add columns to an existing table,
        so databases created before a column was introduced are missing it.
        We use ALTER TABLE ADD COLUMN (no-op if the column already exists,
        caught via the duplicate-column error).
        """
        migrations = [
            ("audio_files", "discord_user_id", "TEXT"),
            ("audio_files", "speaker_name", "TEXT"),
        ]
        with self._cursor() as cur:
            for table, column, coltype in migrations:
                try:
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists

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

    def get_any_active_session(self) -> dict | None:
        """Return the most recent non-ended session across ALL guilds.

        This bot is designed for a single D&D group, so only one session
        should be active at any time — regardless of which guild it was
        started in. start_command calls this before creating a new session
        to enforce single-session-only operation. This also eliminates the
        original Bug #9 (session ID collision from two starts in the same
        UTC second): if a session is already active, no new one is created.
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM sessions
                   WHERE ended_at IS NULL
                   ORDER BY started_at DESC LIMIT 1""",
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

    def fail_session(self, session_id: str):
        """Mark a session as failed and set ended_at.

        Used when a session cannot produce a transcript — most commonly
        when nobody spoke during the recording, so zero audio files were
        captured. Setting ended_at is critical so that get_active_session()
        (which filters on ended_at IS NULL) does not return this session
        and block future recordings in the guild.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE sessions SET ended_at = ?, status = ? WHERE id = ?",
                (now, STATUS_FAILED, session_id),
            )

    def get_queued_sessions_without_files(self) -> list[dict]:
        """Return queued sessions that have zero audio files.

        The transcriber uses this as a safety net to detect sessions stuck
        in QUEUED status with no audio to transcribe (e.g. empty recording
        where the callback path was missed, or all audio files were lost).
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT s.* FROM sessions s
                   WHERE s.status = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM audio_files af WHERE af.session_id = s.id
                     )
                   ORDER BY s.started_at""",
                (STATUS_QUEUED,),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_thread_id(self, session_id: str, thread_id: str):
        with self._cursor() as cur:
            cur.execute("UPDATE sessions SET thread_id = ? WHERE id = ?", (thread_id, session_id))

    def get_sessions_for_delivery(self) -> list[dict]:
        """Return sessions ready for delivery to Discord.

        A session is deliverable when it has a transcript file and has not
        yet been posted to a thread (thread_id is NULL).  Ordered by
        ended_at so the oldest completed session is delivered first.
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT id FROM sessions
                   WHERE status = ? AND transcript_path IS NOT NULL
                     AND thread_id IS NULL
                   ORDER BY ended_at""",
                (STATUS_COMPLETE,),
            )
            return [{"id": row[0]} for row in cur.fetchall()]

    def set_transcript_path(self, session_id: str, transcript_path: str):
        """Set the transcript file path and mark session as complete."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE sessions SET transcript_path = ?, status = ? WHERE id = ?",
                (transcript_path, STATUS_COMPLETE, session_id),
            )

    # -- audio file tracking -------------------------------------------------

    def add_audio_file(
        self,
        session_id: str,
        filepath: str,
        size_bytes: int | None = None,
        discord_user_id: str | None = None,
        speaker_name: str | None = None,
    ):
        """Record an audio file associated with a session.

        Args:
            session_id: The session this audio file belongs to.
            filepath: Path to the WAV file on the shared volume.
            size_bytes: File size in bytes.
            discord_user_id: The Discord user ID of the speaker (for identity).
            speaker_name: Display name of the speaker (resolved by the bot
                from the guild member cache, since the transcriber has no
                Discord API access).
        """
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO audio_files
                   (session_id, file_path, size_bytes, discord_user_id, speaker_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, filepath, size_bytes, discord_user_id, speaker_name),
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

    # -- transcript segments -------------------------------------------------

    def add_transcript_segment(
        self,
        session_id: str,
        file_id: int,
        start_time: float,
        text: str,
        seq: int,
        end_time: float | None = None,
    ):
        """Store a timestamped transcript segment for interleaved merging.

        Each segment is a chunk of recognised speech with its start time
        (in seconds from the start of that speaker's audio file). Since all
        speakers' WAV files share the same zero point (recording starts at
        /start), segments from different files can be merged by sorting on
        start_time.

        Args:
            session_id: The session this segment belongs to.
            file_id: The audio_files.id this segment was transcribed from.
            start_time: Seconds from the start of the audio file.
            text: The recognised text for this segment.
            seq: Sequence number within the file (for tie-breaking).
            end_time: Optional end time in seconds.
        """
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO transcript_segments
                   (session_id, file_id, start_time, end_time, text, seq)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, file_id, start_time, end_time, text, seq),
            )

    def get_transcript_segments(self, session_id: str) -> list[dict]:
        """Return all transcript segments for a session, ordered by time.

        Segments are sorted by start_time, then by seq for tie-breaking
        when two segments from different speakers share the same timestamp.
        Each segment dict includes the speaker_name from the associated
        audio_files row.
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT ts.*, af.speaker_name, af.discord_user_id
                   FROM transcript_segments ts
                   JOIN audio_files af ON ts.file_id = af.id
                   WHERE ts.session_id = ?
                   ORDER BY ts.start_time, ts.seq""",
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_session_file_status(self, session_id: str) -> dict:
        """Return a summary of audio file statuses for a session.

        Returns a dict with keys: total, queued, transcribing, transcribed,
        failed. The transcriber uses this to determine whether all files
        for a session have been processed and the merged transcript can
        be written.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) as cnt FROM audio_files WHERE session_id = ? GROUP BY status",
                (session_id,),
            )
            counts = {r["status"]: r["cnt"] for r in cur.fetchall()}
        total = sum(counts.values())
        return {
            "total": total,
            "queued": counts.get("queued", 0),
            "transcribing": counts.get("transcribing", 0),
            "transcribed": counts.get("transcribed", 0),
            "failed": counts.get("failed", 0),
        }

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
