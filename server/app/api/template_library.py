"""Public template library and project import / upgrade (contract §2.8)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..models import User
from ..security import current_user, project_access
from ..services import template_library as lib
from .workflows import _wf_out

router = APIRouter(tags=["template-library"])


class ShareIn(BaseModel):
    template_version_id: uuid.UUID
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=limits.DESCRIPTION_CHARS)


class PublicVersionIn(BaseModel):
    template_version_id: uuid.UUID
    notes: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class ImportIn(BaseModel):
    public_template_version_id: uuid.UUID
    name: str | None = Field(default=None, max_length=200)


class UpgradeIn(BaseModel):
    public_template_version_id: uuid.UUID
    expected_version: int | None = None


@router.get("/template-library")
def list_library(q: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Every signed-in user can browse the library."""
    return lib.list_templates(db, user, q)


@router.get("/template-library/{public_template_id}")
def get_library_template(public_template_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return lib.template_detail(db, user, lib.load_template(db, public_template_id))


@router.post("/template-library", status_code=201)
def share_template(body: ShareIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Share a project's published version as a new public template (needs the project's `template` action)."""
    pt = lib.share(db, user, template_version_id=body.template_version_id, name=body.name, description=body.description)
    db.commit()
    return lib.template_detail(db, user, pt)


@router.post("/template-library/{public_template_id}/versions", status_code=201)
def add_library_version(public_template_id: uuid.UUID, body: PublicVersionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    lib.add_version(db, user, public_template_id, template_version_id=body.template_version_id, notes=body.notes)
    db.commit()
    return lib.template_detail(db, user, lib.load_template(db, public_template_id))


@router.post("/template-library/versions/{public_version_id}/deprecate")
def deprecate_library_version(public_version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    pv = lib.deprecate_version(db, user, public_version_id)
    db.commit()
    return lib.public_version_out(db, pv)


@router.post("/projects/{project_id}/workflows/import", status_code=201)
def import_template(project_id: uuid.UUID, body: ImportIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """New project template from a public version, published as project v1."""
    w = lib.import_version(db, user, project_id, public_version_id=body.public_template_version_id, name=body.name)
    db.commit()
    return _wf_out(db, w, project_access(db, user, project_id))


@router.post("/workflows/{workflow_id}/upgrade")
def upgrade_template(workflow_id: uuid.UUID, body: UpgradeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Explicit upgrade to a newer public version; old project versions and runs stay unchanged."""
    w = lib.upgrade(db, user, workflow_id, public_version_id=body.public_template_version_id, expected_version=body.expected_version)
    db.commit()
    return _wf_out(db, w, project_access(db, user, w.project_id))
