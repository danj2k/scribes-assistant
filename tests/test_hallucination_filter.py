"""Tests for Whisper hallucination pattern filtering.

Verifies that non-speech descriptions produced by Whisper on low-energy
audio (growling, language tags, ambient sounds) are filtered from
transcripts.
"""
import pytest

from transcriber.worker import _is_whisper_hallucination


class TestHallucinationFilter:
    """Patterns observed in 2026-10-20 transcript that should be filtered."""

    @pytest.mark.parametrize("text", [
        "[BLANK_AUDIO]",
        "[silence]",
        "[ Silence ]",
        "[no audio]",
        "[ Pause ]",
        "[deep breath]",
        "[growling]",
        "[BURP]",
        "[Birds chirping]",
        "(growling)",
        "(Growling)",
        "(snoring)",
        "(Burps)",
        "(Bell rings)",
        "(Buzzing)",
        "(speaking in foreign language)",
        "*Spanish*",
        "*Gibberish*",
        "*Mumbling*",
    ])
    def test_hallucinations_detected(self, text):
        """Known hallucination patterns should be identified."""
        assert _is_whisper_hallucination(text) is True, (
            f"Expected '{text}' to be detected as hallucination"
        )

    @pytest.mark.parametrize("text", [
        "Hello, how are you?",
        "I'll set the back lever.",
        "I'm not super proud of you.",
        "I will wait until the next time.",
        "I sure am a nice little comic book on here.",
        "Oh no, you're broken, son.",
        "You're a kind of suspicious.",
        "I should have looked at some...",
        "I'm sorry, I'm...",
    ])
    def test_real_speech_not_filtered(self, text):
        """Actual transcribed speech should pass through."""
        assert _is_whisper_hallucination(text) is False, (
            f"Expected '{text}' to NOT be detected as hallucination"
        )

    def test_empty_string_not_hallucination(self):
        """Empty string is not a hallucination (handled separately)."""
        assert _is_whisper_hallucination("") is False

    def test_mixed_content_not_filtered(self):
        """Text with speech AND a bracket tag should not be filtered
        (the bracket filter only matches when the ENTIRE text is a tag)."""
        assert _is_whisper_hallucination("Hello [laughter] world") is False
