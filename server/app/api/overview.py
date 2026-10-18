"""Project overview: objective, stage, task counts, my work, approvals, runs,
impacts, stage readiness and completion guard (contract §4; P4 contract §3.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ImpactNotice, MilestoneReview, ProjectMilestone, Task, User, WorkflowRun
from ..models import TASK_STATUSES
from ..security import current_user, project_access
from ..services import approval_requests as req_svc
from ..services import stages
from ..services import tasks as task_svc
from ..services.lifecycle import ACTIVE_RUN, completion_blockers
from ..services.stages import milestone_out, milestones_with_counts  # noqa: F401 - re-exported for api/projects.py

router = APIRouter(tags=["projects"])


@router.get("/projects/{project_id}/overview")
def overview(project_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    acc = project_access(db, user, project_id)
    p = acc.project
    owner = db.get(User, p.owner_id) if p.owner_id else None
    milestones, current = milestones_with_counts(db, project_id)
    if current is not None:
        m = db.get(ProjectMilestone, uuid.UUID(current["id"]))
        current = {**current, "readiness": stages.readiness_summary(db, m) if m is not None else None}
    blockers = completion_blockers(db, project_id)
    completable = p.lifecycle == "active" and p.archived_at is None and not blockers
    reviews = db.scalars(select(MilestoneReview).where(MilestoneReview.project_id == project_id, MilestoneReview.status == "pending")
                         .order_by(MilestoneReview.submitted_at)).all()

    by_status = {s: 0 for s in TASK_STATUSES}
    for status, n in db.execute(select(Task.status, func.count()).where(Task.project_id == project_id).group_by(Task.status)).all():
        by_status[status] = n
    in_scope = sum(n for s, n in by_status.items() if s != "cancelled")
    required_total = required_done = 0
    for status, n in db.execute(select(Task.status, func.count()).where(Task.project_id == project_id, Task.required.is_(True),
                                                                       Task.status != "cancelled").group_by(Task.status)).all():
        required_total += n
        required_done += n if status == "done" else 0

    my_tasks = db.scalars(
        select(Task).where(Task.project_id == project_id, Task.owner_id == user.id, Task.status.notin_(("done", "cancelled")))
        .order_by(Task.due_date.asc().nulls_last(), Task.number).limit(20)
    ).all()
    pending = req_svc.list_requests(db, user, scope="mine", project_id=project_id, status="pending", limit=50)
    active = db.scalars(
        select(WorkflowRun).where(WorkflowRun.project_id == project_id, WorkflowRun.status.in_(ACTIVE_RUN))
        .order_by(WorkflowRun.created_at.desc()).limit(20)
    ).all()
    since = datetime.now(UTC) - timedelta(days=7)
    failed = db.scalars(
        select(WorkflowRun).where(WorkflowRun.project_id == project_id, WorkflowRun.status == "failed", WorkflowRun.updated_at >= since)
        .order_by(WorkflowRun.updated_at.desc()).limit(10)
    ).all()
    impacts = db.scalars(
        select(ImpactNotice).where(ImpactNotice.project_id == project_id, ImpactNotice.status == "open").order_by(ImpactNotice.created_at.desc()).limit(50)
    ).all()

    def run_item(r: WorkflowRun) -> dict:
        return {**task_svc.run_brief(r), "task_id": str(r.task_id) if r.task_id else None, "part_id": str(r.part_id) if r.part_id else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None}

    return {
        "project": {
            "id": str(p.id),
            "code": p.code,
            "name": p.name,
            "description": p.description,
            "lifecycle": p.lifecycle,
            "status": p.status,
            "archived_at": p.archived_at.isoformat() if p.archived_at else None,
            "objective": p.objective,
            "hold_reason": p.hold_reason,
            "version": p.version,
            "owner": {"id": str(owner.id), "name": owner.name, "email": owner.email} if owner else None,
            "current_milestone": current,
            "my_role": acc.role,
        },
        # No weighted percentage (design §4.4): counts only; scope excludes cancelled tasks.
        "tasks": {"by_status": by_status, "required_done": required_done, "required_total": required_total, "in_scope": in_scope},
        "my_tasks": [task_svc.task_out(t) for t in my_tasks],
        "pending_approvals": [req_svc.request_out(db, user, r) for r in pending],
        "active_runs": [run_item(r) for r in active],
        "failed_runs_recent": [run_item(r) for r in failed],
        "open_impacts": [task_svc.notice_out(n) for n in impacts],
        "milestones": milestones,
        "current_milestone": current,
        "completion": {"ready": completable, "blockers": blockers, "lifecycle": p.lifecycle},
        "pending_reviews": [stages.review_out(db, r) for r in reviews],
    }
