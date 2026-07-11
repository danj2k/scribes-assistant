"""Tests for shared.lexicon — D&D terminology auto-correction."""

import pytest
from unittest.mock import patch
from shared.lexicon import Lexicon, build_hotwords


class TestLoadLexicon:
    """Tests for loading lexicon files."""

    def test_load_empty(self, tmp_path):
        """Loading an empty lexicon file returns an empty dict."""
        lex_file = tmp_path / "lexicon.yaml"
        lex_file.write_text("")
        lexicon = Lexicon(str(lex_file))
        assert len(lexicon.terms) == 0

    def test_load_populated(self, tmp_path):
        """Loading a populated lexicon returns the terms."""
        lex_file = tmp_path / "lexicon.yaml"
        lex_file.write_text("Theron: A noble elf name\nGrimjaw: Dwarf name\n")
        lexicon = Lexicon(str(lex_file))
        assert len(lexicon.terms) == 2
        assert "theron" in lexicon.terms

    def test_load_default_path(self):
        """Loading with no path uses the default lexicon location."""
        lexicon = Lexicon()
        assert isinstance(lexicon.terms, dict)


class TestSaveLexicon:
    """Tests for saving lexicon files."""

    def test_save_and_load(self, tmp_path):
        """Terms persist across save/load."""
        lex_file = tmp_path / "lexicon.yaml"
        lex1 = Lexicon(str(lex_file))
        lex1.add_term("TestTerm", "A test term")
        lex1.save()

        lex2 = Lexicon(str(lex_file))
        assert "testterm" in lex2.terms

    def test_save_overwrites(self, tmp_path):
        """Saving replaces the file contents."""
        lex_file = tmp_path / "lexicon.yaml"
        lex_file.write_text("Old: old term\n")
        lex1 = Lexicon(str(lex_file))
        lex1.terms.clear()
        lex1.add_term("New", "new term")
        lex1.save()

        lex2 = Lexicon(str(lex_file))
        assert len(lex2.terms) == 1  # only new term saved
        assert "new" in lex2.terms
        assert "old" not in lex2.terms


class TestAddRemoveTerm:
    """Tests for adding and removing terms."""

    def test_add_term(self, tmp_path):
        """A term can be added."""
        lex_file = tmp_path / "lexicon.yaml"
        lexicon = Lexicon(str(lex_file))
        lexicon.add_term("Theron", "Elf name")
        assert lexicon.correct("theron") == "Theron"

    def test_add_duplicate_term(self, tmp_path):
        """Adding a duplicate term overwrites the existing one."""
        lex_file = tmp_path / "lexicon.yaml"
        lexicon = Lexicon(str(lex_file))
        lexicon.add_term("Theron", "Elf name v1")
        lexicon.add_term("Theron", "Elf name v2")
        assert lexicon.terms["theron"]["description"] == "Elf name v2"

    def test_remove_term(self, tmp_path):
        """A term can be removed."""
        lex_file = tmp_path / "lexicon.yaml"
        lexicon = Lexicon(str(lex_file))
        lexicon.add_term("Theron", "Elf name")
        lexicon.remove_term("theron")
        assert lexicon.correct("theron") is None

    def test_remove_nonexistent(self, tmp_path):
        """Removing a nonexistent term does not raise."""
        lex_file = tmp_path / "lexicon.yaml"
        lexicon = Lexicon(str(lex_file))
        lexicon.remove_term("nobody")  # Should not raise


class TestBuildHotwords:
    """Tests for the sherpa-onnx hotwords builder."""

    def test_build_hotwords(self):
        """Hotwords are joined from a list of terms with forward slash."""
        hotwords = build_hotwords(["Theron", "Grimjaw"])
        assert hotwords == "Theron/Grimjaw"

    def test_build_hotwords_empty(self):
        """An empty list returns an empty string."""
        hotwords = build_hotwords([])
        assert hotwords == ""


class TestCorrectTieBreaking:
    def test_shorter_term_wins_on_tie(self):
        """When two candidates have equal edit distance, the shorter term is preferred."""
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.5
        lex.terms = {}
        # Both distance 1 from "firebal": "fireball" (len 8) vs "firebll" (len 7)
        # Insert longer term first so old code would pick it (first match wins)
        lex.terms["fireball"] = {"term": "fireball"}
        lex.terms["firebll"] = {"term": "firebll"}
        result = lex.correct("firebal")
        assert result == "firebll", f"Expected shorter term 'firebll', got {result!r}"

    def test_alphabetical_on_equal_distance_and_length(self):
        """When distance AND length are tied, the alphabetically earlier term wins."""
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = 0.5
        lex.terms = {}
        # Both distance 1 from "firebal", both length 8
        # Insert alphabetically-later term first: old code picks "firebalx", new picks "fireball"
        lex.terms["firebalx"] = {"term": "firebalx"}
        lex.terms["fireball"] = {"term": "fireball"}
        result = lex.correct("firebal")
        assert result == "fireball", f"Expected 'fireball' (alphabetical), got {result!r}"


class TestDictionaryGate:
    """Tests for the pyspellchecker English dictionary gate in correct().

    The gate prevents false positives where common English words are
    fuzzy-matched to D&D lexicon terms (e.g. "ore" → "Orc").
    """

    def _make_lexicon(self, terms_dict, threshold=0.5):
        """Create a Lexicon without loading from file."""
        lex = Lexicon.__new__(Lexicon)
        lex._fuzzy_threshold = threshold
        lex.terms = terms_dict
        return lex

    def test_english_word_not_corrected_to_fuzzy_lexicon_match(self):
        """The core fix: 'ore' is English, so it must NOT be corrected to 'Orc'."""
        lex = self._make_lexicon({"orc": {"term": "Orc"}})
        # "ore" is a common English word and distance 1 from "orc"
        result = lex.correct("ore")
        assert result is None, f"English word 'ore' should not be corrected, got {result!r}"

    def test_non_english_word_corrected_via_fuzzy_match(self):
        """A non-English word close to a lexicon term IS corrected."""
        lex = self._make_lexicon({"theron": {"term": "Theron"}})
        # "theran" is not English, distance 1 from "theron"
        result = lex.correct("theran")
        assert result == "Theron", f"Expected 'Theron', got {result!r}"

    def test_english_word_blocks_fuzzy_match_only(self):
        """An English word that fuzzy-matches a lexicon term is NOT corrected.

        The dictionary gate blocks fuzzy correction of English words.
        Exact matches still take priority (stage 1), but fuzzy matches
        are blocked. This prevents "ore" → "Orc" while allowing
        "theron" → "Theron" (exact match, bypasses gate).
        """
        lex = self._make_lexicon({"orc": {"term": "Orc"}})
        # "ore" is English, distance 1 from "orc" — gate blocks fuzzy match
        result = lex.correct("ore")
        assert result is None, f"English word 'ore' should not be fuzzy-corrected, got {result!r}"

    def test_exact_lexicon_match_bypasses_gate(self):
        """Exact lexicon match takes priority over the dictionary gate.

        Some lexicon terms happen to appear in English dictionaries
        (e.g. "theron" is a Greek name). The exact match catches
        correctly-transcribed terms for canonicalisation, bypassing
        the gate. The gate only blocks FUZZY matches.
        """
        lex = self._make_lexicon({"theron": {"term": "Theron"}})
        # "theron" is both English AND a lexicon term — exact match wins
        result = lex.correct("theron")
        assert result == "Theron", f"Exact match should bypass gate, got {result!r}"

    def test_may_not_corrected_to_mae(self):
        """Another false-positive scenario: 'may' → 'Mae' blocked by gate."""
        lex = self._make_lexicon({"mae": {"term": "Mae"}})
        # "may" is English, distance 1 from "mae"
        result = lex.correct("may")
        assert result is None, f"English word 'may' should not be corrected, got {result!r}"

    def test_gate_skips_numbers_and_punctuation(self):
        """Non-alphabetic input passes the gate (pyspellchecker ignores them)."""
        lex = self._make_lexicon({"theron": {"term": "Theron"}})
        # Numbers are not in the English dictionary, so they pass the gate
        # but won't match any lexicon term either
        result = lex.correct("12345")
        assert result is None

    def test_fuzzy_correction_still_respects_threshold(self):
        """Non-English words beyond the fuzzy threshold are not corrected."""
        lex = self._make_lexicon(
            {"theron": {"term": "Theron"}},
            threshold=0.2,  # 20% of 10 chars = max distance 2
        )
        # "xyzabc" is not English, but distance 6 from "theron" — too far
        result = lex.correct("xyzabc")
        assert result is None

    def test_gate_fallback_without_pyspellchecker(self):
        """When pyspellchecker is not installed, correction falls back to exact + fuzzy."""
        lex = self._make_lexicon({"orc": {"term": "Orc"}})
        # Simulate pyspellchecker not being available
        with patch("shared.lexicon._get_spell_checker", return_value=None):
            # Without the gate, "ore" (distance 1 from "orc") IS corrected
            result = lex.correct("ore")
            assert result == "Orc", f"Without gate, 'ore' should fuzzy-match to 'Orc', got {result!r}"

    def test_gate_fallback_exact_match_without_pyspellchecker(self):
        """Without pyspellchecker, exact lexicon match still works."""
        lex = self._make_lexicon({"theron": {"term": "Theron"}})
        with patch("shared.lexicon._get_spell_checker", return_value=None):
            result = lex.correct("theron")
            assert result == "Theron"
