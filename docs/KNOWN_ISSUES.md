# Scribe's Assistant — Known Issues

## Limitations

- **No real-time transcription**: Transcripts are produced after the session ends, not during play. This is by design (local Whisper inference is too slow for real-time on this hardware).
- **Speaker diarisation is approximate**: Based on voice fingerprinting, not a dedicated diarisation model. Overlapping speakers are not separated.
- **WAV overhead**: py-cord decodes Opus to WAV before delivering audio, adding unnecessary encoding overhead. Cannot be avoided without switching to an alpha-quality library.

## Technical Debt

- **No transcript_channel_id implementation**: The config example documents discord.transcript_channel_id but it has no backing in _DEFAULTS or the Config class — it is a no-op.
- **Dead code in shared/lexicon.py**: get_terms_list() is only called by the now-orphaned build_initial_prompt() (removed in the Whisper migration). Needs cleanup or repurposing.
- **add_transcript() signature mismatch**: channel_id parameter is accepted but never stored — passed by caller under a different semantic.
- **Inconsistent shared package install**: bot and transcriber Dockerfiles use different pip install patterns (directory install vs editable install).

## Future Considerations

- Campaign wiki integration (mentioned in design doc as potential extension)
- Better speaker diarisation if hardware improves
- Support for concurrent sessions if needed in future
