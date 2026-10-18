"""Engineering specs (W2): sources and read state, spec versions, facts, questions and assumptions.

Thin router: permission → services.engineering_spec → dict. Reading needs project `view`; creating,
answering, re-extracting and continuing need `workflow` (viewers are read-only). Another project's spec is a 404.
Creating / answering / re-extracting / continuing return the new version in status ``extracting`` and run the
extraction as a background job (it records its own failure); poll the version for the result.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import EngineeringSpec, User
from ..security import current_user, project_access
from ..services import engineering_spec as svc
from ..services.feature_flags import require_choices
from ..services.engineering_spec import AnswersIn, ReextractIn, SpecCreateIn

router = APIRouter(tags=["engineering-specs"])


@router.post("/projects/{project_id}/engineering-specs", status_code=201)
def create_spec(project_id: uuid.UUID, body: SpecCreateIn, background: BackgroundTasks, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> dict:
    """Pasted text (no chat-size cap) and / or exact project file versions → a new spec, version 1 extracting."""
    acc = project_access(db, user, project_id, "workflow")
    require_choices(model_config_id=body.model_config_id, execution_policy=body.execution_policy)
    spec, v = svc.create_spec(db, acc, body)
    db.commit()
    background.add_task(svc.run_extraction_job, v.id)
    return {**svc.spec_out(db, spec, with_versions=True), "version": svc.version_out(db, v)}


@router.get("/projects/{project_id}/engineering-specs")
def list_specs(project_id: uuid.UUID, limit: int = 50, offset: int = 0, user: User = Depends(current_user),
               db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id)
    return svc.list_specs(db, project_id, limit=limit, offset=offset)


@router.get("/engineering-specs/{spec_id}")
def get_spec(spec_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    spec, _ = svc.load_spec(db, user, spec_id)
    return svc.spec_out(db, spec, with_versions=True)


@router.get("/engineering-spec-versions/{version_id}")
def get_version(version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    from ..services.verification import spec_verification

    v, _ = svc.load_version(db, user, version_id)
    spec = db.get(EngineeringSpec, v.spec_id)
    return {**svc.version_out(db, v), "verification": spec_verification(db, v),  # S5: with the person decisions
            "spec": {"id": str(v.spec_id), "code": f"SP-{spec.number}" if spec else "", "title": spec.title if spec else ""}}


@router.get("/engineering-spec-versions/{version_id}/chunks/{seq}")
def get_chunk(version_id: uuid.UUID, seq: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The exact text of one chunk as it was sent to the model (to show evidence in context)."""
    v, _ = svc.load_version(db, user, version_id)
    return svc.chunk_text(db, v, seq)


@router.get("/engineering-spec-versions/{version_id}/digest")
def get_digest(version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The text digest planners and the AI workspace receive for this version."""
    v, _ = svc.load_version(db, user, version_id)
    text, truncated = svc.spec_digest(v, db.get(EngineeringSpec, v.spec_id))
    return {"version_id": str(v.id), "text": text, "truncated": truncated}


@router.post("/engineering-spec-versions/{version_id}/answers", status_code=201)
def answer(version_id: uuid.UUID, body: AnswersIn, background: BackgroundTasks, user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> dict:
    """Answers bound to question ids → a new version (the answered version never changes)."""
    parent, acc = svc.load_version(db, user, version_id, "workflow")
    v = svc.answer_questions(db, acc, parent, body)
    db.commit()
    background.add_task(svc.run_extraction_job, v.id)
    return svc.version_out(db, v)


@router.post("/engineering-spec-versions/{version_id}/reextract", status_code=201)
def reextract(version_id: uuid.UUID, body: ReextractIn, background: BackgroundTasks, user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> dict:
    """Read the same pinned sources again into a new version (retry, or another policy / model)."""
    parent, acc = svc.load_version(db, user, version_id, "workflow")
    require_choices(model_config_id=body.model_config_id, execution_policy=body.execution_policy)
    v = svc.reextract(db, acc, parent, body)
    db.commit()
    background.add_task(svc.run_extraction_job, v.id)
    return svc.version_out(db, v)


@router.post("/engineering-spec-versions/{version_id}/continue", status_code=201)
def continue_extraction(version_id: uuid.UUID, background: BackgroundTasks, user: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    """Continue a failed extraction (S3, recovery ``continue_extract``): a new version reusing the chunks already read and
    reading only the rest. 409 spec_not_failed / spec_extracting / not_continuable / already_continued."""
    parent, acc = svc.load_version(db, user, version_id, "workflow")
    v = svc.continue_extraction(db, acc, parent)
    db.commit()
    background.add_task(svc.run_extraction_job, v.id)
    return svc.version_out(db, v)


