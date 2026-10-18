"""Create data-free template drafts from immutable, successfully completed runs."""
from __future__ import annotations

import math
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError, conflict, not_found
from ..models import AuditLog, User, Workflow, WorkflowRun
from ..security import ProjectAccess, project_access
from ..workflow.graph import Graph, GraphEdge, GraphGroup, GraphNode, Position, validate_graph
from ..workflow.nodes import NODE_DEFS
from . import feature_flags as features
from . import lifecycle, templates
from .audit import audit


class FromRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    client_request_id: uuid.UUID

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


# Both node types and retained fields are deliberate allowlists. New catalog fields
# start from server defaults; new node types require their own sanitation review.
SAFE_PARAMS: dict[str, tuple[str, ...]] = {
    "text_input": (),
    "file_input": (),
    "model_input": (),
    "model_3d": (),
    "drawing_reconstruct": (),
    "drawing": ("views", "projection", "hidden_lines", "dimensions"),
    "drawing_intake": (),
    "process_plan": (),
    "quote": (),
    "human_review": ("stage",),
    "export": ("include",),
    "engineering_spec": (),
    "concept_design": (),
    "assembly_process_plan": (),
    "assembly_quote": (),
    "manufacturing_export": (),
    "assembly_from_spec": ("run_checks",),
    "assembly_drawing": ("subassemblies",),
    "delivery_package": ("bom_formats", "step_protocol"),
}
RECEIPT_ACTION = "workflow.template_from_run"


def _invalid() -> AppError:
    return AppError(422, "invalid_graph", "the run snapshot cannot form a valid template graph")


def _unsupported() -> AppError:
    return AppError(422, "template_source_unsupported", "this run cannot be converted to a data-free tool template")


def _id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex}"


def _params(node: GraphNode) -> dict:
    definition = NODE_DEFS[node.type]
    params = definition.params().model_dump(mode="json")
    params.update({key: node.params[key] for key in SAFE_PARAMS[node.type] if key in node.params})
    # Explicit cutting planes carry original geometry; only the closed modes survive.
    if node.type == "drawing" and node.params.get("section") in ("auto", "none"):
        params["section"] = node.params["section"]
    try:
        return definition.params.model_validate(params, strict=True).model_dump(mode="json")
    except ValidationError:
        raise _invalid() from None


def _input_types(node: GraphNode, connected: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Blank inputs can replace inline material, never exact-object operations."""
    if node.type not in ("model_3d", "engineering_spec"):
        return []
    try:
        material = {"text": node.params.get("text", "")}
        if node.type == "engineering_spec":
            material["file_version_ids"] = node.params.get("file_version_ids", [])
        parsed = NODE_DEFS[node.type].params.model_validate(material)
    except ValidationError:
        raise _invalid() from None
    has_text = bool(parsed.text.strip())
    files = parsed.file_version_ids if node.type == "engineering_spec" else []
    if (node.type == "engineering_spec" and node.params.get("spec_version_id")
            and (has_text or files or (node.id, "text") in connected or (node.id, "files") in connected)):
        # A revision target, or a spec whose known files are passed through, cannot
        # become fresh extraction just because its exact version reference is removed.
        raise _unsupported()
    inputs = [("text_input", "text")] if has_text and (node.id, "text") not in connected else []
    inputs.extend(("file_input", "files") for _ in files)
    return inputs


def sanitize_graph(raw: dict) -> Graph:
    """Rebuild only graph structure and approved behavior; never copy business params."""
    try:
        source = Graph.model_validate(raw)
    except ValidationError:
        raise _invalid() from None
    if any(node.type not in SAFE_PARAMS for node in source.nodes):
        raise _unsupported()
    if any(not math.isfinite(value) for node in source.nodes for value in (node.position.x, node.position.y)):
        raise _invalid()
    if len({group.id for group in source.groups}) != len(source.groups):
        raise _invalid()

    # Validate original identities/ports before remapping so duplicates and dangling
    # references cannot disappear during cleanup. Missing fresh values are valid drafts.
    cleaned = [node.model_copy(update={"params": _params(node)}) for node in source.nodes]
    _, errors = validate_graph(Graph(nodes=cleaned, edges=source.edges, groups=source.groups))
    if any(error["code"] not in ("missing_param", "missing_input") for error in errors):
        raise _invalid()
    connected = {(edge.target, edge.target_port) for edge in source.edges}
    node_ids = {node.id: _id("node") for node in source.nodes}
    nodes = [GraphNode(id=node_ids[node.id], type=node.type,
                       position=Position(x=node.position.x, y=node.position.y), params=node.params) for node in cleaned]
    edges = [GraphEdge(id=_id("edge"), source=node_ids[edge.source], source_port=edge.source_port,
                       target=node_ids[edge.target], target_port=edge.target_port) for edge in source.edges]
    added: dict[str, list[str]] = {}
    for node in source.nodes:
        for index, (kind, port) in enumerate(_input_types(node, connected)):
            ident = _id("node")
            nodes.append(GraphNode(id=ident, type=kind, params=NODE_DEFS[kind].params().model_dump(mode="json"),
                                   position=Position(x=node.position.x - 280, y=node.position.y + index * 100)))
            edges.append(GraphEdge(id=_id("edge"), source=ident, source_port="text" if kind == "text_input" else "file",
                                   target=node_ids[node.id], target_port=port))
            added.setdefault(node.id, []).append(ident)
    groups = [GraphGroup(id=_id("group"), label=f"Group {index + 1}",
                         node_ids=[ident for nid in group.node_ids for ident in [node_ids[nid], *added.get(nid, [])]])
              for index, group in enumerate(source.groups)]
    try:
        result = Graph(nodes=nodes, edges=edges, groups=groups)
    except ValidationError:
        raise _invalid() from None
    _, errors = validate_graph(result)
    if any(error["code"] not in ("missing_param", "missing_input") for error in errors):
        raise _invalid()
    return result


def from_run(db: Session, user: User, run_id: uuid.UUID, body: FromRunIn) -> tuple[Workflow, ProjectAccess, bool]:
    """Authorize, serialize, sanitize and save once. The caller owns the transaction."""
    source = db.get(WorkflowRun, run_id)
    if source is None:
        raise not_found("run")
    access = project_access(db, user, source.project_id, "template")
    lifecycle.lock_project(db, source.project_id, share=False)
    lifecycle.require_active(db, source.project_id, what="creating a template from a run")
    source = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
                       .execution_options(populate_existing=True))
    if source is None:
        raise not_found("run")
    if source.status != "succeeded":
        raise conflict("run_not_succeeded", "only a successfully completed run can become a template")

    fingerprint = templates.canon_hash({"run_id": str(run_id), "name": body.name})
    receipt = db.scalar(select(AuditLog).where(
        AuditLog.project_id == source.project_id, AuditLog.user_id == user.id, AuditLog.action == RECEIPT_ACTION,
        AuditLog.data["client_request_id"].astext == str(body.client_request_id)))
    if receipt is not None:
        if receipt.data.get("request_hash") != fingerprint:
            raise conflict("op_id_reused", "this template request id was already used with different input")
        target = db.get(Workflow, uuid.UUID(receipt.subject_id))
        if target is None or target.project_id != source.project_id:
            raise not_found("template")
        return target, access, True

    graph = sanitize_graph(source.graph_snapshot)
    features.guard_graph(graph)
    target = Workflow(project_id=source.project_id, name=body.name, description="", kind="tool",
                      graph=graph.model_dump(mode="json"), policy=templates.normalize_policy(None), created_by=user.id)
    db.add(target)
    db.flush()
    audit(db, action=RECEIPT_ACTION, subject_type="workflow", subject_id=target.id, project_id=source.project_id,
          user_id=user.id, data={"client_request_id": str(body.client_request_id), "request_hash": fingerprint,
                                "run_id": str(run_id)})
    return target, access, False
