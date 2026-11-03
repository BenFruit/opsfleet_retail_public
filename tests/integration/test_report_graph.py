import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from retail_agent.agent.models import AgentAction, AgentLimits
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.data.query_service import QueryLimits
from retail_agent.llm.embeddings import EmbeddingError
from retail_agent.reports.models import ReportError, ReportRequest
from retail_agent.reports.repository import ReportRepository
from retail_agent.reports.service import ReportLibrary
from support.demo_access import DEMO_ENTITLEMENTS

VECTOR = [1.0] + [0.0] * 767
ID = "a" * 32


def row(report_id=ID, **extra):
    return {
        "report_id": report_id,
        "title": "Brand A report",
        "conversation_id": "c" * 32,
        "created_at": datetime.now(UTC),
        "content": "Checked report text",
        **extra,
    }


class ReportGraphTests(unittest.TestCase):
    def setUp(self):
        self.analyst = Mock()
        self.analyst.llm = SimpleNamespace(last_usage={})
        self.queries = Mock()
        self.queries.limits = QueryLimits()
        self.repo = Mock(spec=ReportRepository)
        self.embedder = Mock(model="gemini-embedding-2")
        self.embedder.embed.return_value = VECTOR
        self.library = ReportLibrary(self.repo, self.embedder, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS)
        self.agent = AnalyticalAgent(
            self.analyst, self.queries, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS, reports=self.library
        )

    def test_save_preserves_last_answer_across_history_trimming_and_report_messages(self):
        self.analyst.decide.return_value = AgentAction(action="answer", answer="Checked narrative")
        self.agent.ask("Analyze")
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="save", query="")
        )
        self.agent.session.history.clear()
        self.agent.ask("Save")
        self.agent.ask("Save again")
        self.assertEqual(self.repo.save.call_args.kwargs["content"], "Checked narrative")
        self.assertEqual(self.repo.save.call_count, 2)
        self.queries.execute.assert_not_called()

    def test_invalid_save_action_is_repaired_twice_without_duplicate_writes(self):
        self.agent.session.last_answer = "Original checked answer"
        self.agent.session.last_answer_brands = ["Brand A"]
        try:
            AgentAction.model_validate_json('{"action":"report","report":{"operation":"save"}}')
        except ValueError as error:
            invalid = error
        self.analyst.decide.side_effect = [
            invalid,
            invalid,
            AgentAction(action="report", report=ReportRequest(operation="save", query="")),
        ]
        result = self.agent.ask("Save this")
        self.assertIn("Saved report", result.answer)
        self.repo.save.assert_called_once()
        self.assertEqual(result.model_calls, 3)

    def test_confirm_bypasses_model_and_natural_yes_does_not_delete(self):
        self.repo.preview_delete.return_value = [row()]
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="prepare_delete", scope="all", query="")
        )
        self.assertIn("/confirm", self.agent.ask("Delete all my reports").answer)
        self.repo.delete.assert_not_called()
        self.analyst.decide.return_value = AgentAction(action="answer", answer="Please request a new preview.")
        self.agent.ask("yes")
        self.repo.delete.assert_not_called()
        self.assertIn("No active", self.agent.ask("/confirm").answer)
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="prepare_delete", scope="all", query="")
        )
        self.agent.ask("Delete all my reports")
        calls = self.analyst.decide.call_count
        self.repo.delete.return_value = 1
        self.assertIn("Deleted 1", self.agent.ask("/confirm").answer)
        self.assertEqual(self.analyst.decide.call_count, calls)

    def test_reset_changes_conversation_and_clears_snapshot_and_confirmation(self):
        previous = self.agent.session.conversation_id
        self.agent.session.last_answer = "Old"
        self.repo.preview_delete.return_value = [row()]
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="prepare_delete", scope="conversation", query="")
        )
        self.agent.ask("Delete conversation reports")
        self.agent.reset()
        self.assertNotEqual(previous, self.agent.session.conversation_id)
        self.assertIsNone(self.agent.session.last_answer)
        self.assertIsNone(self.library.pending)

    def test_report_search_is_model_observation_with_deterministic_sources(self):
        self.repo.search.return_value = [row(similarity=0.9)]
        self.analyst.decide.side_effect = [
            AgentAction(action="report", report=ReportRequest(operation="search", query="Brand A")),
            AgentAction(action="answer", answer="The report discusses Brand A."),
        ]
        result = self.agent.ask("Find reports about Brand A")
        self.assertIn(ID, result.answer)
        self.assertEqual(result.queries, 0)
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(self.analyst.decide.call_args.kwargs["observations"][0]["operation"], "search")

    def test_report_search_budget_stops_loop(self):
        self.agent.limits = AgentLimits(max_report_calls=1)
        self.repo.search.return_value = [row(similarity=0.9)]
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="search", query="Brand A")
        )
        self.assertIn("budget", self.agent.ask("Find reports").answer)
        self.repo.search.assert_called_once()

    def test_empty_search_returns_no_reports_without_another_model_call(self):
        self.repo.search.return_value = []
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="search", query="Nike")
        )
        turn = self.agent.ask("Find my reports about Nike")
        self.assertEqual(turn.answer, "No reports found.")
        self.assertEqual(turn.model_calls, 1)

    def test_db_failure_is_not_reported_as_success_and_does_not_overwrite_snapshot(self):
        self.agent.session.last_answer = "Original"
        self.agent.session.last_answer_brands = ["Brand A"]
        self.repo.save.side_effect = ReportError("Report storage is unavailable.")
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="save", query="")
        )
        self.assertIn("unavailable", self.agent.ask("Save").answer)
        self.assertEqual(self.agent.session.last_answer, "Original")

    def test_embedding_failure_is_a_controlled_answer(self):
        self.agent.session.last_answer = "Original"
        self.agent.session.last_answer_brands = ["Brand A"]
        self.embedder.embed.side_effect = EmbeddingError("Embedding service is unavailable.")
        self.analyst.decide.return_value = AgentAction(
            action="report", report=ReportRequest(operation="save", query="")
        )
        self.assertIn("Embedding service is unavailable", self.agent.ask("Save").answer)
        self.repo.save.assert_not_called()
