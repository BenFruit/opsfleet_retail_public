import re
import unittest
from unittest.mock import Mock
from uuid import uuid4

from retail_agent.agent.models import AgentAction
from retail_agent.memory.intent import explicit_remember
from retail_agent.memory.models import MAX_ENTRIES, MAX_ENTRY_CHARS, RememberRequest, UserMemoryError
from retail_agent.memory.repository import MemoryCapacityError, MemoryRepository
from retail_agent.memory.service import UserMemory
from support.paths import REPO_ROOT

VECTOR = [1.0] + [0.0] * 767


class MemoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock(spec=MemoryRepository)
        self.repo.list.return_value = []
        self.repo.add.return_value = 1
        self.embedder = Mock()
        self.embedder.model = "gemini-embedding-2"
        self.embedder.embed.return_value = VECTOR
        self.memory = UserMemory(self.repo, self.embedder, manager_id="manager_a")

    def test_explicit_intent_not_one_off_or_negated(self):
        for text in ("Use bullets, remember this.", "Please remember: I prefer lists."):
            self.assertTrue(explicit_remember(text))
        for text in (
            "This report in bullets.",
            "Do not remember this.",
            "Don't remember my name.",
            "Never remember this.",
        ):
            self.assertFalse(explicit_remember(text))
        with self.assertRaises(UserMemoryError):
            self.memory.remember(
                RememberRequest(memories=["Prefers bullets."]), question="This report in bullets", turn_id="a" * 32
            )
        self.repo.add.assert_not_called()

    def test_embedding_per_new_string_and_owner_is_code_supplied(self):
        request = RememberRequest(memories=["Prefers bullet points.", "The analyst's name is Yerassyl."])
        self.memory.remember(request, question="Remember my name and preferred format", turn_id="a" * 32)
        self.assertEqual(self.embedder.embed.call_count, 2)
        self.assertEqual(self.repo.add.call_args.args[0], "manager_a")
        self.assertEqual(len(self.repo.add.call_args.args[1]), 2)
        self.assertEqual(self.repo.add.call_args.kwargs["model"], "gemini-embedding-2")
        self.assertEqual(self.repo.add.call_args.kwargs["max_entries"], MAX_ENTRIES)

    def test_exact_duplicates_are_not_embedded_or_saved_twice(self):
        self.repo.list.return_value = [{"memory_id": "a" * 32, "content": "Prefers bullet points."}]
        result = self.memory.remember(
            RememberRequest(memories=["  PREFERS   bullet points. "]),
            question="Remember my formatting",
            turn_id="a" * 32,
        )
        self.assertTrue(result["saved"])
        self.assertEqual(result["count"], 0)
        self.embedder.embed.assert_not_called()
        self.repo.add.assert_not_called()

    def test_duplicate_entries_in_one_request_have_one_embedding(self):
        self.memory.remember(
            RememberRequest(memories=["Prefers bullets.", "prefers bullets."]),
            question="Remember this",
            turn_id="a" * 32,
        )
        self.embedder.embed.assert_called_once()

    def test_all_embeddings_must_succeed_before_atomic_write(self):
        self.embedder.embed.side_effect = [VECTOR, RuntimeError("SECRET provider error")]
        with self.assertRaises(UserMemoryError) as error:
            self.memory.remember(
                RememberRequest(memories=["Prefers bullets.", "Prefers English."]),
                question="Remember these preferences",
                turn_id="a" * 32,
            )
        self.assertNotIn("SECRET", str(error.exception))
        self.repo.add.assert_not_called()

    def test_memory_cap_and_text_bounds(self):
        self.repo.list.return_value = [
            {"memory_id": uuidv, "content": f"Preference {i}"}
            for i, uuidv in enumerate([uuid4().hex for _ in range(20)])
        ]
        with self.assertRaises(UserMemoryError):
            self.memory.remember(
                RememberRequest(memories=["New preference"]), question="Remember this", turn_id="a" * 32
            )
        self.embedder.embed.assert_not_called()
        for items in ([], [""], ["x" * 201], ["x"] * 6):
            with self.subTest(items=len(items)), self.assertRaises(ValueError):
                RememberRequest(memories=items)

    def test_contact_details_are_not_embedded(self):
        with self.assertRaises(UserMemoryError):
            self.memory.remember(
                RememberRequest(memories=["Email: secret@example.com"]), question="Remember this", turn_id="a" * 32
            )
        self.embedder.embed.assert_not_called()

    def test_read_and_forget_are_owner_scoped(self):
        self.memory.load()
        self.repo.list.assert_called_once_with("manager_a", limit=MAX_ENTRIES + 1)
        self.repo.delete.return_value = 1
        self.memory.forget("b" * 32)
        self.repo.delete.assert_called_once_with("manager_a", "b" * 32)
        with self.assertRaises(UserMemoryError):
            self.memory.forget("arbitrary SQL")

    def test_action_cannot_smuggle_other_tools_or_owner(self):
        for payload in (
            {"action": "remember", "remember": {"memories": ["Prefers bullets."], "owner_id": "other"}},
            {"action": "query", "sql": "SELECT 1", "remember": {"memories": ["Prefers bullets."]}},
            {"action": "remember", "sql": "SELECT 1", "remember": {"memories": ["Prefers bullets."]}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                AgentAction.model_validate(payload)


class MemoryLimitTests(unittest.TestCase):
    def test_capacity_error_from_storage_becomes_the_limit_message(self):
        repo = Mock()
        repo.list.return_value = []
        repo.add.side_effect = MemoryCapacityError
        embedder = Mock(model="gemini-embedding-2")
        embedder.embed.return_value = [1.0] + [0.0] * 767
        memory = UserMemory(repo, embedder, manager_id="manager_a")
        with self.assertRaises(UserMemoryError) as caught:
            memory.remember(RememberRequest(memories=["Prefers bullets."]), question="Remember: bullets", turn_id="t")
        self.assertEqual(
            str(caught.exception),
            f"Memory limit reached ({MAX_ENTRIES} entries). Forget an old entry first; nothing new was saved.",
        )

    def test_entry_length_matches_the_database_column(self):
        migration = next((REPO_ROOT / "docker/reports-postgres").glob("003_*.sql"))
        column = re.search(r"content VARCHAR\((\d+)\)", migration.read_text(encoding="utf-8"))
        self.assertEqual(int(column.group(1)), MAX_ENTRY_CHARS)
