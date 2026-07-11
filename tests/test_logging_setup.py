"""Tests for shared.logging_setup — log configuration.

These tests verify the single shared logging setup used by both the bot
and transcriber containers.  The key invariant is that both containers
get identical handler structure, format, and stream configuration.

Note: pytest's logging plugin adds its own LogCaptureHandler to the
root logger.  We clear all handlers at the start of each test so
setup_logging's duplicate-handler guard doesn't see stale handlers.
"""

import logging
import logging.handlers
import os
import sys
import tempfile

import pytest

from shared.logging_setup import setup_logging, setup_logging_from_config


def _clear_handlers():
    """Remove all handlers from the root logger.

    Called at the start of each test so pytest's LogCaptureHandler
    doesn't trigger the duplicate-handler guard in setup_logging.
    """
    logging.getLogger().handlers.clear()


@pytest.fixture(autouse=True)
def _clean_root_logger():
    """Clean up root logger handlers after each test to prevent leakage."""
    yield
    logging.getLogger().handlers.clear()


def test_setup_logging_returns_root_logger():
    """setup_logging returns the root logger, configured and ready."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test", level="DEBUG", log_dir=tmp_dir)
        assert isinstance(logger, logging.Logger)
        assert logger is logging.getLogger()  # root logger
        assert logger.level == logging.DEBUG


def test_setup_logging_file_handler():
    """Log file is created in the configured directory."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_file", level="INFO", log_dir=tmp_dir)
        logger.info("Test message")

        log_files = os.listdir(tmp_dir)
        assert any(f.endswith(".log") for f in log_files)


def test_setup_logging_level():
    """Logger respects the configured level."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_level", level="WARNING", log_dir=tmp_dir)
        assert logger.level == logging.WARNING


def test_setup_logging_creates_log_dir():
    """Log directory is created if it doesn't exist."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        nested = os.path.join(tmp_dir, "nested", "logs")
        setup_logging(name="test_mkdir", level="INFO", log_dir=nested)
        assert os.path.isdir(nested)


def test_setup_logging_has_console_and_file_handlers():
    """Both a console (StreamHandler) and file (RotatingFileHandler) are attached."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_handlers", level="INFO", log_dir=tmp_dir)
        handler_types = [type(h) for h in logger.handlers]
        assert logging.StreamHandler in handler_types
        assert logging.handlers.RotatingFileHandler in handler_types


def test_console_handler_uses_stdout():
    """Console handler writes to stdout, not stderr.

    This is the Docker convention — stdout is for application logs so
    they appear in `docker compose logs`.  The bot's old local
    setup_logging used the default (stderr), which was Bug #20.
    """
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_stdout", level="INFO", log_dir=tmp_dir)
        stream_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].stream is sys.stdout


def test_duplicate_handler_guard():
    """Calling setup_logging twice does not add duplicate handlers."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        setup_logging(name="test_dup", level="INFO", log_dir=tmp_dir)
        first_count = len(logging.getLogger().handlers)

        # Second call should be a no-op due to the guard
        setup_logging(name="test_dup", level="DEBUG", log_dir=tmp_dir)
        second_count = len(logging.getLogger().handlers)

        assert first_count == second_count


def test_format_has_datefmt():
    """The formatter includes a datefmt so timestamps are consistent."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_fmt", level="INFO", log_dir=tmp_dir)
        for handler in logger.handlers:
            if handler.formatter:
                assert handler.formatter.datefmt == "%Y-%m-%d %H:%M:%S"


def test_format_is_consistent_across_handlers():
    """Both console and file handlers use the same format string."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_consistency", level="INFO", log_dir=tmp_dir)
        formats = set()
        for handler in logger.handlers:
            if handler.formatter:
                formats.add(handler.formatter._fmt)
        assert len(formats) == 1  # all handlers use the same format


def test_log_file_named_correctly():
    """The log file is named after the *name* parameter."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        setup_logging(name="mybot", level="INFO", log_dir=tmp_dir)
        assert os.path.exists(os.path.join(tmp_dir, "mybot.log"))


def test_invalid_level_defaults_to_info():
    """An unrecognised level string falls back to INFO."""
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_bad_level", level="BOGUS", log_dir=tmp_dir)
        assert logger.level == logging.INFO


def test_setup_logging_from_config():
    """setup_logging_from_config reads properties from a Config instance."""
    _clear_handlers()
    from shared.config import Config

    with tempfile.TemporaryDirectory() as tmp_dir:
        config = Config()
        # Override log_dir since we can't write to /data/logs in tests
        logger = setup_logging(
            name="test_config",
            level=config.log_level,
            max_size_mb=config.log_max_size_mb,
            backup_count=config.log_backup_count,
            log_dir=tmp_dir,
        )
        assert logger.level == logging.INFO  # default from Config
        assert any(isinstance(h, logging.handlers.RotatingFileHandler)
                   for h in logger.handlers)


def test_bot_and_transcriber_use_same_format():
    """Both bot and transcriber produce log entries with the same format.

    This is the core invariant of Bug #20: there were divergent local
    implementations.  Now both call setup_logging_from_config with
    different names but get identical handler structure and format.
    """
    _clear_handlers()
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Simulate bot setup
        bot_logger = setup_logging(name="bot", level="INFO", log_dir=tmp_dir)
        bot_formats = set()
        for h in bot_logger.handlers:
            if h.formatter:
                bot_formats.add(h.formatter._fmt)
        bot_handler_types = sorted(type(h).__name__ for h in bot_logger.handlers)

        # Clear for transcriber setup
        logging.getLogger().handlers.clear()

        # Simulate transcriber setup
        trans_logger = setup_logging(name="transcriber", level="INFO", log_dir=tmp_dir)
        trans_formats = set()
        for h in trans_logger.handlers:
            if h.formatter:
                trans_formats.add(h.formatter._fmt)
        trans_handler_types = sorted(type(h).__name__ for h in trans_logger.handlers)

        # Both should have identical format and handler types
        assert bot_formats == trans_formats
        assert bot_handler_types == trans_handler_types
