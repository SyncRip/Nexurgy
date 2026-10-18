from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import conflict, not_found
from ..models import Drawing, File, FileVersion, ModelRevision, Part, User
from ..security import ProjectAccess, current_user, project_access
from ..services import assets
from ..services import drawings as drawing_svc
from ..services import parts as part_svc
from ..services.audit import audit
from .schemas import PartCopyIn, PartIn, PartOut, PartPatch

router = APIRouter(tags=["parts"])


def load_part(db: Session, user: User, part_id: uuid.UUID, action: str = "view") -> tuple[Part, ProjectAccess]:
    part = db.get(Part, part_id)
    if part is None:
        raise not_found("part")
    acc = project_access(db, user, part.project_id, action)
    return part, acc


def _latest_revisions(db: Session, part_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    if not part_ids:
        return {}
    sub = (
        select(ModelRevision.part_id, func.max(ModelRevision.rev_no).label("rev_no"))
        .where(ModelRevision.part_id.in_(part_ids))
        .group_by(ModelRevision.part_id)
        .subquery()
    )
    rows = db.scalars(
        select(ModelRevision).join(sub, (sub.c.part_id == ModelRevision.part_id) & (sub.c.rev_no == ModelRevision.rev_no))
    ).all()
    return {
        r.part_id: {
            "id": str(r.id),
            "rev_no": r.rev_no,
            "source": r.source,
            "status": r.status,
            "mesh_only": r.mesh_only,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    }


def part_out(db: Session, part: Part) -> PartOut:
    out = PartOut.model_validate(part)
    out.latest_revision = _latest_revisions(db, [part.id]).get(part.id)
    return out


@router.get("/projects/{project_id}/parts", response_model=list[PartOut])
def list_parts(
    project_id: uuid.UUID,
    q: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    folder: str | None = None,
    include_archived: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[PartOut]:
    project_access(db, user, project_id)
    stmt = select(Part).where(Part.project_id == project_id).order_by(Part.updated_at.desc())
    if not include_archived:
        stmt = stmt.where(Part.archived_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Part.number.ilike(like), Part.name.ilike(like), Part.description.ilike(like)))
    if status:
        stmt = stmt.where(Part.status == status)
    if tag:
        stmt = stmt.where(Part.tags.contains([tag]))
    if folder is not None:
        stmt = stmt.where(Part.folder == folder)
    parts = db.scalars(stmt).all()
    latest = _latest_revisions(db, [p.id for p in parts])
    outs = []
    for p in parts:
        o = PartOut.model_validate(p)
        o.latest_revision = latest.get(p.id)
        outs.append(o)
    return outs


@router.post("/projects/{project_id}/parts", response_model=PartOut, status_code=201)
def create_part(project_id: uuid.UUID, body: PartIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PartOut:
    part = part_svc.create_part(db, user, project_id, body.model_dump())
    db.commit()
    return part_out(db, part)


@router.get("/parts/{part_id}", response_model=PartOut)
def get_part(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PartOut:
    part, _ = load_part(db, user, part_id)
    return part_out(db, part)


@router.patch("/parts/{part_id}", response_model=PartOut)
def patch_part(part_id: uuid.UUID, body: PartPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PartOut:
    part, acc = load_part(db, user, part_id, "design")
    data = body.model_dump(exclude_unset=True)
    if "owner_id" in data and data["owner_id"] is not None:
        part_svc.check_member(db, part.project_id, data["owner_id"])
    for k, v in data.items():
        setattr(part, k, v)
    audit(db, action="part.update", subject_type="part", subject_id=part.id, user_id=user.id, project_id=part.project_id, data=body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return part_out(db, part)


@router.post("/parts/{part_id}/archive", response_model=PartOut)
def archive_part(part_id: uuid.UUID, restore: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PartOut:
    part, _ = load_part(db, user, part_id, "design")
    part.archived_at = None if restore else datetime.now(UTC)
    audit(db, action="part.restore" if restore else "part.archive", subject_type="part", subject_id=part.id, user_id=user.id, project_id=part.project_id)
    db.commit()
    return part_out(db, part)


@router.post("/parts/{part_id}/copy", response_model=PartOut, status_code=201)
def copy_part(part_id: uuid.UUID, body: PartCopyIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PartOut:
    """Copy a part with its files, model revisions and generated drawings.

    Provenance is kept (source_part_id / source_file_id / source_revision_id).
    Process plans, quotes and approvals are NOT copied: a copy must be
    reviewed again before it can count as confirmed.
    """
    src, _ = load_part(db, user, part_id, "view")
    project_access(db, user, body.target_project_id, "design")
    number = body.number or (src.number if body.target_project_id != src.project_id else f"{src.number}-COPY")
    dst = Part(
        project_id=body.target_project_id,
        number=number,
        name=src.name,
        material=src.material,
        status="draft",
        owner_id=user.id,
        tags=list(src.tags),
        folder=src.folder,
        description=src.description,
        source_part_id=src.id,
        op_counter=src.op_counter,
    )
    db.add(dst)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("part_number_taken", f"part number {number} already exists in the target project")

    fv_map: dict[uuid.UUID, uuid.UUID] = {}
    for f in db.scalars(select(File).where(File.part_id == src.id, File.archived_at.is_(None))).all():
        nf = File(project_id=dst.project_id, part_id=dst.id, kind=f.kind, name=f.name, source_file_id=f.id, created_by=user.id)
        db.add(nf)
        db.flush()
        for v in db.scalars(select(FileVersion).where(FileVersion.file_id == f.id).order_by(FileVersion.version)).all():
            nv = FileVersion(file_id=nf.id, version=v.version, blob_sha256=v.blob_sha256, original_name=v.original_name, note=v.note, created_by=user.id)
            db.add(nv)
            db.flush()
            fv_map[v.id] = nv.id

    rev_map: dict[uuid.UUID, uuid.UUID] = {}
    for r in db.scalars(select(ModelRevision).where(ModelRevision.part_id == src.id).order_by(ModelRevision.rev_no)).all():
        nr = ModelRevision(
            part_id=dst.id,
            rev_no=r.rev_no,
            parent_id=rev_map.get(r.parent_id) if r.parent_id else None,
            source="copy",
            base=r.base,
            ops=r.ops,
            status=r.status,
            build_key=r.build_key,
            kernel_version=r.kernel_version,
            metrics=r.metrics,
            features=r.features,
            holes=r.holes,
            errors=r.errors,
            warnings=r.warnings,
            step_sha=r.step_sha,
            glb_sha=r.glb_sha,
            mesh_sha=r.mesh_sha,
            mesh_only=r.mesh_only,
            material=r.material,
            request_text=r.request_text,
            note=r.note,
            source_revision_id=r.id,
            source_file_version_id=fv_map.get(r.source_file_version_id) if r.source_file_version_id else None,
            requirements=r.requirements,  # same STEP bytes, so the frozen requirements and check reports still apply
            checks=r.checks,
            created_by=user.id,
        )
        db.add(nr)
        db.flush()
        rev_map[r.id] = nr.id

    for d in db.scalars(select(Drawing).where(Drawing.part_id == src.id).order_by(Drawing.created_at, Drawing.id)).all():
        new_rev = rev_map.get(d.model_revision_id) if d.model_revision_id else None
        db.add(
            Drawing(
                part_id=dst.id,
                model_revision_id=new_rev,
                kind=d.kind,
                file_version_id=fv_map.get(d.file_version_id) if d.file_version_id else None,
                svg_sha=d.svg_sha,
                dxf_sha=d.dxf_sha,
                config=d.config,
                views=d.views,
                # same files; manifest re-bound to the copied revision (new hash, no approvals carried)
                **drawing_svc.copied_package(d, new_rev),
                created_by=user.id,
            )
        )
    assets.register_part_history(db, dst.id)  # copies get their own assets (source_asset_id = original)
    audit(db, action="part.copy", subject_type="part", subject_id=dst.id, user_id=user.id, project_id=dst.project_id, data={"source_part_id": str(src.id)})
    db.commit()
    return part_out(db, dst)
