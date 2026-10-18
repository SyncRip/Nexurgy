"""Execution policy source and the "confirm these assumptions" entry (S5, docs/agent/stability-fixes.zh-CN.md §S5).

- ``GET /projects/{id}/execution-policy``: what a request would be frozen with (explicit > inherited from the source
  version > project default > standard), with the number of AI / rule assumptions upstream that a standard takeover
  needs a person to confirm (members read, viewers included). ``source_type=part``: the part's latest revision (what a
  modeling change of that part continues from); ``source_type=run``: the run a rerun repeats.
- ``GET /projects/{id}/upstream-assumptions``: every AI / rule assumption a product rests on, in one list (its own and
  those of its spec version, assembly revisions and part revisions), each with owner, basis and state.
- ``POST /projects/{id}/upstream-assumptions/confirm``: a person confirms them in one go (design permission); one
  append-only record per value with that person as the confirmer, never the AI. Corrections stay on each artifact.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user, project_access
from ..services import execution_policy as policy_svc
from ..services import verification

router = APIRouter(tags=["execution-policy"])

SourceType = Literal["engineering_spec_version", "assembly_revision", "model_revision"]


class ConfirmUpstreamIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: SourceType
    source_id: uuid.UUID
    # The listed keys only (``<owner type>:<owner id>#<assumption id>``); omitted: every unconfirmed one.
    keys: list[str] | None = Field(default=None, max_length=2000)
    note: str = Field(default="", max_length=1000)


@router.get("/projects/{project_id}/execution-policy")
def execution_policy(project_id: uuid.UUID, source_type: SourceType | Literal["part", "run"] | None = None, source_id: uuid.UUID | None = None,
                     explicit: Literal["standard", "turbo"] | None = None, user: User = Depends(current_user),
                     db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id)
    return policy_svc.preview(db, project_id, explicit=explicit, source_type=source_type, source_id=source_id)


@router.get("/projects/{project_id}/upstream-assumptions")
def upstream_assumptions(project_id: uuid.UUID, source_type: SourceType, source_id: uuid.UUID, user: User = Depends(current_user),
                         db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id)
    return verification.upstream_assumptions(db, project_id=project_id, source_type=source_type, source_id=source_id)


@router.post("/projects/{project_id}/upstream-assumptions/confirm")
def confirm_upstream_assumptions(project_id: uuid.UUID, body: ConfirmUpstreamIn, user: User = Depends(current_user),
                                 db: Session = Depends(get_db)) -> dict:
    project_access(db, user, project_id, "design")
    out = verification.confirm_upstream(db, user, project_id=project_id, source_type=body.source_type, source_id=body.source_id,
                                        keys=body.keys, note=body.note)
    db.commit()
    return out
