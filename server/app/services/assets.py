"""Asset catalogue: one index over the specialised engineering tables.

Every asset version maps one-to-one onto a specialised row (model revision,
drawing, process plan, quote, file version, intake); content stays in those
tables. Registration happens inside the same transaction that creates the
specialised row (design §12.2), is idempotent per row, and keeps the source
labels (simulated / estimated / imported / mesh_reference) that must never be
lost when results are wrapped as assets.

Granularity: part_model / drawing / process_plan / quote / intake are one asset
per part and type (natural_key ``part:<part_id>:<type>``); a document is one
asset per File (natural_key ``file:<file_id>``); a process module is one asset
per module lineage (natural_key ``module:<root module id>``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
import dataclasses
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..errors import bad_request, conflict, not_found
from ..models import (
    Asset,
    AssemblyDrawing,
    AssemblyRevision,
    AssetDependency,
    AssetVersion,
    DeliveryManifest,
    Drawing,
    DrawingDocumentVersion,
    File,
    FileVersion,
    Intake,
    ModelRevision,
    NodeRun,
    Part,
    ProcessModule,
    ProcessPlan,
    Quote,
    RunOutput,
    WorkflowRun,
    User,
)

KIND_FIELD = {
    "part_model": "model_revision_id",
    "drawing": "drawing_id",
    "process_plan": "process_plan_id",
    "quote": "quote_id",
    "document": "file_version_id",
    "intake": "intake_id",
    "process_module": "process_module_id",
    "assembly_model": "assembly_revision_id",
    "delivery_manifest": "delivery_manifest_id",
    "drawing_document": "drawing_document_version_id",  # W6: one asset per standalone drawing document
    "assembly_drawing": "assembly_drawing_id",
}
KIND_MODEL: dict[str, type] = {
    "part_model": ModelRevision,
    "drawing": Drawing,
    "process_plan": ProcessPlan,
    "quote": Quote,
    "document": FileVersion,
    "intake": Intake,
    "process_module": ProcessModule,
    "assembly_model": AssemblyRevision,
    "delivery_manifest": DeliveryManifest,
    "drawing_document": DrawingDocumentVersion,
    "assembly_drawing": AssemblyDrawing,
}
# Specialised-row kind shown in version_out.target.kind (approval subject names).
TARGET_KIND = {
    "part_model": "model_revision",
    "drawing": "drawing",
    "process_plan": "process_plan",
    "quote": "quote",
    "document": "file_version",
    "intake": "intake",
    "process_module": "process_module",
    "assembly_model": "assembly_revision",
    "delivery_manifest": "delivery_manifest",
    "drawing_document": "drawing_document_version",
    "assembly_drawing": "assembly_drawing",
}
# Engineering asset types -> (approval subject_type, approval stage). Documents have none.
ASSET_SUBJECT = {"part_model": "model_revision", "drawing": "drawing", "intake": "intake", "process_plan": "process_plan", "quote": "quote"}
ASSET_STAGE = {"part_model": "design", "drawing": "design", "intake": "design", "process_plan": "process", "quote": "quote"}
SUBJECT_ASSET = {v: k for k, v in ASSET_SUBJECT.items()}
TYPE_TITLE = {"part_model": "3D model", "drawing": "Drawing", "process_plan": "Process plan", "quote": "Quote", "intake": "Drawing intake",
              "process_module": "Process module", "assembly_model": "Assembly", "drawing_document": "2D drawing",
              "assembly_drawing": "Assembly drawings", "delivery_manifest": "Delivery"}
LABEL_PREFIX = {"part_model": "R", "drawing": "D", "process_plan": "P", "quote": "Q", "intake": "I", "document": "v", "process_module": "M",
                "assembly_model": "A", "drawing_document": "V", "assembly_drawing": "AD", "delivery_manifest": "DL-"}
# Process modules are reviewed through engineering requests (no approval subject): stage process.
MODULE_STAGE = "process"
# Output port of each workflow node type that produces a catalogued result.
OUTPUT_KEY = {"part_model": "model", "drawing": "drawing", "process_plan": "plan", "quote": "quote", "intake": "spec", "document": "bundle"}
# Labels that travel downstream along dependencies (design §5.3: never lost when wrapped). ``unverified`` (W7):
# made by a Turbo run whose engineering checks were skipped; everything derived from it stays unverified.
INHERITED_LABELS = ("simulated", "unverified")


def _canon(data: object) -> str:
    """Same canonical hash as services.approvals, so intake/mesh hashes match approval subject hashes."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _uniq(labels: Iterable[str]) -> list[str]:
    out: list[str] = []
    for label in labels:
        if label and label not in out:
            out.append(label)
    return out


def drawing_identity(row: Drawing) -> str | None:
    """Content identity of a drawing package: ``manifest_sha256`` (every page, format, report hash and the
    source binding). A manifest that no longer hashes to its stored sha gets a distinct mismatch identity,
    so nothing bound to the stored hash (approvals) keeps applying. None for rows from before packages."""
    if row.manifest is None or not row.manifest_sha256:
        return None
    from ..domain.hashing import sha256_hex

    actual = sha256_hex(row.manifest)
    return row.manifest_sha256 if actual == row.manifest_sha256 else f"manifest-mismatch:{actual}"


def content_hash(asset_type: str, row: Any) -> str:
    if asset_type == "part_model":
        return row.build_key or row.step_sha or _canon({"ops": row.ops, "base": row.base, "mesh": row.mesh_sha})
    if asset_type == "drawing":
        return (drawing_identity(row) or row.svg_sha or row.dxf_sha
                or _canon({"file": str(row.file_version_id), "rev": str(row.model_revision_id), "config": row.config}))
    if asset_type in ("process_plan", "quote"):
        return row.inputs_hash
    if asset_type == "document":
        return row.blob_sha256
    if asset_type == "intake":
        return _canon(row.summary)
    if asset_type == "process_module":
        return _canon(module_content(row))
    if asset_type == "assembly_model":
        return row.content_sha256
    if asset_type in ("drawing_document", "assembly_drawing", "delivery_manifest"):
        return row.manifest_sha256
    raise ValueError(f"unknown asset type {asset_type}")


def module_content(row: ProcessModule) -> dict[str, Any]:
    """The hashed content of a process module version (display name included)."""
    return {"name": row.name, "summary": row.summary, "applicability": row.applicability or {}, "parameters": row.parameters or [],
            "steps": row.steps or [], "validation": row.validation or {},
            "source_plan_id": str(row.source_plan_id) if row.source_plan_id else None, "source_steps": row.source_steps or []}


# --- core registration --------------------------------------------------------


def version_for(db: Session, asset_type: str, row_id: uuid.UUID | str | None) -> AssetVersion | None:
    if row_id is None:
        return None
    col = getattr(AssetVersion, KIND_FIELD[asset_type])
    return db.scalar(select(AssetVersion).where(col == uuid.UUID(str(row_id))))


def _asset_for_update(
    db: Session,
    *,
    asset_type: str,
    natural_key: str,
    project_id: uuid.UUID,
    part_id: uuid.UUID | None,
    file_id: uuid.UUID | None,
    name: str,
    owner_id: uuid.UUID | None,
) -> Asset:
    """Get-or-create the asset by natural key and lock it (serialises version_no)."""
    db.execute(
        pg_insert(Asset)
        .values(
            id=uuid.uuid4(),
            project_id=project_id,
            type=asset_type,
            name=name[:300],
            part_id=part_id,
            file_id=file_id,
            natural_key=natural_key,
            owner_id=owner_id,
        )
        .on_conflict_do_nothing()
    )
    asset = db.scalar(select(Asset).where(Asset.natural_key == natural_key).with_for_update())
    assert asset is not None
    return asset


def register_version(
    db: Session,
    *,
    asset_type: str,
    row: Any,
    project_id: uuid.UUID,
    part_id: uuid.UUID | None = None,
    file_id: uuid.UUID | None = None,
    name: str,
    created_by: uuid.UUID | None = None,
    labels: Sequence[str] = (),
    node_run_id: uuid.UUID | None = None,
    depends_on: Sequence[tuple[uuid.UUID, str]] = (),
    parent_version_id: uuid.UUID | None = None,
    availability: str = "ready",
    natural_key: str | None = None,
) -> AssetVersion:
    """Register `row` as the next version of its asset. Idempotent per row.

    `depends_on` holds (to_version_id, relation) edges from the new version.
    `natural_key` overrides the derived key (process modules: module:<root id>).
    Caller owns the transaction (this only flushes).
    """
    if asset_type not in KIND_FIELD:
        raise ValueError(f"unknown asset type {asset_type}")
    if getattr(row, "id", None) is None:
        db.flush()
    existing = version_for(db, asset_type, row.id)
    if existing is not None:
        return existing
    if natural_key is not None:
        pass
    elif asset_type == "process_module":
        natural_key = f"module:{row.id}"
    elif asset_type == "document":
        if file_id is None:
            raise ValueError("document assets need file_id")
        natural_key = f"file:{file_id}"
    else:
        if part_id is None:
            raise ValueError(f"{asset_type} assets need part_id")
        natural_key = f"part:{part_id}:{asset_type}"
    asset = _asset_for_update(
        db,
        asset_type=asset_type,
        natural_key=natural_key,
        project_id=project_id,
        part_id=part_id,
        file_id=file_id if asset_type == "document" else None,
        name=name,
        owner_id=created_by,
    )
    last = db.scalar(select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)) or 0
    v = AssetVersion(
        id=uuid.uuid4(),
        asset_id=asset.id,
        version_no=last + 1,
        parent_version_id=parent_version_id,
        content_hash=content_hash(asset_type, row),
        availability=availability,
        publish_state="draft",
        labels=_uniq(labels),
        node_run_id=node_run_id,
        created_by=created_by,
        **{KIND_FIELD[asset_type]: row.id},
    )
    db.add(v)
    db.flush()
    seen: set[uuid.UUID] = set()
    for to_id, relation in depends_on:
        if to_id is None or to_id == v.id or to_id in seen:
            continue
        seen.add(to_id)
        db.add(AssetDependency(from_version_id=v.id, to_version_id=to_id, relation=relation))
    db.flush()
    return v


def get_version(db: Session, version_id: uuid.UUID | str) -> AssetVersion:
    try:
        vid = uuid.UUID(str(version_id))
    except ValueError:
        raise not_found("asset version")
    v = db.get(AssetVersion, vid)
    if v is None:
        raise not_found("asset version")
    return v


def asset_type_of(db: Session, version: AssetVersion) -> str:
    for asset_type, field in KIND_FIELD.items():
        if getattr(version, field) is not None:
            return asset_type
    asset = db.get(Asset, version.asset_id)
    assert asset is not None
    return asset.type


def specialised(db: Session, version: AssetVersion) -> Any:
    """The specialised row (ModelRevision, Drawing, ProcessPlan, Quote, FileVersion, Intake)."""
    asset_type = asset_type_of(db, version)
    return db.get(KIND_MODEL[asset_type], getattr(version, KIND_FIELD[asset_type]))


def dependencies(db: Session, version_id: uuid.UUID) -> list[AssetDependency]:
    """Outgoing edges: what this version depends on."""
    return list(db.scalars(select(AssetDependency).where(AssetDependency.from_version_id == version_id)).all())


def dependents(db: Session, version_id: uuid.UUID) -> list[AssetDependency]:
    """Incoming edges: which versions depend on this one."""
    return list(db.scalars(select(AssetDependency).where(AssetDependency.to_version_id == version_id)).all())


# --- per-type registration (hooks call these) ----------------------------------


def _inherited(db: Session, version_ids: Iterable[uuid.UUID | None]) -> list[str]:
    out: list[str] = []
    for vid in version_ids:
        if vid is None:
            continue
        v = db.get(AssetVersion, vid)
        if v is not None:
            labels = [label for label in (v.labels or []) if label in INHERITED_LABELS]
            if "unverified" in labels and verified_later(db, v):  # a source verified since: nothing to inherit
                labels.remove("unverified")
            out += labels
    return out


def revision_labels(db: Session, rev: ModelRevision, _depth: int = 0) -> list[str]:
    labels: list[str] = []
    if rev.source == "import":
        labels.append("imported")
    if rev.mesh_only:
        labels += ["mesh_reference", "imported"]
    if isinstance(rev.ai_summary, dict) and rev.ai_summary.get("simulated"):
        labels.append("simulated")
    if isinstance(rev.checks, dict) and rev.checks.get("execution_policy") == "turbo":
        labels.append("unverified")
    # Copies do not carry ai_summary: take the labels from the source revision.
    if rev.source_revision_id and _depth < 20:
        src = db.get(ModelRevision, rev.source_revision_id)
        if src is not None:
            labels += revision_labels(db, src, _depth + 1)
    return _uniq(labels)


def register_revision(db: Session, rev: ModelRevision) -> AssetVersion:
    existing = version_for(db, "part_model", rev.id)
    if existing is not None:
        return existing
    part = db.get(Part, rev.part_id)
    assert part is not None
    parent = ensure_version(db, "part_model", rev.parent_id) if rev.parent_id else None
    deps: list[tuple[uuid.UUID, str]] = []
    if rev.source_file_version_id:
        doc = ensure_version(db, "document", rev.source_file_version_id)
        if doc is not None:
            deps.append((doc.id, "imported_from"))
    v = register_version(
        db,
        asset_type="part_model",
        row=rev,
        project_id=part.project_id,
        part_id=part.id,
        name=f"{part.number} {TYPE_TITLE['part_model']}",
        created_by=rev.created_by,
        labels=revision_labels(db, rev),
        node_run_id=rev.node_run_id,
        depends_on=deps,
        parent_version_id=parent.id if parent else None,
        availability="ready" if rev.status == "ready" else "failed",
    )
    if rev.source_revision_id:  # copy provenance is an asset relation, not a parent version
        asset = db.get(Asset, v.asset_id)
        src = ensure_version(db, "part_model", rev.source_revision_id)
        if asset is not None and src is not None and asset.source_asset_id is None and src.asset_id != asset.id:
            asset.source_asset_id = src.asset_id
    return v


def register_drawing(db: Session, d: Drawing) -> AssetVersion:
    existing = version_for(db, "drawing", d.id)
    if existing is not None:
        return existing
    part = db.get(Part, d.part_id)
    assert part is not None
    deps: list[tuple[uuid.UUID, str]] = []
    model = ensure_version(db, "part_model", d.model_revision_id) if d.model_revision_id else None
    if model is not None:
        deps.append((model.id, "derived_from"))
    doc = ensure_version(db, "document", d.file_version_id) if d.file_version_id else None
    if doc is not None:
        deps.append((doc.id, "uploaded_as"))
    return register_version(
        db,
        asset_type="drawing",
        row=d,
        project_id=part.project_id,
        part_id=part.id,
        name=f"{part.number} {TYPE_TITLE['drawing']}",
        created_by=d.created_by,
        labels=_inherited(db, [model.id if model else None])
        + (["unverified"] if isinstance(d.checks, dict) and d.checks.get("execution_policy") == "turbo" else []),
        node_run_id=d.node_run_id,
        depends_on=deps,
    )


def register_intake(db: Session, intake: Intake) -> AssetVersion:
    existing = version_for(db, "intake", intake.id)
    if existing is not None:
        return existing
    part = db.get(Part, intake.part_id)
    assert part is not None
    deps: list[tuple[uuid.UUID, str]] = []
    doc = ensure_version(db, "document", intake.file_version_id) if intake.file_version_id else None
    if doc is not None:
        deps.append((doc.id, "extracted_from"))
    labels = ["estimated"]  # intake data is always an estimate (no exact geometry)
    if isinstance(intake.extraction, dict) and intake.extraction.get("simulated"):
        labels.append("simulated")
    return register_version(
        db,
        asset_type="intake",
        row=intake,
        project_id=part.project_id,
        part_id=part.id,
        name=f"{part.number} {TYPE_TITLE['intake']}",
        created_by=intake.created_by,
        labels=labels,
        node_run_id=intake.node_run_id,
        depends_on=deps,
    )


def register_plan(db: Session, plan: ProcessPlan) -> AssetVersion:
    existing = version_for(db, "process_plan", plan.id)
    if existing is not None:
        return existing
    part = db.get(Part, plan.part_id)
    assert part is not None
    source = (
        ensure_version(db, "part_model", plan.model_revision_id)
        if plan.model_revision_id
        else ensure_version(db, "intake", plan.intake_id)
        if plan.intake_id
        else None
    )
    labels = _inherited(db, [source.id if source else None])
    if (plan.summary or {}).get("estimated") or (plan.plan or {}).get("estimated"):
        labels.append("estimated")
    return register_version(
        db,
        asset_type="process_plan",
        row=plan,
        project_id=part.project_id,
        part_id=part.id,
        name=f"{part.number} {TYPE_TITLE['process_plan']}",
        created_by=plan.created_by,
        labels=labels,
        node_run_id=plan.node_run_id,
        depends_on=[(source.id, "derived_from")] if source else [],
    )


def register_quote(db: Session, q: Quote) -> AssetVersion:
    existing = version_for(db, "quote", q.id)
    if existing is not None:
        return existing
    part = db.get(Part, q.part_id)
    assert part is not None
    plan = ensure_version(db, "process_plan", q.process_plan_id)
    labels = _inherited(db, [plan.id if plan else None])
    if (q.result or {}).get("estimated"):
        labels.append("estimated")
    return register_version(
        db,
        asset_type="quote",
        row=q,
        project_id=part.project_id,
        part_id=part.id,
        name=f"{part.number} {TYPE_TITLE['quote']}",
        created_by=q.created_by,
        labels=labels,
        node_run_id=q.node_run_id,
        depends_on=[(plan.id, "priced_from")] if plan else [],
    )


def register_file_version(db: Session, fv: FileVersion, *, node_run_id: uuid.UUID | None = None) -> AssetVersion:
    """Document asset (one per File, natural_key file:<id>). `node_run_id` for files a node produced (export)."""
    if fv.id is None:
        db.flush()
    existing = version_for(db, "document", fv.id)
    if existing is not None:
        return existing
    f = db.get(File, fv.file_id)
    assert f is not None
    v = register_version(
        db,
        asset_type="document",
        row=fv,
        project_id=f.project_id,
        part_id=f.part_id,
        file_id=f.id,
        name=f.name,
        created_by=fv.created_by,
        node_run_id=node_run_id,
    )
    if f.source_file_id:  # copied file: keep provenance on the asset
        asset = db.get(Asset, v.asset_id)
        src = db.scalar(select(Asset).where(Asset.natural_key == f"file:{f.source_file_id}"))
        if asset is not None and src is not None and asset.source_asset_id is None:
            asset.source_asset_id = src.id
    return v


def register_module(
    db: Session,
    module: ProcessModule,
    *,
    root_id: uuid.UUID | None = None,
    parent_version_id: uuid.UUID | None = None,
) -> AssetVersion:
    """Process module version: one asset per module lineage (module:<root id>).
    Depends on its source plan (extracted_from) and keeps its estimated / simulated labels."""
    existing = version_for(db, "process_module", module.id)
    if existing is not None:
        return existing
    source = ensure_version(db, "process_plan", module.source_plan_id) if module.source_plan_id else None
    labels = [label for label in labels_of(db, source) if label in ("estimated", "simulated")] if source is not None else []
    return register_version(
        db,
        asset_type="process_module",
        row=module,
        project_id=module.project_id,
        name=module.name or TYPE_TITLE["process_module"],
        created_by=module.created_by,
        labels=labels,
        depends_on=[(source.id, "extracted_from")] if source else [],
        parent_version_id=parent_version_id,
        natural_key=f"module:{root_id or module.id}",
    )


def register_assembly_revision(db: Session, rev: AssemblyRevision) -> AssetVersion:
    """W4: one assembly_model asset per assembly (natural_key assembly:<id>:assembly_model), one version per
    assembly revision. Depends on every exact part model / child assembly version it freezes (uses_definition),
    and on the imported STEP document (imported_from). Turbo revisions carry the ``unverified`` label."""
    from ..models import Assembly, AssemblyComponent

    existing = version_for(db, "assembly_model", rev.id)
    if existing is not None:
        return existing
    asm = db.get(Assembly, rev.assembly_id)
    assert asm is not None
    parent = ensure_version(db, "assembly_model", rev.parent_id) if rev.parent_id else None
    deps: list[tuple[uuid.UUID, str]] = []
    comps = db.scalars(select(AssemblyComponent).where(AssemblyComponent.revision_id == rev.id).order_by(AssemblyComponent.position)).all()
    seen: set[uuid.UUID] = set()
    for c in comps:
        if c.model_revision_id and c.model_revision_id not in seen:
            seen.add(c.model_revision_id)
            v = ensure_version(db, "part_model", c.model_revision_id)
            if v is not None:
                deps.append((v.id, "uses_definition"))
        elif c.child_revision_id and c.child_revision_id not in seen:
            seen.add(c.child_revision_id)
            v = ensure_version(db, "assembly_model", c.child_revision_id)
            if v is not None:
                deps.append((v.id, "uses_definition"))
    if rev.source_file_version_id:
        doc = ensure_version(db, "document", rev.source_file_version_id)
        if doc is not None:
            deps.append((doc.id, "imported_from"))
    labels = ["imported"] if rev.source == "import" else []
    if (rev.verification or {}).get("unverified") and rev.execution_policy == "turbo":
        labels.append("unverified")
    if (rev.spec_ref or {}).get("simulated"):  # built from a spec read by the labelled simulated model (W8)
        labels.append("simulated")
    labels += _inherited(db, [d for d, _ in deps])
    return register_version(
        db,
        asset_type="assembly_model",
        row=rev,
        project_id=asm.project_id,
        name=f"{asm.number} {TYPE_TITLE['assembly_model']}",
        created_by=rev.created_by,
        labels=labels,
        node_run_id=rev.node_run_id,
        depends_on=deps,
        parent_version_id=parent.id if parent else None,
        availability="ready" if rev.status == "ready" else "failed",
        natural_key=f"assembly:{asm.id}:assembly_model",
    )


def register_delivery_manifest(db: Session, row: DeliveryManifest) -> AssetVersion:
    """W8: one delivery_manifest asset per delivered assembly (natural_key assembly:<id>:delivery_manifest; a delivery
    without an assembly gets its own key), one version per manifest. Depends on the exact assembly version it delivers
    (``delivers``) and on every part version it lists (``includes``); the drawing package version too when W5 registers
    one. Inherits simulated / unverified; a partial manifest carries ``partial``."""
    existing = version_for(db, "delivery_manifest", row.id)
    if existing is not None:
        return existing
    deps: list[tuple[uuid.UUID, str]] = []
    asm_key: str | None = None
    if row.assembly_revision_id:
        av = ensure_version(db, "assembly_model", row.assembly_revision_id)
        if av is not None:
            deps.append((av.id, "delivers"))
        rev = db.get(AssemblyRevision, row.assembly_revision_id)
        asm_key = str(rev.assembly_id) if rev is not None else None
    for f in (row.manifest or {}).get("files") or []:
        rid = (f.get("source") or {}).get("model_revision_id") if isinstance(f, dict) else None
        v = ensure_version(db, "part_model", rid) if rid else None
        if v is not None:
            deps.append((v.id, "includes"))
    if row.drawing_package_id and "assembly_drawing" in KIND_FIELD:  # W5 registers its own type when merged
        dv = ensure_version(db, "assembly_drawing", row.drawing_package_id)
        if dv is not None:
            deps.append((dv.id, "includes"))
    # generation supplement E (layered delivery): every sub-assembly revision and sub-assembly drawing package it delivers
    extra: dict[tuple[str, str], None] = {}
    for f in (row.manifest or {}).get("files") or []:
        src = (f.get("source") or {}) if isinstance(f, dict) else {}
        if f.get("item") == "subassembly_step" and src.get("assembly_revision_id"):
            extra[("assembly_model", str(src["assembly_revision_id"]))] = None
        elif f.get("item") == "subassembly_drawings" and src.get("package_id") and "assembly_drawing" in KIND_FIELD:
            extra[("assembly_drawing", str(src["package_id"]))] = None
    for kind, oid in extra:
        v = ensure_version(db, kind, oid)
        if v is not None and all(v.id != d for d, _ in deps):
            deps.append((v.id, "includes"))
    labels = _inherited(db, [d for d, _ in deps])
    if row.simulated:
        labels.append("simulated")
    if row.execution_policy == "turbo" or (row.manifest or {}).get("verification", {}).get("unverified"):
        labels.append("unverified")
    if row.completeness != "complete":
        labels.append("partial")
    head = ((row.manifest or {}).get("sources") or {}).get("assembly") or {}
    name = f"{head.get('number')} {TYPE_TITLE['delivery_manifest']}" if head.get("number") else f"{TYPE_TITLE['delivery_manifest']} DL-{row.number}"
    return register_version(
        db,
        asset_type="delivery_manifest",
        row=row,
        project_id=row.project_id,
        name=name,
        created_by=row.created_by,
        labels=labels,
        node_run_id=row.node_run_id,
        depends_on=deps,
        natural_key=f"assembly:{asm_key}:delivery_manifest" if asm_key else f"delivery:{row.id}",
    )


def register_drawing_document(db: Session, v: DrawingDocumentVersion) -> AssetVersion:
    """W6 standalone drawing document version: one asset per document (natural_key drawing_document:<id>), no part.
    Depends on the document asset of a source file version (redrawn_from) and keeps simulated / imported labels."""
    existing = version_for(db, "drawing_document", v.id)
    if existing is not None:
        return existing
    from ..models import DrawingDocument

    doc = db.get(DrawingDocument, v.document_id)
    assert doc is not None
    parent = ensure_version(db, "drawing_document", v.parent_id) if v.parent_id else None
    deps: list[tuple[uuid.UUID, str]] = []
    for src in v.sources or []:
        if src.get("file_version_id"):
            fv = ensure_version(db, "document", src["file_version_id"])
            if fv is not None:
                deps.append((fv.id, "redrawn_from" if src.get("kind") == "file_version" else "underlay_from"))
    labels = [label for label in (v.labels or []) if label in ("simulated", "imported", "assumed")]
    # Turbo versions, and standard versions that still carry AI / rule values no person decided, are unverified
    # (an inherited label, like W5 packages).
    if v.execution_policy == "turbo" or (v.verification or {}).get("unconfirmed_assumptions"):
        labels.append("unverified")
    labels += [x for x in _inherited(db, [d for d, _ in deps]) if x not in labels]
    return register_version(
        db,
        asset_type="drawing_document",
        row=v,
        project_id=doc.project_id,
        name=f"{doc.number} {doc.title}",
        created_by=v.created_by,
        labels=labels,
        depends_on=deps,
        parent_version_id=parent.id if parent else None,
        availability="ready",
        natural_key=f"drawing_document:{doc.id}",
    )


def register_assembly_drawing(db: Session, d: AssemblyDrawing) -> AssetVersion:
    """W5: one assembly_drawing asset per assembly (natural_key assembly:<id>:assembly_drawing), one version per
    package version. Derived from the exact assembly_model version it was drawn from, so W4's downstream /
    impact report it stale; an overlay version's parent is the version it edited. Turbo packages carry
    ``unverified``, packages with a missing page or format carry ``partial``."""
    from ..models import Assembly

    existing = version_for(db, "assembly_drawing", d.id)
    if existing is not None:
        return existing
    asm = db.get(Assembly, d.assembly_id)
    assert asm is not None
    model = ensure_version(db, "assembly_model", d.assembly_revision_id)
    parent = ensure_version(db, "assembly_drawing", d.parent_id) if d.parent_id else None
    deps: list[tuple[uuid.UUID, str]] = [(model.id, "derived_from")] if model is not None else []
    labels = ["unverified"] if d.execution_policy == "turbo" else []
    if d.completeness != "complete":
        labels.append("partial")
    labels += _inherited(db, [model.id if model else None])
    return register_version(
        db,
        asset_type="assembly_drawing",
        row=d,
        project_id=asm.project_id,
        name=f"{asm.number} {TYPE_TITLE['assembly_drawing']}",
        created_by=d.created_by,
        labels=labels,
        node_run_id=d.node_run_id,
        depends_on=deps,
        parent_version_id=parent.id if parent else None,
        natural_key=f"assembly:{asm.id}:assembly_drawing",
    )


_REGISTER = {
    "assembly_drawing": register_assembly_drawing,
    "delivery_manifest": register_delivery_manifest,
    "assembly_model": register_assembly_revision,
    "part_model": register_revision,
    "drawing": register_drawing,
    "process_plan": register_plan,
    "quote": register_quote,
    "document": register_file_version,
    "intake": register_intake,
    "process_module": register_module,
    "drawing_document": register_drawing_document,
}


def register_row(db: Session, asset_type: str, row: Any) -> AssetVersion:
    """Register a specialised row with the labels/dependencies its type implies."""
    return _REGISTER[asset_type](db, row)


def ensure_version(db: Session, asset_type: str, row_id: uuid.UUID | str | None) -> AssetVersion | None:
    """The version of a specialised row, registering it (and its sources) if missing.
    None when the row does not exist."""
    if row_id is None:
        return None
    v = version_for(db, asset_type, row_id)
    if v is not None:
        return v
    row = db.get(KIND_MODEL[asset_type], uuid.UUID(str(row_id)))
    if row is None:
        return None
    return register_row(db, asset_type, row)


def record_output(db: Session, *, node_run: NodeRun, output_key: str, version: AssetVersion | None) -> None:
    """Link a node's produced asset version to the run (idempotent per node/key/version)."""
    if version is None:
        return
    db.execute(
        pg_insert(RunOutput)
        .values(
            id=uuid.uuid4(),
            run_id=node_run.run_id,
            node_run_id=node_run.id,
            attempt=node_run.attempt,
            output_key=output_key,
            asset_version_id=version.id,
        )
        .on_conflict_do_nothing()
    )


def register_part_history(db: Session, part_id: uuid.UUID) -> int:
    """Register every specialised row of one part in creation order (part copies, backfill)."""
    n = 0
    files = db.scalars(select(File.id).where(File.part_id == part_id)).all()
    if files:
        for fv in db.scalars(select(FileVersion).where(FileVersion.file_id.in_(files)).order_by(FileVersion.file_id, FileVersion.version)).all():
            n += _count_new(db, "document", fv)
    for rev in db.scalars(select(ModelRevision).where(ModelRevision.part_id == part_id).order_by(ModelRevision.rev_no)).all():
        n += _count_new(db, "part_model", rev)
    for model, order in ((Intake, Intake.created_at), (Drawing, Drawing.created_at), (ProcessPlan, ProcessPlan.created_at), (Quote, Quote.created_at)):
        asset_type = {Intake: "intake", Drawing: "drawing", ProcessPlan: "process_plan", Quote: "quote"}[model]
        for row in db.scalars(select(model).where(model.part_id == part_id).order_by(order, model.id)).all():
            n += _count_new(db, asset_type, row)
    return n


def _count_new(db: Session, asset_type: str, row: Any) -> int:
    if version_for(db, asset_type, row.id) is not None:
        return 0
    register_row(db, asset_type, row)
    return 1


# --- views ---------------------------------------------------------------------


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _ordinal(db: Session, model: type, row: Any) -> int:
    """1-based position of a drawing/plan/quote/intake among its part's rows of that kind."""
    earlier = db.scalar(
        select(func.count())
        .select_from(model)
        .where(
            model.part_id == row.part_id,
            (model.created_at < row.created_at) | ((model.created_at == row.created_at) & (model.id < row.id)),
        )
    )
    return int(earlier or 0) + 1


def human_label(db: Session, asset_type: str, row: Any, version: AssetVersion | None = None) -> str:
    if row is None:
        return "?"
    if asset_type == "process_module":
        v = version if version is not None else version_for(db, "process_module", row.id)
        return f"M{v.version_no}" if v is not None else "M?"
    if asset_type in ("part_model", "assembly_model"):
        return f"{LABEL_PREFIX[asset_type]}{row.rev_no}"
    if asset_type == "assembly_drawing":
        return f"{LABEL_PREFIX[asset_type]}{row.number}v{row.version}"
    if asset_type == "document":
        return f"v{row.version}"
    if asset_type == "delivery_manifest":
        return f"{LABEL_PREFIX[asset_type]}{row.number}"
    if asset_type == "drawing_document":
        return f"V{row.version_no}"
    return f"{LABEL_PREFIX[asset_type]}{_ordinal(db, KIND_MODEL[asset_type], row)}"


#: Run outcomes whose outputs are labelled ``from_failed_run`` (a ``partial`` run had a failed required branch: before
#: W0 it ended ``failed``, and templates with ``allow_failed_source=False`` must keep refusing its outputs).
FAILED_RUN_STATUSES = ("partial", "failed", "cancelled")


def labels_of(db: Session, version: AssetVersion) -> list[str]:
    """Stored labels plus read-time ones: `from_failed_run` when the producing node failed or its run did not succeed
    (failed, cancelled or partial); `unverified` is dropped once a Turbo model revision / drawing was verified later
    (every skipped check re-run and passed, every assumption confirmed by a person; W0 §2.2)."""
    labels = list(version.labels or [])
    if version.node_run_id and "from_failed_run" not in labels:
        nr = db.get(NodeRun, version.node_run_id)
        run = db.get(WorkflowRun, nr.run_id) if nr else None
        if (nr is not None and nr.status in ("failed", "cancelled")) or (run is not None and run.status in FAILED_RUN_STATUSES):
            labels.append("from_failed_run")
    if "unverified" in labels and verified_later(db, version):
        labels.remove("unverified")
    return labels


def verified_later(db: Session, version: AssetVersion) -> bool:
    """Whether a model revision / drawing version labelled ``unverified`` at creation is verified now (live
    verification: no pending check, no undecided assumption, no unverified upstream, all checks passed)."""
    from . import verification

    if version.model_revision_id is not None:
        rev = db.get(ModelRevision, version.model_revision_id)
        v = verification.revision_verification(rev, db) if rev is not None else None
    elif version.drawing_id is not None:
        d = db.get(Drawing, version.drawing_id)
        v = verification.drawing_verification(d, db) if d is not None else None
    else:
        return False
    return v is not None and not v["unverified"]


def _summary(db: Session, asset_type: str, row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if asset_type == "part_model":
        return {"rev_no": row.rev_no, "source": row.source, "status": row.status, "mesh_only": row.mesh_only,
                "note": row.note, "build_key": row.build_key, "parent_id": str(row.parent_id) if row.parent_id else None}
    if asset_type == "drawing":
        rev = db.get(ModelRevision, row.model_revision_id) if row.model_revision_id else None
        return {"kind": row.kind, "model_revision_id": str(row.model_revision_id) if row.model_revision_id else None,
                "rev_no": rev.rev_no if rev else None, "svg_sha": row.svg_sha, "dxf_sha": row.dxf_sha, "views": len(row.views or []),
                "pages": len(row.pages or []) or (1 if row.svg_sha or row.dxf_sha else 0), "manifest_sha256": row.manifest_sha256,
                "annotation_status": ((row.checks or {}).get("annotation") or {}).get("status"),
                "dwg_status": ((row.checks or {}).get("dwg") or {}).get("status")}
    if asset_type == "process_plan":
        plan = row.plan or {}
        return {"model_revision_id": str(row.model_revision_id) if row.model_revision_id else None,
                "intake_id": str(row.intake_id) if row.intake_id else None, "source": plan.get("source"),
                "estimated": bool(plan.get("estimated")), "priceable": bool(plan.get("priceable")),
                "total_cycle_min": plan.get("total_cycle_min"), "inputs_hash": row.inputs_hash}
    if asset_type == "quote":
        tiers = (row.result or {}).get("tiers") or [{}]
        return {"process_plan_id": str(row.process_plan_id), "currency": row.currency, "estimated": bool((row.result or {}).get("estimated")),
                "first_tier": {"qty": tiers[0].get("qty"), "unit_inc_tax_fen": tiers[0].get("unit_inc_tax_fen")}, "inputs_hash": row.inputs_hash}
    if asset_type == "document":
        f = db.get(File, row.file_id)
        return {"file_id": str(row.file_id), "version": row.version, "original_name": row.original_name,
                "kind": f.kind if f else None, "blob_sha256": row.blob_sha256, "note": row.note}
    if asset_type == "intake":
        s = row.summary or {}
        return {"method": row.method, "file_version_id": str(row.file_version_id) if row.file_version_id else None,
                "estimated": bool(s.get("estimated", True)), "material": s.get("material"), "bbox_mm": s.get("bbox_mm")}
    if asset_type == "assembly_model":
        return {"assembly_id": str(row.assembly_id), "rev_no": row.rev_no, "source": row.source, "status": row.status,
                "execution_policy": row.execution_policy, "check_status": row.check_status, "bom_sha256": row.bom_sha256,
                "stats": row.stats or {}, "parent_id": str(row.parent_id) if row.parent_id else None}
    if asset_type == "delivery_manifest":
        m = row.manifest or {}
        return {"number": row.number, "completeness": row.completeness, "execution_policy": row.execution_policy,
                "check_status": row.check_status, "assembly_revision_id": str(row.assembly_revision_id) if row.assembly_revision_id else None,
                "files": len(m.get("files") or []), "missing": len(m.get("missing") or []), "manifest_sha256": row.manifest_sha256}
    if asset_type == "drawing_document":
        return {"document_id": str(row.document_id), "version_no": row.version_no, "origin": row.origin,
                "execution_policy": row.execution_policy, "check_status": row.check_status, "output_status": row.output_status,
                "scale_status": row.scale_status, "entities": len(row.entities or []), "manifest_sha256": row.manifest_sha256,
                "dwg_status": (row.dwg or {}).get("status")}
    if asset_type == "assembly_drawing":
        return {"assembly_id": str(row.assembly_id), "assembly_revision_id": str(row.assembly_revision_id), "number": row.number,
                "version": row.version, "parent_id": str(row.parent_id) if row.parent_id else None, "execution_policy": row.execution_policy,
                "completeness": row.completeness, "check_status": row.check_status, "pages": len(row.pages or []),
                "manifest_sha256": row.manifest_sha256}
    if asset_type == "process_module":
        app = row.applicability or {}
        return {"name": row.name, "summary": row.summary, "materials": app.get("materials") or [], "equipment": app.get("equipment") or [],
                "size_range_mm": app.get("size_range_mm") or {}, "step_count": len(row.steps or []),
                "parameter_count": len(row.parameters or []), "validation_scope": (row.validation or {}).get("scope", ""),
                "source_plan_id": str(row.source_plan_id) if row.source_plan_id else None}
    return {}


def module_engineering(db: Session, version: AssetVersion) -> dict[str, Any]:
    """Process module review state: approved when the most recently closed
    engineering request on this version was approved for its current content."""
    from ..models import ApprovalDecision, ApprovalRequest

    base = (ApprovalRequest.asset_version_id == version.id, ApprovalRequest.kind == "engineering")
    last = db.scalar(select(ApprovalRequest).where(*base, ApprovalRequest.status.in_(("approved", "rejected")))
                     .order_by(ApprovalRequest.closed_at.desc().nulls_last(), ApprovalRequest.created_at.desc()).limit(1))
    pending = db.scalar(select(ApprovalRequest.id).where(*base, ApprovalRequest.status == "pending").limit(1))
    decision = None
    if last is not None:
        d = db.scalar(select(ApprovalDecision).where(ApprovalDecision.request_id == last.id).order_by(ApprovalDecision.created_at.desc()).limit(1))
        if d is not None:
            decision = {"decision": d.decision, "comment": d.comment, "role": d.role, "user_id": str(d.actor_id),
                        "subject_hash": d.target_hash, "at": _iso(d.created_at), "request_id": str(last.id)}
    return {
        "stage": MODULE_STAGE,
        "approved": last is not None and last.status == "approved" and last.target_hash == version.content_hash,
        "decision": decision,
        "pending_request_id": str(pending) if pending else None,
    }


def _engineering(db: Session, asset_type: str, row: Any) -> dict[str, Any] | None:
    if asset_type not in ASSET_SUBJECT or row is None:
        return None
    from . import approvals

    subject, stage = ASSET_SUBJECT[asset_type], ASSET_STAGE[asset_type]
    a = approvals.latest_decision(db, subject, row.id, stage)
    return {
        "stage": stage,
        "approved": approvals.is_approved(db, subject, row.id, stage),
        "decision": None if a is None else {
            "decision": a.decision, "comment": a.comment, "role": a.role, "user_id": str(a.user_id) if a.user_id else None, "actor_kind": a.actor_kind, "agent_id": str(a.agent_id) if a.agent_id else None,
            "subject_hash": a.subject_hash, "at": _iso(a.created_at),
        },
    }


def _edge_ref(db: Session, version_id: uuid.UUID, relation: str) -> dict[str, Any]:
    v = db.get(AssetVersion, version_id)
    if v is None:
        return {"version_id": str(version_id), "relation": relation}
    asset = db.get(Asset, v.asset_id)
    asset_type = asset_type_of(db, v)
    row = db.get(KIND_MODEL[asset_type], getattr(v, KIND_FIELD[asset_type]))
    return {
        "version_id": str(v.id),
        "relation": relation,
        "asset_id": str(v.asset_id),
        "asset_type": asset_type,
        "asset_name": asset.name if asset else None,
        "version_no": v.version_no,
        "label": human_label(db, asset_type, row, v),
        "content_hash": v.content_hash,
        "target_id": str(getattr(v, KIND_FIELD[asset_type])),
    }


def version_out(db: Session, version: AssetVersion, *, edges: bool = True) -> dict[str, Any]:
    asset = db.get(Asset, version.asset_id)
    assert asset is not None
    asset_type = asset.type
    row = specialised(db, version)
    nr = db.get(NodeRun, version.node_run_id) if version.node_run_id else None
    out: dict[str, Any] = {
        "id": str(version.id),
        "asset_id": str(asset.id),
        "asset_type": asset_type,
        "asset_name": asset.name,
        "project_id": str(asset.project_id),
        "version_no": version.version_no,
        "label": human_label(db, asset_type, row, version),
        "content_hash": version.content_hash,
        "availability": version.availability,
        "publish_state": version.publish_state,
        "labels": labels_of(db, version),
        "parent_version_id": str(version.parent_version_id) if version.parent_version_id else None,
        "created_at": _iso(version.created_at),
        "created_by": str(version.created_by) if version.created_by else None,
        "node_run_id": str(version.node_run_id) if version.node_run_id else None,
        "run_id": str(nr.run_id) if nr else None,
        "target": {
            "kind": TARGET_KIND[asset_type],
            "id": str(getattr(version, KIND_FIELD[asset_type])),
            "part_id": str(asset.part_id) if asset.part_id else (str(row.part_id) if getattr(row, "part_id", None) else None),
            "summary": _summary(db, asset_type, row),
        },
        "engineering": module_engineering(db, version) if asset_type == "process_module" else _engineering(db, asset_type, row),
    }
    if edges:
        out["dependencies"] = [_edge_ref(db, e.to_version_id, e.relation) for e in dependencies(db, version.id)]
        out["dependents"] = [_edge_ref(db, e.from_version_id, e.relation) for e in dependents(db, version.id)]
    return out


def latest_version(db: Session, asset_id: uuid.UUID) -> AssetVersion | None:
    return db.scalar(select(AssetVersion).where(AssetVersion.asset_id == asset_id).order_by(AssetVersion.version_no.desc()).limit(1))


@dataclasses.dataclass
class ListingPrefetch:
    """Rows batch-loaded for a list of ``asset_out`` calls. ``loaded`` keeps them strongly referenced: the session's
    identity map is weak, so rows nobody holds would be dropped and fetched again one by one."""

    latest: dict[uuid.UUID, AssetVersion] = dataclasses.field(default_factory=dict)
    counts: dict[uuid.UUID, int] = dataclasses.field(default_factory=dict)
    loaded: list[Any] = dataclasses.field(default_factory=list)


def prefetch_listing(db: Session, assets: Sequence[Asset]) -> ListingPrefetch:
    """Batch-load what ``asset_out`` reads for a list of assets — latest versions, version counts, parts, producing
    node runs, specialised rows, and the versions / assets / rows their dependency edges point at — so the per-asset
    ``db.get`` calls hit the session's identity map instead of issuing one query each. Read-only; the output of
    ``asset_out`` is unchanged. Keep the returned object alive while calling ``asset_out``."""
    out = ListingPrefetch()
    ids = [a.id for a in assets]
    if not ids:
        return out
    newest = (
        select(AssetVersion.asset_id, func.max(AssetVersion.version_no).label("n"), func.count().label("c"))
        .where(AssetVersion.asset_id.in_(ids))
        .group_by(AssetVersion.asset_id)
        .subquery()
    )
    out.counts = {aid: int(c) for aid, _n, c in db.execute(select(newest.c.asset_id, newest.c.n, newest.c.c)).all()}
    out.latest = {
        v.asset_id: v
        for v in db.scalars(
            select(AssetVersion).join(newest, (AssetVersion.asset_id == newest.c.asset_id) & (AssetVersion.version_no == newest.c.n))
        ).all()
    }

    def load(model: Any, keys: Iterable[Any]) -> list[Any]:
        keys = set(keys)
        rows = list(db.scalars(select(model).where(model.id.in_(keys))).all()) if keys else []
        out.loaded.extend(rows)
        return rows

    load(Part, (a.part_id for a in assets if a.part_id))
    node_runs = load(NodeRun, (v.node_run_id for v in out.latest.values() if v.node_run_id))
    load(WorkflowRun, (nr.run_id for nr in node_runs))
    version_ids = [v.id for v in out.latest.values()]
    edges = db.scalars(
        select(AssetDependency).where(AssetDependency.from_version_id.in_(version_ids) | AssetDependency.to_version_id.in_(version_ids))
    ).all()
    other = load(AssetVersion, {e.to_version_id for e in edges} | {e.from_version_id for e in edges})
    load(Asset, {v.asset_id for v in other} - set(ids))
    by_type: dict[str, set[Any]] = {}
    for v in [*out.latest.values(), *other]:
        for asset_type, column in KIND_FIELD.items():
            target = getattr(v, column)
            if target is not None:
                by_type.setdefault(asset_type, set()).add(target)
                break
    for asset_type, target_ids in by_type.items():
        load(KIND_MODEL[asset_type], target_ids)
    return out


def asset_out(db: Session, asset: Asset, *, latest: AssetVersion | None = None, count: int | None = None) -> dict[str, Any]:
    latest = latest or latest_version(db, asset.id)
    if count is None:
        count = db.scalar(select(func.count()).select_from(AssetVersion).where(AssetVersion.asset_id == asset.id)) or 0
    part = db.get(Part, asset.part_id) if asset.part_id else None
    return {
        "id": str(asset.id),
        "project_id": str(asset.project_id),
        "type": asset.type,
        "name": asset.name,
        "part_id": str(asset.part_id) if asset.part_id else None,
        "part": {"id": str(part.id), "number": part.number, "name": part.name} if part else None,
        "file_id": str(asset.file_id) if asset.file_id else None,
        "natural_key": asset.natural_key,
        "source_asset_id": str(asset.source_asset_id) if asset.source_asset_id else None,
        "owner_id": str(asset.owner_id) if asset.owner_id else None,
        "latest_version": version_out(db, latest) if latest else None,
        "version_count": int(count),
        "created_at": _iso(asset.created_at),
        "archived_at": _iso(asset.archived_at),
    }


def publish_version(db: Session, user: User, version_id: uuid.UUID, *, state: str) -> AssetVersion:
    """Publish/deprecate an exact asset version with its domain and role guards.

    The shared catalogue must not provide a write path around a frozen module.
    Caller commits; engineering approval semantics match the asset's type.
    """
    from ..security import project_access
    from . import approvals, feature_flags as features
    from .audit import audit

    pre = get_version(db, version_id)
    asset = db.get(Asset, pre.asset_id)
    assert asset is not None
    action = {"process_plan": "process", "process_module": "process", "quote": "quote"}.get(asset.type, "design")
    project_access(db, user, asset.project_id, action)
    if asset.type == "process_module":
        features.require_enabled("process_modules")
    if state not in ("published", "deprecated"):
        raise bad_request("invalid_publish_state", "publish state must be published or deprecated")
    v = db.scalar(select(AssetVersion).where(AssetVersion.id == version_id).with_for_update())
    assert v is not None
    if v.publish_state == state:
        return v
    if state == "published":
        if v.availability != "ready":
            raise conflict("asset_not_ready", "only a ready version can be published", availability=v.availability)
        if asset.type == "process_module":
            if not module_engineering(db, v)["approved"]:
                raise conflict("approval_required", "publishing needs an approved process review of this exact module version",
                               stage=MODULE_STAGE, subject_type="process_module")
        elif asset.type in ASSET_SUBJECT:
            subject, stage = ASSET_SUBJECT[asset.type], ASSET_STAGE[asset.type]
            target_id = getattr(v, KIND_FIELD[asset.type])
            if not approvals.is_approved(db, subject, target_id, stage):
                raise conflict("approval_required", f"publishing needs a valid {stage} approval of this exact version", stage=stage, subject_type=subject)
    previous = v.publish_state
    v.publish_state = state
    audit(db, action=f"asset.{state}", subject_type="asset_version", subject_id=v.id, user_id=user.id, project_id=asset.project_id,
          data={"from": previous, "asset_id": str(asset.id), "version_no": v.version_no})
    db.flush()
    return v
