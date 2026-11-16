from typing import Literal

from pydantic import StrictInt

from .base import JsonValue, Result
from .canonical_sessions import CanonicalSessionParams, ExpectedSessionOwner
from .registry import canonical_method


class TaskSourceParams(CanonicalSessionParams):
    expected_owner: ExpectedSessionOwner | None = None


class TaskSourceResult(Result):
    session_id: str
    stored_session_id: str
    resolved_id: str
    owner: ExpectedSessionOwner
    authority_epoch: int


canonical_method('session.task_source', params=TaskSourceParams, result=TaskSourceResult)


class TaskAnalysisParams(CanonicalSessionParams):
    stored_id: str
    expected_owner: ExpectedSessionOwner
    task_id: str
    title: str
    refresh: bool = False


class TaskAnalysisEvidence(Result):
    row_id: StrictInt
    quote: str


class TaskAnalysisClaim(Result):
    text: str
    evidence: list[TaskAnalysisEvidence]


class TaskAnalysis(Result):
    version: Literal[1]
    title: str
    analyzed_at: StrictInt
    conversation_revision: JsonValue
    stale: bool
    scope: Literal['single_conversation']
    message_count: StrictInt
    analyzed_messages: StrictInt
    limitations: list[str]
    status: Literal['todo', 'planning', 'in_progress', 'waiting', 'failed', 'awaiting_acceptance']
    goal: TaskAnalysisClaim | None
    completed: list[TaskAnalysisClaim]
    remaining: list[TaskAnalysisClaim]
    needs_user: list[TaskAnalysisClaim]


class TaskAnalysisResult(Result):
    session_id: str
    stored_session_id: str
    owner: ExpectedSessionOwner
    authority_epoch: int
    task_id: str
    analysis: TaskAnalysis | None


canonical_method('session.task_analysis', params=TaskAnalysisParams, result=TaskAnalysisResult)
