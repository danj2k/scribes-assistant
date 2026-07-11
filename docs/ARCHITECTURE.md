# Scribe's Assistant — Architecture

## System Overview

The system consists of two independent Docker containers communicating through a shared SQLite database and shared filesystem volume:

1. **Bot container** — lightweight, handles Discord interaction and voice capture
2. **Transcriber container** — heavyweight, runs speech-to-text on completed recordings

This separation ensures the large transcription libraries (sherpa-onnx, model weights) only consume resources when actively processing, and the bot remains responsive even during transcription.

## Components

### Bot (scribes-bot)

**Responsibilities:**
- Connect to Discord gateway via py-cord
- Respond to slash commands (/start, /stop, /status, /session, /help, /invite, /lexicon)
- Join voice channels and capture per-speaker audio
- Write WAV audio segments to the shared volume
- Record session metadata in the SQLite queue
- Poll for completed transcripts and upload them to Discord

**Key dependencies:**
- py-cord 2.8.0 (Discord API, voice receive)
- PyNaCl (voice encryption, required by py-cord for voice support)
- davey (DAVE end-to-end encryption protocol for voice, required by py-cord 2.8.0)
- SQLite3 (session/queue management)
- ffmpeg (audio processing, WAV handling)

**Dockerfile:** `Dockerfile.bot` — Python slim base, minimal dependencies.

### Transcriber (scribes-transcriber)

**Responsibilities:**
- Poll the SQLite queue for sessions with status QUEUED
- Load the lexicon from SQLite
- Run sherpa-onnx Whisper transcription with initial prompt injection
- Generate word-level timestamps for speaker diarisation
- Format and save transcripts as plain text
- Update session status to COMPLETE in the SQLite queue via set_transcript_path()

**Key dependencies:**
- sherpa-onnx (speech-to-text, Whisper small model)
- ffmpeg (audio conversion to 16kHz mono WAV)
- SQLite3 (queue and lexicon access)

**Dockerfile:** `Dockerfile.transcriber` — Python base with sherpa-onnx and ffmpeg.

### Shared Volume

A Docker volume mounted at `/data` in both containers provides the filesystem interface:

```
/data/
├── recordings/          # WAV audio files per speaker per session
│   └── <session_id>/
│       ├── speaker_0.wav
│       ├── speaker_1.wav
│       └── ...
├── transcripts/         # Final transcript files
│   └── <session_id>.txt
├── lexicon.yaml         # Lexicon terms and post-correction rules
├── logs/                # Container log files
│   ├── bot.log
│   └── transcriber.log
└── models/              # sherpa-onnx model weights
    └── whisper-small/
```

### SQLite Queue

The database file (`/data/queue.db`) is the coordination point between bot and transcriber. Tables:

**sessions** — one row per recording session
- session_id (TEXT, PK) — timestamp-based identifier
- guild_id, voice_channel_id, transcript_channel_id
- status — RECORDING / QUEUED / TRANSCRIBING / COMPLETE / FAILED
- timestamps for lifecycle tracking

**audio_files** — per-speaker audio files within a session
- session_id (FK), speaker_index, file_path

**lexicon** — words for initial-prompt injection
- word, description, enabled flag

**post_corrections** — fuzzy match corrections applied after transcription
- original, replacement, context, case_sensitive flag

## Data Flow

### Recording (bot-driven)

1. User issues /start → bot joins voice channel, creates session with status RECORDING
2. Bot receives voice data → decodes to WAV (py-cord limitation), writes to shared volume
3. Speaker identification: initial speaker assignment by join order, with simple voice fingerprint refinement
4. User issues /stop → bot stops recording, disconnects, and calls end_session() which sets status to QUEUED
5. Audio files are saved to disk by the recording callback and registered in the audio_files table with status 'queued'

### Transcription (transcriber-driven)

1. Transcriber polls queue, finds session with status QUEUED
2. Updates the audio file status to TRANSCRIBING
3. Loads lexicon from SQLite, builds initial prompt string
4. Converts WAV files to 16kHz mono via ffmpeg
5. Runs sherpa-onnx Whisper small model with initial prompt
6. Produces word-level timestamps, maps to speakers via voice fingerprint alignment
7. Formats transcript with speaker labels and timestamps
8. Writes transcript to /data/transcripts/<session_id>.txt
9. Updates session status to COMPLETE via set_transcript_path()

### Delivery (bot-driven)

1. Bot polls queue, finds session with status COMPLETE and transcript_path set
2. Reads transcript file from shared volume
3. Uploads as Discord file attachment to the transcript channel
4. Updates status to DELIVERED (or marks as complete)

## Logging

Both containers log to files in the shared Docker volume at `/data/logs/`:

- **`/data/logs/bot.log`** — Discord gateway events, voice channel activity, slash command invocations, session state transitions
- **`/data/logs/transcriber.log`** — transcription job progress, model loading, ffmpeg conversion output, error traces

Log level is configurable in `config.yaml` (default: INFO). Log files rotate at 10MB with old files archived. Both containers use Python's `logging` module with a `RotatingFileHandler` writing to the shared volume, so logs persist across container restarts and are accessible from either container.

## Configuration

- `config.yaml` — mounted into both containers, controls model selection, thread settings, lexicon defaults
- Docker secrets — bot token and any other sensitive values, mounted at `/run/secrets/`

## Dependencies

- **py-cord 2.8.0** — Discord bot framework with voice receive support
- **PyNaCl** — Required by py-cord for voice channel encryption
- **davey** — Discord DAVE end-to-end encryption protocol for voice, required by py-cord 2.8.0
- **sherpa-onnx** — Fast, CPU-optimised speech-to-text with Python bindings
- **ffmpeg** — Audio format conversion and processing
- **SQLite** — Lightweight, file-based database for queue coordination

## Open Questions

- Voice fingerprinting approach (how to reliably identify and separate speakers from overlapping audio)
- Lexicon bulk import/export mechanism
