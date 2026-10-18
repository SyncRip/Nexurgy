"""Actors: a person (project member) or an agent profile acting in a project
(.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §3.1).

Two column conventions exist. New tables use a prefix triple
``<prefix>_kind`` / ``<prefix>_user_id`` / ``<prefix>_agent_id``
(:func:`assign` / :func:`read`). Tables that predate agents keep their
historical user column (``submitted_by``, ``actor_id`` …) next to a kind and
an agent column (:func:`assign_legacy` / :func:`read_legacy`); their existing
rows were backfilled as ``human``.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session

from ..ai.harness.base import SUPPORTED_RUNTIMES
from ..errors import AppError, not_found
from ..models import AgentProfile, ProjectMember, User

ActorKind = Literal["human", "agent"]

# What a profile's purpose allows it to do (design §3.1 / §3.4).
PURPOSE_ROLES: dict[str, frozenset[str]] = {
    "execute": frozenset({"execute", "general"}),
    "review": frozenset({"review", "general"}),
    "pre_review": frozenset({"review", "general"}),
    "planner": frozenset({"planner", "general"}),
}

# Historical user column -> (kind column, agent column).
LEGACY_COLUMNS: dict[str, tuple[str, str]] = {
    "submitted_by": ("submitted_by_kind", "submitted_by_agent_id"),
    "selected_by": ("selected_by_kind", "selected_by_agent_id"),
    "adopted_by": ("adopted_by_kind", "adopted_by_agent_id"),
    "actor_id": ("actor_kind", "actor_agent_id"),
    "user_id": ("actor_kind", "agent_id"),
    "created_by": ("created_by_kind", "created_by_agent_id"),
}


class ActorRef(BaseModel):
    """Exactly one of user_id (kind human) or agent_profile_id (kind agent)."""

    model_config = ConfigDict(extra="forbid")

    kind: ActorKind
    user_id: uuid.UUID | None = None
    agent_profile_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> "ActorRef":
        if self.kind == "human" and (self.user_id is None or self.agent_profile_id is not None):
            raise ValueError("a human actor needs user_id only")
        if self.kind == "agent" and (self.agent_profile_id is None or self.user_id is not None):
            raise ValueError("an agent actor needs agent_profile_id only")
        return self

    @property
    def id(self) -> uuid.UUID:
        return self.user_id if self.kind == "human" else self.agent_profile_id  # type: ignore[return-value]

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"


def of_user(user: User | uuid.UUID) -> ActorRef:
    return ActorRef(kind="human", user_id=user if isinstance(user, uuid.UUID) else user.id)


def of_agent(profile: AgentProfile | uuid.UUID) -> ActorRef:
    return ActorRef(kind="agent", agent_profile_id=profile if isinstance(profile, uuid.UUID) else profile.id)


def same(a: ActorRef | None, b: ActorRef | None) -> bool:
    return a is not None and b is not None and a.kind == b.kind and a.id == b.id


def profile_for(db: Session, project_id: uuid.UUID, agent_id: uuid.UUID) -> AgentProfile:
    """An agent profile of this project (404 otherwise: never reveal other projects' agents)."""
    p = db.get(AgentProfile, agent_id)
    if p is None or p.project_id != project_id:
        raise not_found("agent profile")
    return p


def require_supported_runtime(runtime: str | None, *, agent_profile_id: uuid.UUID) -> None:
    """New role bindings may use only retained runtimes; reading frozen actors never calls this guard."""
    runtime = runtime or "native"
    if runtime not in SUPPORTED_RUNTIMES:
        raise AppError(422, "runtime_unavailable", f"the {runtime} executor is not supported",
                       {"agent_profile_id": str(agent_profile_id), "runtime": runtime, "available": list(SUPPORTED_RUNTIMES)})


def validate(
    db: Session,
    project_id: uuid.UUID,
    ref: ActorRef | dict[str, Any],
    *,
    purpose: str | None = None,
    mode: str | None = None,
) -> ActorRef:
    """Check an actor can act in the project.

    human: a project member who is not a viewer. agent: an enabled profile of
    the project with a supported runtime whose purpose fits `purpose` (execute / review / pre_review /
    planner) and whose execution_modes include `mode` when one is given.
    Problems are 422 with a stable code.
    """
    ref = ref if isinstance(ref, ActorRef) else ActorRef.model_validate(ref)
    if ref.kind == "human":
        member = db.get(ProjectMember, {"project_id": project_id, "user_id": ref.user_id})
        if member is None:
            raise AppError(422, "not_a_member", "the person must be a project member", {"user_id": str(ref.user_id)})
        if member.role == "viewer":
            raise AppError(422, "read_only_member", "a viewer cannot execute or review work", {"user_id": str(ref.user_id)})
        return ref
    p = db.get(AgentProfile, ref.agent_profile_id)
    if p is None or p.project_id != project_id:
        raise AppError(422, "agent_not_found", "the agent profile does not belong to this project", {"agent_profile_id": str(ref.agent_profile_id)})
    if not p.enabled:
        raise AppError(422, "agent_disabled", f"agent {p.name} is disabled", {"agent_profile_id": str(p.id)})
    require_supported_runtime(p.runtime, agent_profile_id=p.id)
    if purpose is not None and p.purpose not in PURPOSE_ROLES.get(purpose, frozenset()):
        raise AppError(422, "agent_purpose_mismatch", f"agent {p.name} ({p.purpose}) cannot act as {purpose}",
                       {"agent_profile_id": str(p.id), "purpose": p.purpose, "required": purpose})
    if mode is not None and mode not in (p.execution_modes or []):
        raise AppError(422, "agent_mode_unsupported", f"agent {p.name} is not enabled for {mode}",
                       {"agent_profile_id": str(p.id), "mode": mode})
    return ref


def out(db: Session, kind: str | None, user_id: uuid.UUID | None, agent_id: uuid.UUID | None) -> dict[str, Any] | None:
    """{kind, id, user, agent} view; None when the row has no actor."""
    kind = kind or ("agent" if agent_id and not user_id else "human")
    if kind == "agent":
        if agent_id is None:
            return None
        p = db.get(AgentProfile, agent_id)
        return {
            "kind": "agent",
            "id": str(agent_id),
            "user": None,
            "agent": {"id": str(p.id), "name": p.name, "purpose": p.purpose, "version": p.version, "enabled": p.enabled} if p
            else {"id": str(agent_id), "name": None, "purpose": None, "version": None, "enabled": False},
        }
    if user_id is None:
        return None
    u = db.get(User, user_id)
    return {"kind": "human", "id": str(user_id), "user": {"id": str(user_id), "name": u.name if u else None}, "agent": None}


def out_ref(db: Session, ref: ActorRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return out(db, ref.kind, ref.user_id, ref.agent_profile_id)


def assign(row: Any, prefix: str, ref: ActorRef) -> None:
    """Write a prefix triple (executor_kind / executor_user_id / executor_agent_id …)."""
    setattr(row, f"{prefix}_kind", ref.kind)
    setattr(row, f"{prefix}_user_id", ref.user_id)
    setattr(row, f"{prefix}_agent_id", ref.agent_profile_id)


def read(row: Any, prefix: str) -> ActorRef | None:
    kind = getattr(row, f"{prefix}_kind", None)
    user_id = getattr(row, f"{prefix}_user_id", None)
    agent_id = getattr(row, f"{prefix}_agent_id", None)
    if kind == "agent" and agent_id is not None:
        return ActorRef(kind="agent", agent_profile_id=agent_id)
    if user_id is not None:
        return ActorRef(kind="human", user_id=user_id)
    return None


def assign_legacy(row: Any, human_col: str, ref: ActorRef) -> None:
    """Write a historical user column plus its kind / agent columns."""
    kind_col, agent_col = LEGACY_COLUMNS[human_col]
    setattr(row, human_col, ref.user_id)
    setattr(row, kind_col, ref.kind)
    setattr(row, agent_col, ref.agent_profile_id)


def read_legacy(row: Any, human_col: str) -> ActorRef | None:
    kind_col, agent_col = LEGACY_COLUMNS[human_col]
    kind = getattr(row, kind_col, "human")
    agent_id = getattr(row, agent_col, None)
    user_id = getattr(row, human_col, None)
    if kind == "agent" and agent_id is not None:
        return ActorRef(kind="agent", agent_profile_id=agent_id)
    if user_id is not None:
        return ActorRef(kind="human", user_id=user_id)
    return None


def out_legacy(db: Session, row: Any, human_col: str) -> dict[str, Any] | None:
    kind_col, agent_col = LEGACY_COLUMNS[human_col]
    return out(db, getattr(row, kind_col, "human"), getattr(row, human_col, None), getattr(row, agent_col, None))


def audit_fields(ref: ActorRef | None) -> dict[str, Any]:
    """Keyword arguments for services.audit.audit(): user_id / agent_id."""
    if ref is None:
        return {"user_id": None, "agent_id": None}
    return {"user_id": ref.user_id, "agent_id": ref.agent_profile_id}
