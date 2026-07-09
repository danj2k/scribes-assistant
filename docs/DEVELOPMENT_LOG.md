# Scribe's Assistant — Development Log

## 2025-07-08 — Project Inception

- Created project repository structure
- Wrote project documentation (PROJECT.md, ARCHITECTURE.md, IMPLEMENTATION_NOTES.md)
- Confirmed key design decisions:
  - py-cord WAV output accepted (no raw Opus available without alpha library)
  - Single-session only (no concurrent session support needed)
  - loud.cpp replaced with sherpa-onnx for transcription
  - Decoupled transcriber worker (polls queue, loads heavy libs only when needed)
  - Timestamp-based session IDs (filesystem-safe ISO 8601)
  - Docker Compose with separate bot and transcriber containers
  - Docker secrets for bot token (file-based, not environment variables)
  - sherpa-onnx Whisper small model (suitable for i3-8100)
  - Configurable transcript delivery channel (default: command channel)
  - Auto-join voice channel on /start
  - Lexicon feature retained (initial prompt injection + fuzzy post-correction + /lexicon commands)

## 2025-07-09 -- Bug Fixes: DeliveryLoop Construction and Lifecycle

Two bugs were identified and fixed in the bot startup sequence:

### Bug #1: DeliveryLoop constructed before bot exists

**Symptom:** `bot.config` referenced in `DeliveryLoop.__init__` but `bot` had not been created yet.
**Cause:** `main()` created `DeliveryLoop` before `ScribesBot` -- the lexicon file path came from `bot.config`, which was None.
**Fix (bot/main.py):** Moved `DeliveryLoop(bot, db, logger)` after `bot = ScribesBot(...)` so the bot object is available. Also corrected constructor args from `(db=db)` to `(bot, db, logger)` to match `__init__` signature.

### Bug #2: DeliveryLoop missing is_running / start()

**Symptom:** `on_ready()` called `self.delivery_loop.is_running` and `self.delivery_loop.start()`, but `DeliveryLoop` was a plain class with no such methods. Would crash with `AttributeError` at runtime.
**Cause:** `DeliveryLoop` was designed as a simple async class with a `run()` method, but `on_ready()` expected a `discord.ext.tasks.Loop`-style interface.
**Fix (bot/delivery.py):** Added `is_running` property (checks `_task is not None and not _task.done()`) and `start()` method (wraps `asyncio.create_task(self.run())`). The `_task` attribute tracks the background task lifecycle. `start()` is idempotent -- safe to call multiple times.

## 2025-07-09 -- Bug Fix #3: Transcriber calling nonexistent methods on TranscriptionWorker

### Bug #3: Transcriber calls nonexistent methods on TranscriptionWorker

**Symptom:** The transcriber service would crash on every file it attempted to process with AttributeError/TypeError, never completing a transcription.

**Cause:** `transcriber/main.py` called methods and accessed return values that do not exist on `TranscriptionWorker`:
1. Called `worker.transcribe_file(filepath)` but the method is `worker.transcribe(audio_path)`.
2. Accessed `result["text"]` but `transcribe()` returns a plain string, not a dict.
3. Called `worker.close()` on shutdown but `TranscriptionWorker` has no `close()` method.
4. Never called `worker.load_model()` after construction, so the internal `self.recognizer` was always `None`, causing `RuntimeError("Model not loaded")` on every attempt.

**Fix (transcriber/main.py):**
- Changed `worker.transcribe_file(filepath)` to `worker.transcribe(filepath)`.
- Replaced `result["text"]` with direct use of the returned string, adding a `None` check.
- Removed the `worker.close()` call (nothing to clean up -- sherpa-onnx models are garbage-collected).
- Added `worker.load_model()` after construction so the sherpa-onnx recognizer is ready before the polling loop begins.

### Verification

All changes to `transcriber/main.py` verified via diff:
- `worker.load_model()` added (line 55)
- `worker.transcribe(filepath)` with `None` check (lines 75-77)
- `worker.close()` removed (line 107 is now just `db.close()`)

File backed up to `transcriber/main.py.bak` before patching.

## 2025-07-09 -- Bug Fix #4: model_path uses wrong config key and hardcoded model size

### Bug #4: model_path uses wrong config key and a relative path

**Symptom:** `Config.model_path` returned a relative path `data/models/whisper-small` with
the model size hardcoded into the directory name. Two independent problems:

1. **Wrong config key:** The property looked up top-level key `"model_dir"` which does not
   exist in `_DEFAULTS` (the transcriber config lives under `transcriber.*`). The lookup
   always fell back to the hardcoded default string.

2. **Hardcoded model size in path:** The default string `"data/models/whisper-small"` baked
   in `whisper-small`, making the directory name independent of the `model` config setting.
   A user who sets `model: medium` still gets a path pointing at `whisper-small`.

3. **Relative path:** Default was `"data/models/whisper-small"` while every other path in
   the config uses an absolute path (`/data/queue.db`, `/data/lexicon.yaml`, etc.).

**Fix (shared/config.py):**
- Added `model_dir` key to `_DEFAULTS["transcriber"]` with default `/data/models`.
- Changed `model_path` property to compose the full path dynamically:
  `os.path.join(base, f"whisper-{self.model_size}")` where `base` comes from
  `transcriber.model_dir` and `model_size` from `transcriber.model`.
- This means changing `model: medium` in config.yaml automatically produces the correct
  path `/data/models/whisper-medium`.

**Fix (config.yaml.example):**
- Added `model_dir: /data/models` to the transcriber section with explanatory comment.

**Verification:**
- All 92 tests pass (0 regressions).
- Config loads correctly: `model_path` now computes from `model_dir` + `model_size`.

### Bug #7: add_transcript is a no-op (HIGH PRIORITY)
- **File**: `shared/database.py`, `transcriber/main.py`
- **Issue**: `Database.add_transcript()` accepted `text` and `channel_id` parameters but discarded them, only updating the status. Transcribed text was never stored in the database.
- **Fix**:
  1. Added `transcript_text TEXT` column to the `audio_files` table schema to store the transcribed text.
  2. Updated `add_transcript()` to execute `UPDATE audio_files SET status = 'transcribed', transcript_text = ? WHERE id = ?`, persisting the transcription result.
  3. Updated `transcriber/main.py` to call `db.add_transcript(file_id, session_id, text)` instead of `db.update_file_status(file_id, "transcribed")` on successful transcription.
- **Impact**: Transcribed text is now stored in the database and available for query, search, and future delivery. Both file-based and database-based transcript access paths work.

### Bug #5: Sessions created with STATUS_QUEUED instead of STATUS_RECORDING
- **File**: `shared/database.py`
- **Issue**: `create_session()` inserted sessions with `STATUS_QUEUED` but the bot has already joined a voice channel and started recording at that point. The `/session` list and `/status` commands display this status, so users saw "Queued" during an active recording session.
- **Additional bug found**: `end_session()` also used `STATUS_QUEUED` as the terminal status — a session that just ended should not transition back to "queued". This was corrected to `STATUS_COMPLETE`.
- **Fix (shared/database.py)**:
  1. Changed `create_session()` to insert with `STATUS_RECORDING` instead of `STATUS_QUEUED` (line 83).
  2. Changed `end_session()` to set status to `STATUS_COMPLETE` instead of `STATUS_QUEUED` (line 131).
  3. Updated schema default from `'queued'` to `'recording'` for consistency.
  4. Updated docstring to reflect the new initial status.
- **Fix (tests/test_database.py)**:
  1. Updated `test_create_session` to assert `STATUS_RECORDING`.
  2. Updated `test_get_queued_sessions` to explicitly set sessions to queued before testing (since sessions now start as recording).
  3. Updated `test_end_session` to also verify status becomes `STATUS_COMPLETE`.
- **Impact**: Sessions now correctly show "Recording" in the UI immediately after creation. Ended sessions correctly show "Complete" rather than reverting to "Queued".

### Bug #6: Lexicon fuzzy threshold from config is never used
- **Files**: `shared/lexicon.py`, `bot/main.py`, `bot/delivery.py`
- **Issue**: `Lexicon.correct()` hardcoded its fuzzy matching threshold at `max(len(key) // 2, 1)` (50% of key length). The `fuzzy_threshold` setting in `config.yaml` and the `lexicon_threshold` property on `Config` were silently ignored — the parameter existed in config but was never threaded through to the Lexicon class.
- **Fix (shared/lexicon.py)**:
  1. Added `fuzzy_threshold: float = 0.2` parameter to `Lexicon.__init__()` (default matches `_DEFAULTS`).
  2. Clamped the value to `[0.0, 1.0]` and stored as `self._fuzzy_threshold`.
  3. Updated `correct()` to use `max(int(len(key) * self._fuzzy_threshold), 1)` instead of `max(len(key) // 2, 1)`.
- **Fix (bot/main.py)**: Changed `Lexicon(config.lexicon_file)` to `Lexicon(config.lexicon_file, config.lexicon_threshold)`.
- **Fix (bot/delivery.py)**: Changed `Lexicon(lexicon_file)` to `Lexicon(lexicon_file, bot.config.lexicon_threshold)`.
- **Impact**: The fuzzy correction threshold is now configurable via `config.yaml` (`lexicon.fuzzy_threshold`). Default value of `0.2` means corrections are only offered when the edit distance is within 20% of the key length — tighter than the old hardcoded 50%, reducing false-positive corrections.

### Bug #8: Commands have no permission checks
- **Files**: `bot/commands.py`, `tests/test_commands.py`
- **Issue**: All bot commands (`/start`, `/stop`, `/status`, `/session`, `/lexicon_add`, `/lexicon_remove`, `/lexicon_list`) were accessible to every server member regardless of their role. There was no way for server administrators to restrict command access via the `restrict_commands` and `allowed_roles` configuration options. The config fields existed and were read, but never enforced.
- **Root cause**: The permission-checking logic was never implemented — each command handler simply proceeded directly to its business logic.
- **Fix (bot/commands.py)**:
  1. Added `_check_permission(interaction, config) -> tuple[bool, str | None]` helper function (line 21). Returns `(True, None)` when access is granted, or `(False, error_message)` when denied.
  2. The helper checks `config.restrict_commands` — if `False`, all users are allowed. If `True` but `config.allowed_roles` is empty, all users are allowed. Otherwise, the user's role names (case-insensitive) and role IDs are compared against `config.allowed_roles`.
  3. Added permission check calls at the top of 7 command handlers: `start_command`, `stop_command`, `status_command`, `session_command`, `lexicon_add`, `lexicon_remove`, `lexicon_list`.
  4. Left `/help` and `/invite` unrestricted — information commands should always be accessible.
- **Fix (tests/test_commands.py)**: Created new test file with 10 test cases covering:
  - Restrictions disabled (`restrict_commands=False`)
  - Empty allowed roles list
  - Role name match (including case-insensitive)
  - Role ID match
  - No roles → denied
  - Unrelated role → denied
  - Multiple roles with one match
  - Mixed role types in config
  - String role ID does not falsely match numeric role ID
- **Impact**: Server administrators can now use `restrict_commands: true` and `allowed_roles` in `config.yaml` to limit which Discord roles can execute bot commands. Unconfigured servers (the default) continue to allow all users.

### Bug #9 — Unused Bot Token in Transcriber (Severity: LOW)

- **File**: `transcriber/main.py`
- **Issue**: `_read_bot_token()` function read and validated the Discord bot token, but the transcriber never used it. The transcriber communicates with the bot via SQLite status flags — it marks files as "transcribed" and stores transcript text in the database. The bot's delivery loop picks up the completed transcripts. The token was dead code with a side effect: startup would fail with `RuntimeError` if the token file was missing, even though the token served no purpose.
- **Root cause**: The transcriber was originally designed to potentially post directly to Discord, but that responsibility was moved to the bot's delivery loop. The token code was left behind.
- **Fix**: Removed `_read_bot_token()` function entirely, removed the `bot_token = _read_bot_token()` call and its comment, removed unused `import os`. The transcriber now starts without requiring any Discord credentials.
- **Impact**: The transcriber is a simpler, more focused component. It no longer needs Docker secrets for the bot token, and its startup is no longer gated on token availability.
- **Tests**: 102/102 passing (no new tests needed — the dead code had no callers to test).

### Bug #10 — Non-Deterministic Tie Breaking in Lexicon Correct (Severity: MEDIUM)

- **File**: `shared/lexicon.py`
- **Issue**: When multiple lexicon terms tied on Levenshtein distance from a query word, the chosen result depended on dictionary iteration order — which is non-deterministic across Python versions, runs, and insertion orders. This meant the same transcription could produce different corrected terms on different runs.
- **Root cause**: The `correct()` method used a strict `dist < best_distance` comparison, so the first term to achieve the best distance won, and all subsequent ties were silently skipped.
- **Fix**: Added deterministic tie-breaking in `correct()` when distances are equal:
  1. Prefer the shorter term (fewer characters wins).
  2. If lengths are also equal, prefer alphabetical order (case-insensitive).
- **Tests**: Added `TestCorrectTieBreaking` class (2 tests):
  - `test_shorter_term_wins_on_tie`: "fireball" (len 8) vs "firebll" (len 7) both at distance 1 from "firebal" — "firebll" wins.
  - `test_alphabetical_on_equal_distance_and_length`: "fireball" vs "firebalx" both at distance 1 and length 8 from "firebal" — "fireball" wins alphabetically.
- **Impact**: Lexicon correction is now fully deterministic. The same transcription input will always produce the same corrected output.
- **Tests**: 104/104 passing.
