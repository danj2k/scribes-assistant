"""Tests for shared.lexicon — D&D terminology auto-correction."""

import pytest
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
