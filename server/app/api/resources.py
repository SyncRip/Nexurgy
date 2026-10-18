"""Workflow resources: instance and project queries (design §4 C08, C10). Backend E.

Read-only: nothing here triggers AI, CAD or business writes. Enum, UUID and
paging problems are 422 (FastAPI query validation); a run or task of another
project is 404."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import not_found
from ..models import User, WorkflowRun
from ..security import current_user, project_access
from ..services import collaboration_resources as svc

router = APIRouter(tags=["resources"])

ResourceKind = Literal["input", "candidate", "formal", "failed", "superseded", "evidence"]
AssetType = Literal["part_model", "drawing", "process_plan", "quote", "document", "intake", "process_module",
                    "assembly_model", "assembly_drawing", "drawing_document", "engineering_spec", "delivery_manifest"]
Availability = Literal["building", "ready", "failed"]
PublishState = Literal["draft", "published", "deprecated"]
TaskStatus = Literal["todo", "in_progress", "in_review", "done", "cancelled"]


@router.get("/runs/{run_id}/resources")
def run_resources(
    run_id: uuid.UUID,
    node_id: str | None = Query(default=None, max_length=60),
    kind: ResourceKind | None = None,
    asset_type: AssetType | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """C08: every use relationship of one run → {items, total, limit, offset, facets}."""
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    project_access(db, user, run.project_id)
    return svc.run_resources(db, run, node_id=node_id, kind=kind, asset_type=asset_type, limit=limit, offset=offset)


@router.get("/projects/{project_id}/workflow-resources")
def project_resources(
    project_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=200),
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    node_id: str | None = Query(default=None, max_length=60),
    asset_version_id: uuid.UUID | None = None,
    kind: ResourceKind | None = None,
    asset_type: AssetType | None = None,
    availability: Availability | None = None,
    publish_state: PublishState | None = None,
    task_status: TaskStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """C10: project-wide resource query, one row per use relationship, sorted created_at DESC, id DESC.
    `facets.by_kind` ignores only the kind filter."""
    return svc.project_resources(
        db, user, project_id, q=q, run_id=run_id, task_id=task_id, node_id=node_id, asset_version_id=asset_version_id, kind=kind,
        asset_type=asset_type, availability=availability, publish_state=publish_state, task_status=task_status, limit=limit, offset=offset,
    )
