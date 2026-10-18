"""Static assemblies (W4): definitions, immutable revisions, instances, mates, BOM, checks, STEP, change.

An :class:`~app.models.AssemblyRevision` freezes the direct children of one assembly definition. Each child
(:class:`~app.models.AssemblyComponent`) references an exact part model revision or an exact child assembly
revision, carries a stable instance id (``I-0001``; never reused, carried across revisions), a rigid local
transform, a fixed (ground) flag and a suppressed flag. Nested structure comes from child assembly revisions,
so a sub-assembly definition is stored once and reused by every instance.

Every change creates a new revision against an optimistic base (409 ``revision_conflict``); part revision
updates never rewrite an existing assembly revision: they show up as "update available" on the components
and downstream artifacts bound to an older assembly revision are reported stale.

Frozen service signatures for W5 / W8 (W0 contract §4)::

    resolve_geometry(db, assembly_revision_id) -> AssemblyGeometry
    bom_lines(db, assembly_revision_id) -> {"hierarchical": [BomLine], "flattened": [BomLine]}

plus :func:`create_assembly`, :func:`create_from_spec`, :func:`import_step`, :func:`edit_revision`,
:func:`run_checks`, :func:`export_step`, :func:`rollup`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import policy as pol
from ..cad import assembly_model as am
from ..cad.assembly_model import AssemblyGeometry, BomLine, Datum, Occurrence, PartDefinition
from ..errors import AppError, bad_request, conflict, not_found
from ..models import (
    Assembly,
    AssemblyComponent,
    AssemblyMate,
    AssemblyPartProps,
    AssemblyRevision,
    AssemblyRollup,
    AssetDependency,
    File,
    FileVersion,
    ModelRevision,
    Part,
    ProcessPlan,
    Quote,
    User,
)
from ..storage import read_blob, save_blob
from . import assets
from .audit import audit

log = logging.getLogger(__name__)

# Budgets (W0 contract §7): beyond them the request is refused with 422 assembly_too_large and the counts.
MAX_LEAVES = 2000
MAX_DEFINITIONS = 500
MAX_DEPTH = 10

INSTANCE_RE = re.compile(r"^I-\d{4,}$")
MATE_RE = re.compile(r"^M-\d{4,}$")
UNKNOWN_MATERIALS = {"", "unknown", "UNKNOWN", "未知"}
BOM_SCHEMA = "partroute.assembly_bom/1"
REQUIRED_CHECKS = ("assembly.mate_residual", "assembly.bom", "assembly.interference")


# =====================================================================================================
# Plain component / mate records (the in-memory form of a revision's rows)
# =====================================================================================================


@dataclass
class Comp:
    instance_id: str
    name: str
    kind: str  # part | assembly
    transform: am.Matrix4
    part_id: uuid.UUID | None = None
    model_revision_id: uuid.UUID | None = None
    child_assembly_id: uuid.UUID | None = None
    child_revision_id: uuid.UUID | None = None
    fixed: bool = False
    suppressed: bool = False
    placement: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)
    source_ref: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def def_id(self) -> uuid.UUID:
        return self.model_revision_id if self.kind == "part" else self.child_revision_id  # type: ignore[return-value]

    def content(self) -> dict[str, Any]:
        return {"instance_id": self.instance_id, "name": self.name, "kind": self.kind, "definition": str(self.def_id),
                "transform": [[round(v, 9) + 0.0 for v in row] for row in self.transform], "fixed": self.fixed,
                "suppressed": self.suppressed, "display": self.display}


@dataclass
class MateRec:
    mate_id: str
    kind: str
    a: dict[str, Any]
    b: dict[str, Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    suppressed: bool = False
    source: str = "user"
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    result: dict[str, Any] = field(default_factory=dict)

    def content(self) -> dict[str, Any]:
        return {"mate_id": self.mate_id, "kind": self.kind, "a": self.a, "b": self.b, "params": self.params,
                "priority": self.priority, "suppressed": self.suppressed}


def _comp_from_row(r: AssemblyComponent) -> Comp:
    return Comp(instance_id=r.instance_id, name=r.name, kind=r.definition_kind, transform=am.from_list(r.transform),
                part_id=r.part_id, model_revision_id=r.model_revision_id, child_assembly_id=r.child_assembly_id,
                child_revision_id=r.child_revision_id, fixed=r.fixed, suppressed=r.suppressed, placement=dict(r.placement or {}),
                display=dict(r.display or {}), source_ref=r.source_ref, meta=dict(r.meta or {}))


def _mate_from_row(r: AssemblyMate) -> MateRec:
    return MateRec(mate_id=r.mate_id, kind=r.kind, a=dict(r.a), b=dict(r.b) if r.b else None, params=dict(r.params or {}),
                   priority=r.priority, suppressed=r.suppressed, source=r.source, meta=dict(r.meta or {}), status=r.status,
                   result=dict(r.result or {}))


def _canon(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()


def is_unknown_material(material: str | None) -> bool:
    return material is None or material.strip() in UNKNOWN_MATERIALS


# =====================================================================================================
# Loading and the expanded tree
# =====================================================================================================


def too_large(counts: dict[str, int]) -> AppError:
    return AppError(422, "assembly_too_large",
                    f"the assembly exceeds the supported size (leaf instances ≤ {MAX_LEAVES}, unique definitions ≤ "
                    f"{MAX_DEFINITIONS}, nesting ≤ {MAX_DEPTH} levels)",
                    {"counts": counts, "limits": {"leaf_instances": MAX_LEAVES, "definitions": MAX_DEFINITIONS, "depth": MAX_DEPTH}})


@dataclass
class Leaf:
    path: tuple[str, ...]  # instance ids root -> leaf
    names: tuple[str, ...]
    part_key: str  # model revision id
    world: am.Matrix4


class Tree:
    """Lazy loader of the nested structure under one assembly revision (all rows cached per instance).

    ``override`` replaces the direct children of one revision id with in-memory records (edit previews and
    revisions being created)."""

    ROOT = "__root__"

    def __init__(self, db: Session, root: uuid.UUID | str, *, override: list[Comp] | None = None, root_name: str = ""):
        self.db = db
        self.root = str(root)
        self.override = override
        self.root_name = root_name
        self._comps: dict[str, list[Comp]] = {}
        self.revisions: dict[str, AssemblyRevision] = {}
        self.assemblies: dict[str, Assembly] = {}
        self.part_revisions: dict[str, ModelRevision] = {}
        self.parts: dict[str, Part] = {}

    # -- rows -------------------------------------------------------------------------------------- #

    def revision(self, rid: str) -> AssemblyRevision | None:
        if rid not in self.revisions:
            row = self.db.get(AssemblyRevision, uuid.UUID(rid)) if rid != self.ROOT else None
            if row is None:
                return None
            self.revisions[rid] = row
        return self.revisions[rid]

    def assembly(self, aid: uuid.UUID | str) -> Assembly | None:
        key = str(aid)
        if key not in self.assemblies:
            row = self.db.get(Assembly, uuid.UUID(key))
            if row is None:
                return None
            self.assemblies[key] = row
        return self.assemblies[key]

    def children(self, rid: str) -> list[Comp]:
        if self.override is not None and rid == self.root:
            return self.override
        if rid not in self._comps:
            rows = self.db.scalars(select(AssemblyComponent).where(AssemblyComponent.revision_id == uuid.UUID(rid))
                                   .order_by(AssemblyComponent.position)).all()
            self._comps[rid] = [_comp_from_row(r) for r in rows]
            self._prefetch(self._comps[rid])
        return self._comps[rid]

    def _prefetch(self, comps: list[Comp]) -> None:
        ids = [c.model_revision_id for c in comps if c.model_revision_id and str(c.model_revision_id) not in self.part_revisions]
        if ids:
            for rev in self.db.scalars(select(ModelRevision).where(ModelRevision.id.in_(set(ids)))).all():
                self.part_revisions[str(rev.id)] = rev
            part_ids = {r.part_id for r in self.part_revisions.values() if str(r.part_id) not in self.parts}
            for p in self.db.scalars(select(Part).where(Part.id.in_(part_ids))).all():
                self.parts[str(p.id)] = p

    def part_revision(self, key: str) -> ModelRevision | None:
        if key not in self.part_revisions:
            row = self.db.get(ModelRevision, uuid.UUID(key))
            if row is None:
                return None
            self.part_revisions[key] = row
        return self.part_revisions[key]

    def part(self, part_id: uuid.UUID | str) -> Part | None:
        key = str(part_id)
        if key not in self.parts:
            row = self.db.get(Part, uuid.UUID(key))
            if row is None:
                return None
            self.parts[key] = row
        return self.parts[key]

    def def_name(self, c: Comp) -> str:
        if c.kind == "part":
            p = self.part(c.part_id) if c.part_id else None
            return p.name if p else "?"
        a = self.assembly(c.child_assembly_id) if c.child_assembly_id else None
        return a.name if a else "?"

    # -- expansion ------------------------------------------------------------------------------------ #

    def leaves(self, *, include_suppressed: bool = False) -> list[Leaf]:
        out: list[Leaf] = []

        def walk(rid: str, acc: am.Matrix4, path: tuple[str, ...], names: tuple[str, ...], depth: int) -> None:
            if depth > MAX_DEPTH:
                raise too_large({"depth": depth})
            for c in self.children(rid):
                if c.suppressed and not include_suppressed:
                    continue
                world = am.compose(acc, c.transform)
                if c.kind == "part":
                    out.append(Leaf(path + (c.instance_id,), names + (c.name,), str(c.model_revision_id), world))
                    if len(out) > MAX_LEAVES:
                        raise too_large({"leaf_instances": len(out)})
                else:
                    walk(str(c.child_revision_id), world, path + (c.instance_id,), names + (c.name,), depth + 1)

        walk(self.root, am.IDENTITY, (), (), 0)
        return out

    def stats(self) -> dict[str, Any]:
        """Counts of the current (non-suppressed) structure; raises 422 assembly_too_large past the budget."""
        part_defs: set[str] = set()
        asm_defs: set[str] = {self.root}
        sub_instances = 0
        suppressed = 0
        leaves = 0
        max_depth = 0

        def walk(rid: str, depth: int) -> None:
            nonlocal sub_instances, suppressed, leaves, max_depth
            max_depth = max(max_depth, depth)
            if depth > MAX_DEPTH:
                raise too_large({"depth": depth, "leaf_instances": leaves, "definitions": len(part_defs) + len(asm_defs)})
            for c in self.children(rid):
                if c.suppressed:
                    suppressed += 1
                    continue
                if c.kind == "part":
                    leaves += 1
                    part_defs.add(str(c.model_revision_id))
                else:
                    sub_instances += 1
                    asm_defs.add(str(c.child_revision_id))
                    walk(str(c.child_revision_id), depth + 1)
                if leaves > MAX_LEAVES or len(part_defs) + len(asm_defs) > MAX_DEFINITIONS:
                    raise too_large({"leaf_instances": leaves, "definitions": len(part_defs) + len(asm_defs), "depth": max_depth})

        walk(self.root, 1)
        return {"leaf_instances": leaves, "part_definitions": len(part_defs), "assembly_definitions": len(asm_defs),
                "subassembly_instances": sub_instances, "suppressed_instances": suppressed, "depth": max_depth,
                "direct_children": len(self.children(self.root))}


def _load_revision(db: Session, rid: uuid.UUID | str) -> AssemblyRevision:
    try:
        key = uuid.UUID(str(rid))
    except ValueError:
        raise not_found("assembly revision")
    rev = db.get(AssemblyRevision, key)
    if rev is None:
        raise not_found("assembly revision")
    return rev


def latest_revision(db: Session, assembly_id: uuid.UUID) -> AssemblyRevision | None:
    return db.scalar(select(AssemblyRevision).where(AssemblyRevision.assembly_id == assembly_id).order_by(AssemblyRevision.rev_no.desc()).limit(1))


def latest_part_revision(db: Session, part_id: uuid.UUID) -> ModelRevision | None:
    return db.scalar(select(ModelRevision).where(ModelRevision.part_id == part_id, ModelRevision.status == "ready")
                     .order_by(ModelRevision.rev_no.desc()).limit(1))


def _latest_part_definition(db: Session, part_id: uuid.UUID) -> ModelRevision | None:
    """Latest ready entity usable by an assembly, excluding newer mesh references."""
    return db.scalar(select(ModelRevision).where(ModelRevision.part_id == part_id, ModelRevision.status == "ready",
                                                ModelRevision.mesh_only.is_(False), ModelRevision.step_sha.is_not(None))
                     .order_by(ModelRevision.rev_no.desc()).limit(1))


# =====================================================================================================
# Part BOM attributes
# =====================================================================================================


def part_props(db: Session, part_ids: list[uuid.UUID]) -> dict[str, AssemblyPartProps]:
    if not part_ids:
        return {}
    return {str(r.part_id): r for r in db.scalars(select(AssemblyPartProps).where(AssemblyPartProps.part_id.in_(set(part_ids)))).all()}


def set_part_props(db: Session, part: Part, user: User | None, *, procurement: str | None = None, representation: str | None = None,
                   designation: str | None = None, source: dict[str, Any] | None = None) -> AssemblyPartProps:
    row = db.get(AssemblyPartProps, part.id)
    if row is None:
        row = AssemblyPartProps(part_id=part.id, procurement="unknown", representation="exact", designation="", source={})
        db.add(row)
    if procurement is not None:
        if procurement not in ("make", "buy", "standard", "unknown"):
            raise bad_request("invalid_procurement", "procurement must be make, buy, standard or unknown")
        row.procurement = procurement
    if representation is not None:
        if representation not in ("exact", "nominal", "simplified"):
            raise bad_request("invalid_representation", "representation must be exact, nominal or simplified")
        row.representation = representation
    if designation is not None:
        row.designation = designation[:200]
    if source is not None:
        row.source = source
    row.updated_by = user.id if user else None
    db.flush()
    return row


def _representation(rev: ModelRevision, props: AssemblyPartProps | None) -> str:
    # W3 may freeze a representation level on the part revision itself; it wins over the part-level attribute.
    level = getattr(rev, "representation", None)
    if level in ("exact", "nominal", "simplified"):
        return level
    return props.representation if props else "exact"


# =====================================================================================================
# BOM
# =====================================================================================================


def target_items(spec_ref: dict[str, Any] | None) -> dict[str, str] | None:
    """Generation supplement B-03: the flattened item numbers of a partial spec build (part revision id → the item the
    definition has in the spec's full target). A missing definition keeps its number in the drawing's BOM, so the items
    of what was built keep theirs too (gaps where something is missing). None for every other revision."""
    ref = spec_ref if isinstance(spec_ref, dict) else {}
    if ((ref.get("coverage") or {}).get("status")) != "partial":
        return None
    out = {str(t["revision_id"]): str(t["item"]) for t in ref.get("target_bom") or [] if isinstance(t, dict) and t.get("revision_id")}
    return out or None


def _tree_target_items(tree: Tree) -> dict[str, str] | None:
    if hasattr(tree, "target_items"):
        return tree.target_items  # type: ignore[no-any-return]
    root = tree.revision(tree.root)
    return target_items(root.spec_ref if root is not None else None)


def compute_bom(db: Session, tree: Tree) -> dict[str, Any]:
    """Hierarchical (per parent definition) and flattened (leaf totals) BOM from the instance tree.

    Repeated sub-assembly instances share one definition: its children are listed once (under the first
    occurrence); the parent line counts its instances. Suppressed instances are left out and listed apart.
    A partial spec build numbers its flattened items as in the spec's full target (:func:`target_items`)."""
    leaves = tree.leaves()
    part_ids = [tree.part_revision(leaf.part_key).part_id for leaf in leaves if tree.part_revision(leaf.part_key)]  # type: ignore[union-attr]
    props = part_props(db, part_ids)

    def part_fields(key: str) -> dict[str, Any]:
        rev = tree.part_revision(key)
        part = tree.part(rev.part_id) if rev else None
        p = props.get(str(rev.part_id)) if rev else None
        material = rev.material if rev else None
        return {"name": part.name if part else "?", "number": part.number if part else None,
                "material": None if is_unknown_material(material) else material,
                "procurement": p.procurement if p else "unknown",
                "representation": _representation(rev, p) if rev else "exact",
                "part_id": str(rev.part_id) if rev else None, "rev_no": rev.rev_no if rev else None,
                "designation": p.designation if p else ""}

    # flattened: leaf definitions in first-occurrence order
    flat: OrderedDict[str, list[Leaf]] = OrderedDict()
    for leaf in leaves:
        flat.setdefault(leaf.part_key, []).append(leaf)
    flattened: list[dict[str, Any]] = []
    targets = _tree_target_items(tree)
    numbers: dict[str, str] = {}
    if targets:
        nxt = max((int(v) for v in targets.values() if v.isdigit()), default=0)
        for key in flat:
            if key in targets and targets[key] not in numbers.values():
                numbers[key] = targets[key]
            else:  # not part of the spec's target (e.g. added by a later edit): numbered after it
                nxt += 1
                numbers[key] = str(nxt)
    for i, (key, occ) in enumerate(flat.items(), 1):
        f = part_fields(key)
        flattened.append({"item": numbers.get(key, str(i)), "level": 0, "def_key": key, "def_kind": "part", "name": f["name"], "number": f["number"],
                          "material": f["material"], "procurement": f["procurement"], "representation": f["representation"],
                          "quantity": len(occ), "total_quantity": len(occ), "instance_paths": [list(o.path) for o in occ],
                          "part_id": f["part_id"], "rev_no": f["rev_no"], "designation": f["designation"]})
    if targets:
        flattened.sort(key=lambda line: int(line["item"]))

    # occurrences of every assembly definition (paths of its instances from the root)
    asm_paths: dict[str, list[tuple[str, ...]]] = {tree.root: [()]}

    def collect(rid: str, path: tuple[str, ...], depth: int) -> None:
        if depth > MAX_DEPTH:
            raise too_large({"depth": depth})
        for c in tree.children(rid):
            if c.suppressed or c.kind != "assembly":
                continue
            child = str(c.child_revision_id)
            asm_paths.setdefault(child, []).append(path + (c.instance_id,))
            collect(child, path + (c.instance_id,), depth + 1)

    collect(tree.root, (), 0)
    hierarchical: list[dict[str, Any]] = []
    expanded: dict[str, str] = {}

    def lines_for(rid: str, prefix: str, level: int) -> None:
        expanded[rid] = prefix
        groups: OrderedDict[str, list[Comp]] = OrderedDict()
        for c in tree.children(rid):
            if not c.suppressed:
                groups.setdefault(str(c.def_id), []).append(c)
        parent_paths = asm_paths.get(rid, [])
        for n, (key, comps) in enumerate(groups.items(), 1):
            item = f"{prefix}.{n}" if prefix else str(n)
            c0 = comps[0]
            paths = [list(pp + (c.instance_id,)) for pp in parent_paths for c in comps]
            if c0.kind == "part":
                f = part_fields(key)
                line = {"name": f["name"], "number": f["number"], "material": f["material"], "procurement": f["procurement"],
                        "representation": f["representation"], "part_id": f["part_id"], "rev_no": f["rev_no"]}
            else:
                asm = tree.assembly(c0.child_assembly_id) if c0.child_assembly_id else None
                crev = tree.revision(key)
                line = {"name": asm.name if asm else "?", "number": asm.number if asm else None, "material": None,
                        "procurement": "make", "representation": "exact", "assembly_id": str(c0.child_assembly_id),
                        "rev_no": crev.rev_no if crev else None}
            hierarchical.append({"item": item, "level": level, "def_key": key, "def_kind": c0.kind, **line,
                                 "parent_def_key": rid if rid != Tree.ROOT else None,
                                 "quantity": len(comps), "total_quantity": len(comps) * len(parent_paths), "instance_paths": paths,
                                 "expanded_at": expanded.get(key) if c0.kind == "assembly" and key in expanded else None})
            if c0.kind == "assembly" and key not in expanded:
                lines_for(key, item, level + 1)

    lines_for(tree.root, "", 1)
    suppressed: list[dict[str, Any]] = []

    def sup(rid: str, path: tuple[str, ...], depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for c in tree.children(rid):
            if c.suppressed:
                suppressed.append({"path": list(path + (c.instance_id,)), "name": c.name, "def_kind": c.kind, "def_key": str(c.def_id)})
            elif c.kind == "assembly":
                sup(str(c.child_revision_id), path + (c.instance_id,), depth + 1)

    sup(tree.root, (), 0)
    counts = {"leaf_instances": len(leaves), "part_definitions": len(flattened),
              "assembly_definitions": len(asm_paths), "subassembly_instances": sum(len(v) for k, v in asm_paths.items() if k != tree.root),
              "suppressed_instances": len(suppressed)}
    out = {"schema": BOM_SCHEMA, "flattened": flattened, "hierarchical": hierarchical, "counts": counts, "suppressed": suppressed,
           "rules": {"quantities": "instance tree", "repeated_subassemblies": "children listed once per definition; parent line counts instances",
                     "suppressed": "kept in history, excluded from BOM and outputs", "unknown": "material / procurement stay unknown"}}
    if targets:
        out["item_basis"] = "spec_target"
        out["rules"]["items"] = "partial build: flattened items keep the numbers of the spec's full target (gaps are missing items)"
    return out


def bom_hash(bom: dict[str, Any]) -> str:
    """Hash of the frozen structure of a BOM (definitions, items, quantities, paths)."""
    keep = ("item", "level", "def_key", "def_kind", "quantity", "total_quantity", "instance_paths")
    return _canon({"flattened": [{k: line.get(k) for k in keep} for line in bom.get("flattened") or []],
                   "hierarchical": [{k: line.get(k) for k in keep} for line in bom.get("hierarchical") or []]})


def _bom_line(d: dict[str, Any]) -> BomLine:
    return BomLine(item=d["item"], level=int(d["level"]), def_key=d["def_key"], def_kind=d["def_kind"], name=d["name"], number=d.get("number"),
                   material=d.get("material"), procurement=d.get("procurement") or "unknown", representation=d.get("representation") or "exact",
                   quantity=int(d["quantity"]), total_quantity=int(d["total_quantity"]),
                   instance_paths=tuple(tuple(p) for p in d.get("instance_paths") or []))


def bom_lines(db: Session, assembly_revision_id: uuid.UUID | str) -> dict[str, list[BomLine]]:
    """W0 §4 service: hierarchical (level ≥ 1, per parent) and flattened (level 0, totals) BOM lines of an
    exact assembly revision, from its instance tree and the parts' current BOM attributes."""
    rev = _load_revision(db, assembly_revision_id)
    bom = compute_bom(db, Tree(db, rev.id))
    return {"hierarchical": [_bom_line(d) for d in bom["hierarchical"]], "flattened": [_bom_line(d) for d in bom["flattened"]]}


def subassembly_revisions(db: Session, assembly_revision_id: uuid.UUID | str) -> list[dict[str, Any]]:
    """Every distinct sub-assembly revision under an exact assembly revision (generation supplement E, layered
    delivery), depth first in instance order, the first occurrence of each. Repeated instances of one revision are one
    entry (``instances``: how many instances of it the whole tree holds); two revisions of one assembly are two
    entries. Suppressed instances are left out, as in every other output."""
    rev = _load_revision(db, assembly_revision_id)
    tree = Tree(db, rev.id)
    tree.stats()  # budget
    out: dict[str, dict[str, Any]] = {}

    def walk(rid: str, parent: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise too_large({"depth": depth})
        for c in tree.children(rid):
            if c.suppressed or c.kind != "assembly":
                continue
            key = str(c.child_revision_id)
            if key in out:
                out[key]["instances"] += 1
            else:
                crev = tree.revision(key)
                asm = tree.assembly(c.child_assembly_id) if c.child_assembly_id else None
                out[key] = {"assembly_id": str(c.child_assembly_id), "assembly_revision_id": key, "number": asm.number if asm else None,
                            "name": asm.name if asm else c.name, "rev_no": crev.rev_no if crev else None, "depth": depth,
                            "parent_revision_id": parent, "instances": 1}
            walk(key, key, depth + 1)

    walk(str(rev.id), str(rev.id), 1)
    return list(out.values())


# =====================================================================================================
# Geometry (W5) and part shapes
# =====================================================================================================

_SHAPES: OrderedDict[str, Any] = OrderedDict()


def part_shape(db: Session, rev: ModelRevision, *, deadline: float | None = None,
               cancelled: Callable[[], bool] | None = None) -> Any:
    """The exact STEP B-rep, with optional cooperative bounds on loading/lock wait.

    An individual STEP import cannot be preempted; the deadline is checked before
    and after it. Existing callers without bounds keep their original behavior.
    """
    import time

    from ..cad.assembly_step import read_shape
    from .revisions import KERNEL_LOCK

    def check() -> None:
        if cancelled and cancelled():
            raise AppError(422, "shape_load_cancelled", "part shape loading was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise AppError(422, "shape_load_deadline", "part shape loading reached its deadline")

    check()
    if not rev.step_sha:
        raise AppError(422, "definition_without_step", f"part revision R{rev.rev_no} has no STEP (mesh reference)",
                       {"model_revision_id": str(rev.id)})
    if rev.step_sha in _SHAPES:
        _SHAPES.move_to_end(rev.step_sha)
        return _SHAPES[rev.step_sha]
    data, _ = read_blob(db, rev.step_sha)
    check()
    if deadline is None and cancelled is None:
        KERNEL_LOCK.acquire()
    else:
        while True:
            check()
            remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else 0.05
            if KERNEL_LOCK.acquire(timeout=min(0.05, remaining)):
                break
    try:
        check()
        shape = read_shape(data)
    finally:
        KERNEL_LOCK.release()
    check()
    _SHAPES[rev.step_sha] = shape
    if len(_SHAPES) > 512:
        _SHAPES.popitem(last=False)
    return shape


def _bbox_of(rev: ModelRevision) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    b = (rev.metrics or {}).get("bbox") if isinstance(rev.metrics, dict) else None
    if not b or "min" not in b:
        return None
    return (tuple(b["min"]), tuple(b["max"]))  # type: ignore[return-value]


def _assembly_bbox(tree: Tree, rid: str, depth: int = 0) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """The bounding box of an assembly revision in its own frame: the union of its non-suppressed children's boxes
    (parts by their revision's bbox, sub-assemblies recursively), each moved by its transform. Generation reliability
    §2.6: a sub-assembly instance shown in the Turbo display area keeps the room of what is placed inside it."""
    if depth > MAX_DEPTH:
        return None
    pts: list[tuple[float, float, float]] = []
    for c in tree.children(rid):
        if c.suppressed:
            continue
        if c.kind == "part":
            prev = tree.part_revision(str(c.model_revision_id)) if c.model_revision_id else None
            b = _bbox_of(prev) if prev is not None else None
        else:
            b = _assembly_bbox(tree, str(c.child_revision_id), depth + 1) if c.child_revision_id else None
        if b is not None:
            lo, hi = b
            pts += [am.apply(c.transform, (x, y, z)) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    if not pts:
        return None
    return (tuple(min(p[j] for p in pts) for j in range(3)), tuple(max(p[j] for p in pts) for j in range(3)))  # type: ignore[return-value]


def resolve_geometry(db: Session, assembly_revision_id: uuid.UUID | str, *, with_shapes: bool = True) -> AssemblyGeometry:
    """W0 §4 service: an exact assembly revision as pure data (unique part definitions with shapes in their own
    frame, expanded non-suppressed leaf occurrences with world transforms)."""
    rev = _load_revision(db, assembly_revision_id)
    asm = db.get(Assembly, rev.assembly_id)
    assert asm is not None
    tree = Tree(db, rev.id)
    tree.stats()  # budget
    leaves = tree.leaves()
    keys = list(OrderedDict.fromkeys(leaf.part_key for leaf in leaves))
    props = part_props(db, [tree.part_revision(k).part_id for k in keys if tree.part_revision(k)])  # type: ignore[union-attr]
    parts: dict[str, PartDefinition] = {}
    for key in keys:
        prev = tree.part_revision(key)
        assert prev is not None
        part = tree.part(prev.part_id)
        p = props.get(str(prev.part_id))
        parts[key] = PartDefinition(
            key=key, name=part.name if part else "?", number=part.number if part else None,
            material=None if is_unknown_material(prev.material) else prev.material,
            procurement=(p.procurement if p else "unknown"),  # type: ignore[arg-type]
            representation=_representation(prev, p),  # type: ignore[arg-type]
            shape=part_shape(db, prev) if with_shapes else None,
            meta={"part_id": str(prev.part_id), "rev_no": prev.rev_no, "step_sha": prev.step_sha, "glb_sha": prev.glb_sha,
                  "mesh_sha": prev.mesh_sha, "bbox": (prev.metrics or {}).get("bbox"), "designation": p.designation if p else "",
                  # W5 detail sheets: title-block revision, hole tables and named datums of this exact revision
                  "revision_label": f"R{prev.rev_no}", "holes": list(prev.holes or []), "features": list(prev.features or []),
                  "datums": [asdict(d) for d in part_datums(db, prev)]})
    occurrences = [Occurrence(path=leaf.path, names=leaf.names, part_key=leaf.part_key, world=leaf.world) for leaf in leaves]
    return AssemblyGeometry(key=str(rev.id), name=asm.name, number=asm.number, revision_label=f"A{rev.rev_no}", parts=parts,
                            occurrences=occurrences,
                            meta={"assembly_id": str(asm.id), "rev_no": rev.rev_no, "execution_policy": rev.execution_policy,
                                  "check_status": rev.check_status, "verification": rev.verification, "bom_sha256": rev.bom_sha256,
                                  "content_sha256": rev.content_sha256, "stats": rev.stats, "project_id": str(asm.project_id)})


# =====================================================================================================
# Datums (W3 switch + fallback)
# =====================================================================================================

_DATUMS: OrderedDict[str, list[Datum]] = OrderedDict()


def part_datums(db: Session, rev: ModelRevision) -> list[Datum]:
    """Semantic datums of a part revision. Uses W3's ``cad/datums.datums_for(spec, built)`` when that module
    exists (rebuilding the revision without export), else the op-list fallback of :mod:`app.cad.mates`."""
    from ..cad.mates import fallback_datums
    from . import revisions

    key = str(rev.id)
    if key in _DATUMS:
        return _DATUMS[key]
    out: list[Datum] | None = None
    spec = None
    if not rev.mesh_only:
        try:
            spec = revisions.spec_of(rev)
        except Exception:  # pragma: no cover - a stored op list always parses
            spec = None
    try:
        from ..cad import datums as w3  # type: ignore[attr-defined]
    except ImportError:
        w3 = None
    if w3 is not None and spec is not None and hasattr(w3, "datums_for"):
        try:
            built = revisions.build_spec(db, spec, export=False)
            if built.ok:  # template ports (port.<name>) come from the revision's template provenance
                tpl = (rev.checks or {}).get("template") if isinstance(rev.checks, dict) else None
                out = list(w3.datums_for(spec, built, template=tpl))
        except Exception as exc:  # fall back, never fail the assembly on a datum helper
            log.warning("W3 datums_for failed for %s: %s", key, exc)
    if out is None:
        try:
            out = fallback_datums(spec, bbox=(rev.metrics or {}).get("bbox"), detected_holes=rev.detected_holes) if spec is not None \
                else [Datum(name="origin", kind="frame", origin=(0.0, 0.0, 0.0))]
        except Exception as exc:  # pragma: no cover - an op type the fallback does not know
            log.warning("fallback datums failed for %s: %s", key, exc)
            out = [Datum(name="origin", kind="frame", origin=(0.0, 0.0, 0.0))]
    if (rev.checks or {}).get("design"):
        out += _design_part_interfaces(db, rev)
    _DATUMS[key] = out
    if len(_DATUMS) > 1024:
        _DATUMS.popitem(last=False)
    return out


def _design_part_interfaces(db: Session, rev: ModelRevision) -> list[Datum]:
    """A planned physical axis/plane is exposed only when it exists on the exported STEP.

    This avoids guessed operation IDs, and prevents an arbitrary declared interface from satisfying a
    physical mate. A frame datum is still available separately for intentional static positioning.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from ..cad.datums import interface_datums
    from .revisions import KERNEL_LOCK

    proposed = interface_datums((rev.checks["design"] or {}).get("interfaces") or [])
    if not proposed:
        return []
    physical: list[tuple[str, np.ndarray, np.ndarray]] = []
    shape = part_shape(db, rev)
    with KERNEL_LOCK:
        cast_face = getattr(TopoDS, "Face_s", None) or getattr(TopoDS, "Face")
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            face = cast_face(explorer.Current())
            surface = BRepAdaptor_Surface(face)
            if surface.GetType() in (GeomAbs_Cylinder, GeomAbs_Plane):
                axis = surface.Cylinder().Axis() if surface.GetType() == GeomAbs_Cylinder else surface.Plane().Axis()
                physical.append(("axis" if surface.GetType() == GeomAbs_Cylinder else "plane",
                    np.array(axis.Location().Coord()), np.array(axis.Direction().Coord())))
            explorer.Next()
    out = []
    for datum in proposed:
        direction = np.array(datum.direction, dtype=float)
        direction /= np.linalg.norm(direction)
        for kind, at, normal in physical:
            delta = np.array(datum.origin) - at
            distance = np.linalg.norm(np.cross(delta, normal)) if kind == "axis" else abs(float(delta @ normal))
            if datum.kind == kind and np.linalg.norm(np.cross(direction, normal)) <= 1e-7 and distance <= 1e-5:
                out.append(datum)
                break
    return out


#: Sub-assembly levels whose contents' datums an instance exposes (deeper contents only by their frame and interfaces).
DATUM_DEPTH = 3


def part_frame_datums(datums: Iterable[Datum]) -> dict[str, Datum]:
    """THE datums of a part instance in its own frame, for the build and the Turbo placement layout alike: the same frame
    datums every definition has (``origin``, axes ``X`` / ``Y`` / ``Z``, planes ``XY`` / ``YZ`` / ``ZX``) plus its
    built feature datums (:func:`part_datums`; a feature datum wins a name clash). A part with only end faces (an
    extruded profile) is still fully placeable through its frame."""
    from ..cad.datums import frame_datums

    return {**{d.name: d for d in frame_datums()}, **{d.name: d for d in datums}}


def assembly_datums(interfaces: list[dict[str, Any]], children: Iterable[tuple[Sequence[str], Iterable[Datum], am.Matrix4]]) -> dict[str, Datum]:
    """THE datums of a sub-assembly instance in its own frame — the build (:func:`definition_datums`) and the Turbo
    placement layout (``assembly_placement``) both take them from here, so a mate binds the same way in both: its frame
    datums (``origin``, axes ``X`` / ``Y`` / ``Z``, planes ``XY`` / ``YZ`` / ``ZX``), its interfaces (``if.<key>``) and,
    per child ``(names, datums, transform)``, each datum of the child as ``<name>/<datum>`` moved into this frame."""
    from ..cad.datums import frame_datums, interface_datums
    from ..cad.mates import transformed

    out = {d.name: d for d in frame_datums() + interface_datums(interfaces)}
    for heads, datums, t in children:
        for d in datums:
            for head in dict.fromkeys(heads):
                full = f"{head}/{d.name}"
                out[full] = transformed(d, t, full)
    return out


def definition_datums(tree: Tree, c: Comp, depth: int = 0) -> dict[str, Datum]:
    """Datums of one component's definition in the definition frame: a part's (:func:`part_datums`, ``origin``
    included); a sub-assembly's (:func:`assembly_datums`): the interfaces its spec gives it (stored on its revision's
    ``spec_ref.interfaces``) and each non-suppressed child's datums as ``<child instance id>/<datum>`` — also as
    ``<child spec key>/<datum>`` when the child came from a spec (the names a spec relation or an AI placement mate
    uses; generation reliability round 3, B)."""
    if c.kind == "part":
        rev = tree.part_revision(str(c.model_revision_id))
        return part_frame_datums(part_datums(tree.db, rev)) if rev else {}
    crev = tree.revision(str(c.child_revision_id)) if c.child_revision_id else None
    kids = [] if depth > DATUM_DEPTH else [(tuple(x for x in (child.instance_id, child.meta.get("spec_key")) if x),
                                            definition_datums(tree, child, depth + 1).values(), child.transform)
                                           for child in tree.children(str(c.child_revision_id)) if not child.suppressed]
    return assembly_datums((crev.spec_ref or {}).get("interfaces") or [] if crev else [], kids)


# =====================================================================================================
# Validation, solving, saving
# =====================================================================================================


def _descendant_assemblies(tree: Tree, rid: str, depth: int = 0) -> set[str]:
    rev = tree.revision(rid)
    out = {str(rev.assembly_id)} if rev else set()
    if depth > MAX_DEPTH + 2:
        return out
    for c in tree.children(rid):
        if c.kind == "assembly":
            out |= _descendant_assemblies(tree, str(c.child_revision_id), depth + 1)
    return out


def _validate_definitions(db: Session, asm: Assembly, comps: list[Comp], tree: Tree) -> None:
    errors: list[dict[str, Any]] = []
    for c in comps:
        if c.kind == "part":
            rev = tree.part_revision(str(c.model_revision_id)) if c.model_revision_id else None
            part = tree.part(rev.part_id) if rev else None
            if rev is None or part is None or part.project_id != asm.project_id:
                errors.append({"instance": c.instance_id, "code": "definition_not_found", "message": "part revision not found in this project"})
                continue
            if rev.status != "ready":
                errors.append({"instance": c.instance_id, "code": "definition_not_built", "message": f"part revision R{rev.rev_no} did not build"})
            if rev.mesh_only or not rev.step_sha:
                errors.append({"instance": c.instance_id, "code": "definition_without_step",
                               "message": f"part revision R{rev.rev_no} is a mesh reference without STEP; it cannot be an assembly definition"})
            c.part_id = rev.part_id
        else:
            crev = tree.revision(str(c.child_revision_id)) if c.child_revision_id else None
            child = tree.assembly(crev.assembly_id) if crev else None
            if crev is None or child is None or child.project_id != asm.project_id:
                errors.append({"instance": c.instance_id, "code": "definition_not_found", "message": "assembly revision not found in this project"})
                continue
            c.child_assembly_id = child.id
            if crev.status != "ready":
                errors.append({"instance": c.instance_id, "code": "definition_not_built", "message": "the sub-assembly revision is not ready"})
            if child.id == asm.id or str(asm.id) in _descendant_assemblies(tree, str(crev.id)):
                raise AppError(422, "assembly_cycle", f"instance {c.instance_id} would make assembly {asm.number} contain itself",
                               {"instance": c.instance_id, "child_assembly_id": str(child.id)})
        if not am.is_rigid(c.transform, 1e-6):
            errors.append({"instance": c.instance_id, "code": "non_rigid_transform",
                           "message": "the transform is not rigid (a mirrored / scaled copy needs its own definition)"})
    if errors:
        raise AppError(422, "invalid_components", "some components are invalid", {"errors": errors})
    ids = [c.instance_id for c in comps]
    if len(set(ids)) != len(ids):
        raise AppError(422, "duplicate_instance_id", "instance ids repeat", {"instance_ids": sorted({i for i in ids if ids.count(i) > 1})})


def _solve(tree: Tree, comps: list[Comp], mates: list[MateRec], policy: str) -> dict[str, Any]:
    """Solve the mates of one revision's direct children; updates the comps' transforms in place."""
    from ..cad import mates as M

    if not mates and not any(c.placement.get("mode") == "unplaced" for c in comps):
        insts = {c.instance_id: {"status": "suppressed" if c.suppressed else ("fixed" if c.fixed else "floating"),
                                 "dof": None if c.suppressed else (0 if c.fixed else 6), "free": [],
                                 "grounded": c.fixed and not c.suppressed} for c in comps}
        return {"mates": {}, "instances": insts, "converged": True, "max_residual": {"position_mm": 0.0, "angle_rad": 0.0},
                "notes": [], "assumptions": []}
    datums: dict[str, dict[str, Datum] | None] = {}
    bboxes: dict[str, Any] = {}
    for c in comps:
        try:
            datums[c.instance_id] = definition_datums(tree, c)
        except AppError:
            datums[c.instance_id] = None
        if c.kind == "part":
            rev = tree.part_revision(str(c.model_revision_id))
            bboxes[c.instance_id] = _bbox_of(rev) if rev else None
        elif c.child_revision_id:  # a sub-assembly by what is placed inside it (its room in the display area)
            bboxes[c.instance_id] = _assembly_bbox(tree, str(c.child_revision_id))
    insts = [M.SolveInstance(iid=c.instance_id, transform=c.transform, fixed=c.fixed, datums=datums.get(c.instance_id), suppressed=c.suppressed,
                             unplaced=c.placement.get("mode") == "unplaced", bbox=bboxes.get(c.instance_id), name=c.name) for c in comps]
    by_id = {c.instance_id: c for c in comps}
    smates = [M.SolveMate(mate_id=m.mate_id, kind=m.kind, a=m.a, b=m.b, params=m.params, priority=m.priority, position=i, suppressed=m.suppressed)
              for i, m in enumerate(mates)]
    cache_keys = {c.instance_id: str(c.meta.get("spec_key") or c.instance_id) for c in comps}
    cache_keys.update({m.mate_id: str(m.meta.get("spec_key") or m.mate_id) for m in mates})
    result = solve_level(insts, smates, turbo=pol.is_turbo(policy), cache_keys=cache_keys)
    for iid, t in result.transforms.items():
        c = by_id.get(iid)
        if c is None:
            continue
        status = result.instances.get(iid, {}).get("status")
        if am.to_list(t) != am.to_list(c.transform):
            c.transform = t
            if c.placement.get("mode") == "unplaced" and status == "auto_placed":
                # a display position by rule (Turbo), never an installed one (generation supplement B-02)
                c.placement = {**c.placement, "mode": "explicit", "source": "turbo_layout", "arranged": True}
            elif not c.fixed:
                c.placement = {**c.placement, "mode": "mated", "source": "solver"}
        elif c.placement.get("mode") == "unplaced" and status in ("constrained", "under_constrained"):
            c.placement = {**c.placement, "mode": "mated", "source": "solver"}  # the mates put it where it already was
    for a in result.assumptions:  # the rule layout and the held freedoms name the spec objects they affect
        c = by_id.get(str(a.get("subject"))) if a.get("field") == "dof" or (a.get("author") == "rule" and a.get("field") == "placement") else None
        if c is not None:
            a["affected"] = list(dict.fromkeys([*(a.get("affected") or []), *(str(x) for x in (c.meta.get("spec_key"),
                                                                                           c.meta.get("definition_spec_key")) if x)]))
    for m in mates:
        res = result.mates.get(m.mate_id, {"status": "pending"})
        m.status = res.get("status", "pending")
        m.result = {k: v for k, v in res.items() if k != "status"}
        # record which exact definition the datum names were resolved on (never re-bound to other features)
        for side in ("a", "b"):
            ref = getattr(m, side)
            if ref and not is_frame_ref(ref) and ref.get("instance") in by_id:
                c = by_id[ref["instance"]]
                bound = ref.get("definition_revision_id")
                current = str(c.def_id)
                if bound and bound != current:
                    m.result.setdefault("rebound", []).append({"side": side, "from": bound, "to": current,
                                                               "by": "datum name" if m.status not in ("broken", "invalid") else None})
                if m.status not in ("broken", "invalid"):
                    ref["definition_revision_id"] = current
    out = result.to_json()
    out["assumptions"] = result.assumptions
    return out


def solve_level(instances: list[Any], mates: list[Any], *, turbo: bool, cache_keys: dict[str, str] | None = None) -> Any:
    """THE solve of one assembly level — the build (:func:`_solve`, per stored revision) and the Turbo placement layout
    (``assembly_placement``, before anything is stored) both solve through here, with the datums of
    :func:`part_datums` / :func:`assembly_datums`: ``instances`` (:class:`app.cad.mates.SolveInstance`, the level's
    children) and ``mates`` (:class:`app.cad.mates.SolveMate`). Generation reliability round 3 (B): an assembly frame a
    mate names (``@<assembly key>``) takes part as a fixed instance at the identity with the datums its mates carry
    (``frame``, already in this level's frame); it is left out of the reported instances. Each instance reports
    ``grounded`` — positioned in this level's frame (:func:`app.cad.mates.grounded`)."""
    from ..cad import mates as M

    frames: dict[str, dict[str, Datum]] = {}
    for m in mates:
        for ref in (m.a, m.b):
            if is_frame_ref(ref):
                for n, d in (ref.get("frame") or {}).items():
                    got = _datum_in(n, d)
                    if got is not None:
                        frames.setdefault(str(ref["instance"]), {})[n] = got
    result = M.solve(list(instances) + [M.SolveInstance(iid=k, transform=am.IDENTITY, fixed=True, datums=ds, name=k) for k, ds in frames.items()],
                     mates, turbo=turbo, cache_keys=cache_keys)
    result.instances = {k: v for k, v in result.instances.items() if k not in frames}  # an assembly frame is no component
    return result


def held_fixed(keys: list[str], explicit: set[str], rels: list[dict[str, Any]], anchors: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Which children of one level are fixed in its solve (the build and the placement layout alike): the ones with an
    explicit pose (stated, at the origin by rule, a stated AI position) that no relation moves (:func:`_moving`), or that
    anchor a relation group the AI positioned (``anchors``); when none is, the first child if its pose is explicit."""
    moving = _moving(rels)
    out = {k for k in keys if k in explicit and (k not in moving or k in anchors)}
    if not out and keys and keys[0] in explicit:
        out.add(keys[0])
    return out


def _next_ids(asm: Assembly, n: int, kind: str = "instance") -> list[str]:
    if kind == "instance":
        start = asm.instance_counter
        asm.instance_counter = start + n
        return [f"I-{start + i + 1:04d}" for i in range(n)]
    start = asm.mate_counter
    asm.mate_counter = start + n
    return [f"M-{start + i + 1:04d}" for i in range(n)]


def _bump_counters(asm: Assembly, comps: list[Comp], mates: list[MateRec]) -> None:
    for c in comps:
        if INSTANCE_RE.match(c.instance_id):
            asm.instance_counter = max(asm.instance_counter, int(c.instance_id[2:]))
    for m in mates:
        if MATE_RE.match(m.mate_id):
            asm.mate_counter = max(asm.mate_counter, int(m.mate_id[2:]))


def _lock_assembly(db: Session, assembly_id: uuid.UUID) -> Assembly:
    asm = db.scalar(select(Assembly).where(Assembly.id == assembly_id).with_for_update().execution_options(populate_existing=True))
    if asm is None:
        raise not_found("assembly")
    return asm


def _quick_checks(db: Session, rev_policy: str, tree: Tree, solve: dict[str, Any], bom: dict[str, Any], spec: dict[str, Any] | None,
                  comps: list[Comp]) -> dict[str, dict[str, Any]]:
    """The checks that need no B-rep work (run at every save): mate residual, BOM consistency, spec."""
    from ..cad import assembly_checks as chk

    leaf_paths = [leaf.path for leaf in tree.leaves()]
    out: dict[str, dict[str, Any]] = {
        "assembly.mate_residual": pol.run_check("assembly.mate_residual", rev_policy, lambda: chk.mate_check(solve)),
        "assembly.bom": pol.run_check("assembly.bom", rev_policy, lambda: chk.bom_check(bom, leaf_paths)),
    }
    if spec is not None:
        out["assembly.spec"] = pol.run_check("assembly.spec", rev_policy, lambda: chk.spec_check(spec, _spec_actual(tree, comps)))
    root = tree.revision(tree.root)
    if root is not None and (root.spec_ref or {}).get("design"):
        out["assembly.design_contract"] = pol.run_check("assembly.design_contract", rev_policy,
                                                        lambda: _design_contract_check(db, tree))
    return out


def _design_contract_check(db: Session, tree: Tree) -> dict[str, Any]:
    """Check every required occurrence and mate, including repeated saved child revisions.

    The immutable manifest is installed only by the exact-plan compiler entry. Hand edits retain it, so
    deleting or suppressing a mate cannot turn a previously required connection into a complete result.
    """
    root = tree.revision(tree.root)
    manifest = (root.spec_ref or {}).get("design") or {}
    expected_instances = manifest.get("instances") or []
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for instance in expected_instances:
        by_parent.setdefault(instance["parent"], []).append(instance)
    instance_owner = {i["key"]: i["parent"] for i in expected_instances}
    mapped = {r["key"]: r for r in manifest.get("mapped_relations") or []}
    required = manifest.get("relations") or []
    failures: list[dict[str, Any]] = []
    count, passed = 0, 0

    def fail(key: str, path: str, code: str, reason: str) -> None:
        failures.append({"key": key, "path": path, "code": code, "reason": reason})

    def walk(revision: AssemblyRevision | None, owner: str, path: str, depth: int) -> None:
        nonlocal count, passed
        if depth > MAX_DEPTH:
            fail(owner, path, "design_depth", "saved hierarchy exceeds the design depth bound")
            return
        components, mates = revision_state(db, revision) if revision else ([], [])
        actual = {str(c.meta.get("spec_key")): c for c in components}
        names = {c.instance_id: str(c.meta.get("spec_key")) for c in components}
        actual_mates = {str(m.meta.get("spec_key")): m for m in mates}
        if revision and ((revision.spec_ref or {}).get("definition") != owner
                         or ((revision.spec_ref or {}).get("design") or {}).get("ref") != manifest.get("ref")):
            fail(owner, path, "design_child_mismatch", "saved child revision belongs to another design definition")
        expected_keys = {i["key"] for i in by_parent.get(owner, [])}
        for extra in set(actual) - expected_keys:
            fail(extra, path, "design_instance_extra", "saved assembly contains an unplanned instance")
        for item in by_parent.get(owner, []):
            location = f"{path}/{item['key']}" if path else item["key"]
            component = actual.get(item["key"])
            if (component is None or component.suppressed
                    or component.meta.get("definition_spec_key") != item["definition"]):
                fail(item["key"], location, "design_instance_missing", "required design instance is absent, suppressed or rebound")
            elif not np.allclose(np.array(component.transform), np.array(_placement_matrix(item["placement"])), rtol=0, atol=1e-6):
                fail(item["key"], location, "design_pose_changed", "saved static pose differs from the authorized design")
            if item["definition"] in by_parent:
                child = tree.revision(str(component.child_revision_id)) if component and component.kind == "assembly" and not component.suppressed else None
                walk(child, item["definition"], location, depth + 1)
            elif component and component.kind == "part":
                part = tree.part_revision(str(component.model_revision_id))
                provenance = (part.checks or {}).get("design") if part else None
                if (not provenance or provenance.get("design_ref") != manifest.get("ref")
                        or provenance.get("definition_key") != item["definition"]
                        or provenance.get("requirements_sha256") != manifest["requirements_sha256"].get(item["definition"])):
                    fail(item["key"], location, "design_part_mismatch", "part geometry does not carry this exact design contract")
        for relation in required:
            endpoint = relation["a"]["instance"]
            parent = endpoint[1:] if endpoint.startswith("@") else instance_owner.get(endpoint)
            if parent != owner:
                continue
            count += 1
            key = relation["key"]
            expected, actual_mate = mapped.get(key), actual_mates.get(key)

            def side(value: dict[str, Any] | None) -> dict[str, Any] | None:
                if not value:
                    return None
                instance = value.get("instance")
                return {"instance": instance if str(instance).startswith("@") else names.get(instance), "datum": value.get("datum")}

            matches = bool(expected and actual_mate and not actual_mate.suppressed
                and actual_mate.kind == relation["kind"] == expected["kind"]
                and actual_mate.params == (expected.get("params") or {})
                and all(side(getattr(actual_mate, s)) == {k: expected[s].get(k) for k in ("instance", "datum")} for s in ("a", "b")))
            if not matches:
                fail(key, path, "design_relation_missing", "required mate is missing, suppressed or has changed endpoints/parameters")
            elif actual_mate.status not in ("solved", "redundant"):
                fail(key, path, "design_relation_unsatisfied", f"required mate is {actual_mate.status}")
            else:
                passed += 1

    walk(root, (root.spec_ref or {}).get("definition"), "", 0)
    return {"status": "failed" if failures else "passed", "required": count, "satisfied": passed,
            "problems": failures, "design_ref": manifest.get("ref"), "static_only": True}


def _spec_actual(tree: Tree, comps: list[Comp]) -> dict[str, Any]:
    instances: list[dict[str, Any]] = []
    definitions: dict[str, str] = {}

    def walk(rid: str, parent_key: str | None, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for c in tree.children(rid):
            if c.suppressed:
                continue
            dkey = c.meta.get("definition_spec_key")
            if dkey:
                definitions[dkey] = str(c.def_id)
            instances.append({"instance_id": c.instance_id, "spec_key": c.meta.get("spec_key"), "definition_spec_key": dkey,
                              "parent_spec_key": parent_key})
            if c.kind == "assembly":
                crev = tree.revision(str(c.child_revision_id))
                walk(str(c.child_revision_id), (crev.spec_ref or {}).get("definition") if crev else None, depth + 1)

    root = tree.revision(tree.root)
    walk(tree.root, (root.spec_ref or {}).get("definition") if root and root.spec_ref else None, 0)
    return {"instances": instances, "definitions": definitions}


def _record(check: dict[str, Any], *, user: User | None, policy: str) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {**check, "policy": policy, "at": datetime.now(UTC).isoformat(), "by": str(user.id) if user else None}


def _merge_checks(existing: dict[str, Any], new: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out = {k: {"latest": v.get("latest"), "history": list(v.get("history") or [])} for k, v in (existing or {}).items()}
    for name, rec in new.items():
        slot = out.setdefault(name, {"latest": None, "history": []})
        slot["history"].append(rec)
        slot["latest"] = rec
    return out


def _check_status(checks: dict[str, Any], required: list[str]) -> str:
    from ..cad.assembly_checks import aggregate

    return aggregate({k: (v or {}).get("latest") for k, v in checks.items()}, required)


def _required(rev: AssemblyRevision, checks: dict[str, Any]) -> list[str]:
    req = list(REQUIRED_CHECKS)
    for opt in ("assembly.spec", "assembly.clearance", "assembly.design_contract", "assembly.placement_geometry"):
        if opt in checks:
            req.append(opt)
    return req


def _content_hash(asm: Assembly, comps: list[Comp], mates: list[MateRec]) -> str:
    return _canon({"assembly": str(asm.id), "components": [c.content() for c in comps], "mates": [m.content() for m in mates]})


def _save_revision(db: Session, asm: Assembly, *, user: User, comps: list[Comp], mates: list[MateRec], source: str, policy: str | None,
                   parent: AssemblyRevision | None = None, spec: dict[str, Any] | None = None, spec_ref: dict[str, Any] | None = None,
                   note: str = "", notes: list[dict[str, Any]] | None = None, assumptions: list[dict[str, Any]] | None = None,
                   change: dict[str, Any] | None = None, node_run_id: uuid.UUID | None = None,
                   source_file_version_id: uuid.UUID | None = None, run_heavy_checks: bool = False,
                   clearance_mm: float | None = None, solved: dict[str, Any] | None = None) -> AssemblyRevision:
    """Validate, solve (``solved``: the result of :func:`_solve` on exactly these components and mates, already
    applied to them) and store one assembly revision with its BOM and quick checks."""
    try:
        policy = pol.parse_policy(policy)
    except ValueError as exc:
        raise bad_request("invalid_execution_policy", str(exc))
    if spec is None and spec_ref and spec_ref.get("structure"):
        spec = spec_ref["structure"]
    tree = Tree(db, Tree.ROOT, override=comps)
    tree.target_items = target_items(spec_ref)  # type: ignore[attr-defined]  # (the stored root is not there yet)
    _validate_definitions(db, asm, comps, tree)
    if not comps:
        raise AppError(422, "empty_assembly", "an assembly revision needs at least one component")
    _bump_counters(asm, comps, mates)
    mate_ids = [m.mate_id for m in mates]
    if len(set(mate_ids)) != len(mate_ids):
        raise AppError(422, "duplicate_mate_id", "mate ids repeat")
    solve = solved if solved is not None else _solve(tree, comps, mates, policy)
    stats = tree.stats()  # budget: 422 assembly_too_large before anything is stored
    bom = compute_bom(db, tree)
    all_notes = list(notes or []) + list(solve.get("notes") or [])
    all_assumptions = [dict(a) for a in (assumptions or [])] + list(solve.pop("assumptions", []) or [])
    # Rule / solver assumptions get their own id space (R-nn, next free): positional A-nn ids could repeat the ids of
    # the spec's copies kept here. Stored revisions keep their ids; decisions are keyed by owner and id.
    taken = {str(a["id"]) for a in all_assumptions if a.get("id")}
    n = 0
    for a in all_assumptions:
        if not a.get("id"):
            n += 1
            while f"R-{n:02d}" in taken:
                n += 1
            a["id"] = f"R-{n:02d}"
            taken.add(a["id"])
        pol.Assumption.model_validate({k: v for k, v in a.items() if k in pol.Assumption.model_fields})
    rev_no = (db.scalar(select(func.max(AssemblyRevision.rev_no)).where(AssemblyRevision.assembly_id == asm.id)) or 0) + 1
    rev = AssemblyRevision(
        assembly_id=asm.id, rev_no=rev_no, parent_id=parent.id if parent else None, source=source, execution_policy=policy,
        status="ready", check_status="pending", acceptance_status="not_requested",
        content_sha256=_content_hash(asm, comps, mates), spec_version_id=uuid.UUID(str(spec_ref["spec_version_id"]))
        if spec_ref and spec_ref.get("spec_version_id") else None, spec_ref=spec_ref, stats=stats, solve=solve, checks={},
        verification={}, assumptions=all_assumptions, bom=bom, bom_sha256=bom_hash(bom), notes=all_notes, change=change or {},
        note=note, node_run_id=node_run_id, source_file_version_id=source_file_version_id, created_by=user.id)
    db.add(rev)
    db.flush()
    for i, c in enumerate(comps):
        db.add(AssemblyComponent(revision_id=rev.id, instance_id=c.instance_id, name=c.name[:200], position=i, definition_kind=c.kind,
                                 part_id=c.part_id if c.kind == "part" else None,
                                 model_revision_id=c.model_revision_id if c.kind == "part" else None,
                                 child_assembly_id=c.child_assembly_id if c.kind == "assembly" else None,
                                 child_revision_id=c.child_revision_id if c.kind == "assembly" else None,
                                 transform=am.to_list(c.transform), fixed=c.fixed, suppressed=c.suppressed, placement=c.placement,
                                 display=c.display, source_ref=(c.source_ref or None) and c.source_ref[:200], meta=c.meta))
    for i, m in enumerate(mates):
        db.add(AssemblyMate(revision_id=rev.id, mate_id=m.mate_id, kind=m.kind, a=m.a, b=m.b, params=m.params, priority=m.priority,
                            position=i, suppressed=m.suppressed, source=m.source, status=m.status, result=m.result, meta=m.meta))
    db.flush()
    # checks bound to this exact revision (tree re-read from the stored rows)
    stored = Tree(db, rev.id)
    records = {k: _record(v, user=user, policy=policy) for k, v in _quick_checks(db, policy, stored, solve, bom, spec, comps).items()}
    if run_heavy_checks:
        records.update({k: _record(v, user=user, policy=policy)
                        for k, v in _heavy_checks(db, rev, policy, clearance_mm=clearance_mm).items() if v is not None})
    rev.checks = _merge_checks({}, records)
    rev.check_status = "skipped_turbo" if pol.is_turbo(policy) else _check_status(rev.checks, _required(rev, rev.checks))
    # S5: checks, assumption decisions (none yet) and the part / child revisions it is made of (W0 §2.2).
    from .verification import assembly_verification

    rev.verification = assembly_verification(db, rev)
    db.flush()
    assets.register_assembly_revision(db, rev)
    audit(db, action=f"assembly_revision.{source}", subject_type="assembly_revision", subject_id=rev.id, user_id=user.id,
          project_id=asm.project_id, data={"rev_no": rev_no, "content_sha256": rev.content_sha256, "policy": policy})
    return rev


# =====================================================================================================
# Input parsing (API / W8)
# =====================================================================================================


def _transform_in(data: dict[str, Any]) -> tuple[am.Matrix4 | None, bool]:
    """(transform, given) from ``transform`` (4x4) or ``placement`` ({translate, rotate})."""
    if data.get("transform") is not None:
        try:
            return am.from_list(data["transform"]), True
        except (ValueError, TypeError) as exc:
            raise bad_request("invalid_transform", f"transform must be a 4x4 matrix: {exc}")
    pl = data.get("placement")
    if pl:
        try:
            return am.placement(tuple(pl.get("translate") or (0, 0, 0)), tuple(pl.get("rotate") or (0, 0, 0))), True  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise bad_request("invalid_placement", f"placement needs translate [x,y,z] and rotate [rx,ry,rz] (degrees): {exc}")
    return None, False


def _placement_matrix(pl: Any) -> am.Matrix4 | None:
    """The transform of a ``{translate, rotate}`` placement (None when it is not one)."""
    if not isinstance(pl, dict):
        return None
    try:
        t = tuple(float(x) for x in pl.get("translate") or (0.0, 0.0, 0.0))
        r = tuple(float(x) for x in pl.get("rotate") or (0.0, 0.0, 0.0))
    except (TypeError, ValueError):
        return None
    return am.placement(t, r) if len(t) == 3 and len(r) == 3 else None  # type: ignore[arg-type]


def _definition_in(db: Session, asm: Assembly | None, d: dict[str, Any]) -> dict[str, Any]:
    """Exact revision, or latest definition (parts require a ready entity revision with STEP)."""
    kind = d.get("kind")
    if kind not in ("part", "assembly"):
        raise bad_request("invalid_definition", "definition.kind must be part or assembly")
    if kind == "part":
        if d.get("revision_id"):
            return {"kind": "part", "model_revision_id": uuid.UUID(str(d["revision_id"]))}
        if d.get("part_id"):
            rev = _latest_part_definition(db, uuid.UUID(str(d["part_id"])))
            if rev is None:
                raise AppError(422, "definition_not_built", "the part has no ready entity revision with STEP", {"part_id": str(d["part_id"])})
            return {"kind": "part", "model_revision_id": rev.id}
    else:
        if d.get("revision_id"):
            return {"kind": "assembly", "child_revision_id": uuid.UUID(str(d["revision_id"]))}
        if d.get("assembly_id"):
            rev = latest_revision(db, uuid.UUID(str(d["assembly_id"])))
            if rev is None:
                raise AppError(422, "definition_not_built", "the assembly has no revision", {"assembly_id": str(d["assembly_id"])})
            return {"kind": "assembly", "child_revision_id": rev.id}
    raise bad_request("invalid_definition", "definition needs revision_id (or part_id / assembly_id for the latest revision)")


def _comp_in(db: Session, asm: Assembly, data: dict[str, Any], iid: str) -> Comp:
    d = _definition_in(db, asm, data.get("definition") or {})
    t, given = _transform_in(data)
    placement = {"mode": "explicit" if given else "unplaced", "source": data.get("source") or "user"}
    return Comp(instance_id=iid, name=str(data.get("name") or ""), kind=d["kind"], transform=t or am.IDENTITY,
                model_revision_id=d.get("model_revision_id"), child_revision_id=d.get("child_revision_id"),
                fixed=bool(data.get("fixed", False)), suppressed=bool(data.get("suppressed", False)), placement=placement,
                display=dict(data.get("display") or {}), source_ref=data.get("source_ref"), meta=dict(data.get("meta") or {}))


def _mate_in(data: dict[str, Any], mid: str, comps: dict[str, Comp]) -> MateRec:
    from ..cad.mates import MATE_KINDS

    kind = data.get("kind")
    if kind not in MATE_KINDS:
        raise bad_request("invalid_mate", f"mate kind must be one of {', '.join(MATE_KINDS)}")

    def side(ref: Any) -> dict[str, Any] | None:
        if ref is None:
            return None
        if not isinstance(ref, dict) or not ref.get("instance"):
            raise bad_request("invalid_mate", "a mate side is {instance, datum}")
        out = {"instance": str(ref["instance"]), "datum": str(ref.get("datum") or "origin")}
        if is_frame_ref(out):  # the assembly's own frame: its datums travel with the mate (generation reliability B)
            frame = ref.get("frame")
            if not isinstance(frame, dict) or not frame or not all(_datum_in(n, d) is not None for n, d in frame.items()):
                raise bad_request("invalid_mate", f"a mate on the frame {out['instance']} carries its datums as frame: {{name: "
                                                  "{kind, origin, direction}}")
            return {**out, "frame": {n: _datum_json(_datum_in(n, d)) for n, d in frame.items()}}  # type: ignore[arg-type]
        c = comps.get(out["instance"])
        if c is None:
            raise AppError(422, "invalid_mate", f"mate refers to unknown instance {out['instance']}", {"instance": out["instance"]})
        out["definition_revision_id"] = str(c.def_id)
        return out

    a = side(data.get("a"))
    b = side(data.get("b"))
    if a is None:
        raise bad_request("invalid_mate", "a mate needs side a")
    return MateRec(mate_id=mid, kind=kind, a=a, b=b, params=dict(data.get("params") or {}), priority=int(data.get("priority", 100)),
                   suppressed=bool(data.get("suppressed", False)), source=str(data.get("source") or "user")[:20], meta=dict(data.get("meta") or {}))


def _default_names(tree: Tree, comps: list[Comp]) -> None:
    counts: dict[str, int] = {}
    for c in comps:
        if not c.name:
            base = tree.def_name(c) if (c.part_id or c.child_assembly_id) else "component"
            counts[base] = counts.get(base, 0) + 1
            c.name = f"{base} <{counts[base]}>"


def _resolve_kinds(db: Session, asm: Assembly, comps: list[Comp]) -> None:
    tree = Tree(db, Tree.ROOT, override=comps)
    for c in comps:
        if c.kind == "part" and c.model_revision_id:
            rev = tree.part_revision(str(c.model_revision_id))
            c.part_id = rev.part_id if rev else None
        elif c.kind == "assembly" and c.child_revision_id:
            crev = tree.revision(str(c.child_revision_id))
            c.child_assembly_id = crev.assembly_id if crev else None
    _default_names(tree, comps)


# =====================================================================================================
# Create / edit
# =====================================================================================================


def _new_assembly(db: Session, project_id: uuid.UUID, user: User, number: str, name: str, description: str = "") -> Assembly:
    number = (number or "").strip()[:80]
    if not number:
        raise bad_request("number_required", "an assembly number is required")
    if db.scalar(select(Assembly.id).where(Assembly.project_id == project_id, Assembly.number == number)) is not None:
        raise conflict("assembly_number_taken", f"assembly number {number} already exists in this project", number=number)
    asm = Assembly(project_id=project_id, number=number, name=(name or number)[:200], description=description, created_by=user.id)
    db.add(asm)
    db.flush()
    audit(db, action="assembly.create", subject_type="assembly", subject_id=asm.id, user_id=user.id, project_id=project_id)
    return asm


def create_assembly(db: Session, *, project_id: uuid.UUID, user: User, number: str, name: str, description: str = "",
                    components: list[dict[str, Any]], mates: list[dict[str, Any]] | None = None, policy: str | None = None,
                    source: str = "manual", spec: dict[str, Any] | None = None, spec_ref: dict[str, Any] | None = None, note: str = "",
                    node_run_id: uuid.UUID | None = None, run_heavy_checks: bool = False,
                    clearance_mm: float | None = None) -> tuple[Assembly, AssemblyRevision]:
    """New assembly definition with revision 1. ``components``: [{name?, definition: {kind, revision_id |
    part_id | assembly_id}, transform? | placement?, fixed?, suppressed?, display?, meta?}]; ``mates``: [{kind,
    a: {instance, datum}, b?, params?, priority?}] where ``instance`` is the component's ``instance_id`` (given)
    or its 0-based index as ``"#<n>"``. The first explicitly placed component is fixed (ground) when none is."""
    asm = _new_assembly(db, project_id, user, number, name, description)
    comps = _components_from(db, asm, components)
    mate_recs = _mates_from(asm, mates or [], comps)
    rev = _save_revision(db, asm, user=user, comps=comps, mates=mate_recs, source=source, policy=policy, spec=spec, spec_ref=spec_ref,
                         note=note, node_run_id=node_run_id, run_heavy_checks=run_heavy_checks, clearance_mm=clearance_mm)
    return asm, rev


def _components_from(db: Session, asm: Assembly, components: list[dict[str, Any]]) -> list[Comp]:
    given = [str(c["instance_id"]) for c in components if c.get("instance_id")]
    for g in given:
        if not INSTANCE_RE.match(g):
            raise bad_request("invalid_instance_id", f"instance ids look like I-0001 (got {g})")
        # automatic ids continue after every given one (never a duplicate of an id given in the same request)
        asm.instance_counter = max(asm.instance_counter, int(g[2:]))
    fresh = iter(_next_ids(asm, sum(1 for c in components if not c.get("instance_id"))))
    comps = [_comp_in(db, asm, data, str(data.get("instance_id") or next(fresh))) for data in components]
    _resolve_kinds(db, asm, comps)
    if comps and not any(c.fixed for c in comps):
        first = next((c for c in comps if c.placement.get("mode") == "explicit" and not c.suppressed), None)
        if first is not None:
            first.fixed = True  # like a CAD system: the first placed component is the ground
    return comps


def _mates_from(asm: Assembly, mates: list[dict[str, Any]], comps: list[Comp]) -> list[MateRec]:
    by_id = {c.instance_id: c for c in comps}
    for m in mates:
        for side in ("a", "b"):
            ref = m.get(side)
            if isinstance(ref, dict) and isinstance(ref.get("instance"), str) and ref["instance"].startswith("#"):
                idx = int(ref["instance"][1:])
                if not 0 <= idx < len(comps):
                    raise bad_request("invalid_mate", f"mate refers to component index {idx}")
                ref["instance"] = comps[idx].instance_id
    for m in mates:
        if m.get("mate_id") and MATE_RE.match(str(m["mate_id"])):
            asm.mate_counter = max(asm.mate_counter, int(str(m["mate_id"])[2:]))
    fresh = iter(_next_ids(asm, sum(1 for m in mates if not m.get("mate_id")), "mate"))
    return [_mate_in(m, str(m.get("mate_id") or next(fresh)), by_id) for m in mates]


def revision_state(db: Session, rev: AssemblyRevision) -> tuple[list[Comp], list[MateRec]]:
    comps = [_comp_from_row(r) for r in db.scalars(select(AssemblyComponent).where(AssemblyComponent.revision_id == rev.id)
                                                    .order_by(AssemblyComponent.position)).all()]
    mates = [_mate_from_row(r) for r in db.scalars(select(AssemblyMate).where(AssemblyMate.revision_id == rev.id)
                                                   .order_by(AssemblyMate.position)).all()]
    return comps, mates


EDIT_OPS = ("add_instance", "remove_instance", "suppress_instance", "replace_definition", "update_definition", "move_instance",
            "rename_instance", "set_fixed", "set_display", "add_mate", "edit_mate", "remove_mate", "pattern_linear", "pattern_circular",
            "set_quantity")


def apply_ops(db: Session, asm: Assembly, comps: list[Comp], mates: list[MateRec], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply edit operations in order (mutates the lists). Returns the change summary (ops, affected
    instance ids, notes). Unknown instances / mates are 422 with the op index."""
    affected: list[str] = []
    notes: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []

    def find(iid: Any, i: int) -> Comp:
        c = next((c for c in comps if c.instance_id == iid), None)
        if c is None:
            raise AppError(422, "unknown_instance", f"op {i}: instance {iid} is not in the base revision", {"op": i, "instance": iid})
        return c

    def find_mate(mid: Any, i: int) -> MateRec:
        m = next((m for m in mates if m.mate_id == mid), None)
        if m is None:
            raise AppError(422, "unknown_mate", f"op {i}: mate {mid} is not in the base revision", {"op": i, "mate_id": mid})
        return m

    def touch(iid: str) -> None:
        if iid not in affected:
            affected.append(iid)

    def copy_of(src: Comp, t: am.Matrix4, iid: str, suffix: str) -> Comp:
        return Comp(instance_id=iid, name=f"{src.name.split(' <')[0]} <{suffix}>", kind=src.kind, transform=t, part_id=src.part_id,
                    model_revision_id=src.model_revision_id, child_assembly_id=src.child_assembly_id, child_revision_id=src.child_revision_id,
                    fixed=src.fixed, suppressed=False, placement={"mode": "explicit", "source": "pattern", "of": src.instance_id},
                    display=dict(src.display), meta={k: v for k, v in src.meta.items() if k not in ("spec_key",)})

    for i, op in enumerate(ops):
        kind = op.get("op")
        if kind not in EDIT_OPS:
            raise AppError(422, "invalid_op", f"op {i}: unknown edit op {kind!r}", {"op": i, "allowed": list(EDIT_OPS)})
        if kind == "add_instance":
            (iid,) = _next_ids(asm, 1)
            c = _comp_in(db, asm, op, iid)
            comps.append(c)
            _resolve_kinds(db, asm, [c])
            if c.placement.get("mode") == "unplaced" and not op.get("mates"):
                notes.append({"code": "unplaced_instance", "subject": iid, "message": f"{iid} has no placement; place or mate it"})
            touch(iid)
            applied.append({"op": kind, "instance": iid})
        elif kind == "remove_instance":
            c = find(op.get("instance"), i)
            comps.remove(c)
            gone = [m.mate_id for m in mates if c.instance_id in ((m.a or {}).get("instance"), (m.b or {}).get("instance"))]
            mates[:] = [m for m in mates if m.mate_id not in gone]
            if gone:
                notes.append({"code": "mates_removed", "subject": c.instance_id, "message": f"mates {', '.join(gone)} referenced the removed instance"})
            touch(c.instance_id)
            applied.append({"op": kind, "instance": c.instance_id, "mates_removed": gone})
        elif kind == "suppress_instance":
            c = find(op.get("instance"), i)
            c.suppressed = bool(op.get("suppressed", True))
            touch(c.instance_id)
            applied.append({"op": kind, "instance": c.instance_id, "suppressed": c.suppressed})
        elif kind in ("replace_definition",):
            c = find(op.get("instance"), i)
            d = _definition_in(db, asm, op.get("definition") or {})
            before = str(c.def_id)
            c.kind = d["kind"]
            c.model_revision_id, c.child_revision_id = d.get("model_revision_id"), d.get("child_revision_id")
            c.part_id = c.child_assembly_id = None
            _resolve_kinds(db, asm, [c])
            touch(c.instance_id)
            applied.append({"op": kind, "instance": c.instance_id, "from": before, "to": str(c.def_id)})
        elif kind == "update_definition":
            # every instance of one shared definition (a part or an assembly) moves to the given / latest revision
            target = op.get("part_id") or op.get("assembly_id")
            if op.get("part_id"):
                new = uuid.UUID(str(op["revision_id"])) if op.get("revision_id") else getattr(_latest_part_definition(db, uuid.UUID(str(target))), "id", None)
                hits = [c for c in comps if c.kind == "part" and str(c.part_id) == str(target)]
            elif op.get("assembly_id"):
                new = uuid.UUID(str(op["revision_id"])) if op.get("revision_id") else getattr(latest_revision(db, uuid.UUID(str(target))), "id", None)
                hits = [c for c in comps if c.kind == "assembly" and str(c.child_assembly_id) == str(target)]
            else:
                raise AppError(422, "invalid_op", f"op {i}: update_definition needs part_id or assembly_id", {"op": i})
            if new is None:
                raise AppError(422, "definition_not_built", f"op {i}: no revision to update to", {"op": i})
            if not hits:
                raise AppError(422, "definition_not_used", f"op {i}: the definition is not used by this revision", {"op": i})
            for c in hits:
                if c.kind == "part":
                    c.model_revision_id = new
                else:
                    c.child_revision_id = new
                touch(c.instance_id)
            _resolve_kinds(db, asm, hits)
            applied.append({"op": kind, "definition": str(target), "to": str(new), "instances": [c.instance_id for c in hits]})
        elif kind == "move_instance":
            c = find(op.get("instance"), i)
            t, given = _transform_in(op)
            if op.get("delta"):
                dt = am.placement(tuple(op["delta"].get("translate") or (0, 0, 0)), tuple(op["delta"].get("rotate") or (0, 0, 0)))  # type: ignore[arg-type]
                t, given = am.compose(dt, c.transform), True
            if not given:
                raise AppError(422, "invalid_op", f"op {i}: move_instance needs transform, placement or delta", {"op": i})
            c.transform = t  # type: ignore[assignment]
            c.placement = {"mode": "explicit", "source": "user"}
            if "fixed" in op:
                c.fixed = bool(op["fixed"])
            touch(c.instance_id)
            applied.append({"op": kind, "instance": c.instance_id})
        elif kind == "rename_instance":
            c = find(op.get("instance"), i)
            c.name = str(op.get("name") or c.name)[:200]
            applied.append({"op": kind, "instance": c.instance_id})
        elif kind == "set_fixed":
            c = find(op.get("instance"), i)
            c.fixed = bool(op.get("fixed", True))
            touch(c.instance_id)
            applied.append({"op": kind, "instance": c.instance_id, "fixed": c.fixed})
        elif kind == "set_display":
            c = find(op.get("instance"), i)
            c.display = {**c.display, **dict(op.get("display") or {})}
            applied.append({"op": kind, "instance": c.instance_id})
        elif kind == "add_mate":
            (mid,) = _next_ids(asm, 1, "mate")
            m = _mate_in(op, mid, {c.instance_id: c for c in comps})
            mates.append(m)
            for ref in (m.a, m.b):
                if ref:
                    touch(ref["instance"])
            applied.append({"op": kind, "mate_id": mid})
        elif kind == "edit_mate":
            m = find_mate(op.get("mate_id"), i)
            merged = {**m.content(), **{k: v for k, v in op.items() if k in ("kind", "a", "b", "params", "priority", "suppressed")}}
            new = _mate_in(merged, m.mate_id, {c.instance_id: c for c in comps})
            new.source, new.meta = m.source, m.meta
            mates[mates.index(m)] = new
            for ref in (new.a, new.b):
                if ref:
                    touch(ref["instance"])
            applied.append({"op": kind, "mate_id": m.mate_id})
        elif kind == "remove_mate":
            m = find_mate(op.get("mate_id"), i)
            mates.remove(m)
            applied.append({"op": kind, "mate_id": m.mate_id})
        elif kind == "pattern_linear":
            src = find(op.get("instance"), i)
            count = int(op.get("count", 2))
            step = op.get("step") or op.get("pitch")
            if count < 2 or count > 500 or not step or len(step) != 3:
                raise AppError(422, "invalid_op", f"op {i}: pattern_linear needs count 2..500 and step [dx, dy, dz]", {"op": i})
            new_ids = _next_ids(asm, count - 1)
            for k, iid in enumerate(new_ids, 1):
                t = am.compose(am.translation(*(float(s) * k for s in step)), src.transform)
                comps.append(copy_of(src, t, iid, f"{k + 1}"))
                touch(iid)
            applied.append({"op": kind, "instance": src.instance_id, "created": new_ids})
        elif kind == "pattern_circular":
            from ..cad.mates import rodrigues

            src = find(op.get("instance"), i)
            count = int(op.get("count", 2))
            total = float(op.get("angle", 360.0))
            if count < 2 or count > 360:
                raise AppError(422, "invalid_op", f"op {i}: pattern_circular needs count 2..360", {"op": i})
            d = np.asarray(op.get("axis_direction") or [0, 0, 1], float)
            o = np.asarray(op.get("axis_origin") or [0, 0, 0], float)
            if float(np.linalg.norm(d)) < 1e-12:
                raise AppError(422, "invalid_op", f"op {i}: axis_direction must be non-zero", {"op": i})
            d = d / np.linalg.norm(d)
            step_deg = total / count if abs(total - 360.0) < 1e-9 else total / (count - 1)
            new_ids = _next_ids(asm, count - 1)
            for k, iid in enumerate(new_ids, 1):
                r = rodrigues(d * np.radians(step_deg * k))
                m = np.eye(4)
                m[:3, :3] = r
                m[:3, 3] = o - r @ o
                comps.append(copy_of(src, am.compose(am.from_list(m.tolist()), src.transform), iid, f"{k + 1}"))
                touch(iid)
            applied.append({"op": kind, "instance": src.instance_id, "created": new_ids})
        elif kind == "set_quantity":
            # number of active instances of one definition in this assembly; new copies repeat the last instance's
            # pose plus ``step`` (or the same pose, reported), removals drop the newest instances
            d = _definition_in(db, asm, op.get("definition") or {})
            key = str(d.get("model_revision_id") or d.get("child_revision_id"))
            same = [c for c in comps if str(c.def_id) == key and not c.suppressed]
            want = int(op.get("quantity", len(same)))
            if want < 0 or want > MAX_LEAVES:
                raise AppError(422, "invalid_op", f"op {i}: quantity out of range", {"op": i})
            if want > len(same):
                if not same:
                    raise AppError(422, "definition_not_used", f"op {i}: add the first instance with add_instance", {"op": i})
                last = same[-1]
                step = op.get("step")
                new_ids = _next_ids(asm, want - len(same))
                for k, iid in enumerate(new_ids, 1):
                    t = am.compose(am.translation(*(float(s) * k for s in step)), last.transform) if step else last.transform
                    comps.append(copy_of(last, t, iid, f"{len(same) + k}"))
                    touch(iid)
                if not step:
                    notes.append({"code": "coincident_copies", "subject": key, "message": f"{len(new_ids)} new instance(s) share the pose of "
                                  f"{last.instance_id}; move or mate them (interference check reports the overlap)"})
                applied.append({"op": kind, "definition": key, "created": new_ids})
            elif want < len(same):
                drop = sorted(same, key=lambda c: c.instance_id)[want:]
                for c in drop:
                    comps.remove(c)
                    touch(c.instance_id)
                dropped_ids = {c.instance_id for c in drop}
                mates[:] = [m for m in mates if not ({(m.a or {}).get("instance"), (m.b or {}).get("instance")} & dropped_ids)]
                applied.append({"op": kind, "definition": key, "removed": sorted(dropped_ids)})
    return {"ops": applied, "affected_instances": affected, "notes": notes}


def restore_revision(db: Session, *, assembly_id: uuid.UUID, user: User, base_revision_id: uuid.UUID,
                     revision_id: uuid.UUID, policy: str | None = None) -> AssemblyRevision:
    """Restore exact component and mate definitions into a new, independently checked revision."""
    asm = _lock_assembly(db, assembly_id)
    head = latest_revision(db, asm.id)
    if head is None or head.id != base_revision_id:
        raise conflict("head_changed", "the assembly changed after the operation finished")
    target = _load_revision(db, revision_id)
    if target.assembly_id != asm.id:
        raise not_found("assembly revision")
    comps, mates = revision_state(db, target)
    return _save_revision(db, asm, user=user, comps=comps, mates=mates, source="manual", policy=policy,
                          parent=head, spec_ref=target.spec_ref, notes=target.notes, assumptions=target.assumptions,
                          source_file_version_id=target.source_file_version_id, note=f"Restored A{target.rev_no}",
                          change={"restored_from_id": str(target.id), "ops": [{"op": "restore", "revision_id": str(target.id)}]},
                          run_heavy_checks=not pol.is_turbo(policy))


def _edited_spec_ref(ref: dict[str, Any] | None) -> dict[str, Any] | None:
    """A hand edit of a generated revision: the build's coverage, target BOM numbering and build key belong to that
    build, not to the edited tree (generation supplement B review); a partial build's edit is checked against the whole
    target again (``target_structure``), so completing it by hand passes the spec check and nothing says partial."""
    if not ref:
        return ref
    out = {k: v for k, v in ref.items() if k not in ("coverage", "target_bom", "build_key", "target_structure")}
    if ref.get("target_structure"):
        out["structure"] = ref["target_structure"]
    return out


def edit_revision(db: Session, *, assembly_id: uuid.UUID, user: User, base_revision_id: uuid.UUID, ops: list[dict[str, Any]],
                  policy: str | None = None, note: str = "", source: str = "manual", spec: dict[str, Any] | None = None,
                  node_run_id: uuid.UUID | None = None, run_heavy_checks: bool = False,
                  clearance_mm: float | None = None) -> AssemblyRevision:
    """New revision = base revision + ops. The base must be the latest revision (else 409 revision_conflict)."""
    asm = _lock_assembly(db, assembly_id)
    latest = latest_revision(db, asm.id)
    if latest is None or latest.id != base_revision_id:
        raise conflict("revision_conflict", "the assembly changed since the base revision was read; reload and apply the edit again",
                       base_revision_id=str(base_revision_id), latest_revision_id=str(latest.id) if latest else None,
                       latest_rev_no=latest.rev_no if latest else None)
    if not ops:
        raise bad_request("no_ops", "an edit needs at least one op")
    comps, mates = revision_state(db, latest)
    change = apply_ops(db, asm, comps, mates, ops)
    return _save_revision(db, asm, user=user, comps=comps, mates=mates, source=source, policy=policy, parent=latest,
                          spec=spec, spec_ref=_edited_spec_ref(latest.spec_ref), note=note, notes=change["notes"], change=change, node_run_id=node_run_id,
                          run_heavy_checks=run_heavy_checks, clearance_mm=clearance_mm)


# =====================================================================================================
# Engineering spec (W2 content) -> assembly
# =====================================================================================================


def create_from_spec_version(db: Session, *, project_id: uuid.UUID, user: User, spec_version_id: uuid.UUID | str,
                             bindings: dict[str, dict[str, Any]], policy: str | None = None, number: str | None = None,
                             node_run_id: uuid.UUID | None = None, source: str = "manual", partial: bool = False,
                             missing: list[dict[str, Any]] | None = None,
                             continue_from: AssemblyRevision | None = None, placement: dict[str, Any] | None = None,
                             build_notes: list[dict[str, Any]] | None = None) -> tuple[Assembly, AssemblyRevision, dict[str, Any]]:
    """:func:`create_from_spec` from an exact, usable W2 spec version of this project (the public entry; W8 too; W8
    builds ``partial`` assemblies and passes the Turbo ``placement`` decided by AI — mates the solver turns into
    positions, see :func:`create_from_spec`).

    The content is read from the stored version (404 outside the project, 409 while not usable), never from the
    client. A spec extracted in Turbo holds AI-adopted values: a standard assembly takes it over only once a person
    confirmed every one of them (S5: 409 ``upstream_assumptions_unconfirmed`` lists them with the entry that confirms
    them in one go). The version's policy and ``simulated`` flag travel in the revision's ``spec_ref``."""
    from ..models import EngineeringSpecVersion
    from . import engineering_spec as spec_svc
    from . import verification

    try:
        run_policy = pol.parse_policy(policy)
    except ValueError as exc:
        raise bad_request("invalid_execution_policy", str(exc))
    content = spec_svc.get_content(db, uuid.UUID(str(spec_version_id)), project_id)
    v = db.get(EngineeringSpecVersion, uuid.UUID(str(spec_version_id)))
    assert v is not None
    from . import design_spec
    design = design_spec.resolve_spec_design(db, user, v, tool_kind="assembly_from_spec", node_run_id=node_run_id)
    if not pol.is_turbo(run_policy):
        verification.require_confirmed_spec(db, v)
    meta = {"spec_id": str(v.spec_id), "version_no": v.version_no, "execution_policy": v.execution_policy, "simulated": bool(v.simulated)}
    if design is not None:
        meta["design"] = {"ref": design.ref.model_dump(mode="json"), "instances": content["instances"],
            "relations": content["relations"], "requirements_sha256": content["design"]["requirements_sha256"],
            "static_poses": True}
    return create_from_spec(db, project_id=project_id, user=user, spec=content, bindings=bindings, policy=run_policy,
                            spec_version_id=v.id, number=number, node_run_id=node_run_id, source=source, spec_meta=meta,
                            partial=partial, missing=missing, continue_from=continue_from, placement=placement,
                            build_notes=build_notes)


def is_frame_ref(ref: Any) -> bool:
    """A relation / mate side on an assembly's own frame, ``{"instance": "@<assembly definition key>", "datum": …}``
    (generation reliability round 3, B: the root has no instance; an interface ``if.<key>`` is a datum of its owner's
    frame)."""
    return isinstance(ref, dict) and str(ref.get("instance") or "").startswith("@")


def relation_ends(r: dict[str, Any]) -> list[str]:
    """The instance keys a relation joins (a side on an assembly frame is no instance)."""
    return [str(ref["instance"]) for ref in (r.get("a"), r.get("b")) if isinstance(ref, dict) and ref.get("instance") and not is_frame_ref(ref)]


def _datum_in(name: str, d: Any) -> Datum | None:
    """A datum from its JSON form (None when it is not one)."""
    if isinstance(d, Datum):
        return d
    try:
        kind = d["kind"]
        origin = tuple(float(x) for x in d["origin"])
        direction = tuple(float(x) for x in d.get("direction") or (0.0, 0.0, 1.0))
        x_dir = tuple(float(x) for x in d.get("x_dir") or (1.0, 0.0, 0.0))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if kind not in ("frame", "plane", "axis", "point") or len(origin) != 3 or len(direction) != 3 or len(x_dir) != 3:
        return None
    return Datum(name=name, kind=kind, origin=origin, direction=direction, x_dir=x_dir)  # type: ignore[arg-type]


def _datum_json(d: Datum) -> dict[str, Any]:
    """A frame datum as a mate side carries it (``frame``: name → this)."""
    return {"kind": d.kind, "origin": [round(float(x), 6) + 0.0 for x in d.origin],
            "direction": [round(float(x), 9) + 0.0 for x in d.direction], "x_dir": [round(float(x), 9) + 0.0 for x in d.x_dir]}


def interfaces_of(spec: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    """The spec's interfaces (``content.interfaces``, generation reliability round 3) whose owner is ``owner`` (an
    interface without an owner belongs to the root)."""
    root = spec.get("root") or "D-00"
    return [f for f in spec.get("interfaces") or [] if isinstance(f, dict) and f.get("key") and str(f.get("owner") or root) == owner]


def frame_datums(spec: dict[str, Any], parent: str, ref: str, pose_of: Any = None, *,
                 tree: dict[str, Any] | None = None) -> tuple[dict[str, Datum] | None, str]:
    """The datums of an assembly frame ``ref`` (``@<assembly key>``) in the frame of the assembly ``parent`` whose
    instances a mate joins to it: its frame datums (``origin``, ``X`` / ``Y`` / ``Z``, ``XY`` / ``YZ`` / ``ZX``) and its
    interfaces (``if.<key>``). ``ref`` is ``parent`` itself (as they are) or an assembly above it: then ``parent`` must
    hang under it along exactly one instance path (a sub-assembly used twice is in two places of it — its contents mate
    to its own frame, the instances of it are mated in the assembly above) and ``pose_of(instance key)`` (its position
    in its parent, None when it has none) maps the datums down that path. ``pose_of`` None: only checks that the frame
    can be reached (the datums stay in the owner's frame). Returns (datums | None, why not)."""
    from ..cad.datums import frame_datums as own_frame, interface_datums
    from ..cad.mates import transformed

    owner = ref[1:] if ref.startswith("@") else ref
    t = tree or spec_tree(spec)
    if owner not in t["definitions"] or (owner != t["root"] and not any(
            str(d.get("key")) == owner and d.get("kind") == "assembly" for d in spec.get("definitions") or [] if isinstance(d, dict))):
        return None, f"{ref} is not an assembly of this spec"
    own = {d.name: d for d in own_frame() + interface_datums(interfaces_of(spec, owner))}
    if owner == parent:
        return own, ""
    by_def: dict[str, list[dict[str, Any]]] = {}
    for i in t["instances"]:
        by_def.setdefault(str(i.get("definition")), []).append(i)

    def up(dk: str, depth: int) -> list[list[dict[str, Any]]]:
        if dk == owner:
            return [[]]
        if depth > MAX_DEPTH or dk == t["root"]:
            return []
        out: list[list[dict[str, Any]]] = []
        for i in by_def.get(dk, []):
            out += [p + [i] for p in up(str(i.get("parent") or t["root"]), depth + 1)]
            if len(out) > 1:
                break
        return out

    paths = up(parent, 0)
    if not paths:
        below = paths_down(t["instances"], parent, owner, root=t["root"])
        return None, (f"{ref} is a sub-assembly used more than once under {parent}: relate to the interface on one of its "
                      f"instances ({', '.join(p[0] for p in below)})" if below else f"{ref} is not an assembly above or below {parent}")
    if len(paths) > 1:
        return None, (f"{parent} is used more than once under {owner}: {ref} is not one place in its frame — mate its contents "
                      f"to @{parent}, and its instances in {owner}")
    if pose_of is None:
        return own, ""
    m = am.IDENTITY
    for i in paths[0]:
        p = pose_of(i["key"])
        if p is None:
            return None, f"{i['key']} has no position in {i.get('parent') or t['root']} yet: {ref} cannot be reached from {parent}"
        m = am.compose(m, p)
    inv = am.invert(m)
    return {n: transformed(d, inv, n) for n, d in own.items()}, ""


def frame_side(found: dict[str, Datum], kind: str, datum: str) -> dict[str, Any] | None:
    """The datums a mate side ``datum`` of kind ``kind`` needs from a frame's ``found`` datums (JSON form; a pattern
    match takes every ``<prefix>.axis[i]``); None when the frame has none of them."""
    if kind == "pattern_match":
        prefix = datum.split(".axis")[0]
        got = {n: _datum_json(d) for n, d in found.items() if n.startswith(prefix + ".axis[") and n.endswith("]")}
        return got or None
    d = found.get(datum)
    return {datum: _datum_json(d)} if d is not None else None


def spec_tree(spec: dict[str, Any]) -> dict[str, Any]:
    """The part of a spec that hangs under its root: reachable definition keys (assemblies and parts), the instances
    and relations among them, and the definitions with no path to the root (W2 keeps ambiguous ones at quantity 0).
    A relation side may be an assembly's own frame (``@<key>``, generation reliability round 3) of an assembly under
    the root; a relation is kept with its instance side as ``a`` (a frame on ``a`` swaps the two sides). A frame side
    of a sub-assembly BELOW ``a``'s parent (a root-level part related to ``@D-10``'s interface) is that sub-assembly's
    instance when there is exactly one path down to it — ``{instance: <first instance>, datum: "<rest of the path>/<datum>"}``,
    marked ``via: "@D-10"`` (it moves neither side, :func:`_moving`); with several paths it stays a frame side and is
    reported once as unresolved (:func:`frame_datums`)."""
    defs = {d["key"]: d for d in spec.get("definitions") or [] if d.get("key")}
    root_key = spec.get("root") or "D-00"
    instances = [i for i in spec.get("instances") or [] if i.get("key")]
    children: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        children.setdefault(inst.get("parent") or root_key, []).append(inst)
    reach: set[str] = set()
    frontier = [root_key]
    while frontier:
        k = frontier.pop()
        if k in reach or len(reach) > MAX_DEFINITIONS + 50:
            continue
        reach.add(k)
        frontier.extend(i["definition"] for i in children.get(k, []) if i.get("definition"))
    insts = [i for i in instances if (i.get("parent") or root_key) in reach]
    inst_keys = {i["key"] for i in insts}

    def end_ok(ref: Any) -> bool:
        if is_frame_ref(ref):
            return str(ref["instance"])[1:] in reach
        return isinstance(ref, dict) and ref.get("instance") in inst_keys

    parent_of = {i["key"]: i.get("parent") or root_key for i in insts}
    rels = []
    for r in spec.get("relations") or []:
        if not isinstance(r, dict):
            continue
        if is_frame_ref(r.get("a")) and isinstance(r.get("b"), dict) and not is_frame_ref(r.get("b")):
            r = {**r, "a": r["b"], "b": r["a"]}
        a = (r.get("a") or {}).get("instance") if isinstance(r.get("a"), dict) else None
        if is_frame_ref(r.get("b")) and a in parent_of:
            # an interface of a sub-assembly BELOW the instance's parent is a datum of that sub-assembly's instance
            # (review of round 3: a root-level part related to @D-10's interface): taken along the one instance path
            paths = paths_down(insts, parent_of[a], str(r["b"]["instance"])[1:], root=root_key)
            if len(paths) == 1:
                path = paths[0]
                r = {**r, "b": {**r["b"], "instance": path[0], "datum": "/".join(path[1:] + [str(r["b"].get("datum") or "origin")])},
                     "via": str(r["b"]["instance"])}
        if end_ok(r.get("a")) and not is_frame_ref(r.get("a")) and (end_ok(r.get("b")) if r.get("b") else True):
            rels.append(r)
    return {"root": root_key, "definitions": (reach & set(defs)) | {root_key}, "instances": insts, "relations": rels,
            "unplaced": sorted(set(defs) - reach)}


def paths_down(instances: list[dict[str, Any]], parent: str, owner: str, *, root: str) -> list[list[str]]:
    """The instance paths from assembly ``parent`` down to the instances of assembly definition ``owner`` (at most 2:
    enough to tell one place from several; none when ``owner`` is not below ``parent``)."""
    kids: dict[str, list[dict[str, Any]]] = {}
    for i in instances:
        kids.setdefault(str(i.get("parent") or root), []).append(i)
    out: list[list[str]] = []

    def walk(dk: str, path: list[str], depth: int) -> None:
        for i in kids.get(dk, []):
            if len(out) > 1 or depth > MAX_DEPTH:
                return
            if i.get("definition") == owner:
                out.append(path + [str(i["key"])])
            elif str(i.get("definition")) in kids:
                walk(str(i["definition"]), path + [str(i["key"])], depth + 1)

    if owner != parent:
        walk(parent, [], 0)
    return out


def _spec_props(db: Session, part: Part, user: User, d: dict[str, Any], key: str, spec_version_id: str | None) -> dict[str, Any] | None:
    """Fill a part's BOM attributes from a spec definition without overwriting what is known (they are part-wide and
    show in every assembly that uses the part): only values the spec states (procurement other than unknown — a
    standard part with unknown procurement is ``standard``; representation other than exact) and only where the part
    has none yet. A different known value stays and is returned as a note."""
    proc = d.get("procurement") if d.get("procurement") in ("make", "buy", "standard") else None
    if d.get("kind") == "standard_part" and proc is None:
        proc = "standard"
    rep = d.get("representation") if d.get("representation") in ("nominal", "simplified") else None
    row = db.get(AssemblyPartProps, part.id)
    cur_proc = row.procurement if row is not None else "unknown"
    cur_rep = row.representation if row is not None else "exact"
    set_proc = proc if proc and cur_proc == "unknown" else None
    set_rep = rep if rep and cur_rep == "exact" else None
    if set_proc or set_rep:
        set_part_props(db, part, user, procurement=set_proc, representation=set_rep,
                       source={"spec_version_id": spec_version_id, "definition": key})
    kept = {}
    if proc and cur_proc not in ("unknown", proc):
        kept["procurement"] = {"spec": proc, "part": cur_proc}
    if rep and cur_rep not in ("exact", rep):
        kept["representation"] = {"spec": rep, "part": cur_rep}
    if not kept:
        return None
    have = ", ".join(f"{k} {v['part']}" for k, v in kept.items())
    want = ", ".join(f"{k} {v['spec']}" for k, v in kept.items())
    return {"code": "part_props_kept", "subject": key, "kept": kept,
            "message": f"part {part.number} keeps its own BOM attributes ({have}); the spec says {want}"}


#: Mate kinds that need an axis datum on both sides (cad/mates.py _NEEDS) and the params some kinds require.
_AXIS_KINDS = ("coaxial", "pattern_match")
_NEEDED_PARAMS = {"offset": "distance", "angle": "angle"}
#: Solver priority of an AI placement mate by what it rests on (generation reliability round 3, B): the spec's own
#: relations keep the default 100, so a conflicting AI mate is the one dropped — an estimate first.
AI_MATE_PRIORITY = {"stated": 110, "derived": 120, "estimated": 130}


def _map_relations(db: Session, rels: list[dict[str, Any]], inst_def: dict[str, str], part_bind: dict[str, uuid.UUID],
                   policy: str, *, frames: Any = None, spec: dict[str, Any] | None = None
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Spec relations name datums before any part exists (W0 §5: ``origin`` / ``port.<name>``). A coaxial or
    pattern_match relation on ``origin`` is mapped to the built part's axis when there is exactly one (an assumption
    by ``rule``). Otherwise — several or no axis, an assembly instance, a missing offset distance / angle — standard
    gets a question (the relation is blocked) and Turbo leaves the binding to placement, with the original intent
    and candidate datums in ``choices``; an invalid or guessed mate is never built. Returns
    (relations to build, assumptions, questions, choices).

    Generation reliability round 3 (B): a side on an assembly frame (``@<key>``) takes its datums from ``frames(ref,
    instance)`` (:func:`frame_datums` in the frame of that instance's parent): they travel with the relation
    (``frame``); a frame that cannot be reached or lacks the datum is a question / a Turbo rule decision like any other
    unresolved datum. A side that already carries its ``frame`` (an AI placement mate) is kept as it is.

    Review of round 3: a coaxial / pattern_match relation that joins ONE axis (a single fastener) to a PATTERN of several
    (a hole pattern, an interface ``pattern``) says which pattern, not which of its axes: it is a CHOICE — ``choices``
    lists it ({relation, kind, instance, datum, pattern: {instance, datum}, axes}); standard asks which axis, Turbo leaves
    it to the placement step (the instance stays to be placed; the model picks one axis ``<pattern>.axis[i]``, one per
    instance). ``pattern_match`` between equal counts is mated as it is."""
    from datetime import UTC, datetime

    from ..cad import mates as M

    keep: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    choices: list[dict[str, Any]] = []
    turbo = pol.is_turbo(policy)
    at = datetime.now(UTC).isoformat()

    def indexed(names: Iterable[str], prefix: str) -> list[str]:
        got = [n for n in names if n.startswith(prefix + ".axis[") and n.endswith("]") and n[len(prefix) + 6:-1].isdigit()]
        return sorted(got, key=lambda n: int(n[len(prefix) + 6:-1]))

    def side_axes(rel: dict[str, Any], side: str) -> list[str] | None:
        """The axis datums one side of a coaxial / pattern_match relation denotes — one axis, or every axis of a
        pattern (``<name>.axis[i]``) — or None when that is not known before the build."""
        end = rel.get(side) or {}
        datum = str(end.get("datum") or "origin")
        if is_frame_ref(end):
            names = (frames(str(end["instance"]), str((rel.get("a") or {}).get("instance")))[0] if frames is not None else None) or {}
            kinds = {n: d.kind for n, d in names.items()}
        else:
            inst = str(end.get("instance"))
            axes = axes_of(inst)
            if axes is None:  # a sub-assembly instance: what its interfaces give (its other datums exist once it is built)
                from ..cad.datums import interface_datums

                kinds = {d.name: d.kind for d in interface_datums(interfaces_of(spec or {}, inst_def.get(inst, "")))}
            elif datum == "origin":  # the part's one pattern (pattern_match) or its one plain axis (coaxial)
                heads = sorted({n.split(".axis[")[0] for n in axes if ".axis[" in n})
                plain = [n for n in axes if "[" not in n and not any(n == h + ".axis" for h in heads)]
                if rel.get("kind") == "pattern_match" and len(heads) == 1:
                    return indexed(axes, heads[0])
                if rel.get("kind") == "pattern_match":
                    return plain if len(plain) == 1 and not heads else None
                candidates = origin_axes(inst, str(rel.get("kind")))
                return candidates if candidates is not None and len(candidates) == 1 else None
            else:
                kinds = {n: "axis" for n in axes}
        if rel.get("kind") != "pattern_match" and kinds.get(datum) == "axis":
            return [datum]
        return indexed(kinds, datum.split(".axis")[0]) or ([datum] if kinds.get(datum) == "axis" else None)

    def pattern_choice(rel: dict[str, Any]) -> dict[str, Any] | None:
        """One axis against a pattern of several: the choice of the pattern's axis (see above). An AI placement mate
        names one axis itself (``placement_agent`` checks it)."""
        if rel.get("kind") not in _AXIS_KINDS or rel.get("source") == "ai_turbo":
            return None
        sides = {s: side_axes(rel, s) for s in ("a", "b")}
        counts = sorted(len(v or []) for v in sides.values())
        if counts[0] != 1 or counts[1] < 2:
            return None
        one = "a" if len(sides["a"] or []) == 1 else "b"
        many = "b" if one == "a" else "a"
        pattern = {"instance": str(rel[many]["instance"]), "datum": str(rel[many].get("datum") or "origin").split(".axis")[0]}
        return {"relation": rel.get("key") or "?", "kind": rel.get("kind"),
                "one": {"instance": str(rel[one]["instance"]), "datum": (sides[one] or [""])[0]}, "pattern": pattern,
                "axes": list(sides[many] or []),
                # the instance to place: the single axis's (a fastener), else the pattern's (which of its holes)
                "instance": str(rel[one]["instance"]) if not is_frame_ref(rel[one]) else pattern["instance"]}

    def frame_problem(rel: dict[str, Any]) -> dict[str, Any] | None:
        kind, key = rel.get("kind"), rel.get("key") or "?"
        inst = (rel.get("a") or {}).get("instance")
        for side in ("a", "b"):
            end = rel.get(side)
            if not is_frame_ref(end) or end.get("frame"):
                continue
            ref, datum = str(end["instance"]), str(end.get("datum") or "origin")
            found, why = frames(ref, str(inst)) if frames is not None else (None, "assembly frames are not available here")
            got = frame_side(found, str(kind), datum) if found is not None else None
            bad = next((M.compatible(str(kind), d["kind"], d["kind"]) for d in (got or {}).values()), None) if got else None
            if got is None or bad:
                return {"relation": key, "kind": kind, "side": side, "instance": ref,
                        "question": f"{key} ({kind}): {bad or why or f'{ref} has no datum {datum}'}"}
            end["frame"] = got
        return None

    def axes_of(inst_key: str) -> list[str] | None:
        rid = part_bind.get(inst_def.get(inst_key, ""))
        rev = db.get(ModelRevision, rid) if rid else None
        if rev is None:
            return None  # an assembly instance: its datums are only known after it is built
        return [d.name for d in part_datums(db, rev) if d.kind == "axis"]

    def origin_axes(inst_key: str, kind: str) -> list[str] | None:
        axes = axes_of(inst_key)
        if axes is None:
            return None
        heads = {n.split(".axis[")[0] for n in axes if ".axis[" in n}
        if kind == "pattern_match":
            return sorted(h + ".axis" for h in heads)
        # A hole pattern's plain .axis is only an alias for its first axis, not evidence of a unique hole.
        return [n for n in axes if n not in {h + ".axis" for h in heads}]

    def deferred_origin(rel: dict[str, Any]) -> dict[str, Any] | None:
        """An unqualified part origin is not evidence for a particular hole or contacting feature plane."""
        if rel.get("source") == "ai_turbo" or rel.get("kind") not in (*_AXIS_KINDS, "planar"):
            return None
        kind = rel.get("kind")
        candidates: dict[str, list[str]] = {}
        part_sides: set[str] = set()
        for side in ("a", "b"):
            end = rel.get(side) or {}
            inst = str(end.get("instance") or "")
            named = str(end.get("datum") or "origin")
            if named != "origin":
                candidates[side] = [named]  # includes an explicitly named frame / sub-assembly counterpart
            rid = part_bind.get(inst_def.get(inst, ""))
            rev = db.get(ModelRevision, rid) if rid else None
            if rev is None or is_frame_ref(end):
                continue
            part_sides.add(side)
            datums = part_datums(db, rev)
            if named != "origin":
                continue
            if kind in _AXIS_KINDS:
                candidates[side] = origin_axes(inst, str(kind)) or []
            elif kind == "planar":
                candidates[side] = [d.name for d in datums if d.kind == "plane"]
        ambiguous_axis = kind in _AXIS_KINDS and any(
            str((rel.get(side) or {}).get("datum") or "origin") == "origin" and len(names) > 1
            for side, names in candidates.items())
        unspecified_contact = kind == "planar" and len(part_sides) == 2 and any(
            str((rel.get(side) or {}).get("datum") or "origin") == "origin" for side in ("a", "b"))
        if not ambiguous_axis and not unspecified_contact:
            return None
        return {**rel, "relation": rel.get("key") or "?", "kind": kind,
                "instance": str((rel.get("a") or {}).get("instance")),
                "a": dict(rel.get("a") or {}), "b": dict(rel.get("b") or {}),
                "deferred_binding": {"candidates": candidates,
                                     "reason": ("origin does not identify which built axis is intended" if ambiguous_axis else
                                                "part origins do not identify the physical contacting planes")}}

    for raw in rels:
        rel = json.loads(json.dumps(raw))
        kind, key = rel.get("kind"), rel.get("key") or "?"
        problem: dict[str, Any] | None = None
        need = _NEEDED_PARAMS.get(kind)
        if need and need not in (rel.get("params") or {}):
            problem = {"relation": key, "kind": kind, "missing": f"params.{need}",
                       "question": f"{key} ({kind}) needs params.{need}; what is it?"}
        elif (choice := pattern_choice(rel)) is not None:
            choices.append(choice)
            one, pat, axes = choice["one"], choice["pattern"], choice["axes"]
            problem = {"relation": key, "kind": kind, "instance": choice["instance"], "candidates": axes, "choice": True,
                       "question": f"{key} ({kind}): {one['instance']} {one['datum']} goes on one of the {len(axes)} axes of "
                                   f"{pat['instance']} {pat['datum']} ({axes[0]} … {axes[-1]}); which one?"}
        elif (problem := frame_problem(rel)) is not None:
            pass
        elif (choice := deferred_origin(rel)) is not None:
            choices.append(choice)
            problem = {"relation": key, "kind": kind, "instance": choice["instance"], "deferred": True,
                       "candidates": [n for names in choice["deferred_binding"]["candidates"].values() for n in names],
                       "question": f"{key} ({kind}): {choice['deferred_binding']['reason']}; select named physical datums from the source intent"}
        elif kind in _AXIS_KINDS:
            for side in ("a", "b"):
                end = rel.get(side) or {}
                if is_frame_ref(end) or str(end.get("datum") or "origin") != "origin":
                    continue  # a named datum: the solver binds it exactly (or reports it broken)
                axes = origin_axes(str(end.get("instance")), str(kind))
                if axes is not None and len(axes) == 1:
                    end["datum"] = axes[0]
                    assumptions.append({"subject": key, "field": f"{side}.datum", "adopted": axes[0], "author": "rule", "at": at,
                                        "trigger": "missing",
                                        "basis": f"{kind} needs an axis; the spec gave origin and the built part has exactly one",
                                        "affected": [key, str(end.get("instance"))]})
                    continue
                cands = axes or []
                inst = end.get("instance")
                problem = {"relation": key, "kind": kind, "side": side, "instance": inst, "candidates": cands,
                           "question": (f"{key} ({kind}): which axis of {inst} is meant? candidates: {', '.join(cands)}" if cands
                                        else f"{key} ({kind}): {inst} has no single axis datum to join; how is it located?")}
                break
        if problem is None:
            keep.append(rel)
        elif turbo:
            then = ("the AI placement step must bind this original relation to named physical datums" if problem.get("deferred") else
                    f"the AI placement step connects {problem['instance']} to one of them (one axis per instance)" if problem.get("choice")
                    else "The instances keep their stated placements.")
            assumptions.append({"subject": key, "field": "relation", "adopted": "not applied", "author": "rule", "at": at,
                                "basis": f"Turbo: {problem['question']} {then}",
                                "alternatives": problem.get("candidates") or [], "affected": [key],
                                "trigger": "ambiguous" if problem.get("candidates") else "missing"})
        else:
            questions.append({k: v for k, v in problem.items() if k not in ("choice", "deferred")})
    return keep, assumptions, questions, choices


def _deferred_mates(choices: list[dict[str, Any]], rels: list[dict[str, Any]], *,
                    applied: set[str] | None = None) -> dict[str, list[str]]:
    """Original kinds, specified endpoints and explicit parameters survive physical datum selection.

    ``applied`` is the final solver's proof. Before solving, matching is only provisional; an unrelated or dropped
    placement constraint never removes the original missing relation from the final coverage.
    """
    resolved: dict[str, list[str]] = {}
    for choice in choices:
        binding = choice.get("deferred_binding")
        if not binding:
            continue
        for rel in rels:
            key = str(rel.get("key"))
            if rel.get("source") != "ai_turbo" or rel.get("kind") != choice["kind"] or (applied is not None and key not in applied):
                continue
            aligned = next((order for order in (("a", "b"), ("b", "a")) if all(
                (choice.get(side) or {}).get("instance") == (rel.get(other) or {}).get("instance")
                for side, other in zip(("a", "b"), order, strict=True))), None)
            if aligned is None:
                continue
            def same_datum(side: str, other: str) -> bool:
                name = (rel.get(other) or {}).get("datum")
                candidates = binding["candidates"].get(side, [])
                if not name or name == "origin":
                    return False
                if choice["kind"] == "pattern_match":
                    return str(name).split(".axis")[0] in {n.split(".axis")[0] for n in candidates}
                return name in candidates

            if not all(same_datum(side, other) for side, other in zip(("a", "b"), aligned, strict=True)):
                continue
            # Explicit source parameters are constraints too. Reversing a distance with same-facing normals changes
            # its sign; other planar/coaxial parameters keep their meaning under endpoint reversal.
            expected = dict(choice.get("params") or {})
            actual = dict(rel.get("params") or {})
            if choice["kind"] == "planar":
                # An unspecified contact still means zero gap. Accept the solver's legacy offset alias as well.
                expected["distance"] = expected.pop("distance", expected.pop("offset", 0))
                actual["distance"] = actual.pop("distance", actual.pop("offset", 0))
                # An origin has no physical face normal. Its omitted flip cannot constrain the orientation of the
                # concrete faces chosen later; an explicitly stated flip remains part of the source constraint.
            if aligned == ("b", "a") and expected.get("flip", actual.get("flip", False)) and "distance" in expected:
                expected["distance"] = -expected["distance"]
            if any(actual.get(name, False if name == "flip" else 0) != value for name, value in expected.items()):
                continue
            resolved.setdefault(str(choice["relation"]), []).append(key)
    return resolved


def _deferred_problem(choice: dict[str, Any]) -> dict[str, Any]:
    return {"code": "relation_datum_unresolved", "recovery": "fix_spec",
            "reason": choice["deferred_binding"]["reason"] + "; no applied placement mate binds this relation yet"}


def _deferred_problems(choices: list[dict[str, Any]], rels: list[dict[str, Any]], *,
                       applied: set[str] | None = None) -> dict[str, dict[str, Any]]:
    selected = _deferred_mates(choices, rels, applied=applied)
    return {choice["relation"]: _deferred_problem(choice) for choice in choices
            if choice.get("deferred_binding") and choice["relation"] not in selected}


def _spec_relations(db: Session, tree_part: dict[str, Any], part_bind: dict[str, uuid.UUID], asm_keys: set[str], policy: str, *,
                    partial: bool, spec: dict[str, Any] | None = None,
                    pose_of: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]],
                                                  list[dict[str, Any]]]:
    """The spec relations under the root as :func:`create_from_spec` mates them: (relations with datums mapped by
    :func:`_map_relations`, their rule assumptions, the Turbo assumptions of cross-level relations left out, relation key →
    why it cannot be mated as stated, the axis choices — one axis against a pattern of several). Standard without ``partial`` refuses a cross-level relation or an unresolved datum
    (422). ``partial``: only relations whose ends can exist are mapped (an unbuilt part has no datums). A side on an
    assembly frame (``@<key>``) is resolved by :func:`frame_datums` with ``pose_of`` (None: only checked to be
    reachable)."""
    turbo = pol.is_turbo(policy)
    root_key = tree_part["root"]
    parent_of = {i["key"]: i.get("parent") or root_key for i in tree_part["instances"]}
    inst_def = {i["key"]: i["definition"] for i in tree_part["instances"]}
    cross: list[str] = []
    same_level: list[dict[str, Any]] = []
    for rel in tree_part["relations"]:
        a = (rel.get("a") or {}).get("instance")
        b = None if is_frame_ref(rel.get("b")) else (rel.get("b") or {}).get("instance")
        pa = parent_of.get(a)
        if pa is None or (b is not None and parent_of.get(b) != pa):
            cross.append(rel.get("key") or "?")
            continue
        same_level.append(rel)
    rel_problems: dict[str, dict[str, Any]] = {}
    cross_assumptions: list[dict[str, Any]] = []
    if cross:
        if not turbo and not partial:
            raise AppError(422, "relation_cross_level", "relations must join instances of the same parent assembly", {"relations": cross})
        for k in cross:
            if turbo:
                cross_assumptions.append({"subject": k, "field": "relation", "adopted": "not applied",
                                          "basis": "the relation joins instances of different parent assemblies", "author": "ai_turbo",
                                          "trigger": "conflict", "affected": [k]})
            else:
                rel_problems[k] = {"code": "relation_cross_level", "recovery": "fix_spec",
                                   "reason": "the relation joins instances of different parent assemblies"}
    if partial:  # only relations whose ends can exist are mapped (an unbuilt part has no datums)
        same_level = [r for r in same_level if all(inst_def.get(e) in asm_keys or inst_def.get(e) in part_bind for e in relation_ends(r))]

    def frames(ref: str, inst: str) -> tuple[dict[str, Datum] | None, str]:
        return frame_datums(spec or {}, parent_of.get(inst, root_key), ref, pose_of, tree=tree_part)

    mapped, rel_assumptions, rel_questions, choices = _map_relations(db, same_level, inst_def, part_bind, policy,
                                                                     frames=frames if spec is not None else None, spec=spec)
    if turbo:
        rel_problems.update(_deferred_problems(choices, mapped))
    if pose_of is None:  # only checked to be reachable: the datums are not mapped into the parent frame yet
        for r in mapped:
            for side in ("a", "b"):
                if is_frame_ref(r.get(side)):
                    r[side].pop("frame", None)
    if rel_questions and not partial:
        raise AppError(422, "relation_datum_unresolved", "some spec relations cannot be mated as stated; say which datum is meant in "
                       "the spec, or create the assembly in Turbo / add the mate in the assembly workspace", {"questions": rel_questions})
    for q in rel_questions:
        rel_problems[str(q.get("relation"))] = {"code": "relation_datum_unresolved", "recovery": "fix_spec", "reason": q.get("question") or "",
                                                "question": q.get("question")}
    return mapped, rel_assumptions, cross_assumptions, rel_problems, choices


def placement_gaps(db: Session, *, spec: dict[str, Any], bindings: dict[str, dict[str, Any]], policy: str | None,
                   missing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Generation supplement F (TB-03, B-02.4): what a partial Turbo build (:func:`create_from_spec` with ``partial``)
    would keep, and which of the kept spec instances it could only arrange for display — no stated placement, no kept
    relation positions them, not the lone child of its parent (that one sits at the parent origin). ``{"pending": [...],
    "kept": [...], "at_origin": [...], "mated": [...], "relations": [...]}``: instance keys in spec order (``at_origin``:
    lone children at their parent's origin; ``mated``: positioned by a kept relation) and the kept relations as they
    would be mated (generation supplement G: the level-by-level layout solves them to size the sub-assemblies). The same
    plan as the build itself; nothing is written. Standard: nothing pending (those instances stay missing until the spec
    says where they go).

    Generation reliability round 2 (P): the anchor of each floating group (siblings joined only to each other by
    relations, none anchored to the parent frame) is pending too — once the AI places it, the relations carry the rest
    of its group; ``floating``: those groups ({parent, anchor, members}); ``at_origin`` also holds the anchor of a group
    that is its parent's whole content (it defines the frame).

    Round 3 (B): a relation to an assembly frame (``@<key>``) counts when the frame can be reached at all; its datums
    are mapped where the positions above are known (``assembly_placement`` solves it level by level, the build with the
    positions the placement found). Whether the kept relations actually POSITION an instance (no free translation) is
    known only from the solve: the placement layout solves them and takes up the ones they leave free. ``choices``
    (Turbo, review of round 3): spec relations joining one axis to a pattern of several — the placement step picks the
    axis."""
    policy = pol.parse_policy(policy)
    defs = {d["key"]: d for d in spec.get("definitions") or [] if d.get("key")}
    tree_part = spec_tree(spec)
    reach: set[str] = tree_part["definitions"]
    asm_keys = {tree_part["root"]} | {k for k, d in defs.items() if d.get("kind") == "assembly" and k in reach}
    part_bind = {k: _definition_in(db, None, {"kind": "part", **b})["model_revision_id"] for k, b in bindings.items()
                 if b and k in reach and k in defs and defs[k].get("kind") != "assembly"}
    mapped, _, _, rel_problems, choices = _spec_relations(db, tree_part, part_bind, asm_keys, policy, partial=True, spec=spec)
    plan = _partial_plan(spec, tree_part, set(part_bind), turbo=pol.is_turbo(policy), relations={str(r.get("key")): r for r in mapped},
                         rel_problems=rel_problems, blocked_instances={}, blocked_relations={},
                         causes={str(m.get("key")): m for m in missing or [] if isinstance(m, dict)})
    order = [i["key"] for i in tree_part["instances"]]
    kept = [i["key"] for i in plan["instances"]]
    ends = {e for r in plan["relations"] for e in relation_ends(r)}
    anchors = {g["anchor"] for g in plan["floating"]} if pol.is_turbo(policy) else set()
    deferred = {c["instance"] for c in choices if c.get("deferred_binding") and c["relation"] in rel_problems}
    return {"pending": [k for k in order if k in plan["arranged"] or k in anchors or k in deferred], "kept": kept,
            "at_origin": [k for k in kept if k in plan["at_origin"]], "mated": [k for k in kept if k in ends],
            "relations": list(plan["relations"]), "floating": list(plan["floating"]) if anchors else [],
            "choices": [c for c in choices if c["instance"] in set(kept)] if pol.is_turbo(policy) else []}


def _blocked_keys(spec: dict[str, Any], tree_part: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Standard: (instance key → open question ids, relation key → question ids) blocked under the root (W2 readiness)."""
    from .engineering_spec import readiness

    rd = readiness(spec, "standard")
    inst_keys = {i["key"] for i in tree_part["instances"]}
    rel_keys = {r.get("key") for r in tree_part["relations"]}
    insts = {k: list(v["questions"]) for k, v in rd["instances"].items() if k in inst_keys and v["state"] == "blocked"}
    rels = {k: list(v["questions"]) for k, v in rd["relations"].items() if k in rel_keys and v["state"] == "blocked"}
    return insts, rels


def target_order(spec: dict[str, Any]) -> list[str]:
    """Leaf part definitions of the spec's full target in first-occurrence order (the flattened BOM order of the complete
    assembly: children in spec order, each sub-assembly expanded where it occurs)."""
    tree_part = spec_tree(spec)
    defs = {d["key"]: d for d in spec.get("definitions") or [] if d.get("key")}
    children: dict[str, list[dict[str, Any]]] = {}
    for inst in tree_part["instances"]:
        children.setdefault(inst.get("parent") or tree_part["root"], []).append(inst)
    order: list[str] = []

    def walk(key: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for inst in children.get(key, []):
            dk = inst.get("definition")
            if (defs.get(dk) or {}).get("kind") == "assembly":
                walk(dk, depth + 1)
            elif dk and dk not in order:
                order.append(dk)

    walk(tree_part["root"], 0)
    return order


def _partial_plan(spec: dict[str, Any], tree_part: dict[str, Any], available: set[str], *, turbo: bool,
                  relations: dict[str, dict[str, Any]], rel_problems: dict[str, dict[str, Any]],
                  blocked_instances: dict[str, list[str]], blocked_relations: dict[str, list[str]],
                  causes: dict[str, dict[str, Any]], decided: set[str] | frozenset[str] = frozenset(),
                  basis_kinds: dict[str, str] | None = None, located: set[str] | None = None) -> dict[str, Any]:
    """What a partial assembly keeps of the spec (generation supplement B-01 / B-02, design §3 B.1–2). Pure.

    ``available``: part definitions with geometry; ``relations``: relation key → the relation as it would be mated
    (datums mapped); ``rel_problems``: relation key → why it cannot be mated as stated; ``blocked_*`` (standard): what open
    questions block; ``causes``: definition key → its missing item (why it has no geometry).

    Instances of a definition without geometry are dropped; sub-assemblies left empty are dropped level by level;
    relations touching a dropped instance are dropped. An instance with neither a placement nor a kept relation is
    arranged for display in Turbo (a rule decision, counted ``arranged``) and dropped as a ``placement`` item in standard
    — except the only child of its parent, which sits at the parent's origin. Repeats until nothing changes. Every
    dropped object is one missing item (:func:`app.services.spec_coverage.missing_item`). ``decided``: instance keys
    whose placement an AI decision gave (generation supplement F); their installed leaves are counted in
    ``decided_counts`` too (a subset of ``placed``).

    Generation reliability §2.6 (package L): the leaves are counted along the whole parent chain
    (:func:`app.services.spec_coverage.chain_placement`): ``placed`` only when every sub-assembly instance above is
    positioned too, ``local_only`` when positioned only inside an unplaced (arranged) sub-assembly instance,
    ``arranged_counts`` when without a position of their own; ``subassemblies`` lists the kept sub-assembly instances
    without a position (each one becomes a ``placement`` item in the coverage) and ``positioned`` says how each kept
    instance got its position (stated / mate / ai / rule). A standard placement item of a dropped sub-assembly instance
    says how many part instances under it are not installed (``descendants``).

    Generation reliability round 2 (package P, WP06; :func:`_relation_groups`): siblings joined by relations with none
    anchored to the parent frame are a FLOATING group — counted ``local_only`` and listed as one ``placement`` item
    (``floating_group``, keyed by the group's anchor); a group that is its parent's whole content defines the frame
    instead (its anchor joins ``at_origin``, a rule). ``basis`` says what each kept position rests on (stated / derived
    / estimated — ``basis_kinds``: the AI decisions' own kinds), ``estimated_counts`` the installed leaves with an
    estimate on their path, ``floating`` the floating groups ({parent, anchor, members, free}).

    Review of round 3 (connected is not positioned): ``located`` — the related instances the build's solve positioned
    in their parent frame (:func:`create_from_spec` passes it once every level is solved; None before any solve, e.g.
    :func:`placement_gaps`, where the connections stand in for it). A related instance the solve leaves free to move
    counts local only, like a floating group."""
    from .engineering_spec import quantities
    from .spec_coverage import chain_placement, descendant_instances, missing_item

    defs = {d["key"]: d for d in spec.get("definitions") or [] if d.get("key")}
    root = tree_part["root"]
    insts = {i["key"]: i for i in tree_part["instances"]}
    order = [i["key"] for i in tree_part["instances"]]
    rel_list = [r for r in tree_part["relations"] if r.get("key")]
    qty = quantities(spec)
    sib: dict[str, int] = {}
    for i in insts.values():
        sib[i.get("parent") or root] = sib.get(i.get("parent") or root, 0) + 1

    def mult(parent: str) -> int:
        return 1 if parent == root else int((qty.get(parent) or {}).get("total") or 0)

    def is_asm(dk: str | None) -> bool:
        return (defs.get(dk or "") or {}).get("kind") == "assembly"

    def name_of(key: str) -> str:
        i = insts.get(key)
        d = defs.get((i or {}).get("definition") or key) or {}
        return str((i or {}).get("name") or d.get("name") or key)

    dropped: dict[str, dict[str, Any]] = {}  # instance key → {code, recovery, reason}
    items: list[dict[str, Any]] = []
    for k in order:
        i = insts[k]
        dk = i.get("definition")
        if not is_asm(dk) and dk not in available:
            c = causes.get(dk) or {}
            dropped[k] = {"code": "definition_missing", "recovery": c.get("recovery") or "retry_definition",
                          "reason": f"{dk} has no geometry ({c.get('code') or 'not built'})"}
        elif k in blocked_instances:
            q = blocked_instances[k]
            dropped[k] = {"code": "spec_questions_open", "recovery": "answer_questions", "reason": f"open questions {', '.join(q)}"}
            items.append(missing_item(stage="placement", key=k, name=name_of(k), kind=str((defs.get(dk) or {}).get("kind") or "part"),
                                      code="spec_questions_open", reason=f"open spec questions block this instance: {', '.join(q)}",
                                      quantity=mult(i.get("parent") or root), instances=[k], recovery="answer_questions",
                                      definition=dk, questions=q))
    rel_dropped: dict[str, dict[str, Any]] = {}
    for r in rel_list:
        if r["key"] in blocked_relations:
            rel_dropped[r["key"]] = {"code": "spec_questions_open", "recovery": "answer_questions",
                                     "reason": f"open spec questions block this relation: {', '.join(blocked_relations[r['key']])}"}
        elif r["key"] in rel_problems:
            rel_dropped[r["key"]] = dict(rel_problems[r["key"]])
    arranged: set[str] = set()
    at_origin: set[str] = set()
    changed = True
    while changed:
        changed = False
        kept = [k for k in order if k not in dropped]
        # reachability from the root through kept instances (a dropped sub-assembly takes its children with it)
        reach = {root}
        grow = True
        while grow:
            grow = False
            for k in kept:
                i = insts[k]
                if (i.get("parent") or root) in reach and is_asm(i.get("definition")) and i["definition"] not in reach:
                    reach.add(i["definition"])
                    grow = True
        for k in kept:
            if (insts[k].get("parent") or root) not in reach:
                dropped[k] = {"code": "parent_dropped", "recovery": None, "reason": "its parent assembly is not in the assembly"}
                changed = True
        kept = [k for k in order if k not in dropped]
        # sub-assemblies left without a kept child
        has_child = {(insts[k].get("parent") or root) for k in kept}
        for k in kept:
            dk = insts[k].get("definition")
            if is_asm(dk) and dk not in has_child:
                kids = [x for x in order if (insts[x].get("parent") or root) == dk]
                rec = next((dropped[x]["recovery"] for x in kids if x in dropped and dropped[x].get("recovery") == "retry_definition"), None) \
                    or next((dropped[x]["recovery"] for x in kids if x in dropped and dropped[x].get("recovery")), "fix_spec")
                dropped[k] = {"code": "subassembly_empty", "recovery": rec, "reason": f"{dk} has no part left"}
                changed = True
        kept = [k for k in order if k not in dropped]
        kept_set = set(kept)
        # relations: both ends kept and mateable as stated
        for r in rel_list:
            if r["key"] in rel_dropped:
                continue
            ends = relation_ends(r)
            gone = [e for e in ends if e and e not in kept_set]
            if gone:
                cause = dropped.get(gone[0]) or {}
                rel_dropped[r["key"]] = {"code": "relation_dropped", "recovery": cause.get("recovery") or "fix_spec",
                                         "reason": f"joins {', '.join(gone)}, which is not in the assembly ({cause.get('reason') or 'dropped'})"}
                changed = True
        live = [r for r in rel_list if r["key"] not in rel_dropped and r["key"] in relations]
        mated = {e for r in live for e in relation_ends(r)}
        # instances without a pose: the lone child sits at its parent's origin; Turbo arranges the others for display,
        # standard lists them missing (placement)
        for k in kept:
            i = insts[k]
            if i.get("placement") or k in mated or k in arranged or k in at_origin:
                continue
            if sib.get(i.get("parent") or root, 0) == 1:
                at_origin.add(k)
                continue
            lost = [r["key"] for r in rel_list if r["key"] in rel_dropped and k in ((r.get("a") or {}).get("instance"),
                                                                                    (r.get("b") or {}).get("instance") if r.get("b") else None)]
            if turbo:
                arranged.add(k)
                continue
            rec = next((rel_dropped[x]["recovery"] for x in lost if rel_dropped[x].get("recovery")), None) or "fix_spec"
            dropped[k] = {"code": "placement_lost" if lost else "placement_missing", "recovery": rec,
                          "reason": (f"positioned only by {', '.join(lost)}, which cannot be applied" if lost
                                     else "the spec gives neither a placement nor a relation")}
            changed = True
    # missing items of what the plan dropped (definitions without geometry are listed by the caller)
    for k in order:
        d = dropped.get(k)
        i = insts[k]
        dk = i.get("definition")
        if d is None or d["code"] in ("definition_missing", "parent_dropped", "spec_questions_open"):
            continue
        if d["code"] == "subassembly_empty":
            if any(m.get("key") == dk and m.get("code") == "subassembly_empty" for m in items):
                continue
            all_insts = [x for x in order if insts[x].get("definition") == dk]
            items.append(missing_item(stage="assembly", key=dk, name=str((defs.get(dk) or {}).get("name") or dk), kind="assembly",
                                      code="subassembly_empty", reason="every part of this sub-assembly is missing; it was left out",
                                      quantity=int((qty.get(dk) or {}).get("total") or len(all_insts)), instances=all_insts,
                                      recovery=d["recovery"]))
            continue
        below = {"descendants": descendant_instances(spec, str(dk))} if is_asm(dk) else {}  # generation reliability §2.6
        items.append(missing_item(stage="placement", key=k, name=name_of(k), kind=str((defs.get(dk) or {}).get("kind") or "part"),
                                  code=d["code"], reason=d["reason"] + (f"; its {below['descendants']} part instance(s) are not installed"
                                                                        if below else ""),
                                  quantity=mult(i.get("parent") or root), instances=[k], recovery=d["recovery"], definition=dk, **below))
    for r in rel_list:
        d = rel_dropped.get(r["key"])
        if d is None or r.get("source") == "ai_turbo":  # an AI placement mate is no spec object to miss
            continue
        ends = relation_ends(r)
        items.append(missing_item(stage="assembly", key=r["key"], name=f"{r.get('kind')} {' – '.join(ends)}", kind="relation",
                                  code=d["code"], reason=d["reason"], quantity=0, instances=ends, recovery=d.get("recovery"),
                                  **({"question": d["question"]} if d.get("question") else {})))
    kept = [k for k in order if k not in dropped]
    kept_set = set(kept)
    kept_rels = [relations[r["key"]] for r in rel_list if r["key"] in relations and r["key"] not in rel_dropped
                 and all(e in kept_set for e in relation_ends(r))]
    floating, groups = _relation_groups(insts, kept, kept_rels, at_origin, rank={k: n for n, k in enumerate(order)}, root=root,
                                        located=located)
    for g in groups:  # generation reliability P: a floating group is a placement gap of its own
        if not g["floating"]:
            continue
        anchor, members, parent = g["anchor"], g["free"], g["parent"]
        dk0 = insts[anchor].get("definition")
        said = f"{len(members)} instance(s) ({', '.join(members[:8])}{' …' if len(members) > 8 else ''})"
        items.append(missing_item(stage="placement", key=anchor, name=name_of(anchor), kind=str((defs.get(dk0) or {}).get("kind") or "part"),
                                  code="floating_group",
                                  reason=(f"{said} are positioned only relative to each other by the relations; none of them is anchored to "
                                          f"{parent} (a stated position, an AI decision or a grounding relation)" if g.get("relative")
                                          else f"{said} are related to siblings or to the frame of {parent}, but the relations leave a "
                                               f"translation free (connected, not positioned)")
                                         + f": they count placed only locally until {anchor} is fixed in {parent}",
                                  quantity=len(members) * mult(parent), instances=members, recovery="fix_spec", definition=dk0, parent=parent))
    # generation reliability §2.6: installed / local only / arranged leaf instances per part definition along the whole
    # parent chain (every path through the kept tree); how each kept instance has its position in its parent
    ends = {e for r in kept_rels for e in relation_ends(r)}
    positioned: dict[str, str] = {}
    for k in kept:
        if k in arranged:
            continue
        positioned[k] = ("ai" if k in decided else "stated" if insts[k].get("placement") else "rule" if k in at_origin
                         else "mate" if k in ends else "stated")
    basis = _basis_kinds(positioned, kept_rels, basis_kinds or {})
    chain = chain_placement(spec, kept=kept, positioned=positioned, decided=decided, floating=floating,
                            estimated={k for k, b in basis.items() if b == "estimated"})
    return {"instances": [insts[k] for k in kept], "relations": kept_rels,
            "arranged": arranged, "at_origin": at_origin, "items": items, "placed": chain["placed"], "arranged_counts": chain["arranged"],
            "decided_counts": chain["decided"], "local_only": chain["local_only"], "subassemblies": chain["subassemblies"],
            "positioned": positioned, "definitions": {root} | {insts[k]["definition"] for k in kept},
            "basis": basis, "estimated_counts": chain["estimated"], "floating": [g for g in groups if g["floating"]], "groups": groups}


def _ai_basis_kind(p: dict[str, Any]) -> str:
    """What an AI placement rests on (generation reliability P): its own ``basis_kind``, ``estimated`` when it gives none
    (an older decision, a non-strict reply) — never claimed as a source fact by default."""
    from .spec_coverage import BASIS_KINDS

    kind = p.get("basis_kind") if isinstance(p, dict) else None
    return kind if kind in BASIS_KINDS else "estimated"


def _relation_groups(insts: dict[str, dict[str, Any]], kept: list[str], rels: list[dict[str, Any]], at_origin: set[str], *,
                     rank: dict[str, int], root: str, located: set[str] | None = None) -> tuple[set[str], list[dict[str, Any]]]:
    """Generation reliability round 2 (package P, WP06): the groups of kept siblings joined by the kept two-ended
    relations (:func:`app.cad.mates.relation_groups`), each ``{parent, anchor, members, floating, free}``. A group is
    anchored when a member has a position in the parent frame — stated or AI-decided (``placement``), the lone child at
    the origin, or a one-ended relation / a relation to an assembly frame connecting it. An unanchored group that holds
    every kept child of its parent defines that frame: its anchor (the first member no relation moves) sits at the
    parent's origin by rule (added to ``at_origin``, like the lone child). Any other unanchored group is FLOATING:
    positioned only relative to each other, never installed (its members are returned as the first value; Turbo asks
    the AI to place its anchor). Changes ``at_origin`` in place. Pure.

    ``located`` (review of round 3: connected is not positioned): the related instances the build's solve positions in
    their parent frame (:func:`app.cad.mates.grounded` — no free translation left). Given, it decides instead of the
    connections: a related instance without a stated pose or the rule origin that the solve leaves free counts local
    only (``free``) — a member of a floating group, a part only ``parallel`` to a frame, a slide along an axis — and so
    does its group (a related instance in no two-ended group is a group of its own)."""
    from ..cad.mates import relation_groups

    def parent(k: str) -> str:
        return insts[k].get("parent") or root

    # one-ended relations and relations to an assembly frame (round 3, B: ``@<key>``) connect their instance to the frame
    grounded = {(r.get("a") or {}).get("instance") for r in rels if not r.get("b") or is_frame_ref(r.get("b"))}
    moving = _moving(rels)
    kids: dict[str, set[str]] = {}
    for k in kept:
        kids.setdefault(parent(k), set()).add(k)
    out: list[dict[str, Any]] = []
    for g in relation_groups([tuple(relation_ends(r)) for r in rels if len(relation_ends(r)) == 2]):  # type: ignore[misc]
        members = sorted(g, key=lambda k: rank.get(k, 0))
        anchor = next((m for m in members if m not in moving), members[0])
        entry = {"parent": parent(anchor), "anchor": anchor, "members": members, "floating": False, "free": []}
        out.append(entry)
        if any(insts[m].get("placement") or m in at_origin or m in grounded for m in members):
            continue
        if set(members) == kids.get(parent(anchor), set()):
            at_origin.add(anchor)  # the group is its parent's whole content: it defines the frame (rule)
            continue
        entry.update(floating=True, free=list(members), relative=True)  # positioned only relative to each other
    if located is not None:  # the solve decides what the relations position
        related = {e for r in rels for e in relation_ends(r)}
        free = {k for k in related if not insts[k].get("placement") and k not in at_origin and k not in located}
        grouped = {m for e in out for m in e["members"]}
        out += [{"parent": parent(k), "anchor": k, "members": [k], "floating": True, "free": [k]}
                for k in sorted(free - grouped, key=lambda k: rank.get(k, 0))]
        for e in out:
            e["free"] = [m for m in e["members"] if m in free]
            e["floating"] = bool(e["free"])
            e["relative"] = bool(e.get("relative")) and len(e["free"]) == len(e["members"])
            if e["floating"] and e["anchor"] not in free:
                e["anchor"] = e["free"][0]
    return {m for e in out if e["floating"] for m in e["free"]}, out


def _moving(rels: list[dict[str, Any]]) -> set[str]:
    """The instances the relations move: the ``b`` side of a spec relation (W0 §5), the ``a`` side of an AI placement
    mate (the instance it places; its ``b`` is the reference — an assembly frame or a sibling). A spec relation to an
    assembly frame moves neither side; nor does one to a sub-assembly's interface taken through its instance (``via``)."""
    out: set[str] = set()
    for r in rels:
        ref = r.get("a") if r.get("source") == "ai_turbo" else None if r.get("via") else r.get("b")
        if isinstance(ref, dict) and ref.get("instance") and not is_frame_ref(ref):
            out.add(str(ref["instance"]))
    return out


def _depends(rels: list[dict[str, Any]], positioned: dict[str, str]) -> dict[str, set[str]]:
    """Which instance's position rests on which (round 3, B): an AI mate places its ``a`` relative to its ``b``; a spec
    relation moves the end that is not fixed — its ``b`` unless that one is stated / at the origin — relative to the
    other. Instance → the instances it rests on directly."""
    fixed = ("stated", "rule")
    deps: dict[str, set[str]] = {}
    for r in rels:
        ends = relation_ends(r)
        if len(ends) != 2:
            continue
        a, b = ends
        if r.get("source") == "ai_turbo":
            x, y = a, b
        elif r.get("via"):  # to a sub-assembly's interface: the instance rests on it unless it is the fixed one of the two
            x, y = (b, a) if positioned.get(a) in fixed and positioned.get(b) not in fixed else (a, b)
        else:
            x, y = (a, b) if positioned.get(b) in fixed and positioned.get(a) not in fixed else (b, a)
        deps.setdefault(x, set()).add(y)
    return deps


def _dependents(rels: list[dict[str, Any]], positioned: dict[str, str]) -> dict[str, list[str]]:
    """Instance → every instance whose position rests on it, directly or through others (:func:`_depends`)."""
    up: dict[str, set[str]] = {}
    for x, ys in _depends(rels, positioned).items():
        for y in ys:
            up.setdefault(y, set()).add(x)
    out: dict[str, list[str]] = {}
    for k in up:
        seen: list[str] = []
        todo = sorted(up[k])
        while todo:
            x = todo.pop(0)
            if x != k and x not in seen:
                seen.append(x)
                todo += sorted(up.get(x, ()))
        out[k] = seen
    return out


def _basis_kinds(positioned: dict[str, str], rels: list[dict[str, Any]], ai: dict[str, str]) -> dict[str, str]:
    """Generation reliability round 2 (P) / round 3 (B): what each kept instance's position in its parent rests on
    (``spec_coverage.BASIS_KINDS``): a spec placement ``stated``; a frame rule or relations ``derived``; an AI decision
    its own ``basis_kind`` (``estimated`` when it gives none; AI-mated: the weakest of its mates). A position that DEPENDS
    on an estimate is an estimate too: an AI mate places its ``a`` relative to its ``b``; a spec relation moves the end
    that is not fixed (its ``b`` unless that one is stated / at the origin) relative to the other — followed to the end."""
    from .spec_coverage import BASIS_KINDS

    out = {k: ((ai.get(k) if ai.get(k) in BASIS_KINDS else "estimated") if how == "ai" else "stated" if how == "stated" else "derived")
           for k, how in positioned.items()}
    fixed = ("stated", "rule")
    deps = _depends(rels, positioned)
    changed = True
    while changed:
        changed = False
        for x, ys in deps.items():
            if x in out and out[x] != "estimated" and positioned.get(x) not in fixed and any(out.get(y) == "estimated" for y in ys):
                out[x] = "estimated"
                changed = True
    return out


def create_from_spec(db: Session, *, project_id: uuid.UUID, user: User, spec: dict[str, Any], bindings: dict[str, dict[str, Any]],
                     policy: str | None = None, spec_version_id: uuid.UUID | str | None = None, number: str | None = None,
                     node_run_id: uuid.UUID | None = None, source: str = "manual",
                     spec_meta: dict[str, Any] | None = None, partial: bool = False, missing: list[dict[str, Any]] | None = None,
                     continue_from: AssemblyRevision | None = None, placement: dict[str, Any] | None = None,
                     build_notes: list[dict[str, Any]] | None = None) -> tuple[Assembly, AssemblyRevision, dict[str, Any]]:
    """Build the assembly tree of a spec (W0 §5). Internal: the public API and W8 go through
    :func:`create_from_spec_version` (content read from the stored version); tests may pass content directly.

    Only definitions under the root are built: part / standard-part definitions are bound to existing part revisions
    through ``bindings[def_key] = {"kind": "part", "revision_id" | "part_id"}`` (W8 builds them first; W3 templates
    are used when ``app.cad.templates`` exists and the definition names a template); definitions with no instance
    under the root are reported (``_unplaced``), never required. Standard refuses (422 ``spec_blocked``) while an open
    question blocks a branch under the root. Assembly definitions become assemblies (one revision each, reused by every
    instance). Relations become mates of the parent that owns both instances, with datums mapped by
    :func:`_map_relations`. Spec placements are explicit; an instance with a placement and no mate pulling it is fixed.
    Part BOM attributes are only filled, never overwritten (:func:`_spec_props`). Returns (root assembly, root
    revision, {definition key: created id, "_unplaced": [definition keys not under the root]}).

    ``partial`` (W8, generation supplement B, design §3 B.1–2): a definition without a binding is not refused; the
    assembly is made of what was built (:func:`_partial_plan`): its instances, the sub-assemblies left empty and the
    relations touching them are dropped, and so are (standard) the instances and relations open questions block or
    that cannot be mated as stated. ``missing``: the caller's missing items of the definitions it could not build
    (their reason and recovery). The coverage block (:mod:`app.services.spec_coverage`) and the spec's full target BOM
    are stored on the root revision (``spec_ref.coverage`` / ``spec_ref.target_bom``) and returned as ``_coverage``.
    422 ``nothing_placeable`` (with the coverage) when no instance can be placed. ``continue_from``: an earlier partial
    root revision of the same build (S3 continue): every assembly of it that nobody changed since gets a new revision
    instead of a new assembly (same instance ids per spec instance); a build identical to it returns it unchanged
    (``_reused``).

    ``placement`` (Turbo; generation reliability round 3, B — the AI placement decision of
    ``services/assembly_placement``; it replaced supplement F's numeric positions): ``{"mates": [...], "fixed": {...},
    "poses": {...}}``.

    - ``mates``: AI placement MATES — relations ``{key, kind, a: {instance, datum}, b: {instance | "@<assembly>",
      datum, frame?}, params, priority, source: "ai_turbo", decision: {trigger, basis_kind, basis, alternatives,
      affected, sources}}`` joining an instance the spec leaves without a position (``a``) to a sibling or an assembly
      frame (``b``). They become mates of the parent assembly (source ``ai_turbo``, lower priority than the spec's
      relations) and the solver computes the positions (a side on a frame carries its datums, ``frame``); each one is an
      ``ai_turbo`` decision on the root revision (field ``mate``). An entry with ``dropped`` is a mate the placement's
      solve dropped as conflicting: only its decision is recorded (adopted ``dropped``, the solver's reason).
    - ``fixed``: spec instance key → ``{translate, rotate, basis_kind: stated, trigger, basis, sources, …}`` — an
      instance position a source sentence states (a ``fixed`` mate with ``at``): applied as its placement (one decision,
      field ``placement``).
    - ``poses``: instance key → ``{translate, rotate}`` the placement found (solved) — used only to map an assembly frame
      above a parent into it for spec relations on ``@<assembly>`` (:func:`frame_datums`).

    Applied only to instances without a stated placement (the stored spec is never changed). An instance a kept AI mate
    or fixed position places is installed when the mates tie it to its parent frame (``placed``, also counted in
    ``coverage.decided``; floating otherwise, P); the instances left without one are arranged for display by rule as
    before. ``build_notes``: notes of the build (why a placement was not decided) added to the root revision's notes.

    Review of round 3 (connected is not positioned): a partial build counts an instance its relations or mates join as
    positioned only when the SOLVE positions it — no free translation left (:func:`app.cad.mates.grounded`; a held turn
    about an axis is fine): every level below the root is solved when it is stored and the root level just before, so
    the coverage on the root revision (``_partial_plan(located=…)``) counts what the solves position; one they leave
    free to move counts local only (``floating_group``).

    Generation reliability round 2 (P, WP06): each AI decision carries ``basis_kind`` (stated / derived / estimated),
    ``alternatives`` and the other instances it affects (the ones the reply names, and the relation group an anchor
    carries); every kept component with a position records ``meta.basis_kind`` (an AI-mated instance: the weakest kind
    of its mates); the coverage counts the installed instances at an estimated position (``estimated``) and lists
    floating groups (``floating_group``)."""
    from .spec_coverage import coverage as coverage_of

    try:
        policy = pol.parse_policy(policy)
    except ValueError as exc:
        raise bad_request("invalid_execution_policy", str(exc))
    turbo = pol.is_turbo(policy)
    design_contract = (spec_meta or {}).get("design")
    if spec.get("design") and not design_contract:
        raise AppError(409, "design_authority_missing", "design assemblies require a server-resolved compiled specification")
    defs = {d["key"]: d for d in spec.get("definitions") or [] if d.get("key")}
    tree_part = spec_tree(spec)
    ai_placed: dict[str, dict[str, Any]] = {}  # instance key → the stated position an AI decision applied (Turbo)
    ai_rels: list[dict[str, Any]] = []  # AI placement mates kept as relations (Turbo, round 3 B)
    ai_dropped: list[dict[str, Any]] = []  # AI placement mates the placement's solve dropped (decisions only)
    poses: dict[str, am.Matrix4] = {}
    if placement and turbo:
        fixed = placement.get("fixed") or {}
        own: list[dict[str, Any]] = []
        for inst in tree_part["instances"]:
            p = fixed.get(inst["key"])
            if p and not inst.get("placement"):  # a stated placement always wins
                ai_placed[inst["key"]] = p
                inst = {**inst, "placement": {"translate": [float(x) for x in p["translate"]], "rotate": [float(x) for x in p["rotate"]]}}
            own.append(inst)
        open_keys = {i["key"] for i in own if not i.get("placement")}
        siblings = {i["key"]: i.get("parent") or tree_part["root"] for i in own}
        for m in placement.get("mates") or []:
            a, b = (m.get("a") or {}).get("instance"), m.get("b") or {}
            if a not in open_keys or not (is_frame_ref(b) or siblings.get(b.get("instance")) == siblings.get(a)):
                continue  # only an instance without a stated position, joined to a sibling or a frame
            (ai_dropped if m.get("dropped") else ai_rels).append({**m, "source": "ai_turbo"})
        for k, p in (placement.get("poses") or {}).items():
            t = _placement_matrix(p)
            if t is not None:
                poses[str(k)] = t
        tree_part = {**tree_part, "instances": own, "relations": list(tree_part["relations"]) + ai_rels}
    root_key = tree_part["root"]
    reach: set[str] = tree_part["definitions"]
    instances = tree_part["instances"]
    asm_keys = {root_key} | {k for k, d in defs.items() if d.get("kind") == "assembly" and k in reach}
    unknown = [i["key"] for i in instances if i.get("definition") not in defs and i.get("definition") not in asm_keys]
    if unknown:
        raise AppError(422, "spec_unknown_definition", "spec instances refer to undefined definitions", {"instances": unknown})
    if not turbo and not partial:
        _refuse_blocked(spec, tree_part)
    spec_sha = _canon(spec)
    spec_ref_base: dict[str, Any] = {"spec_version_id": str(spec_version_id) if spec_version_id else None, "sha256": spec_sha,
                                     "root": root_key, "simulated": bool((spec_meta or {}).get("simulated"))}
    if spec_meta:
        spec_ref_base["spec"] = spec_meta
    assumptions = [a for a in spec.get("assumptions") or [] if isinstance(a, dict)]
    created: dict[str, Any] = {}
    part_bind: dict[str, uuid.UUID] = {}
    unbound: list[str] = []
    for key, d in defs.items():
        if d.get("kind") == "assembly" or key not in reach:
            continue
        b = bindings.get(key)
        if b:
            rid = _definition_in(db, None, {"kind": "part", **b})["model_revision_id"]
            if design_contract:
                revision = db.get(ModelRevision, rid)
                provenance = (revision.checks or {}).get("design") if revision else None
                if (not provenance or provenance.get("design_ref") != design_contract["ref"]
                        or provenance.get("definition_key") != key
                        or provenance.get("requirements_sha256") != design_contract["requirements_sha256"].get(key)):
                    raise AppError(409, "design_part_mismatch", "bound geometry does not belong to this exact design", {"definition": key})
            part_bind[key] = rid
            continue
        rid = None if partial else _template_part(db, project_id, user, d)  # partial: the caller already tried
        if rid is None:
            unbound.append(key)
        else:
            part_bind[key] = rid
    if unbound and not partial:
        raise AppError(422, "definition_unbound", "spec definitions have no built part revision (bind them or build them first)",
                       {"definitions": unbound})
    notes: list[dict[str, Any]] = []
    for key, rid in part_bind.items():
        rev = db.get(ModelRevision, rid)
        part = db.get(Part, rev.part_id) if rev else None
        if part is None or part.project_id != project_id:
            raise AppError(422, "definition_not_found", f"binding of {key} is not a part revision of this project", {"definition": key})
        note = _spec_props(db, part, user, defs[key], key, spec_ref_base["spec_version_id"])
        if note is not None:
            notes.append(note)
        created[key] = str(rid)
    parent_of = {i["key"]: i.get("parent") or root_key for i in instances}
    sib_count: dict[str, int] = {}
    for i in instances:
        sib_count[parent_of[i["key"]]] = sib_count.get(parent_of[i["key"]], 0) + 1
    rel_touch = {e for r in tree_part["relations"] for e in relation_ends(r)}
    by_key = {i["key"]: i for i in instances}

    def pose_of(k: str) -> am.Matrix4 | None:  # where an instance sits in its parent, as far as known before the solve
        i = by_key.get(k) or {}
        if i.get("placement"):
            return _placement_matrix(i["placement"])
        if k in poses:
            return poses[k]
        return am.IDENTITY if sib_count.get(parent_of.get(k, ""), 0) == 1 and k not in rel_touch else None

    mapped, rel_assumptions, cross_assumptions, rel_problems, choices = _spec_relations(db, tree_part, part_bind, asm_keys, policy, partial=partial,
                                                                                  spec=spec, pose_of=pose_of)
    if design_contract:
        spec_ref_base["design"] = {**design_contract, "mapped_relations": list(mapped)}
    assumptions += cross_assumptions
    # round 3 (B): what each AI-mated instance rests on — the weakest kind of the AI mates that place it: the instance a
    # mate places (``a``), and an instance only an AI mate positions as its reference (``b``: no stated position, no spec
    # relation — without the mate it would only be arranged)
    ai_kind: dict[str, str] = {k: _ai_basis_kind(p) for k, p in ai_placed.items()}
    rank = {"stated": 0, "derived": 1, "estimated": 2}
    spec_ends = {e for r in tree_part["relations"] if r.get("source") != "ai_turbo" for e in relation_ends(r)}
    own = {str(r["a"]["instance"]) for r in ai_rels}  # instances with mates of their own
    for r in ai_rels:
        kind = _ai_basis_kind(r.get("decision") or {})
        for k in relation_ends(r):
            if k != r["a"]["instance"] and (k in own or (by_key.get(k) or {}).get("placement") or k in spec_ends):
                continue
            if k not in ai_kind or rank[kind] > rank[ai_kind[k]]:
                ai_kind[k] = kind
    plan: dict[str, Any] | None = None
    plan_args: dict[str, Any] = {}
    if partial:
        blocked_i, blocked_r = _blocked_keys(spec, tree_part) if not turbo else ({}, {})
        causes = {str(m.get("key")): m for m in missing or [] if isinstance(m, dict)}
        plan_args = {"turbo": turbo, "relations": {str(r.get("key")): r for r in mapped}, "rel_problems": rel_problems,
                     "blocked_instances": blocked_i, "blocked_relations": blocked_r, "causes": causes, "decided": set(ai_kind),
                     "basis_kinds": ai_kind}
        plan = _partial_plan(spec, tree_part, set(part_bind), **plan_args)
        instances = plan["instances"]
        mapped = plan["relations"]
        kept_rels = {str(r.get("key")) for r in mapped}
        rel_assumptions = [a for a in rel_assumptions if str(a.get("subject")) in kept_rels or a.get("adopted") == "not applied"]
        asm_keys = {k for k in asm_keys if k in plan["definitions"]}
    children: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        children.setdefault(inst.get("parent") or root_key, []).append(inst)

    def counted(pl: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], bool, list[dict[str, Any]]]:
        """(coverage, target BOM, assembled, missing items) of a plan."""
        items = [m for m in missing or [] if isinstance(m, dict)] + pl["items"]
        built_keys = {k for k in part_bind if k in reach}
        # Turbo: instances only arranged by rule (no placement, no relation — e.g. a spec the A-03 repair hung under the
        # root) still make an assembly; it is a partial one (arranged is never installed), never "nothing placeable".
        # Round 2 (P): so does a floating relation group in standard (its relative positions are spec facts; local only).
        made = bool(pl["placed"]) or bool(pl["local_only"]) or (turbo and bool(pl["arranged_counts"]))
        cv = coverage_of(spec, built=built_keys, placed=pl["placed"], arranged=pl["arranged_counts"], missing=items,
                         assembled=made, decided=pl["decided_counts"], local_only=pl["local_only"],
                         subassemblies=pl["subassemblies"], estimated=pl["estimated_counts"])
        qty = _target_quantities(spec)
        bom: list[dict[str, Any]] = []
        for n, key in enumerate(target_order(spec), 1):
            d = defs.get(key) or {}
            got, shown, local = pl["placed"].get(key, 0), pl["arranged_counts"].get(key, 0), pl["local_only"].get(key, 0)
            held = got + shown + local  # in the revision (installed, placed only inside an unplaced sub-assembly, or arranged)
            why = next((m for m in items if m.get("key") == key or m.get("definition") == key), None)
            bom.append({"item": str(n), "key": key, "name": d.get("name") or key, "number": d.get("number"), "kind": d.get("kind") or "part",
                        "quantity": qty.get(key, 0), "placed": got, "arranged": shown, **({"local_only": local} if local else {}),
                        "revision_id": str(part_bind[key]) if key in part_bind else None,
                        "status": "missing" if held == 0 else ("included" if held >= qty.get(key, 0) else "partial"),
                        **({"stage": why.get("stage"), "code": why.get("code")} if why and held < qty.get(key, 0) else {})})
        return cv, bom, made, items

    cov: dict[str, Any] | None = None
    if plan is not None:  # before any solve: whether anything can be assembled at all (installed or local only)
        cov, _, assembled, items = counted(plan)
        if not assembled:
            raise AppError(422, "nothing_placeable", "no instance of the spec can be placed in an assembly; the built parts are kept",
                           {"coverage": cov, "missing": items})
    assumptions += rel_assumptions
    if ai_placed or ai_rels or ai_dropped:  # generation supplement F / D-02: every AI placement decision in use is one decision
        from datetime import UTC, datetime

        at = datetime.now(UTC).isoformat()
        kept_insts = {i["key"]: i for i in instances}
        # generation reliability P: the instances joined to an anchor by relations move with it (affected too)
        # round 3 (B): the instances whose positions rest on it (through spec relations or AI mates) move with it
        group_of = _dependents(mapped, (plan or {}).get("positioned") or {})

        def decision(k: str, p: dict[str, Any], field: str, adopted: Any, *extra: str) -> dict[str, Any]:
            inst = kept_insts[k]
            return {"subject": k, "field": field, "author": "ai_turbo", "at": at, "unit": "mm, °",
                    "trigger": p.get("trigger") if p.get("trigger") in pol.DECISION_TRIGGERS else "missing", "adopted": adopted,
                    # round 2 (P, WP06): what the position rests on; an estimate is never written as a source fact
                    "basis_kind": _ai_basis_kind(p), "basis": str(p.get("basis") or ""),
                    "sources": [str(x) for x in p.get("sources") or []],
                    "alternatives": [str(x) for x in p.get("alternatives") or []],
                    "affected": list(dict.fromkeys([k, *extra, inst.get("parent") or root_key, inst["definition"],
                                                    *[str(x) for x in p.get("affected") or [] if str(x) in kept_insts],
                                                    *group_of.get(k, [])]))}

        for k, p in ai_placed.items():
            if k in kept_insts:  # in its parent frame; ``stated_in``: the frame and numbers the source gives (review of round 3)
                assumptions.append(decision(k, p, "placement", {"translate": list(kept_insts[k]["placement"]["translate"]),
                                                                "rotate": list(kept_insts[k]["placement"]["rotate"]),
                                                                **({"stated_in": p["stated_in"]} if p.get("stated_in") else {})}))
        # round 3 (B): every AI placement mate kept (or dropped by the placement's solve) is one decision: the
        # connection chosen — the position is the solver's
        kept_rel_keys = {str(r.get("key")) for r in mapped}
        for r in ai_rels + ai_dropped:
            k = str(r["a"]["instance"])
            if k not in kept_insts or (r in ai_rels and str(r.get("key")) not in kept_rel_keys):
                continue
            other = [x for x in relation_ends(r) if x != k]
            adopted = {"mate": r.get("key"), "kind": r.get("kind"), "a": {x: r["a"][x] for x in ("instance", "datum")},
                       "b": {x: r["b"][x] for x in ("instance", "datum")}, "params": dict(r.get("params") or {})}
            if r.get("solved"):
                adopted["solved"] = r["solved"]  # where the solve put the instance (in its parent frame)
            dec = decision(k, r.get("decision") or {}, "mate", adopted, *other, str(r.get("key")))
            if r.get("dropped"):  # the solver's own decision: a conflicting lower-priority mate is not applied
                said = (f"{adopted['kind']} {adopted['a']['instance']}.{adopted['a']['datum']} – "
                        f"{adopted['b']['instance']}.{adopted['b']['datum']}")
                dec.update(adopted="dropped", alternatives=[f"apply {said}"], trigger="conflict",
                           basis=f"{r['dropped']}; the mate proposed was {said} ({dec['basis']})")
            assumptions.append(dec)
    rel_by_parent: dict[str, list[dict[str, Any]]] = {}
    for rel in mapped:
        rel_by_parent.setdefault(parent_of[(rel.get("a") or {}).get("instance")], []).append(rel)
    revs: dict[str, AssemblyRevision] = {}
    asms: dict[str, Assembly] = {}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(key: str, depth: int) -> None:
        if state.get(key) == 2:
            return
        if state.get(key) == 1 or depth > MAX_DEPTH:
            raise AppError(422, "assembly_cycle", f"spec assembly {key} contains itself", {"definition": key})
        state[key] = 1
        for inst in children.get(key, []):
            if inst["definition"] in asm_keys:
                visit(inst["definition"], depth + 1)
        state[key] = 2
        order.append(key)

    visit(root_key, 0)
    build_key = _canon({"spec": spec_sha, "policy": policy, "bindings": {k: str(v) for k, v in sorted(part_bind.items())},
                        **({"placements": {i["key"]: i["placement"] for i in tree_part["instances"] if i["key"] in ai_placed}}
                           if ai_placed else {}),
                        **({"mates": [{x: r.get(x) for x in ("kind", "a", "b", "params")} for r in ai_rels]} if ai_rels else {})})
    earlier = _continuable(db, continue_from, root_key) if continue_from is not None else {}
    if continue_from is not None and earlier and (continue_from.spec_ref or {}).get("build_key") == build_key:
        created["_unplaced"] = tree_part["unplaced"]
        created["_coverage"] = (continue_from.spec_ref or {}).get("coverage")
        created["_reused"] = True
        return earlier[root_key][0], continue_from, created
    arranged = plan["arranged"] if plan is not None else set()
    at_origin = plan["at_origin"] if plan is not None else set()
    # generation reliability P: what each position rests on (stated / derived / estimated), kept on the component
    rel_ends = {e for r in mapped for e in relation_ends(r)}
    basis_of: dict[str, str] = plan["basis"] if plan is not None else {
        i["key"]: (ai_kind[i["key"]] if i["key"] in ai_kind else "stated" if i.get("placement") else "derived")
        for i in instances if i["key"] in ai_kind or i.get("placement") or i["key"] in rel_ends}
    # an AI-placed anchor of a relation group holds its group where the decision put it (even when a relation moves it)
    anchors = {g["anchor"] for g in (plan or {}).get("groups") or []} & set(ai_placed)
    if design_contract:
        anchors.update(i["key"] for i in instances)
    located: set[str] = set()  # spec instance keys the solves position in their parent frame
    applied_relations: set[str] = set()  # only solver-applied mates can fulfil deferred source intent
    target_bom: list[dict[str, Any]] = []
    base_number = (number or (defs.get(root_key) or {}).get("number") or (defs.get(root_key) or {}).get("name") or "ASM").strip()[:60]
    for key in order:
        d = defs.get(key) or {"name": base_number}
        num = base_number if key == root_key else (d.get("number") or f"{base_number}-{key}")
        prior = earlier.get(key)
        if prior is not None:  # S3 continue: the next revision of the assembly the partial build made
            asm, parent = _lock_assembly(db, prior[0].id), prior[1]
            old_comps, old_mates = revision_state(db, parent)
            old_ids = {c.meta.get("spec_key"): c.instance_id for c in old_comps if c.meta.get("spec_key")}
            old_mate_ids = {m.meta.get("spec_key"): m.mate_id for m in old_mates if m.meta.get("spec_key")}
        else:
            asm = _new_assembly(db, project_id, user, _free_number(db, project_id, num), d.get("name") or num, d.get("notes") or "")
            parent, old_ids, old_mate_ids = None, {}, {}
        comps_in: list[dict[str, Any]] = []
        ids: dict[str, str] = {}
        for inst in children.get(key, []):
            dk = inst["definition"]
            definition = {"kind": "assembly", "revision_id": str(revs[dk].id)} if dk in asm_keys else {"kind": "part", "revision_id": str(part_bind[dk])}
            entry = {"name": inst.get("name") or "", "definition": definition, "source": "spec",
                     "meta": {"spec_key": inst["key"], "definition_spec_key": dk, "fact_ids": inst.get("fact_ids") or []}}
            if inst.get("placement"):
                entry["placement"] = inst["placement"]
            elif inst["key"] in at_origin:  # the only child of its parent: at the parent's origin
                entry["placement"] = {"translate": [0, 0, 0], "rotate": [0, 0, 0]}
            if inst["key"] in arranged:
                entry["meta"]["arranged"] = True
            if inst["key"] in ai_kind:  # positioned by an AI decision (Turbo): a stated position, or AI mates the solver applies
                entry["source"] = "ai_turbo"
                entry["meta"]["ai_placed"] = True
            if basis_of.get(inst["key"]):
                entry["meta"]["basis_kind"] = basis_of[inst["key"]]
            if old_ids.get(inst["key"]):
                entry["instance_id"] = old_ids[inst["key"]]
            comps_in.append(entry)
        comps = _components_from(db, asm, comps_in)
        for inst, c in zip(children.get(key, []), comps):
            ids[inst["key"]] = c.instance_id
        rels = rel_by_parent.get(key, [])
        held = held_fixed([i["key"] for i in children.get(key, [])],
                          {i["key"] for i, c in zip(children.get(key, []), comps) if c.placement.get("mode") == "explicit"}, rels, anchors)
        for inst, c in zip(children.get(key, []), comps):
            c.fixed = inst["key"] in held

        def side(ref: Any) -> dict[str, Any] | None:
            if not isinstance(ref, dict):
                return None
            if is_frame_ref(ref):  # an assembly frame (round 3, B): its datums travel with the mate, mapped to this frame
                return {"instance": str(ref["instance"]), "datum": ref.get("datum"), "frame": dict(ref.get("frame") or {})}
            # a sub-assembly's inner datum may name its child by spec key (definition_datums knows both names)
            return {"instance": ids.get(ref.get("instance")), "datum": ref.get("datum")}

        mates_in = []
        for r in rels:
            ai = r.get("source") == "ai_turbo"
            m = {"kind": r.get("kind"), "a": side(r.get("a")), "b": side(r.get("b")), "params": r.get("params") or {},
                 "source": "ai_turbo" if ai else "spec",
                 "meta": ({"spec_key": r.get("key"), "ai_placement": True, "basis_kind": _ai_basis_kind(r.get("decision") or {})} if ai
                          else {"spec_key": r.get("key"), "fact_ids": r.get("fact_ids") or []})}
            if ai:
                m["priority"] = int(r.get("priority") or AI_MATE_PRIORITY["estimated"])
            if old_mate_ids.get(r.get("key")):
                m["mate_id"] = old_mate_ids[r.get("key")]
            if r.get("kind") == "fixed" and not r.get("b"):
                comp = next((c for c in comps if c.instance_id == m["a"]["instance"]), None)
                if comp is not None:
                    comp.fixed = True
            mates_in.append(m)
        mate_recs = _mates_from(asm, mates_in, comps)
        spec_ref = {**spec_ref_base, "definition": key}
        if interfaces_of(spec, key):  # round 3 (B): the datums ``if.<key>`` an instance of this assembly offers its parent
            spec_ref["interfaces"] = interfaces_of(spec, key)
        solved: dict[str, Any] | None = None
        if key == root_key and plan is not None:
            # review of round 3 (connected is not positioned): the root level is solved before it is stored — with every
            # level below already solved, the coverage counts what the solves POSITION (no free translation left)
            solved = _solve(Tree(db, Tree.ROOT, override=comps), comps, mate_recs, policy)
            located |= _grounded_keys(comps, solved)
            applied_relations |= {str(m.meta["spec_key"]) for m in mate_recs if m.meta.get("spec_key") and m.status in ("solved", "redundant")}
            selected = _deferred_mates(choices, mapped, applied=applied_relations)
            plan_args["rel_problems"].update(_deferred_problems(choices, mapped, applied=applied_relations))
            for choice in choices:
                if not choice.get("deferred_binding"):
                    continue
                if choice["relation"] not in selected:
                    continue
                assumptions.append({"subject": choice["relation"], "field": "relation", "author": "rule",
                                    "adopted": {"placement_mates": selected[choice["relation"]]}, "trigger": "ambiguous",
                                    "basis": "the solver applied the physical datums selected for the original relation",
                                    "affected": relation_ends(choice)})
            cov, target_bom, _, _ = counted(_partial_plan(spec, tree_part, set(part_bind), **plan_args, located=located))
        if key == root_key:  # the structure the spec check compares against, so later checks need no spec upload
            # a partial assembly is checked against what it keeps; its coverage says what is missing from the target
            spec_ref["structure"] = spec_structure(_kept_spec(spec, plan) if plan is not None else spec)
            if cov is not None:
                spec_ref.update(coverage=cov, target_bom=target_bom, build_key=build_key)
                if cov["status"] != "complete":  # what a later hand edit is checked against (the whole target)
                    spec_ref["target_structure"] = spec_structure(spec)
        root_notes = list(notes) + [dict(n) for n in build_notes or [] if isinstance(n, dict)]
        if key == root_key and cov is not None and cov["status"] != "complete":
            by_ai = (cov.get("decided") or {}).get("instances")
            local = (cov.get("local_only") or {}).get("instances")
            guessed = (cov.get("estimated") or {}).get("instances")
            root_notes.append({"code": "partial_assembly", "message": f"partial assembly: {cov['placed']['instances']} of "
                               f"{cov['target']['instances']} part instances installed" + (f" ({by_ai} placed by AI decision)" if by_ai else "")
                               + (f" ({guessed} at an AI-estimated position)" if guessed else "")
                               + (f", {local} placed only locally (inside a sub-assembly that has no position, or not fixed by "
                                  "their relations)" if local else "")
                               + f", {cov['arranged']['instances']} only arranged for display, {len(cov['missing'])} missing item(s) "
                               "(see coverage)"})
        rev = _save_revision(db, asm, user=user, comps=comps, mates=mate_recs, source=source, policy=policy, parent=parent,
                             spec_ref=spec_ref, notes=root_notes if key == root_key else [],
                             assumptions=assumptions if key == root_key else [], node_run_id=node_run_id,
                             note="continued: the definitions built since are added" if parent is not None else "", solved=solved)
        if key != root_key:
            located |= _grounded_keys(comps, rev.solve or {})
            applied_relations |= {str(m.meta["spec_key"]) for m in mate_recs if m.meta.get("spec_key") and m.status in ("solved", "redundant")}
        revs[key], asms[key] = rev, asm
        created[key] = str(rev.id)
        if key == root_key and design_contract:
            from .spec_coverage import with_design_constraints
            report = (rev.checks.get("assembly.design_contract") or {}).get("latest") or {}
            if cov is None:
                cov = coverage_of(spec, built=set(part_bind), placed={k: n for k, n in _target_quantities(spec).items() if k in part_bind}, missing=[])
            cov = with_design_constraints(cov, report)
            rev.spec_ref = {**rev.spec_ref, "coverage": cov}
            db.flush()
    created["_unplaced"] = tree_part["unplaced"]
    if cov is not None:
        created["_coverage"] = cov
    return asms[root_key], revs[root_key], created


def _grounded_keys(comps: list[Comp], solved: dict[str, Any]) -> set[str]:
    """The spec instance keys of the components a level's solve positioned in its frame (``instances[…].grounded``)."""
    insts = solved.get("instances") or {}
    return {str(c.meta["spec_key"]) for c in comps if c.meta.get("spec_key") and (insts.get(c.instance_id) or {}).get("grounded")}


def _target_quantities(spec: dict[str, Any]) -> dict[str, int]:
    from .engineering_spec import quantities

    return {k: int(v.get("total") or 0) for k, v in quantities(spec).items()}


def _kept_spec(spec: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """The spec reduced to what a partial assembly keeps (the structure its spec check compares against)."""
    keep_i = {i["key"] for i in plan["instances"]}
    keep_r = {r.get("key") for r in plan["relations"]}
    return {**spec, "definitions": [d for d in spec.get("definitions") or [] if d.get("key") in plan["definitions"]],
            "instances": [i for i in spec.get("instances") or [] if i.get("key") in keep_i],
            "relations": [r for r in spec.get("relations") or [] if r.get("key") in keep_r]}


def _continuable(db: Session, rev: AssemblyRevision, root_key: str) -> dict[str, tuple[Assembly, AssemblyRevision]]:
    """Definition key → (assembly, revision) of an earlier partial spec build whose assemblies are still at that
    revision (nobody edited them since): the continued build adds their next revisions. Empty when the root changed."""
    out: dict[str, tuple[Assembly, AssemblyRevision]] = {}

    def walk(r: AssemblyRevision, depth: int) -> None:
        key = (r.spec_ref or {}).get("definition")
        latest = latest_revision(db, r.assembly_id)
        asm = db.get(Assembly, r.assembly_id)
        if not key or key in out or depth > MAX_DEPTH or latest is None or latest.id != r.id or asm is None or asm.archived_at is not None:
            return
        out[key] = (asm, r)
        for c in db.scalars(select(AssemblyComponent).where(AssemblyComponent.revision_id == r.id,
                                                            AssemblyComponent.definition_kind == "assembly")).all():
            child = db.get(AssemblyRevision, c.child_revision_id) if c.child_revision_id else None
            if child is not None:
                walk(child, depth + 1)

    walk(rev, 0)
    return out if root_key in out else {}


def _refuse_blocked(spec: dict[str, Any], tree_part: dict[str, Any]) -> None:
    """Standard: 422 ``spec_blocked`` while an open question blocks a definition, instance or relation under the root
    (W2 readiness: a question blocks only what its ``affects`` lists)."""
    from .engineering_spec import readiness

    rd = readiness(spec, "standard")
    inst_keys = {i["key"] for i in tree_part["instances"]}
    rel_keys = {r.get("key") for r in tree_part["relations"]}
    blocked: dict[str, list[str]] = {}
    for k, v in rd["definitions"].items():
        if k in tree_part["definitions"] and v["state"] == "blocked":
            blocked[k] = v["questions"]
    for k, v in rd["instances"].items():
        if k in inst_keys and v["state"] == "blocked":
            blocked[k] = v["questions"]
    for k, v in rd["relations"].items():
        if k in rel_keys and v["state"] == "blocked":
            blocked[k] = v["questions"]
    if not blocked:
        return
    qs = {q["id"]: q for q in spec.get("questions") or [] if q.get("id")}
    ids = sorted({q for v in blocked.values() for q in v})
    raise AppError(422, "spec_blocked", "open spec questions block branches of this assembly; answer them in the spec first",
                   {"blocked": {k: sorted(v) for k, v in blocked.items()},
                    "questions": [{"id": q, "subject": (qs.get(q) or {}).get("subject"), "question": (qs.get(q) or {}).get("question")}
                                  for q in ids]})


def spec_structure(spec: dict[str, Any]) -> dict[str, Any]:
    """The part of a spec the structural check needs (definitions, instances, relations keys): only what hangs under
    the root (definitions with no instance there are not expected in the assembly). A null parent is the root."""
    t = spec_tree(spec)
    return {"root": t["root"],
            "definitions": [{"key": d.get("key"), "kind": d.get("kind")} for d in spec.get("definitions") or []
                            if d.get("key") in t["definitions"]],
            "instances": [{"key": i.get("key"), "definition": i.get("definition"), "parent": i.get("parent") or t["root"]}
                          for i in t["instances"]],
            "relations": [{"key": r.get("key"), "kind": r.get("kind")} for r in t["relations"]],
            "unplaced": t["unplaced"]}


def _template_part(db: Session, project_id: uuid.UUID, user: User, d: dict[str, Any]) -> uuid.UUID | None:
    """Build a definition from a W3 template when ``app.cad.templates`` exists; None otherwise."""
    tpl = d.get("template") or {}
    if not tpl.get("id"):
        return None
    try:
        from ..cad import templates  # type: ignore[attr-defined]
    except ImportError:
        return None
    from . import revisions

    templates.instantiate(tpl["id"], tpl.get("params") or {})  # refuses an unknown template / invalid params before a part exists
    number = _free_part_number(db, project_id, d.get("number") or f"{d.get('key')}")
    part = Part(project_id=project_id, number=number, name=(d.get("name") or number)[:200],
                material=(d.get("material") or "unknown")[:40], owner_id=user.id)
    db.add(part)
    db.flush()
    _spec_props(db, part, user, d, str(d.get("key")), None)  # the spec's stated values first; the template fills the rest
    # the template provenance (ports for mates on ``port.<name>``) is stored with the revision, as for every template part
    rev = revisions.create_template_revision(db, part_id=part.id, user=user, template_id=str(tpl["id"]), params=tpl.get("params") or {},
                                             note=f"Template {tpl['id']} for spec definition {d.get('key')}")
    return rev.id


def _claim_number(db: Session, kind: str, project_id: uuid.UUID, number: str) -> bool:
    """W8: a transaction-scoped claim on one candidate number, so two concurrent builds of the same spec never pick
    the same free number (an uncommitted insert is invisible to the other's existence check). Never waits: a number
    another transaction is claiming is skipped."""
    from sqlalchemy import text

    return bool(db.scalar(text("select pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"{kind}.number:{project_id}:{number}"}))


def _free_number(db: Session, project_id: uuid.UUID, number: str) -> str:
    base = number[:72] or "ASM"
    cand, n = base, 1
    while (db.scalar(select(Assembly.id).where(Assembly.project_id == project_id, Assembly.number == cand)) is not None
           or not _claim_number(db, "assembly", project_id, cand)):
        n += 1
        cand = f"{base}-{n}"
    return cand


def _free_part_number(db: Session, project_id: uuid.UUID, number: str, skip: Collection[str] = ()) -> str:
    """``skip``: numbers already promised to other parts of the same build (not inserted yet)."""
    base = number[:72] or "P"
    cand, n = base, 1
    while (cand in skip or db.scalar(select(Part.id).where(Part.project_id == project_id, Part.number == cand)) is not None
           or not _claim_number(db, "part", project_id, cand)):
        n += 1
        cand = f"{base}-{n}"
    return cand


# =====================================================================================================
# STEP import
# =====================================================================================================


def import_step(db: Session, *, project_id: uuid.UUID, user: User, file_version_id: uuid.UUID, number: str | None = None,
                name: str | None = None, policy: str | None = None, node_run_id: uuid.UUID | None = None) -> tuple[Assembly, AssemblyRevision, dict[str, Any]]:
    """Structured STEP import (see :mod:`app.cad.assembly_step`): one Part + single-part revision per unique
    leaf definition (each built by the normal single-part import path from its own one-solid STEP), one
    assembly + revision per assembly definition (shared, reused by every instance), source names, hierarchy
    and instance poses kept (as fixed imported positions; the file has no mates). Leaves that are not exactly
    one valid solid are refused with their paths (never fused, dropped or flattened)."""
    from ..cad import assembly_step as S
    from ..cad.ops import ModelSpec, StepBase
    from ..config import get_settings
    from . import revisions

    fv = db.get(FileVersion, file_version_id)
    f = db.get(File, fv.file_id) if fv else None
    if fv is None or f is None or f.project_id != project_id:
        raise not_found("file version")
    data, _ = read_blob(db, fv.blob_sha256)
    try:
        with revisions.KERNEL_LOCK:
            structure = S.read_assembly(data, file_name=fv.original_name, max_definitions=MAX_DEFINITIONS)
        # counted per definition (no occurrence expansion) and checked before anything walks the tree
        counts = structure.counts()
    except S.StepAssemblyError as exc:
        if exc.code == "assembly_too_large":
            raise too_large(exc.details.get("counts") or {}) from None
        raise AppError(422, exc.code, exc.message, {**exc.details, "file_version_id": str(fv.id)})
    if not structure.assemblies:
        raise AppError(422, "step_not_assembly", "the STEP file is a single part; import it as a part model",
                       {"file_version_id": str(fv.id), "counts": counts})
    if counts["leaf_instances"] > MAX_LEAVES or counts["part_definitions"] + counts["assembly_definitions"] > MAX_DEFINITIONS \
            or counts["depth"] > MAX_DEPTH:
        raise too_large(counts)
    problems = structure.problems()
    non_rigid = [n for n in structure.notes if n["code"] == "non_rigid_transform"]
    if problems or non_rigid:
        raise AppError(422, "step_assembly_unsupported",
                       "the STEP assembly has content that cannot become exact single-solid part definitions or rigid instances",
                       {"leaves": problems, "transforms": non_rigid, "file_version_id": str(fv.id), "counts": counts})
    with revisions.KERNEL_LOCK:
        notes = list(structure.notes) + S.pose_notes(structure)
    notes.append({"code": "no_source_mates", "subject": structure.assemblies[structure.root].name,
                  "message": "the STEP file carries geometry poses only: instances are kept as fixed imported positions; "
                             "original design mates are not restored"})
    root_name = structure.assemblies[structure.root].name
    stem = fv.original_name.rsplit(".", 1)[0]
    if number and number.strip() and db.scalar(select(Assembly.id).where(Assembly.project_id == project_id,
                                                                         Assembly.number == number.strip()[:80])) is not None:
        raise conflict("assembly_number_taken", f"assembly number {number.strip()} already exists in this project", number=number.strip())
    base_number = number.strip()[:80] if number and number.strip() else _free_number(db, project_id, (stem or root_name).strip()[:60])
    # parts: one per unique leaf definition, through the single-part build path
    part_revs: dict[str, ModelRevision] = {}
    for k, (key, leaf) in enumerate(structure.leaves.items(), 1):
        with revisions.KERNEL_LOCK:
            step = S.write_part(leaf.shape, leaf.name)
        sha = save_blob(db, step, "model/step")
        spec = ModelSpec(base=StepBase(blob_sha256=sha), ops=[])
        with revisions.KERNEL_LOCK:
            result = revisions.kernel().build(spec, step, tolerance=get_settings().cad_tolerance)
        if not result.ok:
            raise AppError(422, "import_failed", f"part definition '{leaf.name}' could not be built",
                           {"definition": leaf.brief(), "errors": [e.model_dump() for e in result.errors]})
        part = Part(project_id=project_id, number=_free_part_number(db, project_id, f"{base_number}-P{k:03d}"), name=leaf.name[:200],
                    material="unknown", owner_id=user.id, folder=base_number[:200], description=f"Imported from {fv.original_name}")
        db.add(part)
        db.flush()
        rev = revisions.create_revision(
            db, part_id=part.id, user=user, spec=spec, source="import", note=f"Part definition '{leaf.name}' of assembly {fv.original_name}",
            node_run_id=node_run_id, source_file_version_id=fv.id, result=result,
            checks={"assembly_import": {"file_version_id": str(fv.id), "definition": leaf.brief(), "source_sha256": fv.blob_sha256}})
        part_revs[key] = rev
    # assemblies: children before parents, each definition once
    asm_revs: dict[str, AssemblyRevision] = {}
    state: dict[str, int] = {}
    created_assemblies: list[str] = []

    def build(key: str, depth: int) -> AssemblyRevision:
        if key in asm_revs:
            return asm_revs[key]
        if state.get(key) == 1 or depth > MAX_DEPTH:
            raise AppError(422, "step_assembly_cycle", "the STEP product structure is cyclic")
        state[key] = 1
        sdef = structure.assemblies[key]
        for c in sdef.components:
            if c.ref in structure.assemblies:
                build(c.ref, depth + 1)
        is_root = key == structure.root
        num = base_number if is_root else _free_number(db, project_id, f"{base_number}-{sdef.name}"[:72])
        asm = _new_assembly(db, project_id, user, num, sdef.name, f"Imported from {fv.original_name}")
        ids = _next_ids(asm, len(sdef.components))
        comps: list[Comp] = []
        for iid, c in zip(ids, sdef.components):
            if c.ref in structure.assemblies:
                child = asm_revs[c.ref]
                comps.append(Comp(instance_id=iid, name=c.name, kind="assembly", transform=c.transform, child_assembly_id=child.assembly_id,
                                  child_revision_id=child.id, fixed=True, placement={"mode": "explicit", "source": "import"},
                                  source_ref=c.source_name or None))
            else:
                prev = part_revs[c.ref]
                comps.append(Comp(instance_id=iid, name=c.name, kind="part", transform=c.transform, part_id=prev.part_id,
                                  model_revision_id=prev.id, fixed=True, placement={"mode": "explicit", "source": "import"},
                                  source_ref=c.source_name or None))
        rev = _save_revision(db, asm, user=user, comps=comps, mates=[], source="import", policy=policy,
                             note=f"Imported from {fv.original_name}", notes=notes if is_root else [], node_run_id=node_run_id,
                             source_file_version_id=fv.id)
        asm_revs[key] = rev
        created_assemblies.append(str(asm.id))
        state[key] = 2
        return rev

    root_rev = build(structure.root, 0)
    root = db.get(Assembly, root_rev.assembly_id)
    assert root is not None
    summary = {"counts": counts, "notes": notes, "parts": [str(r.part_id) for r in part_revs.values()],
               "part_revisions": {structure.leaves[k].name + f" ({k})": str(r.id) for k, r in part_revs.items()},
               "assemblies": created_assemblies, "source": {"file_version_id": str(fv.id), "sha256": fv.blob_sha256,
                                                             "text_encoding": structure.header.get("text_encoding")}}
    audit(db, action="assembly.import", subject_type="assembly", subject_id=root.id, user_id=user.id, project_id=project_id,
          data={"file_version_id": str(fv.id), "counts": counts})
    return root, root_rev, summary


# =====================================================================================================
# Checks (explicit run)
# =====================================================================================================


def _heavy_checks(db: Session, rev: AssemblyRevision, policy: str, *, clearance_mm: float | None = None,
                  allow: list[list[str]] | None = None) -> dict[str, dict[str, Any] | None]:
    from ..cad import assembly_checks as chk

    def run() -> dict[str, Any]:
        from .revisions import KERNEL_LOCK

        tree = Tree(db, rev.id)
        leaves = tree.leaves()
        shapes: dict[str, Any] = {}
        bboxes: dict[str, Any] = {}
        for key in OrderedDict.fromkeys(leaf.part_key for leaf in leaves):
            prev = tree.part_revision(key)
            if prev is None:
                continue
            try:
                shapes[key] = part_shape(db, prev)
            except AppError:
                continue
            bboxes[key] = _bbox_of(prev)
        cl = [chk.CheckLeaf(path=leaf.path, names=leaf.names, part_key=leaf.part_key, world=leaf.world) for leaf in leaves]
        with KERNEL_LOCK:
            return chk.interference(cl, shapes, bboxes, clearance_mm=clearance_mm, allow=allow)

    # Under Turbo run_check returns the skip record (and logs it in the checks ledger) without calling either checker.
    both: dict[str, Any] = {}

    def interference() -> dict[str, Any]:
        both.update(run())
        return both["interference"]

    out: dict[str, dict[str, Any] | None] = {"assembly.interference": pol.run_check("assembly.interference", policy, interference)}
    if clearance_mm is not None:
        out["assembly.clearance"] = pol.run_check("assembly.clearance", policy, lambda: both["clearance"])
    return out


def run_checks(db: Session, revision_id: uuid.UUID, user: User, *, policy: str | None = None, clearance_mm: float | None = None,
               allow: list[list[str]] | None = None) -> AssemblyRevision:
    """Run the business checks on an exact revision and append the records (history kept). ``policy`` defaults to
    the revision's own; a later standard run on a Turbo revision appends real results beside the skipped ones. The
    spec check compares only against the structure stored with the revision (``spec_ref``), never client JSON."""
    rev = _load_revision(db, revision_id)
    run_policy = pol.parse_policy(policy) if policy else rev.execution_policy
    spec = rev.spec_ref["structure"] if rev.spec_ref and rev.spec_ref.get("structure") else None
    tree = Tree(db, rev.id)
    comps, _ = revision_state(db, rev)
    records = {k: _record(v, user=user, policy=run_policy) for k, v in
               _quick_checks(db, run_policy, tree, rev.solve or {}, compute_bom(db, tree), spec, comps).items()}
    records.update({k: _record(v, user=user, policy=run_policy) for k, v in
                    _heavy_checks(db, rev, run_policy, clearance_mm=clearance_mm, allow=allow).items() if v is not None})
    # Merge into the latest history under the revision row lock (taken after the slow checks): a concurrent run
    # that finished meanwhile keeps its records instead of being overwritten by this run's stale copy.
    rev = db.scalar(select(AssemblyRevision).where(AssemblyRevision.id == rev.id).with_for_update()
                    .execution_options(populate_existing=True))
    assert rev is not None
    rev.checks = _merge_checks(rev.checks or {}, records)
    if pol.is_turbo(run_policy):
        status = "skipped_turbo" if rev.check_status in ("skipped_turbo", "pending", "not_requested") else rev.check_status
    else:
        status = _check_status({k: v for k, v in rev.checks.items()}, _required(rev, rev.checks))
    rev.check_status = status
    from .verification import assembly_verification  # S5: keeps the person's assumption decisions and the upstream state

    rev.verification = assembly_verification(db, rev)
    asm = db.get(Assembly, rev.assembly_id)
    audit(db, action="assembly_revision.checks", subject_type="assembly_revision", subject_id=rev.id, user_id=user.id,
          project_id=asm.project_id if asm else None, data={"policy": run_policy, "status": status, "checks": sorted(records)})
    db.flush()
    return rev


# =====================================================================================================
# STEP export
# =====================================================================================================


#: Exports per (kind, revision, labels key) → blob sha. Part / assembly names and BOM properties are read live (a part
#: renamed, a material set later), so everything that goes into the file besides the frozen geometry is in the key; the
#: revision's step_sha / parts_zip_sha record the latest export only and are never served as a cache.
_EXPORTS: OrderedDict[tuple[str, str, str], str] = OrderedDict()


def _cached_export(db: Session, kind: str, rev: AssemblyRevision, key: str) -> tuple[bytes, str] | None:
    sha = _EXPORTS.get((kind, str(rev.id), key))
    if sha is None:
        return None
    try:
        data = read_blob(db, sha)[0]
    except FileNotFoundError:
        _EXPORTS.pop((kind, str(rev.id), key), None)
        return None
    _EXPORTS.move_to_end((kind, str(rev.id), key))
    return data, sha


def _remember_export(kind: str, rev: AssemblyRevision, key: str, sha: str) -> None:
    _EXPORTS[(kind, str(rev.id), key)] = sha
    if len(_EXPORTS) > 256:
        _EXPORTS.popitem(last=False)


def _structure_key(parts: list[Any], asms: list[Any], root: str) -> dict[str, Any]:
    """What the assembly STEP contains besides the shapes (frozen by part revision): names, hierarchy, transforms."""
    return {"root": root, "parts": sorted((p.key, p.name) for p in parts),
            "assemblies": sorted((a.key, a.name, [(c.ref, c.name, c.transform) for c in a.components]) for a in asms)}


def _write_structure(db: Session, rev: AssemblyRevision, *, shapes: bool = True) -> tuple[list[Any], list[Any], str, dict[str, ModelRevision]]:
    """Parts (with their shapes unless ``shapes`` is False), assemblies, root key and part revisions of an export."""
    from ..cad import assembly_step as S

    tree = Tree(db, rev.id)
    tree.stats()
    parts: dict[str, Any] = {}
    asms: dict[str, Any] = {}
    revs: dict[str, ModelRevision] = {}
    names: dict[str, list[str]] = {}

    def walk(rid: str, depth: int) -> None:
        if rid in asms:
            return
        if depth > MAX_DEPTH:
            raise too_large({"depth": depth})
        crev = tree.revision(rid)
        asm = tree.assembly(crev.assembly_id) if crev else None
        comps: list[Any] = []
        for c in tree.children(rid):
            if c.suppressed:
                continue  # suppressed instances stay in history, not in outputs
            if c.kind == "part":
                key = str(c.model_revision_id)
                if key not in parts:
                    prev = tree.part_revision(key)
                    assert prev is not None
                    part = tree.part(prev.part_id)
                    revs[key] = prev
                    parts[key] = S.WritePart(key=key, name=part.name if part else key, shape=part_shape(db, prev) if shapes else None)
                    names.setdefault(parts[key].name, []).append(key)
            else:
                walk(str(c.child_revision_id), depth + 1)
            comps.append(S.WriteComponent(ref=str(c.def_id), name=c.name, transform=c.transform))
        asms[rid] = S.WriteAssembly(key=rid, name=asm.name if asm else rid, components=comps)

    walk(str(rev.id), 0)
    for name, keys in names.items():  # two revisions of one part in one tree: keep both, disambiguate the names
        if len(keys) > 1:
            for k in keys:
                parts[k].name = f"{name} R{revs[k].rev_no}"
    return list(parts.values()), list(asms.values()), str(rev.id), revs


def export_step(db: Session, revision_id: uuid.UUID | str, *, protocol: str | None = None) -> tuple[bytes, str]:
    """Assembly STEP of an exact revision (shared definitions, hierarchy, names, instance transforms).
    Deterministic for the frozen content and the current names; cached per names and protocol (``_EXPORTS``).
    ``protocol``: AP203 (default; the export kept as the revision's ``step_sha``) or AP214 (written in it directly, no
    re-export). A STEP the writer cannot make (a part STEP unreadable, the protocol refused) is a 422 AppError with
    the writer's code."""
    from ..cad import assembly_step as S
    from .revisions import KERNEL_LOCK

    protocol = protocol or S.DEFAULT_PROTOCOL
    rev = _load_revision(db, revision_id)
    key = _canon(_structure_key(*_write_structure(db, rev, shapes=False)[:3]))
    kind = "step" if protocol == S.DEFAULT_PROTOCOL else f"step:{protocol}"
    hit = _cached_export(db, kind, rev, key)
    if hit is not None:
        return hit
    try:
        parts, asms, root, _ = _write_structure(db, rev)
        with KERNEL_LOCK:
            data = S.write_assembly(parts, asms, root, protocol=protocol)
    except S.StepAssemblyError as exc:
        raise AppError(422, exc.code, exc.message, {**exc.details, "assembly_revision_id": str(rev.id)}) from None
    sha = save_blob(db, data, "model/step")
    if kind == "step":
        rev.step_sha = sha
        db.flush()
    _remember_export(kind, rev, key, sha)
    return data, sha


def _safe(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return out.strip("._")[:80] or "part"


def export_parts_zip(db: Session, revision_id: uuid.UUID | str) -> tuple[bytes, str]:
    """Zip of the unique part definitions (one single-part STEP each, product names kept) + manifest.json
    (definition, revision, quantity, material / procurement / representation, sha256)."""
    from ..cad import assembly_step as S
    from .revisions import KERNEL_LOCK

    rev = _load_revision(db, revision_id)
    bom = compute_bom(db, Tree(db, rev.id))
    qty = {line["def_key"]: line for line in bom["flattened"]}
    asm = db.get(Assembly, rev.assembly_id)
    # names, BOM properties, the assembly label and the verification are read live: all of them key the cache
    named, _, _, _ = _write_structure(db, rev, shapes=False)
    key = _canon({"parts": sorted((p.key, p.name) for p in named),
                  "lines": {k: {f: line.get(f) for f in ("number", "total_quantity", "material", "procurement", "representation")}
                            for k, line in qty.items()},
                  "assembly": [asm.number, asm.name] if asm else None, "verification": rev.verification,
                  "content": rev.content_sha256, "bom": rev.bom_sha256})
    hit = _cached_export(db, "parts_zip", rev, key)
    if hit is not None:
        return hit
    parts, _, _, revs = _write_structure(db, rev)
    buf = io.BytesIO()
    manifest: dict[str, Any] = {"schema": "partroute.assembly_parts/1", "assembly": {"id": str(rev.assembly_id), "number": asm.number if asm else None,
                                "name": asm.name if asm else None, "revision_id": str(rev.id), "rev_no": rev.rev_no,
                                "content_sha256": rev.content_sha256, "bom_sha256": rev.bom_sha256},
                                "verification": rev.verification, "units": "mm", "files": []}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in parts:
            prev = revs[p.key]
            with KERNEL_LOCK:
                data = S.write_part(p.shape, p.name)
            line = qty.get(p.key, {})
            fname = f"parts/{_safe(line.get('number') or p.name)}_R{prev.rev_no}.step"
            info = zipfile.ZipInfo(fname, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
            manifest["files"].append({"file": fname, "sha256": hashlib.sha256(data).hexdigest(), "definition": p.key, "name": p.name,
                                      "number": line.get("number"), "part_id": str(prev.part_id), "rev_no": prev.rev_no,
                                      "quantity": line.get("total_quantity"), "material": line.get("material"),
                                      "procurement": line.get("procurement"), "representation": line.get("representation")})
        info = zipfile.ZipInfo("manifest.json", date_time=(2000, 1, 1, 0, 0, 0))
        z.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True))
    data = buf.getvalue()
    rev.parts_zip_sha = save_blob(db, data, "application/zip")
    db.flush()
    _remember_export("parts_zip", rev, key, rev.parts_zip_sha)
    return data, rev.parts_zip_sha


# =====================================================================================================
# Views: revision, scene, compare, where-used, downstream / stale, impact
# =====================================================================================================


def _update_available(db: Session, comps: list[Comp]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    part_latest: dict[str, ModelRevision | None] = {}
    asm_latest: dict[str, AssemblyRevision | None] = {}
    for c in comps:
        if c.kind == "part" and c.part_id:
            k = str(c.part_id)
            if k not in part_latest:
                part_latest[k] = _latest_part_definition(db, c.part_id)
            lat = part_latest[k]
            if lat is not None and lat.id != c.model_revision_id:
                cur = db.get(ModelRevision, c.model_revision_id)
                if cur is None or lat.rev_no > cur.rev_no:
                    out[c.instance_id] = {"latest_revision_id": str(lat.id), "latest_rev_no": lat.rev_no}
        elif c.kind == "assembly" and c.child_assembly_id:
            k = str(c.child_assembly_id)
            if k not in asm_latest:
                asm_latest[k] = latest_revision(db, c.child_assembly_id)
            lat2 = asm_latest[k]
            if lat2 is not None and lat2.id != c.child_revision_id:
                out[c.instance_id] = {"latest_revision_id": str(lat2.id), "latest_rev_no": lat2.rev_no}
    return out


def comp_out(tree: Tree, c: Comp, update: dict[str, Any] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"kind": c.kind, "id": str(c.def_id)}
    if c.kind == "part":
        prev = tree.part_revision(str(c.model_revision_id))
        part = tree.part(c.part_id) if c.part_id else None
        d.update({"part_id": str(c.part_id), "number": part.number if part else None, "name": part.name if part else None,
                  "rev_no": prev.rev_no if prev else None, "label": f"R{prev.rev_no}" if prev else None,
                  "glb_sha": prev.glb_sha if prev else None, "mesh_sha": prev.mesh_sha if prev else None,
                  "bbox": (prev.metrics or {}).get("bbox") if prev else None,
                  "material": None if prev is None or is_unknown_material(prev.material) else prev.material})
    else:
        crev = tree.revision(str(c.child_revision_id))
        a = tree.assembly(c.child_assembly_id) if c.child_assembly_id else None
        d.update({"assembly_id": str(c.child_assembly_id), "number": a.number if a else None, "name": a.name if a else None,
                  "rev_no": crev.rev_no if crev else None, "label": f"A{crev.rev_no}" if crev else None})
    return {"instance_id": c.instance_id, "name": c.name, "definition": d, "transform": am.to_list(c.transform), "fixed": c.fixed,
            "suppressed": c.suppressed, "placement": c.placement, "display": c.display, "source_ref": c.source_ref, "meta": c.meta,
            "update_available": update}


def mate_out(m: MateRec) -> dict[str, Any]:
    return {"mate_id": m.mate_id, "kind": m.kind, "a": m.a, "b": m.b, "params": m.params, "priority": m.priority,
            "suppressed": m.suppressed, "source": m.source, "status": m.status, "result": m.result, "meta": m.meta}


def assembly_out(db: Session, asm: Assembly, latest: AssemblyRevision | None = None) -> dict[str, Any]:
    latest = latest or latest_revision(db, asm.id)
    used_by = db.scalar(select(func.count(func.distinct(AssemblyRevision.assembly_id))).select_from(AssemblyComponent)
                        .join(AssemblyRevision, AssemblyRevision.id == AssemblyComponent.revision_id)
                        .where(AssemblyComponent.child_assembly_id == asm.id)) or 0
    return {"id": str(asm.id), "project_id": str(asm.project_id), "number": asm.number, "name": asm.name, "description": asm.description,
            "status": asm.status, "created_by": str(asm.created_by), "created_at": asm.created_at.isoformat() if asm.created_at else None,
            "updated_at": asm.updated_at.isoformat() if asm.updated_at else None, "archived_at": asm.archived_at.isoformat() if asm.archived_at else None,
            "used_by_assemblies": int(used_by),
            "latest_revision": None if latest is None else revision_brief(latest)}


def revision_brief(rev: AssemblyRevision) -> dict[str, Any]:
    return {"id": str(rev.id), "assembly_id": str(rev.assembly_id), "rev_no": rev.rev_no, "label": f"A{rev.rev_no}",
            "parent_id": str(rev.parent_id) if rev.parent_id else None, "source": rev.source, "execution_policy": rev.execution_policy,
            "status": rev.status, "check_status": rev.check_status, "acceptance_status": rev.acceptance_status,
            "verification": rev.verification, "stats": rev.stats, "bom_sha256": rev.bom_sha256, "content_sha256": rev.content_sha256,
            "coverage": (rev.spec_ref or {}).get("coverage"),
            "note": rev.note, "change": rev.change, "created_by": str(rev.created_by),
            "created_at": rev.created_at.isoformat() if rev.created_at else None}


def confirm_assumption(db: Session, user: User, revision_id: uuid.UUID | str, assumption_id: str, *, value: Any = None,
                       has_value: bool = False, note: str = "") -> tuple[Any, AssemblyRevision]:
    """Confirm assumptions on an assembly revision (S5), like part revisions and drawings (``verification.confirm_assumption``): a
    person's append-only decision on one AI / rule assumption. Its own assumptions (relation datums, rule mappings) are
    decided on this revision; the values it copied from its spec version are decided on that spec version, so every
    product made from the spec sees the same decision. A correction keeps it unverified (a new revision carries the
    change). The stored verification is refreshed. Returns (record, revision); flushes, the caller commits."""
    from . import verification

    rev = _load_revision(db, revision_id)
    kwargs: dict[str, Any] = {"value": value} if has_value else {}
    spec_v = verification.assembly_assumption_owner(db, rev, assumption_id)
    if spec_v is not None:
        row = verification.confirm_assumption(db, user, spec_version=spec_v, assumption_id=assumption_id, note=note, **kwargs)
        verification.refresh_spec_dependents(db, [spec_v])  # every revision built from that spec version
    else:
        row = verification.confirm_assumption(db, user, assembly_revision=rev, assumption_id=assumption_id, note=note, **kwargs)
    verification.refresh_assembly_verification(db, rev)
    return row, rev


def revision_out(db: Session, rev: AssemblyRevision) -> dict[str, Any]:
    from .verification import assembly_verification

    comps, mates = revision_state(db, rev)
    tree = Tree(db, rev.id)
    updates = _update_available(db, comps)
    latest = latest_revision(db, rev.assembly_id)
    asm = db.get(Assembly, rev.assembly_id)
    checks = {k: (v or {}).get("latest") for k, v in (rev.checks or {}).items()}
    # S5: read live, so a decision made on a part revision or the spec version shows here at once (the stored block is
    # refreshed when this revision's checks run or its assumptions are confirmed).
    live = assembly_verification(db, rev)
    return {**revision_brief(rev), "verification": live,
            "assembly": {"id": str(asm.id), "number": asm.number, "name": asm.name} if asm else None,
            "is_latest": latest is not None and latest.id == rev.id, "latest_revision_id": str(latest.id) if latest else None,
            "components": [comp_out(tree, c, updates.get(c.instance_id)) for c in comps], "mates": [mate_out(m) for m in mates],
            "solve": rev.solve, "checks": checks, "check_history": {k: len((v or {}).get("history") or []) for k, v in (rev.checks or {}).items()},
            "assumptions": rev.assumptions, "notes": rev.notes, "spec_ref": rev.spec_ref,
            "spec_version_id": str(rev.spec_version_id) if rev.spec_version_id else None,
            "bom_counts": (rev.bom or {}).get("counts"), "source_file_version_id": str(rev.source_file_version_id) if rev.source_file_version_id else None,
            "updates_available": len(updates), "step_sha": rev.step_sha, "parts_zip_sha": rev.parts_zip_sha}


def scene(db: Session, revision_id: uuid.UUID | str) -> dict[str, Any]:
    """Viewer payload: the nested tree (local + world transforms, suppressed kept and flagged) and the unique
    part definitions with their existing GLB / mesh blobs (one mesh per definition, instanced per leaf)."""
    rev = _load_revision(db, revision_id)
    tree = Tree(db, rev.id)
    tree.stats()
    definitions: dict[str, dict[str, Any]] = {}

    def node(c: Comp, parent_world: am.Matrix4, path: tuple[str, ...], depth: int) -> dict[str, Any]:
        world = am.compose(parent_world, c.transform)
        out = comp_out(tree, c)
        out.update({"path": list(path + (c.instance_id,)), "world": am.to_list(world)})
        out.pop("meta", None)
        if c.kind == "part":
            key = str(c.model_revision_id)
            if key not in definitions:
                definitions[key] = {k: out["definition"].get(k) for k in ("part_id", "number", "name", "rev_no", "label", "glb_sha", "mesh_sha", "bbox",
                                                                          "material")}
            out["children"] = []
        elif depth < MAX_DEPTH:
            out["children"] = [node(ch, world, path + (c.instance_id,), depth + 1) for ch in tree.children(str(c.child_revision_id))]
        return out

    roots = [node(c, am.IDENTITY, (), 1) for c in tree.children(str(rev.id))]
    occurrences = [{"path": list(leaf.path), "names": list(leaf.names), "part_key": leaf.part_key, "world": am.to_list(leaf.world)}
                   for leaf in tree.leaves()]
    return {"revision_id": str(rev.id), "units": "mm", "up": "+z", "tree": roots, "definitions": definitions, "occurrences": occurrences}


def compare(db: Session, a_id: uuid.UUID, b_id: uuid.UUID) -> dict[str, Any]:
    """Changes from revision a to revision b of the same assembly (direct instances, mates, BOM quantities)."""
    a, b = _load_revision(db, a_id), _load_revision(db, b_id)
    if a.assembly_id != b.assembly_id:
        raise bad_request("different_assemblies", "both revisions must belong to the same assembly")
    ca, ma = revision_state(db, a)
    cb, mb = revision_state(db, b)
    ia, ib = {c.instance_id: c for c in ca}, {c.instance_id: c for c in cb}
    instances: list[dict[str, Any]] = []
    for iid in list(ia) + [i for i in ib if i not in ia]:
        x, y = ia.get(iid), ib.get(iid)
        if x is None:
            instances.append({"instance_id": iid, "change": "added", "name": y.name})  # type: ignore[union-attr]
            continue
        if y is None:
            instances.append({"instance_id": iid, "change": "removed", "name": x.name})
            continue
        changes = []
        if str(x.def_id) != str(y.def_id):
            changes.append({"field": "definition", "from": str(x.def_id), "to": str(y.def_id)})
        if any(abs(p - q) > 1e-9 for rp, rq in zip(x.transform, y.transform) for p, q in zip(rp, rq)):
            changes.append({"field": "transform", "from": am.to_list(x.transform), "to": am.to_list(y.transform)})
        for fld in ("suppressed", "fixed", "name"):
            if getattr(x, fld) != getattr(y, fld):
                changes.append({"field": fld, "from": getattr(x, fld), "to": getattr(y, fld)})
        if changes:
            instances.append({"instance_id": iid, "change": "modified", "name": y.name, "fields": changes})
    mates_a, mates_b = {m.mate_id: m for m in ma}, {m.mate_id: m for m in mb}
    mates: list[dict[str, Any]] = []
    for mid in list(mates_a) + [m for m in mates_b if m not in mates_a]:
        x2, y2 = mates_a.get(mid), mates_b.get(mid)
        if x2 is None:
            mates.append({"mate_id": mid, "change": "added", "kind": y2.kind})  # type: ignore[union-attr]
        elif y2 is None:
            mates.append({"mate_id": mid, "change": "removed", "kind": x2.kind})
        elif x2.content() != y2.content() or x2.status != y2.status:
            mates.append({"mate_id": mid, "change": "modified", "kind": y2.kind, "status": [x2.status, y2.status]})
    fa = {line["def_key"]: line for line in (a.bom or {}).get("flattened") or []}
    fb = {line["def_key"]: line for line in (b.bom or {}).get("flattened") or []}
    bom = []
    for key in list(fa) + [k for k in fb if k not in fa]:
        qa, qb = (fa.get(key) or {}).get("total_quantity", 0), (fb.get(key) or {}).get("total_quantity", 0)
        if qa != qb:
            bom.append({"def_key": key, "name": (fb.get(key) or fa.get(key) or {}).get("name"), "from": qa, "to": qb})
    return {"from": revision_brief(a), "to": revision_brief(b), "instances": instances, "mates": mates, "bom": bom,
            "bom_changed": a.bom_sha256 != b.bom_sha256}


def where_used(db: Session, *, part_id: uuid.UUID | None = None, assembly_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Assembly revisions that reference a part (any revision) or an assembly, with update availability."""
    stmt = select(AssemblyComponent, AssemblyRevision, Assembly).join(AssemblyRevision, AssemblyRevision.id == AssemblyComponent.revision_id) \
        .join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
    if part_id is not None:
        stmt = stmt.where(AssemblyComponent.part_id == part_id)
        latest_def = _latest_part_definition(db, part_id)
    elif assembly_id is not None:
        stmt = stmt.where(AssemblyComponent.child_assembly_id == assembly_id)
        latest_def = latest_revision(db, assembly_id)  # type: ignore[assignment]
    else:
        raise bad_request("where_used_target", "give part_id or assembly_id")
    rows = db.execute(stmt.order_by(Assembly.number, AssemblyRevision.rev_no)).all()
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    latest_by_asm: dict[uuid.UUID, AssemblyRevision | None] = {}
    for comp, rev, asm in rows:
        if asm.id not in latest_by_asm:
            latest_by_asm[asm.id] = latest_revision(db, asm.id)
        entry = grouped.setdefault(str(rev.id), {"assembly": {"id": str(asm.id), "number": asm.number, "name": asm.name},
                                                 "revision_id": str(rev.id), "rev_no": rev.rev_no,
                                                 "is_latest": latest_by_asm[asm.id] is not None and latest_by_asm[asm.id].id == rev.id,  # type: ignore[union-attr]
                                                 "instances": [], "definition_revisions": [], "update_available": False})
        entry["instances"].append({"instance_id": comp.instance_id, "name": comp.name, "suppressed": comp.suppressed})
        dk = str(comp.model_revision_id or comp.child_revision_id)
        if dk not in entry["definition_revisions"]:
            entry["definition_revisions"].append(dk)
        if latest_def is not None and dk != str(latest_def.id):
            entry["update_available"] = True
    return list(grouped.values())


def downstream(db: Session, revision_id: uuid.UUID | str) -> dict[str, Any]:
    """Artifacts bound to this exact revision (asset versions depending on it — assembly drawings, delivery
    manifests, parent assemblies — and roll-ups), each marked stale when a newer revision of this assembly
    exists. Nothing is rewritten: an old baseline and what was derived from it stay readable."""
    rev = _load_revision(db, revision_id)
    latest = latest_revision(db, rev.assembly_id)
    stale = latest is not None and latest.id != rev.id
    reason = None if not stale else {"code": "newer_assembly_revision", "latest_revision_id": str(latest.id), "latest_rev_no": latest.rev_no}  # type: ignore[union-attr]
    items: list[dict[str, Any]] = []
    v = assets.version_for(db, "assembly_model", rev.id)
    if v is not None:
        for edge in db.scalars(select(AssetDependency).where(AssetDependency.to_version_id == v.id)).all():
            ref = assets._edge_ref(db, edge.from_version_id, edge.relation)
            items.append({"kind": ref.get("asset_type"), "asset_version": ref, "stale": stale, "reason": reason})
    for r in db.scalars(select(AssemblyRollup).where(AssemblyRollup.revision_id == rev.id).order_by(AssemblyRollup.created_at)).all():
        items.append({"kind": "assembly_rollup", "id": str(r.id), "created_at": r.created_at.isoformat() if r.created_at else None,
                      "stale": stale, "reason": reason})
    # parents that froze this revision (they show "update available", not stale content)
    for comp, prev, pasm in db.execute(select(AssemblyComponent, AssemblyRevision, Assembly)
                                       .join(AssemblyRevision, AssemblyRevision.id == AssemblyComponent.revision_id)
                                       .join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
                                       .where(AssemblyComponent.child_revision_id == rev.id)).all():
        items.append({"kind": "parent_assembly", "assembly": {"id": str(pasm.id), "number": pasm.number, "name": pasm.name},
                      "revision_id": str(prev.id), "rev_no": prev.rev_no, "instance_id": comp.instance_id, "update_available": stale,
                      "stale": False})
    return {"revision": revision_brief(rev), "latest_revision_id": str(latest.id) if latest else None, "stale": stale, "items": items}


def impact(db: Session, *, assembly_id: uuid.UUID, base_revision_id: uuid.UUID, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Dry run of an edit: affected instances and BOM lines, and everything that would become stale or show an
    update (downstream of the base revision, parent assemblies). Nothing is written."""
    asm = db.get(Assembly, assembly_id)
    if asm is None:
        raise not_found("assembly")
    base = _load_revision(db, base_revision_id)
    if base.assembly_id != asm.id:
        raise not_found("assembly revision")
    counters = (asm.instance_counter, asm.mate_counter)
    try:
        comps, mates = revision_state(db, base)
        change = apply_ops(db, asm, comps, mates, ops)
        tree = Tree(db, Tree.ROOT, override=comps)
        tree.target_items = target_items(base.spec_ref)  # type: ignore[attr-defined]
        _validate_definitions(db, asm, comps, tree)
        stats = tree.stats()
        new_bom = compute_bom(db, tree)
    finally:
        asm.instance_counter, asm.mate_counter = counters  # a preview consumes no instance / mate ids
    fa = {line["def_key"]: line for line in (base.bom or {}).get("flattened") or []}
    fb = {line["def_key"]: line for line in new_bom["flattened"]}
    bom_changes = []
    for key in list(fa) + [k for k in fb if k not in fa]:
        qa, qb = (fa.get(key) or {}).get("total_quantity", 0), (fb.get(key) or {}).get("total_quantity", 0)
        if qa != qb:
            bom_changes.append({"def_key": key, "name": (fb.get(key) or fa.get(key) or {}).get("name"), "from": qa, "to": qb})
    down = downstream(db, base.id)
    # definitions entering the BOM: do their exact part revisions have a process plan / quote yet?
    process: list[dict[str, Any]] = []
    for key in [k for k in fb if k not in fa]:
        plan = db.scalar(select(ProcessPlan).where(ProcessPlan.model_revision_id == uuid.UUID(key)).order_by(ProcessPlan.created_at.desc()).limit(1))
        quote = db.scalar(select(Quote).where(Quote.process_plan_id == plan.id).limit(1)) if plan else None
        process.append({"def_key": key, "name": fb[key].get("name"), "process_plan_id": str(plan.id) if plan else None,
                        "quote_id": str(quote.id) if quote else None,
                        "status": "ready" if quote else ("plan_only" if plan else "missing")})
    return {"base_revision": revision_brief(base), "ops": change["ops"], "affected_instances": change["affected_instances"],
            "notes": change["notes"], "stats": stats, "bom_changes": bom_changes, "bom_changed": bom_hash(new_bom) != base.bom_sha256,
            "process_quote_inputs": process,
            "will_be_stale": [dict(i, stale=True) for i in down["items"] if i["kind"] != "parent_assembly"],
            "parents_update_available": [i for i in down["items"] if i["kind"] == "parent_assembly"]}


# =====================================================================================================
# Process / quote roll-up
# =====================================================================================================


def _tier_for(quote: Quote, qty: int) -> tuple[dict[str, Any] | None, str]:
    tiers = sorted((quote.result or {}).get("tiers") or [], key=lambda t: t.get("qty", 0))
    exact = next((t for t in tiers if t.get("qty") == qty), None)
    if exact is not None:
        return exact, "exact_tier"
    lower = [t for t in tiers if t.get("qty", 0) <= qty]
    if lower:
        return lower[-1], "nearest_lower_tier"
    return None, "no_tier_at_or_below_quantity"


def rollup(db: Session, revision_id: uuid.UUID, user: User, *, quantity: int = 1, buy_costs: dict[str, Any] | None = None,
           assembly_costs: dict[str, Any] | None = None, persist: bool = True,
           pinned_sources: dict[str, dict[str, str | None]] | None = None) -> dict[str, Any]:
    """Process / quote roll-up of the frozen BOM: each part line uses that exact part revision's latest plan and
    quote (needed quantity = BOM total × ``quantity``); buy / standard lines and assembly labour come only from
    explicit inputs (``buy_costs`` / ``assembly_costs`` keyed by definition id, part id or assembly id:
    {"unit_fen", "source", "estimated"?}). Missing data stays unknown (never 0); the total is only given when
    nothing is unknown, else the known part is labelled partial. Turbo / unverified state comes from the
    revision's verification block. With ``pinned_sources`` the stored BOM and only the explicitly selected
    plan/quote IDs are used; absent rows never fall back to another run's newer products."""
    rev = _load_revision(db, revision_id)
    if quantity < 1 or quantity > 1_000_000:
        raise bad_request("invalid_quantity", "quantity must be between 1 and 1,000,000")
    buy_costs = buy_costs or {}
    assembly_costs = assembly_costs or {}
    tree = Tree(db, rev.id)
    bom = rev.bom if pinned_sources is not None else compute_bom(db, tree)
    if pinned_sources is not None and set(pinned_sources) != {line["def_key"] for line in bom["flattened"]}:
        raise bad_request("rollup_source_mismatch", "pinned manufacturing sources must cover the exact frozen BOM")
    lines: list[dict[str, Any]] = []
    sources: list[str] = []
    for line in bom["flattened"]:
        need = int(line["total_quantity"]) * quantity
        key, part_id = line["def_key"], line.get("part_id")
        if pinned_sources is None:
            plan = db.scalar(select(ProcessPlan).where(ProcessPlan.model_revision_id == uuid.UUID(key)).order_by(ProcessPlan.created_at.desc()).limit(1))
            quote = db.scalar(select(Quote).where(Quote.process_plan_id == plan.id).order_by(Quote.created_at.desc()).limit(1)) if plan else None
        else:
            selected = pinned_sources[key]
            try:
                plan = db.get(ProcessPlan, uuid.UUID(selected["process_plan_id"])) if selected.get("process_plan_id") else None
                quote = db.get(Quote, uuid.UUID(selected["quote_id"])) if selected.get("quote_id") else None
            except (ValueError, TypeError, AttributeError):
                raise bad_request("rollup_source_mismatch", "invalid pinned manufacturing identity") from None
            if ((selected.get("process_plan_id") and plan is None) or (selected.get("quote_id") and quote is None)
                    or (plan is not None and str(plan.model_revision_id) != key)
                    or (quote is not None and (plan is None or quote.process_plan_id != plan.id))):
                raise bad_request("rollup_source_mismatch", "pinned plan or quote does not match the exact BOM revision")
        given = buy_costs.get(key) or (buy_costs.get(part_id) if part_id else None)
        entry: dict[str, Any] = {"item": line["item"], "def_key": key, "part_id": part_id, "name": line["name"], "number": line["number"],
                                 "procurement": line["procurement"], "quantity_per_assembly": line["total_quantity"], "quantity": need,
                                 "process_plan_id": str(plan.id) if plan else None, "quote_id": str(quote.id) if quote else None,
                                 "unit_fen": None, "total_fen": None, "status": "unknown", "estimated": False, "basis": None}
        if plan is not None:
            entry["process"] = {"plan_id": str(plan.id), "estimated": bool((plan.plan or {}).get("estimated")),
                                "total_cycle_min": (plan.plan or {}).get("total_cycle_min"), "inputs_hash": plan.inputs_hash}
            sources.append(plan.inputs_hash)
        if given is not None and line["procurement"] in ("buy", "standard", "unknown"):
            unit = given.get("unit_fen") if isinstance(given, dict) else None
            if isinstance(unit, int) and unit >= 0:
                entry.update({"unit_fen": unit, "total_fen": unit * need, "status": "known", "estimated": bool(given.get("estimated")),
                              "basis": {"kind": "input", "source": given.get("source") or "user input"}})
                sources.append(_canon(given))
        elif quote is not None and line["procurement"] in ("make", "unknown"):
            tier, how = _tier_for(quote, need)
            if tier is not None:
                unit = int(tier.get("unit_inc_tax_fen"))
                est = bool((quote.result or {}).get("estimated")) or how != "exact_tier"
                entry.update({"unit_fen": unit, "total_fen": unit * need, "status": "known", "estimated": est,
                              "basis": {"kind": "quote", "quote_id": str(quote.id), "tier_qty": tier.get("qty"), "tier": how,
                                        "quote_estimated": bool((quote.result or {}).get("estimated"))}})
                sources.append(quote.inputs_hash)
            else:
                entry["basis"] = {"kind": "quote", "quote_id": str(quote.id), "tier": how}
        if entry["status"] == "unknown" and entry["basis"] is None:
            entry["basis"] = {"kind": "missing", "reason": "no quote for this exact part revision" if line["procurement"] in ("make", "unknown")
                              else "no purchase cost given"}
        lines.append(entry)
    # assembly labour per assembly definition x its instances (root included); repeated sub-assemblies are
    # billed once per instance, their parts are already counted per leaf above (no double billing)
    labour: list[dict[str, Any]] = []
    counts: dict[str, int] = {tree.root: 1}

    def count(rid: str, mult: int, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for c in tree.children(rid):
            if c.kind == "assembly" and not c.suppressed:
                k = str(c.child_revision_id)
                counts[k] = counts.get(k, 0) + mult
                count(k, mult, depth + 1)

    count(tree.root, 1, 0)
    for rid, n in counts.items():
        crev = tree.revision(rid)
        a = tree.assembly(crev.assembly_id) if crev else None
        given = assembly_costs.get(rid) or (assembly_costs.get(str(a.id)) if a else None)
        e: dict[str, Any] = {"def_key": rid, "assembly_id": str(a.id) if a else None, "name": a.name if a else None,
                             "number": a.number if a else None, "quantity": n * quantity, "unit_fen": None, "total_fen": None,
                             "status": "unknown", "estimated": False, "basis": {"kind": "missing", "reason": "no assembly labour rate given"}}
        if isinstance(given, dict) and isinstance(given.get("unit_fen"), int) and given["unit_fen"] >= 0:
            e.update({"unit_fen": given["unit_fen"], "total_fen": given["unit_fen"] * n * quantity, "status": "known",
                      "estimated": bool(given.get("estimated")), "basis": {"kind": "input", "source": given.get("source") or "user input"}})
            sources.append(_canon(given))
        labour.append(e)
    known = [x for x in lines + labour if x["status"] == "known"]
    unknown = [x for x in lines + labour if x["status"] != "known"]
    known_sum = sum(int(x["total_fen"]) for x in known)
    cycle = [ln.get("process", {}).get("total_cycle_min") for ln in lines]
    result = {
        "schema": "partroute.assembly_rollup/1", "revision_id": str(rev.id), "rev_no": rev.rev_no, "bom_sha256": rev.bom_sha256,
        "quantity": quantity, "currency": "CNY", "lines": lines, "assembly_labour": labour,
        "known_fen": known_sum, "total_fen": known_sum if not unknown else None, "complete": not unknown,
        "unknown_count": len(unknown), "estimated": any(x["estimated"] for x in known),
        "labels": (["estimated"] if any(x["estimated"] for x in known) else []) + (["partial"] if unknown else [])
                  + (["unverified"] if (rev.verification or {}).get("unverified") else []),
        "process": {"lines_with_plan": sum(1 for ln in lines if ln.get("process")), "lines_without_plan": sum(1 for ln in lines if not ln.get("process")),
                    "total_cycle_min": None if any(c is None for c in cycle) else round(sum(float(c) * ln["quantity"] for c, ln in zip(cycle, lines)), 3)},
        "verification": rev.verification,
    }
    input_data = {"bom": rev.bom_sha256, "quantity": quantity, "sources": sorted(sources), "buy": buy_costs, "asm": assembly_costs}
    if pinned_sources is not None:
        input_data.update(pinned_sources=pinned_sources, frozen_bom=_canon(bom))
    inputs = _canon(input_data)
    result["inputs_hash"] = inputs
    if persist:
        row = AssemblyRollup(revision_id=rev.id, execution_policy=rev.execution_policy, bom_sha256=rev.bom_sha256, inputs_hash=inputs,
                             params={"quantity": quantity, "buy_costs": buy_costs, "assembly_costs": assembly_costs,
                                     **({"pinned_sources": pinned_sources} if pinned_sources is not None else {})}, result=result,
                             created_by=user.id)
        db.add(row)
        db.flush()
        result["id"] = str(row.id)
        asm = db.get(Assembly, rev.assembly_id)
        audit(db, action="assembly_rollup.create", subject_type="assembly_rollup", subject_id=row.id, user_id=user.id,
              project_id=asm.project_id if asm else None, data={"inputs_hash": inputs})
    return result


def rollup_out(row: AssemblyRollup, stale: bool) -> dict[str, Any]:
    return {**row.result, "id": str(row.id), "created_at": row.created_at.isoformat() if row.created_at else None,
            "created_by": str(row.created_by), "stale": stale}


def asdict_line(line: BomLine) -> dict[str, Any]:
    d = asdict(line)
    d["instance_paths"] = [list(p) for p in line.instance_paths]
    return d


# --- generation reliability §2.6 (package L): where each AI decision of a delivery was actually used --------------------


def decision_consumers(db: Session, assumptions: list[dict[str, Any]], *, rev: AssemblyRevision | None, part_revs: list[ModelRevision],
                       files: list[dict[str, Any]] | None = None, part_drawings: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The delivery's assumptions (each with its ``owner``), each with ``consumed_by: [{stage, object, ref}]`` — where it
    was actually used — when it was used anywhere (design §2.6; brief WP06 / AC14: an adopted value must really reach
    modeling and placement). Read from what was built, never from the decision itself:

    - ``modeling``: a part revision whose stored modeling request (``request_text``) carries the decision's id — a spec
      decision passed down as a known value (``orchestration.definition_request``) — or that owns the decision;
    - ``placement``: the assembly revision places what the decision is about — a spec instance / relation it names, the
      instances of a part definition modeled with it — or owns it (an AI / rule placement); ``ref``: those spec keys.
      Only instances placed along their whole parent chain count (review of L): a suppressed instance, one only
      arranged for display (placement ``unplaced`` / ``arranged``) and what lies inside such a sub-assembly instance
      are not placed; a decision owned by a part revision is never a placement one;
    - ``export``: the delivered STEP file of such a part revision / assembly revision;
    - ``drawing``: the delivered drawing package of that assembly revision, or the package that owns the decision; a
      decision owned by a part revision: the package holding that part's delivered detail sheet (``part_drawings``:
      model revision id → package id).

    ``object``: ``{type, id, rev_no?, label?}`` like an owner, or ``{type: "file", path}``. Pure reads."""
    files = list(files or [])
    held: dict[str, str] = {}  # placed spec instance key → spec definition key, at every level of the assembly revision
    by_def: dict[str, list[str]] = {}
    def_of_rev: dict[str, str] = {}  # model revision id → spec definition key
    rel_keys: set[str] = set()
    asm_obj: dict[str, Any] | None = None
    if rev is not None:
        tree = Tree(db, rev.id)
        rids: list[uuid.UUID] = []  # revisions whose own relations are placed in the root frame

        def walk(rid: str, depth: int, placed: bool) -> None:
            if depth > MAX_DEPTH:
                return
            if placed:
                rids.append(uuid.UUID(rid))
            for c in tree.children(rid):
                if c.suppressed:
                    continue
                here = placed and c.placement.get("mode") != "unplaced" and not c.placement.get("arranged")
                sk, dk = c.meta.get("spec_key"), c.meta.get("definition_spec_key")
                if sk and dk and here:
                    held[str(sk)] = str(dk)
                    by_def.setdefault(str(dk), [])
                    if str(sk) not in by_def[str(dk)]:
                        by_def[str(dk)].append(str(sk))
                if c.kind == "part" and dk and c.model_revision_id:
                    def_of_rev[str(c.model_revision_id)] = str(dk)
                elif c.kind == "assembly" and c.child_revision_id:
                    walk(str(c.child_revision_id), depth + 1, here)

        walk(str(rev.id), 0, True)
        rel_keys = {str(m.meta.get("spec_key")) for m in db.scalars(select(AssemblyMate).where(AssemblyMate.revision_id.in_(rids))).all()
                    if (m.meta or {}).get("spec_key") and not m.suppressed}
        asm = db.get(Assembly, rev.assembly_id)
        asm_obj = {"type": "assembly_revision", "id": str(rev.id), "rev_no": rev.rev_no,
                   **({"label": f"{asm.number} A{rev.rev_no}"} if asm is not None else {})}
    parts: dict[str, tuple[ModelRevision, dict[str, Any]]] = {}
    for p in part_revs:
        if str(p.id) not in parts:
            part = db.get(Part, p.part_id)
            parts[str(p.id)] = (p, {"type": "model_revision", "id": str(p.id), "rev_no": p.rev_no,
                                    **({"label": f"{part.number} R{p.rev_no}"} if part is not None else {})})
    step_of = {str((f.get("source") or {}).get("model_revision_id") or (f.get("source") or {}).get("assembly_revision_id")): f["path"]
               for f in files if f.get("kind") in ("part_step", "assembly_step") and isinstance(f.get("source"), dict)}
    packages = list(dict.fromkeys(str((f.get("source") or {}).get("package_id")) for f in files
                                  if f.get("item") == "drawings" and (f.get("source") or {}).get("package_id")))

    def mentions(text: str | None, aid: str) -> bool:
        return bool(text) and re.search(rf"(?<![\w-]){re.escape(aid)}(?![\w-])", text or "") is not None

    out: list[dict[str, Any]] = []
    for a in assumptions:
        owner = a.get("owner") if isinstance(a.get("owner"), dict) else {}
        kind, oid, aid = owner.get("type"), str(owner.get("id")), str(a.get("id") or "")
        names = [str(a.get("subject") or "").split(".")[0], *(str(x) for x in a.get("affected") or [])]
        uses: list[dict[str, Any]] = []
        modeled: list[str] = []  # spec definition keys of the part revisions that used it
        if kind == "engineering_spec_version" and aid:
            for rid, (p, obj) in parts.items():
                if mentions(p.request_text, aid):
                    dk = def_of_rev.get(rid) or names[0] or None
                    uses.append({"stage": "modeling", "object": obj, "ref": dk})
                    modeled += [dk] if dk else []
        elif kind == "model_revision" and oid in parts:
            # a decision of that revision's own build (e.g. a font substitution): its modeling, its STEP and its detail
            # sheet — never the placement of the assembly
            dk = def_of_rev.get(oid)
            uses.append({"stage": "modeling", "object": parts[oid][1], "ref": dk})
            pkg = (part_drawings or {}).get(oid)
            if pkg:
                uses.append({"stage": "drawing", "object": {"type": "assembly_drawing", "id": str(pkg)}, "ref": dk})
        elif kind == "assembly_drawing":
            uses.append({"stage": "drawing", "object": {"type": "assembly_drawing", "id": oid}, "ref": a.get("subject")})
        if asm_obj is not None and kind in ("engineering_spec_version", "assembly_revision"):
            refs = [n for n in names if n in held or n in rel_keys] + [k for dk in modeled for k in by_def.get(dk, [])]
            if kind == "assembly_revision" and oid == asm_obj["id"] and not refs:
                refs = [str(a.get("subject"))]
            refs = list(dict.fromkeys(refs))
            if refs and (kind != "assembly_revision" or oid == asm_obj["id"]):
                uses.append({"stage": "placement", "object": asm_obj, "ref": refs})
        for u in list(uses):  # what was delivered from the objects that used it
            if u["stage"] in ("modeling", "placement") and step_of.get(u["object"]["id"]):
                uses.append({"stage": "export", "object": {"type": "file", "path": step_of[u["object"]["id"]]}, "ref": u["ref"]})
            if u["stage"] == "placement":
                uses += [{"stage": "drawing", "object": {"type": "assembly_drawing", "id": pid}, "ref": u["ref"]} for pid in packages]
        out.append({**a, "consumed_by": uses} if uses else a)
    return out


__all__ = ["bom_lines", "create_assembly", "create_from_spec", "edit_revision", "export_step", "import_step", "resolve_geometry",
           "rollup", "run_checks"]
