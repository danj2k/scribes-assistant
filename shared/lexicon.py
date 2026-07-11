"""Lexicon helper — manages the YAML-based lexicon file and hotwords construction.

The lexicon is a map of (lowercase term -> {term, description}) pairs stored as
a simple "Term: Description" YAML-like file (one pair per line).  It's used to
build a hotwords string for the sherpa-onnx Whisper model so it better recognises
fantasy names, locations, and jargon during transcription.

A standalone Lexicon class handles load/save/add/remove/correct operations.
The module also exposes build_hotwords() for sherpa-onnx hotwords construction.
"""

from pathlib import Path

import yaml

try:
    import Levenshtein
except ImportError:
    Levenshtein = None

try:
    from spellchecker import SpellChecker
    _spell_checker = None  # type: ignore[var-annotated]
except ImportError:
    SpellChecker = None
    _spell_checker = None


def _get_spell_checker():
    """Return a lazily-initialised SpellChecker singleton, or None.

    pyspellchecker loads its bundled English dictionary from a JSON
    file on first use (~2 MB).  We defer this to the first correct()
    call rather than import time so that:

    - Environments that only use lexicon CRUD (the bot) pay no cost.
    - Import failures are handled gracefully — correction falls back
      to fuzzy-only when the package is not installed.
    """
    global _spell_checker
    if SpellChecker is not None and _spell_checker is None:
        _spell_checker = SpellChecker()
    return _spell_checker


class Lexicon:
    """In-memory lexicon with file persistence.

    File format (one term per line, colon-separated)::

        Theron: A noble elf name
        Grimjaw: Dwarf name

    ``terms`` is a dict keyed by the *lowercase* form of each term for
    case-insensitive lookups.
    """

    DEFAULT_PATH = "/data/lexicon.yaml"

    def __init__(self, file_path: str | None = None, fuzzy_threshold: float = 0.2):
        self._file_path = file_path or self.DEFAULT_PATH
        self._fuzzy_threshold = max(0.0, min(1.0, fuzzy_threshold))
        self.terms: dict[str, dict] = {}
        if Path(self._file_path).exists():
            self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        """Load terms from the YAML file (``Term: Description`` format)."""
        path = Path(self._file_path)
        if not path.exists():
            self.terms = {}
            return

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}

        if isinstance(data, dict):
            # Expecting {"Term": "Description", ...}
            self.terms = {}
            for term, desc in data.items():
                key = str(term).lower()
                self.terms[key] = {"term": str(term), "description": str(desc)}
        else:
            self.terms = {}

    def save(self) -> None:
        """Persist the current terms back to the YAML file."""
        path = Path(self._file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Build a flat dict for YAML output
        data = {}
        for entry in self.terms.values():
            data[entry["term"]] = entry["description"]

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    # -- term management ----------------------------------------------------

    def add_term(self, term: str, description: str) -> None:
        """Add or overwrite a term in the lexicon.

        Duplicate terms (case-insensitive) overwrite the previous entry.
        """
        key = term.lower()
        self.terms[key] = {"term": term, "description": description}

    def remove_term(self, term: str) -> None:
        """Remove a term by its name (case-insensitive).

        Silently ignores terms that don't exist.
        """
        key = term.lower()
        self.terms.pop(key, None)

    # -- fuzzy correction ---------------------------------------------------

    def correct(self, word: str) -> str | None:
        """Return the lexicon term closest to *word*, or ``None``.

        Uses a three-stage correction pipeline to avoid false positives
        on ordinary English words:

        1. **Exact lexicon match** — if *word* matches a lexicon term
           case-insensitively, return the canonical form immediately.
           This bypasses the dictionary gate because an exact match is
           a true positive, not a false positive — the correction is
           just canonicalisation (e.g. "theron" → "Theron"). Some
           lexicon terms happen to appear in English dictionaries
           (e.g. "theron" is a Greek name); blocking these would
           prevent proper capitalisation of correctly-transcribed terms.
        2. **English dictionary gate** — if *word* is a recognised
           English word (via pyspellchecker), return None. This
           prevents the fuzzy matcher from "correcting" common words
           like "ore" → "Orc" or "may" → "Mae".
        3. **Fuzzy lexicon match** — if *word* is neither an exact
           lexicon match nor an English word, compute Levenshtein
           distance to every lexicon term. If the best match is within
           the configured threshold, return it. This catches
           misrecognised D&D terms like "theran" → "Theron".

        Returns ``None`` when:
        - the lexicon is empty
        - the word is a recognised English word (dictionary gate)
        - no exact or fuzzy match found

        Falls back to exact + fuzzy matching only (no dictionary gate)
        when pyspellchecker is not installed.
        """
        if not self.terms:
            return None

        key = word.lower()

        # Stage 1: Exact case-insensitive lexicon match.
        # Takes priority over the dictionary gate — an exact match
        # is a true positive.  This catches words that are themselves
        # lexicon terms (e.g. "theron" → "Theron"), including terms
        # that happen to appear in English dictionaries.
        if key in self.terms:
            return self.terms[key]["term"]

        # Stage 2: English dictionary gate.
        # If the word is a recognised English word, it should NOT be
        # fuzzy-corrected to a D&D term — this prevents false positives
        # like "ore" → "Orc" or "may" → "Mae".
        spell = _get_spell_checker()
        if spell is not None and key in spell:
            return None

        # Stage 3: Fuzzy match via Levenshtein (optional dependency).
        # Only reached for words that are neither exact lexicon matches
        # nor English words — typically misrecognised D&D proper nouns.
        if Levenshtein is None:
            return None

        best_term = None
        best_distance = float("inf")
        for term_key, entry in self.terms.items():
            dist = Levenshtein.distance(key, term_key)
            if (best_term is None
                    or dist < best_distance
                    or (dist == best_distance
                        and len(entry["term"]) < len(best_term))
                    or (dist == best_distance
                        and len(entry["term"]) == len(best_term)
                        and entry["term"].lower() < best_term.lower())):
                best_distance = dist
                best_term = entry["term"]

        # Only correct when distance is within configured threshold proportion
        if best_distance <= max(int(len(key) * self._fuzzy_threshold), 1):
            return best_term
        return None

    # -- term list ----------------------------------------------------------

    def get_terms_list(self) -> list[str]:
        """Return just the term strings for hotwords injection."""
        return [entry["term"] for entry in self.terms.values()]


def build_hotwords(terms: list[str]) -> str:
    """Build a sherpa-onnx hotwords string from lexicon terms.

    Format: "Term1/Term2/Term3" — forward-slash separated list for
    sherpa-onnx's create_stream(hotwords=...).  Hotwords are a hard
    decoding bias (stronger than the old soft initial_prompt).

    Capped at 100 terms to stay within sherpa-onnx token limits.
    """
    if not terms:
        return ""

    # Cap to prevent token limit issues (100 hotwords limit in sherpa-onnx)
    return "/".join(terms[:100])
