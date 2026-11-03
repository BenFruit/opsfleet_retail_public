"""Storage of user memory entries; the rules for saving them live in UserMemory."""

import json
from uuid import uuid4


class MemoryCapacityError(RuntimeError):
    """The owner would exceed max_entries; the insert was rolled back."""


def _lock_owner(conn, owner) -> None:
    """Transaction-scoped lock on one owner's entries."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (owner,))


class MemoryRepository:
    """Injected transaction factory; no LLM, user/session or transport dependency."""

    def __init__(self, connection_factory):
        self.connection = connection_factory

    def list(self, owner, *, limit):
        """Oldest first, at most `limit` rows."""
        with self.connection() as conn:
            return conn.execute(
                "SELECT memory_id, content, normalized_key FROM user_memories "
                "WHERE owner_id=%s ORDER BY created_at, memory_id LIMIT %s",
                (owner, limit),
            ).fetchall()

    def add(self, owner, entries, *, max_entries, model, turn_id) -> int:
        """Insert entries whose key is new for the owner; returns how many were inserted.

        One transaction: if the owner would then have more than max_entries rows,
        MemoryCapacityError rolls everything back.
        """
        with self.connection() as conn:
            # Serialize writes per owner so concurrent requests cannot both pass
            # the capacity check. A hash collision only serializes extra users.
            _lock_owner(conn, owner)
            inserted = 0
            for item in entries:
                # UNIQUE (owner_id, normalized_key) makes an existing entry a no-op.
                inserted += conn.execute(
                    "INSERT INTO user_memories (memory_id, owner_id, content, normalized_key, embedding, embedding_model, source_turn_id) "
                    "VALUES (%s,%s,%s,%s,%s::vector,%s,%s) ON CONFLICT (owner_id, normalized_key) DO NOTHING",
                    (
                        uuid4().hex,
                        owner,
                        item["text"],
                        item["key"],
                        json.dumps(item["vector"], allow_nan=False),
                        model,
                        turn_id,
                    ),
                ).rowcount
            total = conn.execute("SELECT count(*) AS n FROM user_memories WHERE owner_id=%s", (owner,)).fetchone()["n"]
            if total > max_entries:
                raise MemoryCapacityError
            return inserted

    def delete(self, owner, memory_id=None) -> int:
        """Delete one entry, or all of the owner's entries when memory_id is None."""
        with self.connection() as conn:
            # Same lock as insertion keeps capacity accounting deterministic.
            _lock_owner(conn, owner)
            if memory_id is None:
                cursor = conn.execute("DELETE FROM user_memories WHERE owner_id=%s", (owner,))
            else:
                cursor = conn.execute(
                    "DELETE FROM user_memories WHERE owner_id=%s AND memory_id=%s", (owner, memory_id)
                )
            return cursor.rowcount
