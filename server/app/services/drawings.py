"""Cloud-generated drawing packages from an exact model revision (design §4, §6).

``generate`` is the one producer (workspace route, workflow ``drawing`` node, agent ``drawing`` tool):

1. ``make_drawing_package`` from the revision's spec (+ base STEP) and its frozen requirement list;
2. ``check_package`` re-reads every page against the revision's STEP bytes and requirements; a failed
   check raises 422 ``drawing_check_failed`` and nothing is saved;
3. one native DWG R2000 per page through the LibreDWG adapter (separate processes, LibreDWG + ezdwg
   readback). Runtime missing -> the package is saved with ``checks.dwg.status = "unavailable"``;
   conversion / readback failure -> saved without DWG files, ``status = "failed"`` with the evidence.
   A DXF is never labelled DWG;
4. one savepoint: every file and report as a blob, the ``Drawing`` row (pages, manifest, manifest_sha256,
   checks; ``svg_sha`` / ``dxf_sha`` = page-1 alias) and its asset version.

``package_entries`` is the one export path (``GET /drawings/{id}/package.zip``, the workflow ``export``
node, the agent ``export`` tool): every manifest file of every page, the check reports and
``manifest.json`` (canonical JSON; its sha256 is ``manifest_sha256``). Hashes are verified on the way out.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import checks_ledger
from ..config import get_settings
from ..domain.hashing import canonical_json, sha256_hex
from ..errors import AppError, bad_request, conflict, not_found
from ..models import Asset, AssetVersion, Drawing, ModelRevision, Part, Project, User
from ..storage import read_blob, save_blob
from . import assets
from .audit import audit
from .revisions import KERNEL_LOCK, kernel, lock_part, spec_of

MANIFEST_SCHEMA = "partroute.drawing_package/1"
LEGACY_SCHEMA = "partroute.drawing_package/legacy"
DWG_REPORT_SCHEMA = "partroute.dwg_report/1"
DWG_TARGET = "AC1015"
CONTENT_TYPES = {"svg": "image/svg+xml", "dxf": "image/vnd.dxf", "dwg": "image/vnd.dwg"}
REPORT_TYPE = "application/json"
REPORT_PATHS = {"annotation": "checks/annotation.json", "dwg": "checks/dwg.json"}
MANIFEST_PATH = "manifest.json"

# Drawing config keys every entry accepts: the workspace (``POST /revisions/{id}/drawings`` body.config, all
# PackageConfig fields) and the agent ``drawing`` tool (these keys). Workspace-only: ``title`` (title-block
# overrides; an agent never relabels part number / author). Workflow ``drawing`` node: ``DrawingParams``
# (views, scale, projection, section); hidden_lines / dimensions wait for editor widgets.
CONFIG_KEYS = ("views", "scale", "projection", "hidden_lines", "dimensions", "section")
WORKSPACE_ONLY_KEYS = ("title",)
# Export ``include`` keys that select a drawing; the package is always exported whole.
EXPORT_KEYS = frozenset({"svg", "dxf", "dwg", "drawing"})

_ACADVER = re.compile(rb"\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_LEGACY_VIEWS = ("front", "top", "right", "iso")  # Drawing.views keeps the page-1 ViewInfo list of make_drawing


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _json_bytes(data: Any) -> bytes:
    return canonical_json(data).encode("utf-8")


def _normalised(data: Any) -> Any:
    """The JSON value exactly as the canonical text states it (stored manifests hash back to their sha)."""
    return json.loads(canonical_json(data))


def _strip_timing(value: Any) -> Any:
    """Drop wall-clock fields (``seconds``) so the same input gives the same report bytes."""
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items() if k != "seconds"}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


def _dxf_version(data: bytes) -> str | None:
    m = _ACADVER.search(data[:4096])
    return m.group(1).decode() if m else None


def _report_brief(report: Any) -> dict[str, Any]:
    """What a caller needs to locate an annotation failure: page, check, expected / measured."""
    fails = report.failures()
    return {
        "status": report.status,
        "validator_version": report.validator_version,
        "failed": len(fails),
        "items": [f.model_dump(mode="json") for f in fails[:50]],
        "pages": [{"index": p.index, "role": p.role} for p in report.pages],
    }


@dataclass
class _DwgOutcome:
    status: str  # passed | failed | unavailable
    files: dict[int, bytes] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] | None = None


def _runtime_identity(runtime: Any) -> dict[str, Any] | None:
    if not runtime.available:
        return None
    return {"libredwg_version": runtime.libredwg_version, "fingerprint": runtime.fingerprint,
            "independent_reader": runtime.independent_reader, "host": runtime.host}


def _dwg_runtime() -> Any:
    from ..cad.dwg import dwg_runtime

    return dwg_runtime(get_settings().libredwg_bin_dir)


def _write_dwg_pages(pages: list[Any], *, semantic: bool = True) -> _DwgOutcome:
    """DWG R2000 per page. Any failure keeps every DWG out of the package (no partial set).

    ``semantic=False`` (Turbo, ``drawing.dwg_semantic`` skipped): the readbacks do not run, the conversion must still
    write a non-empty AC1015 file whose hash matches; the outcome status is ``unverified`` (never ``passed``)."""
    from ..cad.dwg import DwgError, write_dwg

    runtime = _dwg_runtime()
    ident = _runtime_identity(runtime)
    if not runtime.available:
        return _DwgOutcome("unavailable", summary={"status": "unavailable", "reason": runtime.reason or "DWG runtime unavailable"})
    if semantic and not runtime.independent_reader:
        reason = "the independent DWG reader (ezdwg) is not installed; DWG output needs both readbacks"
        return _DwgOutcome("unavailable", summary={"status": "unavailable", "reason": reason}, runtime=ident)
    timeout = get_settings().dwg_timeout_s
    files: dict[int, bytes] = {}
    results: list[dict[str, Any]] = []
    # generation reliability N: the readbacks below are the drawing.dwg_semantic check — in the ledger like any other
    (checks_ledger.record if semantic else checks_ledger.record_skipped)("drawing.dwg_semantic")
    for page in pages:
        try:
            res = write_dwg(page.dxf, target=DWG_TARGET, runtime=runtime, timeout_s=timeout, require_independent=semantic,
                            semantic_readback=semantic)
            sha = hashlib.sha256(res.dwg).hexdigest()
            if (semantic and not res.verified) or res.output.version != DWG_TARGET or res.output.sha256 != sha or not res.dwg:
                raise DwgError("dwg_readback_mismatch", "the DWG writer returned an unverified result",
                               {"verified": res.verified, "version": res.output.version})
        except DwgError as exc:
            if exc.code == "dwg_runtime_unavailable":
                return _DwgOutcome("unavailable", summary={"status": "unavailable", "reason": exc.message}, runtime=ident)
            report = {
                "schema": DWG_REPORT_SCHEMA, "status": "failed", "target": DWG_TARGET, "runtime": ident,
                "failed_page": page.index, "code": exc.code, "message": exc.message,
                "details": _strip_timing(json.loads(json.dumps(exc.details, default=str))), "pages": results,
            }
            summary = {"status": "failed", "code": exc.code, "message": exc.message, "page": page.index}
            return _DwgOutcome("failed", report=_normalised(report), summary=summary, runtime=ident)
        files[page.index] = res.dwg
        results.append({"index": page.index, "source_dxf_sha256": res.source.sha256, "dwg_sha256": sha,
                        "result": _strip_timing(res.model_dump(mode="json"))})
    if not semantic:
        skipped = {"check": "drawing.dwg_semantic", "status": "skipped_turbo"}
        report = {"schema": DWG_REPORT_SCHEMA, "status": "unverified", "target": DWG_TARGET, "runtime": ident, "pages": results,
                  "semantic_readback": skipped}
        summary = {"status": "unverified", "verified": False, "target": DWG_TARGET, "pages": len(results), "semantic_readback": skipped}
        return _DwgOutcome("unverified", files=files, report=_normalised(report), summary=summary, runtime=ident)
    report = {"schema": DWG_REPORT_SCHEMA, "status": "passed", "target": DWG_TARGET, "runtime": ident, "pages": results}
    summary = {"status": "passed", "verified": True, "target": DWG_TARGET, "pages": len(results)}
    return _DwgOutcome("passed", files=files, report=_normalised(report), summary=summary, runtime=ident)


def _title(part: Part, project: Project, rev: ModelRevision, user: User, overrides: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "part_number": part.number,
        "part_name": part.name,
        "revision": f"R{rev.rev_no}",
        "material": part.material,
        "author": user.name,
        "date": rev.created_at.date().isoformat() if rev.created_at else "",
        "project": project.code,
        **(overrides or {}),
    }


def _requirements_sha(reqs: list[Any] | None) -> str | None:
    if reqs is None:
        return None
    from ..cad.requirements import requirements_sha256

    try:
        return requirements_sha256(reqs)
    except Exception:  # noqa: BLE001 — a stored list that no longer validates still gets a stable identity
        return sha256_hex(reqs)


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #


def template_notes(checks: Any) -> list[str]:
    """Drawing notes of a revision made from a part template (while its ops are unedited): what was adopted and how
    honest the shape is — the spec label, representation level, procurement, standard and every template note (for a
    spring: diameters, coils, pitch, free / installed length, end form and what the static shape omits)."""
    tpl = checks.get("template") if isinstance(checks, dict) else None
    if not isinstance(tpl, dict) or tpl.get("modified_ops") or tpl.get("removed_ops"):
        return []
    head = (f"PART TEMPLATE {tpl.get('template_id')} v{tpl.get('template_version')}: {tpl.get('spec_label')}; "
            f"REPRESENTATION {str(tpl.get('representation') or '').upper()}; PROCUREMENT {str(tpl.get('procurement') or '').upper()}"
            + (f"; STANDARD {tpl['standard']}" if tpl.get("standard") else "") + ".")
    return [head] + [f"{str(n).rstrip('.')}." for n in tpl.get("notes") or []]


def generate(
    db: Session,
    *,
    rev: ModelRevision,
    user: User,
    config: dict[str, Any] | None = None,
    node_run_id: uuid.UUID | None = None,
    policy: str | None = None,
    restored_from: Drawing | None = None,
    parent_drawing: Drawing | None = None,
) -> Drawing:
    """``policy`` is the caller's frozen execution policy (None = standard). Turbo (W7): the annotation check
    (``drawing.annotation``) and the DWG semantic readback (``drawing.dwg_semantic``) are skipped and recorded as
    ``skipped_turbo``; the package must still be generated and a written DWG must be a real non-empty AC1015 file."""
    from .. import policy as exec_policy
    from ..cad.annotation_check import VALIDATOR_VERSION, check_package
    from ..cad.drawing import DrawingError, PackageConfig, make_drawing_package

    pol = exec_policy.parse_policy(policy)
    turbo = exec_policy.is_turbo(pol)

    if rev.mesh_only:
        raise bad_request("mesh_reference", "mesh references have no exact geometry; drawings need a STEP or modeled revision")
    if rev.status != "ready":
        raise AppError(422, "revision_not_ready", "a failed revision cannot be drawn")
    part = lock_part(db, rev.part_id)
    project = db.get(Project, part.project_id) if part else None
    assert part is not None and project is not None
    cfg_data = dict(config or {})
    title = _title(part, project, rev, user, cfg_data.pop("title", None))
    try:
        cfg = PackageConfig.model_validate({**cfg_data, "title": title})
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError and friends
        raise bad_request("invalid_drawing_config", str(exc)[:300])
    spec = spec_of(rev)
    try:
        base = read_blob(db, spec.base.blob_sha256)[0] if spec.base else None
        step_bytes = read_blob(db, rev.step_sha)[0] if rev.step_sha else None  # the checker's reference
    except FileNotFoundError:
        raise bad_request("source_missing", "the revision's STEP or imported base file is missing from storage")
    reqs = list(rev.requirements) if rev.requirements is not None else None
    try:
        with KERNEL_LOCK:
            pkg = make_drawing_package(spec, cfg, base_step=base, requirements=reqs, notes=template_notes(rev.checks) or None)
            checked = exec_policy.run_check("drawing.annotation", pol, lambda: {
                "report": check_package(pkg.pages, step_bytes=step_bytes, requirements=reqs, features=rev.features)})
            kernel_version = kernel().KERNEL_VERSION
    except DrawingError as exc:
        raise AppError(422, "drawing_failed", str(exc)[:500])
    report = checked.get("report")
    if turbo and (not pkg.pages or not all(p.svg and p.dxf for p in pkg.pages)):  # the check that would catch it is skipped
        raise AppError(422, "drawing_failed", "the generator returned an empty drawing package")
    if report is not None and report.status != "passed":
        n = len(report.failures())
        raise AppError(422, "drawing_check_failed", f"the drawing package failed the annotation check ({n} item(s))",
                       {"report": _report_brief(report)})
    dwg = _write_dwg_pages(pkg.pages, semantic=not turbo)

    # ---- persist: one savepoint, nothing remains when any write fails
    with db.begin_nested():
        pages_out: list[dict[str, Any]] = []
        manifest_pages: list[dict[str, Any]] = []
        for page in pkg.pages:
            stem = f"pages/p{page.index}_{page.role}"
            files = [
                {"format": "svg", "sha256": save_blob(db, page.svg, CONTENT_TYPES["svg"]), "bytes": len(page.svg), "path": f"{stem}.svg"},
                {"format": "dxf", "sha256": save_blob(db, page.dxf, CONTENT_TYPES["dxf"]), "bytes": len(page.dxf),
                 "version": _dxf_version(page.dxf), "path": f"{stem}.dxf"},
            ]
            if page.index in dwg.files:
                data = dwg.files[page.index]
                files.append({"format": "dwg", "sha256": save_blob(db, data, CONTENT_TYPES["dwg"]), "bytes": len(data),
                              "version": DWG_TARGET, "path": f"{stem}.dwg"})
            files = [{k: v for k, v in f.items() if v is not None} for f in files]
            manifest_pages.append({"index": page.index, "role": page.role, "title": page.title, "files": files})
            pages_out.append({
                "index": page.index, "role": page.role, "title": page.title, "files": files, "scale": page.scale,
                "views": [v.model_dump(mode="json") for v in page.views],
                "dimensions": [d.model_dump(mode="json") for d in page.dimensions],
                "callouts": list(page.callouts), "hatches": page.hatches,
                "section": page.section.model_dump(mode="json") if page.section else None,
            })
        if report is not None:
            annotation = report.summary()
            ann_sha = save_blob(db, _json_bytes(annotation), REPORT_TYPE)
            ann_check: dict[str, Any] = {"status": report.status, "validator_version": report.validator_version,
                                         "report_sha256": ann_sha, "report_path": REPORT_PATHS["annotation"]}
        else:  # Turbo: skipped, no report and never a pass
            annotation = exec_policy.skipped("drawing.annotation")
            ann_check = dict(annotation)
        dwg_check: dict[str, Any] = {"status": dwg.status}
        for k in ("reason", "code"):
            if k in dwg.summary:
                dwg_check[k] = dwg.summary[k]
        dwg_row: dict[str, Any] = dict(dwg.summary)
        if dwg.report is not None:
            dwg_sha = save_blob(db, _json_bytes(dwg.report), REPORT_TYPE)
            dwg_check.update(report_sha256=dwg_sha, report_path=REPORT_PATHS["dwg"])
            dwg_row["report_sha256"] = dwg_sha
            if dwg.status == "failed":
                dwg_row["report"] = dwg.report
        source: dict[str, Any] = {
            "model_revision_id": str(rev.id), "rev_no": rev.rev_no, "step_sha256": rev.step_sha,
            "build_key": rev.build_key, "requirements_sha256": _requirements_sha(reqs),
        }
        if pkg.build_key != rev.build_key:
            source["drawn_build_key"] = pkg.build_key  # rebuilt by another kernel; the check compared it with the STEP
        manifest_data: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "source": source,
            "generator": {**pkg.generator, "kernel_version": kernel_version, "annotation_validator": VALIDATOR_VERSION,
                          "dwg_runtime": dwg.runtime},
            "sheet": pkg.sheet,
            "pages": manifest_pages,
            "checks": {"annotation": ann_check, "dwg": dwg_check},
            "coverage": pkg.coverage,
        }
        if restored_from is not None:
            manifest_data["restored_from"] = {"drawing_id": str(restored_from.id),
                                                "manifest_sha256": restored_from.manifest_sha256}
        extra_checks: dict[str, Any] = {}
        if turbo:
            from .verification import assumption_refs

            # The package says it is not verified (download, export) and which revision assumptions it rests on.
            refs = assumption_refs(rev)
            manifest_data["verification"] = exec_policy.verification(pol, "skipped_turbo", "not_requested", refs)
            extra_checks = {"execution_policy": pol, "assumptions": refs}
        manifest = _normalised(manifest_data)
        p1 = pkg.pages[0]
        d = Drawing(
            part_id=part.id,
            model_revision_id=rev.id,
            kind="generated",
            svg_sha=manifest_pages[0]["files"][0]["sha256"],
            dxf_sha=manifest_pages[0]["files"][1]["sha256"],
            config=cfg.model_dump(mode="json"),
            views=[{"name": v.name, "scale": v.scale, "bbox": v.bbox} for v in p1.views if v.name in _LEGACY_VIEWS],
            pages=pages_out,
            manifest=manifest,
            manifest_sha256=sha256_hex(manifest),
            checks={"annotation": annotation, "dwg": dwg_row,
                    "warnings": [w.model_dump(mode="json") for w in pkg.warnings], **extra_checks},
            node_run_id=node_run_id,
            created_by=user.id,
        )
        db.add(d)
        db.flush()
        version = assets.register_drawing(db, d)  # derived_from the model revision's exact asset version
        if parent_drawing is not None:
            parent_version = assets.ensure_version(db, "drawing", parent_drawing.id)
            version.parent_version_id = parent_version.id if parent_version else None
        audit(db, action="drawing.generate", subject_type="drawing", subject_id=d.id, user_id=user.id, project_id=part.project_id,
              data={"revision": rev.rev_no, "pages": len(pages_out), "manifest_sha256": d.manifest_sha256, "dwg": dwg.status})
    return d


def latest(db: Session, part_id: uuid.UUID) -> Drawing | None:
    """Drawing head follows its asset lineage, including multiple saves within one transaction."""
    version = db.scalar(select(AssetVersion).join(Asset, Asset.id == AssetVersion.asset_id)
                        .where(Asset.part_id == part_id, Asset.type == "drawing")
                        .order_by(AssetVersion.version_no.desc()).limit(1))
    if version is not None:
        return db.get(Drawing, version.drawing_id)
    return db.scalar(select(Drawing).where(Drawing.part_id == part_id)
                     .order_by(Drawing.created_at.desc(), Drawing.id.desc()).limit(1))


def restore(db: Session, *, part_id: uuid.UUID, user: User, base_drawing_id: uuid.UUID,
            drawing_id: uuid.UUID, policy: str | None = None) -> Drawing:
    """Rebuild a generated package, or hash-verify and copy an uploaded drawing, into a new asset version."""
    part = lock_part(db, part_id)
    head = latest(db, part.id)
    if head is None or head.id != base_drawing_id:
        raise conflict("head_changed", "the drawing changed after the operation finished")
    target = db.get(Drawing, drawing_id)
    if target is None or target.part_id != part.id:
        raise not_found("drawing")
    package_entries(db, target)  # refuse missing or corrupted source artifacts before making any new row
    if target.kind == "generated":
        rev = db.get(ModelRevision, target.model_revision_id) if target.model_revision_id else None
        if rev is None or rev.part_id != part.id:
            raise not_found("source model revision")
        restored = generate(db, rev=rev, user=user, config=target.config, policy=policy,
                            restored_from=target, parent_drawing=head)
    else:
        if target.file_version_id:
            from ..models import FileVersion

            source = db.get(FileVersion, target.file_version_id)
            if source is None:
                raise not_found("source drawing file")
            _verified(db, source.blob_sha256, "source drawing file")
        package = copied_package(target, target.model_revision_id)
        if package["manifest"] is not None:
            package["manifest"]["restored_from"] = {"drawing_id": str(target.id), "manifest_sha256": target.manifest_sha256}
            package["manifest_sha256"] = sha256_hex(package["manifest"])
        restored = Drawing(part_id=part.id, model_revision_id=target.model_revision_id, kind=target.kind,
                           file_version_id=target.file_version_id, svg_sha=target.svg_sha, dxf_sha=target.dxf_sha,
                           config=target.config, views=target.views, created_by=user.id, **package)
        db.add(restored)
        db.flush()
        version = assets.register_drawing(db, restored)
        parent = assets.ensure_version(db, "drawing", head.id)
        version.parent_version_id = parent.id if parent else None
    audit(db, action="drawing.restore", subject_type="drawing", subject_id=restored.id, user_id=user.id,
          project_id=part.project_id, data={"restored_from_id": str(target.id), "parent_id": str(head.id),
                                          "model_revision_id": str(target.model_revision_id) if target.model_revision_id else None})
    db.flush()
    return restored


# --------------------------------------------------------------------------- #
# copy (parts.copy): same files, manifest re-bound to the copied revision
# --------------------------------------------------------------------------- #


def copied_package(d: Drawing, new_revision_id: uuid.UUID | None) -> dict[str, Any]:
    """Package columns for a copy of ``d`` bound to ``new_revision_id``. Files and check reports are the
    same bytes; the manifest names the copied revision and the original package, so its hash differs
    and nothing bound to the original (approvals) applies to the copy."""
    if d.manifest is None:
        return {"pages": list(d.pages or []), "manifest": None, "manifest_sha256": None, "checks": d.checks}
    manifest = json.loads(json.dumps(d.manifest))
    manifest["source"]["model_revision_id"] = str(new_revision_id) if new_revision_id else None
    manifest["copied_from"] = {"drawing_id": str(d.id), "manifest_sha256": d.manifest_sha256}
    manifest = _normalised(manifest)
    return {"pages": list(d.pages or []), "manifest": manifest, "manifest_sha256": sha256_hex(manifest), "checks": d.checks}


# --------------------------------------------------------------------------- #
# export: one path for package.zip, workflow export and agent export
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PackageEntry:
    path: str  # inside the package folder
    sha256: str
    data: bytes
    kind: str  # page | report | manifest
    page: int | None = None
    format: str | None = None


def _verified(db: Session, sha: str, what: str) -> bytes:
    try:
        data, _ = read_blob(db, sha)
    except FileNotFoundError:
        raise AppError(409, "package_file_missing", f"{what} is missing from storage", {"sha256": sha})
    if hashlib.sha256(data).hexdigest() != sha:
        raise AppError(409, "package_file_mismatch", f"{what} does not match its manifest hash", {"sha256": sha})
    return data


def package_entries(db: Session, d: Drawing) -> list[PackageEntry]:
    """Every file of the drawing package plus ``manifest.json``, hash-verified (design §6 export)."""
    out: list[PackageEntry] = []
    if d.manifest is None:  # rows from before the package: one page, SVG + DXF, no checks
        files = []
        for fmt, sha in (("svg", d.svg_sha), ("dxf", d.dxf_sha)):
            if sha:
                data = _verified(db, sha, f"page 1 {fmt}")
                path = f"pages/p1_overview.{fmt}"
                out.append(PackageEntry(path, sha, data, "page", 1, fmt))
                files.append({"format": fmt, "sha256": sha, "bytes": len(data), "path": path})
        legacy = {"schema": LEGACY_SCHEMA, "drawing_id": str(d.id),
                  "source": {"model_revision_id": str(d.model_revision_id) if d.model_revision_id else None},
                  "pages": [{"index": 1, "role": "overview", "files": files}], "checks": None}
        body = _json_bytes(legacy)
        out.append(PackageEntry(MANIFEST_PATH, hashlib.sha256(body).hexdigest(), body, "manifest"))
        return out
    body = _json_bytes(d.manifest)
    sha = hashlib.sha256(body).hexdigest()
    if sha != d.manifest_sha256:
        raise AppError(409, "package_manifest_mismatch", "the stored manifest does not match its hash",
                       {"manifest_sha256": d.manifest_sha256, "computed": sha})
    for page in d.manifest.get("pages") or []:
        for f in page.get("files") or []:
            data = _verified(db, f["sha256"], f"page {page['index']} {f['format']}")
            out.append(PackageEntry(f["path"], f["sha256"], data, "page", page["index"], f["format"]))
    for name, check in (d.manifest.get("checks") or {}).items():
        if isinstance(check, dict) and check.get("report_sha256"):
            data = _verified(db, check["report_sha256"], f"{name} report")
            out.append(PackageEntry(check.get("report_path") or REPORT_PATHS.get(name, f"checks/{name}.json"),
                                    check["report_sha256"], data, "report"))
    out.append(PackageEntry(MANIFEST_PATH, sha, body, "manifest"))
    return out


def package_folder(db: Session, d: Drawing) -> str:
    """Folder name of a package inside a multi-item export zip, e.g. ``PL-100_R2_D1``."""
    part = db.get(Part, d.part_id)
    rev = db.get(ModelRevision, d.model_revision_id) if d.model_revision_id else None
    label = assets.human_label(db, "drawing", d)
    name = "_".join(x for x in (part.number if part else "drawing", f"R{rev.rev_no}" if rev else "", label) if x)
    return _SAFE.sub("-", name).strip("-.") or "drawing"


def write_package(z: zipfile.ZipFile, entries: list[PackageEntry], folder: str = "") -> list[dict[str, Any]]:
    """Add the entries to an open zip (fixed timestamps) and return their export-manifest lines."""
    lines = []
    for e in entries:
        name = f"{folder}/{e.path}" if folder else e.path
        info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, e.data)
        lines.append({"file": name, "sha256": e.sha256, "kind": e.kind, **({"page": e.page, "format": e.format} if e.kind == "page" else {})})
    return lines


def export_drawing(db: Session, z: zipfile.ZipFile, d: Drawing) -> list[dict[str, Any]]:
    """The whole package of ``d`` into an export zip under its folder (workflow and agent export)."""
    folder = package_folder(db, d)
    lines = write_package(z, package_entries(db, d), folder)
    for line in lines:
        line.update(drawing_id=str(d.id), manifest_sha256=d.manifest_sha256)
    return lines


def package_zip(db: Session, d: Drawing) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        write_package(z, package_entries(db, d))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #


def _checks_out(d: Drawing) -> dict[str, Any] | None:
    if d.checks is None:
        return None
    ann = d.checks.get("annotation") or {}
    items = ann.get("items") or []
    manifest_checks = (d.manifest or {}).get("checks") or {}
    dwg = {k: v for k, v in (d.checks.get("dwg") or {}).items() if k != "report"}
    return {
        "annotation": {"status": ann.get("status"), "validator_version": ann.get("validator_version"),
                       "failed": sum(1 for i in items if i.get("status") == "failed"),
                       "warnings": sum(1 for i in items if i.get("status") == "warning"),
                       "report_sha256": (manifest_checks.get("annotation") or {}).get("report_sha256")},
        "dwg": dwg,
    }


def page_count(d: Drawing) -> int:
    return len(d.pages or []) or (1 if (d.svg_sha or d.dxf_sha) else 0)


def dwg_status(d: Drawing) -> str | None:
    return ((d.checks or {}).get("dwg") or {}).get("status")


def drawing_brief(d: Drawing) -> dict[str, Any]:
    """Outputs shared by the workflow ``drawing`` node and the agent ``drawing`` tool."""
    from .verification import drawing_verification

    return {
        "drawing_id": str(d.id),
        "pages": page_count(d),
        "manifest_sha256": d.manifest_sha256,
        "annotation_status": ((d.checks or {}).get("annotation") or {}).get("status"),
        "dwg_status": dwg_status(d),
        "verification": drawing_verification(d),
    }


def drawing_out(d: Drawing, rev: ModelRevision | None = None) -> dict[str, Any]:
    from .verification import drawing_verification

    return {
        "verification": drawing_verification(d),
        "id": str(d.id),
        "part_id": str(d.part_id),
        "model_revision_id": str(d.model_revision_id) if d.model_revision_id else None,
        "rev_no": rev.rev_no if rev else None,
        "kind": d.kind,
        "file_version_id": str(d.file_version_id) if d.file_version_id else None,
        "svg_sha": d.svg_sha,
        "dxf_sha": d.dxf_sha,
        "config": d.config,
        "views": d.views,
        "pages": d.pages or [],
        "page_count": page_count(d),
        "manifest": d.manifest,
        "manifest_sha256": d.manifest_sha256,
        "checks": _checks_out(d),
        "dwg_status": dwg_status(d),
        "coverage": (d.manifest or {}).get("coverage"),
        "node_run_id": str(d.node_run_id) if d.node_run_id else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }
