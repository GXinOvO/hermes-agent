from typing import Literal

from pydantic import Field

from .base import JsonValue, Result
from .canonical_sessions import CanonicalSessionParams, ExpectedSessionOwner
from .sessions import TodoState
from .registry import canonical_method


class TaskOverviewParams(CanonicalSessionParams):
    stored_id: str
    expected_owner: ExpectedSessionOwner


class OmittedTaskOverviewCall(Result):
    type: Literal['omitted']


class TaskOverviewMessage(Result):
    role: Literal['user', 'assistant', 'tool']
    row_id: int
    content: str | None = None
    timestamp: float | None = None
    display_kind: str | None = None
    compressed_summary: bool | None = Field(default=None, alias='_compressed_summary')
    name: str | None = None
    tool_calls: list[OmittedTaskOverviewCall] | None = None


class TaskOverviewResult(Result):
    version: Literal[1]
    session_id: str
    stored_session_id: str
    resolved_id: str
    owner: ExpectedSessionOwner
    authority_epoch: int
    snapshot_scope: Literal['persisted_display']
    fetched_at: int
    complete: Literal[True]
    conversation_revision: JsonValue
    messages: list[TaskOverviewMessage]
    todo_state: TodoState | None
    verification: dict[str, JsonValue] | None
    verification_cwd: str | None
    verification_error: str | None


canonical_method('session.task_overview', params=TaskOverviewParams, result=TaskOverviewResult)
