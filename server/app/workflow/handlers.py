"""Workflow node handlers. Each one calls the same services the part
workspace uses, so a workflow run and a manual action produce identical
revisions, drawings, plans and quotes."""

from __future__ import annotations

import functools
import hashlib
import io
import json
import uuid
import zipfile
from collections import Counter
from typing import Any

from sqlalchemy import select

from .. import checks_ledger
from .. import policy as exec_policy
from ..ai.providers.base import ProviderError
from ..cad.requirements import DesignDefinitionContext
from ..errors import AppError
from ..models import AiOperation, File, FileVersion, Intake, ModelRevision, ProcessPlan, Quote, WorkflowRun
from ..services import ai as ai_svc
from ..services import assets
from ..services import drawings as drawing_svc
from ..services import manufacturing as mfg
from ..services import revisions as rev_svc
from ..storage import read_blob, save_blob
from .engine import Handler, NodeContext, NodeFailure, NodeOutcome


def _fail_from(exc: AppError) -> NodeFailure:
    return NodeFailure(exc.code, exc.message, details=exc.details)


def _part(ctx: NodeContext):
    if ctx.part is None:
        raise NodeFailure("part_required", "this node needs the run to be attached to a part")
    return ctx.part


def _revision(ctx: NodeContext, value: Any) -> ModelRevision:
    if not isinstance(value, dict) or value.get("type") != "model":
        raise NodeFailure("bad_input", "expected a model revision input")
    rev = ctx.db.get(ModelRevision, uuid.UUID(value["revision_id"]))
    if rev is None or rev.part_id != _part(ctx).id:
        raise NodeFailure("bad_input", "the model revision does not belong to this part")
    return rev


def _model_ref(rev: ModelRevision) -> dict:
    return {"type": "model", "revision_id": str(rev.id), "rev_no": rev.rev_no, "mesh_only": rev.mesh_only}


def _record(ctx: NodeContext, output_key: str, asset_type: str, row_id: Any) -> None:
    """Link a produced row's asset version to this run (registers it if needed; idempotent).
    Runs in the handler transaction, so it commits or rolls back with the node's result."""
    version = assets.ensure_version(ctx.db, asset_type, row_id)
    assets.record_output(ctx.db, node_run=ctx.node_run, output_key=output_key, version=version)


def _turbo(ctx: NodeContext, assumptions: list[dict] | None = None) -> dict[str, Any]:
    """Extra node outputs of a Turbo run (W7): the product is unverified (its checks were skipped) and carries the
    assumptions it rests on. Standard runs add nothing."""
    if not exec_policy.is_turbo(ctx.policy):
        return {}
    out: dict[str, Any] = {"verification": exec_policy.verification(ctx.policy, "skipped_turbo", "not_requested", assumptions)}
    if assumptions:
        out["assumptions"] = assumptions
    return out


def _assumption_refs(rev: ModelRevision) -> list[dict]:
    from ..services.verification import assumption_refs

    return assumption_refs(rev)


def _not_automatable(what: str) -> NodeFailure:
    """Turbo never waits for a person: a step that needs one fails with an explicit reason."""
    return NodeFailure("not_automatable", f"Turbo cannot wait for a person: {what}; run it in standard mode",
                       details={"execution_policy": "turbo"})


def _file_version(ctx: NodeContext, file_version_id: Any) -> FileVersion:
    fv = ctx.db.get(FileVersion, uuid.UUID(str(file_version_id)))
    f = ctx.db.get(File, fv.file_id) if fv else None
    if fv is None or f is None or f.project_id != ctx.project.id:
        raise NodeFailure("bad_input", "the file does not belong to this project")
    return fv


# --- inputs -----------------------------------------------------------------


def text_input(ctx: NodeContext) -> NodeOutcome:
    return NodeOutcome(outputs={"text": {"type": "text", "text": ctx.params.text}})


def file_input(ctx: NodeContext) -> NodeOutcome:
    fv = _file_version(ctx, ctx.params.file_version_id)
    return NodeOutcome(outputs={"file": {"type": "file", "file_version_id": str(fv.id), "name": fv.original_name}})


def model_input(ctx: NodeContext) -> NodeOutcome:
    rev = _revision(ctx, {"type": "model", "revision_id": str(ctx.params.revision_id)})
    return NodeOutcome(outputs={"model": _model_ref(rev)})


# --- 3D modeling ------------------------------------------------------------


def _clarifications(ctx: NodeContext) -> list[dict]:
    return list((ctx.node_run.outputs or {}).get("clarification", {}).get("answers", []))


def _assistant_private(ctx: NodeContext) -> dict | None:
    """The private AI workspace context (pinned constraints, memory, other references) of the operation
    that started THIS run; None (with a step note) when the graph names an operation of another run,
    e.g. a copied graph. Nothing of it is written to steps or outputs."""
    op_id = getattr(ctx.params, "assistant_operation_id", None)
    if op_id is None:
        return None
    from ..services.assistant_context import private_context_for_run

    private = private_context_for_run(ctx.db, op_id, run_id=ctx.node_run.run_id, project_id=ctx.project.id)
    if private is None:
        ctx.step("assistant_context", ok=False, note="assistant context not available for this run")
    return private


def _assistant_design(ctx: NodeContext) -> DesignDefinitionContext | None:
    """Resolve design authority only for the owning model-create operation's fixed model node.

    The public graph contains an operation locator, never a design grant. Copied graphs, another node,
    another target and a stale plan cannot inherit this context or downgrade to unbound modeling.
    """
    from ..security import project_access
    from ..services import design_plans, design_spec
    from ..services.assistant_tool_inputs import design_part_id, source_text
    from ..services.orchestrator import root_of

    op_id = getattr(ctx.params, "assistant_operation_id", None)
    named = ctx.db.get(AiOperation, op_id) if op_id else None
    bound = ctx.db.scalar(select(AiOperation).where(AiOperation.run_id == ctx.run.id,
        AiOperation.request["tool_input"]["design_ref"].astext.is_not(None)))
    op = bound or named
    if op is None or not (op.request or {}).get("tool_input", {}).get("design_ref"):
        return None
    req = op.request
    target = req.get("target") or {}
    nodes = (ctx.run.graph_snapshot or {}).get("nodes") or []
    if (op.id != op_id or op.kind != "model_create" or op.status != "running" or op.run_id != ctx.run.id
            or ctx.node_run.run_id != ctx.run.id or op.project_id != ctx.project.id
            or ctx.run.project_id != ctx.project.id or op.user_id != ctx.user.id or ctx.run.created_by != ctx.user.id
            or ctx.part is None or op.part_id != ctx.part.id or ctx.run.part_id != ctx.part.id
            or target.get("kind") != "part" or str(target.get("object_id")) != str(ctx.part.id)
            or ctx.node_run.node_id != "model" or ctx.node_run.node_type != "model_3d"
            or len(nodes) != 1 or nodes[0].get("id") != "model" or nodes[0].get("type") != "model_3d"
            or (nodes[0].get("params") or {}).get("assistant_operation_id") != str(op.id)
            or (ctx.run.graph_snapshot or {}).get("edges") or ctx.inputs
            or ctx.params.text != source_text(req) or op.execution_policy != ctx.policy
            or ctx.run.cancel_requested):
        raise NodeFailure("design_scope_mismatch", "the part design is not bound to this exact operation, run, node and target")
    try:
        parent = ctx.db.get(AiOperation, uuid.UUID(req["parent_op_id"]))
        if (parent is None or parent.conversation_id != op.conversation_id or parent.user_id != op.user_id
                or root_of(parent) != root_of(op)):
            raise NodeFailure("design_scope_mismatch", "the part design has no matching parent request")
        resolved = design_plans.resolve_authorized_design(ctx.db, ctx.user, req["tool_input"]["design_ref"],
            conversation_id=op.conversation_id, root_operation_id=root_of(op), tool_kind="model_create")
        root = next(d for d in resolved.content.definitions if d.key == resolved.content.root)
        if root.kind == "assembly":
            raise NodeFailure("design_kind_mismatch", "an assembly design cannot execute as a single-part model")
        if ctx.part.id != design_part_id(root_of(op), resolved.ref):
            raise NodeFailure("design_scope_mismatch", "a part design may only model its own new part")
        if (ctx.part.name, ctx.part.material) != (root.name or resolved.content.title, root.material or "unknown"):
            raise NodeFailure("design_part_metadata_changed", "the part's name or material changed after its design was adopted")
        if req.get("human_source") != {k: resolved.source_snapshot[k] for k in ("operation_id", "message_id", "text")}:
            raise NodeFailure("design_hash_mismatch", "the model's original human source differs from the adopted design")
        if ctx.db.scalar(select(ModelRevision.id).where(ModelRevision.part_id == ctx.part.id).limit(1)):
            raise NodeFailure("design_scope_mismatch", "a new part design cannot overwrite an already modeled part")
        _, version = design_spec.compile_design_spec(ctx.db, project_access(ctx.db, ctx.user, ctx.project.id, "design"),
            resolved, policy=ctx.policy, node_run_id=ctx.node_run.id)
        context = design_spec.definition_contexts(ctx.db, ctx.user, version, node_run_id=ctx.node_run.id)[resolved.content.root]
    except AppError as exc:
        raise _fail_from(exc) from exc
    ctx.step("design_context", **context.provenance())
    return context


def model_3d(ctx: NodeContext) -> NodeOutcome:
    """File only → import; text only → AI create; (file or model) + text → AI modify.

    The AI part is the shared modeling core (services/ai_modeling.py, also used by the collaboration
    tool): requirements + server gate + independent check of the exported STEP."""
    from ..services import ai_modeling

    part = _part(ctx)
    design_context = _assistant_design(ctx)
    text = ""
    if isinstance(ctx.inputs.get("text"), dict):
        text = ctx.inputs["text"].get("text", "")
    text = (text or ctx.params.text or "").strip()
    base: ModelRevision | None = None
    try:
        if ctx.inputs.get("file"):
            fv = _file_version(ctx, ctx.inputs["file"]["file_version_id"])
            base = rev_svc.import_file_version(ctx.db, part_id=part.id, user=ctx.user, file_version_id=fv.id, node_run_id=ctx.node_run.id)
            ctx.step("import", file=fv.original_name, revision=base.rev_no, mesh_only=base.mesh_only)
            _record(ctx, "import", "part_model", base.id)
        elif ctx.inputs.get("model"):
            base = _revision(ctx, ctx.inputs["model"])
    except AppError as exc:
        raise _fail_from(exc)

    if not text:
        if base is None:
            raise NodeFailure("missing_input", "connect a file or model, or enter a request")
        if ctx.inputs.get("file"):  # the import is this node's product; a passed-through model is not
            _record(ctx, "model", "part_model", base.id)
        return NodeOutcome(outputs={"model": _model_ref(base), **_turbo(ctx)}, steps=ctx.steps)
    if base is not None and base.mesh_only:
        raise NodeFailure("mesh_reference", "a mesh reference (STL/OBJ/GLB) cannot be modified; import a STEP file instead")

    answers = _clarifications(ctx)
    try:
        provider, cfg = ai_modeling.select_provider(ctx.db, ai_svc.run_model_ref(ctx.run, "modeling", ctx.params.model_config_id))
    except ProviderError as exc:
        raise NodeFailure(exc.code, exc.message, retryable=False)

    private = _assistant_private(ctx)
    res = ai_modeling.run_modeling(ctx.db, provider=provider, cfg=cfg, part=part, user=ctx.user, base=base, text=text,
                                   answers=answers, context=ctx.params.context or None, private=private,
                                   node_run_id=ctx.node_run.id, policy=ctx.policy, design_context=design_context)
    ctx.steps.extend(res.steps)  # kept in the run detail even if the node fails below
    if res.status in ("failed", "unsupported"):
        err = res.error or {}
        kind = err.get("kind")
        details = {} if kind == "provider" else dict(err.get("details") or {})
        raise NodeFailure(err.get("code", "ai_failed"), err.get("message") or "the model could not produce a buildable design",
                          retryable=bool(err.get("retryable")) if kind != "service" else False, details=details, calls=res.calls)
    if res.status == "needs_clarification" and exec_policy.is_turbo(ctx.policy):  # guard: Turbo never waits for answers
        raise NodeFailure("turbo_clarification_refused", "Turbo cannot wait for clarification answers", calls=res.calls)
    ai_svc.record_calls(ctx.db, res.calls, ctx.node_run.id)
    label = res.label

    if res.status == "needs_clarification":
        clarification: dict[str, Any] = {"questions": res.questions, "answers": answers}
        if res.gaps:
            clarification["gaps"] = res.gaps
        return NodeOutcome(outputs={"clarification": clarification, **label}, steps=ctx.steps, wait_human=True)
    rev = res.revision
    assert rev is not None
    _record(ctx, "model", "part_model", rev.id)
    return NodeOutcome(outputs={**label, "model": _model_ref(rev), "requirements_check": res.summary, **_turbo(ctx, _assumption_refs(rev))},
                       steps=ctx.steps)


def drawing_reconstruct(ctx: NodeContext) -> NodeOutcome:
    """2D drawing (DXF / native DWG) → checked revision: the shared core of services/ai_reconstruct.py (also used by
    the collaboration tool). Waits for a person on questions (sheet, units, conflicting dimensions, missing critical
    facts) like model_3d; fails with ``unsupported`` for out-of-scope parts and records the calls on failure."""
    from ..services import ai_modeling, ai_reconstruct

    part = _part(ctx)
    file_in, drawing_in = ctx.inputs.get("file"), ctx.inputs.get("drawing")
    fv_id = file_in.get("file_version_id") if isinstance(file_in, dict) else None
    drawing_id = drawing_in.get("drawing_id") if isinstance(drawing_in, dict) else None
    if fv_id is None and drawing_id is None:
        drawing_id = ctx.params.drawing_id
    if fv_id is not None:
        _file_version(ctx, fv_id)  # same project check as every file input
    answers = _clarifications(ctx)
    try:
        provider, cfg = ai_modeling.select_provider(ctx.db, ai_svc.run_model_ref(ctx.run, "modeling", ctx.params.model_config_id))
    except ProviderError as exc:
        raise NodeFailure(exc.code, exc.message, retryable=False)
    private = _assistant_private(ctx)
    res = ai_reconstruct.run_reconstruct(ctx.db, provider=provider, cfg=cfg, part=part, user=ctx.user, file_version_id=fv_id,
                                         drawing_id=drawing_id, sheet=ctx.params.sheet, notes=ctx.params.notes.strip(),
                                         answers=answers, context=ctx.params.context or None, private=private,
                                         node_run_id=ctx.node_run.id, policy=ctx.policy)
    ctx.steps.extend(res.steps)  # kept in the run detail even if the node fails below
    label = res.label
    if res.status in ("failed", "unsupported"):
        err = res.error or {}
        kind = err.get("kind")
        details = {} if kind == "provider" else dict(err.get("details") or {})
        if label.get("simulated"):
            details.setdefault("simulated", True)
        raise NodeFailure(err.get("code", "ai_failed"), err.get("message") or "the drawing could not be reconstructed",
                          retryable=bool(err.get("retryable")) if kind == "provider" else False, details=details, calls=res.calls)
    if res.status == "needs_clarification" and exec_policy.is_turbo(ctx.policy):  # guard: Turbo never waits for answers
        raise NodeFailure("turbo_clarification_refused", "Turbo cannot wait for clarification answers", calls=res.calls)
    ai_svc.record_calls(ctx.db, res.calls, ctx.node_run.id)
    source = ai_reconstruct.source_brief(res)
    if res.status == "needs_clarification":
        clarification: dict[str, Any] = {"questions": res.questions, "answers": answers}
        if res.gaps:
            clarification["gaps"] = res.gaps
        return NodeOutcome(outputs={"clarification": clarification, **label, "reconstruct": source}, steps=ctx.steps, wait_human=True)
    rev = res.revision
    assert rev is not None
    _record(ctx, "model", "part_model", rev.id)
    return NodeOutcome(outputs={**label, "model": _model_ref(rev), "requirements_check": res.summary, "reconstruct": source,
                                **_turbo(ctx, _assumption_refs(rev))}, steps=ctx.steps)


# --- drawings, intake, planning ----------------------------------------------


def drawing(ctx: NodeContext) -> NodeOutcome:
    rev = _revision(ctx, ctx.inputs.get("model"))
    try:
        d = drawing_svc.generate(ctx.db, rev=rev, user=ctx.user, config=ctx.params.model_dump(mode="json"), node_run_id=ctx.node_run.id,
                                 policy=ctx.policy)
    except AppError as exc:
        raise _fail_from(exc)
    brief = drawing_svc.drawing_brief(d)
    ctx.step("drawing", revision=rev.rev_no, views=len(d.views), pages=brief["pages"], manifest_sha256=brief["manifest_sha256"],
             annotation=brief["annotation_status"], dwg=brief["dwg_status"])
    _record(ctx, "drawing", "drawing", d.id)
    return NodeOutcome(outputs={"drawing": {"type": "drawing", **brief, "svg_sha": d.svg_sha, "dxf_sha": d.dxf_sha},
                                **_turbo(ctx, brief["verification"].get("assumptions"))}, steps=ctx.steps)


def drawing_intake(ctx: NodeContext) -> NodeOutcome:
    """AI organises the drawing when a vision model is available; otherwise (or
    when data is incomplete) the node waits for a person to enter it. Either
    way the result is an estimate and goes to human review next."""
    part = _part(ctx)
    fv = _file_version(ctx, ctx.inputs["file"]["file_version_id"])
    prefill: dict[str, Any] = {}
    reason = "manual_entry"
    try:
        provider, cfg = ai_svc.provider_for(ctx.db, "intake", ai_svc.run_model_ref(ctx.run, "intake", ctx.params.model_config_id))
    except ProviderError as exc:
        provider, reason = None, exc.code
    if provider is not None:
        from ..ai.intake_agent import IntakeIncomplete, run_intake_agent, to_feature_summary
        from ..ai.providers.base import ImagePart
        from ..domain import find_material

        data, blob = read_blob(ctx.db, fv.blob_sha256)
        try:
            part_file = ImagePart.from_bytes(data, blob.content_type, fv.original_name)
        except ValueError as exc:
            raise NodeFailure("unsupported_file", str(exc))
        outcome = run_intake_agent(provider, [part_file], ctx.params.hints or None)
        ai_svc.record_calls(ctx.db, outcome.calls, ctx.node_run.id)
        ctx.steps.extend(s.model_dump() for s in outcome.steps)
        if outcome.status == "extracted" and outcome.result is not None:
            prefill = outcome.result.model_dump(mode="json")
            try:
                resolver = lambda raw: (m.id if (m := find_material(raw)) else None)  # noqa: E731
                summary = to_feature_summary(outcome, {"material": ctx.params.material} if ctx.params.material else None, material_resolver=resolver)
                intake = Intake(part_id=part.id, file_version_id=fv.id, summary=summary.model_dump(mode="json"),
                                extraction={**prefill, "simulated": outcome.simulated, "low_confidence": outcome.low_confidence},
                                method="ai", node_run_id=ctx.node_run.id, created_by=ctx.user.id)
                ctx.db.add(intake)
                ctx.db.flush()
                _record(ctx, "spec", "intake", intake.id)  # intake asset (estimated, simulated if so)
                return NodeOutcome(outputs={"spec": {"type": "spec", "intake_id": str(intake.id), "estimated": True,
                                                     "simulated": outcome.simulated}, **_turbo(ctx)}, steps=ctx.steps)
            except IntakeIncomplete as exc:
                reason = "incomplete:" + ",".join(exc.missing)
        else:
            reason = (outcome.error or {}).get("code", outcome.status)
    if exec_policy.is_turbo(ctx.policy):  # manual entry is a person's work: fail instead of waiting
        raise _not_automatable(f"the drawing data needs manual entry ({reason})")
    # Hand over to a person: POST /parts/{id}/intakes, then the run decision with intake_id.
    return NodeOutcome(outputs={"manual_entry": {"reason": reason, "prefill": prefill, "file_version_id": str(fv.id)}},
                       steps=ctx.steps, wait_human=True)


def process_plan(ctx: NodeContext) -> NodeOutcome:
    part = _part(ctx)
    kwargs: dict[str, Any] = {}
    if ctx.inputs.get("model"):
        kwargs["model_revision_id"] = _revision(ctx, ctx.inputs["model"]).id
    else:
        spec = ctx.inputs.get("spec") or {}
        if spec.get("type") != "spec":
            raise NodeFailure("bad_input", "expected drawing intake data")
        kwargs["intake_id"] = uuid.UUID(spec["intake_id"])
    try:
        row = mfg.create_plan(ctx.db, part=part, user=ctx.user, params=ctx.params.model_dump(mode="json"), node_run_id=ctx.node_run.id, **kwargs)
    except AppError as exc:
        raise _fail_from(exc)
    _record(ctx, "plan", "process_plan", row.id)
    return NodeOutcome(outputs={"plan": {"type": "plan", "process_plan_id": str(row.id), "priceable": bool(row.plan.get("priceable")),
                                         "estimated": bool(row.plan.get("estimated"))}, **_turbo(ctx)})


def quote(ctx: NodeContext) -> NodeOutcome:
    value = ctx.inputs.get("plan") or {}
    if value.get("type") != "plan":
        raise NodeFailure("bad_input", "expected a process plan")
    row = ctx.db.get(ProcessPlan, uuid.UUID(value["process_plan_id"]))
    if row is None or row.part_id != _part(ctx).id:
        raise NodeFailure("bad_input", "the process plan does not belong to this part")
    params = ctx.params.model_dump(mode="json")
    try:
        q = mfg.create_quote(ctx.db, plan_row=row, user=ctx.user, params=params, node_run_id=ctx.node_run.id)
    except AppError as exc:
        raise _fail_from(exc)
    _record(ctx, "quote", "quote", q.id)
    first = (q.result.get("tiers") or [{}])[0]
    return NodeOutcome(outputs={"quote": {"type": "quote", "quote_id": str(q.id), "currency": q.currency,
                                          "first_tier": {"qty": first.get("qty"), "unit_inc_tax_fen": first.get("unit_inc_tax_fen")}},
                                **_turbo(ctx)})


def human_review(ctx: NodeContext) -> NodeOutcome:
    subject = ctx.inputs.get("subject")
    if not isinstance(subject, dict) or subject.get("type") not in ("model", "drawing", "spec", "plan", "quote"):
        raise NodeFailure("bad_input", "nothing reviewable is connected")
    # Turbo (W7): the approval is skipped and recorded as such; no approval request, approver or decision is made and
    # the subject passes on unverified. Standard waits for the person (engineering approval request).
    # The person's decision itself is the review.human check (approval_requests.decide_node).
    if exec_policy.is_turbo(ctx.policy):
        review = exec_policy.skipped("review.human")
        ctx.step("review_skipped", check="review.human", status="skipped_turbo")
        return NodeOutcome(outputs={"subject": subject, "review": review, **_turbo(ctx)}, steps=ctx.steps)
    return NodeOutcome(outputs={"subject": subject}, wait_human=True)


# --- export -----------------------------------------------------------------


def export(ctx: NodeContext) -> NodeOutcome:
    part = _part(ctx)
    include = set(ctx.params.include)
    buf = io.BytesIO()
    manifest: list[dict] = []
    drawings_done: set[uuid.UUID] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def add_blob(sha: str | None, name: str) -> None:
            if sha and all(m["file"] != name for m in manifest):
                z.writestr(name, read_blob(ctx.db, sha)[0])
                manifest.append({"file": name, "sha256": sha})

        for item in ctx.inputs.get("items") or []:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "model":
                rev = _revision(ctx, item)
                if "step" in include:
                    add_blob(rev.step_sha, f"{part.number}_R{rev.rev_no}.step")
                if "glb" in include:
                    add_blob(rev.glb_sha, f"{part.number}_R{rev.rev_no}.glb")
            elif t == "drawing":
                from ..models import Drawing

                d = ctx.db.get(Drawing, uuid.UUID(item["drawing_id"]))
                if d and d.part_id == part.id:
                    rev = ctx.db.get(ModelRevision, d.model_revision_id) if d.model_revision_id else None
                    if include & drawing_svc.EXPORT_KEYS and d.id not in drawings_done:  # whole package + manifest.json
                        drawings_done.add(d.id)
                        try:
                            manifest.extend(drawing_svc.export_drawing(ctx.db, z, d))
                        except AppError as exc:
                            raise _fail_from(exc)
                    if "step" in include and rev:
                        add_blob(rev.step_sha, f"{part.number}_R{rev.rev_no}.step")
            elif t == "plan" and "plan" in include:
                row = ctx.db.get(ProcessPlan, uuid.UUID(item["process_plan_id"]))
                if row:
                    z.writestr(f"{part.number}_process_plan.json", json.dumps(row.plan, ensure_ascii=False, indent=2))
                    manifest.append({"file": f"{part.number}_process_plan.json", "inputs_hash": row.inputs_hash})
            elif t == "quote" and "quote" in include:
                q = ctx.db.get(Quote, uuid.UUID(item["quote_id"]))
                if q:
                    z.writestr(f"{part.number}_quote.json", json.dumps(q.result, ensure_ascii=False, indent=2))
                    manifest.append({"file": f"{part.number}_quote.json", "inputs_hash": q.inputs_hash})
        run: WorkflowRun = ctx.run
        head: dict[str, Any] = {"run_id": str(run.id), "part": part.number, "files": manifest}
        if exec_policy.is_turbo(ctx.policy):  # the download says it is not verified and which values were assumed
            from ..services.verification import assumption_refs

            assumed: list[dict] = []
            for item in ctx.inputs.get("items") or []:
                if isinstance(item, dict) and item.get("type") == "model":
                    assumed += assumption_refs(_revision(ctx, item))
            head["execution_policy"] = ctx.policy
            head["verification"] = exec_policy.verification(ctx.policy, "skipped_turbo", "not_requested", assumed)
        z.writestr("manifest.json", json.dumps(head, indent=2))
    if not manifest:
        raise NodeFailure("nothing_to_export", "no exportable items were connected")
    sha = save_blob(ctx.db, buf.getvalue(), "application/zip")
    f = File(project_id=part.project_id, part_id=part.id, kind="other", name=f"{part.number}_export.zip", created_by=ctx.user.id)
    ctx.db.add(f)
    ctx.db.flush()
    fv = FileVersion(file_id=f.id, version=1, blob_sha256=sha, original_name=f.name, note=f"workflow run {run.id}", created_by=ctx.user.id)
    ctx.db.add(fv)
    ctx.db.flush()
    assets.register_file_version(ctx.db, fv, node_run_id=ctx.node_run.id)
    _record(ctx, "bundle", "document", fv.id)
    return NodeOutcome(outputs={"bundle": {"type": "file", "file_version_id": str(fv.id), "name": f.name, "blob_sha256": sha},
                                **_turbo(ctx)})


# --- general mechanical engineering chain (W8, services/orchestration.py) ------------------------------------
# Project-level nodes (no part): text / files → spec → parts → assembly → drawings → delivery. The AI workspace
# (assembly_create) and the collaboration agent tools call the same service functions.


def _access(ctx: NodeContext):
    """The run starter's project access (permissions were checked for every node type when the run started)."""
    from ..security import project_access

    return project_access(ctx.db, ctx.user, ctx.project.id, "view")


def _owned(assumptions: list[dict], owner: dict[str, Any]) -> list[dict]:
    return [{**a, "owner": a.get("owner") or owner} for a in assumptions if isinstance(a, dict)]


def _sim(simulated: bool) -> dict[str, Any]:
    return {"simulated": True} if simulated else {}


def _continued_spec(ctx: NodeContext, *, design_input_sha256: str | None = None) -> Any:
    """S3: a retry of this node whose earlier attempt left a failed spec version that can be continued
    (recovery continue_extract) continues that version — the chunks already read are kept — instead of extracting
    the material again; a continuation that already exists and is usable is passed through. An interrupted design
    reuses its completed extraction only with the server checkpoint and the exact same input fingerprint.
    None: extract as usual."""
    from ..models import EngineeringSpec, EngineeringSpecVersion
    from ..services import engineering_spec as spec_svc
    from ..services import orchestration as orch

    prev = (ctx.node_run.outputs or {}).get("spec")
    if not isinstance(prev, dict) or not prev.get("spec_version_id"):
        return None
    design_pending = (ctx.node_run.outputs or {}).get("design_pending") or {}
    reuse_for_design = (design_input_sha256 is not None and design_pending.get("input_sha256") == design_input_sha256
                        and design_pending.get("spec_version_id") == prev["spec_version_id"]
                        and prev.get("status") in ("ready", "needs_answers"))
    if prev.get("status") != "failed" and not reuse_for_design:
        return None
    v = ctx.db.get(EngineeringSpecVersion, uuid.UUID(str(prev["spec_version_id"])))
    if v is None or v.project_id != ctx.project.id:
        return None
    spec = ctx.db.get(EngineeringSpec, v.spec_id)
    if reuse_for_design:
        v, spec = orch.load_spec_version(ctx.db, ctx.project.id, v.id, ctx.policy)
        return orch.SpecResult(version=v, spec=spec, created=False)
    nxt = spec_svc.continuation_of(ctx.db, v)
    if nxt is not None:
        return orch.SpecResult(version=nxt, spec=spec, created=False) if spec_svc.effective_status(nxt)[0] in ("ready", "needs_answers") \
            else None
    status, err = spec_svc.effective_status(v)
    if status != "failed" or (err or {}).get("recovery") != spec_svc.CONTINUE:
        return None
    cont = spec_svc.continue_extraction(ctx.db, _access(ctx), v)
    spec_svc.run_extraction(ctx.db, cont.id)
    return orch.SpecResult(version=cont, spec=spec, created=True)


def engineering_spec(ctx: NodeContext) -> NodeOutcome:
    """Text and / or files → spec version (extracted now with the run's policy and frozen planning model), or the exact
    version named by ``spec_version_id``. A failed extraction keeps the failed version (its stage, code, calls and
    recovery) and fails the node; a version with open questions (standard) is a product: the next step decides.

    Generation supplement A (services/attachments.py): an exact spec version together with new text / files is
    ``spec_and_sources`` (never one of them silently); a requested file that is missing / archived or was not read is
    ``source_unread``; before any CAD step a usable version is pre-checked (``spec_no_parts``,
    ``extraction_missed_parts``, ``spec_structure_incomplete``; Turbo hangs definitions that hang nowhere under the root
    in a derived version with one rule decision each, and that version is the node's spec). Supplement H: in Turbo, when
    some definitions hang under the root and others not, the run's planning model resolves the others (alias / member /
    not a part) in a derived version, its calls bound to this node run (services/spec_aliases.py)."""
    from ..services import attachments
    from ..services import orchestration as orch
    from ..services import spec_inputs

    p = ctx.params
    texts = [ctx.inputs["text"].get("text") or ""] if isinstance(ctx.inputs.get("text"), dict) else []
    texts.append(p.text)
    text = "\n\n".join(t.strip() for t in texts if t and t.strip())
    fvs = [f["file_version_id"] for f in ctx.inputs.get("files") or [] if isinstance(f, dict) and f.get("file_version_id")]
    fvs += [str(x) for x in p.file_version_ids]
    fvs = list(dict.fromkeys(str(f) for f in fvs))
    # Generation supplement I (A-04 / IN-08): the run was continued with replacement files (services/spec_inputs.py) —
    # the step makes a new version of the spec its failed attempt left (minus the unread files, plus the new ones), or a
    # new spec from the new file set when that attempt made none (with ``revise``: a new version of the referenced spec).
    # A-04 / IN-07 ``revise``: a new version of the given spec.
    over = spec_inputs.node_override(ctx.run, ctx.node_run.node_id)
    design_input_sha256 = hashlib.sha256(json.dumps({"params": p.model_dump(mode="json"), "inputs": ctx.inputs,
                                                     "override": over}, sort_keys=True, default=str).encode()).hexdigest()
    revise_from, added, dropped, reason = None, list(fvs), [], "revise"
    if over is not None:
        fvs = [str(f) for f in over.get("file_version_ids") or []]
        if over.get("base_spec_version_id"):
            revise_from, reason = over["base_spec_version_id"], "replace_source"
            added = [str(a.get("file_version_id")) for a in over.get("added") or [] if isinstance(a, dict)]
            dropped, text = [str(f) for f in over.get("dropped") or []], ""  # the text is a source of that version already
        elif p.spec_version_id and p.revise:
            # review fix: a revise whose attempt failed before any version existed (a new file archived / missing) keeps
            # the referenced version as its base — its sources, plus the new file set and the revision note
            revise_from, added, dropped = str(p.spec_version_id), list(fvs), [str(f) for f in over.get("dropped") or []]
    elif p.spec_version_id and p.revise and (text or fvs):
        revise_from = str(p.spec_version_id)
    if over is None and revise_from is None:
        try:
            known = attachments.check_spec_and_sources(ctx.db, spec_version_id=p.spec_version_id, file_version_ids=fvs, text=text,
                                                       instruction=False)
        except AppError as exc:
            return NodeOutcome(steps=ctx.steps, failure={"code": exc.code, "message": exc.message, "stage": "spec_input", "retryable": False,
                                                         "recovery": (exc.details or {}).get("recovery"), "details": exc.details or {}})
        if p.spec_version_id and known:
            fvs = []  # files the exact spec version already read: nothing new, the version is used as it is
    missing = attachments.file_problems(ctx.db, ctx.project.id, added if revise_from else fvs)
    if missing:
        f = attachments.failure("source_unread", "some requested files cannot be read: " + ", ".join(
            str(m.get("name") or m.get("file_version_id")) for m in missing), recovery="replace_source", sources=missing)
        return NodeOutcome(steps=ctx.steps, failure={**f, "stage": "spec_input"})
    for fv in fvs:
        _file_version(ctx, fv)  # same project check as every file input
    try:
        # a retry after a failed extraction continues that version (S3); a continue with files never follows such a failure
        # (spec_inputs.REPLACEABLE), so its first run extracts the new input and a later retry continues that extraction
        res = _continued_spec(ctx, design_input_sha256=design_input_sha256 if p.design_from_requirements else None)
        if res is None and revise_from is not None:
            res = orch.resolve_spec(ctx.db, access=_access(ctx), policy=ctx.policy, text=text, file_version_ids=added,
                                    spec_version_id=revise_from, revise=True, drop_file_version_ids=dropped, revise_reason=reason,
                                    title=p.title, model_ref=ai_svc.run_model_ref(ctx.run, "planning", p.model_config_id))
            ctx.step("engineering_spec_revise", reason=reason, of_version=(res.version.planning or {}).get("revision", {}).get("of_version_no"),
                     version=res.version.version_no, added=len(added), dropped=len(dropped), note=bool(text))
        elif res is None:
            res = orch.resolve_spec(ctx.db, access=_access(ctx), policy=ctx.policy, text=text, file_version_ids=fvs,
                                    spec_version_id=p.spec_version_id if not (text or fvs) else None, title=p.title,
                                    model_ref=ai_svc.run_model_ref(ctx.run, "planning", p.model_config_id))
    except AppError as exc:
        raise _fail_from(exc)
    v = res.version
    if res.created:
        orch.attach_spec_calls(ctx.db, v, node_run_id=ctx.node_run.id)
    if p.design_from_requirements and res.usable:
        from ..models import AiOperation
        from ..services import engineering_design

        op = ctx.db.scalar(select(AiOperation).where(AiOperation.run_id == ctx.run.id, AiOperation.kind == "assembly_create"))
        try:
            if op is None:
                raise AppError(422, "design_authorization_missing", "Design requires an original authorized workspace operation")
            v = engineering_design.synthesize(ctx.db, v, op=op, node_run_id=ctx.node_run.id,
                                              model_ref=ai_svc.run_model_ref(ctx.run, "planning", p.model_config_id))
        except AppError as exc:
            # Keep the extracted spec and every attempted design call; a normal failure outcome commits the ledger.
            return NodeOutcome(outputs={"spec": orch.spec_brief(ctx.db, res.version, res.spec),
                                        "design_pending": {"spec_version_id": str(res.version.id), "input_sha256": design_input_sha256}}, steps=ctx.steps,
                               failure={"code": exc.code, "message": exc.message, "stage": "engineering_design",
                                        "retryable": bool((exc.details or {}).get("retryable")),
                                        **({"recovery": exc.details["recovery"]} if exc.details.get("recovery") else {}),
                                        "details": exc.details or {}})
        res = orch.SpecResult(version=v, spec=res.spec, created=True)
        ctx.step("engineering_design", version=v.version_no, status="unconfirmed", assumptions=len((v.content or {}).get("assumptions") or []))
    check = attachments.precheck(ctx.db, v, requested_files=fvs, policy=ctx.policy, user_id=ctx.user.id, title=res.spec.title,
                                 model_ref=ai_svc.run_model_ref(ctx.run, "planning", p.model_config_id)) if res.usable else None
    if check is not None and check.call_ids:  # supplement H: the Turbo alias / structure calls count like every spec call
        orch.attach_spec_calls(ctx.db, v, node_run_id=ctx.node_run.id, ids=check.call_ids)
    if check is not None and check.aliases is not None:
        ctx.step("engineering_spec_aliases", spec=f"SP-{res.spec.number}", version=v.version_no, **check.aliases)
    if check is not None and check.derived is not None:
        ctx.step("engineering_spec_repair", spec=f"SP-{res.spec.number}", version=v.version_no, derived=check.derived.version_no,
                 rule=check.rule, definitions=check.repaired)
        v = check.derived
    brief = orch.spec_brief(ctx.db, v, res.spec)
    ctx.step("engineering_spec", spec=brief["code"], version=v.version_no, status=brief["status"], created=res.created,
             definitions=brief["counts"].get("definitions"), open_questions=len(brief["open_questions"]), simulated=bool(v.simulated),
             sources=dict(Counter(s["read_status"] for s in brief["sources"])))
    owner = {"type": "engineering_spec_version", "id": str(v.id), "version_no": v.version_no}
    outputs = {"spec": brief, **_sim(bool(v.simulated)), **_turbo(ctx, _owned(brief["assumptions"], owner))}
    if not res.usable:
        err = brief.get("error") or {}
        if err.get("code") == "no_readable_source":  # A-03: none of the bound sources could be read — say which, per source
            f = attachments.failure("source_unread", err.get("message") or "none of the sources could be read", recovery="replace_source",
                                    sources=[s for s in brief["sources"] if s["read_status"] in ("failed", "unread")], spec_version_id=str(v.id),
                                    **_sim(bool(v.simulated)))
            return NodeOutcome(outputs=outputs, steps=ctx.steps, failure=f)
        return NodeOutcome(outputs=outputs, steps=ctx.steps,
                           failure={"code": err.get("code") or "spec_failed", "message": err.get("message") or "the spec could not be extracted",
                                    # S3 failure info (design §3.1): continue_extract continues the failed spec version
                                    "stage": err.get("stage") or "spec", "retryable": bool(err.get("retryable")),
                                    "recovery": err.get("recovery") or "re_extract",
                                    "details": {"stage": err.get("stage"), "spec_version_id": str(v.id), "recovery": err.get("recovery"),
                                                "call_ids": err.get("call_ids") or [], **_sim(bool(v.simulated))}})
    if check is not None and check.failure is not None:
        f = check.failure
        return NodeOutcome(outputs=outputs, steps=ctx.steps, failure={**f, "details": {**f["details"], **_sim(bool(v.simulated))}})
    return NodeOutcome(outputs=outputs, steps=ctx.steps)


def _deadline(ctx: NodeContext) -> float:
    """Monotonic time at which this attempt's lease can no longer be extended (engine: started_at + node_max_runtime)."""
    import time
    from datetime import UTC, datetime

    from ..config import get_settings

    started = ctx.node_run.started_at or datetime.now(UTC)
    left = get_settings().node_max_runtime_seconds - (datetime.now(UTC) - started).total_seconds()
    return time.monotonic() + max(0.0, left)


def assembly_from_spec(ctx: NodeContext) -> NodeOutcome:
    """Spec version → every part definition built on its own (template, else AI modeling of a new part) → assembly
    revision + W4 checks. Built parts are products even when the assembly cannot be made: the node then fails with
    every definition's outcome (questions in standard, errors in Turbo) and no assembly (never a fake complete one).
    Generation supplement B: with something missing but something placeable, a partial assembly revision is the product
    and the node fails with it (``assembly_partial``; drawings and delivery still run, design §2.4)."""
    from ..services import orchestration as orch

    spec_in = ctx.inputs.get("spec")
    if not isinstance(spec_in, dict) or spec_in.get("type") != "engineering_spec" or not spec_in.get("spec_version_id"):
        raise NodeFailure("bad_input", "expected an engineering spec version")
    try:
        v, _ = orch.load_spec_version(ctx.db, ctx.project.id, spec_in["spec_version_id"], ctx.policy)
    except AppError as exc:
        raise _fail_from(exc)
    # S3: a retry of this node (a workspace "continue" too) reuses the definitions its earlier attempts built (same node run).
    try:
        build = orch.build_assembly(ctx.db, project_id=ctx.project.id, user=ctx.user, spec_version=v, policy=ctx.policy,
                                    model_ref=ai_svc.run_model_ref(ctx.run, "modeling", ctx.params.model_config_id),
                                    node_run_id=ctx.node_run.id, number=ctx.params.number, run_checks=ctx.params.run_checks,
                                    reuse_earlier_builds=ctx.params.reuse_earlier_builds,
                                    on_calls=lambda calls: ai_svc.record_calls(ctx.db, calls, ctx.node_run.id),
                                    keep_going=ctx.heartbeat, deadline=_deadline(ctx), cancelled=orch.run_cancel_check(ctx.run.id),
                                    checkpoint=ctx.checkpoint,  # J: each definition built is committed at once (engine docstring)
                                    concurrency=ctx.params.modeling_concurrency)
    except AppError as exc:
        raise _fail_from(exc) from exc
    assumed: list[dict] = []
    for o in build.definitions:
        ctx.step("definition", ok=o.status == "built", key=o.key, name=o.name, status=o.status, source=o.source,
                 part=o.part_number, revision=o.rev_no, error=(o.error or {}).get("code"), questions=len(o.questions), reused=o.reused)
        if o.revision_id:
            _record(ctx, "parts", "part_model", o.revision_id)
            assumed += _owned(o.assumptions, {"type": "model_revision", "id": o.revision_id, "rev_no": o.rev_no})
    brief = build.brief(ctx.db)
    if build.revision is not None:
        rev = build.revision
        # Outputs can survive a failed later attempt. Stamp only an output this attempt actually produced.
        if type(ctx.attempt) is int and ctx.attempt > 0 and isinstance(brief.get("coverage"), dict):
            brief["coverage_diagnostics"] = {"assembly_revision_id": str(rev.id), "spec_version_id": str(v.id),
                                              "node_run_id": str(ctx.node_run.id), "attempt": ctx.attempt}
        _record(ctx, "assembly", "assembly_model", rev.id)
        assumed += [{**a, "owner": {"type": "assembly_revision", "id": str(rev.id), "rev_no": rev.rev_no}} for a in rev.assumptions or []
                    if isinstance(a, dict)]
        ctx.step("assembly", number=brief.get("number"), revision=rev.rev_no, leaves=(rev.stats or {}).get("leaf_instances"),
                 check_status=rev.check_status, coverage=(build.coverage or {}).get("status"), reused=build.reused_revision)
        return NodeOutcome(outputs={"assembly": brief, **_sim(brief["simulated"]), **_turbo(ctx, assumed)}, steps=ctx.steps,
                           failure=build.failure)  # None when complete; assembly_partial keeps the partial revision
    return NodeOutcome(outputs={"assembly": brief, **_sim(brief["simulated"]), **_turbo(ctx, assumed)}, steps=ctx.steps,
                       failure=build.failure)


def _assembly_revision(ctx: NodeContext, value: Any):
    from ..models import Assembly, AssemblyRevision

    if not isinstance(value, dict) or value.get("type") != "assembly" or not value.get("assembly_revision_id"):
        raise NodeFailure("bad_input", "expected an assembly revision")
    rev = ctx.db.get(AssemblyRevision, uuid.UUID(str(value["assembly_revision_id"])))
    asm = ctx.db.get(Assembly, rev.assembly_id) if rev is not None else None
    if rev is None or asm is None or asm.project_id != ctx.project.id:
        raise NodeFailure("bad_input", "the assembly revision does not belong to this project")
    return rev


def assembly_drawing(ctx: NodeContext) -> NodeOutcome:
    """Assembly revision → W5 drawing package (guarded: capability_unavailable without W5). A partial package is kept
    and fails the node, so the delivery lists what is missing. The input runs after a failed assembly step
    (``Port.after_failure``): a partial assembly revision is drawn as such; without any revision the node fails with
    ``assembly_missing`` (generation supplement design §2.4)."""
    from ..services import orchestration as orch

    value = ctx.inputs.get("assembly")
    if not (isinstance(value, dict) and value.get("type") == "assembly" and value.get("assembly_revision_id")):
        ctx.step("assembly_drawing", ok=False, error="assembly_missing")
        return NodeOutcome(outputs={}, steps=ctx.steps, failure={
            "code": "assembly_missing", "message": "the assembly step made no assembly revision; there is nothing to draw",
            "stage": "assembly_drawing", "retryable": False, "recovery": "fix_spec",
            "details": {"upstream": (value.get("failure") or {}).get("code") if isinstance(value, dict) else None}})
    rev = _assembly_revision(ctx, value)
    if ctx.params.subassemblies:
        return _level_drawings(ctx, rev)
    try:
        pkg = orch.make_drawings(ctx.db, assembly_revision_id=rev.id, user=ctx.user, policy=ctx.policy, config=ctx.params.config,
                                 node_run_id=ctx.node_run.id)
    except AppError as exc:
        raise _fail_from(exc)
    brief = orch.drawing_brief(pkg)
    if "assembly_drawing" in assets.KIND_FIELD:  # W5 registers its asset type
        _record(ctx, "drawings", "assembly_drawing", pkg.id)
    ctx.step("assembly_drawing", revision=rev.rev_no, pages=brief["pages"], completeness=brief["completeness"])
    # the package's own assumption records (author rule / ai_turbo), not the verification references
    owned = _owned(list(getattr(pkg, "assumptions", None) or []), {"type": "assembly_drawing", "id": brief["package_id"]})
    outputs = {"drawings": brief, **_turbo(ctx, owned)}
    if brief["completeness"] != "complete":
        # S3: retrying this node continues the partial package (only its failed documents / sheets are made again)
        return NodeOutcome(outputs=outputs, steps=ctx.steps,
                           failure={"code": "drawing_package_partial", "message": "the drawing package is incomplete; the delivery lists what is missing",
                                    "stage": "assembly_drawing", "retryable": True, "recovery": "retry_sheet",
                                    "details": {"package_id": brief["package_id"]}})
    return NodeOutcome(outputs=outputs, steps=ctx.steps)


def _level_drawings(ctx: NodeContext, rev: Any) -> NodeOutcome:
    """Generation supplement E (``subassemblies`` on): the root package (GA + every part definition's detail sheet) and
    one package per distinct sub-assembly revision (``orchestration.make_level_drawings``). The ``drawings`` value keeps
    the root package's shape and adds ``subassembly_packages`` (a failed one with its ``error``). A sub-assembly (or the
    root) that could not be drawn keeps the others: the node fails with the products kept (``drawing_package_partial``,
    or the root's own code) and the delivery lists what is missing.

    Generation reliability R (design §5.3): each package made is committed at once (``ctx.checkpoint``, engine
    docstring); cancel, the lease and the run's time budget are checked before each package, and the value's ``units``
    lists kept / reused / not started / failed packages and the continue action. A step stopped before a package fails
    with what it kept (``drawing_package_partial``, or the stop code when the root was not started) and continuing reuses
    the complete packages as they are."""
    from ..models import AssemblyDrawing
    from ..services import orchestration as orch

    try:
        made_all = orch.make_level_drawings(ctx.db, assembly_revision_id=rev.id, user=ctx.user, policy=ctx.policy,
                                            config=ctx.params.config, node_run_id=ctx.node_run.id,
                                            checkpoint=ctx.checkpoint)  # R: each package made is committed at once
    except AppError as exc:
        raise _fail_from(exc)
    pkg, root_error, subs = made_all
    units = made_all.units()
    made = [s for s in subs if s.get("package_id")]
    if pkg is None and not made and made_all.stopped_by is None:  # nothing was drawn at all: as before, a plain failure
        raise NodeFailure(root_error["code"], root_error["message"], details=root_error.get("details") or {})  # type: ignore[index]
    registered = "assembly_drawing" in assets.KIND_FIELD  # W5 registers its asset type
    owned: list[dict] = []
    if pkg is not None:
        brief = orch.drawing_brief(pkg)
        owned += _owned(list(getattr(pkg, "assumptions", None) or []), {"type": "assembly_drawing", "id": brief["package_id"]})
        if registered:
            _record(ctx, "drawings", "assembly_drawing", pkg.id)
    else:
        brief = {"type": "drawing_package", "package_id": None, "completeness": made_all.root_unit["completeness"], "manifest_sha256": None,
                 "pages": 0, "files": 0, "error": {"code": root_error["code"], "message": root_error["message"]}}  # type: ignore[index]
    for s in made:
        row = ctx.db.get(AssemblyDrawing, uuid.UUID(s["package_id"]))
        if row is not None:
            owned += _owned(list(row.assumptions or []), {"type": "assembly_drawing", "id": s["package_id"]})
        if registered:
            _record(ctx, "drawings", "assembly_drawing", s["package_id"])
    brief["subassembly_packages"] = subs
    brief["units"] = units
    short = [s for s in subs if s.get("completeness") != "complete"]
    ctx.step("assembly_drawing", revision=rev.rev_no, pages=brief["pages"], completeness=brief["completeness"],
             subassemblies=len(subs), subassemblies_incomplete=len(short), reused=len(units["reused"]),
             not_started=len(units["not_started"]), stopped_by=units["stopped_by"])
    outputs = {"drawings": brief, **_turbo(ctx, owned)}
    stop = {"stopped_by": units["stopped_by"], "units": units} if units["stopped_by"] else {}
    if root_error is not None:
        # review fix: only a failure another attempt can fix offers "continue" (a refused revision / configuration does not)
        again = bool(root_error.get("retryable"))
        what = "was not started" if brief["completeness"] == "not_started" else "could not be made"
        return NodeOutcome(outputs=outputs, steps=ctx.steps, failure={
            "code": root_error["code"], "message": f"the root drawing package {what}: {root_error['message']}",
            "stage": "assembly_drawing", "retryable": again, "recovery": "retry_sheet" if again else "fix_spec",
            "details": {**(root_error.get("details") or {}), "subassembly_packages": len(made), **stop}})
    if brief["completeness"] != "complete" or short:
        again = brief["completeness"] != "complete" or any((s.get("error") or {}).get("retryable", True) for s in short)
        message = (f"the step stopped ({units['stopped_by']}) before {len(units['not_started'])} drawing package(s); the packages made are "
                   "kept and the delivery lists the rest as missing" if units["not_started"] else
                   "a drawing package is incomplete or could not be made; the delivery lists what is missing")
        return NodeOutcome(outputs=outputs, steps=ctx.steps, failure={
            "code": "drawing_package_partial", "message": message,
            "stage": "assembly_drawing", "retryable": again, "recovery": "retry_sheet" if again else "fix_spec",
            "details": {"package_id": brief["package_id"],
                        "subassemblies": [{k: s.get(k) for k in ("number", "rev_no", "package_id", "completeness", "error")} for s in short],
                        **stop}})
    return NodeOutcome(outputs=outputs, steps=ctx.steps)


def _levels_requested(ctx: NodeContext) -> bool:
    """The drawing step feeding this delivery is a layered one (``subassemblies`` on), read from the run's frozen graph:
    the delivery stays layered even when that step made no value at all."""
    g = ctx.run.graph_snapshot or {}
    src = next((e.get("source") for e in g.get("edges") or [] if e.get("target") == ctx.node_run.node_id and e.get("target_port") == "drawings"),
               None)
    node = next((n for n in g.get("nodes") or [] if n.get("id") == src), None) if src else None
    return bool(node and node.get("type") == "assembly_drawing" and (node.get("params") or {}).get("subassemblies"))


def _upstream(ctx: NodeContext) -> list[dict]:
    """Inputs whose upstream node did not succeed (the delivery records why something is missing)."""
    from sqlalchemy import select

    from ..models import NodeRun
    from ..services.failure_causes import public_error

    ports = {e["source"]: e["target_port"] for e in (ctx.run.graph_snapshot or {}).get("edges") or [] if e.get("target") == ctx.node_run.node_id}
    rows = ctx.db.scalars(select(NodeRun).where(NodeRun.run_id == ctx.run.id, NodeRun.node_id.in_(list(ports)))).all() if ports else []
    result = []
    for row in rows:
        if row.status == "succeeded":
            continue
        error = public_error(row.error)
        if error is not None:
            # Delivery needs bounded causes, not upstream request/response or other private diagnostic details.
            causes = error["details"].get("causes")
            error["details"] = {"causes": causes} if causes else {}
        result.append({"node_id": row.node_id, "node_type": row.node_type, "port": ports[row.node_id],
                       "status": row.status, "error": error})
    return result


def _current_coverage_projection(ctx: NodeContext, value: dict[str, Any]) -> dict[str, Any] | None:
    """Bind diagnostics to the exact assembly input and its persisted upstream attempt."""
    from sqlalchemy import select

    from ..models import NodeRun

    edges = [e for e in (ctx.run.graph_snapshot or {}).get("edges") or []
             if e.get("target") == ctx.node_run.node_id and e.get("target_port") == "assembly"]
    if len(edges) != 1:
        return None
    edge = edges[0]
    source = ctx.db.scalar(select(NodeRun).where(NodeRun.run_id == ctx.run.id, NodeRun.node_id == edge.get("source")))
    if source is None or type(source.attempt) is not int or source.attempt < 1:
        return None
    recorded = (source.outputs or {}).get(edge.get("source_port"))
    try:
        matches = json.dumps(recorded, sort_keys=True, allow_nan=False) == json.dumps(value, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        return None
    if not matches or not isinstance(value.get("coverage"), dict):
        return None
    binding = {"assembly_revision_id": value.get("assembly_revision_id"), "spec_version_id": value.get("spec_version_id"),
               "node_run_id": str(source.id), "attempt": source.attempt}
    tag = value.get("coverage_diagnostics")
    if not isinstance(tag, dict) or type(tag.get("attempt")) is not int or tag != binding:
        return None
    return {**binding, "coverage": value["coverage"]}


def delivery_package(ctx: NodeContext) -> NodeOutcome:
    """Assembly (or the parts built when it could not be) + drawing package → delivery manifest and zip. Runs even when
    an upstream step failed (Port.after_failure) and lists what is missing; complete only with every required file."""
    from ..services import orchestration as orch

    a = ctx.inputs.get("assembly") if isinstance(ctx.inputs.get("assembly"), dict) else None
    d = ctx.inputs.get("drawings") if isinstance(ctx.inputs.get("drawings"), dict) else None
    upstream = _upstream(ctx)
    rev_id = a.get("assembly_revision_id") if a and a.get("type") == "assembly" else None
    if rev_id:
        _assembly_revision(ctx, a)
    parts = [p.get("revision_id") for p in (a or {}).get("parts") or [] if isinstance(p, dict) and p.get("revision_id")]
    pkg, drawing_error = None, None
    if d is not None and d.get("type") == "drawing_package" and d.get("package_id"):
        pkg = orch.load_drawing_package(ctx.db, d["package_id"])
        if pkg is None:
            drawing_error = {"code": "drawing_package_unreadable", "message": "the drawing package of this run cannot be read in this build"}
    elif "drawings" in ctx.inputs:
        up = next((u for u in upstream if u["port"] == "drawings"), None)
        err = (up or {}).get("error") or {}
        drawing_error = {"code": err.get("code") or ("upstream_" + up["status"] if up else "drawings_missing"),
                         "message": err.get("message") or "the drawing step did not produce a package"}
    # generation supplement E: after a layered drawing step every level is delivered on its own (STEP and DWG)
    subs = d.get("subassembly_packages") if d is not None and isinstance(d.get("subassembly_packages"), list) else None
    if subs is None and _levels_requested(ctx):
        subs = []
    try:
        row = orch.deliver(ctx.db, project_id=ctx.project.id, user=ctx.user, policy=ctx.policy, assembly_revision_id=rev_id,
                           part_revision_ids=parts, drawing_package=pkg, drawing_error=drawing_error,
                           drawings_required="drawings" in ctx.inputs, spec_version_id=(a or {}).get("spec_version_id"),
                           upstream=upstream, node_run_id=ctx.node_run.id, bom_formats=ctx.params.bom_formats,
                           step_protocol=ctx.params.step_protocol,
                           coverage=(a or {}).get("coverage") if not rev_id else None,  # no assembly: the build's coverage
                           coverage_projection=_current_coverage_projection(ctx, a) if rev_id else None,
                           subassembly_packages=subs)
    except AppError as exc:
        raise _fail_from(exc)
    _record(ctx, "delivery", "delivery_manifest", row.id)
    brief = orch.delivery_brief(row)
    ctx.step("delivery", number=brief["number"], completeness=row.completeness, files=brief["files"], missing=len(brief["missing"]))
    outputs = {"delivery": brief, **_sim(row.simulated), **_turbo(ctx, list((brief.get("verification") or {}).get("assumptions") or []))}
    if row.completeness != "complete":
        return NodeOutcome(outputs=outputs, steps=ctx.steps,
                           failure={"code": "delivery_partial", "message": f"{len(brief['missing'])} required item(s) are missing; "
                                    "the delivery is partial", "details": {"delivery_id": str(row.id), "missing": brief["missing"]}})
    return NodeOutcome(outputs=outputs, steps=ctx.steps)


# --- collaboration ------------------------------------------------------------------


def concept_design(ctx: NodeContext) -> NodeOutcome:
    from ..services.workflow_design import concept_design as run_design

    return run_design(ctx)


def assembly_process_plan(ctx: NodeContext) -> NodeOutcome:
    from ..services.assembly_manufacturing import process_batch

    return process_batch(ctx)


def assembly_quote(ctx: NodeContext) -> NodeOutcome:
    from ..services.assembly_manufacturing import quote_batch

    return quote_batch(ctx)


def manufacturing_export(ctx: NodeContext) -> NodeOutcome:
    from ..services.assembly_manufacturing import export_package

    return export_package(ctx)


def business_task(ctx: NodeContext) -> NodeOutcome:
    """Prepare the node's Task (bind upstream formal outputs, register inputs,
    start it for an agent executor) and wait: the step completes only when the
    Task is accepted (services/agent_runtime.on_task_accepted). Agents run in
    the runtime queue, never here (design §0.2–0.3)."""
    from ..services import agent_runtime

    try:
        outputs = agent_runtime.prepare_node(ctx.db, ctx.run, ctx.node_run)
    except AppError as exc:
        raise _fail_from(exc)
    ctx.step("prepared", phase=outputs["task"]["phase"], bound_inputs=len(outputs["task"]["bound_inputs"]))
    return NodeOutcome(outputs=outputs, steps=ctx.steps, wait_human=True)


HANDLERS: dict[str, Handler] = {
    "text_input": text_input,
    "file_input": file_input,
    "model_input": model_input,
    "model_3d": model_3d,
    "drawing_reconstruct": drawing_reconstruct,
    "drawing": drawing,
    "drawing_intake": drawing_intake,
    "process_plan": process_plan,
    "quote": quote,
    "human_review": human_review,
    "export": export,
    "engineering_spec": engineering_spec,
    "concept_design": concept_design,
    "assembly_process_plan": assembly_process_plan,
    "assembly_quote": assembly_quote,
    "manufacturing_export": manufacturing_export,
    "assembly_from_spec": assembly_from_spec,
    "assembly_drawing": assembly_drawing,
    "delivery_package": delivery_package,
    "business_task": business_task,
}


def _with_check_ledger(handler: Handler) -> Handler:
    """Generation reliability N (design §2.8): the business checks a node attempt really ran, and the ones its policy
    closed, are kept on the node run as one ``checks_ledger`` step (also when the attempt fails), so a delivery counts
    them for its run from real records (services/verification.checks_report). The step goes where the engine takes the
    attempt's steps from: the outcome's when it has some, else the context's."""

    @functools.wraps(handler)
    def run(ctx: NodeContext) -> NodeOutcome:
        with checks_ledger.scope() as led:
            try:
                out = handler(ctx)
            except BaseException:
                ctx.steps.append(checks_ledger.step(led))
                raise
        (out.steps if out.steps else ctx.steps).append(checks_ledger.step(led))
        return out

    return run


HANDLERS = {node_type: _with_check_ledger(fn) for node_type, fn in HANDLERS.items()}
