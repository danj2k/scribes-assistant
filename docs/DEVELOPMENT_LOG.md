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
