from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import not_found
from ..models import ModelRevision, User
from ..security import current_user
from ..services import revisions as svc
from .parts import load_part

router = APIRouter(tags=["models"])


class SpecIn(BaseModel):
    base: dict[str, Any] | None = None
    ops: list[dict[str, Any]] = Field(default_factory=list)


class PreviewIn(SpecIn):
    part_id: uuid.UUID


class SaveIn(SpecIn):
    parent_id: uuid.UUID | None = None
    note: str = Field(default="", max_length=2000)


class DxfProfileIn(BaseModel):
    """Strict PROFILE import of a DXF / DWG (cad/profile_import.py): no defaults for thickness or scale (N01/N02)."""

    model_config = ConfigDict(extra="forbid")
    layer: str = Field(default="PROFILE", min_length=1, max_length=255)
    thickness_mm: float | None = None
    scale: float | None = None  # part_mm = drawing_units × scale (a 1:2 drawing needs 2)
    base_z: float = 0.0
    units: Literal["mm"] | None = None  # declares the unit of a unitless file (only mm)


class ImportIn(BaseModel):
    file_version_id: uuid.UUID
    dxf_profile: DxfProfileIn | None = None


def _load_revision(db: Session, user: User, revision_id: uuid.UUID, action: str = "view") -> tuple[ModelRevision, Any]:
    rev = db.get(ModelRevision, revision_id)
    if rev is None:
        raise not_found("revision")
    part, _ = load_part(db, user, rev.part_id, action)
    return rev, part


@router.get("/parts/{part_id}/revisions")
def list_revisions(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    part, _ = load_part(db, user, part_id)
    revs = db.scalars(select(ModelRevision).where(ModelRevision.part_id == part_id).order_by(ModelRevision.rev_no.desc())).all()
    return [svc.revision_out(r, part) for r in revs]


@router.get("/revisions/{revision_id}")
def get_revision(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, part = _load_revision(db, user, revision_id)
    return svc.revision_out(rev, part)


@router.post("/cad/preview")
def preview(body: PreviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Build without saving (editor live preview). Same kernel and validation as save."""
    part, _ = load_part(db, user, body.part_id, "design")
    spec = svc.parse_spec({"base": body.base, "ops": body.ops})
    svc.authorize_base(db, part.project_id, spec)  # a base is read only when it is a STEP of this project
    return svc.preview(db, spec)


@router.post("/parts/{part_id}/revisions", status_code=201)
def save_revision(part_id: uuid.UUID, body: SaveIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Manual save from the simplified editor (no LLM involved)."""
    part, _ = load_part(db, user, part_id, "design")
    spec = svc.parse_spec({"base": body.base, "ops": body.ops})
    svc.authorize_base(db, part.project_id, spec)
    rev = svc.create_revision(db, part_id=part.id, user=user, spec=spec, source="manual", parent_id=body.parent_id, note=body.note)
    db.commit()
    return svc.revision_out(rev, part)


@router.post("/parts/{part_id}/revisions/import", status_code=201)
def import_revision(part_id: uuid.UUID, body: ImportIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """File-only path: STEP → editable base; STL/OBJ/GLB → mesh reference; DXF / DWG with ``dxf_profile`` → plate from the
    PROFILE layer (without it 422 drawing_needs_reconstruct). No AI."""
    part, _ = load_part(db, user, part_id, "design")
    rev = svc.import_file_version(db, part_id=part.id, user=user, file_version_id=body.file_version_id,
                                  dxf_profile=body.dxf_profile.model_dump() if body.dxf_profile else None)
    db.commit()
    return svc.revision_out(rev, part)


# ---- W3 geometry library: part templates and datums -------------------------------------------------------------


class TemplateParamsIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class TemplateRevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    parent_id: uuid.UUID | None = None
    mode: Literal["replace", "append"] = "replace"
    note: str = Field(default="", max_length=2000)


@router.get("/cad/templates")
def list_templates(user: User = Depends(current_user)) -> dict:
    """Part template catalog: typed parameters (unit, range, default), representation, procurement, ports."""
    from ..cad.templates import catalog

    return {"templates": catalog()}


@router.post("/cad/templates/{template_id}/instantiate")
def instantiate_template(template_id: str, body: TemplateParamsIn, user: User = Depends(current_user)) -> dict:
    """The op list and metadata of one template instance (nothing is saved); invalid parameters are 422 with
    ``details.problems``."""
    spec, meta = svc.instantiate_template(template_id, body.params)
    return {"ops": [o.model_dump(mode="json") for o in spec.ops], "meta": meta}


@router.post("/parts/{part_id}/revisions/template", status_code=201)
def save_template_revision(part_id: uuid.UUID, body: TemplateRevisionIn, user: User = Depends(current_user),
                           db: Session = Depends(get_db)) -> dict:
    """New revision from a part template (replace the design, or append to the parent's ops)."""
    part, _ = load_part(db, user, part_id, "design")
    rev = svc.create_template_revision(db, part_id=part.id, user=user, template_id=body.template_id, params=body.params,
                                       parent_id=body.parent_id, mode=body.mode, note=body.note)
    db.commit()
    return svc.revision_out(rev, part)


@router.get("/revisions/{revision_id}/datums")
def revision_datums(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Semantic datums of the revision (origin, ``<op>.<side>``, ``<op>.axis[i]``, template ``port.<name>``)."""
    rev, _ = _load_revision(db, user, revision_id)
    if rev.mesh_only:
        return {"datums": []}
    tpl = (rev.checks or {}).get("template") if isinstance(rev.checks, dict) else None
    return {"datums": svc.datums_of(svc.spec_of(rev), template=tpl)}


@router.get("/revisions/{revision_id}/diff")
def diff(revision_id: uuid.UUID, against: uuid.UUID | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, _ = _load_revision(db, user, revision_id)
    other_id = against or rev.parent_id
    if other_id is None:
        return {"from": None, "to": str(rev.id), "ops": [], "metrics": {}}
    other = db.get(ModelRevision, other_id)
    if other is None or other.part_id != rev.part_id:
        raise not_found("revision to compare")
    return svc.diff(other, rev)


class CheckIn(BaseModel):
    """A later standard check on this exact revision (W7, AC-T08)."""

    model_config = ConfigDict(extra="forbid")
    check: Literal["model.conformance", "geometry.features"] = "model.conformance"


class ConfirmIn(BaseModel):
    """A person's decision on one AI / rule assumption (W0 §2.2). Leave ``value`` out to confirm the adopted value;
    a different value is recorded as a correction (the change itself is a new revision)."""

    model_config = ConfigDict(extra="forbid")
    value: Any = None
    note: str = Field(default="", max_length=1000)


@router.get("/revisions/{revision_id}/checks")
def list_revision_checks(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The revision's verification, its later check records and its assumption decisions, oldest first."""
    from ..services import verification

    rev, _ = _load_revision(db, user, revision_id)
    return {"verification": verification.revision_verification(rev, db), "records": verification.list_checks(db, revision=rev),
            "confirmations": verification.list_checks(db, revision=rev, confirmations=True)}


@router.post("/revisions/{revision_id}/checks", status_code=201)
def run_revision_check(revision_id: uuid.UUID, body: CheckIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Run a standard check on the exact revision and append the result (design permission): ``model.conformance``
    on the stored STEP bytes, or ``geometry.features`` (one record per feature kind) on a rebuild of its ops. The
    record made when the revision was created (e.g. ``skipped_turbo``) is never rewritten; the revision stays
    unverified until every skipped check passed and a person confirmed every assumption it carries."""
    from ..services import verification

    rev, _ = _load_revision(db, user, revision_id, "design")
    rows = verification.recheck_revision(db, user, rev, body.check)
    db.commit()
    return {"record": verification.check_out(rows[0]), "records": [verification.check_out(r) for r in rows],
            "verification": verification.revision_verification(rev, db)}


@router.post("/revisions/{revision_id}/assumptions/{assumption_id}/confirm", status_code=201)
def confirm_revision_assumption(revision_id: uuid.UUID, assumption_id: str, body: ConfirmIn, user: User = Depends(current_user),
                                db: Session = Depends(get_db)) -> dict:
    """Confirm assumptions: a person's confirmation (or correction) of one AI / rule assumption of this revision, appended as a
    record with who, when and the value (design permission). A correction keeps the revision unverified."""
    from ..services import verification

    rev, _ = _load_revision(db, user, revision_id, "design")
    kwargs = {"value": body.value} if "value" in body.model_fields_set else {}
    row = verification.confirm_assumption(db, user, revision=rev, assumption_id=assumption_id, note=body.note, **kwargs)
    db.commit()
    return {"record": verification.check_out(row), "verification": verification.revision_verification(rev, db)}
