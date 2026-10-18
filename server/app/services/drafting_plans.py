"""Pure preparation and transactional saves for main-agent standalone drawings.

The operation owner freezes providers, records every returned call, authorizes
human application and saves its receipt/event atomically with these domain rows.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from ..ai import drafting_agent
from ..ai.cad_tool_contracts import (DraftingEditInput, DraftingEditReply, PreparedTextDrawing, TextDrawingOutcome,
                                     ValidatedDrawingEditPlan, drawing_state_hash, edit_plan_hash)
from ..ai.drafting_edit_agent import validate_edit_plan
from ..ai.providers.base import ModelProvider
from ..cad import drafting
from ..errors import AppError, conflict, not_found
from ..models import DrawingDocument, DrawingDocumentVersion, User
from ..security import project_access
from . import drawing_documents, lifecycle


def prepare_text_drawing(provider: ModelProvider, text: str, *, title: str, execution_policy: str,
                         model_snapshot: dict[str, Any], stop: Callable[[], None], timeout_s: int = 180,
                         max_repairs: int = 2) -> TextDrawingOutcome:
    """Exactly one existing generation/repair sequence. No DB, default lookup or save."""
    text = drawing_documents._request_text(text)
    policy = drawing_documents._parse_policy(execution_policy)
    generated = drafting_agent.run_drafting(provider, text, policy=policy, title=title, stop=stop,
                                            timeout_s=timeout_s, max_repairs=max_repairs)
    out = TextDrawingOutcome(calls=list(generated.calls), simulated=generated.simulated, repairs=generated.repairs,
                             questions=list(generated.questions), error=generated.error)
    if generated.error or generated.state is None:
        return out
    source = {'id': 'S-1', 'kind': 'text', 'name': 'request', 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
              'chars': len(text), 'text': text}
    provenance = {'simulated': generated.simulated, 'model_id': provider.model_id, 'model_config_id': provider.config_id,
        'model_snapshot': model_snapshot, 'prompt_version': drafting_agent.DRAFTING_PROMPT_VERSION,
        'summary': generated.summary, 'repairs': generated.repairs, 'calls': len(generated.calls),
        'questions': generated.questions}
    out.prepared = PreparedTextDrawing(state=generated.state, text=text, source=source, ai=provenance,
        labels=['simulated'] if generated.simulated else [], execution_policy=policy)
    return out


def save_text_document(db: Session, *, user: User, project_id: UUID, title: str,
                       prepared: PreparedTextDrawing, execution_policy: str) -> DrawingDocumentVersion:
    """Save the prepared state without another model call. Flush, never commit."""
    project_access(db, user, project_id, 'design')
    lifecycle.require_active(db, project_id, what='saving a drawing')
    return drawing_documents.create_document(db, project_id=project_id, user=user, title=title, mode='text',
        text=prepared.text, execution_policy=execution_policy, prepared_text=prepared)


def apply_edit_plan(db: Session, *, user: User, project_id: UUID, plan: ValidatedDrawingEditPlan,
                    execution_policy: str, note: str = '') -> DrawingDocumentVersion:
    """Reparse/recheck an authorized plan and save against its exact base, never latest.

    The caller holds project/object/operation locks and has checked the displayed
    hash/head and human removal receipt. This adapter supplies no confirmation.
    """
    doc = db.get(DrawingDocument, plan.document_id)
    if doc is None or doc.project_id != project_id:
        raise not_found('drawing document')
    project_access(db, user, project_id, 'design')
    lifecycle.require_active(db, project_id, what='applying a drawing edit')
    doc = drawing_documents._lock(db, doc.id)
    head = drawing_documents._head(db, doc, plan.base_version_id)
    state = drawing_documents.state_of(head)
    if head.version_no != plan.base_version_no or drawing_state_hash(state) != plan.state_sha256:
        raise conflict('plan_changed', 'the exact drawing base does not match this plan')
    request = DraftingEditInput(document_id=doc.id, base_version_id=head.id, base_version_no=head.version_no,
        state=state, state_sha256=plan.state_sha256, selection=plan.selection, text='Apply the stored validated plan',
        execution_policy=execution_policy)
    try:
        checked = validate_edit_plan(request, DraftingEditReply(summary=plan.summary, commands=plan.commands, questions=plan.questions))
    except drafting.DraftingError as exc:
        raise AppError(422, exc.code, exc.message, exc.details) from exc
    if edit_plan_hash(checked) != edit_plan_hash(plan) or checked.affected_entity_ids != plan.affected_entity_ids:
        raise conflict('plan_changed', 'the stored drawing commands or effects do not match the validated plan')
    return drawing_documents.apply_edits(db, document_id=doc.id, user=user, base_version_id=head.id,
        commands=checked.commands, execution_policy=execution_policy, note=note, actor_kind='ai',
        ai={'kind': 'drafting_modify', 'base_version_id': str(head.id), 'plan_hash': edit_plan_hash(checked)})
