"""Deployment features and the audited, run-scoped completion exception.

Call guards after the existing authorization checks and before business writes.
Only startup records a rollout: a read/request must never enroll a newer run in
an existing freeze. The audit ledger is append-only and survives restarts.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..errors import conflict
from ..models import AuditLog, CollaborationNodeTask, CollaborationRun, WorkflowRun

FeatureName = Literal["collaboration", "tasks", "stages", "process_modules", "template_library", "turbo_choice", "model_choice"]
FEATURE_NAMES: tuple[FeatureName, ...] = get_args(FeatureName)
CONTINUATION_FEATURES = frozenset({"collaboration", "tasks"})
NONTERMINAL_RUN_STATUSES = ("queued", "running", "waiting")
ROLLOUT_ACTION = "features.rollout"
COHORT_ACTION = "features.continuation"
_ROLLOUT_LOCK = 76422918714231
_RUN_SCOPE: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar("partroute_feature_run", default=None)
_CLOSURE_SCOPE: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar("partroute_feature_close", default=None)


def effective_features(settings: Settings | None = None) -> dict[FeatureName, bool]:
    settings = settings or get_settings()
    result = {name: bool(getattr(settings, f"feature_{name}")) for name in FEATURE_NAMES}
    result["collaboration"] = result["collaboration"] and result["tasks"]
    return result


def enabled(feature: FeatureName) -> bool:
    return effective_features()[feature]


def _disabled(feature: FeatureName) -> None:
    raise conflict("feature_disabled", "this feature is disabled for new or ordinary writes", feature=feature)


def require_enabled(feature: FeatureName) -> None:
    """New work never inherits a historical run's completion exception."""
    if not enabled(feature):
        _disabled(feature)


def require_choices(*, model_config_id: Any = None, execution_policy: Any = None) -> None:
    """Fresh ordinary requests cannot override hidden choices; snapshots do not call this."""
    if model_config_id is not None:
        require_enabled("model_choice")
    if execution_policy is not None:
        require_enabled("turbo_choice")


def _latest_rollout(db: Session) -> AuditLog | None:
    return db.scalar(select(AuditLog).where(AuditLog.action == ROLLOUT_ACTION, AuditLog.project_id.is_(None))
                     .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1))


def record_rollout(db: Session, *, source: str = "startup") -> AuditLog:
    """Record a changed effective snapshot and enroll only newly disabled scopes.

    API and standalone/inline workers call this before accepting work. The
    transaction lock deduplicates concurrent starts. Existing disabled epochs
    are carried forward unchanged, including when an unrelated flag changes.
    Caller commits; no business rows or historical snapshots are modified.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ROLLOUT_LOCK})
    current = effective_features()
    previous = _latest_rollout(db)
    if previous is not None and previous.data.get("features") == current:
        return previous
    before = previous.data.get("features", {}) if previous is not None else {}
    epochs = dict(previous.data.get("epochs", {})) if previous is not None else {}
    rollout_id = uuid.uuid4()
    newly_disabled: list[FeatureName] = []
    for feature, value in current.items():
        if value:
            epochs.pop(feature, None)
        elif before.get(feature, True) or feature not in epochs:
            epochs[feature] = str(rollout_id)
            newly_disabled.append(feature)
    at = datetime.now(UTC)
    rollout = AuditLog(id=rollout_id, action=ROLLOUT_ACTION, subject_type="feature_rollout", subject_id=str(rollout_id),
                       data={"features": current, "epochs": epochs, "source": source, "version": 1}, created_at=at)
    db.add(rollout)
    if CONTINUATION_FEATURES.intersection(newly_disabled):
        runs = db.scalars(select(WorkflowRun).where(WorkflowRun.status.in_(NONTERMINAL_RUN_STATUSES))).all()
        collaboration_ids = set(db.scalars(select(CollaborationRun.run_id)))
        for run in runs:
            scopes = {feature: epochs[feature] for feature in newly_disabled
                      if (feature == "collaboration" and run.id in collaboration_ids)
                      or (feature == "tasks" and (run.task_id is not None or run.id in collaboration_ids))}
            if scopes:
                db.add(AuditLog(action=COHORT_ACTION, subject_type="workflow_run", subject_id=str(run.id),
                                project_id=run.project_id, created_at=at,
                                data={"epochs": scopes, "rollout_id": str(rollout_id), "status": run.status,
                                      "paused": run.paused, "task_id": str(run.task_id) if run.task_id else None}))
    db.flush()
    return rollout


def _resolve_run(db: Session, run_id: uuid.UUID | None, task_id: uuid.UUID | None, *, live: bool = True) -> WorkflowRun | None:
    if task_id is not None:
        nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.task_id == task_id))
        if nt is not None:
            if run_id is not None and nt.run_id != run_id:
                return None
            run_id = nt.run_id
        elif run_id is None:
            return None  # A generic task has no implicit completion exception.
    run = db.get(WorkflowRun, run_id) if run_id is not None else None
    if run is None or (live and run.status not in NONTERMINAL_RUN_STATUSES):
        return None
    if task_id is not None and (nt is None and run.task_id != task_id):
        return None
    return run


def can_continue(db: Session, feature: FeatureName, *, run_id: uuid.UUID | None = None,
                 task_id: uuid.UUID | None = None) -> bool:
    """Read-only eligibility check, also used to project legal historical actions.

    Callers still enforce user roles, node actions, leases, budgets and cancel
    checks. Feature enablement is not itself proof of a run's existence.
    """
    run = _resolve_run(db, run_id, task_id)
    if run is None:
        return False
    if enabled(feature):
        return True
    if feature not in CONTINUATION_FEATURES:
        return False
    return _in_cohort(db, feature, run)


def _in_cohort(db: Session, feature: FeatureName, run: WorkflowRun) -> bool:
    rollout = _latest_rollout(db)
    if rollout is None or rollout.data.get("features") != effective_features():
        return False
    epoch = rollout.data.get("epochs", {}).get(feature)
    if not epoch:
        return False
    return db.scalar(select(AuditLog.id).where(
        AuditLog.action == COHORT_ACTION, AuditLog.subject_type == "workflow_run", AuditLog.subject_id == str(run.id),
        AuditLog.project_id == run.project_id, AuditLog.data["epochs"][feature].astext == epoch,
    ).limit(1)) is not None


def require_run_continuation(db: Session, run_id: uuid.UUID) -> None:
    """Validate original runtime actions; never use this for a fork/new run."""
    if enabled("collaboration") and enabled("tasks"):
        return
    run = db.get(WorkflowRun, run_id)
    if run is None:
        _disabled("collaboration")
    is_collaboration = db.get(CollaborationRun, run_id) is not None
    relevant: tuple[FeatureName, ...] = (("collaboration", "tasks") if is_collaboration else ("tasks",) if run.task_id else ())
    for feature in relevant:
        if not enabled(feature) and not can_continue(db, feature, run_id=run_id):
            _disabled(feature)


@contextmanager
def continuation_scope(db: Session, run_id: uuid.UUID) -> Iterator[None]:
    """Server-only scope: all nested task writes must match this original run."""
    require_run_continuation(db, run_id)
    token = _RUN_SCOPE.set(run_id)
    try:
        yield
    finally:
        _RUN_SCOPE.reset(token)


def require_write(db: Session, feature: FeatureName, *, run_id: uuid.UUID | None = None,
                  task_id: uuid.UUID | None = None) -> None:
    if enabled(feature):
        return
    scoped_run = _RUN_SCOPE.get()
    if scoped_run is None or (run_id is not None and run_id != scoped_run):
        _disabled(feature)
    if not can_continue(db, feature, run_id=scoped_run, task_id=task_id):
        _disabled(feature)


@contextmanager
def closure_scope(db: Session, run_id: uuid.UUID) -> Iterator[None]:
    """Only pending-submission withdrawal can use this terminal cleanup scope.

    The engine can fail/cancel a run before it closes its stranded node tasks.
    This does not enable any other mutation or reopen a terminal run.
    """
    run = db.get(WorkflowRun, run_id)
    for feature in ("collaboration", "tasks"):
        if not enabled(feature) and (run is None or not _in_cohort(db, feature, run)):
            _disabled(feature)
    token = _CLOSURE_SCOPE.set(run_id)
    try:
        yield
    finally:
        _CLOSURE_SCOPE.reset(token)


def can_close_run(db: Session, run_id: uuid.UUID) -> bool:
    """Worker sweeps may only settle tasks belonging to the audited freeze cohort."""
    if enabled("collaboration") and enabled("tasks"):
        return True
    run = db.get(WorkflowRun, run_id)
    return run is not None and all(enabled(feature) or _in_cohort(db, feature, run) for feature in ("collaboration", "tasks"))


def require_task_closure(db: Session, task_id: uuid.UUID) -> None:
    """Used exclusively by submissions.withdraw_locked; normal calls remain guarded."""
    if enabled("tasks"):
        return
    scoped_run = _CLOSURE_SCOPE.get()
    run = _resolve_run(db, scoped_run, task_id, live=False) if scoped_run is not None else None
    if run is not None and _in_cohort(db, "tasks", run):
        return
    require_write(db, "tasks", task_id=task_id)


def guard_graph(graph: Any) -> None:
    """Guard every new graph save/start independently of draft validation rules."""
    raw = graph.model_dump(mode="json") if hasattr(graph, "model_dump") else graph
    nodes = raw.get("nodes", []) if isinstance(raw, dict) else []
    if any(isinstance(node, dict) and node.get("type") == "business_task" for node in nodes):
        require_enabled("tasks")
        require_enabled("collaboration")
