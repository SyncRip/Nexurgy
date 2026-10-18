"""Append-only audit trail."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog


def audit(
    db: Session,
    *,
    action: str,
    subject_type: str,
    subject_id: uuid.UUID | str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            project_id=project_id,
            user_id=user_id,
            action=action,
            subject_type=subject_type,
            subject_id=str(subject_id),
            data=data or {},
        )
    )
