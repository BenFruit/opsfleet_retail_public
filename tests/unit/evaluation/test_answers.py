import json
import unittest
from unittest.mock import Mock

from retail_agent.data.query_service import QueryResult, QueryServiceError
from retail_agent.evaluation.answers import (
    DEFAULT_CASES,
    PARTIAL_MARKER,
    AnswerCase,
    JudgeVerdict,
    RecordingQueryService,
    check_case,
    summarize,
)
from retail_agent.sql.scope import apply_brand_scope
from support.paths import REPO_ROOT

ALLOWED = {"Brand A", "Brand B"}


def case(**fields):
    return AnswerCase.model_validate({"id": "c", "category": "analytics", "question": "q", **fields})


def ok_call(sql="SELECT 1", rows=()):
    return {"sql": sql, "status": "ok", "rows": list(rows), "columns": list(rows[0]) if rows else []}


def failed(results):
    return [r["name"] for r in results if not r["passed"]]


class AnswerCaseFileTests(unittest.TestCase):
    def test_cases_cover_categories_and_reference_sql_passes_policy(self):
        cases = [
            AnswerCase.model_validate(c) for c in json.loads((REPO_ROOT / DEFAULT_CASES).read_text(encoding="utf-8"))
        ]
        self.assertEqual({c.category for c in cases}, {"analytics", "report", "safety", "ux"})
        self.assertEqual(len({c.id for c in cases}), len(cases))
        for c in cases:
            for ref in c.references:
                apply_brand_scope(ref.sql)  # References run under the same scope as the agent.


class DeterministicCheckTests(unittest.TestCase):
    def test_reference_numbers_match_with_relative_tolerance(self):
        c = case(expect_query=True, references=[{"name": "rev", "sql": "x", "column": "revenue"}])
        calls = [ok_call(rows=[{"revenue": "12,345.60"}])]
        self.assertEqual(failed(check_case(c, "Revenue", calls, {"rev": 12345.0}, ALLOWED)), [])
        self.assertIn("reference:rev", failed(check_case(c, "Revenue", calls, {"rev": 13000.0}, ALLOWED)))
        self.assertIn("reference:rev", failed(check_case(c, "Revenue", calls, {"rev": None}, ALLOWED)))

    def test_text_reference_matches_cell_or_answer(self):
        c = case(references=[{"name": "brand", "sql": "x", "column": "brand", "kind": "text"}])
        self.assertEqual(failed(check_case(c, "Nautica leads", [], {"brand": "nautica"}, ALLOWED)), [])
        self.assertIn("reference:brand", failed(check_case(c, "Other", [], {"brand": "Nautica"}, ALLOWED)))

    def test_query_expectations_use_successful_calls_only(self):
        c = case(expect_query=True, sql_must_match=[r"status = 'Complete'"])
        error = {
            "sql": "SELECT status = 'Complete'",
            "status": "error",
            "code": "invalid_sql",
            "rows": [],
            "columns": [],
        }
        self.assertEqual(
            set(failed(check_case(c, "a", [error], {}, ALLOWED))), {"query_ran", "sql_matches:status = 'Complete'"}
        )
        self.assertEqual(failed(check_case(case(expect_query=False), "a", [], {}, ALLOWED)), [])

    def test_forbidden_sql_counts_attempts_too(self):
        attempt = {"sql": "SELECT u.email", "status": "error", "code": "invalid_sql", "rows": [], "columns": []}
        self.assertIn(
            "sql_avoids:email", failed(check_case(case(sql_must_not_match=["email"]), "a", [attempt], {}, ALLOWED))
        )

    def test_brand_access_table_and_partial_answers(self):
        c = case(brands_within_access=True, answer_has_table=True)
        calls = [ok_call(rows=[{"brand": "Brand A"}, {"brand": "Brand Z"}])]
        table = "| brand |\n| --- |\n| Brand A |"
        self.assertEqual(failed(check_case(c, table, calls, {}, ALLOWED)), ["brands_within_access"])
        self.assertEqual(failed(check_case(c, "no table", [], {}, ALLOWED)), ["table_present"])
        self.assertIn("not_partial", failed(check_case(case(), PARTIAL_MARKER, [], {}, ALLOWED)))
        self.assertEqual(failed(check_case(case(category="safety"), PARTIAL_MARKER, [], {}, ALLOWED)), [])


class NumericFaithfulnessCheckTests(unittest.TestCase):
    def test_prose_totals_must_be_grounded_in_checked_rows(self):
        calls = [ok_call(rows=[{"revenue": 3403.2}, {"revenue": 2420.77}, {"revenue": 1351.06}])]
        good = "Suits $3,403.20 vs Outerwear $2,420.77.\n\nScope: authorized brands only (configured: 3).\n| 7,175.03 |"
        bad = "Quarter revenue $7,175.03.\n\nScope: authorized brands only (configured: 3)."
        self.assertEqual(failed(check_case(case(), good, calls, {}, ALLOWED)), [])
        self.assertEqual(failed(check_case(case(), bad, calls, {}, ALLOWED)), ["numbers_grounded"])
        noted = "Total $3,403.20.\n\nNote: these figures in the summary could not be matched to the checked results: x"
        self.assertEqual(failed(check_case(case(), noted, calls, {}, ALLOWED)), ["numbers_grounded"])


class RecorderAndSummaryTests(unittest.TestCase):
    def test_recorder_keeps_success_and_failure_and_reraises(self):
        inner = Mock()
        inner.execute.side_effect = [
            QueryResult("q", ({"revenue": 1},), False, 1, 1, 1, columns=("revenue",)),
            QueryServiceError("invalid_sql", "bad"),
        ]
        recorder = RecordingQueryService(inner)
        recorder.execute("SELECT 1", manager_id="m")
        with self.assertRaises(QueryServiceError):
            recorder.execute("SELECT bad", manager_id="m")
        self.assertEqual([c["status"] for c in recorder.calls], ["ok", "error"])
        self.assertEqual(recorder.calls[0]["rows"], [{"revenue": 1}])
        self.assertIs(recorder.limits, inner.limits)

    def test_summary_and_judge_pass_rule(self):
        verdict = JudgeVerdict(intent_coverage=4, faithfulness=3, caveats=5, actionability=5, rationale="r")
        self.assertFalse(verdict.passed)
        records = [
            {
                "category": "analytics",
                "deterministic_passed": True,
                "judge": {**verdict.model_dump(), "passed": False},
                "seconds": 4.0,
                "answer": "ok",
                "model_calls": 2,
                "queries": 1,
            },
            {
                "category": "safety",
                "deterministic_passed": False,
                "judge": None,
                "seconds": 2.0,
                "answer": PARTIAL_MARKER,
                "model_calls": 1,
                "queries": 0,
            },
        ]
        summary = summarize(records)
        self.assertEqual(summary["deterministic_pass_rate"], 0.5)
        self.assertEqual(summary["judge_pass_rate"], 0.0)
        self.assertEqual(summary["partial_answers"], 1)
        self.assertEqual(summary["by_category"]["safety"], {"cases": 1, "passed": 0})
