"""What one chat remembers between questions (in process, never persisted)."""

from dataclasses import dataclass, field
from uuid import uuid4

from retail_agent.agent.conversation import append_turn, is_snapshot_answer
from retail_agent.agent.models import AgentLimits


@dataclass
class ChatSession:
    conversation_id: str = field(default_factory=lambda: uuid4().hex)
    history: list[dict] = field(default_factory=list)
    first_question: str | None = None
    history_truncated: bool = False
    # The answer "save report" stores, with the brands it was computed for.
    last_answer: str | None = None
    last_answer_brands: list[str] = field(default_factory=list)

    @property
    def previous_question(self) -> str | None:
        return self.history[-1]["question"] if self.history else None

    def start_question(self, question: str) -> None:
        if self.first_question is None:
            self.first_question = question

    def record_turn(self, question: str, result: dict, *, brands: list[str], limits: AgentLimits) -> None:
        """Keep a saveable answer and a bounded history entry for a finished turn."""
        if is_snapshot_answer(result):
            self.last_answer = result["answer"]
            self.last_answer_brands = list(brands)
        if append_turn(self.history, question, result, limits):
            self.history_truncated = True
