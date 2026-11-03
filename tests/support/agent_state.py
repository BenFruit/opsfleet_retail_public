"""Builders for agent turn state and observations used by pure-module tests."""

from retail_agent.agent.models import AgentAction


def turn_state(*observations, **fields) -> dict:
    """A turn after planning, with counters at zero unless overridden."""
    state = {
        "turn_id": "turn-1",
        "question": "Revenue by category?",
        "history": [],
        "observations": list(observations),
        "action": None,
        "answer": None,
        "queries": 0,
        "model_calls": 1,
        "action_retries": 0,
        "consecutive_errors": 0,
        "report_calls": 0,
        "memory_calls": 0,
    }
    return {**state, **fields}


def checked(*rows, **extra) -> dict:
    return {"status": "ok", "rows": list(rows), "truncated": False, **extra}


def failed(code="query_rejected", message="BigQuery rejected the query.") -> dict:
    return {"status": "error", "code": code, "message": message}


def answer_action(text="Done.") -> AgentAction:
    return AgentAction(action="answer", answer=text)
