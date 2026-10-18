"""Collaboration node actions and run control (design §4 C04, C06).

Thin router: permission and rules live in services/agent_runtime.py; the
responses reuse backend C's projections (node_task_out / collaboration_out).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import agent_runtime

router = APIRouter(tags=["collaboration"])


class TaskActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "add_deliverable", "check", "submit", "withdraw", "reopen", "cancel", "adopt_baseline"]
    # node_task.version the user saw
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    note: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    # add_deliverable
    asset_version_id: uuid.UUID | None = None
    criterion_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    slot_key: str | None = Field(default=None, max_length=80)
    # adopt_baseline: {slot_key: asset_version_id | null}
    items: dict[str, uuid.UUID | None] | None = None


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ForkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reviewer_user_id: uuid.UUID
    idempotency_key: str = Field(min_length=8, max_length=120)
    reason: str = Field(default="", max_length=2000)


@router.post("/runs/{run_id}/nodes/{node_id}/task-action")
def task_action(run_id: uuid.UUID, node_id: str, body: TaskActionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """C04: one node action; returns node_task_out plus {"action": {...}}."""
    out = agent_runtime.task_action(db, user, run_id, node_id, body.model_dump(mode="json"))
    db.commit()
    return out


class AnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(default="", max_length=2000)
    answer: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)


class AnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[AnswerItem] = Field(min_length=1, max_length=agent_runtime.MAX_ANSWERS)
    expected_version: int | None = Field(default=None, ge=1)


@router.post("/runs/{run_id}/nodes/{node_id}/answers")
def answer(run_id: uuid.UUID, node_id: str, body: AnswersIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Answers to the questions an agent was blocked on (``blocked_reason.questions``); the node is queued again and
    its next attempt (state and CAD tools) receives them. Same rights as resume."""
    out = agent_runtime.answer_questions(db, user, run_id, node_id, answers=[a.model_dump() for a in body.answers],
                                         expected_version=body.expected_version)
    db.commit()
    return out


@router.post("/runs/{run_id}/collaboration/pause")
def pause(run_id: uuid.UUID, body: PauseIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run, collab = agent_runtime.pause_run(db, user, run_id, expected_version=body.expected_version, reason=body.reason)
    db.commit()
    return agent_runtime.collaboration_view(db, user, run, collab)


@router.post("/runs/{run_id}/collaboration/resume")
def resume(run_id: uuid.UUID, body: ResumeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run, collab = agent_runtime.resume_run(db, user, run_id, expected_version=body.expected_version)
    db.commit()
    return agent_runtime.collaboration_view(db, user, run, collab)


@router.post("/runs/{run_id}/collaboration/fork-semi-auto", status_code=201)
def fork_semi_auto(run_id: uuid.UUID, body: ForkIn, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """C06 / A34: pause the full-auto run and start a semi-auto instance (candidates only).
    Replaying the same idempotency key with the same request returns the existing fork (200)."""
    run, collab, created = agent_runtime.fork_semi_auto(db, user, run_id, expected_version=body.expected_version,
                                                        reviewer_user_id=body.reviewer_user_id, idempotency_key=body.idempotency_key,
                                                        reason=body.reason)
    db.commit()
    if not created:
        response.status_code = 200
    return agent_runtime.collaboration_view(db, user, run, collab)
