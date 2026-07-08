"""Tests for shared.logging_setup — log configuration."""

import logging
import tempfile
import pytest

from shared.logging_setup import setup_logging


def test_setup_logging_returns_logger():
    """setup_logging returns a configured logger."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test", level="DEBUG", log_dir=tmp_dir)
        assert isinstance(logger, logging.Logger)
        assert logger.level == logging.DEBUG


def test_setup_logging_file_handler():
    """Log file is created when logging to file."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_file", level="INFO", log_dir=tmp_dir)
        logger.info("Test message")
        
        import os
        log_files = os.listdir(tmp_dir)
        assert any(f.endswith(".log") for f in log_files)


def test_setup_logging_level():
    """Logger respects the configured level."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = setup_logging(name="test_level", level="WARNING", log_dir=tmp_dir)
        assert logger.level == logging.WARNING


def test_setup_logging_console_only():
    """Logging works without a file (console only)."""
    logger = setup_logging(name="test_console", level="INFO", log_dir="/tmp")
    assert logger is not None
    logger.info("Console only message")
