"""Agent profiles: project-scoped agents with a purpose, model, tools,
execution modes, knowledge scope and budget limits
(.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §3.2, §4 C07).

A profile is configuration only. Starting a collaboration run snapshots the
profiles it uses (``collaboration_runs.role_snapshots``), so later edits,
disabling or deleting a profile never change a running instance. Every write
compares ``expected_version`` (409 ``version_conflict``) and bumps ``version``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..ai.harness.base import SUPPORTED_RUNTIMES
from ..ai.providers.base import ProviderError
from ..config import get_settings
from ..errors import AppError, conflict, not_found
from ..models import AGENT_PURPOSES, AGENT_RUNTIMES, EXECUTION_MODES, AgentExecution, AgentProfile, CollaborationRun, ModelConfigRow, User, WorkflowRun
from ..security import ProjectAccess, project_access
from ..workflow.engine import TERMINAL_RUN
from . import ai as ai_svc
from .agent_tools import REVIEW_ONLY_TOOLS, TOOL_NAMES
from .audit import audit
from . import feature_flags as features

Purpose = Literal["execute", "review", "planner", "general"]
ExecutionMode = Literal["full_auto", "semi_auto"]
Runtime = Literal["native", "tool_agent"]
assert set(Purpose.__args__) == set(AGENT_PURPOSES)  # type: ignore[attr-defined]
assert set(ExecutionMode.__args__) == set(EXECUTION_MODES)  # type: ignore[attr-defined]
assert set(Runtime.__args__) == set(SUPPORTED_RUNTIMES) <= set(AGENT_RUNTIMES)  # type: ignore[attr-defined]


class AgentLimits(BaseModel):
    """Closed budget structure (design §3.2, requirements §6.2); the ranges are frozen."""

    model_config = ConfigDict(extra="forbid")
    max_steps: int = Field(default=8, ge=1, le=64)
    max_duration_s: int = Field(default=300, ge=1, le=86_400)
    max_attempts: int = Field(default=2, ge=1, le=10)
    max_reworks: int = Field(default=3, ge=0, le=20)
    # None = no cost ceiling; an unknown cost is never treated as 0.
    max_cost_fen: int | None = Field(default=None, ge=0)


class KnowledgeScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["project", "none"] = "project"


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_fen_per_1k: int = Field(ge=0)
    out_fen_per_1k: int = Field(ge=0)


class NativeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolAgentOptions(BaseModel):
    """tool_agent (README §4.7): parallel tool calls in one model response (writing tools still run one after another),
    and per-attempt caps on tool calls and tokens (the cost cap stays the pricing's max_cost_fen)."""

    model_config = ConfigDict(extra="forbid")
    parallel_tool_calls: bool = True
    max_tool_calls: int | None = Field(default=None, ge=1, le=500)
    max_total_tokens: int | None = Field(default=None, ge=1_000, le=20_000_000)


#: Closed options for the runtimes new profiles may select; DB runtime values also preserve retired history.
RUNTIME_OPTIONS: dict[str, type[BaseModel]] = {"native": NativeOptions, "tool_agent": ToolAgentOptions}
AVAILABLE_RUNTIMES = SUPPORTED_RUNTIMES


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def limits_of(p: AgentProfile | dict | None) -> dict[str, Any]:
    """Limits with defaults filled in (stored rows may hold a partial dict)."""
    raw = p if isinstance(p, dict) or p is None else (p.limits or {})
    return AgentLimits.model_validate(raw or {}).model_dump(mode="json")


# --- validation --------------------------------------------------------------------


def check_tools(purpose: str, tools: list[str]) -> list[str]:
    """Tool names must be registered; review-only tools need a review / general purpose."""
    unknown = [t for t in tools if t not in TOOL_NAMES]
    if unknown:
        raise AppError(422, "unknown_tool", "unknown tool name", {"tools": unknown, "known": list(TOOL_NAMES)})
    restricted = [t for t in tools if t in REVIEW_ONLY_TOOLS]
    if restricted and purpose not in ("review", "general"):
        raise AppError(422, "tool_requires_review_purpose", "engineering_review is only allowed for review or general agents",
                       {"tools": restricted, "purpose": purpose})
    return list(dict.fromkeys(tools))


def check_model_config(db: Session, model_config_id: uuid.UUID | None) -> ModelConfigRow | None:
    """The referenced config must exist and be enabled (422 model_config_unusable); None = default config."""
    if model_config_id is None:
        return None
    row = db.get(ModelConfigRow, model_config_id)
    if row is None or not row.enabled:
        raise AppError(422, "model_config_unusable", "the model configuration does not exist or is disabled",
                       {"model_config_id": str(model_config_id)})
    return row


def check_runtime(runtime: str, options: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """The runtime must be available now; its options follow that runtime's closed structure (defaults filled in)."""
    if runtime not in AVAILABLE_RUNTIMES:
        raise AppError(422, "runtime_unavailable", f"the {runtime} executor is not supported",
                       {"runtime": runtime, "available": list(AVAILABLE_RUNTIMES)})
    try:
        opts = RUNTIME_OPTIONS[runtime].model_validate(options or {}).model_dump(mode="json")
    except ValueError as exc:
        raise AppError(422, "runtime_options_invalid", f"invalid options for the {runtime} executor",
                       {"runtime": runtime, "errors": str(exc)[:2000]}) from None
    return runtime, opts


def _check_modes(modes: list[str]) -> list[str]:
    bad = [m for m in modes if m not in EXECUTION_MODES]
    if bad:
        raise AppError(422, "unknown_execution_mode", "unknown execution mode", {"modes": bad})
    return list(dict.fromkeys(modes))


def _check_name(db: Session, project_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None) -> str:
    name = name.strip()
    if not name:
        raise AppError(422, "name_required", "an agent profile needs a name")
    stmt = select(AgentProfile.id).where(AgentProfile.project_id == project_id, AgentProfile.name == name)
    if exclude is not None:
        stmt = stmt.where(AgentProfile.id != exclude)
    if db.scalar(stmt) is not None:
        raise conflict("name_taken", f"an agent profile named {name!r} already exists in this project", name=name)
    return name


# --- loading -------------------------------------------------------------------------


def load_profile(db: Session, user: User, profile_id: uuid.UUID, action: str = "view") -> tuple[AgentProfile, ProjectAccess]:
    p = db.get(AgentProfile, profile_id)
    if p is None:
        raise not_found("agent profile")
    acc = project_access(db, user, p.project_id, action)
    return p, acc


def lock_profile(db: Session, profile_id: uuid.UUID) -> AgentProfile:
    p = db.scalar(select(AgentProfile).where(AgentProfile.id == profile_id).with_for_update().execution_options(populate_existing=True))
    if p is None:
        raise not_found("agent profile")
    return p


def list_profiles(db: Session, project_id: uuid.UUID, *, purpose: str | None = None, enabled: bool | None = None) -> list[AgentProfile]:
    stmt = select(AgentProfile).where(AgentProfile.project_id == project_id).order_by(AgentProfile.created_at, AgentProfile.name)
    if purpose:
        stmt = stmt.where(AgentProfile.purpose == purpose)
    if enabled is not None:
        stmt = stmt.where(AgentProfile.enabled.is_(enabled))
    return list(db.scalars(stmt).all())


# --- writes --------------------------------------------------------------------------


def create_profile(db: Session, acc: ProjectAccess, data: dict[str, Any]) -> AgentProfile:
    """Caller checked the `manage` action and commits. `data` is a validated AgentProfileIn dump."""
    features.require_enabled("collaboration")
    name = _check_name(db, acc.project.id, data["name"])
    purpose = data["purpose"]
    tools = check_tools(purpose, list(data.get("allowed_tools") or []))
    modes = _check_modes(list(data.get("execution_modes") or []))
    cfg = check_model_config(db, data.get("model_config_id"))
    runtime, runtime_options = check_runtime(data.get("runtime") or "native", data.get("runtime_options"))
    p = AgentProfile(
        project_id=acc.project.id,
        name=name,
        purpose=purpose,
        description=(data.get("description") or "").strip(),
        role_prompt=data.get("role_prompt") or "",
        model_config_id=cfg.id if cfg else None,
        allowed_tools=tools,
        execution_modes=modes,
        knowledge_scope=KnowledgeScope.model_validate(data.get("knowledge_scope") or {}).model_dump(mode="json"),
        limits=limits_of(data.get("limits")),
        pricing=Pricing.model_validate(data["pricing"]).model_dump(mode="json") if data.get("pricing") is not None else None,
        runtime=runtime,
        runtime_options=runtime_options,
        enabled=bool(data.get("enabled", True)),
        created_by=acc.user.id,
    )
    if not p.enabled:
        p.disabled_at = datetime.now(UTC)
    db.add(p)
    db.flush()
    audit(db, action="agent_profile.create", subject_type="agent_profile", subject_id=p.id, user_id=acc.user.id, project_id=p.project_id,
          data={"name": p.name, "purpose": p.purpose, "allowed_tools": tools, "execution_modes": modes, "runtime": runtime})
    return p


def patch_profile(db: Session, acc: ProjectAccess, profile_id: uuid.UUID, data: dict[str, Any], expected_version: int) -> AgentProfile:
    """Partial update under a row lock; every change bumps `version`. Running
    instances keep their snapshots (design §3.2)."""
    features.require_enabled("collaboration")
    p = lock_profile(db, profile_id)
    if expected_version != p.version:
        raise conflict("version_conflict", "the agent profile was changed by someone else; reload first", current_version=p.version)
    changes: dict[str, Any] = {}
    purpose = data.get("purpose") or p.purpose
    if "name" in data and data["name"] is not None:
        changes["name"] = _check_name(db, p.project_id, data["name"], exclude=p.id)
    if "purpose" in data and data["purpose"] is not None:
        changes["purpose"] = data["purpose"]
    if "description" in data and data["description"] is not None:
        changes["description"] = data["description"].strip()
    if "role_prompt" in data and data["role_prompt"] is not None:
        changes["role_prompt"] = data["role_prompt"]
    if "model_config_id" in data:  # explicit null = back to the default config
        cfg = check_model_config(db, data["model_config_id"])
        changes["model_config_id"] = cfg.id if cfg else None
    tools = list(data["allowed_tools"]) if data.get("allowed_tools") is not None else list(p.allowed_tools or [])
    changes["allowed_tools"] = check_tools(purpose, tools)
    if data.get("execution_modes") is not None:
        changes["execution_modes"] = _check_modes(list(data["execution_modes"]))
    if data.get("knowledge_scope") is not None:
        changes["knowledge_scope"] = KnowledgeScope.model_validate(data["knowledge_scope"]).model_dump(mode="json")
    if data.get("limits") is not None:
        changes["limits"] = limits_of({**limits_of(p), **data["limits"]})  # only the given fields change
    if "pricing" in data:
        changes["pricing"] = Pricing.model_validate(data["pricing"]).model_dump(mode="json") if data["pricing"] is not None else None
    if "enabled" in data and data["enabled"] is not None:
        changes["enabled"] = bool(data["enabled"])
    if data.get("runtime") is not None or data.get("runtime_options") is not None:
        runtime = data.get("runtime") or p.runtime
        # Given options are validated; a switched runtime without options gets its defaults.
        options = data["runtime_options"] if data.get("runtime_options") is not None else (p.runtime_options if runtime == p.runtime else {})
        changes["runtime"], changes["runtime_options"] = check_runtime(runtime, options)
    changes = {k: v for k, v in changes.items() if getattr(p, k) != v}
    if not changes:
        return p
    for k, v in changes.items():
        setattr(p, k, v)
    if "enabled" in changes:
        p.disabled_at = None if p.enabled else datetime.now(UTC)
    p.version += 1
    audit(db, action="agent_profile.update", subject_type="agent_profile", subject_id=p.id, user_id=acc.user.id, project_id=p.project_id,
          data={"version": p.version, "fields": sorted(changes)})
    db.flush()
    return p


# --- output --------------------------------------------------------------------------


def model_config_brief(db: Session, model_config_id: uuid.UUID | None) -> dict[str, Any] | None:
    row = db.get(ModelConfigRow, model_config_id) if model_config_id else None
    if row is None:
        return None
    status = ai_svc.config_status(ai_svc.to_config(row))
    return {"id": str(row.id), "label": row.label, "provider": row.provider, "model_id": row.model_id, "ready": status.ready and row.enabled}


def usage_counts(db: Session, profile_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """{profile id: {runs_active, executions_total}} in two grouped queries."""
    out = {pid: {"runs_active": 0, "executions_total": 0} for pid in profile_ids}
    if not profile_ids:
        return out
    projects = select(AgentProfile.project_id).where(AgentProfile.id.in_(profile_ids)).distinct()
    active = db.scalars(
        select(CollaborationRun).join(WorkflowRun, WorkflowRun.id == CollaborationRun.run_id)
        .where(CollaborationRun.project_id.in_(projects), WorkflowRun.status.notin_(tuple(TERMINAL_RUN)))
    ).all()
    for cr in active:
        for key in (cr.role_snapshots or {}):
            try:
                pid = uuid.UUID(str(key))
            except ValueError:
                continue
            if pid in out:
                out[pid]["runs_active"] += 1
    rows = db.execute(select(AgentExecution.agent_profile_id, func.count()).where(AgentExecution.agent_profile_id.in_(profile_ids))
                      .group_by(AgentExecution.agent_profile_id)).all()
    for pid, n in rows:
        if pid in out:
            out[pid]["executions_total"] = int(n)
    return out


def snapshot(db: Session, p: AgentProfile) -> dict[str, Any]:
    """Frozen copy stored in collaboration_runs.role_snapshots (profile_out without usage)."""
    return {
        "id": str(p.id),
        "project_id": str(p.project_id),
        "name": p.name,
        "purpose": p.purpose,
        "description": p.description,
        "role_prompt": p.role_prompt,
        "model_config_id": str(p.model_config_id) if p.model_config_id else None,
        "model_config": model_config_brief(db, p.model_config_id),
        # W1: the exact model configuration per purpose, frozen with the run (every call of the role inherits it).
        "model_snapshots": ai_svc.freeze_role_models(db, p.model_config_id, p.purpose),
        "allowed_tools": list(p.allowed_tools or []),
        "execution_modes": list(p.execution_modes or []),
        "knowledge_scope": KnowledgeScope.model_validate(p.knowledge_scope or {}).model_dump(mode="json"),
        "limits": limits_of(p),
        "pricing": p.pricing,
        # P1: the executor, frozen with the run like the model (runs started earlier have none and stay native).
        "runtime": p.runtime or "native",
        "runtime_options": dict(p.runtime_options or {}),
        "enabled": p.enabled,
        "version": p.version,
        "created_by": str(p.created_by),
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
        "disabled_at": _iso(p.disabled_at),
    }


def execution_stats(db: Session, profile_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """{profile id: {running, executions_7d, cost_fen_7d, cost_unknown}} from the executions' recorded usage
    (cost_unknown when one could not be priced; unknown is never counted as 0)."""
    out: dict[uuid.UUID, dict[str, Any]] = {pid: {"running": 0, "executions_7d": 0, "cost_fen_7d": 0, "cost_unknown": False} for pid in profile_ids}
    if not profile_ids:
        return out
    since = datetime.now(UTC) - timedelta(days=7)
    recent = or_(AgentExecution.created_at >= since, AgentExecution.status == "running")
    rows = db.execute(select(AgentExecution.agent_profile_id, AgentExecution.status, AgentExecution.usage, AgentExecution.created_at)
                      .where(AgentExecution.agent_profile_id.in_(profile_ids), recent)).all()
    for pid, status, usage, created in rows:
        st = out.get(pid)
        if st is None:
            continue
        if status == "running":
            st["running"] += 1
        if created is not None and created >= since:
            st["executions_7d"] += 1
            u = usage or {}
            if u.get("cost_unknown"):
                st["cost_unknown"] = True
            st["cost_fen_7d"] += int(u.get("cost_fen") or 0)
    return out


def model_available(db: Session, p: AgentProfile) -> bool:
    """Whether the profile's model (explicit or default) can run now: configured, enabled, keyed; for tool_agent also
    a supported model family with tool calling. Simulation is available only in dev/test."""
    if (p.runtime or "native") not in AVAILABLE_RUNTIMES:
        return False
    if get_settings().ai_simulation:
        try:
            ai_svc.require_simulation_allowed()
        except ProviderError:
            return False
        return True
    purpose = ai_svc.ROLE_PURPOSES.get(p.purpose, ("agent_execute",))[0]
    cfg = ai_svc.config_for(db, purpose, p.model_config_id)
    if cfg is None or not cfg.enabled or not ai_svc.config_status(cfg).ready:
        return False
    if (p.runtime or "native") == "tool_agent":
        from ..ai.harness.tool_agent import TOOL_FAMILIES

        return cfg.provider in TOOL_FAMILIES and bool(cfg.capabilities.tools)
    return True


def profile_out(db: Session, p: AgentProfile, usage: dict[str, int] | None = None, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    data = snapshot(db, p)
    data["usage"] = usage if usage is not None else usage_counts(db, [p.id])[p.id]
    data["stats"] = stats if stats is not None else execution_stats(db, [p.id])[p.id]
    data["model_available"] = model_available(db, p)
    return data


def profiles_out(db: Session, rows: list[AgentProfile]) -> list[dict[str, Any]]:
    ids = [p.id for p in rows]
    usage = usage_counts(db, ids)
    stats = execution_stats(db, ids)
    return [profile_out(db, p, usage[p.id], stats[p.id]) for p in rows]


def brief(p: AgentProfile | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {"id": str(p.id), "name": p.name, "purpose": p.purpose, "version": p.version, "enabled": p.enabled}
