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
        """create_stream called with empty hotwords string."""
        from transcriber.worker import TranscriptionWorker
        with patch("transcriber.worker.sf") as mock_sf, \
             patch.object(TranscriptionWorker, "load_model"):
            mock_audio = MagicMock()
            mock_audio.shape = (16000,)
            mock_sf.read.return_value = (mock_audio, 16000)

            w = TranscriptionWorker("/tmp/model")
            w.recognizer = MagicMock()

            w.transcribe("/tmp/test.wav", hotwords="")

            w.recognizer.create_stream.assert_called_once_with(hotwords="")


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


class TestDeliveryLoopNoLexicon:
    """Verify DeliveryLoop no longer imports or uses Lexicon for correction.

    Bug #13: correction moved from bot/delivery.py to transcriber/main.py.
    The bot's delivery loop just reads the already-corrected transcript
    and posts it to Discord — it does not apply lexicon correction.
    """

    def test_delivery_loop_has_no_lexicon_attribute(self):
        """DeliveryLoop should not have a _lexicon attribute."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        assert not hasattr(loop, "_lexicon")

    def test_delivery_loop_has_no_correct_text_method(self):
        """DeliveryLoop should not have a _correct_text method."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        assert not hasattr(loop, "_correct_text")

    def test_delivery_loop_does_not_import_lexicon(self):
        """bot.delivery module should not import Lexicon."""
        import bot.delivery as delivery_mod
        assert not hasattr(delivery_mod, "Lexicon")


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
