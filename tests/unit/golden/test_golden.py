import json
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from retail_agent.golden.evaluate import best_threshold, score
from retail_agent.golden.ingest import DEFAULT_BUCKET, bigquery_dry_run, ingest, load_bucket, plan_ingest
from retail_agent.golden.models import GoldenError, GoldenTrio
from retail_agent.golden.service import GoldenKnowledge, query_text
from retail_agent.sql.validator import validate_query
from support.paths import REPO_ROOT

BUCKET = REPO_ROOT / DEFAULT_BUCKET
VECTOR = [0.0] * 767 + [1.0]


def seed():
    return load_bucket(BUCKET)


def trio(**changes):
    data = json.loads((BUCKET / "monthly-revenue-trend.json").read_text(encoding="utf-8"))
    data.update(changes)
    return GoldenTrio.model_validate(data)


class GoldenBucketTests(unittest.TestCase):
    def test_seed_bucket_is_valid_under_the_same_sql_policy_as_model_sql(self):
        trios = seed()
        self.assertGreaterEqual(len(trios), 10)
        demographic = {t.id for t in trios if validate_query(t.sql).meta.get("demographic_subject")}
        # Demographic precedents must use the restricted aggregate shape, not a bypass.
        self.assertEqual(demographic, {"age-band-spend", "state-spend-comparison", "traffic-source-performance"})

    def test_reports_must_use_placeholders_not_numbers(self):
        for report in (
            "Revenue was $1,200 this month and more text to pass length.",
            "Revenue grew 12% versus last month, with more words for length.",
        ):
            with self.subTest(report=report), self.assertRaises(ValidationError):
                trio(analyst_report=report)
        self.assertIn("<", trio().analyst_report)

    def test_policy_violations_and_name_mismatch_fail_the_whole_bucket(self):
        bad_sql = "SELECT u.email AS email FROM `bigquery-public-data.thelook_ecommerce.users` AS u"
        with (
            self.subTest("pii"),
            patch("pathlib.Path.read_text", return_value=trio(sql=bad_sql).model_dump_json()),
            patch("pathlib.Path.glob", return_value=[BUCKET / "monthly-revenue-trend.json"]),
        ):
            with self.assertRaises(GoldenError) as caught:
                load_bucket(BUCKET)
            self.assertIn("SQL rejected by policy", str(caught.exception))
        with (
            self.subTest("name"),
            patch("pathlib.Path.glob", return_value=[BUCKET / "other-name.json"]),
            patch("pathlib.Path.read_text", return_value=trio().model_dump_json()),
        ):
            with self.assertRaises(GoldenError) as caught:
                load_bucket(BUCKET)
            self.assertIn("file name must equal id", str(caught.exception))

    def test_bigquery_dry_run_uses_brand_scoped_sql(self):
        runner = Mock()
        self.assertEqual(bigquery_dry_run(seed()[:2], runner), [])
        self.assertIn("@allowed_brands", runner.dry_run.call_args.args[0])
        runner.dry_run.side_effect = RuntimeError("SECRET")
        failures = bigquery_dry_run(seed()[:1], runner)
        self.assertEqual(len(failures), 1)
        self.assertNotIn("SECRET", failures[0])


class GoldenIngestTests(unittest.TestCase):
    def test_plan_reembeds_only_changes_and_removes_withdrawn_or_deleted(self):
        current, edited = trio(), trio(version=2, interpretation=trio().interpretation + " Edited.")
        withdrawn = trio(id="old-method", status="withdrawn")
        indexed = {current.id: ("stale-hash", "m"), "deleted-method": ("h", "m"), "old-method": ("h", "m")}
        plan = plan_ingest([edited, withdrawn], indexed, "m")
        self.assertEqual([t.id for t in plan.upserts], [current.id])
        self.assertEqual(plan.remove, ["deleted-method", "old-method"])
        unchanged = plan_ingest([current], {current.id: (current.content_hash(), "m")}, "m")
        self.assertEqual((unchanged.upserts, unchanged.unchanged), ([], [current.id]))
        model_change = plan_ingest([current], {current.id: (current.content_hash(), "old-model")}, "m")
        self.assertEqual([t.id for t in model_change.upserts], [current.id])

    def test_embedding_failure_publishes_nothing(self):
        repository, embedder = Mock(), Mock(model="m")
        repository.indexed_hashes.return_value = {}
        embedder.embed.side_effect = [VECTOR, RuntimeError("provider down")]
        with self.assertRaises(RuntimeError):
            ingest(seed()[:2], repository, embedder)
        repository.publish.assert_not_called()

    def test_successful_run_publishes_once(self):
        repository, embedder = Mock(), Mock(model="m")
        repository.indexed_hashes.return_value = {"gone": ("h", "m")}
        embedder.embed.return_value = VECTOR
        plan = ingest(seed()[:2], repository, embedder)
        repository.publish.assert_called_once()
        self.assertEqual(repository.publish.call_args.kwargs["remove"], ["gone"])
        self.assertEqual(len(plan.upserts), 2)


def row(t, similarity, lexical=0.0):
    return {
        "trio_id": t.id,
        "version": t.version,
        "document": t.model_dump(mode="json"),
        "similarity": similarity,
        "lexical": lexical,
    }


class GoldenRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.trios = {t.id: t for t in seed()}
        self.repository, self.embedder = Mock(), Mock(model="m")
        self.embedder.embed.return_value = VECTOR
        self.logger = self.enterContext(patch("retail_agent.golden.service.logger"))
        self.golden = GoldenKnowledge(self.repository, self.embedder, top_k=2, min_similarity=0.6)

    def test_threshold_top_k_and_lexical_tie_break(self):
        t = self.trios
        self.repository.search.return_value = [
            row(t["brand-gross-profit"], 0.70),
            row(t["monthly-revenue-trend"], 0.69, lexical=0.5),
            row(t["last-month-vs-previous"], 0.65),
            row(t["dataset-overview"], 0.40),
        ]
        result = self.golden.retrieve("revenue by month", turn_id="t1")
        self.assertEqual(result["status"], "ready")
        self.assertEqual([i["trio_id"] for i in result["items"]], ["monthly-revenue-trend", "brand-gross-profit"])
        self.assertNotIn("reviewer", result["items"][0])
        extra = self.logger.info.call_args.kwargs["extra"]
        self.assertEqual(
            (extra["turn_id"], extra["golden_ids"], extra["candidate_count"]),
            ("t1", ["monthly-revenue-trend", "brand-gross-profit"], 4),
        )

    def test_nothing_relevant_returns_no_examples(self):
        self.repository.search.return_value = [row(self.trios["dataset-overview"], 0.31)]
        self.assertEqual(self.golden.retrieve("hello")["items"], [])

    def test_prompt_budget_is_respected(self):
        self.golden.max_prompt_chars = 500
        self.repository.search.return_value = [row(self.trios["customer-churn-proxy"], 0.9)]
        self.assertEqual(self.golden.retrieve("churn")["items"], [])

    def test_outage_degrades_without_raising_or_leaking(self):
        self.embedder.embed.side_effect = RuntimeError("SECRET")
        self.assertEqual(self.golden.retrieve("revenue"), {"status": "unavailable", "items": []})
        self.assertNotIn("SECRET", repr(self.logger.mock_calls))

    def test_short_follow_up_includes_previous_question(self):
        self.assertEqual(query_text("and by month?", "Revenue for Nike"), "Revenue for Nike\nand by month?")
        long_question = "Which age groups spend the most on outerwear this year?"
        self.assertEqual(query_text(long_question, "previous"), long_question)


class GoldenEvaluationMetricTests(unittest.TestCase):
    def test_metrics_and_threshold_choice(self):
        results = [
            {"expected": "a", "ranked": [("a", 0.80), ("b", 0.60)]},
            {"expected": "b", "ranked": [("a", 0.70), ("b", 0.66)]},
            {"expected": None, "ranked": [("a", 0.55)]},
        ]
        metrics = score(results, 0.50)
        self.assertEqual(
            (metrics["hit_at_1"], metrics["recall_at_3"], metrics["negative_false_positive_rate"]), (0.5, 1.0, 1.0)
        )
        best = best_threshold(results)
        self.assertEqual((best["recall_at_3"], best["negative_false_positive_rate"]), (1.0, 0.0))
        self.assertGreater(best["threshold"], 0.55)
