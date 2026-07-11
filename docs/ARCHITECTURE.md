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
- Write WAV audio segments to the shared volume via a sync after-callback that schedules async processing on the event loop
- Resolve speaker display names from the guild member cache at recording stop time
- Await audio file registration before transitioning session to QUEUED
- Record session metadata in the SQLite queue
- Poll for completed transcripts and upload them to Discord (no lexicon correction — the transcriber applies corrections before storing)
- Manage lexicon entries via /lexicon commands (add, list, remove — CRUD only; fuzzy correction is the transcriber's job)

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
- Load the lexicon for hotwords bias and post-transcription fuzzy correction
- Run sherpa-onnx Whisper transcription with initial prompt injection
- Apply lexicon fuzzy correction via a three-stage pipeline: (1) exact case-insensitive lexicon match → canonicalise, (2) English dictionary gate via pyspellchecker → skip if recognised English word, (3) Levenshtein fuzzy match against the D&D lexicon → replace if within threshold. This prevents false positives like "ore" → "Orc" while still correcting misrecognised D&D terms like "theran" → "Theron". Tokenisation treats apostrophes and hyphens as intra-word characters so D&D names like "Grim-jaw" or "Smith'var" are matched as single tokens. Original word casing is preserved: all-caps stays all-caps ("THERAN" → "THERON"), title case stays title case ("Theran" → "Theron").
- Capture token-level timestamps for interleaved transcript merging
- Store corrected per-file transcript segments in the database
- Merge all speakers' segments chronologically after ALL files are transcribed
- Format and save the merged transcript as plain text (already corrected)
- Update session status to COMPLETE in the SQLite queue via set_transcript_path()

**Key dependencies:**
- sherpa-onnx (speech-to-text, Whisper — model size configurable, default "small")
- Levenshtein (fuzzy string matching for post-transcription lexicon correction)
- pyspellchecker (English dictionary gate — prevents false-positive corrections of common English words to D&D terms; used for O(1) set membership only, not for its own correction suggestions)
- ffmpeg (audio conversion to 16kHz mono WAV)
- SQLite3 (queue and lexicon access)

**Dockerfile:** `Dockerfile.transcriber` — Python base with sherpa-onnx and ffmpeg.

**Graceful shutdown:** The transcriber registers a SIGTERM handler (Docker sends SIGTERM on `docker stop`). The handler sets a module-level flag that the main loop checks between operations; the loop exits cleanly after the current transcription finishes, then closes the database. This avoids SIGKILL after the Docker grace period, which would lose in-progress work. SIGINT (Ctrl-C) is still handled via `KeyboardInterrupt`.

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
    └── whisper-{model_size}/  (default: whisper-small)
```

### SQLite Queue

The database file (`/data/queue.db`) is the coordination point between bot and transcriber. Tables:

**sessions** — one row per recording session
- session_id (TEXT, PK) — timestamp-based identifier
- guild_id, voice_channel_id, transcript_channel_id
- status — RECORDING / QUEUED / TRANSCRIBING / COMPLETE / FAILED
- timestamps for lifecycle tracking

**audio_files** — per-speaker audio files within a session
- session_id (FK), file_path, size_bytes
- discord_user_id — Discord user ID of the speaker (resolved by the bot)
- speaker_name — display name of the speaker (resolved by the bot from the guild member cache, since the transcriber has no Discord API access)
- status — queued / transcribing / transcribed / failed
- transcript_text — full transcribed text for this file (fallback when timestamps unavailable)

**transcript_segments** — timestamped chunks of recognised speech for interleaved merging
- id (PK), session_id (FK), file_id (FK→audio_files.id)
- start_time (REAL) — seconds from the start of the audio file
- end_time (REAL, nullable) — optional end time
- text — recognised text for this segment
- seq — sequence number within the file (for tie-breaking when two segments share the same start_time)

**lexicon** — words for initial-prompt injection
- word, description, enabled flag

**post_corrections** — fuzzy match corrections applied after transcription
- original, replacement, context, case_sensitive flag

## Data Flow

### Recording (bot-driven)

1. User issues /start → bot checks get_any_active_session() across ALL guilds; if a session is already active anywhere, returns an ephemeral error ("A recording session is already in progress") and does not create a new session
2. If no active session exists, bot joins voice channel and creates session with status RECORDING
3. If the voice channel join fails, the bot calls fail_session() (sets status=FAILED and ended_at), sends an error message, and returns — the session does not block future /start commands because get_active_session() filters on ended_at IS NULL
4. Bot creates a sync after-callback (via make_recording_after_callback) that captures the sink and schedules async audio processing on the event loop, returning a future
5. Bot starts recording with the sink and callback
6. User issues /stop → bot calls stop_recording(), which synchronously invokes the after-callback
7. The after-callback schedules audio processing (writing WAV files, resolving speaker names from the guild member cache, registering files in audio_files with discord_user_id + speaker_name) via run_coroutine_threadsafe
8. /stop awaits the future with a 30-second timeout, ensuring all audio files are written and registered before proceeding
9. If the future resolves with 0 audio files (nobody spoke), the callback has already called fail_session() — /stop informs the user and returns without queuing for transcription
10. Otherwise, /stop calls end_session() which sets status to QUEUED — the transcriber is guaranteed to find all audio files already registered

### Transcription (transcriber-driven)

1. Transcriber polls queue, finds session with status QUEUED
2. Safety net: queries get_queued_sessions_without_files() and marks any queued sessions with zero audio files as FAILED (catches empty recordings that slipped past the callback)
3. Updates the audio file status to TRANSCRIBING
4. Loads lexicon from YAML file, builds hotwords string for the recogniser and keeps the Lexicon instance for post-transcription correction
5. Converts WAV files to 16kHz mono via ffmpeg
6. Runs sherpa-onnx Whisper model (size from config, default "small") with hotwords bias
7. Captures token-level timestamps from the recogniser result
8. Groups tokens into segments, applies lexicon correction via a three-stage pipeline (exact lexicon match → English dictionary gate → Levenshtein fuzzy match) to each segment's text, and stores corrected segments in the transcript_segments table with the speaker's name
9. After ALL audio files for the session are transcribed, queries all segments, sorts by timestamp, and formats as an interleaved transcript: [HH:MM:SS] SpeakerName: dialogue
10. Writes the merged transcript to /data/transcripts/<session_id>.txt (already corrected)
11. Updates session status to COMPLETE via set_transcript_path()

### Delivery (bot-driven)

1. Bot polls queue, finds session with status COMPLETE and transcript_path set
2. Reads the already-corrected transcript file from shared volume (no lexicon correction in the delivery loop)
3. Uploads as Discord file attachment to the transcript channel
4. Updates status to DELIVERED (or marks as complete)

## Logging

Both containers log to files in the shared Docker volume at `/data/logs/`:

- **`/data/logs/bot.log`** — Discord gateway events, voice channel activity, slash command invocations, session state transitions
- **`/data/logs/transcriber.log`** — transcription job progress, model loading, ffmpeg conversion output, error traces

Log level is configurable in `config.yaml` (default: INFO). Log files rotate at 10MB with old files archived. Both containers use Python's `logging` module with a `RotatingFileHandler` writing to the shared volume, so logs persist across container restarts and are accessible from either container.

## Configuration

- `config.yaml` — mounted into both containers, controls model selection, thread settings, lexicon defaults. Defaults are defined in `shared/config.py` (`_DEFAULTS`) and deep-copied on each `load_config()` call so mutations to returned config cannot corrupt global state.
- `shared/tar_utils.py` — safe tarfile extraction with path-traversal protection, used by both the transcriber worker and the standalone download script. Validates each member's destination path before extraction and rejects absolute paths, `..` traversal, and symlinks escaping the target directory.
- Docker secrets — bot token and any other sensitive values, mounted at `/run/secrets/`. The bot validates the token file at startup: if the file is missing or empty, `main()` logs a clear error and exits rather than passing `None` to `bot.run()` (which produces a cryptic py-cord traceback).

## Dependencies

- **py-cord 2.8.0** — Discord bot framework with voice receive support
- **PyNaCl** — Required by py-cord for voice channel encryption
- **davey** — Discord DAVE end-to-end encryption protocol for voice, required by py-cord 2.8.0
- **sherpa-onnx** — Fast, CPU-optimised speech-to-text with Python bindings
- **ffmpeg** — Audio format conversion and processing
- **SQLite** — Lightweight, file-based database for queue coordination

## Open Questions

- Lexicon bulk import/export mechanism
