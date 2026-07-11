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

## 2026-07-11 -- Bug Fix: end_session sets STATUS_COMPLETE instead of STATUS_QUEUED

### Bug #3: end_session() prematurely marks session as COMPLETE

**Symptom:** When a user issued `/stop`, the session status was immediately set to `COMPLETE`. This caused two problems:

1. `/status` and `/session` displayed a checkmark ("Complete") for sessions that had not yet been transcribed — the user saw "complete" when transcription hadn't even started.
2. The architecture document describes a flow RECORDING -> QUEUED -> TRANSCRIBING -> COMPLETE, but `end_session()` skipped QUEUED entirely and jumped straight to COMPLETE, making the status flow incoherent.

**Root cause:** Bug #5 (2025-07-09) changed `end_session()` from `STATUS_QUEUED` to `STATUS_COMPLETE` because it was thought that "a session that just ended should not transition back to queued." However, QUEUED is not a terminal status — it is the signal for the transcriber to pick up the session. The session should only move to COMPLETE when the transcriber calls `set_transcript_path()` after successfully producing a transcript. The previous fix conflated "recording has ended" with "session is complete," but in this architecture those are different events separated by the entire transcription step.

**Fix (shared/database.py):**
- Changed `end_session()` to set status to `STATUS_QUEUED` instead of `STATUS_COMPLETE`.
- Updated the docstring to explain that QUEUED is the transition state for transcription pickup, and that COMPLETE is set later by `set_transcript_path()`.

**Fix (tests/test_database.py):**
- Updated `test_end_session` to assert `STATUS_QUEUED` instead of `STATUS_COMPLETE`.
- Updated the docstring to say "queues for transcription."

**Fix (docs/ARCHITECTURE.md):**
- Corrected the sessions table status enum from `CREATED / RECORDING / STOPPED / QUEUED / TRANSCRIBING / TRANSCRIBED / FAILED` to `RECORDING / QUEUED / TRANSCRIBING / COMPLETE / FAILED` to match the actual constants in the code.
- Updated the recording data flow: `/start` creates with status RECORDING (not CREATED), `/stop` calls `end_session()` which sets QUEUED (not STOPPED), and audio files are saved by the recording callback with status 'queued'.
- Updated the transcription data flow: the transcriber updates the audio file status to TRANSCRIBING (not the session status), and on completion calls `set_transcript_path()` which sets the session to COMPLETE (not TRANSCRIBED).
- Updated the delivery data flow: the delivery loop finds sessions with status COMPLETE and transcript_path set (not TRANSCRIBED).
- Updated the transcriber responsibilities list to say "Update session status to COMPLETE" (not TRANSCRIBED).

**Impact:** The status flow now correctly reflects the architecture: RECORDING -> QUEUED (on /stop) -> TRANSCRIBING (transcriber picks up) -> COMPLETE (transcriber finishes). Users see "Queued" after `/stop`, which accurately represents that transcription is pending. The delivery loop only picks up sessions after transcription is actually complete.

## 2026-07-11 — Bug Fix #1/#2: Timestamped Interleaved Transcripts

### Bug #1: Multiple audio files per session overwrite the same transcript file
### Bug #2: Delivery loop delivers partial transcripts

**Symptoms:**
1. When a session had multiple speakers (one WAV per user), the transcriber wrote each speaker's transcript to the same path (`{session_id}.txt`), so only the last speaker's transcript survived — all previous transcriptions were overwritten.
2. The delivery loop could pick up a session after the first file was done, delivering only that speaker's transcript and setting `thread_id` to prevent re-delivery. Remaining files' transcriptions were lost.

**Root cause:** The transcriber's main loop called `set_transcript_path()` (which sets `STATUS_COMPLETE` and triggers delivery) after each individual audio file, rather than waiting for all files in the session to be processed.

**Fix — Database (shared/database.py):**
- Added `discord_user_id` and `speaker_name` columns to the `audio_files` table schema. The bot resolves display names from the guild member cache and stores them — the transcriber has no Discord API access.
- Added `transcript_segments` table: `id`, `session_id`, `file_id`, `start_time`, `end_time` (nullable), `text`, `seq`. Stores timestamped chunks of recognised speech for chronological merging.
- Updated `add_audio_file()` to accept `discord_user_id` and `speaker_name` parameters.
- Added `add_transcript_segment()` to insert individual segments.
- Added `get_transcript_segments()` to retrieve all segments for a session, ordered by start_time then seq.
- Added `get_session_file_status()` to return counts of audio files by status (queued, transcribing, transcribed, failed, total) — used to determine when all files for a session are done.

**Fix — Bot (bot/commands.py):**
- Updated `recording_finished_callback` to resolve each speaker's Discord user ID and display name from the guild member cache, and pass them to `add_audio_file()`.

**Fix — Worker (transcriber/worker.py):**
- Added `TranscriptSegment` and `TranscriptionResult` dataclasses.
- Changed `transcribe()` return type from `Optional[str]` to `Optional[TranscriptionResult]`, containing both the full text and a list of timestamped segments.
- Added `_build_segments()` to group sherpa-onnx token-level timestamps into phrase-level segments by detecting sentence boundaries (punctuation tokens) and natural pauses (timestamp gaps > 1 second).

**Fix — Transcriber main loop (transcriber/main.py):**
- After each file is transcribed, segments are stored in the database and the file is marked as transcribed.
- The loop then checks `get_session_file_status()` — only when no files remain queued or transcribing does it proceed to merge.
- Added `_build_interleaved_transcript()` which queries all segments for the session, sorts by `start_time` then `seq`, and formats as `[HH:MM:SS] SpeakerName: dialogue`.
- `set_transcript_path()` is now called only once, after the merged transcript is written — fixing bug #2 (partial delivery) as well.
- Fallback: if no timestamped segments exist (e.g. recogniser returned empty timestamps), the merge falls back to per-speaker blocks using the `transcript_text` column, ordered by file ID.

**Fix — Documentation:**
- ARCHITECTURE.md: Updated audio_files table description (added discord_user_id, speaker_name columns), added transcript_segments table description, updated transcription data flow to describe segment capture and interleaved merge, removed stale "voice fingerprint alignment" reference and the voice fingerprinting open question.
- PROJECT.md: Replaced "speaker diarisation" with accurate description of per-speaker capture and chronological merge.

**Tests:**
- Added 27 new tests (148 total, all passing):
  - `test_database.py`: segment insertion/retrieval, session file status counts, audio file with speaker info.
  - `test_transcript_merge.py`: interleaved merge with multiple speakers, tie-breaking by seq, fallback to per-speaker blocks, empty segments, timestamp formatting.
  - `test_transcriber_worker.py`: updated to use `TranscriptionResult` return type, added segment grouping tests.

**Impact:** Multi-speaker sessions now produce a single interleaved transcript with all speakers' dialogue in chronological order, each line timestamped and labelled with the speaker's name. The delivery loop only fires once, after the complete transcript is written.

## 2026-07-11 — Bug Fix #4: Model size hardcoded to "small"

### Bug #4: Worker and download script ignore config model_size

**Symptom:** The `transcriber.model` config key (default: `small`) allowed selecting a different Whisper model size (tiny, base, small, medium, large-v3), but the value was never passed to the components that actually load and download the model. Changing it in `config.yaml` had no effect — the worker always loaded `small-encoder.onnx`, `small-decoder.onnx`, `small-tokens.txt`, and the download script always fetched the small model tarball.

**Root cause:** The config infrastructure (`Config.model_size` and `Config.model_path` properties) was already implemented in `shared/config.py`, but three call sites hardcoded "small":

1. `transcriber/worker.py` `load_model()` — filenames constructed with literal "small" prefix.
2. `transcriber/worker.py` `download_model()` — `REQUIRED_FILES` list and default URL both baked in "small".
3. `scripts/download_model.py` — `DEFAULT_MODEL_URL`, `REQUIRED_FILES`, and `EXPECTED_SHA256` all hardcoded for the small model only.
4. `transcriber/main.py` — main loop called `download_model()` and `TranscriptionWorker()` without passing `model_size`.

**Fix — transcriber/worker.py:**
- Added `model_size: str = "small"` parameter to `TranscriptionWorker.__init__()`, stored as `self.model_size`.
- `load_model()` now uses `self.model_size` to construct filenames: `{size}-encoder.onnx`, `{size}-decoder.onnx`, `{size}-tokens.txt`.
- `download_model()` now accepts a `model_size` parameter. `REQUIRED_FILES` is built dynamically via a `_required_files(model_size)` helper. The default URL is built from a template: `sherpa-onnx-whisper-{model_size}.tar.bz2`.
- `EXPECTED_SHA256` changed from a single string to a dict keyed by model size. Only "small" has a verified hash. Unlisted sizes download successfully but skip SHA-256 verification with a WARNING log.

**Fix — scripts/download_model.py:**
- Same structural changes as worker.py: `EXPECTED_SHA256` is now a dict, `_required_files()` builds the list dynamically, `DEFAULT_MODEL_URL_TEMPLATE` uses f-string interpolation.
- `download_model()` accepts `model_size` parameter.
- Added `--model` / `-m` CLI flag to select model size from the command line.
- SHA-256 verification skips with a warning for sizes without a known hash.

**Fix — transcriber/main.py:**
- Main loop now passes `config.model_size` to both `download_model()` and `TranscriptionWorker()`.

**Fix — Documentation:**
- ARCHITECTURE.md: Updated model description to note configurable size, updated directory tree to show `whisper-{model_size}/`.
- IMPLEMENTATION_NOTES.md: Updated model weight persistence section and troubleshooting to reflect configurable model size and `--model` CLI flag.

**Tests:** 158/158 passing (10 new tests added):
- `test_transcriber_worker.py`: `test_load_model_uses_model_size_in_filenames` (base), `test_load_model_default_small_filenames`, `test_download_model_base_size_uses_base_filenames`, `test_download_model_default_url_uses_model_size`, `test_expected_sha256_dict_has_known_hashes`, `test_unknown_model_size_skips_hash_verification`.
- `test_download_model.py`: `test_expected_sha256_dict_has_known_hashes`, `test_unknown_model_size_skips_hash_verification`, `test_main_model_flag`, `test_main_short_model_flag`.

**Impact:** Users can now select any Whisper model size via `config.yaml` (`transcriber.model: medium`) or the CLI (`python scripts/download_model.py --model medium`). The worker, download script, and transcriber main loop all respect the setting. SHA-256 verification is enforced for known sizes (currently "small" only) and gracefully skipped with a warning for others.
