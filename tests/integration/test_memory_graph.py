import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.data.query_service import QueryLimits, QueryResult
from retail_agent.memory.models import RememberRequest, UserMemoryError
from retail_agent.memory.service import UserMemory
from support.demo_access import DEMO_ENTITLEMENTS

SQL = "SELECT SUM(oi.sale_price) AS revenue FROM `bigquery-public-data.thelook_ecommerce.order_items` oi"


def remember(text="Prefers bullet points."):
    return AgentAction(action="remember", remember=RememberRequest(memories=[text]))


class MemoryGraphTests(unittest.TestCase):
    def setUp(self):
        self.analyst = Mock()
        self.analyst.llm = SimpleNamespace(last_usage={})
        self.queries = Mock()
        self.queries.limits = QueryLimits()
        self.queries.execute.return_value = QueryResult("id", ({"revenue": 30},), False, 1, 1, 1)
        self.memory = Mock(spec=UserMemory)
        self.memory.load.return_value = []
        self.memory.remember.return_value = {
            "status": "memory",
            "saved": True,
            "count": 1,
            "message": "Memory saved: 1 new entry.",
        }
        self.agent = AnalyticalAgent(
            self.analyst, self.queries, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS, memory=self.memory
        )

    def test_both_orderings_complete_query_and_remember_in_one_turn(self):
        for ordering in ("remember_first", "query_first"):
            with self.subTest(ordering=ordering):
                self.setUp()
                actions = [remember(), AgentAction(action="query", sql=SQL)]
                if ordering == "query_first":
                    actions.reverse()
                self.analyst.decide.side_effect = [*actions, AgentAction(action="answer", answer="- Revenue is 30.")]
                result = self.agent.ask("Analyze revenue in bullet points, remember this format.")
                self.assertIn("Revenue is 30", result.answer)
                self.assertIn("Memory saved", result.answer)
                self.assertEqual(result.model_calls, 3)
                self.assertEqual(result.queries, 1)
                self.memory.remember.assert_called_once()

    def test_current_turn_and_next_question_receive_updated_memory(self):
        entries = [{"memory_id": "a" * 32, "text": "Prefers bullets."}]
        self.memory.load.side_effect = [[], entries, entries]
        self.analyst.decide.side_effect = [
            remember(),
            AgentAction(action="answer", answer="Noted."),
            AgentAction(action="answer", answer="- Hello."),
        ]
        self.agent.ask("Remember bullets")
        self.assertEqual(self.analyst.decide.call_args.kwargs["user_memory"]["items"], entries)
        self.agent.reset()
        self.agent.ask("What do you know about my preferences?")
        self.assertEqual(self.analyst.decide.call_args.kwargs["user_memory"]["items"], entries)
        self.memory.forget.assert_not_called()

    def test_failed_memory_does_not_prevent_analysis(self):
        self.memory.remember.side_effect = UserMemoryError("Memory was not saved.")
        self.analyst.decide.side_effect = [
            remember(),
            AgentAction(action="query", sql=SQL),
            AgentAction(action="answer", answer="Revenue is 30."),
        ]
        result = self.agent.ask("Analyze revenue and remember bullets")
        self.assertIn("Revenue is 30", result.answer)
        self.assertIn("not saved", result.answer)

    def test_unavailable_memory_does_not_crash_ordinary_analysis(self):
        self.memory.load.side_effect = UserMemoryError("Unavailable")
        self.analyst.decide.return_value = AgentAction(action="answer", answer="Available schema")
        self.assertEqual(self.agent.ask("What data exists?").answer, "Available schema")
        self.assertEqual(self.analyst.decide.call_args.kwargs["user_memory"]["status"], "unavailable")

    def test_repeated_remember_does_not_repeat_side_effects(self):
        self.analyst.decide.side_effect = [remember(), remember(), AgentAction(action="answer", answer="Done.")]
        self.agent.ask("Remember bullets")
        self.memory.remember.assert_called_once()

    def test_memory_commands_bypass_model_and_reset_does_not_forget(self):
        self.memory.forget.return_value = "Forgot 1 memory entries."
        self.assertEqual(self.agent.ask("/memories").answer, "No saved user memories.")
        self.agent.ask("/forget " + "a" * 32)
        self.memory.forget.assert_called_once_with("a" * 32)
        self.agent.reset()
        self.analyst.decide.assert_not_called()
