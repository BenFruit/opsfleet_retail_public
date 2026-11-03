"""LangGraph turn loop: plan -> (query | reports | remember) -> plan ... -> end.

Graph wiring, node bodies and per-turn budgets live here. Slash commands,
final-answer composition and history trimming are in commands.py, answer.py
and conversation.py.
"""

import logging
from contextlib import suppress
from time import monotonic
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from retail_agent.agent.answer import PARTIAL_PREFIX, finalize_answer, partial_answer
from retail_agent.agent.commands import is_command, run_command
from retail_agent.agent.feedback import MEMORY_ALREADY_ATTEMPTED, REJECTED_ACTION
from retail_agent.agent.models import AgentLimits, AgentState, TurnResult
from retail_agent.agent.session import ChatSession
from retail_agent.data.query_service import QueryService, QueryServiceError
from retail_agent.llm.analyst import Analyst
from retail_agent.llm.embeddings import EmbeddingError
from retail_agent.llm.errors import describe_model_error
from retail_agent.memory.models import UserMemoryError
from retail_agent.observability.tracing import get_callbacks
from retail_agent.observations import ObservationStatus
from retail_agent.presentation import render_reports
from retail_agent.reports.models import ReportError, ReportOperation
from retail_agent.safety.errors import ResultGuardError
from retail_agent.safety.result_guard import make_observation
from retail_agent.safety.sensitive_text import check_text

logger = logging.getLogger(__name__)
_STOP_CODES = {"access_denied", "unsafe_result", "timeout", "backend_unavailable", "backend_denied", "rate_limited"}


def route_after_plan(state: AgentState) -> str:
    """Graph edge after planning: finish, re-plan after a rejected action, or run the chosen tool."""
    if state["answer"]:
        return "end"
    if state["action"] is None:
        return "plan"
    if state["action"].action == "remember":
        return "remember"
    return "reports" if state["action"].action == "report" else "query"


class AnalyticalAgent:
    """One trusted manager/session per instance; memory is bounded and in-process."""

    def __init__(
        self,
        analyst: Analyst,
        query_service: QueryService,
        *,
        manager_id: str,
        entitlements,
        limits: AgentLimits | None = None,
        reports=None,
        memory=None,
        golden=None,
    ):
        self.allowed_brands = sorted(entitlements.brands_for(manager_id))
        self.authorized_brand_count = len(self.allowed_brands)
        self.analyst = analyst
        self.query_service = query_service
        self.manager_id = manager_id
        self.limits = limits if limits is not None else AgentLimits()
        self.reports = reports
        self.memory = memory
        self.golden = golden
        self.session = ChatSession()

        graph = StateGraph(AgentState)
        graph.add_node("plan", self._plan)
        graph.add_node("query", self._query)
        graph.add_node("reports", self._reports)
        graph.add_node("remember", self._remember)
        graph.add_edge(START, "plan")
        graph.add_conditional_edges(
            "plan",
            route_after_plan,
            {"query": "query", "reports": "reports", "remember": "remember", "plan": "plan", "end": END},
        )
        graph.add_edge("remember", "plan")
        graph.add_conditional_edges(
            "reports",
            lambda state: "end" if state["answer"] else "plan",
            {
                "plan": "plan",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "query",
            lambda state: "end" if state["answer"] else "plan",
            {
                "plan": "plan",
                "end": END,
            },
        )
        self.graph = graph.compile()  # No persistence/checkpointer.

    def reset(self) -> None:
        if self.reports is not None:
            self.reports.cancel()
        self.session = ChatSession()

    def ask(self, question: str) -> TurnResult:
        if isinstance(question, str):
            command = question.strip().lower()
            if is_command(command):
                return TurnResult(
                    run_command(
                        command, memory=self.memory, reports=self.reports, conversation_id=self.session.conversation_id
                    ),
                    0,
                    0,
                    0,
                )
            if self.reports is not None:
                self.reports.cancel()  # A new question cancels a pending deletion preview.
        rejection = self._reject_question(question)
        if rejection:
            return TurnResult(rejection, 0, 0, 0)
        self.session.start_question(question)

        state = self._new_state(question)
        started = monotonic()
        try:
            self._prepare_context(state)
            # Langfuse is opt-in. Keep implicit LangSmith export disabled even
            # when stale LANGSMITH_TRACING variables remain in the environment.
            with tracing_context(enabled=False):
                result = self.graph.invoke(
                    state,
                    config={
                        "recursion_limit": 2 * self.limits.max_model_calls + 5,
                        "run_name": "retail_question",
                        "metadata": {"turn_id": state["turn_id"]},
                        "callbacks": get_callbacks(),
                    },
                )
        except Exception:
            logger.warning("agent_turn_failed", extra={"turn_id": state["turn_id"], "error_code": "graph_failed"})
            return TurnResult("Analysis could not be completed. Please try again.", 0, 0, 0)

        self.session.record_turn(question, result, brands=self.allowed_brands, limits=self.limits)
        logger.info(
            "agent_turn_completed",
            extra={
                "turn_id": state["turn_id"],
                "query_count": result["queries"],
                # "partial" = controlled stop (budget, unrecovered failure); a key quality metric.
                "status": "partial" if result["answer"].startswith(PARTIAL_PREFIX) else "ok",
                "model_calls": result["model_calls"],
                "scan_bytes_used": result["scan_bytes_used"],
                "duration_ms": round((monotonic() - started) * 1000),
                "report_calls": result["report_calls"],
                "memory_calls": result["memory_calls"],
            },
        )
        return TurnResult(result["answer"], result["queries"], result["model_calls"], result["scan_bytes_used"])

    def _reject_question(self, question) -> str | None:
        if not isinstance(question, str) or not question.strip():
            return "Please enter an analysis question."
        if len(question) > self.limits.max_input_chars:
            return "The question is too long. Please shorten it."
        try:
            check_text(question)
        except ResultGuardError:
            return "Please remove personal contact details before sending the question."
        return None

    def _new_state(self, question: str) -> AgentState:
        return {
            "memory_calls": 0,
            "user_memory": [],
            "memory_status": "disabled",
            "golden": {"status": "disabled", "items": []},
            "action_retries": 0,
            "report_calls": 0,
            "question": question,
            "history": list(self.session.history),
            "observations": [],
            "action": None,
            "answer": "",
            "queries": 0,
            "model_calls": 0,
            "consecutive_errors": 0,
            "scan_bytes_used": 0,
            "turn_id": uuid4().hex,
        }

    def _prepare_context(self, state: AgentState) -> None:
        """Persona, user memory and Golden examples, loaded once before planning."""
        self.analyst.refresh_persona()
        if self.memory is not None:
            try:
                state["user_memory"] = self.memory.load()
                state["memory_status"] = "ready"
            except UserMemoryError:
                state["memory_status"] = "unavailable"
                logger.warning("user_memory_load_failed", extra={"error_code": "memory_unavailable"})
        if self.golden is not None:
            # Every model step in this turn sees the same examples.
            previous = self.session.previous_question
            state["golden"] = self.golden.retrieve(
                state["question"], previous_question=previous, turn_id=state["turn_id"]
            )

    def _plan(self, state: AgentState) -> dict:
        if state["model_calls"] >= self.limits.max_model_calls:
            return {"answer": partial_answer(state, "The model-step budget was exhausted.")}
        started = monotonic()
        try:
            action = self.analyst.decide(**self._decision_context(state))
            if action.action == "answer":
                check_text(action.answer)
            logger.info(
                "agent_model_completed",
                extra={
                    "turn_id": state["turn_id"],
                    "action": action.action,
                    "duration_ms": round((monotonic() - started) * 1000),
                    **self.analyst.llm.last_usage,
                },
            )
        except Exception as error:
            return self._model_failure(state, error, started)
        update = {"action": action, "model_calls": state["model_calls"] + 1}
        if action.action == "answer":
            return finalize_answer(
                state, action.answer, update, limits=self.limits, authorized_brand_count=self.authorized_brand_count
            )
        if action.action == "remember":
            if state["memory_calls"] >= self.limits.max_memory_calls:
                return {
                    **update,
                    "action": None,
                    "observations": [
                        *state["observations"],
                        {
                            "status": ObservationStatus.MEMORY,
                            "saved": False,
                            "message": MEMORY_ALREADY_ATTEMPTED,
                        },
                    ],
                }
        elif action.action == "report":
            if state["report_calls"] >= self.limits.max_report_calls:
                update["answer"] = "The report-tool budget was exhausted."
        elif state["queries"] >= self.limits.max_queries or state["scan_bytes_used"] >= self.limits.max_scan_bytes:
            update["answer"] = partial_answer(state, "The query budget was exhausted.")
        return update

    def _decision_context(self, state: AgentState) -> dict:
        """Everything the model sees for one planning step; identity and budgets are trusted values."""
        return {
            "user_memory": {"status": state["memory_status"], "items": state["user_memory"]},
            "golden": state["golden"],
            "question": state["question"],
            "history": state["history"],
            "observations": state["observations"],
            "access_context": {"authorized_brands": self.allowed_brands},
            "session_context": {
                "reports_available": self.reports is not None,
                "has_answer_to_save": self.session.last_answer is not None,
                "memory_available": self.memory is not None,
                "first_question": self.session.first_question,
                "history_truncated": self.session.history_truncated,
            },
            "budget": {
                "report_calls": self.limits.max_report_calls - state["report_calls"],
                "memory_calls": self.limits.max_memory_calls - state["memory_calls"],
                "sql_attempts": self.limits.max_queries - state["queries"],
                "scan_bytes": max(0, self.limits.max_scan_bytes - state["scan_bytes_used"]),
            },
        }

    def _model_failure(self, state: AgentState, error: Exception, started: float) -> dict:
        """Invalid JSON/actions get up to two bounded corrections; other model failures stop the turn."""
        if isinstance(error, ResultGuardError):
            code, message, metadata = (
                "model_output_withheld",
                "The model response was withheld by the privacy guard.",
                {"error_type": "ResultGuardError"},
            )
        else:
            code, message, metadata = describe_model_error(error)
        logger.warning(
            "agent_model_failed",
            extra={
                "turn_id": state["turn_id"],
                "error_code": code,
                "duration_ms": round((monotonic() - started) * 1000),
                **metadata,
            },
        )
        if (
            code in {"model_invalid_action", "model_invalid_json"}
            and state["action_retries"] < self.limits.max_action_retries
        ):
            return {
                "action": None,
                "model_calls": state["model_calls"] + 1,
                "action_retries": state["action_retries"] + 1,
                "observations": [
                    *state["observations"],
                    {
                        "status": ObservationStatus.ERROR,
                        "code": code,
                        "message": REJECTED_ACTION,
                    },
                ],
            }
        return {"model_calls": state["model_calls"] + 1, "answer": partial_answer(state, f"[{code}] {message}")}

    def _remember(self, state: AgentState) -> dict:
        update = {"memory_calls": state["memory_calls"] + 1}
        try:
            if self.memory is None:
                raise UserMemoryError("User memory is not configured.")
            observation = self.memory.remember(
                state["action"].remember, question=state["question"], turn_id=state["turn_id"]
            )
            try:
                update["user_memory"] = self.memory.load()
                update["memory_status"] = "ready"
            except UserMemoryError:
                update["memory_status"] = "unavailable"
        except UserMemoryError as error:
            observation = {"status": ObservationStatus.MEMORY, "saved": False, "message": str(error)}
        except Exception:
            observation = {
                "status": ObservationStatus.MEMORY,
                "saved": False,
                "message": "Memory write was not confirmed. Inspect /memories before retrying.",
            }
        update["observations"] = [*state["observations"], observation]
        # Always return to planning: remember never ends a mixed-intent turn.
        return update

    def _reports(self, state: AgentState) -> dict:
        request = state["action"].report
        update = {"report_calls": state["report_calls"] + 1}
        status = "ok"
        try:
            if self.reports is None:
                raise ReportError("Report library is not configured.")
            # Save only the previous completed response, never a model-provided
            # replacement or an in-progress answer from this invocation.
            observation = self.reports.execute(
                request,
                conversation_id=self.session.conversation_id,
                last_answer=self.session.last_answer,
                last_brands=self.session.last_answer_brands,
            )
            if request.operation != ReportOperation.SEARCH or not observation.get("reports"):
                update["answer"] = render_reports(observation)
            else:
                update["observations"] = [*state["observations"], observation]
        except (ReportError, EmbeddingError, ResultGuardError, PermissionError) as error:
            status = "error"
            # ResultGuard errors contain controlled messages, never raw rows.
            update["answer"] = str(error) if not isinstance(error, PermissionError) else "No report access."
        except Exception:
            status = "error"
            update["answer"] = "Report operation failed. No success is confirmed; check the library before retrying."
        logger.info(
            "agent_report_completed",
            extra={
                "turn_id": state["turn_id"],
                "action": request.operation,
                "status": status,
            },
        )
        return update

    def _query(self, state: AgentState) -> dict:
        action = state["action"]
        remaining = self.limits.max_scan_bytes - state["scan_bytes_used"]
        # Reserve the full per-query ceiling when an execution failure leaves
        # actual billing unknown. Never blindly replay an ambiguous timeout.
        reserved = min(remaining, self.query_service.limits.maximum_bytes_billed)
        spent = reserved
        try:
            result = self.query_service.execute(
                action.sql,
                allowed_brands=frozenset(self.allowed_brands),
                scan_budget_bytes=remaining,
                turn_id=state["turn_id"],
            )
            spent = max(result.estimated_bytes, result.bytes_billed or 0, result.bytes_processed or 0)
            observation = make_observation(action.sql, result, max_chars=self.limits.max_observation_chars)
            errors = 0
        except QueryServiceError as error:
            observation = {"status": ObservationStatus.ERROR, "code": error.code, "message": str(error)}
            if error.query_id:
                observation["query_id"] = error.query_id
            # Echo the attempted SQL so the model can repair it, unless it contains suspicious literals.
            with suppress(ResultGuardError):
                observation["attempted_sql"] = check_text(action.sql)[: self.limits.max_observation_chars // 2]
                observation["attempted_sql_truncated"] = len(action.sql) > self.limits.max_observation_chars // 2
            errors = state["consecutive_errors"] + 1
            if error.stage in {"authorization", "validation", "dry_run"} or error.code in {
                "access_denied",
                "invalid_sql",
                "cost_limit",
                "estimate_unavailable",
            }:
                spent = 0  # These errors happen before job execution.
        except ResultGuardError:
            observation = {
                "status": ObservationStatus.ERROR,
                "code": "unsafe_result",
                "message": "Result was withheld by the output guard.",
            }
            errors = state["consecutive_errors"] + 1
        except Exception:
            observation = {
                "status": ObservationStatus.ERROR,
                "code": "query_failed",
                "message": "The query could not be completed.",
            }
            errors = state["consecutive_errors"] + 1

        update = {
            "observations": [*state["observations"], observation],
            "queries": state["queries"] + 1,
            "consecutive_errors": errors,
            "scan_bytes_used": state["scan_bytes_used"] + spent,
        }
        logger.info(
            "agent_observation",
            extra={
                "turn_id": state["turn_id"],
                "query_id": observation.get("query_id"),
                "status": observation["status"],
                "error_code": observation.get("code"),
                "query_count": update["queries"],
            },
        )
        if errors >= self.limits.max_consecutive_errors or observation.get("code") in _STOP_CODES:
            update["answer"] = partial_answer({**state, **update}, "Analysis stopped after a query failure.")
        return update
