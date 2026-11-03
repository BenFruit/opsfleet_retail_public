import unittest
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from retail_agent.llm.embeddings import Embedder, EmbeddingError
from retail_agent.presentation import render_reports
from retail_agent.reports.models import ReportError, ReportRequest
from retail_agent.reports.repository import ReportRepository
from retail_agent.reports.service import ReportLibrary
from retail_agent.safety.errors import ResultGuardError
from retail_agent.security.access import Entitlements
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


class ReportPolicyTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock(spec=ReportRepository)
        self.embedder = Mock(spec=Embedder)
        self.embedder.model = "gemini-embedding-2"
        self.embedder.embed.return_value = VECTOR
        self.library = ReportLibrary(self.repo, self.embedder, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS)

    def execute(self, **request):
        return self.library.execute(
            ReportRequest(**{"query": "", **request}),
            conversation_id="c" * 32,
            last_answer="Original answer\n| revenue |\n| --- |\n| 30 |",
            last_brands=["Brand A", "Brand B"],
        )

    def test_save_uses_snapshot_and_trusted_identity(self):
        result = self.execute(operation="save", title="October")
        values = self.repo.save.call_args.kwargs
        self.assertEqual(values["owner"], "manager_a")
        self.assertEqual(values["brands"], ["Brand A", "Brand B"])
        self.assertIn("| 30 |", values["content"])
        self.assertEqual(values["report_id"], result["report_id"])
        self.assertEqual(len(result["report_id"]), 32)

    def test_missing_answer_never_calls_embedding_or_db(self):
        with self.assertRaises(ReportError):
            self.library.execute(
                ReportRequest(operation="save", query=""), conversation_id="c" * 32, last_answer=None, last_brands=[]
            )
        self.embedder.embed.assert_not_called()
        self.repo.save.assert_not_called()

    def test_changed_brands_prevent_saving_old_answer(self):
        with (
            patch.object(self.library, "entitlements", Entitlements({"manager_a": ["Brand A"]})),
            self.assertRaises(ReportError),
        ):
            self.execute(operation="save")
        self.repo.save.assert_not_called()

    def test_embedding_failure_never_saves(self):
        self.embedder.embed.side_effect = EmbeddingError("Embedding unavailable")
        with self.assertRaises(EmbeddingError):
            self.execute(operation="save")
        self.repo.save.assert_not_called()

    def test_sensitive_text_not_sent_to_embedding(self):
        with self.assertRaises(ResultGuardError):
            self.library.execute(
                ReportRequest(operation="save", query=""),
                conversation_id="c" * 32,
                last_answer="secret@example.com",
                last_brands=["Brand A"],
            )
        self.embedder.embed.assert_not_called()

    def test_search_is_bounded_and_access_scoped(self):
        self.repo.search.return_value = [row(similarity=0.8, content="x" * 8000) for _ in range(10)]
        result = self.execute(operation="search", query="Brand A")
        self.assertLess(len(result["reports"]), 10)
        self.assertLessEqual(len(result["reports"][0]["excerpt"]), 1500)
        self.assertEqual(self.repo.search.call_args.kwargs["owner"], "manager_a")
        self.assertEqual(self.repo.search.call_args.kwargs["brands"], ["Brand A", "Brand B"])

    def test_open_inaccessible_has_no_content(self):
        self.repo.open.return_value = None
        with self.assertRaisesRegex(ReportError, "not found or not accessible"):
            self.execute(operation="open", report_id=ID)

    def test_deletion_is_preview_then_fixed_ids_confirmed_once(self):
        self.repo.preview_delete.return_value = [row()]
        result = self.execute(operation="prepare_delete", scope="conversation")
        self.assertEqual(result["operation"], "prepare_delete")
        self.repo.delete.assert_not_called()
        self.repo.delete.return_value = 1
        self.assertIn("Deleted 1", self.library.confirm())
        self.repo.delete.assert_called_once_with((ID,), owner="manager_a", brands=["Brand A", "Brand B"])
        self.assertIn("No active", self.library.confirm())
        self.repo.preview_delete.assert_called_once()

    def test_delete_rechecks_current_access(self):
        self.repo.preview_delete.return_value = [row()]
        self.execute(operation="prepare_delete", scope="all")
        with patch.object(self.library, "entitlements", Entitlements({"manager_a": ["Brand A"]})):
            self.library.confirm()
        self.assertEqual(self.repo.delete.call_args.kwargs["brands"], ["Brand A"])

    def test_cancel_and_expiry_never_delete(self):
        self.repo.preview_delete.return_value = [row()]
        self.execute(operation="prepare_delete", scope="all")
        self.library.cancel()
        self.library.confirm()
        self.execute(operation="prepare_delete", scope="all")
        with patch("retail_agent.reports.service.monotonic", return_value=float("inf")):
            self.library.confirm()
        self.repo.delete.assert_not_called()

    def test_oversized_preview_is_not_partially_deletable(self):
        self.repo.preview_delete.return_value = [row() for _ in range(101)]
        with self.assertRaises(ReportError):
            self.execute(operation="prepare_delete", scope="all")
        self.assertIsNone(self.library.pending)

    def test_no_unrestricted_request_fields_or_delete_action(self):
        cases = [
            {"operation": "delete"},
            {"operation": "save", "owner_id": "manager_b"},
            {"operation": "save", "content": "replacement"},
            {"operation": "search", "query": ""},
            {"operation": "prepare_delete"},
            {"operation": "prepare_delete", "scope": "id"},
            {"operation": "open", "report_id": "x; DROP TABLE saved_reports"},
            {"operation": "list", "query": "ignored filter"},
        ]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(ValueError):
                ReportRequest(**{"query": "", **request})

    def test_structured_output_schema_requires_query_even_for_delete_preview(self):
        self.assertIn("query", ReportRequest.model_json_schema()["required"])

    def test_empty_library_does_not_mention_deletion(self):
        for operation in ("list", "search"):
            self.assertEqual(render_reports({"operation": operation, "reports": []}), "No reports found.")
        self.assertIn("Nothing was deleted", render_reports({"operation": "prepare_delete", "reports": []}))
