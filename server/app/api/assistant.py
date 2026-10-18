"""Unified AI workspace API: conversations, references, messages and operations.

Contract: .trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §4–§6 (backend A). Thin
routes over services/assistant.py; JSON shapes mirror web/src/api/assistant.ts.
Conversations are private: anyone but the owner (who must still see the
project) gets 404.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai import limits
from ..ai.design_contracts import DesignChoiceIn
from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import ai_drafts
from ..services import assistant as svc
from ..services import orchestrator
from ..services.assistant_refs import AiRefInput
from ..services.tasks import AssetType, RuleSpec

router = APIRouter(tags=["assistant"])

OperationIn = Literal["auto", "task_draft", "model_create", "model_modify", "drawing_intake", "ask", "drawing_generate",
                      "drawing_reconstruct", "workflow_edit", "assembly_create", "assembly_modify", "workflow_create",
                      "spec_extract", "drafting_create", "drafting_modify"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ConversationIn(_Body):
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    title: str = Field(default="", max_length=200)
    autonomy: Literal["auto", "confirm_task", "step"] | None = None  # AI native; None = the person's last choice


class ConstraintIn(_Body):
    id: str | None = Field(default=None, max_length=40)
    text: str = Field(min_length=1, max_length=500)
    source_seq: int | None = Field(default=None, ge=1)
    created_at: str | None = None  # echoed by clients; the server keeps its own


class ConversationPatch(_Body):
    title: str | None = Field(default=None, max_length=200)
    task_id: uuid.UUID | None = None
    archived: bool | None = None
    constraints: list[ConstraintIn] | None = Field(default=None, max_length=svc.MAX_CONSTRAINTS)
    # AI native: what the main agent asks before a write tool (auto: nothing; confirm_task: before creating a task; step: every write).
    autonomy: Literal["auto", "confirm_task", "step"] | None = None
    expected_version: int | None = None


class DraftCriterionIn(_Body):
    description: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)
    required: bool = True
    allowed_types: list[AssetType] = Field(default_factory=list, max_length=6)
    rule_spec: RuleSpec | None = None


class TaskDraftPatch(_Body):
    expected_version: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    objective: str | None = Field(default=None, max_length=limits.OBJECTIVE_CHARS)
    criteria: list[DraftCriterionIn] | None = Field(default=None, max_length=20)


class UndoIn(_Body):
    expected_head: uuid.UUID | None = None


class ConfirmIn(_Body):
    accept: bool


class AddReferenceIn(_Body):
    ref: AiRefInput
    as_target: bool = False


class TargetIn(_Body):
    key: str | None = Field(default=None, max_length=500)


class SendIn(_Body):
    client_request_id: uuid.UUID
    text: str = Field(min_length=1, max_length=limits.MATERIAL_TEXT_CHARS)
    operation: OperationIn = "auto"
    # Omitted = the composer's target / every composer reference / the conversation's task; null = none.
    target_key: str | None = Field(default=None, max_length=500)
    reference_keys: list[str] | None = Field(default=None, max_length=svc.MAX_REFERENCES)
    model_config_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    # W7 / S5: frozen on the operation and its run. None = not chosen: inherited from the version the request continues
    # from (its target / referenced spec version), else the project default, else standard; operation "auto": standard.
    execution_policy: Literal["standard", "turbo"] | None = None
    # S2: a text over the chat limit is saved as a project material (a file project members see) only with this consent
    # (the composer sends it when the person sends with the notice shown); else 422 material_consent_required.
    as_material: bool = False
    # Generation supplement A-01: false = the person set the references (the composer resolved the attachment wording
    # while typing); the server adds nothing. True (default): a file the text clearly points at is bound, or 422.
    auto_references: bool = True
    # Generation supplement I (A-04 / IN-07): the choice next to a referenced spec version with new files / change wording
    # (otherwise 422 spec_and_sources). true = a new version of that spec with the new material (the text as a revision
    # note); false = build from the exact version, the new material dropped visibly. None = not chosen.
    revise: bool | None = None
    # False builds new part definitions, without reusing models built by earlier requests.
    reuse_earlier_builds: bool | None = None


class PreflightIn(_Body):
    """Generation reliability K (design §2.3): the body of a send without its client_request_id."""

    text: str = Field(min_length=1, max_length=limits.MATERIAL_TEXT_CHARS)
    operation: OperationIn = "auto"
    target_key: str | None = Field(default=None, max_length=500)
    reference_keys: list[str] | None = Field(default=None, max_length=svc.MAX_REFERENCES)
    model_config_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    execution_policy: Literal["standard", "turbo"] | None = None
    as_material: bool = False
    auto_references: bool = True
    revise: bool | None = None
    reuse_earlier_builds: bool | None = None


class ContinueIn(_Body):
    # Generation supplement I (A-04 / IN-08): continue a spec step that could not read a source with replacement /
    # additional project files (the unread ones are dropped); the same run, a new input snapshot. None: continue as is.
    file_version_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)


class ResolveAttachmentsIn(_Body):
    text: str = Field(min_length=1, max_length=limits.MATERIAL_TEXT_CHARS)
    # Omitted = the composer's target / every composer reference (as a send).
    target_key: str | None = Field(default=None, max_length=500)
    reference_keys: list[str] | None = Field(default=None, max_length=svc.MAX_REFERENCES)
    # The operation the composer would send ("auto" when not chosen): only a request that builds from files binds one.
    operation: str = Field(default="auto", max_length=40)


class ForkIn(_Body):
    operation_id: uuid.UUID | None = None
    reference_keys: list[str] | None = Field(default=None, max_length=svc.MAX_REFERENCES)
    title: str | None = Field(default=None, max_length=200)


class AnswerIn(_Body):
    answers: list[str] = Field(min_length=1, max_length=20)


class RetryIn(_Body):
    client_request_id: uuid.UUID


class ApplyIn(_Body):
    # The workflow version the client shows; the plan's base version is always checked as well.
    expected_version: int | None = Field(default=None, ge=1)
    expected_head: uuid.UUID | None = None
    plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # W8 assembly_modify: a plan that removes instances or mates is applied only with this explicit confirmation.
    confirm_removals: bool = False


class ConversationRunIn(_Body):
    workflow_id: uuid.UUID
    client_request_id: uuid.UUID
    # None = not chosen: as POST /workflows/{id}/runs (the project default, else standard).
    execution_policy: Literal["standard", "turbo"] | None = None


class RepinIn(_Body):
    workflow_id: uuid.UUID


def _given(body: BaseModel, name: str, value):
    return value if name in body.model_fields_set else svc.UNSET


@router.get("/ai/capabilities")
def capabilities(project_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Which operations the caller can start in this project now (no_permission / project_inactive / ai_unavailable)."""
    return svc.capabilities(db, user, project_id)


@router.get("/ai/conversations")
def list_conversations(
    project_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    archived: Literal["false", "true", "all"] = "false",
    limit: int = 50,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    out = svc.list_conversations(db, user, project_id=project_id, task_id=task_id, archived=archived, limit=limit)
    db.commit()  # status sync of unfinished operations
    return out


@router.post("/ai/conversations", status_code=201)
def create_conversation(body: ConversationIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = svc.create_conversation(db, user, project_id=body.project_id, task_id=body.task_id, title=body.title, autonomy=body.autonomy)
    db.commit()
    return svc.detail_out(db, conv)


@router.get("/ai/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, after_seq: int | None = None, user: User = Depends(current_user),
                     db: Session = Depends(get_db)) -> dict:
    """Messages (all, or only seq > after_seq), every operation (synced first) and the composer."""
    conv, _ = svc.load_conversation(db, user, conversation_id)
    svc.sync_conversation(db, conv)
    db.commit()
    return svc.detail_out(db, conv, after_seq=after_seq)


@router.patch("/ai/conversations/{conversation_id}")
def patch_conversation(conversation_id: uuid.UUID, body: ConversationPatch, user: User = Depends(current_user),
                       db: Session = Depends(get_db)) -> dict:
    constraints = None if body.constraints is None else [c.model_dump(exclude={"created_at"}) for c in body.constraints]
    conv = svc.patch_conversation(
        db, user, conversation_id,
        title=_given(body, "title", body.title),
        task_id=_given(body, "task_id", body.task_id),
        archived=_given(body, "archived", body.archived),
        constraints=_given(body, "constraints", constraints),
        autonomy=_given(body, "autonomy", body.autonomy),
        expected_version=body.expected_version,
    )
    db.commit()
    return svc.detail_out(db, conv)


@router.post("/ai/conversations/{conversation_id}/references")
def add_reference(conversation_id: uuid.UUID, body: AddReferenceIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Normalise + validate + dedupe. Without a target, an operable object is preselected (notice target_preselected)."""
    conv, added, notice = svc.add_reference(db, user, conversation_id, body.ref, as_target=body.as_target)
    db.commit()
    return {"composer": svc.composer_out(conv, db=db), "added": added, "notice": notice}


@router.delete("/ai/conversations/{conversation_id}/references/{key:path}")
def remove_reference(conversation_id: uuid.UUID, key: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = svc.remove_reference(db, user, conversation_id, key)
    db.commit()
    return svc.composer_out(conv, db=db)


@router.put("/ai/conversations/{conversation_id}/target")
def set_target(conversation_id: uuid.UUID, body: TargetIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = svc.set_target(db, user, conversation_id, body.key)
    db.commit()
    return svc.composer_out(conv, db=db)


@router.post("/ai/conversations/{conversation_id}/composer/repin")
def repin_composer(conversation_id: uuid.UUID, body: RepinIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """AI work panel: the composer's references to this workflow (and its nodes) follow its current version."""
    conv = svc.repin_composer(db, user, conversation_id, body.workflow_id)
    db.commit()
    return svc.composer_out(conv, db=db)


@router.post("/ai/conversations/{conversation_id}/workflow-runs", status_code=201)
def start_conversation_run(conversation_id: uuid.UUID, body: ConversationRunIn, response: Response, user: User = Depends(current_user),
                           db: Session = Depends(get_db)) -> dict:
    """AI work panel: start a run of a part workflow from the conversation; the run and a `card` message are written
    together. 201 new; 200 when the client_request_id was already used (the same message and run; nothing starts)."""
    out, created = svc.start_conversation_run(db, user, conversation_id, workflow_id=body.workflow_id,
                                              client_request_id=body.client_request_id, execution_policy=body.execution_policy)
    response.status_code = 201 if created else 200
    return out


@router.post("/ai/conversations/{conversation_id}/messages", status_code=201)
def send_message(conversation_id: uuid.UUID, body: SendIn, response: Response, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict:
    """Send a request (design §4.2). 201 new; 200 when the client_request_id was already sent
    (the stored result; nothing runs again). A business failure is a failed operation, not an HTTP error."""
    out, created = svc.send(
        db, user, conversation_id,
        client_request_id=body.client_request_id,
        text=body.text,
        operation=body.operation,
        target_key=_given(body, "target_key", body.target_key),
        reference_keys=_given(body, "reference_keys", body.reference_keys or []),
        model_config_id=body.model_config_id,
        task_id=_given(body, "task_id", body.task_id),
        execution_policy=body.execution_policy,
        as_material=body.as_material,
        auto_references=body.auto_references,
        revise=body.revise,
        reuse_earlier_builds=body.reuse_earlier_builds,
    )
    response.status_code = 201 if created else 200
    return out


@router.post("/ai/conversations/{conversation_id}/preflight")
def preflight(conversation_id: uuid.UUID, body: PreflightIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Generation reliability K (design §2.3): what a send of this request would do — operation and its source, the
    object it works on, references (automatic ones included), what is missing (the same checks as a send, before any
    model call), the policy and whether a model would be called. Read only: no model call, nothing written."""
    out = svc.preflight(
        db, user, conversation_id,
        text=body.text,
        operation=body.operation,
        target_key=_given(body, "target_key", body.target_key),
        reference_keys=_given(body, "reference_keys", body.reference_keys or []),
        model_config_id=body.model_config_id,
        task_id=_given(body, "task_id", body.task_id),
        execution_policy=body.execution_policy,
        as_material=body.as_material,
        auto_references=body.auto_references,
        revise=body.revise,
    )
    db.rollback()  # nothing is written; drop anything a lookup may have touched
    return out


@router.post("/ai/conversations/{conversation_id}/attachments/resolve")
def resolve_attachments(conversation_id: uuid.UUID, body: ResolveAttachmentsIn, user: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    """Generation supplement A-01 (design §2.5): which file a send of this text would bind now — ``status``
    not_needed | unique | ambiguous | required, ``candidates`` as references the composer can add. Read only; same
    access as reading the conversation."""
    return svc.resolve_attachments(db, user, conversation_id, text=body.text, target_key=_given(body, "target_key", body.target_key),
                                   reference_keys=_given(body, "reference_keys", body.reference_keys or []), operation=body.operation)


@router.post("/ai/conversations/{conversation_id}/fork", status_code=201)
def fork_conversation(conversation_id: uuid.UUID, body: ForkIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = svc.fork(db, user, conversation_id, operation_id=body.operation_id, reference_keys=body.reference_keys, title=body.title)
    db.commit()
    return svc.detail_out(db, conv)


@router.get("/ai/conversations/{conversation_id}/context")
def conversation_context(conversation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    from ..services import assistant_context

    conv, _ = svc.load_conversation(db, user, conversation_id)
    return assistant_context.context_out(db, conv)


@router.get("/ai/operations/{operation_id}")
def get_operation(operation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    op, _, _ = svc.load_operation(db, user, operation_id)
    op = svc.sync_operation(db, op)
    db.commit()
    return svc.operation_out(db, op, full_build=True)


@router.post("/ai/operations/{operation_id}/answer")
def answer_operation(operation_id: uuid.UUID, body: AnswerIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    op = svc.answer(db, user, operation_id, [a.strip() for a in body.answers])
    return svc.operation_out(db, op, full_build=True)


@router.post("/ai/operations/{operation_id}/cancel")
def cancel_operation(operation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Stop the operation's run; AI native: a main-agent turn stops before its next step, with its active sub-agents."""
    op = svc.cancel(db, user, operation_id)
    return svc.operation_out(db, op, full_build=True)


# --- AI native: the main agent (services/orchestrator.py, .trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §2.5) ---


@router.post("/ai/operations/{operation_id}/confirm")
def confirm_operation(operation_id: uuid.UUID, body: ConfirmIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Decide the step a main-agent turn waits on (``question.kind = confirm``): accept runs it now and the main agent
    continues in a new turn; decline closes it (no model call). 409 not_waiting; a repeated decision returns the turn."""
    op = orchestrator.confirm(db, user, operation_id, body.accept)
    return svc.operation_out(db, op, full_build=True)


@router.post('/ai/operations/{operation_id}/design-choice')
def design_choice_operation(operation_id: uuid.UUID, body: DesignChoiceIn, user: User = Depends(current_user),
                            db: Session = Depends(get_db)) -> dict:
    from ..services import design_plans
    op, choice = design_plans.respond(db, user, operation_id, body)
    out = svc.operation_out(db, op, full_build=True)
    # The root may display a newer design; acknowledge this exact immutable request.
    out['result']['design_plan']['last_response'] = design_plans.receipt(db, choice)
    return out


@router.post("/ai/operations/{operation_id}/steps/{index}/undo")
def undo_step(operation_id: uuid.UUID, index: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Undo one step of a main-agent turn (draft discarded, part archived, task cancelled, sub-agent stopped) with the
    permission of that reverse action; 409 not_undoable."""
    op = orchestrator.undo(db, user, operation_id, index)
    return svc.operation_out(db, op, full_build=True)


@router.patch("/ai/task-drafts/{draft_id}")
def patch_task_draft(draft_id: uuid.UUID, body: TaskDraftPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Edit an open task draft of one of your conversations (409 version_conflict / draft_closed)."""
    criteria = None if body.criteria is None else [c.model_dump(mode="json") for c in body.criteria]
    d = ai_drafts.patch(db, user, draft_id, expected_version=body.expected_version, title=_given(body, "title", body.title),
                        objective=_given(body, "objective", body.objective), criteria=_given(body, "criteria", criteria))
    db.commit()
    return ai_drafts.draft_out(d)


@router.post("/ai/task-drafts/{draft_id}/discard")
def discard_task_draft(draft_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Discard an open task draft (a discarded one returns as it is; a used one is 409 draft_closed)."""
    d = ai_drafts.discard(db, user, draft_id)
    db.commit()
    return ai_drafts.draft_out(d)


@router.post("/ai/operations/{operation_id}/retry", status_code=201)
def retry_operation(operation_id: uuid.UUID, body: RetryIn, response: Response, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    out, created = svc.retry(db, user, operation_id, body.client_request_id)
    response.status_code = 201 if created else 200
    return out


@router.post("/ai/operations/{operation_id}/continue")
def continue_operation(operation_id: uuid.UUID, body: ContinueIn | None = None, user: User = Depends(current_user),
                       db: Session = Depends(get_db)) -> dict:
    """S3: continue a failed AI assembly request from its failed step (recovery continue_extract / retry_definition /
    retry_sheet) in the same run; what is already done is kept. 409 not_continuable / already_retried.

    Generation supplement I (IN-08): with ``file_version_ids`` a spec step that could not read a source (recovery
    replace_source / fix_input) runs again in the same run with those files (the unread ones dropped; a new input
    snapshot on the run and the operation, the old one kept). The same files again: the operation as it is while it
    runs, 409 input_unchanged once it failed with them."""
    files = [str(f) for f in body.file_version_ids] if body is not None and body.file_version_ids is not None else None
    op = svc.continue_operation(db, user, operation_id, file_version_ids=files)
    return svc.operation_out(db, op, full_build=True)


@router.post("/ai/operations/{operation_id}/apply")
def apply_operation(operation_id: uuid.UUID, body: ApplyIn | None = None, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    """Apply a workflow_edit plan: the planned graph becomes the part workflow's next version (only the next run
    uses it). 409 workflow_changed when the workflow moved past the plan's base version.

    W8 assembly_modify: the planned edit ops become a new assembly revision against the plan's base (409
    revision_conflict when the assembly moved on; 409 removals_need_confirmation without ``confirm_removals``).

    AI work panel workflow_create: the planned part workflow is created (a workflow card follows in the conversation and
    the workflow becomes the composer's target); 409 already_applied."""
    from .workflows import _wf_out

    found, _, _ = svc.load_operation(db, user, operation_id)
    if found.kind == "drafting_modify":
        from ..services.assistant_cad_tools import apply_drafting
        op = apply_drafting(db, user, operation_id, plan_hash=body.plan_hash if body else None,
                           expected_head=body.expected_head if body else None, confirm_removals=bool(body and body.confirm_removals))
        db.commit()
        return {"operation": svc.operation_out(db, op, full_build=True)}
    if found.kind == "workflow_create":
        op, w, acc = svc.apply_workflow_create(db, user, operation_id, plan_hash=body.plan_hash if body else None)
        db.commit()
        return {"operation": svc.operation_out(db, op, full_build=True), "workflow": _wf_out(db, w, acc)}
    if found.kind == "assembly_modify":
        from ..services.assembly_ai import apply_assembly_modify
        from ..services.assemblies import revision_out
        from ..models import AssemblyRevision

        op = apply_assembly_modify(db, user, operation_id, confirm_removals=bool(body and body.confirm_removals),
                                   plan_hash=body.plan_hash if body else None, expected_head=body.expected_head if body else None)
        db.commit()
        rev = db.get(AssemblyRevision, uuid.UUID(op.result["application"]["revision_id"]))
        return {"operation": svc.operation_out(db, op, full_build=True), "revision": revision_out(db, rev) if rev else None}
    op, w, acc = svc.apply_workflow_edit(db, user, operation_id, expected_version=body.expected_version if body else None,
                                         plan_hash=body.plan_hash if body else None)
    db.commit()
    return {"operation": svc.operation_out(db, op, full_build=True), "workflow": _wf_out(db, w, acc)}


@router.post("/ai/operations/{operation_id}/undo")
def undo_operation(operation_id: uuid.UUID, body: UndoIn | None = None, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    """Undo an applied workflow_edit plan: its base graph becomes the workflow's next version, while the workflow is
    still at the version the plan produced."""
    from .workflows import _wf_out

    from ..errors import AppError
    from ..services import cad_results

    found, conv, _ = svc.load_operation(db, user, operation_id)
    svc.require_open(conv)
    if (found.result or {}).get("cad_result"):
        if body is None or body.expected_head is None:
            raise AppError(422, "expected_head_required", "CAD undo needs the exact saved head version")
        op = cad_results.undo(db, user, found, body.expected_head)
        svc._touch_conversations(db, {op.conversation_id})
        db.commit()
        return {"operation": svc.operation_out(db, op, full_build=True)}
    op, w, acc = svc.undo_workflow_edit(db, user, operation_id)
    db.commit()
    return {"operation": svc.operation_out(db, op, full_build=True), "workflow": _wf_out(db, w, acc)}
