"""Current assistant engineering-team preferences, separate from frozen AgentProfiles."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..ai.engineering_team import TeamSettingsIn, TeamSettingsOut
from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import engineering_team

router = APIRouter(tags=["engineering-team"])


@router.get("/projects/{project_id}/engineering-team", response_model=TeamSettingsOut)
def get_engineering_team(
    project_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db),
) -> TeamSettingsOut:
    return engineering_team.get_team(db, user, project_id)


@router.put("/projects/{project_id}/engineering-team", response_model=TeamSettingsOut)
def save_engineering_team(
    project_id: uuid.UUID, body: TeamSettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db),
) -> TeamSettingsOut:
    result = engineering_team.save_team(db, user, project_id, body)
    db.commit()
    return result
