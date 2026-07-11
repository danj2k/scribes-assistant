# Scribe's Assistant — Project Documentation

## Purpose

Scribe's Assistant is a Discord bot for automated transcription of tabletop RPG sessions (initially D&D). It records voice from Discord voice channels (one audio stream per speaker), transcribes each stream with a local Whisper-based model, merges the results chronologically using token-level timestamps, and posts a timestamped, speaker-labelled transcript back to the game channel.

## Goals

- Capture voice audio from Discord voice channels with per-speaker separation
- Transcribe speech using a local Whisper-based speech-to-text model (sherpa-onnx)
- Deliver formatted, timestamped transcripts with speaker identification
- Maintain a lexicon of unfamiliar or fantasy terminology to improve transcription accuracy
- Run on modest hardware (i3-8100, 4 cores, 16GB RAM, no discrete GPU)
- Run entirely in Docker with Docker Compose

## Non-Goals

- Real-time transcription (transcripts are produced after the session ends)
- Automatic translation or multilingual support
- Cloud-based processing — everything runs locally
- Campaign wiki integration (potential future extension, not part of v1)
- Multi-server scalability (designed for a single D&D group)

## Constraints

- Hardware: i3-8100 (4 cores), 16GB RAM, Intel integrated GPU only (not useful for ONNX)
- The Discord bot is free-tier; no paid API usage
- The transcription model must fit comfortably alongside the bot on available cores
- No external dependencies beyond Discord's gateway and the local transcription stack
- Model weights must survive Docker container rebuilds via mounted volumes
