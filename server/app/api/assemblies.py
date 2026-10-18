"""Static assemblies (W4): assemblies, revisions, instances, mates, BOM, checks, STEP import / export.

Project-scoped like parts: members read (viewer included), owners / designers write, other projects get 404.
Every edit creates a new immutable revision against ``base_revision_id`` (409 ``revision_conflict`` when the
assembly moved on). Execution policy is explicit per request (``standard`` default, ``turbo``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..ai import limits
from .. import policy as pol
from ..db import get_db
from ..services.feature_flags import require_choices
from ..errors import bad_request, not_found
from ..models import Assembly, AssemblyRevision, AssemblyRollup, Part, User
from ..security import ProjectAccess, current_user, project_access
from ..services import assemblies as svc
from ..services import execution_policy as policy_svc
from ..services.audit import audit

router = APIRouter(tags=["assemblies"])

Policy = Literal["standard", "turbo"]


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    components: list[dict[str, Any]] = Field(min_length=1, max_length=svc.MAX_LEAVES)
    mates: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    execution_policy: Policy | None = None
    note: str = Field(default="", max_length=2000)
    run_checks: bool = False
    clearance_mm: float | None = Field(default=None, ge=0)


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_version_id: uuid.UUID
    number: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=200)
    execution_policy: Policy | None = None


class FromSpecIn(BaseModel):
    """An exact W2 spec version of the project (its stored content is used, never client JSON) + part bindings."""

    model_config = ConfigDict(extra="forbid")
    spec_version_id: uuid.UUID
    bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    number: str | None = Field(default=None, max_length=80)
    execution_policy: Policy | None = None


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision_id: uuid.UUID
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    execution_policy: Policy | None = None
    note: str = Field(default="", max_length=2000)
    run_checks: bool = False
    clearance_mm: float | None = Field(default=None, ge=0)


class ImpactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision_id: uuid.UUID
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=limits.DESCRIPTION_CHARS)
    status: Literal["draft", "in_design", "in_review", "released", "archived"] | None = None


class ChecksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_policy: Policy | None = None  # default: the revision's own policy
    clearance_mm: float | None = Field(default=None, ge=0)
    allow: list[list[str]] = Field(default_factory=list, max_length=500)


class RollupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: int = Field(default=1, ge=1, le=1_000_000)
    buy_costs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    assembly_costs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PartPropsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    procurement: Literal["make", "buy", "standard", "unknown"] | None = None
    representation: Literal["exact", "nominal", "simplified"] | None = None
    designation: str | None = Field(default=None, max_length=200)


def _policy(value: str | None) -> str:
    try:
        return pol.parse_policy(value)
    except ValueError as exc:
        raise bad_request("invalid_execution_policy", str(exc))


def _frozen(db: Session, project_id: uuid.UUID, explicit: str | None, source: Any = None) -> pol.PolicyChoice:
    """Freeze the current project default for new work; retain the source version as provenance."""
    require_choices(execution_policy=explicit)
    return policy_svc.resolve(db, project_id, explicit, source=source)


def _record(db: Session, user: User, project_id: uuid.UUID, choice: pol.PolicyChoice, rev: AssemblyRevision, action: str) -> dict:
    policy_svc.record(db, user, project_id, choice, subject_type="assembly_revision", subject_id=rev.id, action=action)
    return choice.out()


def load_assembly(db: Session, user: User, assembly_id: uuid.UUID, action: str = "view") -> tuple[Assembly, ProjectAccess]:
    asm = db.get(Assembly, assembly_id)
    if asm is None:
        raise not_found("assembly")
    return asm, project_access(db, user, asm.project_id, action)


def load_revision(db: Session, user: User, revision_id: uuid.UUID, action: str = "view") -> tuple[AssemblyRevision, Assembly]:
    rev = db.get(AssemblyRevision, revision_id)
    if rev is None:
        raise not_found("assembly revision")
    asm, _ = load_assembly(db, user, rev.assembly_id, action)
    return rev, asm


def _attachment(data: bytes, filename: str, media: str) -> Response:
    ascii_name = "".join(c if c.isascii() and (c.isalnum() or c in "._- ") else "_" for c in filename)[:120] or "download"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=data, media_type=media, headers={"Content-Disposition": disposition, "Cache-Control": "private, max-age=0"})


# ---------------------------------------------------------------------------------------------- #
# Assemblies
# ---------------------------------------------------------------------------------------------- #


@router.get("/projects/{project_id}/assemblies")
def list_assemblies(project_id: uuid.UUID, q: str | None = None, include_archived: bool = False, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> list[dict]:
    project_access(db, user, project_id)
    stmt = select(Assembly).where(Assembly.project_id == project_id).order_by(Assembly.updated_at.desc())
    if not include_archived:
        stmt = stmt.where(Assembly.archived_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Assembly.number.ilike(like), Assembly.name.ilike(like)))
    return [svc.assembly_out(db, a) for a in db.scalars(stmt).all()]


@router.post("/projects/{project_id}/assemblies", status_code=201)
def create_assembly(project_id: uuid.UUID, body: CreateIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id, "design")
    choice = _frozen(db, project_id, body.execution_policy)
    asm, rev = svc.create_assembly(db, project_id=project_id, user=user, number=body.number, name=body.name, description=body.description,
                                   components=body.components, mates=body.mates, policy=choice.policy,
                                   note=body.note, run_heavy_checks=body.run_checks, clearance_mm=body.clearance_mm)
    policy = _record(db, user, project_id, choice, rev, "assembly.create")
    db.commit()
    return {"assembly": svc.assembly_out(db, asm, rev), "revision": svc.revision_out(db, rev), "policy": policy}


@router.post("/projects/{project_id}/assemblies/import", status_code=201)
def import_assembly(project_id: uuid.UUID, body: ImportIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Structured STEP import: shared definitions, hierarchy, names and instance poses are kept."""
    project_access(db, user, project_id, "design")
    choice = _frozen(db, project_id, body.execution_policy)
    asm, rev, summary = svc.import_step(db, project_id=project_id, user=user, file_version_id=body.file_version_id, number=body.number,
                                        name=body.name, policy=choice.policy)
    policy = _record(db, user, project_id, choice, rev, "assembly.import")
    db.commit()
    return {"assembly": svc.assembly_out(db, asm, rev), "revision": svc.revision_brief(rev), "import": summary, "policy": policy}


@router.post("/projects/{project_id}/assemblies/from-spec", status_code=201)
def create_from_spec(project_id: uuid.UUID, body: FromSpecIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Without an explicit policy the assembly uses the current project default; a standard assembly of a Turbo spec
    needs a person to confirm the spec's assumptions first (409
    ``upstream_assumptions_unconfirmed`` with the list and the entry that confirms them in one go)."""
    from ..models import EngineeringSpecVersion

    project_access(db, user, project_id, "design")
    v = db.get(EngineeringSpecVersion, body.spec_version_id)
    choice = _frozen(db, project_id, body.execution_policy, v if v is not None and v.project_id == project_id else None)
    asm, rev, created = svc.create_from_spec_version(db, project_id=project_id, user=user, spec_version_id=body.spec_version_id,
                                                     bindings=body.bindings, policy=choice.policy, number=body.number)
    policy = _record(db, user, project_id, choice, rev, "assembly.from_spec")
    db.commit()
    return {"assembly": svc.assembly_out(db, asm, rev), "revision": svc.revision_out(db, rev), "created": created, "policy": policy}


@router.get("/assemblies/{assembly_id}")
def get_assembly(assembly_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    asm, _ = load_assembly(db, user, assembly_id)
    return svc.assembly_out(db, asm)


@router.patch("/assemblies/{assembly_id}")
def patch_assembly(assembly_id: uuid.UUID, body: PatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    asm, _ = load_assembly(db, user, assembly_id, "design")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            setattr(asm, k, v)
    audit(db, action="assembly.update", subject_type="assembly", subject_id=asm.id, user_id=user.id, project_id=asm.project_id, data=data)
    db.commit()
    return svc.assembly_out(db, asm)


@router.post("/assemblies/{assembly_id}/archive")
def archive_assembly(assembly_id: uuid.UUID, restore: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    asm, _ = load_assembly(db, user, assembly_id, "design")
    asm.archived_at = None if restore else datetime.now(UTC)
    audit(db, action="assembly.restore" if restore else "assembly.archive", subject_type="assembly", subject_id=asm.id, user_id=user.id,
          project_id=asm.project_id)
    db.commit()
    return svc.assembly_out(db, asm)


@router.get("/assemblies/{assembly_id}/revisions")
def list_revisions(assembly_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_assembly(db, user, assembly_id)
    revs = db.scalars(select(AssemblyRevision).where(AssemblyRevision.assembly_id == assembly_id).order_by(AssemblyRevision.rev_no.desc())).all()
    return [svc.revision_brief(r) for r in revs]


@router.post("/assemblies/{assembly_id}/revisions", status_code=201)
def edit_assembly(assembly_id: uuid.UUID, body: EditIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """New revision = base revision + ops (add / remove / suppress / replace / update shared definition / move /
    mates / patterns / quantity). 409 revision_conflict when ``base_revision_id`` is not the latest."""
    asm, _ = load_assembly(db, user, assembly_id, "design")
    base = db.get(AssemblyRevision, body.base_revision_id)  # S5: the change inherits the base revision's policy by default
    choice = _frozen(db, asm.project_id, body.execution_policy, base if base is not None and base.assembly_id == asm.id else None)
    rev = svc.edit_revision(db, assembly_id=assembly_id, user=user, base_revision_id=body.base_revision_id, ops=body.ops,
                            policy=choice.policy, note=body.note, run_heavy_checks=body.run_checks,
                            clearance_mm=body.clearance_mm)
    policy = _record(db, user, asm.project_id, choice, rev, "assembly.edit")
    db.commit()
    return {**svc.revision_out(db, rev), "policy": policy}


@router.post("/assemblies/{assembly_id}/impact")
def impact(assembly_id: uuid.UUID, body: ImpactIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Dry run of an edit: affected instances / BOM lines and the downstream that would go stale."""
    load_assembly(db, user, assembly_id)
    out = svc.impact(db, assembly_id=assembly_id, base_revision_id=body.base_revision_id, ops=body.ops)
    db.rollback()
    return out


@router.get("/assemblies/{assembly_id}/where-used")
def assembly_where_used(assembly_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    load_assembly(db, user, assembly_id)
    return svc.where_used(db, assembly_id=assembly_id)


@router.get("/assemblies/{assembly_id}/downstream")
def assembly_downstream(assembly_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Downstream of every revision; artifacts bound to an older revision are stale."""
    load_assembly(db, user, assembly_id)
    revs = db.scalars(select(AssemblyRevision).where(AssemblyRevision.assembly_id == assembly_id).order_by(AssemblyRevision.rev_no.desc())).all()
    return [svc.downstream(db, r.id) for r in revs]


# ---------------------------------------------------------------------------------------------- #
# Revisions
# ---------------------------------------------------------------------------------------------- #


@router.get("/assembly-revisions/{revision_id}")
def get_revision(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, _ = load_revision(db, user, revision_id)
    return svc.revision_out(db, rev)


@router.get("/assembly-revisions/{revision_id}/scene")
def get_scene(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Tree + unique part meshes (existing GLB / mesh blobs of each part revision) + per-leaf world transforms."""
    rev, _ = load_revision(db, user, revision_id)
    return svc.scene(db, rev.id)


@router.get("/assembly-revisions/{revision_id}/bom")
def get_bom(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, _ = load_revision(db, user, revision_id)
    bom = svc.compute_bom(db, svc.Tree(db, rev.id))
    return {**bom, "revision_id": str(rev.id), "rev_no": rev.rev_no, "bom_sha256": svc.bom_hash(bom), "frozen_bom_sha256": rev.bom_sha256,
            "verification": rev.verification}


@router.get("/assembly-revisions/{revision_id}/datums")
def get_datums(revision_id: uuid.UUID, instance: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Semantic datums of one direct instance's definition (for mate editing)."""
    from ..cad.mates import datum_dict

    rev, _ = load_revision(db, user, revision_id)
    comps, _ = svc.revision_state(db, rev)
    c = next((c for c in comps if c.instance_id == instance), None)
    if c is None:
        raise not_found("instance")
    datums = svc.definition_datums(svc.Tree(db, rev.id), c)
    return {"instance_id": instance, "definition": str(c.def_id), "datums": [datum_dict(d) for d in datums.values()],
            "source": "w3" if _has_w3_datums() else "fallback"}


def _has_w3_datums() -> bool:
    try:
        from ..cad import datums  # type: ignore[attr-defined]  # noqa: F401
    except ImportError:
        return False
    return True


@router.post("/assembly-revisions/{revision_id}/checks")
def run_checks(revision_id: uuid.UUID, body: ChecksIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Run the assembly checks on this exact revision (records are appended; Turbo records skipped_turbo)."""
    rev, _ = load_revision(db, user, revision_id, "design")
    require_choices(execution_policy=body.execution_policy)
    svc.run_checks(db, rev.id, user, policy=_policy(body.execution_policy) if body.execution_policy else None,
                   clearance_mm=body.clearance_mm, allow=body.allow)
    db.commit()
    return svc.revision_out(db, rev)


@router.get("/assembly-revisions/{revision_id}/checks")
def check_history(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, _ = load_revision(db, user, revision_id)
    return {"revision_id": str(rev.id), "check_status": rev.check_status, "verification": rev.verification, "checks": rev.checks or {}}


class ConfirmIn(BaseModel):
    """A person's decision on one AI / rule assumption (W0 §2.2). Leave ``value`` out to confirm the adopted value;
    a different value is recorded as a correction (the change itself is a new revision)."""

    model_config = ConfigDict(extra="forbid")
    value: Any = None
    note: str = Field(default="", max_length=1000)


@router.get("/assembly-revisions/{revision_id}/assumptions")
def assumption_decisions(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """S5: the revision's verification (read live), the person decisions recorded on it, and every AI / rule assumption
    upstream of it (spec version, child assemblies, parts) with its state — the "Confirm assumptions" entry of an assembly."""
    from ..services import verification

    rev, asm = load_revision(db, user, revision_id)
    return {"verification": verification.assembly_verification(db, rev),
            "confirmations": verification.list_checks(db, assembly_revision=rev, confirmations=True),
            "upstream": verification.upstream_assumptions(db, project_id=asm.project_id, source_type="assembly_revision", source_id=rev.id)}


@router.post("/assembly-revisions/{revision_id}/assumptions/{assumption_id}/confirm", status_code=201)
def confirm_assumption(revision_id: uuid.UUID, assumption_id: str, body: ConfirmIn, user: User = Depends(current_user),
                       db: Session = Depends(get_db)) -> dict:
    """Confirm assumptions (S5): a person's confirmation (or correction) of one AI / rule assumption this revision carries, appended
    as a record with who, when and the value (design permission), consistent with part revisions and drawings. A value
    copied from the spec version is decided on that spec version. A correction keeps the revision unverified."""
    from ..services import verification

    load_revision(db, user, revision_id, "design")
    row, rev = svc.confirm_assumption(db, user, revision_id, assumption_id, value=body.value, has_value="value" in body.model_fields_set,
                                      note=body.note)
    db.commit()
    return {"record": verification.check_out(row), "verification": verification.assembly_verification(db, rev)}


@router.get("/assembly-revisions/{revision_id}/compare")
def compare(revision_id: uuid.UUID, against: uuid.UUID | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, _ = load_revision(db, user, revision_id)
    other = against or rev.parent_id
    if other is None:
        raise bad_request("nothing_to_compare", "this is the first revision; give ?against=<revision id>")
    load_revision(db, user, other)
    return svc.compare(db, other, rev.id)


@router.get("/assembly-revisions/{revision_id}/step")
def download_step(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    rev, asm = load_revision(db, user, revision_id)
    data, _ = svc.export_step(db, rev.id)
    db.commit()  # cache the export hash on the revision
    return _attachment(data, f"{asm.number}_A{rev.rev_no}.step", "model/step")


@router.get("/assembly-revisions/{revision_id}/parts.zip")
def download_parts(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    rev, asm = load_revision(db, user, revision_id)
    data, _ = svc.export_parts_zip(db, rev.id)
    db.commit()
    return _attachment(data, f"{asm.number}_A{rev.rev_no}_parts.zip", "application/zip")


@router.get("/assembly-revisions/{revision_id}/downstream")
def revision_downstream(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rev, _ = load_revision(db, user, revision_id)
    return svc.downstream(db, rev.id)


@router.post("/assembly-revisions/{revision_id}/rollups", status_code=201)
def create_rollup(revision_id: uuid.UUID, body: RollupIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Process / quote roll-up of the frozen BOM (unknown stays unknown, estimates labelled)."""
    rev, asm = load_revision(db, user, revision_id)
    project_access(db, user, asm.project_id, "quote")
    out = svc.rollup(db, rev.id, user, quantity=body.quantity, buy_costs=body.buy_costs, assembly_costs=body.assembly_costs)
    db.commit()
    latest = svc.latest_revision(db, rev.assembly_id)
    return {**out, "stale": latest is not None and latest.id != rev.id}


@router.get("/assembly-revisions/{revision_id}/rollups")
def list_rollups(revision_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rev, _ = load_revision(db, user, revision_id)
    latest = svc.latest_revision(db, rev.assembly_id)
    stale = latest is not None and latest.id != rev.id
    rows = db.scalars(select(AssemblyRollup).where(AssemblyRollup.revision_id == rev.id).order_by(AssemblyRollup.created_at.desc())).all()
    return [svc.rollup_out(r, stale) for r in rows]


# ---------------------------------------------------------------------------------------------- #
# Parts in assemblies
# ---------------------------------------------------------------------------------------------- #


def _load_part(db: Session, user: User, part_id: uuid.UUID, action: str = "view") -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise not_found("part")
    project_access(db, user, part.project_id, action)
    return part


@router.get("/parts/{part_id}/where-used")
def part_where_used(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Assembly revisions that use any revision of this part ("update available" when not on the latest)."""
    _load_part(db, user, part_id)
    return svc.where_used(db, part_id=part_id)


@router.get("/parts/{part_id}/assembly-props")
def get_part_props(part_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    part = _load_part(db, user, part_id)
    row = svc.part_props(db, [part.id]).get(str(part.id))
    return {"part_id": str(part.id), "procurement": row.procurement if row else "unknown", "representation": row.representation if row else "exact",
            "designation": row.designation if row else "", "source": row.source if row else {}}


@router.put("/parts/{part_id}/assembly-props")
def put_part_props(part_id: uuid.UUID, body: PartPropsIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    part = _load_part(db, user, part_id, "design")
    row = svc.set_part_props(db, part, user, procurement=body.procurement, representation=body.representation, designation=body.designation,
                             source={"kind": "person", "user_id": str(user.id)})
    audit(db, action="part.assembly_props", subject_type="part", subject_id=part.id, user_id=user.id, project_id=part.project_id,
          data=body.model_dump(exclude_none=True))
    db.commit()
    return {"part_id": str(part.id), "procurement": row.procurement, "representation": row.representation, "designation": row.designation,
            "source": row.source}
