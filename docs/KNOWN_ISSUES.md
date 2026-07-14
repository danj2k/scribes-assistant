# Scribe's Assistant — Known Issues

## Limitations

- **No real-time transcription**: Transcripts are produced after the session ends, not during play. This is by design (local Whisper inference is too slow for real-time on this hardware).
- **Speaker diarisation is approximate**: Based on voice fingerprinting, not a dedicated diarisation model. Overlapping speakers are not separated.
- **WAV overhead**: py-cord decodes Opus to WAV before delivering audio, adding unnecessary encoding overhead. Cannot be avoided without switching to an alpha-quality library.

## Technical Debt

- **py-cord 2.8.0 DAVE voice reception bug**: The DAVE (End-to-End Encryption) refactor introduced a `SinkEventRouter` and `PacketRouter` that expect seven attributes/methods missing from `Sink`, `RTPPacket`, and `VoiceClient` (`__sink_listeners__`, `walk_children`, `is_opus`, `RTPPacket.type`, `VoiceClient.recording`, `VoiceClient.decoder`, `Sink.write` VoiceData unwrapping). Worked around with seven monkey-patches in `bot/main.py` (see IMPLEMENTATION_NOTES.md). Remove the patches when py-cord fixes this upstream (tracked: pycord issue #3139).

## Future Considerations

- Campaign wiki integration (mentioned in design doc as potential extension)
- Better speaker diarisation if hardware improves
- Support for concurrent sessions if needed in future
