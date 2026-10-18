from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import not_found
from ..models import Drawing, ModelRevision, User
from ..security import current_user
from ..services import drawings as svc
from ..services import execution_policy as policy_svc
from .parts import load_part

router = APIRouter(tags=["drawings"])


class DrawingIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


@router.post("/revisions/{revision_id}/drawings", status_code=201)
def generate(revision_id: uuid.UUID, body: DrawingIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev = db.get(ModelRevision, revision_id)
    if rev is None:
        raise not_found("revision")
    part, _ = load_part(db, user, rev.part_id, "design")
    choice = policy_svc.resolve(db, part.project_id, None, source=rev)
    d = svc.generate(db, rev=rev, user=user, config=body.config, policy=choice.policy)
    policy_svc.record(db, user, part.project_id, choice, subject_type="drawing", subject_id=d.id, action="drawing.generate")
    db.commit()
    return svc.drawing_out(d, rev)


@router.get("/parts/{part_id}/drawings")
def list_drawings(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_part(db, user, part_id)
    rows = db.scalars(select(Drawing).where(Drawing.part_id == part_id).order_by(Drawing.created_at.desc())).all()
    revs = {r.id: r for r in db.scalars(select(ModelRevision).where(ModelRevision.part_id == part_id)).all()}
    return [svc.drawing_out(d, revs.get(d.model_revision_id)) for d in rows]


def _readable(db: Session, user: User, drawing_id: uuid.UUID) -> Drawing:
    d = db.get(Drawing, drawing_id)
    if d is None:
        raise not_found("drawing")
    load_part(db, user, d.part_id)  # non-members: 404 before anything is read
    return d


@router.get("/drawings/{drawing_id}")
def get_drawing(drawing_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    d = _readable(db, user, drawing_id)
    rev = db.get(ModelRevision, d.model_revision_id) if d.model_revision_id else None
    return svc.drawing_out(d, rev)


@router.get("/drawings/{drawing_id}/package.zip")
def download_package(drawing_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """The whole package: every manifest file of every page, the check reports and manifest.json
    (canonical JSON whose sha256 is the drawing's manifest_sha256)."""
    d = _readable(db, user, drawing_id)
    data = svc.package_zip(db, d)
    name = svc.package_folder(db, d)
    headers = {"Content-Disposition": f'attachment; filename="{name}_package.zip"', "Cache-Control": "private, no-store"}
    if d.manifest_sha256:
        headers["X-Manifest-SHA256"] = d.manifest_sha256
    return Response(content=data, media_type="application/zip", headers=headers)


class CheckIn(BaseModel):
    """A later standard check on this exact drawing package (W7, AC-T08)."""

    check: Literal["drawing.annotation", "drawing.dwg_semantic"] = "drawing.annotation"


class ConfirmIn(BaseModel):
    """A person's decision on one AI / rule assumption (W0 §2.2); see ``POST /revisions/{id}/assumptions/{aid}/confirm``."""

    value: Any = None
    note: str = Field(default="", max_length=1000)


@router.get("/drawings/{drawing_id}/checks")
def list_drawing_checks(drawing_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The package's verification, its later check records and its own assumption decisions, oldest first."""
    from ..services import verification

    d = _readable(db, user, drawing_id)
    return {"verification": verification.drawing_verification(d, db), "records": verification.list_checks(db, drawing=d),
            "confirmations": verification.list_checks(db, drawing=d, confirmations=True)}


@router.post("/drawings/{drawing_id}/assumptions/{assumption_id}/confirm", status_code=201)
def confirm_drawing_assumption(drawing_id: uuid.UUID, assumption_id: str, body: ConfirmIn, user: User = Depends(current_user),
                               db: Session = Depends(get_db)) -> dict:
    """Confirm assumptions from a drawing (design permission): an assumption of the package itself is decided on the package; one
    of its source revision (listed with ``owner`` model_revision) is decided on that revision, where it belongs."""
    from ..services import verification

    d = db.get(Drawing, drawing_id)
    if d is None:
        raise not_found("drawing")
    load_part(db, user, d.part_id, "design")
    kwargs: dict[str, Any] = {"value": body.value} if "value" in body.model_fields_set else {}
    own = {str(a.get("id")) for a in (d.checks or {}).get("assumptions") or [] if isinstance(a, dict) and not a.get("owner")}
    rev = db.get(ModelRevision, d.model_revision_id) if d.model_revision_id else None
    if assumption_id not in own and rev is not None:
        row = verification.confirm_assumption(db, user, revision=rev, assumption_id=assumption_id, note=body.note, **kwargs)
    else:
        row = verification.confirm_assumption(db, user, drawing=d, assumption_id=assumption_id, note=body.note, **kwargs)
    db.commit()
    return {"record": verification.check_out(row), "verification": verification.drawing_verification(d, db)}


@router.post("/drawings/{drawing_id}/checks", status_code=201)
def run_drawing_check(drawing_id: uuid.UUID, body: CheckIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Run a standard drawing check (annotation against the source revision, or the DWG semantic readback) on the
    package's exact files and append the result; the creation record is never rewritten."""
    from ..services import verification

    d = db.get(Drawing, drawing_id)
    if d is None:
        raise not_found("drawing")
    load_part(db, user, d.part_id, "design")
    row = verification.recheck_drawing(db, user, d, body.check)
    db.commit()
    return {"record": verification.check_out(row), "verification": verification.drawing_verification(d, db)}
