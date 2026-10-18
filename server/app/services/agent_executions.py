"""Agent execution views (execution-layer design §6.5): detail, timeline, list and the workbench's agent to-dos.

Read-only projections over ``agent_executions``, ``agent_execution_events`` and ``ai_calls``; the controls (stop,
pause, resume) live in ``services/agent_runtime.py``. Project members may read; controls follow the run-control rule.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..errors import AppError, not_found
from ..models import (
    AgentExecution,
    AgentExecutionEvent,
    AgentProfile,
    AiCall,
    CollaborationNodeTask,
    CollaborationRun,
    ProjectMember,
    Task,
    User,
    WorkflowRun,
)
from ..security import project_access
from . import agent_runtime as rt

SESSION_PURPOSES = ("agent_execute", "agent_review")
#: Workbench to-dos: a blocked agent's questions and a node whose budget is used up.
TODO_KINDS = ("agent_question", "budget_exhausted")
#: A finished execution whose final status event never came (worker crash, lease recovery) ends its stream after this.
STREAM_GRACE_S = 5.0
MAX_EVENTS_PAGE = 1000


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def load_execution(db: Session, user: User, execution_id: uuid.UUID) -> tuple[AgentExecution, WorkflowRun]:
    """The execution when the user is a member of its project (404 otherwise, like every project resource)."""
    ex = db.get(AgentExecution, execution_id)
    run = db.get(WorkflowRun, ex.run_id) if ex is not None else None
    if ex is None or run is None:
        raise not_found("agent execution")
    project_access(db, user, run.project_id)
    return ex, run


def _node_title(run: WorkflowRun, nt: CollaborationNodeTask | None, db: Session) -> str:
    if nt is None:
        return ""
    for n in (run.graph_snapshot or {}).get("nodes") or []:
        if n.get("id") == nt.node_id:
            title = (n.get("params") or {}).get("title")
            if title:
                return str(title)
    task = db.get(Task, nt.task_id)
    return task.title if task is not None else nt.node_id


def _snapshot(db: Session, run_id: uuid.UUID, agent_id: uuid.UUID | None) -> dict[str, Any]:
    collab = db.get(CollaborationRun, run_id)
    return dict(((collab.role_snapshots or {}) if collab is not None else {}).get(str(agent_id)) or {})


def _model_brief(snap: dict[str, Any], purpose: str) -> dict[str, Any] | None:
    frozen = (snap.get("model_snapshots") or {}).get(purpose)
    if isinstance(frozen, dict) and frozen.get("simulated"):
        return {"config_id": None, "provider": "simulated", "model_id": "simulated"}
    if isinstance(frozen, dict) and frozen.get("config_id"):
        return {"config_id": frozen["config_id"], "provider": frozen.get("provider"), "model_id": frozen.get("model_id")}
    mc = snap.get("model_config") if isinstance(snap.get("model_config"), dict) else None
    if mc:
        return {"config_id": mc.get("id"), "provider": mc.get("provider"), "model_id": mc.get("model_id")}
    return None


def last_seq(db: Session, execution_id: uuid.UUID) -> int:
    return int(db.scalar(select(AgentExecutionEvent.seq).where(AgentExecutionEvent.execution_id == execution_id)
                         .order_by(AgentExecutionEvent.seq.desc()).limit(1)) or 0)


def events_after(db: Session, execution_id: uuid.UUID, after: int, limit: int) -> list[AgentExecutionEvent]:
    return list(db.scalars(select(AgentExecutionEvent).where(AgentExecutionEvent.execution_id == execution_id, AgentExecutionEvent.seq > after)
                           .order_by(AgentExecutionEvent.seq).limit(max(1, min(limit, MAX_EVENTS_PAGE)))).all())


def event_out(e: AgentExecutionEvent) -> dict[str, Any]:
    return {"seq": e.seq, "type": e.type, "at": _iso(e.at), "payload": e.payload or {}}


def stream_finished(db: Session, ex: AgentExecution, cursor: int) -> bool:
    """The timeline is complete: the execution is no longer running and every event up to its final status was read
    (an execution that never got a final status event — a crashed worker — ends after a short grace period)."""
    if ex.status == "running":
        return False
    last = db.execute(select(AgentExecutionEvent.seq, AgentExecutionEvent.type).where(AgentExecutionEvent.execution_id == ex.id)
                      .order_by(AgentExecutionEvent.seq.desc()).limit(1)).first()
    if last is not None and cursor < last[0]:
        return False
    if last is not None and last[1] == "status":
        return True
    return ex.finished_at is None or datetime.now(UTC) - ex.finished_at > timedelta(seconds=STREAM_GRACE_S)


def _used(db: Session, ex: AgentExecution, pricing: dict[str, Any] | None) -> dict[str, Any]:
    calls = db.execute(select(AiCall.purpose, AiCall.tokens_in, AiCall.tokens_out).where(AiCall.agent_execution_id == ex.id)).all()
    if ex.status == "running" or ex.duration_ms is None:
        duration = int((datetime.now(UTC) - ex.started_at).total_seconds() * 1000) if ex.started_at else 0
    else:
        duration = int(ex.duration_ms)
    usage = rt.usage_of([{"tokens_in": t_in, "tokens_out": t_out} for _p, t_in, t_out in calls], pricing, duration)
    tool_events = int(db.scalar(select(func.count()).select_from(AgentExecutionEvent)
                                .where(AgentExecutionEvent.execution_id == ex.id, AgentExecutionEvent.type == "tool_result")) or 0)
    return {"steps": sum(1 for p, _a, _b in calls if p in SESSION_PURPOSES), "tool_calls": max(tool_events, len(ex.tool_calls or [])),
            "tokens_in": usage["tokens_in"], "tokens_out": usage["tokens_out"], "cost_fen": usage["cost_fen"],
            "cost_unknown": usage["cost_unknown"], "duration_ms": duration}


def execution_actions(db: Session, user: User, ex: AgentExecution, run: WorkflowRun, nt: CollaborationNodeTask | None) -> list[str]:
    """stop / pause / resume for this user now (run-control rights: project owner or the person who started the run)."""
    role = rt._member_role(db, user, run.project_id)
    if not rt._run_control_allowed(db, user, run, role) or run.cancel_requested or run.status in rt.TERMINAL_RUN:
        return []
    from . import feature_flags as features

    if not features.enabled("collaboration") and not features.can_continue(db, "collaboration", run_id=run.id):
        return ["stop"] if ex.status == "running" and ex.stop_requested_at is None else []
    out: list[str] = []
    if ex.status == "running" and ex.stop_requested_at is None:
        out.append("stop")
        if not run.paused:
            out.append("pause")
    br = (nt.blocked_reason or {}) if nt is not None else {}
    if run.paused or (nt is not None and nt.phase == "blocked" and br.get("code") in rt.RESUMABLE_BLOCKS and br.get("resume_phase")):
        out.append("resume")
    return out


def _outputs(db: Session, ex: AgentExecution, nt: CollaborationNodeTask | None) -> list[dict[str, Any]]:
    formal = {str(f.get("asset_version_id")) for f in (nt.formal_outputs if nt is not None else None) or []}
    delivered = {str(d.get("asset_version_id")) for d in ((ex.result or {}).get("deliverables") or [])}
    out: dict[str, dict[str, Any]] = {}
    for e in db.scalars(select(AgentExecutionEvent).where(AgentExecutionEvent.execution_id == ex.id, AgentExecutionEvent.type == "tool_result")
                        .order_by(AgentExecutionEvent.seq)).all():
        for p in (e.payload or {}).get("produced") or []:
            vid = str(p.get("asset_version_id"))
            out[vid] = {"asset_version_id": vid, "asset_type": p.get("asset_type"), "label": p.get("label"), "event_seq": e.seq}
    # executions recorded before the timeline existed; workspace files registered after the session (P3)
    for p in ((ex.result or {}).get("produced") or []):
        vid = str(p.get("asset_version_id"))
        if vid not in out:
            out[vid] = {"asset_version_id": vid, "asset_type": p.get("asset_type"), "label": p.get("label"), "event_seq": None}
            if p.get("path"):
                out[vid]["path"] = p["path"]
    for vid, item in out.items():
        item["state"] = "formal" if vid in formal else "delivered" if vid in delivered else "candidate"
    return list(out.values())


def execution_out(db: Session, user: User, ex: AgentExecution, run: WorkflowRun) -> dict[str, Any]:
    nt = db.get(CollaborationNodeTask, ex.node_task_id)
    collab = db.get(CollaborationRun, run.id)
    profile = db.get(AgentProfile, ex.agent_profile_id) if ex.agent_profile_id else None
    snap = _snapshot(db, run.id, ex.agent_profile_id)
    purpose = "agent_execute" if ex.role == "execute" else "agent_review"
    node_limits = rt.limits_of(nt.limits if nt is not None else None)
    if ex.role != "execute":
        node_limits = {**node_limits, "max_duration_s": rt.limits_of(snap.get("limits"))["max_duration_s"]}
    options = rt.runtime_options_of(snap) if (ex.runtime or "native") in ("tool_agent", "workspace_agent") else {}
    limits = {"max_steps": node_limits["max_steps"], "max_duration_s": node_limits["max_duration_s"], "max_cost_fen": node_limits.get("max_cost_fen")}
    if options.get("max_tool_calls"):
        limits["max_tool_calls"] = options["max_tool_calls"]
    attempts = db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == ex.node_task_id).order_by(AgentExecution.created_at)).all()
    result = ex.result or {}
    verdict = None
    if ex.role != "execute" and result:
        verdict = {"verdict": result.get("verdict"), "criteria": result.get("criteria") or [], "summary": result.get("summary") or ""}
    inputs = []
    for b in (nt.bound_inputs if nt is not None else None) or []:
        brief = rt._version_brief(db, b.get("asset_version_id"))
        inputs.append({"slot_key": b.get("slot_key"), "asset_version_id": b.get("asset_version_id"), "asset_type": brief.get("asset_type"),
                       "label": brief.get("label"), "content_hash": b.get("content_hash"), "from_node_id": b.get("from_node_id")})
    return {
        "id": str(ex.id),
        "project_id": str(run.project_id),
        "run_id": str(run.id),
        "node_id": nt.node_id if nt is not None else None,
        "node_task_id": str(ex.node_task_id),
        "node_title": _node_title(run, nt, db),
        "agent": {"id": str(ex.agent_profile_id) if ex.agent_profile_id else None, "name": profile.name if profile else snap.get("name"),
                  "version": ex.profile_version},
        "role": ex.role,
        "runtime": ex.runtime or "native",
        "runtime_impl": ex.runtime_impl,
        "runtime_ref": ex.runtime_ref,
        "model": _model_brief(snap, purpose),
        "status": ex.status,
        "stop_requested_at": _iso(ex.stop_requested_at),
        "attempt_no": ex.attempt_no,
        "rework_no": ex.rework_no,
        "started_at": _iso(ex.started_at),
        "finished_at": _iso(ex.finished_at),
        "lease_until": _iso(ex.lease_until),
        "budget": {"limits": limits, "used": _used(db, ex, snap.get("pricing"))},
        "inputs": inputs,
        "outputs": _outputs(db, ex, nt),
        "attempts": [{"id": str(a.id), "role": a.role, "attempt_no": a.attempt_no, "rework_no": a.rework_no, "status": a.status,
                      "runtime": a.runtime or "native", "started_at": _iso(a.started_at)} for a in attempts],
        "verdict": verdict,
        "error": ex.error,
        "simulated": rt.execution_simulated(db, ex.id),
        "node_phase": nt.phase if nt is not None else None,
        "blocked_reason": nt.blocked_reason if nt is not None and nt.phase == "blocked" else None,
        "control_version": collab.control_version if collab is not None else None,
        "allowed_actions": execution_actions(db, user, ex, run, nt),
        "last_seq": last_seq(db, ex.id),
        # Retain frozen options in historical execution details; this runtime no longer accepts actions.
        "runtime_options": options if (ex.runtime or "native") == "workspace_agent" else None,
        "pending_confirmations": [],
    }


# --- list ------------------------------------------------------------------------------------------------------------


def _cursor(raw: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not raw:
        return None
    try:
        at, _, eid = raw.partition("|")
        return datetime.fromisoformat(at), uuid.UUID(eid)
    except ValueError:
        raise AppError(422, "bad_cursor", "the cursor is not one this list returned") from None


def list_executions(db: Session, user: User, *, project_id: uuid.UUID | None = None, status: str | None = None, agent_id: uuid.UUID | None = None,
                    runtime: str | None = None, role: str | None = None, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    """Newest first; without project_id only the user's projects. ``next_cursor`` continues the list."""
    limit = max(1, min(int(limit), 200))
    q = select(AgentExecution, WorkflowRun).join(WorkflowRun, WorkflowRun.id == AgentExecution.run_id)
    if project_id is not None:
        project_access(db, user, project_id)
        q = q.where(WorkflowRun.project_id == project_id)
    else:
        q = q.where(WorkflowRun.project_id.in_(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)))
    if status:
        q = q.where(AgentExecution.status.in_([s for s in status.split(",") if s]))
    if agent_id is not None:
        q = q.where(AgentExecution.agent_profile_id == agent_id)
    if runtime:
        q = q.where(AgentExecution.runtime == runtime) if runtime != "native" else q.where(or_(AgentExecution.runtime == "native",
                                                                                              AgentExecution.runtime.is_(None)))
    if role:
        q = q.where(AgentExecution.role == role)
    pos = _cursor(cursor)
    if pos is not None:
        q = q.where(or_(AgentExecution.created_at < pos[0], and_(AgentExecution.created_at == pos[0], AgentExecution.id < pos[1])))
    rows = db.execute(q.order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(limit + 1)).all()
    items = []
    for ex, run in rows[:limit]:
        nt = db.get(CollaborationNodeTask, ex.node_task_id)
        profile = db.get(AgentProfile, ex.agent_profile_id) if ex.agent_profile_id else None
        snap = _snapshot(db, run.id, ex.agent_profile_id)
        items.append({"id": str(ex.id), "project_id": str(run.project_id), "run_id": str(run.id), "node_id": nt.node_id if nt else None,
                      "node_title": _node_title(run, nt, db), "agent": {"id": str(ex.agent_profile_id) if ex.agent_profile_id else None,
                                                                         "name": profile.name if profile else snap.get("name")},
                      "role": ex.role, "runtime": ex.runtime or "native", "status": ex.status, "started_at": _iso(ex.started_at),
                      "finished_at": _iso(ex.finished_at), "used": _used(db, ex, snap.get("pricing")), "simulated": rt.execution_simulated(db, ex.id)})
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1][0]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"
    return {"items": items, "next_cursor": next_cursor}


# --- workbench to-dos ------------------------------------------------------------------------------------------------


def agent_todos(db: Session, user: User, *, project_id: uuid.UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Questions and exhausted budgets, visible to people who may control the run."""
    if project_id is not None:
        project_access(db, user, project_id)
    return _blocked_todos(db, user, project_id=project_id, limit=limit)


def _blocked_todos(db: Session, user: User, *, project_id: uuid.UUID | None, limit: int) -> list[dict[str, Any]]:
    members = select(ProjectMember.project_id).where(ProjectMember.user_id == user.id, ProjectMember.role != "viewer")
    q = (select(CollaborationNodeTask, WorkflowRun).join(WorkflowRun, WorkflowRun.id == CollaborationNodeTask.run_id)
         .where(CollaborationNodeTask.phase == "blocked", CollaborationNodeTask.executor_kind == "agent",
                WorkflowRun.project_id.in_(members), WorkflowRun.cancel_requested.is_(False),
                WorkflowRun.status.notin_(sorted(rt.TERMINAL_RUN))))
    if project_id is not None:
        project_access(db, user, project_id)
        q = q.where(WorkflowRun.project_id == project_id)
    out: list[dict[str, Any]] = []
    for nt, run in db.execute(q.order_by(CollaborationNodeTask.updated_at.desc()).limit(500)).all():
        br = nt.blocked_reason or {}
        code = br.get("code")
        # An undecided agent review (unknown_verdict) also waits for a person: the item says which criteria are open.
        kind = ("agent_question" if code in ("agent_blocked", "unknown_verdict") else "budget_exhausted" if code == "budget_exceeded"
                else None)
        if kind is None or not rt._run_control_allowed(db, user, run, rt._member_role(db, user, run.project_id)):
            continue
        ex = db.scalar(select(AgentExecution).where(AgentExecution.node_task_id == nt.id).order_by(AgentExecution.created_at.desc()).limit(1))
        agent_id = nt.executor_agent_id
        profile = db.get(AgentProfile, agent_id) if agent_id else None
        item: dict[str, Any] = {
            "kind": kind, "project_id": str(run.project_id), "run_id": str(run.id), "run_title": run.title, "node_id": nt.node_id,
            "node_task_id": str(nt.id), "node_title": _node_title(run, nt, db), "task_id": str(nt.task_id),
            "agent": {"id": str(agent_id) if agent_id else None, "name": profile.name if profile else None},
            "execution_id": str(ex.id) if ex is not None else None, "code": code, "reason": br.get("message"), "at": _iso(nt.updated_at),
            "node_version": nt.version,
        }
        if kind == "agent_question":
            item["questions"] = list(br.get("questions") or br.get("missing") or [])
        else:
            item["limit"] = br.get("limit")
            item["budget"] = nt.budget or {}
        out.append(item)
        if len(out) >= limit:
            break
    return out
