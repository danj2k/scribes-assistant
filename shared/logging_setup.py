"""Logging configuration for Scribe's Assistant.

Single source of truth for logging setup, used by both the bot and
transcriber containers.  Configures the root logger with:

- A console handler writing to stdout (visible via ``docker compose logs``).
- A rotating file handler writing to ``/data/logs/<name>.log``.
- A consistent format with timestamp, logger name, level, and message.

Both containers share the same format and handler structure so log
entries from different containers are visually consistent when
interleaved for debugging.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

# Single format for all log entries across both containers.
# Example: 2026-07-11 20:34:12 [scribes.bot] INFO: Session started
_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Default log directory — shared Docker volume mounted in both containers.
_DEFAULT_LOG_DIR = "/data/logs"

# Default rotation parameters.
_DEFAULT_MAX_SIZE_MB = 10
_DEFAULT_BACKUP_COUNT = 5


def setup_logging(
    name: str,
    level: str = "INFO",
    log_dir: str = _DEFAULT_LOG_DIR,
    max_size_mb: int = _DEFAULT_MAX_SIZE_MB,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """Configure the root logger with console and rotating file handlers.

    Both handlers are attached to the **root** logger so that log records
    from all modules (``scribes.bot``, ``discord``, ``sherpa_onnx``,
    etc.) are captured.  The *name* parameter only controls the log
    filename — it does not restrict which loggers are configured.

    A duplicate-handler guard prevents double-attachment if the function
    is called more than once (e.g. in tests or on container restart
    within the same process).

    Args:
        name: Logger name used for the log filename (e.g. ``"bot"``,
            ``"transcriber"``).  The file will be ``<name>.log``.
        level: Log level string (DEBUG/INFO/WARNING/ERROR).
        log_dir: Directory for log files.
        max_size_mb: Maximum log file size before rotation.
        backup_count: Number of rotated log files to keep.

    Returns:
        The root logger, configured and ready to use.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers if called multiple times in the same
    # process (e.g. tests, restarts).  Each handler is checked
    # individually so a partial setup from a previous call is not
    # left in a broken state.
    if root_logger.handlers:
        return root_logger

    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    # Console handler — stdout so output appears in `docker compose logs`.
    # Using stdout (not stderr) is the Docker convention for application
    # logs; stderr is for fatal/diagnostic output.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)

    # Rotating file handler — persistent logs in the shared volume.
    # Creates the log directory if it doesn't exist (e.g. first run).
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path / f"{name}.log",
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
    )
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

    return root_logger


def setup_logging_from_config(config, name: str) -> logging.Logger:
    """Configure logging from a :class:`~shared.config.Config` instance.

    This is the convenience wrapper that both ``bot/main.py`` and
    ``transcriber/main.py`` call at startup.  It reads the logging
    parameters from the config so they are tunable via ``config.yaml``
    without touching code.

    Args:
        config: A :class:`Config` instance with ``log_level``,
            ``log_max_size_mb``, and ``log_backup_count`` properties.
        name: Logger name / log filename (``"bot"`` or ``"transcriber"``).

    Returns:
        The root logger, configured and ready to use.
    """
    return setup_logging(
        name=name,
        level=config.log_level,
        max_size_mb=config.log_max_size_mb,
        backup_count=config.log_backup_count,
    )
