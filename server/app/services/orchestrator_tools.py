"""The main agent's tools (AI native, .trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §2.2, §2.4).

:data:`TOOLS` is the one table of what each tool of ``tool_registry.ORCHESTRATOR_TOOL_SPECS`` does: its implementation, the
project action it needs (the operation's own ``OP_ACTION`` for a tool that stands for an operation kind), the autonomies
in which it asks the person first, the child operation kind it creates and how its step is undone (:data:`UNDO`).

Every call (:func:`run_tool`) runs in its own session, as the turn's user: the tool's project action is checked with
``project_access`` first (a refusal is ``ok=false`` / ``code=forbidden``, never an exception) and every other business rule
is the one the same action has elsewhere (task, part and draft services; sub-agents are ordinary AI operations started
through ``assistant.execute``). Results are evidence-style (``ok``, ``message``, ``data``, ``error {code, message}``);
``data.refs`` / ``data.title`` are what the turn's step records.
"""

from __future__ import annotations

import logging
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..ai import evidence
from ..ai.harness.base import ToolSpec
from ..ai.providers.base import clip
from ..ai.tool_registry import ORCHESTRATOR_TOOL_SPECS, check_args
from ..db import session_scope
from ..errors import AppError, conflict
from ..models import AiOperation, Asset, Drawing, File, Intake, ModelRevision, Part, ProcessPlan, Quote, Task, User, Workflow
from ..security import project_access
from . import feature_flags as features
from . import assistant as asst
from .assistant_queries import QUERIES as LOOKUPS
from .assistant_tools import RESULT_CHARS_MAX
from .assistant_tool_inputs import ChildInput, ResolvedDesignPlan, design_part_id, source_text

log = logging.getLogger("partroute.orchestrator")

_CJK_START, _CJK_END = "㐀", "鿿"
_QUOTE_QUANTITY = re.compile(r"(?:批量|数量|件数|qty|quantity|quantities|batch(?:\s+size)?)\s*[:：=]?\s*\d+|"
                             r"\d+\s*(?:件|个|辆|pcs\b|pieces\b|units\b)", re.IGNORECASE)


@dataclass(frozen=True)
class ToolContext:
    """Who runs a tool call and where: the turn, its step, the turn's user, project and conversation."""

    turn_id: uuid.UUID
    step: int
    user_id: uuid.UUID
    project_id: uuid.UUID
    conversation_id: uuid.UUID
    limit_chars: int = RESULT_CHARS_MAX


def _done(tool: str, message: str, *, refs: dict[str, Any], title: str, simulated: bool = False, **data: Any) -> evidence.EvidenceResult:
    return evidence.EvidenceResult(ok=True, tool=tool, message=message, simulated=simulated,
                                   data={"refs": {k: str(v) for k, v in refs.items() if v}, "title": title, **data})


def _zh(text: str) -> bool:
    return any(_CJK_START <= ch <= _CJK_END for ch in text or "")


def _turn(db: Session, ctx: ToolContext) -> AiOperation:
    op = db.get(AiOperation, ctx.turn_id)
    assert op is not None
    return op


def _ref(db: Session, user: User, ctx: ToolContext, kind: str, object_id: Any, **selection: Any) -> dict:
    from .assistant_refs import AiRefInput, normalize_ref

    return normalize_ref(db, user, ctx.project_id, AiRefInput(kind=kind, object_id=object_id, **selection))  # type: ignore[arg-type]


# --- look-ups -------------------------------------------------------------------------------------------------


def _lookup(name: str) -> Callable[[Session, User, ToolContext, dict], evidence.EvidenceResult]:
    """The shared query implementation inside the turn's authorized project."""
    return lambda db, user, ctx, args: LOOKUPS[name](db, ctx.project_id, args, ctx.limit_chars)


def _get_operation_status(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import child_brief, root_of

    op = db.get(AiOperation, uuid.UUID(str(args["operation_id"])))
    if op is None or op.conversation_id != ctx.conversation_id or op.project_id != ctx.project_id or op.user_id != user.id:
        return evidence.fail("get_operation_status", "not_found", "no such operation in this conversation")
    if args.get("event_id"):
        from ..models import OutboxEvent
        from . import completion_context, operation_events

        event = db.get(OutboxEvent, uuid.UUID(str(args["event_id"])))
        turn = _turn(db, ctx)
        if (event is None or event.kind != operation_events.KIND or event.ai_operation_id != op.id
                or event.project_id != ctx.project_id or turn.user_id != user.id
                or turn.conversation_id != ctx.conversation_id
                or event.payload.get("root_operation_id") != str(root_of(turn))):
            return evidence.fail("get_operation_status", "not_found", "no such completion in this request")
        if (event.payload.get("event_id") != str(event.id) or event.payload.get("operation_id") != str(op.id)
                or event.payload.get("generation") != event.ai_generation
                or event.payload.get("event_seq") != event.ai_event_seq or event.payload.get("phase") != event.ai_phase):
            raise AppError(422, "completion_context_invalid", "The frozen event identity does not match its stored locator")
        data = completion_context.fragment(event.payload, args.get("event_path") or "", args.get("offset", 0),
                                           min(args.get("chars", 1000), max(1, ctx.limit_chars // 8)))
        return evidence.EvidenceResult(ok=True, tool="get_operation_status", message="Exact immutable completion JSON fragment",
                                       data=data)
    brief = child_brief(op)
    return evidence.EvidenceResult(ok=True, tool="get_operation_status", message=f"{op.kind} {op.status}",
                                   data={"operation": brief, "note": "status only; the sub-agent's context is not shown"})


# --- parts ---------------------------------------------------------------------------------


def _selected_design(db: Session, user: User, ctx: ToolContext, *, assembly: bool) -> ResolvedDesignPlan | None:
    from . import design_plans
    from .orchestrator import _steps, root_of

    turn = _turn(db, ctx)
    ref = design_plans.selected_ref(db, user, turn, tool_kind="modeling")
    if ref is None:
        root = db.get(AiOperation, root_of(turn))
        request = root.request or {}
        if not any(request.get(key) for key in ("target", "references", "materials")):
            turns = db.scalars(select(AiOperation).where(AiOperation.conversation_id == ctx.conversation_id,
                or_(AiOperation.id == root.id, AiOperation.request["root_op_id"].astext == str(root.id))))
            if any(step.get("tool") == "plan_design" and step.get("status") != "undone"
                   for previous in turns for step in _steps(previous)):
                raise conflict("design_not_authorized", "concept planning has not produced an adopted design; resolve the planning failure before generating CAD")
        return None
    resolved = design_plans.resolve_authorized_design(db, user, ref, conversation_id=ctx.conversation_id,
                                                       root_operation_id=root_of(turn), tool_kind="modeling")
    root = next(d for d in resolved.content.definitions if d.key == resolved.content.root)
    if (root.kind == "assembly") != assembly:
        raise conflict("design_kind_mismatch", "use create_assembly for an assembly plan or create_part/create_model for a part plan")
    return resolved


def _create_part(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from . import parts as part_svc
    from .lifecycle import lock_project
    from .orchestrator import root_of, _steps

    # This synchronous write has no child operation. Give it the same per-human-request protection, including a
    # crash after the domain commit but before the tool step's result was recorded.
    lock_project(db, ctx.project_id, share=False)
    turn = asst._lock_op(db, ctx.turn_id)
    asst.require_open(asst.lock_conversation(db, ctx.conversation_id))
    if turn.status != "running":
        raise conflict("turn_stopped", "the main agent's turn was stopped; no part is created")
    design = _selected_design(db, user, ctx, assembly=False)
    data = {k: args[k] for k in ("name", "number", "material", "description") if args.get(k)}
    if design is not None:
        index, root = next((i, d) for i, d in enumerate(design.content.definitions) if d.key == design.content.root)
        if len(root.material or "") > 40 or len(root.number or "") > 80:
            raise conflict("design_part_metadata_invalid", "the adopted part material or number exceeds its supported length")
        number_path = ["definitions", index, "number"]
        number_bound = any(c.field_path == number_path and c.value == root.number for c in design.content.hard_constraints) or any(
            override.get("field_path") == number_path and override.get("value") == root.number
            and override.get("semantic_anchor") == {"definition_key": root.key}
            for override in design.source_snapshot.get("parameter_overrides", []))
        # A model-proposed label stays in the immutable plan; business numbers use the existing locked allocator
        # unless this exact root field is bound to a validated human constraint or persisted typed choice.
        data = {"name": root.name or design.content.title, "material": root.material or "unknown",
                "number": root.number if number_bound else "", "description": root.notes}
    def identity(value: dict) -> str:
        return json.dumps([" ".join(str(value.get(k) or "").split()).casefold() for k in ("name", "number")], ensure_ascii=False)
    root_id = root_of(turn)
    key = identity(args)
    part_id = design_part_id(root_id, design.ref) if design is not None else uuid.uuid5(root_id, f"create_part:{key}")
    # Existing turns written before deterministic part identities retain their successful/undone receipts.
    for prior in db.scalars(select(AiOperation).where(AiOperation.conversation_id == ctx.conversation_id,
                            AiOperation.request["root_op_id"].astext == str(root_id), AiOperation.kind == "ask")):
        for step in _steps(prior):
            same = ((step.get("refs") or {}).get("part_id") == str(part_id) if design is not None
                    else identity(step.get("args") or {}) == key)
            if step.get("tool") == "create_part" and same:
                recorded = asst._uuid((step.get("refs") or {}).get("part_id"))
                if recorded:
                    if step.get("status") == "undone":
                        raise conflict("work_undone", "the person undid this part; do not recreate it in the same request")
                    part_id = recorded
    part = db.get(Part, part_id)
    reused = part is not None
    if reused:
        _ref(db, user, ctx, "part", part_id)  # Repeat permission/lifecycle checks before exposing the saved object.
    else:
        if turn.execution_policy == "turbo" and design is None and not data.get("material"):
            from ..policy import Assumption

            data["material"] = "AL6061-T651"
            assumption = Assumption(id=f"material-{part_id}", subject=f"part.{part_id}.material", field="material",
                                    adopted=data["material"], basis="No material was supplied; use the shop table's "
                                    "6061 aluminium baseline for a machinable prototype, not a production material decision.",
                                    trigger="missing", affected=[str(part_id)], author="ai_turbo").model_dump(mode="json")
            turn.result = {**(turn.result or {}), "assumptions": [*((turn.result or {}).get("assumptions") or []), assumption]}
        part = part_svc.create_part(db, user, ctx.project_id,
                                   {"id": part_id, **data})
    label = f"{part.number} · {part.name}"
    return _done("create_part", f"part {label} {'already exists for this request' if reused else 'created'}",
                 refs={"part_id": part.id}, title=label, number=part.number, material=part.material, reused=reused,
                 assumptions=(turn.result or {}).get("assumptions") or [])


# --- sub-agents ---------------------------------------------------------------------------------------------------


def _child_result(tool: str, child: AiOperation, what: str, target: dict) -> evidence.EvidenceResult:
    """A started sub-agent (running, the turn is woken when it finishes) or its synchronous failure; the step is titled
    with the object it works on."""
    refs = {"operation_id": str(child.id), **({"part_id": str(child.part_id)} if child.part_id else {})}
    title = " ".join(str(v) for v in (target.get("title"), target.get("version_label")) if v)
    from .orchestrator import child_brief
    brief = child_brief(child)
    if child.status in ("failed", "cancelled"):
        err = child.error or {}
        format_refusal = child.status == "failed" and child.run_id is None and err.get("code") == "drawing_format_mismatch"
        recovery = ("the file is present and no CAD run started; inspect its format capabilities and choose a compatible tool "
                    "for the same original source and request; do not retry this incompatible tool" if format_refusal else
                    "report its saved partial results and recovery to the person; do not restart it or create a replacement automatically")
        error: dict[str, Any] = {"code": err.get("code") or child.status, "message": err.get("message") or child.status}
        if format_refusal:
            entry = (err.get("details") or {}).get("entry") or {}
            error["details"] = {"entry": {k: entry[k] for k in ("action", "actual_format", "formats", "compatible_tools") if k in entry}}
        return evidence.EvidenceResult(ok=False, tool=tool, message=f"the existing {what} sub-agent {child.status}: "
                                       f"{err.get('message') or err.get('code') or child.status}; {recovery}",
                                       data={"refs": refs, "title": title, "status": child.status, "outcome": brief["outcome"], "operation": brief},
                                       error=error)
    if child.status in asst.TERMINAL:
        return _done(tool, f"the {what} sub-agent already finished ({brief['outcome']}); use this recorded result, "
                     "do not repeat the completed work; only continue remaining dependent steps requested by the person",
                     refs=refs, title=title, status=child.status, outcome=brief["outcome"], operation=brief)
    return _done(tool, f"the {what} sub-agent started (operation {child.id}, {child.status}); end the turn, you are woken with its result",
                 refs=refs, title=title, status=child.status)


def _create_model(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target = _ref(db, user, ctx, "part", args["part_id"])
    design = _selected_design(db, user, ctx, assembly=False)
    child = spawn_child(db, user, ctx, kind="model_create", text=args["description"], target=target, task_id=None,
                        domain_input=ChildInput(design_ref=design.ref) if design is not None else None)
    return _child_result("create_model", child, "modeling", target)


def _modify_model(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target = _ref(db, user, ctx, "part_revision", args["revision_id"])
    request = _turn(db, ctx).request or {}
    selections = [r for r in [request.get("target"), *(request.get("references") or [])]
                  if isinstance(r, dict) and r.get("kind") == "cad_selection"]
    if selections:
        if any(str(r.get("object_id")) != str(args["revision_id"]) for r in selections):
            raise conflict("selection_revision_mismatch", "the selected CAD feature belongs to a different exact revision")
        # Revalidate the original semantic pick, retaining its build/mesh identity.
        from .assistant_refs import AiRefInput, normalize_ref
        target = normalize_ref(db, user, ctx.project_id, AiRefInput.model_validate(selections[0]))
    child = spawn_child(db, user, ctx, kind="model_modify", text=args["change"], target=target, task_id=None)
    return _child_result("modify_model", child, "modeling", target)


def _generate_drawing(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    """Decision 9: only an exact, ready revision of this project whose check did not fail is drawn (revision mode, no
    model call), so a failed model step can never be continued into a drawing."""
    from .orchestrator import spawn_child
    from .revisions import check_status

    rev = db.get(ModelRevision, uuid.UUID(str(args["revision_id"])))
    target = _ref(db, user, ctx, "part_revision", args["revision_id"])  # 404 outside the project / archived part
    assert rev is not None
    if rev.status != "ready":
        raise conflict("revision_not_ready", f"R{rev.rev_no} is {rev.status}; only a ready model revision is drawn", revision_id=str(rev.id))
    if check_status(rev) == "failed":
        raise conflict("model_check_failed", f"the requirement check of R{rev.rev_no} failed; fix the model before drawing it",
                       revision_id=str(rev.id))
    text = f"生成 {target.get('title')} R{rev.rev_no} 的二维工程图" if _zh(_turn(db, ctx).request.get("text") or "") else \
        f"2D drawing of {target.get('title')} R{rev.rev_no}"
    child = spawn_child(db, user, ctx, kind="drawing_generate", text=text, target=target, task_id=None,
                        domain_input=ChildInput(requirements=args.get("requirements") or ""))
    out = _child_result("generate_drawing", child, "drawing", target)
    out.data["refs"]["revision_id"] = str(rev.id)
    return out


def _manufacturing_turn(db: Session, user: User, ctx: ToolContext, part_id: uuid.UUID) -> AiOperation:
    """Serialize the short save and its replay receipt; a stopped/archived turn never creates downstream work."""
    from .lifecycle import lock_project
    from .revisions import lock_part

    lock_project(db, ctx.project_id, share=False)
    _ref(db, user, ctx, "part", part_id)
    lock_part(db, part_id)
    turn = asst._lock_op(db, ctx.turn_id)
    asst.require_open(asst.lock_conversation(db, ctx.conversation_id))
    if turn.status != "running":
        raise conflict("turn_stopped", "the main agent's turn was stopped; no manufacturing output is created")
    return turn


def _manufacturing_receipt(db: Session, turn: AiOperation, key: str) -> dict | None:
    """A saved result belongs to the whole human request, including later completion wakes."""
    from .orchestrator import root_of

    root = root_of(turn)
    for prior in db.scalars(select(AiOperation).where(AiOperation.conversation_id == turn.conversation_id,
                            AiOperation.kind == "ask")).all():
        if root_of(prior) == root and (receipt := ((prior.result or {}).get("manufacturing_receipts") or {}).get(key)):
            return receipt
    return None


def _save_manufacturing_receipt(turn: AiOperation, key: str, result: evidence.EvidenceResult) -> evidence.EvidenceResult:
    turn.result = {**(turn.result or {}), "manufacturing_receipts": {
        **((turn.result or {}).get("manufacturing_receipts") or {}), key: result.to_dict()}}
    return result


def _create_process_plan(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from ..domain import PlanParams
    from . import assets, manufacturing
    from .revisions import check_status

    target = _ref(db, user, ctx, "part_revision", args["revision_id"])
    rev = db.get(ModelRevision, uuid.UUID(str(args["revision_id"])))
    assert rev is not None
    turn = _manufacturing_turn(db, user, ctx, rev.part_id)
    key = f"plan:{rev.id}"
    params = PlanParams.model_validate({"allowance_mm": args["allowance_mm"]} if args.get("allowance_mm") is not None else {}).model_dump(mode="json")
    if receipt := _manufacturing_receipt(db, turn, key):
        if receipt["data"]["params"] != params:
            raise conflict("manufacturing_parameters_conflict", "this request already saved a process plan with different "
                           "parameters; report its saved result instead of silently replacing it", saved=receipt["data"])
        return evidence.EvidenceResult(**receipt)
    if rev.status != "ready" or check_status(rev) == "failed":
        raise conflict("model_check_failed", "only an exact ready model revision whose checks did not fail can be planned")
    part = db.get(Part, rev.part_id)
    assert part is not None
    row = manufacturing.create_plan(db, part=part, user=user, model_revision_id=rev.id,
                                    params=params)
    version = assets.ensure_version(db, "process_plan", row.id)
    assert version is not None
    title = f"{target['title']} R{rev.rev_no} · process plan"
    out = _done("create_process_plan", "machining process plan saved from the exact model revision", title=title,
                refs={"part_id": part.id, "revision_id": rev.id, "process_plan_id": row.id,
                      "asset_id": version.asset_id, "asset_version_id": version.id},
                process_plan_id=str(row.id), params=row.params, priceable=bool(row.plan.get("priceable")),
                gaps=row.plan.get("gaps") or [], estimated=bool(row.plan.get("estimated")),
                operations=row.plan.get("operations") or [], source_path=f"/assets/{version.asset_id}?version={version.id}")
    return _save_manufacturing_receipt(turn, key, out)


def _create_quote(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from ..domain import QuoteParams
    from . import assets, manufacturing

    row = db.get(ProcessPlan, uuid.UUID(str(args["process_plan_id"])))
    if row is None:
        from ..errors import not_found
        raise not_found("process plan")
    target = _ref(db, user, ctx, "part", row.part_id)
    turn = _manufacturing_turn(db, user, ctx, row.part_id)
    key = f"quote:{row.id}"
    params = {k: args[k] for k in ("quantities", "margin_pct", "tax_pct", "validity_days") if k in args}
    params.setdefault("quantities", [1])
    if receipt := _manufacturing_receipt(db, turn, key):
        requested = QuoteParams.model_validate({**params, "quote_date": receipt["data"]["params"]["quote_date"]}).model_dump(mode="json")
        if receipt["data"]["params"] != requested:
            raise conflict("manufacturing_parameters_conflict", "this request already saved a quote with different "
                           "parameters; report its saved result instead of silently replacing it", saved=receipt["data"])
        return evidence.EvidenceResult(**receipt)
    if turn.execution_policy == "turbo" and not _QUOTE_QUANTITY.search((turn.request or {}).get("text") or ""):
        from ..policy import Assumption

        assumption = Assumption(id=f"quote-quantity-{row.id}", subject=f"process_plan.{row.id}.quantities", field="quantities",
                                adopted=params["quantities"], unit="pcs", basis="No quote batch size was stated in the human "
                                "request; use the agent's selected estimate tiers with the shop's per-order setup allocation.",
                                trigger="missing", affected=[str(row.id)],
                                author="ai_turbo").model_dump(mode="json")
        turn.result = {**(turn.result or {}), "assumptions": [*((turn.result or {}).get("assumptions") or []), assumption]}
    quote = manufacturing.create_quote(db, plan_row=row, user=user, params=params)
    version = assets.ensure_version(db, "quote", quote.id)
    assert version is not None
    out = _done("create_quote", "estimated quote saved using the shop's synthetic demo rate tables", title=f"{target['title']} · quote",
                refs={"part_id": row.part_id, "process_plan_id": row.id, "quote_id": quote.id,
                      "asset_id": version.asset_id, "asset_version_id": version.id},
                quote_id=str(quote.id), params=quote.params, estimated=True, currency=quote.currency,
                tiers=[{k: tier.get(k) for k in ("qty", "unit_inc_tax_fen", "order_inc_tax_fen", "lead_days")}
                       for tier in quote.result.get("tiers") or []],
                source_path=f"/assets/{version.asset_id}?version={version.id}")
    return _save_manufacturing_receipt(turn, key, out)


def _selected_ref(db: Session, user: User, ctx: ToolContext, kind: str, object_id: str, scope: str) -> dict:
    request = _turn(db, ctx).request or {}
    selected = [r for r in [request.get("target"), *(request.get("references") or [])]
                if isinstance(r, dict) and r.get("kind") == kind and r.get(scope)]
    if selected:
        if len(selected) != 1 or str(selected[0].get("object_id")) != str(object_id):
            raise conflict("selection_revision_mismatch", "the original selection belongs to a different or ambiguous exact version")
        return _ref(db, user, ctx, kind, object_id, **{scope: selected[0][scope]})
    return _ref(db, user, ctx, kind, object_id)


def _source_refs(db: Session, user: User, ctx: ToolContext, args: dict) -> tuple[dict | None, tuple[dict, ...]]:
    files = tuple(_ref(db, user, ctx, "file_version", fid) for fid in args.get("file_version_ids") or [])
    sid = args.get("spec_version_id") or args.get("base_spec_version_id")
    return (_ref(db, user, ctx, "engineering_spec_version", sid) if sid else None), files


def _plan_design(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from . import design_plans
    plan, question = design_plans.plan(db, user, _turn(db, ctx))
    next_action = ('create_assembly' if plan['root_kind'] == 'assembly' else
                   "create_part, then create_model; preserve the request's drawing scope")
    return _done('plan_design', 'Design choices are ready' if question else f'Design plan saved; continue with {next_action}',
                 refs={'design_version_id': plan['ref']['version_id']}, title=plan['title'],
                 design_plan=plan, design_question=question)


def _create_assembly(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target, files = _source_refs(db, user, ctx, args)
    design = _selected_design(db, user, ctx, assembly=True)
    design_ref = design.ref if design is not None else None
    if design_ref is not None and (target or files):
        raise conflict('design_scope_mismatch', 'a new design cannot replace or mix with a referenced source specification')
    child = spawn_child(db, user, ctx, kind="assembly_create", text=args["description"], target=target,
                        task_id=None, domain_input=ChildInput(references=files, design_ref=design_ref,
                            reuse_earlier_builds=args.get("reuse_earlier_builds", True),
                            source_operation_id=str(args["source_operation_id"]) if args.get("source_operation_id") else None,
                            design_from_requirements=bool(args.get("design_from_requirements"))))
    return _child_result("create_assembly", child, "assembly", target or {})


def _modify_assembly(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target = _selected_ref(db, user, ctx, "assembly_revision", args["revision_id"], "instance")
    if len((target.get("instance") or {}).get("path") or []) > 1:
        raise conflict("nested_assembly_edit_unsupported", "select the nested assembly definition explicitly before editing it; the root instance path cannot be flattened")
    child = spawn_child(db, user, ctx, kind="assembly_modify", text=args["change"], target=target, task_id=None)
    return _child_result("modify_assembly", child, "assembly change", target)


def _create_workflow(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target = _ref(db, user, ctx, "part", args["part_id"])
    child = spawn_child(db, user, ctx, kind="workflow_create", text=args["description"], target=target, task_id=None)
    return _child_result("create_workflow", child, "workflow plan", target)


def _edit_workflow(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    selection = {"version": args["expected_version"]}
    if args.get("focus_node_id"):
        selection["node"] = {"node_id": args["focus_node_id"]}
    target = _ref(db, user, ctx, "workflow", args["workflow_id"], **selection)
    child = spawn_child(db, user, ctx, kind="workflow_edit", text=args["change"], target=target, task_id=None)
    return _child_result("edit_workflow", child, "workflow change", target)


def _destination(db: Session, user: User, ctx: ToolContext, source: dict, part_id: str) -> dict:
    part = _ref(db, user, ctx, "part", part_id)
    if source.get("part_id") and str(source["part_id"]) != str(part_id):
        raise conflict("source_part_mismatch", "the exact source belongs to another part; select a compatible destination")
    return part


def _read_drawing(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target = _ref(db, user, ctx, "file_version", args["file_version_id"])
    part = _destination(db, user, ctx, target, args["part_id"])
    child = spawn_child(db, user, ctx, kind="drawing_intake", text=args.get("hints") or "", target=target,
                        task_id=None, domain_input=ChildInput(references=(part,)))
    return _child_result("read_drawing", child, "drawing intake", target)


def _reconstruct_drawing(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child
    from .assistant_refs import AiRefInput, normalize_ref

    req = _turn(db, ctx).request or {}
    target = next((r for r in [req.get("target"), *(req.get("references") or [])]
                   if isinstance(r, dict) and r.get("key") == args["source_ref_key"]), None)
    if target is None:
        raise conflict("source_reference_required", "choose an exact drawing reference already frozen on this request")
    if target["kind"] == "drawing_document_version":
        raise conflict("reconstruct_source_unsupported", "standalone drawing versions do not yet have a verified reconstruction export adapter")
    target = normalize_ref(db, user, ctx.project_id, AiRefInput.model_validate(target))
    part = _destination(db, user, ctx, target, args["part_id"])
    child = spawn_child(db, user, ctx, kind="drawing_reconstruct", text=args.get("notes") or "", target=target,
                        task_id=None, domain_input=ChildInput(sheet=args.get("sheet") or "", notes=args.get("notes") or "", references=(part,)))
    return _child_result("reconstruct_drawing", child, "drawing reconstruction", target)


def _extract_spec(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child
    from . import design_plans

    target, files = _source_refs(db, user, ctx, args)
    design_ref = design_plans.selected_ref(db, user, _turn(db, ctx), tool_kind='spec_extract')
    if design_ref is not None and (target or files):
        raise conflict('design_scope_mismatch', 'a new design cannot overwrite an existing source specification')
    req = _turn(db, ctx).request or {}
    if not (source_text(req).strip() or files or target or req.get("materials")):
        raise conflict("spec_sources_required", "provide text, exact files or an existing specification version")
    child = spawn_child(db, user, ctx, kind="spec_extract", text=args.get("text") or "", target=target,
                        task_id=None, domain_input=ChildInput(title=args.get("title") or "", references=files, design_ref=design_ref,
                            source_operation_id=str(args["source_operation_id"]) if args.get("source_operation_id") else None))
    return _child_result("extract_spec", child, "spec extraction", target or {})


def _create_drawing_document(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    child = spawn_child(db, user, ctx, kind="drafting_create", text=args["description"], target=None,
                        task_id=None, domain_input=ChildInput(title=args["title"]))
    return _child_result("create_drawing_document", child, "standalone drawing", {"title": args["title"]})


def _modify_drawing_document(db: Session, user: User, ctx: ToolContext, args: dict) -> evidence.EvidenceResult:
    from .orchestrator import spawn_child

    target = _selected_ref(db, user, ctx, "drawing_document_version", args["version_id"], "entities")
    child = spawn_child(db, user, ctx, kind="drafting_modify", text=args["change"], target=target, task_id=None)
    return _child_result("modify_drawing_document", child, "standalone drawing change", target)


# --- the table ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDef:
    run: Callable[[Session, User, ToolContext, dict], evidence.EvidenceResult]
    action: str
    asks_in: frozenset[str] = frozenset()  # autonomies in which the step waits for the person's confirmation
    op_kind: str | None = None  # the operation kind the tool stands for (routing)
    title_arg: str | None = None  # the argument a step is titled with until the tool names its object


_EVERY = frozenset({"step"})


def _op(kind: str, run: Callable[..., evidence.EvidenceResult], title_arg: str | None) -> ToolDef:
    return ToolDef(run=run, action=asst.OP_ACTION[kind], asks_in=_EVERY, op_kind=kind, title_arg=title_arg)


TOOLS: dict[str, ToolDef] = {
    **{name: ToolDef(run=_lookup(name), action="view", title_arg="query") for name in LOOKUPS},
    "get_operation_status": ToolDef(run=_get_operation_status, action="view"),
    "plan_design": ToolDef(run=_plan_design, action="design", title_arg="description"),
    "create_part": ToolDef(run=_create_part, action="design", asks_in=_EVERY, title_arg="name"),
    "create_model": _op("model_create", _create_model, "description"),
    "modify_model": _op("model_modify", _modify_model, "change"),
    "generate_drawing": _op("drawing_generate", _generate_drawing, None),
    "create_process_plan": ToolDef(run=_create_process_plan, action="process", asks_in=_EVERY),
    "create_quote": ToolDef(run=_create_quote, action="quote", asks_in=_EVERY),
    "create_assembly": _op("assembly_create", _create_assembly, "description"),
    "modify_assembly": _op("assembly_modify", _modify_assembly, "change"),
    "create_workflow": _op("workflow_create", _create_workflow, "description"),
    "edit_workflow": _op("workflow_edit", _edit_workflow, "change"),
    "read_drawing": _op("drawing_intake", _read_drawing, "hints"),
    "reconstruct_drawing": _op("drawing_reconstruct", _reconstruct_drawing, "notes"),
    "extract_spec": _op("spec_extract", _extract_spec, "text"),
    "create_drawing_document": _op("drafting_create", _create_drawing_document, "description"),
    "modify_drawing_document": _op("drafting_modify", _modify_drawing_document, "change"),
}
assert set(TOOLS) == set(ORCHESTRATOR_TOOL_SPECS), "every main-agent tool has exactly one implementation"


TOOL_FEATURES: dict[str, features.FeatureName] = {
    "search_knowledge": "collaboration",
}


def available_specs(*, read_only: bool = False) -> list[ToolSpec]:
    """The current turn's tool catalog; retained definitions still decode historical steps."""
    return [spec for name, spec in ORCHESTRATOR_TOOL_SPECS.items()
            if (not read_only or spec.read_only) and (name not in TOOL_FEATURES or features.enabled(TOOL_FEATURES[name]))]


def needs_confirm(name: str, autonomy: str) -> bool:
    d = TOOLS.get(name)
    return d is not None and autonomy in d.asks_in


def step_title(name: str, args: dict) -> str:
    d = TOOLS.get(name)
    return clip(str(args.get(d.title_arg) or ""), 120) if d is not None and d.title_arg else ""


def run_tool(name: str, args: Any, ctx: ToolContext) -> evidence.EvidenceResult:
    """One tool call in its own session as the turn's user; never raises (refusals and errors are ok=false results)."""
    spec, d = ORCHESTRATOR_TOOL_SPECS.get(name), TOOLS.get(name)
    if spec is None or d is None:
        return evidence.fail(name, "unknown_tool", f"{name} is not a tool of the main agent")
    checked, problems = check_args(spec, args if args is not None else {})
    if checked is None:
        return evidence.EvidenceResult(ok=False, tool=name, message="invalid arguments: " + "; ".join(problems)[:1500],
                                       error={"code": "invalid_args", "message": "the arguments do not match the tool schema", "problems": problems})
    try:
        with session_scope() as db:
            user = db.get(User, ctx.user_id)
            assert user is not None
            project_access(db, user, ctx.project_id, d.action)
            turn = _turn(db, ctx)
            if (turn.request or {}).get("read_only") and not spec.read_only:
                return evidence.fail(name, "read_only_request", "this request permits read-only tools only")
            if name in TOOL_FEATURES:
                features.require_enabled(TOOL_FEATURES[name])
            return d.run(db, user, ctx, checked)
    except AppError as exc:
        return evidence.EvidenceResult(ok=False, tool=name, message=exc.message,
                                       error={"code": exc.code, "message": exc.message, **({"details": exc.details} if exc.details else {})})
    except Exception as exc:  # noqa: BLE001 - a tool problem is a tool result
        log.exception("main agent tool %s failed", name)
        return evidence.fail(name, "tool_error", clip(f"{type(exc).__name__}: {exc}", 300))


def confirm_question(name: str, args: dict, ctx: ToolContext) -> dict:
    """The exact tool arguments the person is asked to confirm before a write."""
    title = step_title(name, args)
    return {"kind": "confirm", "step": ctx.step, "tool": name, "title": title,
            "summary": f"{name}: {title}".rstrip(": "), "args": args}


# --- undo (design §2.4) ---------------------------------------------------------------------------------------


def _empty_part(db: Session, part: Part) -> bool:
    """Only an untouched draft can reverse creation; saved engineering history must stay accessible."""
    if part.archived_at is not None or part.status != "draft" or part.op_counter:
        return False
    history = [select(model.id).where(model.part_id == part.id).exists()
               for model in (ModelRevision, Drawing, Intake, ProcessPlan, Quote, Asset, File, Task, Workflow)]
    return not db.scalar(select(or_(*history)))


def _undo_part(db: Session, user: User, part: Part) -> None:
    from .audit import audit
    from .cad_admission import check_free
    from .lifecycle import lock_project
    from .revisions import lock_part

    project_access(db, user, part.project_id, "design")  # as POST /parts/{id}/archive
    project = lock_project(db, part.project_id)
    if project.archived_at is not None or project.status == "archived":
        raise conflict("project_archived", "restore the project before undoing this step")
    part = lock_part(db, part.id)
    if not _empty_part(db, part):
        raise conflict("not_undoable", "the part has engineering history; its creation cannot be undone")
    check_free(db, user, part.id)
    part.archived_at = datetime.now(UTC)
    audit(db, action="part.archive", subject_type="part", subject_id=part.id, user_id=user.id, project_id=part.project_id)


def _undo_child(db: Session, user: User, child: AiOperation) -> None:
    card = (child.result or {}).get("cad_result")
    if card:
        from .cad_results import undo

        undo(db, user, child, uuid.UUID(card["undo"]["expected_head"]))
    elif child.status != "cancelled":
        asst.cancel(db, user, child.id)


@dataclass(frozen=True)
class Undo:
    """How a step is undone: the step ref naming the object, its row type, whether it can be undone now, the reverse
    action (with that action's own permission check)."""

    ref: str
    model: type
    can: Callable[[Session, Any], bool]
    run: Callable[[Session, User, Any], None]


_CHILD_UNDO = Undo("operation_id", AiOperation, lambda db, o: o.status in (*asst.ACTIVE, "cancelled") or
                   bool(((o.result or {}).get("cad_result") or {}).get("base_version_id")), _undo_child)
UNDO: dict[str, Undo] = {
    "create_part": Undo("part_id", Part, _empty_part, _undo_part),
    "create_model": _CHILD_UNDO,
    "modify_model": _CHILD_UNDO,
    "generate_drawing": _CHILD_UNDO,
    **{name: _CHILD_UNDO for name, tool in TOOLS.items() if tool.op_kind is not None},
}
