"""Transcriber — audio-to-text using sherpa-onnx.

Polls the shared SQLite database for queued audio files,
runs them through the Whisper model, and stores transcripts.
"""
