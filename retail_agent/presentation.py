"""Markdown the user sees, built only from already-checked data.

Rendering is presentation, not a privacy control: observations reaching these
functions have already passed the result guard, and report payloads come from
the owner/brand-scoped library.
"""

import json
import re
from decimal import Decimal

from retail_agent.reports.models import ReportOperation


def render_observation(observation: dict, *, max_rows: int = 200) -> str:
    """Render checked values; never invent pages or hidden rows."""

    def cell(value, column=""):
        if value is None:
            return "null"
        text = format(value, ".12g") if isinstance(value, float) else str(value)
        if (
            any(word in column.lower() for word in ("revenue", "profit", "price", "cost", "spend", "amount"))
            and isinstance(value, (int, float, Decimal))
            and not isinstance(value, bool)
        ):
            text = f"{Decimal(str(value)):,.2f}"
        return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    rows = observation.get("rows", [])
    lines = ["The checked query returned no publishable rows."]
    if rows:
        columns = list(rows[0])
        lines = [
            "| " + " | ".join(cell(name) for name in columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        lines += ["| " + " | ".join(cell(row[name], name) for name in columns) + " |" for row in rows[:max_rows]]
        if len(rows) > max_rows:
            lines.append(f"Showing {max_rows} of {len(rows)} available result rows.")
    if observation.get("truncated"):
        lines.append("Result was truncated; this is not necessarily a complete ranking or total.")
    context = observation.get("query_context", {})
    if context.get("filters") is not None:
        lines.append("Filters: " + json.dumps(context["filters"], ensure_ascii=False))
    for note in (context.get("age_average_basis"), observation.get("privacy_note")):
        if note:
            lines.append(note)
    return "\n".join(lines)


def remove_markdown_tables(text: str) -> str:
    """Keep model prose; the app supplies the authoritative current-turn tables."""
    lines = text.splitlines()
    kept = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and "|" in lines[index]:
            separators = lines[index + 1].strip().strip("|").split("|")
            if separators and all(re.fullmatch(r"\s*:?-{3,}:?\s*", part) for part in separators):
                index += 2
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    index += 1
                continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept).strip()


def render_reports(observation: dict) -> str:
    operation = observation["operation"]
    if operation == ReportOperation.SAVE:
        return f"Saved report: {observation['title']}\nID: {observation['report_id']}"
    if operation == ReportOperation.OPEN:
        return f"{observation['title']}\nID: {observation['report_id']} | {observation['created_at']}\nHistorical snapshot.\n\n{observation['content']}"
    rows = observation["reports"]
    if not rows:
        return (
            "No matching reports. Nothing was deleted."
            if operation == ReportOperation.PREPARE_DELETE
            else "No reports found."
        )
    lines = [f"{row['report_id']} | {row['title']} | {row['created_at']}" for row in rows]
    if operation == ReportOperation.PREPARE_DELETE:
        return (
            "Deletion preview (nothing deleted yet):\n"
            + "\n".join(lines)
            + "\n\nConfirm ONLY with /confirm within 5 minutes, or /cancel. Any other question cancels this preview."
        )
    return "\n".join(lines) + "\n" + observation.get("note", "")
