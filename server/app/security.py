"""Passwords, bearer tokens, current user and project-role checks (design.md §5)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .errors import AppError, forbidden, not_found
from .models import AuthToken, Project, ProjectMember, User

_hasher = PasswordHasher()

# Which roles may perform which action inside a project.
ACTION_ROLES: dict[str, frozenset[str]] = {
    "view": frozenset({"owner", "designer", "process", "quoter", "viewer"}),
    "manage": frozenset({"owner"}),
    "design": frozenset({"owner", "designer"}),
    "process": frozenset({"owner", "process"}),
    "quote": frozenset({"owner", "quoter"}),
    "workflow": frozenset({"owner", "designer", "process", "quoter"}),
    # Maintain and publish workflow templates; members with the template_manager capability too.
    "template": frozenset({"owner"}),
}
STAGE_ACTION = {"design": "design", "process": "process", "quote": "quote"}
# Extra grants on top of a member's role (project_members.capabilities) -> the action they open.
CAPABILITY_ACTIONS = {"template_manager": "template"}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AuthToken(
            user_id=user.id,
            token_hash=_token_digest(token),
            expires_at=datetime.now(UTC) + timedelta(hours=get_settings().token_ttl_hours),
        )
    )
    return token


def revoke_token(db: Session, token: str) -> None:
    row = db.scalar(select(AuthToken).where(AuthToken.token_hash == _token_digest(token)))
    if row and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError(401, "unauthorized", "missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user(token: str = Depends(bearer_token), db: Session = Depends(get_db)) -> User:
    row = db.scalar(select(AuthToken).where(AuthToken.token_hash == _token_digest(token)))
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise AppError(401, "unauthorized", "token is invalid or expired")
    user = db.get(User, row.user_id)
    if user is None:
        raise AppError(401, "unauthorized", "user no longer exists")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise forbidden("administrator rights are required")
    return user


@dataclass(frozen=True)
class ProjectAccess:
    project: Project
    user: User
    role: str
    capabilities: tuple[str, ...] = ()

    def can(self, action: str) -> bool:
        if self.role in ACTION_ROLES[action]:
            return True
        # A capability never makes a viewer a writer.
        return self.role != "viewer" and any(CAPABILITY_ACTIONS.get(c) == action for c in self.capabilities)

    def require(self, action: str) -> None:
        if not self.can(action):
            raise forbidden(action=action, role=self.role)


def project_access(db: Session, user: User, project_id: uuid.UUID, action: str = "view") -> ProjectAccess:
    """Load a project the user belongs to. Non-members get 404, not 403."""
    project = db.get(Project, project_id)
    if project is None:
        raise not_found("project")
    member = db.get(ProjectMember, {"project_id": project_id, "user_id": user.id})
    if member is None:
        raise not_found("project")
    access = ProjectAccess(project=project, user=user, role=member.role, capabilities=tuple(member.capabilities or ()))
    access.require(action)
    if action != "view" and project.status == "archived" and action != "manage":
        raise forbidden("the project is archived")
    return access
