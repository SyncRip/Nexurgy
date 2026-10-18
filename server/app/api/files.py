from __future__ import annotations

import mimetypes
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select, union
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..errors import bad_request, not_found
from ..models import Blob, Drawing, File, FileVersion, ModelRevision, NodeRun, Part, ProjectMember, User, WorkflowRun
from ..security import current_user, project_access
from ..services import assets
from ..services.audit import audit
from ..storage import read_blob, save_blob
from .schemas import FileKind, FileOut, FileVersionOut

router = APIRouter(tags=["files"])

_EXT_KIND = {
    ".step": "model_step",
    ".stp": "model_step",
    ".stl": "model_mesh",
    ".obj": "model_mesh",
    ".glb": "model_mesh",
    ".pdf": "drawing_pdf",
    ".png": "drawing_image",
    ".jpg": "drawing_image",
    ".jpeg": "drawing_image",
    ".webp": "drawing_image",
    ".dxf": "drawing_dxf",
}


def guess_kind(filename: str) -> str:
    name = filename.lower()
    for ext, kind in _EXT_KIND.items():
        if name.endswith(ext):
            return kind
    return "other"


def content_type_for(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".step", ".stp")):
        return "model/step"
    if lower.endswith(".glb"):
        return "model/gltf-binary"
    if lower.endswith(".stl"):
        return "model/stl"
    if lower.endswith(".dxf"):
        return "image/vnd.dxf"
    if lower.endswith(".dwg"):
        return "image/vnd.dwg"  # kind stays "other" (ck_files_kind has no DWG kind); readers verify the bytes (require_dwg)
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _file_out(db: Session, f: File) -> FileOut:
    out = FileOut.model_validate(f)
    rows = db.execute(
        select(FileVersion).where(FileVersion.file_id == f.id).order_by(FileVersion.version.desc())
    ).scalars().all()
    versions = []
    for v in rows:
        vo = FileVersionOut.model_validate(v)
        blob = db.get(Blob, v.blob_sha256)
        vo.size = blob.size if blob else None
        vo.content_type = blob.content_type if blob else None
        versions.append(vo)
    out.versions = versions
    return out


async def _read_upload(upload: UploadFile) -> bytes:
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise bad_request("file_too_large", f"file exceeds {get_settings().max_upload_mb} MB")
    if not data:
        raise bad_request("empty_file", "file is empty")
    return data


@router.post("/projects/{project_id}/files", response_model=FileOut, status_code=201)
async def upload_file(
    project_id: uuid.UUID,
    file: UploadFile,
    kind: Annotated[FileKind | None, Form()] = None,
    part_id: Annotated[uuid.UUID | None, Form()] = None,
    note: Annotated[str, Form(max_length=2000)] = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileOut:
    project_access(db, user, project_id, "design")
    if part_id is not None:
        part = db.get(Part, part_id)
        if part is None or part.project_id != project_id:
            raise not_found("part")
    name = (file.filename or "upload.bin")[:300]
    data = await _read_upload(file)
    sha = save_blob(db, data, content_type_for(name))
    f = File(project_id=project_id, part_id=part_id, kind=kind or guess_kind(name), name=name, created_by=user.id)
    db.add(f)
    db.flush()
    fv = FileVersion(file_id=f.id, version=1, blob_sha256=sha, original_name=name, note=note, created_by=user.id)
    db.add(fv)
    assets.register_file_version(db, fv)  # document asset file:<id>, version 1
    audit(db, action="file.upload", subject_type="file", subject_id=f.id, user_id=user.id, project_id=project_id, data={"sha256": sha, "name": name})
    db.commit()
    return _file_out(db, f)


@router.post("/files/{file_id}/versions", response_model=FileOut, status_code=201)
async def add_version(
    file_id: uuid.UUID,
    file: UploadFile,
    note: Annotated[str, Form(max_length=2000)] = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileOut:
    f = db.get(File, file_id)
    if f is None:
        raise not_found("file")
    project_access(db, user, f.project_id, "design")
    name = (file.filename or f.name)[:300]
    data = await _read_upload(file)
    sha = save_blob(db, data, content_type_for(name))
    last = db.scalar(select(func.max(FileVersion.version)).where(FileVersion.file_id == f.id)) or 0
    fv = FileVersion(file_id=f.id, version=last + 1, blob_sha256=sha, original_name=name, note=note, created_by=user.id)
    db.add(fv)
    assets.register_file_version(db, fv)
    audit(db, action="file.version", subject_type="file", subject_id=f.id, user_id=user.id, project_id=f.project_id, data={"sha256": sha, "version": last + 1})
    db.commit()
    return _file_out(db, f)


@router.get("/projects/{project_id}/files", response_model=list[FileOut])
def list_files(
    project_id: uuid.UUID,
    part_id: uuid.UUID | None = None,
    kind: str | None = None,
    include_archived: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[FileOut]:
    project_access(db, user, project_id)
    stmt = select(File).where(File.project_id == project_id).order_by(File.created_at.desc())
    if part_id:
        stmt = stmt.where(File.part_id == part_id)
    if kind:
        stmt = stmt.where(File.kind == kind)
    if not include_archived:
        stmt = stmt.where(File.archived_at.is_(None))
    return [_file_out(db, f) for f in db.scalars(stmt).all()]


@router.get("/files/{file_id}", response_model=FileOut)
def get_file(file_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> FileOut:
    f = db.get(File, file_id)
    if f is None:
        raise not_found("file")
    project_access(db, user, f.project_id)
    return _file_out(db, f)


@router.patch("/files/{file_id}", response_model=FileOut)
def patch_file(
    file_id: uuid.UUID,
    name: str | None = None,
    part_id: uuid.UUID | None = None,
    archived: bool | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileOut:
    """Rename, (re)link to a part or archive. Ids stay stable, so engineering
    references (revisions, drawings, plans) never break on rename."""
    f = db.get(File, file_id)
    if f is None:
        raise not_found("file")
    project_access(db, user, f.project_id, "design")
    if name is not None:
        f.name = name[:300]
    if part_id is not None:
        part = db.get(Part, part_id)
        if part is None or part.project_id != f.project_id:
            raise not_found("part")
        f.part_id = part_id
    if archived is not None:
        f.archived_at = datetime.now(UTC) if archived else None
    db.commit()
    return _file_out(db, f)


def _drawing_refs(digest: str):
    """A drawing references a blob through its page-1 alias, any page file of its package (SVG / DXF /
    DWG) or a check report named in its manifest (annotation, DWG provenance)."""
    return (
        (Drawing.svg_sha == digest)
        | (Drawing.dxf_sha == digest)
        | Drawing.pages.contains([{"files": [{"sha256": digest}]}])
        | Drawing.manifest.contains({"checks": {"annotation": {"report_sha256": digest}}})
        | Drawing.manifest.contains({"checks": {"dwg": {"report_sha256": digest}}})
    )


def _revision_refs(digest: str):
    """A revision references a blob through its artefacts or, for a drawing source, the derived DXF of a DWG
    (``checks.source.dwg.derived_dxf.blob_sha256``, shown with the revision's checks)."""
    return (
        (ModelRevision.step_sha == digest)
        | (ModelRevision.glb_sha == digest)
        | (ModelRevision.mesh_sha == digest)
        | ModelRevision.checks.contains({"source": {"dwg": {"derived_dxf": {"blob_sha256": digest}}}})
    )


def _blob_projects(digest: str):
    """Project ids that reference a blob (files, revisions, drawings and their package files, and the derived DXF a
    reconstruction run advertises in its node outputs while it waits for an answer). The JSONB lookups use the GIN
    indexes of migration a1c0ffee0008."""
    via_files = select(File.project_id).join(FileVersion, FileVersion.file_id == File.id).where(FileVersion.blob_sha256 == digest)
    via_revs = select(Part.project_id).join(ModelRevision, ModelRevision.part_id == Part.id).where(_revision_refs(digest))
    via_drawings = select(Part.project_id).join(Drawing, Drawing.part_id == Part.id).where(_drawing_refs(digest))
    via_runs = (select(WorkflowRun.project_id).join(NodeRun, NodeRun.run_id == WorkflowRun.id)
                .where(NodeRun.outputs.contains({"reconstruct": {"dwg": {"derived_dxf_blob": digest}}})))
    return union(via_files, via_revs, via_drawings, via_runs).subquery()


@router.get("/blobs/{digest}")
def get_blob(digest: str, filename: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    projects = _blob_projects(digest)
    allowed = db.scalar(
        select(func.count()).select_from(ProjectMember).where(
            ProjectMember.user_id == user.id, ProjectMember.project_id.in_(select(projects.c.project_id))
        )
    )
    if not allowed:
        raise not_found("blob")
    try:
        data, row = read_blob(db, digest)
    except FileNotFoundError:
        raise not_found("blob")
    headers = {"Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{digest}"'}
    if filename:
        safe = "".join(c for c in filename if c.isalnum() or c in "._- ")[:120] or "download"
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return Response(content=data, media_type=row.content_type, headers=headers)
