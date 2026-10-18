"""Tools an executing or reviewing agent may call (design §3.2, §3.8).

Every tool calls the same service the workspace and the workflow handlers use
(``revisions.create_revision``, ``drawings.generate``, ``manufacturing.*``),
so an agent's product is an ordinary asset version. A tool runs in its own
transaction (the model loop is outside any transaction) and registers what it
produced as a ``candidate`` resource of the node; the runtime then validates a
delivery against exactly these versions. Tool mistakes are results
(``ok=False`` with a code), never exceptions.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
import zipfile
from datetime import UTC, datetime
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import checks_ledger
from ..ai import retry as call_retry
from ..ai.providers.base import ProviderError, clip
from ..ai.tool_registry import PART_TOOLS, REVIEW_ONLY_TOOLS, TOOL_NAMES, TOOL_SPECS, check_args  # noqa: F401 - re-exported
from ..db import session_scope
from ..errors import AppError
from ..models import AgentProfile, AiCall, Asset, AssetVersion, CollaborationNodeTask, File, FileVersion, ModelRevision, Part, ProcessPlan, User, WorkflowResource, WorkflowRun
from ..storage import read_blob, save_blob
from . import assets
from . import feature_flags as features
from . import collaboration_resources as resources
from . import drawings as drawing_svc
from . import manufacturing as mfg
from .ai import call_error

log = logging.getLogger("partroute.agent_tools")

# TOOL_NAMES, REVIEW_ONLY_TOOLS and PART_TOOLS come from the tool registry (app/ai/tool_registry.py, P2): the names in
# their stable order (W8's general mechanical engineering chain last, project level like the workflow nodes); the
# tools only review / general profiles may hold (an executor never approves its own work); the tools that need the
# node's task to be attached to a part.

MAX_DOCUMENT_CHARS = 400_000


@dataclass
class ToolContext:
    """What a tool needs to know about the attempt it runs in (ids only; no ORM rows)."""

    run_id: uuid.UUID
    project_id: uuid.UUID
    part_id: uuid.UUID | None
    task_id: uuid.UUID
    node_task_id: uuid.UUID
    node_id: str
    node_run_id: uuid.UUID
    execution_id: uuid.UUID
    user_id: uuid.UUID  # the person who started the run: technical owner of files the agent writes
    agent_profile_id: uuid.UUID
    agent_purpose: str
    allowed_tools: frozenset[str]
    # The role's frozen model snapshot (W1, services/ai.role_model_ref), or its config id for runs started before W1.
    model_config_id: uuid.UUID | dict | None
    execution_mode: str
    bound_inputs: list[dict[str, Any]] = field(default_factory=list)
    knowledge_scope: dict[str, Any] = field(default_factory=dict)
    simulated_hint: bool = False
    rework_no: int = 0
    attempt_no: int = 1
    # [{question, answer}] a person gave to this node's earlier questions (agent_runtime.answer_questions).
    clarifications: list[dict[str, str]] = field(default_factory=list)
    # The run's frozen execution policy (W7): passed to every CAD service call; never a current default.
    execution_policy: str = "standard"
    # J: the attempt's deadline (time.monotonic of the worker running it; None: a direct call). Long tools stop starting
    # new model requests / work units at it.
    deadline: float | None = None


@dataclass
class ToolResult:
    ok: bool
    tool: str
    message: str = ""
    produced: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    simulated: bool = False
    # AiCallRecords of the model calls the tool made (already stored in ai_calls with the agent_execution_id);
    # the runtime adds them to the attempt's usage and the node budget (design §3.9).
    calls: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "tool": self.tool, "message": self.message, "produced": self.produced, "data": self.data,
                "error": self.error, "simulated": self.simulated}


def _fail(tool: str, code: str, message: str, **details: Any) -> ToolResult:
    return ToolResult(ok=False, tool=tool, message=message, error={"code": code, "message": message, **details})


def _with_calls(res: ToolResult, calls: list[Any]) -> ToolResult:
    """Attach the model calls a tool made before it failed: they still count against the budget."""
    res.calls = list(calls)
    return res


def _turbo_protocol_error(tool: str, questions: list[str], calls: list[Any], simulated: bool) -> ToolResult:
    """S5: a CAD tool that still asks inside a Turbo run is a protocol error (``turbo_clarification_refused``), never a
    ``needs_clarification`` the runtime would turn into a question for a person."""
    from ..policy import turbo_clarification_error

    err = turbo_clarification_error(questions, step=tool)
    out = _fail(tool, err.pop("code"), err.pop("message"), **err)
    out.simulated = simulated
    return _with_calls(out, calls)


def produced_brief(db: Session, v: AssetVersion) -> dict[str, Any]:
    asset = db.get(Asset, v.asset_id)
    row = assets.specialised(db, v)
    return {"asset_version_id": str(v.id), "asset_type": asset.type if asset else None, "label": assets.human_label(db, asset.type, row, v) if asset else "?",
            "content_hash": v.content_hash, "name": asset.name if asset else None, "labels": list(v.labels or [])}


def _version_of(db: Session, ctx: ToolContext, raw: Any, asset_type: str | None = None) -> AssetVersion | None:
    try:
        vid = uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None
    v = db.get(AssetVersion, vid)
    asset = db.get(Asset, v.asset_id) if v else None
    if v is None or asset is None or asset.project_id != ctx.project_id:
        return None
    if asset_type is not None and asset.type != asset_type:
        return None
    return v


def _pick_input(db: Session, ctx: ToolContext, asset_type: str) -> AssetVersion | None:
    """Latest version of a type among this attempt's candidates, else the bound inputs."""
    rows = db.scalars(
        select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id, WorkflowResource.kind == "candidate")
        .order_by(WorkflowResource.created_at.desc())
    ).all()
    for r in rows:
        v = _version_of(db, ctx, r.asset_version_id, asset_type)
        if v is not None:
            return v
    for b in ctx.bound_inputs:
        v = _version_of(db, ctx, b.get("asset_version_id"), asset_type)
        if v is not None:
            return v
    return None


def _register_candidate(db: Session, ctx: ToolContext, v: AssetVersion, tool: str, extra: dict[str, Any] | None = None) -> None:
    run = db.get(WorkflowRun, ctx.run_id)
    node_task = db.get(CollaborationNodeTask, ctx.node_task_id)
    assert run is not None and node_task is not None
    resources.register(db, run=run, kind="candidate", asset_version=v, node_task=node_task, node_run_id=ctx.node_run_id,
                       agent_execution_id=ctx.execution_id, attempt=ctx.attempt_no,
                       metadata={"phase": "executing", "reason": f"tool:{tool}", "tool": tool, "rework_no": ctx.rework_no, **(extra or {})})


def record_ai_calls(db: Session, ctx: ToolContext, calls: list[Any]) -> None:
    """ai_calls rows attributed to the agent and its execution."""
    record_ai_calls_for(db, node_run_id=ctx.node_run_id, agent_profile_id=ctx.agent_profile_id, execution_id=ctx.execution_id, calls=calls)


def record_ai_calls_for(db: Session, *, node_run_id: uuid.UUID | None, agent_profile_id: uuid.UUID | None, execution_id: uuid.UUID | None,
                        calls: list[Any]) -> list[AiCall]:
    """services.ai.record_calls plus the agent columns (ai_calls.agent_profile_id / agent_execution_id). Returns the rows."""
    rows: list[AiCall] = []
    for c in calls:
        data = c.model_dump() if hasattr(c, "model_dump") else dict(c)
        cfg_id = data.get("model_config_id")
        rows.append(AiCall(
            node_run_id=node_run_id,
            model_config_id=uuid.UUID(cfg_id) if cfg_id else None,
            agent_profile_id=agent_profile_id,
            agent_execution_id=execution_id,
            provider=data.get("provider", "unknown"),
            model_id=data.get("model_id", "unknown"),
            purpose=data.get("purpose", "unknown"),
            request=data.get("request") or {},
            response=data.get("response"),
            status=data.get("status", "error"),
            error=call_error(data),
            latency_ms=data.get("latency_ms"),
            tokens_in=data.get("tokens_in"),
            tokens_out=data.get("tokens_out"),
            simulated=bool(data.get("simulated")),
        ))
    db.add_all(rows)
    return rows


def _user(db: Session, ctx: ToolContext) -> User:
    u = db.get(User, ctx.user_id)
    assert u is not None
    return u


def _part(db: Session, ctx: ToolContext, tool: str) -> Part | ToolResult:
    if ctx.part_id is None:
        return _fail(tool, "part_required", "this task is not attached to a part; the tool needs one")
    p = db.get(Part, ctx.part_id)
    if p is None or p.project_id != ctx.project_id:
        return _fail(tool, "part_required", "the task's part is missing")
    return p


# --- document ----------------------------------------------------------------------------


def _document(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    title = str(args.get("title") or "Document").strip()[:280] or "Document"
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return _fail("document", "content_required", "give the markdown content of the document")
    if len(content) > MAX_DOCUMENT_CHARS:
        return _fail("document", "content_too_long", f"the document is longer than {MAX_DOCUMENT_CHARS} characters")
    note = clip(str(args.get("note") or ""), 2000)
    name = title if title.lower().endswith(".md") else f"{title}.md"
    sha = save_blob(db, content.encode("utf-8"), "text/markdown")
    # Same file name written again by this node = a new version of the same file.
    existing: File | None = None
    for r in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id).order_by(WorkflowResource.created_at.desc())).all():
        v = db.get(AssetVersion, r.asset_version_id)
        fv = db.get(FileVersion, v.file_version_id) if v is not None and v.file_version_id else None
        f = db.get(File, fv.file_id) if fv is not None else None
        if f is not None and f.name == name and f.kind == "document" and f.archived_at is None:
            existing = f
            break
    if existing is None:
        existing = File(project_id=ctx.project_id, part_id=ctx.part_id, kind="document", name=name, created_by=ctx.user_id)
        db.add(existing)
        db.flush()
        version_no = 1
    else:
        version_no = (max((fv.version for fv in db.scalars(select(FileVersion).where(FileVersion.file_id == existing.id)).all()), default=0) + 1)
    fv = FileVersion(file_id=existing.id, version=version_no, blob_sha256=sha, original_name=name,
                     note=f"written by agent {ctx.agent_profile_id} (execution {ctx.execution_id}){': ' + note if note else ''}"[:4000],
                     created_by=ctx.user_id)
    db.add(fv)
    db.flush()
    v = assets.register_file_version(db, fv, node_run_id=ctx.node_run_id)
    if ctx.simulated_hint and "simulated" not in (v.labels or []):
        v.labels = list(v.labels or []) + ["simulated"]
    db.flush()
    _register_candidate(db, ctx, v, "document", {"file_name": name})
    return ToolResult(ok=True, tool="document", message=f"document {name} v{version_no} saved", produced=[produced_brief(db, v)],
                      data={"file_id": str(existing.id), "file_version_id": str(fv.id), "chars": len(content)}, simulated=ctx.simulated_hint)


# --- CAD / manufacturing (same services as workflow/handlers.py) --------------------------------


def _answers(ctx: ToolContext) -> list[dict[str, str]] | None:
    """The node's clarification answers in the shape the CAD cores take (question / answer)."""
    out = [{"question": str(c.get("question") or ""), "answer": str(c.get("answer") or "")}
           for c in ctx.clarifications if str(c.get("answer") or "").strip()]
    return out or None


def _step_source(db: Session, ctx: ToolContext, args: dict[str, Any]) -> FileVersion | ToolResult | None:
    """The STEP file version to import: ``file_version_id``, else a STEP document among this attempt's candidates /
    bound inputs; None when there is none (the tool then creates or modifies a model from text)."""

    def _is_step(fv: FileVersion | None) -> bool:
        return fv is not None and fv.original_name.lower().endswith((".step", ".stp"))

    if args.get("file_version_id"):
        try:
            fv = db.get(FileVersion, uuid.UUID(str(args["file_version_id"])))
        except (TypeError, ValueError):
            fv = None
        f = db.get(File, fv.file_id) if fv else None
        if fv is None or f is None or f.project_id != ctx.project_id or not _is_step(fv):
            return _fail("model_3d", "unknown_file", "file_version_id is not a STEP file version of this project")
        return fv
    rows = db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id, WorkflowResource.kind == "candidate")
                      .order_by(WorkflowResource.created_at.desc())).all()
    for v in [_version_of(db, ctx, r.asset_version_id, "document") for r in rows] + \
             [_version_of(db, ctx, b.get("asset_version_id"), "document") for b in ctx.bound_inputs]:
        fv = db.get(FileVersion, v.file_version_id) if v is not None and v.file_version_id else None
        if _is_step(fv):
            return fv
    return None


def _model_3d(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Same paths as the workflow node: a STEP file (``file_version_id`` or a bound / candidate STEP document) is
    imported (source audit first; assemblies are refused) and, with text, then modified; text alone creates or
    modifies a model with the shared modeling core (services/ai_modeling.py): requirements, server gate,
    independent check of the exported STEP, no revision when the requirements are not met."""
    from . import ai_modeling
    from . import revisions as rev_svc

    part = _part(db, ctx, "model_3d")
    if isinstance(part, ToolResult):
        return part
    text = str(args.get("text") or "").strip()
    step = _step_source(db, ctx, args) if not args.get("base_asset_version_id") else None
    if isinstance(step, ToolResult):
        return step
    imported: ModelRevision | None = None
    if step is not None:
        try:
            imported = rev_svc.import_file_version(db, part_id=part.id, user=_user(db, ctx), file_version_id=step.id,
                                                   node_run_id=ctx.node_run_id)
        except AppError as exc:
            return _fail("model_3d", exc.code, exc.message, **dict(exc.details or {}))
        if not text:
            v = assets.ensure_version(db, "part_model", imported.id)
            assert v is not None
            _register_candidate(db, ctx, v, "model_3d", {"imported": True})
            return ToolResult(ok=True, tool="model_3d", message=f"imported {step.original_name} as R{imported.rev_no}",
                              produced=[produced_brief(db, v)],
                              data={"revision_id": str(imported.id), "rev_no": imported.rev_no, "imported": True,
                                    "step_audit": (imported.checks or {}).get("step_audit")})
    if not text:
        return _fail("model_3d", "text_required", "describe the model to create or the change to make, or bind a STEP file to import")
    base: ModelRevision | None = imported
    base_v = None if imported is not None else (
        _version_of(db, ctx, args.get("base_asset_version_id"), "part_model") if args.get("base_asset_version_id") else _pick_input(db, ctx, "part_model"))
    if args.get("base_asset_version_id") and base_v is None:
        return _fail("model_3d", "unknown_version", "base_asset_version_id is not a model version of this project")
    if base_v is not None:
        base = db.get(ModelRevision, base_v.model_revision_id)
        if base is None or base.part_id != part.id:
            return _fail("model_3d", "bad_input", "the base model revision does not belong to this part")
        if base.mesh_only:
            return _fail("model_3d", "mesh_reference", "a mesh reference cannot be modified; import a STEP file instead")
    try:
        provider, cfg = ai_modeling.select_provider(db, ctx.model_config_id)
    except ProviderError as exc:
        return _fail("model_3d", exc.code, exc.message)
    res = ai_modeling.run_modeling(db, provider=provider, cfg=cfg, part=part, user=_user(db, ctx), base=base, text=text,
                                   answers=_answers(ctx), node_run_id=ctx.node_run_id, policy=ctx.execution_policy)
    calls = list(res.calls)
    record_ai_calls(db, ctx, calls)  # stored here (agent_execution_id set); the runtime adds them to the attempt usage
    label = res.label
    if res.status == "needs_clarification" and ctx.execution_policy == "turbo":
        return _turbo_protocol_error("model_3d", res.questions, calls, bool(label.get("simulated")))
    if res.status == "needs_clarification":
        return ToolResult(ok=False, tool="model_3d", message="the modeling agent needs clarification",
                          data={"questions": res.questions, "gaps": res.gaps, **label},
                          error={"code": "needs_clarification", "message": "; ".join(res.questions)}, simulated=bool(label.get("simulated")),
                          calls=calls)
    if res.status in ("failed", "unsupported"):
        err = res.error or {}
        extra: dict[str, Any] = {"retryable": bool(err.get("retryable"))} if err.get("kind") == "provider" else dict(err.get("details") or {})
        return _with_calls(_fail("model_3d", err.get("code", "ai_failed"), err.get("message") or "no buildable design", **extra), calls)
    rev = res.revision
    assert rev is not None
    v = assets.ensure_version(db, "part_model", rev.id)
    assert v is not None
    _register_candidate(db, ctx, v, "model_3d", label)
    return ToolResult(ok=True, tool="model_3d", message=f"revision R{rev.rev_no} built", produced=[produced_brief(db, v)],
                      data={"revision_id": str(rev.id), "rev_no": rev.rev_no, "changes": res.outcome.changes if res.outcome else [],
                            "check": res.summary, **label},
                      simulated=bool(label.get("simulated")), calls=calls)


def _drawing_source(db: Session, ctx: ToolContext, args: dict[str, Any]) -> tuple[uuid.UUID | None, uuid.UUID | None] | ToolResult:
    """(file_version_id, drawing_id) of the drawing to reconstruct: file_version_id, else asset_version_id (a document
    version holding a DXF / DWG, or a drawing version), else this attempt's candidates / bound inputs (a DXF / DWG
    document first, then a drawing)."""
    from . import drawing_input

    def _of(v: AssetVersion) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
        asset = db.get(Asset, v.asset_id)
        if asset is not None and asset.type == "drawing" and v.drawing_id:
            return None, v.drawing_id
        if v.file_version_id:
            fv = db.get(FileVersion, v.file_version_id)
            if fv is not None and drawing_input.drawing_ext(fv.original_name):
                return fv.id, None
        return None

    if args.get("file_version_id"):
        try:
            fv = db.get(FileVersion, uuid.UUID(str(args["file_version_id"])))
        except (TypeError, ValueError):
            fv = None
        f = db.get(File, fv.file_id) if fv else None
        if fv is None or f is None or f.project_id != ctx.project_id:
            return _fail("drawing_reconstruct", "unknown_file", "file_version_id is not a file version of this project")
        return fv.id, None
    if args.get("asset_version_id"):
        v = _version_of(db, ctx, args["asset_version_id"])
        found = _of(v) if v is not None else None
        if found is None:
            return _fail("drawing_reconstruct", "unknown_version", "asset_version_id is not a DXF / DWG file or a drawing of this project")
        return found
    for asset_type in ("document", "drawing"):
        rows = db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id,
                                                          WorkflowResource.kind == "candidate")
                          .order_by(WorkflowResource.created_at.desc())).all()
        for v in [_version_of(db, ctx, r.asset_version_id, asset_type) for r in rows] + \
                 [_version_of(db, ctx, b.get("asset_version_id"), asset_type) for b in ctx.bound_inputs]:
            found = _of(v) if v is not None else None
            if found is not None:
                return found
    return _fail("drawing_reconstruct", "drawing_required", "no DXF / DWG drawing to reconstruct; give file_version_id or bind one")


def _drawing_reconstruct(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Drawing → STEP with the same core as the workflow node (services/ai_reconstruct.py): evidence index, sheet and
    units questions, requirements grounded in the drawing, conflict / critical gates, independent STEP check.
    Args: file_version_id | asset_version_id (else the bound DXF / DWG input), sheet (frame id or drawing number),
    notes (placement, units, …)."""
    from . import ai_modeling, ai_reconstruct

    part = _part(db, ctx, "drawing_reconstruct")
    if isinstance(part, ToolResult):
        return part
    source = _drawing_source(db, ctx, args)
    if isinstance(source, ToolResult):
        return source
    fv_id, drawing_id = source
    try:
        provider, cfg = ai_modeling.select_provider(db, ctx.model_config_id)
    except ProviderError as exc:
        return _fail("drawing_reconstruct", exc.code, exc.message)
    res = ai_reconstruct.run_reconstruct(db, provider=provider, cfg=cfg, part=part, user=_user(db, ctx), file_version_id=fv_id,
                                         drawing_id=drawing_id, sheet=str(args.get("sheet") or ""),
                                         notes=clip(str(args.get("notes") or args.get("text") or "").strip(), 8000),
                                         answers=_answers(ctx), node_run_id=ctx.node_run_id, policy=ctx.execution_policy)
    calls = list(res.calls)
    record_ai_calls(db, ctx, calls)
    label = res.label
    brief = ai_reconstruct.source_brief(res)
    if res.status == "needs_clarification" and ctx.execution_policy == "turbo":
        return _turbo_protocol_error("drawing_reconstruct", res.questions, calls, bool(label.get("simulated")))
    if res.status == "needs_clarification":
        return ToolResult(ok=False, tool="drawing_reconstruct", message="the reconstruction needs clarification",
                          data={"questions": res.questions, "gaps": res.gaps, "source": brief, **label},
                          error={"code": "needs_clarification", "message": "; ".join(res.questions)},
                          simulated=bool(label.get("simulated")), calls=calls)
    if res.status in ("failed", "unsupported"):
        err = res.error or {}
        extra: dict[str, Any] = {"retryable": bool(err.get("retryable"))} if err.get("kind") == "provider" else dict(err.get("details") or {})
        out = _fail("drawing_reconstruct", err.get("code", "ai_failed"), err.get("message") or "the drawing could not be reconstructed",
                    **extra)
        out.simulated = bool(label.get("simulated"))
        return _with_calls(out, calls)
    rev = res.revision
    assert rev is not None
    v = assets.ensure_version(db, "part_model", rev.id)
    assert v is not None
    _register_candidate(db, ctx, v, "drawing_reconstruct", label)
    return ToolResult(ok=True, tool="drawing_reconstruct", message=f"revision R{rev.rev_no} reconstructed from {brief.get('name') if brief else 'the drawing'}",
                      produced=[produced_brief(db, v)],
                      data={"revision_id": str(rev.id), "rev_no": rev.rev_no, "check": res.summary, "source": brief, **label},
                      simulated=bool(label.get("simulated")), calls=calls)


def _drawing(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    part = _part(db, ctx, "drawing")
    if isinstance(part, ToolResult):
        return part
    v = _version_of(db, ctx, args.get("model_asset_version_id"), "part_model") if args.get("model_asset_version_id") else _pick_input(db, ctx, "part_model")
    if v is None:
        return _fail("drawing", "model_required", "no model version to draw; produce or bind a model first")
    rev = db.get(ModelRevision, v.model_revision_id)
    if rev is None or rev.part_id != part.id:
        return _fail("drawing", "bad_input", "the model revision does not belong to this part")
    # Same package config keys as the workspace (drawings.CONFIG_KEYS); title overrides stay workspace-only.
    config = {k: args[k] for k in drawing_svc.CONFIG_KEYS if k in args}
    try:
        d = drawing_svc.generate(db, rev=rev, user=_user(db, ctx), config=config, node_run_id=ctx.node_run_id,
                                 policy=ctx.execution_policy)
    except AppError as exc:
        return _fail("drawing", exc.code, exc.message, **exc.details)
    dv = assets.ensure_version(db, "drawing", d.id)
    assert dv is not None
    _register_candidate(db, ctx, dv, "drawing")
    brief = drawing_svc.drawing_brief(d)
    return ToolResult(ok=True, tool="drawing",
                      message=f"drawing package of R{rev.rev_no} generated: {brief['pages']} page(s), DWG {brief['dwg_status']}",
                      produced=[produced_brief(db, dv)], data={**brief, "views": len(d.views)})


def _process_plan(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    part = _part(db, ctx, "process_plan")
    if isinstance(part, ToolResult):
        return part
    kwargs: dict[str, Any] = {}
    if args.get("intake_asset_version_id"):
        iv = _version_of(db, ctx, args["intake_asset_version_id"], "intake")
        if iv is None:
            return _fail("process_plan", "unknown_version", "intake_asset_version_id is not an intake version of this project")
        kwargs["intake_id"] = iv.intake_id
    else:
        mv = _version_of(db, ctx, args.get("model_asset_version_id"), "part_model") if args.get("model_asset_version_id") else _pick_input(db, ctx, "part_model")
        if mv is None:
            return _fail("process_plan", "model_required", "no model version to plan from")
        kwargs["model_revision_id"] = mv.model_revision_id
    params = {k: args[k] for k in ("allowance_mm",) if k in args}
    try:
        row = mfg.create_plan(db, part=part, user=_user(db, ctx), params=params, node_run_id=ctx.node_run_id, **kwargs)
    except AppError as exc:
        return _fail("process_plan", exc.code, exc.message, **exc.details)
    pv = assets.ensure_version(db, "process_plan", row.id)
    assert pv is not None
    _register_candidate(db, ctx, pv, "process_plan")
    return ToolResult(ok=True, tool="process_plan", message="process plan created", produced=[produced_brief(db, pv)],
                      data={"process_plan_id": str(row.id), "priceable": bool(row.plan.get("priceable")), "estimated": bool(row.plan.get("estimated"))})


def _quote(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    part = _part(db, ctx, "quote")
    if isinstance(part, ToolResult):
        return part
    pv = _version_of(db, ctx, args.get("plan_asset_version_id"), "process_plan") if args.get("plan_asset_version_id") else _pick_input(db, ctx, "process_plan")
    if pv is None:
        return _fail("quote", "plan_required", "no process plan to quote; produce one first")
    row = db.get(ProcessPlan, pv.process_plan_id)
    if row is None or row.part_id != part.id:
        return _fail("quote", "bad_input", "the process plan does not belong to this part")
    params = {k: args[k] for k in ("quantities", "margin_pct", "tax_pct", "validity_days") if k in args}
    try:
        q = mfg.create_quote(db, plan_row=row, user=_user(db, ctx), params=params, node_run_id=ctx.node_run_id)
    except AppError as exc:
        return _fail("quote", exc.code, exc.message, **exc.details)
    qv = assets.ensure_version(db, "quote", q.id)
    assert qv is not None
    _register_candidate(db, ctx, qv, "quote")
    first = (q.result.get("tiers") or [{}])[0]
    return ToolResult(ok=True, tool="quote", message="quote created", produced=[produced_brief(db, qv)],
                      data={"quote_id": str(q.id), "currency": q.currency, "first_tier": {"qty": first.get("qty"), "unit_inc_tax_fen": first.get("unit_inc_tax_fen")}})


def _export(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Bundle versions (given ids, else this node's candidates and bound inputs) into a zip document."""
    from ..models import Drawing, Quote

    include = set(args.get("include") or ["step", "glb", "svg", "dxf", "plan", "quote", "document"])
    ids = args.get("asset_version_ids")
    versions: list[AssetVersion] = []
    if isinstance(ids, list) and ids:
        for raw in ids:
            v = _version_of(db, ctx, raw)
            if v is None:
                return _fail("export", "unknown_version", f"{raw} is not an asset version of this project")
            versions.append(v)
    else:
        seen: set[uuid.UUID] = set()
        for r in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id, WorkflowResource.kind == "candidate")
                            .order_by(WorkflowResource.created_at)).all():
            v = db.get(AssetVersion, r.asset_version_id)
            if v is not None and v.id not in seen:
                seen.add(v.id)
                versions.append(v)
        for b in ctx.bound_inputs:
            v = _version_of(db, ctx, b.get("asset_version_id"))
            if v is not None and v.id not in seen:
                seen.add(v.id)
                versions.append(v)
    part = db.get(Part, ctx.part_id) if ctx.part_id else None
    prefix = part.number if part else ctx.node_id
    buf = io.BytesIO()
    manifest: list[dict] = []
    drawings_done: set[uuid.UUID] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def add_blob(sha: str | None, name: str) -> None:
            if sha and all(m["file"] != name for m in manifest):
                z.writestr(name, read_blob(db, sha)[0])
                manifest.append({"file": name, "sha256": sha})

        for v in versions:
            asset = db.get(Asset, v.asset_id)
            t = asset.type if asset else None
            if t == "part_model":
                rev = db.get(ModelRevision, v.model_revision_id)
                if rev:
                    if "step" in include:
                        add_blob(rev.step_sha, f"{prefix}_R{rev.rev_no}.step")
                    if "glb" in include:
                        add_blob(rev.glb_sha, f"{prefix}_R{rev.rev_no}.glb")
            elif t == "drawing":
                d = db.get(Drawing, v.drawing_id)
                # the whole package (every page, format, report + manifest.json), the same set as the workflow export
                if d and include & drawing_svc.EXPORT_KEYS and d.id not in drawings_done:
                    drawings_done.add(d.id)
                    try:
                        manifest.extend(drawing_svc.export_drawing(db, z, d))
                    except AppError as exc:
                        return _fail("export", exc.code, exc.message, **exc.details)
            elif t == "process_plan" and "plan" in include:
                row = db.get(ProcessPlan, v.process_plan_id)
                if row:
                    z.writestr(f"{prefix}_process_plan_{v.version_no}.json", json.dumps(row.plan, ensure_ascii=False, indent=2))
                    manifest.append({"file": f"{prefix}_process_plan_{v.version_no}.json", "inputs_hash": row.inputs_hash})
            elif t == "quote" and "quote" in include:
                q = db.get(Quote, v.quote_id)
                if q:
                    z.writestr(f"{prefix}_quote_{v.version_no}.json", json.dumps(q.result, ensure_ascii=False, indent=2))
                    manifest.append({"file": f"{prefix}_quote_{v.version_no}.json", "inputs_hash": q.inputs_hash})
            elif t == "document" and "document" in include:
                fv = db.get(FileVersion, v.file_version_id)
                if fv:
                    add_blob(fv.blob_sha256, f"{prefix}_{fv.original_name}")
        z.writestr("manifest.json", json.dumps({"run_id": str(ctx.run_id), "node_id": ctx.node_id, "files": manifest}, indent=2))
    if not manifest:
        return _fail("export", "nothing_to_export", "no exportable versions were found")
    sha = save_blob(db, buf.getvalue(), "application/zip")
    f = File(project_id=ctx.project_id, part_id=ctx.part_id, kind="other", name=f"{prefix}_{ctx.node_id}_export.zip"[:300], created_by=ctx.user_id)
    db.add(f)
    db.flush()
    fv = FileVersion(file_id=f.id, version=1, blob_sha256=sha, original_name=f.name, note=f"agent export, execution {ctx.execution_id}", created_by=ctx.user_id)
    db.add(fv)
    db.flush()
    v = assets.register_file_version(db, fv, node_run_id=ctx.node_run_id)
    _register_candidate(db, ctx, v, "export")
    return ToolResult(ok=True, tool="export", message=f"{len(manifest)} file(s) bundled", produced=[produced_brief(db, v)],
                      data={"file_version_id": str(fv.id), "files": manifest})


# --- general mechanical engineering chain (W8; the same services as the workflow nodes) ---------------------------


def _access(db: Session, ctx: ToolContext):
    """The run starter's project access (the agent acts within the run's project; its tools are its permission)."""
    from ..security import project_access

    return project_access(db, _user(db, ctx), ctx.project_id, "view")


def _uuid_list(raw: Any) -> list[str]:
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            out.append(str(uuid.UUID(str(x))))
        except (TypeError, ValueError):
            continue
    return out


def _engineering_spec(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Text / file versions (and bound document inputs) → a spec version extracted with the run's policy; or an exact
    spec version (``spec_version_id``) of this project passed through. Its calls count against the attempt budget."""
    from . import attachments
    from . import orchestration as orch

    text = str(args.get("text") or "").strip()
    files = _uuid_list(args.get("file_version_ids"))
    sv = args.get("spec_version_id")
    if not sv:
        for b in ctx.bound_inputs:  # bound documents (a description, a drawing file) are read like referenced files
            v = _version_of(db, ctx, b.get("asset_version_id"), "document")
            if v is not None and v.file_version_id and str(v.file_version_id) not in files:
                files.append(str(v.file_version_id))
    # Generation supplement A: the same source checks as the workflow spec node and the assistant (services/attachments.py);
    # supplement I (A-04 / IN-07, E2E-02): the same explicit choice — revise true makes a new version of that spec with the
    # new material, false uses the exact version and says what it left out.
    revise = args.get("revise") if sv else None
    left_out: dict[str, Any] | None = None
    if revise is None:
        try:
            known = attachments.check_spec_and_sources(db, spec_version_id=sv, file_version_ids=files, text=text, instruction=False)
        except AppError as exc:
            return _fail("engineering_spec", exc.code, exc.message, **dict(exc.details or {}))
        if sv and known:
            files = []  # already read by that exact version: nothing new
    else:
        _known, new_files, changes = attachments.spec_material_mix(db, spec_version_id=sv, file_version_ids=files, text=text, instruction=False)
        if revise and not new_files and not changes:  # nothing new to revise with: the exact version, nothing read again
            revise, files, text = False, [], ""
        elif revise:
            files = new_files
        else:
            left_out = {"file_version_ids": new_files, "text_chars": len(text)}
            files, text = [], ""
    missing = attachments.file_problems(db, ctx.project_id, files)
    if missing:
        return _fail("engineering_spec", "source_unread", "some requested files cannot be read", recovery="replace_source", sources=missing)
    try:
        res = orch.resolve_spec(db, access=_access(db, ctx), policy=ctx.execution_policy, text=text, file_version_ids=files,
                                spec_version_id=sv if (revise or not (text or files)) else None, revise=bool(revise),
                                title=str(args.get("title") or ""), model_ref=ctx.model_config_id)
    except AppError as exc:
        return _fail("engineering_spec", exc.code, exc.message, **dict(exc.details or {}))
    calls = orch.attach_spec_calls(db, res.version, node_run_id=ctx.node_run_id, agent_profile_id=ctx.agent_profile_id,
                                   execution_id=ctx.execution_id) if res.created else []
    check = attachments.precheck(db, res.version, requested_files=files, policy=ctx.execution_policy, user_id=ctx.user_id,
                                 title=res.spec.title, model_ref=ctx.model_config_id) if res.usable else None
    if check is not None and check.call_ids:  # supplement H: the Turbo alias / structure calls
        calls += orch.attach_spec_calls(db, res.version, node_run_id=ctx.node_run_id, agent_profile_id=ctx.agent_profile_id,
                                        execution_id=ctx.execution_id, ids=check.call_ids)
    version = check.derived if check is not None and check.derived is not None else res.version
    brief = orch.spec_brief(db, version, res.spec)
    if not res.usable or (check is not None and check.failure is not None):
        err = (check.failure if check is not None and check.failure is not None else brief.get("error")) or {}
        details = {k: val for k, val in (err.get("details") or {}).items() if k != "recovery"}
        out = _fail("engineering_spec", err.get("code") or "spec_failed", err.get("message") or "the spec could not be extracted",
                    **{"stage": err.get("stage"), "spec_version_id": str(version.id), "recovery": err.get("recovery"), **details})
        out.data, out.simulated = {"spec": brief}, bool(version.simulated)
        return _with_calls(out, calls)
    data: dict[str, Any] = {"spec": brief, "spec_version_id": brief["spec_version_id"]}
    if left_out is not None:  # revise false: the exact version, and what was not used
        data["left_out"] = left_out
    return ToolResult(ok=True, tool="engineering_spec", message=f"{brief['code']} v{brief['version_no']} {brief['status']}",
                      data=data, simulated=bool(version.simulated), calls=calls)


def build_scope(ctx: ToolContext) -> str | None:
    """The agent tools' build scope (generation supplement I, E2E-02): one node task's rework round. Every technical attempt
    of that round (a new agent execution after a lost lease or a failed attempt) and every call of the tool in it
    continues what the scope built for the same spec version — the definitions already built are reused and a partial
    assembly gets its next revision (``orchestration._partial_of_scope``) — like a retry of the workflow node or the
    assistant's continue; a business rework (``rework_no`` + 1: a reviewer sent the work back) builds again — and does not
    take the parts of the rejected round through the cross-version reuse either (``reuse_earlier_builds`` False)."""
    return f"node_task:{ctx.node_task_id}:rework:{int(ctx.rework_no or 0)}" if ctx.node_task_id else None


def lease_check(ctx: ToolContext) -> Any:
    """``keep_going`` for a long tool step: the attempt still holds its node task's lease (a lease-lost execution stops
    starting new definitions instead of racing its replacement in the shared build scope). None outside an attempt (no
    execution row: a direct call)."""
    if ctx.execution_id is None:
        return None

    def keep_going() -> bool:
        from datetime import UTC, datetime

        from ..models import AgentExecution

        with session_scope() as s:
            ex = s.get(AgentExecution, ctx.execution_id)
            if ex is None:
                return True  # not an attempt of the runtime: nothing to lose
            nt = s.get(CollaborationNodeTask, ex.node_task_id)
            return (nt is not None and ex.status == "running" and ex.stop_requested_at is None and nt.lease_token == ex.lease_token
                    and nt.lease_until is not None and nt.lease_until > datetime.now(UTC))

    return keep_going


def _assembly_from_spec(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Spec version → every part definition built on its own (template, else AI modeling) → assembly revision + checks.
    Built parts and the assembly are registered as candidates; with something missing, what was built makes a partial
    assembly (ok=False ``assembly_partial`` with the assembly as a product, its coverage and every definition's outcome);
    nothing placeable gives no assembly (ok=False with the outcomes and the questions)."""
    from . import orchestration as orch

    if not args.get("spec_version_id"):
        return _fail("assembly_from_spec", "spec_version_id_required", "give the spec_version_id the engineering_spec tool returned")
    try:
        v, _spec = orch.load_spec_version(db, ctx.project_id, args["spec_version_id"], ctx.execution_policy)
    except AppError as exc:
        return _fail("assembly_from_spec", exc.code, exc.message, **dict(exc.details or {}))
    calls: list[Any] = []

    def on_calls(new: list[Any]) -> None:
        record_ai_calls(db, ctx, new)
        calls.extend(new)

    # S3 / supplement I: progress per node task and rework round (build_scope) — calling the tool again for the same spec
    # version (another attempt, or after a failure) reuses the definitions already built and continues the partial assembly
    # as its next revision; a business rework builds them again (review fix: without the cross-version reuse, which would
    # hand back the rejected round's parts).
    # J: the model requests and definitions stay inside the attempt's time limit (each request capped to what is left)
    limits = call_retry.CallLimits(deadline=ctx.deadline, deadline_kind="tool") if ctx.deadline is not None else None
    with call_retry.limits_scope(limits):
        build = orch.build_assembly(db, project_id=ctx.project_id, user=_user(db, ctx), spec_version=v, policy=ctx.execution_policy,
                                    model_ref=ctx.model_config_id, node_run_id=ctx.node_run_id, number=str(args.get("number") or ""),
                                    run_checks=bool(args.get("run_checks", True)), on_calls=on_calls, keep_going=lease_check(ctx),
                                    cancelled=orch.run_cancel_check(ctx.run_id), scope=build_scope(ctx),
                                    reuse_earlier_builds=int(ctx.rework_no or 0) == 0)
    produced: list[dict[str, Any]] = []
    for o in build.built:
        pv = assets.ensure_version(db, "part_model", o.revision_id)
        if pv is not None:
            _register_candidate(db, ctx, pv, "assembly_from_spec", {"definition": o.key})
            produced.append(produced_brief(db, pv))
    brief = build.brief(db)
    simulated = bool(brief.get("simulated"))
    if build.revision is None:
        failure = build.failure or {}
        out = _fail("assembly_from_spec", failure.get("code") or "definitions_failed", failure.get("message") or "no assembly was made",
                    **dict(failure.get("details") or {}))
        out.produced, out.data, out.simulated = produced, {"assembly": brief}, simulated
        return _with_calls(out, calls)
    av = assets.ensure_version(db, "assembly_model", build.revision.id)
    assert av is not None
    _register_candidate(db, ctx, av, "assembly_from_spec", {"spec_version_id": str(v.id)})
    produced.append(produced_brief(db, av))
    if build.failure is not None:  # generation supplement B: a partial assembly is a product, reported as not complete
        failure = build.failure
        out = _fail("assembly_from_spec", failure.get("code") or "assembly_partial", failure.get("message") or "partial assembly",
                    **{"recovery": failure.get("recovery"), **dict(failure.get("details") or {})})
        out.produced, out.data, out.simulated = produced, {"assembly": brief}, simulated
        return _with_calls(out, calls)
    return ToolResult(ok=True, tool="assembly_from_spec", message=f"assembly {brief.get('number')} {brief.get('label')} built "
                      f"({(brief.get('stats') or {}).get('leaf_instances')} instances)", produced=produced, data={"assembly": brief},
                      simulated=simulated, calls=calls)


def _assembly_revision_of(db: Session, ctx: ToolContext, raw: Any) -> Any:
    from ..models import AssemblyRevision

    v = _version_of(db, ctx, raw, "assembly_model") if raw else _pick_input(db, ctx, "assembly_model")
    return db.get(AssemblyRevision, v.assembly_revision_id) if v is not None and v.assembly_revision_id else None


def _assembly_drawing(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from . import orchestration as orch

    rev = _assembly_revision_of(db, ctx, args.get("assembly_asset_version_id"))
    if rev is None:
        return _fail("assembly_drawing", "assembly_required", "no assembly version to draw; build or bind one first")
    config = args.get("config") if isinstance(args.get("config"), dict) else None
    if args.get("subassemblies", True):
        return _level_drawings(db, ctx, rev, config)
    try:
        pkg = orch.make_drawings(db, assembly_revision_id=rev.id, user=_user(db, ctx), policy=ctx.execution_policy,
                                 config=config, node_run_id=ctx.node_run_id)
    except AppError as exc:
        return _fail("assembly_drawing", exc.code, exc.message, **dict(exc.details or {}))
    brief = orch.drawing_brief(pkg)
    dv = assets.ensure_version(db, "assembly_drawing", pkg.id)
    produced = []
    if dv is not None:
        _register_candidate(db, ctx, dv, "assembly_drawing")
        produced.append(produced_brief(db, dv))
    return ToolResult(ok=brief["completeness"] == "complete", tool="assembly_drawing", produced=produced, data={"drawings": brief},
                      message=f"drawing package {brief['completeness']}: {brief['pages']} page(s)",
                      error=None if brief["completeness"] == "complete" else {"code": "drawing_package_partial",
                                                                               "message": "the drawing package is incomplete"})


def _level_drawings(db: Session, ctx: ToolContext, rev: Any, config: dict[str, Any] | None) -> ToolResult:
    """Generation supplement I (E2E-02): the layered drawing step of the built-in chain (supplement E,
    ``orchestration.make_level_drawings``) — the root package (GA + every part's detail sheet) and one package per distinct
    sub-assembly revision; every package made is a candidate. ``data.drawings`` has the root package's shape plus
    ``subassembly_packages``; one that could not be made keeps the others (ok false, ``drawing_package_partial`` or the
    root's own code), as the workflow node reports it."""
    from ..models import AssemblyDrawing
    from . import orchestration as orch

    try:
        pkg, root_error, subs = orch.make_level_drawings(db, assembly_revision_id=rev.id, user=_user(db, ctx), policy=ctx.execution_policy,
                                                         config=config, node_run_id=ctx.node_run_id)
    except AppError as exc:
        return _fail("assembly_drawing", exc.code, exc.message, **dict(exc.details or {}))
    made = [s for s in subs if s.get("package_id")]
    if pkg is None and not made:
        err = root_error or {}
        return _fail("assembly_drawing", err.get("code") or "drawing_failed", err.get("message") or "no drawing package was made",
                     **dict(err.get("details") or {}))
    produced: list[dict[str, Any]] = []
    # the root package is registered last (the newest candidate a later tool picks by default) and listed first
    for row in [db.get(AssemblyDrawing, uuid.UUID(s["package_id"])) for s in made] + ([pkg] if pkg is not None else []):
        dv = assets.ensure_version(db, "assembly_drawing", row.id) if row is not None else None
        if dv is not None:
            _register_candidate(db, ctx, dv, "assembly_drawing", {"assembly_revision_id": str(row.assembly_revision_id)})
            produced.insert(0, produced_brief(db, dv))
    brief = orch.drawing_brief(pkg) if pkg is not None else {
        "type": "drawing_package", "package_id": None, "completeness": "failed", "manifest_sha256": None, "pages": 0, "files": 0,
        "error": {"code": (root_error or {}).get("code"), "message": (root_error or {}).get("message")}}
    brief["subassembly_packages"] = subs
    short = [s for s in subs if s.get("completeness") != "complete"]
    error = None
    if root_error is not None:
        error = {"code": root_error["code"], "message": f"the root drawing package could not be made: {root_error['message']}",
                 "retryable": bool(root_error.get("retryable"))}
    elif brief["completeness"] != "complete" or short:
        error = {"code": "drawing_package_partial", "message": "a drawing package is incomplete or could not be made; the delivery lists "
                 "what is missing", "subassemblies": [{k: s.get(k) for k in ("number", "rev_no", "package_id", "completeness", "error")}
                                                      for s in short]}
    return ToolResult(ok=error is None, tool="assembly_drawing", produced=produced, data={"drawings": brief}, error=error,
                      message=f"drawing packages: root {brief['completeness']} ({brief['pages']} page(s)), "
                              f"{len(subs) - len(short)}/{len(subs)} sub-assemblies complete")


def _level_packages(db: Session, ctx: ToolContext, rev: Any) -> list[dict[str, Any]]:
    """The sub-assembly packages of a layered delivery (supplement E entries) for the tools: per distinct sub-assembly
    revision of the tree, the newest package of exactly that revision the drawing tool made in this node run (a revision
    without one is listed by the delivery as not drawn)."""
    from ..models import AssemblyDrawing
    from . import assemblies as asm_svc

    out: list[dict[str, Any]] = []
    for s in asm_svc.subassembly_revisions(db, rev.id):
        q = select(AssemblyDrawing).where(AssemblyDrawing.assembly_revision_id == uuid.UUID(s["assembly_revision_id"]),
                                          AssemblyDrawing.project_id == ctx.project_id)
        q = q.where(AssemblyDrawing.node_run_id == ctx.node_run_id) if ctx.node_run_id else q.where(AssemblyDrawing.node_run_id.is_(None))
        row = db.scalar(q.order_by(AssemblyDrawing.created_at.desc()).limit(1))
        if row is not None:
            out.append({**{k: s[k] for k in ("assembly_id", "assembly_revision_id", "number", "name", "rev_no", "depth", "instances")},
                        "package_id": str(row.id), "completeness": row.completeness})
    return out


def _pick_drawing_of(db: Session, ctx: ToolContext, rev: Any) -> AssetVersion | None:
    """The newest assembly drawing among this attempt's candidates and bound inputs that draws exactly ``rev`` (the root
    package: the layered drawing tool also registers every sub-assembly's package)."""
    from ..models import AssemblyDrawing

    if rev is None:
        return None
    rows = db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id, WorkflowResource.kind == "candidate")
                      .order_by(WorkflowResource.created_at.desc())).all()
    for raw in [r.asset_version_id for r in rows] + [b.get("asset_version_id") for b in ctx.bound_inputs]:
        v = _version_of(db, ctx, raw, "assembly_drawing")
        pkg = db.get(AssemblyDrawing, v.assembly_drawing_id) if v is not None and v.assembly_drawing_id else None
        if pkg is not None and pkg.assembly_revision_id == rev.id:
            return v
    return None


def _scope_parts(db: Session, ctx: ToolContext, spec_version_id: Any) -> list[str]:
    """Part revisions the assembly tool built for this spec version in this build scope (installed or not)."""
    from ..models import AssemblyBuildStep

    scope = build_scope(ctx)
    if scope is None or spec_version_id is None:
        return []
    rows = db.scalars(select(AssemblyBuildStep).where(AssemblyBuildStep.scope == scope, AssemblyBuildStep.spec_version_id == spec_version_id,
                                                      AssemblyBuildStep.status == "built")).all()
    return [str(r.revision_id) for r in rows if r.revision_id]


def _delivery_package(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from . import orchestration as orch

    rev = _assembly_revision_of(db, ctx, args.get("assembly_asset_version_id"))
    pkg, drawing_error = None, None
    raw = args.get("drawing_asset_version_id")
    dv = _version_of(db, ctx, raw, "assembly_drawing") if raw else (_pick_drawing_of(db, ctx, rev) or _pick_input(db, ctx, "assembly_drawing"))
    if dv is not None:
        pkg = orch.load_drawing_package(db, dv.assembly_drawing_id)
    if pkg is None:
        drawing_error = {"code": "drawings_missing", "message": "no assembly drawing version was given or produced"}
    part_revs: list[str] = []
    if rev is None:  # no assembly: deliver what this attempt built (its part candidates), listed as partial
        for r in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == ctx.node_task_id, WorkflowResource.kind == "candidate")).all():
            v = _version_of(db, ctx, r.asset_version_id, "part_model")
            if v is not None and v.model_revision_id:
                part_revs.append(str(v.model_revision_id))
    else:  # supplement I (B-02.5, E2E-02): parts built for it but not installed are delivered on their own, as in the chain
        part_revs = _scope_parts(db, ctx, rev.spec_version_id)
    # supplement I (E2E-02): layered like the built-in chain — every sub-assembly's STEP and package, every part's DWG
    subs = (_level_packages(db, ctx, rev) if rev is not None else []) if args.get("subassemblies", True) else None
    try:
        row = orch.deliver(db, project_id=ctx.project_id, user=_user(db, ctx), policy=ctx.execution_policy,
                           assembly_revision_id=rev.id if rev is not None else None, part_revision_ids=part_revs or None,
                           drawing_package=pkg, drawing_error=drawing_error, drawings_required=bool(args.get("require_drawings", True)),
                           node_run_id=ctx.node_run_id, subassembly_packages=subs)
    except AppError as exc:
        return _fail("delivery_package", exc.code, exc.message, **dict(exc.details or {}))
    mv = assets.ensure_version(db, "delivery_manifest", row.id)
    assert mv is not None
    _register_candidate(db, ctx, mv, "delivery_package", {"completeness": row.completeness})
    brief = orch.delivery_brief(row)
    return ToolResult(ok=True, tool="delivery_package", message=f"{brief['number']} {row.completeness}: {brief['files']} file(s), "
                      f"{len(brief['missing'])} missing", produced=[produced_brief(db, mv)], data={"delivery": brief}, simulated=row.simulated)


# --- knowledge (backend E) and engineering review ---------------------------------------------


def _knowledge(db: Session, ctx: ToolContext, args: dict[str, Any], usage: str) -> ToolResult:
    from . import knowledge
    from .actors import ActorRef

    tool = "knowledge_reference" if usage == "reference" else "knowledge_adopt"
    use_item = getattr(knowledge, "use_item", None)
    if use_item is None:
        return _fail(tool, "not_available", "the knowledge base is not available yet; continue without it")
    try:
        item_id = uuid.UUID(str(args.get("item_id")))
    except (TypeError, ValueError):
        return _fail(tool, "item_required", "give the knowledge item_id to use")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return _fail(tool, "reason_required", "give the reason for using this item")
    slot_key = args.get("slot_key")
    if usage == "baseline" and not slot_key:
        return _fail(tool, "slot_required", "knowledge_adopt needs the input slot_key to adopt into")
    try:
        row = use_item(db, item_id=item_id, usage=usage, target={"run_id": ctx.run_id, "node_id": ctx.node_id},
                       actor=ActorRef(kind="agent", agent_profile_id=ctx.agent_profile_id), reason=reason, slot_key=slot_key, mode=ctx.execution_mode)
    except AppError as exc:
        return _fail(tool, exc.code, exc.message, **exc.details)
    return ToolResult(ok=True, tool=tool, message=f"knowledge item used as {usage}",
                      data={"usage_id": str(getattr(row, "id", "")), "confirmation": getattr(row, "confirmation", None),
                            "asset_version_id": str(getattr(row, "asset_version_id", ""))})


def _engineering_review(db: Session, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Engineering approval by an independent review agent (full-auto). Review-purpose agents only."""
    from . import approvals
    from .status import stale_reasons
    from .submissions import ENGINEERING_STAGES, SUBJECT_OF_TYPE

    if ctx.agent_purpose not in ("review", "general"):
        return _fail("engineering_review", "tool_not_allowed", "only a review agent may record engineering approvals")
    if ctx.execution_policy == "turbo":  # W7: no approval, approver or approval time is ever created in Turbo
        return _fail("engineering_review", "skipped_turbo", "Turbo skips engineering approvals; nothing was recorded")
    v = _version_of(db, ctx, args.get("asset_version_id"))
    if v is None:
        return _fail("engineering_review", "unknown_version", "asset_version_id is not a version of this project")
    asset = db.get(Asset, v.asset_id)
    subject = SUBJECT_OF_TYPE.get(asset.type if asset else "")
    if subject is None:
        return _fail("engineering_review", "not_reviewable", f"a {asset.type if asset else '?'} has no engineering approval stage")
    stage = str(args.get("stage") or ENGINEERING_STAGES[asset.type])
    decision = str(args.get("decision") or "approved")
    if decision not in ("approved", "rejected"):
        return _fail("engineering_review", "unknown_decision", "decision is approved or rejected")
    profile = db.get(AgentProfile, ctx.agent_profile_id)
    assert profile is not None
    try:
        row = approvals.decide_as_agent(db, agent_profile=profile, subject_type=subject[0], subject_id=getattr(v, subject[1]), stage=stage,
                                        decision=decision, comment=clip(str(args.get("comment") or ""), 4000), node_run_id=ctx.node_run_id,
                                        stale_check=stale_reasons)
    except AppError as exc:
        return _fail("engineering_review", exc.code, exc.message, **exc.details)
    return ToolResult(ok=True, tool="engineering_review", message=f"{stage} {decision} recorded",
                      data={"approval_id": str(row.id), "stage": stage, "decision": decision, "asset_version_id": str(v.id)})


_TOOLS = {
    "document": _document,
    "export": _export,
    "model_3d": _model_3d,
    "drawing_reconstruct": _drawing_reconstruct,
    "drawing": _drawing,
    "process_plan": _process_plan,
    "quote": _quote,
    "knowledge_reference": lambda db, ctx, args: _knowledge(db, ctx, args, "reference"),
    "knowledge_adopt": lambda db, ctx, args: _knowledge(db, ctx, args, "baseline"),
    "engineering_review": _engineering_review,
    "engineering_spec": _engineering_spec,
    "assembly_from_spec": _assembly_from_spec,
    "assembly_drawing": _assembly_drawing,
    "delivery_package": _delivery_package,
}
assert tuple(_TOOLS) == TOOL_NAMES, "every registered tool has exactly one implementation"


#: Tool arguments naming the object a call acts on (first present wins), and the versions it reads.
_TARGET_ARGS = (("assembly_asset_version_id", "asset_version"), ("model_asset_version_id", "asset_version"),
                ("base_asset_version_id", "asset_version"), ("plan_asset_version_id", "asset_version"),
                ("asset_version_id", "asset_version"), ("spec_version_id", "engineering_spec_version"), ("revision_id", "model_revision"))
_INPUT_ARGS = ("file_version_ids", "file_version_id", "asset_version_ids", "drawing_asset_version_id", "intake_asset_version_id",
               "spec_version_id")


def run_budget(run: WorkflowRun | None) -> dict[str, Any] | None:
    """The run's frozen budget ``{max_calls, max_seconds}`` (package J keeps it with the run), None when it has none."""
    if run is None:
        return None
    value = getattr(run, "budget", None) or (run.inputs or {}).get("budget")
    return dict(value) if isinstance(value, dict) else None


def execution_brief(db: Session, ctx: ToolContext, args: dict[str, Any], ledger: Any = None) -> dict[str, Any]:
    """How one tool call ran (generation reliability N, design §2.8): the run's frozen execution policy and where it came
    from (a tool never takes a policy of its own, so it cannot override the project default), the input versions (bound
    inputs and the versions named in the arguments), the target, the run budget and the checks the call really ran or
    skipped (``checks_ledger``). Never raises: a missing run reads as unknown fields."""
    out: dict[str, Any] = {"execution_policy": ctx.execution_policy, "policy_source": None, "source_version": None,
                           "inputs": [], "target": None, "budget": None}
    try:
        run = db.get(WorkflowRun, ctx.run_id)
        if run is not None:
            out.update(policy_source=run.policy_source, source_version=(run.inputs or {}).get("policy_source_version"),
                       budget=run_budget(run))
        inputs = [{"kind": "asset_version", "id": str(b.get("asset_version_id")), "slot_key": b.get("slot_key")}
                  for b in ctx.bound_inputs if isinstance(b, dict) and b.get("asset_version_id")]
        for key in _INPUT_ARGS:
            raw = args.get(key)
            for value in raw if isinstance(raw, list) else [raw] if raw else []:
                inputs.append({"kind": key.removesuffix("s").removesuffix("_id"), "id": str(value)})
        out["inputs"] = inputs
        kind, oid = next(((k, args[a]) for a, k in _TARGET_ARGS if args.get(a)), (None, None))
        out["target"] = ({"type": kind, "id": str(oid)} if kind else
                         {"type": "part", "id": str(ctx.part_id)} if ctx.part_id else {"type": "project", "id": str(ctx.project_id)})
    except Exception:  # noqa: BLE001 - the brief describes the call; it never fails the tool
        log.exception("execution brief of a tool call failed")
    if ledger is not None:
        out["checks"] = ledger.summary()
    return out


def invalid_args(name: str, problems: list[str]) -> ToolResult:
    """The result of a call whose arguments do not match the tool's argument model: the model corrects it."""
    return ToolResult(ok=False, tool=name, message="invalid arguments: " + "; ".join(problems)[:1500],
                      error={"code": "invalid_args", "message": "the arguments do not match the tool schema", "problems": problems})


def run_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Run one tool in its own transaction, for every executor (native, tool_agent, /agent-mcp). The server-side
    checks come first (registered name, the attempt's allowed tools, a part when the tool needs one), then the
    arguments against the registry's model: a mismatch is ``ok=False`` / ``invalid_args`` with one problem per field
    and the tool does not run. Tool mistakes come back as ``ok=False``; unexpected exceptions are logged and reported
    as ``tool_error`` (the attempt continues or fails on the runtime's terms, never with a crash)."""
    if name not in TOOL_NAMES:
        return _fail(name, "unknown_tool", f"{name} is not a registered tool")
    if name not in ctx.allowed_tools:
        return _fail(name, "tool_not_allowed", f"{name} is not allowed for this task")
    if name in PART_TOOLS and ctx.part_id is None:
        return _fail(name, "part_required", "this task is not attached to a part; the tool needs one")
    checked, problems = check_args(TOOL_SPECS[name], args)
    if checked is None:
        return invalid_args(name, problems)
    fn = _TOOLS[name]
    try:
        with session_scope() as db, checks_ledger.scope() as ledger:
            if not features.enabled("collaboration"):
                from ..models import AgentExecution

                run = db.get(WorkflowRun, ctx.run_id)
                nt = db.get(CollaborationNodeTask, ctx.node_task_id)
                ex = db.get(AgentExecution, ctx.execution_id)
                if (run is None or nt is None or ex is None or run.project_id != ctx.project_id
                        or nt.run_id != run.id or nt.task_id != ctx.task_id or nt.node_run_id != ctx.node_run_id
                        or nt.node_id != ctx.node_id or ex.run_id != run.id or ex.node_task_id != nt.id
                        or ex.agent_profile_id != ctx.agent_profile_id or ex.status != "running"
                        or run.cancel_requested or run.paused or nt.lease_until is None or nt.lease_until <= datetime.now(UTC)
                        or ex.stop_requested_at is not None or ex.lease_token != nt.lease_token):
                    return _fail(name, "feature_disabled", "this attempt is not eligible to continue", feature="collaboration")
                features.require_run_continuation(db, run.id)
            with features.continuation_scope(db, ctx.run_id):
                res = fn(db, ctx, checked)
            # generation reliability N (design §2.8): every tool result says how it ran — the run's frozen policy and its
            # source (never a current default), the inputs, the target, the run budget and the checks it really ran
            res.data = {**(res.data or {}), "execution": execution_brief(db, ctx, checked, ledger)}
            return res
    except AppError as exc:
        return _fail(name, exc.code, exc.message, **exc.details)
    except Exception as exc:  # noqa: BLE001 - a tool crash is an attempt problem, not a worker crash
        log.exception("tool %s failed for execution %s", name, ctx.execution_id)
        return _fail(name, "tool_error", clip(f"{type(exc).__name__}: {exc}", 500))
