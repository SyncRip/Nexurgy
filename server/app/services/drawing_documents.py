"""Standalone 2D drawing documents (W6): create, edit, restore, calibrate, export.

A :class:`DrawingDocument` belongs to a project only (no part, no model revision). Every save writes a new
immutable :class:`DrawingDocumentVersion` (a database trigger refuses UPDATE) against the head version the client
based its edit on; a stale base is a 409 ``version_conflict`` (optimistic concurrency, the document row is locked
while the version is written). Undo is :func:`restore`: a new version with an older version's content.

Producers (W8 calls these; each returns the new version, caller commits):

- :func:`create_document` — ``mode`` empty | text (AI, ``ai/drafting_agent``) | file (DXF / DWG file version,
  faithful re-drawing with readings, conflicts and the unsupported-object inventory) | image (located underlay,
  calibration unknown in standard, an assumed scale with an Assumption in Turbo);
- :func:`apply_edits` — edit commands (``cad/drafting`` :data:`Command`);
- :func:`restore`, :func:`calibrate`.

Saving one version (:func:`_save_version`), both policies:

1. structure (always): unique ids, known layers / blocks, finite coordinates;
2. DXF R2010 written and re-loaded (always; an empty or unreadable DXF fails the save, nothing is stored);
3. ``drafting.rules`` through ``policy.run_check`` (standard; Turbo records ``skipped_turbo``);
4. DWG R2000 through the LibreDWG pipeline (``cad/dwg.write_dwg``: conversion plus LibreDWG and ezdwg readback,
   fail closed). A failed or unavailable DWG keeps the version with ``output_status = partial`` and the evidence
   in ``dwg`` — never a DXF renamed to DWG. Turbo (generation reliability N): the readbacks belong to the closed
   ``drawing.dwg_semantic`` and do not run; the conversion must still write a non-empty AC1015 file (``dwg.status``
   ``unverified``);
5. ``drawing.dwg_semantic`` through ``policy.run_check`` (standard): the DWG read back (LibreDWG, same engine) and
   re-imported, entity ids / types / dimension values / texts / layer visibility / title block compared;
6. blobs, manifest (canonical JSON, its sha256 is the content identity of the asset version), asset version
   ``drawing_document`` and audit, in the caller's transaction.
"""

from __future__ import annotations

import collections
import hashlib
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import policy as pol
from ..ai import limits
from ..ai.cad_tool_contracts import PreparedTextDrawing
from ..cad import drafting
from ..config import get_settings
from ..domain.hashing import canonical_json, sha256_hex
from ..errors import AppError, conflict, not_found
from ..models import DrawingDocument, DrawingDocumentVersion, File, FileVersion, User
from ..storage import read_blob, save_blob
from . import assets
from .audit import audit

MANIFEST_SCHEMA = "partroute.drawing_document/1"
DWG_REPORT_SCHEMA = "partroute.dwg_report/1"
DWG_TARGET = "AC1015"
CONTENT_TYPES = {"dxf": "image/vnd.dxf", "dwg": "image/vnd.dwg", "json": "application/json"}
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "BMP": "image/bmp", "TIFF": "image/tiff", "GIF": "image/gif"}
MAX_IMAGE_PIXELS = 80_000_000
MAX_TEXT_CHARS = limits.DRAWING_TEXT_CHARS  # ai/limits.py
DEFAULT_DPI = 96.0
#: Wall-clock budget of one DXF / DWG import (W0 §7: like a generated page); an import past it saves nothing.
IMPORT_BUDGET_S = 120.0
#: Import errors that keep their own code (limits, not an unreadable file).
_IMPORT_LIMIT_CODES = ("too_many_entities", "import_budget_exceeded")
Mode = Literal["empty", "text", "file", "image"]
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalised(data: Any) -> Any:
    return json.loads(canonical_json(data))


def _json_bytes(data: Any) -> bytes:
    return canonical_json(data).encode("utf-8")


def _parse_policy(value: str | None) -> str:
    try:
        return pol.parse_policy(value)
    except ValueError as exc:
        raise AppError(422, "invalid_execution_policy", str(exc)) from exc


def _drafting_error(exc: drafting.DraftingError, status: int = 422) -> AppError:
    return AppError(status, exc.code, exc.message, exc.details)


def state_of(v: DrawingDocumentVersion) -> drafting.DraftState:
    """The editable content frozen in a version."""
    return drafting.DraftState.model_validate({
        "sheets": v.sheets, "layers": v.layers, "entities": v.entities, "blocks": v.blocks, "underlays": v.underlays,
        "next_entity_no": v.next_entity_no, "provenance": v.provenance, "conflicts": v.conflicts,
        "assumptions": v.assumptions, "open_questions": v.open_questions,
    })


# --------------------------------------------------------------------------- #
# DWG
# --------------------------------------------------------------------------- #


@dataclass
class _Dwg:
    status: str  # passed | failed | unavailable
    data: bytes | None = None
    info: dict[str, Any] = field(default_factory=dict)
    restructured: dict[str, Any] = field(default_factory=dict)  # dwg.py N0 loss details (not persisted as such)


def _runtime() -> Any:
    from ..cad.dwg import dwg_runtime

    return dwg_runtime(get_settings().libredwg_bin_dir)


def _strip_timing(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items() if k != "seconds"}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


def _write_dwg(dxf: bytes, *, semantic: bool = True) -> _Dwg:
    """One DWG R2000 through the LibreDWG adapter (fail closed: both readbacks must match).

    ``semantic=False`` (Turbo: ``drawing.dwg_semantic`` is closed, generation reliability N): the readbacks are not run
    at all — the conversion must still write a non-empty AC1015 file whose hash matches (generation failure detection);
    the status is ``unverified``, never ``passed``."""
    from ..cad.dwg import DwgError, write_dwg
    from .drawing_input import compact

    runtime = _runtime()
    ident = {"libredwg_version": runtime.libredwg_version, "fingerprint": runtime.fingerprint,
             "independent_reader": runtime.independent_reader} if runtime.available else None
    if not runtime.available:
        return _Dwg("unavailable", info={"status": "unavailable", "reason": runtime.reason or "DWG runtime unavailable"})
    if semantic and not runtime.independent_reader:
        return _Dwg("unavailable", info={"status": "unavailable", "runtime": ident,
                                         "reason": "the independent DWG reader (ezdwg) is not installed; DWG output needs both readbacks"})
    try:
        res = write_dwg(dxf, target=DWG_TARGET, runtime=runtime, timeout_s=get_settings().dwg_timeout_s,
                        require_independent=semantic, readback=semantic)
        sha = hashlib.sha256(res.dwg).hexdigest()
        if (semantic and not res.verified) or res.output.version != DWG_TARGET or res.output.sha256 != sha or not res.dwg:
            raise DwgError("dwg_readback_mismatch" if semantic else "dwg_conversion_failed",
                           "the DWG writer returned an unverified result" if semantic else "the converter returned an empty or mislabelled DWG",
                           {"verified": res.verified, "version": res.output.version})
    except DwgError as exc:
        status = "unavailable" if exc.code == "dwg_runtime_unavailable" else "failed"
        return _Dwg(status, info={"status": status, "code": exc.code, "message": exc.message, "runtime": ident,
                                  "details": compact(_strip_timing(json.loads(json.dumps(exc.details, default=str))))})
    readback = [{"reader": c.reader, "reader_version": c.reader_version, "status": c.status, "entities": c.entities,
                 "entities_matched": c.entities_matched, "dimensions": c.dimensions, "texts": c.texts} for c in res.readback]
    # Text the writer restructured (dwg.py N0: Chinese MTEXT exploded, long / quoted lines split) — the semantic
    # check accepts exactly these as split pieces, anything else still fails.
    restructured = next((i.details for i in res.losses if i.code == "dwg_cjk_text_restructured"), None) or {}
    status = "passed" if semantic else "unverified"
    info = {"status": status, "target": DWG_TARGET, "runtime": ident, "verified": bool(semantic and res.verified), "readback": readback,
            "losses": [i.code for i in res.losses], "warnings": [i.code for i in res.warnings]}
    if restructured:
        info["text_restructured"] = {"mtext_exploded": len(restructured.get("mtext_exploded") or []),
                                     "text_split": len(restructured.get("text_split") or [])}
    return _Dwg(status, res.dwg, info=info, restructured=restructured)


def _text_chars(texts: list[str]) -> collections.Counter[str]:
    return collections.Counter(c for t in texts for c in t if not c.isspace())


def _dwg_semantic(dwg: bytes, state: drafting.DraftState, restructured: dict[str, Any] | None = None) -> dict[str, Any]:
    """Standard-mode check: read the DWG back and compare it with the version content.

    ``restructured`` is the writer's ``dwg_cjk_text_restructured`` loss: those text entities come back as several
    pieces without their ids, so missing text entities are accepted only when the unexpected text pieces carry
    exactly their characters (whitespace aside)."""
    from ..cad.dwg import DwgError, read_dwg

    out: dict[str, Any] = {"check": "drawing.dwg_semantic", "reader": "LibreDWG dwg2dxf + cad/drafting import (same engine as the writer)",
                           "independent_reader": "ODA File Converter not installed (W0 §8): same-engine readback only"}
    try:
        rd = read_dwg(dwg, runtime=_runtime(), timeout_s=get_settings().dwg_timeout_s)
        imp = drafting.import_dxf(rd.dxf)
    except (DwgError, drafting.DraftingError) as exc:
        return {**out, "status": "failed", "code": getattr(exc, "code", "dwg_read_failed"), "message": getattr(exc, "message", str(exc))}
    a = {e.id: e for e in state.entities}
    b = {e.id: e for e in imp.state.entities}
    mismatches: list[str] = []
    missing, unexpected = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    emptied: set[str] = set()  # leaders / MTEXT kept by id whose exploded text came back as separate pieces
    if restructured and (restructured.get("mtext_exploded") or restructured.get("text_split")):
        textual = ("text", "mtext", "leader")
        gone = [eid for eid in missing if a[eid].type in textual]
        emptied = {eid for eid in set(a) & set(b) if a[eid].type in ("mtext", "leader") and a[eid].text.strip() and not b[eid].text.strip()}
        pieces = [eid for eid in unexpected if b[eid].type == "text"]
        lost = [a[eid].text for eid in gone] + [a[eid].text for eid in sorted(emptied)]
        if lost and _text_chars(lost) == _text_chars([b[eid].text for eid in pieces]):
            missing = [eid for eid in missing if eid not in gone]
            unexpected = [eid for eid in unexpected if eid not in pieces]
        else:
            emptied = set()
    for eid in missing[:20]:
        mismatches.append(f"missing {eid} ({a[eid].type})")
    for eid in unexpected[:20]:
        mismatches.append(f"unexpected {eid} ({b[eid].type})")
    for eid in sorted(set(a) & set(b)):
        x, y = a[eid], b[eid]
        if x.type != y.type:
            mismatches.append(f"{eid}: {x.type} read back as {y.type}")
        elif x.type == "dimension":
            mx, my = drafting.measure(x)[0], drafting.measure(y)[0]
            if x.dimtype != y.dimtype or abs(mx - my) > 1e-3:
                mismatches.append(f"{eid}: {x.dimtype} {drafting.fmt(mx, 4)} read back as {y.dimtype} {drafting.fmt(my, 4)}")
            elif (x.text_override or None) != (y.text_override or None):
                mismatches.append(f"{eid}: dimension text {x.text_override!r} read back as {y.text_override!r}")
        elif x.type in ("text", "mtext", "leader") and x.text.strip() != y.text.strip() and eid not in emptied:
            mismatches.append(f"{eid}: text {x.text[:40]!r} read back as {y.text[:40]!r}")
    hidden_a = {lay.name for lay in state.layers if not lay.visible}
    hidden_b = {lay.name for lay in imp.state.layers if not lay.visible}
    if hidden_a != hidden_b:
        mismatches.append(f"hidden layers {sorted(hidden_a)} read back as {sorted(hidden_b)}")
    for sh in state.sheets:
        if not sh.draw_frame:
            continue
        other = next((s for s in imp.state.sheets if s.id == sh.id), None)
        if other is None:
            mismatches.append(f"sheet {sh.id} missing")
        elif other.title_block != sh.title_block or other.bom != sh.bom:
            mismatches.append(f"sheet {sh.id}: title block / BOM differ")
    return {**out, "status": "failed" if mismatches else "passed", "entities": len(a), "matched": len(set(a) & set(b)),
            "mismatches": mismatches[:40]}


def _aggregate(policy: str, checks: dict[str, dict[str, Any]]) -> str:
    if pol.is_turbo(policy):
        return "skipped_turbo"
    statuses = [c.get("status") for c in checks.values()]
    if "failed" in statuses:
        return "failed"
    if any(s not in ("passed",) for s in statuses):
        return "unknown"
    return "passed"


def _verify_dxf(data: bytes) -> None:
    """The DXF must exist and load back (Turbo too)."""
    if not data:
        raise AppError(422, "dxf_export_failed", "the DXF export is empty")
    try:
        import ezdxf

        ezdxf.read(io.StringIO(data.decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 — any failure to re-load is an export failure
        raise AppError(422, "dxf_export_failed", f"the exported DXF does not load back: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- #
# save one version
# --------------------------------------------------------------------------- #


def _labels(base: list[str], state: drafting.DraftState, unsupported: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[str]:
    out = [label for label in base if label in ("simulated", "imported")]
    if any(s.get("kind") in ("file_version", "image") for s in sources) and "imported" not in out:
        out.append("imported")
    if state.assumptions:
        out.append("assumed")
    if any(not row.get("preserved", False) for row in unsupported):
        out.append("source_objects_dropped")
    if any(row.get("preserved") and "approximated" in row.get("handling", "") for row in unsupported):
        out.append("approximated")
    return out


def _save_version(
    db: Session,
    doc: DrawingDocument,
    *,
    state: drafting.DraftState,
    parent: DrawingDocumentVersion | None,
    origin: str,
    policy: str,
    sources: list[dict[str, Any]],
    unsupported: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    user: User,
    labels: list[str],
    units: dict[str, Any] | None = None,
    ai: dict[str, Any] | None = None,
    note: str = "",
    restored_from: DrawingDocumentVersion | None = None,
) -> DrawingDocumentVersion:
    try:
        notes = drafting.validate_structure(state)
    except drafting.DraftingError as exc:
        raise _drafting_error(exc) from exc
    drafting.refresh_provenance(state)
    units = units or {"units_known": True}
    units_known = bool(units.get("units_known", True))
    scale_status = drafting.scale_status(state, units_known=units_known)
    try:
        export = drafting.export_dxf(state)
    except Exception as exc:  # noqa: BLE001 — ezdxf raises several types; nothing is saved
        raise AppError(422, "dxf_export_failed", f"the drawing could not be written as DXF: {type(exc).__name__}: {str(exc)[:200]}") from exc
    _verify_dxf(export.dxf)

    checks: dict[str, dict[str, Any]] = {}
    checks["drafting.rules"] = pol.run_check("drafting.rules", policy, lambda: drafting.check_rules(state, units_known=units_known))
    # the DWG is converted from a variant with ASCII aliases for layer / block names beyond ASCII (R2000 limit)
    encoding = export.encoding
    dwg_source = export.dxf
    if drafting.needs_ascii_names(state):
        variant = drafting.export_dxf(state, ascii_names=True)
        encoding, dwg_source = variant.encoding, variant.dxf
    # Turbo (generation reliability N): the DWG semantic readback is closed, so the writer's readbacks do not run either;
    # the conversion itself (a non-empty AC1015 file) is still required.
    dwg = _write_dwg(dwg_source, semantic=not pol.is_turbo(policy))
    if dwg_source is not export.dxf:
        dwg.info["source_dxf_sha256"] = hashlib.sha256(dwg_source).hexdigest()
    if dwg.status == "passed" and dwg.data:
        data = dwg.data
        restructured = dwg.restructured
        checks["drawing.dwg_semantic"] = pol.run_check("drawing.dwg_semantic", policy, lambda: _dwg_semantic(data, state, restructured))
    elif pol.is_turbo(policy):
        checks["drawing.dwg_semantic"] = pol.run_check("drawing.dwg_semantic", policy, dict)  # skipped_turbo, never called
    else:
        checks["drawing.dwg_semantic"] = {"check": "drawing.dwg_semantic", "status": "unknown", "reason": "no DWG was written"}
    check_status = _aggregate(policy, checks)
    output_status = "succeeded" if dwg.status in ("passed", "unverified") else "partial"

    version_no = doc.head_version_no + 1
    stem = _SAFE.sub("-", f"{doc.number}_V{version_no}").strip("-.") or "drawing"
    dxf_sha = save_blob(db, export.dxf, CONTENT_TYPES["dxf"])
    files: list[dict[str, Any]] = [{"format": "dxf", "sha256": dxf_sha, "bytes": len(export.dxf), "version": "AC1024", "path": f"{stem}.dxf"}]
    dwg_sha = None
    if dwg.data:
        dwg_sha = save_blob(db, dwg.data, CONTENT_TYPES["dwg"])
        files.append({"format": "dwg", "sha256": dwg_sha, "bytes": len(dwg.data), "version": DWG_TARGET, "path": f"{stem}.dwg"})
    content = drafting.content_dict(state)
    content_hash = sha256_hex({k: content[k] for k in ("sheets", "layers", "entities", "blocks", "underlays")})
    # W0 §2.2: unverified while a check is open or an AI / rule assumption has no person's decision in this version
    # (calibrate / resolve_conflict / confirm_assumption commands record one).
    pending = sorted(k for k, c in checks.items() if c.get("status") in ("skipped_turbo", "pending", "failed", "unknown"))
    verification = pol.verification(policy, check_status, "not_requested", assumptions=state.assumptions,  # type: ignore[arg-type]
                                    pending_checks=pending)
    calibration = {
        "status": scale_status, **units,
        "underlays": [{"id": u.id, **u.calibration.model_dump(mode="json")} for u in state.underlays],
    }
    vid = uuid.uuid4()
    manifest = _normalised({
        "schema": MANIFEST_SCHEMA,
        "document": {"id": str(doc.id), "project_id": str(doc.project_id), "number": doc.number, "title": doc.title},
        "version": {"id": str(vid), "version_no": version_no, "parent_id": str(parent.id) if parent else None, "origin": origin,
                    "restored_from_id": str(restored_from.id) if restored_from else None},
        "execution_policy": policy,
        "sources": [{k: s.get(k) for k in ("id", "kind", "file_version_id", "name", "sha256", "format", "dxf_sha256") if s.get(k) is not None}
                    for s in sources],
        "content_sha256": content_hash,
        "files": files,
        "underlays": [{"id": u.id, "sha256": u.sha256, "name": u.name, "content_type": u.content_type,
                       "calibration": u.calibration.status, "embedded_in_dxf": False} for u in state.underlays],
        "scale_status": scale_status,
        "encoding": encoding,
        "checks": {k: {"status": v.get("status")} for k, v in checks.items()},
        "dwg": {k: v for k, v in dwg.info.items() if k in ("status", "code", "message", "target", "verified", "reason")},
        "output_status": output_status,
        "verification": verification,
        "unsupported": [{k: row[k] for k in ("type", "count", "preserved", "handling")} for row in unsupported],
        "generator": {"drafting": drafting.DRAFTING_VERSION, "rules": drafting.RULES_VERSION,
                      "dwg_runtime": dwg.info.get("runtime")},
    })
    row = DrawingDocumentVersion(
        id=vid,
        document_id=doc.id,
        version_no=version_no,
        parent_id=parent.id if parent else None,
        restored_from_id=restored_from.id if restored_from else None,
        origin=origin,
        execution_policy=policy,
        sources=sources,
        sheets=content["sheets"],
        layers=content["layers"],
        entities=content["entities"],
        blocks=content["blocks"],
        underlays=content["underlays"],
        next_entity_no=state.next_entity_no,
        scale_status=scale_status,
        calibration=calibration,
        provenance=content["provenance"],
        conflicts=content["conflicts"],
        assumptions=content["assumptions"],
        open_questions=content["open_questions"],
        unsupported=unsupported,
        commands=commands,
        content_hash=content_hash,
        check_status=check_status,
        acceptance_status="not_requested",
        checks={**checks, "structure": {"status": "passed", "notes": notes}},
        verification=verification,
        output_status=output_status,
        files=files,
        dwg=dwg.info,
        dxf_sha=dxf_sha,
        dwg_sha=dwg_sha,
        manifest=manifest,
        manifest_sha256=sha256_hex(manifest),
        labels=_labels(labels, state, unsupported, sources),
        ai=ai,
        note=note[:2000],
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    doc.head_version_id = row.id
    doc.head_version_no = version_no
    doc.updated_at = func.now()
    db.flush()
    assets.register_drawing_document(db, row)
    audit(db, action="drawing_document.version", subject_type="drawing_document", subject_id=doc.id, user_id=user.id,
          project_id=doc.project_id, data={"version_no": version_no, "origin": origin, "execution_policy": policy,
                                           "check_status": check_status, "output_status": output_status,
                                           "manifest_sha256": row.manifest_sha256})
    return row


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


def _file_version(db: Session, project_id: uuid.UUID, file_version_id: uuid.UUID | str) -> tuple[FileVersion, File, bytes]:
    try:
        fv = db.get(FileVersion, uuid.UUID(str(file_version_id)))
    except ValueError:
        fv = None
    f = db.get(File, fv.file_id) if fv else None
    if fv is None or f is None or f.project_id != project_id:
        raise not_found("file version")
    try:
        data, _ = read_blob(db, fv.blob_sha256)
    except FileNotFoundError as exc:
        raise AppError(409, "source_missing", "the file's bytes are missing from storage", {"sha256": fv.blob_sha256}) from exc
    return fv, f, data


def _from_file(db: Session, project_id: uuid.UUID, file_version_id: uuid.UUID | str) -> tuple[drafting.DraftState, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    from .drawing_input import compact, drawing_format, dwg_to_dxf

    fv, f, data = _file_version(db, project_id, file_version_id)
    name = fv.original_name or f.name
    fmt = drawing_format(name, data)
    dwg_prov = None
    if fmt == "dwg":
        dxf, dwg_prov = dwg_to_dxf(db, data)
    else:
        dxf = data
    try:
        imp = drafting.import_dxf(dxf, source_id="S-1", budget_s=IMPORT_BUDGET_S)
    except drafting.DraftingError as exc:
        code = exc.code if exc.code.startswith("dxf") or exc.code in _IMPORT_LIMIT_CODES else "drawing_unreadable"
        raise AppError(422, code, f"{name}: {exc.message}", exc.details) from exc
    source = {
        "id": "S-1", "kind": "file_version", "file_version_id": str(fv.id), "file_id": str(f.id), "name": name,
        "sha256": fv.blob_sha256, "bytes": len(data), "format": fmt, "dxf_sha256": hashlib.sha256(dxf).hexdigest(),
        "dxf_role": "derived" if fmt == "dwg" else "original", "dwg_read": compact(dwg_prov) if dwg_prov else None,
        "import": {"counts": imp.counts, "own_file": imp.own_file, "warnings": imp.warnings[:20]},
    }
    return imp.state, source, imp.unsupported, {"units_known": imp.units_known, **imp.units}


def _image_info(data: bytes) -> tuple[str, int, int, float | None]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format or ""
            w, h = im.size
            dpi = im.info.get("dpi")
    except Exception as exc:  # noqa: BLE001 — PIL raises several types for non-images
        raise AppError(422, "unsupported_image", f"not a readable image: {type(exc).__name__}") from exc
    if fmt not in IMAGE_TYPES:
        raise AppError(422, "unsupported_image", f"image format {fmt or 'unknown'} is not supported (PNG, JPEG, WEBP, BMP, TIFF, GIF)")
    if w * h > MAX_IMAGE_PIXELS:
        raise AppError(422, "image_too_large", f"the image has {w}×{h} pixels; at most {MAX_IMAGE_PIXELS} are used as an underlay")
    d = None
    if isinstance(dpi, tuple) and dpi and float(dpi[0]) > 1:
        d = float(dpi[0])
    return IMAGE_TYPES[fmt], w, h, d


def _from_image(db: Session, project_id: uuid.UUID, file_version_id: uuid.UUID | str, policy: str,
                title_block: dict[str, str]) -> tuple[drafting.DraftState, dict[str, Any]]:
    fv, f, data = _file_version(db, project_id, file_version_id)
    content_type, w, h, dpi = _image_info(data)
    name = fv.original_name or f.name
    underlay = drafting.Underlay(id="U-1", sha256=fv.blob_sha256, name=name[:300], content_type=content_type, width_px=w, height_px=h,
                                 file_version_id=str(fv.id))
    state = drafting.new_state(drafting.Sheet(id="S1", title_block=title_block))
    if pol.is_turbo(policy):
        mm_per_px = 25.4 / (dpi or DEFAULT_DPI)
        basis = (f"no scale reference on the image; its DPI metadata ({drafting.fmt(dpi)} dpi) taken as 1:1" if dpi else
                 f"no scale reference and no DPI metadata; {drafting.fmt(DEFAULT_DPI)} dpi at 1:1 assumed")
        assumption = pol.Assumption(id="A-01", subject="U-1.scale", field="mm_per_px", adopted=round(mm_per_px, 9), unit="mm/px",
                                    basis=basis, alternatives=["calibrate with two points and a known distance"], affected=["U-1"],
                                    sources=["S-1"], author="rule", trigger="missing", at=_now())
        underlay = underlay.model_copy(update={"calibration": drafting.Calibration(status="assumed", mm_per_px=mm_per_px,
                                                                                   assumption_id="A-01")})
        state.assumptions.append(assumption.model_dump(mode="json"))
    state.underlays.append(underlay)
    source = {"id": "S-1", "kind": "image", "file_version_id": str(fv.id), "file_id": str(f.id), "name": name, "sha256": fv.blob_sha256,
              "bytes": len(data), "content_type": content_type, "width_px": w, "height_px": h, "dpi": dpi}
    return state, source


#: The model purpose of a text drawing (W1: frozen once per document creation).
AI_PURPOSE = "assistant"


class DraftingAiError(AppError):
    """A model failure of a text drawing. The AI call rows of the attempt are flushed (never committed) in the caller's
    session: the caller owns the transaction and decides whether to keep them (the API commits them, then answers
    with this error; a workflow node keeps them in its own transaction). ``details.diagnostic`` is W1's payload."""


def freeze_model(db: Session, model_config_id: uuid.UUID | str | None = None) -> dict[str, Any]:
    """W1: the explicit (or default) configuration frozen into a snapshot before any context is built; the snapshot,
    not the live default, is what the drafting agent runs on. Raises :class:`DraftingAiError` (503, diagnostic at
    stage resolve) when nothing usable is configured."""
    from ..ai import diagnostics
    from ..ai.providers.base import ProviderError
    from . import ai as ai_svc

    try:
        return ai_svc.freeze_model(db, AI_PURPOSE, model_config_id)
    except ProviderError as exc:
        diag = diagnostics.from_provider_error(exc, stage="resolve", purpose=AI_PURPOSE, call_ids=[])
        raise DraftingAiError(503, "ai_unavailable", exc.message, {"provider_code": exc.code, "diagnostic": diag}) from exc


def _request_text(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        raise AppError(422, "text_required", "describe the drawing")
    if len(text) > MAX_TEXT_CHARS:
        raise AppError(422, "text_too_long", f"the description has {len(text)} characters; at most {MAX_TEXT_CHARS}")
    return text


def _from_text(db: Session, *, text: str, policy: str, title: str,
               snapshot: dict[str, Any]) -> tuple[drafting.DraftState, dict[str, Any], dict[str, Any], list[str]]:
    from ..ai import diagnostics
    from .drafting_plans import prepare_text_drawing
    from ..ai.providers.base import ProviderError
    from .ai import provider_for, record_calls

    text = _request_text(text)
    try:
        provider, _ = provider_for(db, AI_PURPOSE, snapshot)  # the frozen snapshot, never a live re-pick
    except ProviderError as exc:
        diag = diagnostics.from_provider_error(exc, stage="resolve", purpose=AI_PURPOSE, snapshot=snapshot, call_ids=[])
        raise DraftingAiError(503, "ai_unavailable", exc.message, {"provider_code": exc.code, "diagnostic": diag}) from exc
    out = prepare_text_drawing(provider, text, title=title, execution_policy=policy, model_snapshot=snapshot, stop=lambda: None)
    rows = record_calls(db, out.calls, None)
    if out.error is not None or out.prepared is None:
        err = out.error or {"code": "invalid_drawing", "message": "no drawing", "details": {}}
        db.flush()  # the call ledger of the failed attempt stays in the caller's transaction (no commit here)
        ids = [str(r.id) for r in rows]
        needs_input = err["code"] == "needs_input"
        diag_code = "invalid_plan" if err["code"] in ("needs_input", "invalid_drawing") else err["code"]  # else a provider code
        diag = diagnostics.diagnostic(diag_code, message=err.get("message"), purpose=AI_PURPOSE, snapshot=snapshot, call_ids=ids,
                                      stage="validation" if diag_code == "invalid_plan" else None)
        raise DraftingAiError(422, "drafting_needs_input" if needs_input else "drafting_failed", err["message"],
                              {**(err.get("details") or {}), "simulated": out.simulated, "provider_code": err["code"], "call_ids": ids,
                               "diagnostic": diag})
    prepared = out.prepared
    return prepared.state, prepared.source, prepared.ai, prepared.labels


# --------------------------------------------------------------------------- #
# public producers
# --------------------------------------------------------------------------- #

_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,79}$")


def _next_number(db: Session, project_id: uuid.UUID) -> str:
    n = (db.scalar(select(func.count()).select_from(DrawingDocument).where(DrawingDocument.project_id == project_id)) or 0) + 1
    while True:
        number = f"DD-{n:04d}"
        # A concurrent text child can save under the shared project lock. Claim each number
        # transactionally before testing visibility, just as assembly creation does.
        claimed = db.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(f"drawing_document.number:{project_id}:{number}"))))
        if claimed and db.scalar(select(DrawingDocument.id).where(DrawingDocument.project_id == project_id, DrawingDocument.number == number)) is None:
            return number
        n += 1


def create_document(
    db: Session,
    *,
    project_id: uuid.UUID,
    user: User,
    title: str,
    mode: Mode = "empty",
    execution_policy: str | None = None,
    number: str | None = None,
    description: str = "",
    text: str | None = None,
    file_version_id: uuid.UUID | str | None = None,
    sheet: dict[str, Any] | None = None,
    model_config_id: uuid.UUID | str | None = None,
    model_snapshot: dict[str, Any] | None = None,
    prepared_text: PreparedTextDrawing | None = None,
) -> DrawingDocumentVersion:
    """A new document and its version 1. Permissions are the caller's (design on the project), and so is the
    transaction: nothing is committed here. ``mode="text"`` runs the drafting agent on the model frozen once before
    anything else (W1): ``model_snapshot`` when the caller already froze one (a run's snapshot), else the explicit
    ``model_config_id`` or the default; the snapshot is kept in the version's ``ai`` record. A model failure raises
    :class:`DraftingAiError` (W1 diagnostic) with the attempt's call rows flushed for the caller to keep."""
    policy = _parse_policy(execution_policy)
    if prepared_text is not None and (mode != "text" or prepared_text.execution_policy != policy):
        raise AppError(422, "prepared_drawing_mismatch", "prepared drawing mode/policy must match the save")
    title = (title or "").strip()[:200]
    if not title:
        raise AppError(422, "title_required", "a drawing document needs a title")
    if number is not None:
        number = number.strip()
        if not _NUMBER_RE.match(number):
            raise AppError(422, "invalid_number", "drawing numbers use letters, digits and . _ - / (at most 80)")
        if db.scalar(select(DrawingDocument.id).where(DrawingDocument.project_id == project_id, DrawingDocument.number == number)):
            raise conflict("number_taken", f"drawing number {number} is already used in this project", number=number)
    else:
        number = _next_number(db, project_id)
    title_block = {"title": title, "drawing_number": number}

    sources: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    labels: list[str] = []
    units: dict[str, Any] = {"units_known": True, "units": "mm"}
    ai = None
    if mode == "empty":
        try:
            sh = drafting.Sheet.model_validate({"id": "S1", **(sheet or {}), "title_block": title_block})
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
            raise AppError(422, "invalid_sheet", str(exc)[:300]) from exc
        state = drafting.new_state(sh)
    elif mode == "text":
        from .ai import is_snapshot

        _request_text(text)  # a request without text fails before any model is resolved
        if prepared_text is not None:
            if _request_text(text) != prepared_text.text:
                raise AppError(422, "prepared_drawing_mismatch", "prepared drawing text must match the save")
            state = prepared_text.state.model_copy(deep=True)
            source, ai, labels = dict(prepared_text.source), dict(prepared_text.ai), list(prepared_text.labels)
        else:
            snapshot = model_snapshot if is_snapshot(model_snapshot) else freeze_model(db, model_config_id)
            state, source, ai, labels = _from_text(db, text=text or "", policy=policy, title=title, snapshot=snapshot)
        sources.append(source)
        for sh in state.sheets:
            tb = dict(sh.title_block)
            tb.setdefault("title", title)
            tb.setdefault("drawing_number", number)
            state.sheets = [s.model_copy(update={"title_block": tb}) if s.id == sh.id else s for s in state.sheets]
    elif mode == "file":
        if not file_version_id:
            raise AppError(422, "file_version_required", "choose a DXF or DWG file")
        state, source, unsupported, units = _from_file(db, project_id, file_version_id)
        sources.append(source)
    elif mode == "image":
        if not file_version_id:
            raise AppError(422, "file_version_required", "choose an image file")
        state, source = _from_image(db, project_id, file_version_id, policy, title_block)
        sources.append(source)
    else:
        raise AppError(422, "invalid_mode", f"unknown mode {mode!r}")

    doc = DrawingDocument(project_id=project_id, number=number, title=title, description=(description or "")[:4000], created_by=user.id)
    db.add(doc)
    db.flush()
    origin = {"empty": "empty", "text": "text", "file": "file", "image": "image"}[mode]
    v = _save_version(db, doc, state=state, parent=None, origin=origin, policy=policy, sources=sources, unsupported=unsupported,
                      commands=[], user=user, labels=labels, units=units, ai=ai)
    audit(db, action="drawing_document.create", subject_type="drawing_document", subject_id=doc.id, user_id=user.id,
          project_id=project_id, data={"number": number, "mode": mode, "execution_policy": policy})
    return v


def _lock(db: Session, document_id: uuid.UUID) -> DrawingDocument:
    doc = db.scalar(select(DrawingDocument).where(DrawingDocument.id == document_id).with_for_update()
                    .execution_options(populate_existing=True))
    if doc is None:
        raise not_found("drawing document")
    if doc.archived_at is not None:
        raise conflict("document_archived", "the drawing document is archived; restore it before editing")
    return doc


def _head(db: Session, doc: DrawingDocument, base_version_id: uuid.UUID | str) -> DrawingDocumentVersion:
    try:
        base = uuid.UUID(str(base_version_id))
    except ValueError as exc:
        raise AppError(422, "invalid_base_version", "base_version_id is not a version id") from exc
    if doc.head_version_id != base:
        raise conflict("version_conflict", "the drawing changed since this edit started; reload the latest version and apply the edit again",
                       base_version_id=str(base), head_version_id=str(doc.head_version_id) if doc.head_version_id else None,
                       head_version_no=doc.head_version_no)
    head = db.get(DrawingDocumentVersion, base)
    assert head is not None
    return head


def apply_edits(db: Session, *, document_id: uuid.UUID, user: User, base_version_id: uuid.UUID | str, commands: list[dict[str, Any]],
                execution_policy: str | None = None, note: str = "", actor_kind: Literal["person", "ai"] = "person",
                ai: dict[str, Any] | None = None) -> DrawingDocumentVersion:
    """Apply edit commands to the head version; a new version or 409 ``version_conflict`` (never rebased). The policy
    is the request's own (default standard), never inherited from the head version (W0 §2)."""
    doc = _lock(db, document_id)
    head = _head(db, doc, base_version_id)
    policy = _parse_policy(execution_policy)
    try:
        if actor_kind == "ai":
            from ..ai.drafting_edit_agent import admit_commands
            parsed = admit_commands(commands)
        else:
            parsed = drafting.parse_commands(commands)
        if not parsed:
            raise drafting.DraftingError("no_commands", "nothing to save")
        state, _ = drafting.apply_commands(state_of(head), parsed, actor=str(user.id) if actor_kind == "person" else None,
                                          actor_kind=actor_kind, at=_now())
    except drafting.DraftingError as exc:
        raise _drafting_error(exc) from exc
    origin = "calibrate" if all(c.op == "calibrate" for c in parsed) else "edit"
    return _save_version(db, doc, state=state, parent=head, origin=origin, policy=policy, sources=list(head.sources),
                         unsupported=list(head.unsupported), commands=[c.model_dump(mode="json", by_alias=True) for c in parsed],
                         user=user, labels=list(head.labels), units=_units_of(head), ai=ai, note=note)


def calibrate(db: Session, *, document_id: uuid.UUID, user: User, base_version_id: uuid.UUID | str, underlay_id: str,
              p1_px: list[float], p2_px: list[float], distance_mm: float, note: str = "") -> DrawingDocumentVersion:
    return apply_edits(db, document_id=document_id, user=user, base_version_id=base_version_id, note=note,
                       commands=[{"op": "calibrate", "underlay_id": underlay_id, "p1_px": p1_px, "p2_px": p2_px, "distance_mm": distance_mm}])


def restore(db: Session, *, document_id: uuid.UUID, user: User, base_version_id: uuid.UUID | str, version_id: uuid.UUID | str,
            execution_policy: str | None = None, note: str = "") -> DrawingDocumentVersion:
    """Undo: a new version whose content is an older version's. History stays as it was."""
    doc = _lock(db, document_id)
    head = _head(db, doc, base_version_id)
    try:
        target = db.get(DrawingDocumentVersion, uuid.UUID(str(version_id)))
    except ValueError:
        target = None
    if target is None or target.document_id != doc.id:
        raise not_found("drawing document version")
    policy = _parse_policy(execution_policy)  # the request's own, default standard (never the head's)
    state = state_of(target)
    return _save_version(db, doc, state=state, parent=head, origin="restore", policy=policy, sources=list(target.sources),
                         unsupported=list(target.unsupported),
                         commands=[{"op": "restore", "version_id": str(target.id), "version_no": target.version_no}],
                         user=user, labels=list(target.labels), units=_units_of(target), ai=None,
                         note=note or f"restored V{target.version_no}", restored_from=target)


def _units_of(v: DrawingDocumentVersion) -> dict[str, Any]:
    cal = v.calibration or {}
    return {k: cal[k] for k in ("units_known", "units", "insunits", "mm_per_unit") if k in cal} or {"units_known": True}


def update_document(db: Session, *, document_id: uuid.UUID, user: User, title: str | None = None, description: str | None = None,
                    status: str | None = None, archived: bool | None = None) -> DrawingDocument:
    doc = db.scalar(select(DrawingDocument).where(DrawingDocument.id == document_id).with_for_update()
                    .execution_options(populate_existing=True))
    if doc is None:
        raise not_found("drawing document")
    changes: dict[str, Any] = {}
    if title is not None:
        t = title.strip()[:200]
        if not t:
            raise AppError(422, "title_required", "a drawing document needs a title")
        doc.title, changes["title"] = t, t
    if description is not None:
        doc.description, changes["description"] = description[:4000], True
    if status is not None:
        if status not in ("draft", "released"):
            raise AppError(422, "invalid_status", "status is draft or released")
        doc.status, changes["status"] = status, status
    if archived is not None:
        doc.archived_at = func.now() if archived else None
        changes["archived"] = archived
    audit(db, action="drawing_document.update", subject_type="drawing_document", subject_id=doc.id, user_id=user.id,
          project_id=doc.project_id, data=changes)
    db.flush()
    return doc


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def _verified(db: Session, sha: str, what: str) -> bytes:
    try:
        data, _ = read_blob(db, sha)
    except FileNotFoundError as exc:
        raise AppError(409, "file_missing", f"{what} is missing from storage", {"sha256": sha}) from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise AppError(409, "file_mismatch", f"{what} does not match its manifest hash", {"sha256": sha})
    return data


def version_file(db: Session, v: DrawingDocumentVersion, fmt: str) -> tuple[bytes, str, str]:
    """(bytes, file name, content type) of one export file of a version (dxf / dwg / manifest)."""
    if fmt == "manifest":
        return _json_bytes(v.manifest), "manifest.json", CONTENT_TYPES["json"]
    f = next((x for x in v.files or [] if x.get("format") == fmt), None)
    if f is None:
        reason = (v.dwg or {}).get("message") or (v.dwg or {}).get("reason") if fmt == "dwg" else None
        raise AppError(404, "file_not_available", f"version V{v.version_no} has no {fmt.upper()} file" + (f": {reason}" if reason else ""),
                       {"format": fmt, "dwg": v.dwg if fmt == "dwg" else None})
    return _verified(db, f["sha256"], f"V{v.version_no} {fmt}"), f["path"], CONTENT_TYPES[fmt]


def package_zip(db: Session, v: DrawingDocumentVersion) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in v.files or []:
            info = zipfile.ZipInfo(f["path"], date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, _verified(db, f["sha256"], f"V{v.version_no} {f['format']}"))
        for name, payload in (("checks.json", v.checks), ("manifest.json", v.manifest)):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, _json_bytes(payload))
    return buf.getvalue()


def underlay_bytes(db: Session, v: DrawingDocumentVersion, underlay_id: str) -> tuple[bytes, str]:
    u = next((x for x in v.underlays or [] if x.get("id") == underlay_id), None)
    if u is None:
        raise not_found("underlay")
    return _verified(db, u["sha256"], f"underlay {underlay_id}"), u.get("content_type") or "application/octet-stream"


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def version_brief(v: DrawingDocumentVersion) -> dict[str, Any]:
    ents = v.entities or []
    types: dict[str, int] = {}
    for e in ents:
        types[e.get("type", "?")] = types.get(e.get("type", "?"), 0) + 1
    return {
        "id": str(v.id),
        "document_id": str(v.document_id),
        "version_no": v.version_no,
        "label": f"V{v.version_no}",
        "parent_id": str(v.parent_id) if v.parent_id else None,
        "restored_from_id": str(v.restored_from_id) if v.restored_from_id else None,
        "origin": v.origin,
        "execution_policy": v.execution_policy,
        "check_status": v.check_status,
        "acceptance_status": v.acceptance_status,
        "output_status": v.output_status,
        "scale_status": v.scale_status,
        "verification": v.verification,
        "labels": list(v.labels or []),
        "dwg_status": (v.dwg or {}).get("status"),
        "files": v.files or [],
        "entity_count": len(ents),
        "entity_types": dict(sorted(types.items())),
        "conflicts_open": sum(1 for c in v.conflicts or [] if c.get("status") == "open"),
        "unsupported_count": sum(int(r.get("count", 0)) for r in v.unsupported or [] if not r.get("preserved")),
        "commands": [c.get("op") for c in v.commands or []],
        "note": v.note,
        "manifest_sha256": v.manifest_sha256,
        "content_hash": v.content_hash,
        "simulated": "simulated" in (v.labels or []),
        "created_by": str(v.created_by),
        "created_at": _iso(v.created_at),
    }


def version_out(v: DrawingDocumentVersion) -> dict[str, Any]:
    return {
        **version_brief(v),
        "sources": v.sources,
        "sheets": v.sheets,
        "layers": v.layers,
        "entities": v.entities,
        "blocks": v.blocks,
        "underlays": v.underlays,
        "next_entity_no": v.next_entity_no,
        "calibration": v.calibration,
        "provenance": v.provenance,
        "conflicts": v.conflicts,
        "assumptions": v.assumptions,
        "open_questions": v.open_questions,
        "unsupported": v.unsupported,
        "checks": v.checks,
        "dwg": v.dwg,
        "manifest": v.manifest,
        "ai": v.ai,
        "commands_detail": v.commands,
    }


def document_out(db: Session, doc: DrawingDocument, *, head: DrawingDocumentVersion | None = None, full: bool = False) -> dict[str, Any]:
    head = head if head is not None else (db.get(DrawingDocumentVersion, doc.head_version_id) if doc.head_version_id else None)
    return {
        "id": str(doc.id),
        "project_id": str(doc.project_id),
        "number": doc.number,
        "title": doc.title,
        "description": doc.description,
        "status": doc.status,
        "head_version_id": str(doc.head_version_id) if doc.head_version_id else None,
        "head_version_no": doc.head_version_no,
        "created_by": str(doc.created_by),
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
        "archived_at": _iso(doc.archived_at),
        "head": (version_out(head) if full else version_brief(head)) if head is not None else None,
    }


def list_documents(db: Session, project_id: uuid.UUID, *, include_archived: bool = False) -> list[dict[str, Any]]:
    q = select(DrawingDocument).where(DrawingDocument.project_id == project_id)
    if not include_archived:
        q = q.where(DrawingDocument.archived_at.is_(None))
    docs = db.scalars(q.order_by(DrawingDocument.updated_at.desc(), DrawingDocument.number)).all()
    heads = {v.id: v for v in db.scalars(select(DrawingDocumentVersion).where(
        DrawingDocumentVersion.id.in_([d.head_version_id for d in docs if d.head_version_id]))).all()} if docs else {}
    return [document_out(db, d, head=heads.get(d.head_version_id)) for d in docs]


def history(db: Session, doc: DrawingDocument) -> list[dict[str, Any]]:
    rows = db.scalars(select(DrawingDocumentVersion).where(DrawingDocumentVersion.document_id == doc.id)
                      .order_by(DrawingDocumentVersion.version_no.desc())).all()
    return [version_brief(v) for v in rows]


def get_version(db: Session, doc: DrawingDocument, ref: str) -> DrawingDocumentVersion:
    """A version of ``doc`` by number (``3`` / ``V3``) or id."""
    q = select(DrawingDocumentVersion).where(DrawingDocumentVersion.document_id == doc.id)
    m = re.fullmatch(r"[Vv]?(\d{1,6})", ref)
    if m:
        v = db.scalar(q.where(DrawingDocumentVersion.version_no == int(m.group(1))))
    else:
        try:
            v = db.scalar(q.where(DrawingDocumentVersion.id == uuid.UUID(ref)))
        except ValueError:
            v = None
    if v is None:
        raise not_found("drawing document version")
    return v
