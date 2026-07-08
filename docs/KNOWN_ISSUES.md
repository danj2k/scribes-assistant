# Scribe's Assistant — Known Issues

## Limitations

- **No real-time transcription**: Transcripts are produced after the session ends, not during play. This is by design (local Whisper inference is too slow for real-time on this hardware).
- **Speaker diarisation is approximate**: Based on voice fingerprinting, not a dedicated diarisation model. Overlapping speakers are not separated.
- **WAV overhead**: py-cord decodes Opus to WAV before delivering audio, adding unnecessary encoding overhead. Cannot be avoided without switching to an alpha-quality library.

## Technical Debt

(None yet — project is pre-implementation.)

## Future Considerations

- Campaign wiki integration (mentioned in design doc as potential extension)
- Better speaker diarisation if hardware improves
- Support for concurrent sessions if needed in future
