"""Unified AI workspace: conversations, composer, requests, operations (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §3–§6).

A conversation is one person's private record of work in one project. Each
request message keeps an immutable snapshot (text, target, references,
constraints, task, model config, client_request_id). An operation is one
traceable request: it starts existing business work (task draft, a modeling or
drawing-intake run, a read-only answer, or a plan for a part workflow change or a
new part workflow that is applied only on confirmation) through the existing
services, so permissions,
project lifecycle, task status and collaboration guards are checked on the
server exactly as elsewhere.

Send = two transactions: (1) user message + operation (pending) + assistant
operation message, committed; (2) the business step. A business error rolls
back (2) and records the operation as failed in a fresh transaction, so a
failure is never shown as a success. Operations that start runs follow the run
lazily (`sync_operation`, idempotent, under SELECT … FOR UPDATE).

Lock order (model / drawing operations): project (share) → part → task →
operations → conversations. Never sync an operation while holding a
conversation lock.

AI native (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md): an ``auto`` request can become a main-agent turn
that the assistant worker runs (services/orchestrator.py); the hooks here route it, leave it pending, follow it and
wake it when one of its sub-agents (ordinary operations of this module) finishes.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai import limits, team_prompt
from ..errors import AppError, bad_request, conflict, not_found
from ..models import (
    Asset,
    AssetVersion,
    AiContextBuild,
    AiConversation,
    AiMessage,
    AiOperation,
    File,
    FileVersion,
    ModelRevision,
    NodeRun,
    Part,
    Project,
    ProjectMember,
    RunOutput,
    Task,
    User,
    Workflow,
    WorkflowRun,
)
from ..security import ProjectAccess, project_access
from . import assistant_materials as materials_svc
from . import assistant_refs as refs
from . import execution_policy as policy_svc
from . import feature_flags as features

log = logging.getLogger("partroute.assistant")

OPERATION_KINDS = ("task_draft", "model_create", "model_modify", "drawing_intake", "ask", "drawing_generate", "drawing_reconstruct",
                   "workflow_edit", "assembly_create", "assembly_modify", "workflow_create", "spec_extract", "drafting_create", "drafting_modify")
#: W8 (services/assembly_ai.py): assembly_create starts the chain run spec → parts → assembly → drawings → delivery;
#: assembly_modify plans W4 edit ops on an exact assembly revision, applied only on confirmation.
ASSEMBLY_KINDS = ("assembly_create", "assembly_modify")
ACTIVE = ("pending", "running", "waiting_input")
TERMINAL = ("succeeded", "failed", "cancelled")
MODEL_KINDS = ("model_create", "model_modify")
#: Drawing operations (CAD AI, design §6): drawing_generate (revision / STEP file → drawing package; text → model → drawing)
#: and drawing_reconstruct (DXF / DWG → STEP, optionally → drawing).
DRAWING_KINDS = ("drawing_generate", "drawing_reconstruct")
#: Operations that write a part's models / drawings: one at a time per part (part_busy).
PART_KINDS = MODEL_KINDS + DRAWING_KINDS
RUN_KINDS = ("model_create", "model_modify", "drawing_intake", *DRAWING_KINDS, "assembly_create")
#: workflow_runs statuses after which a run never changes again (workflow.engine.TERMINAL_RUN).
RUN_TERMINAL = ("succeeded", "partial", "failed", "cancelled")
WRITABLE_TASK_STATUSES = ("todo", "in_progress")
#: Project action each operation needs (design §4.2). workflow_edit: the same rule as saving a part workflow (PUT /workflows/{id});
#: workflow_create (AI work panel): the same rule as creating one (POST /projects/{id}/workflows with a part).
OP_ACTION = {"task_draft": "manage", "model_create": "design", "model_modify": "design", "drawing_intake": "design", "ask": "view",
             "drawing_generate": "design", "drawing_reconstruct": "design", "workflow_edit": "workflow",
             "assembly_create": "design", "assembly_modify": "design", "workflow_create": "workflow",
             "spec_extract": "design", "drafting_create": "design", "drafting_modify": "design"}
#: Model purpose each operation uses (services/assistant_context._PURPOSE; task drafts use the modeling config).
#: workflow_edit plans a graph change in text (no CAD): the workspace's assistant configuration, like ask.
OP_PURPOSE = {"task_draft": "modeling", "model_create": "modeling", "model_modify": "modeling", "drawing_intake": "intake",
              "ask": "assistant", "drawing_generate": "modeling", "drawing_reconstruct": "modeling", "workflow_edit": "assistant",
              # assembly_create: the operation's snapshot seeds the part modeling node; the spec step's planning model is
              # frozen with the run (start_run → freeze_run_models). assembly_modify plans edit ops in text.
              "assembly_create": "modeling", "assembly_modify": "assistant", "workflow_create": "assistant",
              "spec_extract": "planning", "drafting_create": "assistant", "drafting_modify": "assistant"}
RESULT_ACTIONS = ("view", "continue", "use_as_input", "add_deliverable", "open_run", "fork")
MAX_REFERENCES = 30
MAX_CONSTRAINTS = 20
#: A pending operation that never reached its business step (process died) stops blocking after this.
STALE_PENDING = timedelta(minutes=10)
#: IntakeParams.hints max_length (the drawing intake request text goes there).
INTAKE_HINTS_MAX = limits.INTAKE_HINTS_CHARS  # ai/limits.py


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


#: "Field omitted" (as opposed to an explicit null) for send / patch arguments.
UNSET: Any = _Unset()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _uuid(v: Any) -> uuid.UUID | None:
    if v is None or v == "":
        return None
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def _freeze_model(db: Session, kind: str, model_config_id: uuid.UUID | None) -> dict | None:
    """W1: the exact model configuration of a new request (None when nothing is usable: the business step then
    fails with the precise reason)."""
    from . import ai as ai_svc

    return ai_svc.try_freeze_model(db, OP_PURPOSE.get(kind, "assistant"), model_config_id)


def _model_ref(req: dict) -> Any:
    """The frozen purpose entry, including a stored routing failure; never resolve newer defaults."""
    from . import ai as ai_svc

    purpose = OP_PURPOSE.get(req.get("operation"), "assistant")
    return ai_svc.bundle_model_ref(req.get("model_snapshots"), purpose, legacy_snapshot=req.get("model_snapshot"))


# --- access, locks --------------------------------------------------------------------


def load_conversation(db: Session, user: User, conversation_id: uuid.UUID) -> tuple[AiConversation, ProjectAccess]:
    """Owner only, and only while the owner can still view the project; anything else is 404."""
    conv = db.get(AiConversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise not_found("conversation")
    acc = project_access(db, user, conv.project_id, "view")
    return conv, acc


def load_operation(db: Session, user: User, operation_id: uuid.UUID) -> tuple[AiOperation, AiConversation, ProjectAccess]:
    op = db.get(AiOperation, operation_id)
    if op is None:
        raise not_found("operation")
    try:
        conv, acc = load_conversation(db, user, op.conversation_id)
    except AppError:
        raise not_found("operation")
    return op, conv, acc


def lock_conversation(db: Session, conversation_id: uuid.UUID) -> AiConversation:
    conv = db.scalar(select(AiConversation).where(AiConversation.id == conversation_id).with_for_update()
                     .execution_options(populate_existing=True))
    if conv is None:
        raise not_found("conversation")
    return conv


def _lock_op(db: Session, op_id: uuid.UUID) -> AiOperation:
    op = db.scalar(select(AiOperation).where(AiOperation.id == op_id).with_for_update().execution_options(populate_existing=True))
    if op is None:
        raise not_found("operation")
    return op


def require_open(conv: AiConversation) -> None:
    if conv.archived_at is not None:
        raise conflict("conversation_archived", "the conversation is archived; restore it before changing it", conversation_id=str(conv.id))


def _check_version(conv: AiConversation, expected: int | None) -> None:
    if expected is not None and expected != conv.version:
        raise conflict("version_conflict", "the conversation was changed elsewhere; reload first", current_version=conv.version)


def _access_of(db: Session, op: AiOperation) -> ProjectAccess | None:
    """The operation owner's current access to its project (role + capabilities), or None when they left it."""
    member = db.get(ProjectMember, {"project_id": op.project_id, "user_id": op.user_id})
    project = db.get(Project, op.project_id)
    user = db.get(User, op.user_id)
    if member is None or project is None or user is None:
        return None
    return ProjectAccess(project=project, user=user, role=member.role, capabilities=tuple(member.capabilities or ()))


def _can(acc: ProjectAccess | None, action: str) -> bool:
    return acc is not None and acc.can(action)


# --- task policy and part conflicts for assistant CAD operations -------------------------


def require_task_writable(db: Session, user: User, task_id: uuid.UUID, project_id: uuid.UUID) -> Task:
    """A task an AI write may bind to (design §4.2 step 2): same project, not a collaboration
    node task (409 use_collaboration_actions), the user participates, and the task is todo /
    in_progress (else 409 task_not_writable). The task row is locked; call after the part lock."""
    from . import tasks as task_svc
    from .agent_runtime import guard_generic_write

    t = task_svc.lock_task(db, task_id)
    if t.project_id != project_id:
        raise not_found("task")
    acc = project_access(db, user, project_id, "workflow")
    features.require_enabled("tasks")
    guard_generic_write(db, t.id, "start AI work")
    task_svc.require_participant(db, acc, t)
    if t.status not in WRITABLE_TASK_STATUSES:
        raise conflict("task_not_writable", f"AI work cannot be bound to a {t.status} task; reopen it or choose another task",
                       status=t.status, task_id=str(t.id))
    return t


def check_part_free(db: Session, user: User, part_id: uuid.UUID, *, exclude: uuid.UUID | None = None,
                    touched: set[uuid.UUID] | None = None) -> None:
    """409 part_busy while another AI workspace modeling operation on the part is unfinished.
    Unfinished operations of the part are synced first. Caller holds the part lock. With
    `touched`, the conversations of operations that finished are collected for the caller.

    Defence in depth: an operation recorded as finished whose run is still not terminal (a node
    retried outside the assistant) keeps the part busy too. The details name the busy operation
    and its conversation only when they belong to the caller (conversations are private)."""
    from .cad_admission import check_free

    check_free(db, user, part_id, exclude_operation_id=exclude)



# --- output -----------------------------------------------------------------------------


def conversation_out(db: Session, conv: AiConversation) -> dict:
    project = db.get(Project, conv.project_id)
    task = db.get(Task, conv.task_id) if conv.task_id else None
    summary = conv.summary if isinstance(conv.summary, dict) else None
    return {
        "id": str(conv.id),
        "title": conv.title,
        "project": {"id": str(project.id), "code": project.code, "name": project.name} if project else None,
        "task": {"id": str(task.id), "title": task.title, "status": task.status} if task else None,
        "archived": conv.archived_at is not None,
        "archived_at": _iso(conv.archived_at),
        "constraints": list(conv.constraints or []),
        "summary_status": {"through_seq": conv.summary_through_seq, "built_at": summary.get("built_at")} if summary else None,
        "forked_from": conv.forked_from,
        "version": conv.version,
        "last_activity_at": _iso(conv.last_activity_at),
        "created_at": _iso(conv.created_at),
        "autonomy": conv.autonomy,  # AI native: what the main agent asks before a write tool (auto / confirm_task / step)
    }


def _refs_of_message(m: AiMessage) -> list[dict]:
    return ([m.target] if m.target else []) + list(m.references or [])


def message_out(m: AiMessage, statuses: dict[str, dict]) -> dict:
    keys = [r.get("key") for r in _refs_of_message(m)]
    return {
        "id": str(m.id),
        "seq": m.seq,
        "role": m.role,
        "kind": m.kind,
        "text": m.text,
        "target": m.target,
        "references": list(m.references or []),
        "operation_id": str(m.operation_id) if m.operation_id else None,
        "payload": m.payload or {},
        "created_at": _iso(m.created_at),
        "ref_status": {k: statuses[k] for k in keys if k in statuses},
    }


def composer_out(conv: AiConversation, statuses: dict[str, dict] | None = None, db: Session | None = None) -> dict:
    comp = conv.composer or {}
    items = list(comp.get("references") or [])
    if statuses is None:
        assert db is not None
        statuses = refs.ref_statuses(db, items)
    return {"target_key": comp.get("target_key"), "references": items,
            "ref_status": {r["key"]: statuses[r["key"]] for r in items if r.get("key") in statuses}}


def _build_out(b: AiContextBuild | None, *, full: bool) -> dict | None:
    """Backend B's serializer (AiContextBuild in web/src/api/assistant.ts); items only in the operation detail."""
    from . import assistant_context as actx

    return actx.build_out(b, items=full) if b is not None else None


def _latest_builds(db: Session, op_ids: list[uuid.UUID]) -> dict[uuid.UUID, AiContextBuild]:
    out: dict[uuid.UUID, AiContextBuild] = {}
    if not op_ids:
        return out
    for b in db.scalars(select(AiContextBuild).where(AiContextBuild.operation_id.in_(op_ids))
                        .order_by(AiContextBuild.created_at.desc(), AiContextBuild.id.desc())).all():
        out.setdefault(b.operation_id, b)
    return out


def operations_out(db: Session, ops: list[AiOperation], *, full_build: bool = False) -> list[dict]:
    from ..workflow import engine
    from . import orchestrator, cad_results, operation_events

    undo_now = orchestrator.undoable(db, ops)  # AI native: which main-agent steps can be undone now
    task_ids = {o.task_id for o in ops if o.task_id}
    part_ids = {o.part_id for o in ops if o.part_id}
    run_ids = {o.run_id for o in ops if o.run_id}
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(task_ids))).all()} if task_ids else {}
    parts = {p.id: p for p in db.scalars(select(Part).where(Part.id.in_(part_ids))).all()} if part_ids else {}
    runs = {r.id: r for r in db.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids))).all()} if run_ids else {}
    builds = _latest_builds(db, [o.id for o in ops])
    retried_by: dict[uuid.UUID, uuid.UUID] = {}
    if ops:
        for parent, child in db.execute(select(AiOperation.retry_of, AiOperation.id).where(AiOperation.retry_of.in_([o.id for o in ops]))
                                        .order_by(AiOperation.created_at, AiOperation.id)).all():
            retried_by.setdefault(parent, child)
    out = []
    for o in ops:
        t, p, r = tasks.get(o.task_id), parts.get(o.part_id), runs.get(o.run_id)
        result = orchestrator.result_out(o, undo_now.get(o.id))
        from . import design_plans
        result, question = design_plans.project_operation(db, o, result)
        if isinstance(result, dict) and result.get("cad_result"):
            result = {**result, "cad_result": cad_results.result_out(db, o)["cad_result"]}
        out.append({
            "id": str(o.id),
            "conversation_id": str(o.conversation_id),
            "message_id": str(o.message_id),
            "kind": o.kind,
            "status": o.status,
            "generation": o.generation,
            "completion_events": operation_events.public_events(db, o.id),
            "execution_policy": o.execution_policy,
            "policy_source": (o.request or {}).get("policy_source"),  # S5; None for requests from before it
            "request": o.request,
            "task": {"id": str(t.id), "title": t.title, "status": t.status} if t else None,
            "part": {"id": str(p.id), "name": p.name, "number": p.number} if p else None,
            # J: the run's frozen budget caps (no live counts; the run page has those)
            "run": {"id": str(r.id), "status": r.status, "title": r.title, "cancel_requested": r.cancel_requested,
                    "budget": engine.budget_caps(r)} if r else None,
            "question": question,
            "result": result,
            "error": _operation_error(db, o),
            # W1: code, stage, purpose, configuration and call ids of an AI failure (None for business refusals).
            "diagnostic": _diagnostic(db, o) if o.status == "failed" and o.error else None,
            "simulated": o.simulated,
            "retry_of": str(o.retry_of) if o.retry_of else None,
            # The operation that retried this one (each operation is retried at most once; hide the retry button).
            "retried_by": str(retried_by[o.id]) if o.id in retried_by else None,
            "created_at": _iso(o.created_at),
            "updated_at": _iso(o.updated_at),
            "finished_at": _iso(o.finished_at),
            "context_build": _build_out(builds.get(o.id), full=full_build),
            "parent_operation_id": (o.request or {}).get("parent_op_id"),  # AI native: the main-agent turn that started it
        })
    return out


def _diagnostic(db: Session, op: AiOperation) -> dict | None:
    """The W1 diagnostic of a failed operation: its error (ai_unavailable carries the provider reason), the
    operation's frozen model snapshot and the ids of its failed model calls (direct calls or its run's)."""
    from ..ai import diagnostics

    err = op.error or {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    code = details.get("reason") if err.get("code") == "ai_unavailable" and details.get("reason") else err.get("code")
    if not diagnostics.is_ai_failure(err.get("code"), details):
        return None
    from ..models import AiCall

    q = select(AiCall.id).where(AiCall.status != "ok")
    if op.run_id is not None:
        q = q.outerjoin(NodeRun, NodeRun.id == AiCall.node_run_id).where((NodeRun.run_id == op.run_id) | (AiCall.ai_operation_id == op.id))
    else:
        q = q.where(AiCall.ai_operation_id == op.id)
    call_ids = [str(i) for i in db.scalars(q.order_by(AiCall.created_at.desc()).limit(10)).all()]
    stage = "context" if err.get("code") == "context_over_budget" else None
    req = op.request or {}
    return diagnostics.diagnostic(code, message=details.get("detail") or err.get("message"), stage=stage,
                                  purpose=OP_PURPOSE.get(op.kind), snapshot=req.get("model_snapshot"),
                                  config_id=req.get("model_config_id"), call_ids=call_ids,
                                  retryable=details.get("retryable") if isinstance(details.get("retryable"), bool) else None)


def _placement_terminal_calls(calls: list[dict]) -> dict[str, dict | None]:
    """A transaction timestamps a retry batch equally; UUID order cannot identify its terminal request."""
    grouped: dict[str, list[dict]] = {}
    for call in calls:
        if isinstance(unit := call.get("unit"), str):
            grouped.setdefault(unit, []).append(call)
    terminal = {}
    for unit, rows in grouped.items():
        latest = max((r.get("created_at") for r in rows if r.get("created_at") is not None), default=None)
        batch = [r for r in rows if r.get("created_at") == latest]
        attempts = []
        for row in batch:
            try:
                attempt = int(row.get("attempt"))
            except (TypeError, ValueError):
                attempt = 0
            attempts.append((attempt, row))
        last = max((a for a, _ in attempts), default=0)
        candidates = [r for a, r in attempts if a == last]
        selected = candidates[0] if candidates else None
        ambiguous = (last < 1 or latest is None or not selected
                     or any((r.get("status"), r.get("error")) != (selected.get("status"), selected.get("error")) for r in candidates))
        if any(r.get("status") == "ok" for r in batch):
            ambiguous = ambiguous or any(isinstance(r.get("error"), dict) and isinstance(r["error"].get("retry"), dict)
                                         and r["error"]["retry"].get("stopped") for r in batch)
        if not ambiguous and selected.get("status") != "ok":
            error = selected.get("error")
            # A success at an earlier attempt means this timestamp contains multiple logical calls;
            # their sequence is unavailable, so an old failed retry cannot be promoted as current.
            ambiguous = (any(r.get("status") == "ok" for r in batch) or not isinstance(error, dict)
                         or not isinstance(error.get("retry"), dict) or not error["retry"].get("stopped"))
        terminal[unit] = None if ambiguous else selected
    return terminal


def _legacy_placement_causes(notes: list[dict], calls: list[dict]) -> list[dict]:
    """Match surviving placement failures to terminal requests, never to recovered retries."""
    from ..ai.providers.base import RETRYABLE_CLASSES
    from .failure_causes import MAX_CAUSES, cause

    terminal = _placement_terminal_calls(calls)
    causes = []
    for note in notes[:MAX_CAUSES]:
        instances = note.get("instances")
        instances = [i for i in instances if isinstance(i, str)] if isinstance(instances, list) else []
        if isinstance(note.get("error"), dict):
            item = cause(note["error"], stage="placement", instances=instances)
            if item is not None:
                causes.append(item)
            continue
        if note.get("code") != "placement_call_failed":
            continue
        matching = [call for unit, call in terminal.items()
                    if unit.startswith("placement:batch:") and unit.removeprefix("placement:batch:") in instances]
        if matching and all(call is not None for call in matching):
            for call in matching:
                if call.get("status") == "ok" or not isinstance(call.get("error"), dict):
                    continue
                error = call["error"]
                cls = error.get("failure_class")
                item = cause({**error, "retryable": isinstance(cls, str) and cls in RETRYABLE_CLASSES}, stage="placement", instances=instances,
                             call_ids=[str(call["id"])])
                if item is not None:
                    causes.append(item)
        else:
            # An old note is explicit evidence of a failed call, but not of a particular transport class.
            item = cause({"code": "placement_call_failed", "message": note.get("message")},
                         stage="placement", instances=instances)
            if item is not None:
                causes.append(item)
    return causes[:MAX_CAUSES]


def _operation_error(db: Session, op: AiOperation) -> dict | None:
    """Enrich legacy partials in a read projection; saved errors and completion history stay unchanged."""
    from .failure_causes import MAX_CAUSES, public_error

    if not isinstance(op.error, dict) or op.status != "failed" or op.error.get("code") not in ("assembly_partial", "definitions_failed"):
        return copy.deepcopy(op.error)
    error = public_error(op.error)
    if any(c.get("stage") == "placement" for c in error["details"].get("causes") or []):
        return error
    # Use the frozen operation result, not a later retry's node output or the assembly's current head.
    result = op.result if isinstance(op.result, dict) else {}
    chain = result.get("chain")
    steps = chain.get("steps") if isinstance(chain, dict) else []
    steps = steps if isinstance(steps, list) else []
    step = next((s for s in steps if isinstance(s, dict) and s.get("status") == "failed"
                 and isinstance(s.get("error"), dict) and s["error"].get("code") == error["code"]
                 and isinstance(s.get("value"), dict) and s["value"].get("type") == "assembly"), None)
    if step is None:
        return error
    assembly = step["value"]
    expected = error["details"].get("assembly_revision_id")
    if expected and assembly.get("assembly_revision_id") != expected:
        return error
    notes = assembly.get("notes")
    notes = notes if isinstance(notes, list) else []
    notes = [n for n in notes if isinstance(n, dict)
             and (isinstance(n.get("error"), dict) or n.get("code") == "placement_call_failed")][:MAX_CAUSES]
    if not notes:
        return error
    calls = []
    if op.run_id is not None and any(not isinstance(n.get("error"), dict) for n in notes):
        node = db.scalar(select(NodeRun).where(NodeRun.run_id == op.run_id, NodeRun.node_id == step.get("node_id")))
        node_error = (node.error or {}) if node is not None else {}
        same_revision = bool(assembly.get("assembly_revision_id")) and (node_error.get("details") or {}).get("assembly_revision_id") == assembly.get("assembly_revision_id")
        finished = getattr(op, "finished_at", None)
        same_attempt = (node is not None and node.started_at is not None and node.finished_at is not None and finished is not None
                        and node.started_at <= node.finished_at <= finished)
        if node is not None and node.status == "failed" and node_error.get("code") == error["code"] and same_revision and same_attempt:
            from ..models import AiCall

            q = select(AiCall.id, AiCall.status, AiCall.error, AiCall.created_at,
                       AiCall.request["unit"].astext.label("unit"), AiCall.request["attempt"].astext.label("attempt")).where(
                AiCall.node_run_id == node.id, AiCall.purpose == "assembly_placement", AiCall.created_at >= node.started_at)
            if node.finished_at is not None:
                q = q.where(AiCall.created_at <= node.finished_at)
            calls = [dict(row) for row in db.execute(q.order_by(AiCall.created_at.desc(), AiCall.id.desc()).limit(200)).mappings().all()]
    return public_error(op.error, causes=_legacy_placement_causes(notes, calls))


def operation_out(db: Session, op: AiOperation, *, full_build: bool = False) -> dict:
    return operations_out(db, [op], full_build=full_build)[0]


def detail_out(db: Session, conv: AiConversation, *, after_seq: int | None = None) -> dict:
    from . import orchestrator

    stmt = select(AiMessage).where(AiMessage.conversation_id == conv.id).order_by(AiMessage.seq)
    if after_seq is not None:
        stmt = stmt.where(AiMessage.seq > after_seq)
    msgs = list(db.scalars(stmt).all())
    ops = list(db.scalars(select(AiOperation).where(AiOperation.conversation_id == conv.id)
                          .order_by(AiOperation.created_at, AiOperation.id)).all())
    composer_refs = list((conv.composer or {}).get("references") or [])
    statuses = refs.ref_statuses(db, composer_refs + [r for m in msgs for r in _refs_of_message(m)])
    return {
        "conversation": conversation_out(db, conv),
        "messages": [message_out(m, statuses) for m in msgs],
        "operations": operations_out(db, ops),
        "composer": composer_out(conv, statuses),
        "work": orchestrator.work_out(db, conv, ops),  # AI native: the drafts, tasks and parts the main agent made here
    }


# --- conversations ----------------------------------------------------------------------


def _check_task_in_project(db: Session, task_id: uuid.UUID, project_id: uuid.UUID) -> Task:
    t = db.get(Task, task_id)
    if t is None or t.project_id != project_id:
        raise not_found("task")
    return t


def create_conversation(db: Session, user: User, *, project_id: uuid.UUID, task_id: uuid.UUID | None, title: str,
                        autonomy: str | None = None) -> AiConversation:
    """AI native: ``autonomy`` chosen before the first request, else the one the person used last (``orchestrator``)."""
    from . import orchestrator

    project_access(db, user, project_id, "view")
    if task_id is not None:
        _check_task_in_project(db, task_id, project_id)
        features.require_enabled("tasks")
    conv = AiConversation(user_id=user.id, project_id=project_id, task_id=task_id, title=(title or "").strip()[:200],
                          composer={"target_key": None, "references": []}, constraints=[], next_seq=1, version=1,
                          autonomy=autonomy or orchestrator.default_autonomy(db, user))
    db.add(conv)
    db.flush()
    return conv


def list_conversations(db: Session, user: User, *, project_id: uuid.UUID | None, task_id: uuid.UUID | None, archived: str,
                       limit: int) -> list[dict]:
    """The caller's conversations in projects they still belong to, most recent activity first.
    Only conversations with unfinished operations are synced."""
    if project_id is not None:
        project_access(db, user, project_id, "view")
    stmt = (select(AiConversation)
            .join(ProjectMember, (ProjectMember.project_id == AiConversation.project_id) & (ProjectMember.user_id == user.id))
            .where(AiConversation.user_id == user.id))
    if project_id is not None:
        stmt = stmt.where(AiConversation.project_id == project_id)
    if task_id is not None:
        stmt = stmt.where(AiConversation.task_id == task_id)
    if archived == "false":
        stmt = stmt.where(AiConversation.archived_at.is_(None))
    elif archived == "true":
        stmt = stmt.where(AiConversation.archived_at.is_not(None))
    stmt = stmt.order_by(AiConversation.last_activity_at.desc(), AiConversation.id).limit(min(max(limit, 1), 200))
    convs = list(db.scalars(stmt).all())
    ids = [c.id for c in convs]
    if not ids:
        return []
    touched: set[uuid.UUID] = set()
    for op in db.scalars(select(AiOperation).where(AiOperation.conversation_id.in_(ids), AiOperation.status.in_(ACTIVE))
                         .order_by(AiOperation.id)).all():
        sync_operation(db, op, defer=touched)
    _touch_conversations(db, touched)
    msg_counts = dict(db.execute(select(AiMessage.conversation_id, func.count()).where(AiMessage.conversation_id.in_(ids))
                                 .group_by(AiMessage.conversation_id)).all())
    active = dict(db.execute(select(AiOperation.conversation_id, func.count())
                             .where(AiOperation.conversation_id.in_(ids), AiOperation.status.in_(ACTIVE))
                             .group_by(AiOperation.conversation_id)).all())
    last_status: dict[uuid.UUID, str] = {}
    for cid, st in db.execute(select(AiOperation.conversation_id, AiOperation.status).where(AiOperation.conversation_id.in_(ids))
                              .order_by(AiOperation.conversation_id, AiOperation.created_at.desc(), AiOperation.id.desc())).all():
        last_status.setdefault(cid, st)
    projects = {p.id: p for p in db.scalars(select(Project).where(Project.id.in_({c.project_id for c in convs}))).all()}
    task_ids = {c.task_id for c in convs if c.task_id}
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(task_ids))).all()} if task_ids else {}
    convs.sort(key=lambda c: c.last_activity_at, reverse=True)
    out = []
    for c in convs:
        p, t = projects.get(c.project_id), tasks.get(c.task_id)
        out.append({
            "id": str(c.id),
            "title": c.title,
            "project": {"id": str(p.id), "code": p.code, "name": p.name} if p else None,
            "task": {"id": str(t.id), "title": t.title} if t else None,
            "archived": c.archived_at is not None,
            "last_activity_at": _iso(c.last_activity_at),
            "message_count": int(msg_counts.get(c.id, 0)),
            "active_operations": int(active.get(c.id, 0)),
            "last_status": last_status.get(c.id),
        })
    return out


def sync_conversation(db: Session, conv: AiConversation) -> None:
    touched: set[uuid.UUID] = set()
    for op in db.scalars(select(AiOperation).where(AiOperation.conversation_id == conv.id, AiOperation.status.in_(ACTIVE))
                         .order_by(AiOperation.id)).all():
        sync_operation(db, op, defer=touched)
    _touch_conversations(db, touched)


def _append(db: Session, conv: AiConversation, *, role: str, kind: str, text: str = "", target: dict | None = None,
            references: list[dict] | None = None, operation_id: uuid.UUID | None = None, payload: dict | None = None) -> AiMessage:
    """Next message of a locked conversation (seq from next_seq)."""
    seq = conv.next_seq
    conv.next_seq = seq + 1
    m = AiMessage(conversation_id=conv.id, seq=seq, role=role, kind=kind, text=text, target=target, references=list(references or []),
                  operation_id=operation_id, payload=payload or {})
    db.add(m)
    db.flush()
    return m


def _normalize_constraints(items: list[dict], existing: list[dict]) -> list[dict]:
    if len(items) > MAX_CONSTRAINTS:
        raise AppError(422, "too_many_constraints", f"a conversation keeps at most {MAX_CONSTRAINTS} constraints", {"max": MAX_CONSTRAINTS})
    old = {c.get("id"): c for c in existing or []}
    out: list[dict] = []
    seen: set[str] = set()
    for c in items:
        text = (c.get("text") or "").strip()
        if not text:
            raise AppError(422, "constraint_empty", "a constraint needs text")
        cid = c.get("id") or uuid.uuid4().hex[:12]
        if cid in seen:
            continue
        seen.add(cid)
        prev = old.get(cid)
        out.append({"id": cid, "text": text[:500], "source_seq": c.get("source_seq"),
                    "created_at": prev.get("created_at") if prev else _now().isoformat()})
    return out


def patch_conversation(db: Session, user: User, conversation_id: uuid.UUID, *, title: Any = UNSET, task_id: Any = UNSET,
                       archived: Any = UNSET, constraints: Any = UNSET, autonomy: Any = UNSET,
                       expected_version: int | None = None) -> AiConversation:
    """Rename, rebind the task (a notice message; only later requests use it), archive / restore,
    replace constraints, set the main agent's autonomy (AI native; a running turn reads it at its next write tool).
    An archived conversation only accepts archived=false (with other fields)."""
    from ..models import AI_AUTONOMY

    conv, _ = load_conversation(db, user, conversation_id)
    conv = lock_conversation(db, conv.id)
    _check_version(conv, expected_version)
    if task_id is not UNSET and task_id is not None:
        _check_task_in_project(db, task_id, conv.project_id)
        features.require_enabled("tasks")
    if conv.archived_at is not None and archived is not False:
        raise conflict("conversation_archived", "the conversation is archived; restore it (archived=false) before changing it",
                       conversation_id=str(conv.id))
    changed = False
    if archived is not UNSET and archived is not None:
        if archived and conv.archived_at is None:
            conv.archived_at, changed = _now(), True  # running operations keep running (design §4.5)
        elif not archived and conv.archived_at is not None:
            conv.archived_at, changed = None, True
    if title is not UNSET and title is not None:
        new = title.strip()[:200]
        if new != conv.title:
            conv.title, changed = new, True
    if task_id is not UNSET and task_id != conv.task_id:
        if task_id is not None:
            _check_task_in_project(db, task_id, conv.project_id)
        previous = conv.task_id
        conv.task_id = task_id
        _append(db, conv, role="assistant", kind="notice",
                payload={"code": "task_changed", "task_id": str(task_id) if task_id else None,
                         "previous_task_id": str(previous) if previous else None})
        conv.last_activity_at = _now()
        changed = True
    if constraints is not UNSET and constraints is not None:
        new_c = _normalize_constraints(constraints, conv.constraints or [])
        if new_c != (conv.constraints or []):
            conv.constraints, changed = new_c, True
    if autonomy is not UNSET and autonomy is not None and autonomy != conv.autonomy:
        if autonomy not in AI_AUTONOMY:
            raise AppError(422, "invalid_autonomy", f"autonomy is one of {', '.join(AI_AUTONOMY)}", {"expected": list(AI_AUTONOMY)})
        conv.autonomy, changed = autonomy, True
    if changed:
        conv.version += 1
    db.flush()
    return conv


# --- composer ---------------------------------------------------------------------------


def _composer(conv: AiConversation) -> tuple[str | None, list[dict]]:
    comp = conv.composer or {}
    return comp.get("target_key"), list(comp.get("references") or [])


def _set_composer(conv: AiConversation, target_key: str | None, items: list[dict]) -> None:
    conv.composer = {"target_key": target_key, "references": items}  # new object: JSONB change detection


def add_reference(db: Session, user: User, conversation_id: uuid.UUID, ref_in: refs.AiRefInput, *, as_target: bool) -> tuple[AiConversation, dict, str | None]:
    """Normalise, validate and add (dedupe by key). Without a target, the first operable object is
    preselected (notice target_preselected); an existing target is kept unless as_target."""
    conv, _ = load_conversation(db, user, conversation_id)
    require_open(conv)
    ref = refs.normalize_ref(db, user, conv.project_id, ref_in)
    conv = lock_conversation(db, conv.id)
    require_open(conv)
    target_key, items = _composer(conv)
    existing = next((r for r in items if r.get("key") == ref["key"]), None)
    notice: str | None = None
    if existing is not None:
        ref = existing
        notice = "already_added"
    else:
        if len(items) >= MAX_REFERENCES:
            raise AppError(422, "too_many_references", f"a conversation holds at most {MAX_REFERENCES} references; remove some first",
                           {"max": MAX_REFERENCES})
        items.append(ref)
    if as_target:
        if target_key != ref["key"]:
            target_key, notice = ref["key"], "target_set"
    elif existing is None:
        if target_key is None and ref["kind"] in refs.OPERABLE_KINDS:
            target_key, notice = ref["key"], "target_preselected"
        elif target_key is not None:
            notice = "target_kept"
    _set_composer(conv, target_key, items)
    db.flush()
    return conv, ref, notice


def remove_reference(db: Session, user: User, conversation_id: uuid.UUID, key: str) -> AiConversation:
    conv, _ = load_conversation(db, user, conversation_id)
    conv = lock_conversation(db, conv.id)
    require_open(conv)
    target_key, items = _composer(conv)
    items = [r for r in items if r.get("key") != key]
    _set_composer(conv, None if target_key == key else target_key, items)
    db.flush()
    return conv


def set_target(db: Session, user: User, conversation_id: uuid.UUID, key: str | None) -> AiConversation:
    conv, _ = load_conversation(db, user, conversation_id)
    conv = lock_conversation(db, conv.id)
    require_open(conv)
    target_key, items = _composer(conv)
    if key is not None and not any(r.get("key") == key for r in items):
        raise AppError(422, "ref_not_in_composer", "add the reference to the conversation first", {"key": key})
    _set_composer(conv, key, items)
    db.flush()
    return conv


# --- source binding and drawing intent --------------------------------------------------

_Q_ZH = ("为什么", "解释", "说明", "多少")
_Q_EN = re.compile(r"\b(what|why|how|explain)\b", re.IGNORECASE)
_CREATE = re.compile(r"创建|新建|建模|画|\b(create|make|design|model)", re.IGNORECASE)
_DRAWING = re.compile(r"出图|图纸|工程图|二维图|\b(drawings?|dwg|dxf)\b", re.IGNORECASE)
_GEN_DRAWING = re.compile(r"出图|(?:生成|创建|新建|输出|导出|绘制|画|出|做)[^，。,.;；!?！？]{0,20}(?:图纸|工程图|二维图)|"
                          r"\b(generate|create|make|produce|output|export|draw)\b[^.;!?]{0,40}\bdrawings?\b", re.IGNORECASE)
_MODIFY = re.compile(r"改|调整|移动|移|增加|添加|删除|去掉|\b(change|modify|move|add|remove|delete|resize|enlarge|widen|deepen)\b",
                     re.IGNORECASE)
# Geometry stated in a request: sizes ("100x60x10", "Ø20", "12 mm", "M8").
_GEOMETRY = re.compile(r"\d\s*(?:mm)?\s*[x×X*]\s*\d|[Øø⌀φΦ]\s*\d|\d(?:\.\d+)?\s*mm\b|\bM\d", re.IGNORECASE)
_DRAW_A_DRAWING = re.compile(r"画\s*(?:一)?\s*(?:张|份|套|个)?\s*(?:图纸|工程图|二维图|图)")


def has_modeling_intent(text: str) -> bool:
    """Whether a drawing request also asks to model / change geometry (create / modify words or stated sizes once the
    drawing phrases are taken out): "model and draw a 100x60x10 plate" does, "produce engineering drawings" / "generate the drawing" do not."""
    rest = _DRAW_A_DRAWING.sub(" ", _GEN_DRAWING.sub(" ", text or ""))
    rest = _DRAWING.sub(" ", rest)
    return bool(_CREATE.search(rest) or _MODIFY.search(rest) or _GEOMETRY.search(rest))


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    return t.endswith(("?", "？")) or any(w in t for w in _Q_ZH) or bool(_Q_EN.search(t))


#: W8: an assembly is asked for ("create a static assembly", "create an assembly"). "Parts / components" names no
#: assembly (review of generation reliability K: "generate an engineering drawing for this part" is a part drawing).
_ASSEMBLY = re.compile(r"装配|总装|(?<!零)部件|\b(assembly|assemblies|sub-?assembly)\b", re.IGNORECASE)
#: Generation reliability K (AC01): the whole product asked for — complete set / full set / general assembly / whole machine — or an assembly with both
#: its model and its drawings ("generate an assembly model and drawings").
_WHOLE_SET = re.compile(r"整套|全套|总装|整机|\b(?:whole|complete|full|entire)\s+(?:assembly|set|machine|product)\b", re.IGNORECASE)
_ASM_WORD = re.compile(r"装配|\bassembl(?:y|ies)\b", re.IGNORECASE)
_MODEL_WORD = re.compile(r"模型|\bmodels?\b", re.IGNORECASE)
_DRAWING_WORD = re.compile(r"图|\bdrawings?\b", re.IGNORECASE)


def _assembly_source(target: dict | None, references: list[dict]) -> bool:
    """Material an assembly can be built from: a referenced spec version or a text / document file (W8; an uploaded
    Markdown / text file keeps file kind "other", so its name counts too — generation supplement A-01)."""
    items = ([target] if target else []) + list(references)
    return any(r.get("kind") == "engineering_spec_version" or _text_document(r) for r in items if isinstance(r, dict))


def _whole_assembly(text: str, references: list[dict]) -> bool:
    """Whether source binding should look for whole-assembly material before tool selection.

    Assembly wording alone is not enough ("draw the bracket; it is part of the pump assembly" describes a part drawing). The request
    must ask for the whole set or reference a document/spec version. Existing part, STEP and drawing references
    stay the request's sources without looking for another upload. This check never chooses an operation kind.
    """
    t = text or ""
    if not _ASSEMBLY.search(t) or _is_question(t) or not (_ASM_GENERATE.search(t) or _GEN_DRAWING.search(t)):
        return False
    if _first_part_ref(None, references) is not None:
        return False
    for r in references:
        if not isinstance(r, dict):
            continue
        if r.get("kind") in ("assembly_revision", "part_revision", "cad_selection") or refs.is_step_file(r) \
                or refs.cad_drawing_format(r) is not None or refs.is_drawing_asset(r) \
                or (r.get("kind") == "file_version" and r.get("file_kind") in refs.DRAWING_FILE_KINDS):
            return False
    whole = bool(_WHOLE_SET.search(t) or (_ASM_WORD.search(t) and _MODEL_WORD.search(t) and _DRAWING_WORD.search(t)))
    return whole or _assembly_source(None, references)


# --- capabilities -----------------------------------------------------------------------


def capabilities(db: Session, user: User, project_id: uuid.UUID) -> dict:
    """``simulation``: some operation would be answered by the labelled simulated model (simulation is on and its
    purpose has no usable config); ``operations[kind].simulated`` says which."""
    from . import orchestrator

    acc = project_access(db, user, project_id, "view")
    status = _ai_status(db)
    return {"operations": _availability(acc, status), "simulation": any(v["simulated"] for v in status.values()),
            "ready_configs": int(status["model_create"]["ready"]), "orchestrator": orchestrator.available(db),
            "autonomy_default": orchestrator.default_autonomy(db, user)}  # what a new conversation starts with


def _ai_status(db: Session) -> dict[str, dict[str, bool]]:
    """Per operation kind: ``ready`` = an enabled config is usable for its purpose; ``simulated`` = no such
    config and simulation is on (the labelled simulated model answers)."""
    from ..ai.providers.base import ProviderError
    from ..config import get_settings
    from . import ai as ai_svc

    sim = bool(get_settings().ai_simulation)
    ready = {}
    for purpose in set(OP_PURPOSE.values()):
        try:
            ai_svc._choose(db, purpose, None)
            ready[purpose] = True
        except ProviderError:
            ready[purpose] = False
    return {k: {"ready": ready[p], "simulated": sim and not ready[p]} for k, p in OP_PURPOSE.items()}


def _availability(acc: ProjectAccess, status: dict[str, dict[str, bool]]) -> dict[str, dict]:
    p = acc.project
    active = p.archived_at is None and p.status != "archived" and p.lifecycle == "active"
    out = {}
    for kind in OPERATION_KINDS:
        st = status.get(kind) or {"ready": False, "simulated": False}
        reason = None
        if not acc.can(OP_ACTION[kind]):
            reason = "no_permission"
        elif kind == "task_draft" and not features.enabled("tasks"):
            reason = "feature_disabled"
        elif kind in RUN_KINDS and not active:
            reason = "project_inactive"
        elif kind == "drawing_reconstruct" and not st["ready"]:
            # The simulated model cannot read drawings (services/ai_reconstruct.py): a real config is required.
            reason = "simulation_unsupported" if st["simulated"] else "ai_unavailable"
        elif kind not in ("drawing_intake", "drawing_generate") and not (st["ready"] or st["simulated"]):
            # intake without a vision model waits for manual entry; a drawing of a revision / STEP file needs no AI
            reason = "ai_unavailable"
        out[kind] = {"available": reason is None, "reason": reason, "simulated": st["simulated"]}
    return out


# --- send -------------------------------------------------------------------------------


def _title_from(text: str) -> str:
    first = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    return first[:40]


def _existing(db: Session, conv: AiConversation, crid: uuid.UUID) -> tuple[AiMessage, AiOperation | None, AiMessage | None] | None:
    """The stored outcome of an earlier request with the same client_request_id (idempotency)."""
    op = db.scalar(select(AiOperation).where(AiOperation.conversation_id == conv.id, AiOperation.client_request_id == crid))
    if op is not None:
        msg = db.get(AiMessage, op.message_id)
        asst = db.scalar(select(AiMessage).where(AiMessage.conversation_id == conv.id, AiMessage.operation_id == op.id,
                                                 AiMessage.role == "assistant").order_by(AiMessage.seq.desc()).limit(1))
        assert msg is not None
        return msg, op, asst
    msg = db.scalar(select(AiMessage).where(AiMessage.conversation_id == conv.id, AiMessage.role == "user",
                                            AiMessage.payload["client_request_id"].astext == str(crid)).limit(1))
    if msg is None:
        return None
    asst = db.scalar(select(AiMessage).where(AiMessage.conversation_id == conv.id, AiMessage.seq == msg.seq + 1, AiMessage.role == "assistant"))
    return msg, None, asst


def send_out(db: Session, msg: AiMessage, op: AiOperation | None, asst: AiMessage | None) -> dict:
    statuses = refs.ref_statuses(db, _refs_of_message(msg))
    return {"message": message_out(msg, statuses), "operation": operation_out(db, op) if op else None,
            "assistant_message": message_out(asst, statuses) if asst else None}


def _check_target_available(db: Session, target: dict | None) -> None:
    if target is None:
        return
    st = refs.ref_statuses(db, [target]).get(target["key"]) or {}
    if st.get("state") == "unavailable":
        raise conflict("target_unavailable", "the operation target is no longer available; choose another target",
                       key=target["key"], reason=st.get("reason"))


def _check_workflow_versions(db: Session, references: list[dict | None]) -> None:
    """Mutable workflow rows cannot supply a newer graph for an exact request reference."""
    for ref in references:
        if not isinstance(ref, dict) or ref.get("kind") != "workflow" or not isinstance(ref.get("version"), int):
            continue
        workflow = db.get(Workflow, _uuid(ref.get("object_id")), populate_existing=True)
        if workflow is not None and workflow.version != ref["version"]:
            raise conflict("ref_version_changed", "the referenced workflow changed; refresh its context before sending",
                           key=ref.get("key"), expected_version=ref["version"], current_version=workflow.version)


#: Source binding recognizes generation wording; an assembly drawing alone does not request new sources.
_ASM_GENERATE = re.compile(r"生成|搭建|\b(?:generate|build)\b", re.IGNORECASE)
_ASM_DRAWING = re.compile(r"(?:装配|总装)图|\bassembly\s+drawings?\b", re.IGNORECASE)
#: Uploaded text / Markdown files keep file kind "other" (only materials are "document"): a text file by its name.
_TEXT_FILE_EXTS = (".md", ".markdown", ".txt", ".text")


def _text_document(ref: dict | None) -> bool:
    return bool(ref) and ref.get("kind") == "file_version" and (  # type: ignore[union-attr]
        ref.get("file_kind") == "document" or refs.file_name_of(ref).lower().endswith(_TEXT_FILE_EXTS))  # type: ignore[union-attr]


def _generates_assembly(text: str, target: dict | None, references: list[dict]) -> bool:
    """An assembly asked for from a text document (an uploaded Markdown, as kind "other", too) or a spec version that
    needs source validation (A-01: "generate an assembly from the Markdown just uploaded" with the bound file)."""
    t = text or ""
    if not _ASSEMBLY.search(t) or _ASM_DRAWING.search(t) or _is_question(t) or not (_CREATE.search(t) or _ASM_GENERATE.search(t)):
        return False
    if target is not None and not _text_document(target):
        return False
    return any(_text_document(r) or r.get("kind") == "engineering_spec_version" for r in [target, *references] if isinstance(r, dict))


#: Objects that are not a file: wording such as "this drawing / this file" in a request on one of them refers to it, not to an upload.
_NOT_FILE_TARGET = ("file_version",)


def _binds_attachments(operation: str, text: str, target: dict | None, references: list[dict]) -> bool:
    """Generation supplement A-01 applies only to a request that builds from source files: ``assembly_create``, or an
    ``auto`` request asking for a new assembly. A question, a request on another object (a part, a drawing, an
    assembly) and a request on a referenced spec version (it is the source) never get a file bound."""
    t = text or ""
    if _is_question(t):
        return False
    if target is not None and target.get("kind") not in _NOT_FILE_TARGET:
        return False
    if any(isinstance(r, dict) and r.get("kind") == "engineering_spec_version" for r in references):
        return False
    if operation == "assembly_create":
        return True
    if operation == "auto" and target is None and _whole_assembly(t, references):
        return True  # bind the requested whole-assembly sources before the main Agent chooses a tool
    return operation == "auto" and bool(_ASSEMBLY.search(t)) and not _ASM_DRAWING.search(t) and bool(_CREATE.search(t) or _ASM_GENERATE.search(t))


def _bind_attachment(db: Session, user: User, conv: AiConversation, text: str, target: dict | None, references: list[dict], *,
                     search: bool) -> tuple[list[dict], list[dict]]:
    """Generation supplement A-01 (design §2.5, services/attachments.py): (auto_references, references with the bound
    file). A unique candidate is added to the composer too (visible, removable); 422 attachment_ambiguous /
    attachment_required otherwise."""
    from . import attachments

    tkey, items = _composer(conv)
    b = attachments.find_binding(db, user, conv, text, [target, *references], composer_refs=items, search=search)
    if b.status == "not_needed":
        return [], references
    cands = [attachments.candidate_ref(db, user, conv.project_id, c) for c in b.candidates]
    if b.status != "unique":
        raise attachments.binding_error(b, cands)
    ref = {k: v for k, v in cands[0].items() if k != "binding_reason"}
    if ref["key"] not in {r.get("key") for r in items} and len(items) < MAX_REFERENCES:
        _set_composer(conv, tkey, [*items, ref])
    return [attachments.auto_reference(ref, b.candidates[0])], [*references, ref]


def resolve_attachments(db: Session, user: User, conversation_id: uuid.UUID, *, text: str, target_key: Any = UNSET,
                        reference_keys: Any = UNSET, operation: str = "auto") -> dict:
    """POST /ai/conversations/{id}/attachments/resolve (generation supplement A-01): what a send of this text would bind
    now — ``status`` not_needed | unique | ambiguous | required with the candidates as references the composer can add.
    Read only (the composer adds a unique candidate as a visible reference; a send with ``auto_references=false`` then
    keeps what the person left)."""
    from . import attachments

    conv, _ = load_conversation(db, user, conversation_id)
    tkey, items = _composer(conv)
    by_key = {r["key"]: r for r in items}
    if target_key is not UNSET:
        tkey = target_key
    keys = [k for k in by_key if k != tkey] if reference_keys is UNSET else [k for k in reference_keys or [] if k in by_key and k != tkey]
    refs_ = [by_key[tkey]] if tkey in by_key else []
    refs_ += [by_key[k] for k in keys]
    if not _binds_attachments(operation, text, refs_[0] if tkey in by_key else None, refs_[1:] if tkey in by_key else refs_):
        return attachments.binding_out(db, user, conv.project_id, attachments.Binding())
    b = attachments.find_binding(db, user, conv, text, refs_, composer_refs=items)
    return attachments.binding_out(db, user, conv.project_id, b)


def send(db: Session, user: User, conversation_id: uuid.UUID, *, client_request_id: uuid.UUID, text: str, operation: str,
         target_key: Any = UNSET, reference_keys: Any = UNSET, model_config_id: uuid.UUID | None = None,
         task_id: Any = UNSET, execution_policy: str | None = None, as_material: bool = False,
         auto_references: bool = True, revise: bool | None = None, reuse_earlier_builds: bool | None = None) -> tuple[dict, bool]:
    """POST /ai/conversations/{id}/messages. Returns (AiSendResult, created). Commits.

    ``as_material`` (S2): the person agreed that a text over the chat limit is saved as a project material; without it
    such a text is 422 ``material_consent_required`` (nothing is saved).

    ``auto_references`` (generation supplement A-01, design §2.5; services/attachments.py): a request whose text clearly
    points at an attachment while neither its target nor its references hold a file version gets the file it means — one
    candidate is added to the request and the composer and recorded as ``auto_references`` (snapshot and message);
    several are 422 ``attachment_ambiguous`` with the candidates; none is 422 ``attachment_required``. False: the person
    set the references (the composer resolved them while typing), nothing is added; the wording without any file is still
    ``attachment_required``. An assembly request that references a spec version together with new files or change
    wording is 422 ``spec_and_sources`` (A-04) unless ``revise`` says what to do (generation supplement I, IN-07): true makes
    a new version of that spec with the new material and the text as a revision note, false builds from the exact
    version and drops the new material visibly (``spec_choice`` in the snapshot). Nothing is written before these checks.

    ``reuse_earlier_builds`` is the person's explicit assembly build choice. False forbids reuse across requests;
    completed work within the same run is still resumable. None leaves the tool's choice or the historical default.

    A fresh request resolves current project defaults unless an authorized explicit policy is supplied. The policy,
    project version, model bundle and account preferences are frozen before execution and inherited by continuations.
    An explicit policy is part of the idempotency
    fingerprint: the same client_request_id with another policy is 409 ``idempotency_conflict``.

    Generation reliability K (design §2.3): the checks up to the policy are :func:`_prepare`, shared with
    :func:`preflight`; the target checks of the operation (``part_required`` / ``target_required``) are
    :func:`target_plan`, shared with the business step.

    AI native (services/orchestrator.py): 409 ``agent_busy`` while a main-agent turn of the conversation is pending /
    running (a turn waiting for a confirmation is superseded instead); an ``auto`` request becomes a
    main-agent turn (an ``ask`` operation with ``runtime = orchestrator``) that the assistant worker runs."""
    from . import orchestrator, ai as ai_svc, preferences

    _check_policy_value(execution_policy)
    conv, acc = load_conversation(db, user, conversation_id)
    conv = lock_conversation(db, conv.id)  # serialises sends of one conversation (idempotency, seq)
    found = _existing(db, conv, client_request_id)
    if found is not None:
        if execution_policy:
            _check_same_policy(found, execution_policy, client_request_id)
        if reuse_earlier_builds is not None and (found[1] is None or
                                                found[1].request.get("reuse_earlier_builds") is not reuse_earlier_builds):
            raise conflict("idempotency_conflict", "this request was already sent with a different model reuse choice")
        db.commit()
        return send_out(db, *found), False
    require_open(conv)
    features.require_choices(model_config_id=model_config_id, execution_policy=execution_policy)
    # Check the frozen boundary before attachments can create a blob or a superseded turn is closed.
    if not features.enabled("tasks"):
        preview = _prepare(db, user, conv, acc, _Checks(collect=False), text=text, operation=operation,
                           target_key=target_key, reference_keys=reference_keys, task_id=task_id,
                           execution_policy=execution_policy, as_material=as_material,
                           auto_references=auto_references, revise=revise, write=False)
        if preview.kind == "task_draft" and not orchestrator.routed(operation):
            project_access(db, user, conv.project_id, OP_ACTION[preview.kind])
            features.require_enabled("tasks")
    orchestrator.before_send(db, conv)
    p = _prepare(db, user, conv, acc, _Checks(collect=False), text=text, operation=operation, target_key=target_key,
                 reference_keys=reference_keys, task_id=task_id, execution_policy=execution_policy, as_material=as_material,
                 auto_references=auto_references, revise=revise, write=True)
    turn = orchestrator.routed(operation)
    text, target, references, tid, choice = p.text, p.target, p.references, p.tid, p.choice
    kind = "ask" if turn else p.kind
    policy = choice.policy
    snapshot = {
        "text": text,
        "operation": kind,
        "requested_operation": operation,
        "target": target,
        "references": references,
        # The person's revision choice also belongs to main-agent requests whose tool is chosen later.
        # No model-facing child input may manufacture or erase this authorization.
        "revise": revise,
        "reuse_earlier_builds": reuse_earlier_builds,
        "constraints": [{"id": c.get("id"), "text": c.get("text")} for c in conv.constraints or []],
        "task_id": str(tid) if tid else None,
        "model_config_id": str(model_config_id) if model_config_id else None,
        "client_request_id": str(client_request_id),
        "execution_policy": policy,
        # S5: where the frozen policy came from and the version the request continues from.
        "policy_source": choice.source,
        "policy_source_version": choice.source_version,
        "project_version": choice.project_version,
        "requested_execution_policy": execution_policy or None,
        "preferences_snapshot": preferences.freeze(db, user.id),
        "team_snapshot": _freeze_team(db, conv.project_id),
        "model_snapshots": ai_svc.freeze_model_bundle(db, preferred={OP_PURPOSE.get(kind, "assistant"): model_config_id}
                                                     if model_config_id else None),
        "read_only": operation == "ask" or (operation == "auto" and not turn),
    }
    if p.materials:
        # S2: what the context build reads in full / as a digest / in chunks (or the spec step, for assembly_create).
        snapshot["materials"] = p.materials
    if p.auto_refs:
        # Generation supplement A-01: the files the server bound (the card shows "Automatically referenced: X v1 (reason)").
        snapshot["auto_references"] = p.auto_refs
    if p.spec_choice is not None:
        # Generation supplement I (A-04 / IN-07): the explicit choice next to a spec version and the material it adds / drops.
        snapshot["revise"] = bool(revise)
        snapshot["spec_choice"] = p.spec_choice
    # W1: the exact configuration (or the labelled simulated model) is frozen before any context build.
    entry = snapshot["model_snapshots"]["entries"].get(OP_PURPOSE.get(kind, "assistant"), {})
    snapshot["model_snapshot"] = entry.get("snapshot")
    if turn:
        snapshot = orchestrator.as_turn(snapshot)
    payload = {"client_request_id": str(client_request_id), "requested_operation": operation, "operation": kind,
               "task_id": snapshot["task_id"], "model_config_id": snapshot["model_config_id"], "execution_policy": policy,
               "policy_source": choice.source, "preferences_snapshot": snapshot["preferences_snapshot"]}
    if p.material is not None:
        # S2: the conversation shows that this request's text was saved as a material.
        payload["material"] = next(m for m in p.materials if m["new"])
    if p.auto_refs:
        payload["auto_references"] = p.auto_refs
    msg = _append(db, conv, role="user", kind="request", text=text, target=target, references=references, payload=payload)
    if not conv.title:
        conv.title = _title_from(text)
    conv.last_activity_at = _now()
    op = _create_operation(db, user, conv, msg, kind=kind, request=snapshot, task_id=tid, client_request_id=client_request_id)
    orchestrator.root_turn(op)
    db.commit()  # transaction 1: the request is on record before any business work starts
    return _execute_and_out(db, user, op.id, msg.id), True


def _check_policy_value(execution_policy: str | None) -> None:
    from ..policy import parse_policy

    if execution_policy not in (None, ""):
        try:
            parse_policy(execution_policy)
        except ValueError:
            raise bad_request("invalid_execution_policy", f"unknown execution_policy {execution_policy!r}")


# --- request checks shared by send and preflight (generation reliability K, design §2.3) ------------------------------


class _Checks:
    """Send raises the first problem (nothing written); preflight collects every problem as a ``missing`` entry."""

    def __init__(self, *, collect: bool) -> None:
        self.collect = collect
        self.missing: list[dict] = []

    def fail(self, exc: AppError) -> None:
        if not self.collect:
            raise exc
        self.missing.append(missing_of(exc))


@dataclass
class _Prepared:
    text: str
    target: dict | None
    references: list[dict]
    tid: Any
    kind: str = "ask"
    choice: Any = None  # policy.PolicyChoice
    auto_refs: list[dict] = field(default_factory=list)
    material: dict | None = None
    materials: list[dict] = field(default_factory=list)
    spec_choice: dict | None = None


def _request_refs(conv: AiConversation, target_key: Any, reference_keys: Any) -> tuple[dict | None, list[dict], list[dict]]:
    """(target, references, composer items) of a request: omitted keys = the composer's target / every composer
    reference; 422 ref_not_in_composer for keys the conversation does not hold."""
    tkey, items = _composer(conv)
    by_key = {r["key"]: r for r in items}
    if target_key is not UNSET:
        tkey = target_key
    if tkey is not None and tkey not in by_key:
        raise AppError(422, "ref_not_in_composer", "the target is not in this conversation's references", {"key": tkey})
    if reference_keys is UNSET:
        rkeys = [k for k in by_key if k != tkey]
    else:
        rkeys = [k for k in dict.fromkeys(reference_keys or []) if k != tkey]
        missing = [k for k in rkeys if k not in by_key]
        if missing:
            raise AppError(422, "ref_not_in_composer", "some references are not in this conversation", {"key": missing[0], "keys": missing})
    return (by_key[tkey] if tkey else None), [by_key[k] for k in rkeys], items


def _prepare(db: Session, user: User, conv: AiConversation, acc: ProjectAccess, chk: _Checks, *, text: str, operation: str,
             target_key: Any, reference_keys: Any, task_id: Any, execution_policy: str | None, as_material: bool,
             auto_references: bool, revise: bool | None, write: bool) -> _Prepared:
    """The checks of a request up to its frozen policy, in send's order: text, references, task, target available,
    operation, attachment binding (A-01), material (S2), spec and sources (A-04), policy (S5).
    ``write`` (send): a bound file is added to the composer and an over-long text is saved as a material; preflight
    writes nothing (a bound file is listed as an automatic reference, the material as the reference it would become)."""
    text = (text or "").strip()
    if not text:
        raise AppError(422, "text_required", "write what you need")
    target, references, items = _request_refs(conv, target_key, reference_keys)
    tid = conv.task_id if task_id is UNSET else task_id
    if tid is not None and tid != conv.task_id:
        _check_task_in_project(db, tid, conv.project_id)
    if tid is not None:
        features.require_enabled("tasks")
    try:
        _check_target_available(db, target)
        _check_workflow_versions(db, [target, *references])
    except AppError as exc:
        chk.fail(exc)
    if operation not in ("auto", *OPERATION_KINDS):
        raise bad_request("invalid_operation", f"unknown operation {operation!r}")
    if operation == "task_draft" and not features.enabled("tasks"):
        # Enabled requests keep the existing preflight diagnostic / recorded failure path.
        project_access(db, user, conv.project_id, OP_ACTION[operation])
        features.require_enabled("tasks")
    out = _Prepared(text=text, target=target, references=references, tid=tid)
    # Generation supplement A-01: the file a request clearly points at becomes a visible reference (or 422). A text over
    # the chat limit is itself saved as a file (the material below): nothing to bind.
    if not materials_svc.needs_material(text) and _binds_attachments(operation, text, target, references):
        try:
            if write:
                out.auto_refs, out.references = _bind_attachment(db, user, conv, text, target, references, search=auto_references)
                _, items = _composer(conv)
            else:
                out.auto_refs, out.references = _preview_binding(db, user, conv, text, target, references, search=auto_references)
        except AppError as exc:
            chk.fail(exc)
    # S2: a request longer than the chat limit is not refused: it becomes a project material that the request (and
    # the composer, for follow-ups) references; the request text keeps its beginning and end and says where the rest is.
    # A material saved by an earlier request and still referenced is read the same way (flag ``material`` on the ref).
    if materials_svc.needs_material(text):
        if not as_material:
            chk.fail(AppError(422, "material_consent_required",
                              f"a request longer than {limits.CHAT_TEXT_CHARS} characters is saved as a project material that project "
                              "members can see; send it with as_material=true to agree, or shorten it",
                              {"max": limits.CHAT_TEXT_CHARS, "length": len(text)}))
        elif write:
            material = materials_svc.save_material(db, user, acc, conv, text)
            out.material = material
            if material["key"] not in {r.get("key") for r in out.references}:
                out.references = [*out.references, material["ref"]]
            if material["key"] not in {r.get("key") for r in items} and len(items) < MAX_REFERENCES:
                _set_composer(conv, (conv.composer or {}).get("target_key"), [*items, material["ref"]])
            out.text = materials_svc.request_text(text, material)
        elif not acc.can("design"):
            chk.fail(AppError(403, "material_not_allowed", "a request this long is saved as a project material, which needs the "
                              "permission to add project files; shorten it or ask a project designer"))
        else:
            name = f"{materials_svc._title(text)}.md"  # the name save_material gives it
            out.references = [*out.references, {"key": "material:new", "kind": "file_version", "file_kind": "document", "object_id": None,
                                                "title": name, "file_name": name, "version_label": "v1", "material": True,
                                                "chars": len(text)}]
            # Use the same shortened text as send (save_material's name and length).
            out.text = materials_svc.request_text(text, {"name": name, "chars": len(text)})
    out.materials = [materials_svc.snapshot_entry(r, new=out.material is not None and r.get("key") == out.material["key"])
                     for r in out.references if isinstance(r, dict) and r.get("material") and r.get("object_id")]
    out.kind = "ask" if operation == "auto" else operation
    # Source checks do not choose an execution kind. Preserve the person's revise/keep gate and source provenance
    # before the main Agent selects its tools, including requests with a newly bound attachment or material.
    assembly_sources = operation == "assembly_create" or (operation == "auto" and (
        _generates_assembly(out.text, target, out.references)
        or ((target is None or _text_document(target)) and _whole_assembly(out.text, out.references))))
    if assembly_sources:
        from .assembly_ai import request_spec_sources

        # A-04: 422 spec_and_sources before anything is written, unless the person chose (revise true / false). In a
        # preflight the material a send would save is new material too (it has no file version yet: its key stands in).
        srcs = [{**r, "object_id": r["key"]} if r.get("key") == "material:new" and not r.get("object_id") else r
                for r in out.references if isinstance(r, dict)]
        try:
            out.spec_choice = request_spec_sources(db, target, srcs, out.text, revise=revise).choice
        except AppError as exc:
            chk.fail(exc)
    out.choice = policy_svc.resolve(db, conv.project_id, execution_policy,
                                    source=policy_svc.request_source(db, conv.project_id,
                                                                  "assembly_create" if assembly_sources else out.kind,
                                                                  target, out.references))
    return out


def _preview_binding(db: Session, user: User, conv: AiConversation, text: str, target: dict | None, references: list[dict], *,
                     search: bool) -> tuple[list[dict], list[dict]]:
    """:func:`_bind_attachment` without writing: (auto_references, references with the file a send would bind)."""
    from . import attachments

    _, items = _composer(conv)
    b = attachments.find_binding(db, user, conv, text, [target, *references], composer_refs=items, search=search)
    if b.status == "not_needed":
        return [], references
    cands = [attachments.candidate_ref(db, user, conv.project_id, c) for c in b.candidates]
    if b.status != "unique":
        raise attachments.binding_error(b, cands)
    ref = {k: v for k, v in cands[0].items() if k != "binding_reason"}
    return [attachments.auto_reference(ref, b.candidates[0])], [*references, ref]


#: The entry that fixes a refused request (the composer shows it next to the problem).
_ENTRY_OF = {
    "part_required": "add_reference",
    "target_required": "choose_target",
    "target_unavailable": "choose_target",
    "ref_version_changed": "choose_target",
    "attachment_required": "attach_file",
    "attachment_ambiguous": "choose_file",
    "spec_and_sources": "choose_spec_or_revise",
    "material_consent_required": "consent_material",
    "material_not_allowed": "shorten_text",
    "several_spec_versions": "remove_reference",
    "workflow_edit_part_only": "choose_target",
}


def missing_of(exc: AppError) -> dict:
    """One ``missing`` entry of a preflight ({code, message, entry, expected}) from the error a send would return."""
    details = dict(exc.details or {})
    entry: dict[str, Any] = {"action": _ENTRY_OF.get(exc.code) or details.get("recovery") or "change_request"}
    entry.update({k: v for k, v in (details.get("entry") or {}).items() if k != "action"})
    for k in ("candidates", "spec_key", "file_keys", "text_changes", "key", "formats"):
        if k in details:
            entry[k] = details[k]
    return {"code": exc.code, "message": exc.message, "entry": entry, "expected": list(details.get("expected") or [])}


def preflight(db: Session, user: User, conversation_id: uuid.UUID, *, text: str, operation: str, target_key: Any = UNSET,
              reference_keys: Any = UNSET, model_config_id: uuid.UUID | None = None, task_id: Any = UNSET,
              execution_policy: str | None = None, as_material: bool = False, auto_references: bool = True,
              revise: bool | None = None) -> dict:
    """POST /ai/conversations/{id}/preflight (generation reliability K, design §2.3): what a send of this request would
    do now — the operation (``operation_source`` main_agent / explicit / fallback), the object it works on, the references (the
    automatic ones included), every problem a send would refuse or fail with before any model call (``missing``), the
    policy and whether a model would be called. Read only: no model call, nothing written (same access as reading the
    conversation). ``auto``/``ask`` requests keep the main-agent route even when its model is unavailable;
    model readiness changes the diagnostics, never the operation dispatch."""
    from . import orchestrator

    _check_policy_value(execution_policy)
    conv, acc = load_conversation(db, user, conversation_id)
    require_open(conv)
    features.require_choices(model_config_id=model_config_id, execution_policy=execution_policy)
    chk = _Checks(collect=True)
    p = _prepare(db, user, conv, acc, chk, text=text, operation=operation, target_key=target_key, reference_keys=reference_keys,
                 task_id=task_id, execution_policy=execution_policy, as_material=as_material, auto_references=auto_references,
                 revise=revise, write=False)
    turn = orchestrator.routed(operation)
    kind = "ask" if turn else p.kind
    plan = TargetPlan(target=target_out(p.target))
    try:
        plan = target_plan(db, conv.project_id, kind, p.text, p.target, p.references, task_id=p.tid)
    except AppError as exc:
        plan = TargetPlan(target=target_out(p.target), missing=[missing_of(exc)])
    avail = _availability(acc, _ai_status(db)).get(kind) or {}
    if not avail.get("available", True):
        reason = avail.get("reason") or "ai_unavailable"
        message = f"{kind} cannot run now ({reason})"
        action = "configure_model" if reason in ("ai_unavailable", "simulation_unsupported") else "change_operation"
        if turn and reason == "ai_unavailable":
            from ..ai.providers.base import ProviderError
            from . import ai as ai_svc

            try:
                ai_svc._choose(db, "assistant", model_config_id)
            except ProviderError as exc:
                reason, message = exc.code, exc.message
        chk.missing.append({"code": reason, "message": message, "entry": {"action": action}, "expected": []})
    missing = [*chk.missing, *plan.missing]
    auto = {a.get("key"): a.get("reason") for a in p.auto_refs}
    references = []
    # a new assembly is built FROM its target document: listed with the material it reads, the target is the new assembly
    read = [p.target] if kind == "assembly_create" and p.target is not None and p.target.get("kind") == "file_version" else []
    for r in [*read, *p.references]:
        if not isinstance(r, dict):
            continue
        item = reference_out(r)
        if r.get("key") in auto:
            item.update(auto=True, reason=auto[r.get("key")])
        elif r.get("key") == "material:new":
            item.update(auto=True, reason="material")
        references.append(item)
    out: dict[str, Any] = {
        "operation": kind,
        "requested_operation": operation,
        "operation_source": "explicit" if operation != "auto" else "main_agent" if turn else "fallback",
        "target": plan.target,
        "references": references,
        "missing": missing,
        "policy": p.choice.policy,
        "policy_source": p.choice.source,
        "will_call_model": bool(not missing and plan.will_call_model),
    }
    if turn:
        out["runtime"] = orchestrator.RUNTIME
    return out


def _check_same_policy(found: tuple[AiMessage, AiOperation | None, AiMessage | None], policy: str, crid: uuid.UUID) -> None:
    """A replay must ask for the same execution policy as the stored request (W0 §2: part of the fingerprint)."""
    msg, op, _ = found
    used = (op.execution_policy if op is not None else None) or (msg.payload or {}).get("execution_policy") or "standard"
    if used != policy:
        raise conflict("idempotency_conflict", "this client_request_id was already used with another execution policy",
                       client_request_id=str(crid), execution_policy=used)


def _create_operation(db: Session, user: User, conv: AiConversation, msg: AiMessage, *, kind: str, request: dict,
                      task_id: uuid.UUID | None, client_request_id: uuid.UUID, retry_of: uuid.UUID | None = None) -> AiOperation:
    op = AiOperation(conversation_id=conv.id, message_id=msg.id, user_id=user.id, project_id=conv.project_id,
                     task_id=task_id if task_id and db.get(Task, task_id) is not None else None, kind=kind, status="pending",
                     client_request_id=client_request_id, request=request, retry_of=retry_of,
                     execution_policy=request.get("execution_policy") or "standard")
    db.add(op)
    db.flush()
    if msg.operation_id is None:  # written once, by the operation its request created (sub-agents / later turns keep it)
        msg.operation_id = op.id
    _append(db, conv, role="assistant", kind="operation", operation_id=op.id, payload={"operation": kind})
    return op


def _execute_and_out(db: Session, user: User, op_id: uuid.UUID, msg_id: uuid.UUID) -> dict:
    execute(db, user, op_id)
    op = db.get(AiOperation, op_id)
    msg = db.get(AiMessage, msg_id)
    assert op is not None and msg is not None
    asst = db.scalar(select(AiMessage).where(AiMessage.conversation_id == op.conversation_id, AiMessage.operation_id == op.id,
                                             AiMessage.role == "assistant").order_by(AiMessage.seq.desc()).limit(1))
    return send_out(db, msg, op, asst)


# --- execution (transaction 2) ---------------------------------------------------------------


def _freeze_team(db: Session, project_id: uuid.UUID) -> dict:
    from .engineering_team import freeze_team

    return freeze_team(db, project_id)


def execute(db: Session, user: User, op_id: uuid.UUID) -> None:
    op = db.get(AiOperation, op_id)
    assert op is not None
    with team_prompt.team_scope((op.request or {}).get("team_snapshot")):
        _execute_scoped(db, user, op_id)


def _execute_scoped(db: Session, user: User, op_id: uuid.UUID) -> None:
    """Run the business step of a pending operation. Any error is recorded on the operation
    (status failed) in a fresh transaction; nothing here reports a failure as a success."""
    from . import orchestrator
    from ..ai.providers.base import ProviderError

    op = db.get(AiOperation, op_id)
    assert op is not None
    if orchestrator.is_turn(op):
        return  # AI native: a main-agent turn stays pending; the assistant worker runs it (orchestrator.run_next_turn)
    ctx: dict[str, Any] = {"part_id": None}
    try:
        if op.kind == "task_draft":
            project_access(db, user, op.project_id, OP_ACTION[op.kind])
            features.require_enabled("tasks")
        req = op.request or {}
        _check_workflow_versions(db, [req.get("target"), *(req.get("references") or [])])
        # S2: a material that fits only when read in chunks is read first (no lock held during the calls); the
        # operation's own context build then places the reading notes.
        ctx["material_notes"] = _read_materials(db, op)
        if op.kind == "task_draft":
            _run_task_draft(db, user, op, ctx)
        elif op.kind == "ask":
            _run_ask(db, user, op, ctx)
        elif op.kind == "workflow_edit":
            _run_workflow_edit(db, user, op, ctx)
        elif op.kind == "workflow_create":
            _run_workflow_create(db, user, op, ctx)
        elif op.kind == "assembly_create":
            from .assembly_ai import start_assembly_create

            start_assembly_create(db, user, op)
        elif op.kind == "assembly_modify":
            from .assembly_ai import plan_assembly_modify

            plan_assembly_modify(db, user, op)
        elif op.kind in ("drafting_create", "drafting_modify"):
            from .assistant_cad_tools import run_drafting
            run_drafting(db, user, op)
        elif op.kind == "spec_extract":
            from .assistant_cad_tools import run_spec
            run_spec(db, user, op)
        else:
            if op.kind == "drawing_generate" and (req.get("tool_input") or {}).get("requirements"):
                from .assistant_cad_tools import plan_drawing_config
                ctx["drawing_config"] = plan_drawing_config(db, user, op)
            _run_with_run(db, user, op, ctx)
    except ProviderError as exc:
        db.rollback()
        _fail(db, op_id, {"code": "ai_unavailable", "message": exc.message,
                          "details": {"reason": exc.code, "retryable": exc.retryable}}, part_id=ctx["part_id"])
    except AppError as exc:
        db.rollback()
        _fail(db, op_id, {"code": exc.code, "message": exc.message, "details": exc.details or {}}, part_id=ctx["part_id"])
    except Exception:
        log.exception("AI workspace operation %s failed", op_id)
        db.rollback()
        _fail(db, op_id, {"code": "internal_error", "message": "the operation failed unexpectedly", "details": {}}, part_id=ctx["part_id"])


#: Operations whose context build places materials (assembly_create hands them to its spec step; an intake fails clearly).
MATERIAL_BUILD_KINDS = ("ask", "task_draft", "workflow_edit", "model_create", "model_modify", "drawing_reconstruct", "drawing_generate",
                        "workflow_create")


def material_keys(req: dict) -> list[str]:
    """Keys of the request's materials (S2): text saved from an over-long request, read by the context build."""
    return [str(m.get("key")) for m in req.get("materials") or [] if isinstance(m, dict) and m.get("key")]


def _base_revision_of(db: Session, op: AiOperation) -> ModelRevision | None:
    target = (op.request or {}).get("target") or {}
    if op.kind != "model_modify" or target.get("kind") not in ("part_revision", "cad_selection"):
        return None
    return db.get(ModelRevision, _uuid(target.get("object_id")))


def _read_materials(db: Session, op: AiOperation) -> dict[str, dict] | None:
    """S2 chunked reading: when the operation's build (a read-only estimate made here) can place a material only as
    reading notes, every chunk is read in a call of its own (``ai/material_reader.py``; calls recorded on the operation)
    and the notes are returned for the real build. None when nothing has to be read. A chunk that cannot be read fails
    the operation with ``material_read_failed`` (which material, which chunk, why)."""
    from ..ai import material_reader
    from ..ai.providers.base import ProviderError, clip
    from . import ai as ai_svc
    from . import assistant_context as actx

    req = op.request or {}
    keys = material_keys(req)
    if not keys or op.kind not in MATERIAL_BUILD_KINDS:
        return None
    conv = db.get(AiConversation, op.conversation_id)
    # The kind the operation's AI build uses (a drawing of a described part is modelled first: model_create; a new
    # workflow is planned by the workflow edit agent).
    build_kind = {"drawing_generate": "model_create", "workflow_create": "workflow_edit"}.get(op.kind, op.kind)
    probe = actx.build_context(db, conversation=conv, kind=build_kind, text=req.get("text") or "", target=req.get("target"),
                               references=list(req.get("references") or []), constraints=list(req.get("constraints") or []),
                               model_config_id=_model_ref(req), base_revision=_base_revision_of(db, op), materials=keys)
    if not probe.pending_materials:
        return None
    db.commit()  # read only so far; nothing is held during the calls
    try:
        provider, _ = ai_svc.provider_for(db, OP_PURPOSE.get(op.kind, "assistant"), _model_ref(req))
    except ProviderError as exc:
        raise AppError(503, "ai_unavailable", "the AI model could not read the material; check the model settings",
                       {"reason": exc.code, "detail": exc.message})
    notes: dict[str, dict] = {}
    for m in probe.pending_materials:
        outcome = material_reader.read_chunks(provider, request=req.get("text") or "", label=m["label"], text=m["text"], ranges=m["ranges"])
        ai_svc.record_calls(db, list(outcome.calls or []), None, ai_operation_id=op.id)
        db.commit()
        if outcome.error:
            err = outcome.error
            raise AppError(503 if err.get("retryable") else 422, "material_read_failed",
                           f"chunk {err.get('chunk')} of {len(m['ranges'])} of the material \"{m['label']}\" could not be read "
                           f"({err.get('code')}); nothing else was started",
                           {"material": {"key": m["key"], "label": m["label"], "chars": m["chars"]}, "chunk": err.get("chunk"),
                            "chunks": len(m["ranges"]), "reason": err.get("code"), "detail": clip(str(err.get("message") or ""), 300),
                            "retryable": bool(err.get("retryable"))})
        notes[m["key"]] = {"chunks": [vars(n) for n in outcome.notes], "calls": len(outcome.calls or []), "simulated": outcome.simulated}
    return notes


def _fail(db: Session, op_id: uuid.UUID, error: dict, *, part_id: uuid.UUID | None = None) -> None:
    op = _lock_op(db, op_id)
    if op.status in ACTIVE:
        op.status = "failed"
        op.error = error
        if op.part_id is None and part_id is not None:
            op.part_id = part_id
        _on_terminal(db, op)
    db.commit()


def _on_terminal(db: Session, op: AiOperation, *, defer: set[uuid.UUID] | None = None) -> None:
    """Finish bookkeeping: finished_at, build usage, then the conversation (activity, summary).
    Summary / usage problems never block recording the operation's real status. With `defer`,
    the conversation is only collected: a caller syncing several operations touches the
    conversations after all operation locks (operation → conversation order, no deadlock).
    AI native: a finished sub-agent is queued to wake its main agent (drained by _touch_conversations)."""
    from . import assistant_context as actx
    from . import orchestrator, cad_results

    from .assistant_cad_tools import stamp_plan
    stamp_plan(op)
    cad_results.record(db, op)
    orchestrator.queue_wake(db, op)
    op.finished_at = op.finished_at or _now()
    db.flush()
    try:
        with db.begin_nested():
            actx.finalize_build_usage(db, op)
    except Exception:
        log.exception("AI workspace usage update failed for operation %s", op.id)
    if defer is not None:
        defer.add(op.conversation_id)
    else:
        _touch_conversations(db, {op.conversation_id})


def _touch_conversations(db: Session, conversation_ids: set[uuid.UUID]) -> None:
    """Activity and summary of the conversations of finished operations, then (AI native) the main agents those
    sub-agents wake. Called after every operation lock (operation → conversation)."""
    from . import assistant_context as actx
    from . import orchestrator

    for cid in sorted(conversation_ids):
        conv = db.get(AiConversation, cid)
        if conv is None:
            continue
        conv.last_activity_at = _now()
        db.flush()
        try:
            with db.begin_nested():
                actx.refresh_summary(db, conv)
        except Exception:
            log.exception("AI workspace summary refresh failed for conversation %s", cid)


#: Context objects that never stand in for a part, a model or a file (generation reliability K, design §2.3: a task record
#: is not a model; its part is offered as the entry to add instead).
_CONTEXT_ONLY = ("task", "criterion")


def _first_part_ref(target: dict | None, references: list[dict]) -> uuid.UUID | None:
    for r in ([target] if target else []) + list(references):
        if not r or r.get("kind") in _CONTEXT_ONLY:
            continue
        pid = _uuid(r.get("part_id"))
        if pid is not None:
            return pid
    return None


def _reconstruct_source(db: Session, op: Any, target: dict | None) -> tuple[FileVersion | None, Any]:
    """(file version, drawing) a drawing_reconstruct starts from: a DXF / DWG file version (or a document version of one),
    or a drawing version. 422 target_required otherwise; 404 outside the operation's project."""
    from ..models import Drawing

    if refs.cad_drawing_format(target) is not None:
        oid = _uuid((target or {}).get("object_id"))
        if (target or {}).get("kind") == "file_version":
            fv = db.get(FileVersion, oid)
        else:
            v = db.get(AssetVersion, oid)
            fv = db.get(FileVersion, v.file_version_id) if v is not None and v.file_version_id else None
        f = db.get(File, fv.file_id) if fv else None
        if fv is None or f is None or f.project_id != op.project_id:
            raise not_found("file version")
        return fv, None
    if refs.is_drawing_asset(target):
        v = db.get(AssetVersion, _uuid((target or {}).get("object_id")))
        d = db.get(Drawing, v.drawing_id) if v is not None and v.drawing_id else None
        p = db.get(Part, d.part_id) if d else None
        if d is None or p is None or p.project_id != op.project_id:
            raise not_found("drawing")
        return None, d
    raise AppError(422, "target_required", "choose the DXF or DWG drawing file (or a drawing) to reconstruct",
                   {"expected": ["file_version", "asset_version"], "formats": ["dxf", "dwg"]})


# --- the object an operation works on (generation reliability K, design §2.3) ----------------------------------------------

#: Target types a preflight reports (design §2.3; ``file`` / ``workflow`` / ``run`` added for objects that are none of them).
TARGET_TYPES = ("part", "model_revision", "step_file", "drawing_file", "assembly_revision", "spec_version", "task", "file", "workflow",
                "run", "none")


def target_type(ref: dict | None) -> str:
    """The type of the object a reference names, as a preflight shows it. A task is a task, never a model or a file."""
    if not ref:
        return "none"
    kind = ref.get("kind")
    if kind == "part":
        return "part"
    if kind in ("part_revision", "cad_selection"):
        return "model_revision"
    if kind in _CONTEXT_ONLY:
        return "task"
    if kind == "assembly_revision":
        return "assembly_revision"
    if kind == "engineering_spec_version":
        return "spec_version"
    if kind in ("workflow", "run"):
        return str(kind)
    if refs.is_step_file(ref):
        return "step_file"
    if refs.cad_drawing_format(ref) is not None or refs.is_drawing_asset(ref) or (
            kind == "file_version" and ref.get("file_kind") in refs.DRAWING_FILE_KINDS):
        return "drawing_file"
    return "file"


def target_out(ref: dict | None, **extra: Any) -> dict:
    """{type, id, label, version} of the object a reference names (``key``: the composer key)."""
    if not ref:
        return {"type": "none", "id": None, "label": "", "version": None, "key": None, **extra}
    return {"type": target_type(ref), "id": ref.get("object_id"), "label": str(ref.get("title") or ""), "version": ref.get("version_label"),
            "key": ref.get("key"), **extra}


def reference_out(ref: dict) -> dict:
    return {"key": ref.get("key"), "kind": ref.get("kind"), "type": target_type(ref), "label": str(ref.get("title") or ""),
            "version": ref.get("version_label")}


def _part_out(part: Part, **extra: Any) -> dict:
    return {"type": "part", "id": str(part.id), "label": f"{part.number} {part.name}".strip(), "version": None, "key": f"part:{part.id}",
            **extra}


def _revision_out(rev: ModelRevision, part: Part | None, **extra: Any) -> dict:
    return {"type": "model_revision", "id": str(rev.id), "label": (part.name if part else ""), "version": f"R{rev.rev_no}",
            "key": f"part_revision:{rev.id}", **extra}


@dataclass
class TargetPlan:
    """What an operation of a request works on (``target``: {type, id, label, version}) and the problems that stop it
    before any model call (``missing``: {code, message, entry, expected}). Drawing / model operations also carry what
    their business step needs (part, mode, file version, drawing, base revision)."""

    target: dict
    missing: list[dict] = field(default_factory=list)
    part_id: uuid.UUID | None = None
    mode: str | None = None
    fv: FileVersion | None = None
    drawing: Any = None
    base_rev: ModelRevision | None = None
    will_call_model: bool = True

    def raise_first(self) -> None:
        """The business step refuses with the first problem (the same code a preflight showed)."""
        if self.missing:
            m = self.missing[0]
            details = {k: v for k, v in m.items() if k not in ("code", "message") and v not in (None, [], {})}
            raise AppError(422, m["code"], m["message"], details)


def _need(code: str, message: str, *, action: str, expected: list[str] | None = None, **entry: Any) -> dict:
    return {"code": code, "message": message, "entry": {"action": action, **entry}, "expected": list(expected or [])}


def _task_part_hint(db: Session, refs_: list[dict | None]) -> dict:
    """A task / criterion reference whose task has a part: that part is the entry to add (the task never is the part)."""
    for r in refs_:
        if r and r.get("kind") in _CONTEXT_ONLY and _uuid(r.get("part_id")) is not None:
            part = db.get(Part, _uuid(r.get("part_id")))
            if part is not None and part.archived_at is None:
                return {"kind": "part", "object_id": str(part.id), "label": f"{part.number} {part.name}".strip(), "from": r.get("key")}
    return {"kind": "part"}


def _part_required(db: Session, message: str, refs_: list[dict | None]) -> dict:
    hint = _task_part_hint(db, refs_)
    if hint.get("object_id"):
        message += f" (a task is not a part: add its part {hint['label']} as a reference)"
    return _need("part_required", message, action="add_reference", expected=["part"], **hint)


def target_plan(db: Session, project_id: uuid.UUID, kind: str, text: str, target: dict | None, references: list[dict], *,
                task_id: Any = None) -> TargetPlan:
    """The target checks of an operation, shared by the preflight and the business step (design §2.3): which object it
    works on and what is missing (``part_required`` / ``target_required`` …) — checked before any model call. Lookups of
    objects that no longer exist raise (404) as before; a preflight lists them as ``missing``."""
    req = types.SimpleNamespace(project_id=project_id, request={"target": target, "references": references, "task_id": task_id})
    tkind = target.get("kind") if target else None
    plan = TargetPlan(target=target_out(target))
    if kind == "model_modify":
        if tkind not in ("part_revision", "cad_selection"):
            plan.missing.append(_need("target_required", "choose the model revision (or a face / edge of it) to modify",
                                      action="choose_target", expected=["part_revision", "cad_selection"]))
            return plan
        plan.base_rev = db.get(ModelRevision, _uuid(target.get("object_id")))  # type: ignore[union-attr]
        if plan.base_rev is None:
            raise not_found("revision")
        plan.part_id = plan.base_rev.part_id
        plan.target = _revision_out(plan.base_rev, db.get(Part, plan.part_id))
        return plan
    if kind == "model_create":
        part_id = _uuid(target.get("part_id")) if target and tkind not in _CONTEXT_ONLY else None
        plan.part_id = part_id or _first_part_ref(None, references)
        if plan.part_id is None:
            plan.missing.append(_part_required(db, "choose the part to model (add it as the target)", [target, *references]))
            return plan
        part = db.get(Part, plan.part_id)
        plan.target = _part_out(part) if part is not None else plan.target
        return plan
    if kind == "drawing_intake":
        if tkind != "file_version":
            plan.missing.append(_need("target_required", "choose the drawing file version to analyse", action="choose_target",
                                      expected=["file_version"]))
            return plan
        fv = db.get(FileVersion, _uuid(target.get("object_id")))  # type: ignore[union-attr]
        f = db.get(File, fv.file_id) if fv else None
        if fv is None or f is None:
            raise not_found("file version")
        if f.kind not in refs.DRAWING_FILE_KINDS:
            drawing_format = refs.cad_drawing_format({"kind": "file_version", "file_name": fv.original_name, "file_kind": f.kind})
            actual_format = drawing_format or f.kind
            message = f"the selected {actual_format.upper()} file exists, but read_drawing accepts only PDF or image drawings"
            compatible_tools: list[str] = []
            if drawing_format is not None:
                message += ("; use extract_spec to read the complete original CAD source first when its scope is unknown "
                            "or it contains an assembly; reconstruct_drawing supports only an identified single part and sheet")
                compatible_tools = ["extract_spec", "reconstruct_drawing"]
            plan.missing.append(_need("drawing_format_mismatch", message, action="change_request", expected=["file_version"],
                                      file_kind=f.kind, actual_format=actual_format, formats=["pdf", "image"],
                                      compatible_tools=compatible_tools))
            return plan
        plan.fv, plan.part_id = fv, f.part_id or _first_part_ref(None, references)
        plan.target = target_out(target)
        if plan.part_id is None:
            plan.missing.append(_part_required(db, "this drawing is not attached to a part; add the part as a reference", references))
        return plan
    if kind == "drawing_reconstruct":
        try:
            plan.fv, plan.drawing = _reconstruct_source(db, req, target)
        except AppError as exc:
            if exc.status != 422:
                raise
            plan.missing.append(missing_of(exc))
            return plan
        owner = db.get(File, plan.fv.file_id).part_id if plan.fv is not None else plan.drawing.part_id  # type: ignore[union-attr]
        plan.part_id, plan.mode = owner or _first_part_ref(None, references), "reconstruct"
        if plan.part_id is None:
            plan.missing.append(_part_required(db, "this drawing is not attached to a part; add the part as a reference", references))
        return plan
    if kind == "drawing_generate":
        if tkind in ("part_revision", "cad_selection"):
            plan.base_rev = db.get(ModelRevision, _uuid(target.get("object_id")))  # type: ignore[union-attr]
            if plan.base_rev is None:
                raise not_found("revision")
            plan.part_id, plan.mode = plan.base_rev.part_id, "revision"
            plan.target, plan.will_call_model = _revision_out(plan.base_rev, db.get(Part, plan.part_id)), False
        elif refs.is_step_file(target):
            fv = db.get(FileVersion, _uuid(target.get("object_id")))  # type: ignore[union-attr]
            f = db.get(File, fv.file_id) if fv else None
            if fv is None or f is None or f.project_id != project_id:
                raise not_found("file version")
            plan.fv, plan.part_id, plan.mode, plan.will_call_model = fv, f.part_id or _first_part_ref(None, references), "step", False
            if plan.part_id is None:
                plan.missing.append(_part_required(db, "this STEP file is not attached to a part; add the part as a reference", references))
        elif target is None or tkind == "part" or _first_part_ref(target, references) is not None:
            plan.part_id, plan.mode = _first_part_ref(target, references), "text"
            if plan.part_id is None:
                plan.missing.append(_part_required(db, "choose the part to model and draw (add it as the target)", [target, *references]))
                return plan
            part = db.get(Part, plan.part_id)
            plan.target = _part_out(part) if part is not None else plan.target
            if not has_modeling_intent(text):
                # A part that already has a model and a request that only asks for drawings: draw its latest ready
                # revision (no AI, no new model). A request that also describes geometry stays D2 (model → draw).
                from .manufacturing import latest_revision

                latest = latest_revision(db, plan.part_id)
                if latest is not None:
                    plan.base_rev, plan.mode, plan.will_call_model = latest, "revision", False
                    plan.target = _revision_out(latest, part, via="latest_revision")
        else:
            entry = _task_part_hint(db, [target, *references]) if tkind in _CONTEXT_ONLY else {}
            plan.missing.append(_need("target_required", "choose a model revision, a STEP file or a part (with a description) to draw"
                                      + (" (a task is not a model or a file)" if tkind in _CONTEXT_ONLY else ""),
                                      action="choose_target", expected=["part_revision", "cad_selection", "file_version", "part"], **entry))
        return plan
    if kind == "workflow_edit":
        try:
            w, run, _ = _plan_target(db, req, target)  # type: ignore[arg-type]
        except AppError as exc:
            if exc.status not in (409, 422):
                raise
            plan.missing.append(missing_of(exc))
            return plan
        plan.target = {"type": "workflow", "id": str(w.id), "label": w.name, "version": f"v{w.version}",
                       "key": target.get("key") if target else None, **({"via": "run", "run_id": str(run.id)} if run is not None else {})}
        return plan
    if kind == "workflow_create":
        try:
            part = _create_part(db, req, target, references, task_id)  # type: ignore[arg-type]
        except AppError as exc:
            if exc.status != 422:
                raise
            plan.missing.append(missing_of(exc))
            return plan
        plan.part_id, plan.target = part.id, _part_out(part)
        return plan
    if kind == "assembly_modify":
        from .assembly_ai import _target_revision

        try:
            _target_revision(db, req)  # type: ignore[arg-type]
        except AppError as exc:
            if exc.status != 422:
                raise
            plan.missing.append(missing_of(exc))
        return plan
    if kind == "assembly_create":
        spec = next((r for r in [target, *references] if r and r.get("kind") == "engineering_spec_version"), None)
        plan.target = target_out(spec) if spec else target_out(None, label="new assembly")
        return plan
    return plan  # ask / task_draft: the target is context


def _run_drawing_op(db: Session, user: User, op: AiOperation, ctx: dict) -> None:
    """drawing_reconstruct (DXF / DWG → STEP [→ drawing]) and drawing_generate (revision → drawing; STEP file → import →
    drawing; a part with a ready revision and a drawing-only request → its latest revision → drawing, no AI; text with
    modeling intent → model_3d create → drawing). Same checks and order as the model operations (project share → part →
    task → part_busy), the same context build / private context rules for the AI nodes, then start_run."""
    from .ai_model_graph import build_model_graph
    from ..api.workflows import start_run
    from . import assistant_context as actx
    from .cad_admission import admit_part

    req = op.request or {}
    text: str = req.get("text") or ""
    target: dict | None = req.get("target")
    references: list[dict] = list(req.get("references") or [])
    mcid = _uuid(req.get("model_config_id"))
    project_access(db, user, op.project_id, "design")
    # Generation reliability K: the same target checks as the preflight (design §2.3).
    plan = target_plan(db, op.project_id, op.kind, text, target, references, task_id=req.get("task_id"))
    plan.raise_first()
    fv, drawing, base_rev, part_id = plan.fv, plan.drawing, plan.base_rev, plan.part_id
    mode = plan.mode or op.kind
    assert part_id is not None
    ctx["part_id"] = part_id
    part = admit_part(db, user, part_id, op.project_id, exclude_operation_id=op.id)
    op = _lock_op(db, op.id)
    if op.status not in ("pending", "running"):
        db.commit()
        return
    if base_rev is not None and base_rev.mesh_only:
        raise AppError(422, "mesh_reference", "a mesh reference (STL/OBJ/GLB) has no STEP to draw; import a STEP file instead",
                       {"revision_id": str(base_rev.id)})
    tid = _uuid(req.get("task_id"))
    task = require_task_writable(db, user, tid, op.project_id) if tid else None
    touched: set[uuid.UUID] = {op.conversation_id}
    conv = db.get(AiConversation, op.conversation_id)

    def ai_build(kind: str) -> Any | None:
        """The context build of the AI node (recorded on the operation); None after failing the operation (over budget)."""
        build = actx.build_context(db, conversation=conv, kind=kind, text=text, target=target, references=references,
                                   constraints=list(req.get("constraints") or []), model_config_id=_model_ref(req),
                                   materials=material_keys(req), material_notes=ctx.get("material_notes"))
        actx.record_build(db, op, build)
        if not build.ok:
            op.status, op.part_id = "failed", part.id
            op.error = build.error or {"code": "context_over_budget", "message": "the required context does not fit the model", "details": {}}
            _on_terminal(db, op, defer=touched)
            _touch_conversations(db, touched)
            db.commit()
            return None
        return build

    nodes: list[dict] = []
    edges: list[dict] = []
    if op.kind == "drawing_reconstruct":
        build = ai_build("drawing_reconstruct")
        if build is None:
            return
        # The graph is readable by every project member: only the public target block goes into it; the private part
        # stays on the build and is read by the node of this run only (assistant_operation_id).
        params: dict[str, Any] = {"notes": text, "sheet": (req.get("tool_input") or {}).get("sheet") or "",
                                  "context": build.context_text or "", "assistant_operation_id": str(op.id)}
        if mcid:
            params["model_config_id"] = str(mcid)
        if fv is not None:
            nodes.append({"id": "file", "type": "file_input", "params": {"file_version_id": str(fv.id)}})
            edges.append({"id": "file->reconstruct", "source": "file", "source_port": "file", "target": "reconstruct", "target_port": "file"})
        else:
            params["drawing_id"] = str(drawing.id)
        nodes.append({"id": "reconstruct", "type": "drawing_reconstruct", "params": params})
        title, model_node = "AI reconstruct from drawing", "reconstruct"
        with_drawing = bool(_GEN_DRAWING.search(text))  # the user also asked for a drawing package
    else:
        with_drawing = True
        if mode == "revision":
            assert base_rev is not None
            nodes.append({"id": "base", "type": "model_input", "params": {"revision_id": str(base_rev.id)}})
            title, model_node = f"Drawing of R{base_rev.rev_no}", "base"
        elif mode == "step":
            assert fv is not None
            nodes += [{"id": "file", "type": "file_input", "params": {"file_version_id": str(fv.id)}},
                      {"id": "model", "type": "model_3d", "params": {}}]  # file only: STEP audit + import, no AI
            edges.append({"id": "file->model", "source": "file", "source_port": "file", "target": "model", "target_port": "file"})
            title, model_node = "Drawing of an imported STEP", "model"
        else:
            build = ai_build("model_create")
            if build is None:
                return
            g = build_model_graph(part, None, text, mcid, build.context_text or "", assistant_operation_id=op.id)
            nodes, edges = list(g["nodes"]), list(g["edges"])
            title, model_node = "AI create and draw", "model"
    if with_drawing:
        nodes.append({"id": "drawing", "type": "drawing", "params": ctx.get("drawing_config") or {}})
        edges.append({"id": f"{model_node}->drawing", "source": model_node, "source_port": "model", "target": "drawing",
                      "target_port": "model"})
    run = start_run(db, user, project_id=op.project_id, graph_raw={"nodes": nodes, "edges": edges}, part_id=part.id, title=title,
                    origin="workspace", task=task, purpose=text, model_snapshot=req.get("model_snapshot"), model_snapshots=req.get("model_snapshots"),
                    preferences_snapshot=req.get("preferences_snapshot"), team_snapshot=req.get("team_snapshot"),
                    **policy_svc.frozen_on(op))
    op.run_id, op.part_id, op.status = run.id, part.id, "running"
    _touch_conversations(db, touched)
    db.commit()


def _run_with_run(db: Session, user: User, op: AiOperation, ctx: dict) -> None:
    """model_create / model_modify / drawing_intake: checks in the contract order, then start_run."""
    from .ai_model_graph import build_model_graph, model_run_title
    from ..api.workflows import start_run
    from . import assistant_context as actx
    from .cad_admission import admit_part

    if op.kind in DRAWING_KINDS:
        _run_drawing_op(db, user, op, ctx)
        return
    req = op.request or {}
    text: str = req.get("text") or ""
    target: dict | None = req.get("target")
    references: list[dict] = list(req.get("references") or [])
    mcid = _uuid(req.get("model_config_id"))
    project_access(db, user, op.project_id, "design")
    # Generation reliability K: the same target checks as the preflight (design §2.3).
    plan = target_plan(db, op.project_id, op.kind, text, target, references, task_id=req.get("task_id"))
    plan.raise_first()
    base_rev, fv, part_id = plan.base_rev, plan.fv, plan.part_id
    assert part_id is not None
    ctx["part_id"] = part_id
    # Lock order: project (share) → part → task.
    part = admit_part(db, user, part_id, op.project_id, exclude_operation_id=op.id)
    if base_rev is not None and base_rev.mesh_only:
        raise AppError(422, "mesh_reference", "a mesh reference (STL/OBJ/GLB) cannot be modified; import a STEP file instead",
                       {"revision_id": str(base_rev.id)})
    tid = _uuid(req.get("task_id"))
    task = require_task_writable(db, user, tid, op.project_id) if tid else None
    # Conversations are written last, together and in id order (after every operation lock).
    touched: set[uuid.UUID] = {op.conversation_id}
    conv = db.get(AiConversation, op.conversation_id)
    if op.kind in MODEL_KINDS:
        build = actx.build_context(db, conversation=conv, kind=op.kind, text=text, target=target, references=references,
                                   constraints=list(req.get("constraints") or []), model_config_id=_model_ref(req), base_revision=base_rev,
                                   materials=material_keys(req), material_notes=ctx.get("material_notes"))
        actx.record_build(db, op, build)
        if not build.ok:
            op.status, op.part_id = "failed", part.id
            op.error = build.error or {"code": "context_over_budget", "message": "the required context does not fit the model", "details": {}}
            _on_terminal(db, op, defer=touched)
            _touch_conversations(db, touched)
            db.commit()
            return
        # The graph is readable by every project member: only the target block goes into it; the private part
        # (constraints, memory, other references) stays on the build and is read by the node of this run only.
        graph = build_model_graph(part, base_rev, text, mcid, build.context_text or "", assistant_operation_id=op.id)
        title = model_run_title(base_rev)
    else:
        assert fv is not None
        if len(text) > INTAKE_HINTS_MAX:  # the node's hint limit; never cut the request silently
            raise AppError(422, "text_too_long", f"a drawing analysis request can be at most {INTAKE_HINTS_MAX} characters",
                           {"max": INTAKE_HINTS_MAX, "length": len(text)})
        # Manifest of what the intake model gets: the drawing (attached) and the request as hints.
        build = actx.build_context(db, conversation=conv, kind="drawing_intake", text=text, target=target, references=references,
                                   constraints=[], model_config_id=_model_ref(req), materials=material_keys(req))
        actx.record_build(db, op, build)
        if not build.ok:  # S2: a material saved from the request would not be read by an intake
            op.status, op.part_id, op.error = "failed", part.id, build.error
            _on_terminal(db, op, defer=touched)
            _touch_conversations(db, touched)
            db.commit()
            return
        params: dict[str, Any] = {"hints": text}
        if mcid:
            params["model_config_id"] = str(mcid)
        graph = {"nodes": [{"id": "file", "type": "file_input", "params": {"file_version_id": str(fv.id)}},
                           {"id": "intake", "type": "drawing_intake", "params": params}],
                 "edges": [{"id": "file->intake", "source": "file", "source_port": "file", "target": "intake", "target_port": "file"}]}
        title = "AI drawing intake"
    run = start_run(db, user, project_id=op.project_id, graph_raw=graph, part_id=part.id, title=title, origin="workspace", task=task,
                    purpose=text, model_snapshot=req.get("model_snapshot"), model_snapshots=req.get("model_snapshots"),
                    preferences_snapshot=req.get("preferences_snapshot"), team_snapshot=req.get("team_snapshot"),
                    **policy_svc.frozen_on(op))
    op.run_id, op.part_id, op.status = run.id, part.id, "running"
    _touch_conversations(db, touched)
    db.commit()


def _run_task_draft(db: Session, user: User, op: AiOperation, ctx: dict | None = None) -> None:
    """stages.draft_task with the request, the built context as a separate data block (target, references
    such as tasks, criteria and excerpts, recent messages), the pinned constraints and the chosen model
    config (so the build's model label is the config actually used); the build is the call's manifest."""
    from . import assistant_context as actx
    from . import stages

    project_access(db, user, op.project_id, "manage")
    req = op.request or {}
    text = req.get("text") or ""
    conv = db.get(AiConversation, op.conversation_id)
    ctx = ctx or {}
    build = actx.build_context(db, conversation=conv, kind="task_draft", text=text, target=req.get("target"),
                               references=list(req.get("references") or []), constraints=list(req.get("constraints") or []),
                               model_config_id=_model_ref(req), materials=material_keys(req), material_notes=ctx.get("material_notes"))
    actx.record_build(db, op, build)
    if not build.ok:
        op.status = "failed"
        op.error = build.error or {"code": "context_over_budget", "message": "the required context does not fit the model", "details": {}}
        _on_terminal(db, op)
        db.commit()
        return
    db.commit()  # the manifest stays on record whatever the model call does; no lock held during it
    draft = stages.draft_task(db, op.project_id, text, context=build.context_text or "", constraints=list(build.constraints),
                              model_config_id=_model_ref(req))
    op = _lock_op(db, op.id)
    if op.status != "pending":
        db.commit()
        return
    op.status = "succeeded"
    op.result = {"kind": "task_draft", "draft": draft}
    op.simulated = bool(draft.get("simulated"))
    _on_terminal(db, op)
    db.commit()


def _run_ask(db: Session, user: User, op: AiOperation, ctx: dict | None = None) -> None:
    """Read-only answer (backend B's run_ask). The context build is committed before the model
    call so it stays on record; no row lock is held during the call. P4: a model that can call tools answers on the
    read-only tool agent (``assistant_tools``): same build, look-ups inside the project, ``consulted`` on the answer;
    the agent never writes (a change is still only ``suggested_operation``). Simulation stays the single call."""
    from ..ai import assistant_agent
    from ..ai.providers.base import ProviderError
    from . import ai as ai_svc
    from . import assistant_context as actx
    from . import assistant_tools

    req = op.request or {}
    conv = db.get(AiConversation, op.conversation_id)
    ctx = ctx or {}
    build = actx.build_context(db, conversation=conv, kind="ask", text=req.get("text") or "", target=req.get("target"),
                               references=list(req.get("references") or []), constraints=list(req.get("constraints") or []),
                               model_config_id=_model_ref(req), materials=material_keys(req), material_notes=ctx.get("material_notes"))
    runtime = "native"
    provider = None
    if build.ok:
        try:
            provider, cfg = ai_svc.provider_for(db, "assistant", _model_ref(req))
        except ProviderError as exc:
            actx.record_build(db, op, build)
            db.commit()
            raise AppError(503, "ai_unavailable", "the AI model could not answer; check the model settings",
                           {"reason": exc.code, "detail": exc.message}) from None
        runtime = assistant_tools.ask_runtime(provider, cfg, build)
        if runtime == "tool_agent":
            from ..ai.tool_registry import ASSISTANT_TOOL_SPECS

            available = [s for name, s in ASSISTANT_TOOL_SPECS.items() if name != "search_knowledge" or features.enabled("collaboration")]
            schema = json.dumps({"tools": [{"name": s.name, "description": s.description, "parameters": s.json_schema()}
                                           for s in available], "output": assistant_agent.AskReply.model_json_schema()}, ensure_ascii=False)
            tool_build = actx.build_context(db, conversation=conv, kind="ask", text=req.get("text") or "", target=req.get("target"),
                                           references=list(req.get("references") or []), constraints=list(req.get("constraints") or []),
                                           model_config_id=_model_ref(req), materials=material_keys(req), material_notes=ctx.get("material_notes"),
                                           system_prompt=assistant_agent.ask_tool_system_prompt(), output_schema=schema)
            if tool_build.ok:
                build = tool_build
            else:
                runtime = "native"  # the established fallback when there is no room for lookup tools
    actx.record_build(db, op, build)
    if not build.ok:
        op.status = "failed"
        op.error = build.error or {"code": "context_over_budget", "message": "the required context does not fit the model", "details": {}}
        _on_terminal(db, op)
        db.commit()
        return
    db.commit()
    assert provider is not None
    consulted: list[dict] | None = None
    try:
        if runtime == "tool_agent":
            outcome, consulted = assistant_tools.run_ask_agent(provider, build, project_id=op.project_id)
        else:
            outcome = assistant_agent.run_ask(provider, build.system_text, build.context_text, timeout_s=60)
    except ProviderError as exc:
        raise AppError(503, "ai_unavailable", "the AI model could not answer; check the model settings",
                       {"reason": exc.code, "detail": exc.message})
    op = _lock_op(db, op.id)
    ai_svc.record_calls(db, list(outcome.calls or []), None, ai_operation_id=op.id)
    if op.status != "pending":
        db.commit()
        return
    op.simulated = bool(outcome.simulated)
    if outcome.error:
        op.status, op.error = "failed", outcome.error
    else:
        conv = lock_conversation(db, op.conversation_id)
        payload = {"cited_keys": list(outcome.cited_keys or []), "suggested_operation": outcome.suggested_operation,
                   "follow_ups": list(outcome.follow_ups or []), "simulated": bool(outcome.simulated)}
        if consulted is not None:  # P4: the tool agent's look-ups ("what was consulted")
            payload.update(runtime="tool_agent", consulted=consulted)
        _append(db, conv, role="assistant", kind="answer", text=outcome.answer, operation_id=op.id, payload=payload)
        op.status = "succeeded"
    _on_terminal(db, op)
    db.commit()


# --- workflow edit plans (handoff docs/archive/web-refactor-r12-handoff.zh-CN.md §5.A item 5) ------------------
#
# An operation of kind workflow_edit asks the model for a change plan of ONE part workflow. Succeeded means
# "plan ready": the result holds the diff and the planned graph, nothing is written. The owner applies it
# (POST /ai/operations/{id}/apply) while the workflow is still at the plan's base version, and can undo it
# while the workflow is still at the applied version; both write a NEW workflow version (history is never
# rewritten) under the same permission as saving the workflow. Runs are never touched: a run keeps its
# graph_snapshot, only the next run uses the new graph. Nothing is approved, run or answered.
# Lock order: operation → workflow → conversation.

#: Repair calls after the first reply while the plan does not match the schema or does not apply.
WORKFLOW_EDIT_REPAIRS = 2


def _edit_workflow(db: Session, project_id: uuid.UUID, workflow_id: Any) -> Workflow:
    """The part workflow a plan is for: 404 outside the project; 409 workflow_edit_part_only for a project
    template (templates are edited by template managers in the editor) or a collaboration draft; 409
    workflow_archived."""
    w = db.get(Workflow, _uuid(workflow_id)) if _uuid(workflow_id) else None
    if w is None or w.project_id != project_id:
        raise not_found("workflow")
    if w.part_id is None or w.kind != "tool":
        raise conflict("workflow_edit_part_only", "AI plans change part workflows only; project templates are edited by "
                       "template managers in the workflow editor", workflow_id=str(w.id))
    if w.archived_at is not None:
        raise conflict("workflow_archived", "the workflow is archived; restore it first", workflow_id=str(w.id))
    return w


def _private_in_plan(build: Any, request_text: str, diff: dict | None) -> list[str]:
    """Nodes whose planned label / parameters copy text from the private context (pinned constraints, memory,
    other references): applying the plan makes them visible to every project member, so the plan says so."""
    from ..ai.prompts import contains_private, private_needles

    if not build.private_context or not diff:
        return []
    try:
        data = json.loads(build.private_context)
    except ValueError:
        return []
    needles = private_needles(data.get("constraints") or [], data.get("data") or "", public=[request_text])
    if not needles:
        return []
    texts: dict[str, list[str]] = {}
    for n in diff.get("added_nodes") or []:
        texts.setdefault(n["id"], []).extend([n.get("label") or "", json.dumps(n.get("params") or {}, ensure_ascii=False)])
    for c in diff.get("changed_params") or []:
        texts.setdefault(c["node_id"], []).append(json.dumps(c.get("after") or {}, ensure_ascii=False))
    for r in diff.get("renamed") or []:
        texts.setdefault(r["node_id"], []).append(r.get("after") or "")
    return sorted(i for i, parts in texts.items() if any(contains_private(t, needles) for t in parts))


def _plan_target(db: Session, op: AiOperation, target: dict | None) -> tuple[Workflow, WorkflowRun | None, str | None]:
    """(part workflow, run, focus node id) of a workflow_edit target. A workflow (or a node of it) is planned itself; a
    run (or a node of it) plans the run's part workflow for its NEXT run (AI work panel, design §2.2): 409
    workflow_edit_part_only when the run was not started from a part workflow. 422 target_required otherwise."""
    kind = (target or {}).get("kind")
    node = (target or {}).get("node")
    focus = str(node["node_id"]) if isinstance(node, dict) and node.get("node_id") else None
    if kind == "workflow":
        return _edit_workflow(db, op.project_id, (target or {}).get("object_id")), None, focus
    if kind == "run":
        rid = _uuid((target or {}).get("object_id"))
        run = db.get(WorkflowRun, rid) if rid else None
        if run is None or run.project_id != op.project_id:
            raise not_found("run")
        if run.workflow_id is None:
            raise conflict("workflow_edit_part_only", "this run was not started from a part workflow; AI plans change part workflows only",
                           run_id=str(run.id))
        return _edit_workflow(db, op.project_id, run.workflow_id), run, focus
    raise AppError(422, "target_required", "choose the part workflow (or a run of it) to change (add it as the target)",
                   {"expected": ["workflow", "run"]})


def _with_blocks(build: Any, blocks: list[tuple[str, str]], ref_key: str | None, *,
                 insert: Callable[[str, list[str]], str] | None = None) -> None:
    """Blocks the AI work panel adds to a plan's user turn (focus node, run, base of a new workflow): placed after the
    workflow block (or where ``insert`` puts them: a main-agent turn's <work> / <event>) and listed in the build's
    manifest as required items (their tokens counted)."""
    from ..ai import workflow_edit_agent
    from . import assistant_context as actx

    if not blocks or not build.ok:
        return
    build.context_text = (insert or workflow_edit_agent.with_blocks)(build.context_text, [b for _, b in blocks])
    for label, text in blocks:
        item = actx.ContextItem(layer="required", source="target", label=label, text=text, tokens_est=actx.estimate_tokens(text),
                                included=True, ref_key=ref_key)
        build.items.append(item)
        for k in ("required_est", "used_est"):
            build.budget[k] = int(build.budget.get(k) or 0) + item.tokens_est
    required, available = int(build.budget.get("required_est") or 0), int(build.budget.get("available") or 0)
    if required > available:  # the blocks are required: a build they push over the budget fails like any other
        build.ok = False
        build.error = actx._over_budget("workflow_edit", build.items, required, available, limit="input", model_label=build.model_label)


def _plan_on(db: Session, op: AiOperation, build: Any, base_graph: dict) -> tuple[AiOperation, Any] | None:
    """Record the build, then ask the workflow edit agent for a plan on ``base_graph``. The build is committed before
    the model call; no row lock is held during it; the calls are recorded whatever the outcome. Returns (locked
    operation, outcome) for a plan, or None when the operation was finished here (failed build / plan, not pending)."""
    from ..ai import workflow_edit_agent
    from ..ai.providers.base import ProviderError
    from . import ai as ai_svc
    from . import assistant_context as actx

    req = op.request or {}
    actx.record_build(db, op, build)
    if not build.ok:
        op.status = "failed"
        op.error = build.error or {"code": "context_over_budget", "message": "the required context does not fit the model", "details": {}}
        _on_terminal(db, op)
        db.commit()
        return None
    db.commit()  # the manifest stays on record whatever the model call does
    try:
        provider, _ = ai_svc.provider_for(db, "assistant", _model_ref(req))
    except ProviderError as exc:
        raise AppError(503, "ai_unavailable", "the AI model could not plan the change; check the model settings",
                       {"reason": exc.code, "detail": exc.message})
    from .assistant_cad_tools import stop_guard, bind_stop, _save_lock
    bind_stop(provider, stop_guard(op))
    db.commit()
    outcome = workflow_edit_agent.run_workflow_edit(provider, build.system_text, build.context_text, base_graph, timeout_s=90,
                                                    max_repairs=WORKFLOW_EDIT_REPAIRS)
    ai_svc.record_calls(db, list(outcome.calls or []), None, ai_operation_id=op.id)
    db.commit()  # A late cancellation/revocation must not discard a completed provider call.
    provider.check_live()
    op = _save_lock(db, db.get(User, op.user_id), op)
    if op.status != "pending":
        db.commit()
        return None
    op.simulated = bool(outcome.simulated)
    if outcome.error:
        op.status, op.error = "failed", outcome.error
        _on_terminal(db, op)
        db.commit()
        return None
    return op, outcome


def _private_warning(build: Any, text: str, diff: dict | None) -> list[dict]:
    private = _private_in_plan(build, text, diff)
    if not private:
        return []
    return [{"code": "private_context", "message": "the plan copies text from your private conversation context (pinned "
             "constraints, memory or other references); applying it makes that text visible to the project", "node_ids": private}]


def _run_workflow_edit(db: Session, user: User, op: AiOperation, ctx: dict) -> None:
    """Plan a change of the target part workflow (read-only). AI work panel: a workflow / run NODE target is the plan's
    focus node (its block tells the model what "this node" is); a run target plans the run's part workflow from its
    current version for the next run (warning run_unchanged; the run itself never changes)."""
    from ..ai import workflow_edit_agent
    from . import assistant_context as actx

    req = op.request or {}
    text: str = req.get("text") or ""
    target: dict | None = req.get("target")
    project_access(db, user, op.project_id, "workflow")
    w, run, focus_id = _plan_target(db, op, target)
    ctx["part_id"] = w.part_id
    part = db.get(Part, w.part_id)
    if part is None or part.archived_at is not None:
        raise bad_request("part_archived", "the part is archived")
    base_graph, base_version = copy.deepcopy(w.graph or {}), w.version
    wid, name, part_id = w.id, w.name, w.part_id
    focus = refs.graph_node(base_graph, focus_id) if focus_id else None
    blocks: list[tuple[str, str]] = []
    if focus is not None:
        blocks.append((f"Focus node {focus_id}", workflow_edit_agent.focus_node_block(focus)))
    from_run: dict | None = None
    if run is not None:
        # The run's workflow at its current version is what the plan changes (the model reads its facts as the target).
        pinned = run.workflow_version
        from_run = {"run_id": str(run.id), "workflow_version": run.workflow_version}
        blocks.append((f"Run {run.title or str(run.id)[:8]}", workflow_edit_agent.run_context_block(run.title or name, pinned, base_version)))
        build_target = refs.workflow_ref(db, w)
    else:
        pin = (target or {}).get("version")
        pinned, build_target = (pin if isinstance(pin, int) else None), target
    op.part_id = part_id
    conv = db.get(AiConversation, op.conversation_id)
    build = actx.build_context(db, conversation=conv, kind="workflow_edit", text=text, target=build_target,
                               references=list(req.get("references") or []), constraints=list(req.get("constraints") or []),
                               model_config_id=_model_ref(req), materials=material_keys(req), material_notes=ctx.get("material_notes"))
    _with_blocks(build, blocks, (target or {}).get("key"))
    planned = _plan_on(db, op, build, base_graph)
    if planned is None:
        return
    op, outcome = planned
    warnings: list[dict] = []
    if from_run is not None:
        warnings.append({"code": "run_unchanged", "message": "this run keeps its graph and results; the plan changes the workflow for "
                         "the next run"})
    warnings += list(outcome.warnings)
    if pinned is not None and pinned != base_version:
        used = f"the run used v{pinned}" if from_run is not None else f"the workflow was referenced as v{pinned}"
        warnings.append({"code": "workflow_moved_on", "message": f"{used}; the plan is based on its current version v{base_version}",
                         "pinned_version": pinned, "base_version": base_version})
    if focus_id is not None and focus is None:
        warnings.append({"code": "focus_node_missing", "message": f"node {focus_id} is not in the current version v{base_version}; "
                         "the plan is for the whole workflow", "node_id": focus_id})
    warnings += _private_warning(build, text, outcome.diff)
    op.result = {
        "kind": "workflow_edit", "workflow_id": str(wid), "workflow_name": name, "part_id": str(part_id),
        "base_version": base_version, "pinned_version": pinned, "summary": outcome.summary, "changes": outcome.changes,
        "diff": outcome.diff, "graph_before": base_graph, "graph_after": outcome.graph_after, "warnings": warnings,
        "repairs": outcome.repairs, "application": None,
        "focus_node_id": focus_id if focus is not None else None, "from_run": from_run,
    }
    op.status = "succeeded"
    _on_terminal(db, op)
    db.commit()


# --- workflow_create (AI work panel, .trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §2.5) --------------------------
#
# A new part workflow planned from a built-in template chosen by the referenced file (deterministic), with the file
# version filled in by the server, then adjusted by the workflow edit agent to the request (no change is a success).
# Succeeded = plan ready; POST /ai/operations/{id}/apply creates the workflow (no undo: archive it instead).


def _file_version_of(db: Session, op: AiOperation, ref: dict) -> FileVersion:
    """The file version a file reference stands for (a DXF / DWG document version: its file); 404 outside the project."""
    oid = _uuid(ref.get("object_id"))
    if ref.get("kind") == "file_version":
        fv = db.get(FileVersion, oid) if oid else None
    else:
        v = db.get(AssetVersion, oid) if oid else None
        fv = db.get(FileVersion, v.file_version_id) if v is not None and v.file_version_id else None
    f = db.get(File, fv.file_id) if fv else None
    if fv is None or f is None or f.project_id != op.project_id:
        raise not_found("file version")
    return fv


def _create_base(db: Session, op: AiOperation, items: list[dict]) -> tuple[str, dict]:
    """(template key, base graph): a DXF / DWG → drawing_model_to_quote; a PDF / image drawing → drawing_to_quote; a STEP
    file → design_to_quote with its text request replaced by a file input into the model; else design_to_quote. The
    file version is filled into the file input here (never by the model)."""
    from ..workflow.graph import validate_graph
    from ..workflow.templates import template_graph

    def filled(key: str, node_id: str, ref: dict) -> tuple[str, dict]:
        g = template_graph(key)
        fv = _file_version_of(db, op, ref)
        for n in g["nodes"]:
            if n["id"] == node_id:
                n["params"] = {**(n.get("params") or {}), "file_version_id": str(fv.id)}
        return key, g

    cad = next((r for r in items if refs.cad_drawing_format(r) is not None), None)
    pdf = next((r for r in items if r.get("kind") == "file_version" and r.get("file_kind") in refs.DRAWING_FILE_KINDS), None)
    step = next((r for r in items if refs.is_step_file(r)), None)
    if cad is not None:
        key, graph = filled("drawing_model_to_quote", "drawing_file", cad)
    elif pdf is not None:
        key, graph = filled("drawing_to_quote", "drawing_file", pdf)
    elif step is not None:
        key, graph = "design_to_quote", template_graph("design_to_quote")
        fv = _file_version_of(db, op, step)
        graph["nodes"] = [{"id": "model_file", "type": "file_input", "position": n["position"], "params": {"file_version_id": str(fv.id)}}
                          if n["id"] == "request" else n for n in graph["nodes"]]
        graph["edges"] = [{"id": "model_file.file->model.file", "source": "model_file", "source_port": "file", "target": "model",
                           "target_port": "file"} if e["source"] == "request" else e for e in graph["edges"]]
        for grp in graph.get("groups") or []:
            grp["node_ids"] = ["model_file" if i == "request" else i for i in grp["node_ids"]]
    else:
        key, graph = "design_to_quote", template_graph("design_to_quote")
    g, errors = validate_graph(graph)
    if g is None:  # pragma: no cover - the templates are checked at import
        raise AppError(422, "invalid_graph", "the template graph is not valid", {"errors": errors})
    return key, g.model_dump(mode="json")


def _create_part(db: Session, op: AiOperation, target: dict | None, references: list[dict], task_id: Any) -> Part:
    """The part of a new workflow: the target's, else the first reference's, else the request task's (422 part_required)."""
    part_id = _first_part_ref(target, references)
    if part_id is None and _uuid(task_id) is not None:
        t = db.get(Task, _uuid(task_id))
        part_id = t.part_id if t is not None else None
    if part_id is None:
        raise AppError(422, "part_required", "choose the part the workflow is for (add the part, or a file of it, as a reference)")
    part = db.get(Part, part_id)
    if part is None or part.project_id != op.project_id:
        raise not_found("part")
    if part.archived_at is not None:
        raise bad_request("part_archived", "the part is archived")
    return part


def _run_workflow_create(db: Session, user: User, op: AiOperation, ctx: dict) -> None:
    """Plan a new part workflow (read-only): the part, the base template and its file input are decided here; the
    workflow edit agent adjusts the base to the request (the base is its <target>, keyed template:<key>)."""
    from ..ai import workflow_edit_agent
    from ..workflow.templates import TEMPLATES
    from . import assistant_context as actx

    req = op.request or {}
    text: str = req.get("text") or ""
    target: dict | None = req.get("target")
    references: list[dict] = list(req.get("references") or [])
    project_access(db, user, op.project_id, "workflow")
    part = _create_part(db, op, target, references, req.get("task_id"))
    ctx["part_id"] = op.part_id = part.id
    items = [r for r in ([target] if target else []) + references if isinstance(r, dict)]
    key, base_graph = _create_base(db, op, items)
    zh = bool(re.search(r"[㐀-鿿]", text))
    tname = TEMPLATES[key]["name"]
    name = f"{part.number} · {tname['zh' if zh else 'en']}"[:200]
    head = (f'New part workflow "{name}" for part {part.number} · {part.name}, made from the built-in template {key} '
            f'("{tname["en"]}"); not saved yet.')
    block = workflow_edit_agent.template_target_block(key, name, head, base_graph)
    part_id, part_number = part.id, part.number
    conv = db.get(AiConversation, op.conversation_id)
    # The request's target and references are reference material here; the base workflow is the plan's target.
    build = actx.build_context(db, conversation=conv, kind="workflow_edit", text=text, target=None, references=items,
                               constraints=list(req.get("constraints") or []), model_config_id=_model_ref(req),
                               materials=material_keys(req), material_notes=ctx.get("material_notes"))
    _with_blocks(build, [(f"New workflow from the template {key}", block)], None)
    planned = _plan_on(db, op, build, base_graph)
    if planned is None:
        return
    op, outcome = planned
    op.result = {
        "kind": "workflow_create", "part_id": str(part_id), "part_number": part_number, "name": name, "template_key": key,
        "summary": outcome.summary, "changes": outcome.changes, "diff": outcome.diff, "graph_after": outcome.graph_after,
        "warnings": list(outcome.warnings) + _private_warning(build, text, outcome.diff), "repairs": outcome.repairs,
        "application": None,
    }
    op.status = "succeeded"
    _on_terminal(db, op)
    db.commit()


def _plan_of(op: AiOperation) -> dict:
    res = op.result if isinstance(op.result, dict) else None
    if op.kind != "workflow_edit":
        raise conflict("not_workflow_edit", "only a workflow edit plan can be applied or undone", kind=op.kind)
    if op.status != "succeeded" or res is None or res.get("kind") != "workflow_edit":
        raise conflict("plan_not_ready", "this request has no plan to apply", status=op.status)
    return res


def _lock_plan_workflow(db: Session, user: User, op: AiOperation, res: dict) -> tuple[Workflow, ProjectAccess]:
    """The plan's workflow, locked, after the same permission check as PUT /workflows/{id} (a part workflow
    needs the project "workflow" action now)."""
    from ..api.workflows import _workflow_access
    from . import templates as tpl
    from .lifecycle import require_active

    project_access(db, user, op.project_id, "workflow")
    require_active(db, op.project_id, what="applying a workflow plan")
    wid = _uuid(res.get("workflow_id"))
    if wid is None:
        raise not_found("workflow")
    w, acc = _workflow_access(db, user, wid, "template")
    if w.part_id:
        part = db.scalar(select(Part).where(Part.id == w.part_id).with_for_update().execution_options(populate_existing=True))
        if part is None or part.project_id != op.project_id or part.archived_at is not None:
            raise not_found("part")
    w = tpl.lock_workflow(db, w.id)
    return _edit_workflow(db, op.project_id, w.id), acc


def _repin_composer(db: Session, conversation_id: uuid.UUID, w: Workflow) -> AiConversation:
    """After an apply / undo by the conversation's owner (or a save in the work panel, ``repin_composer``), the
    composer's references to this workflow follow its new version (the target stays the target), so the next request
    is not planned against a reference it just made stale. Node references follow too while their node is still in the
    graph; one whose node is gone stays as it is (it reads unavailable, node_removed). Request snapshots never change."""
    conv = lock_conversation(db, conversation_id)
    target_key, items = _composer(conv)
    out: list[dict] = []
    changed = False
    for r in items:
        if r.get("kind") == "workflow" and r.get("object_id") == str(w.id):
            node = r.get("node") if isinstance(r.get("node"), dict) else None
            node_id = str(node.get("node_id")) if node and node.get("node_id") else None
            if node_id is None or refs.graph_node(w.graph, node_id) is not None:
                fresh = refs.workflow_ref(db, w, node_id)
                if r.get("key") != fresh["key"]:
                    target_key = fresh["key"] if target_key == r.get("key") else target_key
                    changed = True
                    r = fresh
        if not any(x.get("key") == r.get("key") for x in out):
            out.append(r)
    if changed:
        _set_composer(conv, target_key, out)
    return conv


def repin_composer(db: Session, user: User, conversation_id: uuid.UUID, workflow_id: uuid.UUID) -> AiConversation:
    """POST /ai/conversations/{id}/composer/repin (AI work panel): the composer's references to this workflow and its nodes
    follow its current version (after the panel saved it). Owner only, open conversations. Flushes; the caller commits."""
    conv, _ = load_conversation(db, user, conversation_id)
    require_open(conv)
    w = db.get(Workflow, workflow_id)
    if w is None or w.project_id != conv.project_id:
        raise not_found("workflow")
    conv = _repin_composer(db, conv.id, w)
    require_open(conv)
    db.flush()
    return conv


def _checked_graph(raw: Any) -> dict:
    from ..workflow.graph import validate_graph

    features.guard_graph(raw or {})
    g, errors = validate_graph(raw or {})
    if g is None:
        raise AppError(422, "invalid_graph", "the planned graph is no longer valid; ask for a new plan", {"errors": errors})
    return g.model_dump(mode="json")


def apply_workflow_edit(db: Session, user: User, operation_id: uuid.UUID, *,
                        expected_version: int | None = None, plan_hash: str | None = None) -> tuple[AiOperation, Workflow, ProjectAccess]:
    """Write the planned graph as the workflow's next version. 409 already_applied / already_undone /
    nothing_to_apply; 409 workflow_changed when the workflow is no longer at the plan's base version (or at
    ``expected_version``): a plan is never rebased silently. The composer's reference follows the new version.
    Flushes; the caller commits."""
    from .audit import audit

    op, conv, _ = load_operation(db, user, operation_id)
    require_open(conv)
    w, acc = _lock_plan_workflow(db, user, op, _plan_of(op))
    op = _lock_op(db, op.id)
    require_open(lock_conversation(db, op.conversation_id))
    from .assistant_cad_tools import check_plan
    check_plan(op, plan_hash=plan_hash, expected_version=expected_version)
    res = _plan_of(op)
    app = res.get("application") if isinstance(res.get("application"), dict) else {}
    if app.get("state") == "applied":
        if not res.get("plan_hash"):
            raise conflict("already_applied", "this plan was already applied", version_after=app.get("version_after"))
        return op, w, acc
    if app.get("state") == "undone":
        raise conflict("already_undone", "this plan was applied and undone; ask for a new plan", undo_version=app.get("undo_version"))
    if (res.get("diff") or {}).get("empty", True):
        raise conflict("nothing_to_apply", "the plan changes nothing")
    base = res.get("base_version")
    if w.version != base or (expected_version is not None and expected_version != w.version):
        raise conflict("workflow_changed", "the workflow changed after this plan was made; ask for a new plan",
                       current_version=w.version, base_version=base, expected_version=expected_version)
    graph = _checked_graph(res.get("graph_after"))
    before = w.version
    w.graph, w.version = graph, before + 1
    audit(db, action="ai.workflow_edit.apply", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=w.project_id,
          data={"operation_id": str(op.id), "version_before": before, "version_after": w.version,
                "changes": len(res.get("changes") or []), "simulated": bool(op.simulated)})
    op.result = {**res, "application": {"state": "applied", "by": str(user.id), "at": _now().isoformat(),
                                        "version_before": before, "version_after": w.version, "plan_hash": res.get("plan_hash"),
                                        "expected_version": expected_version}}
    from . import operation_events
    operation_events.emit(db, op, phase="applied")
    _repin_composer(db, op.conversation_id, w)
    _touch_conversations(db, {op.conversation_id})
    db.flush()
    return op, w, acc


def undo_workflow_edit(db: Session, user: User, operation_id: uuid.UUID) -> tuple[AiOperation, Workflow, ProjectAccess]:
    """Restore the plan's base graph as a NEW workflow version (history is never rewritten), only while the
    workflow is still at the version the plan produced (409 workflow_changed otherwise; not_applied /
    already_undone). Flushes; the caller commits."""
    from .audit import audit

    op, conv, _ = load_operation(db, user, operation_id)
    require_open(conv)
    w, acc = _lock_plan_workflow(db, user, op, _plan_of(op))
    op = _lock_op(db, op.id)
    require_open(lock_conversation(db, op.conversation_id))
    res = _plan_of(op)
    app = res.get("application") if isinstance(res.get("application"), dict) else {}
    if app.get("state") == "undone":
        if not res.get("plan_hash"):
            raise conflict("already_undone", "this plan was already undone", undo_version=app.get("undo_version"))
        return op, w, acc
    if app.get("state") != "applied":
        raise conflict("not_applied", "this plan has not been applied")
    if w.version != app.get("version_after"):
        raise conflict("workflow_changed", "the workflow changed after the plan was applied; change it in the workflow editor instead",
                       current_version=w.version, applied_version=app.get("version_after"))
    graph = _checked_graph(res.get("graph_before"))
    before = w.version
    w.graph, w.version = graph, before + 1
    audit(db, action="ai.workflow_edit.undo", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=w.project_id,
          data={"operation_id": str(op.id), "version_before": before, "version_after": w.version,
                "restored_version": res.get("base_version")})
    op.result = {**res, "application": {**app, "state": "undone", "undone_by": str(user.id), "undone_at": _now().isoformat(),
                                        "undo_version": w.version}}
    from . import operation_events
    operation_events.emit(db, op, phase="undone")
    _repin_composer(db, op.conversation_id, w)
    _touch_conversations(db, {op.conversation_id})
    db.flush()
    return op, w, acc


def apply_workflow_create(db: Session, user: User, operation_id: uuid.UUID, *, plan_hash: str | None = None) -> tuple[AiOperation, Workflow, ProjectAccess]:
    """Create the planned part workflow (same permission and validation as POST /projects/{id}/workflows with a part).
    In the same transaction: the plan records its application, a workflow ``card`` message follows in the conversation
    and the new workflow becomes the composer's target. 409 already_applied / plan_not_ready. There is no undo (archive
    the workflow instead). Lock order: project → part → operation → conversation. Flushes; the caller commits."""
    from ..api.workflows import _check_part
    from . import templates as tpl
    from .audit import audit
    from .lifecycle import require_active

    op, conv, _ = load_operation(db, user, operation_id)
    require_open(conv)
    acc = project_access(db, user, op.project_id, "workflow")
    require_active(db, op.project_id, what="applying a workflow plan")
    part_id = _uuid((op.result or {}).get("part_id"))
    db.scalar(select(Part).where(Part.id == part_id).with_for_update().execution_options(populate_existing=True))
    part = _check_part(db, op.project_id, part_id)  # type: ignore[arg-type]
    op = _lock_op(db, op.id)
    conv = lock_conversation(db, op.conversation_id)
    require_open(conv)
    from .assistant_cad_tools import check_plan
    check_plan(op, plan_hash=plan_hash)
    res = op.result if isinstance(op.result, dict) else None
    if op.status != "succeeded" or res is None or res.get("kind") != "workflow_create":
        raise conflict("plan_not_ready", "this request has no plan to apply", status=op.status)
    app = res.get("application") if isinstance(res.get("application"), dict) else {}
    if app.get("state") == "applied":
        if not res.get("plan_hash"):
            raise conflict("already_applied", "this plan was already applied", workflow_id=app.get("workflow_id"))
        w = db.get(Workflow, _uuid(app.get("workflow_id")))
        if w is None:
            raise not_found("workflow")
        return op, w, acc
    graph = _checked_graph(res.get("graph_after"))
    w = Workflow(project_id=op.project_id, name=str(res.get("name") or part.number)[:200], description="", graph=graph,
                 template_key=res.get("template_key"), policy=tpl.normalize_policy(None), created_by=user.id, part_id=part.id)
    db.add(w)
    db.flush()
    audit(db, action="ai.workflow_create.apply", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=w.project_id,
          data={"operation_id": str(op.id), "template_key": w.template_key, "changes": len(res.get("changes") or []),
                "simulated": bool(op.simulated)})
    op.result = {**res, "application": {"state": "applied", "workflow_id": str(w.id), "version": w.version, "by": str(user.id),
                                        "at": _now().isoformat(), "plan_hash": res.get("plan_hash")}}
    from . import operation_events
    operation_events.emit(db, op, phase="applied")
    conv = lock_conversation(db, op.conversation_id)
    _append(db, conv, role="assistant", kind="card", text=f"已创建流程：{w.name}",
            payload={"object": {"kind": "workflow", "id": str(w.id)}, "origin": "workflow_created"})
    target_key, items = _composer(conv)
    ref = refs.workflow_ref(db, w)
    if len(items) >= MAX_REFERENCES:  # room for the new workflow: the oldest reference that is not the target goes
        drop = next((i for i, r in enumerate(items) if r.get("key") != target_key), 0)
        items.pop(drop)
    _set_composer(conv, ref["key"], [*items, ref])
    _touch_conversations(db, {op.conversation_id})
    db.flush()
    return op, w, acc


# --- runs started from the conversation (AI work panel, design §2.4) ---------------------------------------------


def _card_for(db: Session, conversation_id: uuid.UUID, crid: uuid.UUID) -> AiMessage | None:
    return db.scalar(select(AiMessage).where(AiMessage.conversation_id == conversation_id, AiMessage.kind == "card",
                                             AiMessage.payload["client_request_id"].astext == str(crid)).limit(1))


def _card_out(db: Session, user: User, msg: AiMessage, run: WorkflowRun | None = None) -> dict:
    from ..api.workflows import run_out

    if run is None:
        rid = _uuid(((msg.payload or {}).get("object") or {}).get("id"))
        run = db.get(WorkflowRun, rid) if rid else None
    return {"message": message_out(msg, {}), "run": run_out(db, user, run) if run is not None else None}


def _replayed_card(db: Session, user: User, msg: AiMessage, workflow_id: uuid.UUID, policy: str | None) -> dict:
    """The stored outcome of the same client_request_id; 409 idempotency_conflict when it asked for something else."""
    p = msg.payload or {}
    rid = _uuid((p.get("object") or {}).get("id"))
    run = db.get(WorkflowRun, rid) if rid else None
    if p.get("workflow_id") != str(workflow_id) or (policy and run is not None and run.execution_policy != policy):
        raise conflict("idempotency_conflict", "this client_request_id was already used to start another run",
                       client_request_id=p.get("client_request_id"), workflow_id=p.get("workflow_id"))
    return _card_out(db, user, msg, run)


def start_conversation_run(db: Session, user: User, conversation_id: uuid.UUID, *, workflow_id: uuid.UUID, client_request_id: uuid.UUID,
                           execution_policy: str | None = None) -> tuple[dict, bool]:
    """POST /ai/conversations/{id}/workflow-runs: start a run of a part workflow of the conversation's project (409
    workflow_edit_part_only otherwise) through the same checks and start as POST /workflows/{id}/runs, and follow it
    with a ``card`` message, both in one transaction. The same client_request_id returns the same message and run
    (created False). The run records the conversation in ``inputs.ai_conversation_id`` (tracing only). Commits.
    Lock order: project (share, start_run) → conversation."""
    from ..api.workflows import start_run
    from . import templates as tpl

    conv, _ = load_conversation(db, user, conversation_id)
    found = _card_for(db, conv.id, client_request_id)
    if found is not None:
        out = _replayed_card(db, user, found, workflow_id, execution_policy)
        db.commit()
        return out, False
    require_open(conv)
    project_access(db, user, conv.project_id, "workflow")
    features.require_choices(execution_policy=execution_policy)
    w = _edit_workflow(db, conv.project_id, workflow_id)
    graph, version = tpl.resolve_run(db, user, w, template_version_id=None, draft=False, params={}, task=None)
    run = start_run(db, user, project_id=w.project_id, graph_raw=graph, part_id=w.part_id, title="", origin="workflow", workflow=w,
                    template_version=version, execution_policy=execution_policy)
    run.inputs = {**(run.inputs or {}), "ai_conversation_id": str(conv.id)}
    conv = lock_conversation(db, conv.id)
    require_open(conv)
    found = _card_for(db, conv.id, client_request_id)
    if found is not None:  # the same request won the race: its run stands, this one is never written
        db.rollback()
        out = _replayed_card(db, user, found, workflow_id, execution_policy)
        db.commit()
        return out, False
    msg = _append(db, conv, role="assistant", kind="card", text=f"已开始运行：{run.title or w.name}",
                  payload={"object": {"kind": "run", "id": str(run.id)}, "origin": "run_started",
                           "client_request_id": str(client_request_id), "workflow_id": str(w.id)})
    conv.last_activity_at = _now()
    db.commit()
    return _card_out(db, user, msg, run), True


# --- status sync (design §4.3) ---------------------------------------------------------------


def _public_error(err: dict | None) -> dict | None:
    from .failure_causes import public_error

    return public_error(err)


def _simulated(nodes: list[NodeRun]) -> bool:
    for n in nodes:
        out = n.outputs or {}
        if out.get("simulated") or (isinstance(out.get("spec"), dict) and out["spec"].get("simulated")):
            return True
        if isinstance(n.error, dict) and isinstance(n.error.get("details"), dict) and n.error["details"].get("simulated"):
            return True  # e.g. drawing_reconstruct refused in simulated mode (simulation_unsupported)
    return False


def _result(db: Session, op: AiOperation, run: WorkflowRun, acc: ProjectAccess | None, nodes: list[NodeRun] | None = None) -> dict:
    """§4.4: result items from the run's outputs (exact asset versions), with base and allowed actions
    (the owner's project capabilities decide; the real checks stay in the action endpoints). Model
    operations also carry the requirement check of the modeling node (status, requirement counts,
    uncovered notes; ``check``) and each model item its revision's ``check_status``."""
    from .revisions import check_status
    rows = db.execute(select(RunOutput, NodeRun.node_id).join(NodeRun, NodeRun.id == RunOutput.node_run_id)
                      .where(RunOutput.run_id == run.id).order_by(RunOutput.created_at, NodeRun.node_id, RunOutput.output_key)).all()
    base = (op.request or {}).get("target") if op.kind != "model_create" else None
    can_write = _can(acc, "workflow")
    can_design = _can(acc, "design")
    items: list[dict] = []
    seen: set[uuid.UUID] = set()
    for o, node_id in rows:
        if o.asset_version_id in seen:
            continue
        seen.add(o.asset_version_id)
        v = db.get(AssetVersion, o.asset_version_id)
        asset = db.get(Asset, v.asset_id) if v else None
        if v is None or asset is None:
            continue
        if asset.type == "part_model" and v.model_revision_id:
            rev = db.get(ModelRevision, v.model_revision_id)
            ref = refs.revision_ref(db, rev) if rev else refs.asset_version_ref(db, v)
        elif asset.type == "assembly_model" and v.assembly_revision_id:  # W8: a version-pinned assembly revision reference
            ref = refs.assembly_revision_ref(db, v.assembly_revision_id) or refs.asset_version_ref(db, v)
        else:
            ref = refs.asset_version_ref(db, v)
        actions = list(RESULT_ACTIONS)
        if asset.type not in ("part_model", "assembly_model") or not can_design:
            actions.remove("continue")
        if v.availability != "ready" or not can_write:
            actions = [a for a in actions if a not in ("use_as_input", "add_deliverable")]
        item = {"ref": ref, "asset_version_id": str(v.id), "asset_id": str(asset.id), "asset_type": asset.type,
                "label": ref.get("version_label") or "", "base": base, "run_id": str(run.id), "node_id": node_id,
                "output_key": o.output_key, "availability": v.availability, "publish_state": v.publish_state,
                "labels": list(v.labels or []), "actions": actions}
        if asset.type == "part_model" and v.model_revision_id:
            rev = db.get(ModelRevision, v.model_revision_id)
            item["check_status"] = check_status(rev) if rev else None
        items.append(item)
    kind = "model" if op.kind in MODEL_KINDS else {"drawing_reconstruct": "reconstruct", "drawing_generate": "drawing",
                                                   "assembly_create": "assembly"}.get(op.kind, "intake")
    out: dict[str, Any] = {"kind": kind, "items": items}
    if (op.result or {}).get("drawing_config_plan"):
        out["drawing_config_plan"] = copy.deepcopy(op.result["drawing_config_plan"])
    if op.kind == "assembly_create":  # W8: every chain step with its value, the assumptions and the open questions
        from .assembly_ai import chain_result

        out["chain"] = chain_result(db, op, run, list(nodes or []))
    if op.kind in PART_KINDS:
        check = next((n.outputs.get("requirements_check") for n in nodes or []
                      if n.node_type in ("model_3d", "drawing_reconstruct") and isinstance(n.outputs, dict)
                      and n.outputs.get("requirements_check")), None)
        out["check"] = check
    if op.kind in DRAWING_KINDS:
        # The drawing node's output as the node returns it (drawing id, page-1 aliases, pages / manifest when present).
        out["drawings"] = [dict(n.outputs["drawing"]) for n in nodes or []
                           if n.node_type == "drawing" and n.status == "succeeded" and isinstance((n.outputs or {}).get("drawing"), dict)]
    if op.kind == "drawing_reconstruct":
        out["source"] = next((n.outputs.get("reconstruct") for n in nodes or []
                              if n.node_type == "drawing_reconstruct" and isinstance(n.outputs, dict) and n.outputs.get("reconstruct")), None)
    return out


def _waiting_question(op: AiOperation, run: WorkflowRun, nodes: list[NodeRun], acc: ProjectAccess | None) -> dict | None:
    for n in nodes:
        if n.status != "waiting_human":
            continue
        out = n.outputs or {}
        if n.node_type in ("model_3d", "drawing_reconstruct"):
            clar = out.get("clarification") or {}
            # gaps: the critical requirement fields the server gate found missing / assumed (empty when the model asked).
            return {"kind": "clarification", "questions": list(clar.get("questions") or []), "node_id": n.node_id,
                    "can_answer": _can(acc, "design"), "gaps": list(clar.get("gaps") or [])}
        if n.node_type == "drawing_intake":
            me = out.get("manual_entry") or {}
            return {"kind": "manual_entry", "reason": me.get("reason"), "node_id": n.node_id, "run_path": f"/runs/{run.id}",
                    "file_version_id": me.get("file_version_id")}
    return None


def sync_operation(db: Session, op: AiOperation, *, defer: set[uuid.UUID] | None = None) -> AiOperation:
    """Follow the run of an unfinished operation (idempotent; locks the operation row).
    Several in one transaction: pass `defer` and call _touch_conversations afterwards."""
    from . import orchestrator

    if op.status not in ACTIVE:
        return op
    if orchestrator.is_turn(op):
        return orchestrator.sync_turn(db, op, defer=defer)  # AI native: a main-agent turn has no run (its own stale rules)
    if op.run_id is None:
        generation = op.generation
        op = _lock_op(db, op.id)
        if op.status not in ACTIVE or op.run_id is not None or op.generation != generation:
            return op
        if op.kind == "spec_extract":
            from .assistant_cad_tools import sync_spec_operation

            if sync_spec_operation(db, op, defer=defer):
                return op
        fixed = op.kind in ("spec_extract", "drafting_create", "drafting_modify", "workflow_edit", "workflow_create", "assembly_modify") or (
            op.kind == "drawing_generate" and (op.request or {}).get("tool_input", {}).get("requirements"))
        if fixed and op.status == "running" and op.updated_at >= _now() - STALE_PENDING:
            return op
        if op.status == "pending" or fixed:
            # Normally its business step is running right now; only a long-dead one is closed.
            if op.created_at is None or op.created_at >= _now() - STALE_PENDING:
                return op
            error = {"code": "interrupted", "message": "the request was interrupted before its work started; retry it", "details": {}}
        else:  # running / waiting_input whose run was deleted
            error = {"code": "run_missing", "message": "the run of this operation no longer exists", "details": {}}
        op.status, op.error = "failed", error
        _on_terminal(db, op, defer=defer)
        return op
    op = _lock_op(db, op.id)
    if op.status not in ACTIVE or op.run_id is None:
        return op
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == op.run_id).execution_options(populate_existing=True))
    if run is None:
        op.status = "failed"
        op.error = {"code": "run_missing", "message": "the run of this operation no longer exists", "details": {}}
        _on_terminal(db, op, defer=defer)
        return op
    nodes = list(db.scalars(select(NodeRun).where(NodeRun.run_id == run.id).order_by(NodeRun.node_id)
                            .execution_options(populate_existing=True)).all())
    acc = _access_of(db, op)
    status, question, result, error = op.status, op.question, op.result, op.error
    if run.status in ("queued", "running"):
        status, question = "running", None
    elif run.status == "waiting":
        q = _waiting_question(op, run, nodes, acc)
        status, question = ("waiting_input", q) if q is not None else ("running", None)
    elif run.status == "succeeded":
        status, question, result = "succeeded", None, _result(db, op, run, acc, nodes)
    elif run.status in ("failed", "partial"):
        # assembly_create: the first failure in chain order (the drawing step before the delivery it made partial)
        ordered = sorted(nodes, key=lambda n: ("spec", "assembly", "drawings", "delivery").index(n.node_id)
                         if n.node_id in ("spec", "assembly", "drawings", "delivery") else 9) if op.kind == "assembly_create" else nodes
        failed = next((n for n in ordered if n.status == "failed"), None)
        status, question = "failed", None
        error = _public_error(failed.error if failed else None) or _public_error(run.error) or {
            "code": "run_failed", "message": "the run failed", "details": {}}
        if run.status == "partial" or op.kind == "assembly_create":
            # Some products really exist (e.g. the model, not its drawing): shown with the failure, never as success.
            # assembly_create: even a failed chain shows its steps (a failed spec version keeps its error record).
            result = _result(db, op, run, acc, nodes)
            error = {**error, "details": {**(error.get("details") or {}), "run_status": "partial",
                                          "failed_node": failed.node_id if failed else None}}
    elif run.status == "cancelled":
        status, question = "cancelled", None
    if status in ("failed", "cancelled"):
        saved = _result(db, op, run, acc, nodes)
        if saved.get("items"):
            result = {**(result or {}), **saved}
    simulated = _simulated(nodes) or op.simulated
    if not simulated and nodes:
        from ..models import AiCall

        # Failed nodes may have no outputs; the call ledger still records that their provider was simulated.
        simulated = db.scalar(select(AiCall.id).where(AiCall.node_run_id.in_([node.id for node in nodes]),
                                                     AiCall.simulated.is_(True)).limit(1)) is not None
    if (status, question, result, error, simulated) != (op.status, op.question, op.result, op.error, op.simulated):
        op.status, op.question, op.result, op.error, op.simulated = status, question, result, error, simulated
        if status in TERMINAL:
            if status == "succeeded":
                op.error = None
            _on_terminal(db, op, defer=defer)
        db.flush()
    return op


# --- answer / cancel / retry (design §4.5) -----------------------------------------------------


def answer(db: Session, user: User, operation_id: uuid.UUID, answers: list[str]) -> AiOperation:
    """Answer the model's clarification questions: the run's own decision endpoint (it commits)."""
    from ..api.workflows import DecisionIn, decide

    op, conv, _ = load_operation(db, user, operation_id)
    require_open(conv)
    op = sync_operation(db, op)
    db.commit()
    q = op.question or {}
    if op.status != "waiting_input" or q.get("kind") != "clarification" or op.run_id is None:
        raise conflict("not_waiting", "this operation is not waiting for answers", status=op.status)
    decide(op.run_id, q.get("node_id") or "model", DecisionIn(decision="approved", answers=answers), user, db)
    op = db.get(AiOperation, operation_id)
    assert op is not None
    op = sync_operation(db, op)
    db.commit()
    return op


def cancel(db: Session, user: User, operation_id: uuid.UUID) -> AiOperation:
    """Stop the operation's run (the run's cancel endpoint); the result shows what really happened. AI native: a
    main-agent turn stops before its next step and its active sub-agents are stopped too."""
    from ..api.workflows import cancel as cancel_run
    from . import orchestrator

    op, conv, _ = load_operation(db, user, operation_id)
    require_open(conv)
    op = sync_operation(db, op)
    db.commit()
    if op.status in TERMINAL:
        return op
    if orchestrator.is_turn(op):
        return orchestrator.cancel_turn(db, user, op)
    if op.run_id is None:
        if op.kind in ("spec_extract", "drafting_create", "drafting_modify"):
            op = _lock_op(db, op.id)
            if op.status not in TERMINAL:
                op.status, op.question = "cancelled", None
                _on_terminal(db, op)
            db.commit()
            return op
        raise conflict("not_cancellable", "this operation runs synchronously and cannot be stopped", kind=op.kind)
    try:
        cancel_run(op.run_id, user, db)
    except AppError as exc:
        if exc.code != "run_finished":
            raise
        db.rollback()
    op = db.get(AiOperation, operation_id)
    assert op is not None
    op = sync_operation(db, op)
    db.commit()
    return op


def retry(db: Session, user: User, operation_id: uuid.UUID, client_request_id: uuid.UUID) -> tuple[dict, bool]:
    """A new request message copying the failed / cancelled operation's snapshot (payload.retry_of),
    dispatched again as the same operation kind. An operation is retried at most once (409
    already_retried names the retry); the source row stays locked until the retry is recorded, so two
    concurrent retries cannot both create one. Returns (AiSendResult, created). AI native: a retry is a send (409
    agent_busy while a turn works); a retried turn of the person's request is its own root again."""
    from . import ai as ai_svc
    from . import orchestrator

    op, conv, _ = load_operation(db, user, operation_id)
    op = sync_operation(db, op)
    db.commit()
    op = _lock_op(db, op.id)  # lock order: operation → conversation
    conv = lock_conversation(db, conv.id)
    found = _existing(db, conv, client_request_id)
    if found is not None:
        db.commit()
        return send_out(db, *found), False
    require_open(conv)
    if op.kind == "task_draft" or (op.request or {}).get("task_id") is not None:
        features.require_enabled("tasks")
    orchestrator.before_send(db, conv)
    orchestrator.require_enabled(op)
    if op.status not in ("failed", "cancelled"):
        raise conflict("not_retryable", "only failed or cancelled operations can be retried", status=op.status)
    child = db.scalar(select(AiOperation.id).where(AiOperation.retry_of == op.id).order_by(AiOperation.created_at, AiOperation.id).limit(1))
    if child is not None:
        raise conflict("already_retried", "this request was already retried; continue from the retry", operation_id=str(child))
    req = dict(op.request or {})
    if (req.get("model_snapshot") or {}).get("simulated"):
        from ..ai.providers.base import ProviderError

        # A historical simulated request cannot silently become a real-model
        # operation when retried in production.
        try:
            ai_svc.require_simulation_allowed()
        except ProviderError as exc:
            raise AppError(503, "ai_unavailable", exc.message, {"reason": exc.code}) from None
    target = req.get("target")
    _check_target_available(db, target)
    req["client_request_id"] = str(client_request_id)
    req.pop("input_revisions", None)  # generation supplement I: a retry starts from the request as sent (its own run)
    # Retry is a continuation: model, credential, policy and personal preference snapshots remain immutable.
    # A purpose whose selection failed froze no model; it is resolved again so a fixed configuration is used.
    purpose = OP_PURPOSE.get(req.get("operation") or op.kind, "assistant")
    req["model_snapshots"] = ai_svc.refreeze_failed(db, req.get("model_snapshots"),
                                                    preferred={purpose: req["model_config_id"]} if req.get("model_config_id") else None)
    if not req.get("model_snapshot") and isinstance(req["model_snapshots"], dict):
        req["model_snapshot"] = ((req["model_snapshots"].get("entries") or {}).get(purpose) or {}).get("snapshot")
    payload = {"client_request_id": str(client_request_id), "requested_operation": req.get("requested_operation") or op.kind,
               "operation": op.kind, "task_id": req.get("task_id"), "model_config_id": req.get("model_config_id"),
               "retry_of": str(op.id), "execution_policy": op.execution_policy}
    if "preferences_snapshot" in req:
        payload["preferences_snapshot"] = req["preferences_snapshot"]
    req["execution_policy"] = op.execution_policy  # a retry keeps the frozen policy of the request it repeats
    msg = _append(db, conv, role="user", kind="request", text=req.get("text") or "", target=target,
                  references=list(req.get("references") or []), payload=payload)
    conv.last_activity_at = _now()
    new = _create_operation(db, user, conv, msg, kind=op.kind, request=req, task_id=_uuid(req.get("task_id")),
                            client_request_id=client_request_id, retry_of=op.id)
    orchestrator.root_turn(new)
    from . import operation_events
    operation_events.retry_context(db, op, new)
    db.commit()
    return _execute_and_out(db, user, new.id, msg.id), True


#: Recovery codes a chain step can be continued with in place (S3, design §3.1).
#: ``re_extract`` (generation supplement A: the sources show a parts list the extraction missed) runs the spec step again.
CONTINUE_RECOVERIES = ("continue_extract", "retry_design", "retry_definition", "retry_sheet", "re_extract")
CHAIN_ORDER = ("spec", "assembly", "drawings", "delivery")


def continue_operation(db: Session, user: User, operation_id: uuid.UUID, *, file_version_ids: list[str] | None = None) -> AiOperation:
    """S3: continue a failed ``assembly_create`` from its checkpoint instead of starting over: the first failed chain
    step of the operation's run is retried in that run (the engine's node retry), which continues the failed spec
    version, reuses the definitions already built and continues a partial drawing package; the steps after it run
    again. The operation is running again until the run finishes. 409 ``not_continuable`` (another kind, not failed,
    the failure is not one continuing can fix — its recovery code says what to do instead) or ``already_retried``.

    ``file_version_ids`` (generation supplement I, A-04 / IN-08): the spec step failed because a source could not be read
    (recovery ``replace_source`` / ``fix_input``) — it runs again in the same run with these files instead of the unread
    ones (:func:`_continue_with_files`)."""
    from ..workflow import engine
    from .lifecycle import require_active

    op, conv, _ = load_operation(db, user, operation_id)
    op = sync_operation(db, op)
    db.commit()
    op = _lock_op(db, op.id)  # lock order: operation → conversation → run
    conv = lock_conversation(db, conv.id)
    require_open(conv)
    if op.kind != "assembly_create" or op.run_id is None:
        raise conflict("not_continuable", "only an AI assembly request continues from its failed step; retry the request instead", kind=op.kind)
    if file_version_ids is None and op.generation > 1 and op.status != "failed":
        db.commit()
        return op
    if file_version_ids is not None:
        return _continue_with_files(db, user, op, conv, file_version_ids)
    if op.status != "failed":
        raise conflict("not_continuable", f"the request is {op.status}; only a failed request can be continued", status=op.status)
    child = db.scalar(select(AiOperation.id).where(AiOperation.retry_of == op.id).limit(1))
    if child is not None:
        raise conflict("already_retried", "this request was already retried; continue from the retry", operation_id=str(child))
    project_access(db, user, op.project_id, "design")
    require_active(db, op.project_id, what="continuing a run")
    run = engine.lock_run_for_retry(db, user, op.run_id)
    if run.cancel_requested:
        raise conflict("not_continuable", "the run was cancelled; retry the request instead", status="cancelled")
    nodes = {n.node_id: n for n in db.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all()}
    failed = next((nodes[n] for n in CHAIN_ORDER if n in nodes and nodes[n].status == "failed"), None)
    err = dict((failed.error if failed is not None else None) or {})
    if failed is None or err.get("recovery") not in CONTINUE_RECOVERIES or not err.get("retryable"):
        raise conflict("not_continuable", "continuing would fail the same way: " + str(err.get("recovery_message") or err.get("message") or ""),
                       node_id=failed.node_id if failed is not None else None, recovery=err.get("recovery"), failure_code=err.get("code"))
    try:
        engine.retry_node(db, run, failed.node_id)
    except ValueError as exc:
        raise conflict("not_continuable", str(exc), node_id=failed.node_id)
    from . import operation_events
    operation_events.reopen(db, op)
    op.status, op.error, op.result, op.question, op.finished_at = "running", None, None, None, None
    from .audit import audit

    audit(db, action="ai_operation.continue", subject_type="ai_operation", subject_id=op.id, user_id=user.id, project_id=op.project_id,
          data={"run_id": str(run.id), "node_id": failed.node_id, "code": err.get("code"), "recovery": err.get("recovery")})
    conv.last_activity_at = _now()
    db.commit()
    return op


def _continue_with_files(db: Session, user: User, op: AiOperation, conv: AiConversation, file_version_ids: list[str]) -> AiOperation:
    """Generation supplement I (A-04 / IN-08): the spec step of this request's run could not read a source; it runs again
    in the same run with the chosen files (services/spec_inputs.py: the unread files dropped, the new ones added, a new
    version of the spec it left). The input revision is recorded on the run and appended to the operation's request
    (``input_revisions``; the original request is kept) with a new context manifest. Idempotent: the same files again
    while the request runs (or after it succeeded) return it as it is — no second run, no second spec version; once it
    failed again with them, 409 ``input_unchanged``. Caller holds the operation and conversation locks."""
    from ..workflow import engine
    from . import assistant_context as actx
    from . import spec_inputs
    from .assembly_ai import _record_manifest
    from .audit import audit
    from .lifecycle import require_active

    wanted = list(dict.fromkeys(str(f) for f in file_version_ids))
    done = [r for r in (op.request or {}).get("input_revisions") or [] if isinstance(r, dict)]
    same = bool(done) and sorted(done[-1].get("requested") or []) == sorted(wanted)
    if same and op.status != "failed":
        db.commit()
        return op  # the same click again: that input is already running (or done)
    if same:
        spec_node = db.scalar(select(NodeRun).where(NodeRun.run_id == op.run_id, NodeRun.node_id == "spec"))
        if spec_node is not None and spec_node.status != "failed":
            db.commit()
            return op  # the spec step took that input; what failed later continues its own way (its own recovery)
    if op.status != "failed":
        raise conflict("not_continuable", f"the request is {op.status}; only a failed request can be continued", status=op.status)
    child = db.scalar(select(AiOperation.id).where(AiOperation.retry_of == op.id).limit(1))
    if child is not None:
        raise conflict("already_retried", "this request was already retried; continue from the retry", operation_id=str(child))
    project_access(db, user, op.project_id, "design")
    require_active(db, op.project_id, what="continuing a run")
    run = engine.lock_run_for_retry(db, user, op.run_id)
    if run.cancel_requested:
        raise conflict("not_continuable", "the run was cancelled; retry the request instead", status="cancelled")
    nodes = {n.node_id: n for n in db.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all()}
    failed = next((nodes[n] for n in CHAIN_ORDER if n in nodes and nodes[n].status == "failed"), None)
    if failed is None or failed.node_id != "spec":
        raise conflict("not_continuable", "only a spec step that could not read its sources takes other files; continue or retry instead",
                       node_id=failed.node_id if failed is not None else None)
    entry = spec_inputs.replace_sources(db, run, failed, wanted, by=user.id, origin={"operation_id": str(op.id)})
    try:
        engine.retry_node(db, run, failed.node_id)
    except ValueError as exc:
        raise conflict("not_continuable", str(exc), node_id=failed.node_id)
    req = dict(op.request or {})
    req["input_revisions"] = [*done, entry]
    op.request = req
    labels = {str(r.get("object_id")): r.get("title") for r in [req.get("target"), *(req.get("references") or [])]
              if isinstance(r, dict) and r.get("kind") == "file_version"}
    labels.update({a["file_version_id"]: f"{a['name']} v{a['version']}" for a in entry["added"]})
    items = [actx._item("required", "request", "Request", str(req.get("text") or ""))]
    items += [actx._item("required", "reference", f"file_version: {labels.get(f) or f}", "", ref_key=f"file_version:{f}", read="metadata",
                         ranges=["read by the engineering spec step (see its chunks)"]) for f in entry["file_version_ids"]]
    items += [actx._item("required", "reference", f"file_version: {labels.get(f) or f}", "", included=False, ref_key=f"file_version:{f}",
                         reason="unread_replaced") for f in entry["dropped"]]
    _record_manifest(db, op, items, purpose="modeling", model_ref=_model_ref(req),
                     note=f"input revision {entry['n']}: the spec step reads the files again with the replacements")
    from . import operation_events
    operation_events.reopen(db, op)
    op.status, op.error, op.result, op.question, op.finished_at = "running", None, None, None, None
    audit(db, action="ai_operation.continue", subject_type="ai_operation", subject_id=op.id, user_id=user.id, project_id=op.project_id,
          data={"run_id": str(run.id), "node_id": failed.node_id, "code": entry["failure"]["code"], "recovery": entry["failure"]["recovery"],
                "input_revision": entry["n"], "added": [a["file_version_id"] for a in entry["added"]], "dropped": entry["dropped"]})
    conv.last_activity_at = _now()
    db.commit()
    return op


# --- fork ------------------------------------------------------------------------------------


def fork(db: Session, user: User, conversation_id: uuid.UUID, *, operation_id: uuid.UUID | None, reference_keys: list[str] | None,
         title: str | None) -> AiConversation:
    """New conversation in the same project / task whose composer holds only the chosen result
    items (or the chosen references) and the source's constraints. History is not copied."""
    src, _ = load_conversation(db, user, conversation_id)
    if src.task_id is not None:
        features.require_enabled("tasks")
    op: AiOperation | None = None
    if operation_id is not None:
        op = db.get(AiOperation, operation_id)
        if op is None or op.conversation_id != src.id:
            raise not_found("operation")
        op = sync_operation(db, op)
        candidates = [it["ref"] for it in ((op.result or {}).get("items") or []) if isinstance(it, dict) and it.get("ref")]
    else:
        if not reference_keys:
            raise AppError(422, "reference_keys_required", "choose a result or the references to start from")
        candidates = list((src.composer or {}).get("references") or [])
        for m in db.scalars(select(AiMessage).where(AiMessage.conversation_id == src.id).order_by(AiMessage.seq.desc())).all():
            candidates += _refs_of_message(m)
    by_key: dict[str, dict] = {}
    for r in candidates:
        if r and r.get("key") and r["key"] not in by_key:
            by_key[r["key"]] = r
    if reference_keys is not None:
        missing = [k for k in reference_keys if k not in by_key]
        if missing:
            raise AppError(422, "ref_not_in_conversation", "some references are not part of this conversation or result",
                           {"key": missing[0], "keys": missing})
        chosen = [by_key[k] for k in dict.fromkeys(reference_keys)]
    else:
        chosen = list(by_key.values())
    if not chosen:
        raise AppError(422, "nothing_to_fork", "the operation has no result to continue from")
    statuses = refs.ref_statuses(db, chosen)
    target_key = next((r["key"] for r in chosen if r["kind"] in refs.OPERABLE_KINDS
                       and (statuses.get(r["key"]) or {}).get("state") != "unavailable"), None)
    constraints = [{"id": c.get("id"), "text": c.get("text"), "source_seq": None, "created_at": _now().isoformat()}
                   for c in src.constraints or []]
    new = AiConversation(user_id=user.id, project_id=src.project_id, task_id=src.task_id, title=(title or "").strip()[:200],
                         composer={"target_key": target_key, "references": chosen}, constraints=constraints, next_seq=1, version=1,
                         forked_from={"conversation_id": str(src.id), "operation_id": str(op.id) if op else None})
    db.add(new)
    db.flush()
    return new
