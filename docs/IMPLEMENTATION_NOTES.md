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

**Stage 2: Fuzzy post-correction (after transcription, transcriber side)**
After each audio file is transcribed, `transcriber/main.py`'s `_correct_text()` applies a three-stage correction pipeline to every word in each segment:

1. **Exact lexicon match** — if the word matches a lexicon term case-insensitively, it's canonicalised (e.g. "theron" → "Theron"). This takes priority over the dictionary gate because an exact match is a true positive — the correction is just capitalisation/canonical form. Some lexicon terms happen to appear in English dictionaries (e.g. "theron" is a Greek name); blocking these would prevent proper canonicalisation of correctly-transcribed terms.
2. **English dictionary gate** — if the word is a recognised English word (via pyspellchecker's bundled dictionary, O(1) set membership), it's left alone. This prevents false positives like "ore" → "Orc" or "may" → "Mae". pyspellchecker is used for dictionary membership only, not for its own correction suggestions.
3. **Fuzzy lexicon match** — if the word is neither an exact lexicon match nor an English word, Levenshtein distance is computed against every lexicon term. If the best match is within the configured threshold (proportional to word length, default factor 0.5, minimum 1), the word is corrected. This catches misrecognised D&D terms like "theran" → "Theron" (distance 1).

The corrected text is what gets stored in the database and written to the transcript file.

This runs in the transcriber, not the bot's delivery loop, so the corrected text is persisted before delivery. The bot's `/lexicon` commands manage the YAML file (add, list, remove) — the bot does not call `correct()`.

Both stages use the same YAML lexicon file (`/data/lexicon.yaml`). Terms added via `/lexicon add` are immediately available for both stages in future sessions.

**Tokenisation (Bug #14):** The regex `\w+(?:['-]\w+)*` treats apostrophes and hyphens as intra-word characters so that D&D names like "Grim-jaw", "Smith'var", or "O'Brien" are matched as single tokens. The old regex `\b\w+\b` split these into fragments ("grim" + "jaw") that would never match lexicon terms. Leading/trailing apostrophes and hyphens are not captured — those are punctuation, not part of a name (e.g. "'tis" tokenises as "tis").

**Case preservation (Bug #15):** When a correction is applied, the original word's casing pattern is preserved via `_match_case()`:
- All uppercase (len > 1) → correction is uppercased (e.g. "THERAN" → "THERON" — shouting preserved)
- Title case (first upper, rest lower) → correction is title-cased (e.g. "Theran" → "Theron")
- All lowercase or mixed casing → canonical form from lexicon is used as-is (e.g. "theron" → "Theron")

This means a word spoken in shouting ("GRIM-JAW attacked") keeps its all-caps form after correction ("GRIM-JAW attacked"), and title-case names ("Grim-jaw attacked") stay title-case.

### SQLite Concurrency

SQLite supports WAL mode for concurrent reads. Both the bot and transcriber access the same database file. Enable WAL mode to allow the transcriber to read while the bot writes. Locking is handled at the application level (status field transitions).

### Delivery Loop Database Access (Bug #17)

The delivery loop polls for sessions ready to be delivered (status = COMPLETE, transcript_path set, thread_id NULL). It previously accessed the database's private `_cursor()` context manager directly, coupling it to the database's internal implementation. It now calls the public `Database.get_sessions_for_delivery()` method instead, which returns a list of `{"id": session_id}` dicts ordered by `ended_at`. The delivery loop has no direct SQL access and no unnecessary commit after the SELECT query.

### py-cord Recording After-Callback

py-cord 2.8.0's `start_recording(sink, callback)` stores `callback` as an `AudioReader.after` callback. When `stop_recording()` is called, py-cord invokes `after(exc)` **synchronously** from the voice client's thread — it receives the exception (or None), NOT the sink. The callback must be a regular (sync) callable, not a coroutine function.

The bot uses `make_recording_after_callback()` to create a sync callback that:
1. Captures the sink, session metadata, and event loop at creation time (in `start_command`)
2. Schedules the async audio-processing coroutine via `asyncio.run_coroutine_threadsafe()` — safe from any thread
3. Returns a `Future` that `stop_command` awaits with a 30-second timeout

This ensures `/stop` does not call `end_session()` (which transitions to QUEUED) until all audio files have been written to disk and registered in the `audio_files` table. Without this, the transcriber could find zero files for a session and produce an empty transcript.

#### Non-blocking file writes

The async processing coroutine (`_process_recording`) runs on the event loop. WAV file writes — which can be tens of MB for a 30-minute session — are offloaded to a thread pool via `asyncio.to_thread(_write_audio_files_sync, file_specs)`. The sync helper `_write_audio_files_sync` iterates the file specs, creates directories, writes bytes, and returns the count of successfully written files. Speaker name resolution (guild member cache lookup, no I/O) and DB registration (`add_audio_file`) remain on the event loop. This prevents blocking the event loop during file I/O, which would stall Discord gateway heartbeats and cause the bot to appear unresponsive.

If the callback raises an exception or the future times out, `stop_command` logs the error and proceeds with `end_session()` anyway — partial or no audio is better than a stuck session.

### Empty Recording Handling

When nobody speaks during a recording, `sink.audio_data` is empty. The callback writes zero WAV files, calls `fail_session()` to mark the session as FAILED (with `ended_at` set), and resolves the future with result 0. `stop_command` sees the zero result, sends an ephemeral message to the user ("No audio was captured — no transcript will be generated"), and returns without calling `end_session()`.

The transcriber also has a safety net: before each poll of `get_next_queued_file()`, it queries `get_queued_sessions_without_files()` and marks any sessions stuck in QUEUED with zero audio files as FAILED. This catches edge cases where the callback's `fail_session()` call didn't fire (e.g. callback exception, timeout, or files lost after writing).

### Timestamp-Based Session IDs

Session IDs are derived from the recording start time: `YYYY-MM-DD_HH-MM-SS`. This makes them human-readable, naturally sorted, and unique (no two sessions can start at the exact same second). Timezone is UTC for consistency.

### Docker Secrets

Bot tokens and other secrets are provided via Docker secrets, which mount as files at `/run/secrets/<name>`. The bot reads the token from `/run/secrets/bot_token` rather than environment variables. This is more secure — secrets are not visible in process listings or Docker inspect output.

The `Config.bot_token` property validates the token file at access time: if the file does not exist, it raises `FileNotFoundError` with an actionable message (including the expected path and how to create it); if the file exists but is empty or whitespace-only, it raises `ValueError`. `bot/main.py` catches both exceptions at startup, logs a clear error message, and exits with status 1. This prevents the cryptic traceback that `bot.run(None)` produces when the token is silently missing.

### Model Weight Persistence

sherpa-onnx model weights (~609MB for whisper-small; varies by model size) are stored in a Docker volume mounted at `/data/models/whisper-{model_size}/`. The model size is set via the `transcriber.model` config key (default: `small`). This volume persists across container rebuilds — only downloaded once on first run.

A standalone download script (`scripts/download_model.py`) handles the download with progress reporting. It can be run via `docker compose exec transcriber python scripts/download_model.py`. The script is idempotent — skips download if all required files already exist. The transcriber's main loop also calls `download_model()` as a fallback on startup.

### SHA-256 Model Verification
Downloaded model archives are verified with SHA-256 before extraction. The expected hash is stored in `EXPECTED_SHA256` — a dict keyed by model size (e.g. `"small"`, `"base"`) in `transcriber/worker.py`. When a model size is not in the dict (no known hash), verification is skipped with a WARNING rather than failing. This allows new model sizes to be used without immediately knowing their hash.

Verification flow: download, then hash check (if available), then extract. On mismatch, the bad archive is deleted and a `RuntimeError` is raised. This prevents corrupted or tampered archives from being extracted into the model directory.

The `_sha256_file()` helper reads in 8KB chunks to handle large files without excessive memory use. Progress reporting is deduplicated — logs only fire when the percentage or megabyte count changes, avoiding log spam on slow connections.

To add a hash for a new model size, download the archive, run `_sha256_file()` against it, and add the entry to `EXPECTED_SHA256` in `transcriber/worker.py`.

### Safe Tar Extraction

Model archives are extracted using `safe_extract_members()` from `shared/tar_utils.py`. This provides path-traversal protection that the bare `tarfile.extract()` call does not — without it, a malicious or corrupted archive could write files outside the target directory (e.g. `../../etc/passwd`).

The validation logic rejects:
- Absolute paths (`/etc/passwd`)
- Parent-directory traversal (`../../../tmp/evil`)
- Symlinks or hardlinks with targets that escape the extraction directory

On Python 3.12+, `tarfile.extract()` is called with `filter='data'` for an additional layer of protection. On Python 3.11 (the Docker base image), the manual validation in `_validate_member_path()` provides equivalent protection.

The `strip_components=1` parameter strips the top-level archive directory (e.g. `sherpa-onnx-whisper-small/small-encoder.onnx` becomes `small-encoder.onnx`), preserving the previous behaviour while adding the safety check.

Extraction is fail-closed: if any member has an unsafe path, a `ValueError` is raised and extraction halts. A partial extraction is preferable to silently writing outside the target directory.


### Transcript Delivery

Discord has a file size limit (8MB for free servers). Most D&D sessions (3-4 hours) should produce transcripts well under this limit. If a transcript exceeds it, split into multiple parts or compress.

### Config Defaults Are Deep-Copied

`load_config()` in `shared/config.py` uses `copy.deepcopy(_DEFAULTS)` to produce the base config dict before merging YAML overrides. This ensures nested dicts (`discord`, `permissions`, `lexicon`, etc.) are independent copies, not shared references to the module-level `_DEFAULTS`. Without this, any in-place mutation of a nested default value (e.g. `config["discord"]["permissions"]["allowed_roles"].append(...)`) would permanently corrupt `_DEFAULTS` for all future `load_config()` calls within the same process.

The `_deep_merge()` helper uses a shallow `.copy()` internally, which is safe because it reassigns keys rather than mutating nested dicts in place — and the `base` it receives is already a deep copy from `load_config()`.


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

### Transcriber Signal Handling (SIGTERM)

Docker sends `SIGTERM` to a container's PID 1 process on `docker stop`. If the process doesn't exit within the grace period (default 10 seconds), Docker sends `SIGKILL` — which cannot be caught and terminates the process immediately. Any in-progress transcription work would be lost.

The transcriber registers a `SIGTERM` handler (`_handle_sigterm`) that sets a module-level `_shutdown_requested` flag. The main polling loop checks this flag at the top of each iteration (`while not _shutdown_requested`). When SIGTERM arrives:

1. The handler sets the flag and logs an informational message
2. If a transcription is in progress, it runs to completion (sherpa-onnx C extension calls are not safe to interrupt mid-execution)
3. On the next loop iteration, the `while not _shutdown_requested` check fails and the loop exits
4. The database is closed cleanly and the process exits

The handler does NOT raise an exception (unlike SIGINT, which triggers `KeyboardInterrupt`). Raising from a signal handler is dangerous when the signal arrives during a C extension call (sherpa-onnx, SQLite) — the exception can propagate into a frame that doesn't expect it, causing undefined behaviour or a segfault. The flag-based approach is safe because it only checks at Python-level loop boundaries.

SIGINT (Ctrl-C) is still handled via `KeyboardInterrupt` in the loop's `except` clause, providing immediate interruption for interactive use.

### Single-Session Enforcement

This bot is designed for a single D&D group, so only one recording session should be active at any time — regardless of which Discord guild it was started in. `start_command` calls `get_any_active_session()` (which queries across ALL guilds, not just the calling guild) before creating a new session. If a session is already active, `/start` returns an ephemeral error ("A recording session is already in progress. Use `/stop` to end it first.") and does not create a new session record. This also eliminates the original Bug #9 (session ID collision risk from two starts in the same UTC second): since only one session can be active at a time, two sessions can never be created in the same second.

### Infrastructure Error Handling

Errors that cannot be reported to Discord (connection loss, crashes, model failures) are handled as follows:

- **Discord connection lost** — py-cord handles reconnection automatically with exponential backoff. Log a WARNING on each attempt, INFO when reconnected.
- **Voice channel join failure** — `start_command` creates the session record before attempting the voice channel join. If the join fails, it calls `fail_session()` (NOT `update_session_status(STATUS_FAILED)`) to set both `status=FAILED` and `ended_at`. Using `update_session_status` alone would leave `ended_at=NULL`, causing `get_active_session()` to keep returning the dead session and block all future `/start` commands in that guild.
- **Empty recording (nobody spoke)** — the recording callback detects zero audio files, calls `fail_session()` (sets FAILED + ended_at), and resolves the future with 0. `stop_command` informs the user and does not queue for transcription. The transcriber safety net also catches any sessions that slip past the callback.
- **Transcriber fails mid-job** — update session status to FAILED in SQLite. The bot can check for failed sessions and optionally notify the user. Log the full error traceback.
- **SQLite lock contention** — WAL mode allows concurrent reads. If a write fails due to a lock, retry with a short backoff (100ms, 3 attempts).
- **Model not found** — if `/data/models/whisper-{model_size}/` is missing or corrupt, the transcriber logs an ERROR and exits. The bot remains functional but transcription will not proceed until the model is restored. Run `docker compose exec transcriber python scripts/download_model.py --model {model_size}` to re-download.
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
