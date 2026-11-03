import json
import tempfile
import unittest
from pathlib import Path

from retail_agent.observability.metrics import load_events, summarize, timeline


def event(name, **fields):
    return {"time": "2026-11-03T10:00:00+00:00", "event": name, **fields}


EVENTS = [
    event("golden_retrieved", turn_id="t1", row_count=2, top_similarity=0.8, golden_ids=["a", "b"]),
    event("query_failed", query_id="q1", turn_id="t1", stage="validation", error_code="invalid_sql"),
    event("agent_observation", turn_id="t1", query_id="q1", status="error", error_code="invalid_sql"),
    event("query_completed", query_id="q2", turn_id="t1", row_count=3),
    event("agent_observation", turn_id="t1", query_id="q2", status="ok"),
    event("agent_model_completed", turn_id="t1", total_tokens=100, model="m"),
    event("agent_model_completed", turn_id="t1", total_tokens=50, model="m"),
    event(
        "agent_turn_completed",
        turn_id="t1",
        status="ok",
        duration_ms=4000,
        model_calls=2,
        query_count=2,
        scan_bytes_used=20_000_000,
    ),
    event("golden_retrieved", turn_id="t2", row_count=0, top_similarity=0.5, golden_ids=[]),
    event("agent_observation", turn_id="t2", query_id="q3", status="ok"),
    event("model_retry", error_code="model_unavailable", fallback=True, model="backup"),
    event("answer_ungrounded_figures", turn_id="t2", status="repair", row_count=1),
    event(
        "agent_turn_completed",
        turn_id="t2",
        status="partial",
        duration_ms=8000,
        model_calls=3,
        query_count=1,
        scan_bytes_used=0,
    ),
]


class MetricsTests(unittest.TestCase):
    def test_summary_metrics(self):
        summary = summarize(EVENTS)
        self.assertEqual(summary["turns"], 2)
        self.assertEqual(summary["partial_answer_rate"], 0.5)
        self.assertEqual(summary["sql"]["first_attempt_success_rate"], 0.5)
        self.assertEqual(summary["sql"]["recovered_after_error_rate"], 1.0)
        self.assertEqual(summary["sql"]["failures_by_stage"], {"validation": 1})
        self.assertEqual(summary["per_turn_mean"]["tokens"], 75)
        self.assertEqual(summary["model"]["fallbacks"], 1)
        self.assertEqual(summary["golden"]["hit_rate"], 0.5)
        self.assertEqual(summary["grounding"], {"repair": 1})

    def test_timeline_joins_query_events_and_load_skips_bad_lines(self):
        ids = [e.get("query_id") for e in timeline(EVENTS, "t1") if e.get("query_id")]
        self.assertEqual(sorted(set(ids)), ["q1", "q2"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            path.write_text(
                "\n".join([json.dumps(EVENTS[0]), "not json", json.dumps(event("x", time="2026-11-01"))]),
                encoding="utf-8",
            )
            self.assertEqual(len(load_events(path)), 2)
            self.assertEqual(len(load_events(path, since="2026-11-02")), 1)

    def test_empty_log_does_not_crash(self):
        self.assertEqual(summarize([])["turns"], 0)
