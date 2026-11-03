"""Bounded in-process conversation history (no persistence)."""

import json

from retail_agent.agent.answer import query_failures
from retail_agent.observations import ObservationStatus


def is_snapshot_answer(result: dict) -> bool:
    """Whether a finished turn may become the answer that "save report" stores.

    Only a real analysis answer qualifies: not a report or memory-only turn,
    and not an answer that followed only failed queries.
    """
    action = result["action"]
    if result["report_calls"] or (result["memory_calls"] and not result["queries"]):
        return False
    if action is None or action.action != "answer":
        return False
    return not query_failures(result["observations"]) or any(
        item["status"] == ObservationStatus.OK for item in result["observations"]
    )


def append_turn(history: list[dict], question: str, result: dict, limits) -> bool:
    """Append a compact turn and trim the oldest; returns True if anything was dropped."""
    turn = {"question": question, "answer": result["answer"][:8000], "observations": result["observations"][-2:]}
    while turn["observations"] and len(json.dumps(turn, ensure_ascii=False)) > limits.max_history_chars:
        turn["observations"].pop(0)
    history.append(turn)
    truncated = False
    while history and (
        len(history) > limits.max_history_turns
        or len(json.dumps(history, ensure_ascii=False)) > limits.max_history_chars
    ):
        history.pop(0)
        truncated = True
    return truncated
