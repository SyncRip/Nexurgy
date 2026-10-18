"""In-app notifications (P4 contract §3.6). Rows are written by the outbox
dispatcher (worker loop); users only list their own and mark them read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Notification, OutboxEvent, Project, User
from ..security import current_user
from ..services import outbox

router = APIRouter(tags=["notifications"])


class ReadIn(BaseModel):
    ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    all: bool = False


@router.get("/notifications")
def list_notifications(unread: bool = False, limit: int = 50, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    stmt = (
        select(Notification, OutboxEvent, Project)
        .join(OutboxEvent, OutboxEvent.id == Notification.event_id)
        .outerjoin(Project, Project.id == Notification.project_id)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id)
        .limit(min(max(limit, 1), 200))
    )
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    return [outbox.notification_out(n, ev, p) for n, ev, p in db.execute(stmt).all()]


@router.get("/notifications/unread-count")
def unread_count(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"count": outbox.unread_count(db, user.id)}


@router.post("/notifications/read")
def mark_read(body: ReadIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Mark the given ids (or all) read. Idempotent; other users' rows are never touched."""
    stmt = update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    if not body.all:
        if not body.ids:
            return {"updated": 0, "count": outbox.unread_count(db, user.id)}
        stmt = stmt.where(Notification.id.in_(body.ids))
    n = db.execute(stmt.values(read_at=datetime.now(UTC))).rowcount
    db.commit()
    return {"updated": int(n or 0), "count": outbox.unread_count(db, user.id)}
