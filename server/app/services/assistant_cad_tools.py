"""Operation-owned adapters for fixed CAD planners and human plan application."""
from __future__ import annotations

import copy
import json
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import retry as call_retry
from ..ai.timeouts import SPEC_EXTRACTION_S
from ..ai.cad_tool_contracts import DraftingEditInput, DrawingConfigInput, ValidatedDrawingEditPlan, drawing_state_hash, edit_plan_hash
from ..ai.providers.base import ProviderError
from ..db import new_session
from ..domain.hashing import sha256_hex
from ..errors import AppError, conflict, not_found
from ..models import (AiContextBuild, AiConversation, AiOperation, DrawingDocument, DrawingDocumentVersion,
                      EngineeringSpec, EngineeringSpecVersion, ModelRevision, Part, Project, User)
from ..security import project_access
from . import ai as ai_svc, assistant as asst, assistant_context as actx, operation_events
from .assistant_tool_inputs import extraction_text, source_text
from .lifecycle import require_active

PLAN_KINDS = ("workflow_create", "workflow_edit", "assembly_modify", "drafting_modify")


def plan_digest(kind: str, result: dict) -> str:
    if kind == "drafting_modify":
        return edit_plan_hash(ValidatedDrawingEditPlan.model_validate(result["plan"]))
    keys = {"workflow_create": ("part_id", "name", "template_key", "graph_after"),
            "workflow_edit": ("workflow_id", "base_version", "graph_before", "graph_after", "diff"),
            "assembly_modify": ("assembly_id", "base_revision_id", "ops", "removals")}[kind]
    return sha256_hex({"kind": kind, **{key: result.get(key) for key in keys}})


def stamp_plan(op: AiOperation) -> None:
    # Historical explicit-operation clients keep their compatibility protocol during rollout.
    if op.kind in PLAN_KINDS and op.status == "succeeded" and ((op.request or {}).get("parent_op_id") or op.kind == "drafting_modify"):
        result = dict(op.result or {})
        if not result.get("plan_hash"):
            result["plan_hash"] = plan_digest(op.kind, result)
            op.result = result


def check_plan(op: AiOperation, *, plan_hash: str | None, expected_head: uuid.UUID | None = None,
               expected_version: int | None = None) -> None:
    result = op.result or {}
    stored = result.get("plan_hash")
    if not stored:
        return  # Plans created before the new tool protocol remain readable/applicable.
    if plan_hash != stored:
        raise conflict("plan_hash_mismatch", "reload the exact plan and confirm its current content")
    if plan_digest(op.kind, result) != stored:
        raise conflict("plan_changed", "the stored executable plan changed; ask for a new plan")
    if op.kind in ("assembly_modify", "drafting_modify"):
        base = result.get("base_revision_id") or result.get("base_version_id")
        if str(expected_head) != str(base):
            raise conflict("head_changed", "the application must name the plan's exact base version")
    if op.kind == "workflow_edit" and expected_version != result.get("base_version"):
        raise conflict("workflow_changed", "the application must name the plan's exact numeric base version")


def stop_guard(op: AiOperation, *, timeout_s: int | None = 180):
    """No locks are retained across model I/O; recheck cancellation and permission before every call/save."""
    oid, generation, uid, pid = op.id, op.generation, op.user_id, op.project_id
    parent_id = asst._uuid((op.request or {}).get("parent_op_id"))
    root_id = asst._uuid((op.request or {}).get("root_op_id"))
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None

    def stop():
        if deadline is not None and time.monotonic() >= deadline:
            raise ProviderError("budget_exhausted", "the CAD planning time budget was exhausted")
        with new_session() as fresh:
            current = fresh.get(AiOperation, oid)
            if current is None or current.generation != generation:
                raise ProviderError("lease_lost", "the operation generation changed")
            if current.status not in ("pending", "running"):
                raise ProviderError("cancelled", "the operation is no longer active")
            conv = fresh.get(AiConversation, current.conversation_id)
            if conv is None or conv.archived_at is not None:
                raise ProviderError("cancelled", "the conversation was archived")
            for ident in {parent_id, root_id} - {None, oid}:
                ancestor = fresh.get(AiOperation, ident)
                if ancestor is None or ancestor.status == "cancelled":
                    raise ProviderError("cancelled", "the parent request was stopped")
            actor = fresh.get(User, uid)
            try:
                project_access(fresh, actor, pid, asst.OP_ACTION[current.kind])
                require_active(fresh, pid, what="running a CAD operation")
            except AppError as exc:
                raise ProviderError("permission_denied", exc.message, retryable=False) from exc
    return stop


def bind_stop(provider, stop):
    previous = provider._guard
    def guarded():
        stop()
        if previous is not None:
            previous()
    provider.bind_guard(guarded)


def _prepare(db: Session, user: User, op: AiOperation, *, purpose: str, payload: dict):
    from .assembly_ai import _record_manifest

    project_access(db, user, op.project_id, asst.OP_ACTION[op.kind])
    require_active(db, op.project_id, what="running a CAD operation")
    model_ref = ai_svc.bundle_model_ref((op.request or {}).get("model_snapshots"), purpose,
                                      legacy_snapshot=(op.request or {}).get("model_snapshot") if purpose == asst.OP_PURPOSE[op.kind] else None)
    items = [actx._item("required", "request", "CAD planner input", json.dumps(payload, ensure_ascii=False))]
    _record_manifest(db, op, items, purpose=purpose, model_ref=model_ref)
    build = db.scalar(select(AiContextBuild).where(AiContextBuild.operation_id == op.id).order_by(AiContextBuild.created_at.desc()).limit(1))
    if build and int(build.budget.get("used_est") or 0) > int(build.budget.get("available") or 0):
        build.ok, build.error = False, {"code": "context_over_budget", "message": "the exact CAD planning context does not fit"}
        db.commit()
        raise AppError(422, "context_over_budget", "the exact CAD planning context does not fit")
    op = asst._lock_op(db, op.id)
    if op.status != "pending":
        raise conflict("operation_stopped", "this operation has already started or stopped")
    op.status = "running"
    db.commit()
    stop = stop_guard(op)
    stop()
    provider, _ = ai_svc.provider_for(db, purpose, model_ref)
    bind_stop(provider, stop)
    db.commit()
    return provider, stop


def _record_outcome(db: Session, op: AiOperation, outcome):
    # A model call remains accounted for even when cancellation prevents any domain save.
    ai_svc.record_calls(db, list(outcome.calls), None, ai_operation_id=op.id)
    db.commit()


def _save_lock(db: Session, user: User, op: AiOperation, *, document_id: uuid.UUID | None = None) -> AiOperation:
    db.scalar(select(Project).where(Project.id == op.project_id).with_for_update(read=True).execution_options(populate_existing=True))
    project_access(db, user, op.project_id, asst.OP_ACTION[op.kind])
    require_active(db, op.project_id, what="saving a CAD operation")
    if op.part_id is not None:
        part = db.scalar(select(Part).where(Part.id == op.part_id).with_for_update().execution_options(populate_existing=True))
        if part is None or part.project_id != op.project_id or part.archived_at is not None:
            raise not_found("part")
    if document_id is not None:
        doc = db.scalar(select(DrawingDocument).where(DrawingDocument.id == document_id).with_for_update().execution_options(populate_existing=True))
        if doc is None or doc.project_id != op.project_id or doc.archived_at is not None:
            raise not_found("drawing document")
    op = asst._lock_op(db, op.id)
    asst.require_open(asst.lock_conversation(db, op.conversation_id))
    return op


def _saved_drawing(db: Session, op: AiOperation, version: DrawingDocumentVersion) -> dict:
    from . import drawing_documents, assets

    asset = assets.version_for(db, "drawing_document", version.id)
    brief = drawing_documents.version_brief(version)
    ref = asst.refs.normalize_ref(db, db.get(User, op.user_id), op.project_id,
                                 asst.refs.AiRefInput(kind="drawing_document_version", object_id=version.id))
    item = _result_item(op, ref, "drawing_document")
    if asset is not None:
        item.update(asset_version_id=str(asset.id), asset_id=str(asset.asset_id), availability=asset.availability,
                    publish_state=asset.publish_state, labels=list(asset.labels or []), check_status=version.check_status)
        if asset.availability == "ready" and asst._can(asst._access_of(db, op), "workflow"):
            item["actions"] += ["use_as_input", "add_deliverable"]
    return {"document_id": str(version.document_id), "drawing_document_version_id": str(version.id),
            "version_no": version.version_no, "asset_version_id": str(asset.id) if asset else None,
            "drawing_document": brief, "check_status": version.check_status, "output_status": version.output_status,
            "items": [item]}


def _result_item(op: AiOperation, ref: dict, asset_type: str) -> dict:
    """The same navigable, exact-version item envelope consumed by the shared result card."""
    return {"ref": ref, "asset_type": asset_type, "asset_id": None, "asset_version_id": None,
            "label": ref.get("version_label") or ref.get("title") or "", "base": (op.request or {}).get("target"),
            "run_id": str(op.run_id) if op.run_id else None, "node_id": None, "output_key": None,
            "availability": None, "publish_state": None, "labels": ["simulated"] if op.simulated else [],
            "actions": ["view", "continue", "fork"]}


def run_drafting(db: Session, user: User, op: AiOperation) -> None:
    from ..ai.drafting_edit_agent import run_drafting_edit
    from . import drafting_plans, drawing_documents

    req, version = op.request or {}, None
    text = str(req.get("text") or "")
    constraints = [str(c["text"]) for c in req.get("constraints") or [] if c.get("text")]
    if constraints:
        text += "\nPinned user constraints:\n" + "\n".join(constraints)
    if op.kind == "drafting_modify":
        target = req.get("target") or {}
        normalized = asst.refs.normalize_ref(db, user, op.project_id, asst.refs.AiRefInput.model_validate(target))
        version = db.get(DrawingDocumentVersion, uuid.UUID(normalized["object_id"]))
        assert version is not None
        state = drawing_documents.state_of(version)
        payload = DraftingEditInput(document_id=version.document_id, base_version_id=version.id, base_version_no=version.version_no,
                                   state=state, state_sha256=drawing_state_hash(state), selection=normalized.get("entities"),
                                   text=text, execution_policy=op.execution_policy)
        provider, stop = _prepare(db, user, op, purpose="assistant", payload=payload.model_dump(mode="json"))
        outcome = run_drafting_edit(provider, payload, stop=stop)
    else:
        title = str((req.get("tool_input") or {}).get("title") or "Drawing")
        provider, stop = _prepare(db, user, op, purpose="assistant", payload={"title": title, "text": text})
        outcome = drafting_plans.prepare_text_drawing(provider, text, title=title, execution_policy=op.execution_policy,
                    model_snapshot=asst._model_ref(req), stop=stop)
    _record_outcome(db, op, outcome)
    try:
        stop()
    except (ProviderError, AppError):
        current = db.get(AiOperation, op.id, populate_existing=True)
        if current is not None and current.status not in ("pending", "running"):
            return
        raise
    op = _save_lock(db, user, op, document_id=version.document_id if version else None)
    if op.status != "running":
        db.commit()
        return
    op.simulated = bool(outcome.simulated)
    if outcome.error:
        op.status, op.error = "failed", outcome.error
        op.result = {"kind": op.kind, "questions": outcome.questions, "repairs": outcome.repairs}
    elif op.kind == "drafting_modify":
        plan = outcome.plan
        assert plan is not None
        op.result = {"kind": op.kind, "plan": plan.model_dump(mode="json"), "summary": plan.summary,
                     "base_version_id": str(plan.base_version_id), "document_id": str(plan.document_id),
                     "changes": plan.changes, "warnings": plan.warnings, "questions": plan.questions,
                     "removals": plan.removed_entity_ids, "plan_hash": edit_plan_hash(plan), "application": None}
        op.status = "succeeded"
    else:
        if outcome.prepared is None:
            raise conflict("drafting_not_ready", "no validated drawing was prepared")
        saved = drafting_plans.save_text_document(db, user=user, project_id=op.project_id, title=title,
                                                 prepared=outcome.prepared, execution_policy=op.execution_policy)
        op.result = {"kind": op.kind, **_saved_drawing(db, op, saved)}
        op.status = "failed" if saved.check_status == "failed" else "succeeded"
        if op.status == "failed":
            op.error = {"code": "drawing_check_failed", "message": "the drawing was saved with failed checks"}
    asst._on_terminal(db, op)
    db.commit()


def plan_drawing_config(db: Session, user: User, op: AiOperation) -> dict:
    from ..ai.drawing_config_agent import run_drawing_config, PROMPT_VERSION

    req = op.request or {}
    target = req.get("target") or {}
    normalized = asst.refs.normalize_ref(db, user, op.project_id, asst.refs.AiRefInput.model_validate(target))
    revision = db.get(ModelRevision, uuid.UUID(normalized["object_id"]))
    if revision is None or normalized.get("kind") != "part_revision":
        raise conflict("revision_required", "drawing configuration requires an exact model revision")
    payload = DrawingConfigInput(revision_id=revision.id, revision_facts={"rev_no": revision.rev_no, "status": revision.status,
                                "checks": revision.checks}, requirements=req["tool_input"]["requirements"], execution_policy=op.execution_policy)
    provider, stop = _prepare(db, user, op, purpose="assistant", payload=payload.model_dump(mode="json"))
    outcome = run_drawing_config(provider, payload, stop=stop)
    _record_outcome(db, op, outcome)
    stop()
    op = _save_lock(db, user, op)
    if op.status != "running":
        raise conflict("operation_stopped", "the drawing configuration operation was stopped")
    op.simulated = bool(op.simulated or outcome.simulated)
    snapshot = ai_svc.bundle_model_ref(req.get("model_snapshots"), "assistant")
    config = outcome.config.model_dump(mode="json", exclude={"title", "units"}) if outcome.config else None
    op.result = {**(op.result or {}), "drawing_config_plan": {
        "requirements": payload.requirements, "revision_id": str(revision.id), "config": config,
        "coverage": copy.deepcopy(outcome.coverage), "questions": list(outcome.questions), "unsupported": list(outcome.unsupported),
        "validation": {"status": "passed" if config is not None and not outcome.error else "failed", "error": outcome.error},
        "provenance": {"prompt_version": PROMPT_VERSION, "simulated": outcome.simulated,
                       "model": {k: snapshot.get(k) for k in ("config_id", "config_version", "provider", "model_id")} if isinstance(snapshot, dict) else None}}}
    db.commit()
    if outcome.error or outcome.config is None:
        err = outcome.error or {"code": "drawing_config_invalid", "message": "no valid drawing configuration"}
        raise AppError(422, err["code"], err["message"], {"questions": outcome.questions, "unsupported": outcome.unsupported})
    return config


def sync_spec_operation(db: Session, op: AiOperation, *, defer: set[uuid.UUID] | None = None) -> bool:
    """Project the exact saved extraction under the operation lock; its heartbeat owns liveness.

    Return False only when no version has been allocated. Never lock the spec after the operation:
    extraction may be saving its call ledger while holding the spec row.
    """
    from . import orchestration, engineering_spec

    result = op.result or {}
    if not result.get("spec_version_id"):
        return False
    if op.status not in asst.ACTIVE:
        return True
    try:
        stop_guard(op, timeout_s=None)()
        version_id = asst._uuid(result.get("spec_version_id"))
        version = db.get(EngineeringSpecVersion, version_id, populate_existing=True) if version_id else None
        spec = db.get(EngineeringSpec, version.spec_id, populate_existing=True) if version is not None else None
        if (version is None or spec is None or version.project_id != op.project_id or spec.project_id != op.project_id
                or asst._uuid(result.get("spec_id")) != version.spec_id):
            raise not_found("engineering spec version")
        user = db.get(User, op.user_id)
        ref = asst.refs.normalize_ref(db, user, op.project_id,
            asst.refs.AiRefInput(kind="engineering_spec_version", object_id=version.id))
        status, error = engineering_spec.effective_status(version)
        if status == "extracting":
            op.status = "running"
            return True
        brief = orchestration.spec_brief(db, version, spec)
        op.simulated = bool(version.simulated)
        op.result = {**result, "spec": brief, "sources": brief.get("sources"), "readiness": brief.get("readiness"),
                     "open_questions": brief.get("open_questions"), "items": [_result_item(op, ref, "engineering_spec")]}
        op.status = "failed" if status == "failed" else "succeeded"
        op.error = copy.deepcopy(error) if op.status == "failed" else None
    except ProviderError as exc:
        op.status, op.error = "failed", {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
    except AppError as exc:
        op.status, op.error = "failed", {"code": exc.code, "message": exc.message, "details": exc.details or {}}
    op.question = None
    asst._on_terminal(db, op, defer=defer)
    return True


def run_spec(db: Session, user: User, op: AiOperation) -> None:
    from . import orchestration, engineering_spec, assembly_ai

    req = op.request or {}
    text = source_text(req)
    access = project_access(db, user, op.project_id, "design")
    require_active(db, op.project_id, what="extracting a specification")
    src = assembly_ai._sources(db, op)
    model_ref = asst._model_ref(req)
    generation = op.generation
    op = asst._lock_op(db, op.id)
    if op.status != "pending" or op.generation != generation:
        return
    op.status = "running"
    items = [actx._item("required", "request", "Specification request", text)]
    items += [actx._item("required", "reference", f"file_version:{fid}", "", ref_key=f"file_version:{fid}", read="metadata",
                         ranges=["read by specification extraction; exact source/chunk manifest retained"]) for fid in src.files]
    assembly_ai._record_manifest(db, op, items, purpose="planning", model_ref=model_ref)
    authorized_design = None
    if (req.get('tool_input') or {}).get('design_ref'):
        from .design_plans import resolve_authorized_design
        authorized_design = resolve_authorized_design(db, user, req['tool_input']['design_ref'],
            conversation_id=op.conversation_id, root_operation_id=uuid.UUID(req['root_op_id']), tool_kind='spec_extract')
    resolved = orchestration.resolve_spec(db, access=access, policy=op.execution_policy, text=extraction_text(req),
                file_version_ids=src.files, spec_version_id=src.spec_version, title=(req.get("tool_input") or {}).get("title") or "",
                model_ref=model_ref, revise=src.revise, extract=False, **({'authorized_design': authorized_design} if authorized_design else {}))
    version = resolved.version
    op.result = {"kind": "spec_extract", "spec_version_id": str(version.id), "spec_id": str(version.spec_id),
                 "created": resolved.created}
    operation_id, version_id = op.id, version.id
    db.commit()  # Preserve the allocated exact version before any extraction call; never duplicate on replay.
    local_deadline = time.monotonic() + SPEC_EXTRACTION_S
    live_stop = stop_guard(op, timeout_s=None)
    if resolved.created and authorized_design is None:
        with call_retry.deadline_scope(local_deadline, check=live_stop) as call_limits:
            def stop() -> None:
                reason = call_limits.stop_reason(calls=False)
                remaining = call_limits.remaining_s()
                # The request reserve is not a reason to discard a completed reply still inside its deadline.
                if reason is not None and (reason.kind in ("cancelled", "lease_lost") or remaining is not None and remaining <= 0):
                    raise reason.error()

            engineering_spec.run_extraction(db, version_id, commit=db.commit, stop=stop, ai_operation_id=operation_id)
    orchestration.attach_spec_calls(db, version, node_run_id=None, ai_operation_id=operation_id)
    db.commit()
    op = asst._lock_op(db, operation_id)
    if (op.status != "running" or op.generation != generation
            or asst._uuid((op.result or {}).get("spec_version_id")) != version_id):
        db.commit()
        return
    sync_spec_operation(db, op)
    db.commit()


def apply_drafting(db: Session, user: User, operation_id: uuid.UUID, *, plan_hash: str | None,
                   expected_head: uuid.UUID | None, confirm_removals: bool) -> AiOperation:
    from . import drafting_plans

    op, conv, _ = asst.load_operation(db, user, operation_id)
    asst.require_open(conv)
    if op.kind != "drafting_modify":
        raise conflict("plan_not_ready", "this operation has no drawing edit plan")
    document_id = asst._uuid((op.result or {}).get("document_id"))
    op = _save_lock(db, user, op, document_id=document_id)
    check_plan(op, plan_hash=plan_hash, expected_head=expected_head)
    result = copy.deepcopy(op.result or {})
    if (result.get("application") or {}).get("state") == "applied":
        return op
    if op.status != "succeeded" or not result.get("plan"):
        raise conflict("plan_not_ready", "this operation has no validated drawing edit plan")
    plan = ValidatedDrawingEditPlan.model_validate(result["plan"])
    if plan.removed_entity_ids and not confirm_removals:
        raise conflict("removals_need_confirmation", "confirm the planned entity removals before applying", removals=plan.removed_entity_ids)
    with db.begin_nested():
        version = drafting_plans.apply_edit_plan(db, user=user, project_id=op.project_id, plan=plan,
                      execution_policy=op.execution_policy, note=f"AI change: {plan.summary}"[:2000])
        result.update(_saved_drawing(db, op, version))
        result["application"] = {"state": "applied", "by": str(user.id), "at": asst._now().isoformat(), "plan_hash": plan_hash,
                                 "expected_head": str(expected_head), "version_id": str(version.id), "removals_confirmed": confirm_removals}
        op.result = result
        from . import cad_results
        cad_results.record(db, op)
        operation_events.emit(db, op, phase="applied")
        db.flush()
    return op
