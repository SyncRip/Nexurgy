from __future__ import annotations

import uuid
from datetime import UTC, datetime

from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..errors import AppError, bad_request, conflict, not_found
from ..models import Part, Project, ProjectMember, ProjectMilestone, User
from ..security import current_user, project_access
from ..services import feature_flags as features, lifecycle, stages
from ..services.audit import audit
from .overview import milestones_with_counts
from .schemas import MemberIn, MemberOut, ProjectIn, ProjectOut, ProjectPatch, UserOut

router = APIRouter(tags=["projects"])


class ProjectFullOut(ProjectOut):
    """ProjectOut plus the management fields (design §4.1)."""

    lifecycle: str = "active"
    objective: str = ""
    owner_id: uuid.UUID | None = None
    hold_reason: str | None = None
    version: int = 1
    current_milestone_id: uuid.UUID | None = None
    # New requests use the saved project policy; new projects start with Turbo enabled.
    default_execution_policy: str = "turbo"


class ProjectCreateIn(ProjectIn):
    """ProjectIn plus an optional stage plan (P4): explicit names, or the default list."""

    milestones: list[str] | None = Field(default=None, max_length=30)
    stage_template: Literal["default"] | None = None
    # Language of the default stage names (zh / en); falls back to ?lang= and Accept-Language.
    lang: str | None = Field(default=None, max_length=10)


class ProjectPatchIn(ProjectPatch):
    objective: str | None = Field(default=None, max_length=limits.OBJECTIVE_CHARS)
    owner_id: uuid.UUID | None = None
    expected_version: int | None = None
    # S5: the project's "default Turbo" switch (manage permission, like the other project settings). Off = standard.
    default_execution_policy: Literal["standard", "turbo"] | None = None


class LifecycleIn(BaseModel):
    action: Literal["start", "pause", "resume", "complete", "cancel", "reopen"]
    reason: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    expected_version: int
    # cancel only: also cancel the project's active runs (otherwise they may finish).
    cancel_runs: bool = False
    # reopen only: the stage the project resumes at (required when it has stages).
    milestone_id: uuid.UUID | None = None


class MilestoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    requirements: stages.Requirements | None = None


class MilestonePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=limits.DESCRIPTION_CHARS)
    requirements: stages.Requirements | None = None
    expected_version: int


def _out(p: Project, role: str | None, part_count: int | None = None) -> ProjectFullOut:
    o = ProjectFullOut.model_validate(p)
    o.my_role = role  # type: ignore[assignment]
    o.part_count = part_count
    return o


@router.get("/projects", response_model=list[ProjectFullOut])
def list_projects(
    include_archived: bool = False,
    q: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[ProjectOut]:
    counts = (
        select(Part.project_id, func.count().label("n"))
        .where(Part.archived_at.is_(None))
        .group_by(Part.project_id)
        .subquery()
    )
    stmt = (
        select(Project, ProjectMember.role, counts.c.n)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .outerjoin(counts, counts.c.project_id == Project.id)
        .where(ProjectMember.user_id == user.id)
        .order_by(Project.updated_at.desc())
    )
    if not include_archived:
        stmt = stmt.where(Project.status == "active")
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Project.name.ilike(like) | Project.code.ilike(like))
    return [_out(p, role, n or 0) for p, role, n in db.execute(stmt).all()]


@router.post("/projects", response_model=ProjectFullOut, status_code=201)
def create_project(
    body: ProjectCreateIn,
    lang: str | None = None,
    accept_language: str | None = Header(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """Create a project; `milestones` (names) or `stage_template: "default"`
    adds its stage plan, the first stage becoming current."""
    names = [n.strip() for n in (body.milestones or []) if n and n.strip()]
    if names or body.stage_template == "default":
        features.require_enabled("stages")
    project = Project(code=body.code, name=body.name, description=body.description, created_by=user.id, owner_id=user.id)
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("project_code_taken", f"project code {body.code} is already used")
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    audit(db, action="project.create", subject_type="project", subject_id=project.id, user_id=user.id, project_id=project.id)
    if not names and body.stage_template == "default":
        names = stages.stage_names(body.lang or lang, accept_language)
    if names:
        stages.create_milestones(db, project, names, user.id)
    db.commit()
    return _out(project, "owner", 0)


@router.get("/projects/{project_id}", response_model=ProjectFullOut)
def get_project(project_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProjectOut:
    acc = project_access(db, user, project_id)
    n = db.scalar(select(func.count()).select_from(Part).where(Part.project_id == project_id, Part.archived_at.is_(None)))
    return _out(acc.project, acc.role, n or 0)


@router.patch("/projects/{project_id}", response_model=ProjectFullOut)
def patch_project(project_id: uuid.UUID, body: ProjectPatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProjectOut:
    acc = project_access(db, user, project_id, "manage")
    data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    for key in ("name", "objective", "default_execution_policy"):
        if key in data and data[key] is None:
            raise AppError(422, f"{key}_required", f"{key} cannot be empty")
    p = lifecycle.lock_project(db, project_id, share=False)
    if "default_execution_policy" in data and body.expected_version is None:
        raise AppError(422, "validation_error", "expected_version is required when changing project policy")
    if body.expected_version is not None and body.expected_version != p.version:
        raise conflict("version_conflict", "the project was changed by someone else; reload first", current_version=p.version)
    if ("objective" in data or "owner_id" in data) and p.lifecycle not in lifecycle.PLANNABLE:
        raise conflict("project_closed", f"the project is {p.lifecycle}; reopen it before changing its objective or owner", lifecycle=p.lifecycle)
    if data.get("owner_id") and db.get(ProjectMember, {"project_id": project_id, "user_id": data["owner_id"]}) is None:
        raise AppError(422, "not_a_member", "the project owner must be a project member")
    changed = {k: v for k, v in data.items() if getattr(p, k) != v}
    for k, v in changed.items():
        setattr(p, k, v)
    if changed:
        p.version += 1
    audit(db, action="project.update", subject_type="project", subject_id=project_id, user_id=user.id, project_id=project_id,
          data={k: str(v) if v is not None else None for k, v in changed.items()})
    db.commit()
    return _out(p, acc.role)


@router.post("/projects/{project_id}/lifecycle", response_model=ProjectFullOut)
def project_lifecycle(project_id: uuid.UUID, body: LifecycleIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProjectOut:
    acc = project_access(db, user, project_id, "manage")
    p = lifecycle.transition(db, user, project_id, action=body.action, reason=body.reason, expected_version=body.expected_version,
                             cancel_runs=body.cancel_runs, milestone_id=body.milestone_id)
    db.commit()
    return _out(p, acc.role)


@router.get("/projects/{project_id}/milestones")
def list_milestones(project_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    project_access(db, user, project_id)
    return milestones_with_counts(db, project_id)[0]


@router.post("/projects/{project_id}/milestones", status_code=201)
def create_milestone(project_id: uuid.UUID, body: MilestoneIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id, "manage")
    features.require_enabled("stages")
    p = lifecycle.lock_project(db, project_id, share=False)  # serialises seq
    lifecycle.require_plannable(p)
    seq = (db.scalar(select(func.max(ProjectMilestone.seq)).where(ProjectMilestone.project_id == project_id)) or 0) + 1
    m = ProjectMilestone(project_id=project_id, seq=seq, name=body.name, description=body.description,
                         requirements=body.requirements.model_dump(mode="json") if body.requirements else {})
    db.add(m)
    db.flush()
    stages.adopt_new_stage(db, p, m)  # first stage, or a stage added after the last one was accepted
    audit(db, action="milestone.create", subject_type="milestone", subject_id=m.id, user_id=user.id, project_id=project_id, data={"name": m.name, "seq": seq})
    db.commit()
    return stages.milestone_view(db, m)


@router.patch("/milestones/{milestone_id}")
def patch_milestone(milestone_id: uuid.UUID, body: MilestonePatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Name, description, requirements ({required_asset_types}); an accepted stage keeps its requirements."""
    _, acc = stages.load_milestone(db, user, milestone_id, "manage")
    data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    for key in ("name", "description"):
        if key in data and data[key] is None:
            data.pop(key)
    m = stages.update_milestone(db, acc, milestone_id, data, body.expected_version)
    db.commit()
    return stages.milestone_view(db, m)


@router.delete("/milestones/{milestone_id}", status_code=204)
def delete_milestone(milestone_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    """Only a stage without tasks and without reviews (409 milestone_in_use)."""
    _, acc = stages.load_milestone(db, user, milestone_id, "manage")
    stages.delete_milestone(db, acc, milestone_id)
    db.commit()


@router.post("/projects/{project_id}/archive", response_model=ProjectFullOut)
def archive_project(project_id: uuid.UUID, restore: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProjectOut:
    acc = project_access(db, user, project_id, "manage")
    if not restore:
        # Archive needs every run finished (design §4.1); restoring never changes the lifecycle.
        lifecycle.lock_project(db, project_id, share=False)
        from ..models import WorkflowRun

        n = db.scalar(select(func.count()).select_from(WorkflowRun).where(WorkflowRun.project_id == project_id, WorkflowRun.status.in_(lifecycle.ACTIVE_RUN)))
        if n:
            raise conflict("active_runs", "finish or cancel the active runs before archiving", active_runs=n)
    acc.project.status = "active" if restore else "archived"
    acc.project.archived_at = None if restore else datetime.now(UTC)
    audit(db, action="project.restore" if restore else "project.archive", subject_type="project", subject_id=project_id, user_id=user.id, project_id=project_id)
    db.commit()
    return _out(acc.project, acc.role)


class MemberCapIn(MemberIn):
    # Extra grants on top of the role; omitted = keep the current ones.
    capabilities: list[Literal["template_manager"]] | None = Field(default=None, max_length=5)


class MemberCapOut(MemberOut):
    capabilities: list[str] = Field(default_factory=list)


@router.get("/projects/{project_id}/members", response_model=list[MemberCapOut])
def list_members(project_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[MemberCapOut]:
    project_access(db, user, project_id)
    rows = db.execute(
        select(User, ProjectMember.role, ProjectMember.capabilities).join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project_id).order_by(User.name)
    ).all()
    return [MemberCapOut(user=UserOut.model_validate(u), role=r, capabilities=list(caps or [])) for u, r, caps in rows]


@router.put("/projects/{project_id}/members", response_model=MemberCapOut)
def upsert_member(project_id: uuid.UUID, body: MemberCapIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MemberCapOut:
    project_access(db, user, project_id, "manage")
    target = db.scalar(select(User).where(func.lower(User.email) == body.email))
    if target is None:
        raise not_found("user")
    member = db.get(ProjectMember, {"project_id": project_id, "user_id": target.id})
    capabilities = list(dict.fromkeys(body.capabilities)) if body.capabilities is not None else list(member.capabilities or []) if member else []
    if body.role == "viewer":  # read-only: capabilities never apply (an explicit request is an error)
        if body.capabilities:
            raise AppError(422, "read_only_member", "a viewer cannot hold extra capabilities")
        capabilities = []
    if member is None:
        member = ProjectMember(project_id=project_id, user_id=target.id, role=body.role, capabilities=capabilities)
        db.add(member)
    else:
        if member.role == "owner" and body.role != "owner":
            _ensure_other_owner(db, project_id, target.id)
        member.role = body.role
        member.capabilities = capabilities
    audit(db, action="member.set", subject_type="user", subject_id=target.id, user_id=user.id, project_id=project_id,
          data={"role": body.role, "capabilities": capabilities})
    db.commit()
    return MemberCapOut(user=UserOut.model_validate(target), role=body.role, capabilities=capabilities)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(project_id: uuid.UUID, user_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    project_access(db, user, project_id, "manage")
    member = db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
    if member is None:
        raise not_found("member")
    if member.role == "owner":
        _ensure_other_owner(db, project_id, user_id)
    db.delete(member)
    audit(db, action="member.remove", subject_type="user", subject_id=user_id, user_id=user.id, project_id=project_id)
    db.commit()


def _ensure_other_owner(db: Session, project_id: uuid.UUID, leaving: uuid.UUID) -> None:
    others = db.scalar(
        select(func.count()).select_from(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.role == "owner", ProjectMember.user_id != leaving
        )
    )
    if not others:
        raise bad_request("last_owner", "a project must keep at least one owner")
