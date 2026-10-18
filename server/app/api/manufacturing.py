from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from typing import Annotated

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..domain.schemas import HoleFeature, ToleranceNote
from ..errors import not_found
from ..models import Approval, File, FileVersion, Intake, ProcessPlan, Quote, User
from ..security import current_user
from ..services import approvals, assets, manufacturing as mfg
from ..services.audit import audit
from ..services.status import part_status, plan_staleness, quote_staleness, stale_reasons
from .parts import load_part

router = APIRouter(tags=["manufacturing"])

# Overall size of a drawing-only part: strictly positive, sane upper bound.
PositiveMM = Annotated[float, Field(gt=0, le=10000)]


class PlanIn(BaseModel):
    model_revision_id: uuid.UUID | None = None
    intake_id: uuid.UUID | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class QuoteIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class ApprovalIn(BaseModel):
    subject_type: Literal["model_revision", "drawing", "intake", "process_plan", "quote"]
    subject_id: uuid.UUID
    stage: Literal["design", "process", "quote"]
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    expected_hash: str | None = None


class ManualIntakeIn(BaseModel):
    """Drawing-only path filled in by a person (no AI). Always an estimate."""

    file_version_id: uuid.UUID | None = None
    material: str | None = None
    bbox_mm: tuple[PositiveMM, PositiveMM, PositiveMM]
    volume_mm3: float | None = Field(default=None, gt=0)
    holes: list[HoleFeature] = Field(default_factory=list)
    fillets: int = Field(default=0, ge=0)
    chamfers: int = Field(default=0, ge=0)
    setup_directions: int = Field(default=1, ge=1, le=6)
    tolerances: list[ToleranceNote] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


def _plan_view(db: Session, row: ProcessPlan) -> dict:
    return {**mfg.plan_out(row), "staleness": plan_staleness(db, row),
            "approval": _approval(db, "process_plan", row.id, "process")}


def _quote_view(db: Session, row: Quote) -> dict:
    return {**mfg.quote_out(row), "staleness": quote_staleness(db, row),
            "approval": _approval(db, "quote", row.id, "quote")}


def _approval(db: Session, subject_type: str, subject_id: uuid.UUID, stage: str) -> dict | None:
    a = approvals.latest_decision(db, subject_type, subject_id, stage)
    if a is None:
        return None
    return {"decision": a.decision, "comment": a.comment, "role": a.role, "user_id": str(a.user_id) if a.user_id else None, "actor_kind": a.actor_kind, "agent_id": str(a.agent_id) if a.agent_id else None, "at": a.created_at.isoformat(),
            "valid": a.decision == "approved" and approvals.is_approved(db, subject_type, subject_id, stage)}


@router.get("/parts/{part_id}/status")
def status(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    part, _ = load_part(db, user, part_id)
    return part_status(db, part)


@router.post("/parts/{part_id}/intakes", status_code=201)
def create_manual_intake(part_id: uuid.UUID, body: ManualIntakeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    part, _ = load_part(db, user, part_id, "design")
    if body.file_version_id:
        fv = db.get(FileVersion, body.file_version_id)
        f = db.get(File, fv.file_id) if fv else None
        if f is None or f.project_id != part.project_id:
            raise not_found("file version")
    bx, by, bz = body.bbox_mm
    summary = {
        "source": "drawing_intake",
        "estimated": True,
        "material": body.material or part.material,
        "bbox_mm": [bx, by, bz],
        # Without a model the volume is an estimate; bbox volume x 0.7 unless given.
        "volume_mm3": body.volume_mm3 if body.volume_mm3 else round(bx * by * bz * 0.7, 3),
        "area_mm2": None,
        "holes": [h.model_dump() for h in body.holes],
        "fillets": body.fillets,
        "chamfers": body.chamfers,
        "setup_directions": body.setup_directions,
        "unsupported": body.unsupported,
        "tolerances": [t.model_dump() for t in body.tolerances],
    }
    row = Intake(part_id=part.id, file_version_id=body.file_version_id, summary=summary, extraction={"notes": body.notes, "volume_estimated": body.volume_mm3 is None}, method="manual", created_by=user.id)
    db.add(row)
    db.flush()
    assets.register_intake(db, row)  # intake asset, label estimated
    audit(db, action="intake.manual", subject_type="intake", subject_id=row.id, user_id=user.id, project_id=part.project_id)
    db.commit()
    return intake_out(db, row)


def intake_out(db: Session, row: Intake) -> dict:
    return {
        "id": str(row.id),
        "part_id": str(row.part_id),
        "file_version_id": str(row.file_version_id) if row.file_version_id else None,
        "summary": row.summary,
        "extraction": row.extraction,
        "method": row.method,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "approval": _approval(db, "intake", row.id, "design"),
    }


@router.get("/parts/{part_id}/intakes")
def list_intakes(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_part(db, user, part_id)
    rows = db.scalars(select(Intake).where(Intake.part_id == part_id).order_by(Intake.created_at.desc())).all()
    return [intake_out(db, r) for r in rows]


@router.post("/parts/{part_id}/process-plans", status_code=201)
def create_plan(part_id: uuid.UUID, body: PlanIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    part, _ = load_part(db, user, part_id, "process")
    row = mfg.create_plan(db, part=part, user=user, model_revision_id=body.model_revision_id, intake_id=body.intake_id, params=body.params)
    db.commit()
    return _plan_view(db, row)


@router.get("/parts/{part_id}/process-plans")
def list_plans(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_part(db, user, part_id)
    rows = db.scalars(select(ProcessPlan).where(ProcessPlan.part_id == part_id).order_by(ProcessPlan.created_at.desc())).all()
    return [_plan_view(db, r) for r in rows]


@router.get("/process-plans/{plan_id}")
def get_plan(plan_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ProcessPlan, plan_id)
    if row is None:
        raise not_found("process plan")
    load_part(db, user, row.part_id)
    return _plan_view(db, row)


@router.post("/process-plans/{plan_id}/quotes", status_code=201)
def create_quote(plan_id: uuid.UUID, body: QuoteIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ProcessPlan, plan_id)
    if row is None:
        raise not_found("process plan")
    load_part(db, user, row.part_id, "quote")
    q = mfg.create_quote(db, plan_row=row, user=user, params=body.params)
    db.commit()
    return _quote_view(db, q)


@router.get("/parts/{part_id}/quotes")
def list_quotes(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_part(db, user, part_id)
    rows = db.scalars(select(Quote).where(Quote.part_id == part_id).order_by(Quote.created_at.desc())).all()
    return [_quote_view(db, r) for r in rows]


@router.get("/quotes/{quote_id}")
def get_quote(quote_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(Quote, quote_id)
    if row is None:
        raise not_found("quote")
    load_part(db, user, row.part_id)
    return _quote_view(db, row)


@router.post("/approvals", status_code=201)
def decide(body: ApprovalIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = approvals.decide(
        db,
        user,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        stage=body.stage,
        decision=body.decision,
        comment=body.comment,
        expected_hash=body.expected_hash,
        stale_check=stale_reasons,
    )
    db.commit()
    return {"id": str(row.id), "decision": row.decision, "stage": row.stage, "subject_hash": row.subject_hash, "created_at": row.created_at.isoformat()}


@router.get("/parts/{part_id}/approvals")
def list_approvals(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_part(db, user, part_id)
    rows = db.scalars(select(Approval).where(Approval.part_id == part_id).order_by(Approval.created_at.desc())).all()
    return [
        {"id": str(a.id), "subject_type": a.subject_type, "subject_id": str(a.subject_id), "stage": a.stage, "decision": a.decision,
         "comment": a.comment, "role": a.role, "user_id": str(a.user_id) if a.user_id else None, "actor_kind": a.actor_kind, "agent_id": str(a.agent_id) if a.agent_id else None, "subject_hash": a.subject_hash, "created_at": a.created_at.isoformat(),
         "valid": a.decision == "approved" and approvals.is_approved(db, a.subject_type, a.subject_id, a.stage)}
        for a in rows
    ]
