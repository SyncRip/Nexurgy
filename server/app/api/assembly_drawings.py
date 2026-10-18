"""Assembly drawing packages (W5): general-arrangement and detail pages bound to an assembly revision.

Router created by the W0 contract so registration in app/main.py does not conflict; only W5 adds endpoints here.
Permissions follow assemblies: project members read (viewer included), owners / designers generate and edit the
layout, other projects get 404. Packages are immutable; a layout overlay is stored as the next version.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..services.feature_flags import require_choices
from ..models import AssemblyDrawing, User
from ..security import current_user
from ..services import assembly_drawings as svc
from .assemblies import load_assembly, load_revision

router = APIRouter(tags=["assembly-drawings"])

Policy = Literal["standard", "turbo"]
Format = Literal["dxf", "svg", "dwg"]


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # AssemblyDrawingConfig fields (views, section, bom_view, key_dimensions, notes, details, title, …); the formats
    # (DXF + SVG + DWG per page) and the DWG timeout are set by the service.
    config: dict[str, Any] = Field(default_factory=dict)
    execution_policy: Policy | None = None


class OverlayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_manifest_sha256: str | None = Field(default=None, max_length=64)
    edits: list[dict[str, Any]] = Field(min_length=1, max_length=200)


def _readable(db: Session, user: User, package_id: uuid.UUID, action: str = "view") -> AssemblyDrawing:
    row = svc.get_package(db, package_id)
    load_assembly(db, user, row.assembly_id, action)  # non-members: 404 before anything is read
    return row


def _attachment(data: bytes, filename: str, media: str, headers: dict[str, str] | None = None) -> Response:
    ascii_name = "".join(c if c.isascii() and (c.isalnum() or c in "._- ") else "_" for c in filename)[:160] or "download"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store", **(headers or {})})


@router.post("/assembly-revisions/{revision_id}/drawings", status_code=201)
def generate(revision_id: uuid.UUID, body: GenerateIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Generate a package of this exact revision using the current project default for this new request."""
    from ..services import execution_policy as policy_svc

    rev, asm = load_revision(db, user, revision_id, "design")
    require_choices(execution_policy=body.execution_policy)
    choice = policy_svc.resolve(db, asm.project_id, body.execution_policy, source=rev)
    row = svc.generate(db, assembly_revision_id=rev.id, user=user, config=body.config, policy=choice.policy)
    policy_svc.record(db, user, asm.project_id, choice, subject_type="assembly_drawing", subject_id=row.id, action="assembly_drawing.generate")
    db.commit()
    return {**svc.package_out(db, row), "policy": choice.out()}


@router.get("/assembly-revisions/{revision_id}/drawings")
def list_for_revision(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rev, _ = load_revision(db, user, revision_id)
    return svc.packages_out(db, svc.list_packages(db, assembly_id=rev.assembly_id, revision_id=rev.id))


@router.get("/assemblies/{assembly_id}/drawings")
def list_for_assembly(assembly_id: uuid.UUID, revision_id: uuid.UUID | None = None, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> list[dict]:
    asm, _ = load_assembly(db, user, assembly_id)
    return svc.packages_out(db, svc.list_packages(db, assembly_id=asm.id, revision_id=revision_id))


@router.get("/assembly-drawings/{package_id}")
def get_package(package_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return svc.package_out(db, _readable(db, user, package_id))


@router.get("/assembly-drawings/{package_id}/pages/{page_key}/{fmt}")
def download_page_file(package_id: uuid.UUID, page_key: str, fmt: Format, user: User = Depends(current_user),
                       db: Session = Depends(get_db)) -> Response:
    """One page file (``page_key`` is the page key, e.g. ``GA``, ``D2``, ``D2-2``, or its index). 404
    ``page_format_missing`` when that format failed: no other format is served in its place."""
    row = _readable(db, user, package_id)
    data, name, media, sha = svc.page_file(db, row, page_key, fmt)
    return _attachment(data, name, media, {"X-Content-SHA256": sha, "X-Manifest-SHA256": row.manifest_sha256})


@router.get("/assembly-drawings/{package_id}/package.zip")
def download_package(package_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """The whole package: every page file listed in the manifest, the check reports and manifest.json (canonical
    JSON whose sha256 is the package's manifest_sha256), hash-verified."""
    row = _readable(db, user, package_id)
    data = svc.package_zip(db, row)
    return _attachment(data, f"{svc.package_folder(db, row)}_package.zip", "application/zip", {"X-Manifest-SHA256": row.manifest_sha256})


@router.post("/assembly-drawings/{package_id}/continue", status_code=201)
def continue_package(package_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """S3: continue a partial package (recovery ``retry_sheet``): a new package of the same revision, configuration and
    policy that keeps the documents / sheets already complete and produces only the failed ones. 409
    ``package_not_continuable`` (complete, layout version, already continued, generator changed)."""
    row = _readable(db, user, package_id, "design")
    new = svc.continue_package(db, row.id, user)
    db.commit()
    return svc.package_out(db, new)


@router.post("/assembly-drawings/{package_id}/overlays", status_code=201)
def apply_overlay(package_id: uuid.UUID, body: OverlayIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Layout / annotation edits saved as the next version of the package. Engineering edits: 422
    ``engineering_change_required`` (change the model and regenerate instead)."""
    row = _readable(db, user, package_id, "design")
    new = svc.apply_overlay(db, row.id, body.model_dump(exclude_none=True), user)
    db.commit()
    return svc.package_out(db, new)
