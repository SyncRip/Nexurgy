"""Reusable process modules (P4 contract §3.5): create, new versions, view,
and the engineering review request. Decisions go through the approval center;
publishing uses POST /asset-versions/{id}/publish (needs an approved review)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user, project_access
from ..services import approval_requests as req_svc
from ..services import modules

router = APIRouter(tags=["process-modules"])


class ReviewRequestIn(BaseModel):
    # Omitted = any member whose role may decide process reviews.
    assignee_id: uuid.UUID | None = None


@router.post("/projects/{project_id}/process-modules", status_code=201)
def create_module(project_id: uuid.UUID, body: modules.ModuleIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """First version of a new module → {module, version}. 422 module_incomplete (details.missing) when essentials are missing."""
    acc = project_access(db, user, project_id, "process")
    v = modules.create_module(db, acc, body)
    db.commit()
    return modules.module_version_out(db, v)


@router.post("/assets/{asset_id}/module-versions", status_code=201)
def add_module_version(asset_id: uuid.UUID, body: modules.ModuleIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Next immutable version of a module (parent = latest version) → {module, version}."""
    asset, acc = modules.load_module_asset(db, user, asset_id, "process")
    v = modules.add_version(db, acc, asset, body)
    db.commit()
    return modules.module_version_out(db, v)


@router.get("/process-module-versions/{version_id}")
def get_module_version(version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """{module, version} for an asset version id (a process module row id also works)."""
    v = modules.load_module_version(db, user, version_id)
    return modules.module_version_out(db, v)


@router.post("/asset-versions/{version_id}/request-review", status_code=201)
def request_review(version_id: uuid.UUID, body: ReviewRequestIn | None = None, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    """Process modules only: open an engineering (process) request on this exact
    version. 409 request_pending when one is already open."""
    req = modules.request_review(db, user, version_id, body.assignee_id if body else None)
    db.commit()
    return req_svc.request_detail(db, user, req)
