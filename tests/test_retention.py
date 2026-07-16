"""Tests for bot.retention — recording purge logic."""

import asyncio
import os
import time
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from bot.retention import (
    purge_old_recordings,
    _parse_iso_timestamp,
    _format_age,
    retention_loop,
)


class TestPurgeOldRecordings:
    """Tests for purge_old_recordings()."""

    def test_retention_zero_does_nothing(self, tmp_path):
        """retention_days=0 means keep forever — nothing is purged."""
        old_dir = tmp_path / "recordings" / "2025-01-01_12-00-00"
        old_dir.mkdir(parents=True)
        (old_dir / "user.wav").write_bytes(b"audio data")

        removed = purge_old_recordings(str(tmp_path / "recordings"), 0, db=None)

        assert removed == 0
        assert old_dir.exists()

    def test_nonexistent_recordings_dir(self, tmp_path):
        """Non-existent recordings directory returns 0, no error."""
        removed = purge_old_recordings(
            str(tmp_path / "nonexistent"), 8, db=None
        )
        assert removed == 0

    def test_empty_recordings_dir(self, tmp_path):
        """Empty recordings directory returns 0."""
        (tmp_path / "recordings").mkdir()
        removed = purge_old_recordings(
            str(tmp_path / "recordings"), 8, db=None
        )
        assert removed == 0

    def test_old_directory_is_purged(self, tmp_path):
        """Directory older than retention threshold is deleted."""
        rec_dir = tmp_path / "recordings"
        old_session = rec_dir / "2025-01-01_12-00-00"
        old_session.mkdir(parents=True)
        (old_session / "user1.wav").write_bytes(b"x" * 1024)
        (old_session / "user2.wav").write_bytes(b"y" * 1024)

        # Set mtime to 10 days ago
        old_time = time.time() - 10 * 86400
        os.utime(old_session, (old_time, old_time))

        removed = purge_old_recordings(str(rec_dir), 8, db=None)

        assert removed == 1
        assert not old_session.exists()

    def test_recent_directory_is_kept(self, tmp_path):
        """Directory within retention window is kept."""
        rec_dir = tmp_path / "recordings"
        recent_session = rec_dir / "2025-07-10_12-00-00"
        recent_session.mkdir(parents=True)
        (recent_session / "user.wav").write_bytes(b"audio")

        # Set mtime to 2 days ago (within 8-day retention)
        recent_time = time.time() - 2 * 86400
        os.utime(recent_session, (recent_time, recent_time))

        removed = purge_old_recordings(str(rec_dir), 8, db=None)

        assert removed == 0
        assert recent_session.exists()

    def test_mixed_old_and_recent(self, tmp_path):
        """Only old directories are purged; recent ones are kept."""
        rec_dir = tmp_path / "recordings"

        old_session = rec_dir / "2025-01-01_12-00-00"
        old_session.mkdir(parents=True)
        (old_session / "user.wav").write_bytes(b"old")

        recent_session = rec_dir / "2025-07-10_12-00-00"
        recent_session.mkdir(parents=True)
        (recent_session / "user.wav").write_bytes(b"new")

        old_time = time.time() - 30 * 86400
        os.utime(old_session, (old_time, old_time))

        recent_time = time.time() - 1 * 86400
        os.utime(recent_session, (recent_time, recent_time))

        removed = purge_old_recordings(str(rec_dir), 8, db=None)

        assert removed == 1
        assert not old_session.exists()
        assert recent_session.exists()

    def test_non_directory_files_are_skipped(self, tmp_path):
        """Loose files in the recordings root are skipped (only dirs purged)."""
        rec_dir = tmp_path / "recordings"
        rec_dir.mkdir()
        (rec_dir / "README.txt").write_text("not a session")

        removed = purge_old_recordings(str(rec_dir), 8, db=None)

        assert removed == 0
        assert (rec_dir / "README.txt").exists()

    def test_db_age_used_over_mtime(self, tmp_path):
        """When DB has session.started_at, it takes priority over mtime."""
        rec_dir = tmp_path / "recordings"
        session = rec_dir / "2025-07-10_12-00-00"
        session.mkdir(parents=True)
        (session / "user.wav").write_bytes(b"audio")

        # Directory mtime is recent (today), but DB says it's old
        mock_db = MagicMock()
        old_dt = datetime.now(timezone.utc) - timedelta(days=30)
        mock_db.get_session.return_value = {
            "id": "2025-07-10_12-00-00",
            "started_at": old_dt.isoformat(),
        }

        removed = purge_old_recordings(str(rec_dir), 8, db=mock_db)

        assert removed == 1
        assert not session.exists()

    def test_db_missing_falls_back_to_mtime(self, tmp_path):
        """When DB returns None for session, falls back to directory mtime."""
        rec_dir = tmp_path / "recordings"
        session = rec_dir / "2025-01-01_12-00-00"
        session.mkdir(parents=True)
        (session / "user.wav").write_bytes(b"audio")

        old_time = time.time() - 30 * 86400
        os.utime(session, (old_time, old_time))

        mock_db = MagicMock()
        mock_db.get_session.return_value = None

        removed = purge_old_recordings(str(rec_dir), 8, db=mock_db)

        assert removed == 1
        assert not session.exists()

    def test_db_exception_falls_back_to_mtime(self, tmp_path):
        """DB query exceptions are caught and fall back to mtime."""
        rec_dir = tmp_path / "recordings"
        session = rec_dir / "2025-01-01_12-00-00"
        session.mkdir(parents=True)
        (session / "user.wav").write_bytes(b"audio")

        old_time = time.time() - 30 * 86400
        os.utime(session, (old_time, old_time))

        mock_db = MagicMock()
        mock_db.get_session.side_effect = Exception("DB locked")

        removed = purge_old_recordings(str(rec_dir), 8, db=mock_db)

        assert removed == 1

    def test_audio_files_marked_purged_in_db(self, tmp_path):
        """Audio files for purged sessions are marked 'purged' in the DB."""
        rec_dir = tmp_path / "recordings"
        session = rec_dir / "2025-01-01_12-00-00"
        session.mkdir(parents=True)
        (session / "user.wav").write_bytes(b"audio")

        old_time = time.time() - 30 * 86400
        os.utime(session, (old_time, old_time))

        mock_db = MagicMock()
        mock_db.get_session.return_value = None
        mock_db.get_audio_files.return_value = [
            {"id": 1, "status": "transcribed"},
            {"id": 2, "status": "queued"},
            {"id": 3, "status": "purged"},
        ]

        removed = purge_old_recordings(str(rec_dir), 8, db=mock_db)

        assert removed == 1
        # Only the 'queued' file should be updated (transcribed/purged already done)
        mock_db.update_file_status.assert_called_once_with(2, "purged")

    def test_rmtree_failure_is_logged_not_raised(self, tmp_path):
        """rmtree failure logs a warning but doesn't crash the sweep."""
        rec_dir = tmp_path / "recordings"
        session = rec_dir / "2025-01-01_12-00-00"
        session.mkdir(parents=True)
        (session / "user.wav").write_bytes(b"audio")

        old_time = time.time() - 30 * 86400
        os.utime(session, (old_time, old_time))

        with patch("bot.retention.shutil.rmtree", side_effect=OSError("Permission denied")):
            removed = purge_old_recordings(str(rec_dir), 8, db=None)

        assert removed == 0
        assert session.exists()


class TestParseIsoTimestamp:
    """Tests for _parse_iso_timestamp."""

    def test_valid_iso_with_z(self):
        """ISO 8601 with Z suffix parses correctly."""
        ts = _parse_iso_timestamp("2025-01-01T12:00:00Z")
        assert ts is not None
        # Should be around Jan 1, 2025
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 1

    def test_valid_iso_with_offset(self):
        """ISO 8601 with timezone offset parses correctly."""
        ts = _parse_iso_timestamp("2025-01-01T12:00:00+00:00")
        assert ts is not None
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.year == 2025

    def test_naive_iso_treated_as_utc(self):
        """ISO timestamp without timezone is treated as UTC."""
        ts = _parse_iso_timestamp("2025-01-01T12:00:00")
        assert ts is not None
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.hour == 12

    def test_invalid_string_returns_none(self):
        """Invalid timestamp string returns None."""
        assert _parse_iso_timestamp("not a timestamp") is None

    def test_none_returns_none(self):
        """None input returns None."""
        assert _parse_iso_timestamp(None) is None  # type: ignore[arg-type]


class TestFormatAge:
    """Tests for _format_age."""

    def test_minutes(self):
        """Age under 1 hour shows minutes."""
        ts = time.time() - 300  # 5 minutes ago
        age = _format_age(ts)
        assert "min" in age

    def test_hours(self):
        """Age under 1 day shows hours."""
        ts = time.time() - 7200  # 2 hours ago
        age = _format_age(ts)
        assert "hour" in age

    def test_days(self):
        """Age over 1 day shows days."""
        ts = time.time() - 86400 * 5  # 5 days ago
        age = _format_age(ts)
        assert "day" in age


class TestRetentionLoop:
    """Tests for retention_loop async function."""

    @pytest.mark.asyncio
    async def test_immediate_sweep_on_start(self, tmp_path):
        """retention_loop runs an immediate purge on start."""
        rec_dir = tmp_path / "recordings"
        old_session = rec_dir / "2025-01-01_12-00-00"
        old_session.mkdir(parents=True)
        (old_session / "user.wav").write_bytes(b"audio")
        old_time = time.time() - 30 * 86400
        os.utime(old_session, (old_time, old_time))

        mock_config = MagicMock()
        mock_config.recording_retention_days = 8
        mock_config.purge_interval_hours = 0  # Don't loop

        with patch("bot.retention.purge_old_recordings") as mock_purge:
            mock_purge.return_value = 1
            await retention_loop(mock_config, MagicMock(), str(rec_dir))

        mock_purge.assert_called_once()

    @pytest.mark.asyncio
    async def test_interval_zero_exits_after_startup_sweep(self):
        """purge_interval_hours=0 exits after the initial sweep."""
        mock_config = MagicMock()
        mock_config.recording_retention_days = 8
        mock_config.purge_interval_hours = 0

        with patch("bot.retention.purge_old_recordings") as mock_purge:
            mock_purge.return_value = 0
            await retention_loop(mock_config, MagicMock(), "/tmp/nonexistent")

        # Should have been called exactly once (startup sweep only)
        assert mock_purge.call_count == 1

    @pytest.mark.asyncio
    async def test_periodic_sweep_loops(self):
        """Non-zero interval loops — but we use asyncio.sleep mock to avoid waiting."""
        mock_config = MagicMock()
        mock_config.recording_retention_days = 8
        mock_config.purge_interval_hours = 6

        call_count = 0

        def fake_purge(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return 0

        async def fake_sleep(seconds):
            # Allow 2 sweeps then raise to break the loop
            if call_count >= 3:
                raise asyncio.CancelledError()

        with patch("bot.retention.purge_old_recordings", side_effect=fake_purge), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await retention_loop(mock_config, MagicMock(), "/tmp/nonexistent")

        # Startup + 2 periodic sweeps = 3 calls
        assert call_count == 3
