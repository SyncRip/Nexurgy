"""Collaboration planning, drafts, start and projections (design §4 C01–C03).

Thin router: permission → services.collaboration → dict. Planning and
starting need the `workflow` action; reading needs `view`. A 503
`ai_unavailable` from planning still commits the draft (the demand is kept).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import AppError, not_found
from ..models import User
from ..security import current_user, project_access
from ..services import collaboration as svc
from ..services.collaboration import DraftPatchIn, ExecutionMode, PlanIn, StartIn

router = APIRouter(tags=["collaboration"])


@router.post("/projects/{project_id}/collaboration/plan")
def plan(project_id: uuid.UUID, body: PlanIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """C01: demand → draft (graph, per-node criteria, actors, missing facts).
    `auto_start` starts a ready full_auto draft in the same request."""
    acc = project_access(db, user, project_id, "workflow")
    try:
        w, d, _run = svc.plan(db, acc, body)
    except AppError as exc:
        if exc.code == "ai_unavailable":
            db.commit()  # the demand stays as a draft (design §4 C01)
        raise
    db.commit()
    return svc.draft_out(db, user, w, d)


@router.get("/workflows/{workflow_id}/collaboration-draft")
def get_draft(workflow_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    w, d, _ = svc.load_workflow(db, user, workflow_id)
    return svc.draft_out(db, user, w, d)


@router.patch("/workflows/{workflow_id}/collaboration-draft")
def patch_draft(workflow_id: uuid.UUID, body: DraftPatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """C02: edit the draft (only the named nodes change, A54); validation is recomputed."""
    w, d, acc = svc.load_workflow(db, user, workflow_id, "workflow")
    w, d = svc.patch_draft(db, acc, w, d, body)
    db.commit()
    return svc.draft_out(db, user, w, d)


@router.post("/workflows/{workflow_id}/collaboration-runs", status_code=201)
def start_run(workflow_id: uuid.UUID, body: StartIn, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """C02: freeze the draft and start the instance. Replaying the same
    idempotency key with the same request returns the existing run (200)."""
    w, d, acc = svc.load_workflow(db, user, workflow_id, "workflow")
    run, cr, created = svc.start(db, acc, w, d, body=body)
    db.commit()
    if not created:
        response.status_code = 200
    return svc.collaboration_out(db, user, run, cr)


@router.get("/projects/{project_id}/collaboration-runs")
def list_runs(
    project_id: uuid.UUID,
    status: str | None = None,
    mode: ExecutionMode | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project_access(db, user, project_id)
    return svc.list_runs(db, project_id, status=status, mode=mode, limit=limit, offset=offset)


@router.get("/runs/{run_id}/collaboration")
def get_collaboration(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """C03: run, progress (server-side), node tasks, resources, knowledge, activity and actions."""
    run, cr, _ = svc.load_run(db, user, run_id)
    return svc.collaboration_out(db, user, run, cr)


@router.get("/runs/{run_id}/node-tasks")
def list_node_tasks(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    run, _, _ = svc.load_run(db, user, run_id)
    params = {n["id"]: n.get("params", {}) for n in (run.graph_snapshot or {}).get("nodes", [])}
    return [svc.node_task_out(db, user, run, nt, params=params) for nt in svc.node_tasks_of(db, run)]


@router.get("/runs/{run_id}/node-tasks/{node_id}")
def get_node_task(run_id: uuid.UUID, node_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """One node with its executions' steps."""
    run, _, _ = svc.load_run(db, user, run_id)
    nt = next((x for x in svc.node_tasks_of(db, run) if x.node_id == node_id), None)
    if nt is None:
        raise not_found("node task")
    return svc.node_task_out(db, user, run, nt, with_steps=True)
