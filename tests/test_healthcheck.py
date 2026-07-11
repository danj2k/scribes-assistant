"""Tests for the bot health check and heartbeat mechanism.

The health check (bot/healthcheck.py) reads a heartbeat file that the
bot writes on gateway connect and updates every 30 seconds.  These
tests verify both the health check script's logic and the bot's
heartbeat-writing behaviour.
"""
import os
import sys
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch, mock_open

import pytest

# Add project root for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestHealthcheckScript:
    """Test bot/healthcheck.py — the Docker health check entry point."""

    def _run_healthcheck(self, monkeypatch, capsys, heartbeat_exists=True,
                         heartbeat_content=None, heartbeat_mtime=None,
                         unique_id=None):
        """Run healthcheck.py in-process and return its exit code."""
        import bot.healthcheck as hc

        # Use a unique path per test to avoid cross-test pollution
        suffix = f"_{unique_id}" if unique_id else ""
        tmp_path = Path(f"/tmp/test_healthcheck_heartbeat{suffix}")
        # Clean up any previous file
        tmp_path.unlink(missing_ok=True)

        monkeypatch.setattr(hc, "HEARTBEAT_FILE", tmp_path)

        if heartbeat_exists:
            if heartbeat_content is not None:
                tmp_path.write_text(heartbeat_content)
            else:
                tmp_path.write_text(str(time.time()))
            if heartbeat_mtime is not None:
                import os
                os.utime(tmp_path, (heartbeat_mtime, heartbeat_mtime))

        exit_code = 0
        try:
            hc.main()
        except SystemExit as e:
            exit_code = e.code

        captured = capsys.readouterr()
        # Clean up
        tmp_path.unlink(missing_ok=True)
        return exit_code, captured.out, captured.err

    def test_fresh_heartbeat_is_healthy(self, monkeypatch, capsys):
        """Heartbeat file updated within threshold → exit 0."""
        exit_code, out, err = self._run_healthcheck(monkeypatch, capsys,
                                                     unique_id="fresh")
        assert exit_code == 0
        assert "healthy" in out

    def test_missing_heartbeat_file_is_unhealthy(self, monkeypatch, capsys):
        """No heartbeat file → exit 1."""
        exit_code, out, err = self._run_healthcheck(
            monkeypatch, capsys, heartbeat_exists=False, unique_id="missing"
        )
        assert exit_code == 1
        assert "not found" in err

    def test_stale_heartbeat_is_unhealthy(self, monkeypatch, capsys):
        """Heartbeat older than threshold → exit 1."""
        old_time = time.time() - 120  # 120s ago, threshold is 90s
        exit_code, out, err = self._run_healthcheck(
            monkeypatch, capsys, heartbeat_mtime=old_time,
            heartbeat_content=str(old_time), unique_id="stale"
        )
        assert exit_code == 1
        assert "stale" in err.lower() or "old" in err.lower()

    def test_boundary_just_under_threshold_is_healthy(self, monkeypatch, capsys):
        """Heartbeat just under the threshold (80s) → exit 0."""
        recent_time = time.time() - 80  # 80s ago, under 90s threshold
        exit_code, out, err = self._run_healthcheck(
            monkeypatch, capsys, heartbeat_mtime=recent_time,
            heartbeat_content=str(recent_time), unique_id="boundary"
        )
        assert exit_code == 0


class TestHeartbeatWriting:
    """Test the bot's heartbeat-writing methods."""

    def test_write_heartbeat_creates_file(self, tmp_path):
        """_write_heartbeat writes current timestamp to the heartbeat file."""
        from bot.main import ScribesBot

        heartbeat_file = tmp_path / "bot.heartbeat"

        # Create a minimal mock that satisfies ScribesBot's __init__
        config = MagicMock()
        db = MagicMock()
        lexicon = MagicMock()

        with patch.object(ScribesBot, "__init__", lambda self, *a, **kw: None):
            bot = ScribesBot.__new__(ScribesBot)
            bot._heartbeat_file = heartbeat_file
            bot._heartbeat_task = None

            # Run the async method
            asyncio.run(bot._write_heartbeat())

        assert heartbeat_file.exists()
        content = heartbeat_file.read_text()
        # Should be a float timestamp
        timestamp = float(content)
        assert abs(timestamp - time.time()) < 5  # within 5 seconds

    def test_heartbeat_loop_writes_and_sleeps(self, tmp_path):
        """_heartbeat_loop writes heartbeat then sleeps 30 seconds."""
        from bot.main import ScribesBot

        heartbeat_file = tmp_path / "bot.heartbeat"

        with patch.object(ScribesBot, "__init__", lambda self, *a, **kw: None):
            bot = ScribesBot.__new__(ScribesBot)
            bot._heartbeat_file = heartbeat_file

            # Mock is_closed to return True after one iteration so the
            # loop body runs once then exits.
            call_count = 0

            def mock_is_closed():
                nonlocal call_count
                call_count += 1
                return call_count > 1  # False first time, True second

            bot.is_closed = mock_is_closed

            # Make asyncio.sleep return immediately
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                asyncio.run(bot._heartbeat_loop())

            # Should have written the heartbeat file
            assert heartbeat_file.exists()
            # Should have slept once (30 seconds normally, mocked)
            mock_sleep.assert_awaited_once_with(30)

    def test_heartbeat_started_on_ready(self):
        """on_ready writes initial heartbeat and starts the heartbeat task."""
        from bot.main import ScribesBot

        with patch.object(ScribesBot, "__init__", lambda self, *a, **kw: None):
            bot = ScribesBot.__new__(ScribesBot)
            bot.logger = MagicMock()
            bot._heartbeat_file = Path("/tmp/test_on_ready_heartbeat")
            bot._heartbeat_task = None
            bot._write_heartbeat = AsyncMock()
            bot.sync_commands = AsyncMock()
            bot.delivery_loop = None

            # Patch read-only properties from commands.Bot
            with patch.object(type(bot), "user", new_callable=PropertyMock,
                              return_value=MagicMock(id=12345)), \
                 patch.object(type(bot), "guilds", new_callable=PropertyMock,
                              return_value=[]):
                # Mock asyncio.create_task
                created_tasks = []
                with patch("asyncio.create_task",
                           side_effect=lambda coro: created_tasks.append(coro)):
                    asyncio.run(bot.on_ready())

                # Close the coroutine that create_task captured but
                # didn't actually schedule (since create_task is mocked).
                for coro in created_tasks:
                    coro.close()

            # Should have written heartbeat immediately
            bot._write_heartbeat.assert_awaited_once()
            # Should have started the heartbeat loop task
            assert len(created_tasks) == 1

    def test_heartbeat_task_not_restarted_if_running(self):
        """on_ready does not restart heartbeat if task is already running."""
        from bot.main import ScribesBot

        with patch.object(ScribesBot, "__init__", lambda self, *a, **kw: None):
            bot = ScribesBot.__new__(ScribesBot)
            bot.logger = MagicMock()
            bot._heartbeat_file = Path("/tmp/test_on_ready_heartbeat2")
            bot._write_heartbeat = AsyncMock()
            bot.sync_commands = AsyncMock()
            bot.delivery_loop = None

            # Simulate an already-running task
            mock_task = MagicMock()
            mock_task.done.return_value = False
            bot._heartbeat_task = mock_task

            with patch.object(type(bot), "user", new_callable=PropertyMock,
                              return_value=MagicMock(id=12345)), \
                 patch.object(type(bot), "guilds", new_callable=PropertyMock,
                              return_value=[]):
                with patch("asyncio.create_task") as mock_create:
                    asyncio.run(bot.on_ready())

                # Close any coroutines that create_task captured but
                # didn't actually schedule (since create_task is mocked).
                if mock_create.called:
                    coro = mock_create.call_args.args[0]
                    coro.close()

            # Should NOT have created a new task
            mock_create.assert_not_called()


class TestTranscriberHealthcheck:
    """Test transcriber/healthcheck.py — the Docker health check for
    the transcriber container.

    The transcriber writes /data/transcriber.heartbeat after each poll
    cycle.  The health check verifies the file exists and is fresh.
    """

    def _run_transcriber_healthcheck(self, monkeypatch, capsys,
                                     heartbeat_exists=True,
                                     heartbeat_mtime=None,
                                     unique_id=None):
        """Run transcriber/healthcheck.py in-process and return exit code."""
        import transcriber.healthcheck as thc

        suffix = f"_{unique_id}" if unique_id else ""
        tmp_path = Path(f"/tmp/test_transcriber_hc{suffix}")
        tmp_path.unlink(missing_ok=True)

        monkeypatch.setattr(thc, "HEARTBEAT_FILE", tmp_path)

        if heartbeat_exists:
            tmp_path.write_text(str(time.time()))
            if heartbeat_mtime is not None:
                import os
                os.utime(tmp_path, (heartbeat_mtime, heartbeat_mtime))

        exit_code = 0
        try:
            thc.main()
        except SystemExit as e:
            exit_code = e.code

        captured = capsys.readouterr()
        tmp_path.unlink(missing_ok=True)
        return exit_code, captured.out, captured.err

    def test_fresh_heartbeat_is_healthy(self, monkeypatch, capsys):
        """Heartbeat file updated recently → exit 0."""
        exit_code, out, err = self._run_transcriber_healthcheck(
            monkeypatch, capsys, unique_id="fresh"
        )
        assert exit_code == 0
        assert "healthy" in out

    def test_missing_heartbeat_is_unhealthy(self, monkeypatch, capsys):
        """No heartbeat file → exit 1."""
        exit_code, out, err = self._run_transcriber_healthcheck(
            monkeypatch, capsys, heartbeat_exists=False, unique_id="missing"
        )
        assert exit_code == 1
        assert "not found" in err

    def test_stale_heartbeat_is_unhealthy(self, monkeypatch, capsys):
        """Heartbeat older than 5 minutes → exit 1."""
        old_time = time.time() - 400  # 400s ago, threshold is 300s
        exit_code, out, err = self._run_transcriber_healthcheck(
            monkeypatch, capsys, heartbeat_mtime=old_time, unique_id="stale"
        )
        assert exit_code == 1
        assert "unhealthy" in err.lower()

    def test_just_under_threshold_is_healthy(self, monkeypatch, capsys):
        """Heartbeat just under the 5-minute threshold → exit 0."""
        recent_time = time.time() - 250  # 250s ago, under 300s threshold
        exit_code, out, err = self._run_transcriber_healthcheck(
            monkeypatch, capsys, heartbeat_mtime=recent_time, unique_id="boundary"
        )
        assert exit_code == 0
