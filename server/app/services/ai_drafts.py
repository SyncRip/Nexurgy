"""Task drafts the main agent stores (AI native, .trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §1.8, §2.5).

A draft is private to its conversation, like the conversation itself: only the conversation's owner (who must still see
the project) reads, edits or discards it; anyone else gets 404. It is editable while ``open`` (``expected_version``,
409 ``version_conflict`` / ``draft_closed``) and becomes a task once: :func:`create_task` writes the task, its criteria
and the part link through ``services/tasks.py`` (same permissions and guards as the task pages) and marks the draft
``used``; a used draft returns its task again instead of creating a second one.

Criteria keep the shape ``stages.draft_task`` returns: ``{description, required, allowed_types, rule_spec}``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError, conflict, not_found
from ..models import ASSET_TYPES, AiConversation, AiTaskDraft, Task, User
from ..security import ProjectAccess
from . import assistant as asst
from . import feature_flags as features

#: Criteria a draft keeps (stages.draft_task keeps 20).
MAX_CRITERIA = 20
UNSET = asst.UNSET  # "field omitted" (as opposed to an explicit null), as in the conversation patch


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def draft_out(d: AiTaskDraft) -> dict:
    return {"id": str(d.id), "conversation_id": str(d.conversation_id), "operation_id": str(d.operation_id) if d.operation_id else None,
            "step_index": d.step_index, "part_id": str(d.part_id) if d.part_id else None, "title": d.title, "objective": d.objective,
            "criteria": list(d.criteria or []),
            "suggested_workflow_id": str(d.suggested_workflow_id) if d.suggested_workflow_id else None, "status": d.status,
            "task_id": str(d.task_id) if d.task_id else None, "simulated": d.simulated, "version": d.version,
            "created_at": _iso(d.created_at), "updated_at": _iso(d.updated_at)}


def normalize_criteria(items: list[dict]) -> list[dict]:
    """Criteria as a draft keeps them: a description, required (default true), allowed asset types (``allowed_types``,
    or the agent's ``asset_types``; unknown types are 422) and the rule (given, else derived from the types)."""
    from .stages import _criterion
    from .tasks import RuleSpec

    if len(items) > MAX_CRITERIA:
        raise AppError(422, "too_many_criteria", f"a draft keeps at most {MAX_CRITERIA} criteria", {"max": MAX_CRITERIA})
    out = []
    for c in items:
        desc = str(c.get("description") or "").strip()
        if not desc:
            raise AppError(422, "criterion_empty", "a criterion needs a description")
        types = list(c.get("allowed_types") if c.get("allowed_types") is not None else c.get("asset_types") or [])
        bad = [t for t in types if t not in ASSET_TYPES]
        if bad:
            raise AppError(422, "unknown_asset_type", "unknown asset type", {"types": bad})
        item = _criterion(desc, types[:6], bool(c.get("required", True)))
        if c.get("rule_spec") is not None:
            item["rule_spec"] = RuleSpec.model_validate(c["rule_spec"]).model_dump(mode="json")
        out.append(item)
    return out


def store_of(db: Session, operation_id: uuid.UUID, step_index: int) -> AiTaskDraft | None:
    """The draft a turn step stored, if any (a replayed step finds it instead of drafting again)."""
    return db.scalar(select(AiTaskDraft).where(AiTaskDraft.operation_id == operation_id, AiTaskDraft.step_index == step_index))


def store(db: Session, conv: AiConversation, *, operation_id: uuid.UUID, step_index: int, part_id: uuid.UUID | None,
          draft: dict) -> AiTaskDraft:
    """The draft of one turn step (``stages.draft_task``'s result); the step's existing draft when it was stored before."""
    found = store_of(db, operation_id, step_index)
    if found is not None:
        return found
    features.require_enabled("tasks")
    d = AiTaskDraft(conversation_id=conv.id, project_id=conv.project_id, user_id=conv.user_id, operation_id=operation_id,
                    step_index=step_index, part_id=part_id, title=str(draft.get("title") or "")[:300],
                    objective=str(draft.get("objective") or ""), criteria=list(draft.get("criteria") or []),
                    suggested_workflow_id=uuid.UUID(draft["suggested_workflow_id"]) if draft.get("suggested_workflow_id") else None,
                    simulated=bool(draft.get("simulated")))
    db.add(d)
    db.flush()
    return d


def lock(db: Session, draft_id: uuid.UUID) -> AiTaskDraft:
    d = db.scalar(select(AiTaskDraft).where(AiTaskDraft.id == draft_id).with_for_update().execution_options(populate_existing=True))
    if d is None:
        raise not_found("task draft")
    return d


def load(db: Session, user: User, draft_id: uuid.UUID) -> AiTaskDraft:
    """The conversation owner's draft (404 for anyone else, as for the conversation)."""
    d = db.get(AiTaskDraft, draft_id)
    if d is None:
        raise not_found("task draft")
    try:
        asst.load_conversation(db, user, d.conversation_id)
    except AppError:
        raise not_found("task draft")
    return d


def edit(d: AiTaskDraft, *, title: Any = UNSET, objective: Any = UNSET, criteria: Any = UNSET) -> AiTaskDraft:
    """Change an open draft (locked by the caller); 409 draft_closed once used or discarded."""
    features.require_enabled("tasks")
    if d.status != "open":
        raise conflict("draft_closed", f"the draft is {d.status}; it can no longer change", status=d.status)
    changed = False
    if title is not UNSET and title is not None and title.strip() and title.strip()[:300] != d.title:
        d.title, changed = title.strip()[:300], True
    if objective is not UNSET and objective is not None and objective.strip() != d.objective:
        d.objective, changed = objective.strip(), True
    if criteria is not UNSET and criteria is not None:
        new = normalize_criteria(criteria)
        if new != list(d.criteria or []):
            d.criteria, changed = new, True
    if changed:
        d.version += 1
    return d


def patch(db: Session, user: User, draft_id: uuid.UUID, *, expected_version: int, title: Any = UNSET, objective: Any = UNSET,
          criteria: Any = UNSET) -> AiTaskDraft:
    """PATCH /ai/task-drafts/{id}. Flushes; the caller commits."""
    load(db, user, draft_id)
    d = lock(db, draft_id)
    if d.version != expected_version:
        raise conflict("version_conflict", "the draft was changed elsewhere; reload first", current_version=d.version)
    edit(d, title=title, objective=objective, criteria=criteria)
    db.flush()
    return d


def discard(db: Session, user: User | None, draft_id: uuid.UUID) -> AiTaskDraft:
    """POST /ai/task-drafts/{id}/discard (and the undo of a draft step, ``user`` None: the caller checked the owner). A
    discarded draft stays discarded (replay); a used one is 409 draft_closed. Flushes; the caller commits."""
    if user is not None:
        load(db, user, draft_id)
    features.require_enabled("tasks")
    d = lock(db, draft_id)
    if d.status == "used":
        raise conflict("draft_closed", "the draft was already turned into a task; cancel the task instead", task_id=str(d.task_id))
    if d.status == "open":
        d.status, d.version = "discarded", d.version + 1
    db.flush()
    return d


def create_task(db: Session, acc: ProjectAccess, draft_id: uuid.UUID, *, part_id: uuid.UUID | None) -> tuple[Task, bool]:
    """(task, created): the draft's task with its criteria and part (``part_id``, else the draft's) through the task service;
    a used draft returns its task (created False). 409 draft_closed for a discarded draft. The draft row is locked first, so
    two confirmations of one draft make one task (nothing else locks a draft together with a project or task)."""
    from . import tasks as task_svc

    features.require_enabled("tasks")
    d = lock(db, draft_id)
    if d.status == "used" and d.task_id is not None and db.get(Task, d.task_id) is not None:
        return db.get(Task, d.task_id), False  # type: ignore[return-value]
    if d.status != "open":
        raise conflict("draft_closed", f"the draft is {d.status}; it can no longer become a task", status=d.status)
    pid = part_id or d.part_id
    t = task_svc.create_task(db, acc, {"title": d.title, "objective": d.objective, "part_id": pid}, [])
    for c in d.criteria or []:
        task_svc.add_criterion(db, acc, t.id, {"description": c["description"], "required": bool(c.get("required", True)),
                                               "allowed_types": list(c.get("allowed_types") or []), "rule_spec": c.get("rule_spec")})
    d.status, d.task_id, d.part_id, d.version = "used", t.id, pid, d.version + 1
    db.flush()
    return t, True
