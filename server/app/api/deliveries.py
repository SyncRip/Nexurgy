"""Delivery manifests (W8): the required vs actual files of one delivery, their hashes and sources, and the zip.

Project-scoped like assemblies: members read (viewer included), owners / designers create, other projects get 404.
A manifest is immutable; a delivery of an assembly revision that is no longer the latest is reported ``stale`` and
stays downloadable. Thin routes over services/orchestration.py (the workflow node and the agent tool call it too).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..services.feature_flags import require_choices
from ..errors import conflict, not_found
from ..models import Assembly, AssemblyRevision, DeliveryManifest, User
from ..security import current_user, project_access
from ..services import orchestration as orch
from ..services import execution_policy as policy_svc
from ..storage import read_blob

router = APIRouter(tags=["deliveries"])


class DeliveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A direct delivery is new work and uses the current project default.
    execution_policy: Literal["standard", "turbo"] | None = None
    drawing_package_id: uuid.UUID | None = None
    require_drawings: bool = True
    bom_formats: list[Literal["json", "csv"]] = Field(default_factory=lambda: ["json", "csv"], min_length=1)
    # generation reliability M: the STEP protocol every delivered STEP must be in (frozen in the manifest)
    step_protocol: Literal["AP203", "AP214"] = "AP203"


def _load(db: Session, user: User, delivery_id: uuid.UUID) -> DeliveryManifest:
    row = db.get(DeliveryManifest, delivery_id)
    if row is None:
        raise not_found("delivery manifest")
    project_access(db, user, row.project_id)
    return row


def _attachment(data: bytes, filename: str, media: str) -> Response:
    ascii_name = "".join(c if c.isascii() and (c.isalnum() or c in "._- ") else "_" for c in filename)[:120] or "download"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=data, media_type=media, headers={"Content-Disposition": disposition, "Cache-Control": "private, max-age=0"})


@router.get("/projects/{project_id}/delivery-manifests")
def list_deliveries(project_id: uuid.UUID, assembly_revision_id: uuid.UUID | None = None, limit: int = 50,
                    user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    project_access(db, user, project_id)
    stmt = select(DeliveryManifest).where(DeliveryManifest.project_id == project_id)
    if assembly_revision_id is not None:
        stmt = stmt.where(DeliveryManifest.assembly_revision_id == assembly_revision_id)
    rows = db.scalars(stmt.order_by(DeliveryManifest.number.desc()).limit(max(1, min(limit, 200)))).all()
    return [orch.delivery_brief(r) | {"id": str(r.id), "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


@router.post("/assembly-revisions/{revision_id}/delivery-manifests", status_code=201)
def create_delivery(revision_id: uuid.UUID, body: DeliveryIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Deliver an exact assembly revision directly (the same service as the delivery_package node)."""
    rev = db.get(AssemblyRevision, revision_id)
    asm = db.get(Assembly, rev.assembly_id) if rev is not None else None
    if rev is None or asm is None:
        raise not_found("assembly revision")
    project_access(db, user, asm.project_id, "design")
    require_choices(execution_policy=body.execution_policy)
    choice = policy_svc.resolve(db, asm.project_id, body.execution_policy, source=rev)
    pkg = None
    if body.drawing_package_id is not None:
        pkg = orch.load_drawing_package(db, body.drawing_package_id)
        if pkg is None:
            raise not_found("drawing package")
    row = orch.deliver(db, project_id=asm.project_id, user=user, policy=choice.policy, assembly_revision_id=rev.id, drawing_package=pkg,
                       drawings_required=body.require_drawings, bom_formats=body.bom_formats, step_protocol=body.step_protocol,
                       drawing_error=None if pkg is not None else {"code": "drawings_not_given", "message": "no drawing package was given"})
    policy_svc.record(db, user, asm.project_id, choice, subject_type="delivery_manifest", subject_id=row.id, action="delivery.create")
    db.commit()
    return orch.delivery_out(db, row)


@router.get("/delivery-manifests/{delivery_id}")
def get_delivery(delivery_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return orch.delivery_out(db, _load(db, user, delivery_id))


@router.get("/delivery-manifests/{delivery_id}/download")
def download_delivery(delivery_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """The zip: every delivered file and manifest.json (with its own sha256)."""
    row = _load(db, user, delivery_id)
    data, _ = read_blob(db, row.zip_sha256)
    if hashlib.sha256(data).hexdigest() != row.zip_sha256:  # every file inside was hashed into the manifest when written
        raise conflict("delivery_zip_mismatch", "the stored delivery zip does not match its recorded sha256", delivery_id=str(row.id))
    head = (row.manifest or {}).get("sources", {}).get("assembly") or {}
    name = f"{head.get('number') or 'delivery'}_DL-{row.number}_{row.completeness}.zip"
    return _attachment(data, name, "application/zip")


@router.get("/delivery-manifests/{delivery_id}/manifest.json")
def delivery_manifest_json(delivery_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    row = _load(db, user, delivery_id)
    body = json.dumps({**(row.manifest or {}), "manifest_sha256": row.manifest_sha256}, ensure_ascii=False, indent=1, sort_keys=True)
    return _attachment(body.encode("utf-8"), f"DL-{row.number}_manifest.json", "application/json")
