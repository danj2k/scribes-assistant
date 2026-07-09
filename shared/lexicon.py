"""Lexicon helper — manages the YAML-based lexicon file and initial_prompt building.

The lexicon is a map of (lowercase term -> {term, description}) pairs stored as
a simple "Term: Description" YAML-like file (one pair per line).  It's used to
build an initial_prompt hint for the Whisper model so it better recognises
fantasy names, locations, and jargon.

A standalone Lexicon class handles load/save/add/remove/correct operations.
The module also exposes build_initial_prompt() for Whisper prompt construction.
"""

from pathlib import Path

import yaml

try:
    import Levenshtein
except ImportError:
    Levenshtein = None


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

        Uses Levenshtein distance when available.  Falls back to
        case-insensitive exact matching when Levenshtein is not installed.
        Returns ``None`` when:
        - the lexicon is empty
        - no exact or fuzzy match found
        """
        if not self.terms:
            return None

        key = word.lower()
        # Exact case-insensitive match (always available)
        if key in self.terms:
            return self.terms[key]["term"]

        # Fuzzy match via Levenshtein (optional dependency)
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

    # -- Whisper prompt construction ----------------------------------------

    def get_terms_list(self) -> list[str]:
        """Return just the term strings for Whisper prompt injection."""
        return [entry["term"] for entry in self.terms.values()]


def build_initial_prompt(terms: list[str]) -> str:
    """Build a Whisper initial_prompt from lexicon terms.

    Format: "This is a D&D session with fantasy terminology: Term1, Term2, ..."
    The prompt is a plain-text hint, not a hard dictionary.  Kept within
    token limits by capping at 30 terms.
    """
    if not terms:
        return ""

    # Cap to prevent token limit issues
    capped = terms[:30]
    term_list = ", ".join(capped)

    return f"This is a D&D session with fantasy terminology: {term_list}"
