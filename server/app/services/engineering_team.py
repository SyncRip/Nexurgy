"""Versioned project AI preferences; execution snapshots resolve reusable skills once."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.engineering_team import (
    BUILTIN_SKILLS,
    ENGINEERING_ROLES,
    TeamConfig,
    TeamSettingsIn,
    TeamSettingsOut,
    default_team_config,
)
from ..errors import conflict
from ..models import ProjectEngineeringTeam, User
from ..security import project_access
from .audit import audit
from .lifecycle import lock_project


def _current(db: Session, project_id: uuid.UUID) -> ProjectEngineeringTeam | None:
    return db.scalar(select(ProjectEngineeringTeam).where(ProjectEngineeringTeam.project_id == project_id)
                     .execution_options(populate_existing=True))


def _config(row: ProjectEngineeringTeam | None) -> TeamConfig:
    return TeamConfig.model_validate(row.config) if row is not None else default_team_config()


def _out(project_id: uuid.UUID, row: ProjectEngineeringTeam | None) -> TeamSettingsOut:
    return TeamSettingsOut(project_id=project_id, version=row.version if row else 0,
                           updated_at=row.updated_at if row else None, config=_config(row),
                           defaults=default_team_config(), builtin_skills=list(BUILTIN_SKILLS))


def get_team(db: Session, user: User, project_id: uuid.UUID) -> TeamSettingsOut:
    """Inspect saved settings or defaults without materializing a row."""
    project_access(db, user, project_id)
    return _out(project_id, _current(db, project_id))


def save_team(db: Session, user: User, project_id: uuid.UUID, body: TeamSettingsIn) -> TeamSettingsOut:
    """The project lock serializes first saves as well as updates; the caller commits."""
    project_access(db, user, project_id, "manage")
    lock_project(db, project_id, share=False)
    row = _current(db, project_id)
    version = row.version if row is not None else 0
    if body.expected_version != version:
        raise conflict("version_conflict", "the engineering team was changed; reload before saving", current_version=version)
    old = _config(row).model_dump(mode="json")
    new = body.config.model_dump(mode="json")
    if old == new:
        return _out(project_id, row)

    changed_roles = [role for role in ENGINEERING_ROLES if old["agents"][role] != new["agents"][role]]
    old_skills = {skill["id"]: skill for skill in old["skills"]}
    new_skills = {skill["id"]: skill for skill in new["skills"]}
    changed_skills = sorted(key for key in old_skills.keys() | new_skills.keys() if old_skills.get(key) != new_skills.get(key))
    if row is None:
        row = ProjectEngineeringTeam(project_id=project_id, version=1, config=new, updated_by=user.id)
        db.add(row)
    else:
        row.version = version + 1
        row.config = new
        row.updated_by = user.id
    db.flush()
    audit(db, action="engineering_team.update", subject_type="engineering_team", subject_id=project_id,
          user_id=user.id, project_id=project_id,
          data={"previous_version": version, "version": row.version, "role_ids": changed_roles, "skill_ids": changed_skills})
    return _out(project_id, row)


def freeze_team(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    """Freeze an authorized root's exact role text and resolved skill contents without writing settings."""
    row = _current(db, project_id)
    config = _config(row)
    skills = {skill.id: {"id": skill.id, "name": skill.name_en, "instructions": skill.instructions}
              for skill in BUILTIN_SKILLS}
    skills.update({skill.id: {"id": skill.id, "name": skill.name, "instructions": skill.instructions}
                   for skill in config.skills})
    agents = {}
    for role in ENGINEERING_ROLES:
        agent = config.agents[role]
        agents[role] = {
            "display_name": agent.display_name,
            "profile": agent.profile,
            "tone": agent.tone,
            "personality": agent.personality,
            "skills": [dict(skills[skill_id]) for skill_id in agent.skill_ids],
        }
    return {"v": 1, "version": row.version if row else 0, "agents": agents}
