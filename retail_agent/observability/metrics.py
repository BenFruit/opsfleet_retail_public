"""Agent-level metrics and per-turn deep dive from the allowlisted JSON log.

    uv run python -m retail_agent.observability.metrics                  # summary
    uv run python -m retail_agent.observability.metrics --since 2026-11-03
    uv run python -m retail_agent.observability.metrics --turn <turn_id>  # timeline

The log holds metadata only (no prompts, SQL, rows or exception text), so this
report is safe to share. In production the same events flow to Cloud Logging and
a BigQuery sink, and these aggregations become dashboard queries and alerts.
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def load_events(path: Path, since: str | None = None) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is None or event.get("time", "") >= since:
            events.append(event)
    return events


def _percentile(values: list[float], share: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def _rate(part: int, whole: int):
    return round(part / whole, 3) if whole else None


def summarize(events: list[dict]) -> dict:
    by_name = defaultdict(list)
    for event in events:
        by_name[event["event"]].append(event)
    turns = by_name["agent_turn_completed"]
    turn_ids = {t["turn_id"] for t in turns}

    # SQL quality: per turn, first observation outcome and recovery after errors.
    observations = defaultdict(list)
    for event in by_name["agent_observation"]:
        observations[event.get("turn_id")].append(event)
    sql_turns = [obs for tid, obs in observations.items() if tid in turn_ids]
    first_ok = sum(1 for obs in sql_turns if obs[0]["status"] in {"ok", "empty"})
    had_error = [obs for obs in sql_turns if any(o["status"] == "error" for o in obs)]
    recovered = sum(
        1
        for obs in had_error
        if any(
            o["status"] in {"ok", "empty"} for o in obs[next(i for i, o in enumerate(obs) if o["status"] == "error") :]
        )
    )

    tokens = defaultdict(int)
    for event in by_name["agent_model_completed"]:
        tokens[event.get("turn_id")] += event.get("total_tokens", 0)
    golden = by_name["golden_retrieved"]
    durations = [t.get("duration_ms", 0) / 1000 for t in turns]
    return {
        "turns": len(turns),
        "partial_answer_rate": _rate(sum(t.get("status") == "partial" for t in turns), len(turns)),
        "failed_turns": len(by_name["agent_turn_failed"]),
        "latency_seconds": {"p50": _percentile(durations, 0.5), "p95": _percentile(durations, 0.95)},
        "per_turn_mean": {
            "model_calls": round(sum(t.get("model_calls", 0) for t in turns) / len(turns), 2) if turns else None,
            "sql_attempts": round(sum(t.get("query_count", 0) for t in turns) / len(turns), 2) if turns else None,
            "tokens": round(sum(tokens[t["turn_id"]] for t in turns) / len(turns)) if turns else None,
            "scan_mb": round(sum(t.get("scan_bytes_used", 0) for t in turns) / len(turns) / 1e6, 1) if turns else None,
        },
        "sql": {
            "turns_with_sql": len(sql_turns),
            "first_attempt_success_rate": _rate(first_ok, len(sql_turns)),
            "turns_with_errors": len(had_error),
            "recovered_after_error_rate": _rate(recovered, len(had_error)),
            "failures_by_code": dict(Counter(e.get("error_code") for e in by_name["query_failed"]).most_common()),
            "failures_by_stage": dict(Counter(e.get("stage") for e in by_name["query_failed"]).most_common()),
        },
        "model": {
            "failures_by_code": dict(Counter(e.get("error_code") for e in by_name["agent_model_failed"]).most_common()),
            "retries": sum(not e.get("fallback") for e in by_name["model_retry"]),
            "fallbacks": sum(bool(e.get("fallback")) for e in by_name["model_retry"]),
            "answered_by": dict(
                Counter(e.get("model") for e in by_name["agent_model_completed"] if e.get("model")).most_common()
            ),
        },
        "golden": {
            "retrievals": len(golden),
            "hit_rate": _rate(sum(e.get("row_count", 0) > 0 for e in golden), len(golden)),
            "top_similarity_p50": _percentile(
                [e["top_similarity"] for e in golden if e.get("top_similarity") is not None], 0.5
            ),
            "failures": len(by_name["golden_retrieval_failed"]),
            "most_used": dict(Counter(i for e in golden for i in e.get("golden_ids", [])).most_common(5)),
        },
        "grounding": dict(Counter(e.get("status") for e in by_name["answer_ungrounded_figures"])),
        "reports": dict(Counter(e.get("action") for e in by_name["agent_report_completed"])),
        "deletions_confirmed": len(by_name["report_delete_completed"]),
        "dependency_warnings": {
            name: len(by_name[name])
            for name in ("persona_load_failed", "user_memory_load_failed", "tracing_unavailable", "startup_failed")
            if by_name[name]
        },
    }


def timeline(events: list[dict], turn_id: str) -> list[dict]:
    """All events of one turn, including its queries (joined by query_id)."""
    query_ids = {e["query_id"] for e in events if e.get("turn_id") == turn_id and e.get("query_id")}
    return [e for e in events if e.get("turn_id") == turn_id or e.get("query_id") in query_ids]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agent metrics from logs/agent.jsonl")
    parser.add_argument("--log-file", type=Path, default=Path("logs/agent.jsonl"))
    parser.add_argument("--since", help="ISO date/time prefix, e.g. 2026-11-03")
    parser.add_argument("--turn", help="print the event timeline of one turn_id")
    args = parser.parse_args(argv)
    events = load_events(args.log_file, args.since)
    if args.turn:
        for event in timeline(events, args.turn):
            print(json.dumps(event, ensure_ascii=False))
        return 0
    print(json.dumps(summarize(events), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
