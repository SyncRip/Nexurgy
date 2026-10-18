"""Approval center: one inbox for engineering reviews and task acceptance (contract §4, backend B)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..errors import not_found
from ..models import ApprovalRequest, User
from ..security import current_user, project_access
from ..services import approval_requests as svc

router = APIRouter(tags=["approvals"])


class CriterionDecisionIn(BaseModel):
    """One criterion's final result (design §4 C05)."""

    model_config = ConfigDict(extra="forbid")
    criterion_id: uuid.UUID
    result: Literal["pass", "fail", "unknown"]
    reason: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)
    evidence_asset_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class RequestDecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    # The target hash the reviewer saw (request.target_hash); required.
    expected_hash: str = Field(min_length=1, max_length=64)
    # Required for collaboration node acceptance (every required criterion); optional elsewhere.
    criterion_decisions: list[CriterionDecisionIn] | None = Field(default=None, max_length=50)


class ReassignIn(BaseModel):
    assignee_id: uuid.UUID


SubjectType = Literal["model_revision", "drawing", "intake", "process_plan", "quote"]
Stage = Literal["design", "process", "quote"]


class PartRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: SubjectType
    subject_id: uuid.UUID
    stage: Stage
    expected_hash: str = Field(pattern="^[0-9a-f]{64}$")
    assignee_id: uuid.UUID | None = None


@router.get("/parts/{part_id}/approval-subject")
def approval_subject(part_id: uuid.UUID, subject_type: SubjectType, subject_id: uuid.UUID, stage: Stage,
                     user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return svc.subject_out(db, user, part_id, subject_type, subject_id, stage)


@router.post("/parts/{part_id}/approval-requests", status_code=201)
def create_part_request(part_id: uuid.UUID, body: PartRequestIn, response: Response,
                        user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    req, reused = svc.create_part_request(db, user, part_id, **body.model_dump(),
                                          assignee_supplied="assignee_id" in body.model_fields_set)
    db.commit()
    response.status_code = 200 if reused else 201
    return {**svc.request_detail(db, user, req), "reused": reused}


@router.get("/parts/{part_id}/approval-requests")
def part_requests(part_id: uuid.UUID, subject_type: SubjectType | None = None, subject_id: uuid.UUID | None = None,
                  stage: Stage | None = None, status: str | None = "pending",
                  user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [svc.request_out(db, user, req) for req in svc.part_requests(
        db, user, part_id, subject_type=subject_type, subject_id=subject_id, stage=stage, status=status)]


@router.get("/approval-requests")
def list_requests(
    scope: Literal["mine", "created", "all"] = "mine",
    project_id: uuid.UUID | None = None,
    status: str | None = "pending",
    kind: Literal["engineering", "task_acceptance", "milestone_review"] | None = None,
    task_id: uuid.UUID | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = svc.list_requests(db, user, scope=scope, project_id=project_id, status=status, kind=kind, task_id=task_id, limit=limit)
    return [svc.request_out(db, user, r) for r in rows]


@router.get("/approval-requests/{request_id}")
def get_request(request_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    req = db.get(ApprovalRequest, request_id)
    if req is None:
        raise not_found("approval request")
    project_access(db, user, req.project_id)
    return svc.request_detail(db, user, req)


@router.post("/approval-requests/{request_id}/decisions")
def decide(request_id: uuid.UUID, body: RequestDecisionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Idempotent: repeating the decision that closed the request returns it unchanged; another decision is 409 request_closed."""
    pre = db.get(ApprovalRequest, request_id)
    was_closed = pre is not None and pre.status != "pending"
    crit = [c.model_dump(mode="json") for c in body.criterion_decisions] if body.criterion_decisions is not None else None
    req = svc.decide(db, user, request_id, decision=body.decision, comment=body.comment, expected_hash=body.expected_hash, criterion_decisions=crit)
    db.commit()
    # `repeated` tells the client this call changed nothing (the request was already closed this way).
    return {**svc.request_detail(db, user, req), "repeated": was_closed}


@router.post("/approval-requests/{request_id}/reassign")
def reassign(request_id: uuid.UUID, body: ReassignIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    req = svc.reassign(db, user, request_id, body.assignee_id)
    db.commit()
    return svc.request_detail(db, user, req)
