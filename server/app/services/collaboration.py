"""Collaboration workflows: planning (C01), drafts and start (C02), projections (C03)
(.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §0, §3.3–3.5, §4).

- A draft is a `Workflow(kind="collaboration")` whose graph holds only
  `business_task` nodes, plus a `CollaborationDraft` row with the demand,
  mode, default actors, fixed inputs, missing facts and validation errors.
  `ready_to_start` = no missing information and no graph / contract errors.
- Start publishes the draft as an immutable template version, creates the
  run, one Task + `CollaborationNodeTask` per node (same transaction) and
  freezes the contract and the agent profile snapshots on `CollaborationRun`.
  The same idempotency key with the same request returns the existing run
  (A04); the same key with other content is 409 `idempotency_mismatch`.
  A start bumps the draft version; another start of the same version (a new
  key) is 409 `draft_already_started`.
- Projections compute progress on the server; node actions come from the
  agent runtime (backend D) via `agent_runtime.allowed_actions`.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..ai import limits
from ..ai.collaboration_planner import (
    MAX_PLAN_EDGES,
    MAX_PLAN_NODES,
    FixedInputSummary,
    KnowledgeSummary,
    MissingInformation,
    PlanOutcome,
    PlanReply,
    PlanRequest,
    SourceContent,
    run_planner,
)
from ..ai.providers.base import ProviderError
from ..errors import AppError, conflict, not_found
from ..models import (
    AgentExecution,
    AgentProfile,
    AiCall,
    ApprovalDecision,
    ApprovalRequest,
    Asset,
    AssetVersion,
    AuditLog,
    CollaborationDraft,
    CollaborationNodeTask,
    CollaborationRun,
    KnowledgeCollection,
    KnowledgeCollectionVersion,
    KnowledgeItem,
    NodeRun,
    Part,
    RunInputBinding,
    Task,
    TaskDeliverableDraft,
    TaskSubmission,
    User,
    Workflow,
    WorkflowResource,
    WorkflowRun,
)
from ..security import ProjectAccess, project_access
from ..workflow.engine import TERMINAL_RUN
from ..workflow.graph import Graph, _err, topo_order, validate_graph
from ..workflow.nodes import SLOT_PATTERN, ActorParam, BusinessTaskParams
from . import actors
from . import agent_profiles as profiles_svc
from . import agent_runtime, ai as ai_svc, collaboration_resources
from . import tasks as task_svc
from . import templates as tpl
from .actors import ActorRef
from .agent_tools import PART_TOOLS, TOOL_NAMES
from .audit import audit
from . import feature_flags as features
from .lifecycle import require_active
from .submissions import submission_out
from .tasks import RuleSpec, canon_hash

log = logging.getLogger("partroute.collaboration")

ExecutionMode = Literal["full_auto", "semi_auto"]
# W7: chosen explicitly per request before planning; standard is the default and keeps every acceptance rule.
ExecutionPolicy = Literal["standard", "turbo"]
PlanningSource = Literal["ai", "manual"]
EMPTY_GRAPH: dict[str, list] = {"nodes": [], "edges": [], "groups": []}
EMPTY_BUDGET: dict[str, Any] = {"steps": 0, "duration_ms": 0, "attempts": 0, "reworks": 0, "cost_fen": None, "cost_unknown": False}
RESOURCE_KINDS = ("input", "candidate", "formal", "failed", "superseded", "evidence")
ACTIVITY_LIMIT = 100


# --- request bodies (shared with api/collaboration.py) ------------------------------------


class FixedInputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_key: str = Field(pattern=SLOT_PATTERN)
    asset_version_id: uuid.UUID


class PlanIn(BaseModel):
    """C01 body. `planning_source=manual` prepares a semi_auto draft from an explicit graph."""

    model_config = ConfigDict(extra="forbid")
    demand: str = Field(min_length=1, max_length=30000)
    execution_mode: ExecutionMode
    execution_policy: ExecutionPolicy = "standard"
    planning_source: PlanningSource = "ai"
    planner_agent_id: uuid.UUID | None = None
    executor: ActorRef
    # Required in standard (reviewer_required); a Turbo plan needs no acceptance subject.
    reviewer: ActorRef | None = None
    pre_reviewer_agent_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    fixed_inputs: list[FixedInputIn] = Field(default_factory=list, max_length=20)
    knowledge_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    # W2: an exact engineering spec version whose digest (definitions, readiness, facts with evidence) the planner reads.
    engineering_spec_version_id: uuid.UUID | None = None
    graph: dict[str, Any] | None = None
    auto_start: bool = False
    idempotency_key: str = Field(min_length=8, max_length=120)
    workflow_id: uuid.UUID | None = None


class NodePatch(BaseModel):
    """Partial BusinessTaskParams for one node (A54: only the named node changes)."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    objective: str | None = Field(default=None, max_length=limits.OBJECTIVE_CHARS)
    executor: ActorParam | None = None
    reviewer: ActorParam | None = None
    pre_reviewer_agent_id: uuid.UUID | None = None
    criteria: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=50)
    output_slots: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=20)
    input_slots: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    recipe: dict[str, Any] | None = None


class DraftPatchIn(BaseModel):
    """C02 PATCH body. Draft-level actors are defaults and apply to every node;
    `nodes` edits only the named nodes; `missing_information` replaces the list
    once the person has supplied the facts (an addition to the contract)."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    demand: str | None = Field(default=None, min_length=1, max_length=30000)
    execution_mode: ExecutionMode | None = None
    # Only accepted unchanged: another policy means a new plan (409 replan_required; POST …/plan with workflow_id).
    execution_policy: ExecutionPolicy | None = None
    executor: ActorRef | None = None
    reviewer: ActorRef | None = None
    pre_reviewer_agent_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    fixed_inputs: list[FixedInputIn] | None = Field(default=None, max_length=20)
    knowledge_item_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    missing_information: list[MissingInformation] | None = Field(default=None, max_length=50)
    nodes: dict[str, NodePatch] | None = None
    graph: dict[str, Any] | None = None


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_mode: ExecutionMode
    # Must equal the draft's frozen policy (execution_policy_mismatch); part of the start fingerprint.
    execution_policy: ExecutionPolicy = "standard"
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    fixed_inputs: list[FixedInputIn] | None = Field(default=None, max_length=20)
    title: str | None = Field(default=None, max_length=200)


# --- helpers --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _assets():
    from . import assets as mod

    return mod


def _version_brief(db: Session, version_id: uuid.UUID | str | None) -> dict[str, Any] | None:
    if version_id is None:
        return None
    try:
        vid = uuid.UUID(str(version_id))
    except ValueError:
        return {"id": str(version_id), "missing": True}
    v = db.get(AssetVersion, vid)
    if v is None:
        return {"id": str(vid), "missing": True}
    return _assets().version_out(db, v, edges=False)


def _project_version(db: Session, project_id: uuid.UUID, version_id: uuid.UUID) -> AssetVersion:
    v = db.get(AssetVersion, version_id)
    asset = db.get(Asset, v.asset_id) if v else None
    if v is None or asset is None or asset.project_id != project_id:
        raise not_found("asset version")
    return v


def _check_part(db: Session, project_id: uuid.UUID, part_id: uuid.UUID | None) -> Part | None:
    if part_id is None:
        return None
    p = db.get(Part, part_id)
    if p is None or p.project_id != project_id:
        raise not_found("part")
    return p


def _resolve_fixed_inputs(db: Session, project_id: uuid.UUID, items: list[FixedInputIn] | list[dict]) -> list[dict[str, Any]]:
    """[{slot_key, asset_version_id, content_hash, label, asset_type}] for versions of this project."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        slot = item.slot_key if isinstance(item, FixedInputIn) else str(item["slot_key"])
        vid = item.asset_version_id if isinstance(item, FixedInputIn) else uuid.UUID(str(item["asset_version_id"]))
        if slot in seen:
            raise AppError(422, "duplicate_slot", f"fixed input slot {slot} is given twice", {"slot_key": slot})
        seen.add(slot)
        v = _project_version(db, project_id, vid)
        brief = _assets().version_out(db, v, edges=False)
        out.append({"slot_key": slot, "asset_version_id": str(v.id), "content_hash": v.content_hash,
                    "label": f"{brief.get('asset_name') or ''} {brief.get('label') or ''}".strip(), "asset_type": brief.get("asset_type")})
    return out


def _resolve_knowledge(db: Session, project_id: uuid.UUID, item_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    """Knowledge item summaries visible in this project (404 for anything else)."""
    out: list[dict[str, Any]] = []
    for iid in dict.fromkeys(item_ids):
        row = db.execute(
            select(KnowledgeItem, KnowledgeCollection.project_id)
            .join(KnowledgeCollectionVersion, KnowledgeCollectionVersion.id == KnowledgeItem.collection_version_id)
            .join(KnowledgeCollection, KnowledgeCollection.id == KnowledgeCollectionVersion.collection_id)
            .where(KnowledgeItem.id == iid)
        ).first()
        if row is None or row[1] != project_id:
            raise not_found("knowledge item")
        item: KnowledgeItem = row[0]
        brief = _version_brief(db, item.asset_version_id) or {}
        out.append({"item_id": str(item.id), "kind": item.kind, "slot_key": item.slot_key, "asset_type": brief.get("asset_type") or "",
                    "label": f"{brief.get('asset_name') or ''} {brief.get('label') or ''}".strip()})
    return out


def _name_from_demand(demand: str) -> str:
    line = next((ln.strip() for ln in demand.splitlines() if ln.strip()), "") or "Collaboration"
    return line[:200]


def _ref(a: ActorParam | dict | ActorRef) -> ActorRef:
    if isinstance(a, ActorRef):
        return a
    data = a.model_dump() if isinstance(a, ActorParam) else dict(a)
    return ActorRef.model_validate(data)


def _agent_ref(agent_id: uuid.UUID) -> ActorRef:
    return ActorRef(kind="agent", agent_profile_id=agent_id)


_SLOT_CLEAN = re.compile(r"[^a-z0-9_:.-]")


def _slot(key: str | None) -> str:
    s = _SLOT_CLEAN.sub("_", (key or "").strip().lower())[:80]
    if not s or not s[0].isalpha():
        s = ("s_" + s)[:80]
    return s


# --- loading --------------------------------------------------------------------------


def load_workflow(db: Session, user: User, workflow_id: uuid.UUID, action: str = "view") -> tuple[Workflow, CollaborationDraft, ProjectAccess]:
    """A collaboration workflow with its draft (404 for tool workflows and non-members)."""
    w = db.get(Workflow, workflow_id)
    if w is None or w.kind != "collaboration":
        raise not_found("collaboration draft")
    acc = project_access(db, user, w.project_id, action)
    d = db.scalar(select(CollaborationDraft).where(CollaborationDraft.workflow_id == w.id))
    if d is None:
        raise not_found("collaboration draft")
    return w, d, acc


def load_run(db: Session, user: User, run_id: uuid.UUID, action: str = "view") -> tuple[WorkflowRun, CollaborationRun, ProjectAccess]:
    run = db.get(WorkflowRun, run_id)
    cr = db.get(CollaborationRun, run_id) if run else None
    if run is None or cr is None:
        raise not_found("collaboration run")
    acc = project_access(db, user, run.project_id, action)
    return run, cr, acc


def _lock_draft(db: Session, draft_id: uuid.UUID) -> CollaborationDraft:
    d = db.scalar(select(CollaborationDraft).where(CollaborationDraft.id == draft_id).with_for_update().execution_options(populate_existing=True))
    if d is None:
        raise not_found("collaboration draft")
    return d


def _latest_run(db: Session, draft: CollaborationDraft) -> tuple[WorkflowRun, CollaborationRun] | None:
    row = db.execute(
        select(WorkflowRun, CollaborationRun).join(CollaborationRun, CollaborationRun.run_id == WorkflowRun.id)
        .where(CollaborationRun.draft_id == draft.id).order_by(CollaborationRun.created_at.desc(), CollaborationRun.run_id.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def _started_version(cr: CollaborationRun) -> int | None:
    """Draft version a run was started at (start bumps the draft and records the new version in the contract)."""
    raw = (cr.contract or {}).get("started_draft_version")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_current_start(cr: CollaborationRun, d: CollaborationDraft) -> bool:
    """True when the run was started from the draft's current version (no edit since).
    Runs from before the version was recorded fall back to timestamps: the start
    and the draft update share one transaction, any later edit is newer."""
    started = _started_version(cr)
    if started is not None:
        return started == d.version
    return cr.created_at is not None and d.updated_at is not None and cr.created_at >= d.updated_at


# --- contract validation (design §3.4) --------------------------------------------------


def validate_contract(db: Session, project_id: uuid.UUID, graph: Graph | dict, mode: str, *, part_id: uuid.UUID | None = None,
                      policy: str = "standard") -> list[dict]:
    """Mode rules and actor / tool checks per business_task node; errors are
    shaped like graph._err with node_id (A03) and param.

    Turbo (W7) has no acceptance: the reviewer / pre-reviewer rules of the mode do not apply (no reviewer needed);
    executors, tools, part and inputs are checked as usual (permissions and structure stay)."""
    g = graph if isinstance(graph, Graph) else Graph.model_validate(graph)
    turbo = policy == "turbo"
    errors: list[dict] = []
    for n in g.nodes:
        if n.type != "business_task":
            continue
        try:
            bp = BusinessTaskParams.model_validate(n.params)
        except ValidationError:
            continue  # validate_graph reported invalid_params

        def err(code: str, message: str, param: str) -> None:
            errors.append(_err(code, message, node_id=n.id, param=param))

        def check(ref: ActorRef, *, purpose: str, param: str, with_mode: bool = True) -> None:
            try:
                actors.validate(db, project_id, ref, purpose=purpose, mode=mode if with_mode else None)
            except AppError as exc:
                err(exc.code, exc.message, param)

        try:
            ex = _ref(bp.executor)
            rv = _ref(bp.reviewer) if bp.reviewer is not None else None
        except ValidationError as exc:
            err("actor_invalid", exc.errors()[0]["msg"], "executor")
            continue
        if turbo or rv is None:  # Turbo: no acceptance subject; a human executor is bound to an agent at start
            if not turbo:
                err("reviewer_required", "a standard run needs a final reviewer for every task", "reviewer")
            check(ex, purpose="execute", param="executor")
        else:
            if mode == "full_auto":
                if ex.kind != "agent":
                    err("executor_must_be_agent", "full_auto tasks are executed by an agent profile", "executor")
                if rv.kind != "agent":
                    err("reviewer_must_be_agent", "full_auto tasks are accepted by an independent agent profile", "reviewer")
                if ex.kind == "agent" and rv.kind == "agent" and ex.id == rv.id:
                    err("self_review", "the executing agent cannot be its own final reviewer", "reviewer")
                for i, c in enumerate(bp.criteria):
                    if c.manual_required:
                        err("manual_in_full_auto", "a full_auto task cannot have criteria that need a manual check", f"criteria.{i}.manual_required")
            else:
                if rv.kind != "human":
                    err("reviewer_must_be_human", "semi_auto tasks are accepted by a person", "reviewer")
                if ex.kind == "human" and rv.kind == "human" and ex.id == rv.id:
                    err("self_review", "the executor cannot be the final reviewer", "reviewer")
            check(ex, purpose="execute", param="executor")
            check(rv, purpose="review", param="reviewer")
            if bp.pre_reviewer_agent_id is not None:
                pre = _agent_ref(bp.pre_reviewer_agent_id)
                if ex.kind == "agent" and ex.id == pre.id:
                    err("self_review", "the executing agent cannot pre-review its own work", "pre_reviewer_agent_id")
                check(pre, purpose="pre_review", param="pre_reviewer_agent_id", with_mode=False)
        tools = list(bp.recipe.tools)
        if ex.kind == "agent":
            profile = db.get(AgentProfile, ex.id)
            if profile is not None:
                bad = [t for t in tools if t in TOOL_NAMES and t not in (profile.allowed_tools or [])]
                if bad:
                    err("tool_not_allowed", f"agent {profile.name} may not use: {', '.join(bad)}", "recipe.tools")
        if PART_TOOLS.intersection(tools) and part_id is None:
            err("part_required", "model, drawing, process and quote tools need a part on the draft", "recipe.tools")
        for i, c in enumerate(bp.criteria):
            try:
                RuleSpec.model_validate(c.rule_spec)
            except ValidationError as exc:
                err("invalid_rule_spec", exc.errors()[0]["msg"], f"criteria.{i}.rule_spec")
        for i, s in enumerate(bp.input_slots):
            if s.source_node_id is not None or s.fixed_asset_version_id is None:
                continue
            v = db.get(AssetVersion, s.fixed_asset_version_id)
            asset = db.get(Asset, v.asset_id) if v else None
            if v is None or asset is None or asset.project_id != project_id:
                err("fixed_input_unknown", f"input {s.slot_key} names a version that is not in this project", f"input_slots.{i}")
            elif asset.type != s.asset_type:
                err("slot_type_mismatch", f"input {s.slot_key} is {s.asset_type} but the fixed version is {asset.type}", f"input_slots.{i}")
    return errors


def revalidate(db: Session, w: Workflow, d: CollaborationDraft) -> tuple[Graph | None, list[dict]]:
    """Recompute validation_errors / ready_to_start from the current graph and draft."""
    g, errors = validate_graph(w.graph)
    if g is not None:
        if len(g.nodes) > MAX_PLAN_NODES:
            errors.append(_err("too_many_nodes", f"a collaboration workflow has at most {MAX_PLAN_NODES} tasks"))
        if len(g.edges) > MAX_PLAN_EDGES:
            errors.append(_err("too_many_edges", f"a collaboration workflow has at most {MAX_PLAN_EDGES} dependencies"))
        errors = errors + validate_contract(db, d.project_id, g, d.execution_mode, part_id=d.part_id, policy=d.execution_policy)
    d.validation_errors = errors
    # Turbo never blocks a start on missing facts (they are adopted as assumptions); standard waits for the answers.
    missing = [] if d.execution_policy == "turbo" else (d.missing_information or [])
    d.ready_to_start = bool(g is not None and g.nodes) and not errors and not missing
    return g, errors


# --- planning (C01) --------------------------------------------------------------------


def plan_to_graph(
    plan: PlanReply,
    *,
    executor: ActorRef,
    reviewer: ActorRef | None,
    pre_reviewer_agent_id: uuid.UUID | None,
    fixed_inputs: list[dict[str, Any]],
    simulated: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """PlanReply → business_task graph (ids n1, n2 …) plus the missing-information
    list with node ids mapped. Unknown tools and dangling references are kept so
    validation locates them (A03)."""
    ids = {n.id: f"n{i + 1}" for i, n in enumerate(plan.nodes)}
    fixed_by_slot = {f["slot_key"]: f for f in fixed_inputs}
    edges: list[dict[str, Any]] = []
    edge_set: set[tuple[str, str]] = set()

    def add_edge(src: str, dst: str) -> None:
        if src == dst or (src, dst) in edge_set:
            return
        edge_set.add((src, dst))
        edges.append({"id": f"e{len(edges) + 1}", "source": src, "source_port": "outputs", "target": dst, "target_port": "inputs"})

    for e in plan.edges:
        s, t = ids.get(e.source), ids.get(e.target)
        if s and t:
            add_edge(s, t)
    nodes: list[dict[str, Any]] = []
    for pn in plan.nodes:
        nid = ids[pn.id]
        criteria = []
        for c in pn.criteria:
            rule: dict[str, Any] = {"asset_types": list(c.allowed_types), "require_approval_stage": c.require_approval_stage}
            if simulated:
                rule["allow_simulated"] = True  # a simulated plan accepts labelled simulated products
            criteria.append({"description": c.description, "required": c.required, "allowed_types": list(c.allowed_types), "rule_spec": rule,
                             "manual_required": False})
        input_slots = []
        for s in pn.input_slots:
            slot: dict[str, Any] = {"slot_key": _slot(s.slot_key), "asset_type": s.asset_type, "required": True}
            if s.source_node_id is not None:
                src = ids.get(s.source_node_id)
                slot["source_node_id"] = src or s.source_node_id
                slot["source_slot_key"] = _slot(s.source_slot_key) if s.source_slot_key else None
                if src:
                    add_edge(src, nid)
            else:
                f = fixed_by_slot.get(slot["slot_key"])
                if f is not None:
                    slot["fixed_asset_version_id"] = f["asset_version_id"]
            input_slots.append(slot)
        params = {
            "title": pn.title[:300],
            "objective": pn.objective[:10000],
            "executor": executor.model_dump(mode="json"),
            "reviewer": reviewer.model_dump(mode="json") if reviewer is not None else None,
            "pre_reviewer_agent_id": str(pre_reviewer_agent_id) if pre_reviewer_agent_id else None,
            "criteria": criteria,
            "output_slots": [{"slot_key": _slot(s.slot_key), "asset_type": s.asset_type, "required": True} for s in pn.output_slots],
            "input_slots": input_slots,
            "recipe": {"instructions": pn.objective[:8000], "tools": list(dict.fromkeys(pn.tools))[:12]},
        }
        nodes.append({"id": nid, "type": "business_task", "params": params, "label": pn.title[:120], "position": {"x": 0, "y": 0}})
    # Layout by dependency depth so the canvas reads left to right.
    preds: dict[str, set[str]] = defaultdict(set)
    for s, t in edge_set:
        preds[t].add(s)
    depth: dict[str, int] = {}

    def depth_of(nid: str, seen: frozenset[str] = frozenset()) -> int:
        if nid in depth:
            return depth[nid]
        if nid in seen:
            return 0
        d = max((depth_of(p, seen | {nid}) + 1 for p in preds.get(nid, ())), default=0)
        depth[nid] = d
        return d

    rows: dict[int, int] = defaultdict(int)
    for n in nodes:
        d = depth_of(n["id"])
        n["position"] = {"x": 80 + d * 320, "y": 80 + rows[d] * 180}
        rows[d] += 1
    missing = [{"field": m.field, "question": m.question, "node_id": ids.get(m.node_id) if m.node_id else None} for m in plan.missing_information]
    return {"nodes": nodes, "edges": edges, "groups": []}, missing


def _record_calls(db: Session, calls: list[Any], planner: AgentProfile | None) -> list[str]:
    before = {id(o) for o in db.new}
    ai_svc.record_calls(db, calls, None)
    rows = [o for o in db.new if isinstance(o, AiCall) and id(o) not in before]
    for r in rows:
        r.agent_profile_id = planner.id if planner else None
    db.flush()
    return [str(r.id) for r in rows]


def plan(db: Session, acc: ProjectAccess, body: PlanIn) -> tuple[Workflow, CollaborationDraft, WorkflowRun | None]:
    """C01. Creates (or re-plans) a draft; AI planning goes through the planning
    provider (simulated results are labelled). 503 `ai_unavailable` is raised
    after the demand is saved on the draft: the caller commits and re-raises."""
    features.require_enabled("collaboration")
    pid = acc.project.id
    if body.planning_source == "manual":
        if body.execution_mode != "semi_auto":
            raise AppError(422, "manual_requires_semi_auto", "an explicit graph can only prepare a semi_auto draft")
        if body.graph is None:
            raise AppError(422, "graph_required", "planning_source=manual needs a graph")
    if body.reviewer is None and body.execution_policy != "turbo":
        raise AppError(422, "reviewer_required", "a standard plan needs a final reviewer (only Turbo runs without acceptance)")
    req_hash = _plan_request_hash(body)
    # Same-key requests serialise here (held until commit) so only one draft is created.
    db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"collaboration.plan:{pid}:{body.idempotency_key}"})
    existing = db.scalar(select(CollaborationDraft).where(
        CollaborationDraft.project_id == pid, CollaborationDraft.planning["idempotency_keys"].has_key(body.idempotency_key)))
    target_workflow_id = body.workflow_id
    if existing is not None:
        if (existing.planning or {}).get("idempotency_keys", {}).get(body.idempotency_key) != req_hash:
            used = ((existing.planning or {}).get("idempotency_policies") or {}).get(body.idempotency_key, "standard")
            if used != body.execution_policy:  # the policy is part of the fingerprint (W0 §2)
                raise conflict("idempotency_conflict", "this idempotency key was already used with another execution policy",
                               idempotency_key=body.idempotency_key, workflow_id=str(existing.workflow_id), execution_policy=used)
            raise conflict("idempotency_mismatch", "this idempotency key was already used for a different planning request",
                           idempotency_key=body.idempotency_key, workflow_id=str(existing.workflow_id))
        if (existing.planning or {}).get("status") not in ("unavailable", "failed"):
            w = db.get(Workflow, existing.workflow_id)
            assert w is not None
            latest = _latest_run(db, existing)
            return w, existing, latest[0] if latest else None
        target_workflow_id = existing.workflow_id  # the same request again after a failed attempt: plan that draft again

    actors.validate(db, pid, body.executor)
    if body.reviewer is not None:
        actors.validate(db, pid, body.reviewer)
    planner_profile: AgentProfile | None = None
    if body.planner_agent_id is not None:
        actors.validate(db, pid, _agent_ref(body.planner_agent_id), purpose="planner")
        planner_profile = db.get(AgentProfile, body.planner_agent_id)
    if body.pre_reviewer_agent_id is not None:
        actors.validate(db, pid, _agent_ref(body.pre_reviewer_agent_id), purpose="pre_review")
    _check_part(db, pid, body.part_id)
    fixed = _resolve_fixed_inputs(db, pid, body.fixed_inputs)
    knowledge = _resolve_knowledge(db, pid, body.knowledge_item_ids)
    # W2 planner input: real content (spec digest, document text) with its read state, read before any planning.
    from . import engineering_spec as spec_svc

    sources = spec_svc.planner_sources(db, pid, spec_version_id=body.engineering_spec_version_id, fixed_inputs=fixed)

    keys: dict[str, str] = {}
    if target_workflow_id is not None:
        w = tpl.lock_workflow(db, target_workflow_id)
        if w.project_id != pid or w.kind != "collaboration":
            raise not_found("collaboration draft")
        if w.archived_at is not None:
            raise conflict("workflow_archived", "restore the draft before planning it again")
        d = db.scalar(select(CollaborationDraft).where(CollaborationDraft.workflow_id == w.id))
        if d is None:
            raise not_found("collaboration draft")
        d = _lock_draft(db, d.id)
        d.version += 1
        keys = dict((d.planning or {}).get("idempotency_keys") or {})
        key_policies = dict((d.planning or {}).get("idempotency_policies") or {})
    else:
        w = Workflow(project_id=pid, name=_name_from_demand(body.demand), kind="collaboration", graph=dict(EMPTY_GRAPH), created_by=acc.user.id)
        db.add(w)
        db.flush()
        d = CollaborationDraft(workflow_id=w.id, project_id=pid, execution_mode=body.execution_mode, created_by=acc.user.id)
        db.add(d)
        key_policies = {}
    d.demand = body.demand
    d.execution_mode = body.execution_mode
    # Frozen before planning (W7): a new policy is a new plan and a new draft version.
    d.execution_policy = body.execution_policy
    d.assumptions = []
    d.planning_source = body.planning_source
    d.planner_agent_id = body.planner_agent_id
    actors.assign(d, "executor", body.executor)
    if body.reviewer is not None:
        actors.assign(d, "reviewer", body.reviewer)
    else:  # Turbo without an acceptance subject
        d.reviewer_kind, d.reviewer_user_id, d.reviewer_agent_id = "human", None, None
    d.pre_reviewer_agent_id = body.pre_reviewer_agent_id
    d.part_id = body.part_id
    d.fixed_inputs = fixed
    d.knowledge_item_ids = list(dict.fromkeys(body.knowledge_item_ids))
    # Every key that planned this draft answers with it (replanning keeps the history).
    keys[body.idempotency_key] = req_hash
    key_policies[body.idempotency_key] = body.execution_policy
    record: dict[str, Any] = {"idempotency_keys": keys, "idempotency_policies": key_policies, "source": body.planning_source,
                              "planner_agent_id": str(body.planner_agent_id) if body.planner_agent_id else None, "at": _now().isoformat(),
                              "execution_policy": body.execution_policy}
    db.flush()

    if body.planning_source == "manual":
        g, errors = validate_graph(body.graph or {})
        if g is None:
            raise AppError(422, "invalid_graph", "graph structure is invalid", {"errors": errors})
        new_graph = g.model_dump(mode="json")
        if new_graph != w.graph:
            w.graph = new_graph
            w.version += 1
        d.missing_information = []
        d.summary = ""
        d.planning = {**record, "status": "manual", "simulated": False}
    else:
        d.planning = _plan_with_ai(db, w, d, body, record, planner_profile, fixed, knowledge, sources)
    revalidate(db, w, d)
    audit(db, action="collaboration.plan", subject_type="workflow", subject_id=w.id, user_id=acc.user.id, project_id=pid,
          data={"source": body.planning_source, "execution_mode": body.execution_mode, "status": d.planning.get("status"),
                "simulated": bool(d.planning.get("simulated")), "nodes": len((w.graph or {}).get("nodes", [])),
                "missing": len(d.missing_information or []), "errors": len(d.validation_errors or []),
                "execution_policy": body.execution_policy, "assumptions": len(d.assumptions or [])})
    db.flush()
    if d.planning.get("status") == "unavailable":
        err = d.planning.get("error") or {}
        raise AppError(503, "ai_unavailable", "no AI model is usable for planning; the demand was saved as a draft",
                       {"reason": err.get("code"), "detail": err.get("message"), "workflow_id": str(w.id), "stage": err.get("stage"),
                        "call_ids": list(d.planning.get("ai_call_ids") or []), "diagnostic": d.planning.get("diagnostic")})
    run: WorkflowRun | None = None
    # Turbo needs no acceptance subject, so it may auto-start in either mode; standard only in full_auto.
    if body.auto_start and (body.execution_mode == "full_auto" or body.execution_policy == "turbo") and d.ready_to_start:
        run, _, _ = start(db, acc, w, d, body=StartIn(execution_mode=body.execution_mode, execution_policy=body.execution_policy,
                                                       expected_version=d.version, idempotency_key=body.idempotency_key))
    return w, d, run


def _plan_request_hash(body: PlanIn) -> str:
    """Fingerprint of a planning request. The policy is part of it; a standard request hashes exactly as before
    W7 (no policy key), so stored keys keep answering their replays."""
    data = body.model_dump(mode="json", exclude={"idempotency_key"})
    if data.get("execution_policy") == "standard":
        data.pop("execution_policy")
    # An unset spec version stays out of the fingerprint so earlier requests keep their hash (W2 field).
    if data.get("engineering_spec_version_id") is None:
        data.pop("engineering_spec_version_id", None)
    return canon_hash(data)


def _plan_with_ai(
    db: Session,
    w: Workflow,
    d: CollaborationDraft,
    body: PlanIn,
    record: dict[str, Any],
    planner_profile: AgentProfile | None,
    fixed: list[dict[str, Any]],
    knowledge: list[dict[str, Any]],
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the planning provider and apply the plan to the workflow graph.
    Returns the `planning` record; status unavailable / failed leaves the graph as it was.

    W1: the planner's configuration (or the default) is frozen into ``model_snapshot`` before the request is
    built; a failure carries a ``diagnostic`` (code, stage, purpose, config, call ids, recovery). W2: errors also
    carry the stage (provider_select / provider_call / parse / validate) and call ids (AC-I05)."""
    from ..ai import diagnostics

    requested = planner_profile.model_config_id if planner_profile else None
    sources = sources or []
    record = {**record, "engineering_spec_version_id": str(body.engineering_spec_version_id) if body.engineering_spec_version_id else None,
              "context_manifest": [{k: s[k] for k in ("key", "kind", "label", "read", "ranges", "chars")} for s in sources]}
    try:
        snap = ai_svc.freeze_model(db, "planning", requested)
        record = {**record, "model_snapshot": snap}
        provider, _cfg = ai_svc.provider_for(db, "planning", snap)
    except ProviderError as exc:
        return {**record, "status": "unavailable", "simulated": False,
                "error": {"code": exc.code, "message": exc.message, "stage": "provider_select", "retryable": bool(exc.retryable)},
                "diagnostic": diagnostics.from_provider_error(exc, stage="resolve", purpose="planning",
                                                              snapshot=record.get("model_snapshot"),
                                                              call_ids=[])}
    executor_tools: list[str] = []
    if body.executor.kind == "agent":
        p = db.get(AgentProfile, body.executor.agent_profile_id)
        executor_tools = list(p.allowed_tools or []) if p else []
    req = PlanRequest(
        demand=body.demand, execution_mode=body.execution_mode,
        fixed_inputs=[FixedInputSummary(slot_key=f["slot_key"], asset_type=str(f.get("asset_type") or "document"), label=f.get("label") or "") for f in fixed],
        knowledge=[KnowledgeSummary(**k) for k in knowledge],
        available_tools=list(TOOL_NAMES), executor_tools=executor_tools, has_part=body.part_id is not None,
        sources=[SourceContent(**{k: s[k] for k in ("key", "kind", "label", "read", "ranges", "text")}) for s in sources],
        execution_policy=body.execution_policy,
    )
    try:
        outcome: PlanOutcome = run_planner(provider, req)
    except ProviderError as exc:
        partial = getattr(exc, "partial", None)
        ids = _record_calls(db, partial.calls, planner_profile) if partial is not None else []
        return {**record, "status": "unavailable", "simulated": bool(provider.simulated), "provider": provider.provider_name,
                "model_id": provider.model_id, "ai_call_ids": ids,
                "error": {"code": exc.code, "message": exc.message, "stage": "provider_call", "retryable": bool(exc.retryable), "call_ids": ids},
                "diagnostic": diagnostics.from_provider_error(exc, purpose="planning", snapshot=snap, call_ids=ids)}
    ids = _record_calls(db, outcome.calls, planner_profile)
    base = {**record, "provider": outcome.provider, "model_id": outcome.model_id, "model_config_id": outcome.model_config_id,
            "simulated": outcome.simulated, "ai_call_ids": ids, "repairs": outcome.repairs}
    if outcome.status != "planned" or outcome.plan is None:
        stage = "parse" if any(e.startswith("the reply was not a JSON object") for e in outcome.errors) else "validate"
        return {**base, "status": "failed", "errors": outcome.errors,
                "error": {"code": "invalid_plan", "message": "; ".join(outcome.errors[:3]), "stage": stage, "retryable": True, "call_ids": ids},
                "diagnostic": diagnostics.diagnostic("invalid_plan", stage="validation", purpose="planning", snapshot=snap, call_ids=ids)}
    graph, missing = plan_to_graph(outcome.plan, executor=body.executor, reviewer=body.reviewer, pre_reviewer_agent_id=body.pre_reviewer_agent_id,
                                   fixed_inputs=fixed, simulated=outcome.simulated)
    if graph != w.graph:
        w.graph = graph
        w.version += 1
    if body.execution_policy == "turbo":
        # Turbo never waits for the facts: the planner's adopted values become assumption records (author ai_turbo),
        # and a fact it still left open is recorded as open (adopted null) for the executing step to decide.
        d.assumptions = turbo_plan_assumptions(outcome.plan, missing, simulated=outcome.simulated)
        d.missing_information = []
    else:
        d.missing_information = missing
    d.summary = outcome.plan.summary
    return {**base, "status": "planned", "summary": outcome.plan.summary, "assumptions": list(outcome.plan.assumptions),
            "node_count": len(graph["nodes"]), "edge_count": len(graph["edges"])}


def turbo_plan_assumptions(plan: PlanReply, missing: list[dict[str, Any]], *, simulated: bool) -> list[dict[str, Any]]:
    """Assumption records (``app.policy.Assumption``) of a Turbo plan: one per adopted value (node ids mapped to the
    draft's n1, n2 …), then one per fact the model still listed as missing (adopted null, decided downstream)."""
    from ..policy import Assumption

    ids = {n.id: f"n{i + 1}" for i, n in enumerate(plan.nodes)}
    out: list[dict[str, Any]] = []
    note = " (simulated plan)" if simulated else ""
    for a in getattr(plan, "adopted_values", None) or []:
        node = ids.get(a.node_id) if a.node_id else None
        out.append(Assumption(id=f"A-{len(out) + 1:02d}", subject=f"demand.{a.field}", field=a.field, adopted=a.adopted, unit=a.unit,
                              basis=(a.basis or "adopted by the planner (Turbo)") + note, affected=[node] if node else [],
                              author="ai_turbo").model_dump(mode="json"))
    for m in missing:
        out.append(Assumption(id=f"A-{len(out) + 1:02d}", subject=f"demand.{m['field']}", field=m["field"], adopted=None,
                              basis="not stated in the demand; Turbo leaves the value to the executing step" + note,
                              affected=[m["node_id"]] if m.get("node_id") else [], author="ai_turbo").model_dump(mode="json"))
    return out


# --- draft (C02) ----------------------------------------------------------------------


def draft_out(db: Session, user: User, w: Workflow, d: CollaborationDraft) -> dict[str, Any]:
    latest = _latest_run(db, d)
    run_brief = None
    if latest is not None:
        run, cr = latest
        # current_version: the run froze the draft as it is now (a start of this version is refused: draft_already_started).
        run_brief = {"id": str(run.id), "title": run.title, "status": run.status, "execution_mode": cr.execution_mode,
                     "execution_policy": cr.execution_policy,
                     "created_at": _iso(run.created_at), "draft_version": _started_version(cr), "current_version": _is_current_start(cr, d)}
    return {
        "workflow": {"id": str(w.id), "name": w.name, "kind": w.kind, "version": w.version},
        "draft": {
            "id": str(d.id),
            "workflow_id": str(d.workflow_id),
            "project_id": str(d.project_id),
            "demand": d.demand,
            "execution_mode": d.execution_mode,
            "execution_policy": d.execution_policy,
            "assumptions": list(d.assumptions or []),
            "planning_source": d.planning_source,
            "planner_agent_id": str(d.planner_agent_id) if d.planner_agent_id else None,
            "planner": actors.out(db, "agent", None, d.planner_agent_id) if d.planner_agent_id else None,
            "executor": actors.out(db, d.executor_kind, d.executor_user_id, d.executor_agent_id),
            "reviewer": actors.out(db, d.reviewer_kind, d.reviewer_user_id, d.reviewer_agent_id),
            "pre_reviewer": actors.out(db, "agent", None, d.pre_reviewer_agent_id) if d.pre_reviewer_agent_id else None,
            "pre_reviewer_agent_id": str(d.pre_reviewer_agent_id) if d.pre_reviewer_agent_id else None,
            "part_id": str(d.part_id) if d.part_id else None,
            "fixed_inputs": list(d.fixed_inputs or []),
            "knowledge_item_ids": [str(x) for x in d.knowledge_item_ids or []],
            "missing_information": list(d.missing_information or []),
            "validation_errors": list(d.validation_errors or []),
            "planning": d.planning or {},
            "summary": d.summary,
            "ready_to_start": d.ready_to_start,
            "version": d.version,
            "created_by": str(d.created_by),
            "created_at": _iso(d.created_at),
            "updated_at": _iso(d.updated_at),
        },
        "graph": w.graph,
        "validation_errors": list(d.validation_errors or []),
        "missing_information": list(d.missing_information or []),
        "ready_to_start": d.ready_to_start,
        "planning": d.planning or {},
        "run": run_brief,
    }


def sync_fixed_slots(graph: dict[str, Any], fixed: list[dict[str, Any]], old_fixed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Copy of `graph` whose fixed input slots (no source node) point at the
    draft's fixed versions with the same slot_key; a slot whose draft input was
    removed loses its version (validation then reports it)."""
    by_slot = {f["slot_key"]: str(f["asset_version_id"]) for f in fixed}
    dropped = {f["slot_key"] for f in old_fixed or []} - set(by_slot)
    out = {**graph, "nodes": []}
    for n in graph.get("nodes", []):
        n = dict(n)
        if n.get("type") == "business_task":
            params = dict(n.get("params") or {})
            slots = []
            for s in params.get("input_slots") or []:
                s = dict(s)
                if not s.get("source_node_id"):
                    if s.get("slot_key") in by_slot:
                        s["fixed_asset_version_id"] = by_slot[s["slot_key"]]
                    elif s.get("slot_key") in dropped:
                        s["fixed_asset_version_id"] = None
                slots.append(s)
            params["input_slots"] = slots
            n["params"] = params
        out["nodes"].append(n)
    return out


def _apply_graph(db: Session, acc: ProjectAccess, w: Workflow, graph: dict[str, Any]) -> bool:
    """Store a structurally valid graph on the draft workflow; version + 1 when it changed."""
    features.require_enabled("collaboration")
    g, errors = validate_graph(graph)
    if g is None:
        raise AppError(422, "invalid_graph", "graph structure is invalid", {"errors": errors})
    new_graph = g.model_dump(mode="json")
    if new_graph == w.graph:
        return False
    w.graph = new_graph
    w.version += 1
    audit(db, action="workflow.update", subject_type="workflow", subject_id=w.id, user_id=acc.user.id, project_id=w.project_id,
          data={"version": w.version, "collaboration": True})
    return True


def patch_draft(db: Session, acc: ProjectAccess, w: Workflow, d: CollaborationDraft, body: DraftPatchIn) -> tuple[Workflow, CollaborationDraft]:
    """C02 PATCH under row locks; draft-level actors apply to every node, `nodes`
    only to the named ones (A54); validation is recomputed; version + 1."""
    features.require_enabled("collaboration")
    pid = w.project_id
    w = tpl.lock_workflow(db, w.id)
    d = _lock_draft(db, d.id)
    if body.expected_version != d.version:
        raise conflict("version_conflict", "the draft was changed by someone else; reload first", current_version=d.version)
    data = body.model_dump(exclude_unset=True)
    graph = dict(w.graph or EMPTY_GRAPH)
    nodes = [dict(n) for n in graph.get("nodes", [])]
    graph_changed = False
    changed: list[str] = []

    def set_all(param: str, value: Any) -> None:
        nonlocal graph_changed
        for n in nodes:
            if n.get("type") == "business_task":
                n["params"] = {**(n.get("params") or {}), param: value}
                graph_changed = True

    if body.graph is not None:
        g, errors = validate_graph(body.graph)
        if g is None:
            raise AppError(422, "invalid_graph", "graph structure is invalid", {"errors": errors})
        graph = g.model_dump(mode="json")
        nodes = graph["nodes"]
        graph_changed = True
        changed.append("graph")
    if body.execution_policy is not None and body.execution_policy != d.execution_policy:
        # The policy is frozen before planning: another policy is another plan (a new draft version).
        raise conflict("replan_required", "changing the execution policy plans the draft again; use the planning request with this "
                       "draft's workflow_id", execution_policy=d.execution_policy)
    if "demand" in data and body.demand is not None:
        d.demand = body.demand
        changed.append("demand")
    if "execution_mode" in data and body.execution_mode is not None:
        d.execution_mode = body.execution_mode
        changed.append("execution_mode")
    if "executor" in data and body.executor is not None:
        actors.validate(db, pid, body.executor)
        actors.assign(d, "executor", body.executor)
        set_all("executor", body.executor.model_dump(mode="json"))
        changed.append("executor")
    if "reviewer" in data and body.reviewer is not None:
        actors.validate(db, pid, body.reviewer)
        actors.assign(d, "reviewer", body.reviewer)
        set_all("reviewer", body.reviewer.model_dump(mode="json"))
        changed.append("reviewer")
    if "pre_reviewer_agent_id" in data:
        if body.pre_reviewer_agent_id is not None:
            actors.validate(db, pid, _agent_ref(body.pre_reviewer_agent_id), purpose="pre_review")
        d.pre_reviewer_agent_id = body.pre_reviewer_agent_id
        set_all("pre_reviewer_agent_id", str(body.pre_reviewer_agent_id) if body.pre_reviewer_agent_id else None)
        changed.append("pre_reviewer_agent_id")
    if "part_id" in data:
        _check_part(db, pid, body.part_id)
        d.part_id = body.part_id
        changed.append("part_id")
    old_fixed: list[dict[str, Any]] | None = None
    if "fixed_inputs" in data and body.fixed_inputs is not None:
        old_fixed = list(d.fixed_inputs or [])
        d.fixed_inputs = _resolve_fixed_inputs(db, pid, body.fixed_inputs)
        changed.append("fixed_inputs")
    if "knowledge_item_ids" in data and body.knowledge_item_ids is not None:
        _resolve_knowledge(db, pid, body.knowledge_item_ids)
        d.knowledge_item_ids = list(dict.fromkeys(body.knowledge_item_ids))
        changed.append("knowledge_item_ids")
    if "missing_information" in data and body.missing_information is not None:
        d.missing_information = [m.model_dump(mode="json") for m in body.missing_information]
        changed.append("missing_information")
    if body.nodes:
        by_id = {n.get("id"): n for n in nodes}
        for node_id, patch in body.nodes.items():
            node = by_id.get(node_id)
            if node is None or node.get("type") != "business_task":
                raise AppError(422, "unknown_node", f"the draft has no business task {node_id}", {"node_id": node_id})
            node["params"] = {**(node.get("params") or {}), **patch.model_dump(mode="json", exclude_unset=True)}
            graph_changed = True
        changed.append("nodes")
    if graph_changed or old_fixed is not None:
        graph["nodes"] = nodes
        if old_fixed is not None:  # changed input versions reach the node slots that read them (A54)
            graph = sync_fixed_slots(graph, list(d.fixed_inputs or []), old_fixed)
        _apply_graph(db, acc, w, graph)
    revalidate(db, w, d)
    d.version += 1
    audit(db, action="collaboration.draft.update", subject_type="workflow", subject_id=w.id, user_id=acc.user.id, project_id=pid,
          data={"version": d.version, "fields": changed, "ready_to_start": d.ready_to_start})
    db.flush()
    return w, d


# --- start (C02) ----------------------------------------------------------------------


def _start_request_hash(w: Workflow, body: StartIn) -> str:
    """Hash of the start request itself (draft edits after a start do not turn a replay into a conflict). The policy is
    part of it; a standard start hashes exactly as before W7."""
    data = body.model_dump(mode="json", exclude={"idempotency_key"})
    if data.get("execution_policy") == "standard":
        data.pop("execution_policy")
    return canon_hash({"workflow_id": str(w.id), **data})


def _turbo_binding(db: Session, project_id: uuid.UUID, ex: ActorRef, bp: BusinessTaskParams, *, mode: str,
                   default_executor: ActorRef | None) -> tuple[ActorRef, dict[str, Any] | None]:
    """Turbo (W7) never waits for a person: a node with a human executor is bound to an eligible agent of the project
    — enabled, a supported runtime, purpose execute / general, valid for the mode and already allowed every tool of the node (no permission
    is widened) — the draft's default executor first, then by name. A node without tools, or whose tools no agent may
    use, keeps its person and is recorded ``not_automatable`` (its step fails, it never waits). Returns the executor
    to freeze and the binding record for the contract (None for an agent executor)."""
    if ex.kind == "agent":
        return ex, None
    origin = ex.model_dump(mode="json")
    tools = list(dict.fromkeys(bp.recipe.tools))
    if not tools:
        return ex, {"status": "not_automatable", "from": origin, "tools": [],
                    "reason": "the task names no tool an agent could run (it needs a person)"}
    candidates: list[AgentProfile] = []
    if default_executor is not None and default_executor.kind == "agent":
        p = db.get(AgentProfile, default_executor.agent_profile_id)
        if p is not None:
            candidates.append(p)
    candidates += [p for p in db.scalars(select(AgentProfile).where(AgentProfile.project_id == project_id, AgentProfile.enabled.is_(True),
                                                                    AgentProfile.purpose.in_(("execute", "general")))
                                         .order_by(AgentProfile.name, AgentProfile.id)).all() if p not in candidates]
    for p in candidates:
        ref = _agent_ref(p.id)
        try:
            actors.validate(db, project_id, ref, purpose="execute", mode=mode)
        except AppError:
            continue
        if set(tools) <= set(p.allowed_tools or []):
            return ref, {"status": "bound", "from": origin, "to": ref.model_dump(mode="json"), "tools": tools,
                         "agent_version": p.version, "reason": f"Turbo: executed by agent {p.name}, which is allowed every tool of the task"}
    return ex, {"status": "not_automatable", "from": origin, "tools": tools,
                "reason": f"no enabled agent of the project may use {', '.join(tools)}"}


def start(db: Session, acc: ProjectAccess, w: Workflow, d: CollaborationDraft, *, body: StartIn) -> tuple[WorkflowRun, CollaborationRun, bool]:
    """C02 POST: freeze the draft into a run. Returns (run, collaboration run,
    created); created is False when the idempotency key replayed an existing
    run (A04). Caller checked the `workflow` action and commits.

    Lock order: workflow → draft → project (FOR UPDATE: task numbering takes it
    anyway, and taking it up front avoids a share→update upgrade deadlock
    between two starts) → tasks. Replays are answered under the draft lock, so
    two requests with the same key never both create a run.

    One run per draft version: a start bumps the draft version (recorded in the
    contract as `started_draft_version`), so a second start of the same version
    with another key (a second tab, a reload) is 409 `draft_already_started`
    with the run id; editing the draft makes a new version that can start again."""
    features.require_enabled("collaboration")
    from ..workflow import engine
    from .lifecycle import lock_project

    pid = w.project_id
    w = tpl.lock_workflow(db, w.id)
    d = _lock_draft(db, d.id)
    req_hash = _start_request_hash(w, body)
    existing = db.scalar(select(CollaborationRun).where(CollaborationRun.project_id == pid, CollaborationRun.idempotency_key == body.idempotency_key))
    if existing is not None:
        if existing.request_hash == req_hash:
            run = db.get(WorkflowRun, existing.run_id)
            assert run is not None
            return run, existing, False
        if existing.execution_policy != body.execution_policy:  # the policy is part of the fingerprint (W0 §2)
            raise conflict("idempotency_conflict", "this idempotency key already started a run with another execution policy",
                           idempotency_key=body.idempotency_key, run_id=str(existing.run_id), execution_policy=existing.execution_policy)
        raise conflict("idempotency_mismatch", "this idempotency key was already used to start a different request",
                       idempotency_key=body.idempotency_key, run_id=str(existing.run_id))
    latest = _latest_run(db, d)
    if latest is not None and _is_current_start(latest[1], d):
        raise conflict("draft_already_started", "this version of the draft was already started; open its run, or edit the draft to start a new one",
                       run_id=str(latest[0].id), current_version=d.version)
    lock_project(db, pid, share=False)
    require_active(db, pid, what="starting a collaboration run")
    if w.archived_at is not None:
        raise conflict("workflow_archived", "restore the draft before starting it")
    if body.expected_version != d.version:
        raise conflict("version_conflict", "the draft was changed by someone else; reload first", current_version=d.version)
    if body.execution_mode != d.execution_mode:
        raise AppError(422, "execution_mode_mismatch", f"the draft is {d.execution_mode}; change the draft before starting",
                       {"draft_mode": d.execution_mode})
    if body.execution_policy != d.execution_policy:  # never switched at start: the plan was made for the draft's policy
        raise AppError(422, "execution_policy_mismatch", f"the draft was planned as {d.execution_policy}; plan it again to change it",
                       {"draft_policy": d.execution_policy})
    turbo = d.execution_policy == "turbo"
    if body.fixed_inputs is not None:
        # Versions chosen at start replace the draft's (draft and node slots follow, so the contract stays consistent).
        new_fixed = _resolve_fixed_inputs(db, pid, body.fixed_inputs)
        if new_fixed != list(d.fixed_inputs or []):
            d.fixed_inputs = new_fixed
            _apply_graph(db, acc, w, sync_fixed_slots(w.graph, new_fixed))
            d.version += 1
    fixed = list(d.fixed_inputs or [])
    g, errors = revalidate(db, w, d)
    if g is None or errors or (d.missing_information and not turbo):
        raise AppError(422, "draft_not_ready", "the draft cannot start yet", {"errors": errors, "missing_information": list(d.missing_information or [])})

    # The planner is outside the node contract and may have changed since planning. It is frozen too.
    if d.planner_agent_id is not None:
        planner = db.get(AgentProfile, d.planner_agent_id)
        if planner is not None:
            actors.require_supported_runtime(planner.runtime, agent_profile_id=planner.id)

    # Immutable template version: reuse the latest one when the graph is unchanged.
    h = tpl.draft_hash(w)
    latest = tpl.latest_published(db, w.id)
    version = latest if latest is not None and latest.hash == h else tpl.publish(db, acc.user, w.id, expected_version=None, notes="collaboration start")
    run = engine.create_run(db, graph=version.graph, project_id=pid, part_id=d.part_id, user_id=acc.user.id, origin="workflow",
                            title=(body.title or w.name)[:200], workflow_id=w.id, workflow_version=version.draft_version,
                            execution_policy=d.execution_policy)
    run.template_version_id = version.id
    run.purpose = "collaboration"
    run.task_id = None
    db.flush()

    graph_obj = Graph.model_validate(version.graph)
    nodes_by_id = {n.id: n for n in graph_obj.nodes}
    preds: dict[str, set[str]] = defaultdict(set)
    for e in graph_obj.edges:
        preds[e.target].add(e.source)
    order = topo_order(graph_obj) or list(nodes_by_id)
    node_runs = {nr.node_id: nr for nr in db.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all()}
    profiles: dict[uuid.UUID, AgentProfile] = {}
    role_snapshots: dict[str, Any] = {}

    def snapshot(agent_id: uuid.UUID) -> AgentProfile:
        p = profiles.get(agent_id)
        if p is None:
            p = db.get(AgentProfile, agent_id)
            if p is None:
                raise AppError(422, "agent_not_found", "an agent profile of the draft no longer exists", {"agent_profile_id": str(agent_id)})
            actors.require_supported_runtime(p.runtime, agent_profile_id=p.id)
            profiles[agent_id] = p
            role_snapshots[str(agent_id)] = profiles_svc.snapshot(db, p)
        return p

    if d.planner_agent_id is not None and db.get(AgentProfile, d.planner_agent_id) is not None:
        snapshot(d.planner_agent_id)
    contract_nodes: dict[str, Any] = {}
    task_by_node: dict[str, Task] = {}
    node_tasks: list[CollaborationNodeTask] = []
    default_executor = actors.read(d, "executor")
    for nid in order:
        n = nodes_by_id[nid]
        bp = BusinessTaskParams.model_validate(n.params)
        ex = _ref(bp.executor)
        # Turbo: no acceptance subject and no pre-review; human executors bound to an agent (snapshot) when possible.
        rv = None if turbo or bp.reviewer is None else _ref(bp.reviewer)
        pre_reviewer = None if turbo else bp.pre_reviewer_agent_id
        binding: dict[str, Any] | None = None
        if turbo:
            ex, binding = _turbo_binding(db, pid, ex, bp, mode=d.execution_mode, default_executor=default_executor)
        for ref in (ex, rv):
            if ref is not None and ref.kind == "agent":
                snapshot(ref.id)
        if pre_reviewer is not None:
            snapshot(pre_reviewer)
        # The starter owns the task while criteria and dependencies are written (require_edit),
        # then the frozen executor / reviewer take over (people) or the task has no person (agents).
        t = task_svc.create_task(db, acc, {"title": bp.title, "objective": bp.objective, "part_id": d.part_id, "required": True}, [])
        crits = [
            task_svc.add_criterion(db, acc, t.id, {"description": c.description, "required": c.required, "allowed_types": list(c.allowed_types),
                                                   "rule_spec": dict(c.rule_spec), "manual_required": c.manual_required})
            for c in bp.criteria
        ]
        pred_ids = [task_by_node[p].id for p in sorted(preds[nid]) if p in task_by_node]
        if pred_ids:
            task_svc.set_dependencies(db, acc, t.id, pred_ids)
        t.owner_id = ex.user_id if ex.kind == "human" else None
        t.reviewer_id = rv.user_id if rv is not None and rv.kind == "human" else None
        task_by_node[nid] = t
        limits = profiles_svc.limits_of(profiles[ex.id]) if ex.kind == "agent" else profiles_svc.limits_of(None)
        nt = CollaborationNodeTask(
            run_id=run.id, node_id=nid, node_run_id=node_runs[nid].id, task_id=t.id, pre_reviewer_agent_id=pre_reviewer,
            input_slots=[s.model_dump(mode="json") for s in bp.input_slots], output_slots=[s.model_dump(mode="json") for s in bp.output_slots],
            recipe=bp.recipe.model_dump(mode="json"), limits=limits, phase="blocked",
            blocked_reason={"code": "upstream_pending", "message": "waiting for upstream tasks"} if preds[nid]
            else {"code": "not_started", "message": "waiting for the scheduler to bind inputs"},
            budget=dict(EMPTY_BUDGET), bound_inputs=[], formal_outputs=[],
        )
        actors.assign(nt, "executor", ex)
        if rv is not None:
            actors.assign(nt, "reviewer", rv)
        db.add(nt)
        node_tasks.append(nt)
        contract_nodes[nid] = {
            "title": bp.title, "objective": bp.objective, "executor": ex.model_dump(mode="json"),
            "reviewer": rv.model_dump(mode="json") if rv is not None else None,
            "pre_reviewer_agent_id": str(pre_reviewer) if pre_reviewer else None,
            "criteria": [task_svc.criterion_out(c) for c in crits], "output_slots": nt.output_slots, "input_slots": nt.input_slots,
            "recipe": nt.recipe, "limits": limits, "task_id": str(t.id),
        }
        if binding is not None:
            contract_nodes[nid]["turbo_binding"] = binding
    db.flush()
    # The started version is frozen: another start of it conflicts, a stale expected_version too (edits make a new version).
    d.version += 1
    contract = {
        "schema": 1, "demand": d.demand, "execution_mode": d.execution_mode, "graph_hash": version.hash, "template_version_id": str(version.id),
        "started_draft_version": d.version, "nodes": contract_nodes,
        "fixed_inputs": [{"slot_key": f["slot_key"], "asset_version_id": str(f["asset_version_id"]), "content_hash": f["content_hash"]} for f in fixed],
        "knowledge_item_ids": [str(x) for x in d.knowledge_item_ids or []], "part_id": str(d.part_id) if d.part_id else None,
    }
    if turbo:  # frozen with the run: the policy and the values the planner adopted (standard contracts are unchanged)
        contract["execution_policy"] = d.execution_policy
        contract["assumptions"] = list(d.assumptions or [])
    cr = CollaborationRun(
        run_id=run.id, project_id=pid, workflow_id=w.id, template_version_id=version.id, draft_id=d.id, execution_mode=d.execution_mode,
        execution_policy=d.execution_policy,
        idempotency_key=body.idempotency_key, request_hash=req_hash, contract=contract, contract_hash=canon_hash(contract),
        role_snapshots=role_snapshots, created_by=acc.user.id,
    )
    db.add(cr)
    db.flush()
    # Fixed inputs: the draft's (port fixed:<slot>) plus versions a node names directly (manual graphs).
    bound: dict[str, uuid.UUID] = {}
    wanted: list[tuple[str, uuid.UUID, dict[str, Any]]] = [
        (f["slot_key"], uuid.UUID(str(f["asset_version_id"])), {"asset_type": f.get("asset_type"), "label": f.get("label")}) for f in fixed]
    for nid in order:
        for s in BusinessTaskParams.model_validate(nodes_by_id[nid].params).input_slots:
            if s.source_node_id is None and s.fixed_asset_version_id is not None:
                wanted.append((s.slot_key, s.fixed_asset_version_id, {"asset_type": s.asset_type, "node_id": nid}))
    for slot_key, vid, extra in wanted:
        v = db.get(AssetVersion, vid)
        if v is None:
            raise not_found("asset version")
        port = f"fixed:{slot_key}"
        if port not in bound:
            bound[port] = v.id
            db.add(RunInputBinding(run_id=run.id, port_key=port, asset_version_id=v.id, content_hash=v.content_hash,
                                   snapshot={"slot_key": slot_key, **extra, "availability": v.availability, "publish_state": v.publish_state}))
        collaboration_resources.register(db, run=run, kind="input", asset_version=v, slot_key=slot_key,
                                         metadata={"phase": "blocked", "reason": "fixed_input", **({"node_id": extra["node_id"]} if "node_id" in extra else {})})
    audit(db, action="collaboration.start", subject_type="run", subject_id=run.id, user_id=acc.user.id, project_id=pid,
          data={"workflow_id": str(w.id), "draft_id": str(d.id), "template_version_id": str(version.id), "execution_mode": d.execution_mode,
                "contract_hash": cr.contract_hash, "nodes": len(node_tasks), "tasks": [str(t.id) for t in task_by_node.values()],
                "idempotency_key": body.idempotency_key, "draft_version": d.version, "execution_policy": d.execution_policy})
    db.flush()
    return run, cr, True


# --- projections (C03) ------------------------------------------------------------------


def _collab_meta(cr: CollaborationRun) -> dict[str, Any]:
    return {
        "execution_mode": cr.execution_mode,
        "execution_policy": cr.execution_policy,
        "assumptions": list((cr.contract or {}).get("assumptions") or []),
        "paused_at": _iso(cr.paused_at),
        "pause_reason": cr.pause_reason,
        "control_version": cr.control_version,
        "contract_hash": cr.contract_hash,
        "forked_from_run_id": str(cr.forked_from_run_id) if cr.forked_from_run_id else None,
        "fork_reason": cr.fork_reason,
        "archive_status": cr.archive_status,
        "archive_error": cr.archive_error,
        "knowledge_collection_id": str(cr.knowledge_collection_id) if cr.knowledge_collection_id else None,
        "completed_at": _iso(cr.completed_at),
        "draft_id": str(cr.draft_id) if cr.draft_id else None,
        "workflow_id": str(cr.workflow_id) if cr.workflow_id else None,
        "template_version_id": str(cr.template_version_id) if cr.template_version_id else None,
        "idempotency_key": cr.idempotency_key,
        "created_by": str(cr.created_by),
        "created_at": _iso(cr.created_at),
    }


def _node_order(run: WorkflowRun) -> list[str]:
    try:
        g = Graph.model_validate(run.graph_snapshot)
    except ValidationError:
        return [n.get("id", "") for n in (run.graph_snapshot or {}).get("nodes", [])]
    return topo_order(g) or [n.id for n in g.nodes]


def node_tasks_of(db: Session, run: WorkflowRun) -> list[CollaborationNodeTask]:
    rows = {nt.node_id: nt for nt in db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id)).all()}
    ordered = [rows.pop(nid) for nid in _node_order(run) if nid in rows]
    return ordered + sorted(rows.values(), key=lambda x: x.node_id)


def _execution_out(e: AgentExecution, db: Session, *, with_steps: bool) -> dict[str, Any]:
    data = {
        "id": str(e.id), "role": e.role, "attempt_no": e.attempt_no, "rework_no": e.rework_no, "status": e.status,
        "runtime": e.runtime or "native", "stop_requested_at": _iso(e.stop_requested_at),
        "agent": actors.out(db, "agent", None, e.agent_profile_id), "profile_version": e.profile_version,
        "started_at": _iso(e.started_at), "finished_at": _iso(e.finished_at), "usage": e.usage or {}, "duration_ms": e.duration_ms,
        "error": e.error, "submission_id": str(e.submission_id) if e.submission_id else None,
        "decision_id": str(e.decision_id) if e.decision_id else None, "superseded_by_id": str(e.superseded_by_id) if e.superseded_by_id else None,
    }
    if with_steps:
        data["steps"] = e.steps or []
        data["tool_calls"] = e.tool_calls or []
        data["result"] = e.result
    return data


def _final_decision(db: Session, nt: CollaborationNodeTask) -> dict[str, Any] | None:
    sub_id = nt.accepted_submission_id or nt.current_submission_id
    if sub_id is None:
        return None
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.submission_id == sub_id).order_by(ApprovalRequest.created_at.desc()).limit(1))
    if req is None:
        return None
    dec = db.scalar(select(ApprovalDecision).where(ApprovalDecision.request_id == req.id).order_by(ApprovalDecision.created_at.desc()).limit(1))
    if dec is None:
        return None
    return {"decision_id": str(dec.id), "request_id": str(req.id), "submission_id": str(sub_id), "decision": dec.decision, "comment": dec.comment,
            "actor": actors.out_legacy(db, dec, "actor_id"), "at": _iso(dec.created_at), "criterion_decisions": dec.criterion_decisions or []}


def node_task_out(
    db: Session,
    user: User,
    run: WorkflowRun,
    nt: CollaborationNodeTask,
    *,
    with_steps: bool = False,
    params: dict[str, dict] | None = None,
) -> dict[str, Any]:
    params = params if params is not None else {n["id"]: n.get("params", {}) for n in (run.graph_snapshot or {}).get("nodes", [])}
    p = params.get(nt.node_id, {})
    t = db.get(Task, nt.task_id)
    cur = db.get(TaskSubmission, nt.current_submission_id) if nt.current_submission_id else None
    execs = db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id).order_by(AgentExecution.created_at)).all()
    pre = next((e for e in reversed(execs) if e.role == "pre_review" and e.result is not None), None)
    # Candidates: versions registered as candidate resources of this node plus the selected deliverables (each once).
    candidate_ids = list(dict.fromkeys([
        *db.scalars(select(WorkflowResource.asset_version_id).where(WorkflowResource.node_task_id == nt.id, WorkflowResource.kind == "candidate")
                    .order_by(WorkflowResource.created_at, WorkflowResource.id)).all(),
        *db.scalars(select(TaskDeliverableDraft.asset_version_id).where(TaskDeliverableDraft.task_id == nt.task_id)
                    .order_by(TaskDeliverableDraft.selected_at)).all(),
    ]))
    return {
        "id": str(nt.id),
        "run_id": str(nt.run_id),
        "node_id": nt.node_id,
        "node_run_id": str(nt.node_run_id),
        "task_id": str(nt.task_id),
        "title": p.get("title") or (t.title if t else ""),
        "objective": p.get("objective") or (t.objective if t else ""),
        "phase": nt.phase,
        "blocked_reason": nt.blocked_reason,
        "clarifications": [{"question": c.get("question"), "answer": c.get("answer"), "at": c.get("at")} for c in nt.clarifications or []],
        "task": task_svc.task_out(t) if t else None,
        "executor": actors.out(db, nt.executor_kind, nt.executor_user_id, nt.executor_agent_id),
        "reviewer": actors.out(db, nt.reviewer_kind, nt.reviewer_user_id, nt.reviewer_agent_id),
        "pre_reviewer": actors.out(db, "agent", None, nt.pre_reviewer_agent_id) if nt.pre_reviewer_agent_id else None,
        "input_slots": nt.input_slots or [],
        "output_slots": nt.output_slots or [],
        "bound_inputs": [{"slot_key": b.get("slot_key"), "version": _version_brief(db, b.get("asset_version_id")), "content_hash": b.get("content_hash"),
                          "from_node_id": b.get("from_node_id")} for b in nt.bound_inputs or []],
        "candidates": [_version_brief(db, vid) for vid in candidate_ids],
        "current_submission": submission_out(db, cur, with_snapshot=False) if cur else None,
        "accepted_submission_id": str(nt.accepted_submission_id) if nt.accepted_submission_id else None,
        "formal_outputs": [{"slot_key": f.get("slot_key"), "version": _version_brief(db, f.get("asset_version_id")), "content_hash": f.get("content_hash")}
                           for f in nt.formal_outputs or []],
        "pre_review": {"execution_id": str(pre.id), "result": pre.result, "at": _iso(pre.finished_at or pre.started_at),
                       "agent": actors.out(db, "agent", None, pre.agent_profile_id)} if pre else None,
        "final_decision": _final_decision(db, nt),
        # W7: a Turbo node's outputs are unverified (verification from its node run); its executor binding when a
        # person was replaced by an agent, or why the node could not be automated.
        "verification": _node_verification(db, run, nt),
        "turbo_binding": ((run_contract_nodes(db, run).get(nt.node_id) or {}).get("turbo_binding")),
        "executions": [_execution_out(e, db, with_steps=with_steps) for e in execs],
        "budget": nt.budget or {},
        "limits": nt.limits or {},
        "lease_until": _iso(nt.lease_until),
        "version": nt.version,
        "actions": agent_runtime.allowed_actions(db, user, nt),
    }


def run_contract_nodes(db: Session, run: WorkflowRun) -> dict[str, Any]:
    cr = db.get(CollaborationRun, run.id)
    return dict(((cr.contract if cr is not None else None) or {}).get("nodes") or {})


def _node_verification(db: Session, run: WorkflowRun, nt: CollaborationNodeTask) -> dict[str, Any] | None:
    """The verification block of a node's outputs: what the Turbo completion recorded on its node run; None for a
    standard node (its acceptance is the task's decision)."""
    if nt.phase != "completed_unverified":
        return None
    nr = db.get(NodeRun, nt.node_run_id)
    return (nr.outputs or {}).get("verification") if nr is not None else None


def _progress(db: Session, node_tasks: list[CollaborationNodeTask], *, turbo: bool = False) -> dict[str, int]:
    """Server-side counts: executed = at least one submission; accepted = Task done
    with a valid accepted submission; blocked = phase blocked. Turbo (W7): unverified =
    completed without review or acceptance — never counted as accepted, never as executed submissions."""
    task_ids = [nt.task_id for nt in node_tasks]
    extra = {"unverified": sum(1 for nt in node_tasks if nt.phase == "completed_unverified")} if turbo else {}
    if not task_ids:
        return {"total": 0, "executed": 0, "accepted": 0, "blocked": 0, **extra}
    executed = set(db.scalars(select(TaskSubmission.task_id).where(TaskSubmission.task_id.in_(task_ids)).distinct()).all())
    done = set(db.scalars(select(Task.id).where(Task.id.in_(task_ids), Task.status == "done")).all())
    accepted_ids = [nt.accepted_submission_id for nt in node_tasks if nt.accepted_submission_id]
    approved = set(db.scalars(select(TaskSubmission.id).where(TaskSubmission.id.in_(accepted_ids), TaskSubmission.status == "approved")).all()) \
        if accepted_ids else set()
    accepted = sum(1 for nt in node_tasks if nt.task_id in done and nt.accepted_submission_id in approved)
    return {"total": len(node_tasks), "executed": sum(1 for nt in node_tasks if nt.task_id in executed), "accepted": accepted,
            "blocked": sum(1 for nt in node_tasks if nt.phase == "blocked"), **extra}


def _resources(db: Session, run_id: uuid.UUID) -> dict[str, Any]:
    by_kind = {k: 0 for k in RESOURCE_KINDS}
    for kind, n in db.execute(select(WorkflowResource.kind, func.count()).where(WorkflowResource.run_id == run_id).group_by(WorkflowResource.kind)).all():
        by_kind[kind] = int(n)
    return {"by_kind": by_kind, "total": sum(by_kind.values())}


def _knowledge(db: Session, run_id: uuid.UUID) -> dict[str, Any] | None:
    kc = db.scalar(select(KnowledgeCollection).where(KnowledgeCollection.run_id == run_id))
    if kc is None:
        return None
    return {"collection_id": str(kc.id), "status": kc.status, "index_status": kc.index_status, "current_version_id": str(kc.current_version_id)
            if kc.current_version_id else None}


def _activity(db: Session, run: WorkflowRun, node_tasks: list[CollaborationNodeTask], limit: int = ACTIVITY_LIMIT) -> list[dict[str, Any]]:
    """Audit rows about the run, its tasks and their approval requests plus agent
    execution summaries, newest first."""
    node_of_task = {nt.task_id: nt.node_id for nt in node_tasks}
    node_of_nt = {nt.id: nt.node_id for nt in node_tasks}
    subjects = {str(run.id)} | {str(t) for t in node_of_task} | {str(i) for i in node_of_nt}
    req_rows = db.execute(select(ApprovalRequest.id, ApprovalRequest.task_id).where(ApprovalRequest.task_id.in_(list(node_of_task)))).all() \
        if node_of_task else []
    node_of_req = {str(rid): node_of_task.get(tid) for rid, tid in req_rows}
    subjects |= set(node_of_req)
    items: list[dict[str, Any]] = []
    rows = db.scalars(select(AuditLog).where(AuditLog.project_id == run.project_id, AuditLog.subject_id.in_(list(subjects)))
                      .order_by(AuditLog.created_at.desc()).limit(limit)).all()
    for a in rows:
        sid = a.subject_id
        node_id = None
        try:
            sid_uuid = uuid.UUID(sid)
            node_id = node_of_task.get(sid_uuid) or node_of_nt.get(sid_uuid) or node_of_req.get(sid)
        except ValueError:
            pass
        items.append({"at": _iso(a.created_at), "kind": a.action, "actor": actors.out(db, "agent" if a.agent_id else "human", a.user_id, a.agent_id),
                      "node_id": node_id, "message": a.action.replace(".", " ").replace("_", " "),
                      "ref": {"type": a.subject_type, "id": sid, "data": a.data or {}}})
    if node_tasks:
        execs = db.scalars(select(AgentExecution).where(AgentExecution.run_id == run.id).order_by(AgentExecution.created_at.desc()).limit(limit)).all()
        for e in execs:
            items.append({"at": _iso(e.finished_at or e.started_at or e.created_at), "kind": f"agent.{e.role}",
                          "actor": actors.out(db, "agent", None, e.agent_profile_id), "node_id": node_of_nt.get(e.node_task_id),
                          "message": f"{e.role} attempt {e.attempt_no} {e.status}",
                          "ref": {"type": "agent_execution", "id": str(e.id), "status": e.status, "error": e.error}})
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items[:limit]


def run_actions(db: Session, acc: ProjectAccess, run: WorkflowRun, cr: CollaborationRun) -> list[str]:
    """Run-level controls for the current user (C06 semantics: owner or the starter)."""
    if run.status in TERMINAL_RUN:
        return []
    control = acc.can("manage") or run.created_by == acc.user.id
    out: list[str] = []
    if control and (features.enabled("collaboration") or features.can_continue(db, "collaboration", run_id=run.id)):
        out.append("resume" if run.paused else "pause")
        if cr.execution_mode == "full_auto" and cr.execution_policy != "turbo" and features.enabled("collaboration"):
            out.append("fork_semi_auto")
    if acc.can("workflow"):
        out.append("cancel")
    return out


def collaboration_out(db: Session, user: User, run: WorkflowRun, cr: CollaborationRun) -> dict[str, Any]:
    from ..api.workflows import run_summaries

    acc = project_access(db, user, run.project_id)
    nts = node_tasks_of(db, run)
    params = {n["id"]: n.get("params", {}) for n in (run.graph_snapshot or {}).get("nodes", [])}
    return {
        "run": run_summaries(db, [run])[0],
        "collaboration": _collab_meta(cr),
        "progress": _progress(db, nts, turbo=cr.execution_policy == "turbo"),
        "node_tasks": [node_task_out(db, user, run, nt, params=params) for nt in nts],
        "resources": _resources(db, run.id),
        "knowledge": _knowledge(db, run.id),
        "activity": _activity(db, run, nts),
        "actions": run_actions(db, acc, run, cr),
    }


def list_runs(
    db: Session,
    project_id: uuid.UUID,
    *,
    status: str | None = None,
    mode: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Collaboration instances of a project, newest first, with server-side progress."""
    from ..api.workflows import run_summaries

    stmt = (select(WorkflowRun, CollaborationRun).join(CollaborationRun, CollaborationRun.run_id == WorkflowRun.id)
            .where(WorkflowRun.project_id == project_id).order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
            .offset(max(offset, 0)).limit(min(max(limit, 1), 200)))
    if status:
        stmt = stmt.where(WorkflowRun.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if mode:
        stmt = stmt.where(CollaborationRun.execution_mode == mode)
    rows = db.execute(stmt).all()
    runs = [r[0] for r in rows]
    summaries = {s["id"]: s for s in run_summaries(db, runs)}
    by_run: dict[uuid.UUID, list[CollaborationNodeTask]] = defaultdict(list)
    if runs:
        for nt in db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id.in_([r.id for r in runs]))).all():
            by_run[nt.run_id].append(nt)
    return [{**summaries[str(run.id)], "collaboration": _collab_meta(cr), "progress": _progress(db, by_run.get(run.id, []), turbo=cr.execution_policy == "turbo")} for run, cr in rows]
