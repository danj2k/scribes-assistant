"""Logging configuration for Scribe's Assistant.

Sets up both console and rotating file handlers with consistent formatting.
Both bot and transcriber containers use this module.
"""

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(
    name: str,
    level: str = "INFO",
    log_dir: str = "/data/logs",
    max_size_mb: int = 10,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure logging with console and rotating file handlers.
    
    Args:
        name: Logger name (typically 'bot' or 'transcriber').
        level: Log level string (DEBUG/INFO/WARNING/ERROR).
        log_dir: Directory for log files.
        max_size_mb: Maximum log file size before rotation.
        backup_count: Number of rotated log files to keep.
        
    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger
    
    # Log format
    fmt = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Console handler (stdout/stderr)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)
    
    # Rotating file handler
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / f"{name}.log",
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    
    return logger
