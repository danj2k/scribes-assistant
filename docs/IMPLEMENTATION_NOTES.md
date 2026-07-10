# Scribe's Assistant — Implementation Notes

## Non-Obvious Details and Tricky Areas

### py-cord Voice Receive Limitation

py-cord decodes Opus to WAV internally before passing audio data to the receive callback. This means we cannot avoid the WAV encoding overhead. The alternative (discord-ext-voice-recv, which exposes raw Opus) is alpha quality and not suitable. Accept WAV as-is at capture time.

### sherpa-onnx Thread Management

sherpa-onnx will consume as many CPU cores as available by default. On a 4-core machine, this will starve the bot container and the OS. Explicitly clamp thread count in the sherpa-onnx configuration — likely to 2-3 cores, leaving headroom for the bot and system processes. Test empirically to find the right balance.

### WAV to 16kHz Conversion

sherpa-onnx expects 16kHz mono WAV input. py-cord delivers audio at Discord's native rate (48kHz stereo). ffmpeg must be used in the transcriber to convert before running inference. This is an extra step but unavoidable given the py-cord limitation.

### Lexicon Integration — Two-Stage Correction

The lexicon improves transcription quality through two independent mechanisms:

**Stage 1: Hotwords bias (before transcription)**
The lexicon terms are formatted by `build_hotwords()` into a forward-slash separated string: `"Term1/Term2/Term3"`. This is passed to sherpa-onnx via `create_stream(hotwords=...)`. Hotwords are a hard decoding bias — the model is strongly biased toward recognising these terms during transcription. Capped at 100 terms to stay within sherpa-onnx token limits.

**Stage 2: Fuzzy post-correction (after transcription)**
When the transcript is delivered, `DeliveryLoop._correct_text()` applies Levenshtein-based fuzzy matching to every word. Each word is checked against the lexicon — if a close match exists (within 50% of the word's length), it's corrected to the canonical form. This catches misrecognitions that the initial prompt didn't prevent.

Both stages use the same YAML lexicon file (`/data/lexicon.yaml`). Terms added via `/lexicon add` are immediately available for both stages in future sessions.

### SQLite Concurrency

SQLite supports WAL mode for concurrent reads. Both the bot and transcriber access the same database file. Enable WAL mode to allow the transcriber to read while the bot writes. Locking is handled at the application level (status field transitions).

### Speaker Diarisation Approach

No dedicated diarisation library is used. Instead:
- py-cord delivers audio per-user (each user's voice is a separate stream)
- Initial speaker assignment is by Discord user join order
- Voice fingerprinting (MFCCs or similar) refines assignment over time
- Overlapping speakers are not separated — the last active speaker wins
- This is acknowledged as an approximation; it works well enough for a small group

### Timestamp-Based Session IDs

Session IDs are derived from the recording start time: `YYYY-MM-DD_HH-MM-SS`. This makes them human-readable, naturally sorted, and unique (no two sessions can start at the exact same second). Timezone is UTC for consistency.

### Docker Secrets

Bot tokens and other secrets are provided via Docker secrets, which mount as files at `/run/secrets/<name>`. The bot reads the token from `/run/secrets/bot_token` rather than environment variables. This is more secure — secrets are not visible in process listings or Docker inspect output.

### Model Weight Persistence

sherpa-onnx model weights (~500MB for whisper-small) are stored in a Docker volume mounted at `/data/models/`. This volume persists across container rebuilds — only downloaded once on first run.

A standalone download script (`scripts/download_model.py`) handles the download with progress reporting. It can be run via `docker compose exec transcriber python scripts/download_model.py`. The script is idempotent — skips download if all required files already exist. The transcriber's main loop also calls `download_model()` as a fallback on startup.

### SHA-256 Model Verification
Downloaded model archives are verified with SHA-256 before extraction. The expected hash is hardcoded as `EXPECTED_SHA256` in `transcriber/worker.py` and must be updated manually when the model version changes.

Verification flow: download, then hash check, then extract. On mismatch, the bad archive is deleted and a `RuntimeError` is raised. This prevents corrupted or tampered archives from being extracted into the model directory.

The `_sha256_file()` helper reads in 8KB chunks to handle large files without excessive memory use. Progress reporting is deduplicated — logs only fire when the percentage or megabyte count changes, avoiding log spam on slow connections.

To update the expected hash after a model upgrade, run `_sha256_file()` against the new archive and update `EXPECTED_SHA256` in `transcriber/worker.py`.


### Transcript Delivery

Discord has a file size limit (8MB for free servers). Most D&D sessions (3-4 hours) should produce transcripts well under this limit. If a transcript exceeds it, split into multiple parts or compress.

### Logging Configuration

Both containers use Python's standard `logging` module with two handlers:
- **Console handler** — writes to stdout/stderr (visible via `docker compose logs`)
- **File handler** — `RotatingFileHandler` writing to `/data/logs/<container>.log`, max 10MB per file, 5 archived backups

Log format includes timestamp, container name, log level, and message. Example:
```
2025-01-15 20:34:12 bot INFO: Session 2025-01-15_20-30-00 started in channel #general
2025-01-15 20:45:33 transcriber INFO: Transcription job queued for session 2025-01-15_20-30-00
```

The shared `/data/logs/` directory is mounted in both Dockerfiles so either container can access the logs for debugging.

### Slash Command Error Handling

Discord slash commands have built-in parameter validation — mistyped command names or parameter names are rejected by Discord's client before they reach the bot. The bot only needs to handle semantic errors:

- **Missing or invalid parameters** — respond with a user-friendly error message (e.g. "Term not found in lexicon")
- **Empty session** — `/status` or `/stop` when no session is active returns "No active recording session"
- **Concurrent session** — `/start` when already recording returns "Session already in progress"

These are normal application logic responses, not exceptions. Use `respond()` or `respond(embed=error_embed)` to give clear feedback.

### Infrastructure Error Handling

Errors that cannot be reported to Discord (connection loss, crashes, model failures) are handled as follows:

- **Discord connection lost** — py-cord handles reconnection automatically with exponential backoff. Log a WARNING on each attempt, INFO when reconnected.
- **Transcriber fails mid-job** — update session status to FAILED in SQLite. The bot can check for failed sessions and optionally notify the user. Log the full error traceback.
- **SQLite lock contention** — WAL mode allows concurrent reads. If a write fails due to a lock, retry with a short backoff (100ms, 3 attempts).
- **Model not found** — if `/data/models/whisper-small/` is missing or corrupt, the transcriber logs an ERROR and exits. The bot remains functional but transcription will not proceed until the model is restored.
- **Container crash** — Docker restarts the container automatically (`restart: unless-stopped`). The bot reconnects to Discord; the transcriber resumes polling.

### py-cord Slash Command API (discord.commands vs discord.app_commands)

py-cord 2.8.0 does NOT have a `discord.app_commands` module or `bot.tree`
attribute. These are discord.py 2.0 features. py-cord uses its own
`discord.commands` module instead:

- Import `SlashCommandGroup` and `Option` from `discord.commands`
- Use `@bot.slash_command(...)` instead of `@bot.tree.command(...)`
- Use `Option(str, description=...)` type hints instead of `@app_commands.describe(...)`
- Use `bot.add_application_command(...)` instead of `bot.tree.add_command(...)`
- Use `await bot.sync_commands()` instead of `await bot.tree.sync()`

If migrating code from discord.py to py-cord, search for all uses of
`app_commands`, `bot.tree`, and `tree.command` — none of these exist
in py-cord. See `bot/commands.py` for the corrected patterns.
