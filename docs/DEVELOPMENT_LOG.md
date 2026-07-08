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
