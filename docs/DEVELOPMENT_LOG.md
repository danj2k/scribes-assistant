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
  4. Left `/help` unrestricted — informational command, no security concern.
  5. `/invite` was also left unrestricted at this point (later addressed in Bug #22).
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

## 2026-07-11 — Bug Fix #5: stop_command doesn't await recording callback

### Bug #5: Recording after-callback never executes — audio files never saved

**Symptom:** When a user issued `/stop`, the session was marked QUEUED but no audio files were ever registered in the database. The transcriber found zero files, produced an empty transcript, and the user received nothing.

**Root cause:** The bug was worse than a simple race condition. py-cord 2.8.0's `start_recording(sink, callback)` stores the callback as `AudioReader.after`. When `stop_recording()` is called, py-cord invokes `after(exc)` **synchronously** — passing the exception (or None), NOT the sink. The callback must be a regular sync callable.

The old code passed an **async function** `callback(sink)` as the after-callback. Two problems:
1. py-cord called `after(exc)` passing the exception as the `sink` parameter — the wrong argument entirely.
2. Calling an async function from sync code creates a coroutine object that is **never awaited** — so the audio processing (file writing, DB registration) never ran at all.

Even if the callback had been invoked correctly, `stop_command` called `end_session()` immediately after `stop_recording()` returned, without waiting for the callback to finish. This was a secondary race condition on top of the primary bug.

**Fix — bot/commands.py:**
- Replaced the old `recording_finished_callback` (which returned an async function) with `make_recording_after_callback()` — a factory that returns a **sync** callable matching py-cord's `AfterCallback` signature `(exc) -> None`, plus an `asyncio.Future` for awaiting completion.
- The factory captures the sink, session metadata, event loop, and guild at creation time (called from `start_command`). This is necessary because py-cord does not pass the sink to the after-callback.
- The sync callback schedules the async audio-processing coroutine via `asyncio.run_coroutine_threadsafe(coro, loop)` — safe regardless of whether py-cord invokes the callback from the event loop thread or a socket listener thread.
- The async coroutine writes WAV files, resolves speaker display names from the guild member cache, and registers audio files in the database (with `discord_user_id` and `speaker_name`).
- On success, the future's result is set to the count of saved files. On exception, the future's exception is set so the caller can detect failures.
- `start_command` now creates the callback via the factory and stores the future on `self._pending_recording`.
- `stop_command` calls `vc.stop_recording()`, then `await`s the future with a 30-second timeout. Only after the future resolves (or times out) does it call `end_session()`. This guarantees the transcriber will find all audio files already registered when it picks up the QUEUED session.
- On timeout or callback exception, `stop_command` logs the error and proceeds with `end_session()` anyway — partial or no audio is better than a permanently stuck session.

**Fix — Documentation:**
- ARCHITECTURE.md: Updated recording data flow (steps 2-7) to describe the sync after-callback, `run_coroutine_threadsafe` scheduling, and the future await pattern. Updated bot responsibilities to mention speaker name resolution at recording stop time and awaiting audio file registration before QUEUED transition.
- IMPLEMENTATION_NOTES.md: Added "py-cord Recording After-Callback" section documenting the py-cord 2.8.0 after-callback contract (sync, receives exception not sink), the `make_recording_after_callback` pattern, and the rationale for `run_coroutine_threadsafe`. Updated SHA-256 section to reflect the dict-keyed `EXPECTED_SHA256`.

**Tests:** 169/169 passing (11 new tests in `tests/test_recording_callback.py`):
- `test_returns_sync_callable_and_future`: Factory returns a callable and a future.
- `test_callback_writes_audio_files`: Audio data written to disk for each speaker.
- `test_callback_registers_audio_files_in_db`: Audio files registered with correct speaker info.
- `test_callback_resolves_speaker_names`: Display names resolved from guild member cache.
- `test_callback_resolves_unknown_speaker`: Unknown user ID falls back to "Unknown".
- `test_future_resolves_with_file_count`: Future result is the number of saved files.
- `test_future_resolves_on_error`: Future resolves even when processing raises.
- `test_callback_with_empty_sink`: No audio data — future resolves with 0, no crash.
- `test_callback_handles_missing_member`: Member not in cache — falls back to "Unknown".
- `test_stop_command_awaits_future`: `stop_command` awaits the callback future before `end_session`.
- `test_stop_command_proceeds_on_timeout`: `stop_command` proceeds with `end_session` on future timeout.

**Impact:** Audio files are now reliably saved and registered in the database before the session transitions to QUEUED. The transcriber always finds the expected files. The fix also corrects a fundamental misunderstanding of py-cord's after-callback API — the old code's async callback was never invoked at all, meaning no recording ever worked correctly.

---

## 2026-07-11 — Bug Fix #6: Empty recording creates stuck session with no feedback

### Bug #6: Nobody speaks during recording — session stuck forever, no user feedback

**Symptom:** When a user starts recording, says nothing, and issues `/stop`, the recording callback runs but finds `sink.audio_data` empty. Zero WAV files are written, zero audio_files rows are registered. `end_session()` sets the session to QUEUED, but the transcriber's `get_next_queued_file()` always returns None (no files to transcribe), so it polls forever. The session is stuck in QUEUED with no transcript, no delivery, and no feedback to the user. `/status` shows it as "queued" indefinitely.

**Root cause:** Two issues:
1. The recording callback did not detect the zero-audio case. It wrote 0 files, logged "saved 0 audio file(s)", and returned. `stop_command` called `end_session()` unconditionally — moving a session with no audio into QUEUED.
2. The transcriber had no safety net for sessions stuck in QUEUED with zero audio files. `get_next_queued_file()` returns None when there are no queued files, so the transcriber just sleeps and polls — it never checks whether the session itself should be abandoned.

**Fix — shared/database.py:**
- Added `fail_session(session_id)`: sets status to FAILED and sets `ended_at`. Setting `ended_at` is critical — `get_active_session()` filters on `ended_at IS NULL`, so without it, a failed session would block future recordings in the guild.
- Added `get_queued_sessions_without_files()`: returns sessions in QUEUED status that have zero rows in `audio_files`. The transcriber calls this each poll cycle as a safety net.

**Fix — bot/commands.py (recording callback):**
- After writing audio files, if `audio_count == 0`, the callback calls `db.fail_session(session_id)` and logs a warning. The future result is the audio file count (0 for empty), so `stop_command` can distinguish empty recordings from real ones.

**Fix — bot/commands.py (stop_command):**
- `stop_command` now captures the future result (audio file count). If 0, it sends an ephemeral message informing the user that no audio was captured and no transcript will be generated, then returns without calling `end_session()`. The callback has already called `fail_session()`.

**Fix — transcriber/main.py:**
- Before calling `get_next_queued_file()`, the main loop queries `get_queued_sessions_without_files()` and marks each result as FAILED. This catches edge cases where the callback's `fail_session()` call was missed (e.g. callback timed out, exception in processing, or all audio files lost after writing).

**Tests:** 169 + 9 new = 178 tests:
- `test_fail_session_sets_status_and_ended_at`: fail_session sets FAILED status and ended_at.
- `test_fail_session_clears_active`: Failed sessions are not returned by get_active_session.
- `test_get_queued_sessions_without_files_finds_empty`: Queued session with no files is returned.
- `test_get_queued_sessions_without_files_excludes_with_files`: Queued session with files is not returned.
- `test_get_queued_sessions_without_files_excludes_non_queued`: Non-queued sessions are excluded.
- `test_get_queued_sessions_without_files_excludes_failed`: Already-failed sessions are excluded.
- `test_get_queued_sessions_without_files_multiple`: Multiple empty queued sessions all returned.
- `test_callback_with_no_audio_data` (updated): Now verifies fail_session is called and future result is 0.
- `test_future_returns_audio_count`: Future result is the audio file count; non-empty recordings do NOT call fail_session.

**Impact:** Empty recordings now fail gracefully with user feedback instead of hanging forever. The session is marked FAILED with `ended_at` set, so it doesn't block future recordings. The transcriber safety net catches any edge cases that slip past the callback.

---

## 2026-07-11 — Bug Fix #7: Shallow copy of _DEFAULTS corrupts global config defaults

### Bug #7: load_config() shallow-copies _DEFAULTS — nested mutations persist across calls

**Symptom:** Any code that mutated a nested dict in the config returned by `load_config()` would silently corrupt the module-level `_DEFAULTS` dictionary. All subsequent `load_config()` calls within the same process would return the corrupted defaults — even if no YAML file was loaded. This could cause cascading configuration errors (e.g. allowed_roles accumulating stale entries, permission settings bleeding across test cases, etc.).

**Root cause:** `load_config()` used `result = _DEFAULTS.copy()` — a shallow copy. While the top-level dict was copied, nested dicts (`discord`, `permissions`, `lexicon`, `transcriber`, etc.) were shared references to the same objects in `_DEFAULTS`. The `_deep_merge()` function was also called on this shallow copy, but `_deep_merge` itself is safe because it reassigns keys rather than mutating nested dicts in place — the bug was purely in the initial shallow copy.

**Fix (shared/config.py):**
- Added `import copy` at module level.
- Changed `result = _DEFAULTS.copy()` to `result = copy.deepcopy(_DEFAULTS)` in `load_config()`.
- `_deep_merge()` was left unchanged — its shallow `.copy()` is safe because it only reassigns keys, and the `base` it receives from `load_config()` is already a deep copy.

**Tests (tests/test_config.py):**
- Added `test_nested_defaults_are_independent`: loads config (no YAML), mutates `config["discord"]["permissions"]["allowed_roles"]` by appending a value, then asserts that `_DEFAULTS` is unmodified and that a fresh `load_config()` call returns the original empty list. This test would fail with the old shallow copy.

**Impact:** Config defaults are now fully isolated per `load_config()` call. Mutations to returned config dicts cannot corrupt global state. 178/178 tests pass.

---

## Bug #8 — Failed voice join leaves `ended_at = NULL`, blocking all future sessions in that guild

**Date:** 2026-07-11

**Root cause:** In `start_command` (`bot/commands.py`), when the voice channel join fails, the error handler called `db.update_session_status(session_id, STATUS_FAILED)`. This sets only the `status` column — it does NOT set `ended_at`. Since `get_active_session()` filters on `ended_at IS NULL`, the dead session keeps appearing as "active", and all future `/start` commands in that guild fail the "there's already an active session" check. The guild is permanently blocked from recording until manual database intervention.

**Fix:** Changed the error handler to call `db.fail_session(session_id)` (added in Bug #6 fix) instead. `fail_session()` sets BOTH `status = STATUS_FAILED` AND `ended_at = now()`, ensuring `get_active_session()` no longer returns the dead session.

One-line code change in `bot/commands.py`:
- `db.update_session_status(session_id, STATUS_FAILED)` → `db.fail_session(session_id)`

**Why `fail_session()` already existed:** It was added for Bug #6 (empty recordings) with the exact same pattern — any session that should never produce a transcript must have `ended_at` set to avoid blocking `get_active_session()`. Bug #8 was another instance of the same class of bug: using `update_session_status` when `ended_at` also needs to be set.

**Tests (tests/test_database.py):**
- `test_update_status_failed_does_not_clear_active`: Demonstrates the bug directly — `update_session_status(STATUS_FAILED)` leaves `ended_at=NULL` and `get_active_session()` still returns the session. Then shows `fail_session()` fixes it by setting `ended_at` and clearing the active session.
- `test_failed_voice_join_allows_new_session`: End-to-end regression — after a failed session (via `fail_session`), a new session in the same guild can be created and is returned by `get_active_session()`.

**Impact:** Failed voice joins no longer block future recordings. 180/180 tests pass.

---

## Bug #9 — Single-Session Enforcement (Session ID Collision)

**Date:** 2026-07-11

**Root cause:** `start_command` only checked for active sessions within the same guild (`get_active_session(guild_id)`). Two sessions started in the same UTC second (even in different guilds) would generate the same session ID (`YYYY-MM-DD_HH-MM-SS`), causing a PRIMARY KEY collision. More fundamentally, this bot is designed for a single D&D group — only one session should run at a time, period.

**Fix:** Added `get_any_active_session()` to `shared/database.py` — queries for any non-ended session across ALL guilds (no guild_id filter). `start_command` now calls this instead of the per-guild `get_active_session()` before creating a new session. If any session is active anywhere, `/start` returns an ephemeral error and does not create a new session record. This eliminates the collision risk entirely: two sessions can never be created in the same UTC second because the second `/start` is rejected before it reaches `create_session()`.

**Files changed:**
- `shared/database.py`: Added `get_any_active_session()` method
- `bot/commands.py`: `start_command` now calls `get_any_active_session()` instead of `get_active_session(guild_id)`
- `docs/ARCHITECTURE.md`: Updated recording flow steps 1-2 to document the global active-session check
- `docs/IMPLEMENTATION_NOTES.md`: Added "Single-Session Enforcement" section

**Tests (tests/test_database.py):**
- `test_get_any_active_session_finds_across_guilds`: Finds an active session regardless of guild
- `test_get_any_active_session_none`: Returns None when no session is active
- `test_get_any_active_session_blocks_different_guild`: Regression — active session in guild A is visible globally, blocks guild B
- `test_get_any_active_session_excludes_ended`: Ended sessions are not returned
- `test_get_any_active_session_excludes_failed`: Failed sessions are not returned
- `test_single_session_allows_new_after_previous_ends`: After ending, a new session in a different guild can be started

**Impact:** Only one recording session can be active at a time. Session ID collisions are impossible. 186/186 tests pass.

---

## Bug #10 — bot_token returns None silently

**Date:** 2026-07-11

**Root cause:** The `Config.bot_token` property in `shared/config.py` only returned a value when the token file existed. If the file was missing (e.g. Docker secret not mounted, wrong path), the property fell through and returned `None` implicitly. `bot/main.py` then called `bot.run(None)`, which fails inside py-cord with a cryptic traceback that gives no hint about the actual cause (missing token file).

**Fix — shared/config.py:**
- `bot_token` property now raises `FileNotFoundError` with an actionable message when the token file does not exist (includes the expected path and how to create it).
- Also raises `ValueError` when the file exists but is empty or whitespace-only.
- Both messages include the token file path so the user knows exactly what to fix.

**Fix — bot/main.py:**
- `main()` now validates the token early by accessing `config.bot_token` in a try/except block before calling `bot.run()`.
- On `FileNotFoundError` or `ValueError`, it logs a clear error message and exits with status 1, rather than letting py-cord produce a confusing traceback.

**Tests (tests/test_config.py):**
- `test_bot_token_reads_file`: Token file with content returns the stripped token.
- `test_bot_token_strips_whitespace`: Leading/trailing whitespace is stripped.
- `test_bot_token_raises_when_file_missing`: Missing file raises `FileNotFoundError` (regression test for Bug #10).
- `test_bot_token_raises_when_file_empty`: Empty file raises `ValueError`.
- `test_bot_token_raises_when_file_whitespace_only`: Whitespace-only file raises `ValueError`.

Also fixed a pre-existing syntax bug in `test_config_guild_id`: the `discord` dict literal was missing a closing brace, which would have been a syntax error if the test had been run with a YAML writer that didn't tolerate the malformed input.

**Impact:** Missing or empty bot tokens now produce a clear, actionable error message at startup instead of a cryptic py-cord traceback. 191/191 tests pass.

## 2025-07-11 — Bug #11: Transcriber Doesn't Handle SIGTERM for Graceful Shutdown

**Symptom:** Running `docker stop scribes-transcriber` would kill the process with SIGKILL after the 10-second grace period. Any in-progress transcription work was lost, and the database connection was not closed cleanly.

**Root cause:** The transcriber's main loop in `transcriber/main.py` only caught `KeyboardInterrupt` (SIGINT). Docker sends `SIGTERM` on `docker stop`, which Python's default handler terminates the process with — but not before Docker escalates to `SIGKILL` after the grace period. There was no `SIGTERM` handler, so the process had no opportunity to shut down gracefully.

**Fix — transcriber/main.py:**
- Added a module-level `_shutdown_requested` flag and a `_handle_sigterm()` signal handler that sets the flag and logs an informational message.
- Registered the handler with `signal.signal(signal.SIGTERM, _handle_sigterm)` before the main loop.
- Changed the loop condition from `while True` to `while not _shutdown_requested` so the loop exits after the current operation completes.
- After the loop, logs whether shutdown was triggered by SIGTERM or SIGINT, then closes the database.

**Design decision — flag, not exception:** The SIGTERM handler sets a flag rather than raising an exception. Raising from a signal handler is dangerous when the signal arrives during a C extension call (sherpa-onnx inference, SQLite writes) — the exception can propagate into a C frame that doesn't expect it, causing undefined behaviour or a segfault. The flag-based approach is safe because it only checks at Python-level loop boundaries (between transcriptions), ensuring the current file always completes or fails cleanly. This means a transcription in progress when SIGTERM arrives will run to completion before shutdown — acceptable since most transcriptions finish well within Docker's 10-second grace period, and partial work is worse than a few seconds of delay.

SIGINT (Ctrl-C) is still handled via `KeyboardInterrupt` for interactive use, providing immediate interruption.

**Tests (tests/test_sigterm_handling.py):**
- `test_handler_sets_shutdown_flag`: Calling `_handle_sigterm` sets `_shutdown_requested` to True.
- `test_handler_is_idempotent`: Multiple calls keep the flag set.
- `test_handler_does_not_raise`: The handler must not raise (it runs in a signal context).
- `test_flag_resets`: The flag can be reset for clean test isolation.

**Impact:** `docker stop scribes-transcriber` now triggers a clean shutdown — the current transcription (if any) completes, the database is closed, and the process exits 0. 195/195 tests pass.


## Bug #12 — tarfile extraction without path traversal protection (moderate)

**Commit:** (pending)

**Problem:** Both `transcriber/worker.py` (`download_model()`) and `scripts/download_model.py` extracted tar archives by calling `tar.extract(member, path)` in a loop with no path validation. A malicious or corrupted archive could contain members with paths like `../../../etc/passwd` or absolute paths like `/tmp/evil`, allowing files to be written outside the model directory — a classic path traversal vulnerability.

The old extraction code only stripped the top-level directory component from each member name, then passed it directly to `tarfile.extract()`. No check was made on whether the resulting path stayed within the extraction directory.

**Fix:** Created `shared/tar_utils.py` with two functions:

- `_validate_member_path(member, extract_dir, strip_components)` — validates a single tar member's destination path. Rejects absolute paths, `..` traversal, and symlinks with targets that escape the extraction directory. Returns `None` for top-level directory entries (which should be skipped) or the resolved destination `Path`.

- `safe_extract_members(tar, extract_dir, strip_components=1)` — iterates all members, validates each one, and extracts only those that pass validation. On Python 3.12+, passes `filter='data'` to `tarfile.extract()` for an additional safety layer. On Python 3.11 (the Docker base image), the manual validation in `_validate_member_path()` provides equivalent protection.

Both `transcriber/worker.py` and `scripts/download_model.py` now import and call `safe_extract_members()` instead of the bare `tar.extract()` loop. The `strip_components=1` parameter preserves the previous behaviour of stripping the top-level archive directory.

Extraction is fail-closed: if any member has an unsafe path, a `ValueError` is raised and extraction halts immediately. The caller catches this and re-raises as `RuntimeError` with a descriptive message.

**Tests (tests/test_tarfile_safety.py):**
- `test_normal_relative_path_strips_one_component` — normal `dir/file.onnx` extracts as `file.onnx`
- `test_nested_relative_path_strips_one_component` — nested paths preserved after stripping
- `test_top_level_directory_entry_returns_none` — top-level dir entries are skipped
- `test_absolute_path_rejected` — `/etc/passwd` raises `ValueError`
- `test_parent_traversal_rejected` — `../../etc/passwd` raises `ValueError`
- `test_parent_traversal_after_strip_rejected` — traversal in the remaining path after stripping is caught
- `test_symlink_with_absolute_target_rejected` — symlink to `/etc/passwd` rejected
- `test_symlink_with_traversal_target_rejected` — symlink with `../../` target rejected
- `test_safe_symlink_within_dir_allowed` — symlink to a relative path within the dir is allowed
- `test_extracts_normal_archive` — a real legitimate archive extracts correctly
- `test_rejects_path_traversal_member` — an archive with a traversal entry raises `ValueError`
- `test_rejects_absolute_path_member` — an archive with an absolute path raises `ValueError`
- `test_rejects_symlink_escape` — an archive with an escaping symlink raises `ValueError`
- `test_empty_archive_returns_empty_list` — only-dir archive returns `[]`
- `test_preserves_nested_directory_structure` — subdirectories are created correctly
- `test_strip_components_zero` — `strip_components=0` keeps full paths

Existing `fake_extract` test mocks in `test_transcriber_worker.py` and `test_download_model.py` updated to accept `**kwargs` (the `filter` keyword argument passed on Python 3.12+).

**Impact:** Model archive extraction is now safe against path traversal attacks. 211/211 tests pass.

---

## Bug #13 — Levenshtein missing from requirements + lexicon architecture fix

**Date:** 2026-07-11

**Problem:** The `Levenshtein` package was an optional import in `shared/lexicon.py` but was missing from `transcriber/requirements.txt`. Without it installed, `Lexicon.correct()` always returns `None` for non-exact matches, making the fuzzy post-correction dead code. Additionally, the correction logic lived in `bot/delivery.py` (`DeliveryLoop._correct_text()`), which was architecturally wrong — the bot should only manage lexicon entries (CRUD via `/lexicon` commands), while the transcriber should apply corrections.

**Fix:**

1. **Moved `_correct_text()` from `bot/delivery.py` to `transcriber/main.py`** — correction now runs in the transcriber after each audio file is transcribed, before segments are stored in the database. The corrected text is what gets merged and written to the transcript file.

2. **Removed lexicon loading from `DeliveryLoop.__init__`** — the delivery loop no longer creates a `Lexicon` instance or applies correction. It just reads the already-corrected transcript file and uploads it to Discord. Removed the `re` and `shared.lexicon` imports from `bot/delivery.py`.

3. **Added `Levenshtein>=0.21.0` to `transcriber/requirements.txt`** — the transcriber container now installs the dependency required for fuzzy correction. The bot's `requirements.txt` does not include Levenshtein (it doesn't need it).

4. **Updated `transcriber/main.py` `run_worker()`** — the lexicon loaded for hotwords is now reused as `correction_lexicon`. Each segment's text is passed through `_correct_text(segment.text, correction_lexicon)` before `db.add_transcript_segment()`. The full text fallback also gets corrected before `db.add_transcript()`.

**Architectural principle:** `correct()` is the transcriber's responsibility — it applies fuzzy matching to fix misrecognised words after transcription. The bot's lexicon interactions are limited to CRUD: `/lexicon add`, `/lexicon list`, `/lexicon remove`. This keeps the correction logic in the service that has the Levenshtein dependency and the transcribed text, rather than in the delivery loop where it was a secondary concern.

**Tests:**
- `TestTranscriberCorrectText` (5 tests) — moved from `TestDeliveryLoopLexicon`, now tests `transcriber.main._correct_text()` with the same scenarios (basic correction, no match, whitespace preservation, empty lexicon, None lexicon)
- `TestDeliveryLoopNoLexicon` (3 tests) — verifies `DeliveryLoop` no longer has `_lexicon` attribute, `_correct_text` method, or imports `Lexicon`

**Impact:** Fuzzy correction is no longer dead code. The transcriber applies corrections before storing transcripts. The bot's delivery loop is simplified — it just reads and uploads. 214/214 tests pass.

## Enhancement — pyspellchecker dictionary gate for lexicon correction

**Date:** 2026-07-11

**Problem:** `Lexicon.correct()` fuzzy-matched every input word against the D&D lexicon with no way to distinguish ordinary English words from misrecognised D&D terms. This caused false positives: "ore" → "Orc" (distance 1), "may" → "Mae" (distance 1), and similar corrections of common English words to D&D proper nouns. The lexicon should contain only non-English D&D terminology, but the corrector had no dictionary to check against.

**Fix:** Added a three-stage correction pipeline in `shared/lexicon.py`:

1. **Exact lexicon match** (stage 1, bypasses gate) — if the word matches a lexicon term case-insensitively, return the canonical form immediately. This catches correctly-transcribed terms that need canonicalisation (e.g. "theron" → "Theron"), including terms that happen to appear in English dictionaries. Exact matches are true positives, not false positives.
2. **English dictionary gate** (stage 2) — if the word is a recognised English word (checked via `pyspellchecker`'s bundled dictionary, O(1) set membership using `word in spell`), return None. This blocks fuzzy correction of common English words, preventing false positives like "ore" → "Orc" and "may" → "Mae". pyspellchecker is used for dictionary membership only, not for its own correction suggestions.
3. **Fuzzy lexicon match** (stage 3) — if the word is neither an exact lexicon match nor an English word, compute Levenshtein distance against every lexicon term. If the best match is within the configured threshold (proportional to word length, default factor 0.5, minimum 1), return the matched term. This catches misrecognised D&D terms like "theran" → "Theron" (distance 1).

**Dependencies:**
- Added `pyspellchecker>=0.8.0` to `transcriber/requirements.txt`
- pyspellchecker is an optional import — if not installed, the gate is skipped and correction falls back to exact + fuzzy matching only (same behaviour as before this change)
- A module-level lazy singleton (`_get_spell_checker()`) initialises SpellChecker on first use and caches the instance

**Stage ordering rationale:** Exact match comes before the dictionary gate because some lexicon terms appear in English dictionaries (e.g. "theron" is a Greek name). If the gate came first, these terms would be blocked from canonicalisation. The gate only blocks FUZZY matches — exact matches are always true positives.

**Tests:**
- `TestDictionaryGate` (8 tests in `test_lexicon.py`) — tests the gate at the `Lexicon.correct()` level: English word blocked from fuzzy match, non-English word still corrected, exact match bypasses gate, "may" → "Mae" blocked, numbers/punctuation skip gate, threshold still respected, fallback without pyspellchecker (both fuzzy and exact)
- `TestCorrectTextDictionaryGate` (4 tests in `test_lexicon_integration.py`) — tests the gate through the full `_correct_text()` pipeline: English words not corrected in context, non-English words still corrected, mixed English and lexicon words in one sentence, regression test for "may" → "Mae"

**Impact:** The false-positive problem is solved. Common English words like "ore", "may", "elf" are no longer corrected to D&D terms. Misrecognised D&D terms like "theran" → "Theron" are still corrected. Correctly-transcribed lexicon terms are still canonicalised. 227/227 tests pass (13 new).

## Bug #14 — _correct_text regex doesn't handle apostrophes/hyphens in D&D names

**Date:** 2026-07-11

**Problem:** `_correct_text()` in `transcriber/main.py` used `re.sub(r"\b\w+\b", ...)` to tokenise words for correction. The `\w+` pattern only matches `[a-zA-Z0-9_]`, so D&D names containing apostrophes (e.g. "Smith'var") or hyphens (e.g. "Grim-jaw") were split into fragments — "Grim-jaw" became "grim" and "jaw", neither of which matches the lexicon term "Grim-jaw". This meant hyphenated and apostrophe-containing names could never be corrected.

**Fix:** Changed the tokenisation regex from `r"\b\w+\b"` to `r"\w+(?:['-]\w+)*"`. The new pattern matches word characters followed by zero or more (apostrophe-or-hyphen + word characters) groups, so "Grim-jaw", "Smith'var", "O'Brien", and "Three-Fang-Killer" are all matched as single tokens. Leading/trailing apostrophes and hyphens are not captured (they start/end with `\w`, not `['-]`), so punctuation like "'tis" or "well—" is handled correctly.

**Files changed:**
- `transcriber/main.py` — regex updated, docstring expanded with tokenisation explanation

**Tests:**
- `TestCorrectTextTokenisation` (8 tests in `test_lexicon_integration.py`) — hyphenated name corrected, apostrophe name corrected, O'Brien style, fuzzy match on hyphenated name, multiple hyphens, regression test (old regex would have failed), leading apostrophe handling, normal words still work

**Impact:** D&D names with apostrophes and hyphens are now correctly tokenised and can be matched by both exact and fuzzy correction. 243/243 tests pass (16 new).

## Bug #15 — _correct_text doesn't preserve original word case

**Date:** 2026-07-11

**Problem:** `Lexicon.correct()` returned the lexicon's canonical form regardless of the original word's casing. If the transcript had "THERAN" (all caps, e.g. shouting), the correction returned "Theron" (the canonical form) — losing the all-caps casing that may be intentional. Similarly, title-case words like "Theran" would get the canonical form "Theron" (correct by coincidence, but not by design).

**Fix:** Added `_match_case(original, replacement)` helper in `shared/lexicon.py` that applies the original word's casing pattern to the corrected term:
- All uppercase (len > 1) → `replacement.upper()` (e.g. "THERAN" → "THERON")
- Title case (first upper, rest lower) → `replacement[0].upper() + replacement[1:].lower()` (e.g. "Theran" → "Theron")
- All lowercase or mixed casing → canonical form as-is (e.g. "theron" → "Theron")

The `_match_case()` call is applied at both correction stages (exact match and fuzzy match) in `Lexicon.correct()`. Single-character uppercase words are treated as title case, not all-caps (since a single letter can't distinguish "shouting" from "capitalised").

**Files changed:**
- `shared/lexicon.py` — added `_match_case()` helper, updated both `correct()` return paths to use it

**Tests:**
- `TestMatchCase` (8 tests in `test_lexicon.py`) — all-caps on exact match, title case on exact match, lowercase returns canonical on exact match, all-caps on fuzzy match, title case on fuzzy match, mixed casing returns canonical, single-char uppercase, case preservation with hyphenated term via `_correct_text`

**Impact:** Shouting ("THERAN") is now preserved as "THERON" after correction, and title-case words ("Theran") get proper title-case corrections ("Theron"). All-lowercase words still get the canonical form. 243/243 tests pass (8 new).

## Bug #16 — `bot._voice_clients` dict is never read (dead code) (minor)

**Commit:** (pending)

**Problem:** `start_command` stored the py-cord `VoiceClient` in `bot._voice_clients[interaction.guild_id]`, but this dict was never read anywhere in the codebase. `stop_command` uses `interaction.guild.voice_client` (py-cord's built-in per-guild voice client accessor) instead. The dict served no purpose — it was likely added with the intention of using it for `/stop` but was superseded by py-cord's own API.

**Investigation:** Confirmed that speaker names are NOT stored in `_voice_clients`. Speaker names are resolved from the Discord guild member cache during the recording callback and stored in the `audio_files` table (`speaker_name` column). The transcriber reads speaker names from the database, not from any bot-side dict. Removing `_voice_clients` has no impact on speaker labelling in transcripts.

**Fix:** Removed the three lines that initialised and populated `bot._voice_clients`. The adjacent `bot._recording_futures` dict (used by `stop_command` to await the recording callback future) is retained — it IS read.

**Files changed:**
- `bot/commands.py` — removed `bot._voice_clients` initialisation and assignment (3 lines)

**Impact:** No functional change. Dead code removed. 243/243 tests pass (no new tests — the attribute was never referenced in tests or any other code).

---

## Bug #17: Delivery loop accesses private _cursor() method

**Date:** 2026-07-11

**Problem:** The delivery loop in `bot/delivery.py` used `self.db._cursor()` (a private context manager) to run a raw SQL query polling for sessions ready for delivery. This coupled the delivery loop to the database's internal implementation and included an unnecessary `commit()` after a SELECT.

**Fix:** Added a public `Database.get_sessions_for_delivery()` method that encapsulates the query (status = COMPLETE, transcript_path IS NOT NULL, thread_id IS NULL, ordered by ended_at). The delivery loop's `_poll()` method now calls this public API instead of accessing the private cursor. Removed the unused `STATUS_COMPLETE` import from `delivery.py`.

**Files changed:**
- `shared/database.py` — added `get_sessions_for_delivery()` method
- `bot/delivery.py` — replaced `_cursor()` usage with `get_sessions_for_delivery()`, removed unused `STATUS_COMPLETE` import
- `tests/test_database.py` — 5 new tests for `get_sessions_for_delivery()`
- `docs/ARCHITECTURE.md` — updated delivery flow step 1
- `docs/IMPLEMENTATION_NOTES.md` — added "Delivery Loop Database Access" section

**Impact:** 248/248 tests pass (5 new). The delivery loop no longer reaches into the database's internals — all queries go through the public API.

---

## Bug #18: Blocking file I/O in async recording callback

**Date:** 2026-07-11

**Problem:** The `_process_recording` async coroutine in `bot/commands.py` wrote WAV files to disk using synchronous `open()` and `f.write()` calls directly on the event loop. For a 30-minute D&D session, the per-speaker WAV files can be tens of MB, and writing them synchronously blocked the event loop — stalling Discord gateway heartbeats and causing the bot to appear unresponsive during the critical recording-stop window.

**Fix:** Extracted the file-writing logic into a standalone sync helper `_write_audio_files_sync(file_specs)`, which takes a list of `(filepath, raw_bytes)` tuples, creates directories, writes files, and returns the success count. The async coroutine calls this helper via `asyncio.to_thread()`, offloading blocking I/O to a thread pool. Speaker name resolution (guild member cache, no I/O) and DB registration (`add_audio_file`) remain on the event loop. The existing `run_coroutine_threadsafe` pattern (from Bug #5) and future resolution (from Bug #6) are preserved unchanged.

**Files changed:**
- `bot/commands.py` — added `_write_audio_files_sync()` helper; refactored `_process_recording` to build file specs + speaker info on the event loop, write files via `asyncio.to_thread`, then register in DB on the event loop
- `tests/test_recording_callback.py` — 5 new tests: verifies `asyncio.to_thread` is used, helper is sync, helper writes all files, helper continues on error, empty list returns 0
- `docs/ARCHITECTURE.md` — updated step 7 of recording flow and container responsibilities
- `docs/IMPLEMENTATION_NOTES.md` — added "Non-blocking file writes" subsection

**Impact:** 253/253 tests pass (5 new). The event loop is no longer blocked during WAV file writes — Discord gateway events are handled promptly even during the recording-stop processing window.

---

## Bug #19: Numpy array converted to Python list before passing to sherpa-onnx

**Date:** 2026-07-11

**Problem:** `TranscriptionWorker.transcribe()` called `audio.tolist()` on the numpy array returned by `soundfile.read()` before passing it to `stream.accept_waveform()`. For a 30-minute D&D session at 16 kHz, this created ~28.8 million Python float objects (~690 MB of object overhead), wasting both memory and CPU on a conversion that sherpa-onnx doesn't need — its pybind11 bindings accept numpy arrays natively via the buffer protocol.

**Fix:** Removed the `.tolist()` call. The numpy array is now passed directly to `stream.accept_waveform(sample_rate, audio)`. The official sherpa-onnx Python examples use the same pattern (see `python-api-examples/offline-decode-files.py` in the k2-fsa/sherpa-onnx repo). After stereo-to-mono downmix via `audio.mean(axis=1)`, the result remains a numpy array and is passed through unchanged.

**Files changed:**
- `transcriber/worker.py` — removed `.tolist()` call, added explanatory comment
- `tests/test_transcriber_worker.py` — 3 new tests in `TestTranscribeNoNumpyToList`: verifies accept_waveform receives the exact numpy array object (not a list), verifies .tolist() was not called (result is not a Python list), verifies stereo downmix still produces a numpy array
- `docs/ARCHITECTURE.md` — updated transcription flow step 6 to note numpy array passthrough
- `docs/IMPLEMENTATION_NOTES.md` — added "Numpy Array Passthrough to sherpa-onnx" section

**Impact:** 256/256 tests pass (3 new). Eliminates ~690 MB of unnecessary memory allocation and seconds of CPU-bound list conversion for a typical 30-minute session.

---

## Bug #20: Inconsistent `setup_logging` between bot and transcriber

**Date:** 2026-07-11

**Problem:** Three divergent `setup_logging` implementations existed — one in `bot/main.py`, one in `transcriber/main.py`, and a third in `shared/logging_setup.py` that was never wired into either entry point. The divergences:

- **Stream:** Bot used `StreamHandler()` (defaults to stderr); transcriber used `StreamHandler(sys.stdout)`. This meant `docker compose logs` captured transcriber output but missed bot logs.
- **Format:** Bot had no `datefmt` (timestamps showed as raw `asctime`); transcriber had `datefmt="%Y-%m-%d %H:%M:%S"`. The format strings also differed (bot had brackets around `[%(name)s]`, transcriber did not).
- **Duplicate guard:** Transcriber had `if root_logger.handlers: return`; bot had none — re-imports could attach duplicate handlers.
- **Logger scope:** The shared module configured a named logger (not root), so library log records (py-cord, sherpa-onnx, etc.) were never captured.

**Fix:** Rewrote `shared/logging_setup.py` as the single source of truth. `setup_logging(name, level, log_dir, max_size_mb, backup_count)` configures the **root** logger with:

1. A `StreamHandler` writing to **stdout** (Docker convention — all container logs to stdout).
2. A `RotatingFileHandler` writing to `<log_dir>/<name>.log` (10MB rotation, 5 backups).
3. A single format: `%(asctime)s [%(name)s] %(levelname)s: %(message)s` with `datefmt="%Y-%m-%d %H:%M:%S"`.
4. A duplicate-handler guard: if the root logger already has handlers, the function returns immediately without modifying the configuration.

The `setup_logging_from_config(config, name)` wrapper reads `config.log_level`, `config.log_max_size_mb`, and `config.log_backup_count` and delegates to `setup_logging()`. Both `bot/main.py` and `transcriber/main.py` now import and call `setup_logging_from_config(config, "bot")` / `setup_logging_from_config(config, "transcriber")` respectively. The local `setup_logging` functions were removed from both entry points.

**Files changed:**
- `shared/logging_setup.py` — rewritten (119 lines): root logger, stdout handler, consistent format with `datefmt`, duplicate guard, `setup_logging_from_config()` wrapper
- `bot/main.py` — removed local `setup_logging`, added import of `setup_logging_from_config`, updated call site
- `transcriber/main.py` — removed local `setup_logging`, removed unused `logging.handlers` import, added import of `setup_logging_from_config`, updated call site
- `tests/test_logging_setup.py` — rewritten (13 tests): root logger return, file handler creation, level respect, log dir creation, handler types, stdout stream, duplicate guard, datefmt, format consistency across handlers, log file naming, invalid level fallback, `setup_logging_from_config` wrapper, bot-vs-transcriber format consistency
- `docs/ARCHITECTURE.md` — updated Logging section to describe shared module, handler structure, format, and duplicate guard
- `docs/IMPLEMENTATION_NOTES.md` — updated Logging Configuration section with shared module details, stdout unification, and corrected format examples

**Impact:** 265/265 tests pass (9 net new: 4 old logging tests replaced by 13 new). Both containers now produce identically-formatted log output to stdout and rotating files. Library log records (py-cord, sherpa-onnx) are captured via the root logger. No more duplicate-handler risk on re-import.

---

## Bug #23 — Redundant logging config sections

**Date:** 2026-07-11

**Problem:** The `_DEFAULTS` dict in `shared/config.py` had both a `bot` section with `log_level` / `log_max_size_mb` / `log_backup_count` keys AND a `logging` section with `level` / `max_size_mb` / `backup_count` — the same settings duplicated under two different key paths. The `Config` properties used a fallback chain: `self.get("logging.level", self.get("bot.log_level", "INFO"))`, meaning users could configure logging via either path, with `bot.*` silently shadowed by `logging.*`. This was confusing: logging is a shared concern (both containers read the same config), so having a bot-specific copy was unnecessary and error-prone.

**Fix:** Removed `log_level`, `log_max_size_mb`, and `log_backup_count` from the `bot` section of `_DEFAULTS`. The `Config` properties (`log_level`, `log_max_size_mb`, `log_backup_count`) now read exclusively from the `logging` section with sensible defaults — no fallback to `bot.*` keys. The `bot` section in `_DEFAULTS` now contains only `token_file`.

**Files changed:**
- `shared/config.py` — removed `log_level` / `log_max_size_mb` / `log_backup_count` from `_DEFAULTS["bot"]`; simplified the three Config properties to read only `logging.*`
- `tests/test_config.py` — added `TestLoggingConfig` class (9 tests): defaults, reading from `logging.*` section, `bot.log_*` keys are ignored, `logging.*` overrides `bot.*`, `_DEFAULTS["bot"]` only has `token_file`
- `docs/ARCHITECTURE.md` — updated Configuration section to note logging is configured via shared `logging` section only
- `docs/IMPLEMENTATION_NOTES.md` — added paragraph to Logging Configuration section explaining the removal of `bot.*` logging keys

**Impact:** 274/274 tests pass (9 new). The `config.yaml.example` already only used `logging.*` keys, so no example config changes were needed. Existing deployments with `bot.log_*` keys in their `config.yaml` will silently fall through to defaults (same behaviour as if the key was absent) — no error, just ignored.

---

## Bug #22 — `/invite` command missing permission check

**Date:** 2026-07-11

**Problem:** The `/invite` slash command in `bot/commands.py` generated a bot invite URL without checking whether the calling user had permission. All other operational commands (`/start`, `/stop`, `/status`, `/session`, `/lexicon *`) called `_check_permission()` at the top of their handlers, but `/invite` was missed. This allowed any server member to generate an OAuth2 invite URL for the bot, even when `restrict_commands` was enabled with a role allowlist.

The `/help` command was also unrestricted, but this is intentional — `/help` only displays command usage text (no side effects, no security surface). `/invite` is different: it produces a bot invite URL that could be used to add the bot to other servers.

**Root cause:** When Bug #8 added permission checks, `/invite` was explicitly left open alongside `/help` under the assumption that both were "information commands." `/invite` is not purely informational — it generates an actionable OAuth2 URL.

**Fix:** Added `_check_permission(interaction, bot.config)` call at the top of `invite_command()`, following the same pattern as all other command handlers. If denied, sends an ephemeral error message and returns early. `/help` remains intentionally unrestricted.

**Files changed:**
- `bot/commands.py` — added permission check to `invite_command()` (5 lines, lines 287–292)
- `tests/test_commands.py` — added `TestInviteCommandPermission` class (4 tests): denied without matching role, allowed with matching role, allowed when restriction disabled, allowed when no roles configured
- `docs/DEVELOPMENT_LOG.md` — updated Bug #8 entry to note `/invite` was later addressed; added this entry

**Impact:** 277/277 tests pass (4 new). `/invite` now respects the same permission model as all other commands. `/help` remains open to all users.

---

## Bug #24 — `start_command` doesn't defer interaction before voice connect (3s timeout risk)

**Date:** 2026-07-11

**Problem:** `start_command` in `bot/commands.py` called `voice_channel.connect()` without first deferring the Discord interaction. Discord enforces a 3-second response deadline for slash command interactions. Voice join is a potentially slow async operation — it involves a Discord gateway state change, WebSocket handshake, and encryption key exchange. Under high latency or server load, this can exceed 3 seconds, causing Discord to show "The application did not respond" to the user.

**Fix:** Added `await interaction.response.defer(ephemeral=True)` after the DB session record is created but before the voice channel join attempt. The defer is placed after session creation so that if the voice join fails, `fail_session()` has a valid session ID to operate on. After deferring, all subsequent responses in the command use `interaction.followup.send()` instead of `interaction.response.send_message()` (which would raise an error after a defer). Early-return paths (permission denied, active session already running, user not in a voice channel) do NOT defer — they respond immediately with `response.send_message()` since they complete well within the 3-second deadline.

**Files changed:**
- `bot/commands.py` — added `await interaction.response.defer(ephemeral=True)` before voice join in `start_command`; switched post-defer responses (success and error) to `interaction.followup.send()`
- `tests/test_start_command_defer.py` — new test file (8 tests): defer called before connect, success uses followup, voice join failure uses followup, permission denied does not defer, active session does not defer, no voice channel does not defer, defer uses ephemeral=True, session created before voice join failure
- `docs/ARCHITECTURE.md` — updated Recording flow step 2 to document the defer and step 3 to mention followup
- `docs/IMPLEMENTATION_NOTES.md` — updated Voice channel join failure section to explain the defer and followup.send pattern

**Impact:** 286/286 tests pass (8 new). `/start` now reliably responds within Discord's interaction deadline regardless of voice gateway latency.

---

## Test Suite Pruning — Removing Absence Tests, Adding Missing Coverage

**Date:** 2026-07-11

**Problem:** During the bug-fixing session, several tests were added that verified removed functionality was no longer present (absence tests). While useful as regression guards during active development, they test what ISN'T there rather than what IS — adding maintenance burden without ongoing value. The user requested pruning these in favour of tests that exercise the project's current and expected functionality.

**Absence tests removed (5 tests across 3 files):**

1. `tests/test_lexicon_integration.py` — `TestDeliveryLoopNoLexicon` class (3 tests): `test_delivery_loop_has_no_lexicon_attribute`, `test_delivery_loop_has_no_correct_text_method`, `test_delivery_loop_does_not_import_lexicon`. These asserted DeliveryLoop didn't have lexicon-related attributes (removed in Bug #13). The correct behaviour is now simply the default — DeliveryLoop has never had lexicon in its final design.

2. `tests/test_database.py` — `test_update_status_failed_does_not_clear_active` (1 test): Demonstrated that the old `update_session_status(STATUS_FAILED)` code path left `ended_at = NULL`. This code path is no longer used — `fail_session()` replaced it in Bug #8. The correct behaviour is already covered by `test_fail_session_sets_status_and_ended_at` and `test_fail_session_clears_active`.

3. `tests/test_config.py` — `test_bot_section_only_has_token_file` (1 test): Asserted `log_*` keys were absent from `_DEFAULTS["bot"]` (removed in Bug #23). Tests the absence of removed config keys rather than testing real configuration behaviour.

**Missing positive tests added (23 tests across 2 new files):**

1. `tests/test_delivery.py` (14 tests) — New file covering `bot/delivery.py` DeliveryLoop, which previously had zero test coverage:
   - `TestDeliveryLoopInit` (2): Constructor sets attributes, poll_interval default.
   - `TestDeliveryLoopStart` (2): `start()` creates asyncio task; idempotent when already running.
   - `TestDeliveryLoopPoll` (4): Polls DB for complete sessions; skips sessions with no transcript path; skips already-delivered sessions (thread_id set); handles DB returning empty list.
   - `TestDeliveryLoopDeliver` (3): Creates Discord thread with correct name; splits long transcripts at 1900-char boundary; sends followup message with thread link.
   - `TestDeliveryLoopRun` (3): Run loop polls then sleeps; run loop stops on shutdown event; run loop handles exceptions without crashing.

2. `tests/test_voice.py` (9 tests) — New file covering `bot/voice.py` idle timeout, which previously had zero test coverage:
   - `TestOnVoiceStateUpdate` (4): Ignores bot members; ignores when no voice client; starts idle timer when alone in channel; cancels idle timer when a user joins.
   - `TestIdleTimeout` (5): Timeout ends session and disconnects; no active session just disconnects; cancellation is silent (no session end, no crash); cleans up task from `_idle_tasks` dict; not-recording state doesn't stop the bot.

**Impact:** 304/304 tests pass (0 warnings). Net change: -5 absence tests, +23 positive tests = +18 tests. The test suite now covers two previously untested modules (delivery.py and voice.py) and no longer carries tests for removed functionality.

---

## Deployment Fixes: docker-compose.yml (#1–#3)

**Date:** 2026-07-11

A comprehensive repository review identified 10 deployment-layer shortcomings. This entry covers fixes #1, #2, and #3 — all related to `docker-compose.yml`.

### Fix #1: config.yaml not mounted into containers (CRITICAL)

**Problem:** `docker-compose.yml` had no volume mount for `config.yaml` in either service. Neither Dockerfile copies it (the bot Dockerfile had `COPY config.yaml* /app/` but the glob matches `config.yaml.example` when the real config is absent). Both containers fell back to defaults only — no guild ID, no transcript channel, no custom model settings.

**Fix:**
- Added `./config.yaml:/data/config.yaml:ro` volume mount to both `bot` and `transcriber` services
- Added `CONFIG_PATH=/data/config.yaml` environment variable to both services (the bot already read this env var; the transcriber was updated to read it too)
- Updated `transcriber/main.py:run_worker()` to read `CONFIG_PATH` from env, falling back to `/data/config.yaml` — consistent with the bot's config path resolution
- Removed the misleading `COPY config.yaml* /app/` from `bot/Dockerfile` (the glob copies the example file when the real config doesn't exist; config is now always mounted at runtime)

### Fix #2: bot_token secret not attached to bot service (CRITICAL)

**Problem:** `docker-compose.yml` defined a top-level `secrets:` block with `bot_token` from `./secrets/bot_token`, but the `bot:` service had no `secrets:` key. Docker Compose requires each service to explicitly declare which secrets it uses. Without `secrets: [bot_token]` under `bot:`, the file was never mounted at `/run/secrets/bot_token`, so the bot exited with `FileNotFoundError`.

**Fix:**
- Added `secrets: [bot_token]` under the `bot:` service

### Fix #3: Bot health check not meaningful (CRITICAL)

**Problem:** The bot's health check ran `python -c "import discord; print('ok')"`. This only verified the library was importable — it said nothing about the Discord connection, event loop health, or gateway responsiveness. A crashed bot with a dead event loop would still pass the check.

**Fix:**
- Created `bot/healthcheck.py` — a standalone health check script that reads the heartbeat file (`/data/bot.heartbeat`) and checks its modification time. If the file is missing or older than 90 seconds, the check fails (exit 1). If recent, it passes (exit 0).
- Added heartbeat mechanism to `bot/main.py` (`ScribesBot`):
  - `_write_heartbeat()` — writes current timestamp to `/data/bot.heartbeat`
  - `_heartbeat_loop()` — background task that updates the heartbeat every 30 seconds while the bot is running
  - `on_ready()` — writes an initial heartbeat immediately (only fires after the gateway handshake completes), then starts the heartbeat loop task
- Updated `docker-compose.yml` health check from the trivial import test to `python /app/bot/healthcheck.py` with 30s interval, 5s timeout, 3 retries, and 30s start period

The heartbeat approach catches:
- Dead event loop (stops updating the file)
- Crashed bot process (no process to write the file)
- Lost gateway connection (on_ready never fires, no initial heartbeat)

**Tests added (11 tests):**
- `tests/test_healthcheck.py` (8 tests): healthcheck script logic (fresh/missing/stale/boundary), heartbeat writing, heartbeat loop, on_ready integration, task not restarted if already running
- `tests/test_transcriber_worker.py::TestRunWorkerConfigPath` (3 tests): explicit config path, env var config path, default fallback

**Impact:** 315/315 tests pass (0 warnings).

## 2025-07-12 — Deployment Audit Fixes (#4, #6, #8, #10)

Four remaining deployment-layer issues from the repository audit were fixed in a single batch.

### Fix #4: Transcriber Dockerfile mkdir dead paths

**Problem:** The transcriber Dockerfile created directories under `/app/data/` (`/app/data/recordings`, `/app/data/transcripts`, `/app/data/logs`). The Docker volume is mounted at `/data`, so these were dead paths — the mkdir commands created empty directories that were never used by the running container.

**Fix:**
- Changed the `RUN mkdir -p` commands in `transcriber/Dockerfile` from `/app/data/*` to `/data/*` — matching the actual volume mount point.

### Fix #6: Transcriber health check not meaningful

**Problem:** The transcriber's Docker health check was `python -c "import sys; sys.exit(0)"`, which only verified the Python interpreter was alive. A crashed or stuck transcriber would still pass the check.

**Fix:**
- Created `transcriber/healthcheck.py` — a standalone health check script that reads the heartbeat file (`/data/transcriber.heartbeat`) and checks its modification time. If the file is missing or older than 300 seconds (5 minutes), the check fails (exit 1). The 5-minute threshold gives headroom for a single long transcription (the transcriber doesn't update the heartbeat during transcription, only between poll cycles).
- Added heartbeat writing to `transcriber/main.py`:
  - Initial heartbeat written after model load, before the polling loop starts (confirms the model loaded successfully).
  - Heartbeat updated after each poll cycle (at the end of the try/except block, before the loop repeats).
- Updated `transcriber/Dockerfile` HEALTHCHECK to `CMD python /app/transcriber/healthcheck.py` with `start-period=120s` (allows model download/load time on first start).
- Added explicit healthcheck to `docker-compose.yml` transcriber service (`test: ["CMD", "python", "/app/transcriber/healthcheck.py"]`, `start_period: 120s`).

### Fix #8: Bot has no SIGTERM handler for graceful shutdown

**Problem:** The bot had no SIGTERM handler. Docker sends SIGTERM on `docker stop`; without a handler, the bot would be killed by SIGKILL after the 10-second grace period, potentially mid-delivery or mid-gateway-event.

**Fix:**
- Added `import signal` to `bot/main.py`.
- Added `stop()` method to `DeliveryLoop` in `bot/delivery.py` — cancels the delivery polling asyncio task and sets the running flag to False. Safe no-op if the loop was never started or the task is already done.
- Added `setup_hook()` to `ScribesBot` — registers a SIGTERM handler within the running event loop using `loop.add_signal_handler()`. The handler calls `DeliveryLoop.stop()` and `bot.close()`.
- Added `on_disconnect` callback for additional cleanup logging.

The handler is registered in `setup_hook()` (not at module level) because it needs access to the running event loop and the bot instance. `setup_hook()` runs after the event loop starts but before `on_ready`.

### Fix #10: No startup config validation

**Problem:** The bot had no validation of its configuration before connecting to Discord. A misconfigured `config.yaml` (e.g. missing guild_id) would cause cryptic py-cord errors at runtime rather than a clear startup error.

**Fix:**
- Added `validate()` method to `Config` class in `shared/config.py` — checks:
  1. Config file existence (verifies the file path exists and is readable)
  2. `discord.guild_id` is non-zero (zero/missing would cause global slash command sync instead of guild-specific)
  3. `discord.transcript_channel_id` is non-zero (zero/missing means nowhere to deliver transcripts)
  Returns a list of error strings (empty = valid).
- Called `validate()` from `bot/main.py` `main()` — after logging setup (so errors are logged) but before creating the bot instance. If errors are found, each is logged as ERROR and the process exits with `sys.exit(1)`.

**Tests added (13 tests):**
- `tests/test_config.py` (3 tests): Config.validate() — valid config, missing guild_id, missing transcript_channel_id
- `tests/test_delivery.py` (4 tests): DeliveryLoop.stop() — cancels running task, no-op when not running, no-op when task already done, logs message
- `tests/test_healthcheck.py` (4 tests): Transcriber healthcheck — fresh heartbeat, missing heartbeat, stale heartbeat, boundary case

**Impact:** 328/328 tests pass (0 warnings).

## 2025-07-14 — Fix: py-cord 2.8.0 Sink `__sink_listeners__` AttributeError

### Problem

When issuing `/start` on the deployed bot, `vc.start_recording()` raised `AttributeError: 'WaveSink' object has no attribute '__sink_listeners__'` before any audio was captured. The `/start` command failed immediately.

### Root Cause

py-cord 2.8.0 refactored voice reception to add a `SinkEventRouter` (in `discord/voice/receive/router.py`). The router's `__init__` → `register_events()` calls `sink.walk_children()` and accesses `sink.__sink_listeners__`. However, the `Sink` base class in `discord/sinks/core.py` was never updated to define either attribute. This is a known py-cord bug (issue #3139), unfixed on master as of 2.8.0.

### Fix

Monkey-patched the `Sink` class in `bot/main.py` at module import time, before `bot.run()`:
- `Sink.__sink_listeners__ = []` — empty list because we register no sink event listeners
- `Sink.walk_children()` — returns an empty iterator because WaveSink (the only sink we use) has no child sinks

Both patches are guarded with `if not hasattr(...)` so they are no-ops if py-cord fixes this upstream or if downgrading to 2.6.3.

### Documentation Updated

- `docs/IMPLEMENTATION_NOTES.md` — new section "py-cord 2.8.0 Sink Monkey-Patch"
- `docs/KNOWN_ISSUES.md` — added to Technical Debt with upstream tracking reference

**Impact:** 328/328 tests pass (0 warnings). No new tests added — the fix is a third-party library workaround, not application logic.
