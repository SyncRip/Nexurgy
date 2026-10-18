"""Read-only evidence tools of a review attempt: the database side (execution-layer design §9).

``scope_for`` freezes what one review may read — the pending submission's items, the node's bound inputs, the criteria
and the submission's frozen automatic checks — and :func:`run_evidence_tool` serves one call of the registry's
evidence tools (``list_submission``, ``read_document``, ``read_checks``, ``compare_versions``) in its own short read
transaction. The tool logic and the scope check live in ``ai/evidence.py`` (pure, shared with the offline sample
runner); this module only turns a version of the scope into plain facts. Versions outside the scope are never loaded:
the scope check runs before any read. Nothing here writes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from ..ai import evidence
from ..ai.providers.base import clip
from ..db import session_scope
from ..models import Asset, AssetVersion, FileVersion, ModelRevision
from . import assets

log = logging.getLogger("partroute.review_evidence")

#: Characters of a document read into the facts (a review reads it page by page).
MAX_DOCUMENT_CHARS = 400_000


def scope_for(state: dict[str, Any], checks: dict[str, Any] | None) -> evidence.EvidenceScope:
    """The scope of one review from its frozen review state (``agent_runtime._review_context``): the submission items,
    the bound inputs (with type and label when known) and the criteria."""
    return evidence.EvidenceScope(items=[dict(i) for i in state.get("items") or []], bound=[dict(b) for b in state.get("bound_inputs") or []],
                                  criteria=[dict(c) for c in state.get("criteria") or []], checks=checks)


def _document_text(db: Session, fv: FileVersion) -> str | None:
    from .source_reader import file_version_text

    text = file_version_text(db, fv)
    return None if text is None else text[:MAX_DOCUMENT_CHARS]


def version_facts(db: Session, project_id: uuid.UUID, vid: str) -> dict[str, Any] | None:
    """Plain facts of one version of the project (``ai/evidence.py`` documents the shape); None when it is gone."""
    try:
        v = db.get(AssetVersion, uuid.UUID(vid))
    except (TypeError, ValueError):
        return None
    asset = db.get(Asset, v.asset_id) if v is not None else None
    if v is None or asset is None or asset.project_id != project_id:
        return None
    asset_type = asset.type
    row = assets.specialised(db, v)
    facts: dict[str, Any] = {"asset_version_id": str(v.id), "asset_type": asset_type, "name": asset.name,
                             "label": assets.human_label(db, asset_type, row, v), "labels": assets.labels_of(db, v)}
    if row is None:
        return facts
    if isinstance(row, FileVersion):
        text = _document_text(db, row)
        if text is not None:
            facts["text"] = text
    if isinstance(row, ModelRevision):
        facts["metrics"] = dict(row.metrics or {})
        facts["holes"] = [h for h in row.holes or [] if isinstance(h, dict)]
    checks = getattr(row, "checks", None)
    if isinstance(checks, dict) and checks:
        facts["checks"] = checks
    summary: dict[str, Any] = {}
    for key in ("status", "check_status", "acceptance_status", "completeness", "material"):
        val = getattr(row, key, None)
        if isinstance(val, str):
            summary[key] = val
    verification = getattr(row, "verification", None)
    if isinstance(verification, dict) and verification:
        summary["verification"] = {k: verification.get(k) for k in list(verification)[:12]}
    if isinstance(row, ModelRevision) and row.errors:
        summary["build_errors"] = [clip(str(e), 200) for e in row.errors[:5]]
    if summary:
        facts["summary"] = summary
    return facts


def run_evidence_tool(name: str, args: Any, scope: evidence.EvidenceScope, project_id: uuid.UUID) -> evidence.EvidenceResult:
    """One evidence tool call in its own read transaction; never raises (a crash is a ``tool_error`` result)."""
    try:
        with session_scope() as db:
            return evidence.run(name, args, scope, lambda vid: version_facts(db, project_id, vid))
    except Exception as exc:  # noqa: BLE001 - a read problem is a tool result, never an attempt crash
        log.exception("evidence tool %s failed", name)
        return evidence.fail(name, "tool_error", clip(f"{type(exc).__name__}: {exc}", 500))
