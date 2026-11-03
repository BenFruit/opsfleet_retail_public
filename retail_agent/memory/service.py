"""Rules for explicit user memory: who may save, limits, duplicates, safe messages."""

import logging
import re

from retail_agent.memory.intent import explicit_remember, memory_key
from retail_agent.memory.models import MAX_ENTRIES, MAX_ENTRY_CHARS, RememberRequest, UserMemoryError
from retail_agent.memory.repository import MemoryCapacityError
from retail_agent.observations import ObservationStatus
from retail_agent.safety.sensitive_text import check_text

logger = logging.getLogger(__name__)

_LIMIT_REACHED = f"Memory limit reached ({MAX_ENTRIES} entries). Forget an old entry first"


class UserMemory:
    def __init__(self, repository, embedder, *, manager_id):
        self.repository = repository
        self.embedder = embedder
        self.manager_id = manager_id

    def load(self):
        try:
            # One row beyond the limit detects an over-limit store instead of dropping entries.
            rows = self.repository.list(self.manager_id, limit=MAX_ENTRIES + 1)
            if len(rows) > MAX_ENTRIES:
                raise UserMemoryError("Stored memory exceeds the MVP context limit.")
            result = []
            for row in rows:
                if not isinstance(row["content"], str) or not 1 <= len(row["content"]) <= MAX_ENTRY_CHARS:
                    raise UserMemoryError("Stored memory has an invalid format.")
                result.append({"memory_id": row["memory_id"], "text": check_text(row["content"])})
            return result
        except Exception:
            raise UserMemoryError(
                "User memory is unavailable. Analysis can continue without saved preferences."
            ) from None

    def remember(self, request: RememberRequest, *, question, turn_id):
        if not explicit_remember(question):
            raise UserMemoryError(
                "Persistent memory requires an explicit 'remember' request; one-off formatting is not saved."
            )
        try:
            known = {memory_key(item["text"]) for item in self.load()}
            entries = {}
            for text in request.memories:
                check_text(text)
                key = memory_key(text)
                if key not in known:
                    entries[key] = text
            # Early check without a lock; the repository repeats it atomically.
            if len(known) + len(entries) > MAX_ENTRIES:
                raise UserMemoryError(f"{_LIMIT_REACHED}.")
            # Obtain all embeddings before the single atomic database write.
            prepared = [
                {
                    "key": key,
                    "text": text,
                    "vector": self.embedder.embed(
                        text,
                        document=True,
                        title="User memory",
                        observation_name="memory_embedding",
                    ),
                }
                for key, text in entries.items()
            ]
            count = self._add(prepared, turn_id) if prepared else 0
            logger.info("user_memory_saved", extra={"row_count": count, "status": ObservationStatus.OK})
            return {
                "status": ObservationStatus.MEMORY,
                "saved": True,
                "count": count,
                "message": f"Memory saved: {count} new entries; existing exact duplicates were not added.",
            }
        except UserMemoryError:
            raise
        except Exception:
            raise UserMemoryError(
                "Memory was not confirmed saved. Analysis can continue; inspect /memories before retrying."
            ) from None

    def forget(self, memory_id=None):
        if memory_id is not None and not re.fullmatch(r"[0-9a-f]{32}", memory_id):
            raise UserMemoryError("Use /forget followed by a memory ID from /memories.")
        try:
            count = self.repository.delete(self.manager_id, memory_id)
            logger.info("user_memory_forgotten", extra={"row_count": count, "status": ObservationStatus.OK})
            return f"Forgot {count} memory entries."
        except Exception:
            raise UserMemoryError("Memory deletion was not confirmed. Inspect /memories before retrying.") from None

    def _add(self, prepared: list[dict], turn_id) -> int:
        try:
            return self.repository.add(
                self.manager_id, prepared, max_entries=MAX_ENTRIES, model=self.embedder.model, turn_id=turn_id
            )
        except MemoryCapacityError:
            # A concurrent save filled the library after the early check above.
            raise UserMemoryError(f"{_LIMIT_REACHED}; nothing new was saved.") from None
