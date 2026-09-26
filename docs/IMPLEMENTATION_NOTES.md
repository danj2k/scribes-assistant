# Scribe's Assistant — Implementation Notes

## Non-Obvious Details and Tricky Areas

### py-cord Voice Receive Limitation

py-cord decodes Opus to WAV internally before passing audio data to the receive callback. This means we cannot avoid the WAV encoding overhead. The alternative (discord-ext-voice-recv, which exposes raw Opus) is alpha quality and not suitable. Accept WAV as-is at capture time.

### sherpa-onnx Thread Management

sherpa-onnx will consume as many CPU cores as available by default. On a 4-core machine, this will starve the bot container and the OS. Explicitly clamp thread count in the sherpa-onnx configuration — likely to 2-3 cores, leaving headroom for the bot and system processes. Test empirically to find the right balance.

### Multi-Speaker Temporal Alignment — TimestampedWaveSink

Discord uses Discontinuous Transmission (DTX): when a user is silent, no RTP packets are sent for them. py-cord's base `WaveSink` simply appends PCM data to a per-user `BytesIO` as packets arrive — no silence padding for gaps. This means each user's WAV file:

1. **Starts at their first speech**, not at recording start. A user who waits 30 seconds before speaking has their timeline shifted by 30 seconds relative to the first speaker.
2. **Has all silence gaps removed.** When the user pauses mid-speech (DTX), those silent frames are simply omitted, compressing their timeline.

The result: Whisper timestamps from different speakers' WAV files are not comparable. Speaker A's 00:30 and speaker B's 00:30 correspond to different absolute times. When the transcriber merges segments by timestamp, dialogue becomes nonsensical — questions appear after answers, interjections are displaced.

`TimestampedWaveSink` (bot/timestamped_sink.py) fixes both problems by subclassing `WaveSink`:

- **Initial offset** — on each user's first packet, pads their file with silence for the elapsed wall-clock time since recording start. The recording start is captured via `time.monotonic()` in `bot/commands.py` right before `start_recording()` is called, and passed to `TimestampedWaveSink` as the `recording_start` parameter. This means every user's WAV file shares the same session-level zero point, rather than being anchored to whichever user happened to send the first audio packet. This prevents timeline shifts when DAVE MLS handshakes complete at different times for different users. Uses `time.monotonic()` because RTP timestamps are per-SSRC and not comparable across users.
- **DTX gaps** — on subsequent packets, if the RTP timestamp delta exceeds one frame, pads silence for the gap. The RTP timestamp marks the start of each packet's audio, so a gap of N frames means N-1 frames of silence between the end of the previous PCM and the start of the current one. Uses unsigned 32-bit subtraction for wraparound safety. Gaps exceeding 60 seconds are treated as SSRC resets and not padded.

Opus frame constants: 48 kHz, 1 channel (mono), 16-bit samples, 960 samples per frame (20 ms), 1920 bytes per frame. These are fixed by the Opus codec standard and Discord's voice transport.

### Audio Format Handling

py-cord's Opus decoder outputs stereo PCM (2 channels) even though Discord sends mono audio per user. `TimestampedWaveSink._pcm_to_mono()` extracts the left channel by taking every other sample, producing clean mono PCM. The WAV header is written as mono (1 channel, 48kHz, 16-bit) by both `TimestampedWaveSink.format_audio()` and the `_write_wav_file()` call in `bot/commands.py`. The transcriber reads these mono WAV files with `soundfile.read()`, which returns a numpy float32 array. No external conversion step is needed:

- **Resampling** — the transcriber passes the original sample rate (48kHz) to `stream.accept_waveform(sample_rate, audio)`. sherpa-onnx resamples internally.
- **Mono input** — the audio is already mono when it reaches the transcriber, so no stereo-to-mono downmix is needed.

No ffmpeg or external audio conversion tool is used anywhere in the pipeline. Both sherpa-onnx (bundles libonnxruntime and libasound in its pip wheel) and soundfile (bundles libsndfile in `_soundfile_data/`) are self-contained — no system audio libraries are installed in the container.

### Lexicon Integration — Two-Stage Correction

The lexicon improves transcription quality through two independent mechanisms:

**Stage 1: Hotwords bias (before transcription)**
The lexicon terms are formatted by `build_hotwords()` into a forward-slash separated string: `"Term1/Term2/Term3"`. This is passed to sherpa-onnx via `create_stream(hotwords=...)`. Hotwords are a hard decoding bias — the model is strongly biased toward recognising these terms during transcription. Capped at 100 terms to stay within sherpa-onnx token limits.

**Empty hotwords → None:** When the lexicon is empty, `build_hotwords([])` returns `""`. This must NOT be passed to `create_stream()` as-is — sherpa-onnx's Python wrapper only skips the C++ contextual biasing code path when `hotwords is None`. An empty string (`""`) enters the biasing path, which prints "Only transducer models support contextual biasing" and segfaults on Whisper (non-transducer) models. The fix in `transcriber/worker.py` passes `hotwords or None` to `create_stream()`, converting `""` to `None`.

**Segment timestamps:** `from_whisper()` accepts `enable_segment_timestamps` (default `False`). When enabled, sherpa-onnx parses Whisper's native `<|0.00|>` timestamp tokens to produce segment-level start times, stored in `result.segment_timestamps` (list of floats, seconds) alongside `result.segment_texts` (list of strings) and `result.segment_durations`. This works with any standard Whisper ONNX model — no cross-attention outputs needed (unlike `enable_token_timestamps`, which requires attention weights in the model and silently produces empty lists on standard exports). The `load_model()` method in `transcriber/worker.py` passes `enable_segment_timestamps=True`. The `_build_segments()` method reads these fields and returns a list of `TranscriptSegment` objects with `start_time` and `text`. When segment data is unavailable, it returns `[]` and the transcript builder falls back to untimestamped per-speaker blocks.

**28-second audio chunking:** sherpa-onnx Whisper only processes the first 30 seconds of audio and silently discards the rest (hardcoded in `offline-recognizer-whisper-impl.h`). The `transcribe()` method in `transcriber/worker.py` splits the audio into 28-second chunks, transcribes each independently, and offsets each chunk's segment timestamps by the chunk's start time so the merged result preserves chronological order across the full file. Text from all chunks is joined with spaces.

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

The delivery loop polls for sessions ready to be delivered (status = COMPLETE, transcript_path set, thread_id NULL). It previously accessed the database's private `_cursor()` context manager directly, coupling it to the database's internal implementation. It now calls the public `Database.get_sessions_for_delivery()` method instead, which returns full row dicts (id, transcript_path, discord_channel_id) ordered by `ended_at`. The delivery loop has no direct SQL access and no unnecessary commit after the SELECT query.

### Delivery Thread Visibility — Public, Not Private

py-cord's `TextChannel.create_thread()` defaults `type` to `ChannelType.private_thread` when no `message` argument is passed. Private threads are invisible to all users except the bot and explicitly-added members. Since our delivery code calls `create_thread()` without a message (transcripts are posted as standalone threads, not attached to an existing message), the transcript was being posted to a private thread that no one could see.

We must explicitly pass `type=discord.ChannelType.public_thread` to make the thread visible to all users in the channel.

### Transcript Delivery as File Attachment

Transcripts are delivered as `.txt` file attachments in a Discord thread, not as inline message content. A 3-hour D&D session can easily produce tens of thousands of characters of transcript text — far exceeding Discord's 2000-character per-message limit. Posting inline would require splitting across many messages, cluttering the thread and making the transcript difficult to read or download. A file attachment (`discord.File` wrapping a `BytesIO` of UTF-8 text, named `{session_id}.txt`) lets users download or view the transcript cleanly regardless of length.

### py-cord PR #3159 — DAVE Voice Reception Support

py-cord 2.8.0 refactored voice reception to support Discord's DAVE (End-to-End Encryption) protocol, but the DAVE decryption for incoming audio was never implemented — pycord itself emitted `RuntimeWarning: Voice reception is currently broken due to Discord's DAVE protocol` (router.py:124, issue #3139). This caused `OpusError: corrupted stream` because DAVE-encrypted bytes were fed directly to the Opus decoder.

PR #3159 ("refactor(voice): Strict type checking in voice internals & DAVE Support (rec)") by Paillat-dev properly implements DAVE E2E decryption for voice reception. It is a 35-commit PR that was confirmed working by the community (June 2026) but had not yet merged into pycord master as of July 2026 (it needs two reviews). We install it from the PR branch in `requirements.txt`:

```
git+https://github.com/Pycord-Development/pycord@refs/pull/3159/head
```

The Dockerfile installs `git` (needed for `pip install git+...`) alongside `libopus0`.

When PR #3159 merges and a new pycord release ships, replace the git URL in `requirements.txt` with a pinned version (e.g. `py-cord==2.9.0`).

The PR also fixes six of the eight bugs we previously monkey-patched in `bot/main.py`:

1. `Sink.__sink_listeners__` — now a class attribute on `Sink` (empty list).
2. `Sink.walk_children()` — now a generator method on `Sink` (yields nothing).
3. `Sink.is_opus()` — now a method on `Sink` returning `False`.
4. `Sink.recording` — now a property on `Sink` checking `self.vc.is_recording()`.
5. `VoiceClient.decoder` — WaveSink now uses `OpusDecoder` class attributes directly (`OpusDecoder.CHANNELS`, etc.) instead of `vc.decoder`.
6. `Sink.write()` — now accepts `VoiceData | bytes`, unwrapping `.pcm` internally.

Those six patches have been removed. Two patches remain for issues PR #3159 does NOT fix:

1. **`RTPPacket.type`** — `reader.py` logs `packet.type` for unexpected RTCP packets. `RTPPacket` lacks the `type` class attribute that `RTCPPacket` defines. Set to `None` (same default as the `RTCPPacket` base).

2. **`VoiceClient.start_recording`** — the DAVE refactor commented out the assignment of `sink._client` in `AudioReader.__init__` (reader.py: `# self.sink._client = client`). `Sink.client` is a property returning `self.vc`, so without this patch `sink.client` is `None` and `PacketDecoder._process_packet` hits `assert self.sink.client` (AssertionError) on the first audio packet. The patch wraps `start_recording` to set `sink.vc = self` before calling the original, replicating the old `Sink.init(vc)` behaviour.

Both remaining patches are guarded with `hasattr()` / a `_start_recording_patched` flag so they are no-ops if py-cord fixes them upstream.

All patches run at module import time, before `bot.run()`. They target the `RTPPacket` and `VoiceClient` classes directly, so any sink subclass will also work.

Note: A previous patch #9 monkey-patched `PacketDecryptor.decrypt_rtp` to set `packet.decrypted_data = raw_payload` when DAVE was not ready. This was removed — it fed DAVE-encrypted bytes to the Opus decoder, causing `OpusError: corrupted stream`. The correct behaviour is to leave `decrypted_data` as `None` so the reader drops the packet until the DAVE MLS handshake completes. With PR #3159, DAVE decryption is now properly implemented so this scenario should not arise.

### DAVE Readiness Wait in /start

After the WebSocket voice handshake completes, py-cord begins an asynchronous MLS (Messaging Layer Security) key exchange with Discord's voice server:

1. `reinit_dave_session()` creates a `davey.DaveSession` and sends an MLS key package (opcode 24).
2. Discord responds with binary MLS messages: external sender package, proposals, and either a commit or welcome.
3. `process_commit()` or `process_welcome()` must succeed for the session to transition from `inactive` to `active` (`dave_session.ready = True`).
4. Only then can `decrypt_rtp()` successfully decrypt incoming audio packets.

If `start_recording()` is called before the handshake completes, the `AudioReader` receives DAVE-encrypted packets, `decrypt_rtp()` returns `None` (correctly dropping them), but the `PacketRouter` may still feed some packets to the Opus decoder, causing `OpusError: corrupted stream`.

The `/start` command now polls `vc._connection.dave_session.ready` every 250ms for up to 15 seconds before calling `start_recording()`. If DAVE is not enabled on the channel (`dave_session is None`), the wait is skipped. If the handshake doesn't complete within 15 seconds, the bot disconnects and informs the user rather than proceeding with a broken session.

### TimestampedWaveSink -- Disk-Backed Writes

The custom `TimestampedWaveSink` (subclass of py-cord's `WaveSink`) overrides `write()` to save PCM data directly to temporary files on disk (`NamedTemporaryFile(delete=False)`) via `_temp_paths[user]`, rather than appending to in-memory `BytesIO` buffers as the base `WaveSink` does. This eliminates the `asyncio.to_thread()` file-writing step entirely -- the recording callback only needs to move temp files to their final paths. On the same filesystem this is an instant `os.rename()`; when `/tmp` and `/data` are on different filesystems (e.g. Docker overlay vs a bind-mounted volume), `shutil.move` falls back to copy+delete via a thread pool so the event loop isn't blocked.

py-cord's `AudioData` still stores its `BytesIO` buffer at `.file` in the base class, but `TimestampedWaveSink.write()` bypasses this by writing PCM directly to a temp file path tracked in `_temp_paths`. PR #3159's `AudioReader._stop()` calls `sink.cleanup()` (including `audio_data.cleanup()` + `format_audio()`) **after** the after-callback, so the callback must not call `cleanup()` itself — `AudioData.cleanup()` raises `SinkException("already finished writing")` if called twice.

### Voice Receive Diagnostics — Two-Layer Instrumentation

Some Discord users' audio arrives as random noise (high-variance PCM) while others are healthy. The failure can occur inside py-cord before `Sink.write()` is called, so logging only at the sink is insufficient. Two-layer diagnostic instrumentation was added to capture the full receive path:

**Layer 1: PacketDecoder wrapper (bot/main.py)**

`_install_voice_receive_diagnostics()` wraps `discord.opus.PacketDecoder._decode_packet` at module import time. The wrapper:
- Logs every decode attempt at DEBUG level (SSRC, sequence, timestamp, user_id)
- Logs failures at ERROR level with the exception message and DAVE session status (`_dave_status` attribute if present)
- Logs successes at DEBUG level with PCM byte count
- Is guarded with `hasattr()` checks and a `_scribes_diagnostics_patched` flag so it's a no-op if py-cord moves or renames these internals
- Logs metadata only — never encrypted packets or raw audio

**Layer 2: Sink-side telemetry (bot/timestamped_sink.py)**

`TimestampedWaveSink._log_receive_diagnostics()` is called from `write()` after PCM extraction. It tracks per-(user, SSRC) counters:
- `_diagnostic_packet_count`: total packets received
- `_diagnostic_zero_pcm_count`: packets with all-zero PCM (silence or decoder failure)
- `_diagnostic_high_variance_count`: packets with high-variance PCM (random decoder garbage)

The high-variance check samples the first 1000 samples (2000 bytes) and computes a variance proxy (sum of absolute differences between consecutive samples). Normal speech has variance_proxy < 50000 for 1000 samples; random decoder garbage typically exceeds 100000. This threshold was calibrated from production recordings where healthy users had autocorrelation ~0.96 and corrupted users had autocorrelation ~0.01.

Logging is rate-limited: first occurrence and every 50th/100th occurrence to avoid log spam. Periodic summaries (every 500 packets) are logged at DEBUG level.

**Why two layers:** The PacketDecoder wrapper catches failures before they reach the sink (e.g., DAVE handshake failures, Opus decoder errors). The sink-side telemetry catches suspicious PCM patterns that made it through the decoder but are still garbage. Together they provide end-to-end visibility into the receive path without modifying py-cord behaviour.

### libopus Docker Dependency

py-cord decodes incoming Opus audio to PCM via `ctypes` at runtime, loading `libopus.so.0`. The `python:3.11-slim` base image does not include this library, so the Dockerfile installs `libopus0` via `apt-get`. Without it, `opus.Decoder()` raises `OpusNotLoaded` at the first incoming audio packet, which kills the recording session.

### Auto-Disconnect on Failed Recording

When a recording session fails (error during recording or zero audio captured), the after-callback's `finally` block disconnects the bot from the voice channel and sends a notification to the text channel where `/start` was issued. Without this, the bot would remain in the voice channel after a failed session with no user feedback — the user would see "Recording started!" followed by silence. The auto-disconnect and notification code is wrapped in `try/except` so cleanup failures don't prevent the processing future from resolving (which would hang `/stop`).

A `_processing_started` flag guards against the after-callback being invoked more than once (e.g. py-cord's DAVE router invoking it from both the error handler and `stop_recording()`), which would cause duplicate log lines and double audio file writes.

### py-cord Recording After-Callback

PR #3159's `start_recording(sink, callback, *args)` stores `callback` as an `AudioReader.after` callback, with `*args` forwarded to the callback at invocation time. When `stop_recording()` is called, `AudioReader._stop()` invokes `after(sink, *args)` **synchronously** from the voice client's thread — the sink is the first positional argument, not an exception. The callback must be a regular (sync) callable, not a coroutine function.

**`self.args` truthiness guard (PR #3159 quirk):** `AudioReader._stop()` guards the callback invocation with `if self.after and self.args:`. If no `*args` are passed to `start_recording()`, `self.args` is an empty tuple (falsy) and the callback is **never invoked**. The `/start` command passes a dummy `None` argument: `vc.start_recording(sink, after_cb, None)`. This is harmless — the dummy is forwarded as `*args` to `after_cb`, which ignores them.

**`sink.cleanup()` called by `_stop()`:** PR #3159's `AudioReader._stop()` calls `sink.cleanup()` **after** the after-callback. This calls `audio_data.cleanup()` + `format_audio()` for each `AudioData`, finalising WAV headers. For the disk-backed `TimestampedWaveSink`, `format_audio()` is a no-op since PCM data was written directly to the temp file -- the WAV header was already finalised by `write()`. The after-callback must **not** call `audio_data.cleanup()` or `format_audio()` itself — `AudioData.cleanup()` raises `SinkException("already finished writing")` if called twice.

The bot uses `make_recording_after_callback()` to create a sync callback that:
1. Captures the sink, session metadata, and event loop at creation time (in `start_command`)
2. Schedules the async audio-processing coroutine via `asyncio.run_coroutine_threadsafe()` — safe from any thread
3. Returns a `Future` that `stop_command` awaits with a 30-second timeout

This ensures `/stop` does not call `end_session()` (which transitions to QUEUED) until all audio files have been written to disk and registered in the `audio_files` table. Without this, the transcriber could find zero files for a session and produce an empty transcript.

#### Non-blocking file writes (disk-backed sink, WAV header fix)

The async processing coroutine (`_process_recording`) runs on the event loop. The custom `TimestampedWaveSink` writes raw PCM data directly to temporary files on disk as packets arrive during recording. However, **the temp files contain only raw PCM — no WAV header**. py-cord's `WaveSink.format_audio()` (called by `AudioReader._stop()` via `sink.cleanup()`) reads the PCM data from the temp file, creates a new `BytesIO` with a proper WAV header + PCM data, and assigns it to `AudioData.file`. For the disk-backed `TimestampedWaveSink`, `format_audio()` is technically a no-op from the sink's perspective (the subclass overrides it) — py-cord's base-class `format_audio()` still runs, producing the WAV-formatted BytesIO, but the temp file on disk remains raw PCM.

The after-callback (`_process_recording`) must **not** rename the raw-PCM temp file to `.wav` — soundfile/libsoundfile will reject it with "Format not recognised". Instead, it reads the WAV-formatted data from `audio_data.file` (the BytesIO that `format_audio()` created) and writes it to the final path via `_write_wav_file()`, which runs in `asyncio.to_thread()` so large files (~1.3 GB) don't block the event loop. The raw PCM temp file is then deleted with `os.unlink()`.

Previously, the code used `os.rename()` to move the temp file to its final path, which worked when the temp file already contained a valid WAV header. This broke when the custom `TimestampedWaveSink` separated the PCM storage (on-disk temp file) from the WAV header application (in-memory BytesIO).

#### Filename sanitisation

py-cord keys `sink.audio_data` by `User`/`Member` objects, not numeric IDs. The callback resolves the display name via `guild.get_member(user_obj)` (falling back to `str(user_obj)`) and passes it through `_sanitize_filename()` before constructing the filepath. This replaces `<>:"/\|?*\0` with underscores, strips leading/trailing spaces and dots, and collapses repeated underscores — preventing a display name like `Duckinell/DM` from creating a spurious directory. The unsanitised display name is stored in the DB `speaker_name` column for transcript output.

If the callback raises an exception or the future times out, `stop_command` logs the error and proceeds with `end_session()` anyway — partial or no audio is better than a stuck session.

### Empty Recording Handling

When nobody speaks during a recording, `sink.audio_data` is empty. The callback writes zero WAV files, calls `fail_session()` to mark the session as FAILED (with `ended_at` set), and resolves the future with result 0. `stop_command` sees the zero result, sends an ephemeral message to the user ("No audio was captured — no transcript will be generated"), and returns without calling `end_session()`.

**Callback timeout:** If the after-callback never fires (e.g. `self.args` quirk before the dummy arg fix, or a py-cord bug), `stop_command`'s 30-second `asyncio.wait_for` times out. The future resolves with `None` (the `except asyncio.TimeoutError` return value). `stop_command` checks `audio_count is None` (not `audio_count == 0`) to distinguish a timeout from an empty recording: a timeout calls `fail_session()` and sends an ephemeral error message; an empty recording sends the "no audio captured" message. Both cases return without calling `end_session()`, preventing the session from being queued for transcription with no audio.

The transcriber also has a safety net: before each poll of `get_next_queued_file()`, it queries `get_queued_sessions_without_files()` and marks any sessions stuck in QUEUED with zero audio files as FAILED. This catches edge cases where the callback's `fail_session()` call didn't fire (e.g. callback exception, timeout, or files lost after writing).

### Timestamp-Based Session IDs

Session IDs are derived from the recording start time: `YYYY-MM-DD_HH-MM-SS`. This makes them human-readable, naturally sorted, and unique (no two sessions can start at the exact same second). Timezone is UTC for consistency.

### Docker Secrets

Bot tokens and other secrets are provided via Docker secrets, which mount as files at `/run/secrets/<name>`. The bot reads the token from `/run/secrets/bot_token` rather than environment variables. This is more secure — secrets are not visible in process listings or Docker inspect output.

The `Config.bot_token` property validates the token file at access time: if the file does not exist, it raises `FileNotFoundError` with an actionable message (including the expected path and how to create it); if the file exists but is empty or whitespace-only, it raises `ValueError`. `bot/main.py` catches both exceptions at startup, logs a clear error message, and exits with status 1. This prevents the cryptic traceback that `bot.run(None)` produces when the token is silently missing.

The `bot:` service in `docker-compose.yml` explicitly declares `secrets: [bot_token]` so Docker Compose mounts the file. Without this declaration, the top-level `secrets:` block defines the secret but never attaches it to the service.

### Config File Mount

`config.yaml` is bind-mounted into both containers at `/data/config.yaml:ro` via docker-compose volumes. The `CONFIG_PATH` environment variable (set to `/data/config.yaml` for both services) tells each container where to find the config. The config file is never baked into the Docker image — this ensures the same image works across environments (dev, staging, production) with different configs. The bot Dockerfile does not `COPY` any config file; the transcriber reads `CONFIG_PATH` from env with the same default.

### Bot Health Check (Heartbeat)

The bot's Docker health check verifies the event loop is alive and the Discord gateway is connected, rather than just checking that the library is importable. The mechanism:

1. On `on_ready` (fires after the gateway handshake completes), the bot writes a timestamp to `/data/bot.heartbeat` and starts a background `_heartbeat_loop` task.
2. The loop updates the heartbeat file every 30 seconds.
3. `bot/healthcheck.py` reads the file's modification time. If the file is missing or older than 90 seconds (3 missed update cycles), the check fails (exit 1). Otherwise it passes (exit 0).

This catches three failure modes that a trivial `import discord` check would miss: a dead event loop (stops updating), a crashed process (no process to write), and a lost gateway connection (on_ready never fires, no initial heartbeat). The 90-second threshold gives 3x tolerance over the 30-second update interval, avoiding false positives during momentary event loop congestion.

### Transcriber Health Check (Heartbeat)

The transcriber's Docker health check follows the same heartbeat pattern as the bot. The transcriber writes a heartbeat file (`/data/transcriber.heartbeat`) at two points:

1. **After model load** — an initial heartbeat written before the polling loop starts, confirming the model loaded successfully and the process is ready to work.
2. **After each poll cycle** — updated at the end of each iteration of the main loop (after the try/except block, before the loop repeats), confirming the process is actively polling.

`transcriber/healthcheck.py` reads the file's modification time and declares unhealthy if older than 300 seconds (5 minutes). This threshold is larger than the bot's (90s) because a single transcription can take several minutes for long recordings — the transcriber doesn't update the heartbeat during transcription, only between poll cycles. 5 minutes gives enough headroom for a long file while still catching a genuinely stuck or crashed process.

The `start_period` is set to 120 seconds in both the Dockerfile (`HEALTHCHECK ... start-period=120s`) and docker-compose.yml (`start_period: 120s`). This is necessary because the transcriber must download and load the Whisper model on first start, which can take over a minute. Without this grace period, the health check would declare the container unhealthy before the model finished loading.

The trivial `python -c "import sys; sys.exit(0)"` check was replaced with this meaningful check because a crashed transcriber process would still pass the import test.

### Transcriber Dockerfile mkdir Paths

The transcriber Dockerfile previously created directories under `/app/data/` (`/app/data/recordings`, `/app/data/transcripts`, `/app/data/logs`). However, the Docker volume is mounted at `/data` (not `/app/data`), so these directories were dead paths — the transcriber wrote to `/data/recordings/`, `/data/transcripts/`, etc. via the volume mount, and the mkdir commands created empty directories that were never used. Fixed by changing the mkdir commands to target `/data/recordings`, `/data/transcripts`, `/data/logs` — matching the actual volume mount point.

### Bot Graceful Shutdown (SIGTERM)

Docker sends SIGTERM to a container's PID 1 on `docker stop`. Without a handler, the bot process would be killed by SIGKILL after the 10-second grace period, potentially mid-delivery or mid-gateway-event.

The bot registers a SIGTERM handler in `setup_hook()` — a py-cord hook that runs after the event loop starts but before `on_ready`. The handler:

1. Calls `DeliveryLoop.stop()` — cancels the delivery polling asyncio task and sets the running flag to False, preventing further poll cycles.
2. Calls `bot.close()` — closes the Discord gateway connection cleanly (sends a proper WebSocket close frame).

An `on_disconnect` callback provides additional cleanup logging.

`DeliveryLoop.stop()` is a safe no-op if the loop was never started (the task is None) or has already completed (the task's `done()` returns True). It sets the `_running` flag to False and cancels the task with `task.cancel()`, then awaits it with `asyncio.wait_for(task, timeout=5.0)` to allow the current poll cycle to finish (up to 5 seconds) before forcing cancellation.

The handler is registered in `setup_hook()` rather than at module level because the signal handler needs access to the running event loop and the bot instance. `setup_hook()` runs within the already-started event loop, so `loop.add_signal_handler()` can be called safely.

### Config Validation

`Config.validate()` performs startup checks before the bot connects to Discord. It returns a list of error strings (empty list means valid):

1. **Config file existence** — verifies the config file path exists and is readable.
2. **`discord.guild_id` non-zero** — a zero/missing guild ID would cause py-cord to sync slash commands globally instead of to the intended guild, which is almost certainly a misconfiguration.
3. **`discord.transcript_channel_id` non-zero** — a zero/missing channel ID means the bot has nowhere to deliver transcripts.

`main()` calls `validate()` after setting up logging (so errors are logged) but before creating the bot instance. If errors are found, each is logged as an ERROR and the process exits with status 1. This provides clear, actionable error messages instead of a cryptic py-cord traceback when the bot tries to sync commands to a non-existent guild.

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

### Numpy Array Passthrough to sherpa-onnx (Bug #19)

`soundfile.read()` returns audio samples as a numpy `float32` array. The transcriber passes this array directly to `stream.accept_waveform(sample_rate, audio)` without calling `.tolist()`. sherpa-onnx's pybind11 bindings accept numpy arrays natively via the buffer protocol — no Python-level conversion is needed.

Calling `.tolist()` would convert the compact C-backed array into a list of individual Python float objects. For a 30-minute D&D session recorded at 16 kHz, that's ~28.8 million float objects (16,000 samples/second × 1,800 seconds), each consuming ~24 bytes of Python object overhead — roughly 690 MB of wasted memory on top of the ~115 MB the numpy array itself uses. The conversion is also CPU-bound, adding seconds of latency before transcription can begin.

After stereo-to-mono downmix (`audio.mean(axis=1)`), the result is still a numpy array and is passed through unchanged.

### Streaming Audio Read (Block-Based I/O)

Previously, `transcribe()` loaded the entire WAV file into memory via `sf.read(audio_path, dtype="float32")`. For a 3-hour stereo 48 kHz recording (two speakers, 16-bit), this allocates ~4 GB of RAM — more than the Docker container's available memory for long sessions.

The transcriber now uses `sf.SoundFile` as a context manager with block-based reading:

1. Opens the file with `sf.SoundFile(audio_path)` — reads only the WAV header, not the audio data.
2. Seeks to the current chunk offset: `f.seek(offset)`.
3. Reads one 28-second chunk at a time: `f.read(frames=chunk_samples, dtype="float32")`.
4. Downmixes to mono inline: `chunk.mean(axis=1)` if `chunk.ndim > 1`.
5. Passes the chunk to `stream.accept_waveform()` immediately, then advances `offset += chunk_samples`.

Peak memory drops from ~4 GB (whole file) to ~5 MB (one 28-second chunk at 48 kHz mono float32). The chunk size is `28 * sample_rate` samples — the same 28-second segment length used for Whisper's 30-second processing window.

### Recording Retention (Automatic Purge)

Recordings are large (~11.5 MB/min per speaker at 48 kHz/16-bit/stereo). A weekly 3-hour 5-player D&D session accumulates ~10 GB of WAV files per session, ~40 GB/month. Without automatic purging, the Docker volume fills up within weeks.

The retention module (`bot/retention.py`) runs as a background task in the bot container:

1. **Startup sweep** — on `on_ready`, immediately purges old recordings.
2. **Periodic sweep** — every `recording.purge_interval_hours` (default 6 hours), repeats the sweep. Set to 0 for startup-only.

Age is determined from the database `sessions.started_at` column (ISO 8601 UTC) when available, falling back to the directory's modification time for orphaned directories not in the database (e.g. from a crash before the DB row was written).

Purging deletes the entire session directory (`/data/recordings/{session_id}/`) and marks all `audio_files` rows for that session as `purged` in the database. The `sessions` row and `transcript_path` are kept intact — transcripts are tiny (tens of KB) and retained indefinitely for historical reference.

Configuration in `config.yaml`:
```yaml
recording:
  retention_days: 8        # 0 = keep forever (no purge)
  purge_interval_hours: 6  # 0 = only on startup
```

Failures during purging (permission errors, locked files) are logged as warnings and do not crash the sweep — the remaining directories are still processed.


### Transcript Delivery

Discord has a file size limit (8MB for free servers). Most D&D sessions (3-4 hours) should produce transcripts well under this limit. If a transcript exceeds it, split into multiple parts or compress.

### Config Defaults Are Deep-Copied

`load_config()` in `shared/config.py` uses `copy.deepcopy(_DEFAULTS)` to produce the base config dict before merging YAML overrides. This ensures nested dicts (`discord`, `permissions`, `lexicon`, etc.) are independent copies, not shared references to the module-level `_DEFAULTS`. Without this, any in-place mutation of a nested default value (e.g. `config["discord"]["permissions"]["allowed_roles"].append(...)`) would permanently corrupt `_DEFAULTS` for all future `load_config()` calls within the same process.

The `_deep_merge()` helper uses a shallow `.copy()` internally, which is safe because it reassigns keys rather than mutating nested dicts in place — and the `base` it receives is already a deep copy from `load_config()`.


### Logging Configuration

Both containers share a single logging setup module (`shared/logging_setup.py`). The `setup_logging_from_config(config, name)` function configures the **root** logger with two handlers:

- **Console handler** — `StreamHandler` writing to **stdout** (visible via `docker compose logs`). Both containers use stdout, not stderr — this was previously inconsistent (the bot used stderr, the transcriber used stdout) and is now unified.
- **File handler** — `RotatingFileHandler` writing to `/data/logs/<name>.log`, max 10MB per file, 5 archived backups.

A duplicate-handler guard prevents double-attachment if `setup_logging` is called more than once in the same process (e.g. during re-import or test runs).

Both handlers use a single consistent format with `datefmt`:
```
2025-01-15 20:34:12 [scribes.bot] INFO: Session 2025-01-15_20-30-00 started in channel #general
2025-01-15 20:45:33 [scribes.transcriber] INFO: Transcription job queued for session 2025-01-15_20-30-00
```

The shared `/data/logs/` directory is mounted in both Dockerfiles so either container can access the logs for debugging.

Logging is configured through a single shared `logging` section in `config.yaml` (`level`, `max_size_mb`, `backup_count`). The `Config` properties (`log_level`, `log_max_size_mb`, `log_backup_count`) read only from `logging.*` — there are no bot-specific or transcriber-specific logging keys. Previously, `bot.log_level` / `bot.log_max_size_mb` / `bot.log_backup_count` existed in `_DEFAULTS` as duplicates of the `logging` section, and the Config properties fell back from `logging.*` to `bot.*`. These were removed because logging is a shared concern: both containers read the same keys, so having a bot-specific copy was confusing and error-prone.

### Slash Command Error Handling

Discord slash commands have built-in parameter validation — mistyped command names or parameter names are rejected by Discord's client before they reach the bot. The bot only needs to handle semantic errors:

- **Missing or invalid parameters** — respond with a user-friendly error message (e.g. "Term not found in lexicon")
- **Empty session** — `/status` or `/stop` when no session is active returns "No active recording session"
- **Concurrent session** — `/start` when already recording returns "Session already in progress"

These are normal application logic responses, not exceptions. Use `respond()` or `respond(embed=error_embed)` to give clear feedback.

### Command Permissions

All slash commands except `/help` call `_check_permission(interaction, config)` at the top of the handler. If `config.restrict_commands` is `False` (the default), all users are allowed. If `True` and `config.allowed_roles` is empty, all users are allowed. Otherwise the user's Discord role names (case-insensitive) and role IDs are checked against `allowed_roles`.

`/help` is intentionally left unrestricted — it only displays command usage text and has no side effects. `/invite` was initially left open alongside `/help`, but was later gated (Bug #22) because generating a bot invite URL is an admin action, not purely informational.

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
- **Voice channel join failure** — `start_command` creates the session record, then defers the interaction (`await interaction.response.defer(ephemeral=True)`) before attempting the voice channel join. Discord requires an interaction response within 3 seconds; voice join can exceed this under high latency. If the join fails, `fail_session()` is called (NOT `update_session_status(STATUS_FAILED)`) to set both `status=FAILED` and `ended_at`. Using `update_session_status` alone would leave `ended_at=NULL`, causing `get_active_session()` to keep returning the dead session and block all future `/start` commands in that guild. After deferring, all responses must use `interaction.followup.send()` instead of `interaction.response.send_message()`.
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

### Testing Philosophy

The test suite is curated to test what IS there, not what ISN'T. Tests that assert the absence of removed functionality (e.g. "DeliveryLoop has no lexicon attribute", "config has no bot.log_level key") are pruned after the corresponding bug fix is committed. They add maintenance burden without ongoing value — the correct behaviour is simply the default once the code is clean. Positive tests that exercise real functionality are preferred over negative tests that guard against regressions of bugs that can no longer recur because the code path has been removed entirely.

**Coverage:**

- `bot/delivery.py` (DeliveryLoop) — covered by `tests/test_delivery.py` (19 tests): constructor, start/idempotency, poll behaviour (complete sessions, no transcript path, already-delivered, empty list), delivery (thread creation, public thread type, file attachment, configured channel, channel-not-found, missing transcript file), run loop (poll-then-sleep, shutdown event, exception handling), and stop() (cancels running task, no-op when not running, no-op when task already done, logs message).
- `bot/voice.py` (idle timeout) — covered by `tests/test_voice.py` (9 tests): on_voice_state_update (bot members ignored, no voice client ignored, starts timer when alone, cancels on user join) and _idle_timeout (timeout ends session and disconnects, no active session just disconnects, cancellation is silent, task cleanup, not-recording state doesn't stop the bot).
- `shared/lexicon.py` — covered by `tests/test_lexicon.py` and `tests/test_lexicon_integration.py` (positive tests only: correction pipeline, tie-breaking, case preservation, tokenisation, hotwords format).
- `shared/database.py` — covered by `tests/test_database.py` (session lifecycle, file registration, segment storage, fail_session, queued sessions without files, single-session enforcement).
- `shared/config.py` — covered by `tests/test_config.py` (defaults, deep copy isolation, logging config, bot token validation, config validation).
- `transcriber/worker.py` — covered by `tests/test_transcriber_worker.py` (model loading, model size filenames, download, segment building, transcription result).
- `bot/healthcheck.py` and `transcriber/healthcheck.py` — covered by `tests/test_healthcheck.py` (12 tests): bot healthcheck (fresh/missing/stale/boundary, heartbeat writing, heartbeat loop, on_ready integration, task not restarted) and transcriber healthcheck (fresh/missing/stale/boundary).
- `transcriber/main.py` — covered by `tests/test_sigterm_handling.py` (SIGTERM flag handling).
- `bot/commands.py` — covered by `tests/test_commands.py` (permissions), `tests/test_recording_callback.py` (after-callback factory), `tests/test_start_command_defer.py` (interaction deferral).
- `shared/tar_utils.py` — covered by `tests/test_tar_utils.py` (path traversal protection).
- Transcript merge — covered by `tests/test_transcript_merge.py` (interleaved merge, tie-breaking, fallback, empty segments, timestamp formatting).
