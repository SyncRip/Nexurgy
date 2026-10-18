"""Agent profiles (design §4 C07): list / create per project, read / patch by id.

Reading needs `view`; writing needs `manage` (project owner). PATCH carries
`expected_version` (409 version_conflict). Enum and range problems are 422
with the field located (pydantic); an unusable model configuration is 422
model_config_unusable.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai import limits
from ..ai.tool_registry import REGISTRY
from ..db import get_db
from ..models import User
from ..security import current_user, project_access
from ..services import agent_profiles as svc
from ..services.agent_profiles import AgentLimits, ExecutionMode, KnowledgeScope, Pricing, Purpose, Runtime

router = APIRouter(tags=["agents"])


class AgentProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    name: str = Field(min_length=1, max_length=200)
    purpose: Purpose
    description: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    role_prompt: str = Field(default="", max_length=limits.ROLE_PROMPT_CHARS)
    model_config_id: uuid.UUID | None = None
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)
    execution_modes: list[ExecutionMode] = Field(default_factory=list, max_length=2)
    knowledge_scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    limits: AgentLimits = Field(default_factory=AgentLimits)
    pricing: Pricing | None = None
    # The supported executor and its closed options (services.agent_profiles.RUNTIME_OPTIONS).
    runtime: Runtime = "native"
    runtime_options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AgentProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: Purpose | None = None
    description: str | None = Field(default=None, max_length=limits.DESCRIPTION_CHARS)
    role_prompt: str | None = Field(default=None, max_length=limits.ROLE_PROMPT_CHARS)
    model_config_id: uuid.UUID | None = None
    allowed_tools: list[str] | None = Field(default=None, max_length=20)
    execution_modes: list[ExecutionMode] | None = Field(default=None, max_length=2)
    knowledge_scope: KnowledgeScope | None = None
    # Partial limits merge into the stored ones; explicit null pricing = cost unknown.
    limits: AgentLimits | None = None
    pricing: Pricing | None = None
    runtime: Runtime | None = None
    runtime_options: dict[str, Any] | None = None
    enabled: bool | None = None


def _dump(body: BaseModel, *, exclude_unset: bool) -> dict[str, Any]:
    # python mode keeps UUIDs as UUID objects for db.get(); nested models become dicts.
    return body.model_dump(mode="python", exclude_unset=exclude_unset)


@router.get("/agent-tools")
def list_agent_tools(project_id: uuid.UUID | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """The tool contract (design §7.3): every registered tool, end tools last, as ``{name, description, kind,
    needs_part, produces_assets, args_schema}`` (``args_schema``: the argument model's JSON Schema, exactly what the
    executors give the model). The registry is not project data; with ``project_id`` the caller must be a member."""
    if project_id is not None:
        project_access(db, user, project_id)
    return [spec.contract() for spec in REGISTRY.values()]


@router.get("/projects/{project_id}/agent-profiles")
def list_profiles(
    project_id: uuid.UUID,
    purpose: Purpose | None = None,
    enabled: bool | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project_access(db, user, project_id)
    return svc.profiles_out(db, svc.list_profiles(db, project_id, purpose=purpose, enabled=enabled))


@router.post("/projects/{project_id}/agent-profiles", status_code=201)
def create_profile(project_id: uuid.UUID, body: AgentProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    acc = project_access(db, user, project_id, "manage")
    p = svc.create_profile(db, acc, _dump(body, exclude_unset=False))
    db.commit()
    return svc.profile_out(db, p)


@router.get("/agent-profiles/{profile_id}")
def get_profile(profile_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    p, _ = svc.load_profile(db, user, profile_id)
    return svc.profile_out(db, p)


@router.patch("/agent-profiles/{profile_id}")
def patch_profile(profile_id: uuid.UUID, body: AgentProfilePatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _, acc = svc.load_profile(db, user, profile_id, "manage")
    data = _dump(body, exclude_unset=True)
    data.pop("expected_version", None)
    p = svc.patch_profile(db, acc, profile_id, data, body.expected_version)
    db.commit()
    return svc.profile_out(db, p)
