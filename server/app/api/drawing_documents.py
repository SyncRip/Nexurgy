"""Standalone drafting (W6): drawing documents, versions, edit commands and DWG / DXF export.

Router created by the W0 contract so registration in app/main.py does not conflict; only W6 adds endpoints here.
Permissions follow the drawings: project members read (view), owners / designers write (design); a non-member
gets 404 for every document, version and file. Writes are optimistic: the body names the head version it edits
(``base_version_id``) and a stale base is a 409 ``version_conflict``.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai import limits
from ..db import get_db
from ..services.feature_flags import require_choices
from ..errors import not_found
from ..models import DrawingDocument, DrawingDocumentVersion, User
from ..security import current_user, project_access
from ..services import drawing_documents as svc
from ..services import execution_policy as policy_svc

router = APIRouter(tags=["drawing-documents"])

Policy = Literal["standard", "turbo"]


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    number: str | None = Field(None, min_length=1, max_length=80)
    description: str = Field("", max_length=limits.DESCRIPTION_CHARS)
    mode: Literal["empty", "text", "file", "image"] = "empty"
    # None: not chosen — the project default applies (S5; generation reliability N: a form never overrides it silently)
    execution_policy: Policy | None = None
    text: str | None = Field(None, max_length=svc.MAX_TEXT_CHARS)
    file_version_id: uuid.UUID | None = None
    sheet: dict[str, Any] | None = None
    model_config_id: uuid.UUID | None = None  # mode text: the explicit model configuration (default otherwise)


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=limits.DESCRIPTION_CHARS)
    status: Literal["draft", "released"] | None = None
    archived: bool | None = None


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version_id: uuid.UUID
    commands: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    execution_policy: Policy | None = None
    note: str = Field("", max_length=2000)


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version_id: uuid.UUID
    version_id: uuid.UUID
    execution_policy: Policy | None = None
    note: str = Field("", max_length=2000)


class CalibrateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version_id: uuid.UUID
    underlay_id: str = Field(min_length=1, max_length=20)
    p1_px: list[float] = Field(min_length=2, max_length=2)
    p2_px: list[float] = Field(min_length=2, max_length=2)
    distance_mm: float = Field(gt=0, le=1e7)
    note: str = Field("", max_length=2000)


def _doc(db: Session, user: User, document_id: uuid.UUID, action: str = "view") -> DrawingDocument:
    doc = db.get(DrawingDocument, document_id)
    if doc is None:
        raise not_found("drawing document")
    project_access(db, user, doc.project_id, action)  # non-members: 404 before anything is read
    return doc


def _version(db: Session, user: User, version_id: uuid.UUID) -> tuple[DrawingDocumentVersion, DrawingDocument]:
    v = db.get(DrawingDocumentVersion, version_id)
    if v is None:
        raise not_found("drawing document version")
    return v, _doc(db, user, v.document_id)


def _created(db: Session, v: DrawingDocumentVersion) -> dict:
    doc = db.get(DrawingDocument, v.document_id)
    assert doc is not None
    return svc.document_out(db, doc, head=v, full=True)


@router.get("/projects/{project_id}/drawing-documents")
def list_documents(project_id: uuid.UUID, include_archived: bool = False, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> list[dict]:
    project_access(db, user, project_id)
    return svc.list_documents(db, project_id, include_archived=include_archived)


@router.post("/projects/{project_id}/drawing-documents", status_code=201)
def create_document(project_id: uuid.UUID, body: DocumentIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id, "design")
    require_choices(model_config_id=body.model_config_id, execution_policy=body.execution_policy)
    choice = policy_svc.resolve(db, project_id, body.execution_policy)
    try:
        v = svc.create_document(db, project_id=project_id, user=user, title=body.title, mode=body.mode, execution_policy=choice.policy,
                                number=body.number, description=body.description, text=body.text, file_version_id=body.file_version_id,
                                sheet=body.sheet, model_config_id=body.model_config_id)
    except svc.DraftingAiError:
        db.commit()  # keep the call ledger of the failed attempt (nothing else was written), then answer with the error
        raise
    policy_svc.record(db, user, project_id, choice, subject_type="drawing_document_version", subject_id=v.id,
                      action="drawing_document.create")
    db.commit()
    return _created(db, v)


@router.get("/drawing-documents/{document_id}")
def get_document(document_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    doc = _doc(db, user, document_id)
    return svc.document_out(db, doc, full=True)


@router.patch("/drawing-documents/{document_id}")
def patch_document(document_id: uuid.UUID, body: DocumentPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _doc(db, user, document_id, "design")
    doc = svc.update_document(db, document_id=document_id, user=user, title=body.title, description=body.description,
                              status=body.status, archived=body.archived)
    db.commit()
    db.refresh(doc)
    return svc.document_out(db, doc)


@router.get("/drawing-documents/{document_id}/versions")
def list_versions(document_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    doc = _doc(db, user, document_id)
    return svc.history(db, doc)


@router.get("/drawing-documents/{document_id}/versions/{version_ref}")
def get_version(document_id: uuid.UUID, version_ref: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    doc = _doc(db, user, document_id)
    return svc.version_out(svc.get_version(db, doc, version_ref))


@router.post("/drawing-documents/{document_id}/versions", status_code=201)
def save_version(document_id: uuid.UUID, body: EditIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    doc = _doc(db, user, document_id, "design")
    require_choices(execution_policy=body.execution_policy)
    choice = policy_svc.resolve(db, doc.project_id, body.execution_policy)
    v = svc.apply_edits(db, document_id=document_id, user=user, base_version_id=body.base_version_id, commands=body.commands,
                        execution_policy=choice.policy, note=body.note)
    policy_svc.record(db, user, doc.project_id, choice, subject_type="drawing_document_version", subject_id=v.id,
                      action="drawing_document.edit")
    db.commit()
    return _created(db, v)


@router.post("/drawing-documents/{document_id}/restore", status_code=201)
def restore_version(document_id: uuid.UUID, body: RestoreIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    doc = _doc(db, user, document_id, "design")
    require_choices(execution_policy=body.execution_policy)
    choice = policy_svc.resolve(db, doc.project_id, body.execution_policy)
    v = svc.restore(db, document_id=document_id, user=user, base_version_id=body.base_version_id, version_id=body.version_id,
                    execution_policy=choice.policy, note=body.note)
    policy_svc.record(db, user, doc.project_id, choice, subject_type="drawing_document_version", subject_id=v.id,
                      action="drawing_document.restore")
    db.commit()
    return _created(db, v)


@router.post("/drawing-documents/{document_id}/calibrate", status_code=201)
def calibrate(document_id: uuid.UUID, body: CalibrateIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _doc(db, user, document_id, "design")
    v = svc.calibrate(db, document_id=document_id, user=user, base_version_id=body.base_version_id, underlay_id=body.underlay_id,
                      p1_px=body.p1_px, p2_px=body.p2_px, distance_mm=body.distance_mm, note=body.note)
    db.commit()
    return _created(db, v)


def _download(data: bytes, name: str, content_type: str, v: DrawingDocumentVersion) -> Response:
    headers = {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store",
               "X-Manifest-SHA256": v.manifest_sha256}
    return Response(content=data, media_type=content_type, headers=headers)


@router.get("/drawing-document-versions/{version_id}/files/{fmt}")
def download_file(version_id: uuid.UUID, fmt: Literal["dxf", "dwg", "manifest"], user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> Response:
    v, _ = _version(db, user, version_id)
    data, name, content_type = svc.version_file(db, v, fmt)
    return _download(data, name, content_type, v)


@router.get("/drawing-document-versions/{version_id}/package.zip")
def download_package(version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    v, doc = _version(db, user, version_id)
    return _download(svc.package_zip(db, v), f"{doc.number}_V{v.version_no}_package.zip", "application/zip", v)


@router.get("/drawing-document-versions/{version_id}/underlays/{underlay_id}")
def underlay_image(version_id: uuid.UUID, underlay_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    v, _ = _version(db, user, version_id)
    data, content_type = svc.underlay_bytes(db, v, underlay_id)
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})
