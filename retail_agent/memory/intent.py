"""Text rules for explicit user memory: when saving is allowed and what counts as a duplicate."""

import re
import unicodedata
from hashlib import sha256

_NEGATED = re.compile(r"\b(?:do not|don't|never)\s+remember\b", re.I)
_EXPLICIT = re.compile(r"\bremember\b", re.I)


def explicit_remember(question: str) -> bool:
    """Whether the user's own words ask to remember something.

    Conservative MVP intent gate in addition to LLM classification. One-off
    formatting alone cannot trigger persistence; quoted/indirect intent still
    needs semantic evaluation (documented, not a security proof).
    """
    return not _NEGATED.search(question) and bool(_EXPLICIT.search(question))


def memory_key(text: str) -> str:
    """Duplicate key: Unicode-normalized, case-folded, whitespace-collapsed, hashed."""
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return sha256(normalized.encode("utf-8")).hexdigest()
