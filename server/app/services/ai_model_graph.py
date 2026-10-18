"""Fixed modeling graph construction shared by assistant CAD operations.

Execution, provider snapshots and geometry checks stay in the existing workflow
and ai_modeling service. These helpers only construct the public run graph.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..errors import not_found
from ..models import ModelRevision, Part
from ..workflow.nodes import Model3DParams


def build_model_graph(part: Part, base_rev: ModelRevision | None, text: str, model_config_id: uuid.UUID | None,
                      context: str = "", *, assistant_operation_id: uuid.UUID | None = None) -> dict:
    """The fixed graph used by assistant CAD operations: [model_input →] model_3d.

    Used by model creation, model modification and create-then-draw operations.
    The graph is readable by every project member: `context` is only the target
    selection / target facts (never the private conversation context), and
    `assistant_operation_id` lets the node load the operation's private context
    from the owner's context build while it runs as that operation's run."""
    if base_rev is not None and base_rev.part_id != part.id:
        raise not_found("revision")
    params: dict[str, Any] = {"text": text}
    if model_config_id:
        params["model_config_id"] = str(model_config_id)
    if context and "context" in Model3DParams.model_fields:
        params["context"] = context
    if assistant_operation_id is not None:
        params["assistant_operation_id"] = str(assistant_operation_id)
    nodes: list[dict] = [{"id": "model", "type": "model_3d", "params": params}]
    edges: list[dict] = []
    if base_rev is not None:
        nodes.insert(0, {"id": "base", "type": "model_input", "params": {"revision_id": str(base_rev.id)}})
        edges.append({"id": "base->model", "source": "base", "source_port": "model", "target": "model", "target_port": "model"})
    return {"nodes": nodes, "edges": edges}


def model_run_title(base_rev: ModelRevision | None) -> str:
    return ("AI modify R%d" % base_rev.rev_no) if base_rev is not None else "AI create"
