from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import todos

router = APIRouter(tags=["todos"])


@router.get("/todos")
def list_todos(project_id: uuid.UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
               cursor: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return todos.listing(db, user, project_id=project_id, limit=limit, cursor=cursor)
