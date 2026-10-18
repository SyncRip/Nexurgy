"""Stage readiness and stage reviews (P4 contract §3.2). Decisions go through
the approval center (POST /approval-requests/{id}/decisions, kind milestone_review)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..models import MilestoneReview, User
from ..security import current_user
from ..services import stages

router = APIRouter(tags=["milestone-reviews"])


class ReviewIn(BaseModel):
    reviewer_id: uuid.UUID
    note: str = Field(default="", max_length=limits.LONG_NOTE_CHARS)
    extra_asset_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    # The milestone's version the submitter saw.
    expected_version: int


class WithdrawIn(BaseModel):
    reason: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


@router.get("/milestones/{milestone_id}/readiness")
def readiness(
    milestone_id: uuid.UUID,
    extra_asset_version_ids: list[uuid.UUID] = Query(default_factory=list),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """{ready, blockers, tasks, suggested_items, checks}: whether a stage review can start now.
    Optional extra_asset_version_ids are evaluated as extra stage deliverables."""
    m, acc = stages.load_milestone(db, user, milestone_id)
    extras = stages.resolve_extras(db, acc.project.id, extra_asset_version_ids)
    return stages.public_readiness(stages.readiness(db, m, extras=extras))


@router.get("/milestones/{milestone_id}/reviews")
def list_reviews(milestone_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    m, _ = stages.load_milestone(db, user, milestone_id)
    rows = db.scalars(select(MilestoneReview).where(MilestoneReview.milestone_id == m.id).order_by(MilestoneReview.seq.desc())).all()
    return [stages.review_out(db, r) for r in rows]


@router.post("/milestones/{milestone_id}/reviews", status_code=201)
def create_review(milestone_id: uuid.UUID, body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Freeze the stage's accepted submissions (plus extras) and ask the reviewer.
    409 stage_not_ready with details.blockers when the guard fails."""
    _, acc = stages.load_milestone(db, user, milestone_id, "manage")
    r = stages.create_review(db, acc, milestone_id, reviewer_id=body.reviewer_id, note=body.note, extra_ids=body.extra_asset_version_ids,
                             expected_version=body.expected_version)
    db.commit()
    return stages.review_out(db, r, detail=True, user=user)


@router.get("/milestone-reviews/{review_id}")
def get_review(review_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    r, _ = stages.load_review(db, user, review_id)
    out = stages.review_out(db, r, detail=True, user=user)
    if r.status == "pending":
        out["readiness"] = stages.pending_readiness(db, r)
    return out


@router.post("/milestone-reviews/{review_id}/withdraw")
def withdraw_review(review_id: uuid.UUID, body: WithdrawIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Submitter or project owner; closes the request (withdrawn) and reopens the stage."""
    r = stages.withdraw_review(db, user, review_id, body.reason)
    db.commit()
    return stages.review_out(db, r, detail=True, user=user)
