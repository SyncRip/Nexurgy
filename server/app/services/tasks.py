"""Project tasks: plan, status, criteria, dependencies, baselines, run inputs
(docs/archive/project-task-asset-design.zh-CN.md §4.2–4.4, §5.2, §6.5; contract §3).

Status changes only happen here (start / cancel / reopen) or in the
submission and approval-request services; PATCH never touches status. Every
write locks the task row and compares `expected_version` before changing it.
A run succeeding never changes a task.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..errors import AppError, bad_request, conflict, forbidden, not_found
from ..models import (
    ASSET_TYPES,
    AcceptanceCriterion,
    ApprovalRequest,
    Asset,
    AssetVersion,
    ImpactNotice,
    Part,
    ProjectMember,
    ProjectMilestone,
    RunInputBinding,
    Task,
    TaskBaselineItem,
    TaskBaselineSnapshot,
    TaskCollaborator,
    TaskDeliverableDraft,
    TaskDependency,
    TaskSubmission,
    User,
    WorkflowRun,
)
from ..security import ProjectAccess, project_access
from . import feature_flags as features
from .audit import audit
from .lifecycle import ACTIVE_RUN, require_active, require_plannable

SLOT_RE = r"^[a-z][a-z0-9_:.-]{0,79}$"
AssetType = Literal["part_model", "drawing", "process_plan", "quote", "document", "intake", "process_module",
                    "assembly_model", "assembly_drawing", "drawing_document", "engineering_spec", "delivery_manifest"]
assert set(AssetType.__args__) == set(ASSET_TYPES)  # type: ignore[attr-defined]
LOCKED_STATUSES = ("in_review", "done")


def assets():
    """Asset catalogue service (backend A). Imported lazily so this module
    loads even while the catalogue is being built."""
    from . import assets as mod

    return mod


def canon_hash(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class RuleSpec(BaseModel):
    """Closed, explainable acceptance rule (never user code)."""

    model_config = ConfigDict(extra="forbid")
    asset_types: list[AssetType] = Field(default_factory=list, max_length=6)
    require_approval_stage: Literal["design", "process", "quote"] | None = None
    allow_estimated: bool = False
    allow_simulated: bool = False
    allow_unverified: bool = False  # W0 §2.2: Turbo / undecided AI output is refused unless a criterion allows it
    consistent_with_slot: str | None = Field(default=None, pattern=SLOT_RE)
    min_count: int = Field(default=1, ge=0, le=50)


# --- loading, locking, permissions -------------------------------------------


def load_task(db: Session, user: User, task_id: uuid.UUID, action: str = "view") -> tuple[Task, ProjectAccess]:
    t = db.get(Task, task_id)
    if t is None:
        raise not_found("task")
    acc = project_access(db, user, t.project_id, action)
    return t, acc


def lock_task(db: Session, task_id: uuid.UUID) -> Task:
    t = db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
    if t is None:
        raise not_found("task")
    return t


def check_version(task: Task, expected: int | None) -> None:
    if expected is not None and expected != task.version:
        raise conflict("version_conflict", "the task was changed by someone else; reload first", current_version=task.version)


def bump(task: Task) -> None:
    task.version += 1


def is_collaborator(db: Session, task: Task, user_id: uuid.UUID) -> bool:
    return db.get(TaskCollaborator, {"task_id": task.id, "user_id": user_id}) is not None


def can_edit(acc: ProjectAccess, task: Task) -> bool:
    """Plan and status changes: project owner or task owner (anyone who may
    run work while the task has no owner)."""
    if acc.role == "owner" or task.owner_id == acc.user.id:
        return True
    return task.owner_id is None and acc.can("workflow")


def can_participate(db: Session, acc: ProjectAccess, task: Task) -> bool:
    """Runs, deliverables and submissions: editors plus collaborators."""
    if can_edit(acc, task):
        return True
    return acc.can("workflow") and is_collaborator(db, task, acc.user.id)


def require_edit(acc: ProjectAccess, task: Task) -> None:
    if not can_edit(acc, task):
        raise forbidden("only the task owner or a project owner can change this task", role=acc.role)


def require_participant(db: Session, acc: ProjectAccess, task: Task) -> None:
    if not can_participate(db, acc, task):
        raise forbidden("only the task owner, its collaborators or a project owner can do this", role=acc.role)


def _require_member(db: Session, project_id: uuid.UUID, user_id: uuid.UUID, what: str) -> None:
    if db.get(ProjectMember, {"project_id": project_id, "user_id": user_id}) is None:
        raise AppError(422, "not_a_member", f"the {what} must be a project member", {"user_id": str(user_id)})


def _check_refs(db: Session, project_id: uuid.UUID, data: dict) -> None:
    for key in ("owner_id", "reviewer_id"):
        if data.get(key):
            _require_member(db, project_id, data[key], key.removesuffix("_id"))
            m = db.get(ProjectMember, {"project_id": project_id, "user_id": data[key]})
            if m is not None and m.role == "viewer":
                raise AppError(422, "read_only_member", f"a viewer cannot be the task {key.removesuffix('_id')}")
    if data.get("milestone_id"):
        m = db.get(ProjectMilestone, data["milestone_id"])
        if m is None or m.project_id != project_id:
            raise not_found("milestone")
        features.require_enabled("stages")
    if data.get("part_id"):
        p = db.get(Part, data["part_id"])
        if p is None or p.project_id != project_id:
            raise not_found("part")


# --- output -------------------------------------------------------------------


def task_out(t: Task) -> dict:
    return {
        "id": str(t.id),
        "project_id": str(t.project_id),
        "number": t.number,
        "key": f"T-{t.number}",
        "title": t.title,
        "objective": t.objective,
        "owner_id": str(t.owner_id) if t.owner_id else None,
        "reviewer_id": str(t.reviewer_id) if t.reviewer_id else None,
        "milestone_id": str(t.milestone_id) if t.milestone_id else None,
        "part_id": str(t.part_id) if t.part_id else None,
        "priority": t.priority,
        "required": t.required,
        "status": t.status,
        "blocked_reason": t.blocked_reason,
        "start_date": _iso(t.start_date),
        "due_date": _iso(t.due_date),
        "current_baseline_id": str(t.current_baseline_id) if t.current_baseline_id else None,
        "version": t.version,
        "created_by": str(t.created_by),
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


def criterion_out(c: AcceptanceCriterion) -> dict:
    return {
        "id": str(c.id),
        "task_id": str(c.task_id),
        "seq": c.seq,
        "description": c.description,
        "required": c.required,
        "allowed_types": list(c.allowed_types or []),
        "rule_spec": c.rule_spec or {},
        "manual_required": c.manual_required,
        "version": c.version,
        "created_at": _iso(c.created_at),
    }


def version_brief(db: Session, version_id: uuid.UUID) -> dict:
    """Catalogue view of one asset version (falls back to ids if the catalogue is unavailable)."""
    v = db.get(AssetVersion, version_id)
    if v is None:
        return {"id": str(version_id), "missing": True}
    try:
        return assets().version_out(db, v)
    except ImportError:  # pragma: no cover - catalogue not installed yet
        asset = db.get(Asset, v.asset_id)
        return {"id": str(v.id), "asset_id": str(v.asset_id), "asset_type": asset.type if asset else None, "version_no": v.version_no,
                "content_hash": v.content_hash, "availability": v.availability, "labels": list(v.labels or [])}


def baseline_items(db: Session, snapshot_id: uuid.UUID | None) -> dict[str, uuid.UUID]:
    if snapshot_id is None:
        return {}
    rows = db.scalars(select(TaskBaselineItem).where(TaskBaselineItem.snapshot_id == snapshot_id)).all()
    return {r.slot_key: r.asset_version_id for r in rows}


def baseline_out(db: Session, s: TaskBaselineSnapshot, current_id: uuid.UUID | None = None) -> dict:
    items = baseline_items(db, s.id)
    return {
        "id": str(s.id),
        "task_id": str(s.task_id),
        "seq": s.seq,
        "hash": s.hash,
        "reason": s.reason,
        "adopted_by": str(s.adopted_by) if s.adopted_by else None,
        "adopted_by_kind": s.adopted_by_kind,
        "adopted_by_agent_id": str(s.adopted_by_agent_id) if s.adopted_by_agent_id else None,
        "adopted_at": _iso(s.adopted_at),
        "current": current_id == s.id,
        "items": [{"slot": k, "asset_version_id": str(v), "version": version_brief(db, v)} for k, v in sorted(items.items())],
    }


def deliverable_out(db: Session, d: TaskDeliverableDraft) -> dict:
    return {
        "id": str(d.id),
        "task_id": str(d.task_id),
        "asset_version_id": str(d.asset_version_id),
        "criterion_ids": [str(c) for c in d.criterion_ids or []],
        "note": d.note,
        "selected_by": str(d.selected_by) if d.selected_by else None,
        "selected_by_kind": d.selected_by_kind,
        "selected_by_agent_id": str(d.selected_by_agent_id) if d.selected_by_agent_id else None,
        "selected_at": _iso(d.selected_at),
        "version": version_brief(db, d.asset_version_id),
    }


def notice_out(n: ImpactNotice) -> dict:
    return {
        "id": str(n.id),
        "project_id": str(n.project_id),
        "task_id": str(n.task_id) if n.task_id else None,
        "submission_id": str(n.submission_id) if n.submission_id else None,
        "from_snapshot_id": str(n.from_snapshot_id) if n.from_snapshot_id else None,
        "to_snapshot_id": str(n.to_snapshot_id) if n.to_snapshot_id else None,
        "milestone_id": str(n.milestone_id) if n.milestone_id else None,
        "milestone_review_id": str(n.milestone_review_id) if n.milestone_review_id else None,
        "reason": n.reason,
        "severity": n.severity,
        "details": n.details or {},
        "status": n.status,
        "resolved_by": str(n.resolved_by) if n.resolved_by else None,
        "resolution_note": n.resolution_note,
        "created_at": _iso(n.created_at),
        "resolved_at": _iso(n.resolved_at),
    }


def run_brief(r: WorkflowRun) -> dict:
    return {"id": str(r.id), "title": r.title, "status": r.status, "purpose": r.purpose, "workflow_id": str(r.workflow_id) if r.workflow_id else None,
            "created_by": str(r.created_by), "created_at": _iso(r.created_at)}


def active_runs(db: Session, task_id: uuid.UUID) -> list[WorkflowRun]:
    return list(db.scalars(select(WorkflowRun).where(WorkflowRun.task_id == task_id, WorkflowRun.status.in_(ACTIVE_RUN)).order_by(WorkflowRun.created_at)).all())


def task_detail(db: Session, user: User, t: Task, acc: ProjectAccess | None = None) -> dict:
    from .approval_requests import request_out
    from .submissions import submission_out

    acc = acc or project_access(db, user, t.project_id)
    data = task_out(t)
    data["criteria"] = [criterion_out(c) for c in db.scalars(
        select(AcceptanceCriterion).where(AcceptanceCriterion.task_id == t.id).order_by(AcceptanceCriterion.seq)).all()]
    cur = db.get(TaskBaselineSnapshot, t.current_baseline_id) if t.current_baseline_id else None
    data["baseline"] = baseline_out(db, cur, t.current_baseline_id) if cur else None
    preds = db.scalars(select(Task).join(TaskDependency, TaskDependency.predecessor_id == Task.id).where(TaskDependency.successor_id == t.id)).all()
    succs = db.scalars(select(Task).join(TaskDependency, TaskDependency.successor_id == Task.id).where(TaskDependency.predecessor_id == t.id)).all()
    brief = lambda x: {"id": str(x.id), "number": x.number, "key": f"T-{x.number}", "title": x.title, "status": x.status}  # noqa: E731
    data["dependencies"] = {"predecessors": [brief(x) for x in preds], "successors": [brief(x) for x in succs]}
    collab = db.execute(select(User.id, User.name, User.email).join(TaskCollaborator, TaskCollaborator.user_id == User.id).where(TaskCollaborator.task_id == t.id)).all()
    data["collaborators"] = [{"id": str(i), "name": n, "email": e} for i, n, e in collab]
    data["deliverables"] = [deliverable_out(db, d) for d in db.scalars(
        select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == t.id).order_by(TaskDeliverableDraft.selected_at)).all()]
    last = db.scalar(select(TaskSubmission).where(TaskSubmission.task_id == t.id).order_by(TaskSubmission.seq.desc()).limit(1))
    data["latest_submission"] = submission_out(db, last, with_snapshot=False) if last else None
    data["active_run_count"] = db.scalar(
        select(func.count()).select_from(WorkflowRun).where(WorkflowRun.task_id == t.id, WorkflowRun.status.in_(ACTIVE_RUN))) or 0
    pending = db.scalars(select(ApprovalRequest).where(ApprovalRequest.task_id == t.id, ApprovalRequest.status == "pending")
                         .order_by(ApprovalRequest.created_at)).all()
    data["pending_requests"] = [request_out(db, user, r) for r in pending]
    data["open_impacts"] = [notice_out(n) for n in db.scalars(
        select(ImpactNotice).where(ImpactNotice.task_id == t.id, ImpactNotice.status == "open").order_by(ImpactNotice.created_at)).all()]
    data["permissions"] = {"edit": features.enabled("tasks") and can_edit(acc, t),
                           "participate": features.enabled("tasks") and can_participate(db, acc, t)}
    data["collaboration"] = collaboration_ref(db, t.id)
    return data


def collaboration_ref(db: Session, task_id: uuid.UUID) -> dict | None:
    """{run_id, node_id, execution_mode, phase} when the task is a collaboration node task."""
    from ..models import CollaborationNodeTask, CollaborationRun

    nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.task_id == task_id))
    if nt is None:
        return None
    cr = db.get(CollaborationRun, nt.run_id)
    return {"run_id": str(nt.run_id), "node_id": nt.node_id, "execution_mode": cr.execution_mode if cr else None, "phase": nt.phase}


# --- tasks --------------------------------------------------------------------


def list_tasks(
    db: Session,
    project_id: uuid.UUID,
    *,
    status: str | None = None,
    owner_id: uuid.UUID | None = None,
    q: str | None = None,
    milestone_id: uuid.UUID | None = None,
) -> list[Task]:
    stmt = select(Task).where(Task.project_id == project_id).order_by(Task.number)
    if status:
        stmt = stmt.where(Task.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if owner_id:
        stmt = stmt.where(Task.owner_id == owner_id)
    if milestone_id:
        stmt = stmt.where(Task.milestone_id == milestone_id)
    if q and q.strip():
        text = q.strip()
        cond = Task.title.ilike(f"%{text}%") | Task.objective.ilike(f"%{text}%")
        digits = text.upper().removeprefix("T-")
        if digits.isdigit():
            cond = or_(cond, Task.number == int(digits))
        stmt = stmt.where(cond)
    return list(db.scalars(stmt).all())


def create_task(db: Session, acc: ProjectAccess, data: dict, collaborator_ids: list[uuid.UUID]) -> Task:
    """Caller has `workflow` access. Assigning someone else needs `manage`."""
    from .lifecycle import lock_project

    acc.require("workflow")
    features.require_enabled("tasks")
    project = lock_project(db, acc.project.id, share=False)  # serialises numbering
    require_plannable(project)
    for key in ("owner_id", "reviewer_id"):
        if data.get(key) and data[key] != acc.user.id and acc.role != "owner":
            raise forbidden("only a project owner can assign other people", field=key)
    _check_refs(db, project.id, data)
    number = (db.scalar(select(func.max(Task.number)).where(Task.project_id == project.id)) or 0) + 1
    t = Task(project_id=project.id, number=number, created_by=acc.user.id, **data)
    if t.owner_id is None:
        t.owner_id = acc.user.id
    db.add(t)
    db.flush()
    set_collaborators(db, t, collaborator_ids)
    audit(db, action="task.create", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=project.id, data={"number": number, "title": t.title})
    db.flush()
    return t


def patch_task(db: Session, acc: ProjectAccess, task_id: uuid.UUID, data: dict, expected_version: int) -> Task:
    t = lock_task(db, task_id)
    from .agent_runtime import guard_generic_write

    check_version(t, expected_version)
    require_plannable(acc.project)
    require_edit(acc, t)
    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "change it")  # owner / reviewer / objective are frozen in the collaboration contract
    changes = {k: v for k, v in data.items() if getattr(t, k) != v}
    if not changes:
        return t
    for key in ("owner_id", "reviewer_id"):
        if key in changes and acc.role != "owner" and changes[key] not in (None, acc.user.id):
            raise forbidden("only a project owner can assign other people", field=key)
    if t.status in LOCKED_STATUSES and "objective" in changes:
        raise conflict("task_locked", f"the objective cannot change while the task is {t.status}; withdraw or reopen first", status=t.status)
    if t.status == "in_review" and "reviewer_id" in changes:
        raise conflict("task_locked", "the reviewer of a pending submission changes through reassign", status=t.status)
    _check_refs(db, t.project_id, changes)
    for k, v in changes.items():
        setattr(t, k, v)
    bump(t)
    audit(db, action="task.update", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id,
          data={k: str(v) if v is not None else None for k, v in changes.items()})
    db.flush()
    return t


def set_collaborators(db: Session, t: Task, user_ids: list[uuid.UUID]) -> None:
    from .agent_runtime import guard_generic_write

    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "change its collaborators")  # the people of a node are frozen in the collaboration contract
    wanted = set(user_ids)
    for uid in wanted:
        _require_member(db, t.project_id, uid, "collaborator")
    current = {c.user_id: c for c in db.scalars(select(TaskCollaborator).where(TaskCollaborator.task_id == t.id)).all()}
    for uid, row in current.items():
        if uid not in wanted:
            db.delete(row)
    for uid in wanted - set(current):
        db.add(TaskCollaborator(task_id=t.id, user_id=uid))
    db.flush()


def transition(
    db: Session,
    acc: ProjectAccess,
    task_id: uuid.UUID,
    *,
    action: str,
    reason: str,
    expected_version: int,
    cancel_runs: bool | None = None,
) -> tuple[Task, dict]:
    """start / cancel / reopen. in_review/done only change via submissions and approvals."""
    from ..workflow import engine
    from .approval_requests import close_for_run
    from .lifecycle import lock_project

    lock_project(db, acc.project.id)  # project -> task: waits for a stage approval in progress
    t = lock_task(db, task_id)
    from .agent_runtime import guard_generic_write

    check_version(t, expected_version)
    require_plannable(acc.project)
    require_edit(acc, t)
    if action == "reopen":
        features.require_enabled("tasks")
    else:
        features.require_write(db, "tasks", task_id=t.id)
    guard_generic_write(db, t.id, f"{action} it")
    reason = (reason or "").strip()
    extra: dict[str, Any] = {}
    old = t.status
    if action == "start":
        if t.status != "todo":
            raise conflict("invalid_transition", f"a {t.status} task cannot be started", status=t.status)
        t.status = "in_progress"
    elif action == "cancel":
        if t.status == "in_review":
            raise conflict("withdraw_first", "withdraw the pending submission before cancelling the task", status=t.status)
        if t.status not in ("todo", "in_progress"):
            raise conflict("invalid_transition", f"a {t.status} task cannot be cancelled", status=t.status)
        runs = active_runs(db, t.id)
        if runs and cancel_runs is None:
            # The user must decide: cancel them, or let them finish and keep the assets.
            raise conflict("active_runs", "the task has active runs; choose whether to cancel them", runs=[run_brief(r) for r in runs])
        cancelled = []
        if runs and cancel_runs:
            for r in runs:
                run = engine.lock_run(db, r.id)
                if run.status in engine.TERMINAL_RUN:
                    continue
                engine.cancel_run(db, run)
                close_for_run(db, run.id, status="withdrawn")
                cancelled.append(str(run.id))
        extra = {"cancelled_runs": cancelled, "active_runs": [run_brief(r) for r in active_runs(db, t.id)]}
        t.status = "cancelled"
    elif action == "reopen":
        if t.status != "done":
            raise conflict("invalid_transition", f"only a done task can be reopened (it is {t.status})", status=t.status)
        if not reason:
            raise AppError(422, "reason_required", "give a reason to reopen the task")
        t.status = "in_progress"
        # Stage reviews that froze this task's accepted submission get an impact
        # notice (P4); the accepted stage itself is not rolled back.
        from .stages import task_reopened

        extra["impacts"] = [notice_out(n) for n in task_reopened(db, t, acc.user.id)]
    else:
        raise AppError(422, "unknown_action", f"unknown task action {action}")
    # Collaboration node tasks (design §4 C04): start / cancel / reopen move the node phase and the run step.
    from . import agent_runtime
    from .actors import of_user

    collab = agent_runtime.on_task_transition(db, t, action=action, actor=of_user(acc.user), reason=reason)
    if collab is not None:
        extra["collaboration"] = collab
    bump(t)
    audit(db, action=f"task.{action}", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id,
          data={"from": old, "to": t.status, "reason": reason, **{k: v for k, v in extra.items() if k == "cancelled_runs"}})
    from . import outbox

    outbox.task_status(db, t, acc.user.id, old, reason)
    db.flush()
    return t, extra


# --- criteria -----------------------------------------------------------------


def _criterion_fields(data: dict) -> dict:
    if "rule_spec" in data and data["rule_spec"] is not None:
        spec = data["rule_spec"]
        data["rule_spec"] = (spec if isinstance(spec, RuleSpec) else RuleSpec.model_validate(spec)).model_dump(mode="json")
    if data.get("allowed_types") is not None:
        bad = [x for x in data["allowed_types"] if x not in ASSET_TYPES]
        if bad:
            raise AppError(422, "unknown_asset_type", "unknown asset type", {"types": bad})
    return data


def _require_criteria_editable(t: Task) -> None:
    if t.status in LOCKED_STATUSES:
        raise conflict("task_locked", f"criteria cannot change while the task is {t.status}; withdraw or reopen first", status=t.status)


def add_criterion(db: Session, acc: ProjectAccess, task_id: uuid.UUID, data: dict) -> AcceptanceCriterion:
    t = lock_task(db, task_id)
    from .agent_runtime import guard_generic_write

    require_plannable(acc.project)
    require_edit(acc, t)
    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "change its criteria")
    _require_criteria_editable(t)
    data = _criterion_fields(data)
    data.setdefault("rule_spec", RuleSpec().model_dump(mode="json"))
    if data.get("rule_spec") is None:
        data["rule_spec"] = RuleSpec().model_dump(mode="json")
    seq = (db.scalar(select(func.max(AcceptanceCriterion.seq)).where(AcceptanceCriterion.task_id == t.id)) or 0) + 1
    c = AcceptanceCriterion(task_id=t.id, seq=seq, **data)
    db.add(c)
    bump(t)
    audit(db, action="criterion.create", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id, data={"description": c.description})
    db.flush()
    return c


def load_criterion(db: Session, user: User, criterion_id: uuid.UUID, action: str = "view") -> tuple[AcceptanceCriterion, Task, ProjectAccess]:
    c = db.get(AcceptanceCriterion, criterion_id)
    if c is None:
        raise not_found("criterion")
    t, acc = load_task(db, user, c.task_id, action)
    return c, t, acc


def patch_criterion(db: Session, acc: ProjectAccess, c: AcceptanceCriterion, data: dict, expected_version: int | None) -> AcceptanceCriterion:
    t = lock_task(db, c.task_id)
    from .agent_runtime import guard_generic_write

    require_plannable(acc.project)
    require_edit(acc, t)
    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "change its criteria")
    _require_criteria_editable(t)
    c = db.scalar(select(AcceptanceCriterion).where(AcceptanceCriterion.id == c.id).with_for_update().execution_options(populate_existing=True))
    assert c is not None
    if expected_version is not None and expected_version != c.version:
        raise conflict("version_conflict", "the criterion was changed by someone else; reload first", current_version=c.version)
    data = _criterion_fields({k: v for k, v in data.items() if v is not None})
    for k, v in data.items():
        setattr(c, k, v)
    c.version += 1
    bump(t)
    audit(db, action="criterion.update", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id, data={"criterion_id": str(c.id)})
    db.flush()
    return c


def delete_criterion(db: Session, acc: ProjectAccess, c: AcceptanceCriterion) -> None:
    t = lock_task(db, c.task_id)
    from .agent_runtime import guard_generic_write

    require_plannable(acc.project)
    require_edit(acc, t)
    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "change its criteria")
    _require_criteria_editable(t)
    for d in db.scalars(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == t.id)).all():
        if c.id in (d.criterion_ids or []):
            d.criterion_ids = [x for x in d.criterion_ids if x != c.id]
    db.delete(c)
    bump(t)
    audit(db, action="criterion.delete", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id, data={"criterion_id": str(c.id)})
    db.flush()


# --- dependencies ---------------------------------------------------------------


def set_dependencies(db: Session, acc: ProjectAccess, task_id: uuid.UUID, predecessor_ids: list[uuid.UUID]) -> Task:
    t = lock_task(db, task_id)
    from .agent_runtime import guard_generic_write

    require_plannable(acc.project)
    require_edit(acc, t)
    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "change its dependencies")  # node dependencies are the frozen graph edges
    wanted = set(predecessor_ids)
    if t.id in wanted:
        raise AppError(422, "dependency_cycle", "a task cannot depend on itself")
    for pid in wanted:
        p = db.get(Task, pid)
        if p is None or p.project_id != t.project_id:
            raise AppError(422, "dependency_other_project", "dependencies must be tasks of the same project", {"task_id": str(pid)})
    # Successor graph of the project without this task's current incoming edges.
    succ: dict[uuid.UUID, set[uuid.UUID]] = {}
    for d in db.scalars(select(TaskDependency).join(Task, Task.id == TaskDependency.successor_id).where(Task.project_id == t.project_id)).all():
        if d.successor_id == t.id:
            continue
        succ.setdefault(d.predecessor_id, set()).add(d.successor_id)
    seen, stack = set(), [t.id]
    while stack:  # anything reachable from t must not become its predecessor
        cur = stack.pop()
        for nxt in succ.get(cur, ()):
            if nxt in wanted:
                raise AppError(422, "dependency_cycle", "this dependency would create a cycle", {"task_id": str(nxt)})
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    current = {d.predecessor_id: d for d in db.scalars(select(TaskDependency).where(TaskDependency.successor_id == t.id)).all()}
    for pid, row in current.items():
        if pid not in wanted:
            db.delete(row)
    for pid in wanted - set(current):
        db.add(TaskDependency(predecessor_id=pid, successor_id=t.id))
    bump(t)
    audit(db, action="task.dependencies", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id,
          data={"predecessors": [str(x) for x in sorted(wanted, key=str)]})
    db.flush()
    return t


# --- baselines -------------------------------------------------------------------


def _project_version(db: Session, project_id: uuid.UUID, version_id: uuid.UUID) -> AssetVersion:
    v = db.get(AssetVersion, version_id)
    asset = db.get(Asset, v.asset_id) if v else None
    if v is None or asset is None or asset.project_id != project_id:
        raise not_found("asset version")
    return v


def adopt_baseline(
    db: Session,
    acc: ProjectAccess,
    task_id: uuid.UUID,
    *,
    items: dict[str, uuid.UUID | None],
    reason: str,
    replace: bool = False,
    expected_version: int | None = None,
) -> tuple[TaskBaselineSnapshot, list[ImpactNotice], bool]:
    """Create an immutable snapshot and move the task's pointer.

    `items` is merged into the current baseline (a null value drops a slot)
    unless `replace` is set. Re-adopting identical items is a no-op.
    Returns (snapshot, impact notices, created).
    """
    from .agent_runtime import guard_generic_write
    from .submissions import consistency_issues

    require_active(db, acc.project.id, what="adopting a baseline")
    t = lock_task(db, task_id)
    # Collaboration nodes adopt through C04 (which re-queues a running agent attempt) or a confirmed knowledge adoption.
    guard_generic_write(db, t.id, "adopt a baseline")
    check_version(t, expected_version)
    require_edit(acc, t)
    features.require_write(db, "tasks", task_id=t.id)
    if t.status == "in_review":
        raise conflict("task_in_review", "withdraw the pending submission before changing the baseline", status=t.status)
    if t.status == "cancelled":
        raise conflict("task_cancelled", "the task is cancelled", status=t.status)
    for slot in items:
        if not re.match(SLOT_RE, slot):
            raise AppError(422, "invalid_slot", "slot names are lower-case identifiers such as target_model", {"slot": slot})
    old_items = baseline_items(db, t.current_baseline_id)
    new_items: dict[str, uuid.UUID] = {} if replace else dict(old_items)
    for slot, vid in items.items():
        if vid is None:
            new_items.pop(slot, None)
        else:
            new_items[slot] = _project_version(db, t.project_id, vid).id
    if not new_items:
        raise AppError(422, "baseline_empty", "a baseline needs at least one slot")
    current = db.get(TaskBaselineSnapshot, t.current_baseline_id) if t.current_baseline_id else None
    if current is not None and new_items == old_items:
        return current, [], False
    hashes = {slot: {"asset_version_id": str(vid), "content_hash": db.get(AssetVersion, vid).content_hash} for slot, vid in new_items.items()}  # type: ignore[union-attr]
    seq = (db.scalar(select(func.max(TaskBaselineSnapshot.seq)).where(TaskBaselineSnapshot.task_id == t.id)) or 0) + 1
    snap = TaskBaselineSnapshot(task_id=t.id, seq=seq, hash=canon_hash(hashes), reason=(reason or "").strip(), adopted_by=acc.user.id)
    db.add(snap)
    db.flush()
    for slot, vid in new_items.items():
        db.add(TaskBaselineItem(snapshot_id=snap.id, slot_key=slot, asset_version_id=vid))
    from_id = t.current_baseline_id
    t.current_baseline_id = snap.id
    bump(t)
    db.flush()

    changed = {s: {"from": str(old_items[s]) if s in old_items else None, "to": str(new_items[s]) if s in new_items else None}
               for s in set(old_items) | set(new_items) if old_items.get(s) != new_items.get(s)}
    notices: list[ImpactNotice] = []

    def notice(reason_code: str, severity: str, details: dict, submission_id: uuid.UUID | None = None) -> None:
        n = ImpactNotice(project_id=t.project_id, task_id=t.id, submission_id=submission_id, from_snapshot_id=from_id, to_snapshot_id=snap.id,
                         reason=reason_code, severity=severity, details=details)
        db.add(n)
        notices.append(n)

    if t.status == "done":
        accepted = db.scalar(select(TaskSubmission).where(TaskSubmission.task_id == t.id, TaskSubmission.status == "approved")
                             .order_by(TaskSubmission.seq.desc()).limit(1))
        notice("baseline_changed_after_done", "warning", {"changed_slots": changed}, accepted.id if accepted else None)
    if from_id is not None:
        drafts = db.scalars(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == t.id)).all()
        draft_versions = [d.asset_version_id for d in drafts]
        for issue in consistency_issues(db, draft_versions, new_items):
            notice("deliverable_inconsistent", "warning", issue)
        for slot, ch in changed.items():
            if ch["from"] and uuid.UUID(ch["from"]) in draft_versions:
                notice("deliverable_not_in_baseline", "info", {"slot": slot, "asset_version_id": ch["from"], "baseline_version_id": ch["to"]})
    audit(db, action="task.baseline", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id,
          data={"snapshot_id": str(snap.id), "seq": seq, "changed": changed, "reason": snap.reason})
    db.flush()
    return snap, notices, True


# --- run inputs -------------------------------------------------------------------

# Input node type -> (param key, asset type)
INPUT_NODES = {"model_input": ("revision_id", "part_model"), "file_input": ("file_version_id", "document")}


def bind_run_inputs(db: Session, run: WorkflowRun, template_version: Any = None) -> list[RunInputBinding]:
    """Freeze the concrete asset versions a run reads (port_key = node id).
    Inputs whose specialised row is not catalogued get no binding.

    Runs pinned to a published template version are checked against its input
    policy (availability, source labels, required approvals); any problem is a
    422 `input_quality` listing every issue. Draft test runs, workspace runs and
    exploratory templates are not checked. Each binding keeps a quality snapshot.
    """
    from . import templates as tpl
    from ..models import WorkflowTemplateVersion

    out: list[RunInputBinding] = []
    try:
        mod = assets()
    except ImportError:  # pragma: no cover - catalogue not installed yet
        return out
    if template_version is None and run.template_version_id is not None:
        template_version = db.get(WorkflowTemplateVersion, run.template_version_id)
    policy, stages = tpl.input_contract(template_version)
    issues: list[dict] = []
    for node in run.graph_snapshot.get("nodes", []):
        spec = INPUT_NODES.get(node.get("type"))
        if spec is None:
            continue
        raw = (node.get("params") or {}).get(spec[0])
        if not raw:
            continue
        try:
            row_id = uuid.UUID(str(raw))
        except ValueError:
            continue
        v = mod.ensure_version(db, spec[1], row_id)  # version_for, registering rows that predate the catalogue
        asset = db.get(Asset, v.asset_id) if v is not None else None
        if v is None or asset is None or asset.project_id != run.project_id:
            continue
        quality, problems = tpl.input_quality(db, node_id=node["id"], version=v, asset_type=spec[1], policy=policy,
                                              required_stage=stages.get(node["id"]))
        issues += problems
        snapshot = {"asset_type": spec[1], "availability": v.availability, "publish_state": v.publish_state, "labels": quality["labels"],
                    "version_no": v.version_no, "engineering": mod.version_out(db, v, edges=False).get("engineering"), "quality": quality}
        b = RunInputBinding(run_id=run.id, port_key=node["id"], asset_version_id=v.id, content_hash=v.content_hash, snapshot=snapshot)
        db.add(b)
        out.append(b)
    if issues:
        raise AppError(422, "input_quality", "some inputs do not meet the template's input requirements", {"issues": issues})
    db.flush()
    return out


def bindings_out(db: Session, run_id: uuid.UUID) -> list[dict]:
    rows = db.scalars(select(RunInputBinding).where(RunInputBinding.run_id == run_id).order_by(RunInputBinding.port_key)).all()
    out = []
    for b in rows:
        v = db.get(AssetVersion, b.asset_version_id)
        brief = assets().version_out(db, v, edges=False) if v is not None else None
        out.append({"port_key": b.port_key, "asset_version_id": str(b.asset_version_id), "content_hash": b.content_hash, "snapshot": b.snapshot,
                    "version": brief})
    return out


def check_task_for_run(db: Session, user: User, task_id: uuid.UUID, project_id: uuid.UUID) -> Task:
    t = db.get(Task, task_id)
    if t is None or t.project_id != project_id:
        raise not_found("task")
    acc = project_access(db, user, project_id, "workflow")
    from .agent_runtime import guard_generic_write

    guard_generic_write(db, t.id, "start runs")  # a node's work happens inside its collaboration run
    require_participant(db, acc, t)
    features.require_write(db, "tasks", task_id=t.id)
    if t.status == "cancelled":
        raise conflict("task_cancelled", "a cancelled task cannot start new runs", status=t.status)
    return t


# --- deliverable drafts -------------------------------------------------------------


def _require_in_progress(t: Task) -> None:
    if t.status != "in_progress":
        raise conflict("task_not_in_progress", f"deliverables can only change while the task is in progress (it is {t.status})", status=t.status)


def upsert_deliverable(db: Session, acc: ProjectAccess, task_id: uuid.UUID, *, asset_version_id: uuid.UUID,
                       criterion_ids: list[uuid.UUID], note: str) -> TaskDeliverableDraft:
    require_active(db, acc.project.id, what="selecting deliverables")
    t = lock_task(db, task_id)
    from .agent_runtime import guard_generic_write

    guard_generic_write(db, t.id, "select deliverables")
    require_participant(db, acc, t)
    features.require_write(db, "tasks", task_id=t.id)
    _require_in_progress(t)
    v = _project_version(db, t.project_id, asset_version_id)
    from .actors import of_user

    d = _write_deliverable(db, t, v, criterion_ids=criterion_ids, note=note, actor=of_user(acc.user))
    audit(db, action="deliverable.select", subject_type="task", subject_id=t.id, user_id=acc.user.id, project_id=t.project_id,
          data={"asset_version_id": str(v.id), "criterion_ids": [str(c) for c in criterion_ids]})
    db.flush()
    return d


def _write_deliverable(db: Session, t: Task, v: AssetVersion, *, criterion_ids: list[uuid.UUID], note: str, actor: Any) -> TaskDeliverableDraft:
    from .actors import assign_legacy

    features.require_write(db, "tasks", task_id=t.id)
    valid = set(db.scalars(select(AcceptanceCriterion.id).where(AcceptanceCriterion.task_id == t.id)).all())
    bad = [str(c) for c in criterion_ids if c not in valid]
    if bad:
        raise AppError(422, "unknown_criterion", "criteria must belong to this task", {"criterion_ids": bad})
    d = db.scalar(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == t.id, TaskDeliverableDraft.asset_version_id == v.id))
    if d is None:
        d = TaskDeliverableDraft(task_id=t.id, asset_version_id=v.id, criterion_ids=list(dict.fromkeys(criterion_ids)), note=note)
        db.add(d)
    else:
        d.criterion_ids = list(dict.fromkeys(criterion_ids))
        d.note = note
    assign_legacy(d, "selected_by", actor)
    return d


def upsert_deliverable_as_actor(db: Session, t: Task, *, actor: Any, asset_version_id: uuid.UUID, criterion_ids: list[uuid.UUID],
                                note: str) -> TaskDeliverableDraft:
    """Deliverable selection by a non-HTTP actor (an executing agent, design §3.8).
    Same rules as upsert_deliverable; the caller holds project(share) → task and
    checked the actor (agent_runtime)."""
    from ..models import AuditLog

    features.require_write(db, "tasks", task_id=t.id)
    _require_in_progress(t)
    v = _project_version(db, t.project_id, asset_version_id)
    d = _write_deliverable(db, t, v, criterion_ids=criterion_ids, note=note, actor=actor)
    db.add(AuditLog(project_id=t.project_id, user_id=actor.user_id, agent_id=actor.agent_profile_id, action="deliverable.select",
                    subject_type="task", subject_id=str(t.id),
                    data={"asset_version_id": str(v.id), "criterion_ids": [str(c) for c in criterion_ids], "actor_kind": actor.kind}))
    db.flush()
    return d


def delete_deliverable(db: Session, user: User, deliverable_id: uuid.UUID) -> None:
    d = db.get(TaskDeliverableDraft, deliverable_id)
    if d is None:
        raise not_found("deliverable")
    t, acc = load_task(db, user, d.task_id, "workflow")
    require_active(db, t.project_id, what="changing deliverables")
    t = lock_task(db, t.id)
    from .agent_runtime import guard_generic_write

    guard_generic_write(db, t.id, "change deliverables")
    require_participant(db, acc, t)
    features.require_write(db, "tasks", task_id=t.id)
    _require_in_progress(t)
    db.delete(d)
    audit(db, action="deliverable.remove", subject_type="task", subject_id=t.id, user_id=user.id, project_id=t.project_id,
          data={"asset_version_id": str(d.asset_version_id)})
    db.flush()


def reject_status_field(status: str | None) -> None:
    if status is not None:
        raise bad_request("use_transitions", "status changes go through POST /tasks/{id}/transitions, submissions or approvals")


def with_dependency_info(db: Session, tasks: list[Task]) -> list[dict]:
    """task_out plus predecessors and the ones not done yet ("waiting on")."""
    ids = [t.id for t in tasks]
    out = []
    preds: dict[uuid.UUID, list[uuid.UUID]] = {}
    if ids:
        for d in db.scalars(select(TaskDependency).where(TaskDependency.successor_id.in_(ids))).all():
            preds.setdefault(d.successor_id, []).append(d.predecessor_id)
    all_pred = {p for ps in preds.values() for p in ps}
    status = {t.id: (t.number, t.status) for t in db.scalars(select(Task).where(Task.id.in_(all_pred))).all()} if all_pred else {}
    for t in tasks:
        data = task_out(t)
        ps = preds.get(t.id, [])
        data["predecessors"] = [{"id": str(p), "key": f"T-{status[p][0]}", "status": status[p][1]} for p in ps if p in status]
        data["waiting_on"] = [x for x in data["predecessors"] if x["status"] not in ("done", "cancelled")]
        out.append(data)
    return out
