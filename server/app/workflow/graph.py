"""Workflow graph model and validation (DAG, port types, required inputs)."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .nodes import NODE_DEFS, BusinessTaskParams


class Position(BaseModel):
    x: float = 0
    y: float = 0


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_.:-]+$")
    type: str
    position: Position = Field(default_factory=Position)
    params: dict[str, Any] = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=120)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    source: str
    source_port: str
    target: str
    target_port: str


class GraphGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str = ""
    node_ids: list[str] = Field(default_factory=list)
    color: str | None = None


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[GraphNode] = Field(default_factory=list, max_length=200)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=600)
    groups: list[GraphGroup] = Field(default_factory=list, max_length=50)


def _err(code: str, message: str, **where: str) -> dict[str, str]:
    return {"code": code, "message": message, **where}


def compatible(src_type: str, dst_type: str) -> bool:
    return src_type == dst_type or "any" in (src_type, dst_type)


def topo_order(graph: Graph) -> list[str] | None:
    """Kahn's algorithm; None when the graph has a cycle."""
    indeg = {n.id: 0 for n in graph.nodes}
    out: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges:
        if e.source in indeg and e.target in indeg:
            out[e.source].append(e.target)
            indeg[e.target] += 1
    queue = deque(sorted(n for n, d in indeg.items() if d == 0))
    order: list[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for m in sorted(out[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return order if len(order) == len(indeg) else None


def validate_graph(raw: dict | Graph) -> tuple[Graph | None, list[dict[str, str]]]:
    """Return (graph, errors). An empty error list means the graph can run."""
    try:
        graph = raw if isinstance(raw, Graph) else Graph.model_validate(raw)
    except ValidationError as exc:
        return None, [_err("invalid_graph", e["msg"], loc=".".join(str(p) for p in e["loc"])) for e in exc.errors()]

    errors: list[dict[str, str]] = []
    if not graph.nodes:
        errors.append(_err("empty_graph", "add at least one node"))
    nodes = {}
    for n in graph.nodes:
        if n.id in nodes:
            errors.append(_err("duplicate_node", f"node id {n.id} is used twice", node_id=n.id))
        nodes[n.id] = n
        d = NODE_DEFS.get(n.type)
        if d is None:
            errors.append(_err("unknown_node_type", f"unknown node type {n.type}", node_id=n.id))
            continue
        try:
            d.params.model_validate(n.params)
        except ValidationError as exc:
            for e in exc.errors():
                errors.append(_err("invalid_params", e["msg"], node_id=n.id, param=".".join(str(p) for p in e["loc"])))

    incoming: dict[tuple[str, str], list[str]] = defaultdict(list)
    seen_edges: set[str] = set()
    for e in graph.edges:
        if e.id in seen_edges:
            errors.append(_err("duplicate_edge", f"edge id {e.id} is used twice", edge_id=e.id))
        seen_edges.add(e.id)
        src, dst = nodes.get(e.source), nodes.get(e.target)
        if src is None or dst is None:
            errors.append(_err("dangling_edge", "edge connects a missing node", edge_id=e.id))
            continue
        if e.source == e.target:
            errors.append(_err("self_loop", "a node cannot connect to itself", edge_id=e.id))
            continue
        sd, dd = NODE_DEFS.get(src.type), NODE_DEFS.get(dst.type)
        if sd is None or dd is None:
            continue
        sp, dp = sd.output(e.source_port), dd.input(e.target_port)
        if sp is None:
            errors.append(_err("unknown_port", f"{src.type} has no output {e.source_port}", edge_id=e.id))
            continue
        if dp is None:
            errors.append(_err("unknown_port", f"{dst.type} has no input {e.target_port}", edge_id=e.id))
            continue
        if not compatible(sp.type, dp.type):
            errors.append(_err("type_mismatch", f"{sp.type} cannot connect to {dp.type}", edge_id=e.id))
        incoming[(e.target, e.target_port)].append(e.id)
        if len(incoming[(e.target, e.target_port)]) > 1 and not dp.multiple:
            errors.append(_err("port_taken", f"input {e.target_port} accepts one connection", edge_id=e.id, node_id=e.target))

    predecessors: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        if e.source in nodes and e.target in nodes:
            predecessors[e.target].add(e.source)
    for n in graph.nodes:
        d = NODE_DEFS.get(n.type)
        if d is None:
            continue
        connected = {port for (nid, port) in incoming if nid == n.id}
        for p in d.inputs:
            if p.required and p.name not in connected:
                errors.append(_err("missing_input", f"connect input '{p.name}'", node_id=n.id, port=p.name))
        errors.extend(_rule_errors(n, d.rule, connected, nodes=nodes, predecessors=predecessors[n.id]))

    # A collaboration graph holds business_task nodes only (design §3.3): tool
    # nodes cannot be mixed in, and each offender is named (A03 / A33).
    if any(n.type == "business_task" for n in graph.nodes):
        for n in graph.nodes:
            if n.type != "business_task" and n.type in NODE_DEFS:
                errors.append(_err("collaboration_mixed_nodes", f"a collaboration workflow cannot contain a {n.type} node", node_id=n.id))

    if graph.nodes and topo_order(graph) is None:
        errors.append(_err("cycle", "the graph contains a cycle; workflows must be acyclic"))
    for g in graph.groups:
        for nid in g.node_ids:
            if nid not in nodes:
                errors.append(_err("group_missing_node", f"group {g.id} references missing node {nid}"))
    return graph, errors


def _rule_errors(
    n: GraphNode,
    rule: str | None,
    connected: set[str],
    *,
    nodes: dict[str, GraphNode] | None = None,
    predecessors: set[str] | None = None,
) -> list[dict[str, str]]:
    p = n.params
    if rule == "business_task":
        return _business_task_errors(n, nodes or {}, predecessors or set())
    if rule == "text_required" and not str(p.get("text", "")).strip():
        return [_err("missing_param", "enter the text", node_id=n.id, param="text")]
    if rule == "file_required" and not p.get("file_version_id"):
        return [_err("missing_param", "choose a file", node_id=n.id, param="file_version_id")]
    if rule == "revision_required" and not p.get("revision_id"):
        return [_err("missing_param", "choose a model revision", node_id=n.id, param="revision_id")]
    if rule == "model_3d_inputs":
        has_text = "text" in connected or bool(str(p.get("text", "")).strip())
        has_geom = "file" in connected or "model" in connected
        if not (has_text or has_geom):
            return [_err("missing_input", "connect a file or model, or enter a request", node_id=n.id)]
        if "file" in connected and "model" in connected:
            return [_err("ambiguous_input", "connect either a file or a model, not both", node_id=n.id)]
    if rule == "drawing_reconstruct_inputs":
        sources = int("file" in connected) + int("drawing" in connected) + int(bool(p.get("drawing_id")))
        if sources == 0:
            return [_err("missing_input", "connect a DXF / DWG file or a drawing", node_id=n.id)]
        if sources > 1:
            return [_err("ambiguous_input", "connect either a file or a drawing (or set drawing_id), not several", node_id=n.id)]
    if rule == "engineering_spec_inputs":  # W8: new material (text / files) or an exact existing spec version, not both
        has_new = ("text" in connected or "files" in connected or bool(str(p.get("text", "")).strip())
                   or bool(p.get("file_version_ids")))
        if not has_new and not p.get("spec_version_id"):
            return [_err("missing_input", "enter or connect the engineering description, connect files, or choose a spec version",
                         node_id=n.id)]
        if has_new and p.get("spec_version_id") and not p.get("revise"):
            return [_err("ambiguous_input", "either new material (text / files) or an existing spec version, not both (or turn on "
                         "revise: a new version of that spec with the new material)", node_id=n.id)]
    if rule == "model_or_spec" and ("model" in connected) == ("spec" in connected):
        return [_err("missing_input", "connect exactly one of model or spec", node_id=n.id)]
    return []


def _business_task_errors(n: GraphNode, nodes: dict[str, GraphNode], predecessors: set[str]) -> list[dict[str, str]]:
    """business_task rules (design §3.3): one required criterion, non-empty
    unique output slots, input slots that name a connected upstream output slot
    of the same asset type (or a fixed version), tools from the registry.
    Parameter-shape problems were already reported as invalid_params."""
    from ..services.agent_tools import TOOL_NAMES  # lazy: the tool registry imports services that import the workflow

    try:
        bp = BusinessTaskParams.model_validate(n.params)
    except ValidationError:
        return []
    errors: list[dict[str, str]] = []
    if not any(c.required for c in bp.criteria):
        errors.append(_err("criteria_required", "add at least one required acceptance criterion", node_id=n.id, param="criteria"))
    seen: set[str] = set()
    for s in bp.output_slots:
        if s.slot_key in seen:
            errors.append(_err("duplicate_slot", f"output slot {s.slot_key} is defined twice", node_id=n.id, param="output_slots"))
        seen.add(s.slot_key)
    seen_in: set[str] = set()
    for i, s in enumerate(bp.input_slots):
        where = f"input_slots.{i}"
        if s.slot_key in seen_in:
            errors.append(_err("duplicate_slot", f"input slot {s.slot_key} is defined twice", node_id=n.id, param=where))
        seen_in.add(s.slot_key)
        if s.source_node_id is None:
            if s.fixed_asset_version_id is None:
                errors.append(_err("input_source_required", f"input {s.slot_key} needs an upstream slot or a fixed version", node_id=n.id, param=where))
            continue
        src = nodes.get(s.source_node_id)
        if src is None or src.type != "business_task":
            errors.append(_err("source_missing", f"input {s.slot_key} refers to unknown node {s.source_node_id}", node_id=n.id, param=where))
            continue
        if s.source_node_id not in predecessors:
            errors.append(_err("source_not_connected", f"connect {s.source_node_id} to this node for input {s.slot_key}", node_id=n.id, param=where))
        if s.source_slot_key is None:
            errors.append(_err("source_slot_required", f"input {s.slot_key} needs the upstream slot key", node_id=n.id, param=where))
            continue
        try:
            upstream = BusinessTaskParams.model_validate(src.params)
        except ValidationError:
            continue  # the upstream node reports its own invalid_params
        out = next((o for o in upstream.output_slots if o.slot_key == s.source_slot_key), None)
        if out is None:
            errors.append(_err("source_slot_missing", f"{s.source_node_id} has no output slot {s.source_slot_key}", node_id=n.id, param=where))
        elif out.asset_type != s.asset_type:
            errors.append(_err("slot_type_mismatch", f"input {s.slot_key} is {s.asset_type} but {s.source_node_id}.{s.source_slot_key} is {out.asset_type}",
                               node_id=n.id, param=where))
    for t in bp.recipe.tools:
        if t not in TOOL_NAMES:
            errors.append(_err("tool_unknown", f"unknown tool {t}", node_id=n.id, param="recipe.tools"))
    return errors
