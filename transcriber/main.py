"""Scribe's Assistant — Transcriber service entry point.

Polls the shared database for queued audio files, runs sherpa-onnx
transcription, and stores results for the bot's delivery loop.
"""

import sys
import time
import logging
import asyncio
from pathlib import Path

# Add project root to path for shared module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import Config, load_config
from shared.database import Database
from transcriber.worker import download_model, TranscriptionWorker

logger = logging.getLogger("scribes.transcriber")

def run_worker(config_path: str = "/app/config.yaml"):
    """Main worker loop — poll DB, transcribe, store transcript for delivery."""
    config = Config(config_path)
    db = Database(config.database_path)

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
                text = worker.transcribe(filepath)
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
