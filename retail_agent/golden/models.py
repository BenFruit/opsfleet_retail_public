"""Golden Knowledge trio contract: Question -> SQL -> Analyst report.

A trio is a reviewed analytical METHOD (how an expert interpreted a question),
not a cache of true answers. Numbers in reports are placeholders such as
``<revenue>`` so the model cannot mistake an example for current data.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TRIO_ID = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_NUMBER_IN_REPORT = re.compile(r"(?<![\w<])\d[\d,]*\.?\d*\s*(?:%|USD|\$)|\$\s*\d")


class GoldenError(RuntimeError):
    """Controlled message; never contains database/provider exception text."""


class GoldenTrio(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=TRIO_ID, max_length=64)
    version: int = Field(ge=1)
    status: Literal["approved", "withdrawn"]
    question: str = Field(min_length=10, max_length=500)
    paraphrases: list[str] = Field(default_factory=list, max_length=5)
    interpretation: str = Field(min_length=20, max_length=1500)
    metric_definitions: list[str] = Field(min_length=1, max_length=8)
    grain: str = Field(min_length=3, max_length=200)
    sql: str = Field(min_length=20, max_length=6000)
    analyst_report: str = Field(min_length=50, max_length=3000)
    tags: list[str] = Field(min_length=1, max_length=10)
    provenance: Literal["synthetic-reviewed", "analyst-authored", "promoted-from-feedback"]
    reviewer: str = Field(min_length=2, max_length=100)
    approved_at: datetime
    supersedes: str | None = Field(default=None, pattern=TRIO_ID)

    @field_validator("analyst_report")
    @classmethod
    def report_uses_placeholders(cls, value: str) -> str:
        # Concrete money/percent values in an example invite the model to quote
        # stale numbers as current facts. Use <placeholders> instead.
        if _NUMBER_IN_REPORT.search(value):
            raise ValueError("analyst_report must use <placeholders>, not concrete money/percent values")
        return value

    @field_validator("tags")
    @classmethod
    def tags_are_slugs(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z0-9-]{2,40}", tag) for tag in value):
            raise ValueError("tags must be lowercase slugs")
        return value

    def search_text(self) -> str:
        """What retrieval embeds and matches: intent and method, not the SQL text."""
        return "\n".join([self.question, *self.paraphrases, self.interpretation, " ".join(self.tags)])

    def content_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def prompt_view(self, similarity: float) -> dict:
        """Bounded fields the planner sees; reviewer metadata stays out of the prompt."""
        return {
            "trio_id": self.id,
            "version": self.version,
            "similarity": round(similarity, 3),
            "question": self.question,
            "interpretation": self.interpretation,
            "metric_definitions": self.metric_definitions,
            "grain": self.grain,
            "reviewed_sql": self.sql,
            "report_example": self.analyst_report,
        }
