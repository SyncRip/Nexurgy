"""Bounded, read-only references to the exact drawings a run has already persisted.

Page metadata and bytes stay behind the existing drawing endpoints. Discovering a committed package does not
require the drawing node to finish, but never borrows the latest package of an assembly or part.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.orm import Session

from ..models import Assembly, AssemblyComponent, AssemblyDrawing, AssemblyRevision, Drawing, ModelRevision, NodeRun, Part, WorkflowRun

_ACTIVE = {"pending", "ready", "running", "waiting_human"}


@dataclass
class _Source:
    node: NodeRun
    revision_id: uuid.UUID
    explicit: set[tuple[uuid.UUID, uuid.UUID]]
    discover: bool
    layered: bool = False


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _sources(run: WorkflowRun, nodes: list[NodeRun], limit: int) -> tuple[list[_Source], bool]:
    by_node = {nr.node_id: nr for nr in nodes}
    graph = run.graph_snapshot or {}
    params = {n.get("id"): n.get("params") or {} for n in graph.get("nodes") or []}
    sources, cut = [], False
    for nr in nodes:
        if nr.node_type not in ("drawing", "assembly_drawing"):
            continue
        assembly = nr.node_type == "assembly_drawing"
        port, key = ("assembly", "assembly_revision_id") if assembly else ("model", "revision_id")
        value = (nr.inputs or {}).get(port)
        if not isinstance(value, dict):
            # Inputs are not necessarily published until the handler returns; the frozen graph identifies the
            # same exact upstream output while a layered handler checkpoints its completed packages.
            for edge in graph.get("edges") or []:
                if edge.get("target") == nr.node_id and edge.get("target_port") == port:
                    parent = by_node.get(edge.get("source"))
                    value = (parent.outputs or {}).get(edge.get("source_port")) if parent else None
                    break
        revision_id = _uuid(value.get(key)) if isinstance(value, dict) and value.get("type") == port else None
        if revision_id is None:
            continue
        output = (nr.outputs or {}).get("drawings" if assembly else "drawing")
        source = _Source(nr, revision_id, set(), not isinstance(output, dict) or nr.status in _ACTIVE,
                         assembly and params.get(nr.node_id, {}).get("subassemblies") is True)
        if isinstance(output, dict):
            rid = _uuid(output.get("package_id" if assembly else "drawing_id"))
            if rid is not None:
                source.explicit.add((rid, revision_id))
            subs = output.get("subassembly_packages") if source.layered else None
            if isinstance(subs, list):
                cut |= len(subs) > limit
                for sub in subs[:limit]:
                    if not isinstance(sub, dict):
                        continue
                    pid, sid = _uuid(sub.get("package_id")), _uuid(sub.get("assembly_revision_id"))
                    if pid is not None and sid is not None:
                        source.explicit.add((pid, sid))
        sources.append(source)
    return sources, cut


def _query_keys(values: Iterable[Any], limit: int) -> tuple[list[Any], bool]:
    """Deduplicate and cap each expanded IN list across the whole view, before SQL binds are constructed.

    A result LIMIT does not bound parameters: many stages can each reference hundreds of packages. Keep the
    first distinct keys in graph order, retaining all source associations separately, and declare any lost key.
    """
    kept, seen, cut = [], set(), False
    for value in values:
        if value in seen:
            continue
        if len(kept) == limit:
            cut = True
            continue
        kept.append(value)
        seen.add(value)
    return kept, cut


def _part_rows(db: Session, project_id: uuid.UUID, sources: list[_Source], limit: int) -> tuple[list[Any], bool]:
    if not sources:
        return [], False
    owned, owned_cut = _query_keys(((s.node.id, s.revision_id) for s in sources if s.discover), limit + 1)
    explicit, explicit_cut = _query_keys((pair for s in sources for pair in sorted(s.explicit)), limit + 1)
    rows = list(db.execute(
        select(Drawing.id, Drawing.model_revision_id.label("revision_id"), Drawing.model_revision_id.label("root_id"),
               Drawing.manifest_sha256, Drawing.node_run_id, Drawing.created_at)
        .join(Part, Part.id == Drawing.part_id)
        .join(ModelRevision, and_(ModelRevision.id == Drawing.model_revision_id, ModelRevision.part_id == Drawing.part_id))
        .where(Part.project_id == project_id, or_(
            tuple_(Drawing.node_run_id, Drawing.model_revision_id).in_(owned),
            tuple_(Drawing.id, Drawing.model_revision_id).in_(explicit),
        ))
        .order_by(Drawing.created_at, Drawing.id).limit(limit + 1)
    ).all())
    return rows, owned_cut or explicit_cut


def _assembly_rows(db: Session, project_id: uuid.UUID, sources: list[_Source], limit: int) -> tuple[list[Any], bool]:
    if not sources:
        return [], False
    roots, roots_cut = _query_keys((s.revision_id for s in sources), limit + 1)
    kept_roots = set(roots)
    tree = (select(AssemblyRevision.id.label("root_id"), AssemblyRevision.id.label("revision_id"),
                   AssemblyRevision.assembly_id)
            .join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
            .where(Assembly.project_id == project_id, AssemblyRevision.id.in_(roots)).cte("drawing_sources", recursive=True))
    # UNION deduplicates repeated instances and terminates even if a malformed graph contains a cycle. Only
    # unsuppressed child revisions in this project are reachable; newer revisions of the same assembly are not.
    tree = tree.union(
        select(tree.c.root_id, AssemblyRevision.id, AssemblyRevision.assembly_id)
        .select_from(tree).join(AssemblyComponent, AssemblyComponent.revision_id == tree.c.revision_id)
        .join(AssemblyRevision, and_(AssemblyRevision.id == AssemblyComponent.child_revision_id,
                                    AssemblyRevision.assembly_id == AssemblyComponent.child_assembly_id))
        .join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
        .where(AssemblyComponent.definition_kind == "assembly", AssemblyComponent.suppressed.is_(False),
               Assembly.project_id == project_id)
    )
    layered, layered_cut = _query_keys(((s.node.id, s.revision_id) for s in sources
                                        if s.revision_id in kept_roots and s.discover and s.layered), limit + 1)
    direct, direct_cut = _query_keys(((s.node.id, s.revision_id) for s in sources
                                     if s.revision_id in kept_roots and s.discover and not s.layered), limit + 1)
    explicit, explicit_cut = _query_keys(((did, rid, s.revision_id) for s in sources if s.revision_id in kept_roots
                                         for did, rid in sorted(s.explicit)), limit + 1)
    rows = list(db.execute(
        select(AssemblyDrawing.id, AssemblyDrawing.assembly_revision_id.label("revision_id"), tree.c.root_id,
               AssemblyDrawing.manifest_sha256, AssemblyDrawing.completeness, AssemblyDrawing.node_run_id,
               AssemblyDrawing.created_at, AssemblyDrawing.parent_id, AssemblyDrawing.resumed_from_id)
        .join(tree, and_(tree.c.revision_id == AssemblyDrawing.assembly_revision_id,
                         tree.c.assembly_id == AssemblyDrawing.assembly_id))
        .where(AssemblyDrawing.project_id == project_id, or_(
            and_(AssemblyDrawing.parent_id.is_(None), or_(
                tuple_(AssemblyDrawing.node_run_id, tree.c.root_id).in_(layered),
                and_(AssemblyDrawing.assembly_revision_id == tree.c.root_id,
                     tuple_(AssemblyDrawing.node_run_id, tree.c.root_id).in_(direct)),
            )),
            tuple_(AssemblyDrawing.id, AssemblyDrawing.assembly_revision_id, tree.c.root_id).in_(explicit),
        ))
        .order_by(AssemblyDrawing.created_at, AssemblyDrawing.id, tree.c.root_id).limit(limit + 1)
    ).all())
    return rows, roots_cut or layered_cut or direct_cut or explicit_cut


def view(db: Session, run: WorkflowRun, nodes: list[NodeRun], *, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Nodes arrive in graph order; at most two batched metadata queries discover saved drawing references."""
    sources, cut = _sources(run, nodes, limit)
    parts = [s for s in sources if s.node.node_type == "drawing"]
    assemblies = [s for s in sources if s.node.node_type == "assembly_drawing"]
    candidates: list[tuple[_Source, Any]] = []
    for group, (rows, keys_cut) in ((parts, _part_rows(db, run.project_id, parts, limit)),
                                   (assemblies, _assembly_rows(db, run.project_id, assemblies, limit))):
        cut |= keys_cut or len(rows) > limit
        for row in rows:
            for source in group:
                if source.revision_id != row.root_id:
                    continue
                exact = (row.id, row.revision_id) in source.explicit
                own = (source.discover and row.node_run_id == source.node.id
                       and (source.layered or row.revision_id == source.revision_id)
                       and getattr(row, "parent_id", None) is None)
                if exact or own:
                    candidates.append((source, row))
    # A continuation replaces its own prior partial package. Explicit terminal output IDs remain exact even if
    # a later manual overlay or another run has produced a new package for that revision.
    superseded = {(s.node.id, r.resumed_from_id) for s, r in candidates
                  if s.discover and getattr(r, "resumed_from_id", None) is not None}
    order = {nr.id: i for i, nr in enumerate(nodes)}
    candidates.sort(key=lambda item: (order[item[0].node.id], item[1].created_at, item[1].id))
    out, seen = [], set()
    for source, row in candidates:
        kind = "assembly_drawing" if source.node.node_type == "assembly_drawing" else "drawing"
        # Multiple stages may explicitly reuse one immutable resource. Keep every stage association so that a
        # consumer can distinguish reused output from no output; image rendering can deduplicate the resource ID.
        identity = (source.node.id, kind, row.id)
        if identity in seen or (source.node.id, row.id) in superseded:
            continue
        seen.add(identity)
        ref = {"kind": kind, "id": str(row.id), "node_id": source.node.node_id, "manifest_sha256": row.manifest_sha256}
        if kind == "drawing":
            ref["model_revision_id"] = str(row.revision_id)
        else:
            ref.update(assembly_revision_id=str(row.revision_id), completeness=row.completeness)
        out.append(ref)
    return out[:limit], cut or len(out) > limit
