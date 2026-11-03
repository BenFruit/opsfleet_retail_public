"""Controller feedback the model reads as an observation and must act on.

These texts steer the next planning step, so they are part of the prompt even
though they live outside analyst_policy.md. Change them deliberately.
"""

REJECTED_ACTION = (
    "Your previous response was rejected before its proposed action executed. "
    "Earlier successful tool results remain valid; do not repeat successful writes. "
    "Return ONE corrected JSON action: query with SQL only, report with a valid request only "
    "(query is required, use empty string when not searching), remember with remember.memories strings only, "
    "or answer with text only. Never use a demographic action. Do not claim the rejected action succeeded."
)

MEMORY_ALREADY_ATTEMPTED = (
    "Memory was already attempted this turn. Do not retry the write; continue the remaining analysis or answer."
)

REPAIR_REQUIRED = (
    "No SQL completed successfully. Repair the previous attempted SQL now using query, unless the request is "
    "prohibited; in that case explain the safety restriction. Never substitute a different grouping or invent totals."
)


def ungrounded_numbers(figures: list[str]) -> str:
    """Ask for one SQL-based correction of prose figures that match no checked value."""
    return (
        "These figures in your prose do not match any checked value or the sum/difference of two: "
        f"{', '.join(figures[:10])}. "
        "Do not add up or average many rows in prose. Run a query that computes the total/average, "
        "or quote only checked values, then answer again."
    )
