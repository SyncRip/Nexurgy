"""Request / response models shared by the routers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated

from ..models import FILE_KINDS, PART_STATUSES, ROLES

# Lower-cased, simple address check; delivery is never attempted.
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]

Role = Literal["owner", "designer", "process", "quoter", "viewer"]
assert set(Role.__args__) == set(ROLES)  # type: ignore[attr-defined]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    name: str
    is_admin: bool


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    token: str
    user: UserOut


class ProjectIn(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)


class ProjectOut(ORM):
    id: uuid.UUID
    code: str
    name: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    my_role: Role | None = None
    part_count: int | None = None


class MemberIn(BaseModel):
    email: Email
    role: Role


class MemberOut(BaseModel):
    user: UserOut
    role: Role


PartStatus = Literal["draft", "in_design", "in_review", "released", "archived"]
assert set(PartStatus.__args__) == set(PART_STATUSES)  # type: ignore[attr-defined]


class PartIn(BaseModel):
    number: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    material: str = Field(default="AL6061-T651", max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=30)
    folder: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=5000)
    owner_id: uuid.UUID | None = None


class PartPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    material: str | None = Field(default=None, max_length=40)
    status: PartStatus | None = None
    tags: list[str] | None = Field(default=None, max_length=30)
    folder: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    owner_id: uuid.UUID | None = None


class PartOut(ORM):
    id: uuid.UUID
    project_id: uuid.UUID
    number: str
    name: str
    material: str
    status: str
    owner_id: uuid.UUID | None
    tags: list[str]
    folder: str
    description: str
    source_part_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    latest_revision: dict[str, Any] | None = None


class PartCopyIn(BaseModel):
    target_project_id: uuid.UUID
    number: str | None = Field(default=None, min_length=1, max_length=80)


FileKind = Literal["model_step", "model_mesh", "drawing_pdf", "drawing_image", "drawing_dxf", "document", "other"]
assert set(FileKind.__args__) == set(FILE_KINDS)  # type: ignore[attr-defined]


class FileVersionOut(ORM):
    id: uuid.UUID
    file_id: uuid.UUID
    version: int
    blob_sha256: str
    original_name: str
    note: str
    created_at: datetime
    size: int | None = None
    content_type: str | None = None


class FileOut(ORM):
    id: uuid.UUID
    project_id: uuid.UUID
    part_id: uuid.UUID | None
    kind: str
    name: str
    source_file_id: uuid.UUID | None
    created_at: datetime
    archived_at: datetime | None
    versions: list[FileVersionOut] = Field(default_factory=list)
