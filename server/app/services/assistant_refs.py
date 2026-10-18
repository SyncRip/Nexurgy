"""References of the unified AI workspace (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §3).

A page sends an `AiRefInput` (kind + object id + optional selection / field /
excerpt / output pointer). The server validates it against the database and
returns a normalised, version-pinned `AiRef`: every label, title and value is
read here, never taken from the client. Reference status (`ok` / `stale` /
`unavailable`) is computed on read in batches and is never stored in a snapshot.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..cad.drafting import MAX_ENTITIES
from ..errors import AppError, not_found
from ..models import (
    AcceptanceCriterion,
    Asset,
    AssetVersion,
    File,
    FileVersion,
    ModelRevision,
    NodeRun,
    Part,
    ProjectMember,
    Quote,
    RunOutput,
    Task,
    User,
    Workflow,
    WorkflowRun,
)

REF_KINDS = ("part", "part_revision", "cad_selection", "quote", "quote_field", "asset_version", "run_output", "run", "task",
             "criterion", "file_version", "text_excerpt", "workflow", "engineering_spec_version", "assembly_revision", "drawing_document_version")
#: Kinds an operation can write from (the first one added becomes the preselected target). W8: assembly_revision.
OPERABLE_KINDS = ("part", "part_revision", "cad_selection", "file_version", "workflow", "assembly_revision", "drawing_document_version")
DRAWING_FILE_KINDS = ("drawing_pdf", "drawing_image")
#: Native CAD drawings an operation can reconstruct (DWG uploads keep kind "other": the name / bytes decide).
CAD_DRAWING_EXTS = (".dxf", ".dwg")
EXCERPT_SOURCES = ("quote", "task", "asset_version", "part_revision", "criterion")
QUOTE_TOTAL_KEYS = ("unit_cost_fen", "unit_inc_tax_fen", "order_inc_tax_fen", "lead_days")
_TOTAL_LABELS = {
    "unit_cost_fen": ("单件成本", "Unit cost"),
    "unit_inc_tax_fen": ("含税单价", "Unit price incl. tax"),
    "order_inc_tax_fen": ("订单含税总价", "Order total incl. tax"),
    "lead_days": ("交期（天）", "Lead time (days)"),
}

Vec3 = tuple[float, float, float]


# --- client input ------------------------------------------------------------------


class _In(BaseModel):
    # Pages may echo extra display fields (e.g. project_id, label); they are ignored, never trusted.
    model_config = ConfigDict(extra="ignore")


class OpCandidateIn(_In):
    op_id: str = Field(min_length=1, max_length=40)
    kind: str = Field(default="", max_length=40)
    label: str = Field(default="", max_length=200)
    diameter: float | None = None
    reason: str = Field(default="", max_length=300)


class CadSelectionIn(_In):
    entity: Literal["face", "edge", "op"]
    id: str | int
    build_key: str | None = Field(default=None, max_length=64)
    mesh_sha: str | None = Field(default=None, max_length=64)
    # Fixed by the server; accepted so a page can echo what it received.
    units: Literal["mm"] | None = None
    frame: Literal["world_mm_z_up"] | None = None
    point: Vec3 | None = None
    normal: Vec3 | None = None
    center: Vec3 | None = None
    area: float | None = None
    radius: float | None = None
    length: float | None = None
    surface: str | None = Field(default=None, max_length=40)
    edge_kind: str | None = Field(default=None, max_length=40)
    candidates: list[OpCandidateIn] = Field(default_factory=list, max_length=20)
    resolved_op_id: str | None = Field(default=None, max_length=40)


class QuoteFieldIn(_In):
    tier_qty: int = Field(ge=1, le=1_000_000)
    key: str = Field(min_length=1, max_length=40)


class ExcerptIn(_In):
    source_kind: Literal["quote", "task", "asset_version", "part_revision", "criterion"]
    text: str = Field(min_length=1, max_length=2000)


class RunOutputIn(_In):
    run_id: uuid.UUID
    node_id: str = Field(min_length=1, max_length=60)
    output_key: str = Field(min_length=1, max_length=60)


class RefNodeIn(_In):
    node_id: str = Field(min_length=1, max_length=60)


class AssemblyInstanceIn(_In):
    path: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(min_length=1, max_length=10)


class DrawingEntitiesIn(_In):
    ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=MAX_ENTITIES)
    sheet_id: str | None = Field(default=None, min_length=1, max_length=40)


class AiRefInput(_In):
    kind: Literal["part", "part_revision", "cad_selection", "quote", "quote_field", "asset_version", "run_output", "run", "task",
                  "criterion", "file_version", "text_excerpt", "workflow", "engineering_spec_version", "assembly_revision", "drawing_document_version"]
    object_id: uuid.UUID
    version: int | None = Field(default=None, ge=1)
    instance: AssemblyInstanceIn | None = None
    entities: DrawingEntitiesIn | None = None
    selection: CadSelectionIn | None = None
    field: QuoteFieldIn | None = None
    excerpt: ExcerptIn | None = None
    output: RunOutputIn | None = None
    # AI work panel: one node of a workflow (its current graph) or of a run (its graph snapshot); workflow / run only.
    node: RefNodeIn | None = None


# --- keys --------------------------------------------------------------------------


def _fmt_point(point: Any) -> str:
    if not point:
        return "-"
    return "_".join(f"{float(v):.1f}" for v in point)


def ref_key(kind: str, object_id: str, *, selection: dict | None = None, field: dict | None = None, output: dict | None = None,
            excerpt: dict | None = None, version: int | None = None, node: dict | None = None,
            instance: dict | None = None, entities: dict | None = None) -> str:
    """Server-computed key (design §3.2); clients only use the returned value. A workflow reference pins its
    version (the workflow row is edited in place), so each version is a reference of its own. A node of a workflow /
    run is a reference of its own too (``workflow:<id>:v<n>:node:<node>``, ``run:<id>:node:<node>``)."""
    key = f"{kind}:{object_id}"
    if kind == "workflow" and version is not None:
        key += f":v{version}"
    if kind in ("workflow", "run") and node and node.get("node_id"):
        key += f":node:{node['node_id']}"
    if kind == "cad_selection" and selection:
        key += f":{selection.get('entity')}:{selection.get('id')}:{_fmt_point(selection.get('point'))}"
    elif kind == "quote_field" and field:
        key += f":{field.get('tier_qty')}:{field.get('key')}"
    elif kind == "run_output" and output:
        key += f":{output.get('node_id')}:{output.get('output_key')}"
    elif kind == "text_excerpt" and excerpt:
        key += ":" + hashlib.sha1((excerpt.get("text") or "").encode()).hexdigest()[:10]
    if kind == "assembly_revision" and instance:
        key += ":instance:" + hashlib.sha256(json.dumps(instance["path"], separators=(",", ":")).encode()).hexdigest()
    if kind == "drawing_document_version" and entities:
        key += ":entities:" + hashlib.sha256(json.dumps(entities, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return key


# --- normalisation -------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _wrong_project(db: Session, user: User, owner_project_id: uuid.UUID, what: str) -> AppError:
    """422 ref_wrong_project when the user can see the other project; otherwise 404 (no existence leak)."""
    if db.get(ProjectMember, {"project_id": owner_project_id, "user_id": user.id}) is not None:
        return AppError(422, "ref_wrong_project", f"this {what} belongs to another project; start a conversation in that project",
                        {"kind": what, "project_id": str(owner_project_id)})
    return not_found(what)


def _check_project(db: Session, user: User, owner_project_id: uuid.UUID, project_id: uuid.UUID, what: str) -> None:
    if owner_project_id != project_id:
        raise _wrong_project(db, user, owner_project_id, what)


def _archived(what: str) -> AppError:
    return AppError(409, "ref_archived", f"the {what} is archived and cannot be referenced", {"kind": what})


def _base(kind: str, project_id: uuid.UUID, object_id: uuid.UUID, **kw: Any) -> dict:
    ref = {
        "key": "",
        "kind": kind,
        "project_id": str(project_id),
        "object_id": str(object_id),
        "part_id": None,
        "task_id": None,
        "version_label": None,
        "title": "",
        "summary": "",
        "selection": None,
        "field": None,
        "excerpt": None,
        "output": None,
        "source_path": "",
        "snapshot_at": _now(),
    }
    ref.update(kw)
    ref["key"] = ref_key(kind, ref["object_id"], selection=ref["selection"], field=ref["field"], output=ref["output"], excerpt=ref["excerpt"],
                         version=ref.get("version"), node=ref.get("node"), instance=ref.get("instance"), entities=ref.get("entities"))
    return ref


def _load_part_of(db: Session, user: User, part_id: uuid.UUID, project_id: uuid.UUID, what: str) -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise not_found(what)
    _check_project(db, user, part.project_id, project_id, what)
    return part


def _revision_summary(rev: ModelRevision) -> str:
    bits = [f"R{rev.rev_no}", f"{len(rev.ops or [])} ops", rev.source]
    size = ((rev.metrics or {}).get("bbox") or {}).get("size")
    if isinstance(size, list) and len(size) == 3:
        bits.append("×".join(f"{float(v):g}" for v in size) + " mm")
    if rev.mesh_only:
        bits.append("mesh reference")
    if rev.status != "ready":
        bits.append(rev.status)
    return " · ".join(bits)


def revision_ref(db: Session, rev: ModelRevision, part: Part | None = None) -> dict:
    """Normalised part_revision reference built from the database (also used for results)."""
    part = part or db.get(Part, rev.part_id)
    assert part is not None
    return _base("part_revision", part.project_id, rev.id, part_id=str(part.id), version_label=f"R{rev.rev_no}", title=part.name,
                 summary=_revision_summary(rev), source_path=f"/parts/{part.id}/model?rev={rev.id}", mesh_only=bool(rev.mesh_only))


def _selection_summary(sel: dict) -> str:
    entity = sel["entity"]
    parts = [f"{entity} {sel['id']}"]
    if sel.get("surface"):
        parts.append(str(sel["surface"]))
    if sel.get("edge_kind"):
        parts.append(str(sel["edge_kind"]))
    if sel.get("radius") is not None:
        parts.append(f"r={float(sel['radius']):g} mm")
    if sel.get("length") is not None:
        parts.append(f"L={float(sel['length']):g} mm")
    if sel.get("area") is not None:
        parts.append(f"A={float(sel['area']):g} mm²")
    if sel.get("resolved_op_id"):
        parts.append(f"op {sel['resolved_op_id']}")
    elif sel.get("candidates"):
        parts.append(f"{len(sel['candidates'])} candidate ops")
    return " · ".join(parts)


def _cad_selection(db: Session, user: User, project_id: uuid.UUID, rev: ModelRevision, part: Part, sel_in: CadSelectionIn | None) -> dict:
    if sel_in is None:
        raise AppError(422, "selection_required", "a CAD selection reference needs the picked face, edge or op")
    sel = sel_in.model_dump(mode="json")
    op_ids = {str(o.get("id")) for o in (rev.ops or []) if isinstance(o, dict)}
    # Face / edge ids belong to one build; an op pick may come from the op list (build ids then optional but checked if given).
    strict = sel["entity"] in ("face", "edge")
    if strict or sel.get("build_key") is not None or sel.get("mesh_sha") is not None:
        if sel.get("build_key") != rev.build_key or sel.get("mesh_sha") != rev.mesh_sha:
            raise AppError(409, "ref_build_mismatch", "the selection was made on a different build of this revision; select it again",
                           {"revision_id": str(rev.id), "build_key": rev.build_key, "mesh_sha": rev.mesh_sha})
    if sel["entity"] == "op" and str(sel["id"]) not in op_ids:
        raise AppError(422, "ref_op_not_found", f"op {sel['id']} is not in R{rev.rev_no}", {"op_id": str(sel["id"])})
    if sel.get("resolved_op_id") and sel["resolved_op_id"] not in op_ids:
        raise AppError(422, "ref_op_not_found", f"op {sel['resolved_op_id']} is not in R{rev.rev_no}", {"op_id": sel["resolved_op_id"]})
    for c in sel.get("candidates") or []:
        if c["op_id"] not in op_ids:
            raise AppError(422, "ref_op_not_found", f"candidate op {c['op_id']} is not in R{rev.rev_no}", {"op_id": c["op_id"]})
    sel["units"] = "mm"
    sel["frame"] = "world_mm_z_up"
    sel["build_key"] = rev.build_key
    sel["mesh_sha"] = rev.mesh_sha
    return _base("cad_selection", project_id, rev.id, part_id=str(part.id), version_label=f"R{rev.rev_no}", title=part.name,
                 summary=_selection_summary(sel), selection=sel, source_path=f"/parts/{part.id}/model?rev={rev.id}", mesh_only=bool(rev.mesh_only))


def graph_node(graph: dict | None, node_id: str) -> dict | None:
    return next((n for n in (graph or {}).get("nodes") or [] if isinstance(n, dict) and n.get("id") == node_id), None)


def _node_missing(node_id: str, where: str, **details: Any) -> AppError:
    return AppError(422, "ref_node_not_found", f"node {node_id!r} is not in {where}", {"node_id": node_id, **details})


def _clip300(text: str) -> str:
    return text if len(text) <= 300 else text[:299] + "…"


def workflow_ref(db: Session, w: Workflow, node_id: str | None = None) -> dict:
    """Normalised workflow reference pinned to the workflow's current version. With ``node_id``: that node of the
    current graph (422 ref_node_not_found otherwise); the title stays the workflow's name."""
    from ..workflow.edit import public_params

    graph = w.graph or {}
    scope = "part" if w.part_id else ("collaboration" if w.kind == "collaboration" else "template")
    part = db.get(Part, w.part_id) if w.part_id else None
    owner = f"part workflow of {part.number}" if part else {"part": "part workflow", "template": "project template",
                                                             "collaboration": "collaboration draft"}[scope]
    summary = f"{owner} · {len(graph.get('nodes') or [])} nodes · {len(graph.get('edges') or [])} edges"
    extra: dict[str, Any] = {}
    if node_id is not None:
        node = graph_node(graph, node_id)
        if node is None:
            raise _node_missing(node_id, f"the current version v{w.version} of this workflow", version=w.version)
        params = public_params(str(node.get("type")), node.get("params"))
        summary = f"node {node.get('label') or node_id} ({node.get('type')})"
        if params:
            summary += " · " + json.dumps(params, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
        extra["node"] = {"node_id": node_id, "type": node.get("type"), "label": node.get("label") or None}
    return _base("workflow", w.project_id, w.id, part_id=str(w.part_id) if w.part_id else None, version_label=f"v{w.version}",
                 title=w.name, summary=_clip300(summary), source_path=f"/workflows/{w.id}", workflow_id=str(w.id), version=w.version,
                 workflow_scope=scope, **extra)


def _run_node(db: Session, run: WorkflowRun, node_id: str) -> tuple[dict, str]:
    """(node of the run's graph snapshot, summary): status, attempt and error code of its node run."""
    node = graph_node(run.graph_snapshot, node_id)
    if node is None:
        raise _node_missing(node_id, "this run's graph", run_id=str(run.id))
    nr = db.scalar(select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == node_id))
    status = nr.status if nr is not None else None
    summary = f"node {node.get('label') or node_id} ({node.get('type')}) · {status or 'pending'}"
    if nr is not None:
        summary += f" · attempt {nr.attempt}/{nr.max_attempts}"
        code = nr.error.get("code") if isinstance(nr.error, dict) else None
        if code:
            summary += f" · error {code}"
    return {"node_id": node_id, "type": node.get("type"), "label": node.get("label") or None, "status": status}, _clip300(summary)


def _quote_label(db: Session, q: Quote) -> str:
    from . import assets

    return f"{assets.human_label(db, 'quote', q)} · {q.created_at.date().isoformat()}"


def _money(fen: Any, currency: str) -> str:
    try:
        return f"{int(fen) / 100:.2f} {currency}"
    except (TypeError, ValueError):
        return "?"


def _quote_summary(q: Quote) -> str:
    tiers = (q.result or {}).get("tiers") or []
    if not (q.result or {}).get("priceable", True) or not tiers:
        return "not priceable"
    return "; ".join(f"qty {t.get('qty')}: {_money(t.get('unit_inc_tax_fen'), q.currency)}/unit" for t in tiers[:3])


def _quote_field(q: Quote, f: QuoteFieldIn) -> dict:
    tier = next((t for t in (q.result or {}).get("tiers") or [] if t.get("qty") == f.tier_qty), None)
    if tier is None:
        raise AppError(422, "ref_field_not_found", f"the quote has no quantity tier {f.tier_qty}", {"tier_qty": f.tier_qty, "key": f.key})
    if f.key in QUOTE_TOTAL_KEYS:
        value = tier.get(f.key)
        zh, en = _TOTAL_LABELS[f.key]
        if f.key == "lead_days":
            formula, unit = str(tier.get("lead_formula") or ""), "days"
        elif f.key == "unit_cost_fen":
            formula = " + ".join(f"{ln.get('key')} {ln.get('fen')}" for ln in tier.get("lines") or []) + f" = {value}"
            unit = "fen"
        elif f.key == "unit_inc_tax_fen":
            formula, unit = f"unit_ex_tax {tier.get('unit_ex_tax_fen')} + tax {tier.get('tax_fen')} = {value}", "fen"
        else:
            formula, unit = f"order_ex_tax {tier.get('order_ex_tax_fen')} + order_tax {tier.get('order_tax_fen')} = {value}", "fen"
        estimated = bool((q.result or {}).get("estimated"))
    else:
        line = next((ln for ln in tier.get("lines") or [] if ln.get("key") == f.key), None)
        if line is None:
            raise AppError(422, "ref_field_not_found", f"the quote has no field {f.key!r} for quantity {f.tier_qty}",
                           {"tier_qty": f.tier_qty, "key": f.key})
        value, zh, en, formula, unit = line.get("fen"), line.get("label_zh") or f.key, line.get("label_en") or f.key, line.get("formula") or "", "fen"
        estimated = bool(line.get("estimated"))
    if value is None:
        raise AppError(422, "ref_field_not_found", f"the quote has no value for {f.key!r}", {"tier_qty": f.tier_qty, "key": f.key})
    return {"tier_qty": f.tier_qty, "key": f.key, "value": value, "unit": unit, "currency": q.currency, "label": en, "label_en": en,
            "label_zh": zh, "formula": formula, "estimated": estimated}


def _version_ref(db: Session, kind: str, project_id: uuid.UUID, v: AssetVersion, asset: Asset, **kw: Any) -> dict:
    from . import assets

    asset_type = asset.type
    row = assets.specialised(db, v)
    label = assets.human_label(db, asset_type, row, v)
    title_type = assets.TYPE_TITLE.get(asset_type, asset_type)
    defaults = {
        "part_id": str(asset.part_id) if asset.part_id else (str(row.part_id) if getattr(row, "part_id", None) else None),
        "version_label": label,
        "title": asset.name,
        "summary": f"{title_type} {label} · {v.availability} · {v.publish_state}",
        "source_path": f"/assets/{asset.id}?version={v.id}",
        "asset_type": asset_type,
        "availability": v.availability,
        "publish_state": v.publish_state,
    }
    defaults.update(kw)
    return _base(kind, project_id, v.id, **defaults)


def asset_version_ref(db: Session, v: AssetVersion) -> dict:
    asset = db.get(Asset, v.asset_id)
    assert asset is not None
    return _version_ref(db, "asset_version", asset.project_id, v, asset)


def _excerpt_source(db: Session, user: User, project_id: uuid.UUID, kind: str, object_id: uuid.UUID) -> dict:
    """Title / label / path of the object an excerpt was selected from (must exist in this project)."""
    if kind == "quote":
        q = db.get(Quote, object_id)
        if q is None:
            raise not_found("quote")
        part = _load_part_of(db, user, q.part_id, project_id, "quote")
        return {"part_id": str(part.id), "title": f"{part.name} quote", "version_label": _quote_label(db, q),
                "source_path": f"/parts/{part.id}/quote?quote={q.id}"}
    if kind == "task":
        t = db.get(Task, object_id)
        if t is None:
            raise not_found("task")
        _check_project(db, user, t.project_id, project_id, "task")
        return {"task_id": str(t.id), "part_id": str(t.part_id) if t.part_id else None, "title": t.title, "version_label": f"T-{t.number}",
                "source_path": f"/tasks/{t.id}"}
    if kind == "criterion":
        c = db.get(AcceptanceCriterion, object_id)
        t = db.get(Task, c.task_id) if c else None
        if c is None or t is None:
            raise not_found("criterion")
        _check_project(db, user, t.project_id, project_id, "criterion")
        return {"task_id": str(t.id), "title": f"T-{t.number} #{c.seq}", "version_label": f"T-{t.number} #{c.seq}",
                "source_path": f"/tasks/{t.id}"}
    if kind == "part_revision":
        rev = db.get(ModelRevision, object_id)
        if rev is None:
            raise not_found("revision")
        part = _load_part_of(db, user, rev.part_id, project_id, "revision")
        return {"part_id": str(part.id), "title": part.name, "version_label": f"R{rev.rev_no}",
                "source_path": f"/parts/{part.id}/model?rev={rev.id}"}
    v = db.get(AssetVersion, object_id)
    asset = db.get(Asset, v.asset_id) if v else None
    if v is None or asset is None:
        raise not_found("asset version")
    _check_project(db, user, asset.project_id, project_id, "asset version")
    from . import assets

    return {"part_id": str(asset.part_id) if asset.part_id else None, "title": asset.name,
            "version_label": assets.human_label(db, asset.type, assets.specialised(db, v), v),
            "source_path": f"/assets/{asset.id}?version={v.id}"}


def normalize_ref(db: Session, user: User, project_id: uuid.UUID, inp: AiRefInput) -> dict:
    """Validate a client reference against the database and return the normalised AiRef (design §3.1–3.2)."""
    kind, oid = inp.kind, inp.object_id
    if kind == "part":
        part = _load_part_of(db, user, oid, project_id, "part")
        if part.archived_at is not None:
            raise _archived("part")
        return _base("part", project_id, part.id, part_id=str(part.id), title=part.name,
                     summary=f"{part.number} · {part.name} · {part.material}", source_path=f"/parts/{part.id}/model")
    if kind in ("part_revision", "cad_selection"):
        rev = db.get(ModelRevision, oid)
        if rev is None:
            raise not_found("revision")
        part = _load_part_of(db, user, rev.part_id, project_id, "revision")
        if part.archived_at is not None:
            raise _archived("part")
        if kind == "part_revision":
            return revision_ref(db, rev, part)
        return _cad_selection(db, user, project_id, rev, part, inp.selection)
    if kind in ("quote", "quote_field"):
        q = db.get(Quote, oid)
        if q is None:
            raise not_found("quote")
        part = _load_part_of(db, user, q.part_id, project_id, "quote")
        base = {"part_id": str(part.id), "version_label": _quote_label(db, q), "source_path": f"/parts/{part.id}/quote?quote={q.id}"}
        if kind == "quote":
            return _base("quote", project_id, q.id, title=f"{part.name} quote", summary=_quote_summary(q), **base)
        if inp.field is None:
            raise AppError(422, "field_required", "a quote field reference needs the tier quantity and the field key")
        fld = _quote_field(q, inp.field)
        summary = f"{fld['label_en']} (qty {fld['tier_qty']}): " + (f"{fld['value']} days" if fld["unit"] == "days" else _money(fld["value"], fld["currency"]))
        return _base("quote_field", project_id, q.id, title=f"{part.name} · {fld['label_en']}", summary=summary, field=fld, **base)
    if kind in ("asset_version", "run_output"):
        v = db.get(AssetVersion, oid)
        asset = db.get(Asset, v.asset_id) if v else None
        if v is None or asset is None:
            raise not_found("asset version")
        _check_project(db, user, asset.project_id, project_id, "asset version")
        if kind == "asset_version":
            return _version_ref(db, "asset_version", project_id, v, asset)
        if inp.output is None:
            raise AppError(422, "output_required", "a run output reference needs the run, node and output key")
        run = db.get(WorkflowRun, inp.output.run_id)
        if run is None:
            raise not_found("run")
        _check_project(db, user, run.project_id, project_id, "run")
        row = db.scalar(
            select(RunOutput.id).join(NodeRun, NodeRun.id == RunOutput.node_run_id)
            .where(RunOutput.run_id == run.id, NodeRun.node_id == inp.output.node_id, RunOutput.output_key == inp.output.output_key,
                   RunOutput.asset_version_id == v.id).limit(1)
        )
        if row is None:
            raise AppError(422, "ref_output_not_found", "this run did not produce that asset version on that output",
                           {"run_id": str(run.id), "node_id": inp.output.node_id, "output_key": inp.output.output_key})
        out = inp.output.model_dump(mode="json")
        return _version_ref(db, "run_output", project_id, v, asset, output=out, task_id=str(run.task_id) if run.task_id else None,
                            summary=f"{run.title or 'run'} · {out['node_id']}.{out['output_key']} · {v.availability}",
                            source_path=f"/runs/{run.id}")
    if kind == "run":
        run = db.get(WorkflowRun, oid)
        if run is None:
            raise not_found("run")
        _check_project(db, user, run.project_id, project_id, "run")
        extra: dict[str, Any] = {"summary": f"{run.status} · {run.origin} · {run.created_at.date().isoformat()}"}
        # AI work panel: whether a change asked on this run can be planned on its part workflow (classify uses it).
        wf = db.get(Workflow, run.workflow_id) if run.workflow_id else None
        extra["workflow_id"] = str(wf.id) if wf is not None else None
        extra["part_workflow"] = wf is not None and wf.part_id is not None and wf.kind == "tool"
        if inp.node is not None:
            extra["node"], extra["summary"] = _run_node(db, run, inp.node.node_id)
        return _base("run", project_id, run.id, part_id=str(run.part_id) if run.part_id else None,
                     task_id=str(run.task_id) if run.task_id else None, title=run.title or "Run", source_path=f"/runs/{run.id}",
                     **extra)
    if kind == "task":
        t = db.get(Task, oid)
        if t is None:
            raise not_found("task")
        _check_project(db, user, t.project_id, project_id, "task")
        return _base("task", project_id, t.id, task_id=str(t.id), part_id=str(t.part_id) if t.part_id else None, version_label=f"T-{t.number}",
                     title=t.title, summary=f"{t.status} · {(t.objective or '').strip()[:200]}", source_path=f"/tasks/{t.id}")
    if kind == "criterion":
        c = db.get(AcceptanceCriterion, oid)
        t = db.get(Task, c.task_id) if c else None
        if c is None or t is None:
            raise not_found("criterion")
        _check_project(db, user, t.project_id, project_id, "criterion")
        return _base("criterion", project_id, c.id, task_id=str(t.id), part_id=str(t.part_id) if t.part_id else None,
                     version_label=f"T-{t.number} #{c.seq}", title=c.description.strip()[:120],
                     summary=("required · " if c.required else "optional · ") + c.description.strip()[:300], source_path=f"/tasks/{t.id}")
    if kind == "workflow":
        w = db.get(Workflow, oid)
        if w is None:
            raise not_found("workflow")
        _check_project(db, user, w.project_id, project_id, "workflow")
        if w.archived_at is not None:
            raise _archived("workflow")
        part = db.get(Part, w.part_id) if w.part_id else None
        if part is not None and part.archived_at is not None:
            raise _archived("part")
        if inp.version is not None and inp.version != w.version:
            raise AppError(409, "ref_version_changed", "the workflow changed since it was selected", {"current_version": w.version})
        return workflow_ref(db, w, inp.node.node_id if inp.node is not None else None)
    if kind == "file_version":
        fv = db.get(FileVersion, oid)
        f = db.get(File, fv.file_id) if fv else None
        if fv is None or f is None:
            raise not_found("file version")
        _check_project(db, user, f.project_id, project_id, "file version")
        if f.archived_at is not None:
            raise _archived("file")
        path = f"/parts/{f.part_id}/files" if f.part_id else f"/projects/{f.project_id}/files"
        return _base("file_version", project_id, fv.id, part_id=str(f.part_id) if f.part_id else None, version_label=f"v{fv.version}",
                     title=f.name, summary=f"{f.kind} · {fv.original_name}", source_path=path, file_kind=f.kind,
                     file_name=fv.original_name)
    if kind == "engineering_spec_version":
        return _spec_version_ref(db, user, project_id, oid)
    if kind == "assembly_revision":
        from ..models import Assembly, AssemblyRevision

        rev = db.get(AssemblyRevision, oid)
        asm = db.get(Assembly, rev.assembly_id) if rev else None
        if rev is None or asm is None:
            raise not_found("assembly revision")
        _check_project(db, user, asm.project_id, project_id, "assembly revision")
        if asm.archived_at is not None:
            raise _archived("assembly")
        instance = inp.instance.model_dump(mode="json") if inp.instance else None
        if instance:
            assembly_instance_path(db, rev.id, instance["path"], project_id)
        out = assembly_revision_ref(db, rev.id, instance=instance)
        assert out is not None
        return out
    if kind == "drawing_document_version":
        from ..models import DrawingDocument, DrawingDocumentVersion

        version = db.get(DrawingDocumentVersion, oid)
        doc = db.get(DrawingDocument, version.document_id) if version else None
        if version is None or doc is None:
            raise not_found("drawing document version")
        _check_project(db, user, doc.project_id, project_id, "drawing document version")
        if doc.archived_at is not None:
            raise _archived("drawing document")
        entities = None
        if inp.entities:
            ids = sorted(set(inp.entities.ids))
            stored = {e["id"]: e for e in version.entities or []}
            if any(eid not in stored for eid in ids):
                raise AppError(422, "drawing_entity_missing", "a selected entity is absent from this exact drawing version")
            entities = {"ids": ids}
            if inp.entities.sheet_id:
                sheets = {s["id"] for s in version.sheets or []}
                sheet_id = inp.entities.sheet_id
                if sheet_id not in sheets:
                    raise AppError(422, "drawing_sheet_missing", "the selected sheet is absent from this exact drawing version")
                if len(sheets) > 1 and any(stored[eid].get("sheet_id") != sheet_id for eid in ids):
                    raise AppError(422, "entity_sheet_unbound", "these model-space entities have no stored sheet membership")
                entities["sheet_id"] = sheet_id
        return _base(kind, project_id, version.id, document_id=str(doc.id), title=doc.title, version_label=f"V{version.version_no}",
                     summary=f"{len(version.entities or [])} entities · {version.check_status}", entities=entities,
                     source_path=f"/drafting/{doc.id}?version={version.id}")
    # text_excerpt
    if inp.excerpt is None:
        raise AppError(422, "excerpt_required", "a text excerpt reference needs the source kind and the selected text")
    src = _excerpt_source(db, user, project_id, inp.excerpt.source_kind, oid)
    exc = inp.excerpt.model_dump(mode="json")
    text = exc["text"].strip()
    return _base("text_excerpt", project_id, oid, part_id=src.get("part_id"), task_id=src.get("task_id"), version_label=src.get("version_label"),
                 title=src["title"], summary=text[:200], excerpt={"source_kind": exc["source_kind"], "text": exc["text"]},
                 source_path=src["source_path"])


def _spec_version_ref(db: Session, user: User, project_id: uuid.UUID, oid: uuid.UUID) -> dict:
    """W2: an exact engineering spec version (its digest and evidence are read by the context adapter)."""
    from ..models import EngineeringSpec, EngineeringSpecVersion
    from . import engineering_spec as spec_svc

    v = db.get(EngineeringSpecVersion, oid)
    spec = db.get(EngineeringSpec, v.spec_id) if v else None
    if v is None or spec is None:
        raise not_found("engineering spec version")
    _check_project(db, user, spec.project_id, project_id, "engineering spec version")
    if spec.archived_at is not None:
        raise _archived("engineering spec")
    status, _ = spec_svc.effective_status(v)
    counts = spec_svc.version_brief(v)["counts"]
    return _base("engineering_spec_version", project_id, v.id, version_label=f"SP-{spec.number} v{v.version_no}", title=spec.title,
                 summary=f"{status} · {v.execution_policy} · {counts['definitions']} definitions · {counts['questions_open']} open questions",
                 source_path=f"/specs/{spec.id}?version={v.id}")


def assembly_revision_ref(db: Session, revision_id: Any, *, instance: dict | None = None) -> dict | None:
    """W8: an exact assembly revision (version-pinned: a newer revision makes it stale). None when it does not exist."""
    from ..models import Assembly, AssemblyRevision

    rev = db.get(AssemblyRevision, revision_id if isinstance(revision_id, uuid.UUID) else _uuid(revision_id))
    asm = db.get(Assembly, rev.assembly_id) if rev else None
    if rev is None or asm is None:
        return None
    stats = rev.stats or {}
    summary = (f"A{rev.rev_no} · {stats.get('leaf_instances', '?')} instances · {stats.get('part_definitions', '?')} parts · "
               f"{rev.execution_policy} · check {rev.check_status}")
    return _base("assembly_revision", asm.project_id, rev.id, version_label=f"A{rev.rev_no}", title=f"{asm.number} {asm.name}".strip(),
                 summary=summary, source_path=f"/assemblies/{asm.id}/structure?rev={rev.id}", assembly_id=str(asm.id),
                 rev_no=rev.rev_no, instance=instance)  # S6: the exact revision, never "the latest"



def assembly_instance_path(db: Session, revision_id: uuid.UUID, path: list[str], project_id: uuid.UUID) -> list[dict]:
    """Resolve every path segment through the stored exact child revision, never the current head."""
    from ..models import Assembly, AssemblyComponent, AssemblyRevision

    resolved = []
    current = revision_id
    for index, key in enumerate(path):
        rev = db.get(AssemblyRevision, current) if current else None
        asm = db.get(Assembly, rev.assembly_id) if rev else None
        component = db.scalar(select(AssemblyComponent).where(AssemblyComponent.revision_id == current,
                                                               AssemblyComponent.instance_id == key)) if rev else None
        if component is None or asm is None or asm.project_id != project_id or asm.archived_at is not None:
            raise AppError(422, "assembly_instance_missing", "the instance path is absent from this exact assembly revision",
                           {"segment": index})
        resolved.append({"instance_id": key, "name": component.name, "revision_id": str(rev.id),
                         "definition_kind": component.definition_kind,
                         "definition_revision_id": str(component.model_revision_id or component.child_revision_id)})
        current = component.child_revision_id if component.definition_kind == "assembly" else None
    return resolved

def file_name_of(ref: dict | None) -> str:
    """Original file name of a file_version reference (older snapshots: from the summary "kind · name")."""
    if not ref:
        return ""
    name = ref.get("file_name")
    if not name:
        summary = str(ref.get("summary") or "")
        name = summary.split(" · ", 1)[1] if " · " in summary else str(ref.get("title") or "")
    return str(name)


def cad_drawing_format(ref: dict | None) -> str | None:
    """"dxf" / "dwg" when the reference is a native CAD drawing file (a file version, or a document asset version of
    one), else None. The bytes are verified when the operation runs."""
    if not ref:
        return None
    if ref.get("kind") in ("asset_version", "run_output") and ref.get("asset_type") == "document":
        name = str(ref.get("title") or "").lower()
        return "dwg" if name.endswith(".dwg") else "dxf" if name.endswith(".dxf") else None
    if ref.get("kind") != "file_version":
        return None
    name = file_name_of(ref).lower()
    if name.endswith(".dwg"):
        return "dwg"
    if name.endswith(".dxf") or ref.get("file_kind") == "drawing_dxf":
        return "dxf"
    return None


def is_step_file(ref: dict | None) -> bool:
    if not ref or ref.get("kind") != "file_version":
        return False
    return ref.get("file_kind") == "model_step" or file_name_of(ref).lower().endswith((".step", ".stp"))


def is_drawing_asset(ref: dict | None) -> bool:
    return bool(ref) and ref.get("kind") in ("asset_version", "run_output") and ref.get("asset_type") == "drawing"  # type: ignore[union-attr]


# --- status on read (design §3.3) --------------------------------------------------------


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def ref_statuses(db: Session, refs: list[dict]) -> dict[str, dict]:
    """{key: {"state": ok|stale|unavailable, "latest_label"?, "reason"?}} for every distinct key, in a few batched queries."""
    by_kind: dict[str, set[uuid.UUID]] = defaultdict(set)
    uniq: dict[str, dict] = {}
    for r in refs:
        if not r or not r.get("key") or r["key"] in uniq:
            continue
        uniq[r["key"]] = r
        oid = _uuid(r.get("object_id"))
        if oid is None:
            continue
        kind = r.get("kind")
        if kind == "text_excerpt":
            src = (r.get("excerpt") or {}).get("source_kind")
            kind = {"quote": "quote", "task": "task", "criterion": "criterion", "asset_version": "asset_version",
                    "part_revision": "part_revision"}.get(src, "")
        if kind == "cad_selection":
            kind = "part_revision"
        if kind == "quote_field":
            kind = "quote"
        if kind == "run_output":
            kind = "asset_version"
        by_kind[kind].add(oid)

    found: dict[str, dict[uuid.UUID, dict]] = defaultdict(dict)
    part_ids: set[uuid.UUID] = set()

    if by_kind.get("part_revision"):
        for rev in db.scalars(select(ModelRevision).where(ModelRevision.id.in_(by_kind["part_revision"]))).all():
            found["part_revision"][rev.id] = {"part_id": rev.part_id, "rev_no": rev.rev_no}
            part_ids.add(rev.part_id)
    if by_kind.get("quote"):
        for q in db.scalars(select(Quote).where(Quote.id.in_(by_kind["quote"]))).all():
            found["quote"][q.id] = {"part_id": q.part_id}
            part_ids.add(q.part_id)
    part_ids |= by_kind.get("part", set())
    if by_kind.get("workflow"):
        for wid, version, archived_at, wpart, graph in db.execute(
                select(Workflow.id, Workflow.version, Workflow.archived_at, Workflow.part_id, Workflow.graph)
                .where(Workflow.id.in_(by_kind["workflow"]))).all():
            # Node ids of the current graph: a node reference whose node left it is unavailable (node_removed).
            nodes = {n.get("id") for n in (graph or {}).get("nodes") or [] if isinstance(n, dict)}
            found["workflow"][wid] = {"version": version, "archived": archived_at is not None, "part_id": wpart, "nodes": nodes}
            if wpart:
                part_ids.add(wpart)
    parts = {p.id: p for p in db.scalars(select(Part).where(Part.id.in_(part_ids))).all()} if part_ids else {}
    latest_rev: dict[uuid.UUID, int] = {}
    rev_parts = {d["part_id"] for d in found["part_revision"].values()}
    if rev_parts:
        latest_rev = dict(db.execute(select(ModelRevision.part_id, func.max(ModelRevision.rev_no))
                                     .where(ModelRevision.part_id.in_(rev_parts)).group_by(ModelRevision.part_id)).all())
    if by_kind.get("asset_version"):
        rows = db.execute(select(AssetVersion, Asset).join(Asset, Asset.id == AssetVersion.asset_id)
                          .where(AssetVersion.id.in_(by_kind["asset_version"]))).all()
        asset_ids = {a.id for _, a in rows}
        latest = {}
        if asset_ids:
            sub = (select(AssetVersion.asset_id, func.max(AssetVersion.version_no).label("n"))
                   .where(AssetVersion.asset_id.in_(asset_ids)).group_by(AssetVersion.asset_id).subquery())
            latest = {v.asset_id: v for v in db.scalars(
                select(AssetVersion).join(sub, (sub.c.asset_id == AssetVersion.asset_id) & (sub.c.n == AssetVersion.version_no))).all()}
        for v, a in rows:
            found["asset_version"][v.id] = {"version": v, "asset": a, "latest": latest.get(a.id)}
    if by_kind.get("run"):
        for rid in db.scalars(select(WorkflowRun.id).where(WorkflowRun.id.in_(by_kind["run"]))).all():
            found["run"][rid] = {}
    if by_kind.get("task"):
        for tid in db.scalars(select(Task.id).where(Task.id.in_(by_kind["task"]))).all():
            found["task"][tid] = {}
    if by_kind.get("criterion"):
        for cid in db.scalars(select(AcceptanceCriterion.id).where(AcceptanceCriterion.id.in_(by_kind["criterion"]))).all():
            found["criterion"][cid] = {}
    if by_kind.get("file_version"):
        for fv, f in db.execute(select(FileVersion, File).join(File, File.id == FileVersion.file_id)
                                .where(FileVersion.id.in_(by_kind["file_version"]))).all():
            found["file_version"][fv.id] = {"archived": f.archived_at is not None}
    if by_kind.get("engineering_spec_version"):
        from ..models import EngineeringSpec, EngineeringSpecVersion

        for sv, spec in db.execute(select(EngineeringSpecVersion, EngineeringSpec).join(EngineeringSpec, EngineeringSpec.id == EngineeringSpecVersion.spec_id)
                                   .where(EngineeringSpecVersion.id.in_(by_kind["engineering_spec_version"]))).all():
            latest = db.scalar(select(func.max(EngineeringSpecVersion.version_no)).where(EngineeringSpecVersion.spec_id == spec.id))
            found["engineering_spec_version"][sv.id] = {"archived": spec.archived_at is not None, "version_no": sv.version_no,
                                                        "latest": latest or sv.version_no, "number": spec.number}

    if by_kind.get("assembly_revision"):
        from ..models import Assembly, AssemblyRevision

        for rev, asm in db.execute(select(AssemblyRevision, Assembly).join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
                                   .where(AssemblyRevision.id.in_(by_kind["assembly_revision"]))).all():
            latest = db.scalar(select(func.max(AssemblyRevision.rev_no)).where(AssemblyRevision.assembly_id == asm.id))
            found["assembly_revision"][rev.id] = {"archived": asm.archived_at is not None, "rev_no": rev.rev_no, "latest": latest or rev.rev_no}

    if by_kind.get("drawing_document_version"):
        from ..models import DrawingDocument, DrawingDocumentVersion

        for version, doc in db.execute(select(DrawingDocumentVersion, DrawingDocument)
                .join(DrawingDocument, DrawingDocument.id == DrawingDocumentVersion.document_id)
                .where(DrawingDocumentVersion.id.in_(by_kind["drawing_document_version"]))):
            found["drawing_document_version"][version.id] = {"archived": doc.archived_at is not None,
                "version_no": version.version_no, "latest": doc.head_version_no}

    out: dict[str, dict] = {}
    for key, r in uniq.items():
        out[key] = _status_of(db, r, found, parts, latest_rev)
    return out


_OK = {"state": "ok"}


def _gone(reason: str = "deleted") -> dict:
    return {"state": "unavailable", "reason": reason}


def _status_of(db: Session, r: dict, found: dict, parts: dict, latest_rev: dict) -> dict:
    kind = r.get("kind")
    oid = _uuid(r.get("object_id"))
    if oid is None:
        return _gone()
    if kind == "text_excerpt":
        src = (r.get("excerpt") or {}).get("source_kind")
        kind_found = {"quote": "quote", "task": "task", "criterion": "criterion", "asset_version": "asset_version",
                      "part_revision": "part_revision"}.get(src)
        return dict(_OK) if kind_found and oid in found[kind_found] else _gone()
    if kind == "part":
        p = parts.get(oid)
        if p is None:
            return _gone()
        return _gone("archived") if p.archived_at is not None else dict(_OK)
    if kind in ("part_revision", "cad_selection"):
        d = found["part_revision"].get(oid)
        if d is None:
            return _gone()
        p = parts.get(d["part_id"])
        if p is None:
            return _gone()
        if p.archived_at is not None:
            return _gone("archived")
        latest = latest_rev.get(d["part_id"], d["rev_no"])
        if latest > d["rev_no"]:
            return {"state": "stale", "latest_label": f"R{latest}"}
        return dict(_OK)
    if kind in ("quote", "quote_field"):
        d = found["quote"].get(oid)
        if d is None:
            return _gone()
        p = parts.get(d["part_id"])
        return _gone("archived") if p is not None and p.archived_at is not None else dict(_OK)
    if kind in ("asset_version", "run_output"):
        d = found["asset_version"].get(oid)
        if d is None:
            return _gone()
        if d["asset"].archived_at is not None:
            return _gone("archived")
        latest = d["latest"]
        if latest is not None and latest.version_no > d["version"].version_no:
            from . import assets

            row = assets.specialised(db, latest)
            return {"state": "stale", "latest_label": assets.human_label(db, d["asset"].type, row, latest)}
        return dict(_OK)
    if kind in ("run", "task", "criterion"):
        return dict(_OK) if oid in found[kind] else _gone()
    if kind == "file_version":
        d = found["file_version"].get(oid)
        if d is None:
            return _gone()
        return _gone("archived") if d["archived"] else dict(_OK)
    if kind == "engineering_spec_version":
        d = found["engineering_spec_version"].get(oid)
        if d is None:
            return _gone()
        if d["archived"]:
            return _gone("archived")
        if d["latest"] > d["version_no"]:
            return {"state": "stale", "latest_label": f"SP-{d['number']} v{d['latest']}"}
        return dict(_OK)
    if kind == "assembly_revision":
        d = found["assembly_revision"].get(oid)
        if d is None:
            return _gone()
        if d["archived"]:
            return _gone("archived")
        if d["latest"] > d["rev_no"]:
            return {"state": "stale", "latest_label": f"A{d['latest']}"}
        return dict(_OK)
    if kind == "drawing_document_version":
        d = found[kind].get(oid)
        if d is None or d["archived"]:
            return _gone("archived" if d else "deleted")
        return {"state": "stale", "latest_label": f"V{d['latest']}"} if d["latest"] > d["version_no"] else dict(_OK)
    if kind == "workflow":
        d = found["workflow"].get(oid)
        if d is None:
            return _gone()
        p = parts.get(d["part_id"]) if d["part_id"] else None
        if d["archived"] or (p is not None and p.archived_at is not None):
            return _gone("archived")
        node = r.get("node") if isinstance(r.get("node"), dict) else None
        if node is not None and node.get("node_id") not in d["nodes"]:
            return _gone("node_removed")
        pinned = r.get("version")
        if isinstance(pinned, int) and d["version"] > pinned:
            return {"state": "stale", "latest_label": f"v{d['version']}"}
        return dict(_OK)
    return _gone()
