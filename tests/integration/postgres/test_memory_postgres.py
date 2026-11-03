import os
import unittest
from unittest.mock import Mock
from uuid import uuid4

from retail_agent.memory.intent import memory_key
from retail_agent.memory.models import MAX_ENTRIES
from retail_agent.memory.repository import MemoryCapacityError, MemoryRepository
from retail_agent.memory.service import UserMemory
from retail_agent.settings import Settings
from retail_agent.storage.postgres import connection_factory

VECTOR = [1.0] + [0.0] * 767


def entry(text):
    return {"text": text, "key": memory_key(text), "vector": VECTOR}


@unittest.skipUnless(os.getenv("RETAIL_TEST_POSTGRES") == "1", "Opt-in local PostgreSQL memory test")
class MemoryPostgresTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository(connection_factory(Settings()))
        self.owner, self.other = "memory_test_" + uuid4().hex, "memory_test_" + uuid4().hex
        self.addCleanup(self.repo.delete, self.other)
        self.addCleanup(self.repo.delete, self.owner)

    def add(self, owner, entries, max_entries=MAX_ENTRIES):
        return self.repo.add(owner, entries, max_entries=max_entries, model="gemini-embedding-2", turn_id=uuid4().hex)

    def rows(self, owner):
        return self.repo.list(owner, limit=MAX_ENTRIES + 1)

    def test_embeddings_exact_duplicates_owner_isolation_and_forget(self):
        repo, owner, other = self.repo, self.owner, self.other
        self.assertEqual(self.add(owner, [entry("Prefers bullets.")]), 1)
        self.assertEqual(self.add(owner, [entry("Prefers bullets.")]), 0)
        self.assertEqual(self.add(other, [entry("Prefers bullets.")]), 1)
        own = self.rows(owner)
        self.assertEqual(len(own), 1)
        restarted = UserMemory(MemoryRepository(connection_factory(Settings())), Mock(), manager_id=owner)
        self.assertEqual(restarted.load()[0]["text"], "Prefers bullets.")
        self.assertEqual(repo.delete(other, own[0]["memory_id"]), 0)
        with repo.connection() as conn:
            size = conn.execute(
                "SELECT vector_dims(embedding) AS dimensions FROM user_memories WHERE owner_id=%s", (owner,)
            ).fetchone()
            self.assertEqual(size["dimensions"], 768)
        self.assertEqual(repo.delete(owner, own[0]["memory_id"]), 1)
        self.assertEqual(len(self.rows(other)), 1)

    def test_capacity_overflow_rolls_back_the_whole_insert(self):
        self.assertEqual(self.add(self.owner, [entry("One."), entry("Two.")], max_entries=3), 2)
        with self.assertRaises(MemoryCapacityError):
            self.add(self.owner, [entry("Three."), entry("Four.")], max_entries=3)
        self.assertEqual(sorted(row["content"] for row in self.rows(self.owner)), ["One.", "Two."])
        # Duplicates do not count towards the limit.
        self.assertEqual(self.add(self.owner, [entry("One."), entry("Three.")], max_entries=3), 1)


if __name__ == "__main__":
    unittest.main()
