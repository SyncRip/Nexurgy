"""Parts: creation shared by POST /projects/{id}/parts and the main agent's create_part tool (AI native, design §2.2).

The rules are the API's: the caller needs ``design`` on the project, the owner must be a project member, a taken number
is 409 ``part_number_taken`` and the creation is audited. Only the main agent omits the number: the next free
``<project code>-<nnn>`` is allocated (numbering is serialised on the project row, as task numbering is).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import conflict, not_found
from ..models import Part, ProjectMember, User
from ..security import project_access
from .audit import audit

#: Width of an allocated part number's sequence ("DEMO-001").
NUMBER_DIGITS = 3


def check_member(db: Session, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if db.get(ProjectMember, {"project_id": project_id, "user_id": user_id}) is None:
        raise not_found("project member")


def next_number(db: Session, project_id: uuid.UUID) -> str:
    """The next ``<project code>-<nnn>`` of the project (after the highest one in use; other numbers are ignored).
    Locks the project row, so two allocations of one project never pick the same number."""
    from .lifecycle import lock_project

    project = lock_project(db, project_id, share=False)
    pattern = re.compile(rf"^{re.escape(project.code)}-(\d+)$")
    used = [int(m.group(1)) for n in db.scalars(select(Part.number).where(Part.project_id == project_id,
                                                                         Part.number.like(f"{project.code}-%"))).all()
            if (m := pattern.match(n))]
    return f"{project.code}-{max(used, default=0) + 1:0{NUMBER_DIGITS}d}"


def create_part(db: Session, user: User, project_id: uuid.UUID, data: dict[str, Any]) -> Part:
    """A new part from the PartIn fields (``number`` empty / omitted: allocated). Flushes; the caller commits."""
    project_access(db, user, project_id, "design")
    data = {k: v for k, v in data.items() if v is not None}
    owner_id = data.pop("owner_id", None) or user.id
    check_member(db, project_id, owner_id)
    if not data.get("number"):
        data["number"] = next_number(db, project_id)
    part = Part(project_id=project_id, owner_id=owner_id, **data)
    try:
        with db.begin_nested():  # a taken number rolls back this insert only
            db.add(part)
            db.flush()
    except IntegrityError:
        raise conflict("part_number_taken", f"part number {data['number']} already exists in this project")
    audit(db, action="part.create", subject_type="part", subject_id=part.id, user_id=user.id, project_id=project_id)
    return part
