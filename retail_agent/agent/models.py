from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from retail_agent.memory.models import RememberRequest
from retail_agent.reports.models import ReportRequest


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["query", "answer", "report", "remember"]
    sql: str = Field(default="", max_length=20_000)
    answer: str = Field(default="", max_length=8_000)
    report: ReportRequest | None = None
    remember: RememberRequest | None = None

    @model_validator(mode="after")
    def check_action(self):
        if self.action == "remember":
            if self.remember is None or self.sql or self.answer or self.report is not None:
                raise ValueError("remember requires memory strings only")
        elif self.remember is not None:
            raise ValueError("remember payload belongs only to remember actions")
        if self.action == "report":
            if self.report is None or self.sql or self.answer:
                raise ValueError("report requires a report request only")
        elif self.report is not None:
            raise ValueError("report request only belongs to report actions")
        if self.action == "query" and (not self.sql.strip() or self.answer):
            raise ValueError("query requires SQL only")
        if self.action == "answer" and (not self.answer.strip() or self.sql):
            raise ValueError("answer requires text and empty SQL")
        return self


@dataclass(frozen=True)
class AgentLimits:
    max_memory_calls: int = 1
    max_report_calls: int = 5
    max_queries: int = 5
    max_model_calls: int = 7
    max_consecutive_errors: int = 3  # First failed SQL + two repair attempts.
    max_action_retries: int = 2
    max_scan_bytes: int = 300_000_000
    max_input_chars: int = 4_000
    max_history_chars: int = 20_000
    max_history_turns: int = 6
    max_observation_chars: int = 12_000

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class AgentState(TypedDict):
    memory_calls: int
    user_memory: list[dict]
    memory_status: str
    golden: dict[str, Any]
    action_retries: int
    report_calls: int
    question: str
    history: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    action: AgentAction | None
    answer: str
    queries: int
    model_calls: int
    consecutive_errors: int
    scan_bytes_used: int
    turn_id: str


@dataclass(frozen=True)
class TurnResult:
    answer: str = field(repr=False)
    queries: int
    model_calls: int
    scan_bytes_used: int
