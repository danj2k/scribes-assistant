# Scribe's Assistant — Known Issues

## Limitations

- **No real-time transcription**: Transcripts are produced after the session ends, not during play. This is by design (local Whisper inference is too slow for real-time on this hardware).
- **Speaker diarisation is approximate**: Based on voice fingerprinting, not a dedicated diarisation model. Overlapping speakers are not separated.
- **WAV overhead**: py-cord decodes Opus to WAV before delivering audio, adding unnecessary encoding overhead. Cannot be avoided without switching to an alpha-quality library.

## Technical Debt

- **py-cord PR #3159 dependency (unmerged)**: We install py-cord from the PR #3159 branch (`git+https://github.com/Pycord-Development/pycord@refs/pull/3159/head`) because py-cord 2.8.0 ships DAVE handshake code but does NOT implement DAVE decryption for incoming audio (pycord issue #3139). PR #3159 properly implements DAVE E2E decryption for voice reception but had not merged as of July 2026 (needs two reviews). Three PR #3159 quirks are worked around in `bot/commands.py`: (1) `AudioReader._stop()` guards the callback with `if self.after and self.args:` — `self.args` is an empty tuple (falsy) when no extra args are passed to `start_recording()`, so the callback never fires; the fix is to pass a dummy `None` arg; (2) the callback signature changed from `after(exc)` to `after(sink, *args)` — the sink is the first positional arg, not an exception; (3) `_stop()` calls `sink.cleanup()` after the callback, so the callback must not call `audio_data.cleanup()` or `format_audio()` itself (raises `SinkException`). Two residual monkey-patches remain in `bot/main.py` for issues PR #3159 does not fix: `RTPPacket.type` and `VoiceClient.start_recording` (setting `sink.vc`). Both are `hasattr`-guarded no-ops if fixed upstream. When PR #3159 merges and a pycord release ships, replace the git URL in `requirements.txt` with a pinned version and re-check whether the two remaining patches can be removed.
- **sherpa-onnx hotwords segfault on Whisper models**: `sherpa_onnx.OfflineRecognizer.create_stream(hotwords="")` crashes the process with a segfault when using a Whisper (non-transducer) model. The C++ code prints "Only transducer models support contextual biasing" and then dies. This happens when the lexicon is empty — `build_hotwords([])` returns `""`, which is passed to `create_stream()`. sherpa-onnx's Python wrapper only skips the biasing code path when `hotwords is None`; any non-None string (including `""`) enters the C++ biasing path. Fixed in `transcriber/worker.py` by passing `hotwords or None` to `create_stream()`, converting empty strings to `None`. The transcriber's `restart: unless-stopped` Docker policy was silently restarting the container on every crash, masking the segfault as a "worker restarted" log line.
- **get_sessions_for_delivery column mismatch (fixed)**: `get_sessions_for_delivery()` in `shared/database.py` originally selected only the `id` column, but `bot/delivery.py`'s `_deliver()` method expected `transcript_path` and `discord_channel_id` keys in the returned dicts. This caused a `KeyError` when the delivery loop tried to deliver a completed transcript. Fixed by selecting all three columns. The existing tests missed this because the delivery tests mocked the DB (constructing session dicts manually with all keys), and the database tests only asserted on `s["id"]`. A regression test now verifies both keys are present.
- **~~Whisper 30-second audio limit~~ [RESOLVED]**: sherpa-onnx Whisper only processes the first 30 seconds of audio and silently discards the rest. The `transcribe()` method now uses silence-aware splitting (`transcriber/splitter.py`) to chunk audio at natural pause points (silence midpoints), with a 25-second cap. This keeps chunks under the 30-second limit while avoiding mid-sentence splits.
- **Whisper token timestamps require attention outputs**: `enable_token_timestamps=True` needs a model exported with `export-onnx-with-attention.py` (cross-attention weights). The standard sherpa-onnx Whisper export does not include these. We use `enable_segment_timestamps=True` instead, which parses Whisper's native timestamp tokens and works with any model — but produces segment-level (not token-level) timestamps.
- **Recording filenames use Discord display names (fixed)**: py-cord keys `sink.audio_data` by `User`/`Member` objects, not numeric IDs. The recording callback iterates `sink.audio_data.items()` and uses `str(user_obj)` (the display name) for the filename. Discord display names can contain slashes and other path-unsafe characters — e.g. `llenikcud (Duckinell/DM)` caused the `/` to be interpreted as a path separator, creating a spurious directory `llenikcud (Duckinell/` with `DM).wav` inside it. Fixed in `bot/commands.py` with `_sanitize_filename()` which replaces `<>:"/\|?*\0` with underscores, strips leading/trailing spaces and dots, and collapses repeated underscores. The unsanitised display name is still stored in the DB as `speaker_name` for transcript output.
- **libopus Docker dependency**: The bot Dockerfile installs `libopus0` via apt-get because `python:3.11-slim` omits it. py-cord loads this shared library via ctypes at runtime for Opus decoding. If the package is removed or the base image changes, recording will fail with `OpusNotLoaded`.

- **WAV header missing on disk (fixed)**: py-cord's `WaveSink.format_audio()` produces WAV-formatted BytesIO in `audio_data.file` but the on-disk temp file (`sink._temp_paths[user]`) contains only raw PCM. The `_process_recording` after-callback was renaming the raw-PCM temp file to `.wav`, producing files that soundfile/libsoundfile rejected with "Format not recognised". Fixed in `bot/commands.py` by reading the WAV-formatted data from `audio_data.file` and writing it to the final path via `_write_wav_file()` (with `asyncio.to_thread()` for large files), then deleting the raw PCM temp file.

- **WAV channel mismatch (fixed)**: `_write_wav_file()` in `bot/commands.py` was writing WAV headers with `channels=2` (stereo) even though `TimestampedWaveSink.format_audio()` and `_pcm_to_mono()` produce mono audio (1 channel). This caused playback issues: mono audio data interpreted as stereo would play at 2× speed and one octave higher (the player treats mono samples as interleaved L/R pairs, halving the effective sample rate from 48kHz to 24kHz). Fixed by changing `_write_wav_file()` to use `channels=1` to match the actual mono audio data.

- **Unflushed writes → silently truncated audio (fixed)**: `TimestampedWaveSink.write()` writes PCM to buffered `NamedTemporaryFile` objects. The writes sit in userspace buffers until flushed. When recording stops, `_process_recording()` reads those same temp files via NEW file handles (`open(temp_path, "rb")`), which cannot see unflushed bytes. Any data still in the writer's buffer at the moment `/stop` fires is silently dropped — the tail end of every recording is truncated. Fixed by flushing all user temp files at the start of `_process_recording()` before reading them. The flush is cheap because frames are already small and periodic.

- **Per-user audio corruption (DAVE/Opus decoder failure)**: Some Discord users' audio arrives as random noise (high-variance PCM) while others are healthy. This is caused by DAVE MLS handshake failures or Opus decoder errors for specific users, producing corrupted PCM before `Sink.write()` is called. Two-layer diagnostic instrumentation added: (1) `PacketDecoder` wrapper in `bot/main.py` logs decode attempts (DEBUG), failures (ERROR with DAVE status), and successes (DEBUG with PCM byte count); (2) `_log_receive_diagnostics()` in `bot/timestamped_sink.py` tracks per-(user, SSRC) packet counts, all-zero PCM, and high-variance noise (variance proxy >100000 for 1000 samples). Both are guarded with try/except and log metadata only (SSRC, sequence, user_id, byte lengths) — never encrypted packets or raw audio. **AEAD error rate-limiting (2026-10-01):** After 10 consecutive failures per (user, SSRC), logs once per 1000 errors and emits a summary every 10,000 failures. After 100 failures, logs a critical warning about suspected stale keys and suggests the user rejoin. This prevents log flooding when the voice connection drops and reconnects with stale keys (e.g. 80,239 AEAD failures in a single session). **Root cause of stale keys after reconnect:** py-cord PR #3159 does not refresh DAVE/MLS keys when the voice WebSocket reconnects. The `VoiceClient` reuses the old encryption keys, causing all subsequent packets to fail decryption. Workaround: users must manually disconnect and reconnect to the voice channel to force a fresh handshake. A permanent fix requires patching py-cord PR #3159 to detect reconnection events and trigger a new DAVE handshake.

- **Timeline drift in TimestampedWaveSink (fixed 2026-10-01)**: The original implementation used RTP timestamp deltas keyed by user (not SSRC) to pad silence gaps. This broke when users reconnected with new SSRCs (RTP timestamp base jumped) or when silence exceeded 60 seconds (the `_MAX_PLAUSIBLE_GAP` threshold caused padding to be skipped). In a D&D session, a quiet player listening for several minutes would have their entire timeline shift earlier, causing their transcript lines to appear before they actually spoke. Replaced with wall-clock arrival time: tracks `_samples_written` per user and compares to expected samples based on elapsed time since `_recording_start`. Pads silence for any deficit between 20ms and 5 seconds. This correctly handles reconnects (new SSRC), DTX gaps, and late joiners without relying on unreliable RTP timestamps. The old RTP-delta approach is documented in git history (commit before 2026-10-01) for reference.

- **~~Whisper timestamp misordering due to blind chunking~~ [RESOLVED]**: `transcribe()` cut each speaker's file into fixed 28-second chunks, and sherpa-onnx forced the first segment's start in each chunk to fall within 0–1 s of the chunk start. If someone started speaking 20 s into a chunk, the line was still stamped at the chunk boundary. Ties were broken by `ORDER BY start_time, seq`, where `seq` was each file's own segment index (says nothing about who spoke first), so tie order was effectively arbitrary. **Fix:** Replaced blind chunking with silence-aware splitting (`transcriber/silence.py` + `transcriber/splitter.py`). Audio is now split at natural pause points (silence midpoints), with a 25-second cap. Blank/silence segments are filtered before storage and again during transcript merging. Side benefits: sentences no longer split at chunk boundaries, `[BLANK_AUDIO]` and hallucinations on padded silence disappear, and transcription is much faster (silence is no longer transcribed).
- **~~Whisper hallucination patterns in transcripts~~ [RESOLVED]**: After the silence-aware chunking refactor, transcripts contained hallucinated non-speech descriptions like `(growling)`, `[Loud growling]`, `*Gibberish*`, `[Distorted sound]`, `(phone buzzing)`, `(sniffing)`, `*Gasps*`, `[Dog growling]`, `(speaking in foreign language)`, `[ Silence ]`, `[no audio]`, and incomplete fragments like `(` or `[`. Root cause: the RMS silence filter was at -60 dB while silence detection was at -40 dB — a 20 dB gap that let low-energy noise chunks through to Whisper, which interpreted them as speech-like patterns and produced descriptive hallucinations. The old blind 28-second chunking didn't have this because it produced fewer chunks overall. **Fix (2026-10-22):** Two-layer defence: (1) raised RMS threshold from 0.001 (-60 dB) to 0.005 (-46 dB), aligning closer to the silence detection threshold; (2) added `_is_whisper_hallucination()` regex filter in `transcriber/worker.py` that catches bracketed tags `[...]`, parenthesised sounds `(...)`, asterisk-wrapped tags `*...*`, and "speaking in X language" variants. Applied to both chunk-level text and individual segment text before storage. The hallucination filter is the primary defence — the RMS bump alone is insufficient because some hallucinated segments have energy *above* real speech (e.g. mic noise at RMS 0.078 vs speech at 0.050).

- **Whole-file audio read (fixed)**: `transcribe()` previously loaded the entire WAV file into memory via `sf.read(audio_path, dtype="float32")`. A 3-hour stereo 48 kHz recording allocates ~4 GB of RAM. Replaced with block-based reading using `sf.SoundFile` context manager — `seek()` + `read(frames=chunk_samples)` pulls one 28-second chunk at a time, downmixed to mono inline. Peak memory drops from ~4 GB to ~5 MB.

## Future Considerations

- Campaign wiki integration (mentioned in design doc as potential extension)
- Better speaker diarisation if hardware improves
- Support for concurrent sessions if needed in future

## Network Resilience Issues (2026-11-20)

Analysis of network instability and DNS resolution failure handling reveals several areas where the application may crash or fail ungracefully:

### Issue 1: Bot startup crashes on DNS failure [FIXED 2026-11-20]

**Location:** `bot/main.py:380` — `bot.run(token)`

**Problem:** The bot's main entry point called `bot.run(token)` with no retry logic. If DNS resolution failed at startup (e.g., `discord.com` unreachable, DNS server down, network interface not yet ready), the bot would crash immediately with an unhandled exception. Docker's `restart: unless-stopped` policy would restart the container, but without exponential backoff, rapid restart loops could occur during extended network outages.

**Fix:** Wrapped `bot.run(token)` in a retry loop with exponential backoff. The loop attempts up to 10 times with delays of 5s, 10s, 20s, 40s, 80s, 160s, 300s, 300s, 300s, 300s (capped at 5 minutes). Network-related exceptions (`ConnectionError`, `OSError`) trigger retries; non-network errors (invalid token, etc.) exit immediately. Each retry is logged with the attempt number and delay. After all retries are exhausted, the process exits with code 1 so Docker's restart policy applies.

**Status:** Implemented in `bot/main.py:380-417`. Tests verify retry behavior and error handling.

---

### Issue 2: Voice connection has no retry with backoff

**Location:** `bot/voice.py:60-82` — `connect_to_voice()`

**Problem:** The voice connection logic catches generic `Exception` and logs it, but does not retry with backoff. If the Discord voice server is unreachable or DNS resolution fails during `/start`, the command returns an error message to the user, but there's no automatic retry. Users must manually retry `/start` after waiting.

**Impact:** During transient network issues, users must manually retry the `/start` command. No automatic recovery.

**Current behavior:** `voice_client.connect(reconnect=True, timeout=10.0)` will attempt to reconnect if the connection drops mid-session, but the initial connection has no retry logic.

**Fix plan:** Add retry logic to `connect_to_voice()` with exponential backoff (3 attempts, 2s/4s/8s delays). Log each attempt. If all retries fail, return a clear error message suggesting the user retry manually after a delay.

---

### Issue 3: Delivery loop has no exponential backoff

**Location:** `bot/delivery.py:42-58` — `_deliver()` and `delivery_loop()`

**Problem:** The delivery loop catches `Exception` and logs it, then waits 60 seconds before retrying. During extended network outages, this produces a log entry every 60 seconds (log flooding) and wastes API calls that will fail. No circuit breaker pattern to detect sustained outages.

**Impact:** Log flooding during network outages. Wasted API calls. No graceful degradation.

**Current behavior:** If Discord API is unreachable, `_deliver()` logs the error and the loop continues. The next iteration tries again after 60 seconds.

**Fix plan:** Implement exponential backoff for delivery failures: 60s, 120s, 240s, 480s, max 900s (15 min). Reset backoff on successful delivery. Add a circuit breaker: after 5 consecutive failures, log a warning and extend the backoff ceiling. This reduces log noise during extended outages.

---

### Issue 4: Model download has no retry logic [FIXED 2026-11-20]

**Location:** `scripts/download_model.py:123` — `urllib.request.urlretrieve()`

**Problem:** The model download from GitHub releases used `urllib.request.urlretrieve()` with no explicit retry logic. If DNS failed or the network dropped during download, the download would fail and the transcriber would crash. Docker's restart policy would restart the container, which would retry the download, but without backoff.

**Fix:** Wrapped `urllib.request.urlretrieve()` in a retry loop with exponential backoff. The loop attempts up to 5 times with delays of 10s, 20s, 40s, 80s, 120s (capped at 2 minutes). Network-related exceptions (`urllib.error.URLError`, `ConnectionError`, `TimeoutError`) trigger retries; non-network errors (`PermissionError`, `FileNotFoundError`, etc.) exit immediately. Each retry prints a message with the attempt number and delay. After all retries are exhausted, a `RuntimeError` is raised with details of the final failure.

**Status:** Implemented in `scripts/download_model.py:120-170`. Tests verify retry behavior, successful recovery after transient failures, and immediate exit on non-network errors.

---

### Issue 5: No circuit breaker for repeated failures

**Location:** Multiple — `bot/delivery.py`, `bot/voice.py`, `transcriber/worker.py`

**Problem:** There is no circuit breaker pattern to detect sustained network outages. Repeated failures are logged at the same frequency, producing log flooding during extended outages. No mechanism to distinguish transient failures from sustained outages.

**Impact:** Log flooding during extended network outages. Difficult to distinguish transient issues from sustained problems.

**Fix plan:** Implement a simple circuit breaker: track consecutive failures per component. After N consecutive failures (e.g., 5), log a critical warning and extend the retry interval. After M consecutive failures (e.g., 20), log a critical alert suggesting manual intervention. Reset the counter on success.

---

### Issue 6: Healthcheck does not verify network connectivity

**Location:** `bot/healthcheck.py`, `transcriber/healthcheck.py`

**Problem:** The healthcheck endpoints only verify that the HTTP server is running and the database is accessible. They do not verify network connectivity to Discord or HuggingFace. A container can be "healthy" according to Docker but unable to reach external services.

**Impact:** Docker reports the container as healthy, but the bot cannot connect to Discord or download models. Users see no feedback until they try to use the bot.

**Fix plan:** Add optional network connectivity checks to the healthcheck: attempt a DNS resolution of `discord.com` and `huggingface.co`. If DNS fails, return HTTP 503 (unhealthy). This allows Docker to detect network issues and restart the container. Make this configurable (disabled by default) to avoid false positives during brief network blips.

---

### Summary

The application has several single points of failure related to network connectivity:

1. **Bot startup** — [FIXED] now retries with exponential backoff (10 attempts, up to 5 min)
2. **Voice connection** — no retry with backoff (still open)
3. **Delivery loop** — no exponential backoff, log flooding (still open)
4. **Model download** — [FIXED] now retries with exponential backoff (5 attempts, up to 2 min)
5. **No circuit breaker** — repeated failures not distinguished from sustained outages (still open)
6. **Healthcheck** — does not verify network connectivity (still open)

Issues 1 and 4 are now resolved. The remaining issues are medium-to-low priority for normal operation (the bot runs on a stable home network), but become problematic during network outages, DNS server failures, or when running in environments with unreliable connectivity.

**Recommended next steps:**
- **Medium priority:** Issue 2 (voice connection), Issue 3 (delivery loop) — these affect user experience during transient issues
- **Low priority:** Issue 5 (circuit breaker), Issue 6 (healthcheck) — these are operational improvements for extended outages
