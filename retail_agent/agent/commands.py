"""Exact CLI commands that bypass the model.

Destructive authorization must never come from model output or from a phrase
like "yes", so /confirm and the memory commands are handled here, before any
model call, with exact string matching.
"""

import logging

from retail_agent.memory.models import UserMemoryError
from retail_agent.observability.tracing import trace_operation
from retail_agent.reports.models import ReportError

logger = logging.getLogger(__name__)

MEMORY_COMMANDS = {"/memories", "/forget-all"}
REPORT_COMMANDS = {"/confirm", "/cancel"}


def is_command(command: str) -> bool:
    return command in MEMORY_COMMANDS or command in REPORT_COMMANDS or command.startswith("/forget ")


def run_command(command: str, *, memory, reports, conversation_id: str) -> str:
    """Run a normalized (stripped, lower-case) command; returns the user-facing text."""
    if command in REPORT_COMMANDS:
        return _report_command(command, reports, conversation_id)
    if reports is not None:
        reports.cancel()  # Any other input cancels a pending deletion preview.
    return _memory_command(command, memory)


def _memory_command(command: str, memory) -> str:
    with trace_operation("user_memory_command", {"command": command.split()[0]}) as span:
        try:
            if memory is None:
                raise UserMemoryError("User memory is not configured.")
            if command == "/memories":
                entries = memory.load()
                text = (
                    "\n".join(f"{item['memory_id']} | {item['text']}" for item in entries) or "No saved user memories."
                )
            else:
                text = memory.forget(None if command == "/forget-all" else command.split(maxsplit=1)[1])
        except UserMemoryError as error:
            text = str(error)
        if span is not None:
            span.update(output={"message": text})
    return text


def _report_command(command: str, reports, conversation_id: str) -> str:
    with trace_operation("report_confirmation", {"command": command, "conversation_id": conversation_id}) as span:
        try:
            if reports is None:
                text = "Report library is not configured."
            elif command == "/cancel":
                reports.cancel()
                text = "Deletion cancelled."
            else:
                text = reports.confirm()
        except (ReportError, PermissionError):
            text = "Deletion could not be confirmed. Check the library and request a new preview."
        if span is not None:
            span.update(output={"message": text})
    logger.info("agent_report_confirmation", extra={"action": command})
    return text
