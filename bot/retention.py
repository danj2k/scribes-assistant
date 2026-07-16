"""Recording retention — purge old WAV files to reclaim disk space.

Recordings are large (~11.5 MB/min per speaker at 48 kHz/16-bit/stereo).
Without purging, a weekly 3-hour 5-player D&D session accumulates ~10 GB
of WAV files per session, ~40 GB/month.  Transcripts (.txt) are tiny
(tens of KB) and kept indefinitely.

The purge sweep:
  1. Scans /data/recordings/{session_id}/ directories.
  2. Checks session age from the database (sessions.started_at) or, if
     the session is not in the DB, from the directory's modification time.
  3. Deletes the entire session directory (all WAV files) if the session
     is older than the configured retention period.
  4. Marks audio_files rows as 'purged' in the database so the transcriber
     doesn't try to re-queue them, but keeps the session row and transcript
     path intact for historical reference.

Retention is configurable via config.yaml:
    recording:
      retention_days: 8        # 0 = keep forever
      purge_interval_hours: 6  # 0 = only on startup
"""
import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from shared.config import Config
from shared.database import Database

logger = logging.getLogger("scribes.retention")


def purge_old_recordings(
    recordings_dir: str,
    retention_days: int,
    db: Database | None = None,
) -> int:
    """Delete recording directories older than *retention_days*.

    Returns the number of session directories removed.  If
    *retention_days* is 0, no purge is performed (keep forever).

    Age is determined from the database ``sessions.started_at`` column
    when available, falling back to the directory's modification time
    for orphaned directories not in the database.
    """
    if retention_days <= 0:
        logger.info("Recording retention is disabled (retention_days=0)")
        return 0

    base = Path(recordings_dir)
    if not base.exists():
        logger.debug(f"Recordings directory does not exist: {recordings_dir}")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()

    removed = 0
    for session_dir in sorted(base.iterdir()):
        if not session_dir.is_dir():
            continue

        session_id = session_dir.name

        # Try to get the session start time from the database for
        # accurate age calculation.  Fall back to directory mtime if
        # the session isn't in the DB (e.g. orphaned from a crash
        # before the DB row was written).
        session_age_ts = _get_session_timestamp(session_id, db)
        if session_age_ts is None:
            session_age_ts = session_dir.stat().st_mtime

        if session_age_ts > cutoff_ts:
            # Still within retention window — skip.
            continue

        # Delete the session directory and all WAV files within.
        try:
            size_mb = sum(
                f.stat().st_size for f in session_dir.rglob("*") if f.is_file()
            ) / (1024 * 1024)
            shutil.rmtree(session_dir)
            removed += 1
            logger.info(
                f"Purged recording session {session_id} "
                f"(freed {size_mb:.1f} MB, age "
                f"{_format_age(session_age_ts)})"
            )

            # Mark audio_files as purged in the DB so the transcriber
            # doesn't try to open the deleted files.  We keep the
            # session row and transcript_path for historical reference.
            if db is not None:
                _mark_files_purged(db, session_id)
        except OSError as exc:
            logger.warning(
                f"Failed to purge recording directory {session_dir}: {exc}"
            )

    if removed:
        logger.info(f"Retention sweep complete: {removed} session(s) purged")
    else:
        logger.debug("Retention sweep complete: nothing to purge")

    return removed


def _get_session_timestamp(session_id: str, db: Database | None) -> float | None:
    """Return the session start time as a Unix timestamp, or None.

    The database stores ``started_at`` as an ISO 8601 string in UTC.
    If the session is not in the database (or the DB is unavailable),
    returns None so the caller can fall back to directory mtime.
    """
    if db is None:
        return None
    try:
        session = db.get_session(session_id)
        if session and session.get("started_at"):
            return _parse_iso_timestamp(session["started_at"])
    except Exception as exc:
        logger.debug(
            f"Could not query DB for session {session_id}: {exc}, "
            f"falling back to directory mtime"
        )
    return None


def _parse_iso_timestamp(ts: str) -> float | None:
    """Parse an ISO 8601 timestamp string into a Unix timestamp.

    Handles both 'Z' suffix and explicit timezone offsets.  Returns
    None if the string cannot be parsed.
    """
    try:
        # Python 3.11+ datetime.fromisoformat handles 'Z' suffix.
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _mark_files_purged(db: Database, session_id: str) -> None:
    """Mark all audio_files for a session as 'purged' in the database.

    This prevents the transcriber from trying to open WAV files that
    have been deleted.  The session row and transcript_path are kept
    intact so transcripts remain accessible.
    """
    try:
        audio_files = db.get_audio_files(session_id)
        for af in audio_files:
            if af.get("status") not in ("transcribed", "purged"):
                db.update_file_status(af["id"], "purged")
    except Exception as exc:
        logger.debug(
            f"Could not mark audio files as purged for {session_id}: {exc}"
        )


def _format_age(ts: float) -> str:
    """Human-readable age string for log messages."""
    age_seconds = time.time() - ts
    if age_seconds < 3600:
        return f"{age_seconds / 60:.0f} min old"
    elif age_seconds < 86400:
        return f"{age_seconds / 3600:.1f} hours old"
    else:
        return f"{age_seconds / 86400:.1f} days old"


async def retention_loop(
    config: Config,
    db: Database,
    recordings_dir: str = "/data/recordings",
):
    """Background task that periodically purges old recordings.

    Runs an immediate sweep on start, then every *purge_interval_hours*.
    If *purge_interval_hours* is 0, only the startup sweep runs and
    the task exits.
    """
    interval = config.purge_interval_hours
    retention = config.recording_retention_days

    # Immediate sweep on startup.
    purge_old_recordings(recordings_dir, retention, db)

    if interval <= 0:
        logger.info(
            "Retention periodic sweep disabled (purge_interval_hours=0), "
            "only startup purge will run"
        )
        return

    while True:
        await asyncio.sleep(interval * 3600)
        logger.debug("Starting periodic retention sweep")
        purge_old_recordings(recordings_dir, retention, db)
