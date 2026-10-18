"""Saved CAD results and conflict-safe, append-only undo across the existing domain services."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import conflict, not_found
from ..models import (AiOperation, Assembly, AssemblyRevision, Asset, AssetVersion, Drawing,
                      DrawingDocument, DrawingDocumentVersion, ModelRevision, NodeRun, Part, Project, User, WorkflowRun)
from ..security import project_access
from . import assets
from .audit import audit

_TYPES = {
    "part_model": (ModelRevision, "part_model", Part, "part_id"),
    "part_drawing": (Drawing, "drawing", Part, "part_id"),
    "assembly": (AssemblyRevision, "assembly_model", Assembly, "assembly_id"),
    "drawing_document": (DrawingDocumentVersion, "drawing_document", DrawingDocument, "document_id"),
}


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _exact_output(db: Session, op: AiOperation, kind: str, row_id: Any) -> tuple[str, Any, AssetVersion | None] | None:
    model, asset_type, owner_model, owner_field = _TYPES[kind]
    ident = _uuid(row_id)
    row = db.get(model, ident) if ident else None
    owner = db.get(owner_model, getattr(row, owner_field)) if row else None
    if owner is None or owner.project_id != op.project_id:
        return None
    if kind in ("part_model", "part_drawing") and op.part_id is not None and owner.id != op.part_id:
        return None
    return kind, row, assets.version_for(db, asset_type, row.id)


def _saved_output(db: Session, op: AiOperation) -> tuple[str, Any, AssetVersion | None] | None:
    result = op.result or {}
    # A plan is not a CAD result until its existing apply service has saved it.
    if op.kind == "assembly_modify":
        application = result.get("application") or {}
        if application.get("state") != "applied":
            return None
        return _exact_output(db, op, "assembly", application.get("revision_id"))
    if result.get("drawing_document_version_id"):
        return _exact_output(db, op, "drawing_document", result["drawing_document_version_id"])
    wanted = {"model_create": "part_model", "model_modify": "part_model", "drawing_reconstruct": "part_model",
              "drawing_generate": "part_drawing", "assembly_create": "assembly"}.get(op.kind)
    if wanted is None:
        return None
    _, asset_type, _, _ = _TYPES[wanted]
    # These snapshots identify the principal output of this completion, while items intentionally
    # retain RunOutput history across checkpoint attempts. Never infer the principal from list order.
    if wanted == "assembly" and isinstance(result.get("chain"), dict):
        ids = {_uuid((step.get("value") or {}).get("assembly_revision_id"))
               for step in result["chain"].get("steps") or [] if step.get("node_id") == "assembly"}
        return _exact_output(db, op, wanted, ids.pop()) if len(ids) == 1 else None
    if wanted == "part_drawing" and "drawings" in result:
        ids = {_uuid(d.get("drawing_id")) for d in result.get("drawings") or [] if isinstance(d, dict)}
        return _exact_output(db, op, wanted, ids.pop()) if len(ids) == 1 else None
    candidates = {}
    for item in result.get("items") or []:
        vid = _uuid(item.get("asset_version_id"))
        version = db.get(AssetVersion, vid) if vid else None
        asset = db.get(Asset, version.asset_id) if version else None
        if asset is None or asset.type != asset_type or asset.project_id != op.project_id:
            continue
        saved = _exact_output(db, op, wanted, getattr(version, assets.KIND_FIELD[asset_type]))
        if saved is not None:
            candidates[saved[1].id] = saved
    if wanted == "part_model" and op.run_id is not None:
        nodes = list(db.scalars(select(NodeRun).join(WorkflowRun, WorkflowRun.id == NodeRun.run_id)
            .where(NodeRun.run_id == op.run_id, WorkflowRun.project_id == op.project_id,
                   NodeRun.node_type.in_(("model_3d", "drawing_reconstruct")))))
        if nodes:
            ids = {_uuid(((node.outputs or {}).get("model") or {}).get("revision_id")) for node in nodes}
            # A later external retry cannot move a historical result to an output absent from its saved items.
            return candidates.get(ids.pop()) if len(ids) == 1 else None
    # Hashless legacy results with one principal version remain supported; an ambiguous history is not a locator.
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _base_id(db: Session, kind: str, row: Any, version: AssetVersion | None) -> uuid.UUID | None:
    if kind != "part_drawing":
        return row.parent_id
    if version is None:
        return None
    previous = db.scalar(select(AssetVersion).where(AssetVersion.asset_id == version.asset_id,
                                                   AssetVersion.version_no < version.version_no)
                         .order_by(AssetVersion.version_no.desc()).limit(1))
    return previous.drawing_id if previous else None


def _head_id(db: Session, kind: str, owner_id: uuid.UUID) -> uuid.UUID | None:
    if kind == "part_drawing":
        from .drawings import latest

        head = latest(db, owner_id)
        return head.id if head else None
    if kind == "drawing_document":
        return db.scalar(select(DrawingDocument.head_version_id).where(DrawingDocument.id == owner_id))
    model, _, _, owner_field = _TYPES[kind]
    return db.scalar(select(model.id).where(getattr(model, owner_field) == owner_id)
                     .order_by(model.rev_no.desc()).limit(1))


def _artifacts(kind: str, row: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name in ("step", "glb", "mesh", "svg", "dxf", "dwg", "parts_zip"):
        sha = getattr(row, f"{name}_sha", None)
        if sha:
            out.append({"kind": name, "sha256": sha})
    files = [f for page in row.pages or [] for f in page.get("files") or []] if kind == "part_drawing" else getattr(row, "files", []) or []
    for file in files:
        sha = file.get("sha256")
        if not sha or any(f["sha256"] == sha for f in out):
            continue
        out.append({"kind": file.get("format") or file.get("kind") or "file", "sha256": sha,
                    **({"filename": file["path"]} if file.get("path") else {})})
    return out


def _changes(db: Session, kind: str, row: Any, base_id: uuid.UUID | None) -> list[dict[str, Any]]:
    if kind == "part_model" and base_id:
        from .revisions import diff

        base = db.get(ModelRevision, base_id)
        return diff(base, row)["ops"] if base else []
    if kind == "assembly":
        return copy.deepcopy((row.change or {}).get("ops") or [])
    if kind == "drawing_document":
        return copy.deepcopy(row.commands or [])
    return [{"kind": "create" if base_id is None else "replace", "version_id": str(row.id)}]


def _card(db: Session, saved: tuple[str, Any, AssetVersion | None], *, event_id: str) -> dict[str, Any]:
    kind, row, version = saved
    _, asset_type, _, owner_field = _TYPES[kind]
    owner_id = getattr(row, owner_field)
    base_id = _base_id(db, kind, row, version)
    undo = {"eligible": base_id is not None, "expected_head": str(row.id)}
    if base_id is None:
        undo["reason"] = "no_previous_version"
    return {"event_id": event_id, "object_type": kind, "object_id": str(owner_id),
            "base_version_id": str(base_id) if base_id else None, "version_id": str(row.id),
            "version_label": f"D{version.version_no}" if kind == "part_drawing" and version else assets.human_label(db, asset_type, row, version),
            "asset_version_id": str(version.id) if version else None,
            "requirements": copy.deepcopy(getattr(row, "requirements", None) or []),
            "changes": _changes(db, kind, row, base_id), "checks": copy.deepcopy(row.checks or {}),
            "artifacts": _artifacts(kind, row), "undo": undo}


def record(db: Session, op: AiOperation) -> None:
    """Persist one immutable result identity after a successful domain save; never synthesize a plan's output."""
    if op.status not in ("succeeded", "failed", "cancelled") or (op.result or {}).get("cad_result") is not None:
        return
    saved = _saved_output(db, op)
    if saved is not None:
        op.result = {**(op.result or {}), "cad_result": _card(db, saved, event_id=str(uuid.uuid4()))}


def projected_card(db: Session, op: AiOperation) -> dict[str, Any] | None:
    """Correct a legacy history-order card from exact saved output identity, without rewriting any record/event."""
    card = copy.deepcopy((op.result or {}).get("cad_result"))
    if not isinstance(card, dict):
        return None
    if (card.get("undo") or {}).get("restored") or op.status not in ("succeeded", "failed", "cancelled"):
        return card
    with db.no_autoflush:
        saved = _saved_output(db, op)
        if saved is not None and (card.get("object_type"), card.get("version_id")) != (saved[0], str(saved[1].id)):
            eid = uuid.uuid5(op.id, f"cad-result:{op.generation}:{saved[0]}:{saved[1].id}")
            return _card(db, saved, event_id=str(eid))
    return card


def result_out(db: Session, op: AiOperation) -> dict[str, Any]:
    """Read-only projection: polling does not register assets, mutate history, or rewrite the saved result."""
    result = copy.deepcopy(op.result or {})
    card = projected_card(db, op)
    if not isinstance(card, dict):
        return result
    result["cad_result"] = card
    kind, owner_id = card.get("object_type"), _uuid(card.get("object_id"))
    if kind not in _TYPES or owner_id is None:
        return result
    undo = card.get("undo") or {}
    reason = None
    if undo.get("restored"):
        reason = "already_undone"
    elif not card.get("base_version_id"):
        reason = "no_previous_version"
    else:
        with db.no_autoflush:
            head_id = _head_id(db, kind, owner_id)
        if str(head_id) != card.get("version_id"):
            reason = "head_changed"
        elif kind == "part_model":
            base = db.get(ModelRevision, _uuid(card["base_version_id"]))
            if base is None or base.status != "ready" or base.mesh_only:
                reason = "restore_unavailable"
    undo["eligible"] = reason is None
    if reason:
        undo["reason"] = reason
    else:
        undo.pop("reason", None)
    card["undo"] = undo
    return result


def undo(db: Session, user: User, op: AiOperation, expected_head: uuid.UUID) -> AiOperation:
    """Lock project → object → operation, save the restore and its idempotency record in the same transaction."""
    if op.user_id != user.id:
        raise not_found("AI operation")
    project_access(db, user, op.project_id, "design")
    db.scalar(select(Project).where(Project.id == op.project_id).with_for_update(read=True)
              .execution_options(populate_existing=True))
    project_access(db, user, op.project_id, "design")
    card = projected_card(db, op)
    if not isinstance(card, dict) or card.get("object_type") not in _TYPES:
        raise conflict("not_applied", "this operation has no saved CAD result to undo")
    kind = card["object_type"]
    _, asset_type, owner_model, _ = _TYPES[kind]
    owner_id = _uuid(card.get("object_id"))
    owner = db.scalar(select(owner_model).where(owner_model.id == owner_id).with_for_update()
                      .execution_options(populate_existing=True))
    if owner is None or owner.project_id != op.project_id:
        raise not_found("CAD object")
    if getattr(owner, "archived_at", None) is not None:
        raise conflict("object_archived", "restore the archived CAD object before undoing a change")
    op = db.scalar(select(AiOperation).where(AiOperation.id == op.id).with_for_update()
                   .execution_options(populate_existing=True))
    assert op is not None
    card = projected_card(db, op)
    if not card or card.get("object_type") != kind or card.get("object_id") != str(owner_id):
        raise conflict("result_changed", "the operation result changed; reload it")
    if str(expected_head) != card["version_id"]:
        raise conflict("head_changed", "the undo request does not match the operation's saved version")
    if (card.get("undo") or {}).get("restored"):
        return op  # committed restore already exists, including after a lost HTTP response or worker restart
    if not card.get("base_version_id"):
        raise conflict("no_previous_version", "the first version has no previous version to restore")
    if _head_id(db, kind, owner_id) != expected_head:
        raise conflict("head_changed", "the CAD object has later changes; they cannot be overwritten by undo")
    if kind in ("part_model", "part_drawing"):
        from .cad_admission import check_free

        check_free(db, user, owner_id, exclude_operation_id=op.id)
    base_id = uuid.UUID(card["base_version_id"])
    with db.begin_nested():
        if kind == "part_model":
            from .revisions import restore

            restored = restore(db, part_id=owner_id, user=user, base_revision_id=expected_head,
                               revision_id=base_id, execution_policy=op.execution_policy)
        elif kind == "part_drawing":
            from .drawings import restore

            restored = restore(db, part_id=owner_id, user=user, base_drawing_id=expected_head,
                               drawing_id=base_id, policy=op.execution_policy)
        elif kind == "assembly":
            from .assemblies import restore_revision

            restored = restore_revision(db, assembly_id=owner_id, user=user, base_revision_id=expected_head,
                                        revision_id=base_id, policy=op.execution_policy)
        else:
            from .drawing_documents import restore

            restored = restore(db, document_id=owner_id, user=user, base_version_id=expected_head,
                               version_id=base_id, execution_policy=op.execution_policy)
        version = assets.version_for(db, asset_type, restored.id)
        card["event_id"] = str(uuid.uuid4())
        card["undo"] = {"eligible": False, "reason": "already_undone", "expected_head": str(expected_head),
                        "restored": {"version_id": str(restored.id),
                                     "version_label": f"D{version.version_no}" if kind == "part_drawing" and version else assets.human_label(db, asset_type, restored, version),
                                     "asset_version_id": str(version.id) if version else None}}
        result = {**(op.result or {}), "cad_result": card}
        if kind in ("assembly", "drawing_document") and isinstance(result.get("application"), dict):
            result["application"] = {**result["application"], "state": "undone", "undo_revision_id": str(restored.id)}
        op.result = result
        from . import operation_events
        operation_events.emit(db, op, phase="undone")
        audit(db, action="ai.cad.undo", subject_type="ai_operation", subject_id=op.id, user_id=user.id,
              project_id=op.project_id, data={"object_type": kind, "object_id": str(owner_id),
                                            "produced_version_id": str(expected_head), "restored_from_id": str(base_id),
                                            "restored_version_id": str(restored.id)})
        db.flush()
    return op
