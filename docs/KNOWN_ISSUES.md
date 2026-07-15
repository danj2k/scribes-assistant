# Scribe's Assistant — Known Issues

## Limitations

- **No real-time transcription**: Transcripts are produced after the session ends, not during play. This is by design (local Whisper inference is too slow for real-time on this hardware).
- **Speaker diarisation is approximate**: Based on voice fingerprinting, not a dedicated diarisation model. Overlapping speakers are not separated.
- **WAV overhead**: py-cord decodes Opus to WAV before delivering audio, adding unnecessary encoding overhead. Cannot be avoided without switching to an alpha-quality library.

## Technical Debt

- **py-cord PR #3159 dependency (unmerged)**: We install py-cord from the PR #3159 branch (`git+https://github.com/Pycord-Development/pycord@refs/pull/3159/head`) because py-cord 2.8.0 ships DAVE handshake code but does NOT implement DAVE decryption for incoming audio (pycord issue #3139). PR #3159 properly implements DAVE E2E decryption for voice reception but had not merged as of July 2026 (needs two reviews). Three PR #3159 quirks are worked around in `bot/commands.py`: (1) `AudioReader._stop()` guards the callback with `if self.after and self.args:` — `self.args` is an empty tuple (falsy) when no extra args are passed to `start_recording()`, so the callback never fires; the fix is to pass a dummy `None` arg; (2) the callback signature changed from `after(exc)` to `after(sink, *args)` — the sink is the first positional arg, not an exception; (3) `_stop()` calls `sink.cleanup()` after the callback, so the callback must not call `audio_data.cleanup()` or `format_audio()` itself (raises `SinkException`). Two residual monkey-patches remain in `bot/main.py` for issues PR #3159 does not fix: `RTPPacket.type` and `VoiceClient.start_recording` (setting `sink.vc`). Both are `hasattr`-guarded no-ops if fixed upstream. When PR #3159 merges and a pycord release ships, replace the git URL in `requirements.txt` with a pinned version and re-check whether the two remaining patches can be removed.
- **libopus Docker dependency**: The bot Dockerfile installs `libopus0` via apt-get because `python:3.11-slim` omits it. py-cord loads this shared library via ctypes at runtime for Opus decoding. If the package is removed or the base image changes, recording will fail with `OpusNotLoaded`.

## Future Considerations

- Campaign wiki integration (mentioned in design doc as potential extension)
- Better speaker diarisation if hardware improves
- Support for concurrent sessions if needed in future
