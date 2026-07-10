"""Scribe's Assistant — Transcriber service entry point.

Polls the shared database for queued audio files, runs sherpa-onnx
transcription, and stores results for the bot's delivery loop.
"""

import sys
import time
import logging
import logging.handlers
import asyncio
from pathlib import Path

# Add project root to path for shared module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import Config, load_config
from shared.database import Database
from shared.lexicon import Lexicon, build_hotwords
from transcriber.worker import download_model, TranscriptionWorker

logger = logging.getLogger("scribes.transcriber")


def setup_logging(config: Config):
    """Configure root logger with console (stdout) and rotating file handlers.

    Console handler ensures docker logs captures all output. File handler
    provides persistent logs at /data/logs/transcriber.log with rotation.
    """
    log_dir = Path("/data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    # Avoid duplicate handlers on restart
    if root_logger.handlers:
        return

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — stdout for docker logs
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    # Rotating file handler — persistent logs
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "transcriber.log",
        maxBytes=config.log_max_size_mb * 1024 * 1024,
        backupCount=config.log_backup_count,
    )
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)


def run_worker(config_path: str = "/app/config.yaml"):
    """Main worker loop — poll DB, transcribe, store transcript for delivery."""
    config = Config(config_path)
    setup_logging(config)

    db = Database(config.database_path)

    # Load lexicon for hotwords
    hotwords = ""
    if config.lexicon_enabled:
        try:
            lexicon = Lexicon(config.lexicon_file, config.lexicon_threshold)
            terms = lexicon.get_terms_list()
            hotwords = build_hotwords(terms)
            logger.info(
                "Loaded %d lexicon terms for hotwords bias", len(terms)
            )
        except Exception as e:
            logger.warning("Could not load lexicon, hotwords disabled: %s", e)

    # Download model if not present
    download_model(config.model_path)

    # Create transcription worker and load model
    worker = TranscriptionWorker(
        model_path=config.model_path,
        num_threads=config.num_threads,
    )
    worker.load_model()

    logger.info("Transcriber worker started, polling for queued files...")

    while True:
        try:
            file_record = db.get_next_queued_file()
            if not file_record:
                time.sleep(config.poll_interval)
                continue

            file_id = file_record["id"]
            session_id = file_record["session_id"]
            filepath = file_record["file_path"]

            logger.info(f"Transcribing file {file_id} for session {session_id}")
            db.update_file_status(file_id, "transcribing")

            # Run transcription
            try:
                text = worker.transcribe(filepath, hotwords=hotwords)
                if text is None:
                    raise RuntimeError('Transcription returned None')

                # Write transcript to shared filesystem
                transcript_dir = Path("/data/transcripts")
                transcript_dir.mkdir(parents=True, exist_ok=True)
                transcript_path = transcript_dir / f"{session_id}.txt"
                transcript_path.write_text(text)

                # Mark session as complete with transcript path
                db.set_transcript_path(session_id, str(transcript_path))
                db.add_transcript(file_id, session_id, text)

                logger.info(f"File {file_id} transcribed successfully")

                # Check if all files for this session are done
                remaining = db.get_session_queued_files(session_id)
                if not remaining:
                    logger.info(f"All files transcribed for session {session_id}")

            except Exception as e:
                logger.error(f"Transcription failed for file {file_id}: {e}")
                db.update_file_status(file_id, "failed")

        except KeyboardInterrupt:
            logger.info("Shutting down transcriber worker")
            break
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
            time.sleep(config.poll_interval)

    db.close()


if __name__ == "__main__":
    run_worker()
