"""Schema of the model's `report` action and the library's shared limits."""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportOperation(StrEnum):
    SAVE = "save"
    SEARCH = "search"
    OPEN = "open"
    LIST = "list"
    PREPARE_DELETE = "prepare_delete"


class DeleteScope(StrEnum):
    MATCHING = "matching"  # Reports whose title or text contains the query.
    CONVERSATION = "conversation"  # Reports saved in this chat session.
    ALL = "all"
    ID = "id"


MAX_DELETE_IDS = 100  # One preview freezes at most this many reports.

_REPORT_ID = re.compile(r"[0-9a-f]{32}")

# Each valid (operation, scope) pair: the one filter field it needs (None: no
# filter) and the error if the filters differ. The other filter must be empty,
# and non-delete operations only accept the default scope.
_REQUEST_SHAPES = {
    (ReportOperation.SAVE, DeleteScope.MATCHING): (None, "save accepts a title only"),
    (ReportOperation.SEARCH, DeleteScope.MATCHING): ("query", "search requires query only"),
    (ReportOperation.OPEN, DeleteScope.MATCHING): ("report_id", "open requires report_id only"),
    (ReportOperation.LIST, DeleteScope.MATCHING): (None, "list accepts no filters"),
    (ReportOperation.PREPARE_DELETE, DeleteScope.MATCHING): ("query", "matching deletion requires query only"),
    (ReportOperation.PREPARE_DELETE, DeleteScope.ID): ("report_id", "ID deletion requires report_id only"),
    **dict.fromkeys(
        ((ReportOperation.PREPARE_DELETE, DeleteScope.ALL), (ReportOperation.PREPARE_DELETE, DeleteScope.CONVERSATION)),
        (None, "all/conversation deletion accepts no other filters"),
    ),
}
_FILTER_FIELDS = ("query", "report_id")


# Payload of the model's `report` action. One flat model on purpose: its JSON
# schema is what the model sees (a docstring would be added to it as a
# description), so per-operation rules live in `_REQUEST_SHAPES` instead of
# separate models. Literal values must equal the enums above (checked by a test).
class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["save", "search", "open", "list", "prepare_delete"]
    title: str = Field(default="", max_length=255)
    query: str = Field(
        max_length=1000,
        description="Nonempty search text for search or matching deletion. Empty string for all other operations.",
    )
    report_id: str = Field(default="", max_length=32)
    scope: Literal["matching", "conversation", "all", "id"] = "matching"

    @model_validator(mode="after")
    def check_request(self):
        if self.operation != ReportOperation.SAVE and self.title:
            raise ValueError("title is only used for save")
        if self.report_id and not _REPORT_ID.fullmatch(self.report_id):
            raise ValueError("report_id must be a lowercase hex ID")
        shape = _REQUEST_SHAPES.get((self.operation, self.scope))
        if shape is None:  # A deletion scope on a non-delete operation.
            raise ValueError(_REQUEST_SHAPES[(self.operation, DeleteScope.MATCHING)][1])
        required, message = shape
        for name in _FILTER_FIELDS:
            value = getattr(self, name)
            # A required filter must have content; any other must be exactly empty.
            present = bool(value.strip()) if name == required else bool(value)
            if present != (name == required):
                raise ValueError(message)
        return self


class ReportError(RuntimeError):
    """Fixed user-safe message; backend errors never become model feedback."""
