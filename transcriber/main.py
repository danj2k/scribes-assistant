"""Scribe's Assistant — Transcriber service entry point.

Polls the shared database for queued audio files, runs sherpa-onnx
transcription, and stores results for the bot's delivery loop.
"""

import os
import sys
import time
import signal
import re
import logging
import asyncio
from pathlib import Path

# Add project root to path for shared module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import Config, load_config
from shared.database import Database
from shared.lexicon import Lexicon, build_hotwords
from shared.logging_setup import setup_logging_from_config
from transcriber.worker import download_model, TranscriptionWorker

logger = logging.getLogger("scribes.transcriber")

# Set by the SIGTERM handler so the main loop knows to exit
# after the current operation completes. Docker sends SIGTERM
# on `docker stop`; without this, the process is SIGKILLed
# after the grace period, losing any in-progress transcription.
_shutdown_requested = False


def _handle_sigterm(signum, frame):
    """SIGTERM handler — request graceful shutdown.

    Sets a flag that the main loop checks between operations. We
    do NOT raise an exception here (unlike SIGINT/KeyboardInterrupt)
    because the signal can arrive during a C extension call (sherpa-onnx)
    that is not safe to interrupt mid-execution. Instead, we let the
    current transcription finish and check the flag at the next loop
    iteration.
    """
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("SIGTERM received — will shut down after current operation completes")


def _format_timestamp(seconds: float) -> str:
    """Format seconds as [HH:MM:SS] for transcript timestamps."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_interleaved_transcript(db: Database, session_id: str) -> str:
    """Build a timestamped, interleaved transcript for a session.

    Queries all transcript_segments for the session (already sorted by
    start_time) and formats each as:

        [HH:MM:SS] SpeakerName: dialogue

    Falls back to per-speaker blocks if no segments are available
    (e.g. the recogniser didn't produce timestamps).
    """
    segments = db.get_transcript_segments(session_id)

    if segments:
        lines = []
        for seg in segments:
            ts = _format_timestamp(seg["start_time"])
            speaker = seg.get("speaker_name") or seg.get("discord_user_id") or "Unknown"
            lines.append(f"[{ts}] {speaker}: {seg['text']}")
        return "\n".join(lines) + "\n"

    # Fallback: per-speaker blocks using audio_files.transcript_text.
    # This path is only hit when the recogniser didn't produce segment
    # timestamps (e.g. model loaded without enable_segment_timestamps).
    # With timestamps enabled, the timestamped-segments path above is used.
    audio_files = db.get_audio_files(session_id)
    if not audio_files:
        return ""

    blocks = []
    for af in audio_files:
        speaker = af.get("speaker_name") or af.get("discord_user_id") or "Unknown"
        text = af.get("transcript_text") or ""
        if text.strip():
            blocks.append(f"{speaker}: {text.strip()}")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _correct_text(text: str, lexicon: Lexicon | None) -> str:
    """Apply lexicon fuzzy correction to transcribed text.

    Splits on word boundaries, corrects each word independently, and
    reassembles with original whitespace/punctuation preserved. Words
    not in the lexicon pass through unchanged.

    Tokenisation uses a pattern that treats apostrophes and hyphens as
    intra-word characters so that D&D names like "Grim-jaw" or
    "Smith'var" are matched as single tokens rather than being split
    into fragments that would never match lexicon terms.

    This runs in the transcriber, not the bot's delivery loop, so the
    corrected text is what gets stored in the database and written to
    the transcript file. The bot's delivery loop simply reads and
    uploads the already-corrected transcript.
    """
    if not lexicon or not lexicon.terms:
        return text

    def _replace_word(match):
        word = match.group(0)
        corrected = lexicon.correct(word)
        return corrected if corrected else word

    # Match word characters plus intra-word apostrophes and hyphens.
    # \w+(?:['-]\w+)* matches "Grim-jaw", "Smith'var", "O'Brien",
    # and ordinary words like "hello".  Leading/trailing apostrophes
    # and hyphens (e.g. "'tis" or "well-") are not captured — those
    # are punctuation, not part of a name.
    return re.sub(r"\w+(?:['-]\w+)*", _replace_word, text)


def run_worker(config_path: str | None = None):
    """Main worker loop — poll DB, transcribe, store transcript for delivery.

    If *config_path* is None, falls back to the CONFIG_PATH environment
    variable, then to /data/config.yaml — consistent with the bot's
    config path.
    """
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "/data/config.yaml")
    config = Config(config_path)
    setup_logging_from_config(config, "transcriber")

    db = Database(config.database_path)

    # Load lexicon for hotwords and post-transcription correction.
    # The same Lexicon instance serves both purposes: hotwords bias the
    # recogniser during transcription, and correct() applies fuzzy
    # matching to fix misrecognised words after transcription.
    # Correction runs here (transcriber side), not in the bot's delivery
    # loop — the bot only manages lexicon entries via /lexicon commands.
    hotwords = ""
    correction_lexicon: Lexicon | None = None
    if config.lexicon_enabled:
        try:
            lexicon = Lexicon(config.lexicon_file, config.lexicon_threshold)
            terms = lexicon.get_terms_list()
            hotwords = build_hotwords(terms)
            correction_lexicon = lexicon
            logger.info(
                "Loaded %d lexicon terms for hotwords bias and post-correction",
                len(terms),
            )
        except Exception as e:
            logger.warning("Could not load lexicon, hotwords and correction disabled: %s", e)

    # Download model if not present
    download_model(config.model_path, model_size=config.model_size)

    # Create transcription worker and load model
    worker = TranscriptionWorker(
        model_path=config.model_path,
        num_threads=config.num_threads,
        model_size=config.model_size,
    )
    worker.load_model()

    # Register SIGTERM handler so `docker stop` triggers a graceful
    # shutdown instead of SIGKILL after the grace period. SIGINT
    # (Ctrl-C) is still caught via KeyboardInterrupt below.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Write an initial heartbeat so the container reports healthy as
    # soon as the model is loaded and the worker is ready to poll.
    # Without this, the health check would see no heartbeat file and
    # report unhealthy during the startup window.
    _heartbeat_file = Path("/data/transcriber.heartbeat")
    _heartbeat_file.write_text(str(time.time()))

    logger.info("Transcriber worker started, polling for queued files...")

    while not _shutdown_requested:
        try:
            # Safety net: clean up sessions stuck in QUEUED with no audio
            # files (e.g. empty recording where the callback's fail_session
            # call was missed, or all audio files were lost). Without this,
            # such sessions poll forever and /status shows them as queued.
            for session in db.get_queued_sessions_without_files():
                logger.warning(
                    "Session %s is queued but has no audio files — marking as failed",
                    session["id"],
                )
                db.fail_session(session["id"])

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
                result = worker.transcribe(filepath, hotwords=hotwords, confidence_threshold=config.confidence_threshold)
                if result is None:
                    raise RuntimeError('Transcription returned None')

                # Store timestamped segments in the DB for later merging.
                # Each segment's start_time is relative to the start of this
                # speaker's audio file. Because TimestampedWaveSink pads
                # silence for DTX gaps and initial offsets, all speakers'
                # WAV files share the same zero point (the recording start
                # at /start), so segments from different files can be
                # merged chronologically. See bot/timestamped_sink.py.
                # Apply lexicon fuzzy correction to each segment's text
                # before storing so the corrected text is what gets merged
                # and delivered.
                for seq, segment in enumerate(result.segments):
                    corrected_text = _correct_text(segment.text, correction_lexicon)
                    db.add_transcript_segment(
                        session_id=session_id,
                        file_id=file_id,
                        start_time=segment.start_time,
                        text=corrected_text,
                        seq=seq,
                    )

                # Also store the full text on the audio file row as a
                # fallback for when timestamp segments aren't available.
                corrected_full_text = _correct_text(result.text, correction_lexicon)
                db.add_transcript(file_id, corrected_full_text)

                logger.info(f"File {file_id} transcribed successfully")

                # Check if all files for this session are done.
                # Only when the last file is transcribed do we merge all
                # segments and write the final transcript. This fixes the
                # partial-delivery bug where each file's transcript was
                # written separately, overwriting the previous one.
                status = db.get_session_file_status(session_id)
                if status["total"] > 0 and status["queued"] == 0 and status["transcribing"] == 0:
                    logger.info(
                        f"All files transcribed for session {session_id}, "
                        f"building merged transcript"
                    )
                    transcript_text = _build_interleaved_transcript(db, session_id)

                    transcript_dir = Path("/data/transcripts")
                    transcript_dir.mkdir(parents=True, exist_ok=True)
                    transcript_path = transcript_dir / f"{session_id}.txt"
                    transcript_path.write_text(transcript_text)

                    db.set_transcript_path(session_id, str(transcript_path))
                    logger.info(
                        f"Session {session_id}: merged transcript written to {transcript_path}"
                    )

            except Exception as e:
                logger.error(f"Transcription failed for file {file_id}: {e}")
                db.update_file_status(file_id, "failed")

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
            time.sleep(config.poll_interval)

        # Update heartbeat after each poll cycle so the health check
        # knows the worker is alive.  Placed here (outside the inner
        # try/except) so it fires whether or not a file was processed.
        _heartbeat_file.write_text(str(time.time()))

    if _shutdown_requested:
        logger.info("Graceful shutdown complete (SIGTERM)")
    else:
        logger.info("Shutting down transcriber worker (SIGINT)")

    db.close()


if __name__ == "__main__":
    run_worker()
