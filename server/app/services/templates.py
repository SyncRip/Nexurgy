"""Workflow template drafts, immutable published versions and run resolution
(docs/archive/project-task-asset-design.zh-CN.md §5; contract .trellis/tasks/archive/2026-10/10-05-templates-stages §2).

- The `workflows` row is the draft: graph, name, description and input policy;
  `version` counts draft edits. Drafts may be incomplete.
- Publishing validates the draft (structure, ports, params, approval gates),
  derives the input / output contract and writes an immutable
  `workflow_template_versions` row. Editing the draft never changes it.
- Runs use a published version's graph (or the draft for an exploration test
  run); the version is pinned on the run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AppError, conflict, not_found
from ..models import (
    AssetVersion,
    PublicTemplate,
    PublicTemplateVersion,
    Task,
    User,
    Workflow,
    WorkflowRun,
    WorkflowTemplateVersion,
)
from ..security import project_access
from ..workflow.graph import Graph, validate_graph
from ..workflow.nodes import NODE_DEFS
from .audit import audit

# Input node type -> (output port type, asset type of the bound value)
INPUT_NODES: dict[str, tuple[str, str | None]] = {
    "text_input": ("text", None),
    "file_input": ("file", "document"),
    "model_input": ("model", "part_model"),
}
# Producing node type -> (output port, asset type registered for it)
OUTPUT_NODES: dict[str, tuple[str, str]] = {
    "assembly_from_spec": ("assembly", "assembly_model"),
    "assembly_drawing": ("drawings", "assembly_drawing"),
    "delivery_package": ("delivery", "delivery_manifest"),
    "assembly_process_plan": ("plans", "process_plan"),
    "assembly_quote": ("quotes", "quote"),
    "manufacturing_export": ("bundle", "document"),
    "model_3d": ("model", "part_model"),
    "drawing_reconstruct": ("model", "part_model"),
    "drawing": ("drawing", "drawing"),
    "drawing_intake": ("spec", "intake"),
    "process_plan": ("plan", "process_plan"),
    "quote": ("quote", "quote"),
    "export": ("bundle", "document"),
}
# Labels checked against policy.input_rules (label -> rule that allows it).
LABEL_RULES = (
    ("from_failed_run", "allow_failed_source"),
    ("estimated", "allow_estimated"),
    ("simulated", "allow_simulated"),
    ("mesh_reference", "allow_mesh_reference"),
    ("unverified", "allow_unverified"),
)
# Parameters fixed by the published contract (node type -> {param: default}).
LOCKED_PARAMS: dict[str, dict[str, Any]] = {"human_review": {"stage": "design"}}


class InputRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_estimated: bool = False
    allow_simulated: bool = True
    allow_mesh_reference: bool = False
    allow_failed_source: bool = False
    allow_unverified: bool = False  # W0 §2.2: Turbo / undecided AI output is no standard input unless allowed
    require_approved_stage: Literal["design", "process", "quote"] | None = None


class Policy(BaseModel):
    """Closed input-quality policy of a template (frozen per published version)."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["standard", "exploratory"] = "standard"
    input_rules: InputRules = Field(default_factory=InputRules)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def canon_hash(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def normalize_policy(raw: dict | Policy | None) -> dict:
    """Policy with defaults filled in (stored drafts may hold `{}`)."""
    if isinstance(raw, Policy):
        return raw.model_dump(mode="json")
    try:
        return Policy.model_validate(raw or {}).model_dump(mode="json")
    except ValidationError:
        return Policy().model_dump(mode="json")


def _assets():
    from . import assets as mod

    return mod


# --- validation and approval gates ---------------------------------------------------


def _incoming(g: Graph) -> dict[tuple[str, str], str]:
    """(target node, target port) -> source node (single-connection ports only matter here)."""
    out: dict[tuple[str, str], str] = {}
    for e in g.edges:
        out.setdefault((e.target, e.target_port), e.source)
    return out


def _trace(g: Graph, node_id: str, port: str) -> tuple[Any | None, list[Any]]:
    """Follow an input port upstream through human_review pass-throughs.
    Returns (producing node, human_review nodes passed on the way)."""
    nodes = {n.id: n for n in g.nodes}
    incoming = _incoming(g)
    reviews: list[Any] = []
    cur, cur_port = node_id, port
    for _ in range(len(nodes) + 1):
        src = nodes.get(incoming.get((cur, cur_port), ""))
        if src is None:
            return None, reviews
        if src.type != "human_review":
            return src, reviews
        reviews.append(src)
        cur, cur_port = src.id, "subject"
    return None, reviews


def _stage(node: Any) -> str:
    return str((node.params or {}).get("stage", "design"))


def gate_errors(g: Graph) -> tuple[list[dict], dict[str, str]]:
    """Approval gates (same preconditions as services/status.py).

    G1: a quote-stage review of a quote node Q needs a process review on Q's plan input path.
    G2: a process-stage review of a process_plan node P needs a design review on P's
        model/spec input path; otherwise the path must start at a model_input, which then
        gets the input contract require_approved_stage = "design".
    Returns (errors, {model_input node id: required stage}).
    """
    errors: list[dict] = []
    contracts: dict[str, str] = {}
    incoming = _incoming(g)
    for h in g.nodes:
        if h.type != "human_review":
            continue
        subject, _ = _trace(g, h.id, "subject")
        if subject is None:
            continue
        if _stage(h) == "quote" and subject.type == "quote":
            _, reviews = _trace(g, subject.id, "plan")
            if not any(_stage(r) == "process" for r in reviews):
                errors.append({"code": "gate_missing", "message": "a quote review needs a process review of the plan it prices",
                               "node_id": h.id, "subject_node_id": subject.id, "required_stage": "process"})
        elif _stage(h) == "process" and subject.type == "process_plan":
            for port in [p for p in ("model", "spec") if (subject.id, p) in incoming]:
                source, reviews = _trace(g, subject.id, port)
                if any(_stage(r) == "design" for r in reviews):
                    continue
                if source is not None and source.type == "model_input":
                    contracts[source.id] = "design"
                    continue
                errors.append({"code": "gate_missing", "message": "a process review needs a design review of the model or drawing data the plan uses",
                               "node_id": h.id, "subject_node_id": subject.id, "required_stage": "design"})
    return errors, contracts


def publish_errors(raw: dict) -> tuple[Graph | None, list[dict], dict[str, str]]:
    """Everything that blocks publishing: graph validation (except missing
    parameters of input nodes, filled in per run) plus gate errors."""
    g, errors = validate_graph(raw)
    if g is None:
        return None, errors, {}
    types = {n.id: n.type for n in g.nodes}
    blocking = [e for e in errors if not (e["code"] == "missing_param" and types.get(e.get("node_id", "")) in INPUT_NODES)]
    gates, contracts = gate_errors(g)
    return g, blocking + gates, contracts


def derive_contract(g: Graph, contracts: dict[str, str]) -> tuple[dict, dict]:
    """(input_schema, output_schema) of a graph."""
    inputs = []
    overridable: dict[str, list[str]] = {}
    outputs = []
    for n in g.nodes:
        d = NODE_DEFS[n.type]
        overridable[n.id] = sorted(set(d.params.model_fields) - set(LOCKED_PARAMS.get(n.type, {})))
        if n.type in INPUT_NODES:
            port_type, asset_type = INPUT_NODES[n.type]
            inputs.append({"node_id": n.id, "node_type": n.type, "port_type": port_type, "asset_type": asset_type, "required": True,
                           "default_params": copy.deepcopy(n.params), "require_approved_stage": contracts.get(n.id)})
        if n.type in OUTPUT_NODES:
            port, asset_type = OUTPUT_NODES[n.type]
            # export writes a new file (new document asset); the others append to the part's asset.
            outputs.append({"node_id": n.id, "port": port, "asset_type": asset_type,
                            "target": "new_asset" if n.type in ("export", "manufacturing_export", "assembly_from_spec", "assembly_drawing", "delivery_package") else "append_to_part_asset"})
    return {"inputs": inputs, "overridable": overridable}, {"outputs": outputs}


def content_hash(graph: dict, input_schema: dict, output_schema: dict, policy: dict) -> str:
    return canon_hash({"graph": graph, "input_schema": input_schema, "output_schema": output_schema, "policy": policy})


def draft_hash(w: Workflow) -> str | None:
    """Hash the draft would publish with (None when its structure is invalid)."""
    g, _, contracts = publish_errors(w.graph)
    if g is None:
        return None
    graph = g.model_dump(mode="json")
    ins, outs = derive_contract(g, contracts)
    return content_hash(graph, ins, outs, normalize_policy(w.policy))


# --- versions ------------------------------------------------------------------------


def lock_workflow(db: Session, workflow_id: uuid.UUID) -> Workflow:
    w = db.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update().execution_options(populate_existing=True))
    if w is None:
        raise not_found("workflow")
    return w


def check_version(w: Workflow, expected: int | None) -> None:
    if expected is not None and expected != w.version:
        raise conflict("version_conflict", "the workflow was saved by someone else; reload first", current_version=w.version)


def latest_published(db: Session, workflow_id: uuid.UUID) -> WorkflowTemplateVersion | None:
    return db.scalar(select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.workflow_id == workflow_id,
                                                           WorkflowTemplateVersion.status == "published")
                     .order_by(WorkflowTemplateVersion.version_no.desc()).limit(1))


def publish(
    db: Session,
    user: User,
    workflow_id: uuid.UUID,
    *,
    expected_version: int | None,
    notes: str = "",
    source_public_version_id: uuid.UUID | None = None,
) -> WorkflowTemplateVersion:
    """Publish the draft as the next immutable version. Caller checked the
    `template` action and commits."""
    w = lock_workflow(db, workflow_id)
    from . import feature_flags as features

    if w.kind == "collaboration":
        features.require_enabled("collaboration")
    features.guard_graph(w.graph)
    check_version(w, expected_version)
    g, errors, contracts = publish_errors(w.graph)
    if g is None or errors:
        raise AppError(422, "template_invalid", "fix the template before publishing it", {"errors": errors})
    graph = g.model_dump(mode="json")
    policy = normalize_policy(w.policy)
    input_schema, output_schema = derive_contract(g, contracts)
    h = content_hash(graph, input_schema, output_schema, policy)
    latest = latest_published(db, w.id)
    if latest is not None and latest.hash == h:
        raise conflict("no_changes", f"the draft is identical to published version v{latest.version_no}", version_id=str(latest.id),
                       version_no=latest.version_no)
    no = (db.scalar(select(func.max(WorkflowTemplateVersion.version_no)).where(WorkflowTemplateVersion.workflow_id == w.id)) or 0) + 1
    v = WorkflowTemplateVersion(
        workflow_id=w.id,
        project_id=w.project_id,
        version_no=no,
        name=w.name,
        description=w.description,
        graph=graph,
        input_schema=input_schema,
        output_schema=output_schema,
        policy=policy,
        hash=h,
        notes=(notes or "").strip(),
        draft_version=w.version,
        import_source=copy.deepcopy(w.import_source),
        source_public_version_id=source_public_version_id,
        status="published",
        published_by=user.id,
    )
    db.add(v)
    db.flush()
    audit(db, action="template.publish", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=w.project_id,
          data={"version_id": str(v.id), "version_no": no, "hash": h, "draft_version": w.version,
                "source_public_version_id": str(source_public_version_id) if source_public_version_id else None})
    return v


def load_version(db: Session, user: User, version_id: uuid.UUID, action: str = "view") -> WorkflowTemplateVersion:
    v = db.get(WorkflowTemplateVersion, version_id)
    if v is None:
        raise not_found("template version")
    project_access(db, user, v.project_id, action)
    return v


def deprecate(db: Session, user: User, version_id: uuid.UUID, reason: str) -> WorkflowTemplateVersion:
    """Deprecated versions cannot start new runs; history stays readable. Idempotent."""
    load_version(db, user, version_id, "template")
    reason = (reason or "").strip()
    if not reason:
        raise AppError(422, "reason_required", "give a reason to deprecate the version")
    v = db.scalar(select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.id == version_id).with_for_update()
                  .execution_options(populate_existing=True))
    assert v is not None
    from . import feature_flags as features

    w = db.get(Workflow, v.workflow_id)
    if w is not None and w.kind == "collaboration":
        features.require_enabled("collaboration")
    features.guard_graph(v.graph)
    if v.status == "deprecated":
        return v
    v.status = "deprecated"
    v.deprecated_by = user.id
    v.deprecated_at = _now()
    v.deprecation_reason = reason
    audit(db, action="template.deprecate", subject_type="workflow", subject_id=v.workflow_id, user_id=user.id, project_id=v.project_id,
          data={"version_id": str(v.id), "version_no": v.version_no, "reason": reason})
    db.flush()
    return v


def _user_brief(db: Session, user_id: uuid.UUID | None) -> dict | None:
    u = db.get(User, user_id) if user_id else None
    return {"id": str(u.id), "name": u.name} if u else None


def run_counts(db: Session, version_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not version_ids:
        return {}
    rows = db.execute(select(WorkflowRun.template_version_id, func.count()).where(WorkflowRun.template_version_id.in_(version_ids))
                      .group_by(WorkflowRun.template_version_id)).all()
    return {vid: int(n) for vid, n in rows}


def version_out(db: Session, v: WorkflowTemplateVersion, run_count: int | None = None, *, user: User | None = None) -> dict:
    from .template_import import receipt_out, can_open_source

    pv = db.get(PublicTemplateVersion, v.source_public_version_id) if v.source_public_version_id else None
    pt = db.get(PublicTemplate, pv.public_template_id) if pv else None
    if run_count is None:
        run_count = run_counts(db, [v.id]).get(v.id, 0)
    return {
        "id": str(v.id),
        "workflow_id": str(v.workflow_id),
        "project_id": str(v.project_id),
        "version_no": v.version_no,
        "name": v.name,
        "description": v.description,
        "status": v.status,
        "hash": v.hash,
        "notes": v.notes,
        "published_by": _user_brief(db, v.published_by),
        "published_at": _iso(v.published_at),
        "deprecated_at": _iso(v.deprecated_at),
        "deprecated_by": _user_brief(db, v.deprecated_by),
        "deprecation_reason": v.deprecation_reason,
        "draft_version": v.draft_version,
        "import_source": receipt_out(v.import_source),
        "can_open_import_source": can_open_source(db, user, v.import_source),
        "source_public_version": {"id": str(pv.id), "public_template_id": str(pv.public_template_id), "version_no": pv.version_no,
                                  "name": pt.name if pt else None} if pv else None,
        "run_count": run_count,
    }


def version_detail(db: Session, v: WorkflowTemplateVersion, *, user: User | None = None) -> dict:
    return {**version_out(db, v, user=user), "graph": v.graph, "input_schema": v.input_schema, "output_schema": v.output_schema, "policy": v.policy}


def version_brief(v: WorkflowTemplateVersion | None) -> dict | None:
    if v is None:
        return None
    return {"id": str(v.id), "version_no": v.version_no, "status": v.status, "workflow_id": str(v.workflow_id), "name": v.name}


def list_versions(db: Session, workflow_id: uuid.UUID) -> list[WorkflowTemplateVersion]:
    return list(db.scalars(select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.workflow_id == workflow_id)
                           .order_by(WorkflowTemplateVersion.version_no.desc())).all())


def library_info(db: Session, w: Workflow) -> dict | None:
    """Public-template origin of an imported workflow and whether a newer library version exists."""
    if w.source_public_template_id is None:
        return None
    pt = db.get(PublicTemplate, w.source_public_template_id)
    if pt is None:
        return None
    adopted = db.scalar(
        select(PublicTemplateVersion.version_no)
        .join(WorkflowTemplateVersion, WorkflowTemplateVersion.source_public_version_id == PublicTemplateVersion.id)
        .where(WorkflowTemplateVersion.workflow_id == w.id)
        .order_by(WorkflowTemplateVersion.version_no.desc()).limit(1)
    )
    latest = db.scalar(select(func.max(PublicTemplateVersion.version_no)).where(PublicTemplateVersion.public_template_id == pt.id,
                                                                                PublicTemplateVersion.status == "published"))
    return {"public_template_id": str(pt.id), "name": pt.name, "adopted_version_no": adopted, "latest_version_no": latest,
            "upgrade_available": latest is not None and (adopted is None or latest > adopted)}


def workflow_extras(db: Session, w: Workflow, can_manage: bool) -> dict:
    """Template fields added to the workflow view (contract §2.3)."""
    latest = latest_published(db, w.id)
    count = db.scalar(select(func.count()).select_from(WorkflowTemplateVersion).where(WorkflowTemplateVersion.workflow_id == w.id)) or 0
    return {
        "policy": normalize_policy(w.policy),
        "latest_published": version_out(db, latest) if latest else None,
        "published_count": int(count),
        "draft_dirty": latest is None or draft_hash(w) != latest.hash,
        "can_manage": can_manage,
        "library": library_info(db, w),
    }


# --- run resolution ---------------------------------------------------------------------


def pick_version(db: Session, w: Workflow, template_version_id: uuid.UUID | None) -> WorkflowTemplateVersion:
    """The explicit version (must belong to `w`, not deprecated) or the latest
    published one. The row is share-locked so a concurrent deprecation waits."""
    vid = template_version_id
    if vid is None:
        latest = latest_published(db, w.id)
        if latest is None:
            raise conflict("template_not_published", "publish the template before running it (or test-run the draft as an exploration)")
        vid = latest.id
    v = db.scalar(select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.id == vid)
                  .with_for_update(read=True).execution_options(populate_existing=True))
    if v is None or v.workflow_id != w.id:
        raise not_found("template version")
    if v.status == "deprecated":
        raise conflict("template_version_deprecated", f"v{v.version_no} is deprecated; choose a newer version", version_no=v.version_no)
    return v


def apply_params(graph: dict, params: dict[str, dict[str, Any]]) -> dict:
    """Merge per-run parameter overrides into a copy of the graph. Approval
    gates are part of the published contract: a human_review stage is locked."""
    out = copy.deepcopy(graph)
    for node in out.get("nodes", []):
        override = params.get(node["id"])
        if not override:
            continue
        current = node.get("params") or {}
        for key, default in LOCKED_PARAMS.get(node.get("type", ""), {}).items():
            if key in override and override[key] != current.get(key, default):
                raise AppError(422, "gate_param_locked", f"the {key} of an approval step cannot be changed per run",
                               {"node_id": node["id"], "param": key})
        node["params"] = {**current, **override}
    return out


def resolve_run(
    db: Session,
    user: User,
    w: Workflow,
    *,
    template_version_id: uuid.UUID | None,
    draft: bool,
    params: dict[str, dict[str, Any]],
    task: Task | None,
) -> tuple[dict, WorkflowTemplateVersion | None]:
    """(graph to run, pinned version). Draft test runs are explorations only and
    need the `template` action; otherwise a published version is required.

    A part workflow has no published versions: it always runs its current
    graph (as an exploration, so task runs keep using published templates),
    by the members who may run workflows."""
    if w.part_id is not None:
        if template_version_id is not None:
            raise AppError(422, "part_workflow_has_no_versions", "a part workflow runs its current graph")
        if task is not None:
            raise AppError(422, "draft_run_needs_exploration", "runs of a part workflow are explorations; task runs use a published template")
        project_access(db, user, w.project_id, "workflow")
        return apply_params(w.graph, params), None
    if draft:
        if template_version_id is not None:
            raise AppError(422, "draft_with_version", "a draft test run does not use a published version")
        if task is not None:
            raise AppError(422, "draft_run_needs_exploration", "only exploration runs (no task) can use the draft; publish it first")
        project_access(db, user, w.project_id, "template")
        return apply_params(w.graph, params), None
    v = pick_version(db, w, template_version_id)
    return apply_params(v.graph, params), v


def input_params(graph: dict) -> dict[str, dict[str, Any]]:
    """Frozen parameters of the input nodes of a run graph (rerun defaults)."""
    return {n["id"]: copy.deepcopy(n.get("params") or {}) for n in graph.get("nodes", []) if n.get("type") in INPUT_NODES}


# --- input quality (contract §2.4) --------------------------------------------------------


def input_contract(v: WorkflowTemplateVersion | None) -> tuple[dict | None, dict[str, str | None]]:
    """(policy or None when unchecked, {input node id: required approval stage})."""
    if v is None:
        return None, {}
    policy = normalize_policy(v.policy)
    stages = {i["node_id"]: i.get("require_approved_stage") for i in (v.input_schema or {}).get("inputs", [])}
    return policy, stages


def input_quality(
    db: Session,
    *,
    node_id: str,
    version: AssetVersion,
    asset_type: str,
    policy: dict | None,
    required_stage: str | None,
) -> tuple[dict, list[dict]]:
    """Quality snapshot of a bound input and the problems under `policy`
    (no problems when policy is None or exploratory)."""
    from . import approvals

    mod = _assets()
    labels = mod.labels_of(db, version)
    row = mod.specialised(db, version)
    label = mod.human_label(db, asset_type, row)
    rules = (policy or {}).get("input_rules") or {}
    stage = required_stage or rules.get("require_approved_stage")
    subject = mod.ASSET_SUBJECT.get(asset_type)
    approved: bool | None = None
    if subject is not None and row is not None:
        # The requested stage when it applies to this kind of asset, else the asset's own stage.
        check = stage if stage and subject in approvals.STAGE_SUBJECTS.get(stage, ()) else mod.ASSET_STAGE[asset_type]
        approved = approvals.is_approved(db, subject, row.id, check)
    quality = {"labels": labels, "approved": approved, "availability": version.availability}
    if policy is None or policy.get("mode") == "exploratory":
        return quality, []
    issues: list[dict] = []

    def issue(code: str, message: str) -> None:
        issues.append({"node_id": node_id, "version_label": label, "code": code, "message": message})

    if version.availability != "ready":
        issue("not_ready", f"{label} is {version.availability}, not ready")
    for lab, rule in LABEL_RULES:
        if lab in labels and not rules.get(rule, False):
            issue(lab, f"{label} is marked {lab.replace('_', ' ')}; this template does not accept it")
    if stage and subject is not None and not approved:
        issue("not_approved", f"{label} needs a valid {stage} approval")
    return quality, issues
