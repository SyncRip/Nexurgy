"""Self-owned account settings; running operations carry immutable snapshots."""
from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import conflict, not_found
from ..models import User
from .audit import audit


def out(user: User) -> dict:
    return {"ai_autonomy": user.ai_autonomy, "version": user.preferences_version}


def snapshot(user: User) -> dict:
    return {"v": 1, **out(user)}


def freeze(db: Session, user_id: uuid.UUID) -> dict:
    user = db.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    if user is None:
        raise not_found("user")
    return snapshot(user)


def update(db: Session, user: User, *, ai_autonomy: str, expected_version: int) -> dict:
    row = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    assert row is not None
    if row.preferences_version != expected_version:
        raise conflict("version_conflict", "reload preferences before saving", current_version=row.preferences_version)
    if row.ai_autonomy != ai_autonomy:
        before = row.ai_autonomy
        row.ai_autonomy = ai_autonomy
        row.preferences_version += 1
        audit(db, action="user.preferences.update", subject_type="user", subject_id=row.id, user_id=row.id,
              data={"before": before, "ai_autonomy": ai_autonomy, "version": row.preferences_version})
    return out(row)
