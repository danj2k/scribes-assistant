# Scribe's Assistant — Implementation Notes

## Non-Obvious Details and Tricky Areas

### py-cord Voice Receive Limitation

py-cord decodes Opus to WAV internally before passing audio data to the receive callback. This means we cannot avoid the WAV encoding overhead. The alternative (discord-ext-voice-recv, which exposes raw Opus) is alpha quality and not suitable. Accept WAV as-is at capture time.

### sherpa-onnx Thread Management

sherpa-onnx will consume as many CPU cores as available by default. On a 4-core machine, this will starve the bot container and the OS. Explicitly clamp thread count in the sherpa-onnx configuration — likely to 2-3 cores, leaving headroom for the bot and system processes. Test empirically to find the right balance.

### WAV to 16kHz Conversion

sherpa-onnx expects 16kHz mono WAV input. py-cord delivers audio at Discord's native rate (48kHz stereo). ffmpeg must be used in the transcriber to convert before running inference. This is an extra step but unavoidable given the py-cord limitation.

### Lexicon Initial Prompt Injection

The lexicon is injected into the Whisper `initial_prompt` parameter. This is a plain text hint, not a hard dictionary. The prompt should be formatted as a comma-separated list of words, preceded by a description like "This is a D&D session with fantasy terminology:" to give the model context. The prompt has a token limit — prioritise by frequency and importance.

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

### Transcript Delivery

Discord has a file size limit (8MB for free servers). Most D&D sessions (3-4 hours) should produce transcripts well under this limit. If a transcript exceeds it, split into multiple parts or compress.
