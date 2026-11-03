"""Turning the model's final answer into what the user sees.

Pure functions over the turn state: no model or database calls. The model's
prose is checked against query results, tables are rebuilt from checked rows,
and a premature or ungrounded answer is sent back for one bounded repair.
"""

import logging

from retail_agent.agent.feedback import REPAIR_REQUIRED, ungrounded_numbers
from retail_agent.observations import CHECKED_RESULT, ObservationStatus
from retail_agent.presentation import remove_markdown_tables, render_observation, render_reports
from retail_agent.reports.models import ReportOperation
from retail_agent.safety.grounding import ungrounded_figures

logger = logging.getLogger(__name__)

PARTIAL_PREFIX = "I could not fully complete the analysis."
# Controller feedback to the model; these are not failed queries.
FEEDBACK_CODES = {"model_invalid_action", "model_invalid_json", "repair_required", "ungrounded_numbers"}


def query_failures(observations: list[dict]) -> list[dict]:
    return [
        item
        for item in observations
        if item["status"] == ObservationStatus.ERROR and item.get("code") not in FEEDBACK_CODES
    ]


def partial_answer(state, reason: str) -> str:
    """Controlled stop: the reason, the last query issue and the last checked result."""
    text = f"{PARTIAL_PREFIX} {reason}"
    failures = query_failures(state["observations"])
    if failures:
        last = failures[-1]
        text += f"\nLast query issue: [{last['code']}] {last['message']}"
    successes = [item for item in state["observations"] if item["status"] == ObservationStatus.OK]
    if successes:
        text += "\nLast checked result (may be partial):\n" + render_observation(successes[-1], max_rows=10)
    return text


def _feedback(state, update: dict, code: str, message: str) -> dict:
    """Send the answer back to the model once instead of showing it."""
    return {
        **update,
        "action": None,
        "action_retries": state["action_retries"] + 1,
        "observations": [*state["observations"], {"status": ObservationStatus.ERROR, "code": code, "message": message}],
    }


def finalize_answer(state, answer: str, update: dict, *, limits, authorized_brand_count: int) -> dict:
    """Return the graph update for an "answer" action: final text or one repair request."""
    observations = state["observations"]
    checked = [item for item in observations if item["status"] in CHECKED_RESULT]
    report_results = [item for item in observations if item["status"] == ObservationStatus.REPORT]

    if query_failures(observations) and not checked and not report_results:
        # A premature final answer is not a replacement for repairing the failed SQL.
        if (
            state["consecutive_errors"] < limits.max_consecutive_errors
            and state["action_retries"] < limits.max_action_retries
            and state["queries"] < limits.max_queries
        ):
            return _feedback(
                state,
                update,
                "repair_required",
                REPAIR_REQUIRED,
            )
        # An answer following only failures cannot pretend to deliver fresh
        # results (including "I will fetch it" placeholder answers).
        return {**update, "answer": partial_answer(state, "No new query completed successfully.")}

    prose = remove_markdown_tables(answer) if checked else answer
    ungrounded = []
    if checked and not report_results:
        # Follow-ups may legitimately quote numbers checked in earlier turns.
        evidence = checked + [item for turn in state["history"] for item in turn.get("observations", [])]
        ungrounded = ungrounded_figures(prose, evidence, context=state["question"])
    if ungrounded:
        can_repair = (
            state["action_retries"] < limits.max_action_retries and state["model_calls"] + 1 < limits.max_model_calls
        )
        logger.warning(
            "answer_ungrounded_figures",
            extra={
                "turn_id": state["turn_id"],
                "row_count": len(ungrounded),
                "status": "repair" if can_repair else "noted",
            },
        )
        # One bounded self-correction: compute totals in SQL instead of in prose.
        if can_repair:
            return _feedback(
                state,
                update,
                "ungrounded_numbers",
                ungrounded_numbers(ungrounded),
            )

    text = (prose or "Here are the checked results.") if checked else answer
    if ungrounded:
        text += (
            "\n\nNote: these figures in the summary could not be matched to the checked "
            f"results and may be miscalculated: {', '.join(ungrounded[:10])}. Rely on the tables."
        )
    if checked:
        text += (
            f"\n\nScope: authorized brands only (configured: {authorized_brand_count})."
            "\n\nChecked query results:\n"
            + "\n\n".join(f"Result {index}:\n{render_observation(item)}" for index, item in enumerate(checked, start=1))
        )
    if report_results:
        text += "\n\nReport sources:\n" + "\n".join(
            render_reports(item)
            for item in report_results
            if item["operation"] in {ReportOperation.SEARCH, ReportOperation.LIST}
        )
    for event in observations:
        if event["status"] == ObservationStatus.MEMORY:
            text += "\n\n" + event["message"]
    return {**update, "answer": text}
