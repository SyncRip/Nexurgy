"""Project management lifecycle (docs/archive/project-task-asset-design.zh-CN.md §4.1).

Lifecycle and archive are separate facts: `lifecycle` is the business state
(draft / active / on_hold / completed / cancelled), `archived_at` only makes the
project read-only. New runs, deliverable selection, baseline adoption and
submissions need an active, unarchived project; the check runs after locking
the project row so a concurrent pause cannot slip between check and write.

Stages (P4, services/stages.py): starting needs a stage plan, completing needs
every stage accepted and no pending stage review, reopening resumes at a chosen
stage, cancelling withdraws pending stage reviews.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..errors import AppError, conflict
from ..models import ApprovalRequest, AssetVersion, CollaborationNodeTask, CollaborationRun, ImpactNotice, Project, Task, TaskSubmission, User, WorkflowRun
from . import feature_flags as features
from .audit import audit

ACTIVE_RUN = ("queued", "running", "waiting")

# action -> (allowed from, to)
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "start": (frozenset({"draft"}), "active"),
    "pause": (frozenset({"active"}), "on_hold"),
    "resume": (frozenset({"on_hold"}), "active"),
    "complete": (frozenset({"active"}), "completed"),
    "cancel": (frozenset({"draft", "active", "on_hold"}), "cancelled"),
    "reopen": (frozenset({"completed", "cancelled"}), "active"),
}
REASON_REQUIRED = {"pause", "cancel", "reopen"}
# Lifecycles in which the task plan (tasks, criteria, milestones) may still be edited.
PLANNABLE = frozenset({"draft", "active", "on_hold"})


def lock_project(db: Session, project_id: uuid.UUID, *, share: bool = True) -> Project:
    """Lock the project row (FOR SHARE by default: many writers may proceed,
    a lifecycle transition (FOR UPDATE) waits for them and vice versa)."""
    stmt = select(Project).where(Project.id == project_id)
    stmt = stmt.with_for_update(read=True) if share else stmt.with_for_update()
    p = db.scalar(stmt.execution_options(populate_existing=True))
    assert p is not None
    return p


def require_active(db: Session, project: Project | uuid.UUID, *, what: str = "this action") -> Project:
    """Guard for new runs, deliverable selection, baselines and submissions."""
    pid = project if isinstance(project, uuid.UUID) else project.id
    p = lock_project(db, pid)
    if p.archived_at is not None or p.status == "archived":
        raise conflict("project_archived", f"the project is archived; restore it before {what}", lifecycle=p.lifecycle)
    if p.lifecycle != "active":
        raise conflict("project_not_active", f"the project is {p.lifecycle}; {what} needs an active project", lifecycle=p.lifecycle)
    return p


def require_plannable(project: Project) -> None:
    """Task plan edits: allowed in draft / active / on_hold, not when closed or archived."""
    if project.archived_at is not None or project.status == "archived":
        raise conflict("project_archived", "the project is archived", lifecycle=project.lifecycle)
    if project.lifecycle not in PLANNABLE:
        raise conflict("project_closed", f"the project is {project.lifecycle}; reopen it before changing the plan", lifecycle=project.lifecycle)


def completion_blockers(db: Session, project_id: uuid.UUID) -> dict:
    """Everything that keeps the project from completing (empty = completable)."""
    from .stages import completion_blockers as stage_blockers

    return {**_completion_blockers(db, project_id), **stage_blockers(db, project_id)}


def _completion_blockers(db: Session, project_id: uuid.UUID) -> dict:
    active_runs = db.scalar(
        select(func.count()).select_from(WorkflowRun).where(WorkflowRun.project_id == project_id, WorkflowRun.status.in_(ACTIVE_RUN))
    ) or 0
    # Frozen historical work cannot hold the retained project lifecycle open.
    # An eligible unfinished collaboration still blocks through its WorkflowRun.
    open_tasks = (db.scalar(
        select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.status.notin_(("done", "cancelled")))
    ) or 0) if features.enabled("tasks") else 0
    kinds = ["engineering"]
    if features.enabled("tasks"):
        kinds.append("task_acceptance")
    if features.enabled("stages"):
        kinds.append("milestone_review")
    requests = select(func.count()).select_from(ApprovalRequest).where(
        ApprovalRequest.project_id == project_id, ApprovalRequest.status == "pending", ApprovalRequest.kind.in_(kinds))
    if not features.enabled("process_modules"):
        requests = requests.where(or_(ApprovalRequest.asset_version_id.is_(None), ApprovalRequest.asset_version_id.notin_(
            select(AssetVersion.id).where(AssetVersion.process_module_id.is_not(None)))))
    pending_requests = db.scalar(requests) or 0
    impacts = select(func.count()).select_from(ImpactNotice).where(
        ImpactNotice.project_id == project_id, ImpactNotice.status == "open")
    if not features.enabled("tasks"):
        impacts = impacts.where(ImpactNotice.milestone_id.is_not(None))
    if not features.enabled("stages"):
        impacts = impacts.where(ImpactNotice.milestone_id.is_(None))
    open_impacts = db.scalar(impacts) or 0
    out = {"active_runs": active_runs, "open_tasks": open_tasks, "pending_requests": pending_requests, "open_impacts": open_impacts}
    return {k: v for k, v in out.items() if v}


def transition(
    db: Session,
    user: User,
    project_id: uuid.UUID,
    *,
    action: str,
    reason: str,
    expected_version: int,
    cancel_runs: bool = False,
    milestone_id: uuid.UUID | None = None,
) -> Project:
    """Apply one lifecycle action. Caller checked `manage` access and commits.
    `milestone_id` (reopen): the stage the project resumes at; required when it has stages."""
    from . import stages

    if action not in TRANSITIONS:
        raise AppError(422, "unknown_action", f"unknown lifecycle action {action}")
    p = lock_project(db, project_id, share=False)
    if p.version != expected_version:
        raise conflict("version_conflict", "the project was changed by someone else; reload first", current_version=p.version)
    if p.archived_at is not None or p.status == "archived":
        raise conflict("project_archived", "restore the archived project first", lifecycle=p.lifecycle)
    allowed, target = TRANSITIONS[action]
    if p.lifecycle not in allowed:
        raise conflict("invalid_transition", f"cannot {action} a {p.lifecycle} project", lifecycle=p.lifecycle, action=action)
    reason = (reason or "").strip()
    if action in REASON_REQUIRED and not reason:
        raise AppError(422, "reason_required", f"give a reason to {action} the project")
    if milestone_id is not None:
        features.require_enabled("stages")
    details: dict = {}
    if action == "start":
        if not p.objective.strip():
            raise AppError(422, "objective_required", "set the project objective before starting it")
        if features.enabled("stages") and stages.milestone_count(db, p.id) == 0:
            raise AppError(422, "stages_required", "add at least one stage before starting the project")
        if p.owner_id is None:
            p.owner_id = user.id
    elif action == "complete":
        blockers = completion_blockers(db, p.id)
        if blockers:
            raise conflict("project_not_completable", "the project still has open work", **blockers)
    elif action == "cancel":
        details = _close_on_cancel(db, user, p, cancel_runs=cancel_runs, reason=reason)
        details["withdrawn_reviews"] = stages.withdraw_pending_reviews(db, user, p, reason)
    elif action == "reopen":
        details = stages.reopen_at(db, user, p, milestone_id)

    old = p.lifecycle
    p.lifecycle = target
    p.hold_reason = reason if target == "on_hold" else None
    p.version += 1
    audit(
        db,
        action=f"project.lifecycle.{action}",
        subject_type="project",
        subject_id=p.id,
        user_id=user.id,
        project_id=p.id,
        data={"from": old, "to": target, "reason": reason, **details},
    )
    db.flush()
    return p


def _close_on_cancel(db: Session, user: User, p: Project, *, cancel_runs: bool, reason: str) -> dict:
    """Cancelling withdraws pending task submissions atomically; active runs
    are cancelled only on request (otherwise they may finish and stay as assets,
    but engineering requests can no longer be decided in a cancelled project).

    Lock order (design §3.7: project → task → submission → request → run → node
    task): the caller holds the project; every Task touched here is locked first
    in id order, then collaboration runs are cancelled through
    agent_runtime.on_run_cancelled (which locks in that order itself) before any
    other run lock is taken, then the remaining pending submissions are
    withdrawn and finally the tool runs are cancelled."""
    from ..workflow import engine
    from . import approval_requests, submissions
    from .agent_runtime import on_run_cancelled
    from .tasks import lock_task

    pending = list(db.scalars(
        select(TaskSubmission).join(Task, Task.id == TaskSubmission.task_id).where(Task.project_id == p.id, TaskSubmission.status == "pending")
    ).all()) if features.enabled("tasks") else []
    run_ids = list(db.scalars(select(WorkflowRun.id).where(WorkflowRun.project_id == p.id, WorkflowRun.status.in_(ACTIVE_RUN))).all()) if cancel_runs else []
    collab_ids = [rid for rid in run_ids if db.get(CollaborationRun, rid) is not None]
    task_ids = {s.task_id for s in pending}
    if collab_ids:
        task_ids |= set(db.scalars(select(CollaborationNodeTask.task_id).where(CollaborationNodeTask.run_id.in_(collab_ids))).all())
    for tid in sorted(task_ids, key=str):
        lock_task(db, tid)
    cancelled_runs = []
    for run_id in collab_ids:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.status in engine.TERMINAL_RUN or run.cancel_requested:
            continue
        # Node tasks, Tasks, pending submissions and agent executions are cancelled with the run.
        on_run_cancelled(db, user, run_id)
        cancelled_runs.append(str(run_id))
    withdrawn = []
    for sub in pending:  # identity-mapped rows: those withdrawn with their run above already say so
        if sub.status == "pending":
            submissions.withdraw_locked(db, user, sub, reason=f"project cancelled: {reason}")
        if sub.status == "withdrawn":
            withdrawn.append(str(sub.id))
    for run_id in run_ids:
        if run_id in collab_ids:
            continue
        run = engine.lock_run(db, run_id)
        if run.status in engine.TERMINAL_RUN:
            continue
        engine.cancel_run(db, run)
        approval_requests.close_for_run(db, run.id, status="withdrawn")
        cancelled_runs.append(str(run.id))
    return {"withdrawn_submissions": withdrawn, "cancelled_runs": cancelled_runs, "at": datetime.now(UTC).isoformat()}
