"""Tasks, criteria, dependencies, baselines, task runs, deliverables,
submissions and impact notices (contract §4, backend B)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..errors import AppError, conflict, not_found
from ..models import ImpactNotice, Task, TaskBaselineSnapshot, TaskSubmission, User, WorkflowRun
from ..security import current_user, project_access
from ..services import feature_flags as features
from ..services import submissions as sub_svc
from ..services import tasks as svc
from ..services import templates as tpl
from ..services.audit import audit
from ..services.tasks import AssetType, RuleSpec
from .workflows import _load_workflow, run_out, run_summaries, start_run

router = APIRouter(tags=["tasks"])

Priority = Literal["low", "normal", "high", "urgent"]


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=limits.OBJECTIVE_CHARS)
    owner_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    priority: Priority = "normal"
    # Required for its stage's review (P4); optional tasks never block a stage.
    required: bool = True
    blocked_reason: str | None = Field(default=None, max_length=2000)
    start_date: datetime | None = None
    due_date: datetime | None = None
    collaborator_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class TaskPatch(BaseModel):
    expected_version: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    objective: str | None = Field(default=None, max_length=limits.OBJECTIVE_CHARS)
    owner_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    priority: Priority | None = None
    required: bool | None = None
    blocked_reason: str | None = Field(default=None, max_length=2000)
    start_date: datetime | None = None
    due_date: datetime | None = None
    # Present only to give a clear error: status never changes through PATCH.
    status: str | None = None


class TransitionIn(BaseModel):
    action: Literal["start", "cancel", "reopen"]
    reason: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    expected_version: int
    # cancel only: true = cancel the task's active runs, false = let them finish.
    cancel_runs: bool | None = None


class CriterionIn(BaseModel):
    description: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)
    required: bool = True
    allowed_types: list[AssetType] = Field(default_factory=list, max_length=6)
    rule_spec: RuleSpec | None = None
    manual_required: bool = False


class CriterionPatch(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=limits.DESCRIPTION_CHARS)
    required: bool | None = None
    allowed_types: list[AssetType] | None = Field(default=None, max_length=6)
    rule_spec: RuleSpec | None = None
    manual_required: bool | None = None
    expected_version: int | None = None


class DependenciesIn(BaseModel):
    predecessor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class CollaboratorsIn(BaseModel):
    user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class BaselineIn(BaseModel):
    # slot -> asset version id (null removes the slot); merged into the current baseline unless replace.
    items: dict[str, uuid.UUID | None] = Field(min_length=1, max_length=50)
    reason: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    replace: bool = False
    expected_version: int | None = None


class TaskRunIn(BaseModel):
    workflow_id: uuid.UUID
    # Published version to run (default: the latest published one). Task runs never use the draft.
    template_version_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    purpose: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    title: str = Field(default="", max_length=200)
    # W7 / S5: frozen on the run. None = not chosen: the project default (standard unless "default Turbo" is on).
    execution_policy: Literal["standard", "turbo"] | None = None


class DeliverableIn(BaseModel):
    asset_version_id: uuid.UUID
    criterion_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class SubmissionIn(BaseModel):
    note: str = Field(default="", max_length=limits.LONG_NOTE_CHARS)
    expected_version: int


class ResolveIn(BaseModel):
    note: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    status: Literal["resolved", "dismissed"] = "resolved"


# --- tasks ---------------------------------------------------------------------


@router.get("/projects/{project_id}/tasks")
def list_tasks(
    project_id: uuid.UUID,
    status: str | None = None,
    owner_id: uuid.UUID | None = None,
    q: str | None = None,
    milestone_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project_access(db, user, project_id)
    tasks = svc.list_tasks(db, project_id, status=status, owner_id=owner_id, q=q, milestone_id=milestone_id)
    return svc.with_dependency_info(db, tasks)


@router.post("/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: uuid.UUID, body: TaskIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    acc = project_access(db, user, project_id, "workflow")
    data = body.model_dump(exclude={"collaborator_ids"})
    t = svc.create_task(db, acc, data, body.collaborator_ids)
    db.commit()
    return svc.task_detail(db, user, t, acc)


class TaskDraftIn(BaseModel):
    text: str = Field(min_length=1, max_length=limits.CHAT_TEXT_CHARS)


@router.post("/projects/{project_id}/tasks/draft")
def draft_task(project_id: uuid.UUID, body: TaskDraftIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """AI-suggested task draft (title, objective, criteria, workflow). Never
    stored: the person confirms it in the create-task dialog. 503 ai_unavailable
    when no model is usable (simulated mode answers deterministically)."""
    from ..services import stages

    project_access(db, user, project_id, "manage")
    out = stages.draft_task(db, project_id, body.text)
    db.rollback()  # nothing from drafting is persisted
    return out


@router.get("/tasks/{task_id}")
def get_task(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    t, acc = svc.load_task(db, user, task_id)
    return svc.task_detail(db, user, t, acc)


@router.patch("/tasks/{task_id}")
def patch_task(task_id: uuid.UUID, body: TaskPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    svc.reject_status_field(body.status)
    _, acc = svc.load_task(db, user, task_id, "workflow")
    data = body.model_dump(exclude_unset=True, exclude={"expected_version", "status"})
    if "title" in data and data["title"] is None:
        raise AppError(422, "title_required", "a task needs a title")
    if "priority" in data and data["priority"] is None:
        data.pop("priority")
    if "required" in data and data["required"] is None:
        data.pop("required")
    t = svc.patch_task(db, acc, task_id, data, body.expected_version)
    db.commit()
    return svc.task_detail(db, user, t, acc)


@router.post("/tasks/{task_id}/transitions")
def transition(task_id: uuid.UUID, body: TransitionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    t, extra = svc.transition(db, acc, task_id, action=body.action, reason=body.reason, expected_version=body.expected_version,
                              cancel_runs=body.cancel_runs)
    db.commit()
    out = svc.task_detail(db, user, t, acc)
    out["transition"] = {"action": body.action, **extra}
    return out


@router.put("/tasks/{task_id}/dependencies")
def set_dependencies(task_id: uuid.UUID, body: DependenciesIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    t = svc.set_dependencies(db, acc, task_id, body.predecessor_ids)
    db.commit()
    return svc.task_detail(db, user, t, acc)


@router.put("/tasks/{task_id}/collaborators")
def set_collaborators(task_id: uuid.UUID, body: CollaboratorsIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    t = svc.lock_task(db, task_id)
    svc.require_edit(acc, t)
    svc.set_collaborators(db, t, body.user_ids)
    svc.bump(t)
    audit(db, action="task.collaborators", subject_type="task", subject_id=t.id, user_id=user.id, project_id=t.project_id,
          data={"user_ids": [str(u) for u in body.user_ids]})
    db.commit()
    return svc.task_detail(db, user, t, acc)


# --- criteria ----------------------------------------------------------------------


@router.get("/tasks/{task_id}/criteria")
def list_criteria(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    t, acc = svc.load_task(db, user, task_id)
    return svc.task_detail(db, user, t, acc)["criteria"]


@router.post("/tasks/{task_id}/criteria", status_code=201)
def add_criterion(task_id: uuid.UUID, body: CriterionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    c = svc.add_criterion(db, acc, task_id, body.model_dump())
    db.commit()
    return svc.criterion_out(c)


@router.patch("/criteria/{criterion_id}")
def patch_criterion(criterion_id: uuid.UUID, body: CriterionPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    c, _, acc = svc.load_criterion(db, user, criterion_id, "workflow")
    c = svc.patch_criterion(db, acc, c, body.model_dump(exclude_unset=True, exclude={"expected_version"}), body.expected_version)
    db.commit()
    return svc.criterion_out(c)


@router.delete("/criteria/{criterion_id}", status_code=204)
def delete_criterion(criterion_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    c, _, acc = svc.load_criterion(db, user, criterion_id, "workflow")
    svc.delete_criterion(db, acc, c)
    db.commit()


# --- baselines -------------------------------------------------------------------------


@router.get("/tasks/{task_id}/baselines")
def list_baselines(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    t, _ = svc.load_task(db, user, task_id)
    rows = db.scalars(select(TaskBaselineSnapshot).where(TaskBaselineSnapshot.task_id == t.id).order_by(TaskBaselineSnapshot.seq.desc())).all()
    return [svc.baseline_out(db, s, t.current_baseline_id) for s in rows]


@router.post("/tasks/{task_id}/baselines", status_code=201)
def adopt_baseline(task_id: uuid.UUID, body: BaselineIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    snap, notices, created = svc.adopt_baseline(db, acc, task_id, items=body.items, reason=body.reason, replace=body.replace,
                                                expected_version=body.expected_version)
    db.commit()
    t = db.get(Task, task_id)
    assert t is not None
    return {"baseline": svc.baseline_out(db, snap, t.current_baseline_id), "created": created,
            "impacts": [svc.notice_out(n) for n in notices], "task": svc.task_out(t)}


# --- runs ---------------------------------------------------------------------------------


@router.get("/tasks/{task_id}/runs")
def list_task_runs(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    t, _ = svc.load_task(db, user, task_id)
    runs = db.scalars(select(WorkflowRun).where(WorkflowRun.task_id == t.id).order_by(WorkflowRun.created_at.desc())).all()
    return run_summaries(db, list(runs))


@router.post("/tasks/{task_id}/runs", status_code=201)
def start_task_run(task_id: uuid.UUID, body: TaskRunIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    t, acc = svc.load_task(db, user, task_id, "workflow")
    from ..services.agent_runtime import guard_generic_write

    guard_generic_write(db, t.id, "start runs")  # 409 use_collaboration_actions: a node's work runs inside its collaboration run
    svc.require_participant(db, acc, t)
    features.require_enabled("tasks")
    features.require_choices(execution_policy=body.execution_policy)
    for params in body.params.values():
        features.require_choices(model_config_id=params.get("model_config_id"))
    w = _load_workflow(db, user, body.workflow_id, "workflow")
    if w.project_id != t.project_id:
        raise not_found("workflow")
    if w.archived_at is not None:
        raise conflict("workflow_archived", "the workflow is archived")
    graph, version = tpl.resolve_run(db, user, w, template_version_id=body.template_version_id, draft=False, params=body.params, task=t)
    run = start_run(db, user, project_id=t.project_id, graph_raw=graph, part_id=body.part_id or t.part_id,
                    title=body.title, origin="workflow", workflow=w, task=t, purpose=body.purpose, template_version=version,
                    execution_policy=body.execution_policy)
    db.commit()
    return run_out(db, user, run)


# --- deliverables ------------------------------------------------------------------------


@router.get("/tasks/{task_id}/deliverables")
def list_deliverables(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    t, acc = svc.load_task(db, user, task_id)
    return svc.task_detail(db, user, t, acc)["deliverables"]


@router.post("/tasks/{task_id}/deliverables", status_code=201)
def add_deliverable(task_id: uuid.UUID, body: DeliverableIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    d = svc.upsert_deliverable(db, acc, task_id, asset_version_id=body.asset_version_id, criterion_ids=body.criterion_ids, note=body.note)
    db.commit()
    return svc.deliverable_out(db, d)


@router.delete("/deliverables/{deliverable_id}", status_code=204)
def delete_deliverable(deliverable_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    svc.delete_deliverable(db, user, deliverable_id)
    db.commit()


@router.get("/tasks/{task_id}/checks")
def preview_checks(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Dry run of the submission checks on the current deliverable drafts."""
    t, _ = svc.load_task(db, user, task_id)
    return sub_svc.preview_checks(db, t)


# --- submissions ----------------------------------------------------------------------------


@router.get("/tasks/{task_id}/submissions")
def list_submissions(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    t, _ = svc.load_task(db, user, task_id)
    rows = db.scalars(select(TaskSubmission).where(TaskSubmission.task_id == t.id).order_by(TaskSubmission.seq.desc())).all()
    return [sub_svc.submission_out(db, s) for s in rows]


@router.post("/tasks/{task_id}/submissions", status_code=201)
def create_submission(task_id: uuid.UUID, body: SubmissionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_task(db, user, task_id, "workflow")
    s = sub_svc.create_submission(db, acc, task_id, note=body.note, expected_version=body.expected_version)
    db.commit()
    t = db.get(Task, task_id)
    assert t is not None
    return {**sub_svc.submission_out(db, s), "task": svc.task_out(t)}


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    s, _, _ = sub_svc.load_submission(db, user, submission_id)
    return sub_svc.submission_out(db, s)


@router.post("/submissions/{submission_id}/withdraw")
def withdraw_submission(submission_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    s = sub_svc.withdraw(db, user, submission_id)
    db.commit()
    t = db.get(Task, s.task_id)
    assert t is not None
    return {**sub_svc.submission_out(db, s), "task": svc.task_out(t)}


# --- impact notices ----------------------------------------------------------------------------


@router.get("/projects/{project_id}/impact-notices")
def list_impacts(
    project_id: uuid.UUID,
    status: str | None = "open",
    task_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project_access(db, user, project_id)
    stmt = select(ImpactNotice).where(ImpactNotice.project_id == project_id).order_by(ImpactNotice.created_at.desc())
    if status and status != "all":
        stmt = stmt.where(ImpactNotice.status == status)
    if task_id:
        stmt = stmt.where(ImpactNotice.task_id == task_id)
    return [svc.notice_out(n) for n in db.scalars(stmt).all()]


@router.post("/impact-notices/{notice_id}/resolve")
def resolve_impact(notice_id: uuid.UUID, body: ResolveIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    n = db.get(ImpactNotice, notice_id)
    if n is None:
        raise not_found("impact notice")
    acc = project_access(db, user, n.project_id, "workflow")
    t = db.get(Task, n.task_id) if n.task_id else None
    if not (acc.role == "owner" or (t is not None and svc.can_edit(acc, t))):
        raise AppError(403, "forbidden", "only the task owner or a project owner can resolve this notice")
    features.require_enabled("stages" if n.milestone_id else "tasks")
    n = db.scalar(select(ImpactNotice).where(ImpactNotice.id == notice_id).with_for_update().execution_options(populate_existing=True))
    assert n is not None
    if n.status != "open":
        return svc.notice_out(n)  # idempotent
    n.status = body.status
    n.resolved_by = user.id
    n.resolution_note = body.note
    n.resolved_at = datetime.now(UTC)
    audit(db, action=f"impact.{body.status}", subject_type="impact_notice", subject_id=n.id, user_id=user.id, project_id=n.project_id,
          data={"note": body.note})
    db.commit()
    return svc.notice_out(n)


@router.get("/tasks/{task_id}/activity")
def task_activity(task_id: uuid.UUID, limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Audit trail of the task (transitions with reasons, edits, criteria, baselines, dependencies)."""
    t = db.get(Task, task_id)
    if t is None:
        raise not_found("task")
    project_access(db, user, t.project_id)
    from ..models import AuditLog

    rows = db.scalars(
        select(AuditLog).where(AuditLog.subject_type == "task", AuditLog.subject_id == str(task_id))
        .order_by(AuditLog.created_at.desc()).limit(min(limit, 500))
    ).all()
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_({r.user_id for r in rows if r.user_id}))).all()} if rows else {}
    return [
        {"id": str(r.id), "action": r.action, "data": r.data, "user_id": str(r.user_id) if r.user_id else None,
         "user_name": names.get(r.user_id), "at": r.created_at.isoformat()}
        for r in rows
    ]
