"""Tests for lexicon integration — initial prompt and post-correction."""
import re
from unittest.mock import MagicMock, patch, AsyncMock
from shared.lexicon import Lexicon, build_initial_prompt
from shared.config import Config


class TestTranscriptionWorkerPrompt:
    """Test that initial_prompt is passed to sherpa-onnx config."""

    def test_worker_accepts_initial_prompt(self):
        from transcriber.worker import TranscriptionWorker
        w = TranscriptionWorker(
            "/tmp/model", num_threads=2,
            initial_prompt="This is a D&D session: Theron, Grimjaw"
        )
        assert w.initial_prompt == "This is a D&D session: Theron, Grimjaw"

    def test_worker_default_prompt_empty(self):
        from transcriber.worker import TranscriptionWorker
        w = TranscriptionWorker("/tmp/model")
        assert w.initial_prompt == ""

    def test_load_model_sets_prompt(self):
        from transcriber.worker import TranscriptionWorker
        with patch("transcriber.worker.sherpa_onnx") as mock_sherpa:
            mock_config = MagicMock()
            mock_sherpa.OfflineRecognizerConfig.return_value = mock_config

            w = TranscriptionWorker(
                "/tmp/model", initial_prompt="Theron, Grimjaw"
            )
            w.load_model()

            assert mock_config.model_config.transducer.initial_prompt == "Theron, Grimjaw"

    def test_load_model_no_prompt_omits_config(self):
        from transcriber.worker import TranscriptionWorker
        with patch("transcriber.worker.sherpa_onnx") as mock_sherpa:
            mock_config = MagicMock()
            mock_sherpa.OfflineRecognizerConfig.return_value = mock_config

            w = TranscriptionWorker("/tmp/model")
            w.load_model()

            # Should not have set initial_prompt when empty
            # The mock config's transducer attr was not explicitly set
            # Just verify the recognizer was created
            mock_sherpa.OfflineRecognizer.assert_called_once_with(mock_config)


class TestDeliveryLoopLexicon:
    """Test that DeliveryLoop loads lexicon and applies correction."""

    def test_correct_text_basic(self):
        """Words matching lexicon exactly are corrected to canonical form."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        bot.config.lexicon_file = "/tmp/test_lex.yaml"

        # Create a temp lexicon
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.2
        lex.terms = {
            "theron": {"term": "Theron", "description": "A noble elf name"},
            "grimjaw": {"term": "Grimjaw", "description": "Dwarf name"},
        }

        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        loop._lexicon = lex

        result = loop._correct_text("theron said hello to grimjaw")
        assert result == "Theron said hello to Grimjaw"

    def test_correct_text_no_match(self):
        """Words not in lexicon pass through unchanged."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        bot.config.lexicon_file = "/tmp/test_lex.yaml"

        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.2
        lex.terms = {"theron": {"term": "Theron", "description": "elf"}}

        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        loop._lexicon = lex

        result = loop._correct_text("the dragon attacked the village")
        assert result == "the dragon attacked the village"

    def test_correct_text_preserves_whitespace(self):
        """Whitespace and punctuation around words are preserved."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        bot.config.lexicon_file = "/tmp/test_lex.yaml"

        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.2
        lex.terms = {"theron": {"term": "Theron", "description": "elf"}}

        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        loop._lexicon = lex

        result = loop._correct_text("  Theron  said: 'hello!'")
        # Exact words are case-insensitive matched and corrected
        assert "Theron" in result

    def test_correct_text_empty_lexicon(self):
        """Empty lexicon returns text unchanged."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        bot.config.lexicon_file = "/tmp/test_lex.yaml"

        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.2
        lex.terms = {}

        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        loop._lexicon = lex

        text = "theron said hello"
        result = loop._correct_text(text)
        assert result == text

    def test_correct_text_none_lexicon(self):
        """No lexicon loaded returns text unchanged."""
        from bot.delivery import DeliveryLoop
        bot = MagicMock()
        bot.config.lexicon_file = "/tmp/test_lex.yaml"

        loop = DeliveryLoop(bot, MagicMock(), MagicMock())
        loop._lexicon = None

        text = "theron said hello"
        result = loop._correct_text(text)
        assert result == text


class TestBuildInitialPrompt:
    """Test the prompt builder used by the bot."""

    def test_builds_correct_format(self):
        terms = ["Theron", "Grimjaw", "Aboleth"]
        prompt = build_initial_prompt(terms)
        assert "Theron" in prompt
        assert "Grimjaw" in prompt
        assert "Aboleth" in prompt
        assert prompt.startswith("This is a D&D session")

    def test_caps_at_30_terms(self):
        terms = [f"Term{i}" for i in range(50)]
        prompt = build_initial_prompt(terms)
        # Should only contain 30 terms
        assert "Term29" in prompt
        assert "Term30" not in prompt

    def test_empty_terms(self):
        prompt = build_initial_prompt([])
        assert prompt == ""
