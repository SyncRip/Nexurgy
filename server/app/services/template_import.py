"""Copy an exact authorized ordinary project template, with explicit object mapping."""
from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.providers.base import ProviderError
from ..errors import AppError, conflict, not_found
from ..models import (Drawing, EngineeringSpec, EngineeringSpecVersion, File, FileVersion, ModelRevision,
                      Part, ProjectMember, User, Workflow, WorkflowTemplateVersion)
from ..security import project_access
from . import ai, assets, lifecycle, templates
from .audit import audit


class ReferenceMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["file_version", "model_revision", "drawing", "engineering_spec_version"]
    source_id: uuid.UUID
    target_id: uuid.UUID


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: uuid.UUID
    source_template_version_id: uuid.UUID
    expected_source_hash: str = Field(pattern="^[a-f0-9]{64}$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    reference_map: list[ReferenceMap] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value.strip() if value is not None else None


REFERENCES = {("file_input", "file_version_id"): "file_version", ("model_input", "revision_id"): "model_revision",
              ("drawing_reconstruct", "drawing_id"): "drawing", ("engineering_spec", "spec_version_id"): "engineering_spec_version"}
MODELS = {"file_version": FileVersion, "model_revision": ModelRevision, "drawing": Drawing,
          "engineering_spec_version": EngineeringSpecVersion}
ASSETS = {"file_version": "document", "model_revision": "part_model", "drawing": "drawing"}


def receipt_out(receipt: dict | None) -> dict | None:
    return {k: v for k, v in receipt.items() if k != "request_hash"} if receipt else None


def can_open_source(db: Session, user: User | None, receipt: dict | None) -> bool:
    if user is None or not receipt:
        return False
    try:
        pid = uuid.UUID(str(receipt["project_id"]))
        wid = uuid.UUID(str(receipt["workflow_id"]))
    except (ValueError, KeyError):
        return False
    workflow = db.get(Workflow, wid)
    return (db.get(ProjectMember, {"project_id": pid, "user_id": user.id}) is not None
            and workflow is not None and workflow.project_id == pid)


def _reference(db: Session, kind: str, ident: uuid.UUID, project_id: uuid.UUID):
    row = db.get(MODELS[kind], ident)
    if row is None:
        raise AppError(422, "invalid_reference_map", "the referenced object is unavailable")
    if kind == "file_version":
        owner = db.get(File, row.file_id)
    elif kind == "engineering_spec_version":
        owner = db.get(EngineeringSpec, row.spec_id)
    else:
        owner = db.get(Part, row.part_id)
    if owner is None or owner.project_id != project_id or owner.archived_at is not None:
        raise AppError(422, "invalid_reference_map", "the referenced object is unavailable in this project")
    if kind == "model_revision" and row.status != "ready":
        raise AppError(422, "invalid_reference_map", "the target revision is not ready")
    if kind == "engineering_spec_version" and row.status not in ("ready", "needs_answers"):
        raise AppError(422, "invalid_reference_map", "the target spec version is not ready")
    return row


def _private_fields(value, path="") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}".strip(".")
            if item not in (None, "", [], {}) and (key in ("context", "assistant_operation_id", "file_version_ids", "actor")
                    or key.startswith(("task_", "agent_", "actor_"))
                    or (path and key.endswith(("_id", "_ids")))):
                found.append(here)
            if isinstance(item, (dict, list)):
                found.extend(_private_fields(item, here))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.extend(_private_fields(item, f"{path}.{i}"))
    return found


def copy_graph(db: Session, user: User, source: WorkflowTemplateVersion, target_project_id: uuid.UUID,
               mappings: list[ReferenceMap]) -> dict:
    graph = copy.deepcopy(source.graph)
    index = {}
    for mapping in mappings:
        key = (mapping.kind, str(mapping.source_id))
        if key in index:
            raise AppError(422, "invalid_reference_map", "duplicate source mapping")
        index[key] = mapping.target_id
    used = set()
    missing = []
    _, _, required_stages = templates.publish_errors(graph)
    policy = templates.normalize_policy(source.policy)
    for node in graph.get("nodes") or []:
        if node.get("type") == "business_task":
            raise AppError(409, "feature_disabled", "business task templates cannot be imported", {"feature": "tasks"})
        params = node.get("params") or {}
        private = _private_fields(params)
        if private:
            raise AppError(422, "template_reference_unsupported", "private execution bindings cannot be imported",
                           {"node_id": node.get("id"), "fields": private})
        for field, value in list(params.items()):
            if value in (None, "", [], {}):
                continue
            if field == "model_config_id":
                purpose = ai.AI_NODE_PURPOSE.get(node.get("type"))
                if purpose is None:
                    raise AppError(422, "template_reference_unsupported", "this node does not select a model",
                                   {"node_id": node.get("id"), "field": field})
                try:
                    ai._choose(db, purpose, value)
                except ProviderError as exc:
                    raise AppError(409, exc.code, exc.message) from None
                continue
            kind = REFERENCES.get((node.get("type"), field))
            if kind is None:
                if field.endswith(("_id", "_ids")):
                    raise AppError(422, "template_reference_unsupported", "unknown object reference",
                                   {"node_id": node.get("id"), "field": field})
                continue
            try:
                source_id = uuid.UUID(str(value))
            except ValueError:
                raise AppError(422, "invalid_reference_map", "invalid source reference") from None
            _reference(db, kind, source_id, source.project_id)
            key = (kind, str(source_id))
            target_id = index.get(key)
            if target_id is not None:
                used.add(key)
            elif source.project_id == target_project_id:
                target_id = source_id
            else:
                missing.append({"node_id": node.get("id"), "field": field, "kind": kind, "source_id": str(source_id)})
                continue
            target = _reference(db, kind, target_id, target_project_id)
            if kind == "drawing" and target.kind != "generated":
                raise AppError(422, "invalid_reference_map", "the target must be a generated drawing")
            if kind in ASSETS:
                version = assets.ensure_version(db, ASSETS[kind], target_id)
                if version is None:
                    raise AppError(422, "invalid_reference_map", "target asset is unavailable")
                _, issues = templates.input_quality(db, node_id=node["id"], version=version, asset_type=ASSETS[kind],
                                                    policy=policy, required_stage=required_stages.get(node["id"]))
                if issues:
                    raise AppError(422, "invalid_reference_map", "target input fails template quality rules", {"issues": issues})
            elif policy["mode"] != "exploratory" and not policy["input_rules"].get("allow_unverified"):
                from .verification import require_confirmed_spec
                require_confirmed_spec(db, target)
            params[field] = str(target_id)
    if missing:
        raise AppError(422, "template_reference_mapping_required", "map source references to target project objects", {"references": missing})
    if set(index) != used:
        raise AppError(422, "invalid_reference_map", "the request contains unused mappings")
    return graph


def import_version(db: Session, user: User, project_id: uuid.UUID, body: ImportIn) -> tuple[Workflow, WorkflowTemplateVersion, bool]:
    # Authorization is deliberately repeated on idempotent retries.
    source = templates.load_version(db, user, body.source_template_version_id)
    project_access(db, user, project_id, "template")
    project = lifecycle.lock_project(db, project_id, share=False)
    lifecycle.require_plannable(project)
    workflow = db.scalar(select(Workflow).where(Workflow.id == source.workflow_id).with_for_update(read=True))
    if workflow is None:
        raise not_found("source template")
    db.refresh(source)  # deprecation serializes on the workflow; inspect the status after acquiring its lock
    if workflow.kind != "tool" or workflow.part_id is not None:
        raise AppError(422, "template_kind_unsupported", "only ordinary project templates can be imported")
    if workflow.archived_at is not None:
        raise conflict("template_archived", "the source template is archived")
    if source.status != "published":
        raise conflict("template_version_deprecated", "the selected template version is deprecated")
    if source.hash != body.expected_source_hash:
        raise conflict("template_source_changed", "reload the source version", current_hash=source.hash)
    name = body.name or source.name
    mapping = sorted([m.model_dump(mode="json") for m in body.reference_map], key=lambda x: (x["kind"], x["source_id"]))
    request_hash = templates.canon_hash({"source": str(source.id), "hash": source.hash, "name": name, "reference_map": mapping})
    previous = db.scalar(select(Workflow).where(Workflow.project_id == project_id, Workflow.created_by == user.id,
                                                Workflow.import_request_id == body.client_request_id))
    if previous is not None:
        if (previous.import_source or {}).get("request_hash") != request_hash:
            raise conflict("op_id_reused", "this import request id was already used with different input")
        version = db.scalar(select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.workflow_id == previous.id,
                                                                 WorkflowTemplateVersion.version_no == 1))
        if version is None:
            raise conflict("template_source_changed", "the original imported version is unavailable")
        return previous, version, True
    graph = copy_graph(db, user, source, project_id, body.reference_map)
    receipt = {"v": 1, "project_id": str(source.project_id), "workflow_id": str(source.workflow_id),
               "template_version_id": str(source.id), "version_no": source.version_no, "hash": source.hash, "name": source.name,
               "imported_at": datetime.now(UTC).isoformat(), "imported_by": str(user.id), "reference_map": mapping,
               "request_hash": request_hash}
    target = Workflow(project_id=project_id, name=name, description=source.description, graph=graph,
                      policy=copy.deepcopy(source.policy), created_by=user.id, import_source=receipt,
                      import_request_id=body.client_request_id, kind="tool")
    db.add(target)
    db.flush()
    version = templates.publish(db, user, target.id, expected_version=1)
    audit(db, action="template.import", subject_type="workflow", subject_id=target.id, user_id=user.id,
          project_id=project_id, data={"source_version_id": str(source.id), "source_hash": source.hash,
                                     "template_version_id": str(version.id)})
    return target, version, False
