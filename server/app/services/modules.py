"""Reusable process modules (design §6.1, §9.3; contract §3.5).

A module is documentation distilled from a concrete process plan: explicit
applicability (materials, size range, equipment, prerequisites, limits),
parameters, steps and validation scope. Nothing in it is executed. Every
version is an immutable `process_modules` row registered as the next version
of one asset per module lineage (natural_key ``module:<root module id>``).
The source plan is never changed. A module version is reviewed through an
engineering request (stage process) and only an approved version can be
published.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import limits
from ..errors import AppError, conflict, not_found
from ..models import ApprovalRequest, Asset, AssetVersion, Part, ProcessModule, ProcessPlan, ProjectMember, User
from ..security import ACTION_ROLES, STAGE_ACTION, ProjectAccess, project_access
from . import assets, feature_flags as features, outbox
from .audit import audit
from .lifecycle import require_active


class SizeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max: list[float] = Field(default_factory=list, max_length=3)
    min: list[float] | None = Field(default=None, max_length=3)


class Applicability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    materials: list[str] = Field(default_factory=list, max_length=50)
    size_range_mm: SizeRange = Field(default_factory=SizeRange)
    equipment: list[str] = Field(default_factory=list, max_length=50)
    prerequisites: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    limitations: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    unit: str = Field(default="", max_length=40)
    default: float | str | None = None
    min: float | None = None
    max: float | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    machine: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    minutes: float | None = Field(default=None, ge=0)
    # Operation id of the source plan the step was copied from (informative).
    source_op_id: str | None = Field(default=None, max_length=60)


class Validation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    evidence: str = Field(default="", max_length=limits.LONG_NOTE_CHARS)
    validated_plan_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class ModuleIn(BaseModel):
    """Module version content. Fields default to empty so that missing
    essentials come back as one 422 module_incomplete with every gap listed."""

    name: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=limits.LONG_NOTE_CHARS)
    applicability: Applicability = Field(default_factory=Applicability)
    parameters: list[Parameter] = Field(default_factory=list, max_length=100)
    steps: list[Step] = Field(default_factory=list, max_length=200)
    validation: Validation = Field(default_factory=Validation)
    source_plan_id: uuid.UUID | None = None
    source_steps: list[int] | None = Field(default=None, max_length=200)


def _plan_in_project(db: Session, project_id: uuid.UUID, plan_id: uuid.UUID) -> ProcessPlan:
    plan = db.get(ProcessPlan, plan_id)
    part = db.get(Part, plan.part_id) if plan else None
    if plan is None or part is None or part.project_id != project_id:
        raise not_found("process plan")
    return plan


def _copied_steps(plan: ProcessPlan, indices: list[int]) -> list[dict]:
    ops = (plan.plan or {}).get("operations") or []
    bad = [i for i in indices if not isinstance(i, int) or i < 0 or i >= len(ops)]
    if bad:
        raise AppError(422, "invalid_source_steps", "source_steps must be operation indices of the source plan",
                       {"invalid": bad, "operation_count": len(ops)})
    out = []
    for i in indices:
        op = ops[i] or {}
        minutes = (op.get("setup_min") or 0) + (op.get("cycle_min") or 0)
        out.append(Step(name=(op.get("label_en") or op.get("label_zh") or op.get("name_key") or f"Op {i + 1}")[:200],
                        machine=(op.get("machine_id") or op.get("subcontract_id") or "")[:120],
                        description=(op.get("label_zh") or "")[:4000], minutes=round(float(minutes), 2),
                        source_op_id=(str(op.get("op_id")) if op.get("op_id") else None)).model_dump(mode="json"))
    return out


def _content(db: Session, project_id: uuid.UUID, body: ModuleIn) -> dict[str, Any]:
    """Validate and normalise the module content; 422 module_incomplete lists every gap."""
    missing: list[str] = []
    invalid: list[str] = []
    plan = _plan_in_project(db, project_id, body.source_plan_id) if body.source_plan_id else None
    if body.source_steps and plan is None:
        raise AppError(422, "invalid_source_steps", "source_steps need a source_plan_id")
    steps = [s.model_dump(mode="json") for s in body.steps]
    if not steps and plan is not None and body.source_steps:
        steps = _copied_steps(plan, body.source_steps)
    elif plan is not None and body.source_steps:
        _copied_steps(plan, body.source_steps)  # validate the indices even when the steps were edited
    for pid in body.validation.validated_plan_ids:
        _plan_in_project(db, project_id, pid)
    app = body.applicability
    materials = [m.strip() for m in app.materials if m and m.strip()]
    equipment = [e.strip() for e in app.equipment if e and e.strip()]
    if not body.name.strip():
        missing.append("name")
    if not materials:
        missing.append("applicability.materials")
    size_max = app.size_range_mm.max
    if len(size_max) != 3 or any(x <= 0 for x in size_max):
        missing.append("applicability.size_range_mm.max")
    size_min = app.size_range_mm.min
    if size_min is not None and (len(size_min) != 3 or any(x < 0 for x in size_min)
                                 or (len(size_max) == 3 and any(lo > hi for lo, hi in zip(size_min, size_max)))):
        invalid.append("applicability.size_range_mm.min")
    if not equipment:
        missing.append("applicability.equipment")
    if not body.validation.scope.strip():
        missing.append("validation.scope")
    if not steps:
        missing.append("steps")
    for i, prm in enumerate(body.parameters):
        if prm.min is not None and prm.max is not None and prm.min > prm.max:
            invalid.append(f"parameters[{i}]")
    if missing or invalid:
        raise AppError(422, "module_incomplete", "the module needs its applicability, steps and validation scope", {"missing": missing, "invalid": invalid})
    size: dict[str, Any] = {"max": list(size_max)}
    if size_min is not None:
        size["min"] = list(size_min)
    return {
        "name": body.name.strip(),
        "summary": body.summary.strip(),
        "applicability": {"materials": materials, "size_range_mm": size, "equipment": equipment,
                          "prerequisites": app.prerequisites.strip(), "limitations": app.limitations.strip()},
        "parameters": [p.model_dump(mode="json") for p in body.parameters],
        "steps": steps,
        "validation": {"scope": body.validation.scope.strip(), "evidence": body.validation.evidence.strip(),
                       "validated_plan_ids": [str(x) for x in dict.fromkeys(body.validation.validated_plan_ids)]},
        "source_plan_id": plan.id if plan else None,
        "source_steps": list(body.source_steps or []),
    }


# --- create / new version ------------------------------------------------------------


def create_module(db: Session, acc: ProjectAccess, body: ModuleIn) -> AssetVersion:
    """First version of a new module (its own asset). Caller has `process` access and commits."""
    acc.require("process")
    features.require_enabled("process_modules")
    require_active(db, acc.project.id, what="creating a process module")
    content = _content(db, acc.project.id, body)
    row = ProcessModule(id=uuid.uuid4(), project_id=acc.project.id, created_by=acc.user.id, **content)
    db.add(row)
    db.flush()
    v = assets.register_module(db, row, root_id=row.id)
    audit(db, action="module.create", subject_type="asset_version", subject_id=v.id, user_id=acc.user.id, project_id=acc.project.id,
          data={"module_id": str(row.id), "asset_id": str(v.asset_id), "name": row.name,
                "source_plan_id": str(row.source_plan_id) if row.source_plan_id else None})
    db.flush()
    return v


def load_module_asset(db: Session, user: User, asset_id: uuid.UUID, action: str = "view") -> tuple[Asset, ProjectAccess]:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.type != "process_module":
        raise not_found("process module")
    return asset, project_access(db, user, asset.project_id, action)


def add_version(db: Session, acc: ProjectAccess, asset: Asset, body: ModuleIn) -> AssetVersion:
    """Next immutable version of a module; its parent is the latest version. Caller commits."""
    acc.require("process")
    features.require_enabled("process_modules")
    require_active(db, asset.project_id, what="changing a process module")
    asset = db.scalar(select(Asset).where(Asset.id == asset.id).with_for_update().execution_options(populate_existing=True))
    assert asset is not None
    if asset.archived_at is not None:
        raise conflict("asset_archived", "the module is archived")
    latest = assets.latest_version(db, asset.id)
    root = uuid.UUID(asset.natural_key.split(":", 1)[1])
    content = _content(db, asset.project_id, body)
    row = ProcessModule(id=uuid.uuid4(), project_id=asset.project_id, created_by=acc.user.id, **content)
    db.add(row)
    db.flush()
    v = assets.register_module(db, row, root_id=root, parent_version_id=latest.id if latest else None)
    if asset.name != row.name:
        asset.name = row.name[:300]
    audit(db, action="module.version", subject_type="asset_version", subject_id=v.id, user_id=acc.user.id, project_id=asset.project_id,
          data={"module_id": str(row.id), "asset_id": str(asset.id), "version_no": v.version_no,
                "parent_version_id": str(latest.id) if latest else None})
    db.flush()
    return v


# --- views -----------------------------------------------------------------------------------


def module_out(row: ProcessModule) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "summary": row.summary,
        "applicability": row.applicability or {},
        "parameters": row.parameters or [],
        "steps": row.steps or [],
        "validation": row.validation or {},
        "source_plan_id": str(row.source_plan_id) if row.source_plan_id else None,
        "source_steps": row.source_steps or [],
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def module_version_out(db: Session, v: AssetVersion) -> dict[str, Any]:
    """{module, version}: the module content and its catalogue version_out."""
    row = db.get(ProcessModule, v.process_module_id) if v.process_module_id else None
    if row is None:
        raise not_found("process module")
    return {"module": module_out(row), "version": assets.version_out(db, v)}


def load_module_version(db: Session, user: User, version_id: uuid.UUID) -> AssetVersion:
    """By asset version id (or, as a convenience, by process module row id)."""
    v = db.get(AssetVersion, version_id)
    if v is None:
        v = assets.version_for(db, "process_module", version_id)
    if v is None or v.process_module_id is None:
        raise not_found("process module version")
    asset = db.get(Asset, v.asset_id)
    assert asset is not None
    project_access(db, user, asset.project_id)
    return v


# --- review ------------------------------------------------------------------------------------


def request_review(db: Session, user: User, version_id: uuid.UUID, assignee_id: uuid.UUID | None) -> ApprovalRequest:
    """Open an engineering (process) request on one exact module version. Caller commits."""
    v = assets.get_version(db, version_id)
    asset = db.get(Asset, v.asset_id)
    assert asset is not None
    project_access(db, user, asset.project_id)  # 404 for non-members first
    if asset.type != "process_module":
        raise AppError(422, "not_reviewable", "only process module versions are reviewed this way; other results are reviewed in their runs")
    project_access(db, user, asset.project_id, "process")
    features.require_enabled("process_modules")
    require_active(db, asset.project_id, what="requesting a module review")
    stage = assets.MODULE_STAGE
    if assignee_id is not None:
        member = db.get(ProjectMember, {"project_id": asset.project_id, "user_id": assignee_id})
        if member is None:
            raise AppError(422, "not_a_member", "the assignee must be a project member", {"user_id": str(assignee_id)})
        if member.role not in ACTION_ROLES[STAGE_ACTION[stage]]:
            raise AppError(422, "assignee_cannot_decide", f"a {member.role} cannot decide a {stage} review")
    v = db.scalar(select(AssetVersion).where(AssetVersion.id == v.id).with_for_update().execution_options(populate_existing=True))
    assert v is not None
    if v.availability != "ready":
        raise conflict("asset_not_ready", "only a ready version can be reviewed", availability=v.availability)
    pending = db.scalar(select(ApprovalRequest).where(ApprovalRequest.asset_version_id == v.id, ApprovalRequest.status == "pending").limit(1))
    if pending is not None:
        raise conflict("request_pending", "this version already has a pending review", request_id=str(pending.id))
    label = assets.human_label(db, "process_module", db.get(ProcessModule, v.process_module_id), v)
    req = ApprovalRequest(id=uuid.uuid4(), project_id=asset.project_id, kind="engineering", stage=stage, asset_version_id=v.id,
                          target_hash=v.content_hash, assignee_id=assignee_id, status="pending", run_id=None, node_run_id=None,
                          title=f"{asset.name} {label} · process review"[:300], created_by=user.id)
    db.add(req)
    db.flush()
    audit(db, action="module.review_request", subject_type="asset_version", subject_id=v.id, user_id=user.id, project_id=asset.project_id,
          data={"request_id": str(req.id), "assignee_id": str(assignee_id) if assignee_id else None})
    outbox.request_opened(db, req, user.id)
    db.flush()
    return req
