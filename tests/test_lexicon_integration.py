"""Tests for lexicon integration — hotwords bias and post-correction."""
import re
from unittest.mock import MagicMock, patch, AsyncMock
from shared.lexicon import Lexicon, build_hotwords
from shared.config import Config


class TestTranscriptionWorkerHotwords:
    """Test that hotwords are passed to create_stream()."""

    def test_transcribe_passes_hotwords_to_create_stream(self):
        """Hotwords string is forwarded to recognizer.create_stream()."""
        from transcriber.worker import TranscriptionWorker
        with patch("transcriber.worker.sf") as mock_sf, \
             patch.object(TranscriptionWorker, "load_model"):
            mock_audio = MagicMock()
            mock_audio.shape = (16000,)
            mock_sf.read.return_value = (mock_audio, 16000)

            w = TranscriptionWorker("/tmp/model")
            w.recognizer = MagicMock()

            w.transcribe("/tmp/test.wav", hotwords="Theron/Grimjaw")

            w.recognizer.create_stream.assert_called_once_with(
                hotwords="Theron/Grimjaw"
            )

    def test_transcribe_no_hotwords_when_none(self):
        """create_stream called without hotwords when none provided."""
        from transcriber.worker import TranscriptionWorker
        with patch("transcriber.worker.sf") as mock_sf, \
             patch.object(TranscriptionWorker, "load_model"):
            mock_audio = MagicMock()
            mock_audio.shape = (16000,)
            mock_sf.read.return_value = (mock_audio, 16000)

            w = TranscriptionWorker("/tmp/model")
            w.recognizer = MagicMock()

            w.transcribe("/tmp/test.wav")

            w.recognizer.create_stream.assert_called_once_with(hotwords=None)

    def test_transcribe_no_hotwords_when_empty_string(self):
        """Empty hotwords string is converted to None — sherpa-onnx's C++
        create_stream segfaults on Whisper models when hotwords is a non-None
        string (even ""), because it enters the contextual biasing code path
        which is only valid for transducer models."""
        from transcriber.worker import TranscriptionWorker
        with patch("transcriber.worker.sf") as mock_sf, \
             patch.object(TranscriptionWorker, "load_model"):
            mock_audio = MagicMock()
            mock_audio.shape = (16000,)
            mock_sf.read.return_value = (mock_audio, 16000)

            w = TranscriptionWorker("/tmp/model")
            w.recognizer = MagicMock()

            w.transcribe("/tmp/test.wav", hotwords="")

            w.recognizer.create_stream.assert_called_once_with(hotwords=None)


class TestTranscriberCorrectText:
    """Test _correct_text in transcriber/main.py applies lexicon correction."""

    def _make_lexicon(self, terms_dict):
        """Create a Lexicon instance without loading from file."""
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.2
        lex.terms = terms_dict
        return lex

    def test_correct_text_basic(self):
        """Words matching lexicon exactly are corrected to canonical form."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "theron": {"term": "Theron", "description": "A noble elf name"},
            "grimjaw": {"term": "Grimjaw", "description": "Dwarf name"},
        })

        result = _correct_text("theron said hello to grimjaw", lex)
        assert result == "Theron said hello to Grimjaw"

    def test_correct_text_no_match(self):
        """Words not in lexicon pass through unchanged."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({"theron": {"term": "Theron", "description": "elf"}})

        result = _correct_text("the dragon attacked the village", lex)
        assert result == "the dragon attacked the village"

    def test_correct_text_preserves_whitespace(self):
        """Whitespace and punctuation around words are preserved."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({"theron": {"term": "Theron", "description": "elf"}})

        result = _correct_text("  Theron  said: 'hello!'", lex)
        # Exact words are case-insensitive matched and corrected
        assert "Theron" in result

    def test_correct_text_empty_lexicon(self):
        """Empty lexicon returns text unchanged."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({})

        text = "theron said hello"
        result = _correct_text(text, lex)
        assert result == text

    def test_correct_text_none_lexicon(self):
        """No lexicon loaded (None) returns text unchanged."""
        from transcriber.main import _correct_text

        text = "theron said hello"
        result = _correct_text(text, None)
        assert result == text


class TestCorrectTextDictionaryGate:
    """Integration tests for the dictionary gate through _correct_text.

    The gate prevents common English words from being fuzzy-corrected
    to D&D lexicon terms, while still allowing correction of
    non-English words and exact lexicon matches.
    """

    def _make_lexicon(self, terms_dict):
        """Create a Lexicon instance without loading from file."""
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.5
        lex.terms = terms_dict
        return lex

    def test_english_words_not_corrected(self):
        """Common English words that fuzzy-match lexicon terms are left alone.

        'ore' is English and distance 1 from 'orc' — the dictionary
        gate must prevent the false-positive correction 'ore' → 'Orc'.
        """
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "orc": {"term": "Orc", "description": "Monster"},
            "theron": {"term": "Theron", "description": "Elf name"},
        })
        # "ore" is English → not corrected to "Orc"
        # "theron" is an exact lexicon match → corrected to "Theron"
        result = _correct_text("theron picked up some ore", lex)
        assert result == "Theron picked up some ore", f"Got: {result!r}"

    def test_misrecognised_non_english_corrected(self):
        """Non-English words close to lexicon terms are still corrected."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "theron": {"term": "Theron", "description": "Elf name"},
            "grimjaw": {"term": "Grimjaw", "description": "Dwarf name"},
        })
        # "theran" and "grimjaw" — "theran" is not English, fuzzy-corrected
        result = _correct_text("theran met grimjaw", lex)
        assert result == "Theron met Grimjaw", f"Got: {result!r}"

    def test_mixed_english_and_lexicon_words(self):
        """A sentence with English words and lexicon terms — only lexicon words corrected."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "theron": {"term": "Theron", "description": "Elf name"},
            "orc": {"term": "Orc", "description": "Monster"},
        })
        # "Theron" is an exact lexicon match → canonicalised
        # "the" is English → left alone
        # "orc" is an exact lexicon match → canonicalised
        # "with" is English → left alone
        # "sword" is English → left alone
        result = _correct_text("Theron the orc with a sword", lex)
        assert result == "Theron the Orc with a sword", f"Got: {result!r}"

    def test_no_false_positive_on_common_words(self):
        """Regression test: common English words that are 1 edit from lexicon terms."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "mae": {"term": "Mae", "description": "Character name"},
        })
        # "may" is English, distance 1 from "mae" — must NOT be corrected
        result = _correct_text("I may go to the tavern", lex)
        assert result == "I may go to the tavern", f"Got: {result!r}"


class TestCorrectTextTokenisation:
    """Tests for _correct_text tokenisation — Bug #14.

    D&D names often contain hyphens (e.g. "Grim-jaw") or apostrophes
    (e.g. "Smith'var"). The old regex \\b\\w+\\b split these into
    fragments that would never match lexicon terms. The new regex
    \\w+(?:['-]\\w+)* treats apostrophes and hyphens as intra-word
    characters so the full name is matched as a single token.
    """

    def _make_lexicon(self, terms_dict):
        """Create a Lexicon instance without loading from file."""
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.5
        lex.terms = terms_dict
        return lex

    def test_hyphenated_name_corrected(self):
        """Hyphenated D&D name is corrected as a single token."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "grim-jaw": {"term": "Grim-jaw", "description": "Dwarf fighter"},
        })
        # Exact match — should be canonicalised as a whole
        result = _correct_text("grim-jaw attacked the goblin", lex)
        assert result == "Grim-jaw attacked the goblin", f"Got: {result!r}"

    def test_apostrophe_name_corrected(self):
        """D&D name with apostrophe is corrected as a single token."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "smith'var": {"term": "Smith'var", "description": "Rogue"},
        })
        result = _correct_text("smith'var picked the lock", lex)
        assert result == "Smith'var picked the lock", f"Got: {result!r}"

    def test_o_brien_style_name(self):
        """O'Brien style name with apostrophe is tokenised correctly."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "o'brien": {"term": "O'Brien", "description": "NPC"},
        })
        result = _correct_text("o'brien said hello", lex)
        assert result == "O'Brien said hello", f"Got: {result!r}"

    def test_fuzzy_match_on_hyphenated_name(self):
        """Fuzzy match works on hyphenated names (not just exact)."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "grim-jaw": {"term": "Grim-jaw", "description": "Dwarf fighter"},
        })
        # "grimjaw" (no hyphen) is distance 1 from "grim-jaw" — should match
        # Note: Levenshtein counts the hyphen, so distance is 1 (insert hyphen)
        result = _correct_text("grimjaw attacked", lex)
        assert result == "Grim-jaw attacked", f"Got: {result!r}"

    def test_multiple_hyphens_in_name(self):
        """Names with multiple hyphens are handled correctly."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "three-fang-killer": {"term": "Three-Fang-Killer", "description": "Boss monster"},
        })
        result = _correct_text("three-fang-killer appeared", lex)
        assert result == "Three-Fang-Killer appeared", f"Got: {result!r}"

    def test_hyphenated_name_not_split_by_old_regex(self):
        """Regression test: the old \\b\\w+\\b would split 'grim-jaw' into
        'grim' and 'jaw' — neither matches 'grim-jaw' so no correction.
        The new regex matches the full token.
        """
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "grim-jaw": {"term": "Grim-jaw", "description": "Dwarf fighter"},
        })
        # With the old regex, "grim-jaw" → "grim" + "-" + "jaw"
        # Neither "grim" nor "jaw" matches "grim-jaw" → no correction
        # With the new regex, "grim-jaw" is one token → exact match → corrected
        result = _correct_text("The grim-jaw fought", lex)
        assert result == "The Grim-jaw fought", f"Got: {result!r}"

    def test_leading_trailing_apostrophe_not_matched(self):
        r"""Leading/trailing apostrophes are punctuation, not intra-word.

        "'tis" should tokenise as "'tis" → the "'tis" token is "'tis"
        which starts with an apostrophe. Our regex \w+(?:['-]\w+)* starts
        with \w, so "'tis" matches just "tis" (the leading ' is skipped).
        This is correct behaviour — leading apostrophes are punctuation.
        """
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "tis": {"term": "Tis", "description": "A test term"},
        })
        # "'tis" → regex matches "tis" (skips leading apostrophe)
        # "tis" is an exact lexicon match → corrected to "Tis"
        result = _correct_text("'tis a shame", lex)
        # The apostrophe stays as punctuation, "tis" → "Tis"
        assert result == "'Tis a shame", f"Got: {result!r}"

    def test_normal_words_unchanged_by_new_regex(self):
        """Normal words without hyphens or apostrophes still work correctly."""
        from transcriber.main import _correct_text

        lex = self._make_lexicon({
            "theron": {"term": "Theron", "description": "Elf name"},
        })
        result = _correct_text("theron said hello to the dragon", lex)
        assert result == "Theron said hello to the dragon", f"Got: {result!r}"


class TestBuildHotwords:
    """Test the hotwords builder used by the transcriber."""

    def test_builds_correct_format(self):
        """Hotwords are joined with forward slashes for sherpa-onnx."""
        terms = ["Theron", "Grimjaw", "Aboleth"]
        hotwords = build_hotwords(terms)
        assert hotwords == "Theron/Grimjaw/Aboleth"

    def test_empty_terms_returns_empty(self):
        """Empty list returns empty string."""
        assert build_hotwords([]) == ""

    def test_single_term(self):
        """Single term has no trailing slash."""
        assert build_hotwords(["Theron"]) == "Theron"

    def test_caps_at_100_terms(self):
        """Hotwords capped at 100 to stay within sherpa-onnx limits."""
        terms = [f"Term{i}" for i in range(150)]
        hotwords = build_hotwords(terms)
        # Should contain 100 terms, not 150
        parts = hotwords.split("/")
        assert len(parts) == 100
        assert parts[99] == "Term99"
        assert "Term100" not in hotwords
