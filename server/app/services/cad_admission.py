"""Shared admission for CAD work: one live writer per part across assistant and workflow entries."""
from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..errors import AppError, bad_request, not_found
from ..models import AiOperation, NodeRun, Part, User, WorkflowRun
from ..security import project_access
from .lifecycle import require_active
from .revisions import lock_part

WRITE_NODES = frozenset({"model_3d", "drawing_reconstruct", "drawing", "drawing_intake"})
WRITE_OPERATIONS = frozenset({"model_create", "model_modify", "drawing_generate", "drawing_reconstruct", "drawing_intake"})
TERMINAL_RUNS = ("succeeded", "partial", "failed", "cancelled")


def check_free(db: Session, user: User, part_id: uuid.UUID, *, exclude_operation_id: uuid.UUID | None = None,
               exclude_run_id: uuid.UUID | None = None) -> None:
    """Caller holds the part lock. Read busy rows without locking operations in reverse order.

    A terminal run releases admission even before its operation has been polled. Conversely, a revived
    run keeps admission despite an old operation's terminal status. Private conversation ids never leak.
    """
    operations = (select(AiOperation).outerjoin(WorkflowRun, WorkflowRun.id == AiOperation.run_id)
                  .where(AiOperation.part_id == part_id, AiOperation.kind.in_(WRITE_OPERATIONS),
                         AiOperation.status.in_(("pending", "running", "waiting_input")),
                         or_(AiOperation.run_id.is_(None), WorkflowRun.status.not_in(TERMINAL_RUNS)))
                  .order_by(AiOperation.id))
    if exclude_operation_id is not None:
        operations = operations.where(AiOperation.id != exclude_operation_id)
    if exclude_run_id is not None:
        operations = operations.where(or_(AiOperation.run_id.is_(None), AiOperation.run_id != exclude_run_id))
    operation = db.scalar(operations.limit(1))
    details: dict = {}
    if operation is not None:
        details.update(kind=operation.kind, status=operation.status)
        if operation.user_id == user.id:
            details.update(operation_id=str(operation.id), conversation_id=str(operation.conversation_id))
    else:
        runs = (select(WorkflowRun).where(WorkflowRun.part_id == part_id, WorkflowRun.status.not_in(TERMINAL_RUNS),
                  select(NodeRun.id).where(NodeRun.run_id == WorkflowRun.id, NodeRun.node_type.in_(WRITE_NODES)).exists())
                .order_by(WorkflowRun.id))
        if exclude_run_id is not None:
            runs = runs.where(WorkflowRun.id != exclude_run_id)
        run = db.scalar(runs.limit(1))
        if run is None:
            return
        details.update(run_id=str(run.id), status=run.status)
    raise AppError(409, "part_busy", "another CAD operation on this part is still in progress; wait for it or stop it", details)


def admit_part(db: Session, user: User, part_id: uuid.UUID, project_id: uuid.UUID, *,
               exclude_operation_id: uuid.UUID | None = None, exclude_run_id: uuid.UUID | None = None) -> Part:
    """Permission/lifecycle, project share lock, part lock, then the shared busy predicate. Caller commits."""
    project_access(db, user, project_id, "design")
    require_active(db, project_id, what="starting CAD work")
    part = lock_part(db, part_id)
    if part.project_id != project_id:
        raise not_found("part")
    if part.archived_at is not None:
        raise bad_request("part_archived", "the part is archived")
    check_free(db, user, part.id, exclude_operation_id=exclude_operation_id, exclude_run_id=exclude_run_id)
    return part
