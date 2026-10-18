from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import checks_ledger
from ..ai import limits
from ..db import end_request_session, get_db, new_session
from ..errors import AppError, bad_request, conflict, not_found
from ..models import (
    ApprovalRequest,
    AssetVersion,
    NodeRun,
    Part,
    RunOutput,
    Task,
    User,
    Workflow,
    WorkflowRun,
    WorkflowTemplateVersion,
)
from ..security import ACTION_ROLES, STAGE_ACTION, ProjectAccess, current_user, project_access
from ..services import ai as ai_svc
from ..services import approval_requests, assets, feature_flags as features
from ..services import execution_policy as policy_svc
from ..services import run_progress, run_templates
from ..services import tasks as task_svc
from ..services import templates as tpl
from ..services import template_import
from ..services.audit import audit
from ..services.failure_causes import public_error
from ..services.lifecycle import require_active
from ..workflow import engine
from ..workflow.graph import Graph, validate_graph
from ..workflow.nodes import node_catalog
from ..workflow.templates import TEMPLATES, template_graph, template_list

router = APIRouter(tags=["workflows"])
log = logging.getLogger("partroute.workflows")

# Omission means a new independent request; explicit None preserves a legacy parent.
_TEAM_UNSET = object()

# Project action each node type needs from the user who starts the run.
NODE_ACTION = {
    "model_3d": "design",
    "drawing_reconstruct": "design",
    "drawing": "design",
    "drawing_intake": "design",
    "process_plan": "process",
    "quote": "quote",
    # W8 general mechanical engineering chain (project-level: no part)
    "engineering_spec": "design",
    "concept_design": "design",
    "assembly_process_plan": "process",
    "assembly_quote": "quote",
    "manufacturing_export": "quote",
    "assembly_from_spec": "design",
    "assembly_drawing": "design",
    "delivery_package": "design",
}
PART_NODES = {"model_input", "model_3d", "drawing_reconstruct", "drawing", "drawing_intake", "process_plan", "quote", "export"}
#: Nodes that ask clarification questions (answered through the run decision endpoint).
CLARIFYING_NODES = ("model_3d", "drawing_reconstruct")


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    graph: dict[str, Any] | None = None
    template_key: str | None = None
    policy: tpl.Policy | None = None
    # Part workflow (R1.2): the part's own working copy instead of a project template.
    part_id: uuid.UUID | None = None


class WorkflowPut(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=limits.DESCRIPTION_CHARS)
    graph: dict[str, Any] | None = None
    policy: tpl.Policy | None = None
    expected_version: int | None = None


class RunBudgetIn(BaseModel):
    """Run budget (generation reliability J, design §2.2): real model requests of the whole run (retries included) and
    seconds the run is active; either may be left out (no run-wide cap). On a node retry a cap left out stays and an
    explicit null clears it."""

    max_calls: int | None = Field(default=None, ge=1, le=engine.BUDGET_MAX_CALLS)
    max_seconds: int | None = Field(default=None, ge=1, le=engine.BUDGET_MAX_SECONDS)


class RunIn(BaseModel):
    part_id: uuid.UUID | None = None
    title: str = Field(default="", max_length=200)
    # Per-node parameter overrides for this run only, e.g. {"request": {"text": "..."}}
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Optional task context (project management layer); the run never changes the task.
    task_id: uuid.UUID | None = None
    purpose: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    # Published version to run (default: the latest published one); draft = exploration test run of the draft.
    template_version_id: uuid.UUID | None = None
    draft: bool = False
    # Frozen on the run and inherited by every node. None uses the current project default.
    execution_policy: Literal["standard", "turbo"] | None = None
    # Generation reliability J (design §2.2): the run's budget, frozen on the run (None: no run-wide cap; every step
    # keeps its runtime cap).
    budget: RunBudgetIn | None = None


class RetryIn(BaseModel):
    """Optional body of a node retry (J): a larger run budget to continue with."""

    budget: RunBudgetIn | None = None


class RerunIn(BaseModel):
    """New run from an existing one (rerun_of). Defaults: the original's version,
    input-node parameters, title and purpose. This is fresh work using the current project default;
    retrying a node in the original run instead preserves its frozen policy."""

    template_version_id: uuid.UUID | None = None
    draft: bool = False
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    title: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=limits.DESCRIPTION_CHARS)
    execution_policy: Literal["standard", "turbo"] | None = None


class PublishIn(BaseModel):
    expected_version: int | None = None
    notes: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class DeprecateIn(BaseModel):
    reason: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class DecisionIn(BaseModel):
    """Resolve a waiting step.

    - human_review: approve / reject the subject (records an approval).
    - model_3d asking questions: `answers` (one per question) re-runs the node;
      reject stops it.
    - drawing_intake waiting for manual entry: `intake_id` of an intake created
      with POST /parts/{id}/intakes completes it; reject stops it.
    """

    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)
    expected_hash: str | None = None
    answers: list[str] | None = Field(default=None, max_length=20)
    intake_id: uuid.UUID | None = None


def _wf_out(db: Session, w: Workflow, acc: ProjectAccess, errors: list | None = None) -> dict:
    """Draft view plus its published versions (contract §2.3)."""
    return {
        "id": str(w.id),
        "project_id": str(w.project_id),
        "name": w.name,
        "description": w.description,
        "graph": w.graph,
        "version": w.version,
        "template_key": w.template_key,
        "part_id": str(w.part_id) if w.part_id else None,
        "source_public_template_id": str(w.source_public_template_id) if w.source_public_template_id else None,
        "import_source": template_import.receipt_out(w.import_source),
        "can_open_import_source": template_import.can_open_source(db, acc.user, w.import_source),
        "created_at": w.created_at.isoformat(),
        "updated_at": w.updated_at.isoformat(),
        "archived_at": w.archived_at.isoformat() if w.archived_at else None,
        "validation": errors if errors is not None else validate_graph(w.graph)[1],
        # Problems that block publishing (validation without input values, plus approval gates).
        "publish_errors": tpl.publish_errors(w.graph)[1],
        # Part workflows are managed by workflow members (see _edit_action).
        **tpl.workflow_extras(db, w, acc.can(_edit_action(w, "template"))),
    }


def _load_workflow(db: Session, user: User, workflow_id: uuid.UUID, action: str = "view") -> Workflow:
    return _workflow_access(db, user, workflow_id, action)[0]


def _workflow_access(db: Session, user: User, workflow_id: uuid.UUID, action: str = "view") -> tuple[Workflow, ProjectAccess]:
    w = db.get(Workflow, workflow_id)
    if w is None:
        raise not_found("workflow")
    return w, project_access(db, user, w.project_id, _edit_action(w, action))


def _edit_action(w: Workflow, action: str) -> str:
    """A part workflow is edited by the people who run workflows on the part
    (`workflow`); project templates stay with template managers (`template`)."""
    return "workflow" if action == "template" and w.part_id is not None else action


def _check_part(db: Session, project_id: uuid.UUID, part_id: uuid.UUID) -> Part:
    part = db.get(Part, part_id)
    if part is None or part.project_id != project_id:
        raise not_found("part")
    if part.archived_at is not None:
        raise bad_request("part_archived", "the part is archived")
    return part


@router.get("/workflow-node-types")
def node_types(_: User = Depends(current_user)) -> list[dict]:
    # Keep every decoder in NODE_DEFS for historical graphs; only the creation palette changes.
    return [{**node, "available": node["type"] != "business_task"
             or (features.enabled("tasks") and features.enabled("collaboration"))} for node in node_catalog()]


@router.get("/workflow-templates")
def templates(_: User = Depends(current_user)) -> list[dict]:
    return template_list()


@router.get("/projects/{project_id}/workflows")
def list_workflows(
    project_id: uuid.UUID,
    include_archived: bool = False,
    kind: Literal["tool", "collaboration", "all"] = "tool",
    part_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Templates of a project. `kind` defaults to tool so collaboration drafts
    (kind=collaboration, see /collaboration-draft) stay out of the template list.
    With `part_id`: that part's own workflows instead (part workflows never
    appear in the template list)."""
    acc = project_access(db, user, project_id)
    stmt = select(Workflow).where(Workflow.project_id == project_id).order_by(Workflow.updated_at.desc())
    stmt = stmt.where(Workflow.part_id == part_id) if part_id is not None else stmt.where(Workflow.part_id.is_(None))
    if kind != "all":
        stmt = stmt.where(Workflow.kind == kind)
    if not include_archived:
        stmt = stmt.where(Workflow.archived_at.is_(None))
    return [_wf_out(db, w, acc) for w in db.scalars(stmt).all()]


@router.post("/projects/{project_id}/templates/import", status_code=201)
def import_project_template(project_id: uuid.UUID, body: template_import.ImportIn, response: Response,
                            user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    workflow, version, reused = template_import.import_version(db, user, project_id, body)
    db.commit()
    response.status_code = 200 if reused else 201
    return {"workflow_id": str(workflow.id), "template_version_id": str(version.id), "project_id": str(project_id),
            "version": workflow.version, "version_no": version.version_no, "reused": reused,
            "import_source": template_import.receipt_out(version.import_source)}


def _guard_template_models(db: Session, user: User, project_id: uuid.UUID, graph: dict, previous: dict | None = None) -> None:
    old = {n.get("id"): (n.get("params") or {}).get("model_config_id") for n in (previous or {}).get("nodes", [])}
    for node in graph.get("nodes", []):
        model = (node.get("params") or {}).get("model_config_id")
        if model is None or old.get(node.get("id")) == model:
            continue
        project_access(db, user, project_id, "template")
        purpose = ai_svc.AI_NODE_PURPOSE.get(node.get("type"))
        if purpose is None:
            raise AppError(422, "invalid_model_config", "this node has no model purpose")
        from ..ai.providers.base import ProviderError
        try:
            ai_svc._choose(db, purpose, model)
        except ProviderError as exc:
            raise AppError(409, exc.code, exc.message) from None


@router.post("/projects/{project_id}/workflows", status_code=201)
def create_workflow(project_id: uuid.UUID, body: WorkflowIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    acc = project_access(db, user, project_id, "workflow" if body.part_id else "template")
    if body.part_id:
        _check_part(db, project_id, body.part_id)
    if body.template_key:
        if body.template_key not in TEMPLATES:
            raise not_found("template")
        graph = template_graph(body.template_key)
    else:
        graph = body.graph or {"nodes": [], "edges": [], "groups": []}
    _guard_template_models(db, user, project_id, graph)
    features.guard_graph(graph)
    g, errors = validate_graph(graph)
    if g is None:
        raise AppError(422, "invalid_graph", "graph structure is invalid", {"errors": errors})
    w = Workflow(project_id=project_id, name=body.name, description=body.description, graph=g.model_dump(mode="json"), template_key=body.template_key,
                 policy=tpl.normalize_policy(body.policy), created_by=user.id, part_id=body.part_id)
    db.add(w)
    db.flush()
    audit(db, action="workflow.create", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=project_id)
    db.commit()
    return _wf_out(db, w, acc, errors)


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    w, acc = _workflow_access(db, user, workflow_id)
    return _wf_out(db, w, acc)


@router.put("/workflows/{workflow_id}")
def put_workflow(workflow_id: uuid.UUID, body: WorkflowPut, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Save (drafts may be invalid; validation errors are returned, runs refuse them).

    Every change (graph, name, description or policy) bumps the version. The
    row is locked before comparing `expected_version`, so two concurrent saves
    of the same version cannot both succeed. Published versions never change.
    """
    _, acc = _workflow_access(db, user, workflow_id, "template")
    w = tpl.lock_workflow(db, workflow_id)
    if w.kind == "collaboration":
        features.require_enabled("collaboration")
    features.guard_graph(body.graph if body.graph is not None else w.graph)
    if w.part_id is not None and body.policy is not None:
        raise conflict("part_workflow_policy", "part workflows have no input policy; save the workflow as a template to set one")
    if w.kind == "collaboration":
        # The draft's graph, actors and validation change together through PATCH /workflows/{id}/collaboration-draft.
        raise conflict("use_collaboration_draft", "collaboration drafts are edited through PATCH /workflows/{id}/collaboration-draft",
                       workflow_id=str(w.id))
    tpl.check_version(w, body.expected_version)
    errors = None
    changed = False
    if body.graph is not None:
        _guard_template_models(db, user, w.project_id, body.graph, w.graph)
        g, errors = validate_graph(body.graph)
        if g is None:
            raise AppError(422, "invalid_graph", "graph structure is invalid", {"errors": errors})
        new_graph = g.model_dump(mode="json")
        changed |= new_graph != w.graph
        w.graph = new_graph
    if body.name is not None and body.name != w.name:
        w.name, changed = body.name, True
    if body.description is not None and body.description != w.description:
        w.description, changed = body.description, True
    if body.policy is not None:
        policy = tpl.normalize_policy(body.policy)
        if policy != tpl.normalize_policy(w.policy):
            w.policy, changed = policy, True
    if changed:
        w.version += 1
        audit(db, action="workflow.update", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=w.project_id,
              data={"version": w.version})
    db.commit()
    return _wf_out(db, w, acc, errors)


@router.post("/workflows/{workflow_id}/archive")
def archive_workflow(workflow_id: uuid.UUID, restore: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    w, acc = _workflow_access(db, user, workflow_id, "template")
    if w.kind == "collaboration":
        features.require_enabled("collaboration")
    features.guard_graph(w.graph)
    w.archived_at = None if restore else datetime.now(UTC)
    audit(db, action="workflow.restore" if restore else "workflow.archive", subject_type="workflow", subject_id=w.id, user_id=user.id,
          project_id=w.project_id)
    db.commit()
    return _wf_out(db, w, acc)


@router.post("/workflows/{workflow_id}/validate")
def validate_workflow(workflow_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    w = _load_workflow(db, user, workflow_id)
    return {"errors": validate_graph(w.graph)[1], "publish_errors": tpl.publish_errors(w.graph)[1]}


# --- published versions (contract §2.3) ----------------------------------------------


@router.post("/workflows/{workflow_id}/publish", status_code=201)
def publish_workflow(workflow_id: uuid.UUID, body: PublishIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Publish the draft as an immutable version (422 template_invalid lists every problem)."""
    w = _load_workflow(db, user, workflow_id, "template")
    if w.part_id is not None:
        raise conflict("part_workflow_not_template", "a part workflow is not published; save it as a project template first")
    if w.archived_at is not None:
        raise conflict("workflow_archived", "restore the workflow before publishing it")
    v = tpl.publish(db, user, workflow_id, expected_version=body.expected_version, notes=body.notes)
    db.commit()
    return tpl.version_detail(db, v, user=user)


@router.get("/workflows/{workflow_id}/versions")
def list_versions(workflow_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    w = _load_workflow(db, user, workflow_id)
    rows = tpl.list_versions(db, w.id)
    counts = tpl.run_counts(db, [v.id for v in rows])
    return [tpl.version_out(db, v, counts.get(v.id, 0), user=user) for v in rows]


@router.get("/template-versions/{version_id}")
def get_version(version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return tpl.version_detail(db, tpl.load_version(db, user, version_id), user=user)


@router.post("/template-versions/{version_id}/deprecate")
def deprecate_version(version_id: uuid.UUID, body: DeprecateIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    v = tpl.deprecate(db, user, version_id, body.reason)
    db.commit()
    return tpl.version_out(db, v)


@router.get("/template-versions/{version_id}/runs")
def version_runs(version_id: uuid.UUID, limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Usage history: runs pinned to this version."""
    v = tpl.load_version(db, user, version_id)
    runs = db.scalars(select(WorkflowRun).where(WorkflowRun.template_version_id == v.id).order_by(WorkflowRun.created_at.desc())
                      .limit(min(max(limit, 1), 500))).all()
    return run_summaries(db, list(runs))


def check_run_permissions(db: Session, user: User, project_id: uuid.UUID, graph: Graph) -> None:
    acc = project_access(db, user, project_id, "workflow")
    missing = sorted({NODE_ACTION[n.type] for n in graph.nodes if n.type in NODE_ACTION and acc.role not in ACTION_ROLES[NODE_ACTION[n.type]]})
    if missing:
        raise AppError(403, "forbidden", "your role cannot run some nodes of this workflow", {"actions": missing, "role": acc.role})


def start_run(
    db: Session,
    user: User,
    *,
    project_id: uuid.UUID,
    graph_raw: dict,
    part_id: uuid.UUID | None,
    title: str,
    origin: str,
    workflow: Workflow | None = None,
    task: Task | None = None,
    purpose: str = "",
    template_version: WorkflowTemplateVersion | None = None,
    from_draft: bool = False,
    rerun_of: uuid.UUID | None = None,
    model_snapshot: dict | None = None,
    model_snapshots: dict | None = None,
    preferences_snapshot: dict | None = None,
    team_snapshot: Any = _TEAM_UNSET,
    execution_policy: str | None = None,
    policy_source: str | None = None,
    source_version: dict | None = None,
    project_version: int | None = None,
    budget: dict | None = None,
) -> WorkflowRun:
    """Every run origin (workflow page, task, workspace AI) starts here: the
    project must be active (checked under a lock on the project row), input
    versions are frozen as run_input_bindings (and checked against the pinned
    template version's input policy), and the task context is kept. The model
    configuration of every AI node is frozen too (W1; ``model_snapshot``: the
    starting AI operation's own snapshot, reused for the same choice).

    The execution policy is frozen on the run before anything else (W7) with where it came from (S5, design §3.4):
    ``execution_policy`` is a guarded explicit choice, otherwise the current project default. Source version policy
    is provenance only. With ``policy_source`` the policy and project version were frozen earlier and are kept."""
    from .. import policy as pol
    from ..services.verification import source_brief

    project_access(db, user, project_id, "workflow")
    if task is not None:
        if task.project_id != project_id:
            raise not_found("task")
        features.require_enabled("tasks")
    if workflow is not None and workflow.kind == "collaboration":
        features.require_enabled("collaboration")
    features.guard_graph(graph_raw)
    if policy_source is not None:
        choice = pol.frozen_choice(execution_policy or None, policy_source, source_version, project_version=project_version)
    else:
        if source_version is None and not execution_policy:
            pinned = policy_svc.graph_source(db, project_id, graph_raw)
            source_version = source_brief(db, pinned) if pinned is not None else None
        choice = policy_svc.resolve(db, project_id, execution_policy, source_version=source_version)
    g, errors = validate_graph(graph_raw)
    if errors or g is None:
        raise AppError(422, "invalid_graph", "fix the workflow before running it", {"errors": errors})
    check_run_permissions(db, user, project_id, g)
    require_active(db, project_id, what="starting a run")
    if task is not None:
        if task.project_id != project_id:
            raise not_found("task")
        if task.status == "cancelled":
            raise conflict("task_cancelled", "a cancelled task cannot start new runs")
    if any(n.type in PART_NODES for n in g.nodes):
        if part_id is None:
            raise bad_request("part_required", "choose the part this run works on")
    if part_id is not None:
        part = db.get(Part, part_id)
        if part is None or part.project_id != project_id:
            raise not_found("part")
        if part.archived_at is not None:
            raise bad_request("part_archived", "the part is archived")
    run = engine.create_run(
        db,
        graph=g,
        project_id=project_id,
        part_id=part_id,
        user_id=user.id,
        origin=origin,
        title=title or (template_version.name if template_version else workflow.name if workflow else ""),
        workflow_id=workflow.id if workflow else None,
        workflow_version=template_version.draft_version if template_version else (workflow.version if workflow else None),
        execution_policy=choice.policy,
    )
    run.policy_source = choice.source
    run.inputs = {**(run.inputs or {}), "project_version": choice.project_version}
    try:  # J (design §2.2): the run's budget is frozen with it and shown on the run
        run.inputs = {**(run.inputs or {}), "budget": engine.freeze_budget(budget)}
    except ValueError as exc:
        raise bad_request("invalid_budget", str(exc)) from None
    if choice.source_version:
        run.inputs = {**(run.inputs or {}), "policy_source_version": choice.source_version}
    run.purpose = purpose or ""
    run.template_version_id = template_version.id if template_version else None
    run.rerun_of = rerun_of
    if from_draft:
        run.inputs = {**(run.inputs or {}), "from_draft": True}
    if task is not None:
        run.task_id = task.id
        run.task_baseline_snapshot_id = task.current_baseline_id
    if model_snapshots is not None:
        run.inputs = {**(run.inputs or {}), "model_snapshots": model_snapshots}
    if preferences_snapshot is not None:
        run.inputs = {**(run.inputs or {}), "preferences_snapshot": preferences_snapshot}
    if team_snapshot is _TEAM_UNSET:
        from ..services.engineering_team import freeze_team

        team_snapshot = freeze_team(db, project_id)
    if team_snapshot is not None:
        run.inputs = {**(run.inputs or {}), "team_snapshot": copy.deepcopy(team_snapshot)}
    ai_svc.freeze_run_models(db, run, seed=model_snapshot, bundle=model_snapshots)
    db.flush()
    task_svc.bind_run_inputs(db, run, template_version=template_version)
    audit(db, action="run.start", subject_type="run", subject_id=run.id, user_id=user.id, project_id=project_id,
          data={"origin": origin, "task_id": str(task.id) if task else None,
                "template_version_id": str(template_version.id) if template_version else None, "from_draft": from_draft,
                "rerun_of": str(rerun_of) if rerun_of else None, "execution_policy": run.execution_policy,
                "policy_source": run.policy_source, "policy_source_version": choice.source_version,
                "project_version": choice.project_version})
    return run


@router.post("/workflows/{workflow_id}/runs", status_code=201)
def run_workflow(workflow_id: uuid.UUID, body: RunIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Start a run of a published version (default: the latest), or a draft test run (`draft`, explorations only)."""
    w = _load_workflow(db, user, workflow_id, "workflow")
    _guard_run_choices(body)
    if w.kind == "collaboration":
        raise conflict("use_collaboration_runs", "collaboration workflows start through POST /workflows/{id}/collaboration-runs")
    task = task_svc.check_task_for_run(db, user, body.task_id, w.project_id) if body.task_id else None
    if w.part_id is not None and body.part_id not in (None, w.part_id):
        raise bad_request("part_workflow_other_part", "a part workflow runs on its own part")
    graph, version = tpl.resolve_run(db, user, w, template_version_id=body.template_version_id, draft=body.draft, params=body.params, task=task)
    run = start_run(db, user, project_id=w.project_id, graph_raw=graph, part_id=w.part_id or body.part_id, title=body.title,
                    origin="workflow", workflow=w, task=task, purpose=body.purpose, template_version=version, from_draft=body.draft,
                    execution_policy=body.execution_policy, budget=body.budget.model_dump() if body.budget else None)
    db.commit()
    return run_out(db, user, run)


@router.post("/runs/{run_id}/rerun", status_code=201)
def rerun(run_id: uuid.UUID, body: RerunIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """A new run (rerun_of = this run) for the same task and part. The original
    run never changes; node retries stay inside a run (POST …/retry)."""
    orig = _load_run(db, user, run_id, "workflow")
    _guard_run_choices(body)
    _refuse_collaboration_run(db, orig, "use_collaboration_runs", "a collaboration instance is restarted from its draft (POST /workflows/{id}/collaboration-runs)")
    if orig.workflow_id is None:
        raise conflict("rerun_unsupported", "only runs started from a workflow template can be rerun")
    w = _load_workflow(db, user, orig.workflow_id, "workflow")
    if w.kind == "collaboration":
        raise conflict("use_collaboration_runs", "a collaboration instance is restarted from its draft (POST /workflows/{id}/collaboration-runs)",
                       workflow_id=str(w.id))
    task = task_svc.check_task_for_run(db, user, orig.task_id, orig.project_id) if orig.task_id else None
    version_id = body.template_version_id
    # A part workflow reruns its current graph (resolve_run); there is no version to pin.
    if version_id is None and not body.draft and w.part_id is None:
        if orig.template_version_id is None:
            raise AppError(422, "template_version_required", "this run used no published version; choose one (or a draft test run)")
        version_id = orig.template_version_id
    params = tpl.input_params(orig.graph_snapshot)
    for node_id, override in body.params.items():
        params[node_id] = {**params.get(node_id, {}), **override}
    graph, version = tpl.resolve_run(db, user, w, template_version_id=version_id, draft=body.draft, params=params, task=task)
    run = start_run(db, user, project_id=orig.project_id, graph_raw=graph, part_id=orig.part_id,
                    title=body.title if body.title is not None else orig.title, origin="workflow", workflow=w, task=task,
                    purpose=body.purpose if body.purpose is not None else orig.purpose, template_version=version, from_draft=body.draft,
                    rerun_of=orig.id, execution_policy=body.execution_policy, source_version=policy_svc.run_source(orig),
                    budget=_requested_budget(orig))
    db.commit()
    return run_out(db, user, run)


def _guard_run_choices(body: RunIn | RerunIn) -> None:
    features.require_choices(execution_policy=body.execution_policy)
    for params in body.params.values():
        features.require_choices(model_config_id=params.get("model_config_id"))


def _requested_budget(run: WorkflowRun) -> dict | None:
    """J: a rerun keeps the budget of the original run when a person set it — requested at start or raised to continue
    (else the default)."""
    b = engine.run_budget(run)
    return engine.budget_caps(run) if b.get("source") in ("request", "raised") else None


@router.get("/projects/{project_id}/runs")
def list_runs(
    project_id: uuid.UUID,
    part_id: uuid.UUID | None = None,
    status: str | None = None,
    task_id: uuid.UUID | None = None,
    unlinked: bool = False,
    workflow_id: uuid.UUID | None = None,
    template_version_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    origin: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Execution monitor: runs across tasks, newest first. `status` takes a comma-separated list."""
    project_access(db, user, project_id)
    stmt = (select(WorkflowRun).where(WorkflowRun.project_id == project_id).order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
            .offset(max(offset, 0)).limit(min(max(limit, 1), 200)))
    if part_id:
        stmt = stmt.where(WorkflowRun.part_id == part_id)
    if status:
        stmt = stmt.where(WorkflowRun.status.in_([x.strip() for x in status.split(",") if x.strip()]))
    if task_id:
        stmt = stmt.where(WorkflowRun.task_id == task_id)
    if unlinked:
        stmt = stmt.where(WorkflowRun.task_id.is_(None))
    if workflow_id:
        stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
    if template_version_id:
        stmt = stmt.where(WorkflowRun.template_version_id == template_version_id)
    if created_by:
        stmt = stmt.where(WorkflowRun.created_by == created_by)
    if origin:
        stmt = stmt.where(WorkflowRun.origin == origin)
    if since:
        stmt = stmt.where(WorkflowRun.created_at >= since)
    return run_summaries(db, list(db.scalars(stmt).all()))


def run_summaries(db: Session, runs: list[WorkflowRun]) -> list[dict]:
    """List rows with node counts, task and template version briefs (batched)."""
    ids = [r.id for r in runs]
    counts: dict[uuid.UUID, dict[str, int]] = {}
    if ids:
        for run_id, st, n in db.execute(select(NodeRun.run_id, NodeRun.status, func.count()).where(NodeRun.run_id.in_(ids))
                                        .group_by(NodeRun.run_id, NodeRun.status)).all():
            counts.setdefault(run_id, {})[st] = int(n)
    refs = run_refs(db, runs)
    return [_run_summary(r, counts.get(r.id, {}), refs) for r in runs]


def run_refs(db: Session, runs: list[WorkflowRun]) -> dict[str, dict]:
    """Briefs of the tasks, template versions and rerun sources the runs point at."""
    task_ids = {r.task_id for r in runs if r.task_id}
    version_ids = {r.template_version_id for r in runs if r.template_version_id}
    source_ids = {r.rerun_of for r in runs if r.rerun_of}
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(task_ids))).all()} if task_ids else {}
    versions = {v.id: v for v in db.scalars(select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.id.in_(version_ids))).all()} if version_ids else {}
    sources = {r.id: r for r in db.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(source_ids))).all()} if source_ids else {}
    return {"tasks": tasks, "versions": versions, "sources": sources}


def _task_brief(t: Task | None) -> dict | None:
    return {"id": str(t.id), "key": f"T-{t.number}", "title": t.title, "status": t.status} if t else None


def _run_summary(r: WorkflowRun, counts: dict[str, int], refs: dict[str, dict] | None = None) -> dict:
    refs = refs or {"tasks": {}, "versions": {}, "sources": {}}
    source = refs["sources"].get(r.rerun_of) if r.rerun_of else None
    return {
        "id": str(r.id),
        "project_id": str(r.project_id),
        "part_id": str(r.part_id) if r.part_id else None,
        "workflow_id": str(r.workflow_id) if r.workflow_id else None,
        "workflow_version": r.workflow_version,
        "origin": r.origin,
        "title": r.title,
        "task_id": str(r.task_id) if r.task_id else None,
        "task_baseline_snapshot_id": str(r.task_baseline_snapshot_id) if r.task_baseline_snapshot_id else None,
        "rerun_of": str(r.rerun_of) if r.rerun_of else None,
        "rerun_of_run": {"id": str(source.id), "title": source.title} if source else None,
        "template_version_id": str(r.template_version_id) if r.template_version_id else None,
        "template_version": tpl.version_brief(refs["versions"].get(r.template_version_id)) if r.template_version_id else None,
        "from_draft": bool((r.inputs or {}).get("from_draft")),
        "task": _task_brief(refs["tasks"].get(r.task_id)) if r.task_id else None,
        "purpose": r.purpose,
        "created_by": str(r.created_by),
        "execution_policy": r.execution_policy,
        # S5: where the frozen policy came from (None for runs started before S5) and the version it continued from.
        "policy_source": r.policy_source,
        "policy_source_version": (r.inputs or {}).get("policy_source_version"),
        "project_version": (r.inputs or {}).get("project_version"),
        "status": r.status,
        "cancel_requested": r.cancel_requested,
        # J: the frozen caps (the run detail replaces this with run_budget_out: caps and what was used)
        "budget": engine.budget_caps(r),
        "node_counts": counts,
        "created_at": r.created_at.isoformat(),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "updated_at": r.updated_at.isoformat(),
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def run_out(db: Session, user: User, run: WorkflowRun) -> dict:
    nodes = db.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all()
    acc = project_access(db, user, run.project_id)
    graph = run.graph_snapshot
    params = {n["id"]: n.get("params", {}) for n in graph.get("nodes", [])}
    pending_requests = {
        r.node_run_id: r for r in db.scalars(
            select(ApprovalRequest).where(ApprovalRequest.run_id == run.id, ApprovalRequest.status == "pending")
        ).all()
    }
    counts: dict[str, int] = {}
    out_nodes = []
    for nr in nodes:
        counts[nr.status] = counts.get(nr.status, 0) + 1
        item = {
            "id": str(nr.id),
            "node_id": nr.node_id,
            "node_type": nr.node_type,
            "status": nr.status,
            "attempt": nr.attempt,
            "max_attempts": nr.max_attempts,
            "inputs": nr.inputs,
            "outputs": nr.outputs,
            "error": _public_error(nr.error),
            # generation reliability N: the attempts' check ledgers are listed apart (checks really run / closed)
            "steps": checks_ledger.public_steps(nr.steps),
            "checks_ledger": checks_ledger.from_steps(nr.steps),
            "started_at": _iso(nr.started_at),
            "finished_at": _iso(nr.finished_at),
            "updated_at": _iso(nr.updated_at),
        }
        if nr.status == "waiting_human" and nr.node_type in (*CLARIFYING_NODES, "drawing_intake"):
            item["review"] = {
                "kind": "clarification" if nr.node_type in CLARIFYING_NODES else "manual_entry",
                "can_decide": acc.role in ACTION_ROLES["design"],
                "clarification": (nr.outputs or {}).get("clarification"),
                "manual_entry": (nr.outputs or {}).get("manual_entry"),
            }
        elif nr.status == "waiting_human":
            stage = params.get(nr.node_id, {}).get("stage", "design")
            req = pending_requests.get(nr.id)
            item["review"] = {
                "kind": "approval",
                "stage": stage,
                "instructions": params.get(nr.node_id, {}).get("instructions", ""),
                "can_decide": approval_requests.can_decide(db, user, req, acc.role, acc.project) if req
                else acc.role in ACTION_ROLES[STAGE_ACTION[stage]],
                "subject": (nr.outputs or {}).get("subject"),
                # Same decision as the approval center (POST /approval-requests/{id}/decisions).
                "request_id": str(req.id) if req else None,
                "target_hash": req.target_hash if req else None,
            }
        out_nodes.append(item)
    data = _run_summary(run, counts, run_refs(db, [run]))
    data["graph"] = graph
    data["nodes"] = out_nodes
    data["error"] = run.error
    data["input_bindings"] = task_svc.bindings_out(db, run.id)
    data["outputs"] = _run_outputs(db, run.id)
    data["reruns"] = [{"id": str(r.id), "title": r.title, "status": r.status} for r in db.scalars(
        select(WorkflowRun).where(WorkflowRun.rerun_of == run.id).order_by(WorkflowRun.created_at)).all()]
    data["assumptions"] = run_assumptions(db, run, list(nodes))
    data["calls"] = run_calls(db, run.id)
    data["budget"] = run_budget_out(run, data["calls"])
    return data


def run_calls(db: Session, run_id: uuid.UUID) -> dict:
    """Generation reliability J (design §2.1): ``{units, requests, retries, failed, by_class}`` — logical work units apart
    from the real model requests (retries included) of every node and attempt of the run."""
    from ..ai import retry as call_retry
    from ..models import AiCall

    rows = db.execute(select(AiCall.node_run_id, AiCall.purpose, AiCall.request["unit"].astext, AiCall.request["attempt"].astext,
                             AiCall.status, AiCall.error)
                      .join(NodeRun, NodeRun.id == AiCall.node_run_id).where(NodeRun.run_id == run_id)).all()
    return call_retry.summarize({"scope": r[0], "purpose": r[1], "unit": r[2], "attempt": r[3], "status": r[4], "error": r[5]}
                                for r in rows)


def run_budget_out(run: WorkflowRun, calls: dict) -> dict:
    """The run's frozen budget (J, design §2.2) with what it used so far: model requests and the seconds of the time
    budget's clock (active time only, ``engine.time_used``). The snapshot holds no live clock (the event stream sends a
    snapshot only when something changed): while the clock runs, ``used.seconds`` is what it had before ``running_since``
    and the client adds the time since."""
    b = engine.run_budget(run)
    seconds, since = engine.time_used(run, b)
    return {**{k: v for k, v in b.items() if k != "clock"},
            "used": {"requests": calls.get("requests", 0), "seconds": int(seconds), "running_since": _iso(since)}}


def run_assumptions(db: Session, run: WorkflowRun, nodes: list[NodeRun]) -> list[dict]:
    """Every assumption a Turbo run rests on (W7): the planner's (collaboration contract) and each node's (modeling,
    reconstruct), once per owner and id. Empty for standard runs."""
    from ..models import CollaborationRun

    if run.execution_policy != "turbo":
        return []
    cr = db.get(CollaborationRun, run.id)
    out: list[dict] = [{**a, "owner": {"type": "collaboration_run", "id": str(run.id)}}
                       for a in ((cr.contract or {}).get("assumptions") or [] if cr is not None else []) if isinstance(a, dict)]
    seen = {(str((a.get("owner") or {}).get("id")), a.get("id")) for a in out}
    for nr in sorted(nodes, key=lambda n: n.node_id):
        outputs = nr.outputs or {}
        items = list(outputs.get("assumptions") or []) + list((outputs.get("verification") or {}).get("assumptions") or [])
        for a in items:
            if not isinstance(a, dict):
                continue
            key = (str((a.get("owner") or {}).get("id") or nr.node_id), a.get("id"))
            if key not in seen:
                seen.add(key)
                out.append({**a, "node_id": nr.node_id})
    return out


def _run_outputs(db: Session, run_id: uuid.UUID) -> list[dict]:
    """Asset versions the run's nodes registered (run_outputs)."""
    rows = db.execute(
        select(RunOutput, NodeRun.node_id).join(NodeRun, NodeRun.id == RunOutput.node_run_id)
        .where(RunOutput.run_id == run_id).order_by(RunOutput.created_at, NodeRun.node_id, RunOutput.output_key)
    ).all()
    out = []
    for o, node_id in rows:
        v = db.get(AssetVersion, o.asset_version_id)
        out.append({"node_id": node_id, "output_key": o.output_key, "attempt": o.attempt, "asset_version_id": str(o.asset_version_id),
                    "version": assets.version_out(db, v, edges=False) if v else None})
    return out


def _public_error(err: dict | None) -> dict | None:
    out = public_error(err)
    if out is not None:
        # Historical run errors may omit these fields; preserve their existing nulls.
        out.update(code=err.get("code"), message=err.get("message"))
    return out


def _load_run(db: Session, user: User, run_id: uuid.UUID, action: str = "view") -> WorkflowRun:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    project_access(db, user, run.project_id, action)
    return run


def _refuse_collaboration_run(db: Session, run: WorkflowRun, code: str, message: str) -> None:
    """409 for generic run operations on a collaboration instance (its steps move with its node tasks)."""
    from ..models import CollaborationRun

    if db.get(CollaborationRun, run.id) is not None:
        raise conflict(code, message, run_id=str(run.id))


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return run_out(db, user, _load_run(db, user, run_id))


@router.post("/runs/{run_id}/template", status_code=201)
def template_from_run(run_id: uuid.UUID, body: run_templates.FromRunIn, response: Response,
                      user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    workflow, access, reused = run_templates.from_run(db, user, run_id, body)
    db.commit()
    if reused:
        response.status_code = 200
    return _wf_out(db, workflow, access)


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _load_run(db, user, run_id, "workflow")
    from ..models import CollaborationRun

    if db.get(CollaborationRun, run_id) is not None:
        # Collaboration run: node tasks cancelled, pending submissions withdrawn, executions stopped (backend D).
        from ..services import agent_runtime

        run = agent_runtime.on_run_cancelled(db, user, run_id)
        db.commit()
        return run_out(db, user, run)
    run = engine.lock_run(db, run_id)
    if run.status in engine.TERMINAL_RUN:
        raise conflict("run_finished", "the run already finished")
    engine.cancel_run(db, run)
    approval_requests.close_for_run(db, run.id, status="withdrawn")
    audit(db, action="run.cancel", subject_type="run", subject_id=run.id, user_id=user.id, project_id=run.project_id)
    db.commit()
    return run_out(db, user, run)


def _refuse_assistant_run(db: Session, run: WorkflowRun) -> None:
    """409 use_assistant_retry for a node retry of a run an AI workspace operation started: a retried
    node would run again while the operation already shows its final status (and would no longer count
    as busy). Retry the operation instead (POST /ai/operations/{id}/retry). Cancel and node decisions
    (clarification answers, manual intake) stay allowed: they keep the operation unfinished and it syncs."""
    from ..models import AiOperation

    if db.scalar(select(AiOperation.id).where(AiOperation.run_id == run.id).limit(1)) is not None:
        raise conflict("use_assistant_retry", "this run belongs to an AI workspace request; retry the request in the AI assistant",
                       run_id=str(run.id))


@router.post("/runs/{run_id}/nodes/{node_id}/retry")
def retry(run_id: uuid.UUID, node_id: str, body: RetryIn | None = None, user: User = Depends(current_user),
          db: Session = Depends(get_db)) -> dict:
    """Retry a failed node. J: ``budget`` raises the run's budget first (a run stopped on its budget continues only with
    a larger one; the change is recorded on the run)."""
    orig = _load_run(db, user, run_id, "workflow")
    _refuse_collaboration_run(db, orig, "use_collaboration_actions", "collaboration steps follow their node tasks; use the node actions of the run")
    _refuse_assistant_run(db, orig)
    run = engine.lock_run_for_retry(db, user, run_id)
    try:
        engine.retry_node(db, run, node_id)
    except KeyError:
        raise not_found("node")
    except ValueError as exc:
        raise conflict("cannot_retry", str(exc))
    if body is not None and body.budget is not None:
        try:
            # only the caps the request names change; an explicit null clears that cap
            new = engine.raise_budget(run, body.budget.model_dump(exclude_unset=True), user_id=user.id)
        except ValueError as exc:
            raise bad_request("invalid_budget", str(exc)) from None
        audit(db, action="run.budget", subject_type="run", subject_id=run.id, user_id=user.id, project_id=run.project_id,
              data={"node_id": node_id, "budget": {k: new.get(k) for k in ("max_calls", "max_seconds")}})
    db.commit()
    return run_out(db, user, run)


@router.post("/runs/{run_id}/nodes/{node_id}/decision")
def decide(run_id: uuid.UUID, node_id: str, body: DecisionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    from ..models import Intake

    orig = _load_run(db, user, run_id)
    # Collaboration steps are accepted through their task's acceptance request (C05), not here.
    _refuse_collaboration_run(db, orig, "use_collaboration_actions", "collaboration steps follow their node tasks; decide on the task's acceptance request")
    run = engine.lock_run(db, run_id)
    nr = db.scalar(select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == node_id))
    if nr is None:
        raise not_found("node")
    if nr.status != "waiting_human":
        raise conflict("not_waiting", "this step is not waiting for a decision")
    now = datetime.now(UTC).isoformat()
    rejected_error = {"code": "rejected", "message": body.comment or "rejected by reviewer", "details": {"by": user.name}}

    def note(kind: str, **detail: Any) -> None:
        nr.steps = (nr.steps or []) + [{"kind": kind, "ok": body.decision == "approved", "at": now, "detail": {"by": user.name, **detail}}]

    if nr.node_type in CLARIFYING_NODES:
        project_access(db, user, run.project_id, "design")
        if body.decision == "rejected":
            note("clarification", decision="rejected", comment=body.comment)
            engine.complete_waiting(db, run, node_id, approved=False, error=rejected_error)
        else:
            clar = dict((nr.outputs or {}).get("clarification") or {})
            questions = clar.get("questions") or []
            if not body.answers or len(body.answers) != len(questions):
                raise bad_request("answers_required", f"answer all {len(questions)} questions")
            clar["answers"] = list(clar.get("answers") or []) + [{"question": q, "answer": a} for q, a in zip(questions, body.answers)]
            clar["questions"] = []
            nr.outputs = {**(nr.outputs or {}), "clarification": clar}
            note("clarification", answers=body.answers)
            nr.status = "ready"
            nr.max_attempts = nr.attempt + 1
            engine.refresh_run_status(db, run)
    elif nr.node_type == "drawing_intake":
        project_access(db, user, run.project_id, "design")
        if body.decision == "rejected":
            note("manual_entry", decision="rejected", comment=body.comment)
            engine.complete_waiting(db, run, node_id, approved=False, error=rejected_error)
        else:
            intake = db.get(Intake, body.intake_id) if body.intake_id else None
            if intake is None or intake.part_id != run.part_id:
                raise bad_request("intake_required", "create the intake for this part first and pass its id")
            nr.outputs = {**(nr.outputs or {}), "spec": {"type": "spec", "intake_id": str(intake.id), "estimated": True, "method": intake.method}}
            assets.record_output(db, node_run=nr, output_key="spec", version=assets.ensure_version(db, "intake", intake.id))
            note("manual_entry", intake_id=str(intake.id))
            engine.complete_waiting(db, run, node_id, approved=True)
    else:
        # human_review: the same service as the approval center; closes the matching request.
        approval_requests.decide_node(db, user, run, nr, decision=body.decision, comment=body.comment, expected_hash=body.expected_hash)
    db.commit()
    return run_out(db, user, run)


@router.get("/runs/{run_id}/progress")
def progress(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The run's execution process (services/run_progress.py): stages, their work units, model calls and decisions.
    Same access as the run detail; metadata only."""
    return run_progress.view(db, _load_run(db, user, run_id))


@router.get("/runs/{run_id}/events")
def events(run_id: uuid.UUID, progress: bool = False, user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> StreamingResponse:
    """Server-sent events: a full run snapshot (``run``) whenever it changes; with ``?progress=true`` also its progress
    view (``progress``, as ``GET …/progress``) whenever that changes. The request session is closed before the stream
    starts; every poll reads in a short session of its own on a worker thread."""
    _load_run(db, user, run_id)
    user_id = user.id
    end_request_session(db)

    def snapshot() -> tuple[dict, dict | None] | None:
        with new_session() as s:
            run = s.get(WorkflowRun, run_id)
            u = s.get(User, user_id)
            if run is None or u is None:
                return None
            run_snapshot = run_out(s, u, run)
            progress_snapshot = None
            if progress:
                try:
                    progress_snapshot = run_progress.view(s, run)
                except Exception:
                    # Progress is an optional read projection. Its failure must not stop the authoritative run stream.
                    log.exception("Could not read progress for run %s", run_id)
            return run_snapshot, progress_snapshot

    async def stream():
        last = last_progress = None
        idle = 0
        while True:
            snap = await run_in_threadpool(snapshot)  # sync DB I/O off the event loop
            if snap is None:
                break
            run_snap, progress_snap = snap
            key = json.dumps(run_snap, sort_keys=True, default=str)
            progress_key = json.dumps(progress_snap, sort_keys=True, default=str)
            if progress_snap is not None and progress_key != last_progress:
                last_progress = progress_key
                idle = 0
                yield f"event: progress\ndata: {progress_key}\n\n"
            if key != last:
                last = key
                idle = 0
                yield f"event: run\ndata: {key}\n\n"
                if run_snap["status"] in engine.TERMINAL_RUN:
                    break
            else:
                idle += 1
                if idle % 20 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
