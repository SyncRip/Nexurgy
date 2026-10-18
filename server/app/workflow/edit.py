"""Change plans for part workflow graphs (AI workspace operation ``workflow_edit``).

A plan is an ordered list of changes (add / remove a node, add / remove an
edge, merge parameters, rename) proposed by the model. :func:`apply_changes`
applies it to a COPY of the base graph and validates the result with
``graph.validate_graph``; it never touches the database. Problems (unknown
node / port, id collisions, new structural errors) go back to the model;
errors the user has to fix anyway (``missing_param``: a file or a revision to
pick, a text to write) and errors the base graph already had are warnings.

The graph rendering used in prompts (:func:`render_graph_lines`) and its
parser (:func:`parse_graph_lines`, for the simulated model) live here so the
two never drift apart.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .graph import Graph, compatible, topo_order, validate_graph
from .nodes import NODE_DEFS

#: Most changes one plan may hold (a larger rework is done in the editor).
MAX_CHANGES = 60
#: Node types a part workflow may contain (collaboration nodes belong to collaboration drafts).
PART_WORKFLOW_NODE_TYPES = tuple(t for t, d in NODE_DEFS.items() if d.category != "collaboration")
#: Validation codes the user resolves (choose a file / revision, write a text); reported, never a model error.
USER_FILLED_CODES = ("missing_param",)
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,60}$")
#: Auto placement: one column to the right of the anchor; rows 200 apart (web/src/features/workflow/model.ts).
COL_GAP = 320
ROW_GAP = 200


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class AddNode(_Change):
    op: Literal["add_node"]
    node_id: str | None = Field(default=None, max_length=60, description="New unique node id (letters, digits, _ . : -); omit to let the server choose")
    type: str = Field(max_length=40, description="A node type from the catalog")
    label: str | None = Field(default=None, max_length=200, description="Display name (≤ 120 characters)")
    params: dict[str, Any] = Field(default_factory=dict, description="Parameters of the node type; omitted ones keep their defaults")
    position: ChangePosition | None = Field(default=None, description="Canvas position; omit to place it automatically")
    after: str | None = Field(default=None, max_length=60, description="Existing node the new one follows (placement; connected from it when no edge into the new node is added)")


class RemoveNode(_Change):
    op: Literal["remove_node"]
    node_id: str = Field(max_length=60, description="Node to remove; its edges are removed with it")


class AddEdge(_Change):
    op: Literal["add_edge"]
    source: str = Field(max_length=60)
    source_port: str = Field(max_length=40, description="An output port of the source node")
    target: str = Field(max_length=60)
    target_port: str = Field(max_length=40, description="An input port of the target node")


class RemoveEdge(_Change):
    op: Literal["remove_edge"]
    edge_id: str | None = Field(default=None, max_length=80, description="Edge id; or give source and target (and ports when several edges join them)")
    source: str | None = Field(default=None, max_length=60)
    source_port: str | None = Field(default=None, max_length=40)
    target: str | None = Field(default=None, max_length=60)
    target_port: str | None = Field(default=None, max_length=40)


class SetParams(_Change):
    op: Literal["set_params"]
    node_id: str = Field(max_length=60)
    params: dict[str, Any] = Field(description="Merged into the node's parameters; null resets a parameter to its default")


class RenameNode(_Change):
    op: Literal["rename_node"]
    node_id: str = Field(max_length=60)
    label: str = Field(max_length=200, description="New display name (≤ 120 characters); empty clears it")


Change = Annotated[Union[AddNode, RemoveNode, AddEdge, RemoveEdge, SetParams, RenameNode], Field(discriminator="op")]


@dataclass
class EditOutcome:
    """``graph`` / ``diff`` are set only when there are no problems."""

    graph: dict | None
    diff: dict | None
    problems: list[str]
    warnings: list[dict] = field(default_factory=list)


# --- params ----------------------------------------------------------------------------------


def internal_params(node_type: str) -> set[str]:
    """Parameters set by services only (``x-internal``, e.g. the assistant's context block): never by a plan."""
    d = NODE_DEFS.get(node_type)
    if d is None:
        return set()
    props = d.params.model_json_schema().get("properties") or {}
    return {k for k, v in props.items() if isinstance(v, dict) and v.get("x-internal")}


def public_params(node_type: str, params: dict | None) -> dict:
    hidden = internal_params(node_type)
    return {k: v for k, v in (params or {}).items() if k not in hidden}


# --- apply -----------------------------------------------------------------------------------


def _conn(e: dict) -> tuple[str, str, str, str]:
    return (e["source"], e["source_port"], e["target"], e["target_port"])


def _new_node_id(node_type: str, taken: set[str]) -> str:
    base = node_type[:50]
    n = 1
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _new_edge_id(source: str, target: str, taken: set[str]) -> str:
    base = f"{source}->{target}"[:74]
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _err_key(e: dict) -> tuple:
    return (e.get("code"), e.get("node_id"), e.get("edge_id"), e.get("port"), e.get("param"))


def _err_text(e: dict) -> str:
    where = [f"{k} {e[k]}" for k in ("node_id", "edge_id", "port", "param", "loc") if e.get(k)]
    return f"{e.get('code')}: {e.get('message')}" + (f" ({', '.join(where)})" if where else "")


def _label(raw: str | None, prefix: str, problems: list[str]) -> str | None:
    label = (raw or "").strip()
    if len(label) > 120:
        problems.append(f"{prefix}: the label is longer than 120 characters")
        return None
    return label or None


def apply_changes(base: dict, changes: list[Any]) -> EditOutcome:
    """Apply ``changes`` (``Change`` models, in order) to a copy of ``base`` and validate the result.

    Removing a node removes its edges (and its group membership). A new node without a position is
    placed one column right of its anchor (``after``, else its first upstream node, else the right-most
    node). ``after`` also connects the anchor to the new node when the plan adds no edge into it (the
    anchor's first output that fits one of the new node's inputs)."""
    problems: list[str] = []
    if len(changes) > MAX_CHANGES:
        return EditOutcome(None, None, [f"a plan holds at most {MAX_CHANGES} changes; propose a smaller change"])
    try:
        g0 = Graph.model_validate(base)
    except ValidationError:
        return EditOutcome(None, None, ["the base workflow graph is not readable"])
    before = g0.model_dump(mode="json")
    work = copy.deepcopy(before)
    nodes: dict[str, dict] = {n["id"]: n for n in work["nodes"]}
    order: list[str] = [n["id"] for n in work["nodes"]]
    edges: list[dict] = work["edges"]
    added: list[tuple[str, str | None, bool]] = []  # (node id, after, has explicit position)

    def need_node(node_id: str | None, prefix: str, role: str = "node") -> dict | None:
        n = nodes.get(node_id or "")
        if n is None:
            problems.append(f"{prefix}: {role} {node_id!r} does not exist in the workflow")
        return n

    for i, ch in enumerate(changes, 1):
        prefix = f"change {i} ({ch.op})"
        if isinstance(ch, AddNode):
            if ch.type not in NODE_DEFS:
                problems.append(f"{prefix}: unknown node type {ch.type!r}; use a type from the node catalog")
                continue
            if ch.type not in PART_WORKFLOW_NODE_TYPES:
                problems.append(f"{prefix}: {ch.type} nodes belong to collaboration workflows, not to a part workflow")
                continue
            if ch.node_id is not None:
                if not NODE_ID_RE.match(ch.node_id):
                    problems.append(f"{prefix}: node id {ch.node_id!r} may only use letters, digits, _ . : - (at most 60)")
                    continue
                if ch.node_id in nodes:
                    problems.append(f"{prefix}: node id {ch.node_id!r} is already used; choose a new id")
                    continue
            hidden = sorted(set(ch.params) & internal_params(ch.type))
            if hidden:
                problems.append(f"{prefix}: parameter(s) {', '.join(hidden)} are set by the server only")
                continue
            if ch.after is not None and need_node(ch.after, prefix, "after node") is None:
                continue
            nid = ch.node_id or _new_node_id(ch.type, set(nodes))
            node = {"id": nid, "type": ch.type, "position": ch.position.model_dump() if ch.position else {"x": 0.0, "y": 0.0},
                    "params": {k: v for k, v in ch.params.items() if v is not None}, "label": _label(ch.label, prefix, problems)}
            nodes[nid] = node
            order.append(nid)
            added.append((nid, ch.after, ch.position is not None))
        elif isinstance(ch, RemoveNode):
            if need_node(ch.node_id, prefix) is None:
                continue
            del nodes[ch.node_id]
            order.remove(ch.node_id)
            edges[:] = [e for e in edges if ch.node_id not in (e["source"], e["target"])]
            for grp in work["groups"]:
                grp["node_ids"] = [x for x in grp["node_ids"] if x != ch.node_id]
            added = [a for a in added if a[0] != ch.node_id]
        elif isinstance(ch, AddEdge):
            src, dst = need_node(ch.source, prefix, "source"), need_node(ch.target, prefix, "target")
            if src is None or dst is None:
                continue
            sd, dd = NODE_DEFS[src["type"]], NODE_DEFS[dst["type"]]
            if sd.output(ch.source_port) is None:
                outs = ", ".join(p.name for p in sd.outputs) or "none"
                problems.append(f"{prefix}: {ch.source} ({src['type']}) has no output {ch.source_port!r}; its outputs: {outs}")
                continue
            if dd.input(ch.target_port) is None:
                ins = ", ".join(p.name for p in dd.inputs) or "none"
                problems.append(f"{prefix}: {ch.target} ({dst['type']}) has no input {ch.target_port!r}; its inputs: {ins}")
                continue
            conn = (ch.source, ch.source_port, ch.target, ch.target_port)
            if any(_conn(e) == conn for e in edges):
                problems.append(f"{prefix}: {ch.source}.{ch.source_port} is already connected to {ch.target}.{ch.target_port}")
                continue
            edges.append({"id": _new_edge_id(ch.source, ch.target, {e["id"] for e in edges}), "source": ch.source,
                          "source_port": ch.source_port, "target": ch.target, "target_port": ch.target_port})
        elif isinstance(ch, RemoveEdge):
            if ch.edge_id is not None:
                found = [e for e in edges if e["id"] == ch.edge_id]
                if not found:
                    problems.append(f"{prefix}: edge {ch.edge_id!r} does not exist in the workflow")
                    continue
            else:
                if not ch.source or not ch.target:
                    problems.append(f"{prefix}: give the edge_id, or the source and the target of the edge")
                    continue
                found = [e for e in edges if e["source"] == ch.source and e["target"] == ch.target
                         and (ch.source_port is None or e["source_port"] == ch.source_port)
                         and (ch.target_port is None or e["target_port"] == ch.target_port)]
                if not found:
                    problems.append(f"{prefix}: no edge from {ch.source} to {ch.target} matches")
                    continue
                if len(found) > 1:
                    problems.append(f"{prefix}: {len(found)} edges join {ch.source} and {ch.target}; give the edge_id or the ports")
                    continue
            edges[:] = [e for e in edges if e is not found[0]]
        elif isinstance(ch, SetParams):
            n = need_node(ch.node_id, prefix)
            if n is None:
                continue
            hidden = sorted(set(ch.params) & internal_params(n["type"]))
            if hidden:
                problems.append(f"{prefix}: parameter(s) {', '.join(hidden)} are set by the server only")
                continue
            params = dict(n.get("params") or {})
            for k, v in ch.params.items():
                if v is None:
                    params.pop(k, None)
                else:
                    params[k] = v
            n["params"] = params
        elif isinstance(ch, RenameNode):
            n = need_node(ch.node_id, prefix)
            if n is not None:
                n["label"] = _label(ch.label, prefix, problems)
        else:  # pragma: no cover - the union is closed
            problems.append(f"{prefix}: unsupported change")

    work["nodes"] = [nodes[i] for i in order]
    _auto_wire(work, added)
    _place(work, added)
    if problems:
        return EditOutcome(None, None, problems)
    g, errors = validate_graph(work)
    if g is None:
        return EditOutcome(None, None, [_err_text(e) for e in errors[:20]])
    base_errors = {_err_key(e) for e in validate_graph(before)[1]}
    warnings: list[dict] = []
    for e in errors:
        if e.get("code") in USER_FILLED_CODES:
            warnings.append({**e, "preexisting": _err_key(e) in base_errors})
        elif _err_key(e) in base_errors:
            warnings.append({**e, "preexisting": True})
        else:
            problems.append(_err_text(e))
    if problems:
        return EditOutcome(None, None, problems)
    after = g.model_dump(mode="json")
    diff = diff_graphs(before, after)
    removed_reviews = [n["id"] for n in diff["removed_nodes"] if n["type"] == "human_review"]
    if removed_reviews:
        warnings.append({"code": "removes_review", "message": "the plan removes approval step(s); later runs will not wait for them",
                         "node_ids": removed_reviews})
    return EditOutcome(after, diff, [], warnings)


def _auto_wire(work: dict, added: list[tuple[str, str | None, bool]]) -> None:
    nodes = {n["id"]: n for n in work["nodes"]}
    edges: list[dict] = work["edges"]
    for nid, after, _ in added:
        if not after or after not in nodes or nid not in nodes or any(e["target"] == nid for e in edges):
            continue
        sd, dd = NODE_DEFS.get(nodes[after]["type"]), NODE_DEFS.get(nodes[nid]["type"])
        if sd is None or dd is None:
            continue
        inputs = sorted(dd.inputs, key=lambda p: not p.required)
        pair = next(((o, i) for o in sd.outputs for i in inputs if compatible(o.type, i.type)), None)
        if pair is not None:
            edges.append({"id": _new_edge_id(after, nid, {e["id"] for e in edges}), "source": after, "source_port": pair[0].name,
                          "target": nid, "target_port": pair[1].name})


def _place(work: dict, added: list[tuple[str, str | None, bool]]) -> None:
    nodes = {n["id"]: n for n in work["nodes"]}
    auto = {nid for nid, _, has_pos in added if not has_pos}
    for nid, after, has_pos in added:
        if has_pos or nid not in nodes:
            continue
        anchor = nodes.get(after or "")
        if anchor is None:
            up = next((e["source"] for e in work["edges"] if e["target"] == nid and e["source"] in nodes), None)
            anchor = nodes.get(up or "")
        if anchor is None:
            others = [n for n in nodes.values() if n["id"] not in auto]
            anchor = max(others, key=lambda n: n["position"]["x"]) if others else None
        x, y = (anchor["position"]["x"] + COL_GAP, anchor["position"]["y"]) if anchor else (0.0, 0.0)
        placed = [n for n in nodes.values() if n["id"] != nid and n["id"] not in auto]
        while any(abs(n["position"]["x"] - x) < 240 and abs(n["position"]["y"] - y) < 120 for n in placed):
            y += ROW_GAP
        nodes[nid]["position"] = {"x": float(x), "y": float(y)}
        auto.discard(nid)


# --- diff ------------------------------------------------------------------------------------

_MISSING = object()


def diff_graphs(before: dict, after: dict) -> dict:
    """What a plan changes, computed from the two graphs (not from the change list): nodes added /
    removed (a kept id whose type changed counts as both), parameters changed (only the keys that
    differ; a key absent from ``after`` went back to its default), labels changed, and edges added /
    removed (compared by their connection, not by id)."""
    bn = {n["id"]: n for n in before.get("nodes") or []}
    an = {n["id"]: n for n in after.get("nodes") or []}

    def same(i: str) -> bool:
        return i in bn and i in an and bn[i]["type"] == an[i]["type"]

    added_nodes = [{"id": n["id"], "type": n["type"], "label": n.get("label"), "params": public_params(n["type"], n.get("params")),
                    "position": n.get("position")} for i, n in an.items() if not same(i)]
    removed_nodes = [{"id": n["id"], "type": n["type"], "label": n.get("label")} for i, n in bn.items() if not same(i)]
    changed_params: list[dict] = []
    renamed: list[dict] = []
    for i, n in an.items():
        if not same(i):
            continue
        bp, ap = bn[i].get("params") or {}, n.get("params") or {}
        keys = sorted(k for k in set(bp) | set(ap) if bp.get(k, _MISSING) != ap.get(k, _MISSING))
        if keys:
            changed_params.append({"node_id": i, "type": n["type"], "keys": keys, "before": {k: bp[k] for k in keys if k in bp},
                                   "after": {k: ap[k] for k in keys if k in ap}})
        if (bn[i].get("label") or None) != (n.get("label") or None):
            renamed.append({"node_id": i, "before": bn[i].get("label"), "after": n.get("label")})
    be = {_conn(e): e for e in before.get("edges") or []}
    ae = {_conn(e): e for e in after.get("edges") or []}
    diff = {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "changed_params": changed_params,
        "added_edges": [e for k, e in ae.items() if k not in be],
        "removed_edges": [e for k, e in be.items() if k not in ae],
        "renamed": renamed,
    }
    diff["empty"] = not any(diff.values())
    return diff


# --- prompt rendering ------------------------------------------------------------------------


def _attr(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace("\r", " ").replace("\n", " ")


def render_graph_lines(graph: dict, *, clip: Any = None) -> list[str]:
    """Nodes (id, type, label, parameters as JSON without internal ones) and edges (source.port -> target.port)
    as prompt lines. ``clip(text, limit) -> str`` caps a long parameter JSON (references); None renders all."""
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    edges = [e for e in graph.get("edges") or [] if isinstance(e, dict)]
    lines = [f"nodes ({len(nodes)}; id: type \"label\" parameters):"]
    for n in nodes:
        params = public_params(str(n.get("type")), n.get("params"))
        text = json.dumps(params, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if params else ""
        if text and clip is not None:
            text = clip(text, 600)
        label = f' "{_attr(n["label"])}"' if n.get("label") else ""
        lines.append(f"- {n.get('id')}: {n.get('type')}{label}" + (f" {text}" if text else ""))
    lines.append(f"edges ({len(edges)}; source.output -> target.input):")
    for e in edges:
        lines.append(f"- {e.get('source')}.{e.get('source_port')} -> {e.get('target')}.{e.get('target_port')} (id {e.get('id')})")
    return lines


_NODE_LINE = re.compile(r'^- (\S+): ([a-z0-9_]+)(?: "([^"]*)")?(?: .*)?$')
_EDGE_LINE = re.compile(r"^- (\S+)\.([a-z0-9_]+) -> (\S+)\.([a-z0-9_]+) \(id (.*)\)$")


def parse_graph_lines(text: str) -> tuple[list[dict], list[dict]]:
    """(nodes [{id, type, label}], edges [{id, source, source_port, target, target_port}]) read back from
    :func:`render_graph_lines` output (the simulated model reads the prompt like a real one)."""
    nodes: list[dict] = []
    edges: list[dict] = []
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("nodes ("):
            section = "nodes"
            continue
        if line.startswith("edges ("):
            section = "edges"
            continue
        if not line.startswith("- "):
            section = ""  # any other heading (validation problems, …) ends the list
            continue
        if section == "nodes":
            m = _NODE_LINE.match(line)
            if m:
                label = m.group(3)
                nodes.append({"id": m.group(1), "type": m.group(2),
                              "label": label.replace("&quot;", '"').replace("&lt;", "<").replace("&amp;", "&") if label else None})
        elif section == "edges":
            m = _EDGE_LINE.match(line)
            if m:
                edges.append({"id": m.group(5), "source": m.group(1), "source_port": m.group(2), "target": m.group(3),
                              "target_port": m.group(4)})
    return nodes, edges


def ordered_ids(nodes: list[dict], edges: list[dict]) -> list[str]:
    """Node ids in dependency order (graph order when there is a cycle)."""
    try:
        g = Graph.model_validate({"nodes": [{"id": n["id"], "type": n["type"]} for n in nodes],
                                  "edges": [{k: e[k] for k in ("id", "source", "source_port", "target", "target_port")} for e in edges]})
    except ValidationError:
        return [n["id"] for n in nodes]
    return topo_order(g) or [n["id"] for n in nodes]


# --- node catalog for the prompt ---------------------------------------------------------------

_RULE_NOTES = {
    "text_required": "needs a non-empty text to run",
    "file_required": "needs file_version_id (the user chooses the file) to run",
    "revision_required": "needs revision_id (the user chooses the model revision) to run",
    "model_3d_inputs": "connect a file or a model, or give a text; never both file and model",
    "drawing_reconstruct_inputs": "connect exactly one of file / drawing (or set drawing_id)",
    "model_or_spec": "connect exactly one of model / spec",
}


def _type_brief(s: dict, defs: dict) -> str:
    if "$ref" in s:
        target = defs.get(s["$ref"].rsplit("/", 1)[-1], {})
        props = target.get("properties") or {}
        return "{" + ", ".join(f"{k}: {_type_brief(v, defs)}" for k, v in props.items()) + "}"
    if "const" in s:
        return json.dumps(s["const"])
    if "enum" in s:
        return "|".join(json.dumps(v) for v in s["enum"])
    if "anyOf" in s:
        return " or ".join(_type_brief(v, defs) for v in s["anyOf"])
    t = s.get("type")
    if t == "array":
        out = f"list of {_type_brief(s.get('items') or {}, defs)}"
        if "minItems" in s or "maxItems" in s:
            out += f" ({s.get('minItems', 0)}..{s.get('maxItems', 'n')} items)"
        return out
    if t == "string":
        return "uuid" if s.get("format") == "uuid" else "string" + (f" ≤{s['maxLength']} chars" if "maxLength" in s else "")
    if t in ("number", "integer"):
        rng = f" {s.get('minimum', '')}..{s.get('maximum', '')}" if "minimum" in s or "maximum" in s else ""
        return f"{t}{rng}"
    return str(t or "value")


def _ports(ports: tuple) -> str:
    return ", ".join(f"{p.name}:{p.type}{'' if p.required else '?'}{'*' if p.multiple else ''}" for p in ports) or "none"


def catalog_lines(model_configs: list[tuple[str, str, str]] | None = None) -> list[str]:
    """The node types a part workflow may use: category, ports, parameters (type, allowed values, default),
    the graph rule and a short description. ``model_configs`` ((id, label, model_id)) list the values of
    ``model_config_id``."""
    lines = ['Ports are name:type ("?" optional, "*" accepts several connections); an edge joins an output to an input of '
             'the same type, or either side "any". Parameters not given keep their defaults.']
    for t in PART_WORKFLOW_NODE_TYPES:
        d = NODE_DEFS[t]
        schema = d.params.model_json_schema()
        defs = schema.get("$defs") or {}
        try:
            defaults = d.params().model_dump(mode="json")
        except ValidationError:
            defaults = {}
        params = []
        for name, s in (schema.get("properties") or {}).items():
            if not isinstance(s, dict) or s.get("x-internal"):
                continue
            brief = f"{name} ({_type_brief(s, defs)}"
            if defaults.get(name) not in ("", None):
                brief += f", default {json.dumps(defaults[name], ensure_ascii=False)}"
            params.append(brief + ")")
        doc = d.doc if len(d.doc) <= 220 else d.doc[:217].rstrip() + "…"
        lines.append(f"- {t} [{d.category}{', AI' if d.uses_ai else ''}]: inputs {_ports(d.inputs)}; outputs {_ports(d.outputs)}; "
                     f"params {', '.join(params) or 'none'}" + (f"; rule: {_RULE_NOTES[d.rule]}" if d.rule in _RULE_NOTES else "")
                     + f". {doc}")
    if model_configs:
        lines.append("model_config_id values (null = the default configuration):")
        lines.extend(f"- {cid}: {label} ({model_id})" for cid, label, model_id in model_configs)
    return lines
