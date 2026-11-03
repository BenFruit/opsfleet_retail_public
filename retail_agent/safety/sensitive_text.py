"""Pattern check for contact details in any text that leaves the application.

Used for questions, answers, reports, memory, Golden trios, embeddings and query
results. Supplemental patterns, NOT comprehensive name/address/PII detection.
"""

import re

from retail_agent.safety.errors import ResultGuardError

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_CANDIDATE = re.compile(r"(?<!\w)\+\d[\d ()-]{6,}\d(?!\w)")
# Analytical prose such as "+12 (2023-09-01 - 2023-09-30)" or "+3 (2024 - 2023)"
# is not a phone number: exclude ISO dates and year ranges from candidates.
_DATE_LIKE = re.compile(
    r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?"
    r"|(?:19|20)\d{2}\s*-\s*(?:19|20)\d{2}"
)


def _looks_like_phone(candidate: str) -> bool:
    # E.164 numbers have at most 15 digits; real numbers have at least 10.
    digits = sum(character.isdigit() for character in candidate)
    return 10 <= digits <= 15 and not _DATE_LIKE.search(candidate)


def check_text(text: str) -> str:
    """Return text unchanged, or raise ResultGuardError if it looks like an email or phone."""
    if _EMAIL.search(text) or any(_looks_like_phone(match.group()) for match in _PHONE_CANDIDATE.finditer(text)):
        raise ResultGuardError("Text contains a potentially sensitive value")
    return text
