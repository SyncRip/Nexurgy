"""Knowledge collections, archive, index and reuse (design §4 C09). Backend E.

Collections are read with `view`, edited with `manage`; archive / reindex need
`workflow`. Using an item needs `view` on the source project and `workflow` on
the target project; confirming an adoption is reserved to the target node's
designated human reviewer."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..errors import not_found
from ..models import KnowledgeCollection, KnowledgeCollectionVersion, KnowledgeItem, Task, User, WorkflowRun
from ..security import current_user, project_access
from ..services import actors
from ..services import knowledge as svc
from ..services.tasks import SLOT_RE

router = APIRouter(tags=["knowledge"])


class CollectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=300)
    scope: dict[str, Any] | None = None
    status: Literal["active", "deprecated"] | None = None


class UseTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    node_id: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _one(self) -> "UseTarget":
        if self.task_id is None and not (self.run_id and self.node_id):
            raise ValueError("give task_id, or run_id and node_id")
        return self


class UseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usage: Literal["reference", "baseline"]
    target: UseTarget
    slot_key: str | None = Field(default=None, pattern=SLOT_RE)
    reason: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirmed", "rejected"]
    comment: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


@router.get("/projects/{project_id}/knowledge")
def list_collections(
    project_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=200),
    reusable: bool | None = None,
    status: Literal["active", "deprecated"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    return svc.list_collections(db, user, project_id, q=q, reusable=reusable, status=status, limit=limit, offset=offset)


@router.get("/knowledge/{collection_id}")
def get_collection(collection_id: uuid.UUID, version_id: uuid.UUID | None = None, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    col, _ = svc.load_collection(db, user, collection_id)
    return svc.collection_detail(db, user, col, version_id=version_id)


@router.patch("/knowledge/{collection_id}")
def patch_collection(collection_id: uuid.UUID, body: CollectionPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    col, acc = svc.load_collection(db, user, collection_id, "manage")
    col = svc.patch_collection(db, acc, col, expected_version=body.expected_version, title=body.title, scope=body.scope, status=body.status)
    db.commit()
    return svc.collection_out(db, col)


@router.post("/knowledge/{collection_id}/reindex")
def reindex(collection_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Queue a rebuild of the structured index (outbox `knowledge.index`); manifest, hashes and acceptance never change."""
    col, _ = svc.load_collection(db, user, collection_id, "workflow")
    col = svc.reindex(db, col, actor=actors.of_user(user))
    db.commit()
    return svc.collection_out(db, col)


@router.post("/runs/{run_id}/knowledge/archive")
def archive_run(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Idempotent manual archive (retry after a failed automatic archive). 409 run_not_completed otherwise."""
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    project_access(db, user, run.project_id, "workflow")
    ver = svc.archive(db, run.id, actor=actors.of_user(user))
    db.commit()
    col = db.get(KnowledgeCollection, ver.collection_id)
    assert col is not None
    return {**svc.collection_out(db, col), "archived_version": svc.version_brief(ver, svc._version_counts(db, [ver.id])[ver.id])}


@router.post("/knowledge/items/{item_id}/use", status_code=201)
def use_item(item_id: uuid.UUID, body: UseIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    item = db.get(KnowledgeItem, item_id)
    ver = db.get(KnowledgeCollectionVersion, item.collection_version_id) if item else None
    col = db.get(KnowledgeCollection, ver.collection_id) if ver else None
    if item is None or col is None:
        raise not_found("knowledge item")
    project_access(db, user, col.project_id)
    # The target must be writable by this person (workflow role) in its own project.
    if body.target.task_id is not None:
        task = db.get(Task, body.target.task_id)
        if task is None:
            raise not_found("task")
        target_project = task.project_id
    else:
        run = db.get(WorkflowRun, body.target.run_id)
        if run is None:
            raise not_found("run")
        target_project = run.project_id
    project_access(db, user, target_project, "workflow")
    u = svc.use_item(db, item_id=item.id, usage=body.usage, target=body.target.model_dump(), actor=actors.of_user(user), reason=body.reason,
                     slot_key=body.slot_key)
    db.commit()
    return svc.usage_out(db, u, collection_id=col.id)


@router.post("/knowledge-usages/{usage_id}/confirm")
def confirm_usage(usage_id: uuid.UUID, body: ConfirmIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    u = svc.confirm_usage(db, usage_id=usage_id, user=user, decision=body.decision, comment=body.comment)
    db.commit()
    ver = db.get(KnowledgeCollectionVersion, u.collection_version_id)
    return svc.usage_out(db, u, collection_id=ver.collection_id if ver else None)
