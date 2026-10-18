"""Agent runtime: node-task queue, agent executions, independent review and
DAG gating (design §0, §3.6–3.9, §4 C04–C06).

One business node = one Task = one ``collaboration_node_tasks`` row whose
``phase`` is the node's internal stage. The ``business_task`` handler only
prepares the node (``prepare_node``): it binds the upstream formal outputs,
registers them as ``input`` resources and, for an agent executor, starts the
Task; the NodeRun then waits (``waiting_human``) until the Task is accepted by
a legitimate actor. Acceptance (human HTTP decision or agent decision through
the same service) calls ``on_task_accepted``: formal outputs are frozen,
written to ``NodeRun.outputs`` and ``engine.complete_waiting(approved=True)``
releases the downstream nodes exactly once. A rejection never fails the
NodeRun: the phase goes to ``rework``.

Agents never run inside a handler. ``claim_one`` claims a node task with
``FOR UPDATE SKIP LOCKED`` (phase + lease), creates an ``agent_executions``
row, and ``run_claim`` runs the model loop outside any transaction. Results
are written back only while the lease token still matches (A15); a stale
attempt is kept as ``superseded`` history.

Lock order (design §3.7): project(share) → task → submission → request → run → node_task.

P1 (execution-layer design §6): an execute attempt runs through an executor (``app/ai/harness``: native or tool_agent,
chosen by the frozen profile's supported runtime; native for permitted simulation). :class:`_AttemptControl` gives it the
control callbacks: ``alive`` (lease, profile, run, stop request, duration, steps / tool calls / cost),
``call_tool``, ``record_model_call`` (an ai_calls row at once) and ``emit`` (a timeline event under the lease).
The write-back below ``_write_back_execute`` / ``_write_back_review`` is unchanged.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import policy as exec_policy
from ..ai import collaboration_agent as _collab_agent  # noqa: F401 - registers the simulated agent_execute / agent_review responders
from ..ai.collaboration_agent import deliverable_problems
from ..ai.harness.base import SUPPORTED_RUNTIMES, AttemptStopped, HarnessEvent, safe_args
from ..ai.providers.base import ProviderError, clip
from ..config import get_settings
from ..db import session_scope
from ..errors import AppError, conflict, forbidden, not_found
from ..models import (
    AcceptanceCriterion,
    AgentExecution,
    AgentExecutionEvent,
    AgentProfile,
    ApprovalDecision,
    ApprovalRequest,
    Asset,
    AssetVersion,
    AuditLog,
    CollaborationNodeTask,
    CollaborationRun,
    FileVersion,
    ImpactNotice,
    ModelConfigRow,
    ModelRevision,
    NodeRun,
    Part,
    Project,
    ProjectMember,
    RunInputBinding,
    Task,
    TaskDeliverableDraft,
    TaskDependency,
    TaskSubmission,
    User,
    WorkflowResource,
    WorkflowRun,
)
from ..storage import read_blob
from . import actors
from . import agent_tools
from . import ai as ai_svc
from . import collaboration_resources as resources
from . import knowledge
from . import outbox
from . import feature_flags as features
from .actors import ActorRef
from .audit import audit
from .tasks import canon_hash

log = logging.getLogger("partroute.agent_runtime")

LEASE_MAX_S = 3600
DEFAULT_LIMITS: dict[str, Any] = {"max_steps": 8, "max_duration_s": 300, "max_attempts": 2, "max_reworks": 3, "max_cost_fen": None}
EMPTY_BUDGET: dict[str, Any] = {"steps": 0, "duration_ms": 0, "attempts": 0, "reworks": 0, "cost_fen": None, "cost_unknown": False,
                                "tokens_in": 0, "tokens_out": 0}
TERMINAL_RUN = {"succeeded", "partial", "failed", "cancelled"}
# W7 Turbo: a node whose real outputs were released downstream without review / acceptance (never "accepted").
COMPLETED_UNVERIFIED = "completed_unverified"
# Phases whose formal outputs downstream nodes may bind (accepted in standard runs, completed_unverified in Turbo).
RELEASED_PHASES = ("accepted", COMPLETED_UNVERIFIED)
# The review / approval / acceptance checks a Turbo node skips (W0 §2.1).
TURBO_SKIPPED_CHECKS = ("review.agent", "review.human", "acceptance.final")
# Blocks that wait for the scheduler, not a person: they never fail a Turbo node.
TRANSIENT_BLOCKS = frozenset({"upstream_pending", "not_started", "upstream_invalidated"})
NODE_ACTIONS = ("start", "add_deliverable", "check", "submit", "withdraw", "reopen", "cancel", "adopt_baseline")
RUN_ACTIONS = ("pause", "resume", "cancel", "fork_semi_auto")
MAX_REPAIRS = 1
DOC_EXCERPT_CHARS = 20_000


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --- limits and budget (design §3.9) ----------------------------------------------------------------


def limits_of(raw: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_LIMITS)
    for k, v in (raw or {}).items():
        if k == "max_cost_fen":
            out[k] = v
        elif k in out and v is not None:
            out[k] = v
    return out


def budget_of(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {**EMPTY_BUDGET, **(raw or {})}


def lease_seconds(limits: dict[str, Any]) -> int:
    return int(min(int(limits.get("max_duration_s") or DEFAULT_LIMITS["max_duration_s"]) + 30, LEASE_MAX_S))


def budget_exceeded(limits: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any] | None:
    """Reason a new attempt may not start, or None. Cumulative counters never reset (A50)."""
    b = budget_of(budget)
    if b["attempts"] >= int(limits["max_attempts"]):
        return {"code": "budget_exceeded", "limit": "max_attempts", "value": b["attempts"], "max": limits["max_attempts"],
                "message": "technical attempts exhausted"}
    if b["reworks"] > int(limits["max_reworks"]):
        return {"code": "budget_exceeded", "limit": "max_reworks", "value": b["reworks"], "max": limits["max_reworks"],
                "message": "rework limit reached"}
    return cost_exceeded(limits, b)


def cost_exceeded(limits: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any] | None:
    """The cost part of the budget check. Unknown cost counts as exceeding only when
    a cap is set (it is never counted as zero); without a cap it is only reported."""
    cap = limits.get("max_cost_fen")
    if cap is None:
        return None
    b = budget_of(budget)
    if b.get("cost_unknown") and int(b.get("steps") or 0) > 0:
        return {"code": "budget_exceeded", "limit": "max_cost_fen", "reason": "cost_unknown", "value": None, "max": cap,
                "message": "a model call could not be priced (no pricing or no token counts) and a cost cap is set; unknown cost is never counted as zero"}
    if (b.get("cost_fen") or 0) > int(cap):
        return {"code": "budget_exceeded", "limit": "max_cost_fen", "value": b.get("cost_fen"), "max": cap, "message": "cost cap reached"}
    return None


def priced(pricing: dict[str, Any] | None) -> bool:
    return bool(pricing) and pricing.get("in_fen_per_1k") is not None and pricing.get("out_fen_per_1k") is not None


def unpriced_under_cap(limits: dict[str, Any], snap: dict[str, Any], role: str) -> dict[str, Any] | None:
    """A cost cap applies (the node's frozen cap, or the reviewing agent's own cap) but
    the claiming agent has no pricing: its cost could only be unknown, so it is not
    started at all (design §3.9; unknown cost can never be released)."""
    cap = limits.get("max_cost_fen")
    if cap is None and role != "execute":
        cap = limits_of(snap.get("limits")).get("max_cost_fen")
    if cap is None or priced(snap.get("pricing")):
        return None
    return {"code": "budget_exceeded", "limit": "max_cost_fen", "reason": "cost_unknown", "value": None, "max": cap, "role": role,
            "agent_profile_id": snap.get("id"),
            "message": f"the {role} agent has no pricing while a cost cap of {cap} fen applies; its cost would be unknown, so it is not started "
                       "(give the profile a pricing or remove the cap, then fork or start a new run)"}


def usage_of(calls: list[Any], pricing: dict[str, Any] | None, duration_ms: int) -> dict[str, Any]:
    """Tokens and cost of one attempt: cost from the profile pricing per 1k tokens, else unknown (never 0)."""
    tokens_in = tokens_out = 0
    known = True
    n = 0
    for c in calls:
        data = c.model_dump() if hasattr(c, "model_dump") else dict(c)
        n += 1
        if data.get("tokens_in") is None or data.get("tokens_out") is None:
            known = False
        tokens_in += int(data.get("tokens_in") or 0)
        tokens_out += int(data.get("tokens_out") or 0)
    cost: int | None = None
    cost_unknown = False
    if n:
        if pricing and known and pricing.get("in_fen_per_1k") is not None and pricing.get("out_fen_per_1k") is not None:
            cost = int(math.ceil(tokens_in / 1000 * float(pricing["in_fen_per_1k"]) + tokens_out / 1000 * float(pricing["out_fen_per_1k"])))
        else:
            cost_unknown = True
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "calls": n, "cost_fen": cost, "cost_unknown": cost_unknown, "duration_ms": duration_ms}


def _add_usage(budget: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    """Accumulate one attempt's usage. cost_unknown becomes true only when a call
    happened whose cost could not be priced (no pricing or no token counts)."""
    b = budget_of(budget)
    if int(b["steps"]) == 0:
        b["cost_unknown"] = False  # no call accounted yet: nothing can be unknown (older rows started as unknown)
    calls = int(usage.get("calls") or 0)
    b["steps"] = int(b["steps"]) + calls
    b["duration_ms"] = int(b["duration_ms"]) + int(usage.get("duration_ms") or 0)
    b["tokens_in"] = int(b.get("tokens_in") or 0) + int(usage.get("tokens_in") or 0)
    b["tokens_out"] = int(b.get("tokens_out") or 0) + int(usage.get("tokens_out") or 0)
    if calls and usage.get("cost_unknown"):
        b["cost_unknown"] = True
    elif calls and usage.get("cost_fen") is not None:
        b["cost_fen"] = int(b.get("cost_fen") or 0) + int(usage["cost_fen"])
    return b


# --- rows, locks, snapshots -----------------------------------------------------------------------


def node_task_for_task(db: Session, task_id: uuid.UUID) -> CollaborationNodeTask | None:
    return db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.task_id == task_id))


def node_task_of(db: Session, run_id: uuid.UUID, node_id: str) -> CollaborationNodeTask | None:
    return db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run_id, CollaborationNodeTask.node_id == node_id))


def lock_node_task(db: Session, node_task_id: uuid.UUID) -> CollaborationNodeTask:
    nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.id == node_task_id).with_for_update().execution_options(populate_existing=True))
    if nt is None:
        raise not_found("node task")
    return nt


def lock_run_tasks(db: Session, run_id: uuid.UUID) -> list[Task]:
    """project(share) then every node task's Task in id order: the prefix of the
    global lock order for callers that will lock the run afterwards (cancel)."""
    from .lifecycle import lock_project

    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    lock_project(db, run.project_id)
    ids = sorted(db.scalars(select(CollaborationNodeTask.task_id).where(CollaborationNodeTask.run_id == run_id)).all(), key=str)
    out = []
    for tid in ids:
        out.append(db.scalar(select(Task).where(Task.id == tid).with_for_update().execution_options(populate_existing=True)))
    return [t for t in out if t is not None]


def _collab(db: Session, run_id: uuid.UUID) -> CollaborationRun:
    c = db.get(CollaborationRun, run_id)
    if c is None:
        raise conflict("not_collaboration", "this run is not a collaboration instance")
    return c


def profile_snapshot(db: Session, collab: CollaborationRun | None, agent_id: uuid.UUID | None) -> dict[str, Any]:
    """The frozen profile of an agent in this instance (role_snapshots), falling
    back to the live row. Enabled state is always read live (A16)."""
    if agent_id is None:
        return {}
    snap = dict((collab.role_snapshots or {}).get(str(agent_id)) or {}) if collab is not None else {}
    live = db.get(AgentProfile, agent_id)
    if live is not None:
        snap.setdefault("id", str(live.id))
        snap.setdefault("name", live.name)
        snap.setdefault("purpose", live.purpose)
        snap.setdefault("role_prompt", live.role_prompt)
        snap.setdefault("allowed_tools", list(live.allowed_tools or []))
        snap.setdefault("limits", dict(live.limits or {}))
        snap.setdefault("pricing", live.pricing)
        snap.setdefault("version", live.version)
        snap.setdefault("knowledge_scope", dict(live.knowledge_scope or {}))
        mc = snap.get("model_config")
        if "model_config_id" not in snap:
            snap["model_config_id"] = (mc or {}).get("id") if isinstance(mc, dict) else (str(live.model_config_id) if live.model_config_id else None)
        snap["enabled"] = live.enabled
    else:
        snap.setdefault("enabled", False)
    return snap


def _model_config_id(snap: dict[str, Any]) -> uuid.UUID | None:
    raw = snap.get("model_config_id")
    if not raw and isinstance(snap.get("model_config"), dict):
        raw = snap["model_config"].get("id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _bump(nt: CollaborationNodeTask) -> None:
    nt.version += 1


def _clear_lease(nt: CollaborationNodeTask) -> None:
    nt.lease_until = None
    nt.lease_token = None


def _block(nt: CollaborationNodeTask, code: str, message: str, **extra: Any) -> None:
    nt.phase = "blocked"
    nt.blocked_reason = {"code": code, "message": message, **extra}
    _clear_lease(nt)


def _blocked_questions(outcome: Any) -> list[str]:
    """What a person must answer for a blocked agent: the CAD tools' own clarification questions of this attempt
    (sizes, through / blind, sheet, units, conflicts), else the agent's list of missing facts."""
    qs: list[str] = []
    for tc in getattr(outcome, "tool_calls", None) or []:
        if ((tc.get("error") or {}).get("code") == "needs_clarification"):
            qs += [str(q).strip() for q in tc.get("questions") or [] if str(q).strip()]
    if not qs:
        qs = [str(m).strip() for m in (getattr(outcome, "blocked", None) or {}).get("missing") or [] if str(m).strip()]
    return list(dict.fromkeys(qs))[:20]


def _queue_phase(nt: CollaborationNodeTask, phase: str | None = None) -> str:
    """The claimable phase a node returns to. `executing` is never a resting phase
    for an agent without a lease: it maps to rework (a submission exists) or ready."""
    p = phase or nt.phase
    if p == "executing" and nt.executor_kind == "agent":
        return "rework" if nt.current_submission_id or int(budget_of(nt.budget)["reworks"]) > 0 else "ready"
    return p


def policy_of(db: Session, run_id: uuid.UUID) -> str:
    """The frozen execution policy of a collaboration run (standard when the row predates W7)."""
    collab = db.get(CollaborationRun, run_id)
    return (collab.execution_policy if collab is not None else None) or "standard"


def _fail_turbo_node(db: Session, run: WorkflowRun, nt: CollaborationNodeTask, code: str, message: str, **extra: Any) -> None:
    """Turbo never waits for a person: a node that would block (agent blocked, budget used up, agent disabled, a step
    nobody can automate) fails for good with that reason. Its dependants are skipped by the engine and independent
    branches go on; the run ends partial / failed. Caller holds run → node task; it closes the stranded Tasks
    afterwards in its own transaction (:func:`close_stranded_run`, lock order)."""
    from ..workflow import engine

    nt.phase = "failed"
    nt.blocked_reason = {"code": code, "message": message, **extra}
    _clear_lease(nt)
    _bump(nt)
    nr = db.get(NodeRun, nt.node_run_id)
    if nr is not None and nr.status not in engine.TERMINAL_NODE:
        nr.status = "failed"
        nr.finished_at = _now()
        nr.lease_until = None
        nr.error = {"code": code, "message": message, "details": {"execution_policy": "turbo", **extra}}
        nr.steps = (nr.steps or []) + [{"kind": "turbo_failed", "ok": False, "at": _now().isoformat(), "detail": {"code": code}}]
    engine._activate_downstream(db, run)
    engine.refresh_run_status(db, run)
    _audit(db, action="collab.node_failed", subject_type="node_task", subject_id=nt.id, actor=None, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nt.node_id, "code": code, "execution_policy": "turbo", "run_status": run.status})


def _turbo_settle(db: Session, run: WorkflowRun, nt: CollaborationNodeTask) -> bool:
    """After a write-back of a Turbo node (caller holds run → node task): a node left blocked for a person fails
    instead. True when it failed (the caller then closes the stranded Tasks)."""
    if nt.phase != "blocked" or policy_of(db, run.id) != "turbo":
        return False
    reason = dict(nt.blocked_reason or {})
    code = str(reason.pop("code", "") or "blocked")
    if code in TRANSIENT_BLOCKS:
        return False
    message = str(reason.pop("message", "") or "the node cannot continue without a person")
    _fail_turbo_node(db, run, nt, code, message, **{k: v for k, v in reason.items() if k != "resume_phase"})
    return True


def _close_after_turbo_failure(run_id: uuid.UUID) -> None:
    """Close the Tasks of a Turbo node that failed and of its skipped dependants (own transaction, lock order)."""
    try:
        with session_scope() as db:
            close_stranded_run(db, run_id)
    except Exception:  # noqa: BLE001 - the worker sweep retries it
        log.exception("could not close the stranded tasks of Turbo run %s", run_id)


def _version_brief(db: Session, vid: Any) -> dict[str, Any]:
    from .tasks import version_brief

    try:
        return version_brief(db, uuid.UUID(str(vid)))
    except (ValueError, TypeError):
        return {"id": str(vid), "missing": True}


def _criteria(db: Session, task_id: uuid.UUID) -> list[dict[str, Any]]:
    from .tasks import criterion_out

    return [criterion_out(c) for c in db.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.task_id == task_id).order_by(AcceptanceCriterion.seq)).all()]


def _audit(db: Session, *, action: str, subject_type: str, subject_id: Any, actor: ActorRef | None, project_id: uuid.UUID, data: dict | None = None) -> None:
    """audit_log row with the agent column when an agent acted (services.audit.audit has no agent argument)."""
    db.add(AuditLog(project_id=project_id, user_id=actor.user_id if actor else None, agent_id=actor.agent_profile_id if actor else None,
                    action=action, subject_type=subject_type, subject_id=str(subject_id), data=data or {}))


# --- guard: collaboration tasks change only through the runtime / C04 ----------------------------------

_COLLAB_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar("partroute_collaboration_scope", default=False)


@contextmanager
def collaboration_scope() -> Iterator[None]:
    """Marks calls made by the runtime / C04 actions: generic task services may then
    act on collaboration node tasks."""
    token = _COLLAB_SCOPE.set(True)
    try:
        yield
    finally:
        _COLLAB_SCOPE.reset(token)


def guard_generic_write(db: Session, task_id: uuid.UUID, what: str) -> None:
    """Generic /tasks endpoints (transitions, deliverables, submissions) refuse a task
    bound to a collaboration node: 409 use_collaboration_actions (its owner may be
    empty because an agent executes it, and its phase must stay in step)."""
    if _COLLAB_SCOPE.get():
        return
    nt = node_task_for_task(db, task_id)
    if nt is not None:
        raise conflict("use_collaboration_actions", f"this task is a collaboration node; {what} through the run's node actions",
                       run_id=str(nt.run_id), node_id=nt.node_id)


# --- task status by the runtime (agent executors) ---------------------------------------------------


def _start_task(db: Session, task: Task, actor: ActorRef, reason: str) -> None:
    """tasks.transition(start) for an agent executor (same status rule, actor = agent). Caller holds the task lock."""
    from .tasks import bump

    if task.status != "todo":
        raise conflict("invalid_transition", f"a {task.status} task cannot be started", status=task.status)
    task.status = "in_progress"
    bump(task)
    _audit(db, action="task.start", subject_type="task", subject_id=task.id, actor=actor, project_id=task.project_id,
           data={"from": "todo", "to": "in_progress", "reason": reason})
    outbox.task_status(db, task, actor.user_id, "todo", reason)


def _system_user(db: Session, project_id: uuid.UUID) -> User:
    """A person to attribute system bookkeeping to when no user acts (the project owner)."""
    p = db.get(Project, project_id)
    uid = getattr(p, "owner_id", None) or getattr(p, "created_by", None)
    u = db.get(User, uid) if uid else None
    if u is None:
        u = db.scalar(select(User).join(ProjectMember, ProjectMember.user_id == User.id).where(ProjectMember.project_id == project_id).limit(1))
    assert u is not None
    return u


# --- handler support: node preparation (design §0.2) ------------------------------------------------


def prepare_node(db: Session, run: WorkflowRun, nr: NodeRun) -> dict[str, Any]:
    """Called by the business_task handler inside its transaction. Lock order:
    task → (pending submission withdrawn) → run → node task; the engine's
    _record_success then re-locks the run it already holds. Binds the upstream
    formal outputs and fixed inputs, registers them as input resources, starts
    the Task for an agent executor and sets the phase. Returns the node outputs."""
    from ..workflow import engine
    from .submissions import withdraw_locked
    from .tasks import lock_task

    features.require_run_continuation(db, run.id)

    nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.node_run_id == nr.id))
    if nt is None:
        raise AppError(409, "node_task_missing", f"node {nr.node_id} has no collaboration node task")
    turbo = policy_of(db, run.id) == "turbo"
    if turbo and nt.executor_kind == "human":
        # Start bound every automatable node to an agent; what is left needs a person (or no agent has its tools).
        collab = db.get(CollaborationRun, run.id)
        binding = (((collab.contract or {}).get("nodes") or {}).get(nr.node_id) or {}).get("turbo_binding") if collab else None
        reason = (binding or {}).get("reason") or "the task has a human executor and no agent of the project can run its tools"
        raise AppError(422, "not_automatable", f"Turbo cannot hand this task to a person: {reason}",
                       {"node_id": nr.node_id, "execution_policy": "turbo"})
    task = lock_task(db, nt.task_id)
    # Re-entry after an upstream reopen: a pending submission rests on stale inputs.
    for s in db.scalars(select(TaskSubmission).where(TaskSubmission.task_id == task.id, TaskSubmission.status == "pending")).all():
        with features.continuation_scope(db, run.id):
            withdraw_locked(db, db.get(User, run.created_by) or _system_user(db, run.project_id), s, reason="upstream_reopened")
    engine.lock_run(db, run.id)
    nt = lock_node_task(db, nt.id)
    bound: list[dict[str, Any]] = []
    missing: list[str] = []
    for slot in nt.input_slots or []:
        key = slot.get("slot_key")
        entry: dict[str, Any] | None = None
        src = slot.get("source_node_id")
        if src:
            up = node_task_of(db, run.id, src)
            fo = next((f for f in (up.formal_outputs if up else []) or [] if f.get("slot_key") == slot.get("source_slot_key")), None)
            if fo is not None:
                entry = {"slot_key": key, "asset_version_id": fo["asset_version_id"], "content_hash": fo["content_hash"], "from_node_id": src}
        else:
            vid = slot.get("fixed_asset_version_id")
            v = db.get(AssetVersion, uuid.UUID(str(vid))) if vid else None
            if v is None:
                b = db.scalar(select(RunInputBinding).where(RunInputBinding.run_id == run.id, RunInputBinding.port_key == f"fixed:{key}"))
                v = db.get(AssetVersion, b.asset_version_id) if b is not None else None
            if v is not None:
                entry = {"slot_key": key, "asset_version_id": str(v.id), "content_hash": v.content_hash, "from_node_id": None}
        if entry is None:
            if slot.get("required", True):
                missing.append(key)
            continue
        bound.append(entry)
    for e in bound:
        v = db.get(AssetVersion, uuid.UUID(e["asset_version_id"]))
        if v is None:
            missing.append(e["slot_key"])
            continue
        snapshot = {"from_node_id": e["from_node_id"], "slot_key": e["slot_key"]}
        db.execute(
            pg_insert(RunInputBinding)
            .values(id=uuid.uuid4(), run_id=run.id, port_key=f"{nr.node_id}/{e['slot_key']}", asset_version_id=v.id, content_hash=v.content_hash,
                    snapshot=snapshot)
            .on_conflict_do_update(index_elements=["run_id", "port_key"],
                                   set_={"asset_version_id": v.id, "content_hash": v.content_hash, "snapshot": snapshot})
        )
        resources.register(db, run=run, kind="input", asset_version=v, node_task=nt, node_run_id=nr.id, slot_key=e["slot_key"],
                           metadata={"phase": "ready", "reason": "bound_input", "from_node_id": e["from_node_id"], "attempt": nr.attempt})
    nt.bound_inputs = bound
    executor = actors.read(nt, "executor")
    if task.status in ("cancelled", "done"):
        nt.phase = "cancelled" if task.status == "cancelled" else nt.phase
    elif missing and turbo:  # nobody will supply them: fail (the handler transaction rolls back), never wait
        raise AppError(422, "input_missing", "required inputs are not available", {"slots": missing, "execution_policy": "turbo"})
    elif missing:
        _block(nt, "input_missing", "required inputs are not available", slots=missing)
    elif executor is not None and executor.kind == "agent":
        if task.status == "todo":
            _start_task(db, task, executor, reason="agent executor")
        budget = budget_of(nt.budget)
        nt.phase = "rework" if int(budget["reworks"]) > 0 else "ready"
        nt.blocked_reason = None
    else:
        nt.phase = "executing" if task.status == "in_progress" else "ready"
        nt.blocked_reason = None
    _clear_lease(nt)
    _bump(nt)
    _audit(db, action="collab.node_prepared", subject_type="node_task", subject_id=nt.id, actor=None, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nr.node_id, "phase": nt.phase, "bound_inputs": bound, "missing": missing})
    db.flush()
    return {"task": {"task_id": str(task.id), "node_task_id": str(nt.id), "phase": nt.phase, "bound_inputs": bound}}


# --- queue (design §0.3) ------------------------------------------------------------------------------


@dataclass
class Claim:
    node_task_id: uuid.UUID
    execution_id: uuid.UUID
    lease_token: uuid.UUID
    role: str
    run_id: uuid.UUID
    node_id: str
    prev_phase: str
    limits: dict[str, Any] = field(default_factory=dict)
    # P1: the executor of this attempt, and the model call records already stored by record_model_call (by id()):
    # the write-back's _persist_calls skips them, so no call is recorded twice.
    runtime: str = "native"
    recorded: set[int] = field(default_factory=set)


def _claimable(db: Session, now: datetime, exclude: set[uuid.UUID] | None = None) -> CollaborationNodeTask | None:
    return db.scalar(
        select(CollaborationNodeTask)
        .join(WorkflowRun, WorkflowRun.id == CollaborationNodeTask.run_id)
        .join(Project, Project.id == WorkflowRun.project_id)
        .where(
            CollaborationNodeTask.id.notin_(sorted(exclude or ())),
            Project.lifecycle == "active",
            Project.archived_at.is_(None),
            WorkflowRun.cancel_requested.is_(False),
            WorkflowRun.paused.is_(False),
            WorkflowRun.status.notin_(sorted(TERMINAL_RUN)),
            or_(CollaborationNodeTask.lease_until.is_(None), CollaborationNodeTask.lease_until < now),
            # a stopped attempt still running (stop clears the node's lease, resume requeues it) keeps the node until it
            # wrote back or its own lease ended: never two attempts on one node
            ~exists().where(AgentExecution.node_task_id == CollaborationNodeTask.id, AgentExecution.status == "running",
                            AgentExecution.stop_requested_at.isnot(None), AgentExecution.lease_until >= now),
            or_(
                and_(CollaborationNodeTask.phase.in_(("ready", "rework")), CollaborationNodeTask.executor_kind == "agent"),
                # an execution whose worker died: its lease expired, take it over (the old attempt becomes history);
                # an agent node left executing without a lease is taken over the same way (never stranded)
                and_(CollaborationNodeTask.phase == "executing", CollaborationNodeTask.executor_kind == "agent"),
                and_(CollaborationNodeTask.phase == "submitted", CollaborationNodeTask.pre_reviewer_agent_id.isnot(None)),
                and_(CollaborationNodeTask.phase == "in_review", CollaborationNodeTask.reviewer_kind == "agent"),
            ),
        )
        .order_by(CollaborationNodeTask.updated_at)
        .limit(1)
        .with_for_update(of=CollaborationNodeTask, skip_locked=True)
    )


def _skip_pre_review(db: Session, nt: CollaborationNodeTask, reason: dict[str, Any]) -> None:
    """A pre-review is optional help: when it cannot run, the node goes straight to the person's review (audited)."""
    nt.phase = "in_review"
    _clear_lease(nt)
    run_row = db.get(WorkflowRun, nt.run_id)
    assert run_row is not None
    _audit(db, action="collab.pre_review_skipped", subject_type="node_task", subject_id=nt.id, actor=None, project_id=run_row.project_id,
           data={"run_id": str(nt.run_id), "node_id": nt.node_id, "reason": reason})


def claim_one(db: Session, max_skips: int = 20) -> Claim | None:
    """Claim one claimable node task (SKIP LOCKED) and open its execution row.
    Nodes that cannot run (agent disabled, budget exhausted) are blocked on the
    way and the search continues. Caller commits."""
    now = _now()
    waiting: set[uuid.UUID] = set()  # frozen runs outside the rollout cohort stay queued
    for _ in range(max_skips):
        nt = _claimable(db, now, waiting)
        if nt is None:
            return None
        if not features.enabled("collaboration") and not features.can_continue(db, "collaboration", run_id=nt.run_id):
            waiting.add(nt.id)
            continue
        if nt.phase in ("ready", "rework", "executing"):
            role, agent_id = "execute", nt.executor_agent_id
        elif nt.phase == "submitted":
            role, agent_id = "pre_review", nt.pre_reviewer_agent_id
        else:
            role, agent_id = "review", nt.reviewer_agent_id
        collab = db.get(CollaborationRun, nt.run_id)
        snap = profile_snapshot(db, collab, agent_id)
        if agent_id is None or not snap.get("enabled"):
            if role == "pre_review":  # a pre-review is optional help: skip it, the person still decides
                nt.phase = "in_review"
                _clear_lease(nt)
            else:
                _block(nt, "agent_disabled", "the agent profile is disabled or missing", agent_profile_id=str(agent_id) if agent_id else None,
                       role=role, resume_phase=_queue_phase(nt))
            _bump(nt)
            db.flush()
            continue
        runtime = effective_runtime(snap)
        problem = runtime_problem(db, snap, runtime, "agent_execute" if role == "execute" else "agent_review")
        if problem is not None:
            if role == "pre_review":  # optional help (e.g. a tool_agent profile on a model without tool calling): skipped
                _skip_pre_review(db, nt, problem)
            else:
                _block(nt, problem["code"], problem["message"], agent_profile_id=str(agent_id), role=role, runtime=runtime,
                       resume_phase=_queue_phase(nt))
            _bump(nt)
            db.flush()
            continue
        limits = limits_of(nt.limits)
        if role == "execute":
            reason = budget_exceeded(limits, nt.budget) or unpriced_under_cap(limits, snap, role)
        else:
            reason = cost_exceeded(limits, nt.budget) or unpriced_under_cap(limits, snap, role)
            limits = {**limits, "max_duration_s": limits_of(snap.get("limits"))["max_duration_s"]}
        if reason is not None:
            if role == "pre_review":  # optional help that may not spend unknown / capped cost: the person decides alone
                _skip_pre_review(db, nt, reason)
            else:
                _block(nt, **reason)
            _bump(nt)
            db.flush()
            continue
        # Attempts whose lease expired without a write-back: history, superseded by this claim. A stopped one was a
        # person's decision, never a technical attempt.
        expired = db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running")).all()
        budget = budget_of(nt.budget)
        for old in expired:
            old.status = "superseded"
            old.error = {"code": "lease_expired", "message": "the lease expired before the result came back"}
            old.finished_at = now
            if old.stop_requested_at is None:
                budget["attempts"] = int(budget["attempts"]) + 1
        nt.budget = budget
        if expired and role == "execute":
            lost = budget_exceeded(limits_of(nt.limits), budget)
            if lost is not None:  # the lost leases used up the technical attempts
                _block(nt, **lost)
                _bump(nt)
                db.flush()
                continue
        token = uuid.uuid4()
        nt.lease_until = now + timedelta(seconds=lease_seconds(limits))
        nt.lease_token = token
        prev_phase = _queue_phase(nt)
        if role == "execute":
            nt.phase = "executing"
        rework_no = int(budget["reworks"])
        attempt_no = (db.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.node_task_id == nt.id, AgentExecution.role == role, AgentExecution.rework_no == rework_no)) or 0) + 1
        ex = AgentExecution(run_id=nt.run_id, node_task_id=nt.id, agent_profile_id=agent_id, profile_version=int(snap.get("version") or 1),
                            role=role, attempt_no=attempt_no, rework_no=rework_no, status="running", lease_until=nt.lease_until, lease_token=token,
                            usage={"tokens_in": 0, "tokens_out": 0, "calls": 0, "cost_fen": None, "cost_unknown": False}, started_at=now,
                            runtime=runtime)
        db.add(ex)
        db.flush()
        for old in expired:
            old.superseded_by_id = ex.id
        return Claim(node_task_id=nt.id, execution_id=ex.id, lease_token=token, role=role, run_id=nt.run_id, node_id=nt.node_id,
                     prev_phase=prev_phase, limits=limits, runtime=runtime)
    return None


def effective_runtime(snap: dict[str, Any]) -> str:
    """The frozen runtime (legacy missing value means native). Only supported runtimes may use dev/test simulation."""
    runtime = str(snap.get("runtime") or "native")
    settings = get_settings()
    if runtime in SUPPORTED_RUNTIMES and settings.ai_simulation and settings.env != "prod":
        return "native"
    return runtime


def runtime_options_of(snap: dict[str, Any]) -> dict[str, Any]:
    return dict(snap.get("runtime_options") or {})


def runtime_problem(db: Session, snap: dict[str, Any], runtime: str, purpose: str = "agent_execute") -> dict[str, Any] | None:
    """Validate the saved and claimed runtimes before any model selection or simulation fallback.

    A retained tool_agent also needs a supported model family with tool calling; capability diagnosis is read live.
    """
    for selected in (str(snap.get("runtime") or "native"), runtime):
        if selected not in SUPPORTED_RUNTIMES:
            return {"code": "runtime_unavailable", "message": f"the {selected} executor is not supported", "retryable": False}
    ref = ai_svc.role_model_ref(snap, purpose)
    if get_settings().ai_simulation or (ai_svc.is_snapshot(ref) and ref.get("simulated")):
        try:
            ai_svc.require_simulation_allowed()
        except ProviderError as exc:
            return {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
    if runtime != "tool_agent":
        return None
    if ai_svc.is_snapshot(ref):
        if ref.get("simulated"):  # type: ignore[union-attr]
            return {"code": "model_missing_capability", "message": f"the simulated model has no tool calling; {runtime} needs a real model"}
        row = db.get(ModelConfigRow, uuid.UUID(str(ref["config_id"])))  # type: ignore[index]
        provider = ref.get("provider")  # type: ignore[union-attr]
        caps = (row.capabilities if row is not None else ref.get("capabilities")) or {}  # type: ignore[union-attr]
    else:
        cfg = ai_svc.config_for(db, purpose, ref)
        provider = cfg.provider if cfg is not None else None
        caps = cfg.capabilities.model_dump() if cfg is not None else {}
    from ..ai.harness.tool_agent import TOOL_FAMILIES

    if provider not in TOOL_FAMILIES:
        return {"code": "model_missing_capability",
                "message": f"{runtime} needs an OpenAI, OpenAI-compatible or Gemini model configuration (got {provider or 'none'})"}
    if not caps.get("tools"):
        return {"code": "model_missing_capability",
                "message": "the model configuration has no tool calling (capabilities.tools); run its capability diagnosis, then resume"}
    return None


def lease_valid(claim: Claim) -> bool:
    """Re-check before every model / tool step (A16, C06): token unchanged, run not paused or cancelled."""
    with session_scope() as db:
        nt = db.get(CollaborationNodeTask, claim.node_task_id)
        run = db.get(WorkflowRun, claim.run_id)
        ex = db.get(AgentExecution, claim.execution_id)
        if nt is None or run is None or ex is None:
            return False
        if nt.lease_token != claim.lease_token or nt.lease_until is None or nt.lease_until < _now():
            return False
        profile = db.get(AgentProfile, ex.agent_profile_id) if ex.agent_profile_id else None
        if profile is None or not profile.enabled:  # A16: a profile disabled after the claim stops the attempt
            return False
        return (not run.cancel_requested and not run.paused and run.status not in TERMINAL_RUN
                and (features.enabled("collaboration") or features.can_continue(db, "collaboration", run_id=run.id)))


def claim_and_run_one() -> bool:
    """Worker entry: claim one node task and run it to its write-back. False when nothing was claimable."""
    with session_scope() as db:
        claim = claim_one(db)
    if claim is None:
        return False
    run_claim(claim)
    return True


def run_claim(claim: Claim) -> str:
    """Run a claimed execution (model loop outside transactions) and write the result back under the lease."""
    ctl: _AttemptControl | None = None
    try:
        ctl = _AttemptControl(claim)
        if claim.role == "execute":
            outcome = _execute_attempt(claim, ctl)
            status = _write_back_execute(claim, outcome)
        else:
            review = _review_attempt(claim, ctl)
            status = _write_back_review(claim, review)
    except Exception as exc:  # noqa: BLE001 - never let an attempt kill the worker; record it as a technical failure
        log.exception("agent execution %s crashed", claim.execution_id)
        error = {"code": "internal_error", "message": clip(str(exc), 500)}
        calls = list(ctl.calls) if ctl is not None else []  # already stored; they still count in the node budget
        tool_ai_calls = list(ctl.tool_ai_calls) if ctl is not None else []
        if ctl is not None:
            ctl.emit(HarnessEvent(type="system", payload={"code": "internal_error", "message": error["message"]}))
        if claim.role == "execute":
            status = _write_back_execute(claim, _Outcome(status="failed", error=error, calls=calls, tool_ai_calls=tool_ai_calls))
        else:
            status = _write_back_review(claim, _ReviewOutcome(status="failed", error=error, calls=calls))
    if ctl is not None:
        ctl.finish(status)
    return status


# --- control callbacks of one attempt (execution-layer design §6.2) ---------------------------------------------------


def _append_event(db: Session, execution_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> int:
    """One timeline row; seq = the next number of this execution (its row is locked, so every writer — the attempt's
    control, a stop request, the final status — gets its own number). Callers lock the node task first."""
    db.scalar(select(AgentExecution.id).where(AgentExecution.id == execution_id).with_for_update())
    seq = int(db.scalar(select(func.coalesce(func.max(AgentExecutionEvent.seq), 0))
                        .where(AgentExecutionEvent.execution_id == execution_id)) or 0) + 1
    payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))  # tool details may carry ids / dates
    db.add(AgentExecutionEvent(execution_id=execution_id, seq=seq, type=type_, payload=payload, at=_now()))
    db.flush()
    return seq


class _AttemptControl:
    """``AttemptControl`` of one claimed attempt: every check and write is its own short transaction, so nothing an
    executor does holds a lock while a model or a tool runs."""

    def __init__(self, claim: Claim) -> None:
        self.claim = claim
        self.t0 = time.monotonic()
        self.limits = claim.limits or limits_of(None)
        self.deadline = self.t0 + float(self.limits["max_duration_s"])
        self.stop_reason: str | None = None
        self.calls: list[Any] = []  # the attempt's own model calls (stored at once)
        self.tool_ai_calls: list[Any] = []  # calls made inside tools (stored by the tool)
        self.tool_count = 0
        self.ctx: agent_tools.ToolContext | None = None
        # P4: a review attempt's evidence scope (submission items + bound inputs) and project; its tools are the
        # read-only evidence tools, never run_tool.
        self.evidence: tuple[Any, uuid.UUID] | None = None
        self.simulated = False
        self.max_tool_calls: int | None = None
        self._lease_ok = True
        self._lock = threading.Lock()
        with session_scope() as db:
            nt = db.get(CollaborationNodeTask, claim.node_task_id)
            ex = db.get(AgentExecution, claim.execution_id)
            collab = db.get(CollaborationRun, claim.run_id)
            assert nt is not None and ex is not None
            snap = profile_snapshot(db, collab, ex.agent_profile_id)
            self.node_run_id = nt.node_run_id
            self.agent_profile_id = ex.agent_profile_id
            self.pricing = snap.get("pricing")
            self.node_budget = budget_of(nt.budget)
        if claim.runtime == "tool_agent":
            self.max_tool_calls = runtime_options_of(snap).get("max_tool_calls") or None

    def bind(self, ctx: agent_tools.ToolContext | None, *, simulated: bool) -> None:
        self.ctx = ctx
        self.simulated = simulated
        if ctx is not None:  # J: long tools (assembly build) stop starting model requests at the attempt's deadline
            ctx.deadline = self.deadline

    def alive(self) -> bool:
        self.stop_reason = self._why_not()
        return self.stop_reason is None

    def _why_not(self) -> str | None:
        if not self._lease_ok or not lease_valid(self.claim):
            with session_scope() as db:
                ex = db.get(AgentExecution, self.claim.execution_id)
                run = db.get(WorkflowRun, self.claim.run_id)
                if ex is not None and ex.stop_requested_at is not None:
                    return "stopped"
                if run is not None and run.paused:
                    return "paused"
                if run is not None and (run.cancel_requested or run.status in TERMINAL_RUN):
                    return "cancelled"
                profile = db.get(AgentProfile, ex.agent_profile_id) if ex is not None and ex.agent_profile_id else None
                if profile is not None and not profile.enabled:
                    return "disabled"
            return "lease_lost"
        if time.monotonic() >= self.deadline:
            return "timeout"
        with self._lock:
            calls, tool_ai_calls, tool_count = list(self.calls), list(self.tool_ai_calls), self.tool_count
        if len(calls) > int(self.limits["max_steps"]):
            return "max_steps"
        if self.max_tool_calls is not None and tool_count > int(self.max_tool_calls):
            return "max_tool_calls"
        if self.limits.get("max_cost_fen") is not None:
            spent = _add_usage(self.node_budget, usage_of(calls + tool_ai_calls, self.pricing, 0))
            if cost_exceeded(self.limits, spent) is not None:
                return "budget_exceeded"
        return None

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """The server-side tool (allowed tools, review-only tools, part ownership, project state are checked there).
        A review attempt (P4) only reaches the read-only evidence tools, scoped to its submission and bound inputs."""
        if not self.alive():
            raise AttemptStopped(self.stop_reason)
        if self.evidence is not None:
            from .review_evidence import run_evidence_tool

            res = run_evidence_tool(name, args, *self.evidence)
        else:
            assert self.ctx is not None
            res = agent_tools.run_tool(name, args, self.ctx)
        with self._lock:
            self.tool_ai_calls.extend(res.calls)
            self.tool_count += 1
        return res

    def record_model_call(self, call: Any) -> str | None:
        """An ai_calls row now (its own transaction; a crashed worker loses nothing), counted in this attempt's usage."""
        with self._lock:
            self.calls.append(call)
            self.claim.recorded.add(id(call))
        with session_scope() as db:
            rows = agent_tools.record_ai_calls_for(db, node_run_id=self.node_run_id, agent_profile_id=self.agent_profile_id,
                                                   execution_id=self.claim.execution_id, calls=[call])
            db.flush()
            return str(rows[0].id) if rows else None

    def emit(self, event: HarnessEvent) -> None:
        """A timeline event under the lease (own transaction); a lost lease writes nothing and stops the attempt."""
        payload = {**event.payload, "simulated": self.simulated}
        with self._lock:
            if not self._lease_ok:
                return
            try:
                with session_scope() as db:
                    nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.id == self.claim.node_task_id)
                                   .with_for_update(read=True).execution_options(populate_existing=True))
                    if nt is None or nt.lease_token != self.claim.lease_token or nt.lease_until is None or nt.lease_until < _now():
                        self._lease_ok = False
                        return
                    _append_event(db, self.claim.execution_id, event.type, payload)
            except Exception:  # noqa: BLE001 - the timeline is a view; a failed write never ends the attempt
                log.exception("could not write a %s event of execution %s", event.type, self.claim.execution_id)

    def finish(self, status: str) -> None:
        """After the write-back (the lease is gone by then): why it stopped when the lease was lost, then the final status."""
        try:
            with session_scope() as db:
                nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.id == self.claim.node_task_id).with_for_update())
                if nt is None:
                    return
                if self.stop_reason in ("lease_lost", "cancelled", "disabled") or (not self._lease_ok and self.stop_reason is None):
                    _append_event(db, self.claim.execution_id, "system", {
                        "code": self.stop_reason or "lease_lost", "message": "the attempt lost its lease; its result is history only",
                        "simulated": self.simulated})
                _append_event(db, self.claim.execution_id, "status", {"status": status, "simulated": self.simulated})
        except Exception:  # noqa: BLE001 - the timeline is a view; the write-back already happened
            log.exception("could not write the final event of execution %s", self.claim.execution_id)


def _set_runtime(claim: Claim, runtime: str, impl: str | None, ref: str | None = None) -> None:
    with session_scope() as db:
        ex = db.get(AgentExecution, claim.execution_id)
        if ex is not None:
            ex.runtime = runtime
            ex.runtime_impl = (impl or "")[:60] or None
            if ref:
                ex.runtime_ref = ref[:200]


def recover_expired(db: Session) -> int:
    """Executions whose lease expired (worker died): mark them failed (a technical
    attempt) and free the node for a new claim. Locks node task → executions,
    like claim_one. Caller commits."""
    fail_blocked_turbo_nodes()  # own transactions, before this session takes any lock
    close_stranded_runs(db)  # first: it takes project → task → run locks before any node task lock below
    now = _now()
    node_ids = db.scalars(select(AgentExecution.node_task_id).where(AgentExecution.status == "running", AgentExecution.lease_until < now).distinct()).all()
    n = 0
    for nt_id in node_ids:
        nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.id == nt_id).with_for_update(skip_locked=True)
                       .execution_options(populate_existing=True))
        if nt is None:
            continue
        if not features.can_close_run(db, nt.run_id):
            continue
        rows = db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running",
                                                       AgentExecution.lease_until < now)).all()
        for ex in rows:
            ex.status = "failed"
            ex.error = {"code": "lease_expired", "message": "the worker did not finish before the lease expired"}
            ex.finished_at = now
            n += 1
            if nt.lease_token != ex.lease_token:
                continue
            _clear_lease(nt)
            budget = budget_of(nt.budget)
            budget["attempts"] = int(budget["attempts"]) + 1
            nt.budget = budget
            reason = budget_exceeded(limits_of(nt.limits), budget)
            if reason is not None:
                _block(nt, **reason)
            elif nt.phase == "executing" and nt.executor_kind == "agent":
                nt.phase = _queue_phase(nt)
            _bump(nt)
    return n


def run_until_idle(max_steps: int = 100) -> int:
    steps = 0
    while steps < max_steps and claim_and_run_one():
        steps += 1
    return steps


def drain(handlers: dict[str, Any], max_rounds: int = 50) -> int:
    """Tests / scripts: alternate the node engine and the agent queue until both are idle."""
    from ..workflow import engine

    total = 0
    for _ in range(max_rounds):
        did = engine.run_until_idle(handlers) + run_until_idle()
        total += did
        if not did:
            break
    return total


# --- execute attempt (design §3.8) --------------------------------------------------------------------


@dataclass
class _Outcome:
    status: str  # delivered | blocked | failed | superseded | paused
    deliverables: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    blocked: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)  # the session's model calls (stored by the write-back)
    tool_ai_calls: list[Any] = field(default_factory=list)  # model calls made inside tools (already stored by the tool)
    duration_ms: int = 0
    simulated: bool = False
    produced: list[dict[str, Any]] = field(default_factory=list)


def attempt_allowed_tools(nt: CollaborationNodeTask, snap: dict[str, Any]) -> frozenset[str]:
    """The tools an execute attempt may call: the node recipe's tools that the frozen executor profile allows, never a
    review-only tool (the attempt's ``ToolContext.allowed_tools``)."""
    recipe_tools = list((nt.recipe or {}).get("tools") or [])
    return frozenset(t for t in recipe_tools if t in set(snap.get("allowed_tools") or []) and t not in agent_tools.REVIEW_ONLY_TOOLS)


def _execute_context(db: Session, claim: Claim) -> tuple[dict[str, Any], dict[str, Any], agent_tools.ToolContext, dict[str, Any]]:
    nt = db.get(CollaborationNodeTask, claim.node_task_id)
    run = db.get(WorkflowRun, claim.run_id)
    collab = db.get(CollaborationRun, claim.run_id)
    task = db.get(Task, nt.task_id)
    assert nt is not None and run is not None and task is not None
    snap = profile_snapshot(db, collab, nt.executor_agent_id)
    allowed = attempt_allowed_tools(nt, snap)
    bound = []
    for b in nt.bound_inputs or []:
        brief = _version_brief(db, b.get("asset_version_id"))
        bound.append({"slot_key": b.get("slot_key"), "asset_version_id": b.get("asset_version_id"), "content_hash": b.get("content_hash"),
                      "from_node_id": b.get("from_node_id"), "asset_type": brief.get("asset_type"), "label": brief.get("label")})
    candidates: dict[str, dict[str, Any]] = {}
    for d in db.scalars(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == task.id)).all():
        candidates[str(d.asset_version_id)] = {**_brief_for_state(db, d.asset_version_id), "note": d.note}
    for r in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == nt.id, WorkflowResource.kind == "candidate")).all():
        candidates.setdefault(str(r.asset_version_id), _brief_for_state(db, r.asset_version_id))
    feedback = None
    if nt.current_submission_id:
        d = db.scalar(select(ApprovalDecision).join(ApprovalRequest, ApprovalRequest.id == ApprovalDecision.request_id)
                      .where(ApprovalRequest.submission_id == nt.current_submission_id).order_by(ApprovalDecision.created_at.desc()).limit(1))
        if d is not None and d.decision == "rejected":
            feedback = d.comment + ("\n" + "\n".join(f"- {c.get('result')}: {c.get('reason')}" for c in (d.criterion_decisions or [])) if d.criterion_decisions else "")
    criteria = [{"id": c["id"], "description": c["description"], "required": c["required"], "allowed_types": sorted(set(c["allowed_types"]) | set((c["rule_spec"] or {}).get("asset_types") or []))}
                for c in _criteria(db, task.id)]
    part = db.get(Part, task.part_id) if task.part_id else None
    knowledge_items: list[dict[str, Any]] = []
    available = getattr(knowledge, "available_items", None)
    if available is not None and (snap.get("knowledge_scope") or {}).get("scope", "project") != "none":
        try:
            knowledge_items = list(available(db, run.project_id, agent_scope=snap.get("knowledge_scope") or {}))[:50]
        except Exception:  # noqa: BLE001 - knowledge is optional context
            log.exception("available_items failed")
    budget = budget_of(nt.budget)
    context = {"title": task.title, "objective": task.objective, "demand": (collab.contract or {}).get("demand") if collab else None,
               "instructions": (nt.recipe or {}).get("instructions") or "", "rework_no": int(budget["reworks"]), "feedback": feedback}
    state = {"title": task.title, "objective": task.objective, "instructions": context["instructions"], "tools": sorted(allowed),
             "output_slots": list(nt.output_slots or []), "input_slots": list(nt.input_slots or []), "bound_inputs": bound,
             "candidates": list(candidates.values()), "produced": [], "criteria": criteria, "rework_no": int(budget["reworks"]),
             "part": {"id": str(part.id), "number": part.number, "name": part.name, "material": part.material} if part else None,
             "knowledge_items": knowledge_items, "execution_mode": collab.execution_mode if collab else None,
             "clarifications": [{"question": c.get("question"), "answer": c.get("answer")} for c in nt.clarifications or []]}
    policy = (collab.execution_policy if collab else None) or "standard"
    if policy == "turbo":  # the frozen policy of the run; standard states stay byte-identical
        state["execution_policy"] = policy
    ctx = agent_tools.ToolContext(run_id=run.id, project_id=run.project_id, part_id=task.part_id, task_id=task.id, node_task_id=nt.id,
                                  node_id=nt.node_id, node_run_id=nt.node_run_id, execution_id=claim.execution_id, user_id=run.created_by,
                                  agent_profile_id=nt.executor_agent_id, agent_purpose=str(snap.get("purpose") or "execute"), allowed_tools=allowed,
                                  model_config_id=ai_svc.role_model_ref(snap, "agent_execute"), execution_mode=collab.execution_mode if collab else "semi_auto",
                                  bound_inputs=list(nt.bound_inputs or []), knowledge_scope=dict(snap.get("knowledge_scope") or {}),
                                  rework_no=int(budget["reworks"]), attempt_no=1,
                                  clarifications=[{"question": str(c.get("question") or ""), "answer": str(c.get("answer") or "")}
                                                  for c in nt.clarifications or []],
                                  execution_policy=policy)
    return context, state, ctx, snap


def _brief_for_state(db: Session, vid: Any) -> dict[str, Any]:
    b = _version_brief(db, vid)
    return {"asset_version_id": str(vid), "asset_type": b.get("asset_type"), "label": b.get("label"), "content_hash": b.get("content_hash"),
            "name": b.get("asset_name"), "labels": b.get("labels") or []}


#: The delivery check every executor uses (moved to ai/collaboration_agent.deliverable_problems in P1).
_deliverable_problems = deliverable_problems


def _execute_attempt(claim: Claim, ctl: _AttemptControl | None = None) -> _Outcome:
    """Build the attempt (context, state, allowed tools, the role's frozen model) and hand it to the executor of the
    frozen profile runtime (native in simulation mode). The executor's result maps 1:1 onto ``_Outcome``."""
    from ..ai.harness import get_harness
    from ..ai.harness.base import AttemptSpec
    from ..ai.tool_registry import specs_for

    t0 = time.monotonic()
    ctl = ctl or _AttemptControl(claim)
    with session_scope() as db:
        context, state, ctx, snap = _execute_context(db, claim)
        ex = db.get(AgentExecution, claim.execution_id)
        ctx.attempt_no = ex.attempt_no if ex else 1
        problem = runtime_problem(db, snap, claim.runtime)
        if problem is not None:
            return _Outcome(status="failed", error=problem, duration_ms=int((time.monotonic() - t0) * 1000))
        try:
            provider, cfg = ai_svc.provider_for(db, "agent_execute", ctx.model_config_id)
        except ProviderError as exc:
            return _Outcome(status="failed", error={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                            duration_ms=int((time.monotonic() - t0) * 1000))
    runtime = claim.runtime if not provider.simulated else "native"  # a simulated model only ever runs the native loop
    ctx.simulated_hint = provider.simulated if runtime == "native" else False
    limits = claim.limits or limits_of(None)
    role_prompt = str(snap.get("role_prompt") or "")
    if ctx.execution_policy == "turbo":
        from ..ai.turbo import EXECUTE_TURBO_RULES

        role_prompt = f"{role_prompt}\n\n{EXECUTE_TURBO_RULES}".strip()
    options = runtime_options_of(snap) if runtime == "tool_agent" else {}
    spec = AttemptSpec(role="execute", runtime=runtime, runtime_options=options, role_prompt=role_prompt, context=context, state=state,  # type: ignore[arg-type]
                       tools=specs_for(ctx.allowed_tools), provider=provider, simulated=ctx.simulated_hint,
                       limits={**limits, "max_tool_calls": options.get("max_tool_calls"), "max_total_tokens": options.get("max_total_tokens")})
    ctl.bind(ctx, simulated=ctx.simulated_hint)
    harness = get_harness(runtime)
    _set_runtime(claim, runtime, harness.impl)
    result = harness.run(spec, ctl)
    if result.runtime_ref:
        _set_runtime(claim, runtime, result.runtime_impl or harness.impl, result.runtime_ref)
    return _Outcome(status=result.status, deliverables=result.deliverables, note=result.note, blocked=result.blocked, error=result.error,
                    steps=result.steps, tool_calls=result.tool_calls, calls=list(result.calls), tool_ai_calls=list(result.tool_ai_calls),
                    duration_ms=int((time.monotonic() - t0) * 1000), simulated=result.simulated, produced=result.produced)


def _lost_reason(claim: Claim) -> str:
    with session_scope() as db:
        run = db.get(WorkflowRun, claim.run_id)
        if run is not None and run.paused:
            return "paused"
    return "superseded"


_safe_args = safe_args


def _finish_execution(db: Session, ex: AgentExecution, status: str, outcome: Any, usage: dict[str, Any]) -> None:
    ex.status = status
    ex.steps = list(getattr(outcome, "steps", []) or [])
    ex.tool_calls = list(getattr(outcome, "tool_calls", []) or [])
    ex.error = getattr(outcome, "error", None)
    ex.usage = {k: usage.get(k) for k in ("tokens_in", "tokens_out", "calls", "cost_fen", "cost_unknown")}
    ex.duration_ms = int(usage.get("duration_ms") or 0)
    ex.finished_at = _now()
    ex.lease_until = None
    ex.mcp_token_hash = None  # clear any historical retired-runtime token when ending an attempt


def _record_calls(db: Session, nt: CollaborationNodeTask, ex: AgentExecution, calls: list[Any]) -> None:
    agent_tools.record_ai_calls_for(db, node_run_id=nt.node_run_id, agent_profile_id=ex.agent_profile_id, execution_id=ex.id, calls=calls)


def _supersede(claim: Claim, outcome: Any, usage: dict[str, Any], result: dict[str, Any] | None) -> str:
    """A stale attempt (lease taken over, A15): its result is kept as history only.
    The real usage still counts in the node budget (cost and time were spent)."""
    with session_scope() as db:
        nt = lock_node_task(db, claim.node_task_id)
        ex = db.get(AgentExecution, claim.execution_id)
        if ex is not None and ex.status == "running":
            _finish_execution(db, ex, "superseded", outcome, usage)
            ex.error = ex.error or {"code": "lease_lost", "message": "the lease was taken over; this result is history only"}
            ex.result = result
        nt.budget = _add_usage(nt.budget, usage)
    return "superseded"


def _persist_calls(claim: Claim, calls: list[Any]) -> dict[str, Any]:
    """Store the attempt's ai_calls in their own transaction (never lost to a later
    rollback) and compute its usage from the frozen profile pricing. Calls the
    attempt's control already stored (record_model_call, P1) are not stored again."""
    calls = [c for c in calls if id(c) not in claim.recorded]
    with session_scope() as db:
        nt = db.get(CollaborationNodeTask, claim.node_task_id)
        ex = db.get(AgentExecution, claim.execution_id)
        collab = db.get(CollaborationRun, claim.run_id)
        assert nt is not None and ex is not None
        if calls:
            _record_calls(db, nt, ex, calls)
        return profile_snapshot(db, collab, ex.agent_profile_id)


def _write_back_execute(claim: Claim, outcome: _Outcome) -> str:
    """Write an execution attempt back. Lock order: project(share) → task →
    submission → request (agent submission, in a savepoint) → run → node task.
    Only applied while the lease token still matches (A15)."""
    from ..workflow import engine

    snap = _persist_calls(claim, outcome.calls)
    usage = usage_of(list(outcome.calls) + list(outcome.tool_ai_calls), snap.get("pricing"), outcome.duration_ms)
    result = _execute_result(outcome)
    stale = False
    final_status = "superseded"
    turbo_failed = False
    with session_scope() as db:
        nt = db.get(CollaborationNodeTask, claim.node_task_id)
        run = db.get(WorkflowRun, claim.run_id)
        ex = db.get(AgentExecution, claim.execution_id)
        assert nt is not None and run is not None and ex is not None
        turbo = policy_of(db, run.id) == "turbo"
        if (nt.lease_token != claim.lease_token or outcome.status == "superseded"
                or (not features.enabled("collaboration") and not features.can_continue(db, "collaboration", run_id=run.id))):
            stale = True
        else:
            actor = ActorRef(kind="agent", agent_profile_id=ex.agent_profile_id)
            submission: TaskSubmission | None = None
            sub_error: AppError | None = None
            deliver = outcome.status == "delivered" and not run.paused and not run.cancel_requested
            if deliver and turbo:
                # Turbo (W7): no submission, review or acceptance. The project must still accept work (lock order:
                # project share first, then run → node task below).
                from .lifecycle import require_active

                try:
                    require_active(db, run.project_id, what="delivering outputs")
                except AppError as exc:
                    sub_error = exc
            elif deliver:
                try:
                    with db.begin_nested(), features.continuation_scope(db, run.id):
                        submission = _submit_as_agent(db, nt, outcome, actor, ex)
                except AppError as exc:
                    sub_error = exc
            run = engine.lock_run(db, claim.run_id)
            nt = lock_node_task(db, claim.node_task_id)
            if nt.lease_token != claim.lease_token:
                db.rollback()
                stale = True
            else:
                if deliver and turbo and sub_error is None and not run.paused and not run.cancel_requested:
                    final_status = _complete_unverified(db, run, nt, ex, actor, outcome, usage, result)
                else:
                    final_status = _apply_execute(db, claim, run, nt, ex, actor, outcome, usage, result, submission, sub_error)
                if turbo:
                    turbo_failed = _turbo_settle(db, run, nt)
    if stale:
        return _supersede(claim, outcome, usage, result)
    if turbo_failed:
        _close_after_turbo_failure(claim.run_id)
    return final_status


def _complete_unverified(db: Session, run: WorkflowRun, nt: CollaborationNodeTask, ex: AgentExecution, actor: ActorRef,
                         outcome: _Outcome, usage: dict[str, Any], result: dict[str, Any]) -> str:
    """Turbo delivery (W7): the agent's real deliverables become the node's outputs and are released downstream at
    once as "completed · unverified" — no submission, pre-review, review, approval or acceptance is created, and the
    Task is never marked done (it never counts as accepted). Structure still applies: every deliverable is a ready
    version of this project with a content hash, in an output slot of its type (else a business rework, budgeted).
    Caller holds run → node task and the lease."""
    from ..workflow import engine

    nt.budget = _add_usage(nt.budget, usage)
    ex.result = result
    slots = {s["slot_key"]: s for s in nt.output_slots or []}
    formal: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for d in outcome.deliverables:
        v = db.get(AssetVersion, uuid.UUID(str(d["asset_version_id"])))
        asset = db.get(Asset, v.asset_id) if v is not None else None
        slot = slots.get(d.get("slot_key"))
        if v is None or asset is None or asset.project_id != run.project_id:
            problems.append(f"unknown_deliverable: {d['asset_version_id']}")
        elif v.availability != "ready" or not v.content_hash:
            problems.append(f"not_ready: {v.id} is {v.availability}")
        elif slot is None or slot.get("asset_type") != asset.type:
            problems.append(f"slot_type_mismatch: {v.id} is a {asset.type}")
        else:
            formal[slot["slot_key"]] = {"slot_key": slot["slot_key"], "asset_version_id": str(v.id), "content_hash": v.content_hash}
    missing = [k for k, s in slots.items() if s.get("required", True) and k not in formal]
    if missing:
        problems.append(f"output_slot_missing: {', '.join(missing)}")
    if problems:  # a delivery that breaks the structure: a business rework within the cumulative budget
        outcome.error = {"code": "delivery_invalid", "message": "; ".join(problems)[:2000], "problems": problems[:20]}
        _finish_execution(db, ex, "failed", outcome, usage)
        budget = budget_of(nt.budget)
        budget["reworks"] = int(budget["reworks"]) + 1
        nt.budget = budget
        reason = budget_exceeded(limits_of(nt.limits), budget)
        if reason is not None:
            _block(nt, **reason)
        else:
            nt.phase = "rework"
            nt.blocked_reason = None
            _clear_lease(nt)
        _bump(nt)
        return ex.status
    outputs = list(formal.values())
    skipped = [exec_policy.run_check(c, "turbo", dict) for c in TURBO_SKIPPED_CHECKS]  # recorded, never called
    assumptions = node_assumptions(db, run, nt.node_id, outputs)
    verification = exec_policy.verification("turbo", "skipped_turbo", "not_requested", assumptions)
    for f in outputs:
        v = db.get(AssetVersion, uuid.UUID(f["asset_version_id"]))
        if v is not None:
            resources.register(db, run=run, kind="candidate", asset_version=v, node_task=nt, node_run_id=nt.node_run_id,
                               agent_execution_id=ex.id, attempt=ex.attempt_no, slot_key=f["slot_key"],
                               metadata={"phase": COMPLETED_UNVERIFIED, "reason": "turbo_unverified", "checks": skipped})
    nt.formal_outputs = outputs
    nt.phase = COMPLETED_UNVERIFIED
    nt.blocked_reason = None
    _clear_lease(nt)
    _bump(nt)
    _finish_execution(db, ex, "delivered", outcome, usage)
    nr = db.get(NodeRun, nt.node_run_id)
    assert nr is not None
    nr.outputs = {**(nr.outputs or {}),
                  "outputs": {f["slot_key"]: {"asset_version_id": f["asset_version_id"], "content_hash": f["content_hash"]} for f in outputs},
                  "completed_unverified": True, "verification": verification, "checks": skipped}
    nr.steps = (nr.steps or []) + [{"kind": COMPLETED_UNVERIFIED, "ok": True, "at": _now().isoformat(),
                                    "detail": {"execution_id": str(ex.id), "skipped": [s["check"] for s in skipped]}}]
    released = False
    if nr.status == "waiting_human":
        engine.complete_waiting(db, run, nt.node_id, approved=True)
        released = True
    _audit(db, action="collab.node_completed_unverified", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nt.node_id, "outputs": outputs, "released": released, "execution_policy": "turbo",
                 "skipped": [s["check"] for s in skipped], "simulated": outcome.simulated})
    return ex.status


def node_assumptions(db: Session, run: WorkflowRun, node_id: str, outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assumptions a Turbo node's outputs rest on: the planner's adopted values for this node (run contract) and
    the values the CAD tools adopted for the model revisions it delivered."""
    from .verification import assumption_refs

    collab = db.get(CollaborationRun, run.id)
    out = [{**a, "owner": {"type": "collaboration_run", "id": str(run.id)}}
           for a in ((collab.contract or {}).get("assumptions") or [] if collab else [])
           if isinstance(a, dict) and (not a.get("affected") or node_id in (a.get("affected") or []))]
    for f in outputs:
        v = db.get(AssetVersion, uuid.UUID(str(f["asset_version_id"])))
        if v is not None and v.model_revision_id:
            out += assumption_refs(db.get(ModelRevision, v.model_revision_id))
    return out


def _apply_execute(db: Session, claim: Claim, run: WorkflowRun, nt: CollaborationNodeTask, ex: AgentExecution, actor: ActorRef, outcome: _Outcome,
                   usage: dict[str, Any], result: dict[str, Any], submission: TaskSubmission | None, sub_error: AppError | None) -> str:
    nt.budget = _add_usage(nt.budget, usage)
    limits = limits_of(nt.limits)
    ex.result = result
    status = outcome.status
    if run.cancel_requested:
        status = "cancelled"
    elif run.paused or (sub_error is not None and sub_error.code in ("project_not_active", "project_archived")):
        status = "paused"  # the project stopped accepting work: keep the products as candidates, no rework counted
    if status == "delivered" and submission is not None:
        _finish_execution(db, ex, "delivered", outcome, usage)
        ex.submission_id = submission.id
        # The submission hook already moved the phase to submitted / in_review and cleared the lease.
        nt.current_submission_id = submission.id
        if nt.phase not in ("submitted", "in_review"):
            nt.phase = "submitted" if nt.pre_reviewer_agent_id else "in_review"
        nt.blocked_reason = None
        _clear_lease(nt)
    elif status == "delivered":  # the delivery failed the submission checks: a business rework, not a crash
        err = sub_error or AppError(422, "submission_failed", "the submission could not be created")
        outcome.error = {"code": err.code, "message": err.message, "details": err.details}
        _finish_execution(db, ex, "failed", outcome, usage)
        budget = budget_of(nt.budget)
        budget["reworks"] = int(budget["reworks"]) + 1
        nt.budget = budget
        reason = budget_exceeded(limits, budget)
        if reason is not None:
            _block(nt, **reason)
        else:
            nt.phase = "rework"
            nt.blocked_reason = None
            _clear_lease(nt)
    elif status == "blocked":
        _finish_execution(db, ex, "blocked", outcome, usage)
        budget = budget_of(nt.budget)
        _block(nt, "agent_blocked", (outcome.blocked or {}).get("reason") or "the agent reported it is blocked",
               missing=(outcome.blocked or {}).get("missing") or [], questions=_blocked_questions(outcome),
               resume_phase="rework" if int(budget["reworks"]) > 0 else "ready")
    elif status in ("paused", "cancelled"):
        outcome.error = {"code": "run_paused" if status == "paused" else "run_cancelled",
                         "message": "the run was paused or cancelled during the attempt; its products stay as candidates"}
        _finish_execution(db, ex, "cancelled", outcome, usage)
        nt.phase = claim.prev_phase if status == "paused" else "cancelled"
        _clear_lease(nt)
    else:  # failed: a technical attempt (provider / protocol / timeout)
        _finish_execution(db, ex, "failed", outcome, usage)
        budget = budget_of(nt.budget)
        budget["attempts"] = int(budget["attempts"]) + 1
        nt.budget = budget
        reason = budget_exceeded(limits, budget)
        err = outcome.error or {}
        if reason is None and err.get("retryable") is False and policy_of(db, run.id) == "turbo":
            # Turbo retries only retryable causes (a refused key, an unsupported capability stay failures).
            reason = {"code": str(err.get("code") or "failed"), "message": str(err.get("message") or "the attempt failed"),
                      "retryable": False}
        if reason is not None:
            _block(nt, **reason)
        else:
            nt.phase = claim.prev_phase
            _clear_lease(nt)
    _bump(nt)
    _audit(db, action=f"collab.execution.{ex.status}", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"execution_id": str(ex.id), "run_id": str(run.id), "node_id": nt.node_id, "phase": nt.phase, "attempt_no": ex.attempt_no,
                 "rework_no": ex.rework_no, "error": ex.error, "simulated": outcome.simulated})
    return ex.status


def _execute_result(outcome: _Outcome) -> dict[str, Any]:
    return {"status": outcome.status, "deliverables": outcome.deliverables, "note": outcome.note, "blocked": outcome.blocked,
            "produced": outcome.produced, "simulated": outcome.simulated, "error": outcome.error}


def _submit_as_agent(db: Session, nt: CollaborationNodeTask, outcome: _Outcome, actor: ActorRef, ex: AgentExecution) -> TaskSubmission:
    """Deliverables → drafts (agent) → submission (agent). Lock order: project(share) → task."""
    from .lifecycle import require_active
    from .submissions import create_submission_as_actor
    from .tasks import lock_task, upsert_deliverable_as_actor

    run = db.get(WorkflowRun, nt.run_id)
    assert run is not None
    require_active(db, run.project_id, what="submitting deliverables")
    task = lock_task(db, nt.task_id)
    if task.status == "todo":
        _start_task(db, task, actor, reason="agent delivery")
    if task.status != "in_progress":
        raise conflict("task_not_in_progress", f"the task is {task.status}", status=task.status)
    wanted = {d["asset_version_id"] for d in outcome.deliverables}
    for d in db.scalars(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == task.id)).all():
        if str(d.asset_version_id) not in wanted:
            db.delete(d)
    db.flush()
    for d in outcome.deliverables:
        v = db.get(AssetVersion, uuid.UUID(d["asset_version_id"]))
        if v is None:
            raise AppError(422, "unknown_deliverable", f"version {d['asset_version_id']} does not exist")
        upsert_deliverable_as_actor(db, task, actor=actor, asset_version_id=v.id, criterion_ids=[uuid.UUID(c) for c in d["criterion_ids"]], note=d.get("note") or "")
        resources.register(db, run=run, kind="candidate", asset_version=v, node_task=nt, node_run_id=nt.node_run_id, agent_execution_id=ex.id,
                           attempt=ex.attempt_no, slot_key=d["slot_key"], metadata={"phase": "executing", "reason": "deliver", "slot_key": d["slot_key"]})
    _require_output_slots(db, nt, task)
    return create_submission_as_actor(db, task, actor=actor, note=outcome.note or "")


def _slot_assignment(db: Session, nt: CollaborationNodeTask, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Map submitted items ({asset_version_id, asset_type, content_hash}) onto
    the output slots: the slot a candidate was delivered to (resource rows with
    slot_key), else the first open slot of the same type. Returns (formal, missing required slots)."""
    slot_of: dict[str, str] = {}
    for r in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == nt.id, WorkflowResource.kind == "candidate",
                                                       WorkflowResource.slot_key.isnot(None)).order_by(WorkflowResource.created_at.desc())).all():
        slot_of.setdefault(str(r.asset_version_id), r.slot_key or "")
    formal: list[dict[str, Any]] = []
    assigned: set[str] = set()
    missing: list[str] = []
    for slot in nt.output_slots or []:
        key, typ = slot["slot_key"], slot["asset_type"]
        cands = [i for i in items if slot_of.get(str(i["asset_version_id"])) == key and i.get("asset_type") == typ and str(i["asset_version_id"]) not in assigned]
        if not cands:
            cands = [i for i in items if i.get("asset_type") == typ and str(i["asset_version_id"]) not in assigned and str(i["asset_version_id"]) not in slot_of]
        if not cands:
            cands = [i for i in items if i.get("asset_type") == typ and str(i["asset_version_id"]) not in assigned]
        if cands:
            i = cands[-1]
            assigned.add(str(i["asset_version_id"]))
            formal.append({"slot_key": key, "asset_version_id": str(i["asset_version_id"]), "content_hash": i["content_hash"]})
        elif slot.get("required", True):
            missing.append(key)
    return formal, missing


def _items_of_drafts(db: Session, task_id: uuid.UUID) -> list[dict[str, Any]]:
    out = []
    for d in db.scalars(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == task_id).order_by(TaskDeliverableDraft.selected_at)).all():
        v = db.get(AssetVersion, d.asset_version_id)
        a = db.get(Asset, v.asset_id) if v else None
        if v is not None:
            out.append({"asset_version_id": str(v.id), "asset_type": a.type if a else None, "content_hash": v.content_hash})
    return out


def _require_output_slots(db: Session, nt: CollaborationNodeTask, task: Task) -> None:
    _, missing = _slot_assignment(db, nt, _items_of_drafts(db, task.id))
    if missing:
        raise AppError(422, "output_slot_missing", "every required output slot needs a deliverable", {"slots": missing})


# --- review attempt (design §3.8) ----------------------------------------------------------------------


@dataclass
class _ReviewOutcome:
    status: str  # decided | unknown | failed | stale | superseded | paused
    verdict: str | None = None  # approved | rejected
    decisions: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    error: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)
    duration_ms: int = 0
    simulated: bool = False
    submission_id: uuid.UUID | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error_emitted: bool = False  # the executor already put the failure on the timeline (tool_agent, P4)


def _document_text(db: Session, v: AssetVersion) -> str | None:
    if not v.file_version_id:
        return None
    fv = db.get(FileVersion, v.file_version_id)
    if fv is None:
        return None
    try:
        data, blob = read_blob(db, fv.blob_sha256)
    except Exception:  # noqa: BLE001
        return None
    if not blob.content_type.startswith("text/") and blob.content_type not in ("application/json", "application/markdown"):
        return None
    try:
        return data[: DOC_EXCERPT_CHARS * 4].decode("utf-8", errors="replace")[:DOC_EXCERPT_CHARS]
    except Exception:  # noqa: BLE001
        return None


def _review_context(db: Session, claim: Claim) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], TaskSubmission | None]:
    nt = db.get(CollaborationNodeTask, claim.node_task_id)
    collab = db.get(CollaborationRun, claim.run_id)
    task = db.get(Task, nt.task_id)
    assert nt is not None and task is not None
    agent_id = nt.pre_reviewer_agent_id if claim.role == "pre_review" else nt.reviewer_agent_id
    snap = profile_snapshot(db, collab, agent_id)
    sub = db.get(TaskSubmission, nt.current_submission_id) if nt.current_submission_id else None
    if sub is None or sub.status != "pending":
        return {}, {}, snap, None
    snapshot = sub.snapshot or {}
    items = []
    documents = []
    for i in snapshot.get("items") or []:
        items.append({"asset_version_id": i["asset_version_id"], "asset_type": i.get("asset_type"), "label": i.get("label"),
                      "content_hash": i.get("content_hash"), "criterion_ids": i.get("criterion_ids") or [], "note": i.get("note") or "",
                      "labels": i.get("labels") or [], "engineering": i.get("engineering")})
        if i.get("asset_type") == "document":
            v = db.get(AssetVersion, uuid.UUID(i["asset_version_id"]))
            text = _document_text(db, v) if v is not None else None
            if text:
                documents.append({"asset_version_id": i["asset_version_id"], "label": i.get("label"), "text": text})
    criteria = [{"id": c["id"], "description": c.get("description"), "required": c.get("required", True),
                 "allowed_types": sorted(set(c.get("allowed_types") or []) | set((c.get("rule_spec") or {}).get("asset_types") or [])),
                 "rule_spec": c.get("rule_spec") or {}, "manual_required": c.get("manual_required", False)}
                for c in snapshot.get("criteria") or []]
    bound = [{"slot_key": b.get("slot_key"), "asset_version_id": b.get("asset_version_id"), "content_hash": b.get("content_hash")} for b in nt.bound_inputs or []]
    budget = budget_of(nt.budget)
    context = {"title": task.title, "objective": task.objective, "submission_seq": sub.seq, "rework_no": int(budget["reworks"]), "note": sub.note, "documents": documents}
    state = {"title": task.title, "objective": task.objective, "criteria": criteria, "items": items, "bound_inputs": bound, "note": sub.note,
             "rework_no": int(budget["reworks"]), "checks": (snapshot.get("checks") or {}).get("ok"), "role": claim.role,
             "output_slots": list(nt.output_slots or [])}
    return context, state, snap, sub


def _review_attempt(claim: Claim, ctl: _AttemptControl | None = None) -> _ReviewOutcome:
    """A review / pre-review: one structured call (native), or — profile runtime ``tool_agent`` with a real model (P4) —
    the tool_agent executor with the read-only evidence tools and the ReviewReply output tool. Its model calls are
    stored at once and the timeline gets started / model_call / (tool_call / tool_result) / verdict (or the failure)
    events. A tool_agent decision carries, per criterion, the evidence the agent read (``evidence``)."""
    ctl = ctl or _AttemptControl(claim)
    out = _review_attempt_run(claim, ctl)
    if out.decisions:
        ctl.emit(HarnessEvent(type="verdict", payload={
            "criteria": [{"criterion_id": d["criterion_id"], "result": d["result"], "reason": clip(d["reason"], 2000),
                          **({"evidence": d["evidence"]} if "evidence" in d else {})} for d in out.decisions],
            "summary": clip(out.summary, 4000), "verdict": out.verdict}))
    elif out.error and not out.error_emitted:
        from ..ai.harness.base import error_event

        event = error_event(out.error)
        if event is not None:
            ctl.emit(event)
    return out


def _evidence_scope(db: Session, state: dict[str, Any], sub: TaskSubmission) -> Any:
    """P4: what a tool_agent review may read — the submission items and the bound inputs (with type and label)."""
    from .review_evidence import scope_for

    bound = []
    for b in state.get("bound_inputs") or []:
        brief = _version_brief(db, b.get("asset_version_id"))
        bound.append({**b, "asset_type": brief.get("asset_type"), "label": brief.get("label")})
    return scope_for({**state, "bound_inputs": bound}, (sub.snapshot or {}).get("checks"))


def _review_attempt_run(claim: Claim, ctl: _AttemptControl) -> _ReviewOutcome:
    from ..ai.collaboration_agent import run_review
    from ..ai.harness.base import model_call_payload

    t0 = time.monotonic()
    with session_scope() as db:
        context, state, snap, sub = _review_context(db, claim)
        if sub is None:
            return _ReviewOutcome(status="stale", error={"code": "no_pending_submission", "message": "nothing is waiting for review"})
        sub_id = sub.id
        problem = runtime_problem(db, snap, claim.runtime, "agent_review")
        if problem is not None:
            return _ReviewOutcome(status="failed", error=problem, submission_id=sub_id,
                                  duration_ms=int((time.monotonic() - t0) * 1000))
        pol = policy_of(db, claim.run_id)
        try:
            provider, cfg = ai_svc.provider_for(db, "agent_review", ai_svc.role_model_ref(snap, "agent_review"))
        except ProviderError as exc:
            return _ReviewOutcome(status="failed", error={"code": exc.code, "message": exc.message, "retryable": exc.retryable}, submission_id=sub_id,
                                  duration_ms=int((time.monotonic() - t0) * 1000))
        run_row = db.get(WorkflowRun, claim.run_id)
        assert run_row is not None
        # a simulated model only ever runs the native single call (handoff §6 rule 7)
        scope = _evidence_scope(db, state, sub) if claim.runtime == "tool_agent" and not provider.simulated else None
        project_id = run_row.project_id
    ctl.bind(None, simulated=provider.simulated)
    if not ctl.alive():
        from ..ai.harness.base import stopped

        status, error = stopped(ctl)
        return _ReviewOutcome(status=status if status in ("paused", "superseded") else "failed", error=error, submission_id=sub_id)
    if scope is not None:
        return _review_with_tools(claim, ctl, provider, context, state, snap, pol, (scope, project_id), sub_id, t0)
    _set_runtime(claim, "native", "native")
    ctl.emit(HarnessEvent(type="system", payload={"code": "started", "message": f"the {claim.role.replace('_', '-')} started",
                                                  "detail": {"runtime": "native", "role": claim.role}}))
    limits = claim.limits or limits_of(None)
    try:
        # review.agent (W0 §2.1): the review model is a business check; a Turbo node never reaches it (guard).
        checked = exec_policy.run_check("review.agent", pol, lambda: {"res": run_review(
            provider, context, state, role_prompt=str(snap.get("role_prompt") or ""),
            timeout_s=max(5.0, min(float(limits["max_duration_s"]), 600.0)))})
    except ProviderError as exc:
        return _ReviewOutcome(status="failed", error={"code": exc.code, "message": exc.message, "retryable": exc.retryable}, submission_id=sub_id,
                              duration_ms=int((time.monotonic() - t0) * 1000), simulated=provider.simulated)
    res = checked.get("res")
    if res is None:
        return _ReviewOutcome(status="stale", error={"code": "skipped_turbo", "message": "Turbo skips reviews"}, submission_id=sub_id)
    for call in res.calls:
        call_id = ctl.record_model_call(call)
        ctl.emit(HarnessEvent(type="model_call", payload=model_call_payload(call, call_id)))
    out = _ReviewOutcome(status="failed", steps=res.tracer_steps, calls=res.calls, simulated=res.simulated, submission_id=sub_id,
                         duration_ms=int((time.monotonic() - t0) * 1000))
    if res.reply is None:
        out.error = {"code": "protocol_error", "message": "; ".join(res.problems)[:2000], "problems": res.problems[:20]}
        return out
    return _decide(out, res.reply, state)


def _decide(out: _ReviewOutcome, reply: Any, state: dict[str, Any]) -> _ReviewOutcome:
    """The decisions of a review reply (every executor): the semantic check (``review_problems``: criteria of this
    submission, known evidence, every required criterion), then the verdict — any fail rejects, any unknown leaves it
    undecided, else approved."""
    from ..ai.collaboration_agent import review_problems

    problems = review_problems(reply, state)
    if problems:
        out.error = {"code": "protocol_error", "message": "; ".join(problems)[:2000], "problems": problems[:20]}
        return out
    seen: dict[str, dict[str, Any]] = {}
    for d in reply.criteria:
        cid = str(d.criterion_id)
        seen[cid] = {"criterion_id": cid, "result": d.result, "reason": d.reason, "evidence_asset_version_ids": [str(e) for e in d.evidence_asset_version_ids]}
    out.decisions = list(seen.values())
    out.summary = reply.summary
    results = [d["result"] for d in out.decisions]
    if "fail" in results:
        out.status, out.verdict = "decided", "rejected"
    elif "unknown" in results:
        out.status = "unknown"
    else:
        out.status, out.verdict = "decided", "approved"
    return out


def _review_with_tools(claim: Claim, ctl: _AttemptControl, provider: Any, context: dict[str, Any], state: dict[str, Any],
                       snap: dict[str, Any], pol: str, evidence_scope: tuple[Any, uuid.UUID], sub_id: uuid.UUID, t0: float) -> _ReviewOutcome:
    """P4: the review on the tool_agent executor. The agent reads the evidence with the read-only evidence tools (the
    control serves them, scoped to this submission and its bound inputs) and gives its verdict with the ReviewReply
    output tool; the decisions then go through the same check (``_decide``) and the write-back is unchanged. Each
    decision gets the evidence the agent actually read (``ai/evidence.criterion_evidence``)."""
    from ..ai import evidence as ev
    from ..ai.collaboration_agent import REVIEW_TOOL_AGENT_PROMPT_VERSION, REVIEW_TOOL_AGENT_SYSTEM_PROMPT, ReviewReply, review_tool_user_message
    from ..ai.harness import get_harness
    from ..ai.harness.base import AttemptSpec
    from ..ai.tool_registry import EVIDENCE_TOOL_SPECS

    limits = claim.limits or limits_of(None)
    options = runtime_options_of(snap)
    spec = AttemptSpec(role=claim.role, runtime="tool_agent", runtime_options=options, role_prompt=str(snap.get("role_prompt") or ""),  # type: ignore[arg-type]
                       context=context, state=state, tools=list(EVIDENCE_TOOL_SPECS.values()), provider=provider,
                       limits={**limits, "max_tool_calls": options.get("max_tool_calls"), "max_total_tokens": options.get("max_total_tokens")},
                       instructions=REVIEW_TOOL_AGENT_SYSTEM_PROMPT, user_message=review_tool_user_message(context, state),
                       prompt_version=REVIEW_TOOL_AGENT_PROMPT_VERSION)
    ctl.evidence = evidence_scope
    harness = get_harness("tool_agent")
    _set_runtime(claim, "tool_agent", harness.impl)
    # review.agent (W0 §2.1): the review model is a business check; a Turbo node never reaches it (guard).
    checked = exec_policy.run_check("review.agent", pol, lambda: {"res": harness.run(spec, ctl)})
    result = checked.get("res")
    if result is None:
        return _ReviewOutcome(status="stale", error={"code": "skipped_turbo", "message": "Turbo skips reviews"}, submission_id=sub_id)
    if result.runtime_ref:
        _set_runtime(claim, "tool_agent", result.runtime_impl or harness.impl, result.runtime_ref)
    out = _ReviewOutcome(status="failed", steps=result.steps, calls=list(result.calls), simulated=False, submission_id=sub_id,
                         duration_ms=int((time.monotonic() - t0) * 1000), tool_calls=list(result.tool_calls), error_emitted=bool(result.error))
    if result.status != "delivered" or result.output is None:
        out.status = result.status if result.status in ("paused", "superseded") else "failed"
        out.error = result.error if result.error or out.status != "failed" else {"code": "protocol_error", "message": "the review ended without a verdict"}
        return out
    out = _decide(out, ReviewReply.model_validate(result.output), state)
    if out.decisions:
        ev.criterion_evidence(out.decisions, state["items"], out.tool_calls)
    return out


def _write_back_review(claim: Claim, outcome: _ReviewOutcome) -> str:
    """Write a review / pre-review back. A final review decides through
    approval_requests.decide_as_agent (task → submission → request → run → node
    task, lease verified under the node-task lock); everything else only locks
    run → node task."""
    from ..workflow import engine
    from . import approval_requests

    snap = _persist_calls(claim, outcome.calls)
    usage = usage_of(outcome.calls, snap.get("pricing"), outcome.duration_ms)
    result = {"status": outcome.status, "verdict": outcome.verdict, "criteria": outcome.decisions, "summary": outcome.summary,
              "simulated": outcome.simulated, "submission_id": str(outcome.submission_id) if outcome.submission_id else None, "role": claim.role}
    stale = False
    final_status = "superseded"
    with session_scope() as db:
        nt = db.get(CollaborationNodeTask, claim.node_task_id)
        ex = db.get(AgentExecution, claim.execution_id)
        run = db.get(WorkflowRun, claim.run_id)
        assert nt is not None and ex is not None and run is not None
        actor = ActorRef(kind="agent", agent_profile_id=ex.agent_profile_id)
        if (nt.lease_token != claim.lease_token or outcome.status == "superseded"
                or (not features.enabled("collaboration") and not features.can_continue(db, "collaboration", run_id=run.id))):
            stale = True
        elif outcome.status == "paused" or run.paused or run.cancel_requested:
            nt = lock_node_task(db, nt.id)
            _finish_execution(db, ex, "cancelled", outcome, usage)
            ex.error = {"code": "run_paused" if run.paused else "run_cancelled", "message": "the run was paused or cancelled during the review"}
            ex.result = result
            if nt.lease_token == claim.lease_token:
                _clear_lease(nt)
                nt.budget = _add_usage(nt.budget, usage)
                _bump(nt)
            final_status = ex.status
        else:
            decided = False
            if claim.role == "review" and outcome.status == "decided" and outcome.submission_id is not None:
                profile = db.get(AgentProfile, ex.agent_profile_id)
                req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.submission_id == outcome.submission_id, ApprovalRequest.status == "pending"))
                if profile is None or req is None:
                    outcome.error = {"code": "request_missing", "message": "no pending acceptance request for the submission"}
                    outcome.status = "stale"
                else:
                    try:
                        decided = _agent_decide(db, approval_requests, profile, ex, nt, req, outcome, claim, snap)
                    except AppError as exc:
                        if exc.code == "lease_lost":
                            db.rollback()
                            stale = True
                        else:
                            outcome.error = {"code": exc.code, "message": exc.message, "details": exc.details}
                            outcome.status = _review_error_status(exc.code)
                    if decided:
                        result.update({"decision_id": str(ex.decision_id), "verdict": outcome.verdict, "criteria": outcome.decisions})
            if not stale:
                run = engine.lock_run(db, claim.run_id)
                nt = lock_node_task(db, claim.node_task_id)
                if nt.lease_token != claim.lease_token and not decided:
                    db.rollback()
                    stale = True
                else:
                    final_status = _apply_review(db, claim, run, nt, ex, actor, outcome, usage, result, decided)
    if stale:
        return _supersede(claim, outcome, usage, result)
    return final_status


# Errors of the final decision that are not a technical failure of the review (no attempt counted):
# the project stopped accepting decisions → back to the queue; the reviewer was disabled / the node
# cannot be released now → blocked with that reason; the request or submission closed meanwhile → stale.
REVIEW_DEFERRED = frozenset({"project_not_active", "project_archived"})
REVIEW_BLOCKING = frozenset({"agent_disabled", "node_not_waiting", "node_blocked", "inputs_stale"})
REVIEW_STALE = frozenset({"request_closed", "submission_closed", "target_changed", "request_missing", "node_cancelled", "not_found"})


def _review_error_status(code: str) -> str:
    if code in REVIEW_DEFERRED:
        return "deferred"
    if code in REVIEW_BLOCKING:
        return "blocked"
    if code in REVIEW_STALE:
        return "stale"
    return "failed"


def _agent_decide(db: Session, approval_requests: Any, profile: AgentProfile, ex: AgentExecution, nt: CollaborationNodeTask, req: ApprovalRequest,
                  outcome: _ReviewOutcome, claim: Claim, snap: dict[str, Any]) -> bool:
    """Engineering approvals (when the review agent holds engineering_review) and
    decide_as_agent in ONE savepoint: a failed acceptance rolls the stage approvals
    back with it. When an approval fails the acceptance re-check
    (submission_no_longer_valid), the submission is rejected instead with that
    reason (it can never be accepted as frozen)."""
    comment = outcome.summary or ("approved by the review agent" if outcome.verdict == "approved" else "rejected by the review agent")
    if outcome.verdict == "rejected":
        fails = [d for d in outcome.decisions if d["result"] != "pass"]
        comment = (comment + "\n" + "\n".join(f"- {d['result']}: {d['reason']}" for d in fails)).strip()[:4000]
    eng: list[dict[str, Any]] = []
    try:
        with db.begin_nested():
            if outcome.verdict == "approved" and "engineering_review" in set(snap.get("allowed_tools") or []):
                eng = _engineering_approvals(db, nt, ex, profile, outcome)
            _, row = approval_requests.decide_as_agent(db, agent_profile=profile, execution=ex, request_id=req.id, decision=outcome.verdict or "rejected",
                                                       comment=comment, criterion_decisions=outcome.decisions, lease_token=claim.lease_token)
    except AppError as exc:
        for rec in eng:  # recorded in the rolled-back savepoint: history of the attempt only
            if rec.get("ok"):
                rec.update(ok=False, rolled_back=True, error={"code": "rolled_back", "message": f"the acceptance failed ({exc.code}); the approval was not kept"})
        outcome.tool_calls += eng
        if exc.code != "submission_no_longer_valid" or outcome.verdict != "approved":
            raise
        outcome.verdict = "rejected"
        outcome.decisions = [{**d, "result": "fail" if d["result"] == "pass" else d["result"],
                              "reason": clip(f"{d['reason']} (the frozen checks no longer pass: {exc.message})", 4000)} for d in outcome.decisions]
        with db.begin_nested():
            _, row = approval_requests.decide_as_agent(db, agent_profile=profile, execution=ex, request_id=req.id, decision="rejected",
                                                       comment=clip(f"the frozen checks no longer pass at acceptance: {exc.message}", 4000),
                                                       criterion_decisions=outcome.decisions, lease_token=claim.lease_token)
    else:
        outcome.tool_calls += eng
    ex.decision_id = row.id
    return True


def _apply_review(db: Session, claim: Claim, run: WorkflowRun, nt: CollaborationNodeTask, ex: AgentExecution, actor: ActorRef,
                  outcome: _ReviewOutcome, usage: dict[str, Any], result: dict[str, Any], decided: bool) -> str:
    nt.budget = _add_usage(nt.budget, usage)
    ex.result = result
    limits = limits_of(nt.limits)
    if decided:
        _finish_execution(db, ex, "delivered", outcome, usage)  # the decision hooks already set the phase and cleared the lease
    elif claim.role == "pre_review" and outcome.status in ("decided", "unknown"):
        _finish_execution(db, ex, "delivered", outcome, usage)
        nt.phase = "in_review"  # a pre-review never releases anything: the person decides (A31)
        nt.blocked_reason = None
        _clear_lease(nt)
    elif outcome.status == "unknown":
        _finish_execution(db, ex, "blocked", outcome, usage)
        unknown = [d for d in outcome.decisions if d["result"] == "unknown"]
        _block(nt, "unknown_verdict", "the review agent could not decide some criteria; evidence is missing",
               criteria=[{"criterion_id": d["criterion_id"], "reason": d["reason"]} for d in unknown], execution_id=str(ex.id), resume_phase="in_review")
    elif outcome.status == "stale":
        _finish_execution(db, ex, "failed", outcome, usage)
        _clear_lease(nt)
        sub = db.get(TaskSubmission, nt.current_submission_id) if nt.current_submission_id else None
        if nt.phase in ("in_review", "submitted") and (sub is None or sub.status != "pending"):
            nt.phase = "rework"
    elif outcome.status == "deferred":  # the project stopped accepting decisions: back to the queue, no attempt counted
        _finish_execution(db, ex, "cancelled", outcome, usage)
        nt.phase = claim.prev_phase
        nt.blocked_reason = None
        _clear_lease(nt)
    elif outcome.status == "blocked":  # not a technical failure: the node cannot be decided now, for a specific reason
        _finish_execution(db, ex, "cancelled", outcome, usage)
        code = (outcome.error or {}).get("code") or "review_blocked"
        message = (outcome.error or {}).get("message") or "the review cannot decide this node now"
        if code == "inputs_stale":
            _reprepare(db, run, nt, f"the inputs changed since this task was prepared ({message})")
        elif nt.phase == "blocked":
            pass  # already blocked (e.g. an upstream task was reopened): keep that reason
        else:
            _block(nt, code, message, resume_phase=claim.prev_phase, execution_id=str(ex.id), role=claim.role)
    else:  # technical failure of the review
        _finish_execution(db, ex, "failed", outcome, usage)
        budget = budget_of(nt.budget)
        budget["attempts"] = int(budget["attempts"]) + 1
        nt.budget = budget
        reason = budget_exceeded(limits, budget)
        if reason is not None:
            _block(nt, **reason)
        else:
            _clear_lease(nt)
    _bump(nt)
    _audit(db, action=f"collab.{claim.role}.{ex.status}", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"execution_id": str(ex.id), "run_id": str(run.id), "node_id": nt.node_id, "phase": nt.phase, "verdict": outcome.verdict,
                 "error": ex.error, "simulated": outcome.simulated})
    return ex.status


def _engineering_approvals(db: Session, nt: CollaborationNodeTask, ex: AgentExecution, profile: AgentProfile, outcome: _ReviewOutcome) -> list[dict[str, Any]]:
    """Criteria that require an engineering approval stage: the independent review
    agent records it (same approvals service) before accepting, so the frozen
    checks pass. Returns tool-call records."""
    from . import approvals
    from .status import stale_reasons
    from .submissions import SUBJECT_OF_TYPE

    sub = db.get(TaskSubmission, outcome.submission_id) if outcome.submission_id else None
    if sub is None:
        return []
    snapshot = sub.snapshot or {}
    stage_of_crit = {str(c["id"]): (c.get("rule_spec") or {}).get("require_approval_stage") for c in snapshot.get("criteria") or []}
    passed = {d["criterion_id"] for d in outcome.decisions if d["result"] == "pass"}
    calls: list[dict[str, Any]] = []
    done: set[tuple[str, str]] = set()
    for item in snapshot.get("items") or []:
        for cid in item.get("criterion_ids") or []:
            stage = stage_of_crit.get(str(cid))
            if not stage or str(cid) not in passed:
                continue
            subject = SUBJECT_OF_TYPE.get(item.get("asset_type") or "")
            if subject is None:
                continue
            v = db.get(AssetVersion, uuid.UUID(item["asset_version_id"]))
            if v is None:
                continue
            key = (item["asset_version_id"], stage)
            if key in done:
                continue
            done.add(key)
            sid = getattr(v, subject[1])
            if approvals.is_approved(db, subject[0], sid, stage):
                continue
            rec: dict[str, Any] = {"name": "engineering_review", "args": {"asset_version_id": item["asset_version_id"], "stage": stage, "decision": "approved"}, "at": _now().isoformat()}
            try:
                with db.begin_nested():
                    row = approvals.decide_as_agent(db, agent_profile=profile, subject_type=subject[0], subject_id=sid, stage=stage, decision="approved",
                                                    comment=f"engineering {stage} approval by review agent (execution {ex.id})", node_run_id=nt.node_run_id,
                                                    stale_check=stale_reasons)
                rec.update(ok=True, message=f"{stage} approved", data={"approval_id": str(row.id)})
            except AppError as exc:
                rec.update(ok=False, error={"code": exc.code, "message": exc.message})
            calls.append(rec)
    return calls


# --- inputs freshness (A18: nothing is accepted on superseded inputs) -------------------------------------


def _upstream_ids(run: WorkflowRun, node_id: str) -> list[str]:
    return sorted({e["source"] for e in (run.graph_snapshot or {}).get("edges", []) if e.get("target") == node_id})


def _downstream_ids(run: WorkflowRun, node_id: str) -> list[str]:
    edges = (run.graph_snapshot or {}).get("edges", [])
    seen: set[str] = set()
    frontier = [node_id]
    while frontier:
        cur = frontier.pop()
        for e in edges:
            if e.get("source") == cur and e.get("target") not in seen:
                seen.add(e["target"])
                frontier.append(e["target"])
    return sorted(seen)


def _open_upstream(db: Session, run: WorkflowRun, nt: CollaborationNodeTask) -> list[str]:
    """Direct upstream nodes that are not accepted right now."""
    out = []
    for nid in _upstream_ids(run, nt.node_id):
        up = node_task_of(db, run.id, nid)
        if up is not None and up.phase not in RELEASED_PHASES:
            out.append(nid)
    return out


def _stale_inputs(db: Session, run_id: uuid.UUID, nt: CollaborationNodeTask) -> list[dict[str, Any]]:
    """Bound inputs that no longer equal their upstream's current formal output
    (same slot, version and hash, upstream accepted). Fixed inputs never go stale."""
    slots = {s.get("slot_key"): s for s in nt.input_slots or []}
    out: list[dict[str, Any]] = []
    for b in nt.bound_inputs or []:
        src = b.get("from_node_id")
        if not src:
            continue
        up = node_task_of(db, run_id, src)
        want = (slots.get(b.get("slot_key")) or {}).get("source_slot_key")
        fo = next((f for f in (up.formal_outputs if up is not None else None) or [] if f.get("slot_key") == want), None)
        fresh = (up is not None and up.phase in RELEASED_PHASES and fo is not None
                 and str(fo.get("asset_version_id")) == str(b.get("asset_version_id")) and fo.get("content_hash") == b.get("content_hash"))
        if not fresh:
            out.append({"slot_key": b.get("slot_key"), "from_node_id": src, "bound_asset_version_id": b.get("asset_version_id"),
                        "current_asset_version_id": fo.get("asset_version_id") if fo else None, "upstream_phase": up.phase if up is not None else None})
    return out


def _reprepare(db: Session, run: WorkflowRun, nt: CollaborationNodeTask, message: str, **extra: Any) -> None:
    """Send a node back through preparation: phase blocked `upstream_invalidated`
    and its NodeRun pending; the engine releases it to ready as soon as every
    upstream step has succeeded and the business_task handler rebinds the inputs
    (withdrawing a pending submission that rests on the old ones). Caller holds run → node task."""
    from ..workflow import engine

    _block(nt, "upstream_invalidated", message, **extra)
    engine.requeue_node(db, run, nt.node_id)


# --- decision hooks (called by approval_requests; design §4 C05) --------------------------------------


def lock_for_decision(db: Session, nt: CollaborationNodeTask, *, decision: str, submission: TaskSubmission,
                      lease_token: uuid.UUID | None = None) -> tuple[WorkflowRun, CollaborationNodeTask]:
    """Before accept / reject: lock run → node task (the caller holds task,
    submission and request), verify the lease when an agent decides and refuse
    to accept a node that cannot be released. Raises 409."""
    from ..workflow import engine

    run = engine.lock_run(db, nt.run_id)
    nt = lock_node_task(db, nt.id)
    if lease_token is not None and nt.lease_token != lease_token:
        raise conflict("lease_lost", "the agent's lease on this node was taken over; its result is kept as history only")
    if nt.phase == "cancelled" or run.cancel_requested:
        raise conflict("node_cancelled", "the node or its run was cancelled")
    if decision == "approved":
        if nt.phase == "blocked" and (nt.blocked_reason or {}).get("code") == "upstream_invalidated":
            raise conflict("node_blocked", "an upstream task was reopened; this submission rests on invalidated inputs", blocked_reason=nt.blocked_reason)
        stale = _stale_inputs(db, run.id, nt)
        if stale:
            raise conflict("inputs_stale", "the submission rests on inputs that are no longer the upstream tasks' accepted outputs", inputs=stale)
        nr = db.get(NodeRun, nt.node_run_id)
        if nr is None or nr.status != "waiting_human":
            raise conflict("node_not_waiting", f"the node step is {nr.status if nr else 'missing'}; it cannot be released now",
                           status=nr.status if nr else None)
        items = [{"asset_version_id": i["asset_version_id"], "asset_type": i.get("asset_type"), "content_hash": i["content_hash"]}
                 for i in (submission.snapshot or {}).get("items") or []]
        _, missing = _slot_assignment(db, nt, items)
        if missing:
            raise conflict("output_slot_missing", "the submission does not fill every required output slot", slots=missing)
    return run, nt


def on_task_accepted(db: Session, *, run: WorkflowRun, nt: CollaborationNodeTask, submission: TaskSubmission, decision_row: ApprovalDecision,
                     actor: ActorRef) -> dict[str, Any]:
    """After submissions.accept: freeze the formal outputs, register formal and
    evidence resources, write NodeRun.outputs and release the downstream nodes
    exactly once (engine.complete_waiting(approved=True)); when every node task
    is accepted the run completes and the knowledge hook fires. Caller holds
    task → submission → request → run → node task."""
    from ..workflow import engine

    # acceptance.final (W0 §2.1): a Turbo run has no acceptance at all — never an accepted node, approver or time.
    if exec_policy.run_check("acceptance.final", policy_of(db, run.id), dict).get("status") == "skipped_turbo":
        raise conflict("turbo_no_acceptance", "a Turbo run has no acceptance; its outputs stay completed and unverified")
    items = [{"asset_version_id": i["asset_version_id"], "asset_type": i.get("asset_type"), "content_hash": i["content_hash"]}
             for i in (submission.snapshot or {}).get("items") or []]
    formal, missing = _slot_assignment(db, nt, items)
    if missing:
        raise conflict("output_slot_missing", "the submission does not fill every required output slot", slots=missing)
    if nt.formal_outputs:
        resources.supersede_formal(db, node_task=nt, reason="replaced by a newer accepted submission")
    for f in formal:
        v = db.get(AssetVersion, uuid.UUID(f["asset_version_id"]))
        if v is not None:
            resources.register(db, run=run, kind="formal", asset_version=v, node_task=nt, node_run_id=nt.node_run_id, submission_id=submission.id,
                               slot_key=f["slot_key"], metadata={"phase": "accepted", "reason": "accepted", "decision_id": str(decision_row.id),
                                                                 "actor_kind": actor.kind, "actor_id": str(actor.id)})
    for cd in decision_row.criterion_decisions or []:
        for raw in cd.get("evidence_asset_version_ids") or []:
            v = db.get(AssetVersion, uuid.UUID(str(raw)))
            if v is not None:
                resources.register(db, run=run, kind="evidence", asset_version=v, node_task=nt, node_run_id=nt.node_run_id, submission_id=submission.id,
                                   slot_key=f"criterion:{cd.get('criterion_id')}"[:80],
                                   metadata={"phase": "accepted", "reason": "criterion_evidence", "criterion_id": cd.get("criterion_id"),
                                             "result": cd.get("result"), "decision_id": str(decision_row.id)})
    nt.formal_outputs = formal
    nt.accepted_submission_id = submission.id
    nt.current_submission_id = submission.id
    nt.phase = "accepted"
    nt.blocked_reason = None
    _clear_lease(nt)
    _bump(nt)
    nr = db.get(NodeRun, nt.node_run_id)
    assert nr is not None
    nr.outputs = {**(nr.outputs or {}),
                  "outputs": {f["slot_key"]: {"asset_version_id": f["asset_version_id"], "content_hash": f["content_hash"]} for f in formal},
                  "accepted_submission_id": str(submission.id), "decision_id": str(decision_row.id)}
    nr.steps = (nr.steps or []) + [{"kind": "accepted", "ok": True, "at": _now().isoformat(),
                                    "detail": {"submission_id": str(submission.id), "decision_id": str(decision_row.id), "actor": actor.kind,
                                               "actor_id": str(actor.id)}}]
    released = False
    if nr.status == "waiting_human":
        engine.complete_waiting(db, run, nt.node_id, approved=True)
        released = True
    else:  # lock_for_decision refuses this; kept as a guard
        log.warning("node %s of run %s accepted while its step is %s; downstream not released", nt.node_id, run.id, nr.status)
    _audit(db, action="collab.node_accepted", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nt.node_id, "submission_id": str(submission.id), "formal_outputs": formal, "released": released})
    completed = _check_run_completed(db, run)
    db.flush()
    return {"formal_outputs": formal, "released": released, "run_completed": completed}


def _check_run_completed(db: Session, run: WorkflowRun) -> bool:
    """Completion (requirements §2.2): every node task accepted with a valid accepted
    submission, run not paused or cancelled. Then knowledge.on_run_completed."""
    collab = db.get(CollaborationRun, run.id)
    if collab is None or collab.completed_at is not None or run.paused or run.cancel_requested:
        return False
    if collab.execution_policy == "turbo":  # never accepted, never archived as accepted knowledge (AC-T07)
        return False
    rows = db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id)).all()
    if not rows or any(nt.phase != "accepted" or nt.accepted_submission_id is None for nt in rows):
        return False
    collab.completed_at = _now()
    try:
        with db.begin_nested():
            knowledge.on_run_completed(db, run)
    except Exception:  # noqa: BLE001 - the archive can be retried (C09); completion stands
        log.exception("knowledge.on_run_completed failed for run %s", run.id)
    _audit(db, action="collab.run_completed", subject_type="run", subject_id=run.id, actor=None, project_id=run.project_id, data={})
    return True


def on_task_rejected(db: Session, *, run: WorkflowRun, nt: CollaborationNodeTask, submission: TaskSubmission, decision_row: ApprovalDecision,
                     actor: ActorRef, comment: str) -> dict[str, Any]:
    """After submissions.reject: rejected items become `failed` resources, the
    phase goes to rework (never a failed NodeRun, A11) and an agent executor is
    re-queued; one business rework is counted and may reach the limit (A30:
    blocked, no human fallback)."""
    invalidated = nt.phase == "blocked" and (nt.blocked_reason or {}).get("code") == "upstream_invalidated"
    for i in (submission.snapshot or {}).get("items") or []:
        v = db.get(AssetVersion, uuid.UUID(i["asset_version_id"]))
        if v is not None:
            resources.register(db, run=run, kind="failed", asset_version=v, node_task=nt, node_run_id=nt.node_run_id, submission_id=submission.id,
                               metadata={"phase": nt.phase if invalidated else "rework", "reason": (comment or "rejected")[:2000],
                                         "decision_id": str(decision_row.id), "actor_kind": actor.kind})
    if invalidated:
        # The submission rested on inputs an upstream reopen invalidated: no rework is burned and the
        # node stays blocked until the handler rebinds its inputs (after the upstream is accepted again).
        nt.current_submission_id = submission.id
        _bump(nt)
        _audit(db, action="collab.node_rejected", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
               data={"run_id": str(run.id), "node_id": nt.node_id, "submission_id": str(submission.id), "phase": nt.phase,
                     "reworks": budget_of(nt.budget)["reworks"], "upstream_invalidated": True})
        db.flush()
        return {"phase": nt.phase, "reworks": budget_of(nt.budget)["reworks"], "blocked_reason": nt.blocked_reason}
    budget = budget_of(nt.budget)
    budget["reworks"] = int(budget["reworks"]) + 1
    nt.budget = budget
    nt.current_submission_id = submission.id
    reason = budget_exceeded(limits_of(nt.limits), budget) if nt.executor_kind == "agent" else None
    if reason is not None:
        _block(nt, **reason)
    else:
        nt.phase = "rework"
        nt.blocked_reason = None
        _clear_lease(nt)
    _bump(nt)
    _audit(db, action="collab.node_rejected", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nt.node_id, "submission_id": str(submission.id), "phase": nt.phase, "reworks": budget["reworks"]})
    db.flush()
    return {"phase": nt.phase, "reworks": budget["reworks"], "blocked_reason": nt.blocked_reason}


def on_submission_created(db: Session, task: Task, submission: TaskSubmission) -> None:
    """submissions hook: a submission of a node task (person or agent) → submitted
    (pre-review queued) or in_review. Caller holds project → task → submission; locks run → node task."""
    from ..workflow import engine

    nt = node_task_for_task(db, task.id)
    if nt is None:
        return
    engine.lock_run(db, nt.run_id)
    nt = lock_node_task(db, nt.id)
    nt.current_submission_id = submission.id
    if nt.phase != "executing" or nt.executor_kind != "agent":  # an agent's own write-back keeps its lease until it finishes
        _clear_lease(nt)
    nt.phase = "submitted" if nt.pre_reviewer_agent_id else "in_review"
    nt.blocked_reason = None
    _bump(nt)


def on_submission_withdrawn(db: Session, task: Task, submission: TaskSubmission) -> None:
    """submissions hook: the pending submission was withdrawn → rework; running reviews become history."""
    from ..workflow import engine

    nt = node_task_for_task(db, task.id)
    if nt is None or nt.current_submission_id != submission.id:
        return
    engine.lock_run(db, nt.run_id)
    nt = lock_node_task(db, nt.id)
    for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running",
                                                       AgentExecution.role.in_(("review", "pre_review")))).all():
        ex.status = "superseded"
        ex.error = {"code": "submission_withdrawn", "message": "the submission was withdrawn while it was being reviewed"}
        ex.finished_at = _now()
    # A node blocked (e.g. upstream_invalidated: its inputs are rebound by the handler) stays blocked;
    # only a node that was waiting for review goes back to rework.
    if nt.phase in ("submitted", "in_review"):
        nt.phase = "rework"
        nt.blocked_reason = None
        _clear_lease(nt)
    _bump(nt)


def on_task_transition(db: Session, task: Task, *, action: str, actor: ActorRef, reason: str) -> dict[str, Any] | None:
    """tasks.transition hook for node tasks (caller holds project → task):
    start (people executing their node; refused while the node is blocked),
    cancel (the node step is cancelled) and reopen (A18)."""
    nt = node_task_for_task(db, task.id)
    if nt is None:
        return None
    if action == "start":
        from ..workflow import engine

        if nt.phase == "blocked" or nt.phase in ("accepted", "cancelled"):
            raise conflict("node_blocked", "this collaboration task is not ready yet (its inputs are not bound)", phase=nt.phase,
                           blocked_reason=nt.blocked_reason)
        engine.lock_run(db, nt.run_id)
        nt = lock_node_task(db, nt.id)
        if nt.executor_kind == "human" and nt.phase == "ready":
            nt.phase = "executing"
            _bump(nt)
        return {"collaboration": {"run_id": str(nt.run_id), "node_id": nt.node_id, "phase": nt.phase}}
    if action == "cancel":
        return on_task_cancelled(db, task=task, actor=actor, reason=reason)
    if action == "reopen":
        return on_task_reopened(db, task=task, actor=actor, reason=reason)
    return None


def on_task_reopened(db: Session, *, task: Task, actor: ActorRef, reason: str) -> dict[str, Any] | None:
    """Reopen an accepted node (A18): formal outputs lose their status (superseded
    rows), the NodeRun waits again — or, when an upstream task is not accepted
    now or its accepted outputs changed, goes back through preparation (pending,
    blocked `upstream_invalidated`) so nothing is accepted on superseded inputs;
    downstream steps that were not accepted go back to pending with their node
    tasks blocked `upstream_invalidated`; accepted downstream nodes keep their
    state and get an impact notice. Caller holds project → task."""
    from ..workflow import engine
    from .tasks import notice_out

    nt = node_task_for_task(db, task.id)
    if nt is None:
        return None
    run = engine.lock_run(db, nt.run_id)
    nt = lock_node_task(db, nt.id)
    collab = db.get(CollaborationRun, run.id)
    if run.cancel_requested:
        raise conflict("run_finished", "the run was cancelled")
    superseded = resources.supersede_formal(db, node_task=nt, reason=f"reopened: {reason}"[:500])
    invalidated = knowledge.invalidate_items(db, nt)  # archived items of this node stop being reusable for adoption
    for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running")).all():
        ex.status = "superseded"
        ex.error = {"code": "task_reopened", "message": "the task was reopened"}
        ex.finished_at = _now()
    nt.formal_outputs = []
    nt.accepted_submission_id = None
    # Never re-accept on superseded inputs: when an upstream task is not accepted right now, or
    # its accepted outputs are no longer the ones this task was prepared with, the node goes back
    # through preparation (NodeRun pending; the handler rebinds the inputs once the upstream steps
    # have succeeded) instead of waiting for a decision on stale inputs.
    open_up = _open_upstream(db, run, nt)
    stale = [] if open_up else _stale_inputs(db, run.id, nt)
    if open_up:
        _block(nt, "upstream_invalidated", f"upstream task(s) {', '.join(open_up)} are not accepted; inputs are rebound after they are accepted again",
               node_ids=open_up)
    elif stale:
        _block(nt, "upstream_invalidated", "the upstream outputs changed since this task was prepared; its inputs are rebound first", inputs=stale)
    else:
        nt.phase = "rework"
        nt.blocked_reason = None
        _clear_lease(nt)
    _bump(nt)
    result = engine.reopen_node(db, run, nt.node_id, reprepare=bool(open_up or stale))
    notices: list[ImpactNotice] = []
    for nid in result["reset"]:
        down = node_task_of(db, run.id, nid)
        if down is None:
            continue
        down = lock_node_task(db, down.id)
        for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == down.id, AgentExecution.status == "running")).all():
            ex.status = "superseded"
            ex.error = {"code": "upstream_invalidated", "message": f"upstream node {nt.node_id} was reopened"}
            ex.finished_at = _now()
        if down.phase not in ("cancelled",):
            _block(down, "upstream_invalidated", f"upstream task {nt.node_id} was reopened; inputs are rebound after it is accepted again",
                   node_id=nt.node_id)
        _bump(down)
    for nid in result["accepted"]:
        down = node_task_of(db, run.id, nid)
        if down is None:
            continue
        n = ImpactNotice(id=uuid.uuid4(), project_id=run.project_id, task_id=down.task_id, submission_id=down.accepted_submission_id,
                         reason="upstream_reopened", severity="warning",
                         details={"run_id": str(run.id), "upstream_node_id": nt.node_id, "node_id": nid, "upstream_task_id": str(task.id), "reason": reason})
        db.add(n)
        db.flush()
        outbox.impact_opened(db, n, actor.user_id)
        notices.append(n)
    if collab is not None:
        collab.completed_at = None
    _audit(db, action="collab.node_reopened", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nt.node_id, "reason": reason, "reset": result["reset"], "accepted_downstream": result["accepted"],
                 "superseded_resources": len(superseded), "invalidated_knowledge_items": invalidated})
    db.flush()
    return {"run_id": str(run.id), "node_id": nt.node_id, "reset_nodes": result["reset"], "impacted_nodes": result["accepted"],
            "impacts": [notice_out(n) for n in notices]}


def on_task_cancelled(db: Session, *, task: Task, actor: ActorRef, reason: str) -> dict[str, Any] | None:
    """Cancel one node (C04 cancel): the NodeRun is cancelled, the downstream steps
    follow the engine rules (skipped) and every node whose step can no longer run
    is closed too (pending submission withdrawn, Task cancelled), so nothing is
    stranded and project completion is not blocked. Lock order: project(share) →
    every Task of the run in id order (C04 cancel already holds them) → run → node tasks."""
    from ..workflow import engine

    nt = node_task_for_task(db, task.id)
    if nt is None:
        return None
    lock_run_tasks(db, nt.run_id)
    run = engine.lock_run(db, nt.run_id)
    nt = lock_node_task(db, nt.id)
    for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running")).all():
        ex.status = "cancelled"
        ex.error = {"code": "task_cancelled", "message": reason or "cancelled"}
        ex.finished_at = _now()
    nt.phase = "cancelled"
    nt.blocked_reason = None
    _clear_lease(nt)
    _bump(nt)
    nr = db.get(NodeRun, nt.node_run_id)
    if nr is not None and nr.status not in engine.TERMINAL_NODE:
        nr.status = "cancelled"
        nr.finished_at = _now()
        nr.error = {"code": "task_cancelled", "message": reason or "cancelled"}
    engine._activate_downstream(db, run)
    user = (db.get(User, actor.user_id) if actor.user_id else None) or _system_user(db, run.project_id)
    closed = _close_stranded(db, run, user=user, actor=actor, reason=f"upstream task {nt.node_id} was cancelled" + (f": {reason}" if reason else ""))
    engine.refresh_run_status(db, run)
    _audit(db, action="collab.node_cancelled", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
           data={"run_id": str(run.id), "node_id": nt.node_id, "reason": reason, "closed_nodes": closed})
    return {"collaboration": {"run_id": str(run.id), "node_id": nt.node_id, "phase": "cancelled", "run_status": run.status, "closed_nodes": closed}}


STRANDED_NODE = ("failed", "skipped", "cancelled")


def _close_node(db: Session, nt: CollaborationNodeTask, *, user: User, actor: ActorRef | None, reason: str, phase: str) -> None:
    """Close a node whose step will never run again: pending submission withdrawn,
    running executions cancelled, Task cancelled, phase cancelled / failed.
    Caller holds the node's Task (then the run)."""
    from .submissions import withdraw_locked
    from .tasks import bump

    task = db.get(Task, nt.task_id)
    if task is not None:
        for s in db.scalars(select(TaskSubmission).where(TaskSubmission.task_id == task.id, TaskSubmission.status == "pending")).all():
            with features.closure_scope(db, nt.run_id):
                withdraw_locked(db, user, s, reason=reason)
    nt = lock_node_task(db, nt.id)
    if task is not None and task.status in ("todo", "in_progress", "in_review"):
        old = task.status
        task.status = "cancelled"
        bump(task)
        _audit(db, action="task.cancel", subject_type="task", subject_id=task.id, actor=actor, project_id=task.project_id,
               data={"from": old, "to": "cancelled", "reason": reason})
        outbox.task_status(db, task, user.id, old, reason)
    for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running")).all():
        ex.status = "cancelled"
        ex.error = {"code": "node_closed", "message": reason}
        ex.finished_at = _now()
    if nt.phase not in RELEASED_PHASES:
        nt.phase = phase
        nt.blocked_reason = {"code": "node_failed", "message": reason} if phase == "failed" else None
    _clear_lease(nt)
    _bump(nt)


def _close_stranded(db: Session, run: WorkflowRun, *, user: User, actor: ActorRef | None, reason: str) -> list[str]:
    """Close every node of the run whose step can no longer run (failed, skipped
    or cancelled NodeRun) but whose Task or phase is still open. Collaboration
    steps are never retried by hand, so these Tasks would otherwise stay open
    forever. Caller holds every Task of the run, then the run lock."""
    statuses = dict(db.execute(select(NodeRun.id, NodeRun.status).where(NodeRun.run_id == run.id)).all())
    closed: list[str] = []
    for nt in db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id).order_by(CollaborationNodeTask.id)).all():
        st = statuses.get(nt.node_run_id)
        if st not in STRANDED_NODE or nt.phase in RELEASED_PHASES:
            continue
        task = db.get(Task, nt.task_id)
        phase = "failed" if st == "failed" else "cancelled"
        if nt.phase in ("cancelled", "failed") and (task is None or task.status in ("done", "cancelled")):
            continue
        _close_node(db, nt, user=user, actor=actor, reason=reason if st != "failed" else f"the task's step failed: {reason}", phase=phase)
        closed.append(nt.node_id)
    return closed


def close_stranded_run(db: Session, run_id: uuid.UUID) -> list[str]:
    """Engine hook (a step failed for good) and worker sweep: close the open Tasks
    of the steps of a collaboration run that can no longer run. Lock order:
    project(share) → Tasks (id order) → run → node tasks. Caller commits."""
    from ..workflow import engine

    if db.get(CollaborationRun, run_id) is None:
        return []
    if not features.can_close_run(db, run_id):
        return []
    lock_run_tasks(db, run_id)
    run = engine.lock_run(db, run_id)
    user = db.get(User, run.created_by) or _system_user(db, run.project_id)
    closed = _close_stranded(db, run, user=user, actor=None, reason="the collaboration step can no longer run")
    if closed:
        engine.refresh_run_status(db, run)
        _audit(db, action="collab.nodes_closed", subject_type="run", subject_id=run.id, actor=None, project_id=run.project_id,
               data={"nodes": closed, "run_status": run.status})
    db.flush()
    return closed


def fail_blocked_turbo_nodes() -> int:
    """Worker sweep (W7): Turbo nodes blocked for a person outside a write-back (agent disabled or budget used up at
    claim time, lost leases) fail with that reason; each in its own transaction, lock order run → node task, then
    their stranded Tasks are closed. Returns the number of failed nodes."""
    from ..workflow import engine

    with session_scope() as db:
        rows = db.execute(select(CollaborationNodeTask.id, CollaborationNodeTask.run_id)
                          .join(CollaborationRun, CollaborationRun.run_id == CollaborationNodeTask.run_id)
                          .where(CollaborationRun.execution_policy == "turbo", CollaborationNodeTask.phase == "blocked")).all()
    n = 0
    for nt_id, run_id in rows:
        try:
            with session_scope() as db:
                if not features.can_close_run(db, run_id):
                    continue
                run = engine.lock_run(db, run_id)
                nt = lock_node_task(db, nt_id)
                failed = _turbo_settle(db, run, nt)
            if failed:
                n += 1
                _close_after_turbo_failure(run_id)
        except Exception:  # noqa: BLE001 - retried by the next sweep
            log.exception("could not fail the blocked Turbo node %s", nt_id)
    return n


def close_stranded_runs(db: Session) -> int:
    """Worker sweep: collaboration runs with failed / skipped / cancelled steps whose
    Tasks are still open (e.g. a step that failed through a lease expiry). Each run in a savepoint."""
    run_ids = db.scalars(
        select(CollaborationNodeTask.run_id)
        .join(NodeRun, NodeRun.id == CollaborationNodeTask.node_run_id)
        .join(Task, Task.id == CollaborationNodeTask.task_id)
        .where(NodeRun.status.in_(STRANDED_NODE), Task.status.notin_(("done", "cancelled")))
        .distinct()
    ).all()
    n = 0
    for rid in run_ids:
        try:
            with db.begin_nested():
                n += len(close_stranded_run(db, rid))
        except Exception:  # noqa: BLE001 - retried by the next sweep
            log.exception("could not close the stranded tasks of run %s", rid)
    return n


def on_run_cancelled(db: Session, user: User, run_id: uuid.UUID) -> WorkflowRun:
    """POST /runs/{id}/cancel for a collaboration run (the whole cancel, in lock
    order): project(share) → tasks → pending submissions / requests withdrawn →
    run (engine.cancel_run, engineering requests closed) → node tasks cancelled,
    unfinished Tasks → cancelled, running executions → cancelled. Caller commits."""
    from ..workflow import engine
    from .approval_requests import close_for_run
    from .submissions import withdraw_locked
    from .tasks import bump

    tasks = lock_run_tasks(db, run_id)
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    if run.status in TERMINAL_RUN:
        raise conflict("run_finished", "the run already finished")
    for t in tasks:
        for s in db.scalars(select(TaskSubmission).where(TaskSubmission.task_id == t.id, TaskSubmission.status == "pending")).all():
            with features.continuation_scope(db, run.id):
                withdraw_locked(db, user, s, reason="run cancelled")
    run = engine.lock_run(db, run_id)
    engine.cancel_run(db, run)
    close_for_run(db, run.id, status="withdrawn")
    actor = actors.of_user(user)
    cancelled: list[str] = []
    for nt in db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id).order_by(CollaborationNodeTask.id)).all():
        nt = lock_node_task(db, nt.id)
        task = db.get(Task, nt.task_id)
        if task is not None and task.status in ("todo", "in_progress", "in_review"):
            old = task.status
            task.status = "cancelled"
            bump(task)
            _audit(db, action="task.cancel", subject_type="task", subject_id=task.id, actor=actor, project_id=task.project_id,
                   data={"from": old, "to": "cancelled", "reason": "run cancelled"})
            outbox.task_status(db, task, user.id, old, "run cancelled")
        for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running")).all():
            ex.status = "cancelled"
            ex.error = {"code": "run_cancelled", "message": "the run was cancelled"}
            ex.finished_at = _now()
        if nt.phase not in RELEASED_PHASES:
            nt.phase = "cancelled"
            nt.blocked_reason = None
            cancelled.append(nt.node_id)
        _clear_lease(nt)
        _bump(nt)
    _audit(db, action="collab.run_cancelled", subject_type="run", subject_id=run.id, actor=actor, project_id=run.project_id, data={"nodes": cancelled})
    audit(db, action="run.cancel", subject_type="run", subject_id=run.id, user_id=user.id, project_id=run.project_id)
    db.flush()
    return run


# --- C04 node actions ----------------------------------------------------------------------------------


def _member_role(db: Session, user: User, project_id: uuid.UUID) -> str | None:
    m = db.get(ProjectMember, {"project_id": project_id, "user_id": user.id})
    return m.role if m else None


def _run_control_allowed(db: Session, user: User, run: WorkflowRun, role: str | None) -> bool:
    """C06 pause / resume / fork: project owner or the person who started the run
    (same rule as services.collaboration.run_actions; cancel needs `workflow` only)."""
    if role is None or role == "viewer":
        return False
    return role == "owner" or run.created_by == user.id


def allowed_actions(db: Session, user: User, node_task: CollaborationNodeTask) -> list[str]:
    """Node actions the user may perform now (C04 names), by mode, actor and phase.
    Agent-executed nodes never offer start / add_deliverable / submit to people;
    full-auto nodes never offer adopt_baseline (fork first)."""
    run = db.get(WorkflowRun, node_task.run_id)
    collab = db.get(CollaborationRun, node_task.run_id)
    if run is None or collab is None or run.cancel_requested or run.status in ("partial", "failed", "cancelled"):
        return []
    role = _member_role(db, user, run.project_id)
    if role is None or role == "viewer":
        return []
    frozen = not features.enabled("collaboration")
    if frozen and not features.can_continue(db, "collaboration", run_id=run.id):
        return []
    is_owner = role == "owner"
    is_executor = node_task.executor_kind == "human" and node_task.executor_user_id == user.id
    may = is_owner or is_executor
    phase = node_task.phase
    if collab.execution_policy == "turbo":  # nobody executes, submits, reviews or reopens a Turbo node by hand
        return ["cancel"] if may and phase not in (*RELEASED_PHASES, "cancelled", "failed") and run.status not in TERMINAL_RUN else []
    if run.status == "succeeded" or run.paused:
        out = ["check"] if may and phase not in ("blocked", "ready", "cancelled") else []
        if phase == "accepted" and may and not run.paused and not frozen:
            out.append("reopen")
        return out
    task = db.get(Task, node_task.task_id)
    out: list[str] = []
    if node_task.executor_kind == "human" and may:
        if phase == "ready" and task is not None and task.status == "todo":
            out.append("start")
        if phase in ("executing", "rework") and task is not None and task.status == "in_progress":
            out += ["add_deliverable", "check", "submit"]
    if may and "check" not in out and phase in ("executing", "rework", "submitted", "in_review"):
        out.append("check")
    pending = None
    if node_task.current_submission_id:
        sub = db.get(TaskSubmission, node_task.current_submission_id)
        pending = sub if sub is not None and sub.status == "pending" else None
    if pending is not None and may and phase in ("submitted", "in_review", "blocked"):
        out.append("withdraw")
    if phase == "accepted" and may and not frozen:
        out.append("reopen")
    if phase not in ("accepted", "cancelled", "failed") and may:
        out.append("cancel")
    if collab.execution_mode == "semi_auto" and phase in ("ready", "executing", "rework", "blocked") and may and pending is None:
        out.append("adopt_baseline")
    return list(dict.fromkeys(out))


def run_actions(db: Session, user: User, run: WorkflowRun) -> list[str]:
    """Run-level actions for this user (mirrors services.collaboration.run_actions)."""
    collab = db.get(CollaborationRun, run.id)
    if collab is None or run.cancel_requested or run.status in TERMINAL_RUN:
        return []
    role = _member_role(db, user, run.project_id)
    out: list[str] = []
    if (_run_control_allowed(db, user, run, role)
            and (features.enabled("collaboration") or features.can_continue(db, "collaboration", run_id=run.id))):
        out.append("resume" if run.paused else "pause")
        if collab.execution_mode == "full_auto" and collab.execution_policy != "turbo" and features.enabled("collaboration"):
            out.append("fork_semi_auto")
    if role is not None and role != "viewer":
        out.append("cancel")
    return out


def task_action(db: Session, user: User, run_id: uuid.UUID, node_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """C04: POST /runs/{id}/nodes/{node_id}/task-action. Status changes go through
    the task / submission services (whose hooks move the phase). Caller commits."""
    from ..security import project_access

    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    project_access(db, user, run.project_id, "workflow")
    if body.get("action") == "reopen":
        features.require_enabled("collaboration")
    with collaboration_scope(), features.continuation_scope(db, run_id):
        return _task_action(db, user, run_id, node_id, body)


def _task_action(db: Session, user: User, run_id: uuid.UUID, node_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from ..security import project_access
    from . import submissions as sub_svc
    from . import tasks as task_svc

    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    acc = project_access(db, user, run.project_id, "workflow")
    collab = _collab(db, run.id)
    nt = node_task_of(db, run.id, node_id)
    if nt is None:
        raise not_found("node task")
    action = body.get("action")
    if action not in NODE_ACTIONS:
        raise AppError(422, "unknown_action", f"unknown node action {action}", {"actions": list(NODE_ACTIONS)})
    expected = body.get("expected_version")
    if expected is not None and int(expected) != nt.version:
        raise conflict("version_conflict", "the node changed; reload first", current_version=nt.version)
    if run.cancel_requested or run.status in ("partial", "failed", "cancelled"):
        raise conflict("run_finished", "the run is finished or cancelled")
    if run.paused and action not in ("check",):
        raise conflict("run_paused", "the run is paused; resume it first")
    allowed = allowed_actions(db, user, nt)
    if action not in allowed:
        if collab.execution_policy == "turbo" and action != "cancel":
            raise conflict("turbo_no_human_action", "a Turbo run has no human execution, submission, review or reopen; "
                           "start a standard run for that", action=action)
        if action in ("start", "add_deliverable", "submit") and nt.executor_kind == "agent":
            raise conflict("mode_forbids", "this node is executed by an agent; people cannot act for it", executor_kind=nt.executor_kind)
        if action == "adopt_baseline" and collab.execution_mode != "semi_auto":
            raise conflict("mode_forbids", "a full-auto node has no human baseline adoption; fork the run to semi-auto first",
                           execution_mode=collab.execution_mode)
        raise AppError(403, "actor_not_allowed", f"{action} is not available to you in phase {nt.phase}", {"allowed": allowed, "phase": nt.phase})
    actor = actors.of_user(user)
    task = db.get(Task, nt.task_id)
    assert task is not None
    extra: dict[str, Any] = {}
    reason = str(body.get("reason") or "").strip()
    if action == "start":
        _, t_extra = task_svc.transition(db, acc, task.id, action="start", reason=reason, expected_version=task.version)
        extra.update(t_extra)
    elif action == "add_deliverable":
        vid = body.get("asset_version_id")
        if not vid:
            raise AppError(422, "asset_version_required", "give the asset_version_id to deliver")
        slot_key = body.get("slot_key")
        if slot_key and slot_key not in {s["slot_key"] for s in nt.output_slots or []}:
            raise AppError(422, "unknown_slot", f"{slot_key} is not an output slot of this node", {"slot_key": slot_key})
        crit = [uuid.UUID(str(c)) for c in body.get("criterion_ids") or []]
        d = task_svc.upsert_deliverable(db, acc, task.id, asset_version_id=uuid.UUID(str(vid)), criterion_ids=crit, note=str(body.get("note") or ""))
        v = db.get(AssetVersion, d.asset_version_id)
        from ..workflow import engine

        engine.lock_run(db, run.id)
        nt = lock_node_task(db, nt.id)
        resources.register(db, run=run, kind="candidate", asset_version=v, node_task=nt, node_run_id=nt.node_run_id, slot_key=slot_key,
                           metadata={"phase": nt.phase, "reason": "add_deliverable", "actor_kind": "human", "actor_id": str(user.id)})
        _bump(nt)
        extra["deliverable"] = task_svc.deliverable_out(db, d)
    elif action == "check":
        extra["checks"] = sub_svc.preview_checks(db, task)
    elif action == "submit":
        _require_output_slots(db, nt, task)
        sub = sub_svc.create_submission(db, acc, task.id, note=str(body.get("note") or ""), expected_version=task.version)
        extra["submission"] = sub_svc.submission_out(db, sub, with_snapshot=False)
    elif action == "withdraw":
        sub = db.get(TaskSubmission, nt.current_submission_id) if nt.current_submission_id else None
        if sub is None or sub.status != "pending":
            raise conflict("submission_closed", "there is no pending submission to withdraw")
        sub_svc.withdraw(db, user, sub.id)
        _audit(db, action="collab.node_withdrawn", subject_type="node_task", subject_id=nt.id, actor=actor, project_id=run.project_id,
               data={"submission_id": str(sub.id), "reason": reason})
    elif action == "reopen":
        if not reason:
            raise AppError(422, "reason_required", "give a reason to reopen the task")
        _, t_extra = task_svc.transition(db, acc, task.id, action="reopen", reason=reason, expected_version=task.version)
        extra.update(t_extra)
    elif action == "cancel":
        # Lock order: project(share) → every Task of the run in id order (this node's downstream
        # Tasks are closed by the cancel hook) → submissions / requests → run → node tasks.
        lock_run_tasks(db, run.id)
        sub = db.get(TaskSubmission, nt.current_submission_id) if nt.current_submission_id else None
        if sub is not None and sub.status == "pending":
            sub_svc.withdraw(db, user, sub.id)
        task = db.get(Task, nt.task_id)
        assert task is not None
        if task.status not in ("todo", "in_progress"):
            raise conflict("invalid_transition", f"a {task.status} task cannot be cancelled", status=task.status)
        _, t_extra = task_svc.transition(db, acc, task.id, action="cancel", reason=reason, expected_version=task.version, cancel_runs=False)
        extra.update(t_extra)
    elif action == "adopt_baseline":
        items_raw = body.get("items") or {}
        if not isinstance(items_raw, dict) or not items_raw:
            raise AppError(422, "items_required", "give the baseline items {slot: asset_version_id}")
        items = {k: (uuid.UUID(str(v)) if v else None) for k, v in items_raw.items()}
        snap, notices, created = task_svc.adopt_baseline(db, acc, task.id, items=items, reason=reason, expected_version=task.version)
        from ..workflow import engine

        engine.lock_run(db, run.id)
        nt = lock_node_task(db, nt.id)
        requeued = False
        if created:
            for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running",
                                                               AgentExecution.role == "execute")).all():
                ex.status = "superseded"
                ex.error = {"code": "baseline_changed", "message": "the baseline changed; the attempt is re-queued on the new baseline"}
                ex.finished_at = _now()
                requeued = True
            if nt.executor_kind == "agent" and nt.phase in ("executing", "ready", "rework"):
                nt.phase = "rework" if int(budget_of(nt.budget)["reworks"]) > 0 else "ready"
                _clear_lease(nt)
        _bump(nt)
        task = db.get(Task, nt.task_id)
        extra["baseline"] = task_svc.baseline_out(db, snap, task.current_baseline_id if task else None)
        extra["impacts"] = [task_svc.notice_out(n) for n in notices]
        extra["requeued"] = requeued
    db.flush()
    nt = db.get(CollaborationNodeTask, nt.id)
    db.refresh(nt)
    out = node_task_out(db, user, nt)
    out["action"] = {"action": action, **extra}
    return out


# --- answers to a blocked agent's questions ---------------------------------------------------------------

MAX_ANSWERS = 20


def answer_questions(db: Session, user: User, run_id: uuid.UUID, node_id: str, *, answers: list[dict[str, str]],
                     expected_version: int | None = None) -> dict[str, Any]:
    """A person answers the questions an agent was blocked on (``blocked_reason.questions``: the CAD tools'
    clarification questions, else the agent's missing facts). The answers are kept on the node task, the node goes
    back to its queue phase, and the next attempt's state and CAD tools receive them, exactly as a workflow node's
    clarification answers. Same control rights as resume (project owner or the person who started the run); the
    budget is not reset. Caller commits."""
    run, _collab_row, _role = _control_access(db, user, run_id)
    features.require_run_continuation(db, run_id)
    nt = node_task_of(db, run.id, node_id)
    if nt is None:
        raise not_found("node task")
    nt = lock_node_task(db, nt.id)
    if expected_version is not None and int(expected_version) != nt.version:
        raise conflict("version_conflict", "the node changed; reload first", current_version=nt.version)
    if run.cancel_requested or run.status in TERMINAL_RUN:
        raise conflict("run_finished", "the run is finished or cancelled")
    br = nt.blocked_reason or {}
    if nt.phase != "blocked" or br.get("code") != "agent_blocked":
        raise conflict("node_not_waiting_for_answers", "the node is not blocked on questions", phase=nt.phase,
                       blocked_reason=nt.blocked_reason)
    clean = [{"question": str(a.get("question") or "").strip()[:2000], "answer": str(a.get("answer") or "").strip()[:4000]}
             for a in answers if str(a.get("answer") or "").strip()]
    if not clean:
        raise AppError(422, "answers_required", "give at least one non-empty answer")
    stamp = _now().isoformat()
    who = actors.of_user(user)
    nt.clarifications = (list(nt.clarifications or []) + [{**a, "by": str(user.id), "at": stamp} for a in clean])[-MAX_ANSWERS * 5:]
    nt.phase = _queue_phase(nt, br.get("resume_phase") or "ready")
    nt.blocked_reason = None
    _clear_lease(nt)
    _bump(nt)
    _audit(db, action="collab.answer", subject_type="node_task", subject_id=nt.id, actor=who, project_id=run.project_id,
           data={"node_id": nt.node_id, "answers": len(clean)})
    db.flush()
    return node_task_out(db, user, nt)


# --- C06 pause / resume / fork ----------------------------------------------------------------------------

# agent_stopped: a person stopped the attempt (POST /agent-executions/{id}/stop); model_missing_capability: tool_agent
# found no tool calling (resumable once diagnosis enables it). Historical runtime_unavailable blocks remain readable;
# requeueing them still validates the frozen runtime and never substitutes another executor.
RESUMABLE_BLOCKS = {"agent_blocked", "unknown_verdict", "agent_disabled", "node_not_waiting", "agent_stopped", "model_missing_capability",
                    "runtime_unavailable"}


def _control_access(db: Session, user: User, run_id: uuid.UUID) -> tuple[WorkflowRun, CollaborationRun, str]:
    from ..security import project_access

    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    acc = project_access(db, user, run.project_id, "workflow")
    collab = _collab(db, run.id)
    if not _run_control_allowed(db, user, run, acc.role):
        raise forbidden("only a project owner or the person who started the run may control it", role=acc.role)
    return run, collab, acc.role


def pause_run(db: Session, user: User, run_id: uuid.UUID, *, expected_version: int, reason: str) -> tuple[WorkflowRun, CollaborationRun]:
    """C06 pause: claim_next and the agent queue skip the run; a running attempt finishes
    but its result stays a candidate (no submission). Caller commits."""
    from ..workflow import engine

    _control_access(db, user, run_id)
    features.require_run_continuation(db, run_id)
    run = engine.lock_run(db, run_id)
    collab = _collab(db, run.id)
    if collab.control_version != expected_version:
        raise conflict("version_conflict", "the run control state changed; reload first", current_version=collab.control_version)
    if run.cancel_requested or run.status in TERMINAL_RUN:
        raise conflict("run_finished", "the run is finished or cancelled")
    if not run.paused:
        run.paused = True
        collab.paused_at = _now()
        collab.pause_reason = (reason or "").strip()[:2000] or None
        collab.control_version += 1
        _audit(db, action="collab.pause", subject_type="run", subject_id=run.id, actor=actors.of_user(user), project_id=run.project_id,
               data={"reason": reason})
    db.flush()
    return run, collab


def resume_run(db: Session, user: User, run_id: uuid.UUID, *, expected_version: int) -> tuple[WorkflowRun, CollaborationRun]:
    """C06 resume: the run is claimable again. Nodes blocked for a recoverable reason
    (agent reported blocked, unknown verdict, disabled agent re-enabled) go back to
    their queue phase; budgets are never reset, budget_exceeded stays blocked."""
    from ..workflow import engine

    _control_access(db, user, run_id)
    features.require_run_continuation(db, run_id)
    run = engine.lock_run(db, run_id)
    collab = _collab(db, run.id)
    if collab.control_version != expected_version:
        raise conflict("version_conflict", "the run control state changed; reload first", current_version=collab.control_version)
    if run.cancel_requested or run.status in TERMINAL_RUN:
        raise conflict("run_finished", "the run is finished or cancelled")
    requeued: list[str] = []
    for nt in db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id, CollaborationNodeTask.phase == "blocked")
                         .order_by(CollaborationNodeTask.id)).all():
        br = nt.blocked_reason or {}
        if br.get("code") in RESUMABLE_BLOCKS and br.get("resume_phase"):
            nt = lock_node_task(db, nt.id)
            nt.phase = _queue_phase(nt, br["resume_phase"])  # a stored "executing" (lease lost to a disabled agent) → rework / ready
            nt.blocked_reason = None
            _clear_lease(nt)
            _bump(nt)
            requeued.append(nt.node_id)
    if run.paused or requeued:
        run.paused = False
        collab.paused_at = None
        collab.pause_reason = None
        collab.control_version += 1
        _audit(db, action="collab.resume", subject_type="run", subject_id=run.id, actor=actors.of_user(user), project_id=run.project_id,
               data={"requeued": requeued})
    _check_run_completed(db, run)  # a decision may have accepted the last node while the run was paused
    db.flush()
    return run, collab


def execution_simulated(db: Session, execution_id: uuid.UUID) -> bool:
    """Whether an execution ran on the simulated model (its result, else its first timeline event)."""
    ex = db.get(AgentExecution, execution_id)
    if ex is not None and isinstance(ex.result, dict) and "simulated" in ex.result:
        return bool(ex.result["simulated"])
    first = db.scalar(select(AgentExecutionEvent.payload).where(AgentExecutionEvent.execution_id == execution_id)
                      .order_by(AgentExecutionEvent.seq).limit(1))
    return bool((first or {}).get("simulated"))


def stop_execution(db: Session, user: User, execution_id: uuid.UUID, *, reason: str = "") -> AgentExecution:
    """POST /agent-executions/{id}/stop (execution-layer design §6.5): same rights as pause (project owner or the
    person who started the run). The node is blocked ``agent_stopped`` (resumable: resume requeues it) and its lease
    cleared, so the running attempt's ``alive()`` is false at once and its result can only be history (the usage it
    spent still counts). Lock order: run → node task → execution. Caller commits."""
    from ..workflow import engine

    ex = db.get(AgentExecution, execution_id)
    if ex is None:
        raise not_found("agent execution")
    _control_access(db, user, ex.run_id)
    run = engine.lock_run(db, ex.run_id)
    nt = lock_node_task(db, ex.node_task_id)
    ex = db.scalar(select(AgentExecution).where(AgentExecution.id == execution_id).with_for_update().execution_options(populate_existing=True))
    assert ex is not None
    if ex.status != "running":
        raise conflict("execution_finished", f"the execution is {ex.status}; there is nothing to stop", status=ex.status)
    if ex.stop_requested_at is not None:
        return ex
    ex.stop_requested_at = _now()
    message = (reason or "").strip()[:2000] or f"stopped by {user.name}"
    if nt.lease_token == ex.lease_token:
        resume_phase = _queue_phase(nt) if ex.role == "execute" else nt.phase
        _block(nt, "agent_stopped", message, execution_id=str(ex.id), role=ex.role, resume_phase=resume_phase)
        _bump(nt)
    _audit(db, action="collab.execution.stop", subject_type="agent_execution", subject_id=ex.id, actor=actors.of_user(user),
           project_id=run.project_id, data={"run_id": str(run.id), "node_id": nt.node_id, "role": ex.role, "reason": message})
    _append_event(db, ex.id, "system", {"code": "stopped", "message": message, "simulated": execution_simulated(db, ex.id)})
    db.flush()
    return ex


def fork_semi_auto(db: Session, user: User, run_id: uuid.UUID, *, expected_version: int, reviewer_user_id: uuid.UUID, idempotency_key: str,
                   reason: str) -> tuple[WorkflowRun, CollaborationRun, bool]:
    """A34: pause the full-auto run and start a semi-auto instance from its frozen
    contract (every reviewer = the person, executors kept, pre_reviewer = the old
    review agent). The original's candidate and formal versions become candidates
    of the new nodes; nothing inherits an approval. Same key + same request →
    the existing fork (created=False); same key, other request → 409.
    Lock order: project (numbering) → run. Caller commits."""
    from ..workflow import engine
    from .lifecycle import lock_project, require_active

    run, _, _ = _control_access(db, user, run_id)
    features.require_enabled("collaboration")
    if policy_of(db, run.id) == "turbo":  # a Turbo contract (agent bindings, assumptions) is not a semi-auto contract
        raise conflict("turbo_fork_unsupported", "a Turbo run cannot be forked to semi-auto; plan the draft again in standard mode")
    request_hash = canon_hash({"fork_of": str(run_id), "reviewer_user_id": str(reviewer_user_id), "reason": (reason or "").strip()})
    # The idempotency lookup runs under the project lock: a concurrent identical fork waits here and
    # then returns the fork the first one created (instead of a second fork or a unique violation).
    lock_project(db, run.project_id, share=False)
    existing = db.scalar(select(CollaborationRun).where(CollaborationRun.project_id == run.project_id, CollaborationRun.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise conflict("idempotency_mismatch", "this idempotency key was used for a different request", run_id=str(existing.run_id))
        fork_run = db.get(WorkflowRun, existing.run_id)
        assert fork_run is not None
        return fork_run, existing, False
    require_active(db, run.project_id, what="forking a run")  # a fork starts new work: the project must be active
    run = engine.lock_run(db, run_id)
    collab = _collab(db, run.id)
    if collab.execution_mode != "full_auto":
        raise conflict("mode_mismatch", "only a full-auto instance can be forked to semi-auto", execution_mode=collab.execution_mode)
    if collab.control_version != expected_version:
        raise conflict("version_conflict", "the run control state changed; reload first", current_version=collab.control_version)
    if run.cancel_requested:
        raise conflict("run_finished", "the run was cancelled")
    reviewer = actors.validate(db, run.project_id, ActorRef(kind="human", user_id=reviewer_user_id), purpose="review", mode="semi_auto")
    graph = dict(run.graph_snapshot)
    contract = dict(collab.contract or {})
    nodes_c = {k: dict(v) for k, v in (contract.get("nodes") or {}).items()}
    new_nodes = []
    for n in graph.get("nodes", []):
        params = dict(n.get("params") or {})
        if n.get("type") == "business_task":
            old_reviewer = params.get("reviewer") or {}
            pre = old_reviewer.get("agent_profile_id") if old_reviewer.get("kind") == "agent" else params.get("pre_reviewer_agent_id")
            executor = params.get("executor") or {}
            if executor.get("kind") == "human" and str(executor.get("user_id")) == str(reviewer.user_id):
                raise AppError(422, "self_review", "the reviewer executes one of the tasks; choose another reviewer", {"node_id": n["id"]})
            if executor.get("kind") == "agent":
                executor_ref = actors.validate(db, run.project_id, executor, purpose="execute", mode="semi_auto")
                frozen = profile_snapshot(db, collab, executor_ref.id)
                actors.require_supported_runtime(frozen.get("runtime"), agent_profile_id=executor_ref.id)
            if pre:
                p = db.get(AgentProfile, uuid.UUID(str(pre)))
                if p is None or not p.enabled or p.purpose not in ("review", "general"):
                    pre = None  # the old reviewer cannot pre-review any more; the person reviews alone
                else:
                    actors.require_supported_runtime(p.runtime, agent_profile_id=p.id)
                    frozen = profile_snapshot(db, collab, p.id)
                    actors.require_supported_runtime(frozen.get("runtime"), agent_profile_id=p.id)
            params["reviewer"] = {"kind": "human", "user_id": str(reviewer.user_id), "agent_profile_id": None}
            params["pre_reviewer_agent_id"] = str(pre) if pre else None
            if n["id"] in nodes_c:
                nodes_c[n["id"]]["reviewer"] = params["reviewer"]
                nodes_c[n["id"]]["pre_reviewer_agent_id"] = params["pre_reviewer_agent_id"]
        new_nodes.append({**n, "params": params})
    graph["nodes"] = new_nodes
    # The forked graph is not the template version the original froze: the contract records where it
    # came from (forked_from) and materialize_run recomputes graph_hash from the new graph snapshot.
    forked_from = {"run_id": str(run.id), "graph_hash": contract.get("graph_hash"), "template_version_id": contract.get("template_version_id"),
                   "contract_hash": collab.contract_hash, "reason": (reason or "").strip() or None}
    contract.pop("graph_hash", None)
    contract.update({"execution_mode": "semi_auto", "nodes": nodes_c, "forked_from_run_id": str(run.id), "forked_from": forked_from,
                     "template_version_id": None})
    if not run.paused and run.status not in TERMINAL_RUN:
        run.paused = True
        collab.paused_at = _now()
        collab.pause_reason = f"forked to semi-auto: {reason}"[:2000]
    collab.control_version += 1
    new_run, new_collab = materialize_run(
        db, user=user, project_id=run.project_id, graph=graph, contract=contract, execution_mode="semi_auto",
        role_snapshots=dict(collab.role_snapshots or {}), workflow_id=run.workflow_id, template_version_id=None,
        title=f"{run.title} (semi-auto)"[:200], idempotency_key=idempotency_key, request_hash=request_hash, forked_from_run_id=run.id,
        fork_reason=(reason or "").strip() or None, part_id=run.part_id, draft_id=collab.draft_id,
    )
    copied = 0
    for old_nt in db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id)).all():
        new_nt = node_task_of(db, new_run.id, old_nt.node_id)
        if new_nt is None:
            continue
        # The budget is cumulative across the fork (§3.9: never reset); the new node starts from what was spent.
        new_nt.budget = budget_of(old_nt.budget)
        seen: set[uuid.UUID] = set()
        for r in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == old_nt.id, WorkflowResource.kind.in_(("candidate", "formal")))
                            .order_by(WorkflowResource.created_at)).all():
            if r.asset_version_id in seen:
                continue
            seen.add(r.asset_version_id)
            v = db.get(AssetVersion, r.asset_version_id)
            if v is not None:
                resources.register(db, run=new_run, kind="candidate", asset_version=v, node_task=new_nt, node_run_id=new_nt.node_run_id, slot_key=r.slot_key,
                                   metadata={"phase": "blocked", "reason": "forked", "from_run_id": str(run.id), "from_resource_id": str(r.id),
                                             "from_kind": r.kind})
                copied += 1
    _audit(db, action="collab.fork", subject_type="run", subject_id=run.id, actor=actors.of_user(user), project_id=run.project_id,
           data={"new_run_id": str(new_run.id), "reviewer_user_id": str(reviewer_user_id), "reason": reason, "candidates": copied})
    db.flush()
    return new_run, new_collab, True


def materialize_run(
    db: Session,
    *,
    user: User,
    project_id: uuid.UUID,
    graph: dict[str, Any],
    contract: dict[str, Any],
    execution_mode: str,
    role_snapshots: dict[str, Any],
    workflow_id: uuid.UUID | None = None,
    template_version_id: uuid.UUID | None = None,
    title: str = "",
    idempotency_key: str | None = None,
    request_hash: str | None = None,
    forked_from_run_id: uuid.UUID | None = None,
    fork_reason: str | None = None,
    part_id: uuid.UUID | None = None,
    draft_id: uuid.UUID | None = None,
    fixed_inputs: list[dict[str, Any]] | None = None,
) -> tuple[WorkflowRun, CollaborationRun]:
    """Create the WorkflowRun, the CollaborationRun, one Task (+ criteria,
    dependencies) and one node task per business_task node (design §4 C02
    steps 4–6). Backend C's start() may call this; fork and the tests do.
    Caller holds nothing yet; locks project(share=False) for task numbering."""
    from ..workflow import engine
    from ..workflow.nodes import BusinessTaskParams
    from .lifecycle import lock_project, require_active
    from .tasks import RuleSpec

    features.require_enabled("collaboration")
    lock_project(db, project_id, share=False)
    require_active(db, project_id, what="starting a collaboration run")  # new work needs an active project (as C02 start)
    run = engine.create_run(db, graph=graph, project_id=project_id, part_id=part_id, user_id=user.id, origin="workflow", title=title,
                            workflow_id=workflow_id, workflow_version=None)
    run.template_version_id = template_version_id
    db.flush()
    contract = dict(contract)
    contract.setdefault("schema", 1)
    contract.setdefault("execution_mode", execution_mode)
    contract.setdefault("graph_hash", canon_hash(run.graph_snapshot))
    collab = CollaborationRun(run_id=run.id, project_id=project_id, workflow_id=workflow_id, template_version_id=template_version_id, draft_id=draft_id,
                              execution_mode=execution_mode, idempotency_key=idempotency_key, request_hash=request_hash, contract=contract,
                              contract_hash=canon_hash(contract), role_snapshots=role_snapshots, forked_from_run_id=forked_from_run_id,
                              fork_reason=fork_reason, created_by=user.id)
    db.add(collab)
    node_runs = {nr.node_id: nr for nr in db.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all()}
    number = db.scalar(select(func.max(Task.number)).where(Task.project_id == project_id)) or 0
    tasks_by_node: dict[str, Task] = {}
    nodes_contract = dict(contract.get("nodes") or {})
    for n in run.graph_snapshot.get("nodes", []):
        if n.get("type") != "business_task":
            continue
        p = BusinessTaskParams.model_validate(n.get("params") or {})
        executor = ActorRef(kind=p.executor.kind, user_id=p.executor.user_id, agent_profile_id=p.executor.agent_profile_id)
        reviewer = ActorRef(kind=p.reviewer.kind, user_id=p.reviewer.user_id, agent_profile_id=p.reviewer.agent_profile_id)
        number += 1
        task = Task(project_id=project_id, number=number, title=p.title, objective=p.objective, owner_id=executor.user_id, reviewer_id=reviewer.user_id,
                    part_id=part_id or contract.get("part_id") and uuid.UUID(str(contract["part_id"])), required=True, status="todo", created_by=user.id)
        db.add(task)
        db.flush()
        crit_out = []
        for i, c in enumerate(p.criteria, start=1):
            spec = RuleSpec.model_validate(c.rule_spec or {}).model_dump(mode="json")
            row = AcceptanceCriterion(task_id=task.id, seq=i, description=c.description, required=c.required, allowed_types=list(c.allowed_types),
                                      rule_spec=spec, manual_required=c.manual_required)
            db.add(row)
            db.flush()
            crit_out.append({"id": str(row.id), "seq": i, "description": c.description, "required": c.required, "allowed_types": list(c.allowed_types),
                             "rule_spec": spec, "manual_required": c.manual_required})
        exec_snap = role_snapshots.get(str(executor.agent_profile_id)) if executor.is_agent else None
        limits = limits_of((exec_snap or {}).get("limits")) if executor.is_agent else limits_of(None)
        if executor.is_agent and exec_snap is None:
            prof = db.get(AgentProfile, executor.agent_profile_id)
            limits = limits_of(prof.limits if prof else None)
        nt = CollaborationNodeTask(run_id=run.id, node_id=n["id"], node_run_id=node_runs[n["id"]].id, task_id=task.id, pre_reviewer_agent_id=p.pre_reviewer_agent_id,
                                   input_slots=[s.model_dump(mode="json") for s in p.input_slots], output_slots=[s.model_dump(mode="json") for s in p.output_slots],
                                   recipe=p.recipe.model_dump(mode="json"), limits=limits, phase="blocked", budget=dict(EMPTY_BUDGET))
        actors.assign(nt, "executor", executor)
        actors.assign(nt, "reviewer", reviewer)
        db.add(nt)
        tasks_by_node[n["id"]] = task
        node_c = dict(nodes_contract.get(n["id"]) or {})
        node_c.update({"title": p.title, "objective": p.objective, "executor": executor.model_dump(mode="json"), "reviewer": reviewer.model_dump(mode="json"),
                       "pre_reviewer_agent_id": str(p.pre_reviewer_agent_id) if p.pre_reviewer_agent_id else None, "criteria": crit_out,
                       "output_slots": nt.output_slots, "input_slots": nt.input_slots, "recipe": nt.recipe, "limits": limits, "task_id": str(task.id)})
        nodes_contract[n["id"]] = node_c
        audit(db, action="task.create", subject_type="task", subject_id=task.id, user_id=user.id, project_id=project_id,
              data={"number": number, "title": task.title, "run_id": str(run.id), "node_id": n["id"]})
    for e in run.graph_snapshot.get("edges", []):
        pred, succ = tasks_by_node.get(e["source"]), tasks_by_node.get(e["target"])
        if pred is not None and succ is not None and pred.id != succ.id:
            db.add(TaskDependency(predecessor_id=pred.id, successor_id=succ.id))
    contract["nodes"] = nodes_contract
    collab.contract = contract
    collab.contract_hash = canon_hash(contract)
    db.flush()
    for fi in fixed_inputs or contract.get("fixed_inputs") or []:
        v = db.get(AssetVersion, uuid.UUID(str(fi["asset_version_id"])))
        if v is None:
            continue
        db.execute(pg_insert(RunInputBinding).values(id=uuid.uuid4(), run_id=run.id, port_key=f"fixed:{fi['slot_key']}", asset_version_id=v.id,
                                                     content_hash=v.content_hash, snapshot={"slot_key": fi["slot_key"], "fixed": True})
                   .on_conflict_do_nothing(index_elements=["run_id", "port_key"]))
        resources.register(db, run=run, kind="input", asset_version=v, slot_key=fi["slot_key"], metadata={"phase": "start", "reason": "fixed_input"})
    audit(db, action="run.start", subject_type="run", subject_id=run.id, user_id=user.id, project_id=project_id,
          data={"origin": "collaboration", "execution_mode": execution_mode, "forked_from_run_id": str(forked_from_run_id) if forked_from_run_id else None})
    db.flush()
    return run, collab


# --- projections (backend C's services.collaboration owns the C03 views) ------------------------------------


def node_task_out(db: Session, user: User, nt: CollaborationNodeTask, *, with_steps: bool = False) -> dict[str, Any]:
    from . import collaboration as collab_svc

    run = db.get(WorkflowRun, nt.run_id)
    assert run is not None
    return collab_svc.node_task_out(db, user, run, nt, with_steps=with_steps)


def collaboration_view(db: Session, user: User, run: WorkflowRun, collab: CollaborationRun) -> dict[str, Any]:
    from . import collaboration as collab_svc

    return collab_svc.collaboration_out(db, user, run, collab)
