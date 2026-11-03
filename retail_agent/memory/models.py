"""Limits and the model-facing schema of explicit user memory.

Memory is a small owner-scoped list of preferences the user explicitly asked to
remember. All entries are loaded into every question's context, so the limits
below bound prompt size; they are not a retrieval design (see HLD).
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_ENTRIES = 20  # Per owner; checked by the service and again under the DB lock.
MAX_ENTRY_CHARS = 200  # Also the user_memories.content VARCHAR(200) column limit.
MAX_ENTRIES_PER_REQUEST = 5  # One remember action may add at most this many.

# One entry as written by the model: surrounding whitespace removed, never empty.
MemoryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ENTRY_CHARS)]


# Payload of the model's `remember` action: only a list of 1-5 short texts.
# Extra fields (owner, IDs, flags) are rejected, so the model cannot choose whose
# memory it writes. Whether saving is allowed at all is decided by the service
# from the user's own words. No docstring: it would enter the model-facing schema.
class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memories: list[MemoryText] = Field(min_length=1, max_length=MAX_ENTRIES_PER_REQUEST)


class UserMemoryError(RuntimeError):
    """Controlled messages only; never expose database/provider exception text."""
